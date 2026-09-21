"""Per-site login choices for restricted media (plan §6.4). No Qt imports, no engine imports.

Advanced and off by default. The app works with no entry here, and the GUI only offers one
after a download fails because the site shows the media to signed-in viewers only.

What is stored is the *choice* — a browser name and profile, or the path of a cookies.txt the
owner picked — never cookie contents. The choice is attached to a job at launch time and never
written into the job's options, so it does not reach the queue database or history either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Browsers yt-dlp can read. Firefox first: it is the one that works reliably on Windows,
# because Chromium browsers lock their cookie database and use app-bound encryption.
BROWSERS = ("firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi")
# A profile is a name, never a path: a path here would let a settings file point the engine at
# any directory on disk.
_PROFILE = re.compile(r"[A-Za-z0-9 ._-]{0,64}")
MAX_COOKIE_FILE_BYTES = 5_000_000
_STRIP_PREFIXES = ("www.", "m.", "mobile.")

GUIDANCE = (
    "Only for media the site shows to signed-in viewers. Public links never need this.\n\n"
    "• Firefox works best: sign in to the site in Firefox, then choose Firefox here.\n"
    "• Chrome, Edge and Brave lock and encrypt their cookies on Windows, so reading them often"
    " fails. Close the browser first, or use a cookies.txt file instead.\n"
    "• A cookies.txt file (Netscape format) exported for just this site also works.\n\n"
    "A cookies file is a secret: anyone who has it can act as you on that site. Stuff"
    " Downloader stores only which browser or file you picked, never the cookies themselves."
)


@dataclass(frozen=True)
class SiteLogin:
    source: str  # "browser" | "file"
    browser: str = ""
    profile: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        if self.source == "browser":
            return {"source": "browser", "browser": self.browser, "profile": self.profile}
        return {"source": "file", "path": self.path}

    def describe(self) -> str:
        if self.source == "browser":
            name = self.browser.capitalize()
            return f"{name} ({self.profile})" if self.profile else name
        return f"cookies file {Path(self.path).name}"


def site_key(url_or_host: str) -> str:
    """The site a choice belongs to: lower-case host without www./m./mobile. — or ""."""
    text = url_or_host.strip()
    host = urlsplit(text).hostname if "://" in text else text
    host = (host or "").lower().rstrip(".")
    for prefix in _STRIP_PREFIXES:
        if host.startswith(prefix) and host.count(".") > 1:
            host = host[len(prefix) :]
            break
    if not host or "." not in host or any(c in host for c in "/\\:@ "):
        return ""
    return host


def parse(value: Any) -> SiteLogin | None:
    """A stored or submitted choice, validated. ``None`` for anything malformed."""
    if not isinstance(value, dict):
        return None
    source = value.get("source")
    if source == "browser":
        browser = value.get("browser")
        profile = value.get("profile", "")
        if browser not in BROWSERS or not isinstance(profile, str):
            return None
        name = profile.strip()
        if not _PROFILE.fullmatch(profile) or (name and set(name) <= {"."}):
            return None  # "." and ".." are names that are really paths
        return SiteLogin("browser", browser=browser, profile=name)
    if source == "file":
        path = value.get("path")
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        if not Path(path).is_absolute() or Path(path).suffix.lower() != ".txt":
            return None
        return SiteLogin("file", path=path)
    return None


def load_all(raw: Any) -> dict[str, SiteLogin]:
    """Settings' ``site_logins`` map, keeping only well-formed entries."""
    result: dict[str, SiteLogin] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        site = site_key(key) if isinstance(key, str) else ""
        choice = parse(value)
        if site and choice is not None:
            result[site] = choice
    return result


def dump_all(choices: dict[str, SiteLogin]) -> dict[str, dict[str, str]]:
    return {site: choice.to_dict() for site, choice in choices.items()}


def choice_for(url: str, choices: dict[str, SiteLogin]) -> SiteLogin | None:
    """The owner's choice for ``url``'s site, matching subdomains of a stored site too."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for site, choice in choices.items():
        if host == site or host.endswith("." + site):
            return choice
    return None


def check_file(path: str) -> str:
    """Why a picked cookies file cannot be used, or "" when it can. Reads nothing but its size."""
    file = Path(path)
    if file.suffix.lower() != ".txt":
        return "Pick a cookies.txt file (Netscape format)."
    try:
        size = file.stat().st_size
    except OSError:
        return "That file could not be found."
    if not file.is_file() or size == 0:
        return "That file is empty or not a file."
    if size > MAX_COOKIE_FILE_BYTES:
        return "That file is too large to be a cookies.txt export."
    return ""
