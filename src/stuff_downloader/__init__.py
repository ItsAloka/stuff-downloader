"""Stuff Downloader. ``release_version()`` is the one place the GUI reads its version from."""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

__version__ = "1.0.0"

DIST_NAME = "stuff-downloader"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pyproject_version(pyproject: Path) -> str | None:
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    # Only the [project] table's own key, never a dependency pin or a tool's setting.
    project = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    match = project and re.search(r'^version\s*=\s*"([^"\r\n]{1,40})"\s*$', project.group(1), re.M)
    return match.group(1) if match else None


def release_version() -> str:
    """Installed package metadata first, then the source tree's pyproject, then ``__version__``.

    Never raises: the About dialog must open even from a half-installed or frozen copy.
    """
    try:
        return metadata.version(DIST_NAME)
    except (metadata.PackageNotFoundError, ValueError, OSError):
        pass
    if not getattr(sys, "frozen", False):
        found = _pyproject_version(_source_root() / "pyproject.toml")
        if found:
            return found
    return __version__


def data_root() -> Path:
    """Where LICENSE, THIRD_PARTY_LICENSES.txt and resources\\ live: the bundle, or the repo."""
    if getattr(sys, "frozen", False):
        bundled = getattr(sys, "_MEIPASS", None)
        return Path(bundled) if bundled else Path(sys.executable).parent
    return _source_root()


def resource_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        return data_root() / "resources" / name
    return Path(__file__).resolve().parent / "resources" / name
