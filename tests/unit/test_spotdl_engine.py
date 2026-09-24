"""Spotify engine (plan §7) against stand-ins. No network, no Spotify, no YouTube.

The embed page, ytmusicapi, the yt-dlp download, the cover fetch and mutagen are replaced at the
engine's own seams (``fetch_embed``, ``ytmusic``, ``YtDlpEngine.download``, ``fetch_cover``,
``tag_mp3``), and a stand-in spotdl package covers the one place spotDL is still used: the
listing past the embed page's 100-row cap. So these tests pin the engine's rules: what reaches
core, how a match is found and labelled, what the file is named and tagged with, and what the
archive does. Tagging itself is checked against the real mutagen in the spotdl runtime at the
bottom of this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from stuff_downloader.core import errors, spotify
from stuff_downloader.core.runner import default_worker_command
from stuff_downloader_worker.engines import get_engine
from stuff_downloader_worker.engines import spotdl as engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec

T1, T2 = "6OmhkSOpvYBokMKQxpIGx2", "2iblMMIgSznA464mNov7A8"
ALBUM = "4aawyAB9vmqN3uQ7FjRGTy"
PLAYLIST = "37i9dQZF1DXcBWIGoYBM5M"
VID = "q7xBoh0emqo"
TRACK_URL = f"https://open.spotify.com/track/{T1}"
ALBUM_URL = f"https://open.spotify.com/album/{ALBUM}"
PLAYLIST_URL = f"https://open.spotify.com/playlist/{PLAYLIST}"
H300 = "ab67616d00001e02" + "a" * 24
H640 = "ab67616d0000b273" + "a" * 24
H64 = "ab67616d00004851" + "a" * 24
COVER = "ab67706f00000002" + "b" * 24

# Resolved at import time, before conftest redirects LOCALAPPDATA (see tests/network/netjob.py).
SPOTDL_PYTHON = default_worker_command("spotdl")[0]


def visual(*hashes_and_widths, host="image-cdn-ak.spotifycdn.com"):
    return {
        "image": [
            {"url": f"https://{host}/image/{h}", "maxHeight": w, "maxWidth": w}
            for h, w in hashes_and_widths
        ]
    }


def track_entity(track_id=T1, **extra):
    """A track's embed entity, shaped as seen live in September 2026."""
    return {
        "type": "track",
        "id": track_id,
        "name": "Global Warming (feat. Sensato)",
        "title": "Global Warming (feat. Sensato)",
        "artists": [{"name": "Pitbull", "uri": "x"}, {"name": "Sensato", "uri": "y"}],
        "releaseDate": {"isoString": "2012-11-16T00:00:00Z"},
        "duration": 85000,
        "isExplicit": True,
        "visualIdentity": visual((H300, 300), (H64, 64), (H640, 640)),
        **extra,
    }


def list_item(track_id, title="One", subtitle="Pitbull", ms=85000, **extra):
    return {
        "uri": f"spotify:track:{track_id}",
        "title": title,
        "subtitle": subtitle,
        "duration": ms,
        "isExplicit": False,
        "entityType": "track",
        **extra,
    }


def album_entity(items):
    return {
        "type": "album",
        "id": ALBUM,
        "name": "Global Warming",
        "subtitle": "Pitbull",
        "visualIdentity": visual((H300, 300), (H640, 640)),
        "trackList": items,
    }


def playlist_entity(items):
    return {
        "type": "playlist",
        "id": PLAYLIST,
        "name": "Today's Top Hits",
        "authors": [{"name": "Spotify"}],
        "coverArt": {"sources": [{"url": f"https://i.scdn.co/image/{COVER}"}]},
        "trackList": items,
    }


def ytm_song(video_id, title, artists, seconds, explicit=False, album="Global Warming"):
    """One ytmusicapi search(filter="songs") row, shaped as seen live in September 2026."""
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a, "id": "x"} for a in artists],
        "album": {"name": album, "id": "y"},
        "duration": f"{seconds // 60}:{seconds % 60:02d}",
        "duration_seconds": seconds,
        "isExplicit": explicit,
    }


def ytm_video(video_id, title, artists, seconds):
    return {
        "resultType": "video",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a, "id": "x"} for a in artists],
        "duration_seconds": seconds,
    }


def ytm_album(browse_id, title, artists):
    return {"resultType": "album", "browseId": browse_id, "title": title,
            "artists": [{"name": a, "id": "x"} for a in artists]}  # fmt: skip


def album_track(video_id, title, artists, seconds):
    """One ``get_album`` track: its album is a plain string there."""
    return {
        "videoId": video_id,
        "title": title,
        "album": "Global Warming",
        "artists": [{"name": a, "id": "x"} for a in artists],
        "duration_seconds": seconds,
    }


class FakeSong:
    def __init__(self, **fields):
        self.__dict__.update(fields)


@pytest.fixture
def fake(monkeypatch):
    state = {
        "embeds": {("track", T1): track_entity(), ("track", T2): track_entity(T2)},
        "embed_error": None,
        "embed_calls": [],
        "songs": [],
        "videos": [],
        "albums": [],
        "album_pages": {},
        "queries": [],
        "init": [],
        "rest": [],
        "ytdlp_calls": [],
        "cover": b"\xff\xd8cover",
        "covers": [],
        "tagged": [],
    }

    def fetch_embed(kind, spotify_id):
        state["embed_calls"].append((kind, spotify_id))
        if state["embed_error"]:
            raise state["embed_error"]
        entity = state["embeds"].get((kind, spotify_id))
        if entity is None:
            raise EngineError("download_error", f"http error 404: that {kind} was not found")
        return entity

    class YTMusic:
        def search(self, query, filter=None, limit=None):
            state["queries"].append((query, filter))
            found = state[filter]
            if isinstance(found, Exception):
                raise found
            return found

        def get_album(self, browse_id):
            state["queries"].append((browse_id, "get_album"))
            return state["album_pages"].get(browse_id, {})

    monkeypatch.setattr(engine, "fetch_embed", fetch_embed)
    monkeypatch.setattr(engine, "ytmusic", YTMusic)

    # The spotdl stand-in: only the >100-row fallback touches it.
    class SpotifyClient:
        _instance = None

        def __new__(cls):
            return cls._instance

        @classmethod
        def init(cls, **kwargs):
            state["init"].append(kwargs)
            cls._instance = object()
            return cls._instance

    def get_metadata(url):
        state["metadata_url"] = url
        if isinstance(state["rest"], Exception):
            raise state["rest"]
        return {}, state["rest"]

    modules = {
        name: types.ModuleType(name)
        for name in (
            "spotdl", "spotdl.utils", "spotdl.utils.config", "spotdl.utils.spotify",
            "spotdl.types", "spotdl.types.album", "spotdl.types.playlist",
        )
    }  # fmt: skip
    modules["spotdl.utils.config"].DEFAULT_CONFIG = {"client_id": "id", "client_secret": "sec"}
    modules["spotdl.utils.spotify"].SpotifyClient = SpotifyClient
    modules["spotdl.types.album"].Album = types.SimpleNamespace(get_metadata=get_metadata)
    modules["spotdl.types.playlist"].Playlist = types.SimpleNamespace(get_metadata=get_metadata)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    def fake_ytdlp_download(self, job, emit):
        state["ytdlp_calls"].append(job)
        emit("stage", {"stage": "downloading"})
        emit("progress", {"downloaded_bytes": 1, "total_bytes": 2, "percent": 50.0})
        emit("stage", {"stage": "completed"})
        path = Path(job.output_dir) / "PitbullVEVO - Some YouTube title.mp3"
        path.write_bytes(b"ID3fake-mp3")
        return {"files": [str(path)], "preset": "mp3_music"}

    from stuff_downloader_worker.engines import ytdlp

    monkeypatch.setattr(ytdlp.YtDlpEngine, "download", fake_ytdlp_download)

    def fetch_cover(url, allowed=None):
        state["covers"].append(url)
        return state["cover"]

    monkeypatch.setattr(engine, "fetch_cover", fetch_cover)

    def fake_tag(path, fields, cover):
        state["tagged"].append((Path(path).name, fields, cover))
        return {"title": fields["name"], "artist": ", ".join(fields["artists"])}

    monkeypatch.setattr(engine, "tag_mp3", fake_tag)
    return state


def _run(options, url=TRACK_URL, out="."):
    events = []
    result = get_engine("spotdl").download(
        JobSpec("j1", "spotdl", url, str(out), options), lambda k, d: events.append((k, d))
    )
    return result, events


GOOD_SONG = ytm_song("qjgnkysCPm4", "Global Warming (feat. Sensato)", ["Pitbull", "Sensato"], 85)


# ── validation ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        f"http://open.spotify.com/track/{T1}",
        f"https://open.spotify.com/track/{T1}?si=x",
        f"https://evil.example/track/{T1}",
        f"https://open.spotify.com/artist/{T1}",
        f"https://open.spotify.com/track/{T1}/x",
        "https://open.spotify.com/track/short",
    ],
)
def test_only_a_rebuilt_spotify_link_is_accepted(fake, url):
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"}, url=url)
    assert info.value.code == "bad_options"


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "sync"},
        {"mode": "analyze", "extra": 1},
        {"mode": "match", "video_id": VID},
        {"mode": "match", "album": 5},
        {"mode": "match", "album": "x" * 301},
        {"mode": "download", "preset": "mp3_music"},
        {"mode": "download", "preset": "spotify_mp3", "video_id": "bad id"},
        {"mode": "download", "preset": "spotify_mp3", "archive": "yes"},
        {"mode": "download", "preset": "spotify_mp3", "output": "C:/Windows"},
        {"mode": "download", "preset": "spotify_mp3", "album_track": 0},
        {"mode": "download", "preset": "spotify_mp3", "album_track": True},
        {"mode": "download", "preset": "spotify_mp3", "album_track": "1"},
    ],
)
def test_bad_options_are_refused_before_spotify_is_touched(fake, options):
    with pytest.raises(EngineError) as info:
        _run(options)
    assert info.value.code == "bad_options"
    assert fake["embed_calls"] == []


@pytest.mark.parametrize("mode", ["match", "download"])
def test_match_and_download_work_on_one_track_only(fake, mode):
    options = {"mode": mode} if mode == "match" else spotify.download_options()
    with pytest.raises(EngineError, match="one track"):
        _run(options, url=ALBUM_URL)


def test_the_core_options_are_exactly_what_the_worker_accepts(fake, tmp_path):
    fake["songs"] = [GOOD_SONG]
    for options in (
        spotify.analyze_options(),
        spotify.match_options(),
        spotify.match_options("Global Warming"),
        spotify.download_options(VID, archive=False),
        spotify.download_options(VID, archive=False, album="Global Warming", album_track=3),
        spotify.download_options(archive=False),
    ):
        _run(options, out=tmp_path)


# ── analyze ───────────────────────────────────────────────────────────────────────────────
def test_analyzing_a_track_lists_it_with_its_own_art_and_no_spotdl(fake):
    fake["embeds"][("track", T1)]["name"] = "Title\n with https://evil.example/x link"
    result, events = _run({"mode": "analyze"})
    listing = spotify.parse_listing(result)
    assert (listing.kind, listing.spotify_id) == ("track", T1)
    assert (result["kind"], result["tabs"], result["site"]) == ("playlist", ["tracks"], "Spotify")
    assert result["entries"] == result["tracks"] and len(result["entries"]) == 1
    assert result["webpage"] == f"https://open.spotify.com/track/{T1}"
    (track,) = listing.tracks
    assert track.title == "Title with [link] link"
    assert track.artists == ("Pitbull", "Sensato") and track.duration == 85.0
    # Spotify's own 300 px picture, re-hosted on i.scdn.co by its hash.
    assert track.art == listing.cover == f"https://i.scdn.co/image/{H300}"
    assert events[0] == ("stage", {"stage": "analyzing"})
    assert fake["init"] == []  # no spotDL client for a listing the embed covers


def test_an_album_lists_its_rows_with_the_album_name_and_cover(fake):
    fake["embeds"][("album", ALBUM)] = album_entity(
        [
            list_item(T1, "One", "Pitbull, Sensato", 85000),
            {"uri": "spotify:episode:xyz", "title": "A podcast", "entityType": "episode"},
            list_item("bad"),
            list_item(T2, "Two", "Pitbull", 206000, isExplicit=True),
        ]
    )
    result, _ = _run({"mode": "analyze"}, url=ALBUM_URL)
    listing = spotify.parse_listing(result)
    assert listing.kind == "album" and listing.title == "Global Warming"
    assert listing.owner == "Pitbull" and listing.skipped == 2
    assert [t.track_id for t in listing.tracks] == [T1, T2]
    one, two = listing.tracks
    assert one.artists == ("Pitbull", "Sensato") and one.duration == 85.0
    assert two.explicit and two.duration == 206.0
    assert {t.album for t in listing.tracks} == {"Global Warming"}
    art = f"https://i.scdn.co/image/{H300}"
    assert listing.cover == art and {t.art for t in listing.tracks} == {art}
    assert fake["embed_calls"] == [("album", ALBUM)] and fake["init"] == []


def test_a_playlist_has_its_cover_but_rows_look_up_their_own_art(fake):
    fake["embeds"][("playlist", PLAYLIST)] = playlist_entity([list_item(T1), list_item(T2)])
    result, _ = _run({"mode": "analyze"}, url=PLAYLIST_URL)
    listing = spotify.parse_listing(result)
    assert listing.owner == "Spotify" and listing.cover == f"https://i.scdn.co/image/{COVER}"
    # A playlist's rows name no album and no picture: the GUI asks oEmbed per visible row.
    assert all(t.album == "" and t.art == "" for t in listing.tracks)
    assert listing.tracks[0].art_url == spotify.oembed_url(T1)


def test_a_full_embed_page_falls_back_to_spotdl_for_the_rest(fake):
    items = [list_item(f"{i:022d}") for i in range(engine.EMBED_PAGE_CAP)]
    fake["embeds"][("playlist", PLAYLIST)] = playlist_entity(items)
    fake["rest"] = [
        FakeSong(song_id=f"{0:022d}", name="Already listed", artists=["A"], duration=85),
        FakeSong(song_id=T1, name="Row 101", artists=["Pitbull"], duration=85, explicit=True),
        FakeSong(song_id=None, name="A local file"),
    ]
    result, _ = _run({"mode": "analyze"}, url=PLAYLIST_URL)
    listing = spotify.parse_listing(result)
    assert fake["metadata_url"] == PLAYLIST_URL
    assert len(listing.tracks) == engine.EMBED_PAGE_CAP + 1
    assert listing.tracks[-1].track_id == T1 and listing.tracks[-1].title == "Row 101"
    (kwargs,) = fake["init"]  # spotDL's free client, never the over-quota official API
    assert kwargs["use_official_api"] is False and kwargs["no_cache"] is True
    for key in ("user_auth", "auth_token", "use_cache_file"):
        assert not kwargs.get(key)


def test_a_short_list_never_asks_spotdl(fake):
    items = [list_item(f"{i:022d}") for i in range(engine.EMBED_PAGE_CAP - 1)]
    fake["embeds"][("playlist", PLAYLIST)] = playlist_entity(items)
    _run({"mode": "analyze"}, url=PLAYLIST_URL)
    assert "metadata_url" not in fake


def test_a_failing_fallback_keeps_the_first_page_with_a_warning(fake):
    items = [list_item(f"{i:022d}") for i in range(engine.EMBED_PAGE_CAP)]
    fake["embeds"][("playlist", PLAYLIST)] = playlist_entity(items)
    fake["rest"] = RuntimeError("spotDL is over quota")
    result, events = _run({"mode": "analyze"}, url=PLAYLIST_URL)
    assert len(result["tracks"]) == engine.EMBED_PAGE_CAP
    message = f"only the first {engine.EMBED_PAGE_CAP} songs could be listed"
    assert ("log", {"level": "warning", "message": message}) in events


def test_a_missing_track_reads_as_not_found(fake):
    fake["embeds"] = {}
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "404" in info.value.message
    assert errors.friendly_message(info.value.code, info.value.message)


def test_an_embed_for_another_track_is_not_accepted(fake):
    fake["embeds"][("track", T1)] = track_entity(T2)
    with pytest.raises(EngineError, match="404"):
        _run({"mode": "analyze"})


def test_a_spotify_failure_is_one_redacted_error_not_a_traceback(fake):
    fake["embed_error"] = RuntimeError(
        "429 Too Many Requests for https://open.spotify.com/x?token=SUPERSECRET"
    )
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "SUPERSECRET" not in info.value.message and "https://" not in info.value.message
    assert "429" in info.value.message


def test_the_embed_entity_is_read_from_next_data():
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"state": {"data": {"entity": {"type": "x"}}}}}})
        + "</script></html>"
    )
    assert engine.embed_entity(html) == {"type": "x"}
    for broken in ("<html></html>", '<script id="__NEXT_DATA__">{not json</script>'):
        with pytest.raises(EngineError, match="404"):
            engine.embed_entity(broken)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://i.scdn.co/image/{H300}", f"https://i.scdn.co/image/{H300}"),
        (f"https://image-cdn-ak.spotifycdn.com/image/{H300}", f"https://i.scdn.co/image/{H300}"),
        (f"https://image-cdn-fa.spotifycdn.com/image/{H640}", f"https://i.scdn.co/image/{H640}"),
        (f"http://i.scdn.co/image/{H300}", None),
        (f"https://i.scdn.co.evil.example/image/{H300}", None),
        (f"https://evil.spotifycdn.com/image/{H300}", None),
        (f"https://i.scdn.co/image/{H300}?x=1", None),
        (f"https://user@i.scdn.co/image/{H300}", None),
        (f"https://i.scdn.co:8443/image/{H300}", None),
        ("https://i.scdn.co/image/../../etc", None),
        ("https://mosaic.scdn.co/640/abc", None),
        (None, None),
    ],
)
def test_spotify_pictures_are_normalized_to_one_host_by_hash(url, expected):
    assert engine.spotify_image(url) == expected


# ── match ─────────────────────────────────────────────────────────────────────────────────
def listing_track(album=""):
    return spotify.SpotifyTrack(T1, 1, "Global Warming (feat. Sensato)", ("Pitbull",), album, 85.0)


def test_a_song_match_is_certain_and_carries_its_album(fake):
    fake["songs"] = [GOOD_SONG]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == "qjgnkysCPm4" and result["method"] == "song"
    assert result["album"] == "Global Warming" and result["duration"] == 85.0
    assert result["channel"] == "Pitbull, Sensato" and result["confidence"] >= 90
    m = spotify.parse_match(result, listing_track())
    assert m.method == "song" and m.album == "Global Warming" and not spotify.is_uncertain(m)
    ((query, kind),) = fake["queries"]  # a song hit needs no video search
    assert kind == "songs" and "Pitbull" in query and "Global Warming" in query
    assert fake["init"] == []  # spotDL is not involved in matching


def test_an_album_link_looks_on_the_album_first(fake):
    fake["albums"] = [
        ytm_album("MPREb_other", "Global Warming", ["Someone Else"]),
        ytm_album("MPREb_right", "Global Warming", ["Pitbull"]),
    ]
    fake["album_pages"]["MPREb_right"] = {
        "title": "Global Warming",
        "tracks": [
            album_track("wrongLength", "Global Warming (feat. Sensato)", ["Pitbull"], 89),
            album_track("rightTrack1", "Global Warming (feat. Sensato)", ["Pitbull"], 86),
        ],
    }
    fake["songs"] = [GOOD_SONG]
    result, _ = _run(spotify.match_options("Global Warming"))
    assert result["video_id"] == "rightTrack1" and result["method"] == "album"
    assert result["album"] == "Global Warming"
    assert ("Global Warming Pitbull", "albums") in fake["queries"]
    assert ("MPREb_other", "get_album") not in fake["queries"]  # another artist's album
    # The songs are still looked up, so Change… has alternatives to offer.
    assert [c["video_id"] for c in result["candidates"]] == ["qjgnkysCPm4"]


def test_an_album_without_the_track_falls_back_to_songs(fake):
    fake["albums"] = [ytm_album("MPREb_right", "Global Warming", ["Pitbull"])]
    fake["album_pages"]["MPREb_right"] = {
        "title": "Global Warming",
        "tracks": [album_track("otherSong11", "Another Song", ["Pitbull"], 85)],
    }
    fake["songs"] = [GOOD_SONG]
    result, _ = _run(spotify.match_options("Global Warming"))
    assert result["video_id"] == "qjgnkysCPm4" and result["method"] == "song"


def test_a_failing_album_search_warns_and_songs_still_run(fake):
    fake["albums"] = RuntimeError("YouTube Music is down")
    fake["songs"] = [GOOD_SONG]
    result, events = _run(spotify.match_options("Global Warming"))
    assert result["method"] == "song"
    assert ("log", {"level": "warning", "message": "YouTube Music album search failed"}) in events


def test_no_album_known_means_no_album_search(fake):
    fake["songs"] = [GOOD_SONG]
    _run({"mode": "match"})
    assert all(kind != "albums" for _, kind in fake["queries"])


def test_only_a_video_is_a_match_marked_uncertain(fake):
    fake["songs"] = [ytm_song("otherVideo1", "Some Other Song", ["Pitbull"], 85)]
    fake["videos"] = [
        ytm_video("liveVideo11", "Pitbull - Global Warming (Live)", ["Pitbull"], 85),
        ytm_video("musicVideo1", "Pitbull - Global Warming (Official Video)", ["Pitbull"], 95),
        ytm_video("lyricVideo1", "Pitbull - Global Warming (Lyric Video)", ["Pitbull"], 86),
    ]
    result, _ = _run({"mode": "match"})
    # The closest length wins; a lyric video carries the same audio, a live cut never does.
    assert result["video_id"] == "lyricVideo1" and result["method"] == "video"
    m = spotify.parse_match(result, listing_track())
    assert spotify.is_uncertain(m)  # a video is never certain, however well it scores
    kinds = {c["video_id"]: c["kind"] for c in result["candidates"]}
    assert kinds == {"otherVideo1": "song", "liveVideo11": "video", "musicVideo1": "video"}


def test_no_match_at_all_is_a_clear_error(fake):
    fake["videos"] = [ytm_video("unrelated11", "Totally Different", ["Nobody"], 300)]
    with pytest.raises(EngineError) as info:
        _run({"mode": "match"})
    assert info.value.code == "no_match"


def test_failing_searches_are_warnings_then_no_match(fake):
    fake["songs"] = RuntimeError("down")
    fake["videos"] = RuntimeError("down")
    with pytest.raises(EngineError) as info:
        _run({"mode": "match"})
    assert info.value.code == "no_match"


FIELDS = {
    "name": "10 Things I Hate About You",
    "artists": ["Leah Kate"],
    "album_name": "10 Things I Hate About You",
    "duration": 157,
    "explicit": False,
}
LIVE_SONGS = [  # the real top results for this track, September 2026
    ytm_song("qjgnkysCPm4", "10 Things I Hate About You", ["Leah Kate"], 158, True, FIELDS["name"]),
    ytm_song(
        "V3ejrgCuYcE", "10 Things I Hate About You", ["Leah Kate"], 158, False, FIELDS["name"]
    ),
    ytm_song("4uX_eFKAdvI", "Life Sux", ["Leah Kate"], 175, True, "Life Sux"),
    ytm_song("fBKdcbV7Ybw", "10 Things I Hate About You (Sped Up)", ["10X", "Leah Kate"], 143),
    ytm_song("efdwopOJtRk", "Twinkle Twinkle", ["Leah Kate"], 157, True, "Twinkle Twinkle"),
]


def test_the_clean_official_song_is_picked_for_a_clean_spotify_track():
    picked = engine.pick_song(LIVE_SONGS, FIELDS)
    assert picked["video_id"] == "V3ejrgCuYcE" and picked["duration"] == 158.0
    assert picked["method"] == "song"


def test_the_explicit_version_is_picked_for_an_explicit_spotify_track():
    assert engine.pick_song(LIVE_SONGS, {**FIELDS, "explicit": True})["video_id"] == "qjgnkysCPm4"


@pytest.mark.parametrize(
    "song",
    [
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Sped Up)", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Live)", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Lyric Video)", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Someone Else"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 161),  # 4 s off
        ytm_song("aaaaaaaaaaa", "Twinkle Twinkle", ["Leah Kate"], 157),
        ytm_song("bad id", "10 Things I Hate About You", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 0),
        {
            **ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 157),
            "resultType": "video",
        },
    ],
)
def test_a_different_recording_is_never_picked_as_a_song(song):
    assert engine.pick_song([song], FIELDS) is None


def test_a_song_three_seconds_off_still_matches():
    song = ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 160)
    assert engine.pick_song([song], FIELDS)["video_id"] == "aaaaaaaaaaa"


def test_a_variant_spotify_itself_names_is_allowed():
    fields = {**FIELDS, "name": "10 Things I Hate About You (Sped Up)", "duration": 143}
    picked = engine.pick_song(LIVE_SONGS, fields)
    assert picked["video_id"] == "fBKdcbV7Ybw"


def test_a_feat_part_does_not_stop_a_match():
    fields = {**FIELDS, "name": "10 Things I Hate About You (feat. Somebody)"}
    assert engine.pick_song(LIVE_SONGS, fields)["video_id"] == "V3ejrgCuYcE"


@pytest.mark.parametrize("results", [None, "x", [None, 3, {"videoId": 5}]])
def test_garbage_results_pick_nothing(results):
    assert engine.pick_song(results, FIELDS) is None
    assert engine.pick_video(results, FIELDS) is None
    assert engine.pick_album_track({"tracks": results}, FIELDS) is None


def test_an_album_track_must_be_within_two_seconds():
    album = {
        "title": "10 Things I Hate About You",
        "tracks": [
            album_track("threeOff111", "10 Things I Hate About You", ["Leah Kate"], 160),
            album_track("twoOff11111", "10 Things I Hate About You", ["Leah Kate"], 159),
        ],
    }
    picked = engine.pick_album_track(album, FIELDS)
    assert picked["video_id"] == "twoOff11111" and picked["method"] == "album"
    assert picked["album"] == "10 Things I Hate About You"
    album["tracks"].pop()
    assert engine.pick_album_track(album, FIELDS) is None


def test_a_video_far_off_in_length_is_not_offered_as_the_match():
    far = ytm_video("aaaaaaaaaaa", "Leah Kate - 10 Things I Hate About You", ["Leah Kate"], 250)
    assert engine.pick_video([far], FIELDS) is None


# ── download ──────────────────────────────────────────────────────────────────────────────
def test_a_download_uses_the_reviewed_match_and_tags_from_the_embed(fake, tmp_path):
    result, events = _run(spotify.download_options(VID), out=tmp_path)
    (call,) = fake["ytdlp_calls"]
    assert call.url == f"https://www.youtube.com/watch?v={VID}"
    assert call.options["preset"] == "mp3_music" and call.options["mode"] == "download"
    assert fake["queries"] == []  # a reviewed match is not second-guessed

    final = tmp_path / "Pitbull, Sensato - Global Warming (feat. Sensato).mp3"
    assert result["files"] == [str(final)] and final.is_file()
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".mp3"] == [final.name]
    ((_, fields, cover),) = fake["tagged"]
    assert fields["name"] == "Global Warming (feat. Sensato)"
    assert fields["artists"] == ["Pitbull", "Sensato"] and fields["year"] == "2012"
    # A playlist row's album is unknown until a match names it; no track number either.
    assert fields["album_name"] == "" and fields["track_number"] is None
    # The 640 px picture from the track's own embed (P9).
    assert fake["covers"] == [f"https://i.scdn.co/image/{H640}"]
    assert cover == fake["cover"]
    assert result["match"] == {"video_id": VID, "manual": True}

    stages = [d["stage"] for k, d in events if k == "stage"]
    assert stages.count("completed") == 1 and stages[-1] == "completed"  # only once tagged
    assert stages.index("tagging") < stages.index("completed")
    assert any(k == "progress" for k, _ in events)


def test_an_album_download_tags_the_album_and_track_number(fake, tmp_path):
    _run(spotify.download_options(VID, album="Global Warming", album_track=4), out=tmp_path)
    ((_, fields, _),) = fake["tagged"]
    assert fields["album_name"] == "Global Warming" and fields["track_number"] == 4


def test_a_download_without_a_match_searches_and_takes_the_songs_album(fake, tmp_path):
    fake["songs"] = [GOOD_SONG]
    result, _ = _run(spotify.download_options(), out=tmp_path)
    assert fake["ytdlp_calls"][0].url.endswith("qjgnkysCPm4")
    assert result["match"] == {"video_id": "qjgnkysCPm4", "manual": False}
    ((_, fields, _),) = fake["tagged"]
    assert fields["album_name"] == "Global Warming" and fields["track_number"] is None


def test_an_edited_title_names_the_file_but_tags_stay_spotifys(fake, tmp_path):
    result, _ = _run(spotify.download_options(VID, edited_title="My own name"), out=tmp_path)
    assert Path(result["files"][0]).name == "My own name.mp3"
    ((_, fields, _),) = fake["tagged"]
    assert fields["name"] == "Global Warming (feat. Sensato)"


def test_the_archive_skips_a_track_before_spotify_is_asked(fake, tmp_path):
    _run(spotify.download_options(VID), out=tmp_path)
    fake["embed_error"] = AssertionError("must not be read again")
    result, _ = _run(spotify.download_options(VID), out=tmp_path)
    assert result["skipped"] is True and result["files"] == []
    assert len(fake["ytdlp_calls"]) == 1


def test_archive_off_downloads_again_without_overwriting(fake, tmp_path):
    _run(spotify.download_options(VID, archive=False), out=tmp_path)
    result, _ = _run(spotify.download_options(VID, archive=False), out=tmp_path)
    assert Path(result["files"][0]).name == (
        "Pitbull, Sensato - Global Warming (feat. Sensato) (2).mp3"
    )
    assert not (tmp_path / engine.ARCHIVE_FILENAME).exists()


def test_a_missing_cover_is_a_warning_not_a_failure(fake, tmp_path):
    fake["cover"] = None
    result, events = _run(spotify.download_options(VID), out=tmp_path)
    assert result["files"]
    assert ("log", {"level": "warning", "message": "the cover could not be loaded"}) in (
        events
    )


def test_no_mp3_is_no_output(fake, tmp_path, monkeypatch):
    from stuff_downloader_worker.engines import ytdlp

    monkeypatch.setattr(ytdlp.YtDlpEngine, "download", lambda self, job, emit: {"files": []})
    with pytest.raises(EngineError) as info:
        _run(spotify.download_options(VID), out=tmp_path)
    assert info.value.code == "no_output"
    assert not (tmp_path / engine.ARCHIVE_FILENAME).exists()  # a failure is never archived


# ── helpers ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("artists", "title", "expected"),
    [
        (["AC/DC"], 'Who: "Me"?', "AC_DC - Who_ _Me__"),
        ([], "", "Unknown artist - Untitled"),
        (["A"], "x" * 400, ("A - " + "x" * 400)[: engine.MAX_NAME]),
        (["CON"], "..", "CON - "),
    ],
)
def test_file_stems_are_windows_safe(artists, title, expected):
    stem = engine.file_stem(artists, title)
    assert stem == expected.strip().rstrip(". ")
    assert not any(c in stem for c in '<>:"/\\|?*')


@pytest.mark.parametrize(
    "url",
    [
        "http://i.scdn.co/image/x",
        "https://i.scdn.co.evil.example/image/x",
        f"https://image-cdn-ak.spotifycdn.com/image/{H640}",  # only the normalized form
        "https://evil.example/cover.jpg",
        "file:///C:/cover.jpg",
        None,
    ],
)
def test_covers_are_only_fetched_from_normalized_spotify_pictures(url, monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)  # any fetch attempt would raise
    assert engine.fetch_cover(url) is None


def test_candidate_rows_are_capped_and_plain():
    rows = engine.candidate_rows(
        [ytm_song(f"{i:011d}", f"Song {i}\x00‮", ["A"], 100) for i in range(30)]
    )
    assert len(rows) == engine.MAX_CANDIDATES
    assert "\x00" not in rows[0]["title"] and "‮" not in rows[0]["title"]
    assert rows[0]["kind"] == "song"
    assert engine.candidate_rows("not a list") == []


def test_a_match_lists_the_other_song_results_as_candidates(fake):
    fake["songs"] = [
        GOOD_SONG,
        ytm_song("otherVideo1", "Global Warming (Live)", ["Pitbull"], 190),
        ytm_song("otherVideo1", "duplicate", ["Pitbull"], 190),
        {"resultType": "video", "videoId": "videoOnly11", "title": "MV"},
        {"resultType": "song", "videoId": "bad id!", "title": "x"},
    ]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == "qjgnkysCPm4"
    assert result["candidates"] == [
        {"video_id": "otherVideo1", "title": "Global Warming (Live)", "channel": "Pitbull",
         "duration": 190.0, "kind": "song"},
    ]  # fmt: skip


# ── real mutagen, in the spotdl runtime ───────────────────────────────────────────────────
@pytest.mark.skipif(
    Path(SPOTDL_PYTHON).resolve() == Path(sys.executable).resolve(),
    reason="the spotDL engine runtime is not installed",
)
@pytest.mark.parametrize("album", [True, False])
def test_tagging_replaces_youtubes_tags_and_cover_with_spotifys(tmp_path, album):
    """Runs tag_mp3 with the runtime's real mutagen on a file that already has YouTube tags."""
    script = r"""
import json, sys
from mutagen.id3 import APIC, ID3, TALB, TIT2, TRCK
from stuff_downloader_worker.engines import spotdl as engine
path, cover = sys.argv[1], bytes.fromhex(sys.argv[2])
old = ID3()
old.add(TIT2(encoding=3, text="YouTube title (Official Video)"))
old.add(TALB(encoding=3, text="YouTube"))
old.add(TRCK(encoding=3, text="7"))
old.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="yt", data=b"\xff\xd8youtube"))
old.save(path)
fields = json.loads(sys.argv[3])
report = engine.tag_mp3(path, fields, cover)
tags = ID3(path)
text = lambda key: tags[key].text if key in tags else None
print(json.dumps({
    "report": report,
    "TIT2": text("TIT2"), "TPE1": text("TPE1"), "TPE2": text("TPE2"),
    "TALB": text("TALB"), "TRCK": text("TRCK"), "TDRC": str(tags.get("TDRC") or tags.get("TYER")),
    "WOAS": tags["WOAS"].url, "APIC": [a.data.hex() for a in tags.getall("APIC")],
    "version": list(tags.version),
}))
"""
    # A real 1x1 JPEG header is enough for jpeg_size; mutagen does not decode images.
    cover = bytes.fromhex("ffd8ffc0000b080001000101011100ffd9")
    path = tmp_path / "song.mp3"
    path.write_bytes(b"")
    opts = {"album": "Global Warming", "album_track": 1} if album else {}
    fields = engine.track_fields(track_entity(), T1, opts)
    src = Path(__file__).resolve().parents[2] / "src"
    out = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [SPOTDL_PYTHON, "-s", "-c", script, str(path), cover.hex(), json.dumps(fields)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": str(src), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
    )
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["TIT2"] == ["Global Warming (feat. Sensato)"]
    assert got["TPE1"] == ["Pitbull, Sensato"] and got["TPE2"] == ["Pitbull"]
    if album:
        assert got["TALB"] == ["Global Warming"] and got["TRCK"] == ["1"]
    else:  # YouTube's album and track number are removed, not left behind (plan §5.7)
        assert got["TALB"] is None and got["TRCK"] is None
    assert got["TDRC"].startswith("2012")
    # v2.3, because Windows Explorer and Media Player show no cover from a v2.4 tag.
    assert got["version"] == [2, 3, 0]
    assert got["WOAS"] == TRACK_URL
    assert got["APIC"] == [cover.hex()]  # YouTube's cover is gone, not kept alongside
    assert got["report"]["cover"] == {"width": 1, "height": 1}
