"""Preset → yt-dlp options (plan §5.4/§5.5). Stdlib only; does not import yt-dlp.

The job spec only names a preset and a few typed knobs. Everything is validated here and the
engine options are built from fixed values, so no job field can reach a postprocessor
argument, an output template or an executable path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engines.base import EngineError
from .names import safe_output_name

PRESET_IDS = frozenset({
    "video_best", "video_1080", "video_720", "mp3_music", "audio_original",
    "audio_m4a", "audio_flac", "audio_wav", "thumbnail",
})
PRESET_MAX_HEIGHT = {"video_1080": 1080, "video_720": 720}
VIDEO_PRESETS = frozenset({"video_best", "video_1080", "video_720"})
HEIGHTS = frozenset({4320, 2160, 1440, 1080, 720, 480, 360, 240, 144})

# Square centre-crop for cover art: the guide's ThumbnailsConvertor ffmpeg arguments.
SQUARE_CROP_ARGS = [
    "-qmin",
    "1",
    "-q:v",
    "1",
    "-vf",
    "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
]

VIDEO_OUTTMPL = "%(title).150B [%(id)s].%(ext)s"
MUSIC_OUTTMPL = "%(artist,uploader)s - %(track,title).150B.%(ext)s"
THUMB_OUTTMPL = "%(title).150B [%(id)s].%(ext)s"

MAX_PLAYLIST_INDEX = 5000
MAX_PLAYLIST_TITLE = 300
ARCHIVE_FILENAME = ".stuff-downloader-archive.txt"

# A playlist title comes from the site and is attacker-influenced, so it never reaches a path
# unsanitized: path separators, drive-letter colons, wildcards, quotes and every control
# character are replaced, and Windows device names are refused outright.
_UNSAFE_NAME_CHARS = re.compile(
    "[" + "".join(chr(c) for c in range(32)) + re.escape('<>:"/|?*' + chr(92)) + "]"
)
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
FALLBACK_FOLDER_NAME = "Playlist"
MAX_FOLDER_NAME = 60


def safe_folder_name(title: str) -> str:
    """One path segment that is safe on Windows, derived from an untrusted playlist title."""
    name = _UNSAFE_NAME_CHARS.sub("_", title or "")
    name = re.sub(r"\s+", " ", name)[:MAX_FOLDER_NAME].strip().strip(".").strip()
    stem = name.split(".")[0].upper()
    if not name or name.upper() in _WINDOWS_RESERVED or stem in _WINDOWS_RESERVED:
        return FALLBACK_FOLDER_NAME
    return name


@dataclass(frozen=True)
class DownloadRequest:
    preset: str
    height: int | None
    compatible: bool
    crop_cover: bool
    playlist_index: int | None = None
    playlist_title: str | None = None
    playlist_count: int | None = None
    archive: bool = False
    output_name: str | None = None

    @property
    def in_playlist(self) -> bool:
        return self.playlist_index is not None


def _optional_index(options: dict[str, Any], key: str) -> int | None:
    value = options.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError("bad_options", f"{key!r} must be a whole number")
    if not 1 <= value <= MAX_PLAYLIST_INDEX:
        raise EngineError("bad_options", f"{key!r} out of range: {value}")
    return value


def parse_request(options: dict[str, Any]) -> DownloadRequest:
    allowed = {
        "mode",
        "preset",
        "height",
        "compatible",
        "crop_cover",
        "playlist_index",
        "playlist_title",
        "playlist_count",
        "archive",
        "output_name",
    }
    unknown = set(options) - allowed
    if unknown:
        raise EngineError("bad_options", f"unknown options: {sorted(unknown)}")
    preset = options.get("preset")
    if preset not in PRESET_IDS:
        raise EngineError("bad_options", f"unknown preset {preset!r}")
    height = options.get("height")
    if height is not None and (
        isinstance(height, bool) or not isinstance(height, int) or height not in HEIGHTS
    ):
        raise EngineError("bad_options", f"unsupported height {height!r}")
    for key in ("compatible", "crop_cover"):
        if not isinstance(options.get(key, True), bool):
            raise EngineError("bad_options", f"{key!r} must be true or false")
    if not isinstance(options.get("archive", False), bool):
        raise EngineError("bad_options", "'archive' must be true or false")
    output_name = options.get("output_name")
    if output_name is not None and not isinstance(output_name, str):
        raise EngineError("bad_options", "'output_name' must be a string")
    playlist_index = _optional_index(options, "playlist_index")
    playlist_count = _optional_index(options, "playlist_count")
    playlist_title = options.get("playlist_title")
    if playlist_title is not None:
        if not isinstance(playlist_title, str):
            raise EngineError("bad_options", "'playlist_title' must be a string")
        if len(playlist_title) > MAX_PLAYLIST_TITLE:
            raise EngineError("bad_options", "'playlist_title' is too long")
    if playlist_title is not None and playlist_index is None:
        raise EngineError("bad_options", "'playlist_title' needs 'playlist_index'")
    if preset not in VIDEO_PRESETS:
        height = None
    cap = PRESET_MAX_HEIGHT.get(preset)
    if cap is not None:
        height = cap if height is None else min(height, cap)
    return DownloadRequest(
        preset=preset,
        height=height,
        compatible=options.get("compatible", True),
        crop_cover=options.get("crop_cover", True),
        playlist_index=playlist_index,
        playlist_title=playlist_title,
        playlist_count=playlist_count,
        archive=options.get("archive", False),
        output_name=safe_output_name(output_name),
    )


def video_format(height: int | None, compatible: bool) -> str:
    """Preferred selectors first, then best within the height, then anything as a last resort."""
    h = f"[height<={height}]" if height else ""
    choices = []
    if compatible:
        choices += [f"bv*{h}[vcodec^=avc1]+ba[acodec^=mp4a]", f"b{h}[ext=mp4]"]
    choices += [f"bv*{h}+ba", f"b{h}"]
    if height:
        choices += ["bv*+ba", "b"]
    return "/".join(choices)


def track_prefix(index: int, count: int | None) -> str:
    """"007 - " for track 7 of a 500-track playlist. Digits only, so it is template-safe."""
    width = max(2, len(str(count or index)))
    return f"{index:0{width}d} - "


def job_home(request: DownloadRequest, output_dir: str) -> str:
    """Where this job's files land: a sanitized playlist subfolder, or the download folder."""
    if request.in_playlist and request.playlist_title:
        return str(Path(output_dir) / safe_folder_name(request.playlist_title))
    return output_dir


def literal_outtmpl(stem: str) -> str:
    """A template that names the file ``stem`` exactly: ``%`` is escaped, so a chosen name
    like ``%(uploader)s`` stays text and is never expanded by yt-dlp."""
    return stem.replace("%", "%%") + ".%(ext)s"


def music_outtmpl(request: DownloadRequest) -> str:
    """Audio filename; playlist ordering belongs in metadata, not the visible name."""
    return literal_outtmpl(request.output_name) if request.output_name else MUSIC_OUTTMPL


def build_ydl_opts(request: DownloadRequest, output_dir: str) -> dict[str, Any]:
    home = job_home(request, output_dir)
    opts: dict[str, Any] = {
        "noplaylist": True,
        "windowsfilenames": True,
        "paths": {"home": home},
    }
    if request.archive:
        # Derived here from the trusted output dir; the job spec never carries a path.
        opts["download_archive"] = str(Path(home) / ARCHIVE_FILENAME)
    if request.preset in VIDEO_PRESETS:
        opts.update(
            {
                "format": video_format(request.height, request.compatible),
                "merge_output_format": "mp4" if request.compatible else "mkv",
                "outtmpl": (
                    literal_outtmpl(request.output_name) if request.output_name else VIDEO_OUTTMPL
                ),
            }
        )
    elif request.preset == "mp3_music":
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": music_outtmpl(request),
                "writethumbnail": True,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
                    {"key": "FFmpegMetadata", "add_metadata": True},
                    {"key": "EmbedThumbnail", "already_have_thumbnail": False},
                ],
            }
        )
    elif request.preset == "audio_original":
        opts.update(
            {
                "format": "bestaudio[acodec=opus]/bestaudio/best",
                "outtmpl": music_outtmpl(request),
            }
        )
    elif request.preset in ("audio_m4a", "audio_flac", "audio_wav"):
        codec = {"audio_m4a": "m4a", "audio_flac": "flac", "audio_wav": "wav"}[
            request.preset
        ]
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": music_outtmpl(request),
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": codec}],
            }
        )
    else:  # thumbnail
        opts.update(
            {
                "skip_download": True,
                "writethumbnail": True,
                "outtmpl": THUMB_OUTTMPL,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
                ],
            }
        )
    if request.crop_cover and request.preset in ("mp3_music", "thumbnail"):
        opts["postprocessor_args"] = {"thumbnailsconvertor+ffmpeg_o": list(SQUARE_CROP_ARGS)}
    return opts
