import pytest

from stuff_downloader.core import settings, tools
from stuff_downloader.gui import theme
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


def test_shell_has_three_pages_and_navigation(window):
    assert [window.sidebar.item(i).text().split()[-1] for i in range(3)] == [
        "Downloads",
        "Tools",
        "Settings",
    ]
    assert window.stack.currentWidget() is window.downloads_page
    window.sidebar.setCurrentRow(1)
    assert window.stack.currentWidget() is window.tools_page
    window.sidebar.setCurrentRow(2)
    assert window.stack.currentWidget() is window.settings_page


def test_theme_is_applied(window):
    assert theme.ACCENT in window.styleSheet()
    assert window.downloads_page.start_button.objectName() == "primary"


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


def test_downloads_initial_empty_state(window):
    page = window.downloads_page
    assert not page.empty_state.isHidden()
    assert page.job_card.isHidden()
    assert page.queue_summary.text() == "Nothing running"
    assert not page.cancel_button.isEnabled()


def test_downloads_page_runs_fake_job(window, qtbot):
    page = window.downloads_page
    page.url_edit.setText("https://example.invalid/video")
    page.start_fake_job()
    assert page.empty_state.isHidden() and not page.job_card.isHidden()
    assert page.job_card.title_label.text() == "https://example.invalid/video"
    qtbot.waitUntil(lambda: "/s" in page.job_card.details_label.text(), timeout=30000)
    qtbot.waitUntil(lambda: page.status_label.text() == "Completed", timeout=30000)
    assert page.progress.value() == 100
    assert page.job_card.percent_label.text() == "100%"
    assert page.status_label.property("state") == "completed"
    assert page.progress.property("state") == "completed"
    assert page.queue_summary.text() == "1 done"
    assert page.start_button.isEnabled() and not page.cancel_button.isEnabled()


def test_downloads_page_cancel(window, qtbot):
    page = window.downloads_page
    page.start_fake_job()
    qtbot.waitUntil(lambda: page.progress.value() > 0, timeout=30000)
    page.cancel_job()
    qtbot.waitUntil(lambda: page.status_label.text() == "Cancelled", timeout=30000)
    assert page.status_label.property("state") == "cancelled"
    assert page.queue_summary.text() == "1 cancelled"
    assert page.start_button.isEnabled() and not page.cancel_button.isEnabled()


def test_downloads_page_failure_state(window):
    from stuff_downloader.core.protocol import Event

    page = window.downloads_page
    page.start_fake_job()
    job_id = page._run.spec.job_id
    run = page._run
    page._on_event(Event("error", job_id, {"code": "boom", "message": "Private on the site"}))
    run.cancel()
    assert page.status_label.text() == "Failed"
    assert page.status_label.property("state") == "failed"
    assert page.job_card.details_label.text() == "Private on the site"


def test_downloads_page_missing_engine_runtime_fails_cleanly(window, monkeypatch, tmp_path):
    import sys

    from stuff_downloader.core import runner

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(tmp_path / "missing" / "python.exe"))
    page = window.downloads_page
    page.start_fake_job()
    assert page._run is None
    assert page.status_label.text() == "Failed"
    assert page.status_label.property("state") == "failed"
    assert "engine runtime not found" in page.job_card.details_label.text()
    assert page.queue_summary.text() == "1 failed"
    assert page.start_button.isEnabled() and not page.cancel_button.isEnabled()


def test_formatters():
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert format_bytes(None) == "—" and format_bytes(-1) == "—" and format_bytes(True) == "—"
    assert format_eta(5.4) == "5s left"
    assert format_eta(125) == "2m 05s left"
    assert format_eta(None) == ""
