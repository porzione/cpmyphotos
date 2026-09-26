#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["dateparser"]
# ///
"""Safely import photos from a card into a photo archive."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from timeit import default_timer as timer

import dateparser


HASH_CHUNK_SIZE = 1024 * 1024
METADATA_BATCH_SIZE = 50
METADATA_EXTENSIONS = {"jpg", "jpeg", "tif", "tiff", "png", "webp"}
TIMEZONE_RE = re.compile(r"^(?:Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)$")
LINK_UNSUPPORTED_ERRNOS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}


@dataclass
class PendingMetadataCopy:
    """A copy held under a temporary name until metadata work succeeds."""

    source: Path
    destination: Path
    temporary: Path
    write_basic_exif: bool


@dataclass
class ImportState:
    """Shared state for one import run."""

    args: argparse.Namespace
    extensions: set[str]
    newer: datetime | None
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
    parser = argparse.ArgumentParser(description="Safely copy photos from a card")
    parser.add_argument("-D", "--debug", action="store_true", help="Show detailed commands")
    parser.add_argument("-s", "--srcdir", type=Path, required=True, help="Source directory")
    parser.add_argument("-d", "--dstdir", type=Path, required=True, help="Destination directory")
    parser.add_argument("-n", "--newer", help="Copy files with an mtime newer than this date")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Recurse and preserve the source directory structure")
    parser.add_argument("-g", "--gpx", action="append", type=Path, help="GPX file (repeatable)")
    parser.add_argument(
        "--camera-tz", "--tz", dest="camera_tz", metavar="OFFSET",
        default="Z",
        help="Camera timezone for GPX matching (default: Z for UTC)",
    )
    parser.add_argument(
        "--geosync", metavar="SHIFT",
        help="Camera/GPS clock correction passed to ExifTool, for example +00:00:25",
    )
    parser.add_argument(
        "--require-gps", action="store_true",
        help="Treat files that cannot be matched to a GPX track as failed",
    )
    parser.add_argument("-C", dest="exif_copr", help="EXIF copyright")
    parser.add_argument("-L", dest="exif_lens", help="EXIF lens model")
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
        help="Refuse to run unless --srcdir itself is a mounted filesystem",
    )
    parser.add_argument(
        "--require-dst-mount", action="store_true",
        help="Refuse to run unless --dstdir itself is a mounted filesystem",
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


def parse_newer(value: str | None) -> datetime | None:
    """Parse the optional mtime threshold."""
    if value is None:
        return None
    parsed = dateparser.parse(value)
    if parsed is None:
        raise ValueError(f"invalid date for --newer: {value!r}")
    return parsed


def validate_paths(args: argparse.Namespace) -> None:
    """Validate source and destination paths before writing anything."""
    if not args.srcdir.is_dir():
        raise ValueError(f"source directory does not exist or is not a directory: {args.srcdir}")
    if not args.dstdir.is_dir():
        raise ValueError(
            f"destination directory does not exist or is not a directory: {args.dstdir}"
        )
    if not os.access(args.srcdir, os.R_OK | os.X_OK):
        raise ValueError(f"source directory is not readable: {args.srcdir}")
    if not os.access(args.dstdir, os.W_OK | os.X_OK):
        raise ValueError(f"destination directory is not writable: {args.dstdir}")
    if args.require_src_mount and not args.srcdir.is_mount():
        raise ValueError(f"source is not a mount point: {args.srcdir}")
    if args.require_dst_mount and not args.dstdir.is_mount():
        raise ValueError(f"destination is not a mount point: {args.dstdir}")
    if args.recursive and args.dstdir.resolve().is_relative_to(args.srcdir.resolve()):
        raise ValueError("destination cannot be inside source when --recursive is used")


def validate_metadata_options(args: argparse.Namespace) -> None:
    """Validate metadata arguments and required programs."""
    if args.camera_tz and not TIMEZONE_RE.fullmatch(args.camera_tz):
        raise ValueError("--camera-tz must be Z or an offset such as +03:00")
    if args.geosync and not args.gpx:
        raise ValueError("--geosync requires at least one --gpx file")
    for gpx_file in args.gpx or []:
        if not gpx_file.is_file():
            raise ValueError(f"GPX file does not exist or is not a file: {gpx_file}")
    if (args.gpx or args.exif_copr or args.exif_lens) and shutil.which("exiftool") is None:
        raise ValueError("exiftool is required for the requested metadata changes")


def validate_args(args: argparse.Namespace) -> None:
    """Validate all arguments before writing anything."""
    validate_paths(args)
    validate_metadata_options(args)


def source_files(source: Path, recursive: bool) -> list[Path]:
    """Return source files in deterministic order."""
    paths = source.rglob("*") if recursive else source.iterdir()
    return sorted((path for path in paths if path.is_file()), key=lambda path: str(path).lower())


def destination_for(source_file: Path, source_dir: Path, destination_dir: Path,
                    recursive: bool) -> Path:
    """Calculate a destination, preserving relative paths when recursive."""
    relative = source_file.relative_to(source_dir) if recursive else Path(source_file.name)
    return destination_dir / relative


def file_hash(path: Path) -> str:
    """Calculate a SHA-256 digest without loading the whole file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_requested(extension: str, args: argparse.Namespace) -> tuple[bool, bool]:
    """Return (any metadata work, basic EXIF work) for a file."""
    basic_exif = extension in METADATA_EXTENSIONS and bool(args.exif_copr or args.exif_lens)
    return bool(args.gpx) or basic_exif, basic_exif


def apply_mode(path: Path, args: argparse.Namespace) -> None:
    """Apply the configured destination mode."""
    if not args.preserve_mode:
        path.chmod(args.mode)


def create_temporary_copy(source: Path, destination: Path) -> Path:
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
        if file_hash(source) != file_hash(temporary):
            raise OSError(f"verification failed while copying {source}")
        current_stat = source.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != (
                source_stat.st_size, source_stat.st_mtime_ns):
            raise OSError(f"source changed while copying: {source}")
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def direct_copy(source: Path, destination: Path, args: argparse.Namespace) -> None:
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
        apply_mode(destination, args)
        if file_hash(source) != file_hash(destination):
            raise OSError(f"verification failed while copying {source}")
        current_stat = source.stat()
        if (current_stat.st_size, current_stat.st_mtime_ns) != (
                source_stat.st_size, source_stat.st_mtime_ns):
            raise OSError(f"source changed while copying: {source}")
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


def basic_exif_command(args: argparse.Namespace) -> list[str]:
    """Build the ExifTool command for copyright and lens tags, without file names."""
    command = ["exiftool", "-overwrite_original_in_place", "-P"]
    if args.exif_copr:
        command.append(f"-EXIF:Copyright={args.exif_copr}")
    if args.exif_lens:
        command.append(f"-EXIF:LensModel={args.exif_lens}")
    return command


def geotag_command(args: argparse.Namespace) -> list[str]:
    """Build the ExifTool geotagging command, without file names."""
    command = ["exiftool"]
    for gpx_file in args.gpx:
        command.extend(["-geotag", str(gpx_file)])
    if args.camera_tz:
        command.append(f"-geotime<${{DateTimeOriginal}}{args.camera_tz}")
    if args.geosync:
        command.append(f"-geosync={args.geosync}")
    command.extend(["-overwrite_original_in_place", "-P"])
    if args.debug:
        command.append("-v2")
    return command


def process_metadata(pending: list[PendingMetadataCopy], args: argparse.Namespace
                     ) -> tuple[list[PendingMetadataCopy], list[tuple[PendingMetadataCopy, str]]]:
    """Apply requested EXIF and GPX metadata to temporary copies.

    Returns the copies ready to publish and the (copy, reason) pairs that failed.
    """
    ready = list(pending)
    failures: list[tuple[PendingMetadataCopy, str]] = []

    def fail(items: list[PendingMetadataCopy], reason: str) -> None:
        failures.extend((item, reason) for item in items)
        ready[:] = [item for item in ready if item not in items]

    basic_items = [item for item in ready if item.write_basic_exif]
    if basic_items:
        fail(run_exiftool_batch(basic_exif_command(args), basic_items, args.debug),
             "ExifTool could not write EXIF")

    if args.gpx and ready:
        fail(run_exiftool_batch(geotag_command(args), ready, args.debug),
             "ExifTool could not geotag")

    if args.gpx and ready:
        missing = set(files_missing_gps([str(item.temporary) for item in ready]))
        unmatched = [item for item in ready if str(item.temporary) in missing]
        if unmatched:
            sources = [str(item.source) for item in unmatched]
            print("WARNING: no GPX track match for: " + ", ".join(sources), file=sys.stderr)
            if args.require_gps:
                fail(unmatched, "GPS coordinates required but no track match was found")

    # Metadata tools may update timestamps or permissions. Restore the source
    # attributes, then apply the user's requested archive mode.
    for item in list(ready):
        try:
            shutil.copystat(item.source, item.temporary)
            apply_mode(item.temporary, args)
        except OSError as error:
            fail([item], str(error))
    return ready, failures


def publish_temporary(temporary: Path, destination: Path, args: argparse.Namespace) -> None:
    """Publish a temporary file without overwriting an existing one.

    A hard link publishes atomically. Filesystems without hard links (exFAT,
    FAT32, some network shares) fall back to a verified exclusive-create copy.
    """
    try:
        os.link(temporary, destination)
    except OSError as error:
        if error.errno not in LINK_UNSUPPORTED_ERRNOS:
            raise
        direct_copy(temporary, destination, args)
    temporary.unlink()


def preflight_space(files: list[Path], args: argparse.Namespace, extensions: set[str],
                    newer: datetime | None) -> None:
    """Ensure there is reasonable space for planned new and temporary copies."""
    permanent_bytes = 0
    existing_metadata_sizes = []
    for source in files:
        extension = source.suffix[1:].lower()
        if extension not in extensions:
            continue
        if newer is not None and source.stat().st_mtime <= newer.timestamp():
            continue
        destination = destination_for(source, args.srcdir, args.dstdir, args.recursive)
        needs_metadata, _ = metadata_requested(extension, args)
        if not destination.exists():
            permanent_bytes += source.stat().st_size
        elif needs_metadata:
            existing_metadata_sizes.append(source.stat().st_size)
    workspace_bytes = sum(sorted(existing_metadata_sizes, reverse=True)[:METADATA_BATCH_SIZE])
    required = permanent_bytes + workspace_bytes
    if required == 0:
        return
    reserve = max(10 * 1024 * 1024, required // 100)
    free = shutil.disk_usage(args.dstdir).free
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


def handle_existing_direct(source: Path, destination: Path, debug: bool,
                           counters: dict[str, int]) -> None:
    """Classify an existing destination for a byte-preserving copy."""
    if destination.is_file() and file_hash(source) == file_hash(destination):
        counters["identical"] += 1
        if debug:
            print(f"SKIP identical: {destination}")
        return
    counters["conflicts"] += 1
    print(f"CONFLICT: {source} -> {destination}", file=sys.stderr)


def handle_source(source: Path, state: ImportState) -> None:
    """Classify and copy or queue one source file."""
    args = state.args
    counters = state.counters
    counters["scanned"] += 1
    extension = source.suffix[1:].lower()
    if extension not in state.extensions:
        counters["unsupported"] += 1
        if args.debug:
            print(f"SKIP unsupported: {source}")
        return
    if state.newer is not None and source.stat().st_mtime <= state.newer.timestamp():
        counters["old"] += 1
        if args.debug:
            print(f"SKIP old: {source}")
        return

    destination = destination_for(source, args.srcdir, args.dstdir, args.recursive)
    needs_metadata, basic_exif = metadata_requested(extension, args)
    try:
        if needs_metadata:
            temporary = create_temporary_copy(source, destination)
            state.pending.append(PendingMetadataCopy(
                source, destination, temporary, basic_exif
            ))
        elif destination.exists():
            handle_existing_direct(source, destination, args.debug, counters)
        else:
            print(f"COPY: {source} -> {destination}")
            direct_copy(source, destination, args)
            counters["copied"] += 1
    except (OSError, shutil.Error) as error:
        counters["failed"] += 1
        print(f"ERROR: {source}: {error}", file=sys.stderr)


def publish_metadata_copy(item: PendingMetadataCopy, args: argparse.Namespace,
                          counters: dict[str, int]) -> None:
    """Compare or publish one successfully processed metadata candidate."""
    source_digest = file_hash(item.source)
    candidate_digest = file_hash(item.temporary)
    if item.destination.exists():
        if item.destination.is_file() and candidate_digest == file_hash(item.destination):
            counters["identical"] += 1
            if args.debug:
                print(f"SKIP identical: {item.destination}")
        else:
            counters["conflicts"] += 1
            print(f"CONFLICT: {item.source} -> {item.destination}", file=sys.stderr)
        return

    publish_temporary(item.temporary, item.destination, args)
    counters["copied"] += 1
    if source_digest != candidate_digest:
        counters["metadata_changed"] += 1
    print(f"COPY: {item.source} -> {item.destination} (metadata processed)")


def finish_metadata_copies(pending: list[PendingMetadataCopy], args: argparse.Namespace,
                           counters: dict[str, int]) -> None:
    """Process, publish, and clean up all temporary metadata copies."""
    if not pending:
        return
    try:
        try:
            ready, failures = process_metadata(pending, args)
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            counters["failed"] += len(pending)
            print(f"ERROR: metadata processing failed: {error}", file=sys.stderr)
            return
        for item, reason in failures:
            counters["failed"] += 1
            print(f"ERROR: {item.source}: {reason}", file=sys.stderr)
        for item in ready:
            try:
                publish_metadata_copy(item, args, counters)
            except OSError as error:
                counters["failed"] += 1
                print(f"ERROR: {item.source}: {error}", file=sys.stderr)
    finally:
        cleanup_temporaries(pending)


def cleanup_temporaries(pending: list[PendingMetadataCopy]) -> None:
    """Remove temporary copies that were not published."""
    for item in pending:
        item.temporary.unlink(missing_ok=True)


def import_photos(args: argparse.Namespace, extensions: set[str], newer: datetime | None,
                  files: list[Path]) -> dict[str, int]:
    """Copy and process eligible photos, returning operation counters."""
    counters = new_counters()
    pending: list[PendingMetadataCopy] = []
    state = ImportState(args, extensions, newer, pending, counters)

    try:
        for source in files:
            handle_source(source, state)
            if len(pending) >= METADATA_BATCH_SIZE:
                finish_metadata_copies(pending, args, counters)
                pending.clear()
        finish_metadata_copies(pending, args, counters)
    finally:
        # Covers an interrupt or unexpected error while copies are still queued.
        cleanup_temporaries(pending)

    return counters


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        extensions = load_extensions()
        newer = parse_newer(args.newer)
        files = source_files(args.srcdir, args.recursive)
        preflight_space(files, args, extensions, newer)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))

    start = timer()
    counters = import_photos(args, extensions, newer, files)
    elapsed = timer() - start
    print(
        "SUMMARY: "
        + " ".join(f"{name}={value}" for name, value in counters.items())
        + f" time={elapsed:.2f}s"
    )
    return 1 if counters["conflicts"] or counters["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
