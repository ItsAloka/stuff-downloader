"""Spotify listings, matches and the per-track jobs a batch expands into (plan §6.3). No Qt.

Spotify audio is DRM-protected and never captured. Each track's audio is *matched* from YouTube
Music by the spotDL engine, so a wrong recording can be picked. The flow therefore has three
worker modes, each an ordinary job on the ``spotdl`` engine:

- ``analyze`` on a track/album/playlist link lists the tracks, and does no matching: spotDL's
  matcher takes tens of seconds per track, far too slow to run before the list can be shown.
- ``match`` on one track link returns the YouTube result it would download, with the duration
  difference and spotDL's own confidence score, for the match review table.
- ``download`` on one track link downloads the reviewed match (or searches, if none was given),
  and tags the MP3 with Spotify's metadata and cover.

As with playlists and galleries, core re-validates everything the worker sends and rebuilds every
URL from an id, so a malformed or hostile payload cannot reach the GUI or a later job.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from .protocol import JobSpec
from .router import SPOTIFY_ID, SPOTIFY_KINDS, route, spotify_url

ENGINE = "spotdl"
PRESET_ID = "spotify_mp3"
MAX_TRACKS = 500
MAX_TEXT = 300
MAX_ARTISTS = 20
MAX_DURATION = 24 * 3600  # seconds; anything longer is not a song
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

OVERRIDE_INVALID_REASON = "Paste a YouTube or YouTube Music link to a single song."


def _text(value: Any, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    clean = " ".join(value.split())[:MAX_TEXT]
    return clean or fallback


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0 <= value <= MAX_DURATION:
        return None
    return float(value)


@dataclass(frozen=True)
class SpotifyTrack:
    track_id: str
    index: int  # 1-based position in the listing
    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    explicit: bool = False

    @property
    def url(self) -> str:
        return spotify_url("track", self.track_id)

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


@dataclass(frozen=True)
class SpotifyListing:
    kind: str  # "track" | "album" | "playlist"
    spotify_id: str
    title: str
    tracks: tuple[SpotifyTrack, ...]
    owner: str = ""
    truncated: bool = False
    skipped: int = 0  # rows the worker reported that were not usable (local files, podcasts)


@dataclass(frozen=True)
class Match:
    """The YouTube recording chosen for one Spotify track."""

    track_id: str
    video_id: str
    title: str = ""
    channel: str = ""
    duration: float | None = None
    duration_diff: float | None = None  # match minus Spotify, seconds; None when either is unknown
    confidence: float | None = None  # spotDL's score, 0-100; None when it gave none
    manual: bool = False  # the owner pasted this link

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"


def parse_track(raw: Any, index: int) -> SpotifyTrack | None:
    if not isinstance(raw, dict):
        return None
    track_id = raw.get("id")
    if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
        return None
    artists_raw = raw.get("artists")
    artists: list[str] = []
    if isinstance(artists_raw, list):
        for value in artists_raw[:MAX_ARTISTS]:
            name = _text(value)
            if name:
                artists.append(name)
    return SpotifyTrack(
        track_id=track_id,
        index=index,
        title=_text(raw.get("title"), "Untitled"),
        artists=tuple(artists),
        album=_text(raw.get("album")),
        duration=_seconds(raw.get("duration")),
        explicit=raw.get("explicit") is True,
    )


def parse_listing(data: Any) -> SpotifyListing:
    """Turn a worker ``mode=analyze`` result into a SpotifyListing, dropping unusable rows."""
    if not isinstance(data, dict):
        return SpotifyListing("", "", "Spotify", ())
    kind = data.get("spotify_kind")
    spotify_id = data.get("spotify_id")
    if kind not in SPOTIFY_KINDS:
        kind = ""
    if not isinstance(spotify_id, str) or not SPOTIFY_ID.fullmatch(spotify_id):
        spotify_id = ""
    raw_tracks = data.get("tracks")
    raw_tracks = raw_tracks if isinstance(raw_tracks, list) else []
    tracks: list[SpotifyTrack] = []
    seen: set[str] = set()
    dropped = 0
    for raw in raw_tracks[:MAX_TRACKS]:
        track = parse_track(raw, len(tracks) + 1)
        if track is None or track.track_id in seen:
            dropped += 1
            continue
        seen.add(track.track_id)
        tracks.append(track)
    worker_skipped = data.get("skipped")
    if isinstance(worker_skipped, bool) or not isinstance(worker_skipped, int):
        worker_skipped = 0
    return SpotifyListing(
        kind=kind,
        spotify_id=spotify_id,
        title=_text(data.get("title"), "Spotify"),
        tracks=tuple(tracks),
        owner=_text(data.get("owner")),
        truncated=bool(data.get("truncated")) or len(raw_tracks) > MAX_TRACKS,
        skipped=max(0, min(worker_skipped, 100_000)) + dropped,
    )


def parse_match(data: Any, track: SpotifyTrack) -> Match | None:
    """A worker ``mode=match`` result for ``track``, or None when nothing usable was found."""
    if not isinstance(data, dict) or data.get("track_id") != track.track_id:
        return None
    video_id = data.get("video_id")
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return None
    duration = _seconds(data.get("duration"))
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        confidence = None
    else:
        confidence = round(max(0.0, min(float(confidence), 100.0)), 1)
    return Match(
        track_id=track.track_id,
        video_id=video_id,
        title=_text(data.get("title")),
        channel=_text(data.get("channel")),
        duration=duration,
        # Recomputed here from the two durations core trusts, never taken from the payload.
        duration_diff=duration_diff(duration, track.duration),
        confidence=confidence,
    )


def duration_diff(match: float | None, spotify: float | None) -> float | None:
    if match is None or spotify is None:
        return None
    return round(match - spotify, 1)


def override_match(text: str, track: SpotifyTrack) -> Match:
    """A match the owner pasted. Only a single YouTube/YouTube Music video is accepted.

    Raises ValueError with a message fit for the owner otherwise.
    """
    r = route(text)
    if r.kind != "youtube" or not VIDEO_ID.fullmatch(r.video_id):
        raise ValueError(OVERRIDE_INVALID_REASON)
    return Match(track_id=track.track_id, video_id=r.video_id, manual=True)


# ── job specs ─────────────────────────────────────────────────────────────────────────────
def analyze_options() -> dict[str, Any]:
    return {"mode": "analyze"}


def match_options() -> dict[str, Any]:
    return {"mode": "match"}


def download_options(video_id: str | None = None, archive: bool = True) -> dict[str, Any]:
    """The job ``options`` for one track's download. Mirrors the worker's validation exactly.

    Nothing about the listing travels: unlike a YouTube playlist, every Spotify track carries its
    own album, track number and cover, and those are what the file is tagged with.
    """
    options: dict[str, Any] = {"mode": "download", "preset": PRESET_ID, "archive": bool(archive)}
    if video_id is not None:
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise ValueError(f"invalid video id: {video_id!r}")
        options["video_id"] = video_id
    return options


def match_spec(track: SpotifyTrack) -> JobSpec:
    """A short job asking the worker which YouTube recording it would pick for ``track``."""
    return JobSpec(
        job_id=uuid.uuid4().hex,
        engine=ENGINE,
        url=track.url,
        output_dir=".",
        options=match_options(),
    )


def batch_specs(
    tracks: list[SpotifyTrack],
    matches: dict[str, Match],
    output_dir: str,
    archive: bool = True,
) -> list[JobSpec]:
    """One ordinary download job per selected track, carrying its reviewed match if any.

    A track with no reviewed match is still queued: the worker then searches for it itself.
    """
    specs = []
    for track in tracks:
        match = matches.get(track.track_id)
        options = download_options(
            video_id=match.video_id if match is not None else None, archive=archive
        )
        specs.append(
            JobSpec(
                job_id=uuid.uuid4().hex,
                engine=ENGINE,
                url=track.url,
                output_dir=output_dir,
                options=options,
            )
        )
    return specs
