import json
import re
from pathlib import Path

import pytest

from stuff_downloader.core import errors, formats, presets

FIXTURE = Path(__file__).parent / "fixtures" / "youtube_video.json"


@pytest.fixture(scope="module")
def info():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_carries_no_stream_urls_or_signatures():
    text = FIXTURE.read_text(encoding="utf-8").lower()
    assert not re.search(r"https?://|sig=|expire=|cookie", text)


def test_resolution_choices_list_only_existing_heights(info):
    choices = formats.resolution_choices(info["formats"])
    heights = [c.height for c in choices]
    assert heights == sorted(heights, reverse=True)
    existing = {f["height"] for f in info["formats"] if f.get("vcodec", "none") != "none"}
    assert set(heights) <= {h for h in presets.HEIGHTS}
    assert max(heights) == formats._bucket(max(existing))
    top = choices[0]
    assert top.size and top.label.startswith(f"{top.height}p")


def test_resolution_grouping_handles_odd_and_missing_data():
    fmts = [
        {"format_id": "a", "vcodec": "avc1", "height": 1076, "fps": 60, "filesize": 10},
        {"format_id": "b", "vcodec": "vp9", "height": 1080, "dynamic_range": "HDR10"},
        {"format_id": "c", "vcodec": "none", "acodec": "opus", "filesize_approx": 5},
        {"format_id": "d", "vcodec": "none", "acodec": "none"},  # storyboard-like
        {"format_id": "e", "vcodec": "avc1", "height": None},
        {"format_id": "f", "vcodec": "avc1", "height": True},
        "garbage",
    ]
    (choice,) = formats.resolution_choices(fmts)
    assert (choice.height, choice.fps, choice.hdr) == (1080, 60, True)
    assert choice.size == 15 and choice.approx
    assert choice.label == "1080p60 HDR  ·  ~0 MB"
    assert formats.resolution_choices([]) == []


def test_presets_cap_height_and_ignore_it_for_audio():
    assert presets.download_options("video_1080", 2160)["height"] == 1080
    assert presets.download_options("video_1080", None)["height"] == 1080
    assert presets.download_options("video_720", 480)["height"] == 480
    assert presets.download_options("video_best", None)["height"] is None
    assert presets.download_options("mp3_music", 1080)["height"] is None
    opts = presets.download_options("mp3_music", crop_cover=False)
    assert opts == {
        "mode": "download",
        "preset": "mp3_music",
        "height": None,
        "compatible": True,
        "crop_cover": False,
    }


@pytest.mark.parametrize("bad", [("nope", None), ("video_best", 1000), ("video_best", -1)])
def test_presets_reject_unknown_values(bad):
    with pytest.raises(ValueError):
        presets.download_options(*bad)


def test_core_and_worker_preset_ids_match():
    from stuff_downloader_worker import presets as worker_presets

    assert {p.id for p in presets.PRESETS} == worker_presets.PRESET_IDS
    assert set(presets.HEIGHTS) == worker_presets.HEIGHTS


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("download_error", "ERROR: [youtube] x: Private video. Sign in", "private"),
        ("download_error", "Sign in to confirm your age", "age-restricted"),
        ("download_error", "HTTP Error 403: Forbidden", "403"),
        (
            "download_error",
            "ERROR: [youtube] aaaaaaaaaa0: This video is unavailable",
            "unavailable.",
        ),
        ("download_error", "ERROR: [youtube] x: Video unavailable", "unavailable."),
        (
            "download_error",
            "The uploader has not made this video available in your country",
            "region",
        ),
        (
            "download_error",
            "ERROR: You have requested merging but ffmpeg is not installed",
            "FFmpeg",
        ),
        ("timeout", "", "too long"),
        ("worker_exited", "Worker exited with code 3", "unexpectedly"),
        ("weird", "Something odd", "Something odd"),
        (None, None, "Unknown error"),
    ],
)
def test_friendly_error_messages(code, message, expected):
    assert expected.lower() in errors.friendly_message(code, message).lower()



# ── Result-card rows (plan §8 R2) ──────────────────────────────────────────────────────────
def test_row_options_mirror_the_worker_request():
    from stuff_downloader_worker import presets as worker_presets

    cases = [
        ("video", "v:1080:mp4", "mkv", None),
        ("video", "v:orig", None, "Clip"),
        ("audio", "a:mp3:320", None, "  Edited  "),
        ("audio", "a:wav", None, None),
        ("image", "i:1280x720", "png", None),
        ("image", "i:orig", None, None),
    ]
    for tab, row_id, container, title in cases:
        options = presets.row_download_options(tab, row_id, container, title)
        request = worker_presets.parse_row_request(
            options, original_only=row_id.endswith(":orig")
        )
        assert (request.tab, request.row_id) == (tab, row_id)
    assert presets.row_download_options("video", "v:720:webm") == {
        "mode": "download",
        "tab": "video",
        "row_id": "v:720:webm",
        "container": "mp4",
    }
    assert presets.row_download_options("audio", "a:m4a", None, " x ")["edited_title"] == "x"


@pytest.mark.parametrize(
    ("tab", "row_id", "container"),
    [
        ("video", "137", "mp4"),
        ("video", "v:1080:mp4", "flv"),
        ("audio", "a:mp3:999", None),
        ("image", "i:orig", "tiff"),
        ("gallery", "g:1", None),
    ],
)
def test_row_options_refuse_unknown_rows(tab, row_id, container):
    with pytest.raises(ValueError):
        presets.row_download_options(tab, row_id, container)


def test_row_labels_name_what_is_saved():
    label = presets.row_label
    video = presets.row_download_options("video", "v:1080:mp4", "mkv")
    assert label(video) == "Video · 1080p · MKV"
    assert label(presets.row_download_options("audio", "a:mp3:256")) == "MP3 · 256 kbps"
    assert label(presets.row_download_options("audio", "a:flac")) == "Audio · FLAC"
    assert (
        label(presets.row_download_options("image", "i:1280x720", "png"))
        == "Image · PNG  1280x720"
    )
    assert presets.row_kind({"tab": "image"}) == "thumbnail"
