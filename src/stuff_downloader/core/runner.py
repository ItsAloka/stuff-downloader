"""Spawns one worker process per job and streams its JSON-lines events. No Qt imports.

Callbacks run on a background reader thread; the GUI must marshal them to its own thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path

import psutil

from . import tools
from .protocol import Event, JobSpec, ProtocolError

log = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]


class RunState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ENV_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")

RUNTIME_ENV_VAR = "STUFF_DOWNLOADER_RUNTIME"
WORKER_PATH_ENV_VAR = "STUFF_DOWNLOADER_WORKER_PATH"


class WorkerRuntimeMissing(RuntimeError):
    """The frozen app has no engine runtime to run workers in."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_root() -> Path:
    """Root of the app-managed engine runtime (plan §8.1, built by packaging/build_runtime.py)."""
    override = os.environ.get(RUNTIME_ENV_VAR)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StuffDownloader" / "runtime"


def _active_env(root: Path, engine: str) -> str | None:
    try:
        data = json.loads((root / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(engine) if isinstance(data, dict) else None
    env_id = entry.get("active") if isinstance(entry, dict) else None
    if not isinstance(env_id, str) or not _ENV_ID.fullmatch(env_id):
        return None  # reject path tricks such as ".." or "..\\x" in a tampered pointer file
    return env_id


def runtime_python(engine: str = "fake") -> Path:
    """Interpreter for one engine: its active env, else the base runtime. Never the GUI exe."""
    root = runtime_root()
    env_id = _active_env(root, engine)
    if env_id:
        return root / "envs" / engine / env_id / "Scripts" / "python.exe"
    return root / "python" / "python.exe"


def default_worker_command(engine: str = "fake") -> list[str]:
    if not is_frozen():
        return [sys.executable, "-u", "-m", "stuff_downloader_worker"]
    python = runtime_python(engine)
    if not python.is_file():
        raise WorkerRuntimeMissing(f"engine runtime not found: {python}")
    if python.resolve() == Path(sys.executable).resolve():
        raise WorkerRuntimeMissing("engine runtime must not be the GUI executable")
    # -s: ignore the user's site-packages so only the runtime env is importable.
    return [str(python), "-s", "-u", "-m", "stuff_downloader_worker"]


TOOLS_DIR_ENV_VAR = "STUFF_DOWNLOADER_TOOLS_DIR"

# Variables that would point a runtime interpreter at another Python. On Windows a venv launcher
# sets __PYVENV_LAUNCHER__, which can make a child python resolve the wrong base installation.
_INTERPRETER_VARS = {
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
}


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    # Only the runner decides which tools a worker may execute (see worker engines/ytdlp.py).
    env[TOOLS_DIR_ENV_VAR] = str(tools.app_tools_dir())
    if is_frozen():
        # The frozen bundle's own modules are not importable by another interpreter; the worker
        # package lives in the runtime's app dir.
        env = {k: v for k, v in env.items() if k.upper() not in _INTERPRETER_VARS}
        src_dir = env.get(WORKER_PATH_ENV_VAR) or str(runtime_root() / "app")
    else:
        src_dir = str(Path(__file__).resolve().parents[2])
    existing = env.get("PYTHONPATH")
    parts = [p for p in (src_dir, existing) if p]
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def kill_process_tree(pid: int, timeout: float = 3.0) -> None:
    """Terminate a process and all its children; escalate to kill if they linger."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=timeout)


class JobRun:
    """One worker process running one job."""

    def __init__(
        self,
        spec: JobSpec,
        on_event: EventCallback,
        worker_command: Sequence[str] | None = None,
    ) -> None:
        self.spec = spec
        self._on_event = on_event
        self._command = list(worker_command or default_worker_command(spec.engine))
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._terminal_seen = False
        self.state = RunState.PENDING
        self.stderr_tail: list[str] = []

    def start(self) -> None:
        with self._lock:
            if self._proc is not None:
                raise RuntimeError("job already started")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                [*self._command, "--engine", self.spec.engine],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_worker_env(),
                creationflags=creationflags,
            )
            self.state = RunState.RUNNING
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(self.spec.to_json() + "\n")
            self._proc.stdin.close()
        except OSError as exc:  # worker died before reading its spec
            log.warning("could not send job spec to worker: %s", exc)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def cancel(self) -> None:
        with self._lock:
            if self._proc is None or self.state is not RunState.RUNNING:
                return
            self._cancel_requested = True
            pid = self._proc.pid
        kill_process_tree(pid)

    def wait(self, timeout: float | None = None) -> RunState:
        if self._reader is not None:
            self._reader.join(timeout)
        return self.state

    def _emit(self, event: Event) -> None:
        try:
            self._on_event(event)
        except Exception:  # a UI callback bug must not kill the reader thread
            log.exception("event callback failed")

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def _read_events(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        job_id = self.spec.job_id
        for line in self._proc.stdout:
            if not line.strip():
                continue
            try:
                event = Event.from_line(line)
            except ProtocolError as exc:
                log.debug("ignoring malformed worker line: %s", exc)
                continue
            if event.job_id != job_id:
                log.debug("ignoring event for other job %r", event.job_id)
                continue
            if self._terminal_seen:
                continue
            if event.is_terminal:
                with self._lock:
                    self._terminal_seen = True
                    self.state = RunState.COMPLETED if event.type == "result" else RunState.FAILED
            self._emit(event)
        returncode = self._proc.wait()

        with self._lock:
            cancelled = self._cancel_requested and self.state is RunState.RUNNING
            if cancelled:
                self.state = RunState.CANCELLED
            elif self.state is RunState.RUNNING:
                self.state = RunState.FAILED
        if cancelled:
            self._emit(Event("error", job_id, {"code": "cancelled", "message": "Cancelled"}))
        elif not self._terminal_seen:
            detail = self.stderr_tail[-1] if self.stderr_tail else ""
            self._emit(
                Event(
                    "error",
                    job_id,
                    {
                        "code": "worker_exited",
                        "message": f"Worker exited with code {returncode} without a result",
                        "detail": detail,
                    },
                )
            )
