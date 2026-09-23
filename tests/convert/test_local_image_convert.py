"""Real FFmpeg checks for the standalone converter."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.image_convert import LOCAL_FORMATS, convert_local_image

FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.png"
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=red:s=16x12", "-frames:v", "1", str(path)], check=True,
    )
    return path


@pytest.mark.parametrize("fmt", list(LOCAL_FORMATS))
def test_converts_all_local_formats_and_keeps_source(source, tmp_path, fmt):
    destination = tmp_path / "output"
    destination.mkdir()
    result = convert_local_image(source, destination, fmt, ffmpeg=FFMPEG)
    assert source.is_file()
    assert result.path.suffix == LOCAL_FORMATS[fmt]
    assert result.path.is_file() and result.path.stat().st_size > 0
    assert list(destination.glob("*.converting*")) == []


def test_existing_output_gets_a_new_name(source, tmp_path):
    destination = tmp_path / "output"
    destination.mkdir()
    existing = destination / "source.jpg"
    existing.write_bytes(b"existing")
    result = convert_local_image(source, destination, "jpg", ffmpeg=FFMPEG)
    assert result.path != existing
    assert existing.read_bytes() == b"existing"


def test_failed_conversion_preserves_source_and_cleans_temporary_file(tmp_path):
    source = tmp_path / "bad.png"
    source.write_bytes(b"not an image")
    destination = tmp_path / "output"
    destination.mkdir()
    with pytest.raises(EngineError):
        convert_local_image(source, destination, "png", ffmpeg=FFMPEG)
    assert source.read_bytes() == b"not an image"
    assert list(destination.iterdir()) == []
