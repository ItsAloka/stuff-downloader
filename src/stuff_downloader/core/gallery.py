"""A gallery analyze result, re-validated for display (plan §M4). No Qt imports, no engines.

The worker already rebuilds every row, but this is the GUI's own boundary: a row is kept only
if its fields are the expected types, and the preview is carried as base64 text the GUI decodes
itself. A gallery never supplies a URL here, and none is accepted.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any

MAX_ITEMS = 500
MAX_TITLE = 300
MAX_PREVIEW_B64 = 2_800_000  # ~2 MB of image, the worker's own cap, as base64
KINDS = ("image", "video", "file")
_EXT = re.compile(r"[a-z0-9]{1,6}")


@dataclass(frozen=True)
class GalleryItem:
    index: int  # 1-based position; what a download names to fetch exactly this item
    kind: str
    ext: str = ""
    width: int | None = None
    height: int | None = None
    preview: bytes = b""

    @property
    def label(self) -> str:
        size = f"{self.width}×{self.height}" if self.width and self.height else ""
        parts = [f"#{self.index}", self.ext.upper() if self.ext else self.kind, size]
        return "  ".join(p for p in parts if p)


@dataclass(frozen=True)
class Gallery:
    title: str
    uploader: str
    site: str
    items: tuple[GalleryItem, ...]
    truncated: bool = False


def _text(value: Any, limit: int = MAX_TITLE) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _dim(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100_000
    return value if ok else None


def _preview(value: Any) -> bytes:
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, str) or len(data) > MAX_PREVIEW_B64:
        return b""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return b""


def parse_item(raw: Any, seen: set[int]) -> GalleryItem | None:
    if not isinstance(raw, dict):
        return None
    index = raw.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= MAX_ITEMS:
        return None
    if index in seen:
        return None
    kind = raw.get("kind") if raw.get("kind") in KINDS else "file"
    ext = raw.get("ext") if isinstance(raw.get("ext"), str) else ""
    seen.add(index)
    return GalleryItem(
        index=index,
        kind=kind,
        ext=ext if _EXT.fullmatch(ext) else "",
        width=_dim(raw.get("width")),
        height=_dim(raw.get("height")),
        preview=_preview(raw.get("thumbnail")),
    )


def parse(data: dict[str, Any]) -> Gallery:
    raw_items = data.get("items")
    seen: set[int] = set()
    items = []
    for raw in raw_items[:MAX_ITEMS] if isinstance(raw_items, list) else []:
        item = parse_item(raw, seen)
        if item is not None:
            items.append(item)
    return Gallery(
        title=_text(data.get("title")) or "Gallery",
        uploader=_text(data.get("uploader"), 100),
        site=_text(data.get("extractor"), 40),
        items=tuple(sorted(items, key=lambda i: i.index)),
        truncated=data.get("truncated") is True,
    )
