"""URL router (plan §5.3, §M3, §M5): YouTube, Spotify, and public video pages elsewhere.

Only http/https links are accepted. A YouTube link is rebuilt from the parsed video or playlist
id, so tracking parameters and anything else in the pasted text never reach the worker. A link
to any other site cannot be rebuilt from an id — we do not know the site's URL grammar — so it
is instead reduced to scheme, host, path and query, with credentials and the fragment dropped,
and only after the host is shown to be a public internet name.

A link pasted without its scheme (``pbs.twimg.com/media/…``) is completed with ``https://``
when it starts with a public host name (plan §5.3, P14). Refusals are worded without assuming
the link is a video, since it may equally be a song, a photo or a file.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, replace
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
SITE_UNSUPPORTED_REASON = (
    "That is a site's home page. Paste the link to the video, song, photo or file itself."
)
INCOMPLETE_LINK_REASON = "That isn't a complete link. Copy the full address starting with https://"
ADDED_SCHEME_NOTE = "Added https:// to the start of the link."

# Spotify's marketing site holds no tracks. Handing it to the video engine would fail with
# something the owner cannot act on, so it is refused by name instead.
SPOTIFY_SITE_REASON = "Open the song, album or playlist in Spotify, then copy its share link."
_DEFERRED_HOSTS = {
    "spotify.com": SPOTIFY_SITE_REASON,
    "www.spotify.com": SPOTIFY_SITE_REASON,
}

# Plan §6.3: a Spotify track, album or playlist, rebuilt from its id. Artists, podcasts and
# user pages are refused by name: an artist is a discography, not a list the owner picked.
_SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
SPOTIFY_KINDS = ("track", "album", "playlist")
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
_SPOTIFY_LOCALE = re.compile(r"intl-[a-z]{2}(?:-[a-z]{2})?", re.IGNORECASE)
SPOTIFY_KIND_REASON = "Only Spotify songs, albums and playlists can be downloaded."
SPOTIFY_INVALID_REASON = "No Spotify song, album or playlist found in that link."
_NON_PUBLIC_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa", ".lan", ".test")

# Plan §7 "Other music sites": Apple Music and Deezer links get the Spotify treatment, with their
# song lists read from free public lookups (iTunes Lookup, the Deezer API; no keys). Each link is
# rebuilt from its kind and numeric id. Tidal and Amazon Music are refused by name: their audio is
# DRM-protected and they offer no public song information to match from.
CATALOG_SERVICES = ("apple", "deezer")
CATALOG_ID = re.compile(r"[1-9][0-9]{0,15}")
_APPLE_HOSTS = {"music.apple.com", "geo.music.apple.com", "itunes.apple.com"}
_APPLE_COUNTRY = re.compile(r"[a-z]{2}")
_DEEZER_HOSTS = {"deezer.com", "www.deezer.com"}
_DEEZER_SHORT_HOSTS = {"link.deezer.com", "deezer.page.link"}
_DEEZER_LANG = re.compile(r"[a-z]{2}(?:-[a-z]{2})?", re.IGNORECASE)
APPLE_INVALID_REASON = "No Apple Music song or album found in that link."
APPLE_PLAYLIST_REASON = (
    "Apple Music playlists cannot be read without an Apple account. Paste a song or album link."
)
DEEZER_INVALID_REASON = "No Deezer song, album or playlist found in that link."
DEEZER_SHORT_REASON = (
    "Open that Deezer share link in a browser, then copy the full deezer.com address."
)
TIDAL_REASON = (
    "Tidal is not supported: its music is copy-protected and it has no public song information."
)
AMAZON_MUSIC_REASON = (
    "Amazon Music is not supported: its music is copy-protected and it has no public song "
    "information."
)
_REFUSED_MUSIC_HOSTS = {
    "tidal.com": TIDAL_REASON,
    "www.tidal.com": TIDAL_REASON,
    "listen.tidal.com": TIDAL_REASON,
    "embed.tidal.com": TIDAL_REASON,
}
_AMAZON_MUSIC_HOST = re.compile(r"music\.amazon\.[a-z.]{2,6}")
_AMAZON_HOST = re.compile(r"(?:www\.)?amazon\.[a-z.]{2,6}")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_MUSIC_HOSTS = {"music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("shorts", "live", "embed", "v")

MAX_URL_LENGTH = 2048
# A pasted fragment that starts with a host name: labels, an optional port, then the end or a
# path/query/fragment. Anything else without "://" (mailto:, javascript:, a bare word) is not.
_SCHEMELESS_HOST = re.compile(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::\d{1,5})?(?=[/?#]|$)")

# A link whose path ends in one of these is the media file itself, not a page about it, so it
# goes to the direct HTTP engine. The engine re-checks the Content-Type before saving anything,
# and a link that only looks like a file but is a page is refused there, not saved.
DIRECT_FILE_EXTENSIONS = frozenset(
    {
        ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".3gp",
        ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Route:
    # "youtube" | "youtube_playlist" | "video" | "file" | "gallery" | "social" | "page"
    # | "spotify" | "catalog" | "unsupported" | "invalid"
    kind: str
    url: str = ""  # normalized watch or playlist URL (youtube only)
    video_id: str = ""
    playlist_id: str = ""  # set when the link also names a downloadable playlist
    playlist_url: str = ""  # normalized playlist URL, when a playlist can be downloaded
    playlist_reason: str = ""  # why a named playlist cannot be downloaded, when it cannot
    music: bool = False
    reason: str = ""
    note: str = ""  # something the owner should know about how the link was read
    spotify_kind: str = ""  # "track" | "album" | "playlist" (spotify only)
    spotify_id: str = ""
    service: str = ""  # "apple" | "deezer" (catalog only)
    catalog_kind: str = ""  # "track" | "album" | "playlist" (catalog only)
    catalog_id: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in (
            "youtube", "youtube_playlist", "video", "file", "gallery", "social", "page", "spotify",
            "catalog",
        )  # fmt: skip

    @property
    def is_catalog(self) -> bool:
        """A music-service link whose songs are matched from YouTube Music (Spotify included)."""
        return self.kind in ("spotify", "catalog")

    @property
    def is_file(self) -> bool:
        return self.kind == "file"

    @property
    def is_gallery(self) -> bool:
        return self.kind == "gallery"

    @property
    def is_spotify(self) -> bool:
        return self.kind == "spotify"

    @property
    def is_youtube(self) -> bool:
        return self.kind in ("youtube", "youtube_playlist")

    @property
    def is_playlist(self) -> bool:
        return self.kind == "youtube_playlist"

    @property
    def engine(self) -> str:
        if not self.ok:
            return ""
        engines = {
            "file": "http",
            "gallery": "gallerydl",
            "social": "social",
            "page": "social",  # the page's own og:image, last in the chain
            "spotify": "spotdl",
            "catalog": "music",
        }
        return engines.get(self.kind, "ytdlp")


# Plan §5.3 step 3: photo posts, carousels, stories, albums and media timelines go to gallery-dl.
# Video pages on the same sites (reels, TikTok videos, a tweet's video) stay with yt-dlp, which
# handles them better; if yt-dlp finds no video there, the GUI falls back to gallery-dl.
_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_TIKTOK_SHORT_HOSTS = {"vt.tiktok.com", "vm.tiktok.com"}
_REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}
_REDDIT_SHORT_HOSTS = {"redd.it"}
_NUMERIC = re.compile(r"\d{5,25}")
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{5,40}")
# Instagram paths that are site pages, not a profile name.
_INSTAGRAM_RESERVED = frozenset(
    {"explore", "accounts", "direct", "about", "developer", "legal", "reels", "reel", "tv",
     "p", "stories", "web", "challenge", "emails", "privacy", "terms"}
)  # fmt: skip
_INSTAGRAM_USER = re.compile(r"[A-Za-z0-9._]{1,30}")
# Hosts where downloads run one at a time with delays (plan §6.1): bulk access to them is what
# gets an account throttled.
SOCIAL_HOST_GROUPS = {
    **{h: "instagram" for h in _INSTAGRAM_HOSTS},
    **{h: "tiktok" for h in _TIKTOK_HOSTS | _TIKTOK_SHORT_HOSTS},
    **{h: "facebook" for h in _FACEBOOK_HOSTS},
    **{h: "x" for h in _X_HOSTS},
    **{h: "reddit" for h in _REDDIT_HOSTS | _REDDIT_SHORT_HOSTS},
    # Spotify metadata is read by scraping its web player, which throttles bursts.
    **{h: "spotify" for h in _SPOTIFY_HOSTS},
}


def social_group(url: str) -> str:
    """The rate-limited site a URL belongs to ("instagram", "x", …), or "" for any other."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return SOCIAL_HOST_GROUPS.get(host, "")


def is_social_post(host: str, path: str) -> bool:
    """Whether a link names one public post the social extractor reads first (plan §6).

    Instagram posts, reels and IGTV; TikTok videos, photo posts and vt./vm. short links; X
    statuses; Reddit posts. Instagram stories and highlights come here too: anonymously they
    only ever need a login, and gallery-dl reports that as "user not found". Profiles stay
    with gallery-dl.
    """
    segments = [s for s in path.split("/") if s]
    if host in _INSTAGRAM_HOSTS:
        if segments[:1] == ["stories"] and len(segments) >= 2:
            return True
        if len(segments) >= 3 and segments[1] in ("p", "reel", "tv"):
            segments = segments[1:]  # /<user>/p/<code>/
        return (
            len(segments) >= 2
            and segments[0] in ("p", "reel", "reels", "tv")
            and bool(_SHORTCODE.fullmatch(segments[1]))
        )
    if host in _TIKTOK_HOSTS:
        return any(
            seg in ("video", "photo") and _NUMERIC.fullmatch(nxt)
            for seg, nxt in zip(segments, segments[1:], strict=False)
        )
    if host in _TIKTOK_SHORT_HOSTS or host in _REDDIT_SHORT_HOSTS:
        return len(segments) == 1
    if host in _X_HOSTS:
        return any(
            seg == "status" and _NUMERIC.fullmatch(nxt)
            for seg, nxt in zip(segments, segments[1:], strict=False)
        )
    if host in _REDDIT_HOSTS:
        if segments[:1] == ["gallery"] and len(segments) >= 2:
            return True
        return len(segments) >= 4 and segments[0] == "r" and segments[2] in ("comments", "s")
    return False


def is_gallery_link(host: str, path: str, query: str) -> bool:
    """Whether a social-site link names photos rather than a video page."""
    segments = [s for s in path.split("/") if s]
    if host in _INSTAGRAM_HOSTS:
        if segments[:1] == ["p"] and len(segments) >= 2:
            return True
        if segments[:1] == ["stories"] and len(segments) >= 2:
            return True  # a story or a highlight (stories/highlights/<id>)
        return (
            len(segments) == 1
            and segments[0].lower() not in _INSTAGRAM_RESERVED
            and bool(_INSTAGRAM_USER.fullmatch(segments[0]))
        )  # a profile's posts
    if host in _TIKTOK_HOSTS:
        return "photo" in segments
    if host in _FACEBOOK_HOSTS:
        if segments[:1] in (["photo"], ["photo.php"]) or segments[:2] == ["media", "set"]:
            return True
        return "photos" in segments or "photos_albums" in segments
    if host in _X_HOSTS:
        return (len(segments) == 2 and segments[1] == "media") or "photo" in segments
    return False


def is_direct_file_path(path: str) -> bool:
    """Whether a URL path names a media file by its extension (case-insensitive)."""
    last = path.rsplit("/", 1)[-1].lower()
    dot = last.rfind(".")
    return dot > 0 and last[dot:] in DIRECT_FILE_EXTENSIONS


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


def spotify_url(kind: str, spotify_id: str) -> str:
    return f"https://open.spotify.com/{kind}/{spotify_id}"


def _route_spotify(segments: list[str]) -> Route:
    """A Spotify link, rebuilt from its kind and id so ?si= and friends never travel."""
    if segments[:1] and _SPOTIFY_LOCALE.fullmatch(segments[0]):
        segments = segments[1:]
    if segments[:1] == ["embed"]:
        segments = segments[1:]
    if not segments:
        return Route("unsupported", reason=SPOTIFY_INVALID_REASON)
    kind = segments[0].lower()
    if kind not in SPOTIFY_KINDS:
        return Route("unsupported", reason=SPOTIFY_KIND_REASON)
    spotify_id = segments[1] if len(segments) >= 2 else ""
    if len(segments) > 2 or not SPOTIFY_ID.fullmatch(spotify_id):
        return Route("invalid", reason=SPOTIFY_INVALID_REASON)
    url = spotify_url(kind, spotify_id)
    return Route("spotify", url=url, spotify_kind=kind, spotify_id=spotify_id)


def catalog_url(service: str, kind: str, catalog_id: str, country: str = "us") -> str:
    """The canonical link core hands the music engine, built from validated parts only."""
    if service == "apple":
        path = "song" if kind == "track" else kind
        return f"https://music.apple.com/{country}/{path}/{catalog_id}"
    return f"https://www.deezer.com/{kind}/{catalog_id}"


def _catalog(service: str, kind: str, catalog_id: str, country: str = "us") -> Route:
    return Route(
        "catalog",
        url=catalog_url(service, kind, catalog_id, country),
        service=service,
        catalog_kind=kind,
        catalog_id=catalog_id,
    )


def _route_apple(host: str, segments: list[str], query: dict[str, list[str]]) -> Route:
    """An Apple Music song or album link. Playlists have no keyless lookup, so they are refused.

    ``/<cc>/album/<slug>/<id>?i=<track>`` is one song on the album, and becomes a song link.
    iTunes' old ``id123`` spelling is accepted too.
    """
    country = "us"
    if segments[:1] and _APPLE_COUNTRY.fullmatch(segments[0]):
        country, segments = segments[0], segments[1:]
    if not segments:
        return Route("invalid", reason=APPLE_INVALID_REASON)
    kind = segments[0].lower()
    if kind == "playlist":
        return Route("unsupported", reason=APPLE_PLAYLIST_REASON)
    if kind not in ("album", "song") or len(segments) not in (2, 3):
        return Route("unsupported", reason=APPLE_INVALID_REASON)
    raw_id = segments[-1]
    raw_id = raw_id[2:] if host == "itunes.apple.com" and raw_id.startswith("id") else raw_id
    if not CATALOG_ID.fullmatch(raw_id):
        return Route("invalid", reason=APPLE_INVALID_REASON)
    track = _first(query, "i")
    if kind == "album" and track:
        if not CATALOG_ID.fullmatch(track):
            return Route("invalid", reason=APPLE_INVALID_REASON)
        return _catalog("apple", "track", track, country)
    return _catalog("apple", "track" if kind == "song" else "album", raw_id, country)


def _route_deezer(segments: list[str]) -> Route:
    """A Deezer track, album or playlist link, with or without its language prefix."""
    if segments[:1] and _DEEZER_LANG.fullmatch(segments[0]) and len(segments) == 3:
        segments = segments[1:]
    if len(segments) != 2:
        return Route("unsupported", reason=DEEZER_INVALID_REASON)
    kind, catalog_id = segments[0].lower(), segments[1]
    if kind not in ("track", "album", "playlist"):
        return Route("unsupported", reason=DEEZER_INVALID_REASON)
    if not CATALOG_ID.fullmatch(catalog_id):
        return Route("invalid", reason=DEEZER_INVALID_REASON)
    return _catalog("deezer", kind, catalog_id)


def refused_music_site(host: str, path: str) -> str:
    """Why a Tidal or Amazon Music link is refused by name, or "" for any other link."""
    if host in _REFUSED_MUSIC_HOSTS:
        return _REFUSED_MUSIC_HOSTS[host]
    if _AMAZON_MUSIC_HOST.fullmatch(host):
        return AMAZON_MUSIC_REASON
    first = next((s for s in path.split("/") if s), "").lower()
    if _AMAZON_HOST.fullmatch(host) and first == "music":
        return AMAZON_MUSIC_REASON
    return ""


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
    refused = refused_music_site(host, parts.path)
    if refused:
        return Route("unsupported", reason=refused)
    if host in _DEEZER_SHORT_HOSTS:
        return Route("unsupported", reason=DEEZER_SHORT_REASON)
    if not is_public_host(host):
        return Route("unsupported", reason=SITE_PRIVATE_HOST_REASON)
    if parts.path.strip("/") == "" and not parts.query:
        return Route("unsupported", reason=SITE_UNSUPPORTED_REASON)
    if is_social_post(host, parts.path):
        return Route("social", url=_site_url(parts))
    if is_gallery_link(host, parts.path, parts.query):
        return Route("gallery", url=_site_url(parts))
    if is_direct_file_path(parts.path):
        return Route("file", url=_site_url(parts))
    return Route("video", url=_site_url(parts))


# Plan §5.3, §6: which engines analyze a link, in order. The first is the route's own engine;
# the rest get a turn only when the one before reports the link unsupported. A social post goes
# to the no-login extractor, then yt-dlp (videos), then gallery-dl. A page on an unknown site may
# be a photo post (gallery-dl), a file served without an extension (the direct engine, which
# checks the Content-Type before calling it one) or, last, a page with only its own picture
# (og:image). Nothing probes a link as a direct file before its own engine has answered.
_ANALYZE_FALLBACKS = {"social": ("video", "gallery"), "video": ("gallery", "file", "page")}


def analyze_fallbacks(route_: Route) -> list[Route]:
    """The routes to try, in order, after ``route_``'s own engine calls the link unsupported."""
    return [replace(route_, kind=kind) for kind in _ANALYZE_FALLBACKS.get(route_.kind, ())]


def should_fall_back(code: str | None) -> bool:
    """Whether an analyze error means "not this engine's kind of link", not a real failure."""
    return code == "unsupported"


def complete_link(text: str) -> tuple[str, str]:
    """``text`` with ``https://`` added when it is a scheme-less public link, and a note saying so.

    Only a fragment that starts with a public host name is completed; anything else comes back
    unchanged with no note, for ``route`` to refuse.
    """
    raw = (text or "").strip()
    if "://" in raw:
        return raw, ""
    match = _SCHEMELESS_HOST.match(raw)
    if not match or not is_public_host(match.group(0).split(":")[0]):
        return raw, ""
    return "https://" + raw, ADDED_SCHEME_NOTE


def route(text: str) -> Route:
    raw, note = complete_link(text)
    routed = _route(raw)
    return replace(routed, note=note) if note and routed.ok else routed


def _route(raw: str) -> Route:
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
    if "://" not in raw:
        return Route("invalid", reason=INCOMPLETE_LINK_REASON)
    if parts.scheme.lower() not in ("http", "https"):
        return Route("invalid", reason="Only http and https links are supported.")
    if not host:
        return Route("invalid", reason=INCOMPLETE_LINK_REASON)
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
    elif host in _SPOTIFY_HOSTS:
        return _route_spotify(segments)
    elif host in _APPLE_HOSTS:
        return _route_apple(host, segments, query)
    elif host in _DEEZER_HOSTS:
        return _route_deezer(segments)
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
