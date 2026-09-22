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
