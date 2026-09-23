"""Convert a local video with the bundled FFmpeg while preserving its source."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .engines.base import EngineError
from .engines.http import move_into_place
from .engines.ytdlp import trusted_tool

VIDEO_FORMATS = {"mp4": ".mp4", "mkv": ".mkv", "avi": ".avi", "mov": ".mov",
                 "webm": ".webm", "mpeg": ".mpeg", "mpg": ".mpg"}
TIMEOUT = 3600


def convert_local_video(source: Path, destination: Path, fmt: str, *,
                        job_id: str = "local", ffmpeg: Path | None = None,
                        emit=None, timeout: float = TIMEOUT) -> Path:
    if fmt not in VIDEO_FORMATS:
        raise EngineError("bad_options", "unsupported video output format")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise EngineError("bad_options", "invalid conversion job ID")
    if not source.is_file():
        raise EngineError("convert_error", "source video does not exist")
    if not destination.is_dir():
        raise EngineError("convert_error", "destination folder does not exist")
    ffmpeg = ffmpeg or trusted_tool("ffmpeg")
    if ffmpeg is None or not ffmpeg.is_file():
        raise EngineError("convert_error", "FFmpeg is needed to convert videos")

    temp_dir = Path(tempfile.mkdtemp(
        prefix=f".{source.stem[:40]}.{job_id}.", suffix=".converting", dir=destination
    ))
    temp = temp_dir / ("video" + VIDEO_FORMATS[fmt])
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source)]
    if fmt == "webm":
        args += ["-c:v", "libvpx-vp9", "-c:a", "libopus"]
    else:
        args += ["-c:v", "mpeg4", "-c:a", "aac"]
    args.append(str(temp))
    if emit is not None:
        emit("stage", {"stage": "converting", "temporary_path": str(temp_dir)})
    try:
        try:
            proc = subprocess.run(
                args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("convert_error", "video conversion timed out") from exc
        except OSError as exc:
            raise EngineError("convert_error", "FFmpeg could not be started") from exc
        if proc.returncode or not temp.is_file() or temp.stat().st_size == 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
            reason = detail[-1][:200] if detail else "FFmpeg did not produce a video"
            raise EngineError("convert_error", f"Could not convert to {fmt.upper()}: {reason}")
        return move_into_place(temp, destination, source.stem + VIDEO_FORMATS[fmt])
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
