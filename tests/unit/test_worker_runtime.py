"""Frozen builds must run workers in the engine runtime, never by re-running the GUI exe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from stuff_downloader.core import runner


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "gui" / "StuffDownloader.exe"))
    root = tmp_path / "runtime"
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(root))
    monkeypatch.delenv(runner.WORKER_PATH_ENV_VAR, raising=False)
    return root


def test_dev_uses_current_interpreter(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runner.default_worker_command("ytdlp")[0] == sys.executable


def test_dev_uses_installed_engine_env_for_real_engines_only(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(runner.RUNTIME_ENV_VAR, str(tmp_path))
    env_python = _touch(tmp_path / "envs" / "ytdlp" / "2026.8.19-abc" / "Scripts" / "python.exe")
    _touch(tmp_path / "python" / "python.exe")
    (tmp_path / "active.json").write_text(
        json.dumps({"ytdlp": {"active": "2026.8.19-abc"}, "fake": {"active": "x"}}),
        encoding="utf-8",
    )
    assert runner.default_worker_command("ytdlp") == [
        str(env_python),
        "-s",
        "-u",
        "-m",
        "stuff_downloader_worker",
    ]
    assert runner.default_worker_command("fake")[0] == sys.executable
    env_python.unlink()
    assert runner.default_worker_command("ytdlp")[0] == sys.executable


def test_dev_external_python_env_drops_interpreter_vars(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "C:\\dev\\.venv\\Scripts\\python.exe")
    monkeypatch.setenv("PYTHONPATH", "C:\\elsewhere")
    src = str(Path(runner.__file__).resolve().parents[2])
    env = runner._worker_env(external_python=True)
    assert "__PYVENV_LAUNCHER__" not in env and env["PYTHONPATH"] == src
    assert "__PYVENV_LAUNCHER__" in runner._worker_env()


def test_default_runtime_root_is_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv(runner.RUNTIME_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert runner.runtime_root() == tmp_path / "StuffDownloader" / "runtime"


def test_frozen_without_active_env_uses_base_python(frozen):
    python = _touch(frozen / "python" / "python.exe")
    cmd = runner.default_worker_command("fake")
    assert cmd == [str(python), "-s", "-u", "-m", "stuff_downloader_worker"]


def test_frozen_uses_active_engine_env(frozen):
    _touch(frozen / "python" / "python.exe")
    env_python = _touch(frozen / "envs" / "ytdlp" / "2026.8.19-abc" / "Scripts" / "python.exe")
    (frozen / "active.json").write_text(
        json.dumps({"ytdlp": {"active": "2026.8.19-abc", "previous": None}}), encoding="utf-8"
    )
    assert runner.default_worker_command("ytdlp")[0] == str(env_python)
    assert runner.default_worker_command("fake")[0] == str(frozen / "python" / "python.exe")


@pytest.mark.parametrize("pointer", ["..", "..\\..\\evil", "a/b", "", 5, None])
def test_tampered_active_pointer_is_ignored(frozen, pointer):
    _touch(frozen / "python" / "python.exe")
    (frozen / "active.json").write_text(
        json.dumps({"ytdlp": {"active": pointer}}), encoding="utf-8"
    )
    assert runner.runtime_python("ytdlp") == frozen / "python" / "python.exe"


def test_corrupt_active_json_falls_back_to_base(frozen):
    _touch(frozen / "python" / "python.exe")
    (frozen / "active.json").write_text("{not json", encoding="utf-8")
    assert runner.runtime_python("ytdlp") == frozen / "python" / "python.exe"


def test_frozen_missing_runtime_never_falls_back_to_self(frozen):
    with pytest.raises(runner.WorkerRuntimeMissing):
        runner.default_worker_command("fake")


def test_frozen_runtime_equal_to_gui_exe_is_rejected(frozen, monkeypatch):
    python = _touch(frozen / "python" / "python.exe")
    monkeypatch.setattr(sys, "executable", str(python))
    with pytest.raises(runner.WorkerRuntimeMissing):
        runner.default_worker_command("fake")


def test_frozen_env_uses_runtime_app_dir_and_drops_inherited_paths(frozen, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "C:\\somewhere\\else")
    monkeypatch.setenv("PYTHONHOME", "C:\\python")
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "C:\\dev\\.venv\\Scripts\\python.exe")
    monkeypatch.setenv("VIRTUAL_ENV", "C:\\dev\\.venv")
    monkeypatch.setenv("PYTHONSTARTUP", "C:\\evil.py")
    env = runner._worker_env()
    assert env["PYTHONPATH"] == str(frozen / "app")
    for var in ("PYTHONHOME", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        assert var not in env
    monkeypatch.setenv(runner.WORKER_PATH_ENV_VAR, str(tmp_path))
    assert runner._worker_env()["PYTHONPATH"] == str(tmp_path)


def test_worker_env_tools_dir_is_set_by_runner_not_inherited(frozen, monkeypatch):
    monkeypatch.setenv(runner.TOOLS_DIR_ENV_VAR, "C:\\attacker")
    assert runner._worker_env()[runner.TOOLS_DIR_ENV_VAR] == str(runner.tools.app_tools_dir())


def test_dev_env_points_at_src(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    src = Path(runner.__file__).resolve().parents[2]
    assert runner._worker_env()["PYTHONPATH"].split(os.pathsep)[0] == str(src)


def test_frozen_gallerydl_runs_in_its_own_env_never_the_ytdlp_one(frozen):
    """gallery-dl is GPLv2-only (plan §8.3): its worker must come from its own env."""
    expected = _touch(frozen / "envs" / "gallerydl" / "1.32.13-bbbb" / "Scripts" / "python.exe")
    (frozen / "active.json").write_text(
        json.dumps(
            {
                "ytdlp": {"active": "2026.8.19-aaaa", "previous": None},
                "gallerydl": {"active": "1.32.13-bbbb", "previous": None},
            }
        ),
        encoding="utf-8",
    )
    assert runner.default_worker_command("gallerydl")[0] == str(expected)
