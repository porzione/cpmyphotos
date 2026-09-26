"""Opt-in smoke test against the user's latest private GPSLogger track."""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from conftest import CAMERA_JPEG, read_metadata


pytestmark = pytest.mark.real_gpx


def track_points(path: Path) -> list[tuple[datetime, float, float]]:
    """Read timestamped points from a GPX file without depending on its prefix."""
    points = []
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag.endswith("trkpt"):
            time_element = next(
                (child for child in element if child.tag.endswith("time")), None
            )
            if time_element is not None and time_element.text:
                timestamp = datetime.fromisoformat(time_element.text.replace("Z", "+00:00"))
                points.append((
                    timestamp, float(element.attrib["lat"]), float(element.attrib["lon"])
                ))
            element.clear()
    return points


def test_latest_real_gpslogger_track(tmp_path: Path, run_import):
    track_dir = Path(os.environ["REAL_GPX_DIR"])
    tracks = list(track_dir.glob("*.gpx"))
    assert tracks, f"no GPX files found in {track_dir}"
    latest = max(tracks, key=lambda path: path.stat().st_mtime_ns)
    points = track_points(latest)
    assert points, f"no track points found in {latest}"
    timestamp, expected_latitude, expected_longitude = points[len(points) // 2]

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    photo = source / CAMERA_JPEG.name
    shutil.copy2(CAMERA_JPEG, photo)
    subprocess.run(
        [
            "exiftool", "-overwrite_original", "-P",
            f"-DateTimeOriginal={timestamp.strftime('%Y:%m:%d %H:%M:%S')}", str(photo),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    result = run_import(source, destination, "--gpx", latest, host_tz="Asia/Nicosia")
    metadata = read_metadata(destination / CAMERA_JPEG.name, "GPSLatitude", "GPSLongitude")

    assert result.returncode == 0, result.stderr
    assert metadata["GPSLatitude"] == pytest.approx(expected_latitude, abs=0.01)
    assert metadata["GPSLongitude"] == pytest.approx(expected_longitude, abs=0.01)
