"""Worker trust boundary for playlists: option validation, path safety and entry sanitizing."""

from __future__ import annotations

import pytest

from stuff_downloader_worker import presets
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.engines.ytdlp import (
    MAX_ENTRY_TEXT,
    MAX_PLAYLIST_ENTRIES,
    sanitize_entry,
    sanitize_playlist,
)

VID = "dQw4w9WgXcQ"


def options(**extra):
    return {"mode": "download", "preset": "mp3_music", **extra}


# ── option validation ────────────────────────────────────────────────────────────────────
def test_playlist_options_are_accepted_and_typed():
    request = presets.parse_request(
        options(playlist_index=7, playlist_count=24, playlist_title="Chill", archive=True)
    )
    assert request.playlist_index == 7 and request.playlist_count == 24
    assert request.playlist_title == "Chill" and request.archive is True
    assert request.in_playlist


def test_a_single_video_job_is_unchanged():
    request = presets.parse_request(options())
    assert not request.in_playlist and request.archive is False
    assert presets.build_ydl_opts(request, "C:/dl")["outtmpl"] == presets.MUSIC_OUTTMPL
    assert "download_archive" not in presets.build_ydl_opts(request, "C:/dl")


def test_unknown_options_are_still_refused():
    with pytest.raises(EngineError, match="unknown options"):
        presets.parse_request(options(download_archive="C:/anywhere.txt"))
    with pytest.raises(EngineError, match="unknown options"):
        presets.parse_request(options(postprocessor_args=["-vf", "evil"]))


@pytest.mark.parametrize(
    "value", [0, -1, presets.MAX_PLAYLIST_INDEX + 1, "3", 3.0, True, None_ := object()]
)
def test_a_bad_playlist_index_is_refused(value):
    with pytest.raises(EngineError, match="playlist_index"):
        presets.parse_request(options(playlist_index=value))


def test_archive_must_be_a_boolean():
    with pytest.raises(EngineError, match="archive"):
        presets.parse_request(options(archive="yes"))


def test_a_playlist_title_alone_is_refused_and_a_huge_one_is_rejected():
    with pytest.raises(EngineError, match="playlist_index"):
        presets.parse_request(options(playlist_title="Chill"))
    with pytest.raises(EngineError, match="too long"):
        presets.parse_request(options(playlist_index=1, playlist_title="x" * 5000))
    with pytest.raises(EngineError, match="playlist_title"):
        presets.parse_request(options(playlist_index=1, playlist_title=42))


# ── paths ────────────────────────────────────────────────────────────────────────────────
def test_playlist_music_names_have_no_track_number_prefix():
    assert presets.track_prefix(7, 24) == "07 - "
    assert presets.track_prefix(7, 500) == "007 - "
    assert presets.track_prefix(7, None) == "07 - "
    request = presets.parse_request(
        options(playlist_index=7, playlist_count=24, playlist_title="Chill")
    )
    opts = presets.build_ydl_opts(request, "C:/dl")
    assert opts["outtmpl"] == presets.MUSIC_OUTTMPL
    assert not opts["outtmpl"].startswith("07 - ")
    assert request.playlist_index == 7 and request.playlist_count == 24
    assert any(pp["key"] == "FFmpegMetadata" for pp in opts["postprocessors"])


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Chill Mix", "Chill Mix"),
        ("../../Windows/System32", "_.._Windows_System32"),  # separators gone, leading dots gone
        ("C:/evil", "C__evil"),
        ("a" + chr(0) + "b", "a_b"),
        ("CON", "Playlist"),
        ("nul.txt", "Playlist"),
        ("   ", "Playlist"),
        ("...", "Playlist"),
        ("", "Playlist"),
    ],
)
def test_an_untrusted_playlist_title_never_escapes_one_folder(title, expected):
    assert presets.safe_folder_name(title) == expected


def test_a_long_title_is_trimmed():
    assert len(presets.safe_folder_name("x" * 400)) == presets.MAX_FOLDER_NAME


def test_playlist_files_and_the_archive_stay_under_the_download_folder(tmp_path):
    request = presets.parse_request(
        options(playlist_index=1, playlist_title="../../escape", archive=True)
    )
    opts = presets.build_ydl_opts(request, str(tmp_path))
    home = opts["paths"]["home"]
    assert home.startswith(str(tmp_path))
    assert home == str(tmp_path / "_.._escape")
    assert opts["download_archive"] == str(tmp_path / "_.._escape" / presets.ARCHIVE_FILENAME)


def test_the_archive_path_never_comes_from_the_job_spec(tmp_path):
    request = presets.parse_request(options(archive=True))
    assert presets.build_ydl_opts(request, str(tmp_path))["download_archive"] == str(
        tmp_path / presets.ARCHIVE_FILENAME
    )


# ── entry sanitizing ─────────────────────────────────────────────────────────────────────
def test_an_entry_url_is_rebuilt_never_passed_through():
    row = sanitize_entry(
        {
            "id": VID,
            "title": "Song",
            "url": "https://evil.example/x",
            "webpage_url": "javascript:alert(1)",
        },
        1,
    )
    assert row["url"] == f"https://www.youtube.com/watch?v={VID}"
    assert "webpage_url" not in row


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "tooshort"},
        {"id": "waaaaaaaaaaaaay-too-long"},
        {"id": "../../../etc"},
        {"id": 12345},
        {"title": "no id at all"},
        "not a dict",
        None,
    ],
)
def test_entries_without_a_valid_video_id_are_dropped(entry):
    assert sanitize_entry(entry, 1) is None


def test_hostile_text_and_durations_are_bounded():
    row = sanitize_entry({"id": VID, "title": "t" * 9000, "duration": -5}, 1)
    assert len(row["title"]) == MAX_ENTRY_TEXT and "duration" not in row
    assert sanitize_entry({"id": VID, "duration": "soon"}, 1).get("duration") is None
    assert sanitize_entry({"id": VID}, 1)["title"] == "Untitled"


@pytest.mark.parametrize(
    "entry,reason",
    [
        ({"id": VID, "availability": "private"}, "Private video"),
        ({"id": VID, "availability": "premium_only"}, "Members or Premium only"),
        ({"id": VID, "availability": "something_new"}, "Not available"),
        ({"id": VID, "live_status": "is_upcoming"}, "Not available yet"),
        ({"id": VID, "title": "[Deleted video]"}, "Deleted video"),
    ],
)
def test_unavailable_entries_are_listed_with_a_reason_not_dropped(entry, reason):
    row = sanitize_entry(entry, 1)
    assert row is not None and row["unavailable"] == reason


def test_a_normal_entry_carries_no_unavailable_flag():
    assert "unavailable" not in sanitize_entry({"id": VID, "availability": "public"}, 1)


def test_sanitize_playlist_renumbers_and_caps():
    entries = [{"id": "bad"}] + [{"id": VID} for _ in range(MAX_PLAYLIST_ENTRIES + 10)]
    listing = sanitize_playlist({"id": "PL1", "title": "Mix", "entries": entries})
    assert listing["kind"] == "playlist" and listing["playlist_id"] == "PL1"
    assert len(listing["entries"]) == MAX_PLAYLIST_ENTRIES - 1  # the invalid row is dropped
    assert [e["index"] for e in listing["entries"][:3]] == [1, 2, 3]
    assert listing["truncated"] is True


def test_sanitize_playlist_survives_a_junk_payload():
    listing = sanitize_playlist({})
    assert listing["entries"] == [] and listing["title"] == "Playlist"
    assert listing["playlist_id"] == "" and listing["truncated"] is False


# ── R5: metadata comes from the track, never from the list (plan §5.7) ───────────────────
def _tags(path):
    from mutagen.id3 import ID3

    tags = ID3(str(path))
    return {k: str(tags[k]) for k in ("TIT2", "TPE1", "TALB", "TRCK") if tags.getall(k)}


def test_a_playlist_download_keeps_the_real_album_and_no_list_position(tmp_path):
    pytest.importorskip("mutagen")
    from stuff_downloader_worker import tagging

    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    info = {"track": "Midnight City", "artist": "M83", "album": "Hurry Up, We're Dreaming"}
    report = tagging.verify_mp3(song, info)
    assert _tags(song) == {
        "TIT2": "Midnight City",
        "TPE1": "M83",
        "TALB": "Hurry Up, We're Dreaming",
    }
    assert report["album"] == "Hurry Up, We're Dreaming" and report["track"] is None


def test_an_unknown_album_is_left_empty(tmp_path):
    pytest.importorskip("mutagen")
    from stuff_downloader_worker import tagging

    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    tagging.verify_mp3(song, {"title": "Clip", "uploader": "Someone - Topic"})
    assert _tags(song) == {"TIT2": "Clip", "TPE1": "Someone"}


def test_an_album_download_numbers_the_track(tmp_path):
    pytest.importorskip("mutagen")
    from stuff_downloader_worker import tagging

    song = tmp_path / "song.mp3"
    song.write_bytes(b"")
    tagging.verify_mp3(song, {"title": "Two", "album": "LP"}, track_number=2, track_total=9)
    assert _tags(song)["TRCK"] == "2/9" and _tags(song)["TALB"] == "LP"


@pytest.mark.parametrize(
    "uploader, artist",
    [
        ("Justine Skye - Topic", "Justine Skye"),
        ("M83VEVO", "M83"),
        ("Plain", "Plain"),
        (None, None),
    ],
)
def test_a_youtube_uploader_becomes_a_clean_artist(uploader, artist):
    from stuff_downloader_worker import tagging

    assert tagging.artist_from_uploader(uploader) == artist
    info = {"uploader": uploader}
    tagging.fill_artist(info)
    assert info.get("artist") == artist
    named = {"uploader": "X - Topic", "artist": "Real"}
    tagging.fill_artist(named)
    assert named["artist"] == "Real"  # the site's own artist always wins


def _mp3_run(tmp_path, monkeypatch, request):
    from stuff_downloader_worker.engines import ytdlp

    calls = []
    monkeypatch.setattr(ytdlp.tagging, "verify_mp3", lambda path, info, **k: calls.append(k))
    out = tmp_path / "Artist - Song.mp3"

    class Ydl:
        params = {"outtmpl": {"default": "x"}}

        def in_download_archive(self, info):
            return False

        def prepare_filename(self, info):
            return str(tmp_path / "Artist - Song.webm")

        def process_ie_result(self, info, download):
            out.write_bytes(b"a")
            return {}

    ytdlp.YtDlpEngine._download(
        Ydl(), {"album": "Real"}, request, {"title": "t"}, [str(out)], lambda s: None,
        lambda *a: None,
    )
    return calls[0]


def test_a_playlist_row_download_never_passes_the_list_position_as_a_track(tmp_path, monkeypatch):
    request = presets.parse_row_request(
        {"tab": "audio", "row_id": "a:mp3:320", "playlist_index": 4, "playlist_title": "Mix",
         "playlist_count": 9}
    )
    assert _mp3_run(tmp_path, monkeypatch, request) == {"track_number": None, "track_total": None}


def test_an_album_row_download_passes_its_position(tmp_path, monkeypatch):
    request = presets.parse_row_request(
        {"tab": "audio", "row_id": "a:mp3:320", "playlist_index": 4, "playlist_title": "LP",
         "playlist_count": 9, "album_order": True}
    )
    assert _mp3_run(tmp_path, monkeypatch, request) == {"track_number": 4, "track_total": 9}


def test_row_playlist_fields_are_validated():
    base = {"tab": "audio", "row_id": "a:mp3:320"}
    with pytest.raises(EngineError, match="album_order"):
        presets.parse_row_request({**base, "album_order": True})
    with pytest.raises(EngineError, match="needs 'playlist_index'"):
        presets.parse_row_request({**base, "playlist_title": "Mix"})
    with pytest.raises(EngineError, match="unknown options"):
        presets.parse_row_request({**base, "playlist_index": 1}, original_only=True)
    request = presets.parse_row_request(base)
    assert not request.in_playlist and not request.album_order


def test_a_playlist_row_lands_in_the_list_folder_with_its_archive(tmp_path):
    request = presets.parse_row_request(
        {"tab": "video", "row_id": "v:1080:mp4", "container": "mp4", "playlist_index": 1,
         "playlist_title": "Road/Trip", "archive": True}
    )
    opts = presets.build_row_opts(request, str(tmp_path))
    home = tmp_path / "Road_Trip"
    assert opts["paths"]["home"] == str(home)
    assert opts["download_archive"] == str(home / presets.ARCHIVE_FILENAME)


@pytest.mark.parametrize(
    "url, list_id, music, album",
    [
        ("https://www.youtube.com/playlist?list=PL1", "PL1", False, False),
        ("https://music.youtube.com/playlist?list=PL1", "PL1", True, False),
        ("https://music.youtube.com/playlist?list=OLAK5uy_abc", "OLAK5uy_abc", True, True),
        ("https://www.youtube.com/playlist?list=OLAK5uy_abc", "OLAK5uy_abc", True, True),
        ("https://www.youtube.com/playlist?list=RDCLAKabc", "RDCLAKabc", True, False),
    ],
)
def test_a_listing_says_whether_it_is_songs_and_whether_it_is_an_album(url, list_id, music, album):
    listing = sanitize_playlist({"id": list_id, "title": "L", "entries": []}, url)
    assert (listing["music"], listing["is_album"]) == (music, album)
