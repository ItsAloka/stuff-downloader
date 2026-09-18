"""Playlist listings and the per-item jobs a batch expands into (plan §6.2). No Qt imports.

A listing arrives from a worker process as plain JSON. The worker already sanitizes it, but core
re-validates every field and rebuilds each watch URL from the video id, so a malformed or hostile
entry cannot reach the GUI or a later download job.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from . import presets
from .protocol import JobSpec

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_ENTRIES = 500
MAX_TEXT = 300


@dataclass(frozen=True)
class PlaylistEntry:
    video_id: str
    url: str
    index: int
    title: str
    uploader: str = ""
    duration: float | None = None
    unavailable: str = ""

    @property
    def selectable(self) -> bool:
        return not self.unavailable


@dataclass(frozen=True)
class Listing:
    playlist_id: str
    title: str
    entries: tuple[PlaylistEntry, ...]
    uploader: str = ""
    truncated: bool = False

    @property
    def selectable(self) -> tuple[PlaylistEntry, ...]:
        return tuple(e for e in self.entries if e.selectable)


def _text(value: Any, fallback: str = "") -> str:
    return value[:MAX_TEXT] if isinstance(value, str) and value else fallback


def parse_entry(raw: Any, index: int) -> PlaylistEntry | None:
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("id")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return None
    duration = raw.get("duration")
    if not isinstance(duration, int | float) or isinstance(duration, bool) or duration < 0:
        duration = None
    return PlaylistEntry(
        video_id=video_id,
        # Rebuilt here too: whatever "url" the payload carried is ignored on purpose.
        url=f"https://www.youtube.com/watch?v={video_id}",
        index=index,
        title=_text(raw.get("title"), "Untitled"),
        uploader=_text(raw.get("uploader")),
        duration=float(duration) if duration is not None else None,
        unavailable=_text(raw.get("unavailable")),
    )


def parse_listing(data: Any) -> Listing:
    """Turn a worker ``mode=playlist`` result into a Listing, dropping unusable rows."""
    if not isinstance(data, dict):
        return Listing("", "Playlist", ())
    entries: list[PlaylistEntry] = []
    for raw in (data.get("entries") or [])[:MAX_ENTRIES]:
        entry = parse_entry(raw, len(entries) + 1)
        if entry is not None:
            entries.append(entry)
    return Listing(
        playlist_id=_text(data.get("playlist_id")),
        title=_text(data.get("title"), "Playlist"),
        entries=tuple(entries),
        uploader=_text(data.get("uploader")),
        truncated=bool(data.get("truncated")),
    )


def batch_specs(
    listing: Listing,
    entries: list[PlaylistEntry],
    output_dir: str,
    preset_id: str,
    archive: bool = True,
    crop_cover: bool = True,
) -> list[JobSpec]:
    """One JobSpec per selected entry. Each is an ordinary single-video job."""
    count = len(listing.entries)
    specs = []
    for entry in entries:
        options = presets.download_options(
            preset_id,
            crop_cover=crop_cover,
            playlist_index=entry.index,
            playlist_title=listing.title,
            playlist_count=count,
            archive=archive,
        )
        specs.append(
            JobSpec(
                job_id=uuid.uuid4().hex,
                engine="ytdlp",
                url=entry.url,
                output_dir=output_dir,
                options=options,
            )
        )
    return specs
