"""Tests for byte-preserving photo copies."""

from pathlib import Path

from conftest import CAMERA_JPEG


def test_direct_copy_is_exact_and_rerunnable(camera_source, tmp_path: Path, run_import):
    destination = tmp_path / "destination"
    destination.mkdir()

    first = run_import(camera_source, destination)
    copied = destination / CAMERA_JPEG.name

    assert first.returncode == 0, first.stderr
    assert copied.read_bytes() == (camera_source / CAMERA_JPEG.name).read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o644

    second = run_import(camera_source, destination)
    assert second.returncode == 0, second.stderr
    assert "copied=0" in second.stdout
    assert "identical=1" in second.stdout


def test_different_existing_file_is_a_hard_conflict(camera_source, tmp_path: Path,
                                                    run_import):
    destination = tmp_path / "destination"
    destination.mkdir()
    target = destination / CAMERA_JPEG.name
    target.write_bytes(b"do not overwrite me")

    result = run_import(camera_source, destination)

    assert result.returncode == 1
    assert "CONFLICT" in result.stderr
    assert target.read_bytes() == b"do not overwrite me"


def test_recursive_copy_preserves_relative_directories(camera_source, tmp_path: Path,
                                                       run_import):
    nested = camera_source / "DCIM" / "100TEST"
    nested.mkdir(parents=True)
    (camera_source / CAMERA_JPEG.name).replace(nested / CAMERA_JPEG.name)
    destination = tmp_path / "destination"
    destination.mkdir()

    result = run_import(camera_source, destination, "--recursive")

    assert result.returncode == 0, result.stderr
    assert (destination / "DCIM" / "100TEST" / CAMERA_JPEG.name).is_file()
