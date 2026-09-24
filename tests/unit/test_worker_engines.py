"""Probe and yt-dlp spike engines, without network access."""

from __future__ import annotations

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
    assert {"fake", "probe", "ytdlp", "spotdl", "music"} <= set(ENGINES)


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
    from stuff_downloader_worker.engines import http as http_engine
    from stuff_downloader_worker.engines import ytdlp

    state = {"opts": None, "raise": None, "write": None}
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for fmt in raw["formats"]:
        fmt["url"] = f"https://rr1.googlevideo.com/videoplayback?sig=SECRET&itag={fmt['format_id']}"
        fmt["http_headers"] = {"Cookie": "SID=secret"}
    raw["thumbnails"] = [
        {"url": "https://localhost/huge.jpg", "width": 9999, "height": 9999},
        {"url": "https://192.168.1.4/huge.jpg", "width": 9000, "height": 9000},
        {"url": "https://user:pw@cdn.example.com/huge.jpg", "width": 8000, "height": 8000},
        {"url": "https://i.ytimg.com/vi/x/hq.jpg", "width": 480, "height": 360},
        {"url": "http://i.ytimg.com/vi/x/sq.jpg", "width": 544, "height": 544},
    ]
    state["raw"] = raw
    state["fetched"] = []
    state["page_previews"] = []

    def fake_fetch(url, limit, seconds=15.0, https_only=False, accept=()):
        state["fetched"].append({"url": url, "https_only": https_only, "accept": accept})
        return b"\xff\xd8jpegdata"

    def fake_page_preview(url, emit):
        state["page_previews"].append(url)
        return state.get("page_image")

    monkeypatch.setattr(http_engine, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(http_engine, "page_preview", fake_page_preview)

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
    # https + a public host name + no credentials: the biggest such thumbnail is the preview.
    assert fake_ytdlp["fetched"] == [
        {"url": "https://i.ytimg.com/vi/x/hq.jpg", "https_only": True, "accept": ("image/",)}
    ]
    assert fake_ytdlp["page_previews"] == []
    assert result["preview"]["data"]
    assert events[0] == ("stage", {"stage": "analyzing"})
    assert fake_ytdlp["opts"]["noplaylist"] is True


# ── previews on any public host (plan §5.6, R3: P5) ──────────────────────────────────────
def test_a_non_youtube_thumbnail_host_is_used_and_the_largest_wins(fake_ytdlp):
    fake_ytdlp["raw"]["thumbnails"] = [
        {"url": "https://i.vimeocdn.com/video/small.jpg", "width": 295, "height": 166},
        {"url": "https://i.vimeocdn.com/video/big.jpg", "width": 1280, "height": 720},
        {"url": "https://p16-sign.tiktokcdn.com/sq.jpg", "width": 720, "height": 720},
    ]
    result = _analyze_page("https://vimeo.com/76979871")
    assert fake_ytdlp["fetched"][0]["url"] == "https://i.vimeocdn.com/video/big.jpg"
    assert result["preview"]["data"]


def test_music_prefers_square_art_over_a_bigger_wide_thumbnail(fake_ytdlp):
    fake_ytdlp["raw"]["thumbnails"] = [
        {"url": "https://i.ytimg.com/vi/x/maxres.jpg", "width": 1280, "height": 720},
        {"url": "https://lh3.googleusercontent.com/sq=w544", "width": 544, "height": 544},
    ]
    _analyze_page("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    assert fake_ytdlp["fetched"][0]["url"] == "https://lh3.googleusercontent.com/sq=w544"


def test_a_video_does_not_prefer_square_art(fake_ytdlp):
    fake_ytdlp["raw"]["thumbnails"] = [
        {"url": "https://i.ytimg.com/vi/x/maxres.jpg", "width": 1280, "height": 720},
        {"url": "https://i.ytimg.com/vi/x/sq.jpg", "width": 544, "height": 544},
    ]
    _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert fake_ytdlp["fetched"][0]["url"] == "https://i.ytimg.com/vi/x/maxres.jpg"


@pytest.mark.parametrize(
    "thumb",
    [
        "http://cdn.example.com/a.jpg",
        "https://localhost/a.jpg",
        "https://10.0.0.5/a.jpg",
        "https://[::1]/a.jpg",
        "https://printer.local/a.jpg",
        "https://u:p@cdn.example.com/a.jpg",
        "file:///C:/Windows/win.ini",
    ],
)
def test_non_public_or_plain_http_thumbnails_are_never_fetched(fake_ytdlp, thumb):
    fake_ytdlp["raw"]["thumbnails"] = [{"url": thumb, "width": 640, "height": 360}]
    fake_ytdlp["raw"]["thumbnail"] = thumb
    result = _analyze_page("https://www.example.com/watch/1")
    assert fake_ytdlp["fetched"] == []
    # No usable thumbnail: the page's own og:image is tried instead, and there is none here.
    assert fake_ytdlp["page_previews"] == ["https://www.example.com/watch/1"]
    assert result["preview"] is None


def test_no_thumbnail_falls_back_to_the_page_image(fake_ytdlp):
    fake_ytdlp["raw"]["thumbnails"] = []
    fake_ytdlp["page_image"] = b"\xff\xd8og"
    result = _analyze_page("https://www.example.com/news/story")
    assert fake_ytdlp["page_previews"] == ["https://www.example.com/news/story"]
    assert result["preview"]["data"]


def test_a_failed_thumbnail_fetch_does_not_fail_the_analyze(fake_ytdlp, monkeypatch):
    from stuff_downloader_worker.engines import http as http_engine

    def boom(*_args, **_kwargs):
        raise EngineError("unsupported", "that link points at a private network address")

    monkeypatch.setattr(http_engine, "fetch_bytes", boom)
    events, emit = _collect()
    spec = JobSpec("j1", "ytdlp", "https://vimeo.com/1", ".", {"mode": "analyze"})
    result = get_engine("ytdlp").download(spec, emit)
    assert result["preview"] is None and result["title"]
    assert any(k == "log" and "thumbnail preview failed" in d["message"] for k, d in events)


def test_trusted_tool_knows_ffprobe(monkeypatch, tmp_path):
    from stuff_downloader_worker.engines import ytdlp

    (tmp_path / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setenv(ytdlp.TOOLS_DIR_ENV_VAR, str(tmp_path))
    assert ytdlp.trusted_tool("ffprobe") == tmp_path / "ffprobe.exe"


# ── media kind (plan §5.3, R1 acceptance) ────────────────────────────────────────────────
def _analyze_page(url):
    spec = JobSpec("j1", "ytdlp", url, ".", {"mode": "analyze"})
    result = get_engine("ytdlp").download(spec, lambda *_: None)
    from stuff_downloader.core.protocol import validate_media_result

    validate_media_result(json.loads(json.dumps(result)))  # what core receives, re-parsed
    return result


def test_a_youtube_video_is_a_video_opening_on_the_video_tab(fake_ytdlp):
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result["kind"] == "video"
    assert result["tabs"] == ["video", "audio", "image"]
    assert result["site"] and result["title"]


def test_a_youtube_music_song_is_audio_that_also_offers_the_video(fake_ytdlp):
    result = _analyze_page("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result["kind"] == "audio"
    assert result["tabs"] == ["audio", "video", "image"]


def test_a_topic_upload_with_track_and_artist_is_audio(fake_ytdlp):
    fake_ytdlp["raw"].update({"track": "Song", "artist": "Singer"})
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert (result["kind"], result["tabs"]) == ("audio", ["audio", "video", "image"])
    assert result["artist"] == "Singer"


def test_an_audio_only_page_is_audio_without_a_video_tab(fake_ytdlp):
    raw = fake_ytdlp["raw"]
    raw.update({"extractor": "soundcloud", "extractor_key": "Soundcloud"})
    raw["formats"] = [
        {"format_id": "hls_opus_64", "ext": "opus", "vcodec": "none", "acodec": "opus"},
        {"format_id": "http_mp3_128", "ext": "mp3", "vcodec": "none", "acodec": "mp3"},
    ]
    result = _analyze_page("https://soundcloud.com/artist/a-song")
    assert (result["kind"], result["tabs"]) == ("audio", ["audio", "image"])


def test_a_format_without_codec_facts_is_judged_by_its_container(fake_ytdlp):
    raw = fake_ytdlp["raw"]
    raw["extractor_key"] = "Generic"
    raw["formats"] = [{"format_id": "0", "ext": "mp3"}]
    assert _analyze_page("https://example.com/a")["kind"] == "audio"
    raw["formats"] = [{"format_id": "0", "ext": "mp4"}]
    assert _analyze_page("https://example.com/a")["kind"] == "video"


def test_a_playlist_listing_is_a_playlist_media_result(fake_ytdlp):
    fake_ytdlp["raw"] = {
        "_type": "playlist",
        "id": "PL1234567890",
        "title": "Mix",
        "entries": [{"id": "dQw4w9WgXcQ", "title": "One"}],
    }
    url = "https://www.youtube.com/playlist?list=PL1234567890"
    spec = JobSpec("j1", "ytdlp", url, ".", {"mode": "playlist"})
    result = get_engine("ytdlp").download(spec, lambda *_: None)
    assert (result["kind"], result["tabs"]) == ("playlist", ["tracks"])
    assert result["entries"][0]["id"] == "dQw4w9WgXcQ"
    assert result["webpage"] == url


def test_the_webpage_is_the_analyzed_link_without_tokens(fake_ytdlp):
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=track")
    assert result["webpage"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    result = _analyze_page("https://cdn.example.com/v/clip?token=SECRET#t=1")
    assert result["webpage"] == "https://cdn.example.com/v/clip"


def test_the_fake_engine_analyzes_offline_and_refuses_unknown_modes():
    result = get_engine("fake").download(_spec("fake", mode="analyze"), lambda *_: None)
    assert (result["kind"], result["tabs"][0]) == ("video", "video")
    with pytest.raises(EngineError, match="mode"):
        get_engine("fake").download(_spec("fake", mode="playlist"), lambda *_: None)


def test_the_probe_engine_has_no_analyze_mode():
    with pytest.raises(EngineError, match="no modes"):
        get_engine("probe").download(_spec("probe", mode="analyze"), None)


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



# ── the format catalog (plan §5.4, R2) ─────────────────────────────────────────────────────
def test_a_video_offers_one_row_per_existing_height_with_our_ids(fake_ytdlp):
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    rows = result["video_rows"]
    heights = [r["height"] for r in rows]
    assert heights == sorted(set(heights), reverse=True)
    assert all(r["id"] == f"v:{r['height']}:{r['container']}" for r in rows)
    (default,) = [r for r in rows if r["default"]]
    assert default["vcodec"] == "H.264" and default["height"] <= 1080
    assert all(r["size"] is None or r["size"] > 0 for r in rows)
    text = json.dumps(result)
    assert "SECRET" not in text and all("format_id" not in r for r in rows)


def test_audio_rows_cover_mp3_bitrates_m4a_opus_flac_and_wav(fake_ytdlp):
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    ids = [r["id"] for r in result["audio_rows"]]
    assert ids[:5] == ["a:mp3:320", "a:mp3:256", "a:mp3:192", "a:mp3:128", "a:mp3:64"]
    assert ids[5:] == ["a:m4a", "a:opus", "a:flac", "a:wav"]
    m4a = result["audio_rows"][5]
    assert m4a["copy"] is True  # the source has AAC, so M4A is a copy
    wav = result["audio_rows"][-1]
    assert wav["no_cover"] is True and "no embedded cover" in wav["lossless_note"]
    mp3 = result["audio_rows"][0]
    assert mp3["size_is_estimate"] and mp3["size"] == int(result["duration"] * 320_000 / 8)
    assert result["source_audio"]["codec"] in ("Opus", "AAC")


def test_image_rows_are_the_thumbnail_sizes_that_exist(fake_ytdlp):
    result = _analyze_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert [r["id"] for r in result["image_rows"]] == [
        "i:9999x9999", "i:9000x9000", "i:8000x8000", "i:544x544", "i:480x360"
    ]


def test_an_audio_only_page_has_no_video_rows_and_no_opus_row_without_opus(fake_ytdlp):
    fake_ytdlp["raw"]["formats"] = [
        {"format_id": "a1", "ext": "mp3", "vcodec": "none", "acodec": "mp3", "abr": 128}
    ]
    result = _analyze_page("https://soundcloud.com/artist/song")
    assert result["video_rows"] == [] and "video" not in result["tabs"]
    ids = [r["id"] for r in result["audio_rows"]]
    assert "a:opus" not in ids and "a:m4a" in ids
    assert result["audio_rows"][5]["copy"] is False  # no AAC source: encoded at 256 kbps


def test_a_row_download_reaches_yt_dlp_as_fixed_options(fake_ytdlp, tmp_path):
    out = tmp_path / "Edited.mp3"
    fake_ytdlp["write"] = out
    spec = JobSpec(
        "j1",
        "ytdlp",
        "https://www.youtube.com/watch?v=x",
        str(tmp_path),
        {"mode": "download", "tab": "audio", "row_id": "a:mp3:192", "edited_title": "Edited"},
    )
    result = get_engine("ytdlp").download(spec, lambda *_: None)
    opts = fake_ytdlp["opts"]
    assert opts["outtmpl"] == "Edited.%(ext)s"
    assert opts["postprocessors"][1]["preferredquality"] == "192"
    assert result["files"] == [str(out)] and result["preset"] == "mp3_music"


def test_an_image_row_keeps_only_the_chosen_thumbnail(fake_ytdlp, tmp_path, monkeypatch):
    from stuff_downloader_worker.engines import ytdlp

    seen = {}
    monkeypatch.setattr(
        ytdlp.YtDlpEngine,
        "_download",
        staticmethod(lambda ydl, info, *rest: seen.update(info) or {"files": []}),
    )
    spec = JobSpec(
        "j1",
        "ytdlp",
        "https://www.youtube.com/watch?v=x",
        str(tmp_path),
        {"mode": "download", "tab": "image", "row_id": "i:480x360", "container": "png"},
    )
    get_engine("ytdlp").download(spec, lambda *_: None)
    assert [(t["width"], t["height"]) for t in seen["thumbnails"]] == [(480, 360)]
    spec.options["row_id"] = "i:1x1"
    with pytest.raises(EngineError):
        get_engine("ytdlp").download(spec, lambda *_: None)
