"""The sign-in engine's cookies.txt (plan §6.4): written for one site, readable by both engines."""

from __future__ import annotations

import http.cookiejar
import os
from pathlib import Path

import pytest

from stuff_downloader.core import cookies
from stuff_downloader_worker.engines import get_engine
from stuff_downloader_worker.engines import signin as engine
from stuff_downloader_worker.engines.base import EngineError
from stuff_downloader_worker.protocol import JobSpec


def _c(domain, name="sessionid", value="v", path="/", secure=True, expires=0):
    return engine.Cookie(domain, path, secure, expires, name, value)


def _save(site, jar):
    return engine.save_signin(site, jar, cookies.signin_dir())


def test_sign_ins_live_under_the_local_data_folder():
    path = cookies.signin_path("instagram.com")
    assert path.name == "instagram.com.txt"
    assert Path(os.environ["LOCALAPPDATA"]) in path.parents
    assert engine.signin_url("instagram.com").startswith("https://www.instagram.com/")
    assert engine.signin_url("example.org") == "https://example.org/"


def test_only_the_sites_own_cookies_are_kept():
    path, count = _save(
        "instagram.com",
        [
            _c(".instagram.com", "sessionid"),
            _c("www.instagram.com", "csrftoken", secure=False),
            _c(".facebook.com", "c_user"),
            _c(".notinstagram.com", "x"),
        ],
    )
    assert path == cookies.signin_path("instagram.com") and count == 2
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tv" in text
    assert "www.instagram.com\tFALSE\t/\tFALSE\t0\tcsrftoken\tv" in text
    assert "c_user" not in text and "notinstagram" not in text
    login = cookies.SiteLogin("file", path=str(path))
    assert cookies.is_signin(login) and login.describe() == "Signed in"


def test_x_and_twitter_share_a_sign_in():
    path, _ = _save("x.com", [_c(".twitter.com", "auth_token"), _c(".x.com", "ct0")])
    text = path.read_text(encoding="utf-8")
    assert "auth_token" in text and "ct0" in text


def test_nothing_to_keep_writes_nothing():
    assert _save("tiktok.com", [_c(".instagram.com")]) is None
    assert not cookies.signin_path("tiktok.com").exists()


def test_a_field_that_would_break_the_format_is_dropped():
    text = engine.cookies_txt([_c(".a.com", value="x\ty"), _c(".a.com", name=""), _c(".a.com")])
    assert text.count("\n") == 3  # the two header lines and one cookie


def test_forget_deletes_only_sign_in_files(tmp_path):
    path, _ = _save("vimeo.com", [_c(".vimeo.com")])
    login = cookies.SiteLogin("file", path=str(path))
    own = tmp_path / "mine.txt"
    own.write_text("# Netscape HTTP Cookie File\n")
    cookies.forget(cookies.SiteLogin("file", path=str(own)))
    cookies.forget(cookies.SiteLogin("browser", browser="firefox"))
    cookies.forget(None)
    assert own.exists() and path.exists()
    cookies.forget(login)
    assert not path.exists()
    cookies.forget(login)  # already gone: no error


def test_the_file_passes_the_workers_checks_and_loads_in_yt_dlp_and_gallery_dl():
    path, _ = _save(
        "instagram.com",
        [_c(".instagram.com", "sessionid", "abc"), _c(".instagram.com", "ds", "1", expires=2**31)],
    )
    assert cookies.check_file(str(path)) == ""
    from stuff_downloader_worker import site_login

    login = {"source": "file", "path": str(path)}
    assert site_login.ydl_options(login) == {"cookiefile": str(path)}
    # The standard library's reader is what yt-dlp's jar extends.
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert {c.name: c.value for c in jar} == {"sessionid": "abc", "ds": "1"}
    yt_dlp = pytest.importorskip("yt_dlp.cookies")
    ytjar = yt_dlp.YoutubeDLCookieJar(str(path))
    ytjar.load()
    assert {c.name for c in ytjar} == {"sessionid", "ds"}


def test_gallery_dl_reads_the_file():
    util = pytest.importorskip("gallery_dl.util")
    path, _ = _save("reddit.com", [_c(".reddit.com", "token_v2", "t")])
    with open(path, encoding="utf-8") as fp:
        loaded = util.cookiestxt_load(fp)
    assert [(c.domain, c.name, c.value) for c in loaded] == [(".reddit.com", "token_v2", "t")]


# ── the engine's options ─────────────────────────────────────────────────────────────────
def _job(options, output_dir=None):
    folder = str(output_dir or cookies.signin_dir())
    return JobSpec("j", "signin", "https://instagram.com/", folder, options)


def test_the_engine_is_registered_and_takes_a_site_and_an_icon(tmp_path):
    assert get_engine("signin").name == "signin"
    icon = tmp_path / "app.ico"
    icon.write_bytes(b"ico")
    site, folder, chosen = engine.parse_options(_job({"site": "instagram.com", "icon": str(icon)}))
    assert (site, folder, chosen) == ("instagram.com", cookies.signin_dir(), icon)
    # A missing icon is not worth failing a sign-in over.
    missing = str(tmp_path / "no.ico")
    assert engine.parse_options(_job({"site": "x.com", "icon": missing}))[2] is None


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"site": "../../evil"},
        {"site": "C:\\Windows"},
        {"site": "localhost"},
        {"site": "instagram.com", "path": "x"},
        {"site": "instagram.com", "icon": "C:\\x.exe"},
        {"site": 5},
    ],
)
def test_the_engine_refuses_anything_but_a_host_name(options):
    with pytest.raises(EngineError) as info:
        engine.parse_options(_job(options))
    assert info.value.code == "bad_options"


def test_the_engine_needs_an_absolute_folder():
    with pytest.raises(EngineError):
        engine.parse_options(_job({"site": "instagram.com"}, output_dir="relative/folder"))
