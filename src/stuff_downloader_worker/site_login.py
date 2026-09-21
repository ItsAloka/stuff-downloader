"""The ``site_login`` job option (plan §6.4), re-validated in the worker. Stdlib only.

The runner attaches this only when the owner chose a login for the job's site. It names a
browser profile or a cookies.txt path; the cookies themselves are read by yt-dlp inside this
process and never written anywhere, logged, or echoed back in an event.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .engines.base import EngineError

BROWSERS = frozenset({"firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"})
_PROFILE = re.compile(r"[A-Za-z0-9 ._-]{0,64}")
MAX_COOKIE_FILE_BYTES = 5_000_000

# Fixed text: the underlying exception can name the cookie database or file, so it is dropped.
COOKIES_FAILED = "the site login could not be read"

# A header value that slipped into engine text. The whole rest of the token is removed.
_SECRET = re.compile(
    r"(?i)\b(set-cookie|cookie|authorization|proxy-authorization|x-csrf-token)\b['\"]?\s*[:=]"
    r"\s*[^\r\n]*"
)


def redact_secrets(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}: [removed]", text)


def ydl_options(value: Any) -> dict[str, Any]:
    """yt-dlp options for a validated ``site_login``. Raises bad_options for anything else."""
    if not isinstance(value, dict):
        raise EngineError("bad_options", "'site_login' must be an object")
    source = value.get("source")
    if source == "browser" and set(value) <= {"source", "browser", "profile"}:
        browser, profile = value.get("browser"), value.get("profile", "")
        if browser not in BROWSERS or not isinstance(profile, str):
            raise EngineError("bad_options", "unsupported browser for site login")
        name = profile.strip()
        if not _PROFILE.fullmatch(profile) or (name and set(name) <= {"."}):
            raise EngineError("bad_options", "invalid browser profile name")
        return {"cookiesfrombrowser": (browser, name or None, None, None)}
    if source == "file" and set(value) <= {"source", "path"}:
        path = value.get("path")
        if not isinstance(path, str) or not path or "\0" in path:
            raise EngineError("bad_options", "invalid cookies file")
        file = Path(path)
        if not file.is_absolute() or file.suffix.lower() != ".txt":
            raise EngineError("bad_options", "invalid cookies file")
        try:
            ok = file.is_file() and 0 < file.stat().st_size <= MAX_COOKIE_FILE_BYTES
        except OSError:
            ok = False
        if not ok:
            raise EngineError("cookies_unavailable", COOKIES_FAILED)
        return {"cookiefile": str(file)}
    raise EngineError("bad_options", "invalid site login")


class SilentLogger:
    """Swallows yt-dlp's own console output while a login is in use.

    yt-dlp reports a cookie-load failure by printing the cause, which can include the path of
    the browser's cookie database. Errors still reach us as exceptions; only the printing stops.
    """

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass
