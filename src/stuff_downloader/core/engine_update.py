"""Install library updates into the engine runtime, undo them, and watch new envs (R8). No Qt.

An update never touches the running env. For one engine it:

1. pins every top-level library of ``<engine>.in`` (keeping its extras): the chosen ones to the
   new version, the rest to what the active env has now;
2. asks the runtime's own pip to resolve that (``pip install --dry-run --report``, wheels only)
   and turns the report into an exact, SHA-256-pinned snapshot: each file must come over HTTPS
   from files.pythonhosted.org;
3. hands the snapshot to ``build_runtime.py install``, run by the runtime python. That builds a
   new versioned env with ``--require-hashes``, checks imports and the worker ``--self-test``,
   and only then switches ``active.json``, keeping the old env as ``previous``. On any failure
   it deletes the new env and restores ``active.json`` byte for byte;
4. deletes envs older than ``previous``.

Jobs already running keep the interpreter they were started with: ``core.runner`` picks the env
when a job starts, and the env it was using stays on disk as ``previous``.

``undo`` is ``build_runtime.py rollback``, which self-tests the previous env before switching.
``Watchdog`` rolls back automatically when the first three jobs on a freshly updated env all
fail to start their engine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from . import runner, updates

log = logging.getLogger(__name__)

WHEEL_HOST = "files.pythonhosted.org"
RESOLVE_TIMEOUT = 5 * 60
INSTALL_TIMEOUT = 20 * 60
ROLLBACK_TIMEOUT = 5 * 60
ERROR_TAIL = 800  # characters of a failed command's output shown to the owner
WATCH_FILE = "update_watch.json"
WATCH_JOBS = 3

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BLOCKED_ENV = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}
_REQ = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[A-Za-z0-9._,\s-]+\])?")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCK = threading.Lock()  # one update or undo at a time

Progress = Callable[[str, str], None]  # (stage, message); stages: resolve, install, prune, done
Runner = Callable[[list[str], int], str]  # (args, timeout) -> stdout; raises UpdateError


class UpdateError(RuntimeError):
    """An update or undo failed. The message is short and safe to show."""


# ── plumbing ──────────────────────────────────────────────────────────────────────────────────
def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= ERROR_TAIL else "…" + text[-ERROR_TAIL:]


def run_command(args: list[str], timeout: int) -> str:
    """Run one command without a shell, with a clean interpreter environment."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _BLOCKED_ENV}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"{Path(args[0]).name} took longer than {timeout} s") from exc
    except OSError as exc:
        raise UpdateError(f"could not start {Path(args[0]).name}: {exc}") from exc
    if proc.returncode != 0:
        raise UpdateError(_tail(proc.stderr or proc.stdout) or f"exit code {proc.returncode}")
    return proc.stdout


def build_runtime_script() -> Path | None:
    """build_runtime.py: bundled with the frozen app, or in the source checkout."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "runtime-tools" / "build_runtime.py")
    candidates.append(Path(__file__).resolve().parents[3] / "packaging" / "build_runtime.py")
    return next((path for path in candidates if path.is_file()), None)


def _runtime_python(root: Path) -> Path:
    python = root / "python" / "python.exe"
    if not python.is_file():
        raise UpdateError("the engine runtime is not installed")
    return python


def _script() -> Path:
    script = build_runtime_script()
    if script is None:
        raise UpdateError("the runtime updater is missing from this app build")
    return script


# ── step 1: top-level pins ────────────────────────────────────────────────────────────────────
def top_level_pins(
    engine: str,
    selections: Mapping[str, str],
    installed: Mapping[str, str],
    req_dir: Path | None = None,
) -> list[str]:
    """``name[extras]==version`` for every top-level library of ``engine``."""
    req_dir = req_dir or updates.requirements_dir()
    if engine not in updates.ENGINES or req_dir is None:
        raise UpdateError(f"unknown engine {engine!r}")
    try:
        text = (req_dir / f"{engine}.in").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UpdateError(f"cannot read the {engine} library list") from exc
    chosen = {updates.normalize(name): version for name, version in selections.items()}
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = _REQ.match(line) if line and not line.startswith("-") else None
        if not match:
            continue
        name = updates.normalize(match.group(1))
        extras = re.sub(r"\s+", "", match.group(2) or "")
        version = chosen.get(name) or installed.get(name)
        if version is None:
            raise UpdateError(f"{name} is not installed in the {engine} engine")
        if updates.parse_final(version) is None:
            raise UpdateError(f"{name} {version} is not a stable release")
        pins.setdefault(name, f"{name}{extras}=={version}")
    unknown = sorted(set(chosen) - set(pins))
    if unknown:
        raise UpdateError(f"not a library of the {engine} engine: {', '.join(unknown)}")
    if not pins:
        raise UpdateError(f"the {engine} library list is empty")
    return list(pins.values())


# ── step 2: resolve to a hash-pinned snapshot ─────────────────────────────────────────────────
def snapshot_from_report(report: object) -> list[str]:
    """Exact ``name==version --hash=sha256:...`` lines from a pip installation report."""
    items = report.get("install") if isinstance(report, dict) else None
    if not isinstance(items, list) or not items:
        raise UpdateError("pip resolved nothing")
    lines: dict[str, str] = {}
    for item in items:
        try:
            meta, info = item["metadata"], item["download_info"]
            name, version, url = meta["name"], meta["version"], info["url"]
            digest = info["archive_info"]["hashes"]["sha256"]
        except (KeyError, TypeError) as exc:
            raise UpdateError("pip's report is missing a download or hash") from exc
        if not all(isinstance(v, str) for v in (name, version, url, digest)):
            raise UpdateError("pip's report is malformed")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != WHEEL_HOST or parts.port is not None:
            raise UpdateError(f"refusing {name}: not downloaded from PyPI over HTTPS")
        if not parts.path.endswith(".whl"):
            raise UpdateError(f"refusing {name}: not a wheel")
        if not _SHA256.fullmatch(digest):
            raise UpdateError(f"refusing {name}: no valid SHA-256")
        if updates.parse_final(version) is None:
            raise UpdateError(f"refusing {name} {version}: not a stable release")
        key = updates.normalize(name)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key):
            raise UpdateError("pip's report names an invalid package")
        lines.setdefault(key, f"{key}=={version} --hash=sha256:{digest}")
    return [lines[key] for key in sorted(lines)]


def resolve(python: Path, pins: list[str], workdir: Path, run: Runner = run_command) -> list[str]:
    report_path = workdir / "report.json"
    run(
        [
            str(python), "-s", "-m", "pip", "install", "--dry-run", "--ignore-installed",
            "--only-binary", ":all:", "--no-input", "--quiet", "--report", str(report_path),
            *pins,
        ],
        RESOLVE_TIMEOUT,
    )  # fmt: skip
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError("pip wrote no readable report") from exc
    return snapshot_from_report(report)


# ── step 4: prune ─────────────────────────────────────────────────────────────────────────────
def prune_envs(root: Path, engine: str) -> list[str]:
    """Delete every env of ``engine`` except the active and previous ones. Never raises."""
    try:
        entry = json.loads((root / "active.json").read_text(encoding="utf-8")).get(engine)
    except (OSError, ValueError, AttributeError):
        return []  # without a readable pointer nothing is provably unused
    if not isinstance(entry, dict) or not entry.get("active"):
        return []
    keep = {entry.get("active"), entry.get("previous")}
    removed = []
    folder = root / "envs" / engine
    try:
        children = list(folder.iterdir())
    except OSError:
        return []
    for path in children:
        name = path.name
        if name in keep or name.endswith(".bak") or not path.is_dir():
            continue  # .bak belongs to build_runtime's own recovery
        if not runner._ENV_ID.fullmatch(name):
            continue
        try:
            shutil.rmtree(path)
            removed.append(name)
        except OSError as exc:  # a file still in use; try again after the next update
            log.info("Could not delete old env %s: %s", path, exc)
    return removed


# ── the update ────────────────────────────────────────────────────────────────────────────────
def update_engine(
    engine: str,
    selections: Mapping[str, str],
    *,
    root: Path | None = None,
    req_dir: Path | None = None,
    run: Runner = run_command,
    progress: Progress | None = None,
) -> str:
    """Install ``selections`` ({library: version}) into a new env for ``engine``; return its id.

    Raises UpdateError with a short message; the active env is then unchanged."""
    root = root or runner.runtime_root()
    say = progress or (lambda stage, message: None)
    if not selections:
        raise UpdateError("nothing selected")
    if not _LOCK.acquire(blocking=False):
        raise UpdateError("another update is already running")
    try:
        before = runner._active_env(root, engine)
        site = updates.env_site_packages(engine, root)
        if before is None or site is None:
            raise UpdateError(f"the {engine} engine is not installed")
        python = _runtime_python(root)
        script = _script()
        pins = top_level_pins(engine, selections, updates.installed_versions(site), req_dir)
        with tempfile.TemporaryDirectory(prefix="sd-update-") as tmp:
            say("resolve", f"Finding the files for {', '.join(pins)}")
            snapshot = resolve(python, pins, Path(tmp), run)
            requirements = Path(tmp) / f"{engine}.txt"
            requirements.write_text("\n".join(snapshot) + "\n", encoding="utf-8")
            say("install", f"Installing and testing a new {engine} engine")
            out = run(
                [str(python), "-s", str(script), "--root", str(root), "install", engine,
                 str(requirements)],
                INSTALL_TIMEOUT,
            )  # fmt: skip
        env_id = (out.strip().splitlines() or [""])[-1].strip()
        if runner._active_env(root, engine) != env_id or not env_id:
            raise UpdateError(f"the {engine} engine did not switch to the new version")
        say("prune", "Removing old engine versions")
        prune_envs(root, engine)
        Watchdog(root).arm(engine, env_id)
        say("done", f"The {engine} engine is updated")
        return env_id
    finally:
        _LOCK.release()


def undo(engine: str, *, root: Path | None = None, run: Runner = run_command) -> str:
    """Switch ``engine`` back to its previous env after self-testing it; return that env id."""
    root = root or runner.runtime_root()
    if engine not in updates.ENGINES:
        raise UpdateError(f"unknown engine {engine!r}")
    if not _LOCK.acquire(blocking=False):
        raise UpdateError("an update is running; try again when it finishes")
    try:
        before = runner._active_env(root, engine)
        out = run(
            [str(_runtime_python(root)), "-s", str(_script()), "--root", str(root), "rollback",
             engine],
            ROLLBACK_TIMEOUT,
        )  # fmt: skip
        env_id = (out.strip().splitlines() or [""])[-1].strip()
        if not env_id or runner._active_env(root, engine) != env_id or env_id == before:
            raise UpdateError(f"the {engine} engine did not switch back")
        Watchdog(root).disarm(engine)
        return env_id
    finally:
        _LOCK.release()


def can_undo(engine: str, root: Path | None = None) -> bool:
    """Whether ``engine`` has a previous env on disk to switch back to."""
    root = root or runner.runtime_root()
    try:
        entry = json.loads((root / "active.json").read_text(encoding="utf-8")).get(engine)
    except (OSError, ValueError, AttributeError):
        return False
    previous = entry.get("previous") if isinstance(entry, dict) else None
    if not isinstance(previous, str) or not runner._ENV_ID.fullmatch(previous):
        return False
    return (root / "envs" / engine / previous / "Scripts" / "python.exe").is_file()


# ── automatic rollback ────────────────────────────────────────────────────────────────────────
class Watchdog:
    """Rolls an engine back when the first three jobs on its new env all fail at engine start.

    State survives restarts in ``<runtime>/update_watch.json``: {engine: {env, failures}}. A
    successful engine start proves the env and ends the watch."""

    def __init__(self, root: Path | None = None, rollback: Callable[[str], str] | None = None):
        self.root = root or runner.runtime_root()
        self._rollback = rollback or (lambda engine: undo(engine, root=self.root))
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.root / WATCH_FILE

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get("env"), str)
            and isinstance(v.get("failures"), int) and not isinstance(v.get("failures"), bool)
        }  # fmt: skip

    def _save(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save %s: %s", self.path, exc)

    def arm(self, engine: str, env_id: str) -> None:
        with self._lock:
            data = self._load()
            data[engine] = {"env": env_id, "failures": 0}
            self._save(data)

    def disarm(self, engine: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(engine, None) is not None:
                self._save(data)

    def watching(self, engine: str) -> str | None:
        entry = self._load().get(engine)
        return entry["env"] if entry else None

    def record(self, engine: str, env_id: str | None, started: bool) -> str | None:
        """One job's engine start on ``engine``'s env ``env_id``. Returns the env rolled back to."""
        with self._lock:
            data = self._load()
            entry = data.get(engine)
            if entry is None or env_id is None or entry["env"] != env_id:
                return None
            if started:
                data.pop(engine)
                self._save(data)
                return None
            entry["failures"] += 1
            if entry["failures"] < WATCH_JOBS:
                self._save(data)
                return None
            data.pop(engine)  # stop watching whether or not the rollback works: no loops
            self._save(data)
        log.warning("First %d jobs on %s env %s failed; rolling back", WATCH_JOBS, engine, env_id)
        try:
            return self._rollback(engine)
        except UpdateError as exc:
            log.warning("Automatic rollback of %s failed: %s", engine, exc)
            return None
