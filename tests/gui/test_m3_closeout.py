"""M3 closeout in the GUI: direct files, the site-login offer, and subtitle/thumbnail rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stuff_downloader.core import cookies, settings, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages
from stuff_downloader.gui.main_window import MainWindow
from stuff_downloader.gui.widgets import SiteLoginDialog

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


FILE_INFO = {
    "kind": "file",
    "title": "clip",
    "extractor": "Direct file",
    "ext": "mp4",
    "filesize": 2048,
    "formats": [],
}


# ── direct files ─────────────────────────────────────────────────────────────────────────
def test_a_direct_file_is_analyzed_and_downloaded_by_the_http_engine(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/v/clip.mp4?sig=SECRET")
    (run,) = runs
    assert run.spec.engine == "http" and run.spec.options == {"mode": "analyze"}
    run.emit("result", **FILE_INFO)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    combo = page.preview.preset_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["original_file"]
    assert not page.preview.resolution_combo.isEnabled()
    assert page.preview.crop_check.isHidden() and page.preview.compatible_check.isHidden()
    assert "2.0 KB" in page.preview.meta_label.text()
    job = page.start_download()
    assert job.spec.engine == "http"
    # The untouched name field is only a hint: the worker keeps the file's own name.
    assert page.preview.name_edit.placeholderText() == "clip"
    assert job.spec.options == {"mode": "download", "preset": "original_file"}
    ((options, url),) = _stored_options(page)
    assert url == "https://cdn.example.com/v/clip.mp4"  # the token never reaches disk
    assert "SECRET" not in json.dumps(options)


def test_a_video_page_after_a_file_gets_the_video_presets_back(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/clip.mp4")
    runs[-1].emit("result", **FILE_INFO)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    _analyze(page, "https://youtu.be/dQw4w9WgXcQ")
    runs[-1].emit("result", **json.loads(FIXTURE.read_text(encoding="utf-8")))
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    combo = page.preview.preset_combo
    ids = [combo.itemData(i) for i in range(combo.count())]
    assert "original_file" not in ids and "video_1080" in ids
    assert combo.currentData() == "video_1080"


def test_an_unknown_page_probes_direct_then_video_and_gallery_once_each(page, runs):
    _analyze(page, "https://media.example.net/get?id=7")
    assert runs[0].spec.engine == "http"
    runs[0].emit("error", code="unsupported", message="unsupported url: not a media file")
    assert len(runs) == 2
    assert runs[1].spec.engine == "ytdlp" and runs[1].spec.url == runs[0].spec.url
    runs[1].emit("error", code="unsupported", message="ERROR: Unsupported URL: [link]")
    assert len(runs) == 3
    assert runs[2].spec.engine == "gallerydl" and runs[2].spec.url == runs[0].spec.url
    runs[2].emit("error", code="unsupported", message="unsupported url: no gallery found")
    assert len(runs) == 3  # no fourth attempt
    assert "not a video page" in page.message_label.text()


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
    assert "could not be read" in page.message_label.text()


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
    retry.emit("result", **json.loads(FIXTURE.read_text(encoding="utf-8")))
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    job = page.start_download()
    assert "site_login" not in job.spec.options
    worker_spec = page._with_site_login(job.spec)
    assert worker_spec.options["site_login"] == firefox.to_dict()
    assert all("site_login" not in options for options, _ in _stored_options(page))
    db_text = json.dumps(_stored_options(page))
    assert "firefox" not in db_text and "work" not in db_text


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


def test_dialog_validates_before_returning_a_choice(qtbot, tmp_path):
    dialog = SiteLoginDialog("instagram.com")
    qtbot.addWidget(dialog)
    assert dialog.none_radio.isChecked() and dialog.choice() is None
    dialog.browser_radio.setChecked(True)
    dialog.profile_edit.setText("..\\..\\Windows")
    with pytest.raises(ValueError):
        dialog.choice()
    dialog.profile_edit.setText("default-release")
    assert dialog.choice() == cookies.SiteLogin(
        "browser", browser="firefox", profile="default-release"
    )
    dialog.file_radio.setChecked(True)
    dialog.file_edit.setText(str(tmp_path / "missing.txt"))
    with pytest.raises(ValueError):
        dialog.choice()
    good = tmp_path / "cookies.txt"
    good.write_text("# Netscape HTTP Cookie File\n")
    dialog.file_edit.setText(str(good))
    assert dialog.choice() == cookies.SiteLogin("file", path=str(good))
    assert "Firefox" in cookies.GUIDANCE and "secret" in cookies.GUIDANCE


def test_dialog_shows_the_current_choice(qtbot):
    dialog = SiteLoginDialog("x.com", cookies.SiteLogin("browser", browser="edge", profile="p"))
    qtbot.addWidget(dialog)
    assert dialog.browser_radio.isChecked()
    assert dialog.browser_combo.currentData() == "edge" and dialog.profile_edit.text() == "p"


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
    info = json.loads(FIXTURE.read_text(encoding="utf-8"))
    info["subtitles"] = [{"lang": "en", "exts": ["vtt"], "auto": False}]
    info["thumbnails"] = [{"width": 1280, "height": 720}]
    runs[-1].emit("result", **info)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    table = page.advanced_table
    kinds = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert "Subtitles" in kinds and "Thumbnail" in kinds


# ── the offer also follows a failed download (review finding) ────────────────────────────
def _queued_instagram_job(page, runs, qtbot):
    _analyze(page, "https://www.instagram.com/reel/abc/")
    runs[-1].emit("result", **json.loads(FIXTURE.read_text(encoding="utf-8")))
    qtbot.waitUntil(lambda: not page.preview.isHidden())
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
    runs[-1].emit("result", **FILE_INFO)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
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


def test_a_typed_direct_file_name_reaches_the_job_spec(page, runs, qtbot):
    _analyze(page, "https://cdn.example.com/v/clip.mp4")
    runs[-1].emit("result", **FILE_INFO)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    page.preview.name_edit.setText("  holiday  ")
    assert page.start_download().spec.options["output_name"] == "holiday"
