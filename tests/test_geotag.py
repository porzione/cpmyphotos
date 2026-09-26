"""Tests for UTC camera timestamps and GPX interpolation."""

from pathlib import Path

import pytest

from conftest import CAMERA_JPEG, UTC_TRACK, read_metadata


@pytest.mark.parametrize("host_tz", ["UTC", "Asia/Nicosia", "Pacific/Honolulu"])
def test_utc_camera_matching_is_independent_of_host_timezone(
        camera_source, tmp_path: Path, run_import, host_tz: str):
    destination = tmp_path / f"destination-{host_tz.replace('/', '-')}"
    destination.mkdir()

    result = run_import(camera_source, destination, "--gpx", UTC_TRACK, host_tz=host_tz)
    metadata = read_metadata(
        destination / CAMERA_JPEG.name, "DateTimeOriginal", "OffsetTimeOriginal",
        "GPSLatitude", "GPSLongitude", "GPSDateStamp", "GPSTimeStamp",
    )

    assert result.returncode == 0, result.stderr
    assert metadata["DateTimeOriginal"] == "2026:04:12 05:23:39"
    assert metadata["OffsetTimeOriginal"] == "+02:00"
    assert metadata["GPSLatitude"] == pytest.approx(11.0, abs=1e-6)
    assert metadata["GPSLongitude"] == pytest.approx(21.0, abs=1e-6)
    assert metadata["GPSDateStamp"] == "2026:04:12"
    assert metadata["GPSTimeStamp"] == "05:23:39"


def test_geosync_applies_an_explicit_clock_correction(camera_source, tmp_path: Path,
                                                     run_import):
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(
        camera_source, destination, "--gpx", UTC_TRACK, "--geosync", "+00:00:10"
    )
    metadata = read_metadata(destination / CAMERA_JPEG.name, "GPSLatitude", "GPSLongitude")

    assert result.returncode == 0, result.stderr
    assert metadata["GPSLatitude"] == pytest.approx(11.333333, abs=1e-5)
    assert metadata["GPSLongitude"] == pytest.approx(21.333333, abs=1e-5)


def test_missing_track_match_warns_and_publishes(camera_source, tmp_path: Path,
                                                 run_import):
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(
        camera_source, destination, "--gpx", UTC_TRACK, "--geosync", "+01:00:00"
    )

    target = destination / CAMERA_JPEG.name
    metadata = read_metadata(target, "GPSLatitude", "GPSLongitude")

    assert result.returncode == 0, result.stderr
    assert "no GPX track match" in result.stderr
    assert target.is_file()
    assert "GPSLatitude" not in metadata
    assert "GPSLongitude" not in metadata


def test_missing_track_match_can_be_required(camera_source, tmp_path: Path, run_import):
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(
        camera_source, destination, "--gpx", UTC_TRACK, "--geosync", "+01:00:00",
        "--require-gps",
    )

    assert result.returncode == 1
    assert "no GPX track match" in result.stderr
    assert "GPS coordinates required" in result.stderr
    assert not list(destination.iterdir())


def test_geotagged_file_is_identical_on_rerun(camera_source, tmp_path: Path, run_import):
    destination = tmp_path / "destination"
    destination.mkdir()

    first = run_import(camera_source, destination, "--gpx", UTC_TRACK)
    second = run_import(camera_source, destination, "--gpx", UTC_TRACK)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "copied=0" in second.stdout
    assert "identical=1" in second.stdout
    assert "metadata_changed=0" in second.stdout
