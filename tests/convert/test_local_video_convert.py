"""Real bundled-FFmpeg tests for local video conversion."""

import subprocess
from pathlib import Path

import pytest

from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.video_convert import VIDEO_FORMATS, convert_local_video

FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=s=32x24:r=5:d=1", "-c:v", "mpeg4", str(path)], check=True)
    return path


@pytest.mark.parametrize("fmt", list(VIDEO_FORMATS))
def test_formats_keep_source_and_clean_temporary(source, tmp_path, fmt):
    destination = tmp_path / "output"
    destination.mkdir()
    original = source.read_bytes()
    result = convert_local_video(source, destination, fmt, ffmpeg=FFMPEG)
    assert result.suffix == VIDEO_FORMATS[fmt]
    assert result.stat().st_size > 0
    assert source.read_bytes() == original
    assert list(destination.glob("*.converting")) == []


def test_existing_output_is_not_overwritten(source, tmp_path):
    destination = tmp_path / "output"
    destination.mkdir()
    existing = destination / "source.mp4"
    existing.write_bytes(b"existing")
    result = convert_local_video(source, destination, "mp4", ffmpeg=FFMPEG)
    assert result != existing
    assert existing.read_bytes() == b"existing"


def test_failure_keeps_source_and_cleans_temporary(tmp_path):
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"not video")
    with pytest.raises(EngineError, match="Could not convert"):
        convert_local_video(source, tmp_path, "webm", ffmpeg=FFMPEG)
    assert source.read_bytes() == b"not video"
    assert list(tmp_path.glob("*.converting")) == []
