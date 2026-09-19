"""URL router (plan §5.3, §M3): YouTube videos and playlists, plus public video pages elsewhere.

Only http/https links are accepted. A YouTube link is rebuilt from the parsed video or playlist
id, so tracking parameters and anything else in the pasted text never reach the worker. A link
to any other site cannot be rebuilt from an id — we do not know the site's URL grammar — so it
is instead reduced to scheme, host, path and query, with credentials and the fragment dropped,
and only after the host is shown to be a public internet name.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

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

SITE_PRIVATE_HOST_REASON = (
    "That link points at this machine or a private network, not a public website."
)
SITE_CREDENTIALS_REASON = "Links with a username or password in them are not supported."
SITE_UNSUPPORTED_REASON = "That site is not supported yet. Paste a link to a public video page."

# Sites a later milestone owns. Handing them to the video engine would half-work or fail with
# something the owner cannot act on, so they are refused by name instead.
_DEFERRED_HOSTS = {
    "open.spotify.com": "Spotify links are not supported yet.",
    "spotify.com": "Spotify links are not supported yet.",
    "www.spotify.com": "Spotify links are not supported yet.",
}
_NON_PUBLIC_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa", ".lan", ".test")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_MUSIC_HOSTS = {"music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("shorts", "live", "embed", "v")

MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class Route:
    kind: str  # "youtube" | "youtube_playlist" | "video" | "unsupported" | "invalid"
    url: str = ""  # normalized watch or playlist URL (youtube only)
    video_id: str = ""
    playlist_id: str = ""  # set when the link also names a downloadable playlist
    playlist_url: str = ""  # normalized playlist URL, when a playlist can be downloaded
    playlist_reason: str = ""  # why a named playlist cannot be downloaded, when it cannot
    music: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in ("youtube", "youtube_playlist", "video")

    @property
    def is_youtube(self) -> bool:
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


def looks_like_ip(host: str) -> bool:
    """Whether ``host`` is any spelling a client could read as an IP address.

    ``ipaddress.ip_address`` is not enough on its own. It accepts only the canonical dotted
    quad, while ``inet_aton`` — which is what the C resolver, curl and most HTTP stacks use —
    also accepts shorthand, octal and hexadecimal forms, so 127.1, 0177.0.0.1 and 0x7f.1 all
    mean 127.0.0.1. Deciding from ``ipaddress`` alone let every one of those through as if it
    were an ordinary domain. This asks the opposite question — could anything read it as an
    IP? — and answers yes for the whole family.
    """
    probe = host.strip("[]")
    if not probe:
        return False
    if ":" in probe:
        return True  # an IPv6 literal, or something shaped enough like one to refuse
    try:
        ipaddress.ip_address(probe)
    except ValueError:
        pass
    else:
        return True
    labels = probe.split(".")
    if len(labels) > 4:
        return False
    # inet_aton reads 1 to 4 parts, each decimal, 0-prefixed octal or 0x hex. If every part
    # parses as one of those, some resolver somewhere will treat this as an address.
    for label in labels:
        if not label:
            return False
        text = label.lower()
        try:
            if text.startswith("0x"):
                int(text, 16)
            elif text.startswith("0") and len(text) > 1:
                int(text, 8)
            else:
                int(text, 10)
        except ValueError:
            return False
    return True


def is_public_host(host: str) -> bool:
    """True when ``host`` is a name that can only resolve on the public internet.

    The video engine fetches whatever host we hand it, so a link naming localhost, a private
    address or an internal-only name is refused here rather than turned into a request from
    this machine. Every IP-shaped host is refused outright, public or not: a real video page
    has a name, and refusing the whole shape means there is no spelling to get clever with.
    """
    if not host or "_" in host or host.startswith("."):
        return False
    # NFKC first: fullwidth and other compatibility forms normalize to plain ASCII, so
    # "ｌｏｃａｌｈｏｓｔ" cannot walk past a check written against "localhost".
    probe = unicodedata.normalize("NFKC", host).rstrip(".").lower()
    if not probe or probe.startswith("."):
        return False
    if looks_like_ip(probe):
        return False
    if "." not in probe or probe == "localhost":
        return False
    return not probe.endswith(_NON_PUBLIC_SUFFIXES)


def durable_url(url: str) -> tuple[str, bool]:
    """``url`` reduced to a form safe to keep on disk, and whether anything was removed.

    A YouTube link is already rebuilt from a validated id and carries nothing private, so it
    survives whole and stays replayable. A link to any other site keeps its path but loses the
    query and fragment, because on many sites those carry the signed token that makes the link
    work — and history is a file that outlives the download.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - a malformed port must not escape as an exception here
    except ValueError:
        return "", True
    if host in _YOUTUBE_HOSTS or host in _MUSIC_HOSTS or host in _SHORT_HOSTS:
        return url, False
    netloc = f"{host}:{parts.port}" if parts.port else host
    safe = urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))
    return safe, safe != url


def _site_url(parts: Any) -> str:
    """A non-YouTube link reduced to the parts a video page needs.

    Credentials and the fragment are dropped, and scheme and host are lower-cased. The path and
    query are kept as pasted, because only the site knows which of them identify the video.
    """
    host = (parts.hostname or "").lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def _route_site(parts: Any) -> Route:
    """Route a link to a site other than YouTube, or say why we will not.

    ``route`` has already refused credentials and any non-http(s) scheme.
    """
    host = (parts.hostname or "").lower()
    deferred = _DEFERRED_HOSTS.get(host)
    if deferred:
        return Route("unsupported", reason=deferred)
    if not is_public_host(host):
        return Route("unsupported", reason=SITE_PRIVATE_HOST_REASON)
    if parts.path.strip("/") == "" and not parts.query:
        return Route("unsupported", reason=SITE_UNSUPPORTED_REASON)
    return Route("video", url=_site_url(parts))


def route(text: str) -> Route:
    raw = (text or "").strip()
    if not raw:
        return Route("invalid", reason="Paste a link first.")
    if len(raw) > MAX_URL_LENGTH or any(ch.isspace() for ch in raw):
        return Route("invalid", reason="That doesn't look like a single link.")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises on a malformed port, which _site_url would otherwise hit
    except ValueError:
        return Route("invalid", reason="That doesn't look like a valid link.")
    if parts.scheme.lower() not in ("http", "https"):
        return Route("invalid", reason="Only http and https links are supported.")
    if not host:
        return Route("invalid", reason="That doesn't look like a valid link.")
    if parts.username or parts.password:
        return Route("unsupported", reason=SITE_CREDENTIALS_REASON)

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
        return _route_site(parts)

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
