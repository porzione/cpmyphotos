#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["dateparser", "pyyaml"]
# ///
"""Safely import photos from a card into a photo archive."""

from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from timeit import default_timer as timer

import dateparser
import yaml


HASH_CHUNK_SIZE = 1024 * 1024
METADATA_BATCH_SIZE = 50
METADATA_EXTENSIONS = {"jpg", "jpeg", "tif", "tiff", "png", "webp"}
TIMEZONE_RE = re.compile(r"^(?:Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)$")
LINK_UNSUPPORTED_ERRNOS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}
CONFIG_KEYS = {"copyright", "lens", "tz", "card_root", "archive", "gpx_dir", "gpx_name"}
DEFAULT_GPX_NAME = "%Y%m%d.gpx"


@dataclass(frozen=True)
class Layout:
    """Where files come from and where they go."""

    srcdir: Path
    folders: list[Path]  # scanned dirs: a card's DCIM folders, else [srcdir]
    card: Path | None
    recursive: bool
    template: str  # destination with {place} substituted; {year} is per file
    root: Path  # the fixed part of the template, before {year}


@dataclass(frozen=True)
class Metadata:
    """Metadata written into the copies; none of it means byte-exact copies."""

    copyright: str | None
    lens: str | None
    gpx: list[Path]
    camera_tz: str
    geosync: str | None
    require_gps: bool


@dataclass(frozen=True)
class Settings:
    """Resolved options for one run; argparse's namespace stays the raw input."""

    layout: Layout
    metadata: Metadata
    mode: int | None  # None preserves the source mode
    debug: bool


@dataclass
class PendingMetadataCopy:
    """A copy held under a temporary name until metadata work succeeds."""

    source: Path
    destination: Path
    temporary: Path
    write_basic_exif: bool
    source_digest: str


@dataclass
class Plan:
    """Everything resolved before the first write."""

    extensions: set[str]
    files: list[Path]
    targets: list[tuple[Path, Path]]
    newer_ns: int | None
    newer_reason: str
    newest_ns: int | None


@dataclass
class ImportState:
    """Shared state for one import run."""

    settings: Settings
    plan: Plan
    pending: list[PendingMetadataCopy]
    counters: dict[str, int]


def parse_mode(value: str) -> int:
    """Parse an octal file mode for argparse."""
    try:
        mode = int(value, 8)
    except ValueError as error:
        raise argparse.ArgumentTypeError("mode must be octal, for example 0644") from error
    if not 0 <= mode <= 0o777:
        raise argparse.ArgumentTypeError("mode must be between 0000 and 0777")
    return mode


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Safely copy photos from a card",
        epilog="Defaults come from $XDG_CONFIG_HOME/cpmyphotos.yaml "
               "(~/.config/cpmyphotos.yaml); see the README.",
    )
    parser.add_argument("place", nargs="?",
                        help="Value for {place} in the destination template")
    parser.add_argument("-D", "--debug", action="store_true", help="Show detailed commands")
    parser.add_argument("-N", "--dry-run", action="store_true",
                        help="Show what would be copied, then stop without writing")
    parser.add_argument("--config", type=Path, help="Config file (default: %(default)s)",
                        default=None)
    parser.add_argument("-s", "--srcdir", type=Path,
                        help="Source directory or card (default: the one mounted card)")
    parser.add_argument("-d", "--dstdir", type=Path,
                        help="Destination directory or template (default: 'archive' in config)")
    since = parser.add_mutually_exclusive_group()
    since.add_argument("-n", "--newer",
                       help="Copy files with an mtime newer than this date "
                            "(default for a card: newer than its last import)")
    since.add_argument("--all", action="store_true",
                       help="Copy the whole card, ignoring its last import")
    parser.add_argument("--allow-old-dates", action="store_true",
                        help="Allow card photos dated over a year ago")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Recurse and preserve the source directory structure")
    gps = parser.add_mutually_exclusive_group()
    gps.add_argument("-g", "--gpx", action="append", type=Path, help="GPX file (repeatable)")
    gps.add_argument("--no-gps", action="store_true",
                     help="Do not geotag, even with 'gpx_dir' in config")
    parser.add_argument(
        "--camera-tz", "--tz", dest="camera_tz", metavar="OFFSET",
        help="Camera timezone for GPX matching (default: 'tz' in config, else Z for UTC)",
    )
    parser.add_argument(
        "--geosync", metavar="SHIFT",
        help="Camera/GPS clock correction passed to ExifTool, for example +00:00:25",
    )
    parser.add_argument(
        "--require-gps", action="store_true",
        help="Treat files that cannot be matched to a GPX track as failed",
    )
    parser.add_argument("-C", dest="exif_copr",
                        help="EXIF copyright (default: 'copyright' in config; '' for none)")
    parser.add_argument("-L", dest="exif_lens",
                        help="EXIF lens model (default: 'lens' in config; '' for none)")
    parser.add_argument(
        "--mode", type=parse_mode, default=parse_mode("0644"), metavar="OCTAL",
        help="Destination file mode (default: 0644; use 'source' via --preserve-mode)",
    )
    parser.add_argument(
        "--preserve-mode", action="store_true",
        help="Preserve source permissions instead of applying --mode",
    )
    parser.add_argument(
        "--require-src-mount", action="store_true",
        help="Refuse to run unless --srcdir itself is a mounted filesystem "
             "(always on for a detected card)",
    )
    parser.add_argument(
        "--require-dst-mount", action="store_true",
        help="Refuse to run unless the fixed part of the destination is a mounted filesystem",
    )
    return parser


def load_extensions() -> set[str]:
    """Load accepted extensions from ext.json beside this script."""
    ext_path = Path(__file__).resolve().parent / "ext.json"
    with ext_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    try:
        extensions = set(config["img"]) | set(config["raw"])
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid extension configuration in {ext_path}") from error
    return {str(extension).lower().lstrip(".") for extension in extensions}


def xdg_dir(variable: str, default: str) -> Path:
    """Return an XDG base directory."""
    return Path(os.environ.get(variable) or Path.home() / default)


def load_config(path: Path | None) -> dict[str, str]:
    """Load the optional YAML config; a missing default config means no defaults."""
    explicit = path is not None
    if path is None:
        path = xdg_dir("XDG_CONFIG_HOME", ".config") / "cpmyphotos.yaml"
    if not path.is_file():
        if explicit:
            raise ValueError(f"config file does not exist: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"invalid config {path}: {error}") from error
    if not isinstance(config, dict):
        # A usage error like any other bad config, not a programming error.
        raise ValueError(f"invalid config {path}: expected key: value pairs")  # noqa: TRY004
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise ValueError(f"unknown keys in config {path}: {', '.join(sorted(unknown))}")
    return {key: str(value) for key, value in config.items() if value is not None}


def option(value: str | None, default: str | None) -> str | None:
    """Return a command-line value, or the config default when it was not given."""
    return default if value is None else value


def file_extension(path: Path) -> str:
    """Return a file's extension, lowercase and without the dot."""
    return path.suffix[1:].lower()


def is_mount(path: Path) -> bool:
    """Return whether a path is a mount point (a seam for tests)."""
    return path.is_mount()


def is_card(path: Path) -> bool:
    """Return whether a directory looks like a camera card root."""
    try:
        return (path / "DCIM").is_dir()
    except OSError:
        return False


def find_card(config: dict[str, str]) -> Path:
    """Find the single mounted camera card."""
    if "card_root" in config:
        roots = [Path(config["card_root"]).expanduser()]
    else:
        user = getpass.getuser()
        roots = [Path("/run/media") / user, Path("/media") / user]
    cards = sorted(card for root in roots if root.is_dir()
                   for card in root.iterdir() if is_card(card))
    if len(cards) == 1:
        return cards[0]
    if not cards:
        raise ValueError(
            "no camera card (a dir with DCIM) found in "
            + ", ".join(str(root) for root in roots) + "; pass -s"
        )
    raise ValueError(
        "several cards are mounted, choose one with -s: " + ", ".join(map(str, cards))
    )


def card_folders(card: Path) -> list[Path]:
    """Return the camera folders under DCIM, such as 109_PANA or 100OLYMP."""
    return sorted((path for path in (card / "DCIM").iterdir() if path.is_dir()),
                  key=lambda path: path.name.lower())


def resolve_source(args: argparse.Namespace, config: dict[str, str]) -> Path:
    """Choose and validate the source directory; a detected card must be a mount point."""
    srcdir, require_mount = args.srcdir, args.require_src_mount
    if srcdir is None:
        srcdir, require_mount = find_card(config), True
    if not srcdir.is_dir():
        raise ValueError(f"source directory does not exist or is not a directory: {srcdir}")
    if not os.access(srcdir, os.R_OK | os.X_OK):
        raise ValueError(f"source directory is not readable: {srcdir}")
    if require_mount and not is_mount(srcdir):
        raise ValueError(f"source is not a mount point: {srcdir}")
    return srcdir


def static_prefix(template: str) -> Path:
    """Return the leading part of a destination template without {year}."""
    parts = []
    for part in Path(template).parts:
        if "{year}" in part:
            break
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def resolve_destination(args: argparse.Namespace, config: dict[str, str]) -> str:
    """Build the destination template, substituting {place}; {year} is per file."""
    if args.dstdir is not None:
        template = str(args.dstdir)
    elif "archive" in config:
        template = str(Path(config["archive"]).expanduser())
    else:
        raise ValueError("no destination: pass -d or set 'archive' in the config")
    if "{place}" in template:
        if not args.place:
            raise ValueError(f"the destination {template} needs a place: cpmyphotos PLACE")
        place = Path(args.place)
        if place.is_absolute() or ".." in place.parts:
            raise ValueError(f"place must be a relative name: {args.place}")
        template = template.replace("{place}", args.place)
    elif args.place:
        raise ValueError(f"place {args.place!r} given, but the destination has no {{place}}: "
                         f"{template}")
    return template


def resolve_layout(args: argparse.Namespace, config: dict[str, str]) -> Layout:
    """Resolve source and destination; a card root without -r means its DCIM folders."""
    srcdir = resolve_source(args, config)
    template = resolve_destination(args, config)
    card = srcdir if not args.recursive and is_card(srcdir) else None
    return Layout(srcdir, card_folders(card) if card else [srcdir], card, args.recursive,
                  template, static_prefix(template))


def validate_destination(layout: Layout, require_mount: bool) -> None:
    """Validate the fixed part of the destination before writing anything."""
    if not layout.root.is_dir():
        raise ValueError(
            f"destination directory does not exist or is not a directory: {layout.root}"
        )
    if require_mount and not is_mount(layout.root):
        raise ValueError(f"destination is not a mount point: {layout.root}")
    if layout.recursive and layout.root.resolve().is_relative_to(layout.srcdir.resolve()):
        raise ValueError("destination cannot be inside source when --recursive is used")


def validate_target_dirs(targets: list[tuple[Path, Path]]) -> None:
    """Check that every destination dir exists or can be created by us."""
    for directory in sorted({destination.parent for _, destination in targets}):
        existing = directory
        while not existing.exists():
            existing = existing.parent
        if not existing.is_dir():
            raise ValueError(f"destination is not a directory: {existing}")
        if not os.access(existing, os.W_OK | os.X_OK):
            raise ValueError(f"destination directory is not writable: {existing}")


def datetime_ns(moment: datetime) -> int:
    """Convert a datetime to nanoseconds since the epoch."""
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000


def local_time(seconds: float) -> datetime:
    """Convert epoch seconds to an aware datetime in the host timezone."""
    return datetime.fromtimestamp(seconds, timezone.utc).astimezone()


def format_ns(value: int) -> str:
    """Format an mtime in nanoseconds as local time."""
    return f"{local_time(value / 1e9):%Y-%m-%d %H:%M:%S}"


def state_path() -> Path:
    """Return the file recording the last import from each card."""
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "cpmyphotos" / "cards.json"


def load_state() -> dict[str, dict[str, object]]:
    """Load the per-card import state."""
    path = state_path()
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_card_state(card: Path, newest_ns: int) -> None:
    """Record the newest imported mtime for a card, never moving it backwards."""
    state = load_state()
    entry = state.get(card.name, {})
    if int(entry.get("mtime_ns", 0)) >= newest_ns:
        return
    state[card.name] = {
        "mtime_ns": newest_ns,
        "newest": format_ns(newest_ns),
        "saved": format_ns(time.time_ns()),
    }
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def resolve_newer(args: argparse.Namespace, card: Path | None) -> tuple[int | None, str]:
    """Return the mtime threshold in nanoseconds and where it came from."""
    if args.newer is not None:
        parsed = dateparser.parse(args.newer)
        if parsed is None:
            raise ValueError(f"invalid date for --newer: {args.newer!r}")
        return datetime_ns(parsed), f"-n {args.newer}"
    if args.all or card is None:
        return None, ""
    entry = load_state().get(card.name)
    if entry is None:
        raise ValueError(
            f"no import from card {card.name} recorded yet: "
            "pass -n DATE, or --all for the whole card"
        )
    return int(entry["mtime_ns"]), f"last import from {card.name}"


def source_files(layout: Layout) -> list[Path]:
    """Return source files in deterministic order."""
    if layout.recursive:
        paths = layout.srcdir.rglob("*")
    else:
        paths = chain.from_iterable(directory.iterdir() for directory in layout.folders)
    return sorted((path for path in paths if path.is_file()), key=lambda path: str(path).lower())


def is_selected(source: Path, extensions: set[str], newer_ns: int | None) -> bool:
    """Return whether a file has an accepted extension and passes the date filter."""
    if file_extension(source) not in extensions:
        return False
    return newer_ns is None or source.stat().st_mtime_ns > newer_ns


def validate_photo_dates(sources: list[Path]) -> None:
    """Catch implausibly old card timestamps before they drive the import."""
    cutoff_ns = time.time_ns() - 365 * 24 * 60 * 60 * 1_000_000_000
    old = [source for source in sources if source.stat().st_mtime_ns < cutoff_ns]
    if old:
        raise ValueError(
            f"{len(old)} card photo(s) are over a year old "
            f"(first: {old[0]}). If these dates are intentional, pass --allow-old-dates; "
            "if the camera clock reset, use --all or -n plus -g and --geosync to match GPS"
        )


def file_date(path: Path) -> date:
    """Return the local modification date, which cameras set to the capture time."""
    return local_time(path.stat().st_mtime).date()


def destination_for(source_file: Path, layout: Layout) -> Path:
    """Calculate a destination, preserving relative paths when recursive."""
    base = layout.template
    if "{year}" in base:
        base = base.replace("{year}", str(file_date(source_file).year))
    relative = (source_file.relative_to(layout.srcdir) if layout.recursive
                else Path(source_file.name))
    return Path(base) / relative


def resolve_gpx(args: argparse.Namespace, config: dict[str, str], dates: set[date]
                ) -> list[Path]:
    """Return -g tracks plus those from 'gpx_dir' for the photo dates, a day either side."""
    explicit = list(args.gpx or [])
    if args.no_gps or "gpx_dir" not in config:
        return explicit
    gpx_dir = Path(config["gpx_dir"]).expanduser()
    name = config.get("gpx_name", DEFAULT_GPX_NAME)
    found: dict[Path, None] = {}
    uncovered = []
    for day in sorted(dates):
        near = [gpx_dir / (day + timedelta(days=delta)).strftime(name) for delta in (-1, 0, 1)]
        existing = [path for path in near if path.is_file()]
        if not existing:
            uncovered.append(day.isoformat())
        found.update(dict.fromkeys(existing))
    if uncovered:
        print(f"WARNING: no GPX track in {gpx_dir} for photo dates: " + ", ".join(uncovered),
              file=sys.stderr)
    gpx = list(dict.fromkeys([*explicit, *found]))
    if args.require_gps and not gpx and dates:
        raise ValueError("--require-gps is set, but no GPX track was found")
    return gpx


def resolve_metadata(args: argparse.Namespace, config: dict[str, str], dates: set[date]
                     ) -> Metadata:
    """Resolve and validate metadata options, including required programs."""
    metadata = Metadata(
        copyright=option(args.exif_copr, config.get("copyright")),
        lens=option(args.exif_lens, config.get("lens")),
        gpx=resolve_gpx(args, config, dates),
        camera_tz=option(args.camera_tz, config.get("tz", "Z")),
        geosync=args.geosync,
        require_gps=args.require_gps,
    )
    if metadata.camera_tz and not TIMEZONE_RE.fullmatch(metadata.camera_tz):
        raise ValueError("--camera-tz must be Z or an offset such as +03:00")
    if metadata.geosync and not metadata.gpx:
        raise ValueError("--geosync requires at least one GPX track")
    for gpx_file in metadata.gpx:
        if not gpx_file.is_file():
            raise ValueError(f"GPX file does not exist or is not a file: {gpx_file}")
    if ((metadata.gpx or metadata.copyright or metadata.lens)
            and shutil.which("exiftool") is None):
        raise ValueError("exiftool is required for the requested metadata changes")
    return metadata


def file_hash(path: Path) -> str:
    """Calculate a SHA-256 digest without loading the whole file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_requested(extension: str, metadata: Metadata) -> tuple[bool, bool]:
    """Return (any metadata work, basic EXIF work) for a file."""
    basic_exif = extension in METADATA_EXTENSIONS and bool(metadata.copyright or metadata.lens)
    return bool(metadata.gpx) or basic_exif, basic_exif


def apply_mode(path: Path, mode: int | None) -> None:
    """Apply the destination mode, unless the source mode is preserved."""
    if mode is not None:
        path.chmod(mode)


def verify_copy(source: Path, copy: Path, source_stat: os.stat_result) -> str:
    """Check the copy against its source and that the source did not change; return its hash."""
    source_digest = file_hash(source)
    if source_digest != file_hash(copy):
        raise OSError(f"verification failed while copying {source}")
    current_stat = source.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (
            source_stat.st_size, source_stat.st_mtime_ns):
        raise OSError(f"source changed while copying: {source}")
    return source_digest


def create_temporary_copy(source: Path, destination: Path) -> tuple[Path, str]:
    """Copy source to a same-directory temporary path for metadata processing."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source.stat()
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=destination.suffix, dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        shutil.copy2(source, temporary)
        return temporary, verify_copy(source, temporary, source_stat)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def direct_copy(source: Path, destination: Path, mode: int | None) -> None:
    """Copy directly to a new destination and verify its content."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source.stat()
    destination_created = False
    try:
        # The exclusive create prevents an unexpected concurrent overwrite.
        with source.open("rb") as source_handle:
            with destination.open("xb") as destination_handle:
                destination_created = True
                shutil.copyfileobj(source_handle, destination_handle, HASH_CHUNK_SIZE)
        shutil.copystat(source, destination)
        apply_mode(destination, mode)
        verify_copy(source, destination, source_stat)
    except BaseException:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise


def run_exiftool(command: list[str], debug: bool) -> None:
    """Run ExifTool without buffering verbose output in memory."""
    if debug:
        print("CMD:", shlex.join(command))
    subprocess.run(command, check=True, stdout=None if debug else subprocess.DEVNULL)


def files_missing_gps(paths: list[str]) -> list[str]:
    """Return files on which ExifTool did not leave coordinates."""
    command = ["exiftool", "-j", "-n", "-GPSLatitude", "-GPSLongitude", *paths]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    metadata = json.loads(result.stdout)
    return [
        item.get("SourceFile", "<unknown>")
        for item in metadata
        if "GPSLatitude" not in item or "GPSLongitude" not in item
    ]


def run_exiftool_batch(command: list[str], items: list[PendingMetadataCopy],
                       debug: bool) -> list[PendingMetadataCopy]:
    """Run ExifTool over temporary copies, returning the items it failed on.

    One bad file makes ExifTool exit non-zero for the whole batch, so a failed
    batch is retried file by file. Rewriting the same tags is idempotent, so
    files already written by the failed batch are safe to process again.
    """
    try:
        run_exiftool([*command, *(str(item.temporary) for item in items)], debug)
        return []
    except subprocess.CalledProcessError:
        if len(items) == 1:
            return list(items)
    failed = []
    for item in items:
        try:
            run_exiftool([*command, str(item.temporary)], debug)
        except subprocess.CalledProcessError:
            failed.append(item)
    return failed


def basic_exif_command(metadata: Metadata) -> list[str]:
    """Build the ExifTool command for copyright and lens tags, without file names."""
    command = ["exiftool", "-overwrite_original_in_place", "-P"]
    if metadata.copyright:
        command.append(f"-EXIF:Copyright={metadata.copyright}")
    if metadata.lens:
        command.append(f"-EXIF:LensModel={metadata.lens}")
    return command


def geotag_command(metadata: Metadata, debug: bool) -> list[str]:
    """Build the ExifTool geotagging command, without file names."""
    command = ["exiftool"]
    for gpx_file in metadata.gpx:
        command.extend(["-geotag", str(gpx_file)])
    if metadata.camera_tz:
        command.append(f"-geotime<${{DateTimeOriginal}}{metadata.camera_tz}")
    if metadata.geosync:
        command.append(f"-geosync={metadata.geosync}")
    command.extend(["-overwrite_original_in_place", "-P"])
    if debug:
        command.append("-v2")
    return command


def process_metadata(pending: list[PendingMetadataCopy], settings: Settings
                     ) -> tuple[list[PendingMetadataCopy], list[tuple[PendingMetadataCopy, str]]]:
    """Apply requested EXIF and GPX metadata to temporary copies.

    Returns the copies ready to publish and the (copy, reason) pairs that failed.
    """
    metadata, debug = settings.metadata, settings.debug
    ready = list(pending)
    failures: list[tuple[PendingMetadataCopy, str]] = []

    def fail(items: list[PendingMetadataCopy], reason: str) -> None:
        failures.extend((item, reason) for item in items)
        ready[:] = [item for item in ready if item not in items]

    basic_items = [item for item in ready if item.write_basic_exif]
    if basic_items:
        fail(run_exiftool_batch(basic_exif_command(metadata), basic_items, debug),
             "ExifTool could not write EXIF")

    if metadata.gpx and ready:
        fail(run_exiftool_batch(geotag_command(metadata, debug), ready, debug),
             "ExifTool could not geotag")

    if metadata.gpx and ready:
        missing = set(files_missing_gps([str(item.temporary) for item in ready]))
        unmatched = [item for item in ready if str(item.temporary) in missing]
        if unmatched:
            sources = [str(item.source) for item in unmatched]
            print("WARNING: no GPX track match for: " + ", ".join(sources), file=sys.stderr)
            if metadata.require_gps:
                fail(unmatched, "GPS coordinates required but no track match was found")

    # Metadata tools may update timestamps or permissions. Restore the source
    # attributes, then apply the user's requested archive mode.
    for item in list(ready):
        try:
            shutil.copystat(item.source, item.temporary)
            apply_mode(item.temporary, settings.mode)
        except OSError as error:
            fail([item], str(error))
    return ready, failures


def publish_temporary(temporary: Path, destination: Path, mode: int | None) -> None:
    """Publish a temporary file without overwriting an existing one.

    A hard link publishes atomically. Filesystems without hard links (exFAT,
    FAT32, some network shares) fall back to a verified exclusive-create copy.
    """
    try:
        os.link(temporary, destination)
    except OSError as error:
        if error.errno not in LINK_UNSUPPORTED_ERRNOS:
            raise
        direct_copy(temporary, destination, mode)
    temporary.unlink()


def preflight_space(targets: list[tuple[Path, Path]], settings: Settings) -> None:
    """Ensure there is reasonable space for planned new and temporary copies."""
    permanent_bytes = 0
    existing_metadata_sizes = []
    for source, destination in targets:
        needs_metadata, _ = metadata_requested(file_extension(source), settings.metadata)
        if not destination.exists():
            permanent_bytes += source.stat().st_size
        elif needs_metadata:
            existing_metadata_sizes.append(source.stat().st_size)
    workspace_bytes = sum(sorted(existing_metadata_sizes, reverse=True)[:METADATA_BATCH_SIZE])
    required = permanent_bytes + workspace_bytes
    if required == 0:
        return
    reserve = max(10 * 1024 * 1024, required // 100)
    free = shutil.disk_usage(settings.layout.root).free
    if required + reserve > free:
        raise ValueError(
            f"not enough destination space: need about {required + reserve:,} bytes, "
            f"have {free:,} bytes"
        )


def new_counters() -> dict[str, int]:
    """Create operation counters in display order."""
    return {
        "scanned": 0, "copied": 0, "identical": 0, "conflicts": 0,
        "unsupported": 0, "old": 0, "failed": 0, "metadata_changed": 0,
    }


def record_existing(source: Path, destination: Path, digest: str, debug: bool,
                    counters: dict[str, int]) -> None:
    """Count an existing destination as identical if it hashes to digest, else a conflict."""
    if destination.is_file() and file_hash(destination) == digest:
        counters["identical"] += 1
        if debug:
            print(f"SKIP identical: {destination}")
        return
    counters["conflicts"] += 1
    print(f"CONFLICT: {source} -> {destination}", file=sys.stderr)


def handle_source(source: Path, state: ImportState) -> None:
    """Classify and copy or queue one source file."""
    settings, plan, counters = state.settings, state.plan, state.counters
    counters["scanned"] += 1
    extension = file_extension(source)
    if extension not in plan.extensions:
        counters["unsupported"] += 1
        if settings.debug:
            print(f"SKIP unsupported: {source}")
        return
    if plan.newer_ns is not None and source.stat().st_mtime_ns <= plan.newer_ns:
        counters["old"] += 1
        if settings.debug:
            print(f"SKIP old: {source}")
        return

    destination = destination_for(source, settings.layout)
    needs_metadata, basic_exif = metadata_requested(extension, settings.metadata)
    try:
        if needs_metadata:
            temporary, source_digest = create_temporary_copy(source, destination)
            state.pending.append(PendingMetadataCopy(
                source, destination, temporary, basic_exif, source_digest
            ))
        elif destination.exists():
            record_existing(source, destination, file_hash(source), settings.debug, counters)
        else:
            print(f"COPY: {source} -> {destination}")
            direct_copy(source, destination, settings.mode)
            counters["copied"] += 1
    except (OSError, shutil.Error) as error:
        counters["failed"] += 1
        print(f"ERROR: {source}: {error}", file=sys.stderr)


def publish_metadata_copy(item: PendingMetadataCopy, settings: Settings,
                          counters: dict[str, int]) -> None:
    """Compare or publish one successfully processed metadata candidate."""
    candidate_digest = file_hash(item.temporary)
    if item.destination.exists():
        record_existing(item.source, item.destination, candidate_digest, settings.debug,
                        counters)
        return

    publish_temporary(item.temporary, item.destination, settings.mode)
    counters["copied"] += 1
    if item.source_digest != candidate_digest:
        counters["metadata_changed"] += 1
    print(f"COPY: {item.source} -> {item.destination} (metadata processed)")


def finish_metadata_copies(pending: list[PendingMetadataCopy], settings: Settings,
                           counters: dict[str, int]) -> None:
    """Process, publish, and clean up all temporary metadata copies."""
    if not pending:
        return
    try:
        try:
            ready, failures = process_metadata(pending, settings)
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            counters["failed"] += len(pending)
            print(f"ERROR: metadata processing failed: {error}", file=sys.stderr)
            return
        for item, reason in failures:
            counters["failed"] += 1
            print(f"ERROR: {item.source}: {reason}", file=sys.stderr)
        for item in ready:
            try:
                publish_metadata_copy(item, settings, counters)
            except OSError as error:
                counters["failed"] += 1
                print(f"ERROR: {item.source}: {error}", file=sys.stderr)
    finally:
        cleanup_temporaries(pending)


def cleanup_temporaries(pending: list[PendingMetadataCopy]) -> None:
    """Remove temporary copies that were not published."""
    for item in pending:
        item.temporary.unlink(missing_ok=True)


def import_photos(settings: Settings, plan: Plan) -> dict[str, int]:
    """Copy and process eligible photos, returning operation counters."""
    counters = new_counters()
    pending: list[PendingMetadataCopy] = []
    state = ImportState(settings, plan, pending, counters)

    try:
        for source in plan.files:
            handle_source(source, state)
            if len(pending) >= METADATA_BATCH_SIZE:
                finish_metadata_copies(pending, settings, counters)
                pending.clear()
        finish_metadata_copies(pending, settings, counters)
    finally:
        # Covers an interrupt or unexpected error while copies are still queued.
        cleanup_temporaries(pending)

    return counters


def prepare(args: argparse.Namespace) -> tuple[Settings, Plan]:
    """Resolve defaults and validate everything before writing anything."""
    config = load_config(args.config)
    layout = resolve_layout(args, config)
    validate_destination(layout, args.require_dst_mount)
    extensions = load_extensions()
    newer_ns, newer_reason = resolve_newer(args, layout.card)
    files = source_files(layout)
    if layout.card and not args.allow_old_dates:
        validate_photo_dates([source for source in files if file_extension(source) in extensions])
    selected = [path for path in files if is_selected(path, extensions, newer_ns)]
    targets = [(source, destination_for(source, layout)) for source in selected]
    metadata = resolve_metadata(args, config, {file_date(source) for source in selected})
    settings = Settings(layout, metadata, None if args.preserve_mode else args.mode, args.debug)
    validate_target_dirs(targets)
    preflight_space(targets, settings)
    newest_ns = max((source.stat().st_mtime_ns for source in selected), default=None)
    return settings, Plan(extensions, files, targets, newer_ns, newer_reason, newest_ns)


def print_plan(settings: Settings, plan: Plan) -> None:
    """Show the resolved settings, so inferred defaults are visible."""
    layout, metadata = settings.layout, settings.metadata
    if layout.card:
        folders = ", ".join(f"DCIM/{folder.name}" for folder in layout.folders)
        print(f"SOURCE: {layout.card} ({folders or 'no DCIM folders'})")
    else:
        print(f"SOURCE: {layout.srcdir}")
    if plan.newer_ns is not None:
        print(f"NEWER: {format_ns(plan.newer_ns)} ({plan.newer_reason})")
    directories = Counter(destination.parent for _, destination in plan.targets)
    for directory, count in sorted(directories.items()):
        print(f"DEST: {directory} ({count} files)")
    if not directories:
        print(f"DEST: {layout.template} (no files)")
    if metadata.gpx:
        print("GPX: " + ", ".join(path.name for path in metadata.gpx))
    tags = [f"{name}={value}" for name, value in
            (("copyright", metadata.copyright), ("lens", metadata.lens)) if value]
    if tags:
        print("EXIF: " + " ".join(tags))


def dry_run(plan: Plan) -> int:
    """List the files that would be copied."""
    new = [(source, destination) for source, destination in plan.targets
           if not destination.exists()]
    for source, destination in new:
        print(f"NEW: {source} -> {destination}")
    print(f"DRY-RUN: selected={len(plan.targets)} new={len(new)} "
          f"existing={len(plan.targets) - len(new)}")
    return 0


def remember_import(card: Path, plan: Plan, clean: bool) -> None:
    """Record the newest imported file of a card after a clean run."""
    if plan.newest_ns is None:
        return
    if not clean:
        print(f"WARNING: last import from {card.name} not updated because of conflicts or "
              "failures", file=sys.stderr)
        return
    save_card_state(card, plan.newest_ns)
    print(f"IMPORTED: {card.name} up to {format_ns(plan.newest_ns)}")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings, plan = prepare(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))

    print_plan(settings, plan)
    if args.dry_run:
        return dry_run(plan)
    start = timer()
    counters = import_photos(settings, plan)
    elapsed = timer() - start
    print(
        "SUMMARY: "
        + " ".join(f"{name}={value}" for name, value in counters.items())
        + f" time={elapsed:.2f}s"
    )
    clean = not counters["conflicts"] and not counters["failed"]
    if settings.layout.card:
        remember_import(settings.layout.card, plan, clean)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
