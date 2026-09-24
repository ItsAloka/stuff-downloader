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

PRESET_IDS = frozenset(
    {"video_best", "video_1080", "video_720", "mp3_music", "audio_original", "thumbnail"}
)
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
    # The list is an album, so playlist_index is the album position and may be TRCK (§5.7).
    album_order: bool = False

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


def _playlist_fields(options: dict[str, Any]) -> tuple[int | None, str | None, int | None, bool]:
    """``playlist_index``, ``playlist_title``, ``playlist_count`` and ``album_order``, checked."""
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
    album_order = options.get("album_order", False)
    if not isinstance(album_order, bool):
        raise EngineError("bad_options", "'album_order' must be true or false")
    if album_order and playlist_index is None:
        raise EngineError("bad_options", "'album_order' needs 'playlist_index'")
    return playlist_index, playlist_title, playlist_count, album_order


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
        "album_order",
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
    playlist_index, playlist_title, playlist_count, album_order = _playlist_fields(options)
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
        album_order=album_order,
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


def job_home(request: DownloadRequest | RowRequest, output_dir: str) -> str:
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
                "format": "bestaudio/best",
                "outtmpl": music_outtmpl(request),
                "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
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


# ── Result-card rows (plan §5.4, §8 R2) ─────────────────────────────────────────────────────
# The Result card sends {tab, row_id, container, edited_title} for one row. Row ids are ours
# (v:1080:mp4, a:mp3:320, i:1280x720); nothing the site named reaches yt-dlp. They are checked
# here and turned into fixed selectors and postprocessors, like the presets above.
ROW_TABS = ("video", "audio", "image")
ROW_PLAYLIST_KEYS = ("playlist_index", "playlist_title", "playlist_count", "album_order", "archive")
VIDEO_CONTAINERS = ("mp4", "mkv", "webm", "mov", "avi")
REENCODE_CONTAINERS = frozenset({"mov", "avi"})
IMAGE_FORMATS = ("original", "jpg", "png", "webp")
MP3_BITRATES = (320, 256, 192, 128, 64)
AUDIO_ROW_IDS = (*(f"a:mp3:{b}" for b in MP3_BITRATES), "a:m4a", "a:opus", "a:flac", "a:wav")
MAX_ROW_HEIGHT = 8640
MAX_EDITED_TITLE = 300
_VIDEO_ROW = re.compile(r"v:([1-9][0-9]{0,3}):(mp4|webm)")
_IMAGE_ROW = re.compile(r"i:(orig|best|([1-9][0-9]{0,4})x([1-9][0-9]{0,4}))")
# The direct-file engine's single row per tab: the file as the site serves it.
ORIGINAL_ROW_IDS = {"video": "v:orig", "audio": "a:orig", "image": "i:orig"}
# A direct video's other rows (plan §5.3): its audio, extracted, and one still frame.
FRAME_ROW_ID = "i:frame"
DIRECT_FILE_ROW_IDS = {
    "video": frozenset({"v:orig"}),
    "audio": frozenset({"a:orig", *AUDIO_ROW_IDS}),
    "image": frozenset({"i:orig", FRAME_ROW_ID}),
}

VIDEO_ROW_OUTTMPL = "%(title).150B.%(ext)s"
# An AAC source is copied into M4A; anything else is encoded at this quality.
M4A_TRANSCODE_KBPS = "256"


@dataclass(frozen=True)
class RowRequest:
    tab: str
    row_id: str
    container: str | None
    edited_title: str | None
    height: int | None = None
    source_ext: str | None = None  # a video row's source stream container
    image_size: tuple[int, int] | None = None
    # What the rest of the engine keys on: the legacy preset this row belongs to.
    preset: str = ""
    archive: bool = False
    playlist_index: int | None = None
    playlist_title: str | None = None
    playlist_count: int | None = None
    album_order: bool = False

    @property
    def in_playlist(self) -> bool:
        return self.playlist_index is not None

    @property
    def audio_codec(self) -> str | None:
        """mp3, m4a, opus, flac or wav for an audio row; None otherwise."""
        return self.row_id.split(":")[1] if self.tab == "audio" else None

    @property
    def mp3_bitrate(self) -> int | None:
        parts = self.row_id.split(":")
        return int(parts[2]) if self.tab == "audio" and parts[1] == "mp3" else None


def is_row_request(options: dict[str, Any]) -> bool:
    return "row_id" in options


def parse_row_request(options: dict[str, Any], *, original_only: bool = False) -> RowRequest:
    """One Result-card row request, validated. ``original_only`` is the direct-file engine:
    the file itself (``v:orig``, ``a:orig``, ``i:orig``), or from a video its audio as an
    audio row, or ``i:frame``."""
    allowed = {"mode", "tab", "row_id", "container", "edited_title"}
    if not original_only:
        # A playlist batch sends the same row ids with the list's place and folder (R5).
        allowed |= set(ROW_PLAYLIST_KEYS)
    unknown = set(options) - allowed
    if unknown:
        raise EngineError("bad_options", f"unknown options: {sorted(unknown)}")
    tab, row_id = options.get("tab"), options.get("row_id")
    if tab not in ROW_TABS:
        raise EngineError("bad_options", f"unknown tab {tab!r}")
    if not isinstance(row_id, str) or not row_id.startswith(tab[0] + ":"):
        raise EngineError("bad_options", f"row {row_id!r} is not on the {tab} tab")
    container = options.get("container")
    edited = options.get("edited_title")
    if edited is not None and (not isinstance(edited, str) or len(edited) > MAX_EDITED_TITLE):
        raise EngineError("bad_options", "'edited_title' must be a short string")
    fields: dict[str, Any] = {}
    if original_only:
        if row_id not in DIRECT_FILE_ROW_IDS[tab]:
            raise EngineError("bad_options", f"unknown row {row_id!r}")
    elif tab == "video":
        match = _VIDEO_ROW.fullmatch(row_id)
        if not match or int(match.group(1)) > MAX_ROW_HEIGHT:
            raise EngineError("bad_options", f"unknown row {row_id!r}")
        fields = {"height": int(match.group(1)), "source_ext": match.group(2)}
    elif tab == "audio":
        if row_id not in AUDIO_ROW_IDS:
            raise EngineError("bad_options", f"unknown row {row_id!r}")
    else:
        match = _IMAGE_ROW.fullmatch(row_id)
        if not match:
            raise EngineError("bad_options", f"unknown row {row_id!r}")
        if match.group(2):
            fields["image_size"] = (int(match.group(2)), int(match.group(3)))
    if tab == "video":
        if container not in VIDEO_CONTAINERS:
            raise EngineError("bad_options", "'container' must be mp4, mkv, webm, mov or avi")
    elif tab == "image":
        if container not in IMAGE_FORMATS:
            raise EngineError("bad_options", "'container' must be original, jpg, png or webp")
    elif container is not None:
        raise EngineError("bad_options", "an audio row takes no container")
    if not isinstance(options.get("archive", False), bool):
        raise EngineError("bad_options", "'archive' must be true or false")
    playlist_index, playlist_title, playlist_count, album_order = _playlist_fields(options)
    preset = {"video": "video_best", "image": "thumbnail"}.get(tab) or (
        "mp3_music" if row_id.startswith("a:mp3:") else "audio_original"
    )
    return RowRequest(
        tab=tab,
        row_id=row_id,
        container=container,
        edited_title=safe_output_name(edited),
        preset=preset,
        archive=options.get("archive", False),
        playlist_index=playlist_index,
        playlist_title=playlist_title,
        playlist_count=playlist_count,
        album_order=album_order,
        **fields,
    )


def row_video_format(height: int, source_ext: str, container: str) -> str:
    """The row's height in its source container first, then that height in anything, then
    the best below it. A WebM file prefers WebM audio so it stays a remux. An MP4 row asks for
    H.264 first: that is the stream the catalog shows for the height when one exists, and
    yt-dlp's own "best" MP4 would otherwise be AV1."""
    h = f"[height={height}]"
    audio = "ba[ext=webm]" if container == "webm" else "ba[ext=m4a]"
    avc = [f"bv*{h}[ext=mp4][vcodec^=avc1]+{audio}"] if source_ext == "mp4" else []
    return "/".join(
        [
            *avc,
            f"bv*{h}[ext={source_ext}]+{audio}",
            f"bv*{h}[ext={source_ext}]+ba",
            f"bv*{h}+ba",
            f"b{h}",
            f"bv*[height<={height}]+ba",
            f"b[height<={height}]",
            "bv*+ba",
            "b",
        ]
    )


def needs_reencode(container: str, source_ext: str | None) -> bool:
    """True when saving as ``container`` re-encodes the video (plan §5.4)."""
    if container in REENCODE_CONTAINERS:
        return True
    return container == "webm" and source_ext not in (None, "webm")


def build_row_opts(request: RowRequest, output_dir: str) -> dict[str, Any]:
    """yt-dlp options for one Result-card row (plan §8 R2). Built from fixed values only.

    A playlist row lands in the list's subfolder, like a preset batch job."""
    home = job_home(request, output_dir)
    opts: dict[str, Any] = {
        "noplaylist": True,
        "windowsfilenames": True,
        "paths": {"home": home},
    }
    if request.archive:
        opts["download_archive"] = str(Path(home) / ARCHIVE_FILENAME)
    named = literal_outtmpl(request.edited_title) if request.edited_title else None
    if request.tab == "video":
        container = request.container or "mp4"
        if container in ("mp4", "mkv"):
            merge = container
            # A single progressive file is never merged; the remuxer moves it into place.
            postprocessor = {"key": "FFmpegVideoRemuxer", "preferedformat": container}
        else:
            # WebM merges only VP9/AV1 with Opus/Vorbis; anything else lands in MKV and the
            # converter re-encodes it, as do MOV and AVI (the row says "re-encodes, slower").
            merge = "webm/mkv" if container == "webm" else "mkv"
            postprocessor = {"key": "FFmpegVideoConvertor", "preferedformat": container}
        opts.update(
            {
                "format": row_video_format(
                    request.height or 1080, request.source_ext or "mp4", container
                ),
                "merge_output_format": merge,
                "outtmpl": named or VIDEO_ROW_OUTTMPL,
                "postprocessors": [postprocessor],
            }
        )
        return opts
    if request.tab == "audio":
        codec = request.audio_codec or "mp3"
        extract: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if codec == "mp3":
            fmt = "bestaudio/best"
            extract["preferredquality"] = str(request.mp3_bitrate or 320)
        elif codec == "m4a":
            fmt = "bestaudio[acodec^=mp4a]/bestaudio/best"
            extract["preferredquality"] = M4A_TRANSCODE_KBPS
        elif codec == "opus":
            fmt = "bestaudio[acodec=opus]/bestaudio/best"
        else:  # flac, wav: lossless containers of the best source
            fmt = "bestaudio/best"
        # Tags always; a cover for every format that can hold one (WAV cannot, reliably).
        embeds_cover = codec != "wav"
        postprocessors: list[dict[str, Any]] = [
            extract,
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        if embeds_cover:
            postprocessors = [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                *postprocessors,
                {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            ]
            # Native square covers are unchanged by the crop; anything else is centre-cropped.
            opts["postprocessor_args"] = {"thumbnailsconvertor+ffmpeg_o": list(SQUARE_CROP_ARGS)}
        opts.update(
            {
                "format": fmt,
                "outtmpl": named or MUSIC_OUTTMPL,
                "writethumbnail": embeds_cover,
                "postprocessors": postprocessors,
            }
        )
        return opts
    # image: the chosen thumbnail, saved as it is or converted
    fmt = request.container or "original"
    postprocessors = []
    if fmt != "original":
        postprocessors.append(
            {"key": "FFmpegThumbnailsConvertor", "format": fmt, "when": "before_dl"}
        )
    opts.update(
        {
            "skip_download": True,
            "writethumbnail": True,
            "outtmpl": named or VIDEO_ROW_OUTTMPL,
            "postprocessors": postprocessors,
        }
    )
    return opts
