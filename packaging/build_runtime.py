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

``install`` creates a new env, verifies the engine imports in it, then flips ``active.json``,
keeping the old env as ``previous``. A failed install or check deletes the new env and leaves
the active one untouched. Every subprocess gets an argument list; nothing goes through a shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StuffDownloader" / "runtime"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    # Drop anything that points a child interpreter at the Python running this script. On Windows
    # a venv launcher sets __PYVENV_LAUNCHER__, which makes `python -m venv` silently base the new
    # env on the caller's interpreter instead of the runtime's.
    blocked = {"PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV", "PYTHONSTARTUP"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in blocked}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
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
    """Copy the worker package (no GUI code) into the runtime."""
    src = PROJECT / "src" / "stuff_downloader_worker"
    dest = root / "app" / "stuff_downloader_worker"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def env_python(env_dir: Path) -> Path:
    return env_dir / "Scripts" / "python.exe"


def _pinned_version(requirements: Path, dist: str) -> str:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        name, sep, version = line.strip().partition("==")
        if sep and name.split("[")[0].lower() == dist:
            return version.split()[0].rstrip("\\").strip()
    raise RuntimeBuildError(f"{requirements} does not pin {dist}")


def _check_hashes(requirements: Path) -> None:
    text = requirements.read_text(encoding="utf-8")
    pins = [ln for ln in text.splitlines() if ln and not ln[0].isspace() and "==" in ln]
    if not pins or text.count("--hash=sha256:") < len(pins):
        raise RuntimeBuildError(f"{requirements} is not fully hash-pinned")


def load_active(root: Path) -> dict[str, dict[str, str | None]]:
    path = root / "active.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_active(root: Path, data: dict[str, dict[str, str | None]]) -> None:
    path = root / "active.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic on the same volume


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
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _verify_env_base(root: Path, env_dir: Path) -> None:
    """The env must be built on the runtime's CPython, never the interpreter running this script."""
    cfg = (env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(
        (ln.split("=", 1)[1].strip() for ln in cfg.splitlines() if ln.startswith("home")), ""
    )
    if not home or Path(home).resolve() != (root / "python").resolve():
        raise RuntimeBuildError(f"env {env_dir.name} is based on {home!r}, not the runtime python")


def install_engine(root: Path, engine: str, requirements: Path) -> str:
    if engine not in ENGINES:
        raise RuntimeBuildError(f"unknown engine {engine!r}")
    _check_hashes(requirements)
    python = base_python(root)
    if not python.is_file():
        raise RuntimeBuildError("base runtime missing; run the 'base' command first")
    version = _pinned_version(requirements, str(ENGINES[engine]["dist"]))
    env_id = f"{version}-{sha256_file(requirements)[:8]}"
    env_dir = root / "envs" / engine / env_id

    active = load_active(root)
    current = active.get(engine, {})
    if env_dir.exists():
        if current.get("active") == env_id:
            return env_id
        shutil.rmtree(env_dir)
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
        check_env(root, engine, env_dir)
    except Exception:
        shutil.rmtree(env_dir, ignore_errors=True)
        raise
    active[engine] = {"active": env_id, "previous": current.get("active")}
    save_active(root, active)
    return env_id


def rollback_engine(root: Path, engine: str) -> str:
    active = load_active(root)
    entry = active.get(engine) or {}
    previous = entry.get("previous")
    if not previous or not env_python(root / "envs" / engine / previous).is_file():
        raise RuntimeBuildError(f"no previous {engine} env to roll back to")
    active[engine] = {"active": previous, "previous": entry.get("active")}
    save_active(root, active)
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
    except RuntimeBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
