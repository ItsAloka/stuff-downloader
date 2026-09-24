from pathlib import Path

import pytest

from stuff_downloader.core import protocol
from stuff_downloader_worker import protocol as worker_protocol

SRC = Path(__file__).resolve().parents[2] / "src"


def test_protocol_files_identical():
    core = (SRC / "stuff_downloader" / "core" / "protocol.py").read_bytes()
    worker = (SRC / "stuff_downloader_worker" / "protocol.py").read_bytes()
    assert core == worker
    assert protocol.PROTOCOL_VERSION == worker_protocol.PROTOCOL_VERSION


def test_job_spec_round_trip():
    spec = protocol.JobSpec("j1", "fake", "https://x.test/", "C:/out", {"steps": 2})
    assert protocol.JobSpec.from_json(spec.to_json()) == spec


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "[]",
        '{"v": 99, "job_id": "a", "engine": "fake", "url": "u", "output_dir": "o"}',
        '{"v": 1, "job_id": "", "engine": "fake", "url": "u", "output_dir": "o"}',
        '{"v": 1, "job_id": "a", "engine": "fake", "url": "u", "output_dir": "o", "options": 3}',
    ],
)
def test_job_spec_rejects_invalid(text):
    with pytest.raises(protocol.ProtocolError):
        protocol.JobSpec.from_json(text)


def test_event_round_trip_and_terminal():
    event = protocol.Event("progress", "j1", {"percent": 50.0, "title": "ünïcode"})
    line = event.to_line()
    assert line.endswith("\n") and line.count("\n") == 1
    parsed = protocol.Event.from_line(line)
    assert parsed == event and not parsed.is_terminal
    assert protocol.Event("result", "j1").is_terminal
    assert protocol.Event("error", "j1").is_terminal


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        "42",
        '{"v": 1, "type": "bogus", "job_id": "j"}',
        '{"v": 2, "type": "log", "job_id": "j"}',
        '{"v": 1, "type": "log", "job_id": 7}',
    ],
)
def test_event_rejects_invalid(line):
    with pytest.raises(protocol.ProtocolError):
        protocol.Event.from_line(line)


# ── MediaResult (plan §5.2) ────────────────────────────────────────────────────────────────
def test_media_result_fills_every_list_and_round_trips_as_json():
    import json

    page = "https://x.com/a/status/1"
    result = protocol.media_result("image", ["image"], "Photo", page, site="X")
    assert result["preview"] is None
    for key in ("video_rows", "audio_rows", "image_rows", "items", "entries"):
        assert result[key] == []
    assert protocol.validate_media_result(json.loads(json.dumps(result))) == result


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "file"},
        {"kind": "spotify"},
        {"tabs": []},
        {"tabs": ["video", "video"]},
        {"tabs": ["converters"]},
        {"tabs": ["audio", "video"]},  # a video must open on its Video tab
        {"title": None},
        {"site": 3},
        {"duration": -1},
        {"duration": True},
        {"preview": {"url": "https://example.com/a.jpg"}},
        {"source_audio": "opus"},
        {"items": "none"},
        {"entries": [1]},
        {"webpage": None},
        {"webpage": ""},
        {"webpage": "https://cdn.example/a.jpg?sig=SECRET"},
        {"webpage": "https://user:pw@example.com/a"},
        {"webpage": "https://example.com/a#frag"},
        {"webpage": "file:///C:/a.mp4"},
    ],
)
def test_media_result_rejects_malformed_results(change):
    good = protocol.media_result("video", ["video", "audio", "image"], "Clip", "https://a.example/v")
    with pytest.raises(protocol.ProtocolError):
        protocol.validate_media_result({**good, **change})


@pytest.mark.parametrize(
    ("kind", "first"),
    [("video", "video"), ("audio", "audio"), ("image", "image"),
     ("gallery", "gallery"), ("playlist", "tracks")],
)  # fmt: skip
def test_each_kind_opens_on_its_own_tab(kind, first):
    assert protocol.media_result(kind, [first], "t", "https://a.example/p")["tabs"] == [first]


@pytest.mark.parametrize(
    ("url", "durable"),
    [
        ("https://u:p@Www.YouTube.com/watch?v=dQw4w9WgXcQ&si=track&list=PL1#t=3",
         "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1"),
        ("https://music.youtube.com/playlist?list=OLAK5uy_x&feature=share",
         "https://music.youtube.com/playlist?list=OLAK5uy_x"),
        ("https://pbs.twimg.com/media/Gx1AbC?format=jpg&name=large",
         "https://pbs.twimg.com/media/Gx1AbC"),
        ("https://open.spotify.com/track/abc?si=123", "https://open.spotify.com/track/abc"),
        ("http://Example.com:8080", "http://example.com:8080/"),
        ("ftp://example.com/a", ""),
        ("not a link", ""),
        ("https://example.com:99999/a", ""),
    ],
)  # fmt: skip
def test_the_webpage_keeps_no_credentials_tokens_or_fragments(url, durable):
    assert protocol.durable_webpage(url) == durable
    if durable:
        result = protocol.media_result("video", ["video"], "t", url)
        assert result["webpage"] == durable


def test_a_result_without_a_usable_webpage_is_refused():
    with pytest.raises(protocol.ProtocolError, match="webpage"):
        protocol.media_result("video", ["video"], "t", "javascript:alert(1)")
