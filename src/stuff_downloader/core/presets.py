"""Download presets (plan §5.4) as data. No Qt imports, no engine imports.

Core only chooses a preset id and a few typed knobs. The worker owns the mapping to engine
options and re-validates everything it receives, so no user text becomes an engine option,
postprocessor argument or executable path.
"""

from __future__ import annotations

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
    if playlist_index is not None:
        if not 1 <= playlist_index <= MAX_PLAYLIST_INDEX:
            raise ValueError(f"playlist index out of range: {playlist_index}")
        options["playlist_index"] = int(playlist_index)
        if playlist_title:
            options["playlist_title"] = str(playlist_title)[:300]
        if playlist_count is not None:
            options["playlist_count"] = max(int(playlist_count), int(playlist_index))
    return options


def file_download_options() -> dict[str, Any]:
    """The job ``options`` for the direct HTTP engine. Mirrors its validation exactly."""
    return {"mode": "download", "preset": FILE_PRESET.id}


MAX_GALLERY_ITEMS = 500


def gallery_download_options(items: list[int], archive: bool = False) -> dict[str, Any]:
    """The job ``options`` for gallery-dl: exactly the chosen 1-based item positions."""
    clean = sorted({int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)})
    if not clean or clean[0] < 1 or clean[-1] > MAX_GALLERY_ITEMS:
        raise ValueError("choose between 1 and 500 gallery items")
    options: dict[str, Any] = {"mode": "download", "preset": GALLERY_PRESET.id, "items": clean}
    if archive:
        options["archive"] = True
    return options


def analyze_options() -> dict[str, Any]:
    return {"mode": "analyze"}


def playlist_options() -> dict[str, Any]:
    return {"mode": "playlist"}
