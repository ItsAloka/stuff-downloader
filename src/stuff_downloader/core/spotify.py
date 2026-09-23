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
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

from .protocol import JobSpec
from .router import SPOTIFY_ID, SPOTIFY_KINDS, route, spotify_url

ENGINE = "spotdl"
PRESET_ID = "spotify_mp3"
MAX_TRACKS = 500
MAX_TEXT = 300
MAX_ARTISTS = 20
MAX_DURATION = 24 * 3600  # seconds; anything longer is not a song
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

MAX_CANDIDATES = 8

# A match is "uncertain" (item 9) when our score is under UNCERTAIN_SCORE or the recording's
# length is off by more than UNCERTAIN_DIFF seconds. Both are shown to the owner, verbatim.
UNCERTAIN_SCORE = 70.0
UNCERTAIN_DIFF = 10.0
UNCERTAIN_RULE = (
    f"score under {UNCERTAIN_SCORE:.0f}% or length off by more than {UNCERTAIN_DIFF:.0f} s"
)
_FEAT = re.compile(r"[\(\[]\s*(feat|ft|with)\.?\s[^\)\]]*[\)\]]|\s(feat|ft)\.?\s.*$", re.I)
_NON_WORD = re.compile(r"[^\w]+")
_VERSION_MARKERS = (
    "sped up", "speed up", "slowed", "reverb", "nightcore", "remix", "live",
    "acoustic", "instrumental", "karaoke", "cover", "8d", "remaster",
    "extended", "radio edit", "remastered", "spedup", "slowed down",
)
_UNVERIFIED_UPLOAD_MARKERS = ("lyric", "lyrics", "lyric video", "歌詞", "歌词", "歌詞版", "歌词版")


def _versions(title: str) -> set[str]:
    words = f" {_norm(title)} "
    return {marker for marker in _VERSION_MARKERS if f" {marker} " in words}


def _artist_channel(artist: str, channel: str) -> bool:
    if f" {artist} " in f" {_norm(channel)} ":
        return True
    compact_artist = artist.replace(" ", "")
    compact_channel = _norm(channel).replace(" ", "")
    return any(compact_channel == compact_artist + suffix for suffix in ("vevo", "official"))

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


def _cover_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or not host.endswith((".scdn.co", ".spotifycdn.com"))
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        return None
    return value


@dataclass(frozen=True)
class SpotifyTrack:
    track_id: str
    index: int  # 1-based position in the listing
    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration: float | None = None
    explicit: bool = False
    cover_url: str | None = None

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
    manual: bool = False  # the owner chose this one (pasted, or picked from the candidates)
    score: float | None = None  # our own score (match_score); None for a pasted link
    candidates: tuple[Candidate, ...] = ()

    @property
    def url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class Candidate:
    """One YouTube Music result the owner may pick instead of the automatic match."""

    video_id: str
    title: str = ""
    channel: str = ""
    duration: float | None = None


def _norm(text: str) -> str:
    """Lower-case words only, without a "(feat. X)" part: what two titles are compared on."""
    return " ".join(_NON_WORD.sub(" ", _FEAT.sub(" ", text or "").casefold()).split())


def match_score(track: SpotifyTrack, title: str, channel: str, duration: float | None) -> float:
    """How sure we are that a YouTube result is ``track``: 0-100.

    50 for the title (similarity after ignoring "feat." parts and punctuation), 25 when one of
    Spotify's artists appears in the channel or the title, 25 for the length (full at 0 s off,
    nothing at 30 s or more; half when a length is unknown).
    """
    want = _norm(track.title)
    got = _norm(title)
    title_part = SequenceMatcher(None, want, got).ratio() if want and got else 0.0
    if want and got and (f" {want} " in f" {got} "):
        title_part = max(title_part, 0.9)  # "Song (Official Audio)" is still the song
    artists = [_norm(a) for a in track.artists if _norm(a)]
    artist_part = 1.0 if any(_artist_channel(a, channel) for a in artists) else 0.0
    diff = duration_diff(duration, track.duration)
    length_part = 0.5 if diff is None else max(0.0, 1 - min(abs(diff), 30.0) / 30.0)
    score = 50 * title_part + 25 * artist_part + 25 * length_part
    if not artist_part:
        score = min(score, UNCERTAIN_SCORE - 1)
    if _versions(track.title) != _versions(title):
        score = min(score, UNCERTAIN_SCORE - 1)
    got_words = f" {got} "
    want_words = f" {want} "
    if any(
        f" {marker} " in got_words and f" {marker} " not in want_words
        for marker in _UNVERIFIED_UPLOAD_MARKERS
    ):
        score = min(score, UNCERTAIN_SCORE - 1)
    return round(score, 1)


def is_uncertain(match: Match) -> bool:
    """A match to check before downloading. A link the owner chose is never flagged."""
    if match.manual:
        return False
    if match.duration_diff is None:
        return True
    if match.duration_diff is not None and abs(match.duration_diff) > UNCERTAIN_DIFF:
        return True
    return match.score is None or match.score < UNCERTAIN_SCORE


def parse_candidates(data: Any, track: SpotifyTrack) -> tuple[Candidate, ...]:
    """The worker's alternatives, validated; hostile or malformed rows are dropped."""
    raw = data.get("candidates") if isinstance(data, dict) else None
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in raw if isinstance(raw, list) else []:
        if len(out) >= MAX_CANDIDATES:
            break
        if not isinstance(row, dict):
            continue
        video_id = row.get("video_id")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        out.append(
            Candidate(
                video_id=video_id,
                title=_text(row.get("title")),
                channel=_text(row.get("channel")),
                duration=_seconds(row.get("duration")),
            )
        )
    return tuple(out)


def candidate_match(candidate: Candidate, track: SpotifyTrack) -> Match:
    """The owner picked this alternative: it becomes the row's match, scored like any other."""
    return Match(
        track_id=track.track_id,
        video_id=candidate.video_id,
        title=candidate.title,
        channel=candidate.channel,
        duration=candidate.duration,
        duration_diff=duration_diff(candidate.duration, track.duration),
        score=match_score(track, candidate.title, candidate.channel, candidate.duration),
        manual=True,
    )


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
        cover_url=_cover_url(raw.get("cover_url")),
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
    title = _text(data.get("title"))
    channel = _text(data.get("channel"))
    return Match(
        track_id=track.track_id,
        video_id=video_id,
        title=title,
        channel=channel,
        duration=duration,
        # Recomputed here from the two durations core trusts, never taken from the payload.
        duration_diff=duration_diff(duration, track.duration),
        confidence=confidence,
        # Our own score, computed here too: the worker's number is never the verdict.
        score=match_score(track, title, channel, duration),
        candidates=parse_candidates(data, track),
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


def with_candidates(match: Match, candidates: tuple[Candidate, ...]) -> Match:
    """Keep the looked-up alternatives when the owner overrides the match."""
    return replace(match, candidates=candidates) if candidates else match


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
    output_names: dict[str, str] | None = None,
    confirmed_ids: set[str] | None = None,
) -> list[JobSpec]:
    """One job per track, requiring a reviewed or explicitly confirmed recording."""
    specs = []
    for track in tracks:
        match = matches.get(track.track_id)
        if match is None or match.track_id != track.track_id:
            raise ValueError(f"Spotify track {track.track_id} has no reviewed match")
        if is_uncertain(match) and track.track_id not in (confirmed_ids or set()):
            raise ValueError(f"Spotify track {track.track_id} needs match confirmation")
        options = download_options(
            video_id=match.video_id, archive=archive
        )
        if output_names and track.track_id in output_names:
            options["output_name"] = output_names[track.track_id]
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
