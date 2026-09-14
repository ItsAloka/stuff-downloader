"""Filesystem locations and path validation. No Qt imports."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

APP_DIR_NAME = "StuffDownloader"

# FOLDERID_Downloads
_DOWNLOADS_FOLDER_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _known_downloads_folder() -> Path | None:
    """Ask Windows for the user's (possibly relocated) Downloads folder."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        guid = GUID()
        if ctypes.oledll.ole32.CLSIDFromString(_DOWNLOADS_FOLDER_GUID, ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError):
        return None


def default_download_dir() -> Path:
    """Windows Downloads folder, falling back to ~/Downloads, then home."""
    known = _known_downloads_folder()
    if known is not None and known.is_dir():
        return known
    home = Path.home()
    candidate = home / "Downloads"
    if candidate.is_dir():
        return candidate
    return home


def config_dir() -> Path:
    base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / APP_DIR_NAME


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_DIR_NAME


def temp_dir() -> Path:
    return data_dir() / "temp"


def is_writable_dir(path: Path) -> bool:
    """True if ``path`` is an existing directory we can create a file in."""
    try:
        if not path.is_dir():
            return False
        probe = path / f".sd-write-test-{uuid.uuid4().hex}"
        with open(probe, "xb"):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_download_dir(configured: str | None) -> Path:
    """The configured folder if it is usable, else the default."""
    if configured:
        path = Path(configured).expanduser()
        if is_writable_dir(path):
            return path
    default = default_download_dir()
    if is_writable_dir(default):
        return default
    return Path(tempfile.gettempdir())
