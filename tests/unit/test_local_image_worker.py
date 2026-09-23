from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.runner import JobRun, RunState

FFMPEG = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg.exe"
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="bundled ffmpeg not fetched")


def test_local_conversion_runs_through_worker_and_preserves_source(tmp_path):
    source = tmp_path / "source.png"
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=12x12", "-frames:v", "1", str(source)], check=True,
    )
    events = []
    spec = JobSpec(uuid.uuid4().hex, "imageconvert", str(source), str(tmp_path), {"format": "webp"})
    run = JobRun(spec, events.append)
    run.start()
    assert run.wait(10) is RunState.COMPLETED
    assert events[-1].type == "result"
    assert Path(events[-1].data["files"][0]).is_file()
    assert source.is_file()
