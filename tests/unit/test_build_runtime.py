"""packaging/build_runtime.py: hash checks, safe extraction and the active/rollback pointer."""

from __future__ import annotations

import importlib.util
import io
import json
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


@pytest.mark.parametrize("name", ["ytdlp.txt", "ytdlp-previous.txt", "spotdl.txt", "gallerydl.txt"])
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


def test_install_worker_replaces_stale_file_targets_without_losing_working_copy(
    br, tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__main__.py").write_text("new", encoding="utf-8")
    monkeypatch.setattr(br, "PROJECT", tmp_path)
    (tmp_path / "src").mkdir()
    source.rename(tmp_path / "src" / "stuff_downloader_worker")
    dest = tmp_path / "runtime" / "app" / "stuff_downloader_worker"
    dest.parent.mkdir(parents=True)
    dest.write_text("stale file", encoding="utf-8")
    dest.with_name(dest.name + ".new").write_text("stale stage", encoding="utf-8")
    dest.with_name(dest.name + ".bak").write_text("stale backup", encoding="utf-8")

    assert br.install_worker(tmp_path / "runtime") == dest
    assert (dest / "__main__.py").read_text(encoding="utf-8") == "new"
    assert not dest.with_name(dest.name + ".new").exists()
    assert not dest.with_name(dest.name + ".bak").exists()


def test_install_worker_restores_old_copy_when_the_final_rename_fails(br, tmp_path, monkeypatch):
    source = tmp_path / "src" / "stuff_downloader_worker"
    source.mkdir(parents=True)
    (source / "__main__.py").write_text("new", encoding="utf-8")
    monkeypatch.setattr(br, "PROJECT", tmp_path)
    dest = tmp_path / "runtime" / "app" / "stuff_downloader_worker"
    dest.mkdir(parents=True)
    (dest / "__main__.py").write_text("old", encoding="utf-8")
    real_rename = br._rename_dir

    def fail_new(source_path, target_path):
        if source_path.name.endswith(".new"):
            raise OSError(183, "already exists")
        real_rename(source_path, target_path)

    monkeypatch.setattr(br, "_rename_dir", fail_new)
    with pytest.raises(OSError):
        br.install_worker(tmp_path / "runtime")
    assert (dest / "__main__.py").read_text(encoding="utf-8") == "old"


# ── engine update: install, activate, roll back (all subprocesses faked) ────────────────────

HASH = "--hash=sha256:" + "a" * 64


def _reqs(tmp_path: Path, version: str = "2026.9.1", body: str | None = None) -> Path:
    reqs = tmp_path / f"reqs-{version}.txt"
    text = body if body is not None else f"yt-dlp=={version} \\\n    {HASH}\n"
    reqs.write_text(text, encoding="utf-8")
    return reqs


def _fake_env(root: Path, engine: str, env_id: str) -> Path:
    env = root / "envs" / engine / env_id
    python = env / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (env / "pyvenv.cfg").write_text(f"home = {root / 'python'}\n", encoding="utf-8")
    return env


def _kind(args: list[str]) -> str:
    if "venv" in args:
        return "venv"
    if "pip" in args:
        return "pip"
    if "--self-test" in args:
        return "self-test"
    return "check"


class FakeRuntime:
    """Stands in for every subprocess: venv creation, pip, the import check, the self-test."""

    def __init__(self, br, root: Path) -> None:
        self.br = br
        self.root = root
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.fail: dict[str, set[str]] = {"pip": set(), "check": set(), "self-test": set()}
        self.fail_self_test_after_switch = False
        (root / "python").mkdir(parents=True, exist_ok=True)
        (root / "python" / "python.exe").write_bytes(b"")
        worker = root / "app" / "stuff_downloader_worker"
        worker.mkdir(parents=True, exist_ok=True)
        (worker / "__main__.py").write_text("", encoding="utf-8")

    def _switched_to(self, env_id: str) -> bool:
        pointer = self.root / "active.json"
        return pointer.is_file() and f'"active": "{env_id}"' in pointer.read_text("utf-8")

    def __call__(self, args, extra_env=None, **kwargs):
        self.calls.append(list(args))
        self.envs.append(dict(extra_env or {}))
        kind = _kind(args)
        done = self.br.subprocess.CompletedProcess
        if kind == "venv":
            _fake_env(self.root, Path(args[3]).parent.name, Path(args[3]).name)
            return done(args, 0, "", "")
        env_id = Path(args[0]).parents[1].name
        if kind in ("pip", "check") and env_id in self.fail[kind]:
            raise self.br.RuntimeBuildError(f"{kind} failed")
        if kind == "pip":
            return done(args, 0, "", "")
        if kind == "check":
            return done(args, 0, json.dumps({"yt_dlp": "x"}), "")
        failing = env_id in self.fail["self-test"] or (
            self.fail_self_test_after_switch and self._switched_to(env_id)
        )
        return done(args, 0, f"worker self-test: {'FAILED' if failing else 'OK'}", "")


@pytest.fixture
def runtime(br, tmp_path, monkeypatch):
    fake = FakeRuntime(br, tmp_path / "runtime")
    monkeypatch.setattr(br, "_run", fake)
    return fake


@pytest.mark.parametrize(
    "body",
    [
        "yt-dlp==2026.9.1\n",
        f"yt-dlp>=2026.9.1 {HASH}\n",
        f"yt-dlp==2026.9.1 {HASH}\ncertifi==2026.7.22\n",
        f"--extra-index-url https://evil.invalid/simple\nyt-dlp==2026.9.1 {HASH}\n",
        f"-i https://evil.invalid/simple\nyt-dlp==2026.9.1 {HASH}\n",
        f"yt-dlp @ https://evil.invalid/yt.whl {HASH}\n",
        f"-e ./local\nyt-dlp==2026.9.1 {HASH}\n",
        "yt-dlp==2026.9.1 --hash=sha256:abc\n",
        "# nothing but a comment\n",
    ],
)
def test_update_refuses_anything_not_exactly_hash_pinned(br, runtime, tmp_path, body):
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path, body=body))
    assert runtime.calls == []  # refused before any subprocess, so before any download
    assert not (runtime.root / "active.json").exists()


def test_hash_check_accepts_continuations_comments_and_markers(br, tmp_path):
    body = (
        "# header\n"
        f"yt-dlp[default]==2026.9.1 \\\n    {HASH} \\\n    {HASH}\n"
        "    # via -r ytdlp.in\n"
        f'colorama==0.4.6 ; sys_platform == "win32" \\\n    {HASH}\n'
    )
    br._check_hashes(_reqs(tmp_path, body=body))


def test_update_activates_only_after_imports_and_self_test(br, runtime, tmp_path):
    _fake_env(runtime.root, "ytdlp", "old")
    br.save_active(runtime.root, {"ytdlp": {"active": "old", "previous": None}})
    env_id = br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    assert env_id.startswith("2026.9.1-")
    assert br.load_active(runtime.root)["ytdlp"] == {"active": env_id, "previous": "old"}
    # Validated before the switch, then once more through the new pointer after it.
    assert [_kind(c) for c in runtime.calls] == ["venv", "pip", "check", "self-test", "self-test"]
    assert runtime.calls[3][1:] == ["-s", "-u", "-m", "stuff_downloader_worker", "--self-test"]
    assert runtime.envs[3]["PYTHONPATH"] == str(runtime.root / "app")
    assert "--require-hashes" in runtime.calls[1]
    assert (runtime.root / "envs" / "ytdlp" / "old").is_dir()  # the previous env is kept


@pytest.mark.parametrize("stage", ["pip", "check", "self-test"])
def test_failed_update_leaves_pointer_and_active_env_untouched(br, runtime, tmp_path, stage):
    _fake_env(runtime.root, "ytdlp", "old")
    br.save_active(runtime.root, {"ytdlp": {"active": "old", "previous": "older"}})
    before = (runtime.root / "active.json").read_bytes()
    reqs = _reqs(tmp_path)
    env_id = f"2026.9.1-{br.sha256_file(reqs)[:8]}"
    runtime.fail[stage].add(env_id)
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", reqs)
    assert (runtime.root / "active.json").read_bytes() == before
    assert not (runtime.root / "envs" / "ytdlp" / env_id).exists()
    assert (runtime.root / "envs" / "ytdlp" / "old").is_dir()


def test_failure_after_the_switch_restores_the_pointer(br, runtime, tmp_path):
    _fake_env(runtime.root, "ytdlp", "old")
    br.save_active(runtime.root, {"ytdlp": {"active": "old", "previous": None}, "spotdl": {}})
    before = (runtime.root / "active.json").read_bytes()
    runtime.fail_self_test_after_switch = True
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    assert (runtime.root / "active.json").read_bytes() == before
    assert [p.name for p in (runtime.root / "envs" / "ytdlp").iterdir()] == ["old"]


def test_failed_first_install_leaves_no_pointer(br, runtime, tmp_path):
    runtime.fail_self_test_after_switch = True
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    assert not (runtime.root / "active.json").exists()


def test_failed_pointer_write_keeps_the_old_pointer(br, runtime, tmp_path, monkeypatch):
    _fake_env(runtime.root, "ytdlp", "old")
    br.save_active(runtime.root, {"ytdlp": {"active": "old", "previous": None}})
    before = (runtime.root / "active.json").read_bytes()

    def boom(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    monkeypatch.undo()
    assert (runtime.root / "active.json").read_bytes() == before
    assert not (runtime.root / "active.json.tmp").exists()


def test_reinstalling_the_active_env_is_a_checked_no_op(br, runtime, tmp_path):
    reqs = _reqs(tmp_path)
    env_id = br.install_engine(runtime.root, "ytdlp", reqs)
    runtime.calls.clear()
    assert br.install_engine(runtime.root, "ytdlp", reqs) == env_id
    assert [_kind(c) for c in runtime.calls] == ["check", "self-test"]


@pytest.mark.parametrize("stage", ["pip", "self-test"])
def test_failed_rebuild_of_a_broken_active_env_keeps_it_in_place(br, runtime, tmp_path, stage):
    reqs = _reqs(tmp_path)
    env_id = br.install_engine(runtime.root, "ytdlp", reqs)
    env = runtime.root / "envs" / "ytdlp" / env_id
    (env / "original").write_text("", encoding="utf-8")
    before = (runtime.root / "active.json").read_bytes()
    # The active env is damaged, and its same-id replacement then fails too.
    runtime.fail[stage].add(env_id)
    if stage == "pip":
        runtime.fail["check"].add(env_id)
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", reqs)
    assert (runtime.root / "active.json").read_bytes() == before
    assert (env / "original").is_file()  # the very env active.json names, not a rebuilt one
    assert [p.name for p in env.parent.iterdir()] == [env_id]


def test_successful_rebuild_of_a_broken_active_env_drops_the_old_copy(
    br, runtime, tmp_path, monkeypatch
):
    reqs = _reqs(tmp_path)
    env_id = br.install_engine(runtime.root, "ytdlp", reqs)
    env = runtime.root / "envs" / "ytdlp" / env_id
    (env / "original").write_text("", encoding="utf-8")
    real_validate = br.validate_env

    def damaged_original(root, engine, env_dir):
        if (env_dir / "original").exists():
            raise br.RuntimeBuildError("damaged")
        return real_validate(root, engine, env_dir)

    monkeypatch.setattr(br, "validate_env", damaged_original)
    assert br.install_engine(runtime.root, "ytdlp", reqs) == env_id
    assert not (env / "original").exists()  # rebuilt
    assert [p.name for p in env.parent.iterdir()] == [env_id]  # no .bak left behind


def test_an_env_left_aside_by_an_interrupted_rebuild_is_put_back(br, runtime, tmp_path):
    reqs = _reqs(tmp_path)
    env_id = br.install_engine(runtime.root, "ytdlp", reqs)
    env = runtime.root / "envs" / "ytdlp" / env_id
    env.rename(env.with_name(env_id + ".bak"))
    runtime.calls.clear()
    assert br.install_engine(runtime.root, "ytdlp", reqs) == env_id
    assert [_kind(c) for c in runtime.calls] == ["check", "self-test"]  # restored, not rebuilt
    assert [p.name for p in env.parent.iterdir()] == [env_id]


def test_broken_env_rebuild_removes_a_stale_backup_file(br, runtime, tmp_path, monkeypatch):
    reqs = _reqs(tmp_path)
    env_id = br.install_engine(runtime.root, "ytdlp", reqs)
    env = runtime.root / "envs" / "ytdlp" / env_id
    (env / "damaged").write_text("", encoding="utf-8")
    backup = env.with_name(env_id + ".bak")
    backup.write_text("stale collision", encoding="utf-8")
    real_validate = br.validate_env

    def damaged_original(root, engine, env_dir):
        if (env_dir / "damaged").exists():
            raise br.RuntimeBuildError("damaged")
        return real_validate(root, engine, env_dir)

    monkeypatch.setattr(br, "validate_env", damaged_original)
    assert br.install_engine(runtime.root, "ytdlp", reqs) == env_id
    assert env.is_dir() and not backup.exists()


def test_update_to_a_healthy_existing_env_switches_without_reinstalling(br, runtime, tmp_path):
    new_reqs = _reqs(tmp_path, "2026.9.1")
    new_id = br.install_engine(runtime.root, "ytdlp", new_reqs)
    old_id = br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path, "2026.8.19"))
    runtime.calls.clear()
    assert br.install_engine(runtime.root, "ytdlp", new_reqs) == new_id
    assert not any(_kind(c) in ("venv", "pip") for c in runtime.calls)
    assert br.load_active(runtime.root)["ytdlp"] == {"active": new_id, "previous": old_id}


def test_install_requires_the_worker_in_the_runtime(br, runtime, tmp_path):
    (runtime.root / "app" / "stuff_downloader_worker" / "__main__.py").unlink()
    with pytest.raises(br.RuntimeBuildError, match="worker"):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    assert runtime.calls == []


def test_unreadable_pointer_is_refused_not_overwritten(br, runtime, tmp_path):
    (runtime.root / "active.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(br.RuntimeBuildError):
        br.install_engine(runtime.root, "ytdlp", _reqs(tmp_path))
    assert (runtime.root / "active.json").read_text(encoding="utf-8") == "{not json"


def test_rollback_swaps_active_and_previous(br, runtime):
    _fake_env(runtime.root, "ytdlp", "old")
    _fake_env(runtime.root, "ytdlp", "new")
    br.save_active(runtime.root, {"ytdlp": {"active": "new", "previous": "old"}})
    assert br.rollback_engine(runtime.root, "ytdlp") == "old"
    assert br.load_active(runtime.root)["ytdlp"] == {"active": "old", "previous": "new"}
    assert br.rollback_engine(runtime.root, "ytdlp") == "new"


def test_rollback_refuses_a_previous_env_that_fails_its_self_test(br, runtime):
    _fake_env(runtime.root, "ytdlp", "old")
    _fake_env(runtime.root, "ytdlp", "new")
    br.save_active(runtime.root, {"ytdlp": {"active": "new", "previous": "old"}})
    before = (runtime.root / "active.json").read_bytes()
    runtime.fail["self-test"].add("old")
    with pytest.raises(br.RuntimeBuildError):
        br.rollback_engine(runtime.root, "ytdlp")
    assert (runtime.root / "active.json").read_bytes() == before


def test_rollback_without_previous_env_fails(br, tmp_path):
    br.save_active(tmp_path, {"ytdlp": {"active": "new", "previous": "gone"}})
    with pytest.raises(br.RuntimeBuildError):
        br.rollback_engine(tmp_path, "ytdlp")


def test_install_worker_recovers_a_backup_left_by_an_interrupted_swap(br, tmp_path, monkeypatch):
    # A crash between "dest -> .bak" and ".new -> dest" leaves only the backup. The next install
    # must not delete that last working copy before its own swap has succeeded.
    source = tmp_path / "src" / "stuff_downloader_worker"
    source.mkdir(parents=True)
    (source / "__main__.py").write_text("new", encoding="utf-8")
    monkeypatch.setattr(br, "PROJECT", tmp_path)
    dest = tmp_path / "runtime" / "app" / "stuff_downloader_worker"
    backup = dest.with_name(dest.name + ".bak")
    backup.mkdir(parents=True)
    (backup / "__main__.py").write_text("old", encoding="utf-8")
    real_rename = br._rename_dir

    def fail_new(source_path, target_path):
        if source_path.name.endswith(".new"):
            raise OSError(183, "already exists")
        real_rename(source_path, target_path)

    monkeypatch.setattr(br, "_rename_dir", fail_new)
    with pytest.raises(OSError):
        br.install_worker(tmp_path / "runtime")
    assert (dest / "__main__.py").read_text(encoding="utf-8") == "old"
