"""Shared fixtures for cpmyphotos integration tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "cpmyphotos.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
CAMERA_JPEG = FIXTURE_DIR / "utc-camera-no-gps.jpg"
UTC_TRACK = FIXTURE_DIR / "utc-track.gpx"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip private-track tests unless their directory was explicitly supplied."""
    assert config is not None
    if os.environ.get("REAL_GPX_DIR"):
        return
    skip = pytest.mark.skip(reason="set REAL_GPX_DIR to run private GPX smoke tests")
    for item in items:
        if "real_gpx" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def require_exiftool() -> None:
    """These CLI integration tests require the same ExifTool as the script."""
    if shutil.which("exiftool") is None:
        pytest.skip("exiftool is not installed")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's real config and card state out of every test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def camera_source(tmp_path: Path) -> Path:
    """Create an isolated source containing the UTC-camera JPEG."""
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(CAMERA_JPEG, source / CAMERA_JPEG.name)
    return source


@pytest.fixture
def run_import():
    """Return a subprocess runner for the public command-line interface."""
    def run(source: Path, destination: Path, *arguments: object,
            host_tz: str | None = None, env: dict[str, str] | None = None):
        process_env = os.environ.copy() if env is None else env.copy()
        if host_tz is not None:
            process_env["TZ"] = host_tz
        command = [
            str(SCRIPT), "--srcdir", str(source), "--dstdir", str(destination),
            *(str(argument) for argument in arguments),
        ]
        return subprocess.run(
            command, text=True, capture_output=True, env=process_env, check=False
        )
    return run


def read_metadata(path: Path, *tags: str) -> dict[str, object]:
    """Read numeric metadata values from one image."""
    command = ["exiftool", "-j", "-n", *(f"-{tag}" for tag in tags), str(path)]
    return json.loads(subprocess.check_output(command, text=True))[0]
