"""Playlist routing, listing sanitizing in core, and the per-item jobs a batch expands into."""

from __future__ import annotations

import pytest

from stuff_downloader.core import playlist, presets
from stuff_downloader.core.router import route

VID = "dQw4w9WgXcQ"
LIST = "PLabc123_-XYZ"


# ── routing ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        (f"https://www.youtube.com/playlist?list={LIST}", f"https://www.youtube.com/playlist?list={LIST}"),
        (f"https://music.youtube.com/playlist?list={LIST}&si=x", f"https://music.youtube.com/playlist?list={LIST}"),
        ("https://m.youtube.com/playlist?list=OLAK5uy_abcdefghij", "https://www.youtube.com/playlist?list=OLAK5uy_abcdefghij"),
    ],
)
def test_playlist_links_normalize_to_a_clean_playlist_url(text, expected):
    r = route(text)
    assert r.ok and r.is_playlist and r.engine == "ytdlp"
    assert r.url == expected and r.playlist_url == expected
    assert not r.video_id


@pytest.mark.parametrize(
    "list_id,needle",
    [
        ("WL", "private"),
        ("LL", "private"),
        ("RDabcdefghijkl", "radio"),
        ("PLaMIXbcdefghij", "radio"),
        ("PLabc", "No playlist"),
        ("", "No playlist"),
        ("../../etc", "No playlist"),
    ],
)
def test_undownloadable_playlists_are_refused_by_name(list_id, needle):
    r = route(f"https://www.youtube.com/playlist?list={list_id}")
    assert r.kind == "unsupported" and not r.ok
    assert needle.lower() in r.reason.lower()


def test_watch_with_list_offers_both_the_song_and_the_playlist():
    r = route(f"https://www.youtube.com/watch?v={VID}&list={LIST}&index=3")
    assert r.ok and not r.is_playlist
    assert r.url == f"https://www.youtube.com/watch?v={VID}"
    assert r.playlist_id == LIST
    assert r.playlist_url == f"https://www.youtube.com/playlist?list={LIST}"
    assert not r.playlist_reason


def test_watch_inside_a_radio_mix_explains_why_there_is_no_playlist_option():
    r = route(f"https://www.youtube.com/watch?v={VID}&list=RDabcdefghijkl")
    assert r.ok and not r.playlist_id and not r.playlist_url
    assert "radio" in r.playlist_reason.lower()


def test_music_playlist_keeps_the_music_host():
    r = route(f"https://music.youtube.com/playlist?list={LIST}")
    assert r.music and r.url.startswith("https://music.youtube.com/")


# ── listings ─────────────────────────────────────────────────────────────────────────────
def listing_payload(**overrides):
    payload = {
        "kind": "playlist",
        "playlist_id": LIST,
        "title": "Chill Mix",
        "uploader": "Someone",
        "entries": [
            {"id": VID, "title": "First", "uploader": "A", "duration": 210, "index": 1},
            {"id": "aaaaaaaaaaa", "title": "[Private video]", "unavailable": "Private video"},
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_listing_keeps_unavailable_entries_but_marks_them_unselectable():
    listing = playlist.parse_listing(listing_payload())
    assert listing.title == "Chill Mix" and len(listing.entries) == 2
    assert [e.index for e in listing.entries] == [1, 2]
    assert listing.entries[0].selectable
    assert not listing.entries[1].selectable
    assert len(listing.selectable) == 1


def test_parse_listing_rebuilds_every_url_from_the_video_id():
    payload = listing_payload(
        entries=[{"id": VID, "title": "x", "url": "https://evil.example/pwn"}]
    )
    assert playlist.parse_listing(payload).entries[0].url == (
        f"https://www.youtube.com/watch?v={VID}"
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "short", "title": "x"},
        {"id": "../../../etc/passwd", "title": "x"},
        {"title": "no id"},
        "not a dict",
        None,
    ],
)
def test_parse_listing_drops_entries_without_a_usable_video_id(entry):
    assert playlist.parse_listing(listing_payload(entries=[entry])).entries == ()


def test_parse_listing_caps_the_number_of_entries():
    entries = [{"id": VID, "title": f"t{i}"} for i in range(playlist.MAX_ENTRIES + 25)]
    assert len(playlist.parse_listing(listing_payload(entries=entries)).entries) == (
        playlist.MAX_ENTRIES
    )


def test_parse_listing_truncates_long_text_and_survives_junk():
    payload = listing_payload(
        title="T" * 5000,
        entries=[{"id": VID, "title": "x" * 5000, "duration": "soon"}],
    )
    listing = playlist.parse_listing(payload)
    assert len(listing.title) == playlist.MAX_TEXT
    assert len(listing.entries[0].title) == playlist.MAX_TEXT
    assert listing.entries[0].duration is None
    assert playlist.parse_listing("nonsense").entries == ()
    assert playlist.parse_listing({}).title == "Playlist"


# ── batches ──────────────────────────────────────────────────────────────────────────────
def test_batch_specs_number_each_item_and_carry_the_playlist_name():
    listing = playlist.parse_listing(
        listing_payload(entries=[{"id": VID, "title": f"t{i}"} for i in range(3)])
    )
    specs = playlist.batch_specs(listing, list(listing.entries), "C:/dl", "mp3_music")
    assert len(specs) == 3
    assert len({s.job_id for s in specs}) == 3  # each item is its own job
    assert [s.options["playlist_index"] for s in specs] == [1, 2, 3]
    assert all(s.options["playlist_title"] == "Chill Mix" for s in specs)
    assert all(s.options["playlist_count"] == 3 for s in specs)
    assert all(s.options["archive"] is True for s in specs)
    assert all(s.engine == "ytdlp" and s.output_dir == "C:/dl" for s in specs)


def test_batch_specs_can_re_download_ignoring_the_archive():
    listing = playlist.parse_listing(listing_payload())
    specs = playlist.batch_specs(
        listing, [listing.entries[0]], "C:/dl", "mp3_music", archive=False
    )
    assert "archive" not in specs[0].options


def test_batch_options_match_what_a_single_video_job_sends():
    listing = playlist.parse_listing(listing_payload())
    spec = playlist.batch_specs(listing, [listing.entries[0]], "C:/dl", "mp3_music")[0]
    single = presets.download_options("mp3_music")
    assert set(spec.options) - set(single) == {
        "playlist_index",
        "playlist_title",
        "playlist_count",
        "archive",
    }


def test_core_refuses_an_out_of_range_playlist_index():
    with pytest.raises(ValueError, match="out of range"):
        presets.download_options("mp3_music", playlist_index=0)
    with pytest.raises(ValueError, match="out of range"):
        presets.download_options("mp3_music", playlist_index=presets.MAX_PLAYLIST_INDEX + 1)
