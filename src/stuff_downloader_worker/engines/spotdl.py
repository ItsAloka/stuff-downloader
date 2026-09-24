"""Spotify engine (plan §7): Spotify's public embed data, a YouTube Music match, a tagged MP3.

Runs in the isolated ``envs\\spotdl`` runtime, which ships ytmusicapi and requests. Nothing here
needs a Spotify account or developer credentials:

- **Listing** reads the public embed page (``/embed/{track|album|playlist}/<id>``) in one
  request. Its ``trackList`` stops at EMBED_PAGE_CAP rows, so only a longer list falls back to
  spotDL's ``get_metadata`` for the rest. That is the one place spotDL is still imported.
- **Artwork** is Spotify's own. Every picture URL is normalized to ``https://i.scdn.co/image/<hash>``
  (the same hash is served by Spotify's other image hosts), so the GUI only ever fetches from one
  host. An album's rows share the album cover; a playlist's rows carry none, and the GUI looks
  each one up lazily through oEmbed when the row is on screen. Tagging uses the 640 px picture
  from the track's own embed (``visualIdentity.image``).
- **Matching** calls ytmusicapi directly, in three steps: the album (when it is known), then
  YouTube Music *songs* with strict rules, then *videos*. A video is always marked uncertain.
- The audio is downloaded by this project's own yt-dlp engine code (the MP3 preset), and the file
  is then re-tagged with Spotify's title, artists, album, year and cover through mutagen.

Facts this module is built around, checked live in September 2026:

- The embed page's ``__NEXT_DATA__`` holds the entity: title, owner (``authors`` for a playlist,
  ``subtitle`` for an album), cover, and ``trackList`` rows of uri, title, subtitle (the
  artists), duration in ms and explicit. It names no per-track album and no per-track picture.
- oEmbed (``/oembed?url=…/track/<id>``) answers with a 300 px ``thumbnail_url`` per track.
- spotDL's shared Web API credentials are over quota (429, ``Retry-After: 86400``), so when the
  fallback runs it uses spotDL's free client only.

Nothing here prints: stdout is the protocol channel.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from stuff_downloader_worker import tagging
from stuff_downloader_worker.engines.base import Emit, EngineError
from stuff_downloader_worker.names import safe_output_name
from stuff_downloader_worker.protocol import JobSpec, media_result

PRESET_ID = "spotify_mp3"
MAX_TRACKS = 500
MAX_TEXT = 300
MAX_ARTISTS = 20
MAX_COVER_BYTES = 5_000_000
COVER_TIMEOUT = 20
ARCHIVE_FILENAME = ".stuff-downloader-spotify-archive.txt"
MAX_EDITED_TITLE = 300
MAX_NAME = 150  # characters of "Artist - Title" before the extension

EMBED_PAGE_CAP = 100  # rows the embed page lists; a list this long may have more
MAX_EMBED_BYTES = 5_000_000
EMBED_TIMEOUT = 20
EMBED_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
KINDS = ("track", "album", "playlist")
# Spotify serves the same picture, by hash, from all of these; only i.scdn.co ever leaves here.
IMAGE_HOST = re.compile(r"(i\.scdn\.co|image-cdn-[a-z]{2}\.spotifycdn\.com)")
IMAGE_PATH = re.compile(r"/image/([0-9a-f]{40})")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$")

# Matching (plan §7 item 3). The album's own track is the official audio: ±2 s. A YouTube
# Music song must be within ±3 s. A video is only a fallback, and always uncertain.
ALBUM_MAX_DIFF = 2
SONG_MAX_DIFF = 3
VIDEO_MAX_DIFF = 30
SONG_MIN_SCORE = 70.0
SEARCH_LIMIT = 10
ALBUM_TRIES = 2
MIN_TITLE_SIMILARITY = 0.8
METHODS = ("album", "song", "video")
# A version with one of these in its title is a different recording, unless Spotify's title
# says the same thing.
_VARIANT_WORDS = (
    "sped up", "speed up", "slowed", "reverb", "nightcore", "remix", "live", "acoustic",
    "instrumental", "karaoke", "cover", "8d", "remaster", "extended", "radio edit", "lyric",
    "lyrics",
)  # fmt: skip
_FEAT = re.compile(r"[\(\[]\s*(feat|ft|with)\.?\s[^\)\]]*[\)\]]", re.IGNORECASE)
# "(Official Audio)", "[Official Music Video]"… say nothing about the recording.
_OFFICIAL = re.compile(
    r"[\(\[]\s*official\s*(audio|music video|video|visualizer)?\s*[\)\]]", re.IGNORECASE
)
_NON_WORD = re.compile(r"[^\w]+")


# ── validation ────────────────────────────────────────────────────────────────────────────
def parse_url(url: str) -> tuple[str, str]:
    """(kind, id) from the job URL, which core rebuilt as https://open.spotify.com/<kind>/<id>."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise EngineError("bad_options", "not a Spotify link") from None
    segments = [s for s in parts.path.split("/") if s]
    if (
        parts.scheme != "https"
        or parts.hostname != "open.spotify.com"
        or parts.query
        or len(segments) != 2
        or segments[0] not in KINDS
        or not SPOTIFY_ID.fullmatch(segments[1])
    ):
        raise EngineError("bad_options", "not a Spotify track, album or playlist link")
    return segments[0], segments[1]


def _album_options(opts: dict[str, Any]) -> None:
    """The album a track belongs to, when core knows it; the same checks in match and download."""
    album = opts.get("album")
    if album is not None and (not isinstance(album, str) or len(album) > MAX_TEXT):
        raise EngineError("bad_options", "'album' must be a short string")
    number = opts.get("album_track")
    if number is not None and (
        isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_TRACKS
    ):
        raise EngineError("bad_options", "'album_track' must be a track number")


def parse_match_options(opts: dict[str, Any]) -> None:
    if set(opts) - {"mode", "album"}:
        raise EngineError("bad_options", "match takes only an album")
    _album_options(opts)


def parse_download_options(opts: dict[str, Any]) -> tuple[str | None, bool]:
    """(video_id or None, archive). Mirrors core.spotify.download_options exactly."""
    allowed = {"mode", "preset", "video_id", "archive", "edited_title", "album", "album_track"}
    if set(opts) - allowed or opts.get("preset") != PRESET_ID:
        raise EngineError("bad_options", f"a Spotify download takes preset={PRESET_ID}")
    archive = opts.get("archive", True)
    if not isinstance(archive, bool):
        raise EngineError("bad_options", "'archive' must be true or false")
    video_id = opts.get("video_id")
    if video_id is not None and (not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id)):
        raise EngineError("bad_options", "'video_id' must be a YouTube video id")
    edited = opts.get("edited_title")
    if edited is not None and (not isinstance(edited, str) or len(edited) > MAX_EDITED_TITLE):
        raise EngineError("bad_options", "'edited_title' must be a short string")
    _album_options(opts)
    return video_id, archive


def edited_stem(opts: dict[str, Any]) -> str | None:
    """The file name the owner typed (plan §5.5), made Windows-safe; None keeps the default.

    It names the file only: the tags stay Spotify's own title, artists and album.
    """
    return safe_output_name(opts.get("edited_title"))


def safe_text(value: Any) -> str:
    """Bounded, single-line, URL-free text from Spotify or YouTube metadata."""
    if not isinstance(value, str):
        return ""
    # Control and format characters (NUL, bidi overrides that reorder what the owner reads)
    # are dropped before anything else.
    # Whitespace controls (tab, newline) are kept here; split() below turns them into spaces.
    visible = "".join(
        c for c in value if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf")
    )
    return _URL.sub("[link]", " ".join(visible.split()))[:MAX_TEXT]


def describe_error(exc: BaseException) -> tuple[str, str]:
    """(code, safe message) for a Spotify / YouTube Music failure."""
    text = safe_text(f"{exc.__class__.__name__}: {exc}")[:500]
    lowered = text.lower()
    if any(needle in lowered for needle in ("429", "too many requests", "rate limit")):
        return "download_error", f"http error 429: {text}"
    if any(needle in lowered for needle in ("404", "not found")):
        return "download_error", f"http error 404: {text}"
    return "download_error", text or "Spotify could not be read"


def spotify_image(url: Any) -> str | None:
    """``https://i.scdn.co/image/<hash>`` for a Spotify picture URL, or None for anything else."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or not IMAGE_HOST.fullmatch(parts.hostname or "")
        or parts.port not in (None, 443)
        or parts.username
        or parts.query
    ):
        return None
    found = IMAGE_PATH.fullmatch(parts.path)
    return f"https://i.scdn.co/image/{found.group(1)}" if found else None


# ── the embed page ────────────────────────────────────────────────────────────────────────
def fetch_embed(kind: str, spotify_id: str) -> dict[str, Any]:
    """The embed page's entity for one validated link. One request, no credentials."""
    import requests

    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    with requests.get(url, headers=EMBED_HEADERS, stream=True, timeout=EMBED_TIMEOUT) as resp:
        if resp.status_code == 404:
            raise EngineError("download_error", f"http error 404: that {kind} was not found")
        if resp.status_code != 200:
            raise EngineError("download_error", f"http error {resp.status_code}: Spotify")
        body = resp.raw.read(MAX_EMBED_BYTES + 1, decode_content=True)
    if len(body) > MAX_EMBED_BYTES:
        raise EngineError("download_error", "Spotify's page was too large")
    return embed_entity(body.decode("utf-8", "replace"))


def embed_entity(html: str) -> dict[str, Any]:
    """The ``entity`` from an embed page's ``__NEXT_DATA__``; a clear error when it is not there."""
    found = _NEXT_DATA.search(html)
    try:
        data = json.loads(found.group(1)) if found else None
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        entity = None
    if not isinstance(entity, dict):
        raise EngineError("download_error", "http error 404: Spotify showed no song list there")
    return entity


def _images(entity: dict[str, Any]) -> list[tuple[int, str]]:
    """(width, i.scdn.co url) for every picture the entity names."""
    found: list[tuple[int, str]] = []
    visual = entity.get("visualIdentity")
    for raw in (visual.get("image") if isinstance(visual, dict) else None) or []:
        if isinstance(raw, dict) and (url := spotify_image(raw.get("url"))):
            width = raw.get("maxWidth")
            found.append((width if isinstance(width, int) else 0, url))
    cover = entity.get("coverArt")
    for raw in (cover.get("sources") if isinstance(cover, dict) else None) or []:
        if isinstance(raw, dict) and (url := spotify_image(raw.get("url"))):
            width = raw.get("width")
            found.append((width if isinstance(width, int) else 300, url))
    return found


def picture(entity: dict[str, Any], width: int) -> str | None:
    """The entity's picture closest to ``width`` pixels, or None."""
    images = _images(entity)
    if not images:
        return None
    return min(images, key=lambda image: abs(image[0] - width))[1]


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return round(value / 1000, 1)


def _artists(raw: Any) -> list[str]:
    names = []
    if isinstance(raw, list):
        for artist in raw[:MAX_ARTISTS]:
            name = artist.get("name") if isinstance(artist, dict) else artist
            clean = safe_text(name)
            if clean:
                names.append(clean)
    return names


def _track_id(uri: Any) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("spotify:track:"):
        return None
    value = uri.removeprefix("spotify:track:")
    return value if SPOTIFY_ID.fullmatch(value) else None


def embed_row(item: Any, album: str = "", art: str | None = None) -> dict[str, Any] | None:
    """The listing row for one embed ``trackList`` item, or None for an episode or a bad row."""
    if not isinstance(item, dict) or item.get("entityType", "track") != "track":
        return None
    track_id = _track_id(item.get("uri"))
    if track_id is None:
        return None
    # The embed names the artists as one "A, B" line.
    subtitle = safe_text(item.get("subtitle"))
    row = {
        "id": track_id,
        "title": safe_text(item.get("title")) or "Untitled",
        "artists": [a for a in (s.strip() for s in subtitle.split(", ")) if a][:MAX_ARTISTS],
        "album": album,
        "duration": _seconds(item.get("duration")),
        "explicit": item.get("isExplicit") is True,
    }
    if art:
        row["art"] = art
    return row


def track_entity_row(entity: dict[str, Any], spotify_id: str) -> dict[str, Any]:
    """The one row a track link lists, from the track's own embed."""
    if entity.get("type") != "track" or entity.get("id") != spotify_id:
        raise EngineError("download_error", "http error 404: that track was not found")
    row = {
        "id": spotify_id,
        "title": safe_text(entity.get("name") or entity.get("title")) or "Untitled",
        "artists": _artists(entity.get("artists")),
        "album": "",
        "duration": _seconds(entity.get("duration")),
        "explicit": entity.get("isExplicit") is True,
    }
    art = picture(entity, 300)
    if art:
        row["art"] = art
    return row


def song_row(song: Any, album: str = "", art: str | None = None) -> dict[str, Any] | None:
    """The listing row for a spotDL Song from ``get_metadata`` (the >100-row fallback)."""
    track_id = getattr(song, "song_id", None)
    if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
        return None
    duration = getattr(song, "duration", None)
    row = {
        "id": track_id,
        "title": safe_text(getattr(song, "name", "")) or "Untitled",
        "artists": _artists(list(getattr(song, "artists", None) or [])),
        "album": album,
        "duration": float(duration)
        if isinstance(duration, int | float) and not isinstance(duration, bool) and duration > 0
        else None,
        "explicit": getattr(song, "explicit", False) is True,
    }
    if art:
        row["art"] = art
    return row


def track_fields(entity: dict[str, Any], track_id: str, opts: dict[str, Any]) -> dict[str, Any]:
    """What one track is matched and tagged with: its embed, plus the album core knows."""
    row = track_entity_row(entity, track_id)
    artists = row["artists"] or ["Unknown artist"]
    release = entity.get("releaseDate")
    iso = release.get("isoString") if isinstance(release, dict) else None
    date = iso[:10] if isinstance(iso, str) else None
    date = date if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
    return {
        "name": row["title"],
        "artists": artists,
        "artist": artists[0],
        "album_name": safe_text(opts.get("album")),
        "album_artist": artists[0],
        "track_number": opts.get("album_track"),
        "duration": row["duration"] or 0,
        "explicit": row["explicit"],
        "year": date[:4] if date else None,
        "date": date,
        "url": f"https://open.spotify.com/track/{track_id}",
        "cover_url": picture(entity, 640),
        "track_id": track_id,
    }


# ── matching ──────────────────────────────────────────────────────────────────────────────
def _norm(text: Any) -> str:
    """Lower-case words only, without "(feat. X)" or "(Official Audio)", for comparing."""
    if not isinstance(text, str):
        return ""
    text = _OFFICIAL.sub(" ", _FEAT.sub(" ", text))
    return " ".join(_NON_WORD.sub(" ", text.casefold()).split())


def _variants(title: str) -> set[str]:
    lowered = f" {_norm(title)} "
    found = {w for w in _VARIANT_WORDS if f" {w} " in lowered}
    return {"lyric" if w == "lyrics" else w for w in found}


def _similar(a: Any, b: Any) -> float:
    left, right = _norm(a), _norm(b)
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def _length(raw: dict[str, Any]) -> float | None:
    length = raw.get("duration_seconds")
    if isinstance(length, bool) or not isinstance(length, int | float) or length <= 0:
        return None
    return float(length)


def _names(raw: dict[str, Any]) -> list[str]:
    return [a.get("name") for a in raw.get("artists") or [] if isinstance(a, dict)]


def _same_artist(fields: dict[str, Any], names: list[Any]) -> bool:
    want = {_norm(a) for a in fields.get("artists") or []} - {""}
    return bool(want & {_norm(a) for a in names})


def _found(raw: dict[str, Any], method: str, score: float, album: Any) -> dict[str, Any]:
    names = _names(raw)
    return {
        "video_id": raw["videoId"],
        "title": safe_text(raw.get("title")),
        "channel": ", ".join(safe_text(a) for a in names if a),
        "duration": _length(raw),
        "score": round(min(score, 100.0), 1),
        "method": method,
        "album": safe_text(album) if isinstance(album, str) else "",
    }


def _recording(raw: Any, fields: dict[str, Any], max_diff: float) -> tuple[float, float] | None:
    """(title similarity, length difference) when ``raw`` is Spotify's recording, else None.

    The same title (ignoring "feat." parts), the same version markers on both sides, and a
    length within ``max_diff`` seconds.
    """
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("videoId")
    length = _length(raw)
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id) or length is None:
        return None
    title = str(raw.get("title") or "")
    if _variants(title) != _variants(str(fields.get("name") or "")):
        return None
    similarity = _similar(fields.get("name"), title)
    diff = abs(length - float(fields.get("duration") or 0))
    if similarity < MIN_TITLE_SIMILARITY or (fields.get("duration") and diff > max_diff):
        return None
    return similarity, diff


def pick_album_track(album: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Spotify's track inside one ytmusicapi ``get_album`` result: title and length within ±2 s."""
    if not isinstance(album, dict):
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for raw in (album.get("tracks") if isinstance(album.get("tracks"), list) else [])[:200]:
        fit = _recording(raw, fields, ALBUM_MAX_DIFF)
        if fit is None:
            continue
        similarity, diff = fit
        score = 70 * similarity + 30 * (1 - diff / (ALBUM_MAX_DIFF + 1))
        if best is None or score > best[0]:
            best = (score, _found(raw, "album", score, album.get("title")))
    return best[1] if best else None


def pick_song(results: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The YouTube Music *song* that is Spotify's recording, or None if none clearly is.

    ``results`` is ytmusicapi's ``search(filter="songs")`` output. A result must name one of
    Spotify's artists, carry the same title with the same version markers, and run within
    SONG_MAX_DIFF seconds of Spotify's length. Among those, the closest length wins, then the
    same explicit/clean version as Spotify's, then the same album.
    """
    if not isinstance(results, list):
        return None
    want_album = _norm(fields.get("album_name"))
    best: tuple[float, dict[str, Any]] | None = None
    for raw in results[:50]:
        if not isinstance(raw, dict) or raw.get("resultType") not in (None, "song"):
            continue
        if not _same_artist(fields, _names(raw)):
            continue
        fit = _recording(raw, fields, SONG_MAX_DIFF)
        if fit is None:
            continue
        similarity, diff = fit
        album = (raw.get("album") or {}).get("name") if isinstance(raw.get("album"), dict) else None
        score = 60 * similarity + 30 * (1 - diff / (SONG_MAX_DIFF + 1))
        score += 5 if bool(raw.get("isExplicit")) == bool(fields.get("explicit")) else 0
        score += 5 if want_album and _norm(album) == want_album else 0
        if score >= SONG_MIN_SCORE and (best is None or score > best[0]):
            best = (score, _found(raw, "song", score, album))
    return best[1] if best else None


def pick_video(results: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The likeliest *video* for the song. Only a fallback: core always marks it uncertain."""
    if not isinstance(results, list):
        return None
    want = _norm(fields.get("name"))
    best: tuple[float, dict[str, Any]] | None = None
    for raw in results[:50]:
        if not isinstance(raw, dict) or raw.get("resultType") not in (None, "video"):
            continue
        video_id = raw.get("videoId")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            continue
        # A lyric video carries the same audio; a live cut or a remix does not.
        variants = _variants(str(raw.get("title") or "")) - {"lyric"}
        if variants != _variants(str(fields.get("name") or "")) - {"lyric"}:
            continue
        title = _norm(raw.get("title"))
        named = want and f" {want} " in f" {title} "
        similarity = max(_similar(fields.get("name"), raw.get("title")), 0.9 if named else 0.0)
        if similarity < 0.6:
            continue
        artist = _same_artist(fields, _names(raw)) or any(
            f" {_norm(a)} " in f" {title} " for a in fields.get("artists") or [] if _norm(a)
        )
        length = _length(raw)
        diff = abs(length - float(fields.get("duration") or 0)) if length else VIDEO_MAX_DIFF
        if fields.get("duration") and diff > VIDEO_MAX_DIFF:
            continue
        length_part = 1 - min(diff, VIDEO_MAX_DIFF) / VIDEO_MAX_DIFF
        score = 50 * similarity + 25 * artist + 25 * length_part
        if best is None or score > best[0]:
            best = (score, _found(raw, "video", score, None))
    return best[1] if best else None


MAX_CANDIDATES = 8


def candidate_rows(results: Any, kind: str = "song") -> list[dict[str, Any]]:
    """Up to MAX_CANDIDATES results for the owner to choose from: ids and plain text only."""
    rows: list[dict[str, Any]] = []
    for raw in results[:50] if isinstance(results, list) else []:
        if len(rows) >= MAX_CANDIDATES:
            break
        if not isinstance(raw, dict) or raw.get("resultType") not in (None, kind):
            continue
        video_id = raw.get("videoId")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            continue
        if any(row["video_id"] == video_id for row in rows):
            continue
        rows.append(
            {
                "video_id": video_id,
                "title": safe_text(raw.get("title")),
                "channel": ", ".join(safe_text(a) for a in _names(raw) if a),
                "duration": _length(raw),
                "kind": kind,
            }
        )
    return rows


def ytmusic() -> Any:
    from ytmusicapi import YTMusic

    return YTMusic()


def find_match(
    fields: dict[str, Any], emit: Emit, candidates: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Album first, then songs, then videos (plan §7 item 3). None when nothing was found.

    Returns {video_id, title, channel, duration, score, method, album}. Song and video results
    are appended to ``candidates`` when one is given.
    """
    ytm = ytmusic()
    picked = _album_first(ytm, fields, emit)
    query = f"{', '.join(fields.get('artists') or [])} {fields.get('name') or ''}".strip()
    if picked is not None and candidates is None:
        return picked
    try:
        songs = ytm.search(query, filter="songs", limit=SEARCH_LIMIT)
        if candidates is not None:
            # Still looked up after an album hit, so Change… has alternatives to offer.
            candidates.extend(candidate_rows(songs, "song"))
        picked = picked or pick_song(songs, fields)
    except Exception:
        emit("log", {"level": "warning", "message": "YouTube Music song search failed"})
    if picked is not None:
        return picked
    try:
        videos = ytm.search(query, filter="videos", limit=SEARCH_LIMIT)
        if candidates is not None:
            candidates.extend(candidate_rows(videos, "video"))
        return pick_video(videos, fields)
    except Exception:
        emit("log", {"level": "warning", "message": "YouTube Music video search failed"})
        return None


def _album_first(ytm: Any, fields: dict[str, Any], emit: Emit) -> dict[str, Any] | None:
    """Spotify's track on the YouTube Music album of the same name and artist, or None."""
    artist = (fields.get("artists") or [""])[0]
    album_name = fields.get("album_name")
    if album_name:
        try:
            albums = ytm.search(f"{album_name} {artist}".strip(), filter="albums", limit=5)
            tried = 0
            for raw in albums if isinstance(albums, list) else []:
                if tried >= ALBUM_TRIES:
                    break
                browse = raw.get("browseId") if isinstance(raw, dict) else None
                if not isinstance(browse, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", browse):
                    continue
                if not _same_artist(fields, _names(raw)):
                    continue
                if _similar(album_name, raw.get("title")) < MIN_TITLE_SIMILARITY:
                    continue
                tried += 1
                picked = pick_album_track(ytm.get_album(browse), fields)
                if picked is not None:
                    return picked
        except Exception:  # the song search still gets its turn
            emit("log", {"level": "warning", "message": "YouTube Music album search failed"})
    return None


# ── files ─────────────────────────────────────────────────────────────────────────────────
def file_stem(artists: list[str], title: str) -> str:
    """A Windows-safe "Artist, Artist - Title" for the final file name."""
    name = f"{', '.join(artists) or 'Unknown artist'} - {title or 'Untitled'}"
    name = _WINDOWS_BAD.sub("_", name)[:MAX_NAME].strip().rstrip(". ")  # Windows drops both
    if not name or _RESERVED.match(name):
        name = f"_{name}"
    return name


def move_no_overwrite(src: Path, folder: Path, stem: str, suffix: str) -> Path:
    """Move ``src`` to ``folder/stem.suffix``, or "stem (2).suffix" and so on if that exists."""
    for n in range(1, 1000):
        target = folder / (f"{stem}{suffix}" if n == 1 else f"{stem} ({n}){suffix}")
        if target.resolve() == src.resolve():
            return src
        try:
            # os.rename never replaces an existing file on Windows; os.link is the atomic
            # no-overwrite equivalent everywhere else.
            if os.name == "nt":
                os.rename(src, target)
            else:
                os.link(src, target)
                os.unlink(src)
            return target
        except FileExistsError:
            continue
    raise EngineError("download_error", "could not find a free file name")


class Archive:
    """Track ids already downloaded into one folder, one "<service> <id>" per line.

    Spotify, Apple Music and Deezer share the one file; the service prefix keeps their ids apart.
    """

    def __init__(self, folder: Path, service: str = "spotify") -> None:
        self.path = folder / ARCHIVE_FILENAME
        self.service = service

    def __contains__(self, track_id: str) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fp:
                return any(line.strip() == f"{self.service} {track_id}" for line in fp)
        except FileNotFoundError:
            return False

    def add(self, track_id: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write(f"{self.service} {track_id}\n")


def fetch_cover(url: str | None, allowed: Any = None) -> bytes | None:
    """The cover as JPEG bytes, or None.

    Only a picture that ``allowed`` returns unchanged is fetched: by default a normalized
    i.scdn.co picture; the music engine passes its own Apple Music / Deezer check.
    """
    check = allowed or spotify_image
    if not url or check(url) != url:
        return None
    import requests

    try:
        with requests.get(url, stream=True, timeout=COVER_TIMEOUT) as resp:
            if resp.status_code != 200:
                return None
            data = resp.raw.read(MAX_COVER_BYTES + 1, decode_content=True)
    except Exception:
        return None
    if not data or len(data) > MAX_COVER_BYTES or tagging.jpeg_size(data) is None:
        return None
    return data


def tag_mp3(path: Path, fields: dict[str, Any], cover: bytes | None) -> dict[str, Any]:
    """Replace the file's tags with Spotify's metadata and cover. Returns what was written.

    Album and track number are written only when known: an unknown album stays empty, and a
    track number exists only for an album link (plan §5.7). Whatever yt-dlp wrote for those is
    removed rather than left behind.
    """
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, WOAS, ID3NoHeaderError

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    track_no = fields.get("track_number")
    album = fields.get("album_name") or ""
    frames = {
        "TIT2": TIT2(encoding=3, text=fields["name"]),
        # One string: ID3v2.3 has no multi-value frames, and "A, B" reads well everywhere.
        "TPE1": TPE1(encoding=3, text=", ".join(fields["artists"])),
        "TPE2": TPE2(encoding=3, text=fields.get("album_artist") or fields["artist"]),
        "WOAS": WOAS(url=fields["url"]),
    }
    if album:
        frames["TALB"] = TALB(encoding=3, text=album)
    if track_no:
        frames["TRCK"] = TRCK(encoding=3, text=str(track_no))
    if fields.get("date") or fields.get("year"):
        frames["TDRC"] = TDRC(encoding=3, text=str(fields.get("date") or fields.get("year")))
    for frame_id in ("TALB", "TRCK", "TPOS", "TSRC", "TDRC", "TYER", "TDAT", "COMM", "TCON"):
        tags.delall(frame_id)
    for frame_id, frame in frames.items():
        tags.setall(frame_id, [frame])
    if cover is not None:
        # The YouTube thumbnail is replaced, not kept alongside: players show the first APIC.
        tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)])
    # ID3v2.3, not mutagen's default v2.4: Windows Explorer and Media Player show no cover art
    # from a v2.4 tag. update_to_v23 also turns TDRC into the v2.3 year/date frames.
    tags.update_to_v23()
    tags.save(str(path), v2_version=3)
    size = tagging.jpeg_size(cover) if cover else None
    return {
        "title": fields["name"],
        "artist": ", ".join(fields["artists"]),
        "album": album,
        "track_number": track_no,
        "cover": {"width": size[0], "height": size[1]} if size else None,
    }


# ── engine ────────────────────────────────────────────────────────────────────────────────
class SpotDlEngine:
    name = "spotdl"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        kind, spotify_id = parse_url(job.url)
        opts = dict(job.options)
        mode = opts.get("mode", "analyze")
        if mode == "analyze":
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", "analyze takes no options")
        elif mode == "match":
            parse_match_options(opts)
        elif mode == "download":
            video_id, archive = parse_download_options(opts)
        else:
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        if mode != "analyze" and kind != "track":
            raise EngineError("bad_options", f"{mode} works on one track")

        try:
            if mode == "analyze":
                return self._analyze(kind, spotify_id, emit)
            if mode == "match":
                return self._match(spotify_id, opts, emit)
            return self._download(job, spotify_id, video_id, archive, emit)
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(*describe_error(exc)) from None

    # ── analyze ───────────────────────────────────────────────────────────────────────────
    def _analyze(self, kind: str, spotify_id: str, emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        url = f"https://open.spotify.com/{kind}/{spotify_id}"
        entity = fetch_embed(kind, spotify_id)
        cover = picture(entity, 300)
        skipped = 0
        if kind == "track":
            row = track_entity_row(entity, spotify_id)
            rows, title, owner = [row], row["title"], ", ".join(row["artists"])
        else:
            title = safe_text(entity.get("name") or entity.get("title")) or kind.capitalize()
            # An album's rows are the album's own songs: its name and cover are theirs too. A
            # playlist's rows name neither; the GUI looks up each row's picture itself.
            album = title if kind == "album" else ""
            art = cover if kind == "album" else None
            if kind == "album":
                owner = safe_text(entity.get("subtitle"))
            else:
                owner = ", ".join(_artists(entity.get("authors"))) or safe_text(
                    entity.get("subtitle")
                )
            items = entity.get("trackList") if isinstance(entity.get("trackList"), list) else []
            rows = []
            for item in items:
                row = embed_row(item, album, art)
                if row is None:
                    skipped += 1
                else:
                    rows.append(row)
            if len(items) >= EMBED_PAGE_CAP:
                rows, skipped = self._rest(kind, url, rows, skipped, album, art, emit)
        truncated = len(rows) > MAX_TRACKS
        listed = rows[:MAX_TRACKS]
        emit("stage", {"stage": "completed"})
        # Even one song is listed as a playlist of one: it is matched and downloaded the same way.
        # The rows are the MediaResult entries; "tracks" is the same list, which core.spotify
        # parses.
        fields: dict[str, Any] = {}
        if cover:
            fields["cover"] = cover
        return media_result(
            "playlist",
            ["tracks"],
            title,
            url,
            entries=listed,
            site="Spotify",
            spotify_kind=kind,
            spotify_id=spotify_id,
            owner=owner,
            tracks=listed,
            skipped=skipped,
            truncated=truncated,
            **fields,
        )

    @staticmethod
    def _rest(
        kind: str,
        url: str,
        rows: list[dict[str, Any]],
        skipped: int,
        album: str,
        art: str | None,
        emit: Emit,
    ) -> tuple[list[dict[str, Any]], int]:
        """The rows past the embed page's cap, from spotDL (plan §7 item 1). Best effort."""
        try:
            from spotdl.types.album import Album
            from spotdl.types.playlist import Playlist

            SpotDlEngine._client()
            _, songs = (Album if kind == "album" else Playlist).get_metadata(url)
        except ImportError:
            # spotDL is an optional installer component (plan §7 item 5, R7).
            message = (
                f"only the first {len(rows)} songs could be listed. Longer Spotify lists need the "
                "optional spotDL component: run the installer again and tick it"
            )
            emit("log", {"level": "warning", "message": message})
            return rows, skipped
        except Exception:
            message = f"only the first {len(rows)} songs could be listed"
            emit("log", {"level": "warning", "message": message})
            return rows, skipped
        seen = {row["id"] for row in rows}
        extra_skipped = 0
        for song in songs:
            row = song_row(song, album, art)
            if row is None:
                extra_skipped += 1
            elif row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        return rows, max(skipped, extra_skipped)

    @staticmethod
    def _client() -> Any:
        """spotDL's free client, built once: only the >100-row fallback uses it."""
        from spotdl.utils.config import DEFAULT_CONFIG
        from spotdl.utils.spotify import SpotifyClient

        if SpotifyClient._instance is None:
            # The free client, always. None of user_auth, auth_token or use_cache_file is
            # passed, because each silently switches spotDL to the official API, whose shared
            # credentials are over quota. no_cache keeps nothing on disk.
            SpotifyClient.init(
                client_id=DEFAULT_CONFIG["client_id"],
                client_secret=DEFAULT_CONFIG["client_secret"],
                no_cache=True,
                use_official_api=False,
            )
        return SpotifyClient()

    # ── match ─────────────────────────────────────────────────────────────────────────────
    def _match(self, track_id: str, opts: dict[str, Any], emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        fields = track_fields(fetch_embed("track", track_id), track_id, opts)
        candidates: list[dict[str, Any]] = []
        found = find_match(fields, emit, candidates)
        if found is None:
            raise EngineError("no_match", "No matching song was found on YouTube Music.")
        emit("stage", {"stage": "completed"})
        return {
            "kind": "spotify_match",
            "track_id": track_id,
            "video_id": found["video_id"],
            "title": found["title"],
            "channel": found["channel"],
            "duration": found["duration"],
            "confidence": found["score"],
            "method": found["method"],
            "album": found["album"],
            "spotify_duration": fields["duration"],
            # The other results, so a wrong pick can be swapped without a pasted link.
            "candidates": [c for c in candidates if c["video_id"] != found["video_id"]],
        }

    # ── download ──────────────────────────────────────────────────────────────────────────
    def _download(
        self,
        job: JobSpec,
        track_id: str,
        video_id: str | None,
        archive: bool,
        emit: Emit,
    ) -> dict[str, Any]:
        folder = Path(job.output_dir)
        if not folder.is_dir():
            raise EngineError("download_error", "the download folder does not exist")
        ledger = Archive(folder)
        if archive and track_id in ledger:
            emit("stage", {"stage": "completed"})
            return {
                "title": "Spotify track",
                "preset": PRESET_ID,
                "files": [],
                "total_bytes": 0,
                "skipped": True,
                "skipped_reason": "Already downloaded",
            }

        emit("stage", {"stage": "analyzing"})
        fields = track_fields(fetch_embed("track", track_id), track_id, job.options)
        return download_matched(job, folder, fields, video_id, emit, ledger if archive else None)


def download_matched(
    job: JobSpec,
    folder: Path,
    fields: dict[str, Any],
    video_id: str | None,
    emit: Emit,
    ledger: Archive | None,
    cover_check: Any = None,
) -> dict[str, Any]:
    """Match (unless the owner reviewed ``video_id``), download the MP3, tag, name and archive it.

    Shared by the Spotify and music engines: ``fields`` is what the track is matched and tagged
    with, whichever service listed it. ``cover_check`` vets its cover URL (see fetch_cover).
    """
    manual = video_id is not None
    if video_id is None:
        found = find_match(fields, emit)
        if found is None:
            raise EngineError("no_match", "No matching song was found on YouTube Music.")
        video_id = found["video_id"]
        if not fields["album_name"] and found["album"]:
            fields["album_name"] = found["album"]

    # The audio: this project's own MP3 recipe, run by the yt-dlp engine in this env.
    from stuff_downloader_worker.engines.ytdlp import YtDlpEngine

    def forward(event_type: str, data: dict[str, Any]) -> None:
        if event_type == "stage" and data.get("stage") == "completed":
            return  # not complete until it is tagged
        emit(event_type, data)

    audio = YtDlpEngine().download(
        JobSpec(
            job_id=job.job_id,
            engine="ytdlp",
            url=f"https://www.youtube.com/watch?v={video_id}",
            output_dir=str(folder),
            options={
                "mode": "download",
                "preset": "mp3_music",
                "height": None,
                "compatible": True,
                "crop_cover": True,
            },
        ),
        forward,
    )
    mp3s = [Path(p) for p in audio.get("files") or [] if str(p).lower().endswith(".mp3")]
    if not mp3s or not mp3s[0].is_file():
        raise EngineError("no_output", "download finished without an MP3 file")

    emit("stage", {"stage": "tagging"})
    cover = fetch_cover(fields.get("cover_url"), cover_check)
    if cover is None:
        emit("log", {"level": "warning", "message": "the cover could not be loaded"})
    tags = tag_mp3(mp3s[0], fields, cover)
    stem = edited_stem(job.options) or file_stem(fields["artists"], fields["name"])
    final = move_no_overwrite(mp3s[0], folder, stem, ".mp3")
    if ledger is not None:
        ledger.add(fields["track_id"])
    emit("stage", {"stage": "completed"})
    return {
        "title": final.stem,
        "preset": PRESET_ID,
        "files": [str(final)],
        "total_bytes": final.stat().st_size,
        "tags": tags,
        "match": {"video_id": video_id, "manual": manual},
    }
