"""Apple Music and Deezer engine (plan §7 "Other music sites"): a public song list, a YouTube Music
match, a tagged MP3. The same three modes as the Spotify engine, and the same matching and tagging.

Nothing here needs an account or a key:

- **Apple Music** song and album links are read from the iTunes Lookup API
  (``itunes.apple.com/lookup?id=…&entity=song&country=…``). Apple offers no keyless lookup for
  playlists, so core refuses those by name.
- **Deezer** track, album and playlist links are read from the public Deezer API
  (``api.deezer.com/{track|album|playlist}/<id>``). Its lists are paged: every page is asked for by
  index here, never by following the ``next`` link the answer names.

Both services' audio is DRM-protected and never touched. Each song is matched from YouTube Music
by ``spotdl.find_match`` (album first, then songs, then videos, which are always uncertain), and
the MP3 is tagged with the service's title, artists, album, date and cover by ``spotdl.tag_mp3``.

Match and download jobs do not look the song up again: iTunes Lookup allows about 20 requests a
minute, so core hands over the song's details from the listing it already validated
(``options["song"]``), and they are checked again here.

Facts this module is built around, checked live in September 2026:

- A Lookup answer holds one ``collection`` row and then ``track`` rows (``kind: "song"``) with
  trackId, trackName, artistName, collectionName, trackTimeMillis, releaseDate (ISO),
  trackExplicitness, discNumber, trackNumber and artworkUrl100 on ``is<N>-ssl.mzstatic.com``,
  whose last path segment (``100x100bb.jpg``) picks the size.
- The Deezer API answers errors with HTTP 200 and ``{"error": {...}}``. A playlist embeds at most
  50 of its tracks; ``/playlist/<id>/tracks?index=&limit=`` pages through the rest. Pictures are
  ``cdn-images.dzcdn.net/images/cover/<md5>/<N>x<N>-000000-80-0-0.jpg``.

Nothing here prints: stdout is the protocol channel.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from stuff_downloader_worker.engines import spotdl
from stuff_downloader_worker.engines.base import Emit, EngineError
from stuff_downloader_worker.protocol import JobSpec, media_result

SERVICES = ("apple", "deezer")
KINDS = {"apple": ("track", "album"), "deezer": ("track", "album", "playlist")}
SITE_NAMES = {"apple": "Apple Music", "deezer": "Deezer"}
CATALOG_ID = re.compile(r"[1-9][0-9]{0,15}")
COUNTRY = re.compile(r"[a-z]{2}")
MAX_TRACKS = spotdl.MAX_TRACKS
MAX_TEXT = spotdl.MAX_TEXT
MAX_ARTISTS = spotdl.MAX_ARTISTS
MAX_JSON_BYTES = 5_000_000
LOOKUP_TIMEOUT = 20
DEEZER_PAGE = 100
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}

ROW_ART = 300  # px: the pictures rows show
TAG_ART = {"apple": 600, "deezer": 1000}  # px: the cover written into the MP3

_APPLE_IMAGE_HOST = re.compile(r"is[1-5]-ssl\.mzstatic\.com")
_APPLE_IMAGE_PATH = re.compile(
    r"(/image/thumb/[A-Za-z0-9/._-]{1,300})/\d{2,4}x\d{2,4}bb\.(?:jpg|png)"
)
_DEEZER_IMAGE_HOST = re.compile(r"(?:e-)?cdns?-images\.dzcdn\.net")
_DEEZER_IMAGE_PATH = re.compile(
    r"/images/(cover|playlist)/([0-9a-f]{32})/\d{2,4}x\d{2,4}-000000-80-0-0\.jpg"
)
_DATE = re.compile(r"\d{4}(?:-\d{2}-\d{2})?")


# ── validation ────────────────────────────────────────────────────────────────────────────
def parse_url(url: str) -> tuple[str, str, str, str]:
    """(service, kind, id, country) from the job URL, which core rebuilt from validated parts:
    ``https://music.apple.com/<cc>/<song|album>/<id>`` or ``https://www.deezer.com/<kind>/<id>``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        raise EngineError("bad_options", "not an Apple Music or Deezer link") from None
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme == "https" and not parts.query and not parts.fragment and parts.port is None:
        if parts.hostname == "music.apple.com" and len(segments) == 3:
            country, kind, catalog_id = segments
            kind = {"song": "track", "album": "album"}.get(kind, "")
            if COUNTRY.fullmatch(country) and kind and CATALOG_ID.fullmatch(catalog_id):
                return "apple", kind, catalog_id, country
        if parts.hostname == "www.deezer.com" and len(segments) == 2:
            kind, catalog_id = segments
            if kind in KINDS["deezer"] and CATALOG_ID.fullmatch(catalog_id):
                return "deezer", kind, catalog_id, "us"
    raise EngineError("bad_options", "not an Apple Music or Deezer song, album or playlist link")


def canonical_url(service: str, kind: str, catalog_id: str, country: str = "us") -> str:
    if service == "apple":
        path = "song" if kind == "track" else kind
        return f"https://music.apple.com/{country}/{path}/{catalog_id}"
    return f"https://www.deezer.com/{kind}/{catalog_id}"


def apple_image(url: Any, size: int = ROW_ART) -> str | None:
    """An Apple artwork URL resized to ``size`` px, or None for anything that is not one."""
    parts = _image_parts(url)
    if parts is None or not _APPLE_IMAGE_HOST.fullmatch(parts.hostname or ""):
        return None
    found = _APPLE_IMAGE_PATH.fullmatch(parts.path)
    return f"https://{parts.hostname}{found.group(1)}/{size}x{size}bb.jpg" if found else None


def deezer_image(url: Any, size: int = ROW_ART) -> str | None:
    """A Deezer picture URL on cdn-images.dzcdn.net at ``size`` px, or None."""
    parts = _image_parts(url)
    if parts is None or not _DEEZER_IMAGE_HOST.fullmatch(parts.hostname or ""):
        return None
    found = _DEEZER_IMAGE_PATH.fullmatch(parts.path)
    if not found:
        return None
    kind, md5 = found.groups()
    return f"https://cdn-images.dzcdn.net/images/{kind}/{md5}/{size}x{size}-000000-80-0-0.jpg"


def _image_parts(url: Any) -> Any:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.query:
        return None
    return parts


def cover_check(url: Any) -> str | None:
    """``url`` when it is already a normalized Apple or Deezer picture: what fetch_cover accepts."""
    if not isinstance(url, str):
        return None
    size = re.search(r"/(\d{2,4})x\d{2,4}", url)
    if size is None:
        return None
    px = int(size.group(1))
    for normalize in (apple_image, deezer_image):
        if normalize(url, px) == url:
            return url
    return None


def parse_song(opts: dict[str, Any]) -> dict[str, Any]:
    """The song details core sent with a match or download job, checked like any payload."""
    song = opts.get("song")
    if not isinstance(song, dict) or set(song) - {
        "title", "artists", "explicit", "duration", "date", "art"
    }:  # fmt: skip
        raise EngineError("bad_options", "'song' must hold the listed song's details")
    title = song.get("title")
    artists = song.get("artists")
    if not isinstance(title, str) or not title or len(title) > MAX_TEXT:
        raise EngineError("bad_options", "'song.title' must be a short string")
    if (
        not isinstance(artists, list)
        or len(artists) > MAX_ARTISTS
        or any(not isinstance(a, str) or len(a) > MAX_TEXT for a in artists)
    ):
        raise EngineError("bad_options", "'song.artists' must be a list of names")
    duration = song.get("duration")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not 0 < duration <= 24 * 3600
    ):
        raise EngineError("bad_options", "'song.duration' must be a length in seconds")
    if not isinstance(song.get("explicit", False), bool):
        raise EngineError("bad_options", "'song.explicit' must be true or false")
    date = song.get("date")
    if date is not None and (not isinstance(date, str) or not _DATE.fullmatch(date)):
        raise EngineError("bad_options", "'song.date' must be YYYY or YYYY-MM-DD")
    art = song.get("art")
    if art is not None and cover_check(art) != art:
        raise EngineError("bad_options", "'song.art' must be an Apple Music or Deezer picture")
    return song


def parse_match_options(opts: dict[str, Any]) -> dict[str, Any]:
    if set(opts) - {"mode", "album", "song"}:
        raise EngineError("bad_options", "match takes only an album and the song")
    spotdl._album_options(opts)
    return parse_song(opts)


def parse_download_options(opts: dict[str, Any]) -> tuple[str | None, bool, dict[str, Any]]:
    """(video_id or None, archive, song). Mirrors core.spotify.download_options exactly."""
    rest = {k: v for k, v in opts.items() if k != "song"}
    video_id, archive = spotdl.parse_download_options(rest)
    return video_id, archive, parse_song(opts)


def song_fields(
    service: str, track_id: str, country: str, song: dict[str, Any], opts: dict[str, Any]
) -> dict[str, Any]:
    """What one song is matched and tagged with: the listing's details plus the known album."""
    artists = [spotdl.safe_text(a) for a in song.get("artists") or []]
    artists = [a for a in artists if a] or ["Unknown artist"]
    date = song.get("date")
    art = song.get("art")
    big = (apple_image if service == "apple" else deezer_image)(art, TAG_ART[service])
    return {
        "name": spotdl.safe_text(song["title"]) or "Untitled",
        "artists": artists,
        "artist": artists[0],
        "album_name": spotdl.safe_text(opts.get("album")),
        "album_artist": artists[0],
        "track_number": opts.get("album_track"),
        "duration": float(song.get("duration") or 0),
        "explicit": song.get("explicit") is True,
        "year": date[:4] if date else None,
        "date": date if date and len(date) == 10 else None,
        "url": canonical_url(service, "track", track_id, country),
        "cover_url": big,
        "track_id": track_id,
    }


# ── lookups ───────────────────────────────────────────────────────────────────────────────
def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """One bounded JSON request to a fixed public API host."""
    import requests

    with requests.get(
        url, params=params, headers=HEADERS, stream=True, timeout=LOOKUP_TIMEOUT
    ) as resp:
        if resp.status_code == 404:
            raise EngineError("download_error", "http error 404: that was not found")
        if resp.status_code == 403 or resp.status_code == 429:
            raise EngineError("download_error", f"http error 429: {resp.status_code}, try later")
        if resp.status_code != 200:
            raise EngineError("download_error", f"http error {resp.status_code}")
        body = resp.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
    if len(body) > MAX_JSON_BYTES:
        raise EngineError("download_error", "the answer was too large")
    try:
        import json

        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise EngineError("download_error", "the answer was not readable") from None


def _seconds(value: Any, scale: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return round(value / scale, 1)


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    head = value[:10]
    return head if _DATE.fullmatch(head) else None


def apple_row(raw: Any) -> dict[str, Any] | None:
    """The listing row for one Lookup ``track`` result, or None for a video or a bad row."""
    if not isinstance(raw, dict) or raw.get("wrapperType") != "track" or raw.get("kind") != "song":
        return None
    track_id = raw.get("trackId")
    if isinstance(track_id, bool) or not isinstance(track_id, int):
        return None
    track_id = str(track_id)
    if not CATALOG_ID.fullmatch(track_id):
        return None
    row: dict[str, Any] = {
        "id": track_id,
        "title": spotdl.safe_text(raw.get("trackName")) or "Untitled",
        "artists": [a for a in [spotdl.safe_text(raw.get("artistName"))] if a],
        "album": spotdl.safe_text(raw.get("collectionName")),
        "duration": _seconds(raw.get("trackTimeMillis"), 1000),
        "explicit": raw.get("trackExplicitness") == "explicit",
    }
    date = _date(raw.get("releaseDate"))
    if date:
        row["date"] = date
    art = apple_image(raw.get("artworkUrl100"))
    if art:
        row["art"] = art
    return row


def _order(raw: dict[str, Any]) -> tuple[int, int]:
    disc, number = raw.get("discNumber"), raw.get("trackNumber")
    return (
        disc if isinstance(disc, int) and not isinstance(disc, bool) else 1,
        number if isinstance(number, int) and not isinstance(number, bool) else 0,
    )


def apple_listing(kind: str, catalog_id: str, country: str) -> dict[str, Any]:
    """Title, owner, cover and rows for an Apple Music song or album."""
    data = get_json(
        "https://itunes.apple.com/lookup",
        {"id": catalog_id, "entity": "song", "country": country, "limit": 200},
    )
    results = data.get("results") if isinstance(data, dict) else None
    results = [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
    if kind == "track":
        tracks = [r for r in results if str(r.get("trackId")) == catalog_id]
        row = apple_row(tracks[0]) if tracks else None
        if row is None:
            raise EngineError("download_error", "http error 404: that song was not found")
        return {
            "title": row["title"],
            "owner": ", ".join(row["artists"]),
            "cover": row.get("art"),
            "rows": [row],
            "skipped": 0,
        }
    albums = [
        r for r in results
        if r.get("wrapperType") == "collection" and str(r.get("collectionId")) == catalog_id
    ]  # fmt: skip
    if not albums:
        raise EngineError("download_error", "http error 404: that album was not found")
    album = albums[0]
    title = spotdl.safe_text(album.get("collectionName")) or "Album"
    songs = sorted(
        (r for r in results if r.get("wrapperType") == "track"),
        key=_order,
    )
    rows, skipped = [], 0
    for raw in songs:
        row = apple_row(raw)
        if row is None:
            skipped += 1
            continue
        row["album"] = title  # an album's rows are the album's own songs (plan §7 item 1)
        rows.append(row)
    return {
        "title": title,
        "owner": spotdl.safe_text(album.get("artistName")),
        "cover": apple_image(album.get("artworkUrl100")),
        "rows": rows,
        "skipped": skipped,
    }


def _deezer(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = get_json(f"https://api.deezer.com/{path}", params)
    if not isinstance(data, dict):
        raise EngineError("download_error", "Deezer's answer was not readable")
    error = data.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        if code == 4:  # "Quota limit exceeded"
            raise EngineError("download_error", "http error 429: Deezer is busy, try again soon")
        raise EngineError("download_error", "http error 404: Deezer has nothing at that link")
    return data


def deezer_row(raw: Any, album: str = "", date: str | None = None) -> dict[str, Any] | None:
    """The listing row for one Deezer track object, or None for a bad row."""
    if not isinstance(raw, dict) or raw.get("type", "track") != "track":
        return None
    track_id = raw.get("id")
    if isinstance(track_id, bool) or not isinstance(track_id, int):
        return None
    track_id = str(track_id)
    if not CATALOG_ID.fullmatch(track_id):
        return None
    contributors = raw.get("contributors")
    if isinstance(contributors, list) and contributors:
        artists = spotdl._artists(contributors)
    else:
        artist = raw.get("artist")
        artists = spotdl._artists([artist] if isinstance(artist, dict) else [])
    album_raw = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    row: dict[str, Any] = {
        "id": track_id,
        "title": spotdl.safe_text(raw.get("title")) or "Untitled",
        "artists": artists,
        "album": album or spotdl.safe_text(album_raw.get("title")),
        "duration": _seconds(raw.get("duration")),
        "explicit": raw.get("explicit_lyrics") is True,
    }
    date = date or _date(raw.get("release_date")) or _date(album_raw.get("release_date"))
    if date:
        row["date"] = date
    art = deezer_image(album_raw.get("cover_medium"))
    if art:
        row["art"] = art
    return row


def _deezer_pages(kind: str, catalog_id: str, first: dict[str, Any], total: Any) -> list[Any]:
    """Every track of a Deezer album or playlist: the embedded page, then the rest by index."""
    embedded = first.get("data") if isinstance(first.get("data"), list) else []
    items = list(embedded)
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(items)
    want = min(total, MAX_TRACKS + 1)
    while len(items) < want:
        page = _deezer(
            f"{kind}/{catalog_id}/tracks", {"index": len(items), "limit": DEEZER_PAGE}
        ).get("data")
        if not isinstance(page, list) or not page:
            break
        items.extend(page)
    return items


def deezer_listing(kind: str, catalog_id: str) -> dict[str, Any]:
    """Title, owner, cover and rows for a Deezer track, album or playlist."""
    data = _deezer(f"{kind}/{catalog_id}")
    if kind == "track":
        row = deezer_row(data)
        if row is None or row["id"] != catalog_id:
            raise EngineError("download_error", "http error 404: that song was not found")
        return {
            "title": row["title"],
            "owner": ", ".join(row["artists"]),
            "cover": row.get("art"),
            "rows": [row],
            "skipped": 0,
        }
    tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
    items = _deezer_pages(kind, catalog_id, tracks, data.get("nb_tracks"))
    title = spotdl.safe_text(data.get("title")) or kind.capitalize()
    if kind == "album":
        album, date = title, _date(data.get("release_date"))
        artist = data.get("artist")
        owner = spotdl.safe_text(artist.get("name")) if isinstance(artist, dict) else ""
        cover = deezer_image(data.get("cover_medium"))
    else:
        album, date = "", None
        creator = data.get("creator") if isinstance(data.get("creator"), dict) else {}
        owner = spotdl.safe_text(creator.get("name"))
        cover = deezer_image(data.get("picture_medium"))
    rows, skipped, seen = [], 0, set()
    for raw in items:
        row = deezer_row(raw, album, date)
        if row is None or row["id"] in seen:
            skipped += 1
            continue
        if kind == "album" and not row.get("art") and cover:
            row["art"] = cover
        seen.add(row["id"])
        rows.append(row)
    return {"title": title, "owner": owner, "cover": cover, "rows": rows, "skipped": skipped}


# ── engine ────────────────────────────────────────────────────────────────────────────────
class MusicEngine:
    name = "music"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        service, kind, catalog_id, country = parse_url(job.url)
        opts = dict(job.options)
        mode = opts.get("mode", "analyze")
        if mode == "analyze":
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", "analyze takes no options")
        elif mode == "match":
            song = parse_match_options(opts)
        elif mode == "download":
            video_id, archive, song = parse_download_options(opts)
        else:
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        if mode != "analyze" and kind != "track":
            raise EngineError("bad_options", f"{mode} works on one song")
        if kind not in KINDS[service]:
            raise EngineError("bad_options", f"{SITE_NAMES[service]} {kind}s are not supported")

        try:
            if mode == "analyze":
                return self._analyze(service, kind, catalog_id, country, emit)
            fields = song_fields(service, catalog_id, country, song, opts)
            if mode == "match":
                return self._match(fields, emit)
            folder = Path(job.output_dir)
            if not folder.is_dir():
                raise EngineError("download_error", "the download folder does not exist")
            ledger = spotdl.Archive(folder, service)
            if archive and catalog_id in ledger:
                emit("stage", {"stage": "completed"})
                return {
                    "title": f"{SITE_NAMES[service]} song",
                    "preset": spotdl.PRESET_ID,
                    "files": [],
                    "total_bytes": 0,
                    "skipped": True,
                    "skipped_reason": "Already downloaded",
                }
            emit("stage", {"stage": "analyzing"})
            return spotdl.download_matched(
                job, folder, fields, video_id, emit, ledger if archive else None, cover_check
            )
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(*spotdl.describe_error(exc)) from None

    def _analyze(
        self, service: str, kind: str, catalog_id: str, country: str, emit: Emit
    ) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        if service == "apple":
            listing = apple_listing(kind, catalog_id, country)
        else:
            listing = deezer_listing(kind, catalog_id)
        rows = listing["rows"]
        emit("stage", {"stage": "completed"})
        fields: dict[str, Any] = {}
        if listing.get("cover"):
            fields["cover"] = listing["cover"]
        listed = rows[:MAX_TRACKS]
        # Listed like a Spotify link: even one song is a playlist of one (see spotdl._analyze).
        return media_result(
            "playlist",
            ["tracks"],
            listing["title"],
            canonical_url(service, kind, catalog_id, country),
            entries=listed,
            site=SITE_NAMES[service],
            service=service,
            catalog_kind=kind,
            catalog_id=catalog_id,
            country=country,
            owner=listing["owner"],
            tracks=listed,
            skipped=listing["skipped"],
            truncated=len(rows) > MAX_TRACKS,
            **fields,
        )

    @staticmethod
    def _match(fields: dict[str, Any], emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        candidates: list[dict[str, Any]] = []
        found = spotdl.find_match(fields, emit, candidates)
        if found is None:
            raise EngineError("no_match", "No matching song was found on YouTube Music.")
        emit("stage", {"stage": "completed"})
        return {
            "kind": "catalog_match",
            "track_id": fields["track_id"],
            "video_id": found["video_id"],
            "title": found["title"],
            "channel": found["channel"],
            "duration": found["duration"],
            "confidence": found["score"],
            "method": found["method"],
            "album": found["album"],
            "candidates": [c for c in candidates if c["video_id"] != found["video_id"]],
        }
