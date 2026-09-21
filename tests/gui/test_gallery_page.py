"""M4 in the GUI: gallery grid, exact selection, queueing, and the site login for gallery-dl."""

from __future__ import annotations

import base64
import json

import pytest
from PyQt6.QtCore import Qt

from stuff_downloader.core import cookies, settings, tools
from stuff_downloader.core.protocol import Event, JobSpec
from stuff_downloader.gui import pages
from stuff_downloader.gui.main_window import MainWindow

URL = "https://www.instagram.com/p/C0ffee123/?img_index=1"


class FakeRun:
    instances: list = []

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
    yield window.downloads_page  # the window must outlive the test body


def _png():
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QImage

    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return base64.b64encode(bytes(buf.data())).decode()


def gallery_result(n=4):
    items = [
        {"index": i, "kind": "image" if i != 2 else "video", "ext": "jpg" if i != 2 else "mp4"}
        for i in range(1, n + 1)
    ]
    items[0]["thumbnail"] = {"data": _png()}
    return {
        "kind": "gallery",
        "title": "Sunset at the beach",
        "uploader": "some.user",
        "extractor": "Instagram",
        "items": items,
        "truncated": False,
    }


def _analyzed(page, runs, qtbot, url=URL):
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    assert run.spec.engine == "gallerydl" and run.spec.options["mode"] == "analyze"
    run.emit("result", **gallery_result())
    qtbot.waitUntil(lambda: not page.gallery_card.isHidden())
    return page


def _stored(page):
    rows = page.store._conn.execute("SELECT options_json, url, engine FROM jobs").fetchall()
    return [(json.loads(r[0]), r[1], r[2]) for r in rows]


def test_a_gallery_shows_a_checkable_grid_with_everything_selected(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    card = page.gallery_card
    assert card.grid.count() == 4 and card.selected_indices() == [1, 2, 3, 4]
    assert card.title_label.text() == "Sunset at the beach"
    assert "some.user" in card.meta_label.text() and "4 items" in card.meta_label.text()
    assert not card.grid.item(0).icon().isNull()  # the preview became an icon
    assert card.grid.item(1).icon().isNull() and "🎞" in card.grid.item(1).text()
    assert page.preview.isHidden() and card.selection_label.text() == "4 selected"


def test_download_sends_exactly_the_ticked_positions(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    card = page.gallery_card
    card.grid.item(1).setCheckState(Qt.CheckState.Unchecked)
    card.grid.item(3).setCheckState(Qt.CheckState.Unchecked)
    assert card.selection_label.text() == "2 selected"
    job = page.start_gallery_download()
    assert job.spec.engine == "gallerydl"
    assert job.spec.options == {
        "mode": "download",
        "preset": "gallery_original",
        "items": [1, 3],
        "archive": True,
    }
    assert job.title == "Sunset at the beach (2)"
    assert runs[-1].spec.job_id == job.spec.job_id  # the scheduler started it
    ((options, url, engine),) = _stored(page)
    assert engine == "gallerydl" and url == "https://www.instagram.com/p/C0ffee123/"  # no query
    assert "site_login" not in options


def test_select_none_disables_download_and_select_all_restores(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.gallery_card.select_none_button.click()
    assert not page.gallery_card.download_button.isEnabled()
    assert page.start_gallery_download() is None
    page.gallery_card.select_all_button.click()
    assert page.gallery_card.download_button.isEnabled()


def test_a_new_analyze_hides_the_gallery(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    page.url_edit.setText("https://youtu.be/dQw4w9WgXcQ")
    page.analyze()
    assert page.gallery_card.isHidden() and page._gallery is None


def test_two_galleries_from_one_site_run_one_at_a_time(page, runs, qtbot):
    _analyzed(page, runs, qtbot)
    first = page.start_gallery_download()
    count = len(runs)
    _analyzed(page, runs, qtbot, url="https://www.instagram.com/p/Other456/")
    second = page.start_gallery_download()
    assert len(runs) == count + 1  # only the analyze started; the download waits
    assert page.scheduler.queued_ids() == [second.spec.job_id]
    runs[count - 1].emit("result", files=[], total_bytes=0, skipped=True)
    assert runs[-1].spec.job_id == second.spec.job_id and first.state == "skipped"


def test_a_site_login_rides_along_to_gallery_dl_only_at_launch(page, runs, qtbot):
    firefox = cookies.SiteLogin("browser", browser="firefox")
    page.set_site_login("instagram.com", firefox)
    _analyzed(page, runs, qtbot)
    assert runs[-1].spec.options == {"mode": "analyze", "site_login": firefox.to_dict()}
    job = page.start_gallery_download()
    assert runs[-1].spec.options["site_login"] == firefox.to_dict()
    assert "site_login" not in job.spec.options
    assert all("site_login" not in options for options, _, _ in _stored(page))


def test_a_private_gallery_offers_the_login(page, runs, qtbot):
    page.url_edit.setText(URL)
    page.analyze()
    runs[-1].emit("error", code="download_error", message="login required: AuthRequired")
    assert not page.login_button.isHidden() and page._login_site == "instagram.com"


def test_a_photo_only_tweet_falls_back_from_ytdlp_to_gallery_dl(page, runs):
    page.url_edit.setText("https://x.com/someone/status/1700000000000000000")
    page.analyze()
    assert runs[-1].spec.engine == "ytdlp"
    runs[-1].emit("error", code="unsupported", message="No video could be found in this tweet")
    assert runs[-1].spec.engine == "gallerydl"
    assert page._route.is_gallery


def test_restored_gallery_jobs_come_back_paused(qtbot, monkeypatch, runs, tmp_path):
    from stuff_downloader.core import history

    store = history.Store(tmp_path / "db.sqlite3")
    store.add_job(
        "g1",
        "https://www.instagram.com/p/x/",
        "gallerydl",
        {"mode": "download", "preset": "gallery_original", "items": [1, 2]},
        str(tmp_path),
        title="Gallery",
        state="active",
    )
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    downloads = pages.DownloadsPage(settings.Settings(), store)
    qtbot.addWidget(downloads)
    job = downloads.jobs["g1"]
    assert job.state == "paused" and job.spec.engine == "gallerydl"
    assert isinstance(job.spec, JobSpec) and runs == []
