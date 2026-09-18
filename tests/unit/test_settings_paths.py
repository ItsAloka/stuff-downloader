import json
import sys
from pathlib import Path

from stuff_downloader.core import paths, settings, tools


def test_default_download_dir_is_existing_directory():
    assert paths.default_download_dir().is_dir()


def test_default_download_dir_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_known_downloads_folder", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.default_download_dir() == tmp_path
    (tmp_path / "Downloads").mkdir()
    assert paths.default_download_dir() == tmp_path / "Downloads"


def test_config_and_data_dirs_follow_env(isolated_app_dirs):
    assert paths.config_dir() == isolated_app_dirs / "appdata" / "StuffDownloader"
    assert paths.data_dir() == isolated_app_dirs / "localappdata" / "StuffDownloader"


def test_is_writable_dir(tmp_path):
    assert paths.is_writable_dir(tmp_path)
    assert not paths.is_writable_dir(tmp_path / "missing")
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert not paths.is_writable_dir(f)
    assert list(tmp_path.iterdir()) == [f]  # probe file cleaned up


def test_resolve_download_dir_rejects_bad_configured(tmp_path):
    assert paths.resolve_download_dir(str(tmp_path)) == tmp_path
    assert paths.resolve_download_dir(str(tmp_path / "nope")) == paths.default_download_dir()
    assert paths.resolve_download_dir("") == paths.default_download_dir()


def test_settings_missing_file_gives_defaults(tmp_path):
    s = settings.load(tmp_path / "settings.json")
    assert s == settings.Settings()
    assert s.effective_download_dir() == paths.default_download_dir()


def test_settings_round_trip(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    s = settings.Settings(download_dir=str(tmp_path), max_concurrent=2, tool_paths={"ffmpeg": "x"})
    settings.save(s, path)
    loaded = settings.load(path)
    assert loaded == s
    assert json.loads(path.read_text())["schema_version"] == settings.SCHEMA_VERSION
    assert not path.with_suffix(".json.tmp").exists()


def test_settings_default_path_uses_appdata(isolated_app_dirs):
    settings.save(settings.Settings(max_concurrent=4))
    assert (isolated_app_dirs / "appdata" / "StuffDownloader" / "settings.json").is_file()
    assert settings.load().max_concurrent == 4


def test_settings_corrupt_or_invalid_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert settings.load(path) == settings.Settings()
    path.write_text("[1, 2]")
    assert settings.load(path) == settings.Settings()
    path.write_text(json.dumps({"download_dir": 5, "max_concurrent": 99, "tool_paths": {"a": 1}}))
    s = settings.load(path)
    assert s.download_dir == "" and s.max_concurrent == 5 and s.tool_paths == {}
    path.write_text(json.dumps({"max_concurrent": True}))
    assert settings.load(path).max_concurrent == 3

# ── the notifications setting ────────────────────────────────────
# Merged from test_settings.py, which existed only because the notifications field was
# added under a write lease that covered that one path.
def test_notifications_defaults_to_on():
    assert settings.Settings().notifications is True


def test_notifications_survives_a_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    saved = settings.Settings(notifications=False)
    settings.save(saved, path)
    assert json.loads(path.read_text(encoding="utf-8"))["notifications"] is False
    assert settings.load(path).notifications is False

    settings.save(settings.Settings(notifications=True), path)
    assert settings.load(path).notifications is True


def test_a_corrupt_notifications_value_falls_back_to_the_default(tmp_path):
    path = tmp_path / "settings.json"
    for bad in ("yes", 1, None, {}):
        path.write_text(json.dumps({"notifications": bad}), encoding="utf-8")
        assert settings.load(path).notifications is True


def test_a_missing_notifications_key_is_not_an_error(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"download_dir": ""}), encoding="utf-8")
    assert settings.load(path).notifications is True


# ── where a frozen build looks for ffmpeg ─────────────────────────────────
# Regression cover for a real packaging defect: the spec bundled ffmpeg/ffprobe/deno correctly,
# PyInstaller 6 placed them under _internal (sys._MEIPASS), and app_tools_dir() looked only next
# to the exe -- so a build that contained all three reported all three "not found", and the
# packaged app could not convert a single file. Only a real build showed it.


def _frozen(monkeypatch, exe_dir, meipass=None):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "StuffDownloader.exe"))
    if meipass is None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    else:
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)


def test_frozen_finds_tools_bundled_under_meipass(monkeypatch, tmp_path):
    """The normal PyInstaller 6 onedir layout: datas land in _internal."""
    exe_dir = tmp_path / "StuffDownloader"
    internal = exe_dir / "_internal"
    (internal / "tools").mkdir(parents=True)
    _frozen(monkeypatch, exe_dir, meipass=internal)

    assert tools.app_tools_dir() == internal / "tools"


def test_a_tools_folder_beside_the_exe_overrides_the_bundled_one(monkeypatch, tmp_path):
    """So a broken or outdated bundled ffmpeg can be replaced without rebuilding the app."""
    exe_dir = tmp_path / "StuffDownloader"
    internal = exe_dir / "_internal"
    (internal / "tools").mkdir(parents=True)
    (exe_dir / "tools").mkdir(parents=True)
    _frozen(monkeypatch, exe_dir, meipass=internal)

    assert tools.app_tools_dir() == exe_dir / "tools"


def test_frozen_without_meipass_falls_back_beside_the_exe(monkeypatch, tmp_path):
    exe_dir = tmp_path / "StuffDownloader"
    exe_dir.mkdir(parents=True)
    _frozen(monkeypatch, exe_dir, meipass=None)

    assert tools.app_tools_dir() == exe_dir / "tools"


def test_from_source_the_tools_dir_is_the_repo_folder(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert tools.app_tools_dir().name == "tools"
    assert (tools.app_tools_dir().parent / "pyproject.toml").is_file()
