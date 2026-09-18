"""External tool discovery and health (ffmpeg, ffprobe, deno). No Qt imports.

Discovery order: configured path -> app tools dir -> PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TOOLS = ("ffmpeg", "ffprobe", "deno")

_VERSION_ARGS = {"ffmpeg": ["-version"], "ffprobe": ["-version"], "deno": ["--version"]}


@dataclass(frozen=True)
class ToolStatus:
    name: str
    path: str | None
    version: str | None
    source: str  # "configured" | "app" | "path" | "missing"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error is None


def app_tools_dir() -> Path:
    """Where the app's own ffmpeg/ffprobe/deno live.

    Frozen, there are two candidates and the order matters. PyInstaller 6 collects bundled data
    under ``_internal`` (``sys._MEIPASS``), which is where a normal build puts them. But a folder
    the owner creates next to the exe wins, so a broken or outdated bundled ffmpeg can be replaced
    without rebuilding the app. Checking only the exe's own directory is what made a build whose
    tools were bundled correctly still report all three "not found".
    """
    if getattr(sys, "frozen", False):
        beside_exe = Path(sys.executable).parent / "tools"
        if beside_exe.is_dir():
            return beside_exe
        bundled = getattr(sys, "_MEIPASS", None)
        return Path(bundled) / "tools" if bundled else beside_exe
    return Path(__file__).resolve().parents[3] / "tools"


def _exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_tool(
    name: str, configured: str | None = None, tools_dir: Path | None = None
) -> tuple[str | None, str]:
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p), "configured"
    tools_dir = tools_dir if tools_dir is not None else app_tools_dir()
    for candidate in (tools_dir / _exe_name(name), tools_dir / name / "bin" / _exe_name(name)):
        if candidate.is_file():
            return str(candidate), "app"
    found = shutil.which(name)
    if found:
        return found, "path"
    return None, "missing"


def _read_version(name: str, path: str, timeout: float) -> str:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [path, *_VERSION_ARGS.get(name, ["--version"])],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
    )
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return first[0] if first else "unknown"


def check_tool(
    name: str,
    configured: str | None = None,
    tools_dir: Path | None = None,
    timeout: float = 5.0,
) -> ToolStatus:
    path, source = find_tool(name, configured, tools_dir)
    if path is None:
        return ToolStatus(name, None, None, source, "not found")
    try:
        return ToolStatus(name, path, _read_version(name, path, timeout), source)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(name, path, None, source, str(exc))


def check_all(configured: dict[str, str] | None = None) -> list[ToolStatus]:
    configured = configured or {}
    return [check_tool(name, configured.get(name)) for name in TOOLS]
