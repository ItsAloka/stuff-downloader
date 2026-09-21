"""gallery-dl engine against a stand-in gallery_dl package. No network, no real gallery-dl.

The stand-in mirrors only the surface the engine uses (config, extractor.find, DataJob,
DownloadJob with hooks, Message constants, cookie loading), so these tests pin the engine's
own rules: what reaches the GUI, what reaches gallery-dl's config, and how failures read.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import types
from pathlib import Path

import pytest

from stuff_downloader.core import errors
from stuff_downloader_worker import __main__ as worker_main
from stuff_downloader_worker.engines import gallerydl, get_engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import Event, JobSpec

URL = "https://www.instagram.com/p/ABC123/"
SECRET = "sig=SUPERSECRET"
JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self.raw = io.BytesIO(data)
        self.raw.read = lambda n, decode_content=True, _b=self.raw: io.BytesIO.read(_b, n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, state):
        self.state = state

    def get(self, url, stream=True, timeout=None):
        self.state["preview_urls"].append(url)
        return FakeResponse(self.state["preview_bytes"])


@pytest.fixture
def fake_gdl(monkeypatch):
    state = {
        "config": {},
        "items": [
            (
                f"https://scontent.cdninstagram.com/a.jpg?{SECRET}",
                {"extension": "jpg", "width": 1080, "height": 1350},
            ),
            (
                f"https://scontent.cdninstagram.com/b.mp4?{SECRET}",
                {"extension": "mp4", "width": 720, "height": 1280},
            ),
            ("http://insecure.example.com/c.jpg", {"extension": "jpg"}),
            ("https://127.0.0.1/d.png", {"extension": "png"}),
        ],
        "post": {
            "description": "  Sunset\nat   the beach  ",
            "username": "some.user",
            "_secret": "x",
        },
        "data_exception": None,
        "child_error": None,
        "download_fail": None,
        "found": True,
        "preview_urls": [],
        "preview_bytes": JPEG,
        "cookie_file_ok": True,
        "browser_ok": True,
        "cookie_loads": [],
        "download_ranges": [],
        "write_files": True,
    }

    pkg = types.ModuleType("gallery_dl")
    config = types.ModuleType("gallery_dl.config")
    config.clear = lambda: state["config"].clear()
    config.set = lambda path, key, value: state["config"].__setitem__((path, key), value)
    version = types.ModuleType("gallery_dl.version")
    version.__version__ = "1.32.13"
    util = types.ModuleType("gallery_dl.util")

    def cookiestxt_load(fp):
        state["cookie_loads"].append("file")
        if not state["cookie_file_ok"]:
            raise ValueError(f"bad cookie line in {fp.name}")
        return []

    util.cookiestxt_load = cookiestxt_load
    cookies_mod = types.ModuleType("gallery_dl.cookies")

    def load_cookies(source):
        state["cookie_loads"].append(tuple(source))
        if not state["browser_ok"]:
            raise OSError("could not copy C:/Users/me/AppData/Firefox/cookies.sqlite")
        return ["cookie-objects"]

    cookies_mod.load_cookies = load_cookies

    extractor = types.ModuleType("gallery_dl.extractor")
    common = types.ModuleType("gallery_dl.extractor.common")
    common.CACHE_COOKIES = {}
    message = types.ModuleType("gallery_dl.extractor.message")

    class Message:
        Directory = 2
        Url = 3
        Queue = 6

    message.Message = Message

    class FakeExtractor:
        category = "instagram"

        def __init__(self, url):
            self.url = url
            self.session = FakeSession(state)

    extractor.find = lambda url: FakeExtractor(url) if state["found"] else None

    job = types.ModuleType("gallery_dl.job")

    class DataJob:
        def __init__(self, extr, file=None, resolve=False):
            self.extr = extr
            self.data = []
            self.data_urls = []
            self.exception = None

        def run(self):
            if state["data_exception"]:
                self.exception = state["data_exception"]
                return 0
            if state["child_error"]:
                # A queue-resolved child job failed: gallery-dl records it in the shared data as
                # (-1, {...}) and the parent's .exception stays None.
                self.data.append((-1, dict(state["child_error"])))
                return 0
            self.data.append((Message.Directory, dict(state["post"])))
            for url, kwdict in state["items"]:
                self.data.append((Message.Url, url, dict(kwdict)))
                self.data_urls.append(url)
            return 0

    class PathFmt:
        def __init__(self, path):
            self.path = path

    class DownloadJob:
        def __init__(self, extr):
            self.extr = extr
            self.hooks = ()

        def register_hooks(self, hooks):
            for name, callback in hooks.items():
                self.hooks[name].append(callback)

        def run(self):
            import logging

            state["download_ranges"].append(state["config"].get((("extractor",), "image-range")))
            if state["download_fail"]:
                logging.getLogger("instagram").error(state["download_fail"])
                return 16
            base = Path(state["config"][(("extractor",), "base-directory")])
            wanted = gallerydl_positions(state["config"][(("extractor",), "image-range")])
            for position in wanted:
                url, kwdict = state["items"][position - 1]
                path = base / f"instagram_ABC123_{position}.{kwdict['extension']}"
                if state["write_files"]:
                    path.write_bytes(b"x" * position)
                    for callback in self.hooks["after"]:
                        callback(PathFmt(str(path)))
                else:
                    for callback in self.hooks["skip"]:
                        callback(PathFmt(str(path)))
            return 0

    job.DataJob = DataJob
    job.DownloadJob = DownloadJob

    pkg.config, pkg.version, pkg.util, pkg.cookies = config, version, util, cookies_mod
    pkg.extractor, pkg.job = extractor, job
    extractor.common, extractor.message = common, message
    for name, module in {
        "gallery_dl": pkg,
        "gallery_dl.config": config,
        "gallery_dl.version": version,
        "gallery_dl.util": util,
        "gallery_dl.cookies": cookies_mod,
        "gallery_dl.extractor": extractor,
        "gallery_dl.extractor.common": common,
        "gallery_dl.extractor.message": message,
        "gallery_dl.job": job,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    state["common"] = common
    return state


def gallerydl_positions(spec):
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def _collect():
    events = []
    return events, lambda kind, data: events.append((kind, data))


def _run(options, url=URL, out="."):
    events, emit = _collect()
    result = get_engine("gallerydl").download(
        JobSpec("j1", "gallerydl", url, str(out), options), emit
    )
    return result, events


def cfg(state, key, section="extractor"):
    return state["config"].get(((section,), key))


# ── analyze ──────────────────────────────────────────────────────────────────────────────
def test_analyze_returns_rebuilt_rows_and_never_an_item_url(fake_gdl):
    result, events = _run({"mode": "analyze"})
    assert result["kind"] == "gallery" and result["extractor"] == "Instagram"
    assert result["title"] == "Sunset at the beach" and result["uploader"] == "some.user"
    rows = result["items"]
    assert [r["index"] for r in rows] == [1, 2, 3, 4]
    assert rows[0] | {} == {**rows[0], "kind": "image", "ext": "jpg", "width": 1080, "height": 1350}
    assert rows[1]["kind"] == "video" and "thumbnail" not in rows[1]
    text = json.dumps(result)
    assert "SUPERSECRET" not in text and "cdninstagram" not in text and "http" not in text
    assert "_secret" not in text
    assert [k for k, _ in events if k == "stage"] == ["stage", "stage"]


def test_previews_only_from_https_public_hosts(fake_gdl):
    result, _ = _run({"mode": "analyze"})
    rows = result["items"]
    assert base64.b64decode(rows[0]["thumbnail"]["data"]) == JPEG
    assert "thumbnail" not in rows[2]  # plain http
    assert "thumbnail" not in rows[3]  # an IP address
    assert fake_gdl["preview_urls"] == [fake_gdl["items"][0][0]]


def test_oversized_previews_are_dropped(fake_gdl):
    fake_gdl["preview_bytes"] = b"x" * (gallerydl.MAX_PREVIEW_BYTES + 10)
    result, _ = _run({"mode": "analyze"})
    assert "thumbnail" not in result["items"][0]


def test_config_keeps_stdout_clean_and_cookies_file_untouched(fake_gdl):
    _run({"mode": "analyze"})
    assert cfg(fake_gdl, "mode", "output") == "null"
    assert cfg(fake_gdl, "progress", "output") is False
    assert cfg(fake_gdl, "cookies-update") is False
    assert cfg(fake_gdl, "path-restrict") == "windows"
    assert cfg(fake_gdl, "sleep-request") == gallerydl.SOCIAL_SLEEP_REQUEST  # a social site
    assert cfg(fake_gdl, "cookies") is None  # no login unless the owner chose one


def test_no_extractor_is_unsupported(fake_gdl):
    fake_gdl["found"] = False
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"}, url="https://example.com/nothing")
    assert info.value.code == "unsupported"


def test_a_private_account_reads_as_one_the_login_offer_understands(fake_gdl):
    class AuthRequired(Exception):
        pass

    fake_gdl["data_exception"] = AuthRequired(f"Login required for https://x.example/?{SECRET}")
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "SUPERSECRET" not in info.value.message
    assert errors.needs_site_login(info.value.code, info.value.message)


def test_instagrams_login_redirect_offers_a_site_login(fake_gdl):
    """Seen live on public instagram.com/p/<id>/ posts fetched anonymously."""

    class AbortExtraction(Exception):
        pass

    fake_gdl["data_exception"] = AbortExtraction(
        f"HTTP redirect to login page (https://www.instagram.com/accounts/login/?{SECRET})"
    )
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "SUPERSECRET" not in info.value.message
    assert errors.needs_site_login(info.value.code, info.value.message)


def test_a_failure_inside_a_resolved_profile_link_is_reported_not_hidden(fake_gdl):
    """A profile link resolves through a child job whose error never reaches .exception.

    Seen live: an anonymous instagram.com/<user>/ analyze said "no media information found",
    when gallery-dl had actually hit the login wall ("Requested user could not be found").
    """
    fake_gdl["child_error"] = {
        "error": "AuthRequired",
        "message": f"Login required for https://x.example/?{SECRET}",
    }
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert "no media information" not in info.value.message
    assert "SUPERSECRET" not in info.value.message
    assert errors.needs_site_login(info.value.code, info.value.message)


# ── download ─────────────────────────────────────────────────────────────────────────────
def test_download_fetches_exactly_the_selected_positions(fake_gdl, tmp_path):
    result, events = _run(
        {"mode": "download", "preset": "gallery_original", "items": [4, 1, 2, 1]}, out=tmp_path
    )
    assert fake_gdl["download_ranges"] == ["1-2,4"]
    assert [Path(f).name for f in result["files"]] == [
        "instagram_ABC123_1.jpg",
        "instagram_ABC123_2.mp4",
        "instagram_ABC123_4.png",
    ]
    assert result["total_bytes"] == 1 + 2 + 4 and result["item_count"] == 3
    assert cfg(fake_gdl, "base-directory") == str(tmp_path) and cfg(fake_gdl, "directory") == []
    progress = [d for k, d in events if k == "progress"]
    assert progress[-1]["percent"] == 100.0 and progress[-1]["total_bytes"] == 3


def test_archive_path_comes_from_the_output_folder(fake_gdl, tmp_path):
    _run(
        {"mode": "download", "preset": "gallery_original", "items": [1], "archive": True},
        out=tmp_path,
    )
    assert cfg(fake_gdl, "archive") == str(tmp_path / gallerydl.ARCHIVE_FILENAME)


def test_everything_already_downloaded_is_a_skip_not_a_failure(fake_gdl, tmp_path):
    fake_gdl["write_files"] = False
    result, _ = _run(
        {"mode": "download", "preset": "gallery_original", "items": [1, 2]}, out=tmp_path
    )
    assert result["skipped"] is True and result["files"] == []


def test_a_failed_download_reports_redacted_logged_errors(fake_gdl, tmp_path):
    fake_gdl["download_fail"] = (
        f"AuthorizationError: https://i.example.com/x?{SECRET} Cookie: SID=abc"
    )
    with pytest.raises(EngineError) as info:
        _run({"mode": "download", "preset": "gallery_original", "items": [1]}, out=tmp_path)
    assert "SUPERSECRET" not in info.value.message and "SID=abc" not in info.value.message
    assert errors.needs_site_login(info.value.code, info.value.message)


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "download", "preset": "gallery_original"},
        {"mode": "download", "preset": "gallery_original", "items": []},
        {"mode": "download", "preset": "gallery_original", "items": [0]},
        {"mode": "download", "preset": "gallery_original", "items": [True]},
        {"mode": "download", "preset": "gallery_original", "items": ["1"]},
        {"mode": "download", "preset": "gallery_original", "items": list(range(1, 502))},
        {"mode": "download", "preset": "video_best", "items": [1]},
        {"mode": "download", "preset": "gallery_original", "items": [1], "directory": "C:/x"},
        {"mode": "download", "preset": "gallery_original", "items": [1], "archive": "yes"},
        {"mode": "analyze", "items": [1]},
        {"mode": "delete"},
    ],
)
def test_bad_options_are_refused(fake_gdl, options):
    with pytest.raises(EngineError) as info:
        _run(options)
    assert info.value.code == "bad_options"


@pytest.mark.parametrize(
    ("items", "expected"),
    [([1], "1"), ([1, 2, 3], "1-3"), ([1, 2, 3, 7], "1-3,7"), ([2, 4, 5, 6, 9], "2,4-6,9")],
)
def test_image_range(items, expected):
    assert gallerydl.image_range(items) == expected


# ── site login (plan §6.4) ───────────────────────────────────────────────────────────────
def test_a_cookie_file_login_is_preloaded_and_passed_by_path(fake_gdl, tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    _run({"mode": "analyze", "site_login": {"source": "file", "path": str(cookie)}})
    assert fake_gdl["cookie_loads"] == ["file"]
    assert cfg(fake_gdl, "cookies") == str(cookie) and cfg(fake_gdl, "cookies-update") is False


def test_a_browser_login_is_loaded_once_and_cached_for_the_extractor(fake_gdl):
    login = {"source": "browser", "browser": "firefox", "profile": "work"}
    _run({"mode": "analyze", "site_login": login})
    assert cfg(fake_gdl, "cookies") == ["firefox", "work"]
    assert fake_gdl["common"].CACHE_COOKIES[("firefox", "work")] == ["cookie-objects"]


@pytest.mark.parametrize("which", ["file", "browser"])
def test_a_login_that_cannot_load_is_fixed_text(fake_gdl, tmp_path, which):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("junk")
    fake_gdl["cookie_file_ok"] = fake_gdl["browser_ok"] = False
    login = (
        {"source": "file", "path": str(cookie)}
        if which == "file"
        else {"source": "browser", "browser": "firefox"}
    )
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze", "site_login": login})
    assert info.value.code == "cookies_unavailable"
    assert info.value.message == "the site login could not be read"
    assert info.value.__cause__ is None


def test_bad_site_login_is_refused_before_gallery_dl_runs(fake_gdl):
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze", "site_login": {"source": "browser", "browser": "ie"}})
    assert info.value.code == "bad_options" and fake_gdl["config"] == {}


# ── protocol ─────────────────────────────────────────────────────────────────────────────
def test_worker_emits_only_json_lines_for_a_gallery(fake_gdl, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    spec = JobSpec("job-9", "gallerydl", URL, ".", {"mode": "analyze"})
    assert worker_main.run_job("gallerydl", spec.to_json()) == 0
    lines = out.getvalue().splitlines()
    events = [Event.from_line(line) for line in lines]  # every line parses
    assert events[-1].type == "result" and events[-1].data["kind"] == "gallery"
    assert "SUPERSECRET" not in out.getvalue()


def test_missing_gallery_dl_is_engine_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "gallery_dl", None)
    with pytest.raises(EngineError) as info:
        _run({"mode": "analyze"})
    assert info.value.code == "engine_missing"
