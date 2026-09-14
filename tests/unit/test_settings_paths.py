import json
from pathlib import Path

from stuff_downloader.core import paths, settings


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
