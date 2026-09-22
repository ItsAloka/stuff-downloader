"""Build and manage the app-managed engine runtime (plan §8.1, Option A). Stdlib only.

Layout under ROOT (default ``%LOCALAPPDATA%\\StuffDownloader\\runtime``)::

    python\\python.exe             CPython 3.11 (python-build-standalone, SHA-256 pinned)
    app\\stuff_downloader_worker\\  worker package, put on PYTHONPATH by core/runner.py
    envs\\<engine>\\<env_id>\\       one venv per engine version, installed with --require-hashes
    active.json                   {"<engine>": {"active": env_id, "previous": env_id | null}}

Commands (REQS = packaging/engine-requirements)::

    python packaging/build_runtime.py [--root DIR] base
    python packaging/build_runtime.py [--root DIR] install ytdlp REQS/ytdlp.txt
    python packaging/build_runtime.py [--root DIR] rollback ytdlp
    python packaging/build_runtime.py [--root DIR] status

``install`` is also the engine update: it accepts only exact, SHA-256-pinned requirements, builds
a new versioned env, checks that the engine imports in it and that the worker's ``--self-test``
passes there, and only then flips ``active.json`` atomically, keeping the old env as
``previous``. The switch is verified through the new pointer. If anything fails, the new env is
deleted and ``active.json`` is restored byte for byte, including "there was none". ``rollback``
self-tests the previous env before switching back to it. Every subprocess gets an argument list;
nothing goes through a shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

PYTHON_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/"
    "cpython-3.11.16%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
)
PYTHON_SHA256 = "6be524fa6752af802146a4adc7d098565425b0b1c166e19a5a7a4c8cccb86bf6"
PYTHON_VERSION = "3.11.16"

# Modules that must import for an env to be activated, and the distribution to name it by.
ENGINES: dict[str, dict[str, object]] = {
    "ytdlp": {"imports": ["yt_dlp", "curl_cffi", "yt_dlp_ejs"], "dist": "yt-dlp"},
    "spotdl": {"imports": ["spotdl"], "dist": "spotdl"},
    # GPLv2-only (plan §8.3): lives only in its own env, run as a separate worker process.
    "gallerydl": {"imports": ["gallery_dl", "requests"], "dist": "gallery-dl"},
}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeBuildError(RuntimeError):
    pass


def _retry_fs(action, *, attempts: int = 5) -> None:
    """Retry a short-lived Windows filesystem operation (AV/indexer handles are common)."""
    for attempt in range(attempts):
        try:
            action()
            return
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _remove_path(path: Path) -> None:
    """Remove a stale file or directory, tolerating a freshly released Windows handle."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        _retry_fs(lambda: shutil.rmtree(path))
    else:
        _retry_fs(path.unlink)


def _rename_dir(source: Path, target: Path) -> None:
    _retry_fs(lambda: source.rename(target))


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StuffDownloader" / "runtime"


def _run(
    args: list[str], extra_env: dict[str, str] | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    # Drop anything that points a child interpreter at the Python running this script. On Windows
    # a venv launcher sets __PYVENV_LAUNCHER__, which makes `python -m venv` silently base the new
    # env on the caller's interpreter instead of the runtime's. Only extra_env may set them again.
    blocked = {"PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV", "PYTHONSTARTUP"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in blocked}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.update(extra_env or {})
    proc = subprocess.run(
        args, capture_output=True, text=True, env=env, creationflags=NO_WINDOW, **kwargs
    )
    if proc.returncode != 0:
        raise RuntimeBuildError(
            f"command failed ({proc.returncode}): {args[0]} {' '.join(args[1:4])} ...\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
    return proc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(url: str, expected_sha256: str, dest: Path) -> Path:
    if not url.startswith("https://"):
        raise RuntimeBuildError(f"refusing non-HTTPS download: {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)
    actual = sha256_file(tmp)
    if actual != expected_sha256.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeBuildError(f"SHA-256 mismatch for {url}: {actual} != {expected_sha256}")
    tmp.replace(dest)
    return dest


def _safe_extract(archive: Path, target: Path) -> None:
    target = target.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            dest = (target / member.name).resolve()
            if not dest.is_relative_to(target) or member.issym() or member.islnk():
                raise RuntimeBuildError(f"unsafe archive member: {member.name}")
        tar.extractall(target, filter="data")


def base_python(root: Path) -> Path:
    return root / "python" / "python.exe"


def build_base(root: Path) -> Path:
    python = base_python(root)
    if python.is_file():
        version = _run([str(python), "-c", "import platform; print(platform.python_version())"])
        if version.stdout.strip() == PYTHON_VERSION:
            install_worker(root)
            return python
        raise RuntimeBuildError(f"{python} is not CPython {PYTHON_VERSION}; remove it first")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        archive = download_verified(PYTHON_URL, PYTHON_SHA256, Path(tmp) / "python.tar.gz")
        _safe_extract(archive, Path(tmp) / "x")
        shutil.move(str(Path(tmp) / "x" / "python"), str(root / "python"))
    install_worker(root)
    return python


def install_worker(root: Path) -> Path:
    """Stage and swap the worker package without destroying the working copy first."""
    src = PROJECT / "src" / "stuff_downloader_worker"
    dest = root / "app" / "stuff_downloader_worker"
    staged = dest.with_name(dest.name + ".new")
    backup = dest.with_name(dest.name + ".bak")
    if not dest.exists() and not dest.is_symlink() and (backup / "__main__.py").is_file():
        # An interrupted swap left the working copy only in the backup: put it back first,
        # so the stale-backup cleanup below never deletes the last copy.
        _rename_dir(backup, dest)
    _remove_path(staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not (staged / "__main__.py").is_file():
        _remove_path(staged)
        raise RuntimeBuildError("staged worker package is incomplete")
    _remove_path(backup)
    moved_aside = False
    try:
        if dest.exists() or dest.is_symlink():
            _rename_dir(dest, backup)
            moved_aside = True
        _rename_dir(staged, dest)
    except BaseException:
        _remove_path(staged)
        if moved_aside and not dest.exists():
            _rename_dir(backup, dest)
        raise
    if moved_aside:
        _remove_path(backup)
    return dest


def env_python(env_dir: Path) -> Path:
    return env_dir / "Scripts" / "python.exe"


def _pinned_version(requirements: Path, dist: str) -> str:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        name, sep, version = line.strip().partition("==")
        if sep and name.split("[")[0].lower() == dist:
            return version.split()[0].rstrip("\\").strip()
    raise RuntimeBuildError(f"{requirements} does not pin {dist}")


# One logical requirement line: an exact pin, an optional simple marker, and one or more SHA-256
# hashes. Nothing else is accepted: no pip options (-i, --extra-index-url, -f, -e, -r, -c), no
# URLs or paths, no ranges. pip's own --require-hashes is the second line of defence, not the first.
_PINNED_REQ = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9._+!]+"
    r"(?:\s*;\s*[A-Za-z0-9_.'\"<>=!~ ()-]+?)?"
    r"(?:\s+--hash=sha256:[0-9a-f]{64})+"
)


def _logical_lines(text: str) -> list[str]:
    """Requirement lines with backslash continuations joined and comments removed."""
    lines, pending = [], ""
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        lines.append((pending + line).strip())
        pending = ""
    if pending.strip():
        lines.append(pending.strip())
    return [line for line in lines if line]


def _check_hashes(requirements: Path) -> None:
    """Every requirement must be an exact pin carrying its own SHA-256 hashes."""
    lines = _logical_lines(requirements.read_text(encoding="utf-8"))
    if not lines:
        raise RuntimeBuildError(f"{requirements} pins nothing")
    for line in lines:
        if not _PINNED_REQ.fullmatch(line):
            raise RuntimeBuildError(
                f"{requirements} is not fully hash-pinned: {line.split()[0][:80]!r}"
            )


def load_active(root: Path) -> dict[str, dict[str, str | None]]:
    path = root / "active.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Refuse rather than overwrite a pointer we cannot read: the owner decides what it was.
        raise RuntimeBuildError(f"{path} is unreadable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def save_active(root: Path, data: dict[str, dict[str, str | None]]) -> None:
    path = root / "active.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)  # atomic on the same volume
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _snapshot_pointer(root: Path) -> bytes | None:
    try:
        return (root / "active.json").read_bytes()
    except FileNotFoundError:
        return None


def _restore_pointer(root: Path, snapshot: bytes | None) -> None:
    """Put active.json back exactly as it was, or remove it if there was none."""
    path = root / "active.json"
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    if _snapshot_pointer(root) == snapshot:
        return  # the failed switch never landed
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_bytes(snapshot)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def check_env(root: Path, engine: str, env_dir: Path) -> dict[str, str]:
    """Import the engine's modules inside the env, isolated from the user's site and PYTHONPATH."""
    modules = ENGINES[engine]["imports"]
    code = (
        "import importlib, json, sys\n"
        "from importlib import metadata\n"
        f"mods = {modules!r}\n"
        "for m in mods: importlib.import_module(m)\n"
        "names = {'yt_dlp': 'yt-dlp', 'curl_cffi': 'curl_cffi', 'yt_dlp_ejs': 'yt-dlp-ejs',"
        " 'spotdl': 'spotdl', 'gallery_dl': 'gallery-dl', 'requests': 'requests'}\n"
        "found = {m: metadata.version(names[m]) for m in mods}\n"
        "print(json.dumps(found | {'prefix': sys.prefix}))\n"
    )
    proc = _run([str(env_python(env_dir)), "-s", "-c", code])
    try:
        found = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeBuildError(f"env {env_dir.name} gave no import report") from exc
    if not isinstance(found, dict):
        raise RuntimeBuildError(f"env {env_dir.name} gave no import report")
    return found


def _verify_env_base(root: Path, env_dir: Path) -> None:
    """The env must be built on the runtime's CPython, never the interpreter running this script."""
    cfg = (env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(
        (ln.split("=", 1)[1].strip() for ln in cfg.splitlines() if ln.startswith("home")), ""
    )
    if not home or Path(home).resolve() != (root / "python").resolve():
        raise RuntimeBuildError(f"env {env_dir.name} is based on {home!r}, not the runtime python")


def worker_dir(root: Path) -> Path:
    return root / "app" / "stuff_downloader_worker"


def self_test_env(root: Path, env_dir: Path) -> None:
    """Run the worker's own --self-test in the env, exactly as core/runner.py launches it."""
    if not (worker_dir(root) / "__main__.py").is_file():
        raise RuntimeBuildError("worker package missing from the runtime; run 'base' first")
    proc = _run(
        [str(env_python(env_dir)), "-s", "-u", "-m", "stuff_downloader_worker", "--self-test"],
        extra_env={"PYTHONPATH": str(root / "app"), "PYTHONIOENCODING": "utf-8"},
    )
    if "worker self-test: OK" not in proc.stdout:
        raise RuntimeBuildError(f"worker self-test did not pass in {env_dir.name}")


def validate_env(root: Path, engine: str, env_dir: Path) -> None:
    """Everything an env must pass before it may become (or stay) active."""
    if not env_python(env_dir).is_file():
        raise RuntimeBuildError(f"env {env_dir.name} has no interpreter")
    _verify_env_base(root, env_dir)
    check_env(root, engine, env_dir)
    self_test_env(root, env_dir)


def _activate(root: Path, engine: str, entry: dict[str, str | None]) -> None:
    """Switch the pointer, then prove the switch through it; on any failure put it back."""
    snapshot = _snapshot_pointer(root)
    try:
        active = load_active(root)
        active[engine] = entry
        save_active(root, active)
        now = load_active(root).get(engine) or {}
        if now.get("active") != entry["active"]:
            raise RuntimeBuildError(f"active.json did not switch {engine} to {entry['active']}")
        self_test_env(root, root / "envs" / engine / str(now["active"]))
    except BaseException as exc:
        try:
            _restore_pointer(root, snapshot)
        except OSError as restore_error:
            # Keep the original failure; say plainly that the pointer may now be wrong.
            exc.add_note(f"could not restore {root / 'active.json'}: {restore_error}")
        raise


def install_engine(root: Path, engine: str, requirements: Path) -> str:
    """Install (or update to) the env these requirements describe, and make it active."""
    if engine not in ENGINES:
        raise RuntimeBuildError(f"unknown engine {engine!r}")
    _check_hashes(requirements)
    python = base_python(root)
    if not python.is_file():
        raise RuntimeBuildError("base runtime missing; run the 'base' command first")
    if not (worker_dir(root) / "__main__.py").is_file():
        raise RuntimeBuildError("worker package missing from the runtime; run 'base' first")
    version = _pinned_version(requirements, str(ENGINES[engine]["dist"]))
    env_id = f"{version}-{sha256_file(requirements)[:8]}"
    env_dir = root / "envs" / engine / env_id

    current = load_active(root).get(engine) or {}
    was_active = current.get("active") == env_id
    # A broken env is moved aside, not deleted, until its replacement has passed: active.json may
    # still name it, and a failed rebuild must leave that env where the pointer expects it.
    # (A venv hard-codes its own path, so the rebuild cannot happen in a staging dir.)
    backup = env_dir.with_name(env_id + ".bak")
    if backup.exists():
        if env_dir.exists():
            _remove_path(backup)  # stale leftover from an interrupted earlier run
        elif not backup.is_dir():
            _remove_path(backup)  # corrupt stale target; it cannot be a restorable venv
        else:
            _rename_dir(backup, env_dir)  # interrupted rebuild: put the original back
    moved_aside = False
    if env_dir.exists():
        try:
            validate_env(root, engine, env_dir)
        except RuntimeBuildError:
            _remove_path(backup)
            _rename_dir(env_dir, backup)  # keep broken original until replacement succeeds
            moved_aside = True
        else:
            if was_active:
                return env_id
            # Already built and healthy (for example the previous env): switch to it, no reinstall.
            _activate(root, engine, {"active": env_id, "previous": current.get("active")})
            return env_id
    try:
        _run([str(python), "-m", "venv", str(env_dir)])
        _verify_env_base(root, env_dir)
        _run(
            [
                str(env_python(env_dir)), "-s", "-m", "pip", "install",
                "--require-hashes", "--no-deps", "--only-binary", ":all:",
                "--no-input", "--no-cache-dir", "-r", str(requirements),
            ]
        )  # fmt: skip
        validate_env(root, engine, env_dir)
        previous = current.get("previous") if was_active else current.get("active")
        _activate(root, engine, {"active": env_id, "previous": previous})
    except BaseException as exc:
        shutil.rmtree(env_dir, ignore_errors=True)
        if moved_aside:
            try:
                _rename_dir(backup, env_dir)
            except OSError as restore_error:
                exc.add_note(f"could not restore {env_dir} from {backup}: {restore_error}")
        raise
    if moved_aside:
        _remove_path(backup)
    return env_id


def rollback_engine(root: Path, engine: str) -> str:
    """Make the previous env active again, but only once it has passed its checks."""
    entry = load_active(root).get(engine) or {}
    previous = entry.get("previous")
    if not previous or not env_python(root / "envs" / engine / previous).is_file():
        raise RuntimeBuildError(f"no previous {engine} env to roll back to")
    validate_env(root, engine, root / "envs" / engine / previous)
    _activate(root, engine, {"active": previous, "previous": entry.get("active")})
    return previous


def status(root: Path) -> dict[str, object]:
    active = load_active(root)
    envs = {
        engine: sorted(p.name for p in (root / "envs" / engine).iterdir() if p.is_dir())
        for engine in ENGINES
        if (root / "envs" / engine).is_dir()
    }
    return {"root": str(root), "base": base_python(root).is_file(), "active": active, "envs": envs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_runtime")
    parser.add_argument("--root", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("base")
    inst = sub.add_parser("install")
    inst.add_argument("engine", choices=sorted(ENGINES))
    inst.add_argument("requirements", type=Path)
    rb = sub.add_parser("rollback")
    rb.add_argument("engine", choices=sorted(ENGINES))
    sub.add_parser("status")
    args = parser.parse_args(argv)
    root = (args.root or default_root()).resolve()

    try:
        if args.command == "base":
            print(build_base(root))
        elif args.command == "install":
            print(install_engine(root, args.engine, args.requirements.resolve()))
        elif args.command == "rollback":
            print(rollback_engine(root, args.engine))
        else:
            print(json.dumps(status(root), indent=2))
    except (RuntimeBuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
