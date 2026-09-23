"""core.spotify: what a Spotify listing, match and batch may look like (plan §6.3)."""

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


def test_listing_keeps_only_spotify_cover_urls():
    data = listing_data(
        tracks=[
            row(cover_url="https://i.scdn.co/image/abc"),
            row(T2, cover_url="https://evil.example/x"),
        ]
    )
    first, second = spotify.parse_listing(data).tracks
    assert first.cover_url == "https://i.scdn.co/image/abc"
    assert second.cover_url is None


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


def test_wrong_recording_version_is_uncertain_despite_matching_artist_and_length():
    match = spotify.parse_match(match_data(title="Global Warming (Live)", duration=85), track())
    assert match is not None and spotify.is_uncertain(match)
    assert match.score < spotify.UNCERTAIN_SCORE


def test_missing_score_evidence_needs_review():
    assert spotify.is_uncertain(spotify.Match(T1, VID))


def test_unknown_duration_needs_review_even_with_a_high_score():
    assert spotify.is_uncertain(spotify.Match(T1, VID, score=99))


def test_artist_in_title_does_not_verify_a_third_party_channel():
    track_data = spotify.SpotifyTrack(T1, 1, "告白氣球", ("Jay Chou",), duration=215.1)
    score = spotify.match_score(
        track_data, "周杰倫 Jay Chou - 告白氣球【歌詞版】", "EnjoyLife", 216.0
    )
    assert score < spotify.UNCERTAIN_SCORE
    assert spotify.match_score(track_data, "告白氣球", "EnjoyLife", 216.0) < spotify.UNCERTAIN_SCORE


def test_artist_vevo_and_topic_channels_remain_high_confidence():
    original = track()
    assert spotify.match_score(original, original.title, "PitbullVEVO", original.duration) >= 95
    assert spotify.match_score(original, original.title, "Pitbull - Topic", original.duration) >= 95


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
    listing = spotify.parse_listing(listing_data())
    first, second = listing.tracks
    other = "dQw4w9WgXcQ"
    specs = spotify.batch_specs(
        [first, second],
        {T1: spotify.Match(T1, VID, manual=True), T2: spotify.Match(T2, other, manual=True)},
        "C:/Music", archive=True,
    )
    assert [s.engine for s in specs] == ["spotdl", "spotdl"]
    assert [s.url for s in specs] == [first.url, second.url]
    assert specs[0].options == {
        "mode": "download",
        "preset": "spotify_mp3",
        "archive": True,
        "video_id": VID,
    }
    assert specs[1].options["video_id"] == other
    assert len({s.job_id for s in specs}) == 2
    for spec in specs:  # every spec survives the wire
        assert JobSpec.from_json(spec.to_json()) == spec


def test_a_bad_video_id_never_reaches_a_spec():
    with pytest.raises(ValueError):
        spotify.download_options(video_id="x; rm -rf")


def test_batch_specs_carry_only_chosen_file_names():
    first, second = spotify.parse_listing(listing_data()).tracks
    specs = spotify.batch_specs(
        [first, second],
        {T1: spotify.Match(T1, VID, manual=True), T2: spotify.Match(T2, VID, manual=True)},
        "C:/Music", output_names={first.track_id: "My version"},
    )
    assert specs[0].options["output_name"] == "My version"
    assert "output_name" not in specs[1].options


def test_batch_specs_reject_missing_or_unconfirmed_matches():
    first = track()
    with pytest.raises(ValueError, match="no reviewed match"):
        spotify.batch_specs([first], {}, "C:/Music")
    uncertain = spotify.Match(T1, VID, score=60, duration_diff=0)
    with pytest.raises(ValueError, match="needs match confirmation"):
        spotify.batch_specs([first], {T1: uncertain}, "C:/Music")
    specs = spotify.batch_specs(
        [first], {T1: uncertain}, "C:/Music", confirmed_ids={T1}
    )
    assert specs[0].options["video_id"] == VID


def test_a_match_spec_is_a_short_job_on_one_track():
    spec = spotify.match_spec(track())
    assert spec.engine == "spotdl" and spec.url == f"https://open.spotify.com/track/{T1}"
    assert spec.options == {"mode": "match"}
