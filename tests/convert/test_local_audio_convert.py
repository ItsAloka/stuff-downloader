"""Real bundled-FFmpeg tests for local audio conversion."""

import subprocess
from pathlib import Path

import pytest

from stuff_downloader_worker.audio_convert import AUDIO_FORMATS, convert_local_audio
from stuff_downloader_worker.engines.base import EngineError

FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.wav"
    subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.2", str(path)], check=True)
    return path


@pytest.mark.parametrize("fmt", list(AUDIO_FORMATS))
def test_all_formats_preserve_source_and_clean_temporary(source, tmp_path, fmt):
    destination = tmp_path / "output"
    destination.mkdir()
    original = source.read_bytes()
    result = convert_local_audio(source, destination, fmt, ffmpeg=FFMPEG)
    assert result.suffix == AUDIO_FORMATS[fmt][0]
    assert result.stat().st_size > 0
    assert source.read_bytes() == original
    assert list(destination.glob("*.converting")) == []


def test_existing_output_is_not_overwritten(source, tmp_path):
    existing = tmp_path / "source.wav"
    original = existing.read_bytes()
    result = convert_local_audio(source, tmp_path, "wav", ffmpeg=FFMPEG)
    assert result != existing
    assert existing.read_bytes() == original


def test_broken_input_keeps_source_and_cleans_temporary(tmp_path):
    source = tmp_path / "broken.wav"
    source.write_bytes(b"not audio")
    with pytest.raises(EngineError, match="Could not convert to MP3"):
        convert_local_audio(source, tmp_path, "mp3", ffmpeg=FFMPEG)
    assert source.read_bytes() == b"not audio"
    assert list(tmp_path.glob("*.converting")) == []


def test_unsupported_format_is_clear(source, tmp_path):
    with pytest.raises(EngineError, match="unsupported audio output format"):
        convert_local_audio(source, tmp_path, "xyz", ffmpeg=FFMPEG)
