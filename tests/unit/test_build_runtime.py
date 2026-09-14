"""packaging/build_runtime.py: hash checks, safe extraction and the active/rollback pointer."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
REQS = PACKAGING / "engine-requirements"


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("build_runtime", PACKAGING / "build_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["ytdlp.txt", "ytdlp-previous.txt", "spotdl.txt"])
def test_engine_requirements_are_fully_hash_pinned(br, name):
    br._check_hashes(REQS / name)
    for line in (REQS / name).read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            assert "==" in line, line


def test_pinned_versions(br):
    assert br._pinned_version(REQS / "ytdlp.txt", "yt-dlp") == "2026.8.19"
    assert br._pinned_version(REQS / "ytdlp-previous.txt", "yt-dlp") == "2026.7.4"
    assert br._pinned_version(REQS / "spotdl.txt", "spotdl") == "4.5.2"


def test_unhashed_requirements_are_refused(br, tmp_path):
    reqs = tmp_path / "r.txt"
    reqs.write_text("yt-dlp==2026.8.19\n", encoding="utf-8")
    with pytest.raises(br.RuntimeBuildError):
        br._check_hashes(reqs)


def test_non_https_download_is_refused(br, tmp_path):
    with pytest.raises(br.RuntimeBuildError):
        br.download_verified("http://example.invalid/x", "0" * 64, tmp_path / "x")


def test_archive_path_traversal_is_refused(br, tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(br.RuntimeBuildError):
        br._safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_env_based_on_other_python_is_refused(br, tmp_path):
    env = tmp_path / "envs" / "ytdlp" / "x"
    env.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = C:\\Python311\n", encoding="utf-8")
    with pytest.raises(br.RuntimeBuildError):
        br._verify_env_base(tmp_path, env)
    (env / "pyvenv.cfg").write_text(f"home = {tmp_path / 'python'}\n", encoding="utf-8")
    br._verify_env_base(tmp_path, env)


def test_child_env_drops_launcher_and_pythonpath(br, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs["env"])
        return br.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(br.subprocess, "run", fake_run)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "C:\\x\\python.exe")
    monkeypatch.setenv("PYTHONPATH", "C:\\y")
    br._run(["python", "-V"])
    assert "__PYVENV_LAUNCHER__" not in seen and "PYTHONPATH" not in seen


def _fake_env(root: Path, engine: str, env_id: str) -> None:
    python = root / "envs" / engine / env_id / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")


def test_rollback_swaps_active_and_previous(br, tmp_path):
    _fake_env(tmp_path, "ytdlp", "old")
    _fake_env(tmp_path, "ytdlp", "new")
    br.save_active(tmp_path, {"ytdlp": {"active": "new", "previous": "old"}})
    assert br.rollback_engine(tmp_path, "ytdlp") == "old"
    assert br.load_active(tmp_path)["ytdlp"] == {"active": "old", "previous": "new"}
    assert br.rollback_engine(tmp_path, "ytdlp") == "new"


def test_rollback_without_previous_env_fails(br, tmp_path):
    br.save_active(tmp_path, {"ytdlp": {"active": "new", "previous": "gone"}})
    with pytest.raises(br.RuntimeBuildError):
        br.rollback_engine(tmp_path, "ytdlp")
