"""spotDL engine against a stand-in spotdl package. No network, no Spotify, no YouTube.

The stand-in mirrors only the surface the engine uses (DEFAULT_CONFIG, SpotifyClient,
Album/Playlist.get_metadata, Song.from_missing_data, YouTubeMusic.search/get_best_result). The
yt-dlp download, the cover fetch and mutagen are replaced at the engine's own seams, so these
tests pin the engine's rules: which client it builds, what reaches core, how matches and
failures read, and what the archive does. Tagging itself is checked against the real mutagen in
the spotdl runtime at the bottom of this file.
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

T1 = "6OmhkSOpvYBokMKQxpIGx2"
ALBUM = "4aawyAB9vmqN3uQ7FjRGTy"
VID = "q7xBoh0emqo"
TRACK_URL = f"https://open.spotify.com/track/{T1}"
ALBUM_URL = f"https://open.spotify.com/album/{ALBUM}"

# Resolved at import time, before conftest redirects LOCALAPPDATA (see tests/network/netjob.py).
SPOTDL_PYTHON = default_worker_command("spotdl")[0]


def raw_track(track_id=T1, **extra):
    return {
        "id": track_id,
        "name": "Global Warming (feat. Sensato)",
        "artists": [{"name": "Pitbull"}, {"name": "Sensato"}],
        "album": {
            "id": ALBUM,
            "name": "Global Warming",
            "artists": [{"name": "Pitbull"}],
            "album_type": "album",
            "release_date": "2012-11-16",
            "total_tracks": 18,
            "images": [
                {"url": "https://i.scdn.co/image/small", "width": 64, "height": 64},
                {"url": "https://i.scdn.co/image/big", "width": 640, "height": 640},
            ],
        },
        "disc_number": 1,
        "track_number": 1,
        "duration_ms": 85000,
        "explicit": True,
        "external_ids": {"isrc": ""},
        **extra,
    }


class FakeSong:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakeResult:
    def __init__(self, url, name="Pitbull - Global Warming", author="PitbullVEVO", duration=88.0):
        self.url, self.name, self.author, self.duration = url, name, author, duration


@pytest.fixture
def fake_spotdl(monkeypatch):
    state = {
        "init": [],
        "tracks": {T1: raw_track()},
        "album_songs": [],
        "album_meta": {"name": "Global Warming", "artist": {"name": "Pitbull"}},
        "search_url": f"https://music.youtube.com/watch?v={VID}",
        "score": 97.0,
        "score_url": None,  # defaults to search_url
        "track_error": None,
        "ytdlp_calls": [],
        "cover": b"\xff\xd8cover",
        "tagged": [],
        "songs": [],  # YouTube Music song results; none means the spotDL fallback runs
        "song_queries": [],
    }

    class SpotifyClient:
        _instance = None

        def __new__(cls):
            return cls._instance

        @classmethod
        def init(cls, **kwargs):
            state["init"].append(kwargs)
            cls._instance = Client()
            return cls._instance

    class Client:
        def track(self, track_id):
            if state["track_error"]:
                raise state["track_error"]
            return state["tracks"].get(track_id)

    def get_metadata(url):
        state["metadata_url"] = url
        return state["album_meta"], state["album_songs"]

    class Song:
        @classmethod
        def from_missing_data(cls, **fields):
            return FakeSong(**fields)

    class YouTubeMusic:
        def get_best_result(self, results):
            url = state["score_url"] or state["search_url"]
            return FakeResult(url), state["score"]

        def search(self, song):
            state["searched"] = song
            if state["search_url"] is None:
                return None
            self.get_best_result({})
            return state["search_url"]

    modules = {
        "spotdl": types.ModuleType("spotdl"),
        "spotdl.utils": types.ModuleType("spotdl.utils"),
        "spotdl.utils.config": types.ModuleType("spotdl.utils.config"),
        "spotdl.utils.spotify": types.ModuleType("spotdl.utils.spotify"),
        "spotdl.types": types.ModuleType("spotdl.types"),
        "spotdl.types.album": types.ModuleType("spotdl.types.album"),
        "spotdl.types.playlist": types.ModuleType("spotdl.types.playlist"),
        "spotdl.types.song": types.ModuleType("spotdl.types.song"),
        "spotdl.providers": types.ModuleType("spotdl.providers"),
        "spotdl.providers.audio": types.ModuleType("spotdl.providers.audio"),
    }
    modules["spotdl.utils.config"].DEFAULT_CONFIG = {"client_id": "id", "client_secret": "sec"}
    modules["spotdl.utils.spotify"].SpotifyClient = SpotifyClient
    modules["spotdl.types.album"].Album = types.SimpleNamespace(get_metadata=get_metadata)
    modules["spotdl.types.playlist"].Playlist = types.SimpleNamespace(get_metadata=get_metadata)
    modules["spotdl.types.song"].Song = Song
    modules["spotdl.providers.audio"].YouTubeMusic = YouTubeMusic
    ytmusicapi = types.ModuleType("ytmusicapi")

    class YTMusic:
        def search(self, query, filter=None, limit=None):
            state["song_queries"].append((query, filter))
            if isinstance(state["songs"], Exception):
                raise state["songs"]
            return state["songs"]

    ytmusicapi.YTMusic = YTMusic
    modules["ytmusicapi"] = ytmusicapi
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
    monkeypatch.setattr(engine, "fetch_cover", lambda url: state["cover"])

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


# ── the client ────────────────────────────────────────────────────────────────────────────
def test_the_client_is_always_the_free_one_with_no_disk_cache(fake_spotdl):
    _run({"mode": "analyze"})
    (kwargs,) = fake_spotdl["init"]
    assert kwargs["use_official_api"] is False and kwargs["no_cache"] is True
    # Each of these silently switches spotDL to the official API, whose shared quota is spent.
    for key in ("user_auth", "auth_token", "use_cache_file"):
        assert not kwargs.get(key)


def test_the_client_is_built_once_per_process(fake_spotdl):
    _run({"mode": "analyze"})
    _run({"mode": "analyze"})
    assert len(fake_spotdl["init"]) == 1


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
def test_only_a_rebuilt_spotify_link_is_accepted(fake_spotdl, url):
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"}, url=url)
    assert info.value.code == "bad_options"


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "sync"},
        {"mode": "analyze", "extra": 1},
        {"mode": "match", "video_id": VID},
        {"mode": "download", "preset": "mp3_music"},
        {"mode": "download", "preset": "spotify_mp3", "video_id": "bad id"},
        {"mode": "download", "preset": "spotify_mp3", "archive": "yes"},
        {"mode": "download", "preset": "spotify_mp3", "output": "C:/Windows"},
    ],
)
def test_bad_options_are_refused_before_spotify_is_touched(fake_spotdl, options):
    with pytest.raises(EngineError) as info:
        _run(options)
    assert info.value.code == "bad_options"
    assert fake_spotdl["init"] == []


@pytest.mark.parametrize("mode", ["match", "download"])
def test_match_and_download_work_on_one_track_only(fake_spotdl, mode):
    options = {"mode": mode} if mode == "match" else spotify.download_options()
    with pytest.raises(EngineError, match="one track"):
        _run(options, url=ALBUM_URL)


def test_the_core_options_are_exactly_what_the_worker_accepts(fake_spotdl, tmp_path):
    for options in (
        spotify.analyze_options(),
        spotify.match_options(),
        spotify.download_options(VID, archive=False),
        spotify.download_options(),
    ):
        _run(options, out=tmp_path)


# ── analyze ───────────────────────────────────────────────────────────────────────────────
def test_analyzing_a_track_lists_it_with_bounded_text(fake_spotdl):
    fake_spotdl["tracks"][T1]["name"] = "Title\n with https://evil.example/x link"
    result, events = _run({"mode": "analyze"})
    listing = spotify.parse_listing(result)
    assert (listing.kind, listing.spotify_id) == ("track", T1)
    (track,) = listing.tracks
    assert track.title == "Title with [link] link"
    assert track.artists == ("Pitbull", "Sensato") and track.duration == 85.0
    assert track.cover_url == "https://i.scdn.co/image/big"
    assert events[0] == ("stage", {"stage": "analyzing"})


def test_spotify_artwork_is_rejected_from_other_hosts():
    raw = raw_track()
    raw["album"]["images"] = [{"url": "https://localhost/cover.jpg", "width": 640, "height": 640}]
    assert engine.track_row(raw)["cover_url"] is None


def test_analyzing_an_album_uses_one_listing_pass_and_counts_unusable_rows(fake_spotdl):
    good = FakeSong(
        song_id=T1, name="One", artists=["Pitbull"], album_name="A", duration=85, explicit=False
    )
    local = FakeSong(song_id=None, name="A local file")
    fake_spotdl["album_songs"] = [good, local]
    result, _ = _run({"mode": "analyze"}, url=ALBUM_URL)
    listing = spotify.parse_listing(result)
    assert fake_spotdl["metadata_url"] == ALBUM_URL
    assert listing.kind == "album" and listing.title == "Global Warming"
    assert listing.owner == "Pitbull"  # an album's artist arrives as a dict, not text
    assert [t.track_id for t in listing.tracks] == [T1]
    assert listing.skipped == 1


def test_a_missing_track_reads_as_not_found(fake_spotdl):
    fake_spotdl["tracks"] = {}
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "404" in info.value.message
    assert errors.friendly_message(info.value.code, info.value.message)


def test_a_spotify_failure_is_one_redacted_error_not_a_traceback(fake_spotdl):
    fake_spotdl["track_error"] = RuntimeError(
        "429 Too Many Requests for https://api-partner.spotify.com/x?token=SUPERSECRET"
    )
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "SUPERSECRET" not in info.value.message and "https://" not in info.value.message
    assert "429" in info.value.message


# ── match ─────────────────────────────────────────────────────────────────────────────────
def test_a_match_carries_spotdls_pick_and_its_score(fake_spotdl):
    result, _ = _run({"mode": "match"})
    listing_track = spotify.parse_listing(
        {"spotify_kind": "track", "spotify_id": T1, "tracks": [engine.track_row(raw_track())]}
    ).tracks[0]
    m = spotify.parse_match(result, listing_track)
    assert m.video_id == VID and m.confidence == 97.0
    assert m.channel == "PitbullVEVO" and m.duration_diff == 3.0
    # spotDL matched against Spotify's own metadata, taken from the track read.
    assert fake_spotdl["searched"].name == "Global Warming (feat. Sensato)"
    assert fake_spotdl["searched"].cover_url == "https://i.scdn.co/image/big"


def test_a_pick_without_a_score_has_no_confidence(fake_spotdl):
    fake_spotdl["score_url"] = "https://www.youtube.com/watch?v=otherVideo1"  # an ISRC hit
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == VID and result["confidence"] is None


@pytest.mark.parametrize(
    "url",
    [None, "https://evil.example/watch?v=q7xBoh0emqo", "https://www.youtube.com/watch?v=bad"],
)
def test_no_usable_match_is_a_clear_error(fake_spotdl, url):
    fake_spotdl["search_url"] = url
    with pytest.raises(EngineError) as info:
        _run({"mode": "match"})
    assert info.value.code == "no_match"


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


def test_a_youtube_music_song_wins_over_spotdls_video_pick(fake_spotdl):
    """Seen live: spotDL picked a music video 7 s longer than Spotify's recording, because it
    reads every YouTube Music song as length 0 and so never scores one."""
    fake_spotdl["songs"] = [
        ytm_song("qjgnkysCPm4", "Global Warming (feat. Sensato)", ["Pitbull", "Sensato"], 85),
    ]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == "qjgnkysCPm4" and result["duration"] == 85.0
    assert result["channel"] == "Pitbull, Sensato" and result["confidence"] >= 90
    assert "searched" not in fake_spotdl  # spotDL's video search never ran
    ((query, kind),) = fake_spotdl["song_queries"]
    assert kind == "songs" and "Pitbull" in query and "Global Warming" in query


def test_no_fitting_song_falls_back_to_spotdl(fake_spotdl):
    fake_spotdl["songs"] = [ytm_song("otherVideo1", "Some Other Song", ["Pitbull"], 85)]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == VID and "searched" in fake_spotdl


def test_a_failing_song_search_falls_back_with_a_warning(fake_spotdl):
    fake_spotdl["songs"] = RuntimeError("YouTube Music is down")
    result, events = _run({"mode": "match"})
    assert result["video_id"] == VID
    assert ("log", {"level": "warning", "message": "YouTube Music song search failed"}) in events


def test_a_download_without_a_match_uses_the_song_too(fake_spotdl, tmp_path):
    fake_spotdl["songs"] = [ytm_song("qjgnkysCPm4", "Global Warming", ["Pitbull"], 86)]
    result, _ = _run(spotify.download_options(), out=tmp_path)
    assert fake_spotdl["ytdlp_calls"][0].url.endswith("qjgnkysCPm4")
    assert result["match"] == {"video_id": "qjgnkysCPm4", "manual": False}


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


def test_the_explicit_version_is_picked_for_an_explicit_spotify_track():
    assert engine.pick_song(LIVE_SONGS, {**FIELDS, "explicit": True})["video_id"] == "qjgnkysCPm4"


@pytest.mark.parametrize(
    "song",
    [
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Sped Up)", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Live)", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Someone Else"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 170),  # 13 s off
        ytm_song("aaaaaaaaaaa", "Twinkle Twinkle", ["Leah Kate"], 157),
        ytm_song("bad id", "10 Things I Hate About You", ["Leah Kate"], 157),
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 0),
        {
            **ytm_song("aaaaaaaaaaa", "10 Things I Hate About You", ["Leah Kate"], 157),
            "resultType": "video",
        },
    ],
)
def test_a_different_recording_is_never_picked(song):
    assert engine.pick_song([song], FIELDS) is None


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


# ── download ──────────────────────────────────────────────────────────────────────────────
def test_a_download_uses_the_reviewed_match_and_names_the_file_from_spotify(fake_spotdl, tmp_path):
    result, events = _run(spotify.download_options(VID), out=tmp_path)
    (call,) = fake_spotdl["ytdlp_calls"]
    assert call.url == f"https://www.youtube.com/watch?v={VID}"
    assert call.options["preset"] == "mp3_music" and call.options["mode"] == "download"
    assert "searched" not in fake_spotdl  # a reviewed match is not second-guessed

    final = tmp_path / "Pitbull, Sensato - Global Warming (feat. Sensato).mp3"
    assert result["files"] == [str(final)] and final.is_file()
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".mp3"] == [final.name]
    ((_, fields, cover),) = fake_spotdl["tagged"]
    assert fields["album_name"] == "Global Warming" and fields["track_number"] == 1
    assert cover == fake_spotdl["cover"]
    assert result["match"] == {"video_id": VID, "manual": True}

    stages = [d["stage"] for k, d in events if k == "stage"]
    assert stages.count("completed") == 1 and stages[-1] == "completed"  # only once tagged
    assert stages.index("tagging") < stages.index("completed")
    assert any(k == "progress" for k, _ in events)


def test_a_download_without_a_match_searches_itself(fake_spotdl, tmp_path):
    result, _ = _run(spotify.download_options(), out=tmp_path)
    assert fake_spotdl["ytdlp_calls"][0].url.endswith(VID)
    assert result["match"] == {"video_id": VID, "manual": False}


def test_custom_file_name_does_not_change_spotify_tags(fake_spotdl, tmp_path):
    options = spotify.download_options(VID)
    options["output_name"] = "My: Version"
    result, _ = _run(options, out=tmp_path)
    assert (tmp_path / "My_ Version.mp3").is_file()
    assert result["title"] == "My_ Version"
    assert fake_spotdl["tagged"][0][1]["name"] == "Global Warming (feat. Sensato)"


def test_the_archive_skips_a_track_before_spotify_is_asked(fake_spotdl, tmp_path):
    _run(spotify.download_options(VID), out=tmp_path)
    fake_spotdl["track_error"] = AssertionError("must not be read again")
    result, _ = _run(spotify.download_options(VID), out=tmp_path)
    assert result["skipped"] is True and result["files"] == []
    assert len(fake_spotdl["ytdlp_calls"]) == 1


def test_archive_off_downloads_again_without_overwriting(fake_spotdl, tmp_path):
    _run(spotify.download_options(VID, archive=False), out=tmp_path)
    result, _ = _run(spotify.download_options(VID, archive=False), out=tmp_path)
    assert Path(result["files"][0]).name == (
        "Pitbull, Sensato - Global Warming (feat. Sensato) (2).mp3"
    )
    assert not (tmp_path / engine.ARCHIVE_FILENAME).exists()


def test_a_missing_cover_is_a_warning_not_a_failure(fake_spotdl, tmp_path):
    fake_spotdl["cover"] = None
    result, events = _run(spotify.download_options(VID), out=tmp_path)
    assert result["files"]
    assert ("log", {"level": "warning", "message": "the Spotify cover could not be loaded"}) in (
        events
    )


def test_no_mp3_is_no_output(fake_spotdl, tmp_path, monkeypatch):
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
        "https://evil.example/cover.jpg",
        "file:///C:/cover.jpg",
        None,
    ],
)
def test_covers_are_only_fetched_from_spotifys_https_hosts(url, monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", None)  # any fetch attempt would raise
    assert engine.fetch_cover(url) is None


# ── real mutagen, in the spotdl runtime ───────────────────────────────────────────────────
@pytest.mark.skipif(
    Path(SPOTDL_PYTHON).resolve() == Path(sys.executable).resolve(),
    reason="the spotDL engine runtime is not installed",
)
def test_tagging_replaces_youtubes_tags_and_cover_with_spotifys(tmp_path):
    """Runs tag_mp3 with the runtime's real mutagen on a file that already has YouTube tags."""
    script = r"""
import json, sys
from mutagen.id3 import APIC, ID3, TALB, TIT2
from stuff_downloader_worker.engines import spotdl as engine
path, cover = sys.argv[1], bytes.fromhex(sys.argv[2])
old = ID3()
old.add(TIT2(encoding=3, text="YouTube title (Official Video)"))
old.add(TALB(encoding=3, text="YouTube"))
old.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="yt", data=b"\xff\xd8youtube"))
old.save(path)
fields = json.loads(sys.argv[3])
report = engine.tag_mp3(path, fields, cover)
tags = ID3(path)
print(json.dumps({
    "report": report,
    "TIT2": tags["TIT2"].text, "TPE1": tags["TPE1"].text, "TPE2": tags["TPE2"].text,
    "TALB": tags["TALB"].text, "TRCK": tags["TRCK"].text, "TDRC": str(tags["TDRC"]),
    "WOAS": tags["WOAS"].url, "APIC": [a.data.hex() for a in tags.getall("APIC")],
    "TSRC": tags.getall("TSRC") != [], "version": list(tags.version),
}))
"""
    # A real 1x1 JPEG header is enough for jpeg_size; mutagen does not decode images.
    cover = bytes.fromhex("ffd8ffc0000b080001000101011100ffd9")
    path = tmp_path / "song.mp3"
    path.write_bytes(b"")
    fields = engine.song_fields(raw_track())
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
    assert got["TALB"] == ["Global Warming"] and got["TRCK"] == ["1/18"]
    assert got["TDRC"].startswith("2012")
    # v2.3, because Windows Explorer and Media Player show no cover from a v2.4 tag.
    assert got["version"] == [2, 3, 0]
    assert got["WOAS"] == TRACK_URL
    assert got["APIC"] == [cover.hex()]  # YouTube's cover is gone, not kept alongside
    assert got["TSRC"] is False  # the free client gives no ISRC, so none is invented
    assert got["report"]["cover"] == {"width": 1, "height": 1}


# ── candidates for the owner's Change… dialog (item 9) ────────────────────────────────────
def test_a_match_lists_the_other_song_results_as_candidates(fake_spotdl):
    fake_spotdl["songs"] = [
        ytm_song("qjgnkysCPm4", "Global Warming (feat. Sensato)", ["Pitbull", "Sensato"], 85),
        ytm_song("otherVideo1", "Global Warming (Live)", ["Pitbull"], 190),
        ytm_song("otherVideo1", "duplicate", ["Pitbull"], 190),
        {"resultType": "video", "videoId": "videoOnly11", "title": "MV"},
        {"resultType": "song", "videoId": "bad id!", "title": "x"},
    ]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == "qjgnkysCPm4"
    assert result["candidates"] == [
        {
            "video_id": "otherVideo1",
            "title": "Global Warming (Live)",
            "channel": "Pitbull",
            "duration": 190.0,
        },
    ]  # the pick itself, videos, duplicates and bad ids are not repeated


def test_candidates_survive_the_spotdl_fallback(fake_spotdl):
    fake_spotdl["songs"] = [ytm_song("otherVideo1", "Some Other Song", ["Pitbull"], 85)]
    result, _ = _run({"mode": "match"})
    assert result["video_id"] == VID  # spotDL's pick
    assert [c["video_id"] for c in result["candidates"]] == ["otherVideo1"]


def test_alternatives_rank_matching_recordings_ahead_of_wrong_versions():
    results = [
        ytm_song("aaaaaaaaaaa", "10 Things I Hate About You (Live)", ["Leah Kate"], 157),
        ytm_song("bbbbbbbbbbb", "10 Things I Hate About You", ["Leah Kate"], 157),
    ]
    assert [row["video_id"] for row in engine.candidate_rows(results, FIELDS)] == [
        "bbbbbbbbbbb", "aaaaaaaaaaa"
    ]


def test_spotdl_fallback_rejects_an_obvious_wrong_version(fake_spotdl, monkeypatch):
    fake_spotdl["songs"] = []
    monkeypatch.setattr(
        engine.SpotDlEngine, "_spotdl_search",
        staticmethod(lambda song: (
            VID,
            FakeResult(f"https://music.youtube.com/watch?v={VID}", name="Global Warming (Live)"),
            99.0,
        )),
    )
    with pytest.raises(EngineError, match="No matching song"):
        _run({"mode": "match"})


def test_candidate_rows_are_capped_and_plain():
    rows = engine.candidate_rows(
        [ytm_song(f"{i:011d}", f"Song {i}\x00‮", ["A"], 100) for i in range(30)]
    )
    assert len(rows) == engine.MAX_CANDIDATES
    assert "\x00" not in rows[0]["title"] and "‮" not in rows[0]["title"]
    assert engine.candidate_rows("not a list") == []
