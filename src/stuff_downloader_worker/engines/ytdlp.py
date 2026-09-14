"""Minimal yt-dlp engine for the M0 runtime spike (plan §8.1): analyze or download one URL.

Full format selection, playlists, subtitles and post-processing are M1. yt-dlp is imported
lazily so this module loads in envs without it and fails with a clear error instead.

Options: ``mode`` ("analyze" | "download", default "analyze"), ``format`` (yt-dlp format string).
Deno and ffmpeg are taken only from ``STUFF_DOWNLOADER_TOOLS_DIR``, set by the runner.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..protocol import JobSpec
from .base import Emit, EngineError

# Smallest video + smallest audio (merged by ffmpeg); YouTube often has no muxed format any more.
DEFAULT_FORMAT = "worstvideo*+worstaudio/worst"
TOOLS_DIR_ENV_VAR = "STUFF_DOWNLOADER_TOOLS_DIR"
_TOOL_EXES = {"deno": "deno.exe", "ffmpeg": "ffmpeg.exe"}


def trusted_tool(name: str) -> Path | None:
    """A fixed executable name inside the runner-provided tools dir, or None."""
    tools_dir = os.environ.get(TOOLS_DIR_ENV_VAR)
    if not tools_dir or name not in _TOOL_EXES:
        return None
    path = Path(tools_dir) / _TOOL_EXES[name]
    return path if path.is_file() else None


class YtDlpEngine:
    name = "ytdlp"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise EngineError("engine_missing", f"yt-dlp is not installed here: {exc}") from exc

        opts = job.options
        mode = opts.get("mode", "analyze")
        if mode not in ("analyze", "download"):
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        fmt = opts.get("format", DEFAULT_FORMAT)
        if not isinstance(fmt, str) or not fmt:
            raise EngineError("bad_options", "'format' must be a non-empty string")

        def on_progress(d: dict[str, Any]) -> None:
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            emit(
                "progress",
                {
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "percent": round(100 * done / total, 1) if total else None,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                },
            )

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": fmt,
            "paths": {"home": job.output_dir},
            "outtmpl": "%(title).80B [%(id)s].%(ext)s",
            "progress_hooks": [on_progress],
        }
        # Executables come only from the tools dir the trusted runner names in the environment,
        # never from the job spec, so a job cannot make yt-dlp run an arbitrary program.
        deno = trusted_tool("deno")
        if deno:
            ydl_opts["js_runtimes"] = {"deno": {"path": str(deno)}}
        ffmpeg = trusted_tool("ffmpeg")
        if ffmpeg:
            ydl_opts["ffmpeg_location"] = str(ffmpeg)

        emit("stage", {"stage": "analyzing"})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(job.url, download=False)
                result: dict[str, Any] = {
                    "title": info.get("title"),
                    "id": info.get("id"),
                    "extractor": info.get("extractor_key"),
                    "duration": info.get("duration"),
                    "formats": len(info.get("formats") or []),
                    "engine_version": yt_dlp.version.__version__,
                    "files": [],
                }
                if mode == "download":
                    emit("stage", {"stage": "downloading"})
                    info = ydl.process_ie_result(info, download=True)
                    path = (info.get("requested_downloads") or [{}])[0].get("filepath")
                    if not path or not Path(path).is_file():
                        raise EngineError("no_output", "download finished without an output file")
                    result["files"] = [path]
                    result["total_bytes"] = Path(path).stat().st_size
        except yt_dlp.utils.DownloadError as exc:
            raise EngineError("download_error", str(exc)) from exc
        emit("stage", {"stage": "completed"})
        return result
