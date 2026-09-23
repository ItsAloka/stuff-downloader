"""spotDL engine (plan §6.3, §M5): Spotify metadata, a YouTube Music match, a tagged MP3.

Runs only in the isolated ``envs\\spotdl`` runtime, whose own yt-dlp pin never meets ours. spotDL
is used as a library for exactly three things -- reading Spotify's metadata, listing a
track/album/playlist, and picking a YouTube Music match -- and never for its sync or delete
behaviour. The audio itself is downloaded by this project's own yt-dlp engine code (the MP3
preset), and the file is then re-tagged with Spotify's metadata and cover through mutagen.

Facts this module is built around, checked live against spotDL 4.5.2 in September 2026:

- spotDL's shared client id/secret for the official Web API is over quota: every endpoint answers
  429 with ``Retry-After: 86400``, and spotipy sleeps that out instead of failing. So the client
  is always the default *free* one (``SpotipyFree``, which reads Spotify's web player), and none
  of the options that silently switch spotDL to the official API are ever passed.
- The free client takes ~10 s per call. ``Album/Playlist.from_url(fetch_songs=True)`` re-reads
  every track one at a time and takes minutes, so listings use ``get_metadata`` (one pass).
- The free client never returns an ISRC, and playlist rows carry no cover. The cover is read per
  track at download time.
- ``YouTubeMusic.search`` takes ~30 s per track, so matching is its own per-track job.
- spotDL drops every YouTube Music *song* result: it reads their length as 0, so only *videos*
  (music videos with intros, lyric uploads) are ever scored, and the audio is often the wrong
  length. So the match searches YouTube Music songs first itself (``pick_song``), through the
  ytmusicapi spotDL already ships, and only falls back to spotDL's search when no song fits.

Nothing here prints: stdout is the protocol channel.
"""

from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from stuff_downloader_worker import tagging
from stuff_downloader_worker.engines.base import Emit, EngineError
from stuff_downloader_worker.names import safe_output_name
from stuff_downloader_worker.protocol import JobSpec

PRESET_ID = "spotify_mp3"
MAX_TRACKS = 500
MAX_TEXT = 300
MAX_ARTISTS = 20
MAX_COVER_BYTES = 5_000_000
COVER_TIMEOUT = 20
ARCHIVE_FILENAME = ".stuff-downloader-spotify-archive.txt"
MAX_NAME = 150  # characters of "Artist - Title" before the extension

SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
KINDS = ("track", "album", "playlist")
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "music.youtube.com", "m.youtube.com"})
# Spotify serves covers from these; a cover URL anywhere else is not fetched.
COVER_HOST_SUFFIXES = (".scdn.co", ".spotifycdn.com")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$")

# Song matching (see pick_song). A YouTube Music song within this many seconds of Spotify's
# length is the same recording; official audio is usually within 1-2 s.
SONG_MAX_DIFF = 5
SONG_MIN_SCORE = 70.0
SONG_SEARCH_LIMIT = 10
# A version with one of these in its title is a different recording, unless Spotify's title
# says the same thing.
_VARIANT_WORDS = (
    "sped up", "speed up", "slowed", "reverb", "nightcore", "remix", "live", "acoustic",
    "instrumental", "karaoke", "cover", "8d", "remaster", "extended", "radio edit",
)  # fmt: skip
_FEAT = re.compile(r"[\(\[]\s*(feat|ft|with)\.?\s[^\)\]]*[\)\]]", re.IGNORECASE)
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


def parse_download_options(opts: dict[str, Any]) -> tuple[str | None, bool]:
    """(video_id or None, archive). Mirrors core.spotify.download_options exactly."""
    allowed = {"mode", "preset", "video_id", "archive", "output_name"}
    if set(opts) - allowed or opts.get("preset") != PRESET_ID:
        raise EngineError("bad_options", f"a Spotify download takes preset={PRESET_ID}")
    if "output_name" in opts and not isinstance(opts["output_name"], str):
        raise EngineError("bad_options", "'output_name' must be a string")
    archive = opts.get("archive", True)
    if not isinstance(archive, bool):
        raise EngineError("bad_options", "'archive' must be true or false")
    video_id = opts.get("video_id")
    if video_id is not None and (not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id)):
        raise EngineError("bad_options", "'video_id' must be a YouTube video id")
    return video_id, archive


def video_id_of(url: Any) -> str | None:
    """The id of a YouTube watch URL spotDL returned, or None for anything else."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or (parts.hostname or "").lower() not in YOUTUBE_HOSTS:
        return None
    if parts.path != "/watch":
        return None
    value = (parse_qs(parts.query).get("v") or [""])[0]
    return value if VIDEO_ID.fullmatch(value) else None


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
    """(code, safe message) for a spotDL / Spotify / YouTube Music failure."""
    text = safe_text(f"{exc.__class__.__name__}: {exc}")[:500]
    lowered = text.lower()
    if any(needle in lowered for needle in ("429", "too many requests", "rate limit")):
        return "download_error", f"http error 429: {text}"
    if any(needle in lowered for needle in ("404", "not found")):
        return "download_error", f"http error 404: {text}"
    return "download_error", text or "Spotify could not be read"


# ── metadata ──────────────────────────────────────────────────────────────────────────────
def _artists(raw: Any) -> list[str]:
    names = []
    if isinstance(raw, list):
        for artist in raw[:MAX_ARTISTS]:
            name = artist.get("name") if isinstance(artist, dict) else artist
            clean = safe_text(name)
            if clean:
                names.append(clean)
    return names


def _cover_url(album: dict[str, Any]) -> str | None:
    images = [
        i for i in album.get("images") or []
        if isinstance(i, dict) and safe_cover_url(i.get("url"))
    ]
    if not images:
        return None
    best = max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
    return safe_cover_url(best["url"])


def safe_cover_url(value: Any) -> str | None:
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
        or not host.endswith(COVER_HOST_SUFFIXES)
        or port not in (None, 443)
        or parts.username
        or parts.password
    ):
        return None
    return value


def track_row(track: dict[str, Any]) -> dict[str, Any] | None:
    """The listing row for one raw Spotify track, or None if it is not a playable track."""
    track_id = track.get("id")
    if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
        return None
    duration_ms = track.get("duration_ms")
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    return {
        "id": track_id,
        "title": safe_text(track.get("name")) or "Untitled",
        "artists": _artists(track.get("artists")),
        "album": safe_text(album.get("name")),
        "duration": round(duration_ms / 1000, 1)
        if isinstance(duration_ms, int | float) and not isinstance(duration_ms, bool)
        else None,
        "explicit": track.get("explicit") is True,
        "cover_url": _cover_url(album),
    }


def song_row(song: Any) -> dict[str, Any] | None:
    """The listing row for a spotDL Song built by an album/playlist ``get_metadata``."""
    track_id = getattr(song, "song_id", None)
    if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
        return None
    duration = getattr(song, "duration", None)
    return {
        "id": track_id,
        "title": safe_text(getattr(song, "name", "")) or "Untitled",
        "artists": _artists(list(getattr(song, "artists", None) or [])),
        "album": safe_text(getattr(song, "album_name", "")),
        "duration": float(duration)
        if isinstance(duration, int | float) and not isinstance(duration, bool)
        else None,
        "explicit": getattr(song, "explicit", False) is True,
        "cover_url": safe_cover_url(getattr(song, "cover_url", None)),
    }


def song_fields(track: dict[str, Any]) -> dict[str, Any]:
    """spotDL Song fields from one raw track, the same mapping spotDL's playlist code uses."""
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    artists = _artists(track.get("artists")) or ["Unknown artist"]
    album_artists = _artists(album.get("artists"))
    release = album.get("release_date") if isinstance(album.get("release_date"), str) else None
    isrc = (track.get("external_ids") or {}).get("isrc") or None
    return {
        "name": safe_text(track.get("name")) or "Untitled",
        "artists": artists,
        "artist": artists[0],
        "album_id": album.get("id"),
        "album_name": safe_text(album.get("name")),
        "album_artist": album_artists[0] if album_artists else artists[0],
        "album_type": album.get("album_type"),
        "disc_number": track.get("disc_number") or 1,
        "duration": int((track.get("duration_ms") or 0) / 1000),
        "year": release[:4] if release else None,
        "date": release,
        "track_number": track.get("track_number") or 1,
        "tracks_count": album.get("total_tracks"),
        "song_id": track["id"],
        "explicit": track.get("explicit") is True,
        "url": f"https://open.spotify.com/track/{track['id']}",
        "isrc": isrc if isinstance(isrc, str) else None,
        "cover_url": _cover_url(album),
    }


# ── matching ──────────────────────────────────────────────────────────────────────────
def _norm(text: Any) -> str:
    """Lower-case words only, without a "(feat. X)" part, for comparing titles and names."""
    if not isinstance(text, str):
        return ""
    return " ".join(_NON_WORD.sub(" ", _FEAT.sub(" ", text).casefold()).split())


def _variants(title: str) -> set[str]:
    lowered = f" {_norm(title)} "
    return {w for w in _VARIANT_WORDS if f" {w} " in lowered}


def pick_song(results: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The YouTube Music *song* that is Spotify's recording, or None if none clearly is.

    ``results`` is ytmusicapi's ``search(filter="songs")`` output. A result must name one of
    Spotify's artists, carry the same title (ignoring "feat." parts) with no extra "sped up",
    "live", "remix"… marker, and run within SONG_MAX_DIFF seconds of Spotify's length. Among
    those, the closest length wins, then the same explicit/clean version as Spotify's, then the
    same album. Returns {video_id, title, channel, duration, score}.
    """
    if not isinstance(results, list):
        return None
    want_title = _norm(fields.get("name"))
    want_artists = {_norm(a) for a in fields.get("artists") or []} - {""}
    want_album = _norm(fields.get("album_name"))
    want_variants = _variants(str(fields.get("name") or ""))
    want_length = fields.get("duration") or 0
    best: tuple[float, dict[str, Any]] | None = None
    for raw in results[:50]:
        if not isinstance(raw, dict) or raw.get("resultType") not in (None, "song"):
            continue
        video_id = raw.get("videoId")
        length = raw.get("duration_seconds")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            continue
        if isinstance(length, bool) or not isinstance(length, int | float) or length <= 0:
            continue
        artists = [a.get("name") for a in raw.get("artists") or [] if isinstance(a, dict)]
        if not want_artists & {_norm(a) for a in artists}:
            continue
        title = raw.get("title")
        if _variants(str(title or "")) != want_variants:
            continue
        similarity = SequenceMatcher(None, want_title, _norm(title)).ratio()
        diff = abs(float(length) - float(want_length))
        if similarity < 0.8 or (want_length and diff > SONG_MAX_DIFF):
            continue
        album = (raw.get("album") or {}).get("name") if isinstance(raw.get("album"), dict) else None
        score = 60 * similarity + 30 * (1 - diff / (SONG_MAX_DIFF + 1))
        score += 5 if bool(raw.get("isExplicit")) == bool(fields.get("explicit")) else 0
        score += 5 if want_album and _norm(album) == want_album else 0
        if score >= SONG_MIN_SCORE and (best is None or score > best[0]):
            best = (
                score,
                {
                    "video_id": video_id,
                    "title": safe_text(title),
                    "channel": ", ".join(safe_text(a) for a in artists if a),
                    "duration": float(length),
                    "score": round(min(score, 100.0), 1),
                },
            )
    return best[1] if best else None


MAX_CANDIDATES = 8


def candidate_rows(results: Any, fields: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Up to MAX_CANDIDATES songs, with plausible recordings before other versions."""
    rows: list[dict[str, Any]] = []
    for raw in results[:50] if isinstance(results, list) else []:
        if not isinstance(raw, dict) or raw.get("resultType") not in (None, "song"):
            continue
        video_id = raw.get("videoId")
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            continue
        if any(row["video_id"] == video_id for row in rows):
            continue
        length = raw.get("duration_seconds")
        artists = [a.get("name") for a in raw.get("artists") or [] if isinstance(a, dict)]
        rows.append(
            {
                "video_id": video_id,
                "title": safe_text(raw.get("title")),
                "channel": ", ".join(safe_text(a) for a in artists if a),
                "duration": float(length)
                if isinstance(length, int | float) and not isinstance(length, bool) and length > 0
                else None,
            }
        )
        if fields is not None:
            picked = pick_song([raw], fields)
            rows[-1]["_rank"] = picked["score"] if picked is not None else -1.0
    if fields is not None:
        rows.sort(key=lambda row: row.pop("_rank"), reverse=True)
    return rows[:MAX_CANDIDATES]


def search_songs(fields: dict[str, Any]) -> list[Any]:
    """YouTube Music song results for this track (ytmusicapi, as shipped with spotDL)."""
    from ytmusicapi import YTMusic

    query = f"{', '.join(fields.get('artists') or [])} {fields.get('name') or ''}".strip()
    return YTMusic().search(query, filter="songs", limit=SONG_SEARCH_LIMIT)


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
    """Spotify track ids already downloaded into one folder, one per line."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / ARCHIVE_FILENAME

    def __contains__(self, track_id: str) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fp:
                return any(line.strip() == f"spotify {track_id}" for line in fp)
        except FileNotFoundError:
            return False

    def add(self, track_id: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write(f"spotify {track_id}\n")


def fetch_cover(url: str | None) -> bytes | None:
    """Spotify's cover as JPEG bytes, or None. Only Spotify's own https image hosts."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(COVER_HOST_SUFFIXES):
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
    """Replace the file's tags with Spotify's metadata and cover. Returns what was written."""
    from mutagen.id3 import (
        APIC,
        ID3,
        TALB,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TSRC,
        WOAS,
        ID3NoHeaderError,
    )

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    track_no = fields.get("track_number")
    total = fields.get("tracks_count")
    frames = {
        "TIT2": TIT2(encoding=3, text=fields["name"]),
        # One string: ID3v2.3 has no multi-value frames, and "A, B" reads well everywhere.
        "TPE1": TPE1(encoding=3, text=", ".join(fields["artists"])),
        "TPE2": TPE2(encoding=3, text=fields.get("album_artist") or fields["artist"]),
        "TALB": TALB(encoding=3, text=fields.get("album_name") or ""),
        "TRCK": TRCK(encoding=3, text=f"{track_no}/{total}" if total else str(track_no)),
        "TPOS": TPOS(encoding=3, text=str(fields.get("disc_number") or 1)),
        "WOAS": WOAS(url=fields["url"]),
    }
    if fields.get("date") or fields.get("year"):
        frames["TDRC"] = TDRC(encoding=3, text=str(fields.get("date") or fields.get("year")))
    if fields.get("isrc"):
        frames["TSRC"] = TSRC(encoding=3, text=fields["isrc"])
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
        "album": fields.get("album_name") or "",
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
        if mode in ("analyze", "match"):
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", f"{mode} takes no options")
        elif mode == "download":
            video_id, archive = parse_download_options(opts)
        else:
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        if mode != "analyze" and kind != "track":
            raise EngineError("bad_options", f"{mode} works on one track")

        client = self._client()
        try:
            if mode == "analyze":
                return self._analyze(client, kind, spotify_id, emit)
            if mode == "match":
                return self._match(client, spotify_id, emit)
            return self._download(client, job, spotify_id, video_id, archive, emit)
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(*describe_error(exc)) from None

    @staticmethod
    def _client() -> Any:
        try:
            from spotdl.utils.config import DEFAULT_CONFIG
            from spotdl.utils.spotify import SpotifyClient
        except ImportError as exc:
            raise EngineError("engine_missing", f"spotDL is not installed here: {exc}") from exc
        if SpotifyClient._instance is None:
            # The free client, always: see the module docstring. None of user_auth, auth_token or
            # use_cache_file is passed, because each silently switches spotDL to the official
            # API, whose shared credentials are over quota. no_cache keeps nothing on disk.
            SpotifyClient.init(
                client_id=DEFAULT_CONFIG["client_id"],
                client_secret=DEFAULT_CONFIG["client_secret"],
                no_cache=True,
                use_official_api=False,
            )
        return SpotifyClient()

    # ── analyze ───────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _analyze(client: Any, kind: str, spotify_id: str, emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        url = f"https://open.spotify.com/{kind}/{spotify_id}"
        if kind == "track":
            raw = client.track(spotify_id)
            row = track_row(raw) if isinstance(raw, dict) else None
            if row is None:
                raise EngineError("download_error", "http error 404: that track was not found")
            rows, title, owner, skipped = [row], row["title"], ", ".join(row["artists"]), 0
        else:
            from spotdl.types.album import Album
            from spotdl.types.playlist import Playlist

            metadata, songs = (Album if kind == "album" else Playlist).get_metadata(url)
            rows = []
            skipped = 0
            for song in songs:
                row = song_row(song)
                if row is None:
                    skipped += 1
                else:
                    rows.append(row)
            title = safe_text(metadata.get("name")) or kind.capitalize()
            # An album names its first artist as a dict; a playlist names its owner as text.
            owner = ", ".join(_artists([metadata.get("artist")])) or safe_text(
                metadata.get("author_name")
            )
        truncated = len(rows) > MAX_TRACKS
        emit("stage", {"stage": "completed"})
        return {
            "kind": "spotify",
            "spotify_kind": kind,
            "spotify_id": spotify_id,
            "title": title,
            "owner": owner,
            "tracks": rows[:MAX_TRACKS],
            "skipped": skipped,
            "truncated": truncated,
        }

    # ── match ─────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _song(client: Any, track_id: str) -> tuple[Any, dict[str, Any]]:
        from spotdl.types.song import Song

        raw = client.track(track_id)
        if not isinstance(raw, dict) or raw.get("id") != track_id:
            raise EngineError("download_error", "http error 404: that track was not found")
        fields = song_fields(raw)
        return Song.from_missing_data(**fields), fields

    @staticmethod
    def _search(
        song: Any, fields: dict[str, Any], emit: Emit, candidates: list | None = None
    ) -> dict[str, Any] | None:
        """The match: a YouTube Music song first, spotDL's own pick only when no song fits.

        Returns {video_id, title, channel, duration, score}, or None when nothing was found.
        The song search's results are also appended to ``candidates`` when one is given.
        """
        try:
            results = search_songs(fields)
            if candidates is not None:
                candidates.extend(candidate_rows(results, fields))
            picked = pick_song(results, fields)
        except Exception:  # the fallback below still gets its turn
            emit("log", {"level": "warning", "message": "YouTube Music song search failed"})
            picked = None
        if picked is not None:
            return picked
        video_id, result, score = SpotDlEngine._spotdl_search(song)
        if video_id is None:
            return None
        duration = getattr(result, "duration", None) if result is not None else None
        title = safe_text(getattr(result, "name", "")) if result is not None else ""
        if title and _variants(title) != _variants(str(fields.get("name") or "")):
            return None
        return {
            "video_id": video_id,
            "title": title,
            "channel": safe_text(getattr(result, "author", "")) if result is not None else "",
            "duration": float(duration)
            if isinstance(duration, int | float) and not isinstance(duration, bool) and duration
            else None,
            "score": round(score, 1) if score is not None else None,
        }

    @staticmethod
    def _spotdl_search(song: Any) -> tuple[str | None, Any, float | None]:
        """(video id, spotDL result, score) of spotDL's pick, or (None, None, None)."""
        from spotdl.providers.audio import YouTubeMusic

        class Scored(YouTubeMusic):
            """spotDL's own YouTube Music matcher, remembering the score of its pick."""

            picked: tuple[Any, float] | None = None

            def get_best_result(self, results):  # type: ignore[no-untyped-def]
                best = super().get_best_result(results)
                self.picked = best
                return best

        provider = Scored()
        url = provider.search(song)
        video_id = video_id_of(url)
        if video_id is None:
            return None, None, None
        picked = provider.picked
        if picked is not None and video_id_of(getattr(picked[0], "url", None)) == video_id:
            return video_id, picked[0], float(picked[1])
        return video_id, None, None  # an ISRC hit, which spotDL returns without a score

    def _match(self, client: Any, track_id: str, emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        song, fields = self._song(client, track_id)
        candidates: list[dict[str, Any]] = []
        found = self._search(song, fields, emit, candidates)
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
            "spotify_duration": fields["duration"],
            # The other results, so a wrong pick can be swapped without a pasted link.
            "candidates": [c for c in candidates if c["video_id"] != found["video_id"]],
        }

    # ── download ──────────────────────────────────────────────────────────────────────────
    def _download(
        self,
        client: Any,
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
        song, fields = self._song(client, track_id)
        manual = video_id is not None
        if video_id is None:
            found = self._search(song, fields, emit)
            if found is None:
                raise EngineError("no_match", "No matching song was found on YouTube Music.")
            video_id = found["video_id"]

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
        cover = fetch_cover(fields.get("cover_url"))
        if cover is None:
            emit("log", {"level": "warning", "message": "the Spotify cover could not be loaded"})
        tags = tag_mp3(mp3s[0], fields, cover)
        stem = safe_output_name(job.options.get("output_name")) or file_stem(
            fields["artists"], fields["name"]
        )
        final = move_no_overwrite(mp3s[0], folder, stem, ".mp3")
        if archive:
            ledger.add(track_id)
        emit("stage", {"stage": "completed"})
        return {
            "title": final.stem,
            "preset": PRESET_ID,
            "files": [str(final)],
            "total_bytes": final.stat().st_size,
            "tags": tags,
            "match": {"video_id": video_id, "manual": manual},
        }
