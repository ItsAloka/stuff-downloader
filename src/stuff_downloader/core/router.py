"""URL router (plan §5.3): YouTube and YouTube Music videos and playlists. No Qt imports.

Only http/https links to known YouTube hosts are accepted. The normalized URL is rebuilt from
the parsed video or playlist id, so tracking parameters and anything else in the pasted text
never reach the worker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
# Playlists we can actually enumerate: user/curated lists, YT Music albums, channel uploads and
# favourites. Anything else is refused by name rather than half-working.
_PLAYLIST_ID = re.compile(r"(?:PL|OLAK5uy_|UU|FL)[A-Za-z0-9_-]{10,62}")
_PRIVATE_LISTS = frozenset({"WL", "LL"})

PLAYLIST_PRIVATE_REASON = (
    "Watch Later and Liked Videos are private to your account, so they cannot be downloaded."
)
PLAYLIST_RADIO_REASON = (
    "That is an endless radio mix, not a fixed playlist. Open the real playlist and paste it."
)
PLAYLIST_INVALID_REASON = "No playlist found in that link."
HANDLE_UNSUPPORTED_REASON = (
    "YouTube handle links are not supported yet. Open the channel's Uploads playlist and paste it."
)
CHANNEL_INVALID_REASON = "No channel found in that YouTube link."

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_MUSIC_HOSTS = {"music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("shorts", "live", "embed", "v")

MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class Route:
    kind: str  # "youtube" | "youtube_playlist" | "unsupported" | "invalid"
    url: str = ""  # normalized watch or playlist URL (youtube only)
    video_id: str = ""
    playlist_id: str = ""  # set when the link also names a downloadable playlist
    playlist_url: str = ""  # normalized playlist URL, when a playlist can be downloaded
    playlist_reason: str = ""  # why a named playlist cannot be downloaded, when it cannot
    music: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in ("youtube", "youtube_playlist")

    @property
    def is_playlist(self) -> bool:
        return self.kind == "youtube_playlist"

    @property
    def engine(self) -> str:
        return "ytdlp" if self.ok else ""


def playlist_refusal(list_id: str) -> str:
    """Why this ``list=`` value cannot be downloaded, or "" when it can."""
    if list_id in _PRIVATE_LISTS:
        return PLAYLIST_PRIVATE_REASON
    if list_id.startswith("RD") or "MIX" in list_id.upper():
        return PLAYLIST_RADIO_REASON
    if not _PLAYLIST_ID.fullmatch(list_id):
        return PLAYLIST_INVALID_REASON
    return ""


def playlist_url(list_id: str, music: bool) -> str:
    base = "https://music.youtube.com" if music else "https://www.youtube.com"
    return f"{base}/playlist?list={list_id}"


def uploads_playlist_id(channel_id: str) -> str:
    """Return a channel's public uploads playlist id after strict validation."""
    return "UU" + channel_id[2:] if _CHANNEL_ID.fullmatch(channel_id) else ""


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or [""]
    return values[0]


def route(text: str) -> Route:
    raw = (text or "").strip()
    if not raw:
        return Route("invalid", reason="Paste a link first.")
    if len(raw) > MAX_URL_LENGTH or any(ch.isspace() for ch in raw):
        return Route("invalid", reason="That doesn't look like a single link.")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return Route("invalid", reason="That doesn't look like a valid link.")
    if parts.scheme.lower() not in ("http", "https"):
        return Route("invalid", reason="Only http and https links are supported.")
    if not host:
        return Route("invalid", reason="That doesn't look like a valid link.")

    query = parse_qs(parts.query)
    segments = [s for s in parts.path.split("/") if s]
    video_id = ""
    music = host in _MUSIC_HOSTS
    if host in _SHORT_HOSTS:
        video_id = segments[0] if segments else ""
    elif host in _YOUTUBE_HOSTS or music:
        if segments[:1] and segments[0].startswith("@"):
            return Route("unsupported", reason=HANDLE_UNSUPPORTED_REASON)
        if segments[:1] == ["watch"]:
            video_id = _first(query, "v")
        elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
            video_id = segments[1]
        elif segments[:1] == ["playlist"]:
            list_id = _first(query, "list")
            refusal = playlist_refusal(list_id)
            if refusal:
                return Route("unsupported", reason=refusal)
            return Route(
                "youtube_playlist",
                url=playlist_url(list_id, music),
                playlist_id=list_id,
                playlist_url=playlist_url(list_id, music),
                music=music,
            )
        elif not music and len(segments) == 2 and segments[0] == "channel":
            uploads_id = uploads_playlist_id(segments[1])
            if not uploads_id:
                return Route("invalid", reason=CHANNEL_INVALID_REASON)
            return Route(
                "youtube_playlist",
                url=playlist_url(uploads_id, False),
                playlist_id=uploads_id,
                playlist_url=playlist_url(uploads_id, False),
            )
    else:
        return Route("unsupported", reason="Only YouTube and YouTube Music links work so far.")

    if not _VIDEO_ID.fullmatch(video_id):
        return Route("invalid", reason="No video found in that YouTube link.")
    list_id = _first(query, "list")
    playlist_id = ""
    playlist_reason = ""
    if list_id:
        playlist_reason = playlist_refusal(list_id)
        if not playlist_reason:
            playlist_id = list_id
    base = "https://music.youtube.com" if music else "https://www.youtube.com"
    return Route(
        "youtube",
        url=f"{base}/watch?v={video_id}",
        video_id=video_id,
        playlist_id=playlist_id,
        playlist_url=playlist_url(playlist_id, music) if playlist_id else "",
        playlist_reason=playlist_reason,
        music=music,
    )
