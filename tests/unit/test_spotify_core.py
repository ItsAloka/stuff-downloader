"""core.spotify: what a Spotify listing, match and batch may look like (plan §7)."""

from __future__ import annotations

import pytest

from stuff_downloader.core import spotify
from stuff_downloader.core.protocol import JobSpec

T1 = "6OmhkSOpvYBokMKQxpIGx2"
T2 = "2iblMMIgSznA464mNov7A8"
ALBUM = "4aawyAB9vmqN3uQ7FjRGTy"
VID = "q7xBoh0emqo"


def row(track_id=T1, **extra):
    return {
        "id": track_id,
        "title": "Global Warming",
        "artists": ["Pitbull", "Sensato"],
        "album": "Global Warming",
        "duration": 85.0,
        "explicit": True,
        **extra,
    }


def listing_data(**extra):
    return {
        "kind": "spotify",
        "spotify_kind": "album",
        "spotify_id": ALBUM,
        "title": "Global Warming",
        "owner": "Pitbull",
        "tracks": [row(), row(T2, title="Don't Stop the Party", explicit=False)],
        **extra,
    }


# ── listing ───────────────────────────────────────────────────────────────────────────────
def test_a_listing_keeps_order_and_rebuilds_each_track_url():
    listing = spotify.parse_listing(listing_data())
    assert (listing.kind, listing.spotify_id, listing.title, listing.owner) == (
        "album",
        ALBUM,
        "Global Warming",
        "Pitbull",
    )
    first, second = listing.tracks
    assert (first.index, second.index) == (1, 2)
    assert first.url == f"https://open.spotify.com/track/{T1}"
    assert first.artist == "Pitbull, Sensato" and first.duration == 85.0 and first.explicit
    assert not second.explicit


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "string",
        {"id": "short"},
        {"id": f"{T1}x"},
        {"id": "../../../etc/passwd/aa"},
        {"id": 12345},
    ],
)
def test_rows_without_a_real_track_id_are_dropped_and_counted(bad):
    listing = spotify.parse_listing(listing_data(tracks=[bad, row()]))
    assert [t.track_id for t in listing.tracks] == [T1]
    assert listing.tracks[0].index == 1  # positions are the listing's, not the payload's
    assert listing.skipped == 1


def test_a_duplicate_track_is_listed_once():
    listing = spotify.parse_listing(listing_data(tracks=[row(), row()]))
    assert len(listing.tracks) == 1 and listing.skipped == 1


def test_hostile_text_is_bounded_and_flattened():
    long = "x" * 5000
    listing = spotify.parse_listing(
        listing_data(
            title="a\nb\r\n\tc",
            tracks=[row(title=long, artists=[long] * 100 + [7, None], album=["not", "text"])],
        )
    )
    track = listing.tracks[0]
    assert listing.title == "a b c"
    assert len(track.title) == spotify.MAX_TEXT
    assert len(track.artists) == spotify.MAX_ARTISTS
    assert all(len(a) == spotify.MAX_TEXT for a in track.artists)
    assert track.album == ""


@pytest.mark.parametrize("duration", [-1, True, "85", float("inf"), 10**9])
def test_an_impossible_duration_is_unknown(duration):
    listing = spotify.parse_listing(listing_data(tracks=[row(duration=duration)]))
    assert listing.tracks[0].duration is None


def test_a_listing_is_capped_and_says_so():
    tracks = [row(f"{i:022d}") for i in range(spotify.MAX_TRACKS + 5)]
    listing = spotify.parse_listing(listing_data(tracks=tracks))
    assert len(listing.tracks) == spotify.MAX_TRACKS and listing.truncated


@pytest.mark.parametrize(
    "data", [None, [], {"spotify_kind": "artist", "spotify_id": "x", "tracks": "nope"}]
)
def test_garbage_is_an_empty_listing_not_a_crash(data):
    listing = spotify.parse_listing(data)
    assert listing.tracks == () and listing.kind == "" and listing.spotify_id == ""


# ── match ─────────────────────────────────────────────────────────────────────────────────
def match_data(**extra):
    return {
        "kind": "spotify_match",
        "track_id": T1,
        "video_id": VID,
        "title": "Pitbull - Global Warming ft. Sensato",
        "channel": "PitbullVEVO",
        "duration": 88.0,
        "confidence": 97.456,
        **extra,
    }


def track():
    return spotify.parse_listing(listing_data()).tracks[0]


def test_a_match_is_rebuilt_from_its_video_id_with_core_computed_duration_diff():
    m = spotify.parse_match(match_data(duration_diff=-999, url="https://evil.example/"), track())
    assert m is not None and m.video_id == VID and not m.manual
    assert m.url == f"https://music.youtube.com/watch?v={VID}"
    assert m.duration_diff == 3.0  # 88 - 85, never the payload's own figure
    assert m.confidence == 97.5


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({"confidence": 250}, 100.0), ({"confidence": -3}, 0.0), ({"confidence": True}, None)],
)
def test_confidence_is_clamped_or_unknown(extra, expected):
    assert spotify.parse_match(match_data(**extra), track()).confidence == expected


@pytest.mark.parametrize(
    "extra",
    [
        {"track_id": T2},  # an answer for a different row must not land on this one
        {"video_id": "not-an-id"},
        {"video_id": f"{VID}&list=x"},
        {"video_id": None},
    ],
)
def test_an_unusable_match_is_none(extra):
    assert spotify.parse_match(match_data(**extra), track()) is None


def test_an_unknown_duration_gives_no_diff():
    assert spotify.parse_match(match_data(duration=None), track()).duration_diff is None


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com/watch?v={VID}&t=30s&si=track",
        f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
        f"https://youtu.be/{VID}",
    ],
)
def test_a_pasted_youtube_link_becomes_a_manual_match(text):
    m = spotify.override_match(text, track())
    assert m.video_id == VID and m.manual and m.track_id == T1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a link",
        "https://www.youtube.com/playlist?list=PL1234567890abcdef",
        "https://vimeo.com/123456",
        f"https://open.spotify.com/track/{T1}",
        f"file:///C:/{VID}.mp3",
    ],
)
def test_anything_but_one_youtube_video_is_refused_as_an_override(text):
    with pytest.raises(ValueError, match="YouTube"):
        spotify.override_match(text, track())


# ── specs ─────────────────────────────────────────────────────────────────────────────────
def test_batch_specs_are_one_ordinary_job_per_ticked_track():
    listing = spotify.parse_listing(listing_data(tracks=[row(album=""), row(T2, album="")]))
    first, second = listing.tracks
    specs = spotify.batch_specs(
        [first, second], {T1: spotify.Match(T1, VID, album="Global Warming")}, "C:/Music"
    )
    assert [s.engine for s in specs] == ["spotdl", "spotdl"]
    assert [s.url for s in specs] == [first.url, second.url]
    # A playlist row's album is the matched YouTube Music song's; no track number.
    assert specs[0].options == {
        "mode": "download",
        "preset": "spotify_mp3",
        "archive": True,
        "video_id": VID,
        "album": "Global Warming",
    }
    # No reviewed match: the worker searches for itself.
    assert specs[1].options == {"mode": "download", "preset": "spotify_mp3", "archive": True}
    assert len({s.job_id for s in specs}) == 2
    for spec in specs:  # every spec survives the wire
        assert JobSpec.from_json(spec.to_json()) == spec


def test_an_album_link_carries_its_own_album_and_track_numbers():
    listing = spotify.parse_listing(listing_data())
    specs = spotify.batch_specs(
        list(listing.tracks), {T2: spotify.Match(T2, VID, album="Other")}, "C:/M", album_order=True
    )
    assert [s.options["album"] for s in specs] == ["Global Warming", "Global Warming"]
    assert [s.options["album_track"] for s in specs] == [1, 2]


@pytest.mark.parametrize("number", [0, spotify.MAX_TRACKS + 1, True, "1"])
def test_a_bad_track_number_never_reaches_a_spec(number):
    with pytest.raises(ValueError):
        spotify.download_options(album_track=number)


def test_a_bad_video_id_never_reaches_a_spec():
    with pytest.raises(ValueError):
        spotify.download_options(video_id="x; rm -rf")


def test_a_match_spec_is_a_short_job_on_one_track_naming_its_album():
    spec = spotify.match_spec(track())
    assert spec.engine == "spotdl" and spec.url == f"https://open.spotify.com/track/{T1}"
    assert spec.options == {"mode": "match", "album": "Global Warming"}
    playlist_row = spotify.parse_listing(listing_data(tracks=[row(album="")])).tracks[0]
    assert spotify.match_spec(playlist_row).options == {"mode": "match"}


# ── artwork (plan §7 item 2) ──────────────────────────────────────────────────────────────
ART = "https://i.scdn.co/image/ab67616d00001e02" + "c" * 24


def test_listing_art_and_cover_are_kept_only_in_their_normalized_form():
    listing = spotify.parse_listing(listing_data(cover=ART, tracks=[row(art=ART), row(T2)]))
    first, second = listing.tracks
    assert listing.cover == ART and first.art == ART and first.art_url == ART
    # No picture in the listing: the row looks its own up through oEmbed, by its id.
    assert second.art == "" and second.art_url == spotify.oembed_url(T2)
    assert second.art_url == (
        f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{T2}"
    )


@pytest.mark.parametrize(
    "art",
    [
        "https://image-cdn-ak.spotifycdn.com/image/ab67616d00001e02" + "c" * 24,
        "http://i.scdn.co/image/ab67616d00001e02" + "c" * 24,
        ART + "?x=1",
        "https://evil.example/cover.jpg",
        "https://i.scdn.co/image/../../x",
        5,
    ],
)
def test_any_other_picture_url_is_dropped(art):
    listing = spotify.parse_listing(listing_data(cover=art, tracks=[row(art=art)]))
    assert listing.cover == "" and listing.tracks[0].art == ""


def test_an_oembed_lookup_is_only_built_from_a_real_track_id():
    with pytest.raises(ValueError):
        spotify.oembed_url("../x")


# ── how a match was found (plan §7 items 3-4) ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "uncertain"), [("album", False), ("song", False), ("video", True)]
)
def test_only_a_video_match_is_uncertain_by_how_it_was_found(method, uncertain):
    m = spotify.parse_match(match_data(duration=86.0, method=method), track())
    assert m.method == method and spotify.is_uncertain(m) is uncertain


@pytest.mark.parametrize("method", [None, "spotdl", 5])
def test_an_unknown_method_reads_as_a_video(method):
    assert spotify.parse_match(match_data(method=method), track()).method == "video"


def test_a_match_keeps_the_album_it_was_found_on_as_plain_text():
    m = spotify.parse_match(match_data(album="Global\n  Warming", method="song"), track())
    assert m.album == "Global Warming"


# ── Apple Music and Deezer (plan §7 "Other music sites") ──────────────────────────────────
DZ_ART = (
    "https://cdn-images.dzcdn.net/images/cover/5718f7c81c27e0b2417e2a4c45224f8a/"
    "300x300-000000-80-0-0.jpg"
)


def deezer_listing(**extra):
    return {
        "service": "deezer",
        "catalog_kind": "album",
        "catalog_id": "302127",
        "title": "Discovery",
        "cover": DZ_ART,
        "tracks": [
            {"id": "3135553", "title": "One More Time", "artists": ["Daft Punk"],
             "album": "Discovery", "duration": 320.0, "date": "2001-03-07", "art": DZ_ART},
            {"id": "6OmhkSOpvYBokMKQxpIGx2", "title": "A Spotify id is not a Deezer id"},
            {"id": "3135556", "title": "Harder", "date": 2001, "art": "https://evil.example/x.jpg"},
        ],
        **extra,
    }  # fmt: skip


def test_a_deezer_listing_keeps_its_service_numeric_ids_and_pictures():
    listing = spotify.parse_listing(deezer_listing())
    assert (listing.service, listing.kind, listing.spotify_id) == ("deezer", "album", "302127")
    assert listing.site == "Deezer" and listing.cover == DZ_ART
    assert [t.track_id for t in listing.tracks] == ["3135553", "3135556"]
    assert listing.skipped == 1
    first, second = listing.tracks
    assert first.url == "https://www.deezer.com/track/3135553" and first.engine == "music"
    assert first.art_url == DZ_ART and first.date == "2001-03-07"
    assert second.art == "" and second.art_url == "" and second.date == ""  # no oEmbed guess


def test_a_listing_naming_an_unknown_service_is_read_as_spotify():
    listing = spotify.parse_listing(deezer_listing(service="tidal"))
    assert listing.service == "spotify"
    assert [t.track_id for t in listing.tracks] == ["6OmhkSOpvYBokMKQxpIGx2"]  # no numeric ids


def test_catalog_jobs_carry_the_checked_song_and_go_to_the_music_engine():
    track = spotify.parse_listing(deezer_listing()).tracks[0]
    match = spotify.match_spec(track)
    assert match.engine == "music" and match.url == track.url
    assert match.options == {
        "mode": "match",
        "album": "Discovery",
        "song": {
            "title": "One More Time", "artists": ["Daft Punk"], "explicit": False,
            "duration": 320.0, "date": "2001-03-07", "art": DZ_ART,
        },
    }  # fmt: skip
    [download] = spotify.batch_specs([track], {}, "out", album_order=True)
    assert download.engine == "music"
    assert download.options["song"] == match.options["song"]
    assert download.options["album_track"] == 1


def test_spotify_jobs_are_unchanged_and_carry_no_song():
    track = spotify.SpotifyTrack("6OmhkSOpvYBokMKQxpIGx2", 1, "Song")
    assert "song" not in spotify.match_spec(track).options
    assert spotify.match_spec(track).engine == "spotdl"
