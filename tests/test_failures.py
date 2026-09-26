"""Tests for partial failures, interrupts, and awkward destinations."""

import collections
import errno
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import CAMERA_JPEG, UTC_TRACK, read_metadata

import cpmyphotos


def hidden_files(directory: Path) -> list[Path]:
    """Return leftover temporary copies (dot files) anywhere under a directory."""
    return [path for path in directory.rglob(".*") if path.is_file()]


def test_corrupt_file_does_not_fail_the_rest_of_the_batch(camera_source, tmp_path: Path,
                                                         run_import):
    (camera_source / "P0000000.JPG").write_bytes(CAMERA_JPEG.read_bytes()[:3000])
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(camera_source, destination, "-C", "Fixture Copyright")

    assert result.returncode == 1
    assert "P0000000.JPG: ExifTool could not write EXIF" in result.stderr
    assert "copied=1" in result.stdout
    assert "failed=1" in result.stdout
    assert read_metadata(destination / CAMERA_JPEG.name, "Copyright")["Copyright"] == (
        "Fixture Copyright"
    )
    assert not (destination / "P0000000.JPG").exists()
    assert not hidden_files(destination)


def test_required_gps_fails_only_unmatched_files(camera_source, tmp_path: Path, run_import):
    unmatched = camera_source / "P0000000.JPG"
    shutil.copy2(CAMERA_JPEG, unmatched)
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal+=24:0:0", str(unmatched)],
        check=True,
    )
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(camera_source, destination, "--gpx", UTC_TRACK, "--require-gps")

    assert result.returncode == 1
    assert "P0000000.JPG: GPS coordinates required" in result.stderr
    assert "copied=1" in result.stdout
    assert "failed=1" in result.stdout
    assert "GPSLatitude" in read_metadata(destination / CAMERA_JPEG.name, "GPSLatitude")
    assert not (destination / "P0000000.JPG").exists()
    assert not hidden_files(destination)


def test_interrupt_during_scan_removes_queued_temporary_copies(
        camera_source, tmp_path: Path, monkeypatch):
    shutil.copy2(CAMERA_JPEG, camera_source / "P9999999.JPG")
    destination = tmp_path / "destination"
    destination.mkdir()
    original = cpmyphotos.handle_source
    calls = []

    def interrupt_on_second_file(source, state):
        calls.append(source)
        if len(calls) == 2:
            raise KeyboardInterrupt
        original(source, state)

    monkeypatch.setattr(cpmyphotos, "handle_source", interrupt_on_second_file)

    with pytest.raises(KeyboardInterrupt):
        cpmyphotos.main(["-s", str(camera_source), "-d", str(destination), "-C", "x"])

    assert len(calls) == 2
    assert not list(destination.iterdir())


def test_destination_without_hard_links_falls_back_to_copy(
        camera_source, tmp_path: Path, monkeypatch, capsys):
    destination = tmp_path / "destination"
    destination.mkdir()

    def no_hard_links(source, target):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM), str(source))

    monkeypatch.setattr(os, "link", no_hard_links)

    status = cpmyphotos.main(
        ["-s", str(camera_source), "-d", str(destination), "-C", "Fixture Copyright"]
    )

    target = destination / CAMERA_JPEG.name
    assert status == 0, capsys.readouterr().err
    assert read_metadata(target, "Copyright")["Copyright"] == "Fixture Copyright"
    assert target.stat().st_mtime_ns == (camera_source / CAMERA_JPEG.name).stat().st_mtime_ns
    assert target.stat().st_mode & 0o777 == 0o644
    assert not hidden_files(destination)


def test_not_enough_space_is_a_clean_usage_error(camera_source, tmp_path: Path,
                                                 monkeypatch, capsys):
    destination = tmp_path / "destination"
    destination.mkdir()
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(1, 1, 1))

    with pytest.raises(SystemExit) as exit_info:
        cpmyphotos.main(["-s", str(camera_source), "-d", str(destination)])

    assert exit_info.value.code == 2
    assert "not enough destination space" in capsys.readouterr().err
    assert not list(destination.iterdir())
