"""Per-site login choices (plan §6.4): stored as a choice only, validated twice, never leaked."""

from __future__ import annotations

import json
import sys

import pytest

from stuff_downloader.core import cookies, errors, settings
from stuff_downloader_worker import site_login
from stuff_downloader_worker.engines import get_engine
from stuff_downloader_worker.engines import ytdlp as ytdlp_engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec

SECRET = "SID=supersecretcookievalue"


@pytest.fixture
def cookie_file(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(f"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tSID\t{SECRET}\n")
    return path


# ── core: the stored choice ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "site"),
    [
        ("https://www.instagram.com/p/abc/", "instagram.com"),
        ("https://m.facebook.com/x", "facebook.com"),
        ("www.x.com", "x.com"),
        ("https://vimeo.com/1", "vimeo.com"),
        ("localhost", ""),
        ("a/b.com", ""),
        ("", ""),
    ],
)
def test_site_key(text, site):
    assert cookies.site_key(text) == site


@pytest.mark.parametrize(
    "value",
    [
        None,
        "firefox",
        {"source": "browser", "browser": "netscape"},
        {"source": "browser", "browser": "firefox", "profile": "..\\..\\Windows"},
        {"source": "browser", "browser": "firefox", "profile": ".."},
        {"source": "browser", "browser": "firefox", "profile": "C:/Users/x"},
        {"source": "file", "path": "relative/cookies.txt"},
        {"source": "file", "path": "C:/secrets/cookies.sqlite"},
        {"source": "file", "path": ""},
        {"source": "cookie", "value": SECRET},
    ],
)
def test_malformed_choices_are_dropped(value):
    assert cookies.parse(value) is None


def test_valid_choices_round_trip(tmp_path):
    browser = cookies.parse({"source": "browser", "browser": "firefox", "profile": " work "})
    assert browser == cookies.SiteLogin("browser", browser="firefox", profile="work")
    file_path = str(tmp_path / "c.txt")
    file_choice = cookies.parse({"source": "file", "path": file_path})
    assert cookies.parse(file_choice.to_dict()) == file_choice
    assert browser.describe() == "Firefox (work)"


def test_choice_matches_the_site_and_its_subdomains_only():
    choices = {"instagram.com": cookies.SiteLogin("browser", browser="firefox")}
    assert cookies.choice_for("https://www.instagram.com/p/1", choices)
    assert cookies.choice_for("https://scontent.instagram.com/p/1", choices)
    assert cookies.choice_for("https://notinstagram.com/p/1", choices) is None
    assert cookies.choice_for("https://instagram.com.evil.example/p/1", choices) is None


def test_settings_store_the_choice_never_the_cookies(tmp_path, cookie_file):
    path = tmp_path / "settings.json"
    s = settings.Settings()
    s.site_logins = {"instagram.com": {"source": "file", "path": str(cookie_file)}}
    settings.save(s, path)
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text and str(cookie_file).replace("\\", "\\\\") in text
    assert settings.load(path).site_logins == s.site_logins


def test_settings_default_to_no_logins_and_drop_tampered_entries(tmp_path):
    assert settings.Settings().site_logins == {}
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "site_logins": {
                    "instagram.com": {"source": "browser", "browser": "firefox"},
                    "localhost": {"source": "browser", "browser": "firefox"},
                    "x.com": {"source": "browser", "browser": "firefox", "profile": "../../x"},
                    "vimeo.com": "not an object",
                }
            }
        ),
        encoding="utf-8",
    )
    assert settings.load(path).site_logins == {
        "instagram.com": {"source": "browser", "browser": "firefox", "profile": ""}
    }


def test_check_file_reads_only_the_size(tmp_path, cookie_file):
    assert cookies.check_file(str(cookie_file)) == ""
    assert "cookies.txt" in cookies.check_file(str(tmp_path / "c.sqlite"))
    assert "not be found" in cookies.check_file(str(tmp_path / "missing.txt"))
    (tmp_path / "empty.txt").write_text("")
    assert "empty" in cookies.check_file(str(tmp_path / "empty.txt"))


# ── core: when a login is offered ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [instagram] abc: Login required to access this post",
        "ERROR: Private video. Sign in if you've been granted access",
        "ERROR: Sign in to confirm your age",
        "HTTP Error 401: Unauthorized",
        "This account is private",
    ],
)
def test_private_on_the_site_offers_a_login(message):
    assert errors.needs_site_login("download_error", message)
    assert "not public" in errors.friendly_message("download_error", message).lower() or (
        "private" in errors.friendly_message("download_error", message).lower()
        or "age" in errors.friendly_message("download_error", message).lower()
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("download_error", "HTTP Error 404: Not Found"),
        ("download_error", "This video is DRM protected"),
        ("unsupported", "Unsupported URL"),
        ("download_error", "not available in your country"),
        ("cookies_unavailable", "Login required"),  # a failed login never offers another
    ],
)
def test_other_failures_do_not_offer_a_login(code, message):
    assert not errors.needs_site_login(code, message)


def test_cookie_failure_has_its_own_plain_message():
    text = errors.friendly_message("cookies_unavailable", site_login.COOKIES_FAILED)
    assert "could not be read" in text and "cookies.txt" in text


# ── worker: re-validation and yt-dlp options ─────────────────────────────────────────────
def test_worker_builds_browser_options():
    opts = site_login.ydl_options({"source": "browser", "browser": "firefox", "profile": ""})
    assert opts == {"cookiesfrombrowser": ("firefox", None, None, None)}


def test_worker_builds_file_options(cookie_file):
    opts = site_login.ydl_options({"source": "file", "path": str(cookie_file)})
    assert opts == {"cookiefile": str(cookie_file)}


@pytest.mark.parametrize(
    "value",
    [
        "firefox",
        {"source": "browser", "browser": "ie"},
        {"source": "browser", "browser": "firefox", "profile": "C:\\x"},
        {"source": "browser", "browser": "firefox", "extra": 1},
        {"source": "file", "path": "cookies.txt"},
        {"source": "file", "path": "C:/x/cookies.sqlite"},
    ],
)
def test_worker_refuses_bad_logins(value):
    with pytest.raises(EngineError) as info:
        site_login.ydl_options(value)
    assert info.value.code == "bad_options"


def test_worker_reports_a_missing_cookie_file_without_its_path(tmp_path):
    path = tmp_path / "gone" / "cookies.txt"
    with pytest.raises(EngineError) as info:
        site_login.ydl_options({"source": "file", "path": str(path)})
    assert info.value.code == "cookies_unavailable" and str(tmp_path) not in info.value.message


@pytest.mark.parametrize(
    "text",
    [
        f"request failed; Cookie: {SECRET}; path=/",
        f"headers: {{'Authorization': 'Bearer {SECRET}'}}",
        f"set-cookie={SECRET}",
    ],
)
def test_engine_error_text_loses_cookie_and_auth_values(text):
    _, message = ytdlp_engine.describe_download_error(text)
    assert "supersecret" not in message and "[removed]" in message


# ── worker: the yt-dlp engine with a login ───────────────────────────────────────────────
class _Jar:
    pass


@pytest.fixture
def fake_ydl(monkeypatch):
    state = {"opts": None, "fail_cookies": False, "closed_params": None}

    class FakeDownloadError(Exception):
        pass

    class FakeYDL:
        def __init__(self, opts):
            state["opts"] = opts
            self.params = dict(opts)

        @property
        def cookiejar(self):
            if state["fail_cookies"]:
                raise FakeDownloadError(f"could not open C:/Users/me/cookies.txt: {SECRET}")
            return _Jar()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            state["closed_params"] = dict(self.params)  # what close() would save cookies from
            return False

        def extract_info(self, url, download=False):
            raise FakeDownloadError("ERROR: Private video. Sign in if you've been granted access")

    fake = type(sys)("yt_dlp")
    fake.YoutubeDL = FakeYDL
    fake.version = type(sys)("yt_dlp.version")
    fake.version.__version__ = "test"
    fake.utils = type(sys)("yt_dlp.utils")
    fake.utils.DownloadError = FakeDownloadError
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    monkeypatch.delenv(ytdlp_engine.TOOLS_DIR_ENV_VAR, raising=False)
    return state


def _spec(options):
    return JobSpec("j", "ytdlp", "https://www.instagram.com/p/abc/", ".", options)


def test_engine_passes_the_file_login_and_never_saves_back_to_it(fake_ydl, cookie_file):
    login = {"source": "file", "path": str(cookie_file)}
    with pytest.raises(EngineError):
        get_engine("ytdlp").download(_spec({"mode": "analyze", "site_login": login}), lambda *a: 0)
    assert fake_ydl["opts"]["cookiefile"] == str(cookie_file)
    assert isinstance(fake_ydl["opts"]["logger"], site_login.SilentLogger)
    # yt-dlp's close() writes the jar to params["cookiefile"]; it must be gone by then.
    assert fake_ydl["closed_params"]["cookiefile"] is None
    assert SECRET in cookie_file.read_text()  # the owner's file is untouched


def test_engine_passes_a_browser_login(fake_ydl):
    login = {"source": "browser", "browser": "firefox", "profile": "work"}
    with pytest.raises(EngineError):
        get_engine("ytdlp").download(_spec({"mode": "analyze", "site_login": login}), lambda *a: 0)
    assert fake_ydl["opts"]["cookiesfrombrowser"] == ("firefox", "work", None, None)


def test_a_cookie_load_failure_is_fixed_text(fake_ydl, cookie_file):
    fake_ydl["fail_cookies"] = True
    login = {"source": "file", "path": str(cookie_file)}
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec({"mode": "analyze", "site_login": login}), lambda *a: 0)
    assert info.value.code == "cookies_unavailable"
    assert info.value.message == site_login.COOKIES_FAILED
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_without_a_login_no_cookie_options_reach_ytdlp(fake_ydl):
    with pytest.raises(EngineError):
        get_engine("ytdlp").download(_spec({"mode": "analyze"}), lambda *a: 0)
    opts = fake_ydl["opts"]
    assert "cookiefile" not in opts and "cookiesfrombrowser" not in opts and "logger" not in opts


def test_download_mode_accepts_a_login_alongside_the_preset(fake_ydl):
    options = {
        "mode": "download",
        "preset": "video_best",
        "site_login": {"source": "browser", "browser": "firefox"},
    }
    with pytest.raises(EngineError) as info:
        get_engine("ytdlp").download(_spec(options), lambda *a: 0)
    assert info.value.code != "bad_options"
    assert fake_ydl["opts"]["cookiesfrombrowser"][0] == "firefox"
