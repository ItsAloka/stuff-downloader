from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt

from stuff_downloader.core import protocol, router, settings, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui import pages, theme
from stuff_downloader.gui.main_window import MainWindow, tool_health_text
from stuff_downloader.gui.widgets import format_bytes, format_eta
from stuff_downloader_worker.engines import ytdlp as worker_ytdlp

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

    def emit_result(self, data):
        """A result whose own fields include "kind" (a MediaResult), which emit() cannot take."""
        self.on_event(Event("result", self.spec.job_id, data))


def media(url=None, preview=None, **fields):
    """The recorded yt-dlp page as the worker's MediaResult, with ``fields`` changed first."""
    info = json.loads(FIXTURE.read_text(encoding="utf-8"))
    info.update(fields)
    return worker_ytdlp.analyze_result(info, url or VID_URL, preview)


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


def _analyzed(window, runs, qtbot, url=VID_URL, preview=None, **extra):
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    run.emit("stage", stage="analyzing")
    run.emit_result(media(runs[-1].spec.url, preview, **extra))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    return page


def test_downloads_initial_empty_state(window):
    page = window.downloads_page
    assert not page.empty_state.isHidden()
    assert page.result_card.isHidden()
    assert page.queue_summary.text() == "Nothing running"
    assert page.jobs == {}


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("", "paste a link"),
        ("file:///C:/Windows/notepad.exe", "http"),
        # Since M3 another site is not a reason to refuse. These still are.
        ("http://127.0.0.1:8080/watch/1", "private network"),
        ("https://user:pass@vimeo.com/1", "username or password"),
        ("https://open.spotify.com/track/abc", "spotify"),
    ],
)
def test_analyze_rejects_bad_links_without_starting_a_worker(window, runs, url, needle):
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    assert runs == []
    assert needle in page.message_label.text().lower()
    assert not page.message_label.isHidden() and page.result_card.isHidden()


def test_analyze_accepts_a_public_link_from_another_site(window, runs):
    page = window.downloads_page
    page.url_edit.setText("https://vimeo.com/123456789?quality=1080p#t=30")
    page.analyze()
    (run,) = runs
    assert run.started and run.spec.engine == "ytdlp"
    assert run.spec.url == "https://vimeo.com/123456789?quality=1080p"  # fragment dropped
    assert run.spec.options == {"mode": "analyze"}


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


def test_the_result_card_lists_the_rows_that_exist_in_mediaresult_tab_order(
    window, runs, qtbot
):
    page = _analyzed(window, runs, qtbot, preview={"data": _png_b64(160, 90)})
    card = page.result_card
    assert (
        card.title_editor.title()
        == "Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film"
    )
    assert page.analyze_button.isEnabled() and page.analyze_cancel_button.isHidden()
    assert card.playlist_label.isHidden()
    assert card.tab_names() == ["video", "audio", "image"][: len(card.tab_names())]
    assert card.current_tab() == "video"
    heights = [row["height"] for row in card.rows("video")]
    assert heights == [2160, 1440, 1080, 720, 480, 360, 240, 144]
    # ★ is the highest H.264 height up to 1080p, and every row has its own Download button.
    assert [r["id"] for r in card.rows("video") if r.get("default")] == ["v:1080:mp4"]
    assert "★" in card.cell_text("video", 2, 1) and "H.264" in card.cell_text("video", 2, 1)
    assert all(card.download_button("video", r) for r in range(len(heights)))
    assert card.cover.pixmap() is not None and not card.cover.pixmap().isNull()


def test_audio_rows_offer_every_format_and_say_where_the_cover_is_missing(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    card = page.result_card
    ids = [row["id"] for row in card.rows("audio")]
    assert ids[:5] == ["a:mp3:320", "a:mp3:256", "a:mp3:192", "a:mp3:128", "a:mp3:64"]
    assert {"a:m4a", "a:flac", "a:wav"} <= set(ids)
    wav = ids.index("a:wav")
    assert "no embedded cover" in card.cell_text("audio", wav, 1)
    assert card.cell_text("audio", 0, 2).startswith("~")  # an MP3 size is an estimate
    assert card.source_label.text().startswith("Source audio: ")
    assert "do not add quality" in card.audio_note.text()


def test_a_row_download_sends_only_our_row_id_and_the_chosen_container(
    window, runs, qtbot
):
    page = _analyzed(window, runs, qtbot)
    card = page.result_card
    card.container_combo.setCurrentIndex(card.container_combo.findData("mkv"))
    row = next(i for i, r in enumerate(card.rows("video")) if r["id"] == "v:720:mp4")
    assert "MKV" == card.cell_text("video", row, 0)
    card.download_button("video", row).click()
    assert runs[-1].spec.options == {
        "mode": "download",
        "tab": "video",
        "row_id": "v:720:mp4",
        "container": "mkv",
    }
    assert "format_id" not in runs[-1].spec.options
    # MOV re-encodes, and every row says so before anything is downloaded.
    card.container_combo.setCurrentIndex(card.container_combo.findData("mov"))
    assert "re-encodes" in card.cell_text("video", row, 1)


def test_an_edited_title_becomes_the_file_name_and_restores_with_one_click(
    window, runs, qtbot
):
    page = _analyzed(window, runs, qtbot)
    editor = page.result_card.title_editor
    editor.label.mousePressEvent(None)  # one click turns the title into a text box
    assert editor.is_editing()
    editor.edit.setText("My Clip")
    editor.edit.editingFinished.emit()
    assert not editor.is_editing() and editor.edited_title() == "My Clip"
    assert not editor.reset.isHidden()
    job = page.start_row_download("audio", "a:mp3:320")
    assert runs[-1].spec.options["edited_title"] == "My Clip" and job.title == "My Clip"
    editor.restore()
    assert editor.edited_title() is None and editor.reset.isHidden()
    page.start_row_download("audio", "a:mp3:320")
    assert "edited_title" not in runs[-1].spec.options


def test_escape_cancels_a_title_edit(window, runs, qtbot):
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent

    page = _analyzed(window, runs, qtbot)
    editor = page.result_card.title_editor
    editor.start_editing()
    editor.edit.setText("Nope")
    editor.edit.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert not editor.is_editing() and editor.edited_title() is None


def test_a_row_the_card_does_not_show_is_never_queued(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    before = len(runs)
    assert page.start_row_download("video", "v:4320:mp4") is None
    assert page.start_row_download("audio", "a:mp3:999") is None
    assert len(runs) == before


def test_music_links_open_on_the_audio_tab(window, runs, qtbot):
    page = _analyzed(
        window, runs, qtbot, url="https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVM1"
    )
    assert not page.result_card.playlist_label.isHidden()
    assert page.result_card.current_tab() == "audio"
    assert runs[0].spec.url == "https://music.youtube.com/watch?v=dQw4w9WgXcQ"


def test_each_analyzed_link_starts_on_its_own_tab_and_mp4(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url="https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    card = page.result_card
    card.container_combo.setCurrentIndex(card.container_combo.findData("avi"))
    assert card.current_tab() == "audio"

    # A new ordinary YouTube video must not inherit the previous link's choices.
    page.url_edit.setText(VID_URL)
    page.analyze()
    runs[-1].emit_result(media())
    assert card.current_tab() == "video" and card.container() == "mp4"


def test_a_result_the_core_cannot_validate_is_refused(window, runs, qtbot):
    page = window.downloads_page
    page.url_edit.setText(VID_URL)
    page.analyze()
    runs[-1].emit("result", title="old shape", formats=[])
    assert page.result_card.isHidden()
    assert "cannot read" in page.message_label.text()


def test_the_result_card_has_no_file_name_field_and_no_crop_checkbox(window, runs, qtbot):
    from PyQt6.QtWidgets import QCheckBox, QLabel, QLineEdit

    page = _analyzed(window, runs, qtbot)
    card = page.result_card
    texts = [w.text() for w in card.findChildren(QLabel)]
    texts += [w.text() for w in card.findChildren(QCheckBox)]
    assert not any("file name" == t.strip().lower() for t in texts)
    assert not any("crop" in t.lower() for t in texts)
    assert not card.findChildren(QCheckBox)
    # The only text box is the title editor's own, in the title's place.
    assert card.findChildren(QLineEdit) == [card.title_editor.edit]
    for tab, table in card.tables.items():
        headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
        assert "File name" not in headers, tab


def test_youtube_music_playlist_resets_to_mp3(window):
    page = window.downloads_page
    page.playlist_card.format_combo.setCurrentIndex(
        page.playlist_card.format_combo.findData("a:flac")
    )
    page._route = router.route("https://music.youtube.com/playlist?list=PL1234567890")
    page.show_playlist(
        {
            "kind": "playlist",
            "id": "PL1234567890",
            "title": "Music playlist",
            "entries": [{"id": "dQw4w9WgXcQ", "title": "Song"}],
        }
    )
    assert page.playlist_card.format_combo.currentData() == "a:mp3:320"


def test_analyze_error_timeout_and_cancel(window, runs, qtbot):
    page = window.downloads_page
    page.url_edit.setText(VID_URL)
    page.analyze()
    runs[-1].emit("error", code="download_error", message="ERROR: Private video")
    assert page.message_label.text() == "This video is private on the site."
    assert page.result_card.isHidden() and page.analyze_button.isEnabled()

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
    job = page.start_download()  # the ★ row of the tab on show
    run = runs[-1]
    assert run.spec.options == {
        "mode": "download",
        "tab": "video",
        "row_id": "v:1080:mp4",
        "container": "mp4",
    }
    assert job.card.details_label.text() == "Video · 1080p · MP4"
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
    # Separate arguments: one quoted "/select,<path with spaces>" is mis-parsed by explorer.
    assert opened[1] == ["explorer.exe", "/select,", str(out.resolve())]


def test_file_actions_refuse_paths_outside_output_folder(window, runs, qtbot, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window.settings_page.set_folder(str(out_dir))
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    outside = tmp_path / "elsewhere.exe"
    outside.write_bytes(b"MZ")
    runs[-1].emit("result", files=[str(outside), 5, None], total_bytes=2)
    assert not job.card.open_button.isEnabled() and not job.card.folder_button.isEnabled()
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
    page.show_result(media())
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


STREAM_403 = {
    "code": "download_error",
    "message": "ERROR: unable to download video data: HTTP Error 403: Forbidden",
}


def test_a_stream_403_is_retried_and_the_next_run_completes(window, runs, qtbot):
    """The live failure: the stream URL expired partway, and the job must not just die."""
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    job_id = job.spec.job_id
    runs[-1].emit("stage", stage="downloading")
    runs[-1].emit("error", **STREAM_403)
    assert job.state == "retrying" and page.store.get(job_id).state == "queued"

    before = len(runs)
    page._release_retry(job_id)
    assert len(runs) == before + 1 and job.state == "active"
    assert runs[-1].spec.job_id == job_id and runs[-1].started

    runs[-1].emit("result", files=[], total_bytes=1)
    assert job.state == "completed" and job.card.chip.text() != "Failed"
    assert page.scheduler.retrying_count == 0
    page.shutdown()


def test_a_page_403_fails_without_an_automatic_retry(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    before = len(runs)
    runs[-1].emit(
        "error",
        code="download_error",
        message="ERROR: [youtube] abc: Unable to download webpage: HTTP Error 403: Forbidden",
    )
    assert job.state == "failed" and job.card.chip.text() == "Failed"
    assert job.spec.job_id not in page._retry_timers and len(runs) == before
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


# --- M3: the Advanced all-formats view ------------------------------------------------------


SITE_URL = "https://vimeo.com/123456789"


def test_advanced_view_lists_every_format_and_starts_collapsed(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    table = page.advanced_table
    assert not page.advanced_button.isHidden()
    assert page.advanced_box.isHidden() and not page.advanced_button.isChecked()
    assert table.rowCount() == 49  # every format in the fixture, not only the pickable heights
    assert [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())] == [
        "Kind",
        "Quality",
        "File",
        "Codecs",
        "Size",
    ]
    page.advanced_button.setChecked(True)
    assert not page.advanced_box.isHidden()
    assert page.advanced_button.text().startswith("▾")


def test_advanced_view_groups_by_kind_and_never_shows_a_url(window, runs, qtbot):
    page = _analyzed(
        window,
        runs,
        qtbot,
        formats=[
            {"format_id": "a", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128, "filesize": 1024},
            {"format_id": "v", "ext": "mp4", "vcodec": "avc1.64", "height": 1080, "fps": 60},
            {
                "format_id": "b",
                "ext": "mp4",
                "vcodec": "avc1.64",
                "acodec": "mp4a.40.2",
                "height": 720,
                "url": "https://rr1.googlevideo.com/videoplayback?sig=SECRET",
                "http_headers": {"Cookie": "SID=secret"},
            },
        ],
    )
    table = page.advanced_table
    rows = [
        [table.item(r, c).text() for c in range(table.columnCount())]
        for r in range(table.rowCount())
    ]
    assert [r[0] for r in rows] == ["Video + audio", "Video only", "Audio only"]
    assert rows[0][1:] == ["720p", "MP4", "avc1 / mp4a", "—"]
    assert rows[1][1] == "1080p60"
    assert rows[2][1] == "128 kbps" and rows[2][4] == "1.0 KB"
    flat = " ".join(cell for row in rows for cell in row)
    assert "SECRET" not in flat and "http" not in flat and "Cookie" not in flat


def test_advanced_view_is_read_only_and_says_so(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    table = page.advanced_table
    assert table.editTriggers() == table.EditTrigger.NoEditTriggers
    assert table.selectionMode() == table.SelectionMode.NoSelection
    assert "row above" in page.advanced_note.text()


def test_advanced_view_is_hidden_when_the_site_offers_no_formats(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, formats=[])
    assert page.advanced_button.isHidden() and page.advanced_box.isHidden()
    page.advanced_button.setChecked(True)
    assert page.advanced_box.isHidden()  # nothing to reveal


def test_advanced_view_survives_junk_format_metadata(window, runs, qtbot):
    page = _analyzed(
        window,
        runs,
        qtbot,
        formats=[
            "not a dict",
            {"format_id": "x", "vcodec": "none", "acodec": "none"},
            {"format_id": "y", "ext": None, "height": True, "acodec": "opus", "abr": "loud"},
        ],
    )
    table = page.advanced_table
    assert table.rowCount() == 2
    assert all(table.item(r, c).text() for r in range(2) for c in range(table.columnCount()))


def test_a_site_title_with_markup_is_shown_as_text_not_rendered(window, runs, qtbot):
    page = _analyzed(
        window,
        runs,
        qtbot,
        url=SITE_URL,
        title="<b>bold</b> <img src=x>",
        extractor="Vimeo",
    )
    card = page.result_card
    assert card.title_editor.label.text() == "<b>bold</b> <img src=x>"
    assert card.title_editor.label.textFormat() == Qt.TextFormat.PlainText
    assert card.meta_label.textFormat() == Qt.TextFormat.PlainText
    assert "Vimeo" in card.meta_label.text()


def test_the_site_name_comes_from_the_mediaresult(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, extractor="Youtube")
    assert page.result_card.meta_label.text().endswith("Youtube")


def test_another_sites_video_downloads_through_its_rows(window, runs, qtbot, tmp_path):
    page = _analyzed(window, runs, qtbot, url=SITE_URL, extractor="Vimeo")
    page.start_row_download("video", "v:720:mp4")
    spec = runs[-1].spec
    assert spec.engine == "ytdlp" and spec.url == SITE_URL
    assert spec.options["mode"] == "download" and spec.options["row_id"] == "v:720:mp4"
    assert "format_id" not in spec.options and "url" not in spec.options


def test_a_title_containing_an_em_dash_is_not_mistaken_for_a_missing_title(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, title="Song — Live at the Hall")
    assert page.result_card.title_editor.title() == "Song — Live at the Hall"
    page = _analyzed(window, runs, qtbot, title="   ")
    assert page.result_card.title_editor.title() == "Untitled"


def test_a_site_that_reports_no_name_simply_shows_no_name(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url=SITE_URL, extractor=None, uploader="", duration=None)
    assert page.result_card.meta_label.text() == ""


# --- Security rework: a tokenized generic link must not reach the database -------------------


TOKEN_URL = "https://videos.example.com/v/9?sig=SECRET&exp=1"
TOKEN_SAFE = "https://videos.example.com/v/9"


def _downloads_page(qtbot, tmp_path, store=None):
    from stuff_downloader.core import history
    from stuff_downloader.gui.pages import DownloadsPage

    page = DownloadsPage(
        settings.Settings(), store if store is not None else history.Store(tmp_path / "h.sqlite3")
    )
    qtbot.addWidget(page)
    return page


def test_a_generic_links_token_is_never_written_to_history(window, runs, qtbot, tmp_path):
    page = _analyzed(window, runs, qtbot, url=TOKEN_URL, extractor="Generic")
    job = page.start_download()
    # The live job still has the exact link — that is what the download needs.
    assert job.spec.url == TOKEN_URL
    record = page.store.get(job.spec.job_id)
    # What went to disk does not.
    assert record.url == TOKEN_SAFE and record.url_redacted is True
    assert "SECRET" not in record.url
    page.shutdown()


def test_a_youtube_link_is_stored_whole_and_stays_replayable(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot)
    job = page.start_download()
    record = page.store.get(job.spec.job_id)
    assert record.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert record.url_redacted is False
    page.shutdown()


def test_download_again_asks_for_the_link_instead_of_replaying_a_redacted_one(window, runs):
    from stuff_downloader.core import history

    page = window.downloads_page
    record = history.JobRecord(
        "j",
        TOKEN_SAFE,
        "ytdlp",
        "completed",
        options={"preset": "video_1080"},
        url_redacted=True,
    )
    assert page.download_again(record) is None
    assert runs == []  # nothing was downloaded with the wrong link
    assert page.jobs == {}
    assert TOKEN_SAFE in page.url_edit.text()  # handed back as a starting point
    assert "paste" in page.message_label.text().lower()


def test_a_redacted_unfinished_job_is_closed_rather_than_resumed(qtbot, runs, tmp_path):
    from stuff_downloader.core import history

    store = history.Store(tmp_path / "h.sqlite3")
    store.add_job(
        "stale",
        TOKEN_SAFE,
        "ytdlp",
        {"mode": "download", "preset": "video_1080"},
        str(tmp_path),
        title="Half-done",
        state="active",
        url_redacted=True,
    )
    page = _downloads_page(qtbot, tmp_path, store)
    # No card, so there is no retry button that would replay the broken link.
    assert page.jobs == {}
    assert runs == []
    record = store.get("stale")
    assert record.state == "failed"
    assert "paste the original link" in record.error_message.lower()
    page.shutdown()


def test_an_ordinary_unfinished_job_still_comes_back_paused(qtbot, runs, tmp_path):
    from stuff_downloader.core import history

    store = history.Store(tmp_path / "h.sqlite3")
    store.add_job(
        "fine",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "ytdlp",
        {"mode": "download", "preset": "video_1080"},
        str(tmp_path),
        title="Half-done",
        state="active",
    )
    page = _downloads_page(qtbot, tmp_path, store)
    assert page.jobs["fine"].state == "paused"
    assert runs == []
    page.shutdown()


# --- Security rework round 2: a blank title must not become the tokenized link ---------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (None, TOKEN_SAFE),
        ("", TOKEN_SAFE),
        ("   ", TOKEN_SAFE),
        (TOKEN_URL, TOKEN_SAFE),  # a title that IS the link is not trusted as a title
        (f"Watch at {TOKEN_URL}", TOKEN_SAFE),  # nor one that merely carries it
        ("Real Title", "Real Title"),
    ],
)
def test_safe_job_title_never_returns_a_link_with_a_token(title, expected):
    assert pages.safe_job_title(title, TOKEN_URL) == expected


def test_safe_job_title_falls_back_to_untitled_when_there_is_no_usable_link():
    assert pages.safe_job_title(None, "") == "Untitled"
    assert pages.safe_job_title(None, "https://example.com:99999/x") == "Untitled"


def test_safe_job_title_bounds_and_cleans_a_hostile_title():
    assert pages.safe_job_title("a" * 5000, TOKEN_URL) == "a" * pages.TITLE_LIMIT
    assert pages.safe_job_title("line\u2028one\ttwo", TOKEN_URL) == "line one two"


def test_a_blank_title_generic_download_puts_no_token_in_history(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url=TOKEN_URL, extractor="Generic", title=None)
    job = page.start_download()
    record = page.store.get(job.spec.job_id)
    assert record.title == "Untitled" and "SECRET" not in record.title
    assert record.url == TOKEN_SAFE and "SECRET" not in record.url
    # ...and not in the queue card or the in-memory job either.
    assert "SECRET" not in job.title
    assert "SECRET" not in job.card.title_label.text()
    # The live spec still has the real link, which is what the download needs.
    assert job.spec.url == TOKEN_URL
    page.shutdown()


def test_no_token_survives_a_blank_title_job_anywhere_the_owner_can_read_it(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url=TOKEN_URL, extractor="Generic", title=None)
    job = page.start_download()
    runs[-1].emit("result", files=[], total_bytes=7)

    found = page.store.search(limit=50)
    assert found and all("SECRET" not in (r.title + r.url) for r in found)
    # Searching for the token finds nothing, because nothing stored it.
    assert page.store.search("SECRET") == []
    # The History page renders the same rows.
    window.history_page.refresh()
    table = window.history_page.table
    cells = [
        table.item(row, col).text()
        for row in range(table.rowCount())
        for col in range(table.columnCount())
        if table.item(row, col) is not None
    ]
    assert cells and all("SECRET" not in c for c in cells)
    # And the tray line built from that title carries nothing either.
    assert "SECRET" not in pages.safe_notification_line(job.title)
    page.shutdown()


def test_a_worker_crash_message_does_not_carry_a_token_into_history(window, runs, qtbot):
    """An unexpected worker exception reports str(exc), which can name the URL it was fetching.

    The engine redacts yt-dlp's own DownloadError, but not this path — and the message urllib3
    produces carries the query with no scheme in front of it, so matching "scheme://" misses it.
    """
    page = _analyzed(window, runs, qtbot, url=TOKEN_URL, extractor="Generic", title=None)
    job = page.start_download()
    runs[-1].emit(
        "error",
        code="engine_crashed",
        message=(
            "HTTPSConnectionPool(host='videos.example.com', port=443): "
            "Max retries exceeded with url: /v/9?sig=SECRET&exp=1"
        ),
    )
    record = page.store.get(job.spec.job_id)
    assert "SECRET" not in record.error_message, record.error_message
    assert "videos.example.com" not in record.error_message
    assert record.error_message  # redacted, not emptied
    assert "SECRET" not in job.card.details_label.text()
    page.shutdown()


@pytest.mark.parametrize(
    "message",
    [
        "HTTPSConnectionPool(host='h.example.com', port=443): url: /v/9?sig=SECRET",
        "ERROR: Unsupported URL: https://h.example.com/v/9?token=SECRET",
        "failed fetching h.example.com/v/9?k=SECRET",
        "giving up on user:SECRET@h.example.com/x",
    ],
)
def test_safe_error_message_redacts_anything_that_names_a_location(message):
    cleaned = pages.safe_error_message(message)
    assert "SECRET" not in cleaned and "example.com" not in cleaned


@pytest.mark.parametrize(
    "message",
    [
        "The site refused the download (HTTP 403). Updating engines may help.",
        "The site is rate-limiting requests. Wait a bit and retry.",
        "This video is blocked in your region.",
        "FFmpeg is needed for this download. Check the Tools page.",
        "HTTP Error 429: Too Many Requests",
    ],
)
def test_safe_error_message_leaves_an_ordinary_explanation_readable(message):
    # Redaction that eats the explanation would just move the harm onto the owner.
    assert pages.safe_error_message(message) == message


def test_safe_error_message_is_bounded_and_survives_junk():
    assert pages.safe_error_message(None) == ""
    assert len(pages.safe_error_message("word " * 5000)) <= pages.ERROR_MESSAGE_LIMIT
    assert pages.safe_error_message("line two") == "line two"


def test_a_retried_generic_job_is_recorded_under_the_same_rules(window, runs, qtbot):
    """Retry reuses its queue row, so it cannot go through _add_job — but the row it writes
    must be indistinguishable from one _add_job would have written."""
    page = _analyzed(window, runs, qtbot, url=TOKEN_URL, extractor="Generic", title=None)
    job = page.start_download()
    first_id = job.spec.job_id
    runs[-1].emit("error", code="download_error", message="HTTP Error 403: Forbidden")

    page.retry_job(first_id)
    retried = page.store.get(job.spec.job_id)
    assert job.spec.job_id != first_id
    assert retried.url == TOKEN_SAFE and retried.url_redacted is True
    assert retried.title == "Untitled" and "SECRET" not in retried.title
    # The live spec keeps the real link, exactly as the first attempt did.
    assert job.spec.url == TOKEN_URL
    page.shutdown()


def test_every_job_row_is_written_by_one_function(window, runs, qtbot):
    """The chokepoint claim, enforced rather than asserted in a comment.

    If a new path to store.add_job appears, this fails and whoever added it has to decide
    deliberately whether the redaction rules apply — which is the whole point.
    """
    import inspect

    source = inspect.getsource(pages.DownloadsPage)
    callers = [line.strip() for line in source.splitlines() if "store.add_job(" in line]
    assert callers == ["self.store.add_job("], callers


def test_a_link_pasted_without_https_is_completed_and_walks_the_core_chain(window, runs, qtbot):
    """P14 and plan §5.3: the page engines answer first; the direct engine only last."""
    page = window.downloads_page
    page.url_edit.setText("pbs.twimg.com/media/Gx1AbC?format=jpg&name=large")
    page.analyze()
    full = "https://pbs.twimg.com/media/Gx1AbC?format=jpg&name=large"
    assert page.url_edit.text() == full
    assert router.ADDED_SCHEME_NOTE in page.message_label.text()
    assert (runs[0].spec.engine, runs[0].spec.url) == ("ytdlp", full)
    runs[0].emit("error", code="unsupported", message="ERROR: Unsupported URL: [link]")
    assert runs[1].spec.engine == "gallerydl"
    runs[1].emit("error", code="unsupported", message="unsupported url: no gallery found")
    assert runs[2].spec.engine == "http" and len(runs) == 3
    image = protocol.media_result(
        "image", ["image"], "Gx1AbC", full, site="Direct file", ext="jpg",
        preview={"data": _png_b64(40, 30)}, formats=[],
        image_rows=[{"id": "i:orig", "original": True, "ext": "jpg", "default": True}],
    )  # fmt: skip
    runs[2].on_event(Event("result", runs[2].spec.job_id, image))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    # A direct image: one Original row on the Image tab, savable as JPG / PNG / WebP.
    assert page.result_card.tab_names() == ["image"]
    assert [r["id"] for r in page.result_card.rows("image")] == ["i:orig"]
    assert page._thumb is not None


def test_a_real_failure_does_not_move_down_the_chain(window, runs):
    page = window.downloads_page
    page.url_edit.setText("https://www.example.com/post/123")
    page.analyze()
    runs[0].emit("error", code="download_error", message="HTTP Error 500: Server Error")
    assert len(runs) == 1 and page.result_card.isHidden()


# ── the analyzed preview (plan §5.6, R3) ─────────────────────────────────────────────────
def test_a_video_result_draws_a_sharp_480x270_preview_with_its_duration(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, preview={"data": _png_b64(1280, 720)}, duration=244)
    cover = page.result_card.cover
    assert (cover.width(), cover.height()) == (480, 270) and cover.text() == ""
    # Decoded above the 480 px it is shown at, so it is never upscaled.
    assert page._thumb.width() >= 480
    shown = cover.pixmap().toImage()
    assert shown.pixelColor(20, 20).name() == "#336699"
    assert shown.pixelColor(470, 256).name() != "#336699"  # the duration badge


def test_a_song_result_draws_a_300x300_preview(window, runs, qtbot):
    page = _analyzed(
        window, runs, qtbot, url="https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        preview={"data": _png_b64(544, 544)}, duration=61,
    )  # fmt: skip
    cover = page.result_card.cover
    assert (cover.width(), cover.height()) == (300, 300) and cover.text() == ""


def test_no_preview_shows_the_placeholder(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, preview=None)
    assert page.result_card.cover.text() == "🎞" and page.result_card.cover.pixmap().isNull()


def test_a_direct_video_offers_audio_and_frame_rows_that_queue_correctly(window, runs, qtbot):
    from stuff_downloader_worker.engines import http as worker_http

    page = window.downloads_page
    page.url_edit.setText("https://cdn.example.com/v/clip.mp4")
    page.analyze()
    audio = worker_ytdlp.audio_rows([{"vcodec": "none", "acodec": "aac", "abr": 128}], 60)
    payload = protocol.media_result(
        "video", ["video", "audio", "image"], "clip", "https://cdn.example.com/v/clip.mp4",
        site="Direct file", ext="mp4", duration=60, formats=[],
        video_rows=worker_http.file_rows("video", "mp4", 1000)["video_rows"],
        audio_rows=audio,
        image_rows=[{"id": "i:frame", "frame": True, "ext": "jpg", "default": True,
                     "width": 320, "height": 240}],
    )  # fmt: skip
    runs[-1].on_event(Event("result", runs[-1].spec.job_id, payload))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    card = page.result_card
    assert card.tab_names() == ["video", "audio", "image"]
    assert card.tables["image"].item(0, 1).text() == "320×240 · JPG ★"
    card.image_format_combo.setCurrentIndex(card.image_format_combo.findData("png"))
    card.download_button("image", 0).click()
    assert runs[-1].spec.engine == "http"
    assert runs[-1].spec.options == {
        "mode": "download", "tab": "image", "row_id": "i:frame", "container": "png"
    }
    card.download_button("audio", 0).click()
    assert runs[-1].spec.options == {"mode": "download", "tab": "audio", "row_id": "a:mp3:320"}
