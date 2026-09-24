"""Download presets (plan §5.4) as data. No Qt imports, no engine imports.

Core only chooses a preset id and a few typed knobs. The worker owns the mapping to engine
options and re-validates everything it receives, so no user text becomes an engine option,
postprocessor argument or executable path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HEIGHTS = (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144)


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: str  # "video" | "audio" | "thumbnail" | "file" | "gallery"
    max_height: int | None = None  # a cap the resolution picker cannot exceed
    description: str = ""

    @property
    def picks_resolution(self) -> bool:
        return self.kind == "video"


PRESETS: tuple[Preset, ...] = (
    # "Best" is bounded by the compatibility toggle, and saying otherwise was a false promise:
    # verified against a real 4K video, compatible=True picks 1080p avc1 where 2160p AV1 exists,
    # because YouTube publishes no avc1 above 1080p. Turning compatibility off lifts that ceiling
    # at the cost of a file some players and TVs will not open. See tests/network/.
    Preset(
        "video_best",
        "Video — Best",
        "video",
        None,
        "Highest quality that still plays everywhere; turn off compatibility for 4K",
    ),
    Preset("video_1080", "Video — up to 1080p", "video", 1080, "Good quality, reasonable size"),
    Preset("video_720", "Video — up to 720p (small)", "video", 720, "Smaller files"),
    Preset(
        "mp3_music",
        "MP3 — Music (square cover + tags)",
        "audio",
        description="MP3 (VBR ~V0, best transcode) with cover art and tags",
    ),
    Preset("audio_original", "Audio — Original (no re-encode)", "audio", None, "Opus or M4A"),
    Preset("thumbnail", "Thumbnail only", "thumbnail", None, "The cover image as JPG"),
)

# The direct HTTP engine saves the file as the site serves it, so it has exactly one preset. It
# is kept out of PRESETS, which are the yt-dlp choices a video page offers.
FILE_PRESET = Preset(
    "original_file",
    "Original file (as served)",
    "file",
    None,
    "The file exactly as the site sends it",
)

# gallery-dl saves each selected item as the site's original file; it too has one preset.
GALLERY_PRESET = Preset(
    "gallery_original",
    "Original images and videos",
    "gallery",
    None,
    "Each selected item at the site's original quality",
)

PRESETS_BY_ID = {p.id: p for p in (*PRESETS, FILE_PRESET, GALLERY_PRESET)}
DEFAULT_PRESET_ID = "video_1080"


def get(preset_id: str) -> Preset:
    try:
        return PRESETS_BY_ID[preset_id]
    except KeyError:
        raise ValueError(f"unknown preset {preset_id!r}") from None


def effective_height(preset: Preset, height: int | None) -> int | None:
    """The chosen height, capped by the preset. ``None`` means Auto/Best."""
    if not preset.picks_resolution:
        return None
    if height is not None and height not in HEIGHTS:
        raise ValueError(f"unsupported height {height!r}")
    if preset.max_height is None:
        return height
    return preset.max_height if height is None else min(height, preset.max_height)


MAX_PLAYLIST_INDEX = 5000


def download_options(
    preset_id: str,
    height: int | None = None,
    compatible: bool = True,
    crop_cover: bool = True,
    playlist_index: int | None = None,
    playlist_title: str | None = None,
    playlist_count: int | None = None,
    archive: bool = False,
    output_name: str | None = None,
) -> dict[str, Any]:
    """The job ``options`` for a download. Mirrors the worker's validation."""
    preset = get(preset_id)
    options: dict[str, Any] = {
        "mode": "download",
        "preset": preset.id,
        "height": effective_height(preset, height),
        "compatible": bool(compatible),
        "crop_cover": bool(crop_cover),
    }
    if archive:
        options["archive"] = True
    if output_name and output_name.strip():
        options["output_name"] = output_name.strip()
    if playlist_index is not None:
        if not 1 <= playlist_index <= MAX_PLAYLIST_INDEX:
            raise ValueError(f"playlist index out of range: {playlist_index}")
        options["playlist_index"] = int(playlist_index)
        if playlist_title:
            options["playlist_title"] = str(playlist_title)[:300]
        if playlist_count is not None:
            options["playlist_count"] = max(int(playlist_count), int(playlist_index))
    return options


IMAGE_FORMATS = ("original", "jpg", "png", "webp")
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"})


def _image_format(value: str | None) -> str:
    fmt = value or "original"
    if fmt not in IMAGE_FORMATS:
        raise ValueError(f"unknown image format: {fmt!r}")
    return fmt


def file_download_options(
    output_name: str | None = None, image_format: str | None = None
) -> dict[str, Any]:
    """The job ``options`` for the direct HTTP engine. Mirrors its validation exactly."""
    options: dict[str, Any] = {"mode": "download", "preset": FILE_PRESET.id}
    if output_name and output_name.strip():
        options["output_name"] = output_name.strip()
    fmt = _image_format(image_format)
    if fmt != "original":  # the worker converts images only; a video ignores it
        options["image_format"] = fmt
    return options


MAX_GALLERY_ITEMS = 500


def gallery_download_options(
    items: list[int], archive: bool = False, image_format: str | None = None
) -> dict[str, Any]:
    """The job ``options`` for gallery-dl: exactly the chosen 1-based item positions."""
    clean = sorted({int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)})
    if not clean or clean[0] < 1 or clean[-1] > MAX_GALLERY_ITEMS:
        raise ValueError("choose between 1 and 500 gallery items")
    options: dict[str, Any] = {"mode": "download", "preset": GALLERY_PRESET.id, "items": clean}
    if archive:
        options["archive"] = True
    fmt = _image_format(image_format)
    if fmt != "original":
        options["image_format"] = fmt
    return options


def analyze_options() -> dict[str, Any]:
    return {"mode": "analyze"}


def playlist_options() -> dict[str, Any]:
    return {"mode": "playlist"}


# ── Result-card rows (plan §5.4, §8 R2) ─────────────────────────────────────────────────────
# One row's download is {tab, row_id, container, edited_title}. Row ids are the worker's own
# (v:1080:mp4, a:mp3:320, i:1280x720, v:orig…); this mirrors the worker's validation.
# A direct video also offers audio rows (extracted) and ``i:frame``, a still from the video.
ROW_TABS = ("video", "audio", "image")
VIDEO_CONTAINERS = ("mp4", "mkv", "webm", "mov", "avi")
REENCODE_CONTAINERS = frozenset({"mov", "avi"})
IMAGE_SAVE_FORMATS = ("original", "jpg", "png", "webp")
DEFAULT_CONTAINER = "mp4"
MAX_EDITED_TITLE = 300
_ROW_ID = {
    "video": re.compile(r"v:(orig|[1-9][0-9]{0,3}:(mp4|webm))"),
    "audio": re.compile(r"a:(orig|mp3:(320|256|192|128|64)|m4a|opus|flac|wav)"),
    "image": re.compile(r"i:(orig|best|frame|[1-9][0-9]{0,4}x[1-9][0-9]{0,4})"),
}
_AUDIO_NAMES = {"m4a": "M4A", "opus": "Opus", "flac": "FLAC", "wav": "WAV", "orig": "Original"}


# "Download selected as" for a playlist (plan §8 R5): the §5.4 rows every YouTube entry has.
# (label, tab, row_id, container). A height is the most it may be; lower is taken when absent.
BATCH_CHOICES: tuple[tuple[str, str, str, str | None], ...] = (
    ("MP3 320 kbps", "audio", "a:mp3:320", None),
    ("MP3 256 kbps", "audio", "a:mp3:256", None),
    ("MP3 192 kbps", "audio", "a:mp3:192", None),
    ("MP3 128 kbps", "audio", "a:mp3:128", None),
    ("M4A (AAC)", "audio", "a:m4a", None),
    ("Opus", "audio", "a:opus", None),
    ("FLAC", "audio", "a:flac", None),
    ("WAV", "audio", "a:wav", None),
    ("MP4 video, up to 2160p", "video", "v:2160:mp4", "mp4"),
    ("MP4 video, up to 1080p", "video", "v:1080:mp4", "mp4"),
    ("MP4 video, up to 720p", "video", "v:720:mp4", "mp4"),
    ("MP4 video, up to 480p", "video", "v:480:mp4", "mp4"),
)
DEFAULT_MUSIC_BATCH = "a:mp3:320"
DEFAULT_VIDEO_BATCH = "v:1080:mp4"


def batch_choice(row_id: str) -> tuple[str, str, str, str | None]:
    for choice in BATCH_CHOICES:
        if choice[2] == row_id:
            return choice
    raise ValueError(f"unknown batch format {row_id!r}")


def row_download_options(
    tab: str,
    row_id: str,
    container: str | None = None,
    edited_title: str | None = None,
    *,
    playlist_index: int | None = None,
    playlist_title: str | None = None,
    playlist_count: int | None = None,
    album_order: bool = False,
    archive: bool = False,
) -> dict[str, Any]:
    """The job ``options`` for one Result-card row. Mirrors the worker's validation.

    A playlist batch adds the entry's place in the list: its folder comes from the title, and
    only an album (``album_order``) may turn the place into a track number (plan §5.7).
    """
    if tab not in ROW_TABS:
        raise ValueError(f"unknown tab {tab!r}")
    if not isinstance(row_id, str) or not _ROW_ID[tab].fullmatch(row_id):
        raise ValueError(f"unknown row {row_id!r}")
    options: dict[str, Any] = {"mode": "download", "tab": tab, "row_id": row_id}
    if tab == "video":
        container = container or DEFAULT_CONTAINER
        if container not in VIDEO_CONTAINERS:
            raise ValueError(f"unknown container {container!r}")
        options["container"] = container
    elif tab == "image":
        container = container or "original"
        if container not in IMAGE_SAVE_FORMATS:
            raise ValueError(f"unknown image format {container!r}")
        options["container"] = container
    title = (edited_title or "").strip()
    if title:
        options["edited_title"] = title[:MAX_EDITED_TITLE]
    if archive:
        options["archive"] = True
    if playlist_index is not None:
        if not 1 <= playlist_index <= MAX_PLAYLIST_INDEX:
            raise ValueError(f"playlist index out of range: {playlist_index}")
        options["playlist_index"] = int(playlist_index)
        if playlist_title:
            options["playlist_title"] = str(playlist_title)[:300]
        if playlist_count is not None:
            options["playlist_count"] = max(int(playlist_count), int(playlist_index))
        if album_order:
            options["album_order"] = True
    elif album_order:
        raise ValueError("album order needs a playlist index")
    return options


def is_row_options(options: dict[str, Any]) -> bool:
    return isinstance(options.get("row_id"), str)


def row_kind(options: dict[str, Any]) -> str:
    """What a row download saves: "video", "audio" or "thumbnail" (an image row)."""
    tab = options.get("tab")
    return "thumbnail" if tab == "image" else tab if tab in ("video", "audio") else "file"


def row_label(options: dict[str, Any]) -> str:
    """A short description of a row download for queue cards and History."""
    tab, row_id = options.get("tab"), str(options.get("row_id") or "")
    container = str(options.get("container") or "")
    parts = row_id.split(":")
    if tab == "video":
        quality = "Original file" if parts[1:2] == ["orig"] else f"{parts[1]}p"
        return f"Video · {quality} · {container.upper()}"
    if tab == "audio":
        if parts[1:2] == ["mp3"]:
            return f"MP3 · {parts[2]} kbps"
        return f"Audio · {_AUDIO_NAMES.get(parts[1] if len(parts) > 1 else '', 'Audio')}"
    if tab == "image":
        size = parts[1] if len(parts) > 1 and "x" in parts[1] else ""
        fmt = "Original" if container in ("", "original") else container.upper()
        frame = "Frame" if parts[1:2] == ["frame"] else ""
        return "  ".join(p for p in (f"Image · {fmt}", frame, size) if p)
    return "Download"
