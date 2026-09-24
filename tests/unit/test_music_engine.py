"""The Apple Music / Deezer engine (plan §7 "Other music sites"), offline.

The public lookups are faked at ``get_json``; YouTube Music matching and the MP3 download are
faked at the Spotify engine's seams (``find_match``, ``YtDlpEngine.download``, ``fetch_cover``),
because the music engine shares them rather than copying them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stuff_downloader.core import router, spotify
from stuff_downloader_worker.engines import get_engine, music, spotdl
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec

ART100 = (
    "https://is1-ssl.mzstatic.com/image/thumb/Music115/v4/44/06/fd/"
    "4406fdc0-aab5-e300-82ba-3e5fe81a68a7/00602537868858.rgb.jpg/100x100bb.jpg"
)
ART300 = ART100.replace("100x100bb", "300x300bb")
ART600 = ART100.replace("100x100bb", "600x600bb")
MD5 = "5718f7c81c27e0b2417e2a4c45224f8a"
DZ250 = f"https://cdn-images.dzcdn.net/images/cover/{MD5}/250x250-000000-80-0-0.jpg"
DZ300 = f"https://cdn-images.dzcdn.net/images/cover/{MD5}/300x300-000000-80-0-0.jpg"
DZ1000 = f"https://cdn-images.dzcdn.net/images/cover/{MD5}/1000x1000-000000-80-0-0.jpg"
VID = "gAjR4_CbPpQ"


def apple_track(track_id, name, number, **extra):
    return {
        "wrapperType": "track",
        "kind": "song",
        "trackId": track_id,
        "trackName": name,
        "artistName": "Jack Johnson",
        "collectionId": 1440857781,
        "collectionName": "In Between Dreams",
        "trackTimeMillis": 207679,
        "releaseDate": "2005-01-01T12:00:00Z",
        "trackExplicitness": "notExplicit",
        "discNumber": 1,
        "trackNumber": number,
        "artworkUrl100": ART100,
        **extra,
    }


APPLE_ALBUM = {
    "resultCount": 4,
    "results": [
        {
            "wrapperType": "collection",
            "collectionId": 1440857781,
            "collectionName": "In Between Dreams",
            "artistName": "Jack Johnson",
            "artworkUrl100": ART100,
        },
        apple_track(1440857790, "Banana Pancakes", 3),
        apple_track(1440857786, "Better Together", 1),
        apple_track(1440857788, "Never Know", 2, trackExplicitness="explicit"),
        {"wrapperType": "track", "kind": "music-video", "trackId": 99, "trackName": "Video"},
    ],
}


def dz_track(track_id, title, **extra):
    return {
        "id": track_id,
        "type": "track",
        "title": title,
        "duration": 226,
        "explicit_lyrics": False,
        "artist": {"id": 27, "name": "Daft Punk"},
        "album": {"id": 302127, "title": "Discovery", "cover_medium": DZ250},
        **extra,
    }


@pytest.fixture
def answers(monkeypatch):
    """URL (with params) -> JSON; every request the engine makes is recorded."""
    table: dict = {}
    calls: list = []

    def fake(url, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        calls.append(key)
        if key not in table:
            raise AssertionError(f"unexpected request {key}")
        return table[key]

    monkeypatch.setattr(music, "get_json", fake)
    return table, calls


def lookup_key(catalog_id, country="us"):
    params = {"id": catalog_id, "entity": "song", "country": country, "limit": 200}
    return ("https://itunes.apple.com/lookup", tuple(sorted(params.items())))


def dz_key(path, **params):
    return (f"https://api.deezer.com/{path}", tuple(sorted(params.items())))


def run(url, options=None, output_dir="."):
    events = []
    result = music.MusicEngine().download(
        JobSpec("job", "music", url, output_dir, options or {"mode": "analyze"}),
        lambda kind, data: events.append((kind, data)),
    )
    return result, events


# ── the job URL ───────────────────────────────────────────────────────────────────────────
def test_the_engine_is_registered_as_music():
    assert isinstance(get_engine("music"), music.MusicEngine)


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com/us/playlist/1234",
        "https://music.apple.com/us/album/x/1234",
        "https://music.apple.com/us/album/1234?i=5",
        "http://www.deezer.com/track/1",
        "https://deezer.com/track/1",
        "https://www.deezer.com/artist/27",
        "https://www.deezer.com/track/0123",
        "https://evil.example/track/1",
    ],
)
def test_only_the_links_core_builds_are_accepted(url):
    with pytest.raises(EngineError) as info:
        music.parse_url(url)
    assert info.value.code == "bad_options"


def test_core_and_worker_agree_on_every_canonical_link():
    for text, expected in [
        ("https://music.apple.com/gb/album/x/1440857781?i=1440857786", ("apple", "track")),
        ("https://music.apple.com/us/album/in-between-dreams/1440857781", ("apple", "album")),
        ("https://www.deezer.com/fr/playlist/908622995", ("deezer", "playlist")),
    ]:
        r = router.route(text)
        service, kind, catalog_id, _ = music.parse_url(r.url)
        assert (service, kind) == expected and catalog_id == r.catalog_id


# ── pictures ──────────────────────────────────────────────────────────────────────────────
def test_pictures_are_resized_on_their_own_host_and_anything_else_is_dropped():
    assert music.apple_image(ART100) == ART300
    assert music.apple_image(ART100, 600) == ART600
    assert music.deezer_image(DZ250) == DZ300
    e_cdn = DZ250.replace("cdn-images", "e-cdns-images")
    assert music.deezer_image(e_cdn, 1000) == DZ1000
    for bad in [
        ART100.replace("is1-ssl.mzstatic.com", "evil.example"),
        ART100 + "?x=1",
        "http" + ART100[5:],
        DZ250.replace("/cover/", "/artist/"),
        "https://api.deezer.com/album/302127/image",
        None,
    ]:
        assert music.apple_image(bad) is None and music.deezer_image(bad) is None
    assert music.cover_check(ART600) == ART600 and music.cover_check(DZ1000) == DZ1000
    assert music.cover_check("https://i.scdn.co/image/" + "a" * 40) is None


def test_every_picture_the_engine_sends_is_one_core_accepts():
    for url in (ART300, ART600, DZ300, DZ1000):
        assert spotify.image_url(url) == url


# ── analyze ───────────────────────────────────────────────────────────────────────────────
def test_an_apple_album_lists_its_songs_in_album_order_with_the_album_as_their_album(answers):
    table, calls = answers
    table[lookup_key("1440857781")] = APPLE_ALBUM
    result, _ = run("https://music.apple.com/us/album/1440857781")
    listing = spotify.parse_listing(result)
    assert (listing.service, listing.kind, listing.spotify_id) == ("apple", "album", "1440857781")
    assert listing.title == "In Between Dreams" and listing.owner == "Jack Johnson"
    assert [t.title for t in listing.tracks] == ["Better Together", "Never Know", "Banana Pancakes"]
    assert [t.explicit for t in listing.tracks] == [False, True, False]
    assert {t.album for t in listing.tracks} == {"In Between Dreams"}
    assert all(t.art == ART300 and t.date == "2005-01-01" for t in listing.tracks)
    assert listing.tracks[0].duration == 207.7 and listing.cover == ART300
    assert listing.skipped == 1  # the music video
    assert result["site"] == "Apple Music"
    assert len(calls) == 1  # one lookup for the whole album


def test_an_apple_song_link_lists_that_one_song(answers):
    table, _ = answers
    table[lookup_key("1440857788", "gb")] = {"results": [apple_track(1440857788, "Never Know", 2)]}
    listing = spotify.parse_listing(run("https://music.apple.com/gb/song/1440857788")[0])
    assert listing.kind == "track" and listing.country == "gb"
    assert [t.track_id for t in listing.tracks] == ["1440857788"]
    assert listing.tracks[0].url == "https://music.apple.com/gb/song/1440857788"


def test_an_apple_link_with_no_such_song_is_a_404(answers):
    table, _ = answers
    table[lookup_key("5")] = {"resultCount": 0, "results": []}
    with pytest.raises(EngineError) as info:
        run("https://music.apple.com/us/song/5")
    assert "404" in info.value.message


def test_a_deezer_playlist_is_paged_by_index_to_its_last_song(answers):
    table, calls = answers
    first = [dz_track(1000 + n, f"Song {n}") for n in range(50)]
    rest = [dz_track(1000 + n, f"Song {n}") for n in range(50, 120)]
    table[dz_key("playlist/42")] = {
        "id": 42,
        "title": "Big list",
        "nb_tracks": 120,
        "creator": {"name": "Someone"},
        "picture_medium": DZ250.replace("/cover/", "/playlist/"),
        # The answer's own "next" link is never followed.
        "tracks": {"data": first, "next": "https://evil.example/next"},
    }
    table[dz_key("playlist/42/tracks", index=50, limit=100)] = {"data": rest}
    listing = spotify.parse_listing(run("https://www.deezer.com/playlist/42")[0])
    assert len(listing.tracks) == 120 and listing.owner == "Someone"
    assert listing.tracks[0].album == "Discovery"  # a playlist row names its own album
    assert listing.cover.endswith("/300x300-000000-80-0-0.jpg")
    assert all("evil" not in url for url, _ in calls)


def test_a_deezer_album_gives_its_songs_its_name_and_release_date(answers):
    table, _ = answers
    table[dz_key("album/302127")] = {
        "id": 302127,
        "title": "Discovery",
        "release_date": "2001-03-07",
        "nb_tracks": 2,
        "artist": {"name": "Daft Punk"},
        "cover_medium": DZ250,
        "tracks": {"data": [dz_track(3135553, "One More Time"), dz_track(3135556, "Harder")]},
    }
    listing = spotify.parse_listing(run("https://www.deezer.com/album/302127")[0])
    assert [t.title for t in listing.tracks] == ["One More Time", "Harder"]
    assert {t.date for t in listing.tracks} == {"2001-03-07"}
    assert listing.owner == "Daft Punk"


def test_a_deezer_track_names_every_contributor(answers):
    table, _ = answers
    table[dz_key("track/3135556")] = dz_track(
        3135556,
        "Harder",
        release_date="2001-03-12",
        contributors=[{"name": "Daft Punk"}, {"name": "Guest"}],
    )
    listing = spotify.parse_listing(run("https://www.deezer.com/track/3135556")[0])
    assert listing.tracks[0].artists == ("Daft Punk", "Guest")
    assert listing.tracks[0].date == "2001-03-12"


@pytest.mark.parametrize(
    ("error", "expected"),
    [({"code": 800, "message": "no data"}, "404"), ({"code": 4, "message": "Quota"}, "429")],
)
def test_deezer_errors_arrive_as_200_and_are_still_errors(answers, error, expected):
    table, _ = answers
    table[dz_key("track/1")] = {"error": error}
    with pytest.raises(EngineError) as info:
        run("https://www.deezer.com/track/1")
    assert expected in info.value.message


def test_hostile_text_in_a_lookup_is_cleaned(answers):
    table, _ = answers
    table[dz_key("track/7")] = dz_track(7, "Bad‮\x00 http://evil.example/x title")
    title = spotify.parse_listing(run("https://www.deezer.com/track/7")[0]).tracks[0].title
    assert "‮" not in title and "\x00" not in title and "evil" not in title


# ── match and download ────────────────────────────────────────────────────────────────────
def catalog_track(**extra):
    fields = {
        "track_id": "3135556",
        "index": 1,
        "title": "Harder, Better, Faster, Stronger",
        "artists": ("Daft Punk",),
        "album": "Discovery",
        "duration": 226.0,
        "art": DZ300,
        "service": "deezer",
        "date": "2001-03-12",
    }
    fields.update(extra)
    return spotify.SpotifyTrack(**fields)


def test_a_match_uses_the_song_core_sent_and_makes_no_lookup(answers, monkeypatch):
    _, calls = answers
    seen = {}

    def find(fields, emit, candidates=None):
        seen.update(fields)
        candidates.append({"video_id": "dQw4w9WgXcQ", "title": "Other", "kind": "song"})
        return {
            "video_id": VID, "title": "Harder, Better, Faster, Stronger", "channel": "Daft Punk",
            "duration": 227.0, "score": 90.0, "method": "album", "album": "Discovery",
        }  # fmt: skip

    monkeypatch.setattr(spotdl, "find_match", find)
    track = catalog_track()
    spec = spotify.match_spec(track)
    assert spec.engine == "music"
    result, _ = run(spec.url, spec.options)
    assert calls == []
    assert seen["name"] == track.title and seen["album_name"] == "Discovery"
    assert seen["duration"] == 226.0 and seen["artists"] == ["Daft Punk"]
    match = spotify.parse_match(result, track)
    assert match.method == "album" and not spotify.is_uncertain(match)
    assert [c.video_id for c in match.candidates] == ["dQw4w9WgXcQ"]


@pytest.mark.parametrize(
    "song",
    [
        {"title": "x", "artists": ["a"], "extra": 1},
        {"title": "", "artists": ["a"]},
        {"title": "x", "artists": "a"},
        {"title": "x", "artists": ["a"], "duration": -1},
        {"title": "x", "artists": ["a"], "date": "yesterday"},
        {"title": "x", "artists": ["a"], "art": "https://evil.example/300x300bb.jpg"},
        {"title": "x", "artists": ["a"], "explicit": "no"},
    ],
)
def test_a_malformed_song_is_refused(song):
    with pytest.raises(EngineError) as info:
        run("https://www.deezer.com/track/1", {"mode": "match", "song": song})
    assert info.value.code == "bad_options"


def test_match_and_download_work_on_one_song_only():
    with pytest.raises(EngineError):
        song = {"title": "x", "artists": []}
        run("https://www.deezer.com/album/1", {"mode": "match", "song": song})


def test_a_download_is_tagged_from_the_service_and_archived_under_its_name(
    monkeypatch, tmp_path
):
    from stuff_downloader_worker.engines import ytdlp

    def fake_audio(self, job, emit):
        path = Path(job.output_dir) / "yt.mp3"
        path.write_bytes(b"ID3")
        return {"files": [str(path)]}

    written = {}
    monkeypatch.setattr(ytdlp.YtDlpEngine, "download", fake_audio)
    monkeypatch.setattr(spotdl, "fetch_cover", lambda url, allowed=None: written.setdefault(
        "cover", (url, allowed(url))) and None)  # fmt: skip
    monkeypatch.setattr(
        spotdl, "tag_mp3", lambda path, fields, cover: written.setdefault("fields", fields) or {}
    )
    track = catalog_track()
    [spec] = spotify.batch_specs([track], {}, str(tmp_path), archive=True)
    monkeypatch.setattr(
        spotdl, "find_match",
        lambda fields, emit, candidates=None: {"video_id": VID, "album": "Discovery"},
    )  # fmt: skip
    result, _ = run(spec.url, spec.options, str(tmp_path))
    assert Path(result["files"][0]).name == "Daft Punk - Harder, Better, Faster, Stronger.mp3"
    fields = written["fields"]
    assert fields["url"] == "https://www.deezer.com/track/3135556"
    assert fields["date"] == "2001-03-12" and fields["year"] == "2001"
    assert written["cover"] == (DZ1000, DZ1000)  # the 1000 px cover, vetted
    archive = (tmp_path / spotdl.ARCHIVE_FILENAME).read_text(encoding="utf-8")
    assert archive == "deezer 3135556\n"
    again, _ = run(spec.url, spec.options, str(tmp_path))
    assert again["skipped"] is True


def test_the_same_id_on_another_service_is_not_archived(tmp_path):
    (tmp_path / spotdl.ARCHIVE_FILENAME).write_text("apple 3135556\n", encoding="utf-8")
    assert "3135556" not in spotdl.Archive(tmp_path, "deezer")
    assert "3135556" in spotdl.Archive(tmp_path, "apple")
