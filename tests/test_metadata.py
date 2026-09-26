"""Tests for fixture integrity and basic EXIF updates."""

import subprocess
from pathlib import Path

from conftest import CAMERA_JPEG, read_metadata


def test_camera_fixture_is_small_valid_and_has_no_gps():
    metadata = read_metadata(
        CAMERA_JPEG, "ImageWidth", "ImageHeight", "DateTimeOriginal",
        "OffsetTimeOriginal", "Make", "Model", "GPSLatitude", "GPSLongitude",
    )
    validation = subprocess.check_output(
        ["exiftool", "-validate", "-warning", "-error", str(CAMERA_JPEG)], text=True
    )

    assert CAMERA_JPEG.stat().st_size < 40_000
    assert metadata["ImageWidth"] == 32
    assert metadata["ImageHeight"] == 24
    assert metadata["DateTimeOriginal"] == "2026:04:12 05:23:39"
    assert metadata["OffsetTimeOriginal"] == "+02:00"
    assert metadata["Make"] == "Panasonic"
    assert metadata["Model"] == "DC-G9"
    assert "GPSLatitude" not in metadata
    assert "GPSLongitude" not in metadata
    assert "Validate                        : OK" in validation


def test_basic_metadata_preserves_camera_fields(camera_source, tmp_path: Path, run_import):
    destination = tmp_path / "destination"
    destination.mkdir()
    source_file = camera_source / CAMERA_JPEG.name
    source_mtime = source_file.stat().st_mtime_ns

    result = run_import(
        camera_source, destination, "--preserve-mode",
        "-C", "Fixture Copyright", "-L", "Fixture Lens",
    )
    target = destination / CAMERA_JPEG.name
    metadata = read_metadata(
        target, "DateTimeOriginal", "OffsetTimeOriginal", "Make", "Model",
        "Copyright", "LensModel", "GPSLatitude", "GPSLongitude",
    )

    assert result.returncode == 0, result.stderr
    assert metadata["DateTimeOriginal"] == "2026:04:12 05:23:39"
    assert metadata["OffsetTimeOriginal"] == "+02:00"
    assert metadata["Make"] == "Panasonic"
    assert metadata["Model"] == "DC-G9"
    assert metadata["Copyright"] == "Fixture Copyright"
    assert metadata["LensModel"] == "Fixture Lens"
    assert "GPSLatitude" not in metadata
    assert "GPSLongitude" not in metadata
    assert target.stat().st_mtime_ns == source_mtime

    rerun = run_import(
        camera_source, destination, "--preserve-mode",
        "-C", "Fixture Copyright", "-L", "Fixture Lens",
    )
    assert rerun.returncode == 0, rerun.stderr
    assert "identical=1" in rerun.stdout
    assert "metadata_changed=0" in rerun.stdout
