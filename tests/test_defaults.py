"""Tests for config defaults, card detection, remembered imports and automatic GPX."""

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import CAMERA_JPEG, UTC_TRACK, read_metadata

import cpmyphotos


def local(*parts: int) -> datetime:
    """Return a host-local time, as a camera card's FAT timestamps are read."""
    return datetime(*parts).astimezone()


def set_mtime(path: Path, moment: datetime) -> None:
    """Set a file's mtime to a local time."""
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def add_photo(folder: Path, name: str, moment: datetime) -> Path:
    """Put the fixture JPEG on a fake card under a new name and capture time."""
    folder.mkdir(parents=True, exist_ok=True)
    photo = folder / name
    shutil.copy2(CAMERA_JPEG, photo)
    set_mtime(photo, moment)
    return photo


@pytest.fixture(name="setup")
def setup_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mounted card under a card root, an archive, and a config pointing at both."""
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    monkeypatch.setattr(cpmyphotos.time, "time_ns", lambda: int(now.timestamp() * 1e9))
    media = tmp_path / "media"
    card = media / "LUMIX1"
    add_photo(card / "DCIM" / "109_PANA", "P1090001.JPG", local(2026, 4, 12, 12, 0))
    archive = tmp_path / "archive"
    archive.mkdir()
    gpx_dir = tmp_path / "gpslog"
    gpx_dir.mkdir()
    monkeypatch.setattr(cpmyphotos, "is_mount", lambda path: path == card)

    def write_config(**values: str) -> None:
        config = {"card_root": str(media), "archive": str(archive / "{year}/{place}/Oleg"),
                  **values}
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "cpmyphotos.yaml").write_text(
            "".join(f"{key}: '{value}'\n" for key, value in config.items()), encoding="utf-8"
        )

    write_config()
    return {"media": media, "card": card, "archive": archive, "gpx_dir": gpx_dir,
            "write_config": write_config}


def run_main(capsys, *arguments: str) -> tuple[int, str, str]:
    """Run the CLI in-process; usage errors return 2 like the real command."""
    try:
        status = cpmyphotos.main(list(arguments))
    except SystemExit as error:
        status = error.code
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_card_config_and_template(setup, capsys):
    card, archive = setup["card"], setup["archive"]
    add_photo(card / "DCIM" / "110_PANA", "P1100001.JPG", local(2026, 4, 12, 13, 0))
    add_photo(card / "DCIM" / "110_PANA", "P1100002.JPG", local(2025, 12, 31, 23, 0))
    setup["write_config"](copyright="PORZIONEDISOLE")

    status, out, err = run_main(capsys, "Cyprus", "--all")

    assert status == 0, err
    assert "SOURCE: " + str(card) + " (DCIM/109_PANA, DCIM/110_PANA)" in out
    assert "EXIF: copyright=PORZIONEDISOLE" in out
    target = archive / "2026" / "Cyprus" / "Oleg"
    assert sorted(path.name for path in target.iterdir()) == ["P1090001.JPG", "P1100001.JPG"]
    assert (archive / "2025" / "Cyprus" / "Oleg" / "P1100002.JPG").is_file()
    assert read_metadata(target / "P1090001.JPG", "Copyright")["Copyright"] == "PORZIONEDISOLE"
    assert "IMPORTED: LUMIX1 up to 2026-04-12 13:00:00" in out


def test_card_remembers_last_import(setup, capsys):
    card, archive = setup["card"], setup["archive"]

    status, _, err = run_main(capsys, "Cyprus")
    assert status == 2
    assert "no import from card LUMIX1 recorded yet" in err
    assert not list(archive.iterdir())

    assert run_main(capsys, "Cyprus", "--all")[0] == 0
    add_photo(card / "DCIM" / "109_PANA", "P1090002.JPG", local(2026, 4, 13, 9, 0))

    status, out, _ = run_main(capsys, "Cyprus", "-N")
    target = archive / "2026" / "Cyprus" / "Oleg"
    assert status == 0
    assert "NEWER: 2026-04-12 12:00:00 (last import from LUMIX1)" in out
    assert f"NEW: {card}/DCIM/109_PANA/P1090002.JPG -> {target}/P1090002.JPG" in out
    assert "DRY-RUN: selected=1 new=1 existing=0" in out
    assert not (target / "P1090002.JPG").exists()

    status, out, _ = run_main(capsys, "Cyprus")
    assert status == 0
    assert "copied=1 identical=0" in out
    assert "old=1" in out
    assert "IMPORTED: LUMIX1 up to 2026-04-13 09:00:00" in out


def test_old_card_date_needs_override_for_gps_recovery(setup, capsys):
    photo = setup["card"] / "DCIM" / "109_PANA" / "P1090001.JPG"
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2010:01:01 00:00:00",
         str(photo)], check=True
    )
    set_mtime(photo, local(2010, 1, 1))
    recovery = ("Cyprus", "--all", "-g", str(UTC_TRACK),
                "--geosync", "+5945 05:23:39")

    status, _, err = run_main(capsys, *recovery)
    assert status == 2
    assert "--allow-old-dates" in err
    assert not list(setup["archive"].iterdir())

    status, _, err = run_main(capsys, *recovery, "--allow-old-dates")
    assert status == 0, err
    target = setup["archive"] / "2010" / "Cyprus" / "Oleg" / photo.name
    assert read_metadata(target, "GPSLatitude")["GPSLatitude"] == pytest.approx(11.0)


def test_reset_date_is_detected_before_last_import_filter(setup, capsys):
    assert run_main(capsys, "Cyprus", "--all")[0] == 0
    add_photo(setup["card"] / "DCIM" / "109_PANA", "P1090002.JPG", local(2010, 1, 1))

    status, _, err = run_main(capsys, "Cyprus")
    assert status == 2
    assert "P1090002.JPG" in err
    assert "--allow-old-dates" in err

    status, out, err = run_main(capsys, "Cyprus", "--allow-old-dates", "-N")
    assert status == 0, err
    assert "selected=0" in out


def test_conflict_does_not_advance_last_import(setup, capsys):
    card, archive = setup["card"], setup["archive"]
    assert run_main(capsys, "Cyprus", "--all")[0] == 0
    add_photo(card / "DCIM" / "109_PANA", "P1090002.JPG", local(2026, 4, 13, 9, 0))
    (archive / "2026" / "Cyprus" / "Oleg" / "P1090002.JPG").write_bytes(b"other")

    status, out, err = run_main(capsys, "Cyprus")

    assert status == 1
    assert "not updated because of conflicts" in err
    assert "IMPORTED" not in out
    assert "NEWER: 2026-04-12 12:00:00" in run_main(capsys, "Cyprus", "-N")[1]


def test_explicit_card_root_and_destination(setup, tmp_path: Path, capsys):
    destination = tmp_path / "flat"
    destination.mkdir()

    status, out, err = run_main(capsys, "-s", str(setup["card"]), "-d", str(destination),
                                "--all")

    assert status == 0, err
    assert (destination / "P1090001.JPG").is_file()
    assert "IMPORTED: LUMIX1" in out


@pytest.mark.parametrize("arguments, message", [
    ((), "needs a place"),
    (("-d", "/tmp", "Cyprus"), "has no {place}"),
    (("../up",), "place must be a relative name"),
])
@pytest.mark.usefixtures("setup")
def test_place_errors(capsys, arguments, message):
    status, _, err = run_main(capsys, "--all", *arguments)
    assert status == 2
    assert message in err


def test_card_detection_errors(setup, capsys, monkeypatch):
    shutil.copytree(setup["card"], setup["media"] / "4621-0000")
    status, _, err = run_main(capsys, "Cyprus", "--all")
    assert status == 2
    assert "several cards are mounted" in err

    shutil.rmtree(setup["media"])
    setup["media"].mkdir()
    status, _, err = run_main(capsys, "Cyprus", "--all")
    assert status == 2
    assert "no camera card" in err

    add_photo(setup["media"] / "LUMIX1" / "DCIM" / "109_PANA", "P1.JPG", local(2026, 4, 12))
    monkeypatch.setattr(cpmyphotos, "is_mount", lambda path: False)
    status, _, err = run_main(capsys, "Cyprus", "--all")
    assert status == 2
    assert "not a mount point" in err


def test_gpx_tracks_are_chosen_by_photo_date(setup, capsys):
    gpx_dir, archive = setup["gpx_dir"], setup["archive"]
    shutil.copy2(UTC_TRACK, gpx_dir / "20260412.gpx")
    shutil.copy2(UTC_TRACK, gpx_dir / "20260601.gpx")
    add_photo(setup["card"] / "DCIM" / "109_PANA", "P1090009.JPG", local(2026, 4, 20, 10))
    setup["write_config"](gpx_dir=str(gpx_dir))

    status, out, err = run_main(capsys, "Cyprus", "--all")

    target = archive / "2026" / "Cyprus" / "Oleg"
    assert status == 0, err
    assert "GPX: 20260412.gpx\n" in out
    assert "no GPX track in" in err
    assert "2026-04-20" in err
    metadata = read_metadata(target / "P1090001.JPG", "GPSLatitude")
    assert metadata["GPSLatitude"] == pytest.approx(11.0, abs=1e-6)


def test_no_gps_skips_configured_tracks(setup, capsys):
    shutil.copy2(UTC_TRACK, setup["gpx_dir"] / "20260412.gpx")
    setup["write_config"](gpx_dir=str(setup["gpx_dir"]))

    status, out, _ = run_main(capsys, "Cyprus", "--all", "--no-gps")

    assert status == 0
    assert "GPX:" not in out
    target = setup["archive"] / "2026" / "Cyprus" / "Oleg" / "P1090001.JPG"
    assert target.read_bytes() == CAMERA_JPEG.read_bytes()


def test_unknown_config_key_is_a_usage_error(setup, capsys):
    setup["write_config"](copyrigth="typo")
    status, _, err = run_main(capsys, "Cyprus", "--all")
    assert status == 2
    assert "unknown keys in config" in err
