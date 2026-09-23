"""Probe and yt-dlp spike engines, without network access."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from stuff_downloader_worker.engines import ENGINES, get_engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec


def _spec(engine, **options):
    return JobSpec("j1", engine, "https://example.invalid/", ".", options)


def test_registry_has_spike_engines():
    assert {"fake", "probe", "ytdlp"} <= set(ENGINES)


def test_probe_reports_interpreter_and_modules():
    result = get_engine("probe").download(_spec("probe", modules=["json", "no_such_mod_xyz"]), None)
    assert result["executable"] == sys.executable
    assert result["modules"]["no_such_mod_xyz"] is None
    assert result["modules"]["json"] is not None


@pytest.mark.parametrize("modules", ["json", ["os.path"], ["x; import os"], [1], ["a"] * 21])
def test_probe_rejects_bad_module_lists(modules):
    with pytest.raises(EngineError):
        get_engine("probe").download(_spec("probe", modules=modules), None)


def test_ytdlp_rejects_bad_mode_before_network(monkeypatch):
    pytest.importorskip("yt_dlp")
    with pytest.raises(EngineError, match="mode"):
        get_engine("ytdlp").download(_spec("ytdlp", mode="delete"), lambda *_: None)


def test_trusted_tool_only_uses_fixed_names_in_runner_dir(monkeypatch, tmp_path):
    from stuff_downloader_worker.engines import ytdlp

    (tmp_path / "deno.exe").write_bytes(b"")
    monkeypatch.delenv(ytdlp.TOOLS_DIR_ENV_VAR, raising=False)
    assert ytdlp.trusted_tool("deno") is None
    monkeypatch.setenv(ytdlp.TOOLS_DIR_ENV_VAR, str(tmp_path))
    assert ytdlp.trusted_tool("deno") == tmp_path / "deno.exe"
    assert ytdlp.trusted_tool("ffmpeg") is None  # not present
    assert ytdlp.trusted_tool("calc") is None  # not an allowed tool


FIXTURE = Path(__file__).parent / "fixtures" / "youtube_video.json"


class FakeDownloadError(Exception):
    pass


@pytest.fixture
def fake_ytdlp(monkeypatch):
    """A stand-in yt_dlp module: returns raw info with URLs and runs the configured hooks."""
    from stuff_downloader_worker.engines import ytdlp

    state = {"opts": None, "raise": None, "write": None}
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for fmt in raw["formats"]:
        fmt["url"] = f"https://rr1.googlevideo.com/videoplayback?sig=SECRET&itag={fmt['format_id']}"
        fmt["http_headers"] = {"Cookie": "SID=secret"}
    raw["thumbnails"] = [
        {"url": "https://localhost/huge.jpg", "width": 9999, "height": 9999},
        {"url": "https://i.ytimg.com/vi/x/hq.jpg", "width": 480, "height": 360},
        {"url": "http://i.ytimg.com/vi/x/sq.jpg", "width": 544, "height": 544},
    ]
    state["raw"] = raw

    class Resp:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return self.data[:n]

    class FakeYDL:
        def __init__(self, opts):
            state["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            if state["raise"]:
                raise FakeDownloadError(state["raise"])
            return json.loads(json.dumps(state["raw"]))

        def urlopen(self, url):
            state["thumb_url"] = url
            return Resp(b"\xff\xd8jpegdata")

        def process_ie_result(self, info, download=True):
            opts = state["opts"]
            hook = opts["progress_hooks"][0]
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 5,
                    "total_bytes": 10,
                    "info_dict": {"vcodec": "avc1"},
                }
            )
            hook({"status": "finished"})
            for pp in opts.get("postprocessors", []):
                opts["postprocessor_hooks"][0](
                    {"status": "started", "postprocessor": pp["key"].removeprefix("FFmpeg")}
                )
            if state["write"]:
                state["write"].write_bytes(b"media")
                opts["post_hooks"][0](str(state["write"]))
            return {"requested_formats": [{"height": 720}, {"height": None}]}

    fake = type(sys)("yt_dlp")
    fake.YoutubeDL = FakeYDL
    fake.version = type(sys)("yt_dlp.version")
    fake.version.__version__ = "2026.08.19"
    fake.utils = type(sys)("yt_dlp.utils")
    fake.utils.DownloadError = FakeDownloadError
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.delenv(ytdlp.TOOLS_DIR_ENV_VAR, raising=False)
    return state


def _collect():
    events = []
    return events, lambda kind, data: events.append((kind, data))


def test_analyze_returns_sanitized_info_and_safe_thumbnail(fake_ytdlp):
    events, emit = _collect()
    result = get_engine("ytdlp").download(_spec("ytdlp", mode="analyze"), emit)
    text = json.dumps(result)
    assert "googlevideo" not in text and "SECRET" not in text and "Cookie" not in text
    assert result["title"] and result["engine_version"] == "2026.08.19"
    assert all("url" not in f for f in result["formats"])
    assert fake_ytdlp["thumb_url"] == "https://i.ytimg.com/vi/x/hq.jpg"
    assert result["thumbnail_url"] == fake_ytdlp["thumb_url"]
    assert result["thumbnail"]["data"]
    assert events[0] == ("stage", {"stage": "analyzing"})
    assert fake_ytdlp["opts"]["noplaylist"] is True


@pytest.mark.parametrize("ext", ["jpg", "webp"])
def test_image_only_extractor_result_hands_off_to_gallery(fake_ytdlp, ext):
    raw = fake_ytdlp["raw"]
    raw["ext"] = ext
    raw["formats"] = [{"format_id": "image", "ext": ext, "url": "https://cdn.example/image"}]
    with pytest.raises(EngineError, match="image post requires gallery") as exc:
        get_engine("ytdlp").download(_spec("ytdlp", mode="analyze"), lambda *_: None)
    assert exc.value.code == "unsupported"


def test_video_with_image_thumbnail_stays_a_video(fake_ytdlp):
    fake_ytdlp["raw"]["ext"] = "mp4"
    assert get_engine("ytdlp").download(_spec("ytdlp", mode="analyze"), lambda *_: None)[
        "title"
    ]


def test_non_youtube_preview_uses_checked_http_and_falls_back(monkeypatch):
    from stuff_downloader_worker.engines import ytdlp

    url = "https://cdn.instagram.com/reel/cover.jpg"
    assert ytdlp._thumbnail_url({"thumbnail": url}) == url
    assert ytdlp._thumbnail_url({"thumbnail": "https://127.0.0.1/x.jpg"}) is None
    assert ytdlp._thumbnail_url({"thumbnail": "http://cdn.instagram.com/x.jpg"}) is None
    assert ytdlp._thumbnail_url({"thumbnail": None}) is None
    called = []

    class Conn:
        def request(self, *_args, **_kwargs):
            called.append("requested")

        def getresponse(self):
            return Resp()

        def close(self):
            called.append("closed")

    class Resp:
        status = 200

        def read(self, amount):
            return b"image"[:amount]

    monkeypatch.setattr(ytdlp, "_connect", lambda target: (called.append(target.host) or Conn()))
    result = ytdlp.YtDlpEngine._thumbnail_preview(None, {"thumbnail": url}, lambda *_: None)
    assert base64.b64decode(result["data"]) == b"image"
    assert called == ["cdn.instagram.com", "requested", "closed"]
    monkeypatch.setattr(ytdlp, "_connect", lambda _: (_ for _ in ()).throw(OSError("secret")))
    logs = []
    assert (
        ytdlp.YtDlpEngine._thumbnail_preview(
            None, {"thumbnail": url}, lambda *event: logs.append(event)
        )
        is None
    )
    assert "secret" not in str(logs)


def test_thumbnail_redirect_cannot_downgrade_to_http(monkeypatch):
    from stuff_downloader_worker.engines import ytdlp

    called = []

    class Conn:
        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return self

        status = 302

        def getheader(self, _name):
            return "http://cdn.instagram.com/unprotected.jpg"

        def close(self):
            called.append("closed")

    monkeypatch.setattr(ytdlp, "_connect", lambda target: (called.append(target.host) or Conn()))
    assert ytdlp.YtDlpEngine._thumbnail_preview(
        None, {"thumbnail": "https://cdn.instagram.com/cover.jpg"}, lambda *_: None
    ) is None
    assert called == ["cdn.instagram.com", "closed"]


def test_download_emits_stages_progress_and_file(fake_ytdlp, tmp_path):
    out = tmp_path / "Video [x].mp4"
    fake_ytdlp["write"] = out
    events, emit = _collect()
    spec = JobSpec(
        "j1",
        "ytdlp",
        "https://www.youtube.com/watch?v=x",
        str(tmp_path),
        {
            "mode": "download",
            "preset": "video_1080",
            "height": 480,
            "compatible": True,
            "crop_cover": True,
        },
    )
    result = get_engine("ytdlp").download(spec, emit)
    assert result["files"] == [str(out)] and result["total_bytes"] == 5
    assert "formats" not in result and result["preset"] == "video_1080"
    stages = [d["stage"] for k, d in events if k == "stage"]
    assert stages == ["analyzing", "downloading", "downloading video", "completed"]
    progress = [d for k, d in events if k == "progress"]
    assert progress == [
        {"downloaded_bytes": 5, "total_bytes": 10, "percent": 50.0, "speed": None, "eta": None}
    ]
    logs = [d["message"] for k, d in events if k == "log"]
    assert logs == ["480p was not available; got 720p instead"]
    assert fake_ytdlp["opts"]["paths"] == {"home": str(tmp_path)}


def test_download_without_output_file_is_an_error(fake_ytdlp, tmp_path):
    spec = JobSpec(
        "j1",
        "ytdlp",
        "https://www.youtube.com/watch?v=x",
        str(tmp_path),
        {"mode": "download", "preset": "audio_original"},
    )
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(spec, lambda *_: None)
    assert info.value.code == "no_output"


def test_extractor_errors_become_download_error(fake_ytdlp):
    fake_ytdlp["raise"] = "ERROR: Private video"
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec("ytdlp"), lambda *_: None)
    assert info.value.code == "download_error" and "Private video" in info.value.message


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "analyze", "format": "b"},
        {"mode": "download", "preset": "video_best", "deno_path": "C:/evil.exe"},
        {"mode": "download", "preset": "video_best", "ffmpeg_location": "C:/evil.exe"},
    ],
)
def test_ytdlp_rejects_job_supplied_engine_options(fake_ytdlp, options):
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec("ytdlp", **options), lambda *_: None)
    assert info.value.code == "bad_options"
    assert fake_ytdlp["opts"] is None  # rejected before yt-dlp was even constructed


def test_tools_come_only_from_runner_dir(fake_ytdlp, monkeypatch, tmp_path):
    from stuff_downloader_worker.engines import ytdlp

    get_engine("ytdlp").download(_spec("ytdlp"), lambda *_: None)
    assert "js_runtimes" not in fake_ytdlp["opts"]
    assert "ffmpeg_location" not in fake_ytdlp["opts"]
    (tmp_path / "deno.exe").write_bytes(b"")
    (tmp_path / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setenv(ytdlp.TOOLS_DIR_ENV_VAR, str(tmp_path))
    get_engine("ytdlp").download(_spec("ytdlp"), lambda *_: None)
    assert fake_ytdlp["opts"]["js_runtimes"] == {"deno": {"path": str(tmp_path / "deno.exe")}}
    assert fake_ytdlp["opts"]["ffmpeg_location"] == str(tmp_path / "ffmpeg.exe")


def test_jpeg_size_reads_sof_header():
    from stuff_downloader_worker.tagging import jpeg_size

    sof = b"\xff\xc0\x00\x11\x08" + (360).to_bytes(2, "big") + (480).to_bytes(2, "big")
    app0 = b"\xff\xe0\x00\x04ab"
    assert jpeg_size(b"\xff\xd8" + app0 + sof + b"\x00" * 12) == (480, 360)
    assert jpeg_size(b"PNG....") is None
    assert jpeg_size(b"\xff\xd8\x00garbage-without-markers") is None


def test_ytdlp_missing_engine_is_a_clean_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # import now raises ImportError
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec("ytdlp"), lambda *_: None)
    assert info.value.code == "engine_missing"


# --- M3: failures from sites we do not control ----------------------------------------------


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("ERROR: Unsupported URL: https://example.test/page", "unsupported"),
        ("ERROR: [generic] page: No video formats found!", "unsupported"),
        ("ERROR: 'x' is not a valid URL", "unsupported"),
        ("ERROR: [youtube] abc: Private video. Sign in", "download_error"),
        ("HTTP Error 429: Too Many Requests", "download_error"),
    ],
)
def test_engine_failures_are_classified(message, code):
    from stuff_downloader_worker.engines import ytdlp

    assert ytdlp.describe_download_error(message)[0] == code


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Unsupported URL: https://example.test/v?sig=SECRET&token=abc",
        "ERROR: unable to download https://rr1.googlevideo.com/videoplayback?sig=SECRET",
        "ERROR: giving up after http://user:SECRET@example.test/x and rtmp://example.test/SECRET",
    ],
)
def test_engine_failure_text_never_carries_a_url(message):
    from stuff_downloader_worker.engines import ytdlp

    _, safe = ytdlp.describe_download_error(message)
    assert "SECRET" not in safe and "://" not in safe
    assert "[link]" in safe


def test_engine_failure_text_is_bounded_and_never_empty():
    from stuff_downloader_worker.engines import ytdlp

    code, safe = ytdlp.describe_download_error("https://example.test/" + "a" * 5000)
    assert code == "download_error" and safe == "[link]"
    code, safe = ytdlp.describe_download_error("x" * 5000)
    assert len(safe) == ytdlp.MAX_ERROR_TEXT
    assert ytdlp.describe_download_error("   ")[1] == "the download failed"


def test_a_failing_analyze_raises_a_classified_engine_error(fake_ytdlp):
    fake_ytdlp["raise"] = "ERROR: Unsupported URL: https://example.test/x?token=SECRET"
    events, emit = _collect()
    with pytest.raises(EngineError) as excinfo:
        get_engine("ytdlp").download(_spec("ytdlp", mode="analyze"), emit)
    assert excinfo.value.code == "unsupported"
    assert "SECRET" not in excinfo.value.message and "://" not in excinfo.value.message
