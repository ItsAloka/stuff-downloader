"""Direct-file routing, the file preset, and the All formats track data (M3 closeout)."""

from __future__ import annotations

import json

import pytest

from stuff_downloader.core import errors, presets, router
from stuff_downloader_worker.engines import ytdlp


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/media/clip.mp4",
        "https://cdn.example.com/a/b/Song.MP3?sig=abc&exp=1",
        "http://files.example.org/photo.jpeg",
        "https://cdn.example.com/v.webm#t=3",
    ],
)
def test_direct_file_links_go_to_the_http_engine(url):
    route = router.route(url)
    assert route.kind == "file" and route.is_file and route.ok
    assert route.engine == "http" and "#" not in route.url


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456789",
        "https://cdn.example.com/watch.mp4/page",  # the file name is not the last segment
        "https://cdn.example.com/.mp4",  # a dotfile, not a name with an extension
        "https://cdn.example.com/archive.zip",  # not media: stays with yt-dlp's generic page
    ],
)
def test_other_links_stay_with_ytdlp(url):
    assert router.route(url).engine == "ytdlp"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/clip.mp4",
        "http://localhost/clip.mp4",
        "http://nas.local/clip.mp4",
        "https://user:pw@cdn.example.com/clip.mp4",
        "file:///C:/clip.mp4",
    ],
)
def test_direct_file_links_get_the_same_url_validation(url):
    assert not router.route(url).ok


def test_youtube_routing_is_unchanged():
    route = router.route("https://youtu.be/dQw4w9WgXcQ")
    assert route.kind == "youtube" and route.engine == "ytdlp"


def test_durable_url_drops_a_direct_links_token():
    safe, removed = router.durable_url("https://cdn.example.com/clip.mp4?sig=SECRET")
    assert safe == "https://cdn.example.com/clip.mp4" and removed


def test_file_preset_is_known_but_not_offered_to_video_pages():
    assert presets.get("original_file") is presets.FILE_PRESET
    assert presets.FILE_PRESET not in presets.PRESETS
    assert not presets.FILE_PRESET.picks_resolution
    assert presets.file_download_options() == {"mode": "download", "preset": "original_file"}


def test_friendly_messages_for_direct_file_failures():
    assert "no longer exists" in errors.friendly_message("unsupported", "HTTP Error 404: Not Found")
    assert "timed out" in errors.friendly_message("download_error", "the connection timed out")


# ── All formats: subtitles and thumbnails reach the table, URLs do not ──────────────────
RAW = {
    "id": "x",
    "title": "t",
    "formats": [],
    "subtitles": {
        "en": [
            {"ext": "vtt", "url": "https://s.example.com/en.vtt?sig=SECRET"},
            {"ext": "srt", "url": "https://s.example.com/en.srt"},
        ],
        "de; rm -rf": [{"ext": "vtt", "url": "https://x"}],
        "live_chat": [{"ext": "json", "url": "https://x"}],
        "fr": [{"ext": "../evil", "url": "https://x"}],
    },
    "automatic_captions": {
        lang: [{"ext": "vtt", "url": f"https://s.example.com/{lang}?sig=SECRET"}]
        for lang in ("en", "es", "ja")
    },
    "thumbnails": [
        {"url": "https://i.example.com/a.jpg?sig=SECRET", "width": 1280, "height": 720},
        {"url": "https://i.example.com/b.jpg", "width": 320, "height": 180},
        {"url": "https://i.example.com/c.jpg", "width": 1280, "height": 720},  # duplicate size
        {"url": "https://i.example.com/d.jpg"},  # no size: not a row
        {"url": "https://i.example.com/e.jpg", "width": True, "height": 5},
    ],
}


def test_sanitize_info_keeps_track_facts_and_no_urls():
    info = ytdlp.sanitize_info(json.loads(json.dumps(RAW)))
    assert info["subtitles"] == [{"lang": "en", "exts": ["vtt", "srt"], "auto": False}]
    assert [t["lang"] for t in info["automatic_captions"]] == ["en", "es", "ja"]
    assert info["thumbnails"] == [{"width": 1280, "height": 720}, {"width": 320, "height": 180}]
    text = json.dumps(info)
    assert "SECRET" not in text and "example.com" not in text


def test_sanitize_tracks_tolerate_junk():
    assert ytdlp.sanitize_subtitles(None, auto=False) == []
    assert ytdlp.sanitize_subtitles({"en": "nope"}, auto=False) == []
    assert ytdlp.sanitize_thumbnails({"not": "a list"}) == []
    many = {f"l{i}": [{"ext": "vtt"}] for i in range(200)}
    assert len(ytdlp.sanitize_subtitles(many, auto=True)) == ytdlp.MAX_SUBTITLE_TRACKS
