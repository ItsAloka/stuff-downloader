from __future__ import annotations

import json
from importlib import metadata

import pytest
from PyQt6.QtGui import QDesktopServices, QGuiApplication

import stuff_downloader
from stuff_downloader.core import runner, settings, tools
from stuff_downloader.gui import main_window
from stuff_downloader.gui.main_window import (
    AboutDialog,
    MainWindow,
    engine_runtime_statuses,
    is_first_run,
)

FOUND_FFMPEG = tools.ToolStatus("ffmpeg", "C:/ffmpeg.exe", "ffmpeg 7.1", "app")
MISSING_DENO = tools.ToolStatus("deno", None, None, "missing", "not found")


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Settings and the engine runtime both live under tmp_path, never the owner's profile."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(tmp_path / "runtime"))
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [FOUND_FFMPEG, MISSING_DENO])
    return path


def _window(qtbot, **kwargs) -> MainWindow:
    w = MainWindow(**kwargs)
    qtbot.addWidget(w)
    return w


# ── version ──────────────────────────────────────────────────────────────────────────────


def test_release_version_comes_from_installed_metadata(monkeypatch):
    monkeypatch.setattr(metadata, "version", lambda name: "9.8.7")
    assert stuff_downloader.release_version() == "9.8.7"


def test_release_version_falls_back_to_pyproject_then_constant(monkeypatch, tmp_path):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    # The real source tree: its [project] version.
    assert stuff_downloader.release_version() == stuff_downloader._pyproject_version(
        stuff_downloader._source_root() / "pyproject.toml"
    )
    monkeypatch.setattr(stuff_downloader, "_source_root", lambda: tmp_path)
    assert stuff_downloader.release_version() == stuff_downloader.__version__


def test_pyproject_version_reads_only_the_project_table(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.x]\nversion = "0.0.0"\n\n[project]\nname = "a"\nversion = "1.2.3"\n'
        '\n[tool.y]\nversion = "4.4.4"\n',
        encoding="utf-8",
    )
    assert stuff_downloader._pyproject_version(pyproject) == "1.2.3"
    pyproject.write_text('[tool.x]\nversion = "0.0.0"\n', encoding="utf-8")
    assert stuff_downloader._pyproject_version(pyproject) is None


# ── icon and about ───────────────────────────────────────────────────────────────────────


def test_committed_icon_is_used_by_the_window(config, qtbot):
    assert stuff_downloader.resource_path("app.ico").is_file()
    assert stuff_downloader.resource_path("app.png").is_file()
    assert not _window(qtbot, app_settings=settings.Settings()).windowIcon().isNull()


def test_about_shows_version_and_licence_files(config, qtbot, monkeypatch):
    monkeypatch.setattr(main_window, "release_version", lambda: "1.0.0")
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    w = _window(qtbot, app_settings=settings.Settings())
    about = w.show_about()
    qtbot.addWidget(about)
    assert about.version_label.text() == "Version 1.0.0"
    assert set(about.open_buttons) == {"LICENSE", "THIRD_PARTY_LICENSES.txt"}
    for name, button in about.open_buttons.items():
        assert button.isEnabled(), name
        button.click()
    assert [u.toLocalFile().rsplit("/", 1)[-1] for u in opened] == [
        "LICENSE",
        "THIRD_PARTY_LICENSES.txt",
    ]
    about.copy_buttons["LICENSE"].click()
    assert QGuiApplication.clipboard().text().endswith("LICENSE")


def test_about_disables_open_for_a_missing_licence(config, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window, "data_root", lambda: tmp_path / "nowhere")
    about = AboutDialog()
    qtbot.addWidget(about)
    assert not any(b.isEnabled() for b in about.open_buttons.values())
    assert AboutDialog.open_file(tmp_path / "nowhere" / "LICENSE") is False


# ── first-run welcome ────────────────────────────────────────────────────────────────────


def test_first_run_is_a_missing_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    assert is_first_run(path)
    path.write_text("{}", encoding="utf-8")
    assert not is_first_run(path)


def test_window_with_given_settings_never_shows_welcome_by_itself(config, qtbot):
    w = _window(qtbot, app_settings=settings.Settings())
    qtbot.wait(20)
    assert w.welcome_dialog is None


def test_first_run_shows_welcome_once_and_skipping_keeps_defaults(config, qtbot):
    w = _window(qtbot)
    qtbot.waitUntil(lambda: w.welcome_dialog is not None)
    dialog = w.welcome_dialog
    texts = [label.text() for label in dialog.check_labels]
    assert texts[0].startswith("✔  FFmpeg — ffmpeg 7.1")
    assert texts[1].startswith("✖  Deno — not found")
    assert any("yt-dlp engine — not installed" in t for t in texts)
    dialog.accept()  # no folder chosen: optional
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["download_dir"] == ""
    second = _window(qtbot)
    qtbot.wait(20)
    assert second.welcome_dialog is None


def test_closing_welcome_also_counts_as_seen(config, qtbot):
    w = _window(qtbot, show_welcome=True)
    qtbot.waitUntil(lambda: w.welcome_dialog is not None)
    w.welcome_dialog.reject()
    assert config.is_file()


def test_welcome_folder_choice_goes_through_settings(config, qtbot, tmp_path):
    folder = tmp_path / "Media"
    folder.mkdir()
    w = _window(qtbot, show_welcome=True)
    qtbot.waitUntil(lambda: w.welcome_dialog is not None)
    w.welcome_dialog.select_folder(str(folder))
    w.welcome_dialog.accept()
    assert w.app_settings.download_dir == str(folder)
    assert w.settings_page.folder_edit.text() == str(folder)
    assert json.loads(config.read_text(encoding="utf-8"))["download_dir"] == str(folder)


def test_welcome_recheck_refreshes_tools_and_engines(config, qtbot, monkeypatch, tmp_path):
    w = _window(qtbot, app_settings=settings.Settings())
    dialog = w.show_welcome()
    qtbot.addWidget(dialog)
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [FOUND_FFMPEG])
    env = tmp_path / "runtime" / "envs" / "ytdlp" / "e1" / "Scripts"
    env.mkdir(parents=True)
    (env / "python.exe").write_bytes(b"")
    (tmp_path / "runtime" / "active.json").write_text(
        json.dumps({"ytdlp": {"active": "e1", "previous": None}}), encoding="utf-8"
    )
    dialog.recheck_button.click()
    texts = [label.text() for label in dialog.check_labels]
    assert len(texts) == 4
    assert "✔  yt-dlp engine — env e1" in texts
    assert "Welcome" in w.welcome_button.text() and w.about_button.text() == "About"


def test_engine_statuses_flag_a_pointer_without_its_interpreter(config, tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "active.json").write_text(
        json.dumps({"spotdl": {"active": "gone"}, "gallerydl": {"active": "..\\x"}}),
        encoding="utf-8",
    )
    statuses = {engine: (ok, detail) for engine, ok, detail in engine_runtime_statuses()}
    assert statuses["spotdl"] == (False, "env gone is missing its interpreter")
    assert statuses["gallerydl"] == (False, "not installed")
    assert statuses["ytdlp"] == (False, "not installed")
