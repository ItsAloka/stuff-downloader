from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from stuff_downloader.core import router, settings, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages, theme
from stuff_downloader.gui.main_window import MainWindow, tool_health_text
from stuff_downloader.gui.widgets import format_bytes, format_eta

MISSING_FFMPEG = tools.ToolStatus("ffmpeg", None, None, "missing", "not found")
FOUND_DENO = tools.ToolStatus("deno", "C:/deno.exe", "deno 2.0", "path")


@pytest.fixture
def statuses():
    return [MISSING_FFMPEG]


@pytest.fixture
def window(qtbot, monkeypatch, statuses):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: list(statuses))
    w = MainWindow(settings.Settings())
    qtbot.addWidget(w)
    return w


def test_shell_has_four_pages_and_navigation(window):
    assert [window.sidebar.item(i).text().split()[-1] for i in range(4)] == [
        "Downloads",
        "History",
        "Tools",
        "Settings",
    ]
    assert window.stack.currentWidget() is window.downloads_page
    window.sidebar.setCurrentRow(1)
    assert window.stack.currentWidget() is window.history_page
    window.sidebar.setCurrentRow(2)
    assert window.stack.currentWidget() is window.tools_page
    window.sidebar.setCurrentRow(3)
    assert window.stack.currentWidget() is window.settings_page


def test_theme_is_applied(window):
    assert theme.ACCENT in window.styleSheet()
    assert window.downloads_page.analyze_button.objectName() == "primary"


def test_tools_page_shows_health_without_claiming_missing_tools(window):
    table = window.tools_page.table
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "FFmpeg"
    assert "not found" in table.item(0, 1).text().lower()
    assert window.tools_page.summary_chip.text() == "0 of 1 tools found"
    assert window.tools_page.summary_chip.property("state") == "missing"
    assert window.footer_label.text() == "FFmpeg ✖"


@pytest.mark.parametrize("statuses", [[MISSING_FFMPEG, FOUND_DENO]])
def test_footer_and_summary_mixed_health(window):
    assert window.footer_label.text() == "FFmpeg ✖  ·  Deno ✔"
    assert window.tools_page.summary_chip.text() == "1 of 2 tools found"
    assert "Found" in window.tools_page.table.item(1, 1).text()


def test_footer_updates_on_recheck(window, monkeypatch):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [FOUND_DENO])
    window.tools_page.refresh()
    assert window.footer_label.text() == "Deno ✔"
    assert window.tools_page.summary_chip.property("state") == "ok"


def test_tool_health_text_empty():
    assert tool_health_text([]) == "Tools not checked"


def test_settings_folder_selection_persists_and_updates_hint(window, tmp_path):
    assert window.settings_page.set_folder(str(tmp_path))
    assert window.settings_page.folder_edit.text() == str(tmp_path)
    assert settings.load().download_dir == str(tmp_path)
    assert str(tmp_path) in window.downloads_page.folder_hint.text()


def test_settings_rejects_unwritable_folder(window, tmp_path, monkeypatch):
    from stuff_downloader.gui import pages

    warned = []
    monkeypatch.setattr(pages.QMessageBox, "warning", lambda *a: warned.append(a))
    assert not window.settings_page.set_folder(str(tmp_path / "missing"))
    assert warned and window.app_settings.download_dir == ""


VID_URL = "https://youtu.be/dQw4w9WgXcQ?si=track"
FIXTURE = Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "youtube_video.json"


class FakeRun:
    """Stands in for core.runner.JobRun: records the spec and lets tests push events."""

    instances: list[FakeRun] = []

    def __init__(self, spec, on_event):
        self.spec = spec
        self.on_event = on_event
        self.started = False
        self.cancelled = False
        FakeRun.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True
        self.emit("error", code="cancelled", message="Cancelled")

    def emit(self, kind, **data):
        self.on_event(Event(kind, self.spec.job_id, data))


@pytest.fixture
def runs(monkeypatch):
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    return FakeRun.instances


def _png_b64(width, height):
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return base64.b64encode(bytes(buffer.data())).decode("ascii")


def _analyzed(window, runs, qtbot, url=VID_URL, **extra):
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    info = json.loads(FIXTURE.read_text(encoding="utf-8"))
    info.update(extra)
    run.emit("stage", stage="analyzing")
    run.emit("result", **info)
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    return page


def test_downloads_initial_empty_state(window):
    page = window.downloads_page
    assert not page.empty_state.isHidden()
    assert page.preview.isHidden()
    assert page.queue_summary.text() == "Nothing running"
    assert page.jobs == {}


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("", "paste a link"),
        ("file:///C:/Windows/notepad.exe", "http"),
        ("https://vimeo.com/1", "youtube"),
    ],
)
def test_analyze_rejects_bad_links_without_starting_a_worker(window, runs, url, needle):
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    assert runs == []
    assert needle in page.message_label.text().lower()
    assert not page.message_label.isHidden() and page.preview.isHidden()


def test_analyze_sends_normalized_url_and_no_engine_options(window, runs, qtbot):
    page = window.downloads_page
    page.url_edit.setText(VID_URL)
    page.analyze()
    (run,) = runs
    assert run.started
    assert run.spec.engine == "ytdlp"
    assert run.spec.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert run.spec.options == {"mode": "analyze"}
    assert not page.analyze_button.isEnabled() and not page.analyze_cancel_button.isHidden()
    page.analyze()  # a second click while analyzing is ignored
    assert len(runs) == 1


def test_preview_lists_only_existing_heights_and_caps_by_preset(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, thumbnail={"data": _png_b64(160, 90)})
    card = page.preview
    assert (
        card.title_label.text()
        == "Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film"
    )
    assert page.analyze_button.isEnabled() and page.analyze_cancel_button.isHidden()
    assert card.playlist_label.isHidden()

    def heights():
        combo = card.resolution_combo
        return [combo.itemData(i) for i in range(combo.count())]

    card.preset_combo.setCurrentIndex(card.preset_combo.findData("video_best"))
    assert heights() == [None, 2160, 1440, 1080, 720, 480, 360, 240, 144]
    card.preset_combo.setCurrentIndex(card.preset_combo.findData("video_720"))
    assert heights() == [None, 720, 480, 360, 240, 144]
    card.preset_combo.setCurrentIndex(card.preset_combo.findData("mp3_music"))
    assert not card.resolution_combo.isEnabled()
    assert card.compatible_check.isHidden() and not card.crop_check.isHidden()
    assert card.cover.pixmap().width() == card.cover.pixmap().height()  # square preview
    card.crop_check.setChecked(False)
    assert card.cover.pixmap().width() > card.cover.pixmap().height()


def test_playlist_and_music_links(window, runs, qtbot):
    page = _analyzed(
        window, runs, qtbot, url="https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM1"
    )
    assert not page.preview.playlist_label.isHidden()
    assert page.preview.preset_combo.currentData() == "mp3_music"
    assert runs[0].spec.url == "https://music.youtube.com/watch?v=dQw4w9WgXcQ"


def test_analyze_error_timeout_and_cancel(window, runs, qtbot):
    page = window.downloads_page
    page.url_edit.setText(VID_URL)
    page.analyze()
    runs[-1].emit("error", code="download_error", message="ERROR: Private video")
    assert page.message_label.text() == "This video is private on the site."
    assert page.preview.isHidden() and page.analyze_button.isEnabled()

    page.analyze()
    page._analyze_timeout()
    assert runs[-1].cancelled and "too long" in page.message_label.text()

    page.analyze()
    page.cancel_analyze()
    assert page.message_label.isHidden() and page.analyze_button.isEnabled()


def test_download_job_progress_completion_and_file_actions(
    window, runs, qtbot, tmp_path, monkeypatch
):
    from stuff_downloader.gui import pages

    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    card = page.preview
    card.preset_combo.setCurrentIndex(card.preset_combo.findData("video_best"))
    card.resolution_combo.setCurrentIndex(card.resolution_combo.findData(1440))
    card.compatible_check.setChecked(False)
    job = page.start_download()
    run = runs[-1]
    assert run.spec.options == {
        "mode": "download",
        "preset": "video_best",
        "height": 1440,
        "compatible": False,
        "crop_cover": True,
    }
    assert run.spec.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert run.spec.output_dir == str(tmp_path)
    assert page.empty_state.isHidden() and page.queue_summary.text() == "1 active"

    run.emit("stage", stage="downloading video")
    run.emit("progress", downloaded_bytes=1024, total_bytes=2048, percent=50.0, speed=512, eta=3)
    assert job.card.chip.text() == "Downloading video"
    assert job.card.progress.value() == 50
    assert job.card.details_label.text() == "1.0 KB / 2.0 KB  ·  512 B/s  ·  3s left"

    out = tmp_path / "Big Buck Bunny.mp4"
    out.write_bytes(b"x" * 10)
    run.emit("result", files=[str(out)], total_bytes=10)
    assert job.card.chip.text() == "Completed" and job.card.chip.property("state") == "completed"
    assert job.card.progress.value() == 100 and page.queue_summary.text() == "1 done"
    assert job.card.cancel_button.isHidden() and job.card.retry_button.isHidden()
    assert not job.card.open_button.isHidden() and not job.card.folder_button.isHidden()

    opened = []
    monkeypatch.setattr(pages.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    monkeypatch.setattr(pages.subprocess, "Popen", lambda args: opened.append(args))
    assert page.open_file(run.spec.job_id)
    assert page.show_in_folder(run.spec.job_id)
    assert opened[0].toLocalFile().lower() == str(out).replace("\\", "/").lower()
    assert opened[1] == ["explorer.exe", f"/select,{out.resolve()}"]


def test_file_actions_refuse_paths_outside_output_folder(window, runs, qtbot, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window.settings_page.set_folder(str(out_dir))
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    outside = tmp_path / "elsewhere.exe"
    outside.write_bytes(b"MZ")
    runs[-1].emit("result", files=[str(outside), 5, None], total_bytes=2)
    assert job.card.open_button.isHidden()
    assert not page.open_file(job.spec.job_id) and not page.show_in_folder(job.spec.job_id)


def test_download_failure_retry_and_cancel(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    first = runs[-1]
    first.emit("error", code="download_error", message="HTTP Error 403: Forbidden")
    assert job.card.chip.text() == "Failed" and "403" in job.card.details_label.text()
    assert not job.card.retry_button.isHidden() and page.queue_summary.text() == "1 failed"

    page.retry_job(first.spec.job_id)
    second = runs[-1]
    assert second is not first and second.spec.job_id != first.spec.job_id
    assert second.spec.options == first.spec.options and second.spec.url == first.spec.url
    assert job.card.chip.text() == "Starting" and job.card.retry_button.isHidden()
    first.emit("result", files=[], total_bytes=1)  # stale events from the old run are ignored
    assert job.card.chip.text() == "Starting"

    page.cancel_job(second.spec.job_id)
    assert second.cancelled
    assert job.card.chip.text() == "Cancelled" and page.queue_summary.text() == "1 cancelled"
    assert not job.card.retry_button.isHidden()
    page.shutdown()


def test_worker_crash_message_is_friendly(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    runs[-1].emit("error", code="worker_exited", message="Worker exited with code 1")
    assert job.card.details_label.text() == "The downloader stopped unexpectedly."


def test_missing_engine_runtime_fails_cleanly(window, monkeypatch, tmp_path):
    import sys

    from stuff_downloader.core import runner

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(tmp_path / "missing"))
    page = window.downloads_page
    page.url_edit.setText(VID_URL)
    page.analyze()
    assert page._analyze_run is None and page.analyze_button.isEnabled()
    assert "engine runtime not found" in page.message_label.text()

    page._route = router.route(VID_URL)
    page._info = {"title": "t"}
    job = page.start_download()
    assert job.state == "failed" and job.card.chip.text() == "Failed"
    assert "engine runtime not found" in job.card.details_label.text()
    assert page.queue_summary.text() == "1 failed"


def test_formatters():
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert format_bytes(None) == "—" and format_bytes(-1) == "—" and format_bytes(True) == "—"
    assert format_eta(5.4) == "5s left"
    assert format_eta(125) == "2m 05s left"
    assert format_eta(None) == ""


# ── retry with backoff ───────────────────────────────────────────────────────────────────
TRANSIENT = {"code": "download_error", "message": "HTTP Error 429: Too Many Requests"}


def test_a_transient_failure_is_retried_after_a_backoff(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    job_id = job.spec.job_id
    runs[-1].emit("error", **TRANSIENT)

    assert job.state == "retrying" and job.card.chip.text() == "Retrying"
    assert "retrying in 2s" in job.card.details_label.text()
    assert job.card.retry_button.isHidden()  # the app is already doing it
    assert "retrying" in page.queue_summary.text()
    # It is persisted as unfinished work, not as a finished failure.
    assert page.store.get(job_id).state == "queued"
    # The wait is a real armed timer, not just a label.
    timer = page._retry_timers[job_id]
    assert timer.isActive() and timer.interval() == 2000

    before = len(runs)
    page._release_retry(job_id)
    assert not timer.isActive() and job_id not in page._retry_timers
    assert len(runs) == before + 1 and job.state == "active"
    assert runs[-1].spec.job_id == job_id  # same job, not a new one
    page.shutdown()


def test_backoff_gives_up_after_three_attempts(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    job_id = job.spec.job_id
    delays = []
    for _ in range(3):
        runs[-1].emit("error", **TRANSIENT)
        delays.append(job.card.details_label.text())
        page._release_retry(job_id)
    runs[-1].emit("error", **TRANSIENT)

    assert [d.split("retrying in ")[1].split("s")[0] for d in delays] == ["2", "4", "8"]
    assert job.state == "failed" and job.card.chip.text() == "Failed"
    assert not job.card.retry_button.isHidden()
    assert page.store.get(job_id).state == "failed"
    page.shutdown()


def test_a_permanent_failure_is_not_retried(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    runs[-1].emit("error", code="download_error", message="Private video")
    assert job.state == "failed" and page.scheduler.retrying_count == 0
    assert job.card.details_label.text() == "This video is private on the site."
    page.shutdown()


def test_a_manual_retry_resets_the_backoff(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    first = job.spec.job_id
    runs[-1].emit("error", **TRANSIENT)
    page._release_retry(first)
    runs[-1].emit("error", **TRANSIENT)
    assert page.scheduler.attempts(first) == 2

    page.retry_job(first)
    assert page.scheduler.attempts(first) == 0 and page.scheduler.retrying_count == 0
    new_id = runs[-1].spec.job_id
    runs[-1].emit("error", **TRANSIENT)
    assert "retrying in 2s" in page.jobs[new_id].card.details_label.text()
    page.shutdown()


def test_cancelling_during_a_backoff_stops_the_retry(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    job_id = job.spec.job_id
    runs[-1].emit("error", **TRANSIENT)
    before = len(runs)

    page.cancel_job(job_id)
    assert job.state == "cancelled" and page.scheduler.retrying_count == 0
    page._release_retry(job_id)  # a timer that fires anyway must start nothing
    assert len(runs) == before and job.state == "cancelled"
    page.shutdown()


def test_pausing_during_a_backoff_keeps_the_job(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    job_id = job.spec.job_id
    runs[-1].emit("error", **TRANSIENT)
    page.pause_job(job_id)
    assert job.state == "paused" and page.scheduler.retrying_count == 0

    before = len(runs)
    page.resume_job(job_id)
    assert len(runs) == before + 1 and job.state == "active"
    page.shutdown()


def test_shutdown_stops_pending_backoff_timers(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    page.start_download()
    runs[-1].emit("error", **TRANSIENT)
    (timer,) = page._retry_timers.values()
    assert timer.isActive()
    page.shutdown()
    assert not page._retry_timers and not timer.isActive()


# ── download again ───────────────────────────────────────────────────────────────────────
def test_download_again_queues_a_new_job_and_keeps_the_history_row(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    runs[-1].emit("result", files=[], total_bytes=7)
    window.history_page.refresh()
    record = window.history_page._records[0]

    assert window.history_page.table.rowCount() == 1
    window.history_page.table.selectRow(0)
    assert window.history_page.download_again_selected() is True

    new_id = runs[-1].spec.job_id
    assert new_id != job.spec.job_id
    assert runs[-1].spec.url == record.url and runs[-1].spec.options == record.options
    # The original row is still there, untouched.
    window.history_page.refresh()
    assert [r.job_id for r in window.history_page._records if r.job_id == record.job_id]
    assert window.stack.currentWidget() is window.downloads_page
    page.shutdown()


def test_download_again_refuses_a_record_it_cannot_rebuild(window, runs, qtbot):
    from stuff_downloader.core import history

    page = window.downloads_page
    assert page.download_again(history.JobRecord("j", "", "ytdlp", "completed")) is None
    assert page.download_again(
        history.JobRecord("j", "https://x/y", "ytdlp", "completed", options={"preset": "nope"})
    ) is None
    assert page.jobs == {}


@pytest.fixture
def tray_window(qtbot, monkeypatch, statuses):
    """A window with a tray. The test platform is offscreen, which reports no system tray,
    so the tray code would never run otherwise — and three skipped tests prove nothing."""
    from PyQt6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(tools, "check_all", lambda configured=None: list(statuses))
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    w = MainWindow(settings.Settings())
    qtbot.addWidget(w)
    assert w.tray is not None
    return w


# ── tray and notifications ───────────────────────────────────────────────────────────────
def _notices(page):
    """Every notification the page asks for, in order."""
    seen = []
    page.notification_requested.connect(lambda *args: seen.append(args))
    return seen


def test_a_finished_download_asks_for_one_notification(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    job = page.start_download()
    runs[-1].emit("result", files=[], total_bytes=7)

    (title, message, state) = seen[0]
    assert len(seen) == 1
    assert title == "Download finished" and state == "completed"
    assert job.title in message
    page.shutdown()


def test_a_failed_download_is_notified_as_a_failure(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    page.start_download()
    runs[-1].emit("error", code="download_error", message="Private video")
    assert [n[0] for n in seen] == ["Download failed"]
    assert seen[0][2] == "failed"
    page.shutdown()


def test_a_backoff_does_not_notify_until_the_job_really_finishes(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    job = page.start_download()
    runs[-1].emit("error", **TRANSIENT)
    assert seen == []  # it is still going to retry; saying "failed" would be a lie

    page._release_retry(job.spec.job_id)
    runs[-1].emit("result", files=[], total_bytes=1)
    assert [n[0] for n in seen] == ["Download finished"]
    page.shutdown()


def test_a_playlist_notifies_once_at_the_end_not_once_per_track(window, runs, qtbot):
    from stuff_downloader.gui import pages

    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    page.scheduler.set_max_concurrent(3)
    group_id = "g1"
    page._groups[group_id] = pages.GroupState(
        pages.GroupCard("Chill Mix", 3), 3, "Chill Mix"
    )
    jobs = []
    for _ in range(3):
        spec = page.start_download().spec
        job = page.jobs[spec.job_id]
        job.group_id = group_id
        jobs.append(job)

    for job in jobs[:2]:
        page._finish_job(job, "completed", "Done")
        assert seen == []  # two of three: nothing to announce yet
    page._finish_job(jobs[2], "failed", "Nope")

    assert len(seen) == 1
    title, message, state = seen[0]
    assert title == "Playlist finished" and state == "failed"
    assert "Chill Mix" in message and "2 of 3 downloaded" in message and "1 failed" in message
    page.shutdown()


def test_the_window_shows_a_toast_only_when_the_owner_wants_one(tray_window):
    window = tray_window
    shown = []
    window.tray.showMessage = lambda *args: shown.append(args)
    window.tray.supportsMessages = lambda: True

    assert window._notify("Done", "A song", "completed") is True
    assert shown and shown[0][0] == "Done"

    window.settings_page.notifications_check.setChecked(False)
    assert window.app_settings.notifications is False
    assert window._notify("Done", "A song", "completed") is False
    assert len(shown) == 1


def test_a_desktop_without_a_tray_still_works(qtbot, monkeypatch, statuses):
    from PyQt6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(tools, "check_all", lambda configured=None: list(statuses))
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    w = MainWindow(settings.Settings())
    qtbot.addWidget(w)

    assert w.tray is None
    # Nothing may raise, and nothing is shown.
    assert w._notify("Done", "A song", "completed") is False
    w.downloads_page.notification_requested.emit("Done", "A song", "completed")
    w.close()


def test_a_tray_that_cannot_show_messages_is_not_asked_to(tray_window):
    window = tray_window
    window.tray.supportsMessages = lambda: False
    window.tray.showMessage = lambda *args: pytest.fail("showMessage on an unsupported tray")
    assert window._notify("Done", "A song", "completed") is False


def test_the_tray_menu_offers_show_hide_and_quit(tray_window):
    menu = tray_window.tray.contextMenu()
    assert [a.text() for a in menu.actions()] == ["Show window", "Hide window", "Quit"]


# ── nothing private reaches the notification centre ───────────────────────
def _toasts(window):
    """Every (title, message) the tray is actually asked to show."""
    shown = []
    window.tray.showMessage = lambda *args: shown.append(args)
    window.tray.supportsMessages = lambda: True
    return shown


def test_a_local_path_cannot_reach_a_toast(tray_window):
    shown = _toasts(tray_window)
    assert tray_window._notify(
        "Download failed",
        "Song C:\\Users\\testuser\\Music\\song.mp3"
        + "\n"
        + "ffmpeg could not write /home/testuser/Music/song.mp3",
        "failed",
    ) is True

    title, message = shown[0][0], shown[0][1]
    assert "C:" not in message and "Users" not in message and "testuser" not in message
    assert "\\" not in message and "/home/" not in message
    assert pages.REDACTED in message
    assert "Song" in message and title == "Download failed"


def test_a_url_query_string_cannot_reach_a_toast(tray_window):
    shown = _toasts(tray_window)
    assert tray_window._notify(
        "Download failed",
        "Mix" + "\n" + "https://example.com/watch?v=abc123&token=secretvalue failed",
        "failed",
    ) is True

    message = shown[0][1]
    assert "https://" not in message
    assert "token=secretvalue" not in message and "v=abc123" not in message
    assert pages.REDACTED in message


def test_a_schemeless_url_cannot_reach_a_toast(tray_window):
    shown = _toasts(tray_window)
    assert tray_window._notify(
        "Download failed",
        "Watch cdn.example.com/private-video now",
        "failed",
    ) is True

    message = shown[0][1]
    assert "cdn.example.com" not in message and "private-video" not in message
    assert "example" not in message and pages.REDACTED in message
    assert "Watch" in message and "now" in message  # only the host/path is taken out


def test_punctuation_around_a_host_does_not_smuggle_it_through(tray_window):
    """A location does not stop being one because a sentence wrapped it."""
    for text in (
        "example.com.",
        "(example.com)",
        "Visit example.com, now",
        "<cdn.example.com/private-video>",
    ):
        shown = _toasts(tray_window)
        assert tray_window._notify("Download failed", text, "failed") is True
        message = shown[-1][1]
        assert "example.com" not in message and "example" not in message, text
        assert "private-video" not in message, text
        assert pages.REDACTED in message, text


def test_credentials_and_hosts_hidden_behind_an_at_sign_cannot_reach_a_toast(tray_window):
    """userinfo@host hides the host from a start-anchored match, and leaks the password too."""
    shown = _toasts(tray_window)
    assert tray_window._notify(
        "Download failed", "Fetching user:pass@example.com/a failed", "failed"
    ) is True

    message = shown[0][1]
    assert "user" not in message and "pass" not in message
    assert "example.com" not in message and "/a" not in message
    assert pages.REDACTED in message


def test_other_host_shapes_cannot_reach_a_toast(tray_window):
    for text in ("admin@10.0.0.5/x", "host:8080/path", "[2001:db8::1]/x"):
        shown = _toasts(tray_window)
        assert tray_window._notify("Download failed", text, "failed") is True
        assert shown[-1][1] == pages.REDACTED, text


def test_leading_punctuation_does_not_smuggle_a_host_through(tray_window):
    """Any non-word edge is trimmed, not a fixed list of brackets and quotes."""
    for text in ("*example.com", "#example.com/x", "~example.com/x", "「example.com」"):
        shown = _toasts(tray_window)
        assert tray_window._notify("Download failed", text, "failed") is True
        assert "example" not in shown[-1][1], text
        assert pages.REDACTED in shown[-1][1], text


KNOWN_BYPASSES = [
    # Found by author self-testing at version 8, after four rounds of patching the old
    # denylist. Each one used to be emitted verbatim. The needle is the part that leaked.
    ("IDN unicode host", "Saved from 例え.テスト/video", "例"),
    ("IDN punycode", "Saved from xn--r8jz45g.xn--zckzah/video", "xn--"),
    ("Cyrillic homoglyph", "Saved from examplе.com/secret", "е.com"),
    ("fullwidth dot", "Saved from example．com/x", "example"),
    ("trailing-dot FQDN", "Saved from example.com./secret", "example"),
    ("percent-encoded dot", "Saved from example%2ecom/secret", "example"),
    ("octal IPv4", "Saved from 0300.0250.0.1/x", "0300"),
    ("decimal IPv4", "Saved from 3232235777/x", "3232235777"),
    ("uncommon TLD", "Saved from example.museum", "example"),
    ("internal TLD", "Saved from example.internal", "example"),
    ("bare host and one segment", "Saved from myserver/videos", "myserver"),
    # The five closed before version 8 -- kept so a rewrite cannot quietly reopen them.
    ("uppercase host", "Saved from EXAMPLE.COM/secret", "EXAMPLE"),
    ("scheme-relative", "Saved from //cdn.example.com/private", "cdn"),
    ("IPv6 with zone id", "Saved from [fe80::1%eth0]:8080/x", "fe80"),
    ("userinfo", "Saved from user:pass@example.com/a", "pass"),
    ("query string", "https://example.com/watch?v=a&token=s", "token"),
    # Raised by the security review against the allowlist's one slash exemption, which is why
    # there is no longer a slash exemption at all: a compact internal host and path satisfied
    # every gate the exemption applied.
    ("short host and path", "Saved from SRV/x", "SRV"),
    ("mixed-case host and path", "Saved from Host/Path", "Host"),
]


@pytest.mark.parametrize(("name", "text", "needle"), KNOWN_BYPASSES)
def test_no_known_bypass_reaches_a_toast(tray_window, name, text, needle):
    """Every location shape that has ever got through, in one place.

    This list only grows. A sanitizer that fails open loses the race against "every way to
    write a host", which is why the implementation is an allowlist -- these are the cases that
    proved it, not the definition of done.
    """
    shown = _toasts(tray_window)
    assert tray_window._notify("Download failed", text, "failed") is True
    message = shown[-1][1]
    assert needle not in message, f"{name}: {message!r}"
    assert pages.REDACTED in message, f"{name}: {message!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Kevin MacLeod - 25 Years, Vol. 1",
        "Track 3 of 12 done",
        "1:23 remaining",
        "3.5 MB downloaded",
        "Don't Stop Me Now",
        "Mr. Smith goes to town",
    ],
)
def test_ordinary_toast_text_survives_the_allowlist(tray_window, text):
    """The other half of the bargain: failing closed must not redact normal text.

    A sanitizer that redacts everything is trivially safe and useless. A running time, a
    decimal size, a track count, an abbreviation's full stop and an apostrophe all have to
    come through intact, or the toast stops being worth showing.
    """
    shown = _toasts(tray_window)
    tray_window._notify("Download finished", text, "completed")
    assert shown[-1][1] == text
    assert pages.REDACTED not in shown[-1][1]


def test_a_non_latin_title_is_left_alone(tray_window):
    """The edge trim is unicode-aware, so a title in another script is not eaten."""
    shown = _toasts(tray_window)
    tray_window._notify("Download finished", "日本語のタイトル", "completed")
    assert shown[0][1] == "日本語のタイトル"


def test_an_at_sign_in_an_ordinary_title_is_not_a_host(tray_window):
    """Redaction must not widen until real titles break."""
    shown = _toasts(tray_window)
    tray_window._notify("Download finished", "Live @ Wembley (Set 1:23:45)", "completed")
    assert shown[0][1] == "Live @ Wembley (Set 1:23:45)"


def test_redaction_stays_token_local_and_does_not_eat_the_line(tray_window):
    """Redaction must not be so greedy that real titles become unreadable.

    This test used to require "AC/DC - Thunderstruck (Ep. 12)" to survive whole, via a slash
    exemption in the sanitizer. The security review rejected that exemption, because nothing
    operating on a single token can tell the band name from "SRV/x" -- a compact internal host
    and path, which is precisely what must not reach Windows notification history. So the
    slash-bearing token now redacts, and what this guards instead is that the redaction is
    confined to that token: the rest of the title, including "Ep. 12" with its full stop, still
    reads. Losing a band name from a toast is the accepted cost; see pages._is_plain_text.
    """
    shown = _toasts(tray_window)
    tray_window._notify("Download finished", "AC/DC - Thunderstruck (Ep. 12)", "completed")
    assert shown[0][1] == f"{pages.REDACTED} - Thunderstruck (Ep. 12)"


def test_toast_text_is_bounded_and_free_of_control_characters(tray_window):
    shown = _toasts(tray_window)
    tray_window._notify("A" * 300, ("B" * 400) + "\n" + "tail" + "\x07\t" + "end", "completed")

    title, message = shown[0][0], shown[0][1]
    assert len(title) <= pages.NOTIFICATION_TITLE_LIMIT
    assert all(len(line) <= pages.NOTIFICATION_LINE_LIMIT for line in message.split("\n"))
    assert message.count("\n") <= pages.NOTIFICATION_LINES - 1
    assert not any(ord(ch) < 32 for ch in message.replace("\n", ""))


def test_a_failed_job_notifies_a_fixed_summary_not_engine_text(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    page.start_download()
    runs[-1].emit(
        "error",
        code="download_error",
        message="ERROR: unable to write C:\\Users\\testuser\\Music\\song.mp3 from https://x.test/v?id=9",
    )

    (title, message, state) = seen[0]
    assert (title, state) == ("Download failed", "failed")
    assert message.endswith(pages.FAILURE_SUMMARY)
    assert "https://" not in message and "C:" not in message and "id=9" not in message
    page.shutdown()


def test_an_untrusted_site_title_is_sanitized_before_it_is_emitted(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    seen = _notices(page)
    job = page.start_download()
    job.title = "Mix C:\\Users\\testuser\\Music\\song.mp3 https://x.test/a?k=v"
    page._finish_job(job, "completed", "Done")

    message = seen[0][1]
    assert "Mix" in message
    assert "C:" not in message and "https://" not in message and "k=v" not in message
    page.shutdown()
