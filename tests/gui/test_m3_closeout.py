"""M3 closeout in the GUI: direct files, the site-login offer, and subtitle/thumbnail rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stuff_downloader.core import cookies, protocol, settings, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages
from stuff_downloader.gui.main_window import MainWindow
from stuff_downloader.gui.widgets import SiteLoginDialog
from stuff_downloader_worker.engines import http as worker_http
from stuff_downloader_worker.engines import ytdlp as worker_ytdlp

FIXTURE = Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "youtube_video.json"
PRIVATE = "ERROR: [instagram] abc: Login required to access this post"


class FakeRun:
    instances: list[FakeRun] = []

    def __init__(self, spec, on_event):
        self.spec = spec
        self.on_event = on_event
        self.started = False
        FakeRun.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.emit("error", code="cancelled", message="Cancelled")

    def emit(self, event_type, /, **data):
        self.on_event(Event(event_type, self.spec.job_id, data))


@pytest.fixture
def runs(monkeypatch):
    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    return FakeRun.instances


@pytest.fixture
def page(qtbot, monkeypatch, runs):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    window = MainWindow(settings.Settings())
    qtbot.addWidget(window)
    yield window.downloads_page  # yield, not return: the window must outlive the test body


def _analyze(page, url):
    page.url_edit.setText(url)
    page.analyze()


def _stored_options(page):
    rows = page.store._conn.execute("SELECT options_json, url FROM jobs").fetchall()
    return [(json.loads(r[0]), r[1]) for r in rows]


def file_info(url="https://cdn.example.com/v/clip.mp4"):
    """The direct engine's MediaResult for a video file, with its one Original row."""
    return protocol.media_result(
        "video",
        ["video"],
        "clip",
        url,
        site="Direct file",
        extractor="Direct file",
        ext="mp4",
        filesize=2048,
        formats=[],
        **worker_http.file_rows("video", "mp4", 2048),
    )


def page_info(url, **fields):
    """The recorded yt-dlp page as the worker's MediaResult."""
    info = json.loads(FIXTURE.read_text(encoding="utf-8"))
    info.update(fields)
    return worker_ytdlp.analyze_result(info, url, None)


# ── direct files ─────────────────────────────────────────────────────────────────────────
def test_a_direct_file_is_analyzed_and_downloaded_by_the_http_engine(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/v/clip.mp4?sig=SECRET")
    (run,) = runs
    assert run.spec.engine == "http" and run.spec.options == {"mode": "analyze"}
    run.emit("result", **file_info())
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    card = page.result_card
    assert card.tab_names() == ["video"]
    assert [r["id"] for r in card.rows("video")] == ["v:orig"]
    assert card.cell_text("video", 0, 2) == "2.0 KB"
    job = page.start_download()
    assert job.spec.engine == "http"
    # The untouched title is not a name: the worker keeps the file's own name.
    assert job.spec.options == {
        "mode": "download",
        "tab": "video",
        "row_id": "v:orig",
        "container": "mp4",
    }
    ((options, url),) = _stored_options(page)
    assert url == "https://cdn.example.com/v/clip.mp4"  # the token never reaches disk
    assert "SECRET" not in json.dumps(options)


def test_a_video_page_after_a_file_gets_its_own_rows_back(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/clip.mp4")
    runs[-1].emit("result", **file_info("https://cdn.example.com/clip.mp4"))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    _analyze(page, "https://youtu.be/dQw4w9WgXcQ")
    runs[-1].emit("result", **page_info(runs[-1].spec.url))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    card = page.result_card
    ids = [r["id"] for r in card.rows("video")]
    assert "v:orig" not in ids and "v:1080:mp4" in ids
    assert card.tab_names() == ["video", "audio", "image"]


def test_an_unknown_page_falls_back_to_gallery_then_direct_engine_once_each(page, runs):
    _analyze(page, "https://media.example.net/get?id=7")
    assert runs[0].spec.engine == "ytdlp"
    runs[0].emit("error", code="unsupported", message="ERROR: Unsupported URL: [link]")
    assert len(runs) == 2
    assert runs[1].spec.engine == "gallerydl" and runs[1].spec.url == runs[0].spec.url
    runs[1].emit("error", code="unsupported", message="unsupported url: no gallery found")
    assert len(runs) == 3
    assert runs[2].spec.engine == "http" and runs[2].spec.url == runs[0].spec.url
    runs[2].emit("error", code="unsupported", message="unsupported url: not a media file")
    assert len(runs) == 4
    assert runs[3].spec.engine == "social" and runs[3].spec.url == runs[0].spec.url  # og:image
    runs[3].emit("error", code="unsupported", message="unsupported url: no picture found")
    assert len(runs) == 4  # no fifth attempt
    assert "video, photo or file" in page.message_label.text()


def test_youtube_never_falls_back_to_the_direct_engine(page, runs):
    _analyze(page, "https://youtu.be/dQw4w9WgXcQ")
    runs[0].emit("error", code="unsupported", message="ERROR: Unsupported URL")
    assert len(runs) == 1


# ── the site-login offer (plan §6.4) ─────────────────────────────────────────────────────
def test_the_login_offer_appears_only_after_a_private_on_the_site_error(page, runs):
    assert page.login_button.isHidden()
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="download_error", message=PRIVATE)
    assert not page.login_button.isHidden()
    assert "not public" in page.message_label.text()
    _analyze(page, "https://www.instagram.com/reel/abc/")
    assert page.login_button.isHidden()  # a new analyze clears the offer
    runs[-1].emit("error", code="download_error", message="HTTP Error 404: Not Found")
    assert page.login_button.isHidden()


def test_a_failed_login_does_not_offer_another(page, runs):
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="cookies_unavailable", message="the site login could not be read")
    assert page.login_button.isHidden()
    assert "could not be used" in page.message_label.text()


COOKIES_FAILED = "the site login could not be read"


def _signed_in(site):
    """A login as the sign-in engine leaves it: a cookies.txt in the sign-ins folder."""
    path = cookies.signin_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Netscape HTTP Cookie File\n.{site}\tTRUE\t/\tTRUE\t0\tsid\tv\n")
    return cookies.SiteLogin("file", path=str(path))


def test_the_offer_names_the_site_to_sign_in_to(page, runs):
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="download_error", message=PRIVATE)
    assert page.login_button.text() == "Sign in to instagram.com…"


def test_an_unreadable_saved_login_is_skipped_and_analyze_retries_without_it(page, runs):
    chrome = cookies.SiteLogin("browser", browser="chrome")
    page.set_site_login("youtube.com", chrome)
    _analyze(page, "https://music.youtube.com/playlist?list=PLRp25R3Hvv1A")
    assert runs[-1].spec.options["site_login"] == chrome.to_dict()
    runs[-1].emit("error", code="cookies_unavailable", message=COOKIES_FAILED)
    retry = runs[-1]
    assert "site_login" not in retry.spec.options
    assert "Trying without it" in page.message_label.text()
    # The saved choice stays; it is only skipped for this session, for the whole site.
    assert settings.load().site_logins == {"youtube.com": chrome.to_dict()}
    retry.emit("error", code="download_error", message="HTTP Error 404")
    count = len(runs)
    _analyze(page, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert len(runs) == count + 1 and "site_login" not in runs[-1].spec.options


def test_signing_in_again_replaces_a_skipped_login_straight_away(page, runs):
    page.set_site_login("youtube.com", cookies.SiteLogin("browser", browser="chrome"))
    _analyze(page, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    runs[-1].emit("error", code="cookies_unavailable", message=COOKIES_FAILED)
    runs[-1].emit("error", code="download_error", message="HTTP Error 404")  # the retry ends
    signed = _signed_in("youtube.com")
    page.set_site_login("youtube.com", signed)
    _analyze(page, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert runs[-1].spec.options["site_login"] == signed.to_dict()


def test_a_skipped_login_is_retried_only_once(page, runs):
    page.set_site_login("youtube.com", cookies.SiteLogin("browser", browser="chrome"))
    _analyze(page, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    runs[-1].emit("error", code="cookies_unavailable", message=COOKIES_FAILED)
    count = len(runs)
    runs[-1].emit("error", code="cookies_unavailable", message=COOKIES_FAILED)
    assert len(runs) == count
    assert "could not be used" in page.message_label.text()


def test_a_download_with_an_unreadable_login_retries_without_it(page, runs, qtbot):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    _analyze(page, url)
    runs[-1].emit("result", **page_info(url))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    page.set_site_login("youtube.com", cookies.SiteLogin("browser", browser="chrome"))
    job = page.start_download()
    qtbot.waitUntil(lambda: runs[-1].spec.job_id == job.spec.job_id)
    assert "site_login" in runs[-1].spec.options
    runs[-1].emit("error", code="cookies_unavailable", message=COOKIES_FAILED)
    assert job.state == "retrying"
    page._release_retry(job.spec.job_id)
    qtbot.waitUntil(lambda: runs[-1].spec.job_id == job.spec.job_id and runs[-1].started)
    assert "site_login" not in runs[-1].spec.options


class StubDialog:
    def __init__(self, accepted, choice):
        self.accepted, self._choice = accepted, choice

    def exec(self):
        return int(self.accepted)

    def choice(self):
        return self._choice


def test_choosing_a_login_saves_the_choice_and_retries_with_it(page, runs, monkeypatch, qtbot):
    firefox = cookies.SiteLogin("browser", browser="firefox", profile="work")
    seen = {}

    def factory(site, current):
        seen.update(site=site, current=current)
        return StubDialog(True, firefox)

    monkeypatch.setattr(page, "_login_dialog", factory)
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="download_error", message=PRIVATE)
    assert page.offer_site_login()
    assert seen == {"site": "instagram.com", "current": None}
    assert settings.load().site_logins == {"instagram.com": firefox.to_dict()}
    retry = runs[-1]
    assert retry.spec.options["site_login"] == firefox.to_dict()

    # The download carries it to the worker, but the stored job never does.
    retry.emit("result", **page_info(retry.spec.url))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    job = page.start_download()
    assert "site_login" not in job.spec.options
    worker_spec = page._with_site_login(job.spec)
    assert worker_spec.options["site_login"] == firefox.to_dict()
    assert all("site_login" not in options for options, _ in _stored_options(page))
    db_text = json.dumps(_stored_options(page))
    assert "firefox" not in db_text and "work" not in db_text


def test_signing_out_from_the_offer_deletes_the_sign_in_and_retries_nothing(
    page, runs, monkeypatch
):
    signed = _signed_in("instagram.com")
    page.set_site_login("instagram.com", signed)
    monkeypatch.setattr(page, "_login_dialog", lambda site, current: StubDialog(True, None))
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="download_error", message=PRIVATE)
    count = len(runs)
    assert page.offer_site_login()
    assert len(runs) == count
    assert settings.load().site_logins == {}
    assert not Path(signed.path).exists()


def test_cancelling_the_dialog_changes_nothing(page, runs, monkeypatch):
    monkeypatch.setattr(page, "_login_dialog", lambda site, current: StubDialog(False, None))
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("error", code="download_error", message=PRIVATE)
    count = len(runs)
    assert not page.offer_site_login()
    assert len(runs) == count and settings.load().site_logins == {}


def test_no_login_removes_a_saved_choice(page, tmp_path):
    choice = cookies.SiteLogin("browser", browser="firefox")
    assert page.set_site_login("www.instagram.com", choice)
    assert settings.load().site_logins == {"instagram.com": choice.to_dict()}
    assert page.set_site_login("instagram.com", None)
    assert settings.load().site_logins == {}


def test_logins_apply_only_to_their_site_and_never_to_the_http_engine(page):
    page.set_site_login("instagram.com", cookies.SiteLogin("browser", browser="firefox"))
    from stuff_downloader.core.protocol import JobSpec

    other = JobSpec("a", "ytdlp", "https://vimeo.com/1", ".", {"mode": "analyze"})
    direct = JobSpec("b", "http", "https://www.instagram.com/x.mp4", ".", {"mode": "analyze"})
    assert page._with_site_login(other) is other
    assert page._with_site_login(direct) is direct


def test_dialog_signs_in_through_the_sign_in_window(qtbot):
    signed = cookies.SiteLogin("file", path=str(cookies.signin_path("instagram.com")))
    seen = []

    def window(site, parent):
        seen.append(site)
        return signed

    dialog = SiteLoginDialog("instagram.com", signin=window)
    qtbot.addWidget(dialog)
    assert "not signed in" in dialog.status_label.text()
    assert dialog.signout_button.isHidden() and dialog.advanced_box.isHidden()
    assert dialog.sign_in()
    assert seen == ["instagram.com"] and dialog.choice() == signed
    assert dialog.result() == SiteLoginDialog.DialogCode.Accepted


def test_a_cancelled_sign_in_keeps_the_dialog_open(qtbot):
    dialog = SiteLoginDialog("instagram.com", signin=lambda site, parent: None)
    qtbot.addWidget(dialog)
    assert not dialog.sign_in()
    assert dialog.result() != SiteLoginDialog.DialogCode.Accepted


def test_dialog_signs_out(qtbot):
    current = cookies.SiteLogin("file", path=str(cookies.signin_path("x.com")))
    dialog = SiteLoginDialog("x.com", current)
    qtbot.addWidget(dialog)
    assert "Signed in" in dialog.status_label.text()
    assert not dialog.signout_button.isHidden()
    dialog.sign_out()
    assert dialog.choice() is None
    assert dialog.result() == SiteLoginDialog.DialogCode.Accepted


def test_other_ways_offer_firefox_or_a_file_and_validate_them(qtbot, tmp_path):
    dialog = SiteLoginDialog("instagram.com")
    qtbot.addWidget(dialog)
    dialog.browser_radio.setChecked(True)
    dialog.profile_edit.setText("..\\..\\Windows")
    with pytest.raises(ValueError):
        dialog.other_way()
    dialog.profile_edit.setText("default-release")
    assert dialog.other_way() == cookies.SiteLogin(
        "browser", browser="firefox", profile="default-release"
    )
    dialog.file_radio.setChecked(True)
    dialog.file_edit.setText(str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError):
        dialog.other_way()
    dialog._use_other_way()
    assert not dialog.error_label.isHidden()
    good = tmp_path / "cookies.txt"
    good.write_text("# Netscape HTTP Cookie File\n")
    dialog.file_edit.setText(str(good))
    dialog._use_other_way()
    assert dialog.choice() == cookies.SiteLogin("file", path=str(good))
    # Chrome, Edge and Brave are not offered: they cannot be read on Windows.
    assert "Chrome" in cookies.GUIDANCE and "Sign in above" in cookies.GUIDANCE


def test_dialog_shows_an_older_browser_choice(qtbot):
    dialog = SiteLoginDialog("x.com", cookies.SiteLogin("browser", browser="edge", profile="p"))
    qtbot.addWidget(dialog)
    assert "Edge" in dialog.status_label.text()
    assert dialog.browser_radio.isChecked() and dialog.profile_edit.text() == "p"


# ── Settings: site logins ────────────────────────────────────────────────────────────────
def _settings_page(qtbot, app_settings):
    page = pages.SettingsPage(app_settings)
    qtbot.addWidget(page)
    return page


def _login_texts(page):
    out = []
    for row in page._login_widgets:
        if isinstance(row, pages.QLabel):
            out.append(row.text())
        for widget in row.findChildren(pages.QLabel) + row.findChildren(pages.QPushButton):
            out.append(widget.text())
    return out


def test_settings_list_saved_logins_and_sign_out_deletes_the_file(qtbot):
    app_settings = settings.Settings()
    page = _settings_page(qtbot, app_settings)
    assert _login_texts(page) == ["None saved."]
    signed = _signed_in("instagram.com")
    pages.save_site_login(page, app_settings, "instagram.com", signed)
    pages.save_site_login(
        page, app_settings, "youtube.com", cookies.SiteLogin("browser", browser="chrome")
    )
    page.refresh_logins()
    texts = _login_texts(page)
    assert "instagram.com  ·  Signed in" in texts and "Sign out" in texts
    assert "youtube.com  ·  Chrome  (can't be read on Windows: sign in again)" in texts
    assert "Remove" in texts
    assert page.remove_login("instagram.com")
    assert not Path(signed.path).exists()
    assert settings.load().site_logins == {
        "youtube.com": {"source": "browser", "browser": "chrome", "profile": ""}
    }
    assert not page.remove_login("instagram.com")


def test_settings_sign_in_to_a_typed_site(qtbot, monkeypatch):
    app_settings = settings.Settings()
    page = _settings_page(qtbot, app_settings)
    monkeypatch.setattr(page, "_ask_site", lambda: "tiktok.com")
    signed = _signed_in("tiktok.com")
    monkeypatch.setattr(page, "_sign_in_window", lambda site: signed)
    assert page.choose_site_to_sign_in()
    assert settings.load().site_logins == {"tiktok.com": signed.to_dict()}
    assert "tiktok.com  ·  Signed in" in _login_texts(page)


def test_a_file_the_owner_picked_is_never_deleted(qtbot, tmp_path):
    app_settings = settings.Settings()
    page = _settings_page(qtbot, app_settings)
    own = tmp_path / "mine.txt"
    own.write_text("# Netscape HTTP Cookie File\n")
    pages.save_site_login(page, app_settings, "x.com", cookies.SiteLogin("file", path=str(own)))
    assert page.remove_login("x.com")
    assert own.exists()


def test_stored_error_text_loses_auth_values():
    text = pages.safe_error_message("failed {'Authorization': 'Bearer abc123token'}")
    assert "abc123token" not in text


# ── All formats: subtitle and thumbnail rows ─────────────────────────────────────────────
def test_track_rows_list_subtitles_captions_and_thumbnails():
    info = {
        "subtitles": [{"lang": "en", "exts": ["vtt", "srt"], "auto": False}, {"lang": "<b>"}],
        "automatic_captions": [
            {"lang": lang, "exts": ["vtt"], "auto": True} for lang in ("en", "fr")
        ],
        "thumbnails": [{"width": 1280, "height": 720}, {"width": "x", "height": 1}],
    }
    assert pages.track_rows(info) == [
        ("Subtitles", "en", "VTT / SRT", "—", "—"),
        ("Auto captions", "2 languages", "VTT", "—", "—"),
        ("Thumbnail", "1280×720", "—", "—", "—"),
    ]
    assert pages.track_rows(None) == [] and pages.track_rows({"subtitles": "x"}) == []


def test_the_advanced_table_shows_track_rows(page, runs, qtbot):
    _analyze(page, "https://youtu.be/dQw4w9WgXcQ")
    info = page_info(
        runs[-1].spec.url,
        subtitles=[{"lang": "en", "exts": ["vtt"], "auto": False}],
        thumbnails=[{"width": 1280, "height": 720}],
    )
    runs[-1].emit("result", **info)
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    table = page.advanced_table
    kinds = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert "Subtitles" in kinds and "Thumbnail" in kinds


# ── the offer also follows a failed download (review finding) ────────────────────────────
def _queued_instagram_job(page, runs, qtbot):
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("result", **page_info(runs[-1].spec.url))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    job = page.start_download()
    run = runs[-1]
    assert run.spec.job_id == job.spec.job_id and run.started
    return job, run


def test_a_download_that_turns_private_offers_a_login_and_retries_that_job(
    page, runs, monkeypatch, qtbot
):
    firefox = cookies.SiteLogin("browser", browser="firefox")
    monkeypatch.setattr(page, "_login_dialog", lambda site, current: StubDialog(True, firefox))
    job, run = _queued_instagram_job(page, runs, qtbot)
    assert page.login_button.isHidden()
    run.emit("error", code="download_error", message=PRIVATE)
    assert job.state == "failed"
    assert not page.login_button.isHidden()
    assert "instagram.com" in page.message_label.text()

    count = len(runs)
    page.url_edit.setText("https://youtu.be/dQw4w9WgXcQ")  # the box no longer names that job
    assert page.offer_site_login()
    assert len(runs) == count + 1
    retry = runs[-1]
    assert retry.spec.url == job.spec.url and retry.spec.engine == "ytdlp"
    assert retry.spec.options["site_login"] == firefox.to_dict()  # the worker gets it
    assert "site_login" not in job.spec.options  # the job does not
    assert all("site_login" not in options for options, _ in _stored_options(page))


def test_other_download_failures_do_not_offer_a_login(page, runs, qtbot):
    job, run = _queued_instagram_job(page, runs, qtbot)
    run.emit("error", code="download_error", message="This video is DRM protected")
    assert job.state == "failed" and page.login_button.isHidden()


def test_a_failed_direct_file_download_never_offers_a_login(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/clip.mp4")
    runs[-1].emit("result", **file_info("https://cdn.example.com/clip.mp4"))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    page.start_download()
    runs[-1].emit("error", code="download_error", message="HTTP Error 401: Unauthorized")
    assert page.login_button.isHidden()


def test_a_new_analyze_forgets_a_pending_job_retry(page, runs, monkeypatch, qtbot):
    firefox = cookies.SiteLogin("browser", browser="firefox")
    monkeypatch.setattr(page, "_login_dialog", lambda site, current: StubDialog(True, firefox))
    _, run = _queued_instagram_job(page, runs, qtbot)
    run.emit("error", code="download_error", message=PRIVATE)
    _analyze(page, "https://www.instagram.com/reel/other/")
    assert page._login_retry_job_id == "" and page.login_button.isHidden()


def test_an_edited_direct_file_title_reaches_the_job_spec(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/v/clip.mp4")
    runs[-1].emit("result", **file_info())
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    editor = page.result_card.title_editor
    editor.start_editing()
    editor.edit.setText("  holiday  ")
    editor.edit.editingFinished.emit()
    assert page.start_download().spec.options["edited_title"] == "holiday"
