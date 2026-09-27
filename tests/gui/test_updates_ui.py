"""GUI: library updates on app start (plan §2, R8) — popup, Settings, Welcome/Tools, watchdog."""

from __future__ import annotations

import json
import threading

import pytest
from test_main_window import _analyzed
from test_playlist_queue import runs, window  # noqa: F401  (fixtures)

from stuff_downloader.core import engine_update, runner, settings, tools, updates
from stuff_downloader.gui import main_window, pages
from stuff_downloader.gui.main_window import MainWindow, UpdatesDialog, UpdateService

YTDLP = updates.Offer("ytdlp", "yt-dlp", "2026.8.1", "2026.9.20", recommended=True)
YTMUSIC = updates.Offer("music", "ytmusicapi", "1.8.1", "1.8.3", newer_major="2.0.0")
MAJOR_ONLY = updates.Offer("music", "requests", "2.32.3", None, newer_major="3.0.0")


def result(offers=(), reached=True, installed=None):
    return updates.CheckResult(
        list(offers), installed or {"ytdlp": {"yt-dlp": "2026.8.1"}}, reached
    )


class FakeWatchdog:
    def __init__(self, watching=None, rollback_to=None):
        self._watching = watching or {}
        self.rollback_to = rollback_to
        self.records = []

    def watching(self, engine):
        return self._watching.get(engine)

    def record(self, engine, env_id, started):
        self.records.append((engine, env_id, started))
        return None if started else self.rollback_to


class Fakes:
    """What the UpdateService calls, recorded; each call can be made to fail."""

    def __init__(self, check_result=None):
        self.check_result = check_result or result()
        self.check_error: Exception | None = None
        self.install_errors: dict[str, Exception] = {}
        self.undo_errors: dict[str, Exception] = {}
        self.checks, self.installs, self.undos = [], [], []
        self.watchdog = FakeWatchdog()
        self.gate = threading.Event()
        self.gate.set()

    def check(self, snapshot):
        self.checks.append(snapshot)
        if self.check_error:
            raise self.check_error
        return self.check_result

    def install(self, engine, selections, progress=None):
        self.gate.wait(5)
        self.installs.append((engine, dict(selections)))
        progress("resolve", "Finding the files")
        progress("install", "Installing and testing")
        progress("prune", "Removing old engine versions")
        if engine in self.install_errors:
            raise self.install_errors[engine]
        return "new"

    def undo(self, engine):
        self.undos.append(engine)
        if engine in self.undo_errors:
            raise self.undo_errors[engine]
        return "old"

    def service(self, parent=None):
        return UpdateService(
            parent,
            check=self.check,
            install=self.install,
            undo=self.undo,
            watchdog=lambda: self.watchdog,
        )


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def uwin(qtbot, monkeypatch, fakes):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    w = MainWindow(settings.Settings(), updater=fakes.service())
    qtbot.addWidget(w)
    w.checks_seen = []
    w.updater.checked.connect(lambda *args: w.checks_seen.append(args))
    return w


def _checked(qtbot, w):
    """Wait for the next finished check. The count is taken in the fixture, before any click."""
    seen = w.checks_seen
    target = w.checks_waited = getattr(w, "checks_waited", 0) + 1
    qtbot.waitUntil(lambda: len(seen) >= target, timeout=5000)


# ── the popup ────────────────────────────────────────────────────────────────────────────
def test_popup_rows_follow_the_r8_rules(qtbot):
    dialog = UpdatesDialog([YTDLP, YTMUSIC, MAJOR_ONLY])
    qtbot.addWidget(dialog)
    texts = [box.text() for box, _ in dialog.rows]
    assert texts == [
        "yt-dlp    2026.8.1 → 2026.9.20    (recommended)",
        "ytmusicapi    1.8.1 → 1.8.3",
    ]
    assert all(box.isChecked() for box, _ in dialog.rows)
    greyed = [label.text() for label, _ in dialog.blocked_rows]
    assert greyed == [
        "ytmusicapi    1.8.1 → 2.0.0    needs an app update",
        "requests    2.32.3 → 3.0.0    needs an app update",
    ]
    assert not any(label.isEnabled() for label, _ in dialog.blocked_rows)
    assert [b.text() for b in (dialog.update_button, dialog.later_button, dialog.skip_button)] == [
        "Update selected",
        "Later",
        "Skip this version",
    ]


def test_update_selected_sends_only_ticked_rows(qtbot):
    dialog = UpdatesDialog([YTDLP, YTMUSIC])
    qtbot.addWidget(dialog)
    dialog.rows[1][0].setChecked(False)
    with qtbot.waitSignal(dialog.update_requested) as sent:
        dialog.update_button.click()
    assert sent.args == [[YTDLP]]
    dialog.rows[0][0].setChecked(False)
    assert not dialog.update_button.isEnabled()


def test_skip_covers_ticked_versions_and_greyed_majors(qtbot):
    dialog = UpdatesDialog([YTDLP, YTMUSIC, MAJOR_ONLY])
    qtbot.addWidget(dialog)
    dialog.rows[0][0].setChecked(False)
    assert dialog.skip_targets() == [
        ("ytmusicapi", "1.8.3"),
        ("ytmusicapi", "2.0.0"),
        ("requests", "3.0.0"),
    ]


def test_an_install_cannot_be_closed_until_it_finishes(qtbot):
    dialog = UpdatesDialog([YTDLP])
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.start_install()
    assert not dialog.update_button.isEnabled() and not dialog.later_button.isEnabled()
    dialog.reject()
    assert dialog.isVisible()
    dialog.set_progress(2, 3, "Installing")
    assert dialog.progress_bar.value() == 2 and dialog.status_label.text() == "Installing"
    dialog.finish_install(True, "Updated")
    assert dialog.close_button.isVisible() and not dialog.update_button.isVisible()
    dialog.reject()
    assert not dialog.isVisible()


# ── the check ────────────────────────────────────────────────────────────────────────────
def test_startup_check_runs_after_the_window_opens_and_shows_the_popup(qtbot, monkeypatch, fakes):
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    fakes.check_result = result([YTDLP])
    w = MainWindow(settings.Settings(), check_updates=True, updater=fakes.service())
    qtbot.addWidget(w)
    assert fakes.checks == []  # constructing the window never waits on PyPI
    w.show()
    qtbot.waitUntil(lambda: w.updates_dialog is not None, timeout=5000)
    assert w.updates_dialog.offers == [YTDLP]
    assert w.app_settings.update_last_check > 0
    assert settings.load().update_last_check == w.app_settings.update_last_check


def test_windows_handed_settings_do_not_check_by_themselves(uwin, qtbot, fakes):
    uwin.show()
    qtbot.wait(1800)
    assert fakes.checks == []


def test_startup_check_respects_the_switch_and_the_24h_limit(uwin, fakes):
    uwin.app_settings.update_check_on_start = False
    assert not uwin.startup_update_check()
    uwin.app_settings.update_check_on_start = True
    uwin.app_settings.update_last_check = __import__("time").time() - 60
    assert not uwin.startup_update_check()
    assert fakes.checks == []


@pytest.mark.parametrize("failure", ["offline", "raises"])
def test_offline_or_a_crashing_check_shows_no_popup(uwin, qtbot, fakes, failure):
    if failure == "offline":
        fakes.check_result = result([YTDLP], reached=False)
    else:
        fakes.check_error = RuntimeError("boom")
    assert uwin.startup_update_check()
    _checked(qtbot, uwin)
    assert uwin.updates_dialog is None
    assert uwin.app_settings.update_last_check == 0.0  # it asks again next start
    assert uwin.settings_page.update_status.text() == ""  # the startup check says nothing


def test_manual_check_offline_says_so_without_a_popup(uwin, qtbot, fakes):
    fakes.check_result = result([YTDLP], reached=False)
    uwin.settings_page.check_updates_button.click()
    _checked(qtbot, uwin)
    assert uwin.updates_dialog is None
    assert "Could not reach PyPI" in uwin.settings_page.update_status.text()


def test_manual_check_ignores_the_24h_limit_and_reports_up_to_date(uwin, qtbot, fakes):
    uwin.app_settings.update_last_check = __import__("time").time()
    uwin.settings_page.check_updates_button.click()
    assert not uwin.settings_page.check_updates_button.isEnabled()
    _checked(qtbot, uwin)
    assert len(fakes.checks) == 1
    assert uwin.updates_dialog is None
    assert uwin.settings_page.update_status.text() == "Everything is up to date."
    assert uwin.settings_page.check_updates_button.isEnabled()


def test_startup_only_interrupts_for_installable_updates(uwin, qtbot, fakes):
    fakes.check_result = result([MAJOR_ONLY])
    uwin.startup_update_check()
    _checked(qtbot, uwin)
    assert uwin.updates_dialog is None
    uwin.check_for_updates()  # asked for: greyed rows are shown too
    _checked(qtbot, uwin)
    assert uwin.updates_dialog is not None and uwin.updates_dialog.offers == [MAJOR_ONLY]


def test_the_check_works_on_a_copy_of_the_settings(uwin, qtbot, fakes):
    uwin.check_for_updates()
    _checked(qtbot, uwin)
    assert fakes.checks[0] is not uwin.app_settings


def test_tools_page_has_check_for_updates_beside_recheck(uwin, qtbot, fakes):
    uwin.tools_page.check_updates_button.click()
    _checked(qtbot, uwin)
    assert len(fakes.checks) == 1


def test_welcome_check_for_updates_marks_engine_lines(uwin, qtbot, fakes, tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(root))
    for engine in ("ytdlp", "gallerydl"):
        env = root / "envs" / engine / "e1" / "Scripts"
        env.mkdir(parents=True)
        (env / "python.exe").write_bytes(b"")
    (root / "active.json").write_text(
        json.dumps({"ytdlp": {"active": "e1"}, "gallerydl": {"active": "e1"}}), encoding="utf-8"
    )
    fakes.check_result = result(
        [YTDLP], installed={"ytdlp": {"yt-dlp": "2026.8.1"}, "gallerydl": {"gallery-dl": "1.30"}}
    )
    dialog = uwin.show_welcome()
    qtbot.addWidget(dialog)
    assert dialog.check_updates_button.text() == "Check for updates"
    dialog.check_updates_button.click()
    _checked(qtbot, uwin)
    texts = [label.text() for label in dialog.check_labels]
    assert "✔  yt-dlp engine — env e1 · update yt-dlp 2026.9.20 available" in texts
    assert "✔  gallery-dl engine — env e1 · up to date" in texts
    assert "✖  music engine — not installed" in texts


# ── install, skip, undo ──────────────────────────────────────────────────────────────────
def test_update_selected_installs_per_engine_and_reports_success(uwin, qtbot, fakes):
    uwin.show_updates([YTDLP, YTMUSIC])
    dialog = uwin.updates_dialog
    qtbot.addWidget(dialog)
    fakes.gate.clear()
    dialog.update_button.click()
    assert dialog.installing and not uwin.settings_page.check_updates_button.isEnabled()
    fakes.gate.set()
    qtbot.waitUntil(lambda: not dialog.installing, timeout=5000)
    assert fakes.installs == [
        ("ytdlp", {"yt-dlp": "2026.9.20"}),
        ("music", {"ytmusicapi": "1.8.3"}),
    ]
    assert dialog.progress_bar.value() == dialog.progress_bar.maximum()
    assert dialog.status_label.text() == "Updated and tested: yt-dlp engine, music engine."
    assert uwin.app_settings.update_last_engines == ["ytdlp", "music"]
    assert settings.load().update_last_engines == ["ytdlp", "music"]


def test_a_failed_install_shows_a_bounded_error_and_keeps_the_current_engine(uwin, qtbot, fakes):
    fakes.install_errors["ytdlp"] = engine_update.UpdateError("self-test failed\n" + "x" * 5000)
    uwin.show_updates([YTDLP])
    dialog = uwin.updates_dialog
    qtbot.addWidget(dialog)
    dialog.update_button.click()
    qtbot.waitUntil(lambda: dialog.close_button.isVisible(), timeout=5000)
    text = dialog.status_label.text()
    assert text.startswith("Not updated, the current version is still in use:")
    assert "self-test failed" in text and len(text) < 700
    assert uwin.app_settings.update_last_engines == []


def test_an_unexpected_install_error_is_reported_not_raised(uwin, qtbot, fakes):
    fakes.install_errors["ytdlp"] = OSError("disk full")
    uwin.show_updates([YTDLP])
    uwin.updates_dialog.update_button.click()
    qtbot.waitUntil(lambda: uwin.updates_dialog.close_button.isVisible(), timeout=5000)
    assert "disk full" in uwin.updates_dialog.status_label.text()


def test_skip_this_version_is_remembered(uwin, qtbot):
    uwin.show_updates([YTDLP, MAJOR_ONLY])
    uwin.updates_dialog.skip_button.click()
    assert uwin.app_settings.update_skips == {"yt-dlp": ["2026.9.20"], "requests": ["3.0.0"]}
    assert settings.load().update_skips == uwin.app_settings.update_skips


def test_later_just_closes(uwin, qtbot, fakes):
    uwin.show_updates([YTDLP])
    uwin.updates_dialog.later_button.click()
    assert not uwin.updates_dialog.isVisible()
    assert fakes.installs == [] and uwin.app_settings.update_skips == {}


def test_undo_last_update_switches_back_the_updated_engines(uwin, qtbot, fakes, monkeypatch):
    monkeypatch.setattr(engine_update, "can_undo", lambda engine, root=None: engine != "spotdl")
    assert not uwin.settings_page.undo_update_button.isEnabled()
    uwin.app_settings.update_last_engines = ["ytdlp", "spotdl", "bogus"]
    uwin._refresh_undo()
    assert uwin.settings_page.undo_update_button.isEnabled()
    with qtbot.waitSignal(uwin.updater.undone, timeout=5000):
        uwin.settings_page.undo_update_button.click()
    assert fakes.undos == ["ytdlp"]
    assert uwin.app_settings.update_last_engines == ["spotdl", "bogus"]
    assert uwin.settings_page.update_status.text() == "Switched back: yt-dlp engine."
    assert not uwin.settings_page.undo_update_button.isEnabled()


def test_a_failed_undo_is_shown(uwin, qtbot, fakes, monkeypatch):
    monkeypatch.setattr(engine_update, "can_undo", lambda engine, root=None: True)
    fakes.undo_errors["ytdlp"] = engine_update.UpdateError("no previous ytdlp env")
    uwin.app_settings.update_last_engines = ["ytdlp"]
    with qtbot.waitSignal(uwin.updater.undone, timeout=5000):
        assert uwin.undo_last_update()
    assert "Could not switch back" in uwin.settings_page.update_status.text()
    assert uwin.app_settings.update_last_engines == ["ytdlp"]


def test_only_one_update_task_runs_at_a_time(uwin, qtbot, fakes):
    fakes.gate.clear()
    uwin.show_updates([YTDLP])
    uwin.updates_dialog.update_button.click()
    assert not uwin.check_for_updates()
    assert "already running" in uwin.settings_page.update_status.text()
    fakes.gate.set()
    qtbot.waitUntil(lambda: not uwin.updater.busy, timeout=5000)


# ── Settings ─────────────────────────────────────────────────────────────────────────────
def test_settings_shows_library_versions_and_the_startup_switch(uwin):
    page = uwin.settings_page
    page.set_library_versions({"ytdlp": {"yt-dlp": "2026.8.1", "curl-cffi": "0.13.0"}})
    texts = [label.text() for label in page._library_widgets]
    assert texts == ["yt-dlp engine:  yt-dlp 2026.8.1,  curl-cffi 0.13.0"]
    page.set_library_versions({})
    assert "No engine libraries" in page._library_widgets[0].text()
    assert page.update_on_start_check.isChecked()
    page.update_on_start_check.setChecked(False)
    assert settings.load().update_check_on_start is False
    assert [page.check_updates_button.text(), page.undo_update_button.text()] == [
        "Check for updates",
        "Undo last update",
    ]


def test_update_last_engines_round_trips_and_rejects_junk(tmp_path):
    path = tmp_path / "settings.json"
    settings.save(settings.Settings(update_last_engines=["ytdlp", "music"]), path)
    assert settings.load(path).update_last_engines == ["ytdlp", "music"]
    path.write_text(json.dumps({"update_last_engines": ["ytdlp", 3, None]}))
    assert settings.load(path).update_last_engines == ["ytdlp"]
    path.write_text(json.dumps({"update_last_engines": "ytdlp"}))
    assert settings.load(path).update_last_engines == []


def test_library_versions_reads_each_engines_env(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "tracked_packages", lambda: {"ytdlp": ["yt-dlp"], "music": ["x"]})
    monkeypatch.setattr(
        updates, "env_site_packages", lambda engine: tmp_path if engine == "ytdlp" else None
    )
    monkeypatch.setattr(updates, "installed_versions", lambda site: {"yt-dlp": "1", "other": "2"})
    assert pages.library_versions() == {"ytdlp": {"yt-dlp": "1"}}
    monkeypatch.setattr(updates, "tracked_packages", lambda: 1 / 0)
    assert pages.library_versions() == {}


# ── the rollback watchdog ────────────────────────────────────────────────────────────────
@pytest.fixture
def watched(window, monkeypatch, fakes):  # noqa: F811
    """The shared download-test window, with a fake updater and an env id for every run."""
    service = fakes.service(window)
    service.rolled_back.connect(window._on_rolled_back)
    window.downloads_page.engine_start.disconnect()
    window.downloads_page.engine_start.connect(service.record_start)
    monkeypatch.setattr(pages, "_run_env", lambda engine: ("ytdlp", "e2"))
    fakes.watchdog._watching = {"ytdlp": "e2"}
    return window


def _download(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    if page.result_card.isHidden():
        _analyzed(window, runs, qtbot)
    job = page.start_download()
    return next(r for r in runs if r.spec.job_id == job.spec.job_id)


def _wait_records(qtbot, fakes, n):
    qtbot.waitUntil(lambda: len(fakes.watchdog.records) == n, timeout=5000)


def test_first_progress_counts_as_an_engine_start(watched, runs, qtbot, fakes):  # noqa: F811
    run = _download(watched, runs, qtbot)
    run.emit("stage", stage="downloading")
    run.emit("progress", percent=5)
    _wait_records(qtbot, fakes, 1)
    assert fakes.watchdog.records == [("ytdlp", "e2", True)]


@pytest.mark.parametrize(
    ("code", "started"),
    [("engine_missing", False), ("worker_exited", False), ("download_error", True)],
)
def test_errors_before_any_progress_count_by_kind(watched, runs, qtbot, fakes, code, started):  # noqa: F811
    run = _download(watched, runs, qtbot)
    run.emit("error", code=code, message="ERROR: Private video")
    _wait_records(qtbot, fakes, 1)
    assert fakes.watchdog.records == [("ytdlp", "e2", started)]


def test_a_cancel_before_start_is_not_counted(watched, runs, qtbot, fakes):  # noqa: F811
    run = _download(watched, runs, qtbot)
    run.emit("error", code="cancelled", message="Cancelled")
    qtbot.wait(100)
    assert fakes.watchdog.records == []


def test_unwatched_envs_cost_nothing(watched, runs, qtbot, fakes):  # noqa: F811
    fakes.watchdog._watching = {}
    run = _download(watched, runs, qtbot)
    run.emit("error", code="engine_missing", message="gone")
    qtbot.wait(100)
    assert fakes.watchdog.records == []


def test_an_automatic_rollback_is_announced(watched, runs, qtbot, fakes):  # noqa: F811
    fakes.watchdog.rollback_to = "e1"
    watched.app_settings.update_last_engines = ["ytdlp", "music"]
    run = _download(watched, runs, qtbot)
    run.emit("error", code="engine_crashed", message="ImportError")
    qtbot.waitUntil(
        lambda: "previous version is back" in watched.settings_page.update_status.text(),
        timeout=5000,
    )
    assert watched.app_settings.update_last_engines == ["music"]


def test_the_real_watchdog_rolls_back_after_three_engine_start_failures(tmp_path, qtbot):
    rolled = []
    dog = engine_update.Watchdog(tmp_path, rollback=lambda engine: rolled.append(engine) or "e1")
    dog.arm("ytdlp", "e2")
    service = UpdateService(watchdog=lambda: dog)
    for _ in range(2):
        assert service.record_start("ytdlp", "e2", False)
    for t in service.threads:
        t.join(5)
    assert rolled == []
    with qtbot.waitSignal(service.rolled_back, timeout=5000) as signal:
        service.record_start("ytdlp", "e2", False)
    assert signal.args == ["ytdlp"] and rolled == ["ytdlp"]
    assert dog.watching("ytdlp") is None
    assert not service.record_start("ytdlp", "e2", False)  # no longer watched


def test_engine_labels_include_the_music_engine():
    assert main_window.ENGINE_LABELS["music"] == "music engine"
