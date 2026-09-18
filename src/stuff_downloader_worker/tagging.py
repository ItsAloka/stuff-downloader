"""MP3 tag verification and repair with mutagen (plan §5.4). No Qt; mutagen imported lazily."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF header, or None if it is not a readable JPEG."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


def verify_mp3(
    path: str | Path,
    info: dict[str, Any],
    album: str | None = None,
    track_number: int | None = None,
    track_total: int | None = None,
) -> dict[str, Any]:
    """Fill missing title/artist/album from ``info`` and report what the file carries.

    ``album`` and ``track_number`` come from a playlist batch and are written even when the
    file already carries them, because the playlist is the more specific answer.
    """
    try:
        from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK, ID3NoHeaderError
    except ImportError:
        return {"checked": False, "reason": "mutagen not installed"}

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    wanted = {
        "TIT2": (TIT2, info.get("track") or info.get("title")),
        "TPE1": (TPE1, info.get("artist") or info.get("uploader")),
        "TALB": (TALB, info.get("album")),
    }
    changed = False
    for frame_id, (frame_cls, value) in wanted.items():
        if not tags.getall(frame_id) and isinstance(value, str) and value:
            tags.add(frame_cls(encoding=3, text=value))
            changed = True
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=album)])
        changed = True
    if track_number:
        text = f"{track_number}/{track_total}" if track_total else str(track_number)
        tags.setall("TRCK", [TRCK(encoding=3, text=text)])
        changed = True
    if changed:
        tags.save(str(path))

    covers = tags.getall("APIC")
    size = jpeg_size(covers[0].data) if covers else None
    return {
        "checked": True,
        "title": str(tags["TIT2"]) if tags.getall("TIT2") else None,
        "artist": str(tags["TPE1"]) if tags.getall("TPE1") else None,
        "album": str(tags["TALB"]) if tags.getall("TALB") else None,
        "track": str(tags["TRCK"]) if tags.getall("TRCK") else None,
        "cover": bool(covers),
        "cover_size": list(size) if size else None,
        "cover_square": (size[0] == size[1]) if size else None,
        "repaired": changed,
    }
