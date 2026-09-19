"""yt-dlp engine: analyze one video page, or download it with a preset.

Since M3 the URL may name any public site, not just YouTube. Engine failure text from a site we
do not control is therefore not passed through as-is: ``describe_download_error`` classifies it
and strips any URL out of it, so a signed or tokenized link cannot reach the log or history.

yt-dlp is imported lazily so this module loads in envs without it and fails with a clear error.

Options: ``mode`` "analyze" (default), "playlist" or "download". Download also takes
``preset``, ``height``, ``compatible``, ``crop_cover``, ``playlist_index``/``playlist_title``/
``playlist_count`` and ``archive``, validated by ``presets.parse_request``. Deno and ffmpeg
come only from ``STUFF_DOWNLOADER_TOOLS_DIR``, which the runner sets.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import presets, tagging
from ..protocol import JobSpec
from .base import Emit, EngineError

TOOLS_DIR_ENV_VAR = "STUFF_DOWNLOADER_TOOLS_DIR"
_TOOL_EXES = {"deno": "deno.exe", "ffmpeg": "ffmpeg.exe"}

# Fields an analyze result may carry. Stream URLs, headers, cookies and fragments never leave.
_FORMAT_FIELDS = (
    "format_id",
    "ext",
    "vcodec",
    "acodec",
    "height",
    "width",
    "fps",
    "tbr",
    "abr",
    "filesize",
    "filesize_approx",
    "dynamic_range",
    "protocol",
    "format_note",
)
_THUMB_HOST_SUFFIXES = (".ytimg.com", ".ggpht.com", ".googleusercontent.com")
MAX_THUMB_BYTES = 1_500_000

# A playlist is a list of other people's videos, so every field below is attacker-influenced:
# nothing is passed through except values re-validated and rebuilt here.
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_PLAYLIST_ENTRIES = 500
MAX_ENTRY_TEXT = 300
MAX_ERROR_TEXT = 500

# yt-dlp puts the failing URL in most of its messages, and for a generic site that URL can carry
# a signature or a session token. Every one is replaced before the message leaves the worker.
_URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_REDACTED_URL = "[link]"

# (substring of the engine message, lower-case) -> error code. First match wins.
_ERROR_CODES: tuple[tuple[str, str], ...] = (
    ("unsupported url", "unsupported"),
    ("no video formats found", "unsupported"),
    ("is not a valid url", "unsupported"),
)

_UNAVAILABLE_REASONS = {
    "private": "Private video",
    "premium_only": "Members or Premium only",
    "subscriber_only": "Members only",
    "needs_auth": "Needs a signed-in account",
    "unlisted": "",
    "public": "",
}

# yt-dlp postprocessor names → protocol stages.
_PP_STAGES = {
    "Merger": "merging",
    "FFmpegMerger": "merging",
    "ExtractAudio": "converting",
    "FFmpegExtractAudio": "converting",
    "ThumbnailsConvertor": "converting",
    "FFmpegThumbnailsConvertor": "converting",
    "Metadata": "tagging",
    "FFmpegMetadata": "tagging",
    "EmbedThumbnail": "tagging",
}


def trusted_tool(name: str) -> Path | None:
    """A fixed executable name inside the runner-provided tools dir, or None."""
    tools_dir = os.environ.get(TOOLS_DIR_ENV_VAR)
    if not tools_dir or name not in _TOOL_EXES:
        return None
    path = Path(tools_dir) / _TOOL_EXES[name]
    return path if path.is_file() else None


def _thumbnail_url(info: dict[str, Any]) -> str | None:
    """The largest https thumbnail on a known image host, preferring square art."""
    best: tuple[tuple[int, int], str] | None = None
    for thumb in info.get("thumbnails") or []:
        if not isinstance(thumb, dict) or not isinstance(thumb.get("url"), str):
            continue
        parts = urlsplit(thumb["url"])
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(_THUMB_HOST_SUFFIXES):
            continue
        width, height = thumb.get("width") or 0, thumb.get("height") or 0
        if not isinstance(width, int) or not isinstance(height, int):
            width = height = 0
        key = (int(bool(width) and width == height), width * height)
        if best is None or key > best[0]:
            best = (key, thumb["url"])
    if best:
        return best[1]
    url = info.get("thumbnail")
    if isinstance(url, str):
        parts = urlsplit(url)
        if parts.scheme == "https" and (parts.hostname or "").lower().endswith(
            _THUMB_HOST_SUFFIXES
        ):
            return url
    return None


def sanitize_info(info: dict[str, Any]) -> dict[str, Any]:
    formats = []
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict) and fmt.get("format_id") not in (None, ""):
            if str(fmt.get("protocol", "")).startswith("mhtml"):  # storyboards
                continue
            formats.append({k: fmt[k] for k in _FORMAT_FIELDS if fmt.get(k) is not None})
    text_fields = ("id", "title", "uploader", "channel", "artist", "track", "album", "extractor")
    result: dict[str, Any] = {k: info.get(k) for k in text_fields if isinstance(info.get(k), str)}
    for key in ("duration", "release_year", "view_count"):
        value = info.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = value
    result["extractor"] = info.get("extractor_key") or info.get("extractor")
    result["formats"] = formats
    return result


def redact_urls(text: str) -> str:
    """``text`` with every URL replaced, so tokens and signatures never leave the worker."""
    return _URL_IN_TEXT.sub(_REDACTED_URL, text)


def describe_download_error(message: str) -> tuple[str, str]:
    """An engine failure as (code, message the app may show), with URLs removed."""
    safe = redact_urls(message).strip()[:MAX_ERROR_TEXT] or "the download failed"
    lowered = safe.lower()
    for needle, code in _ERROR_CODES:
        if needle in lowered:
            return code, safe
    return "download_error", safe


def _short_text(value: Any) -> str | None:
    return value[:MAX_ENTRY_TEXT] if isinstance(value, str) and value else None


def sanitize_entry(entry: Any, index: int) -> dict[str, Any] | None:
    """One playlist row, rebuilt from scratch. ``None`` when there is no usable video id."""
    if not isinstance(entry, dict):
        return None
    video_id = entry.get("id")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return None
    title = _short_text(entry.get("title")) or "Untitled"
    row: dict[str, Any] = {
        "id": video_id,
        # Never entry["url"] or entry["webpage_url"]: rebuilt from the validated id instead.
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "index": index,
        "title": title,
        "uploader": _short_text(entry.get("uploader") or entry.get("channel")),
    }
    duration = entry.get("duration")
    if isinstance(duration, int | float) and not isinstance(duration, bool) and duration >= 0:
        row["duration"] = float(duration)
    availability = entry.get("availability")
    reason = ""
    if isinstance(availability, str):
        reason = _UNAVAILABLE_REASONS.get(availability, "Not available")
    if not reason and entry.get("live_status") in ("is_upcoming", "post_live"):
        reason = "Not available yet"
    if not reason and title in ("[Private video]", "[Deleted video]"):
        reason = title.strip("[]")
    if reason:
        row["unavailable"] = reason
    return row


def sanitize_playlist(info: dict[str, Any]) -> dict[str, Any]:
    """A flat playlist extraction, reduced to fields the GUI can safely display."""
    raw_entries = info.get("entries") or []
    entries = []
    for raw in raw_entries[:MAX_PLAYLIST_ENTRIES]:
        row = sanitize_entry(raw, len(entries) + 1)
        if row is not None:
            entries.append(row)
    playlist_id = info.get("id")
    return {
        "kind": "playlist",
        "playlist_id": playlist_id if isinstance(playlist_id, str) else "",
        "title": _short_text(info.get("title")) or "Playlist",
        "uploader": _short_text(info.get("uploader") or info.get("channel")),
        "entries": entries,
        "truncated": len(raw_entries) > MAX_PLAYLIST_ENTRIES,
    }


def _rearm_archive(ydl: Any, archive_path: str | None) -> None:
    """Put the download archive back after extraction has run without it.

    yt-dlp preloads the archive into ``ydl.archive`` at construction and, when an id is
    already in it, breaks out of the extractor loop and returns None. That reaches us as
    "no media information found" — a failed download — instead of a skip. So extraction
    runs with no archive at all and it is restored here, leaving the skip branch in
    ``_download`` to be the thing that decides.
    """
    if not archive_path:
        return
    ydl.params["download_archive"] = archive_path  # so a finished download is recorded
    try:
        with open(archive_path, encoding="utf-8") as handle:
            ydl.archive = {line.strip() for line in handle if line.strip()}
    except FileNotFoundError:
        ydl.archive = set()  # nothing downloaded yet; not an error


class YtDlpEngine:
    name = "ytdlp"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise EngineError("engine_missing", f"yt-dlp is not installed here: {exc}") from exc

        opts = job.options
        mode = opts.get("mode", "analyze")
        if mode not in ("analyze", "playlist", "download"):
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        if mode in ("analyze", "playlist"):
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", f"{mode} takes no options")
            request = None
            ydl_opts: dict[str, Any] = {"noplaylist": mode == "analyze"}
            if mode == "playlist":
                ydl_opts.update(
                    {
                        "extract_flat": "in_playlist",
                        "playlistend": MAX_PLAYLIST_ENTRIES,
                        "skip_download": True,
                    }
                )
        else:
            request = presets.parse_request(opts)
            ydl_opts = presets.build_ydl_opts(request, job.output_dir)

        files: list[str] = []
        last_stage = [""]

        def stage(name: str) -> None:
            if name != last_stage[0]:
                last_stage[0] = name
                emit("stage", {"stage": name})

        def on_progress(d: dict[str, Any]) -> None:
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            fmt = (d.get("info_dict") or {}).get("vcodec")
            stage("downloading audio" if fmt in (None, "none") else "downloading video")
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

        def on_postprocess(d: dict[str, Any]) -> None:
            name = str(d.get("postprocessor") or "")
            if d.get("status") == "started" and name in _PP_STAGES:
                stage(_PP_STAGES[name])
            if d.get("status") == "finished" and request and request.preset == "thumbnail":
                for thumb in (d.get("info_dict") or {}).get("thumbnails") or []:
                    path = thumb.get("filepath") if isinstance(thumb, dict) else None
                    if path and path not in files:
                        files.append(path)

        def on_final(path: str) -> None:
            if path not in files:
                files.append(path)

        ydl_opts.update(
            {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "progress_hooks": [on_progress],
                "postprocessor_hooks": [on_postprocess],
                "post_hooks": [on_final],
            }
        )
        # Executables come only from the tools dir the trusted runner names in the environment,
        # never from the job spec, so a job cannot make yt-dlp run an arbitrary program.
        deno = trusted_tool("deno")
        if deno:
            ydl_opts["js_runtimes"] = {"deno": {"path": str(deno)}}
        ffmpeg = trusted_tool("ffmpeg")
        if ffmpeg:
            ydl_opts["ffmpeg_location"] = str(ffmpeg)

        # See _rearm_archive: extraction must not see the archive, or an already-downloaded
        # id makes yt-dlp return None and the job is reported as failed instead of skipped.
        archive_path = ydl_opts.pop("download_archive", None)
        emit("stage", {"stage": "analyzing"})
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(job.url, download=False)
                if not isinstance(info, dict):
                    raise EngineError("download_error", "no media information found")
                _rearm_archive(ydl, archive_path)
                is_playlist = info.get("_type") in ("playlist", "multi_video")
                if mode == "playlist":
                    if not is_playlist:
                        raise EngineError("unsupported", "that link is not a playlist")
                    listing = sanitize_playlist(info)
                    listing["engine_version"] = yt_dlp.version.__version__
                    emit("stage", {"stage": "completed"})
                    return listing
                if is_playlist:
                    raise EngineError("unsupported", "that link is a playlist, not a video")
                summary = sanitize_info(info)
                summary["engine_version"] = yt_dlp.version.__version__
                if request is None:
                    summary["thumbnail"] = self._thumbnail_preview(ydl, info, emit)
                    emit("stage", {"stage": "completed"})
                    return summary
                return self._download(ydl, info, request, summary, files, stage, emit)
        except yt_dlp.utils.DownloadError as exc:
            raise EngineError(*describe_download_error(str(exc))) from exc

    @staticmethod
    def _thumbnail_preview(ydl: Any, info: dict[str, Any], emit: Emit) -> dict[str, Any] | None:
        url = _thumbnail_url(info)
        if not url:
            return None
        try:
            with ydl.urlopen(url) as resp:
                data = resp.read(MAX_THUMB_BYTES + 1)
        except Exception as exc:  # a missing preview must not fail the analyze
            emit("log", {"level": "warning", "message": f"thumbnail preview failed: {exc}"})
            return None
        if len(data) > MAX_THUMB_BYTES:
            return None
        return {"data": base64.b64encode(data).decode("ascii")}

    @staticmethod
    def _download(
        ydl: Any,
        info: dict[str, Any],
        request: presets.DownloadRequest,
        summary: dict[str, Any],
        files: list[str],
        stage: Any,
        emit: Emit,
    ) -> dict[str, Any]:
        if request.archive and ydl.in_download_archive(info):
            stage("completed")
            skipped = {k: v for k, v in summary.items() if k != "formats"}
            skipped.update(
                {
                    "preset": request.preset,
                    "files": [],
                    "total_bytes": 0,
                    "skipped": True,
                    "skipped_reason": "Already downloaded",
                }
            )
            return skipped
        stage("downloading")
        done = ydl.process_ie_result(info, download=True)
        existing = [p for p in files if Path(p).is_file()]
        if not existing and isinstance(done, dict):
            path = (done.get("requested_downloads") or [{}])[0].get("filepath")
            if path and Path(path).is_file():
                existing = [path]
        if not existing:
            raise EngineError("no_output", "download finished without an output file")

        result = {k: v for k, v in summary.items() if k not in ("formats",)}
        result["preset"] = request.preset
        result["files"] = existing
        result["total_bytes"] = sum(Path(p).stat().st_size for p in existing)
        if request.height and isinstance(done, dict):
            got = max(
                (f.get("height") or 0 for f in done.get("requested_formats") or [done]),
                default=0,
            )
            if got and got > request.height:
                emit(
                    "log",
                    {
                        "level": "warning",
                        "message": f"{request.height}p was not available; got {got}p instead",
                    },
                )
        if request.preset == "mp3_music":
            stage("tagging")
            mp3s = [p for p in existing if p.lower().endswith(".mp3")]
            if mp3s:
                result["tags"] = tagging.verify_mp3(
                    mp3s[0],
                    info,
                    album=request.playlist_title,
                    track_number=request.playlist_index,
                    track_total=request.playlist_count,
                )
        stage("completed")
        return result
