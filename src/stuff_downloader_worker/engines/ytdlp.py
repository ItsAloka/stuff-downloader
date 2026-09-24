"""yt-dlp engine: analyze one video page, or download it with a preset.

Since M3 the URL may name any public site, not just YouTube. Engine failure text from a site we
do not control is therefore not passed through as-is: ``describe_download_error`` classifies it
and strips any URL out of it, so a signed or tokenized link cannot reach the log or history.

yt-dlp is imported lazily so this module loads in envs without it and fails with a clear error.

Options: ``mode`` "analyze" (default), "playlist" or "download". Download also takes
``preset``, ``height``, ``compatible``, ``crop_cover``, ``playlist_index``/``playlist_title``/
``playlist_count`` and ``archive``, validated by ``presets.parse_request``. Any mode may also
carry ``site_login`` (plan §6.4), validated by ``site_login.ydl_options``. Deno and ffmpeg
come only from ``STUFF_DOWNLOADER_TOOLS_DIR``, which the runner sets.
"""

from __future__ import annotations

import base64
import glob
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import presets, site_login, tagging
from ..protocol import JobSpec, media_result
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

# The "All formats" table also lists subtitle and thumbnail tracks (plan §5.5). Only the facts a
# row needs survive: a language code, the file types and a size — never a track URL.
_LANG = re.compile(r"[A-Za-z0-9-]{1,20}")
_SUB_EXT = re.compile(r"[a-z0-9]{1,8}")
MAX_SUBTITLE_TRACKS = 60
MAX_THUMBNAIL_ROWS = 20

# yt-dlp puts the failing URL in most of its messages, and for a generic site that URL can carry
# a signature or a session token. Every one is replaced before the message leaves the worker.
_URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_REDACTED_URL = "[link]"

# (substring of the engine message, lower-case) -> error code. First match wins.
_ERROR_CODES: tuple[tuple[str, str], ...] = (
    ("unsupported url", "unsupported"),
    ("no video formats found", "unsupported"),
    ("is not a valid url", "unsupported"),
    # A photo-only post on a site yt-dlp knows: not an error, a job for gallery-dl.
    ("no video could be found", "unsupported"),
    ("there is no video in this post", "unsupported"),
    ("no video in this", "unsupported"),
)

_UNAVAILABLE_REASONS = {
    "private": "Private video",
    "premium_only": "Members or Premium only",
    "subscriber_only": "Members only",
    "needs_auth": "Needs a signed-in account",
    "unlisted": "",
    "public": "",
}

CLAIM_SUFFIX = ".sdclaim"
MAX_NAME_TRIES = 1000
# yt-dlp's own in-progress files. A paused job left them; they must not push its resume to
# "name (2)". Running jobs are kept apart by their claim file instead.
_TRANSIENT = re.compile(r"(\.part|\.ytdl|\.part-Frag\d+|\.f[\w-]+\.\w+|\.temp\.\w+)$")


def _release(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def _taken(pattern: str, own: Path | None = None) -> bool:
    return any(Path(p) != own and not _TRANSIENT.search(p) for p in glob.glob(pattern))


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
    result["subtitles"] = sanitize_subtitles(info.get("subtitles"), auto=False)
    result["automatic_captions"] = sanitize_subtitles(info.get("automatic_captions"), auto=True)
    result["thumbnails"] = sanitize_thumbnails(info.get("thumbnails"))
    return result


# Plan §5.3. yt-dlp leaves vcodec out for some generic pages, so a format with no codec facts at
# all counts as video when its container or size says so, and as audio when its container does.
_AUDIO_EXTS = frozenset({"mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav", "wma"})
_MUSIC_HOSTS = frozenset({"music.youtube.com"})


def _has_video(fmt: dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    if vcodec is not None:
        return vcodec != "none"
    if fmt.get("height") or fmt.get("width"):
        return True
    return fmt.get("acodec") in (None, "none") and fmt.get("ext") not in _AUDIO_EXTS


def media_kind(info: dict[str, Any], url: str) -> tuple[str, list[str]]:
    """(kind, tabs) for one analyzed page, from the sanitized info and the analyzed URL.

    YouTube Music, or a YouTube upload that names a track and an artist (Topic channels, Art
    Tracks), is a song that also has a video. A page whose every format is audio (SoundCloud,
    Bandcamp…) is audio with a cover and nothing else. Anything with a video stream is a video.
    """
    formats = [f for f in info.get("formats") or [] if isinstance(f, dict)]
    host = (urlsplit(url).hostname or "").lower()
    youtube = str(info.get("extractor") or "").lower().startswith("youtube")
    if host in _MUSIC_HOSTS or (youtube and info.get("track") and info.get("artist")):
        return "audio", ["audio", "video", "image"]
    if formats and not any(_has_video(f) for f in formats):
        return "audio", ["audio", "image"]
    return "video", ["video", "audio", "image"]


def _site_name(info: dict[str, Any]) -> str | None:
    name = info.get("extractor")
    return name[:60] if isinstance(name, str) and name else None


# ── the format catalog (plan §5.4) ─────────────────────────────────────────────────────────
# Rows are built from the sanitized formats only, and their ids are ours: the worker maps them
# back to selectors in presets.build_row_opts, so no site format id is ever sent back to it.
DEFAULT_MAX_HEIGHT = 1080
_CODEC_NAMES = (
    ("avc", "H.264"),
    ("h264", "H.264"),
    ("hev", "H.265"),
    ("hvc", "H.265"),
    ("vp09", "VP9"),
    ("vp9", "VP9"),
    ("av01", "AV1"),
    ("mp4a", "AAC"),
    ("aac", "AAC"),
    ("opus", "Opus"),
    ("vorbis", "Vorbis"),
    ("mp3", "MP3"),
    ("flac", "FLAC"),
)
# WAV as yt-dlp writes it from a 48 kHz stereo source; FLAC is roughly half to two thirds.
_WAV_BYTES_PER_SECOND = 48_000 * 2 * 2
_FLAC_SHARE = 0.6


def codec_name(codec: Any) -> str | None:
    text = str(codec or "").lower()
    if text in ("", "none"):
        return None
    for prefix, name in _CODEC_NAMES:
        if text.startswith(prefix):
            return name
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _size(fmt: dict[str, Any] | None) -> tuple[int | None, bool]:
    """(bytes, is_estimate) for one format: filesize, else filesize_approx marked "~"."""
    if not fmt:
        return None, False
    exact = _number(fmt.get("filesize"))
    if exact:
        return int(exact), False
    approx = _number(fmt.get("filesize_approx"))
    return (int(approx), True) if approx else (None, False)


def _audio_only(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        f
        for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]


def _best_audio(formats: list[dict[str, Any]], codec: str | None = None) -> dict | None:
    pool = [
        f for f in _audio_only(formats) if codec is None or codec_name(f.get("acodec")) == codec
    ]
    return max(
        pool, key=lambda f: _number(f.get("abr")) or _number(f.get("tbr")) or 0, default=None
    )


def source_audio(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The best audio the site offers, for the honest "Source audio" line."""
    best = _best_audio(formats)
    if best is None:
        return None
    abr = _number(best.get("abr")) or _number(best.get("tbr"))
    return {
        "codec": codec_name(best.get("acodec")) or "unknown",
        "abr_kbps": round(abr) if abr else None,
    }


def _video_rank(fmt: dict[str, Any]) -> tuple:
    family = codec_name(fmt.get("vcodec"))
    return (
        fmt.get("ext") == "mp4" and family == "H.264",
        fmt.get("ext") == "mp4",
        _number(fmt.get("fps")) or 0,
        _number(fmt.get("tbr")) or 0,
    )


def video_rows(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per height that exists, highest first. Each height names its best stream:
    H.264 in MP4 first (plays everywhere), then other MP4, then WebM."""
    by_height: dict[int, dict[str, Any]] = {}
    for fmt in formats:
        height = fmt.get("height")
        if not _has_video(fmt) or not isinstance(height, int) or isinstance(height, bool):
            continue
        if not 0 < height <= presets.MAX_ROW_HEIGHT or fmt.get("ext") not in ("mp4", "webm"):
            continue
        if height not in by_height or _video_rank(fmt) > _video_rank(by_height[height]):
            by_height[height] = fmt
    # ★ is the highest H.264 height up to 1080p, as in Settings (plan §5.4).
    default_height = max(
        (
            h
            for h, f in by_height.items()
            if h <= DEFAULT_MAX_HEIGHT and codec_name(f.get("vcodec")) == "H.264"
        ),
        default=None,
    )
    if default_height is None:
        default_height = max((h for h in by_height if h <= DEFAULT_MAX_HEIGHT), default=None)
    rows = []
    for height in sorted(by_height, reverse=True):
        fmt = by_height[height]
        size, estimate = _size(fmt)
        if fmt.get("acodec") in (None, "none"):  # video-only: add the audio it is merged with
            audio = _best_audio(formats, "AAC" if fmt.get("ext") == "mp4" else "Opus")
            audio_size, audio_estimate = _size(audio or _best_audio(formats))
            size = size + audio_size if size and audio_size else None
            estimate = estimate or audio_estimate
        fps = _number(fmt.get("fps"))
        rows.append(
            {
                "id": f"v:{height}:{fmt['ext']}",
                "height": height,
                "fps": round(fps) if fps else None,
                "hdr": str(fmt.get("dynamic_range") or "SDR").upper() != "SDR",
                "vcodec": codec_name(fmt.get("vcodec")),
                "container": fmt["ext"],
                "size": size,
                "size_is_estimate": estimate,
                "default": height == default_height,
            }
        )
    return rows


def audio_rows(formats: list[dict[str, Any]], duration: Any) -> list[dict[str, Any]]:
    """MP3 at five bitrates, M4A, Opus when the source is Opus, FLAC and WAV (plan §5.4)."""
    seconds = _number(duration)

    def estimate(bytes_per_second: float) -> int | None:
        return int(seconds * bytes_per_second) if seconds else None

    rows: list[dict[str, Any]] = [
        {
            "id": f"a:mp3:{kbps}",
            "label": "MP3",
            "codec": "mp3",
            "bitrate": kbps,
            "size": estimate(kbps * 1000 / 8),
            "size_is_estimate": True,
            "default": kbps == 320,
        }
        for kbps in presets.MP3_BITRATES
    ]
    aac = _best_audio(formats, "AAC")
    if aac is not None:
        size, approx = _size(aac)
        abr = _number(aac.get("abr"))
        m4a = {"bitrate": round(abr) if abr else None, "copy": True}
        m4a.update({"size": size, "size_is_estimate": approx})
    else:
        m4a = {"bitrate": 256, "copy": False}
        m4a.update({"size": estimate(256 * 1000 / 8), "size_is_estimate": True})
    rows.append({"id": "a:m4a", "label": "M4A", "codec": "aac", **m4a})
    opus = _best_audio(formats, "Opus")
    if opus is not None:
        size, approx = _size(opus)
        abr = _number(opus.get("abr"))
        rows.append(
            {
                "id": "a:opus",
                "label": "Opus",
                "codec": "opus",
                "bitrate": round(abr) if abr else None,
                "copy": True,
                "size": size,
                "size_is_estimate": approx,
            }
        )
    wav = estimate(_WAV_BYTES_PER_SECOND)
    rows.append(
        {
            "id": "a:flac",
            "label": "FLAC",
            "codec": "flac",
            "bitrate": None,
            "size": int(wav * _FLAC_SHARE) if wav else None,
            "size_is_estimate": True,
            "lossless_note": "lossless container: same sound, bigger file",
        }
    )
    rows.append(
        {
            "id": "a:wav",
            "label": "WAV",
            "codec": "wav",
            "bitrate": None,
            "size": wav,
            "size_is_estimate": True,
            "lossless_note": "uncompressed: same sound, much bigger · no embedded cover",
            "no_cover": True,
        }
    )
    return rows


def image_rows(thumbnails: list[dict[str, int]], has_thumbnail: bool) -> list[dict[str, Any]]:
    """The thumbnail sizes that exist, largest first; one "best" row when none has a size."""
    rows: list[dict[str, Any]] = [
        {"id": f"i:{t['width']}x{t['height']}", "width": t["width"], "height": t["height"]}
        for t in thumbnails
    ]
    if not rows and has_thumbnail:
        rows = [{"id": "i:best", "width": None, "height": None}]
    if rows:
        rows[0]["default"] = True
    return rows


def analyze_result(
    summary: dict[str, Any],
    url: str,
    preview: dict[str, Any] | None,
    has_thumbnail: bool = True,
) -> dict[str, Any]:
    """The sanitized page as a MediaResult with its Video / Audio / Image rows."""
    kind, tabs = media_kind(summary, url)
    fields = {k: v for k, v in summary.items() if k != "title"}
    fields.setdefault("uploader", summary.get("channel"))
    fields["site"] = _site_name(summary)
    formats = [f for f in summary.get("formats") or [] if isinstance(f, dict)]
    fields["video_rows"] = video_rows(formats) if "video" in tabs else []
    fields["audio_rows"] = audio_rows(formats, summary.get("duration"))
    fields["image_rows"] = image_rows(summary.get("thumbnails") or [], has_thumbnail)
    fields["source_audio"] = source_audio(formats)
    # A tab with nothing in it is not offered (a page with no thumbnail has no Image tab).
    tabs = [t for t in tabs if t == tabs[0] or fields.get(f"{t}_rows")]
    title = summary.get("title") or "Untitled"
    return media_result(kind, tabs, title, url, preview=preview, **fields)


def playlist_result(listing: dict[str, Any], url: str) -> dict[str, Any]:
    """A playlist listing as a MediaResult; its rows are the MediaResult ``entries``."""
    fields = {k: v for k, v in listing.items() if k not in ("kind", "title")}
    return media_result("playlist", ["tracks"], listing["title"], url, **fields)


def sanitize_subtitles(raw: Any, auto: bool) -> list[dict[str, Any]]:
    """Subtitle tracks as {lang, exts, auto}. Languages are validated codes, nothing else."""
    tracks: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return tracks
    for lang, entries in raw.items():
        if len(tracks) >= MAX_SUBTITLE_TRACKS:
            break
        if not isinstance(lang, str) or not _LANG.fullmatch(lang) or lang == "live_chat":
            continue
        exts = []
        for entry in entries if isinstance(entries, list) else []:
            ext = entry.get("ext") if isinstance(entry, dict) else None
            if isinstance(ext, str) and _SUB_EXT.fullmatch(ext) and ext not in exts:
                exts.append(ext)
        if exts:
            tracks.append({"lang": lang, "exts": exts[:6], "auto": auto})
    return tracks


def _is_dimension(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 20000


def sanitize_thumbnails(raw: Any) -> list[dict[str, int]]:
    """Thumbnail sizes, largest first. The image URLs stay in the worker."""
    sizes: set[tuple[int, int]] = set()
    for thumb in raw if isinstance(raw, list) else []:
        if not isinstance(thumb, dict):
            continue
        width, height = thumb.get("width"), thumb.get("height")
        if all(_is_dimension(v) for v in (width, height)):
            sizes.add((width, height))
    ordered = sorted(sizes, key=lambda s: s[0] * s[1], reverse=True)[:MAX_THUMBNAIL_ROWS]
    return [{"width": w, "height": h} for w, h in ordered]


def redact_urls(text: str) -> str:
    """``text`` with every URL replaced, so tokens and signatures never leave the worker."""
    return _URL_IN_TEXT.sub(_REDACTED_URL, text)


def describe_download_error(message: str) -> tuple[str, str]:
    """An engine failure as (code, message the app may show), with URLs removed."""
    safe = site_login.redact_secrets(redact_urls(message)).strip()[:MAX_ERROR_TEXT]
    safe = safe or "the download failed"
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

        opts = dict(job.options)
        login = opts.pop("site_login", None)
        login_opts = site_login.ydl_options(login) if login is not None else {}
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
        elif presets.is_row_request(opts):
            request = presets.parse_row_request(opts)
            ydl_opts = presets.build_row_opts(request, job.output_dir)
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
        if login_opts:
            ydl_opts.update(login_opts)
            ydl_opts["logger"] = site_login.SilentLogger()

        # See _rearm_archive: extraction must not see the archive, or an already-downloaded
        # id makes yt-dlp return None and the job is reported as failed instead of skipped.
        archive_path = ydl_opts.pop("download_archive", None)
        emit("stage", {"stage": "analyzing"})
        ydl = self._open(yt_dlp, ydl_opts, bool(login_opts))
        try:
            with ydl:
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
                    return playlist_result(listing, job.url)
                if is_playlist:
                    raise EngineError("unsupported", "that link is a playlist, not a video")
                summary = sanitize_info(info)
                summary["engine_version"] = yt_dlp.version.__version__
                if request is None:
                    preview = self._thumbnail_preview(ydl, info, emit)
                    emit("stage", {"stage": "completed"})
                    has_thumb = bool(info.get("thumbnails") or info.get("thumbnail"))
                    return analyze_result(summary, job.url, preview, has_thumb)
                size = getattr(request, "image_size", None)
                if size:
                    # The chosen thumbnail only; yt-dlp writes the last one in the list.
                    chosen = [
                        t
                        for t in info.get("thumbnails") or []
                        if isinstance(t, dict) and (t.get("width"), t.get("height")) == size
                    ]
                    if not chosen:
                        raise EngineError("download_error", "that image size is not available")
                    info["thumbnails"] = chosen[-1:]
                return self._download(ydl, info, request, summary, files, stage, emit)
        except yt_dlp.utils.DownloadError as exc:
            raise EngineError(*describe_download_error(str(exc))) from exc

    @staticmethod
    def _open(yt_dlp: Any, ydl_opts: dict[str, Any], with_login: bool) -> Any:
        """A YoutubeDL instance; with a login, its cookies loaded now and the file detached.

        yt-dlp loads cookies lazily (possibly in its constructor) and, on close, writes the jar
        back to ``cookiefile`` — which would rewrite the owner's cookies.txt with whatever the
        site set during this job. So the jar is forced to load here, where a failure can be
        reported with fixed text, and ``cookiefile`` is then cleared so nothing is saved back.
        """
        if not with_login:
            return yt_dlp.YoutubeDL(ydl_opts)
        try:
            ydl = yt_dlp.YoutubeDL(ydl_opts)
            ydl.cookiejar  # noqa: B018 - force the load inside this try
        except Exception:
            raise EngineError("cookies_unavailable", site_login.COOKIES_FAILED) from None
        ydl.params["cookiefile"] = None
        return ydl

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
    def _claim_name(ydl: Any, info: dict[str, Any]) -> tuple[str, Path] | None:
        """Reserve a free output stem, ``name`` then ``name (2)``…, and pin yt-dlp to it.

        yt-dlp skips a download whose final file already exists and reports that old file, so
        a second track with the same "Artist - Title" (or any earlier file) would be tagged and
        reported as this job's output. An ``O_EXCL`` lock file makes the reservation atomic
        against the other jobs running in the same folder. Returns ``(stem, lock)`` or ``None``
        when the plan cannot be made, in which case yt-dlp's own naming is left alone.
        """
        try:
            planned = Path(ydl.prepare_filename(info))
        except Exception:
            return None
        folder, stem = planned.parent, planned.stem
        if not stem:
            return None
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        for n in range(1, MAX_NAME_TRIES + 1):
            candidate = stem if n == 1 else f"{stem} ({n})"
            pattern = glob.escape(str(folder / candidate)) + ".*"
            if _taken(pattern):
                continue
            lock = folder / (candidate + CLAIM_SUFFIX)
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                continue
            except OSError:
                return None
            # A job that held this name may have finished between the check and the lock.
            if _taken(pattern, own=lock):
                _release(lock)
                continue
            literal = presets.literal_outtmpl(candidate)
            outtmpl = ydl.params.get("outtmpl")
            if isinstance(outtmpl, dict):
                outtmpl["default"] = literal
            else:
                ydl.params["outtmpl"] = literal
            return candidate, lock
        raise EngineError("download_error", "too many files with that name in the folder")

    @staticmethod
    def _download(
        ydl: Any,
        info: dict[str, Any],
        request: presets.DownloadRequest | presets.RowRequest,
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
        claim = YtDlpEngine._claim_name(ydl, info)
        try:
            done = ydl.process_ie_result(info, download=True)
        finally:
            if claim:
                _release(claim[1])
        # The final files only: after merge/convert, never a leftover part or stream.
        existing = [p for p in files if Path(p).is_file() and not _TRANSIENT.search(p)]
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
            # Only the file this run wrote; never re-tag someone's existing mp3.
            mp3s = [
                p
                for p in existing
                if p.lower().endswith(".mp3") and (not claim or Path(p).stem == claim[0])
            ]
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
