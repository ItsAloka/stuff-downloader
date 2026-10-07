"""Per-site logins for restricted media (plan §6.4). No Qt imports, no engine imports.

Off by default: the app works with no entry here, and public links never need one.

The usual way in is the sign-in window (the worker's ``signin`` engine): the owner signs in to
the site there, and the site's cookies are written to a cookies.txt under the app's local data
folder. That file is the only place cookie contents are kept. Settings store just the *choice* —
that file's path, a cookies.txt the owner picked, or a browser name — and the choice is attached
to a job at launch time, never written into the job's options, the queue database or history.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import paths

# Browsers yt-dlp can read. Kept so older settings still load, but the dialog offers Firefox
# only: on Windows, Chrome, Edge and Brave lock their cookie database and use app-bound
# encryption, so reading them fails for nearly everyone.
BROWSERS = ("firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi")
# A profile is a name, never a path: a path here would let a settings file point the engine at
# any directory on disk.
_PROFILE = re.compile(r"[A-Za-z0-9 ._-]{0,64}")
MAX_COOKIE_FILE_BYTES = 5_000_000
_STRIP_PREFIXES = ("www.", "m.", "mobile.")

# Offered first when signing in from Settings; the owner can type any other site.
COMMON_SITES = (
    "instagram.com",
    "tiktok.com",
    "x.com",
    "facebook.com",
    "youtube.com",
    "reddit.com",
    "pinterest.com",
    "vimeo.com",
)
SIGNIN_HELP = (
    "Some posts are only shown to people who are signed in. Sign in once here and Stuff"
    " Downloader uses it for this site's links.\n\n"
    "You sign in on the site's own page, inside Stuff Downloader. Your password goes only to"
    ' the site; Stuff Downloader never sees or saves it. It keeps the site\'s "signed in"'
    " cookie on this PC, and Sign out deletes it."
)

GUIDANCE = (
    "For people who already use these:\n"
    "• Firefox: sign in to the site in Firefox, then choose it here.\n"
    "• A cookies.txt file (Netscape format) exported for just this site.\n\n"
    "Chrome, Edge and Brave are not offered: on Windows they lock and encrypt their cookies,"
    " so no other app can read them. Use Sign in above instead."
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
        if is_signin(self):
            return "Signed in"
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


def site_for(url: str, choices: dict[str, SiteLogin]) -> str:
    """The stored site whose choice applies to ``url`` (subdomains included), or ""."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    for site in choices:
        if host == site or host.endswith("." + site):
            return site
    return ""


def choice_for(url: str, choices: dict[str, SiteLogin]) -> SiteLogin | None:
    """The owner's choice for ``url``'s site, matching subdomains of a stored site too."""
    site = site_for(url, choices)
    return choices[site] if site else None


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


# ── the sign-in window's files ─────────────────────────────────────────────────────────────
# The window is the worker's ``signin`` engine; it writes ``<site>.txt`` into this folder.
def signin_dir() -> Path:
    return paths.data_dir() / "signins"


def signin_path(site: str) -> Path:
    return signin_dir() / f"{site}.txt"


def is_signin(choice: SiteLogin) -> bool:
    """Whether ``choice`` is a file the sign-in window wrote (and so one the app may delete)."""
    if choice.source != "file":
        return False
    parent = os.path.normcase(os.path.abspath(Path(choice.path).parent))
    return parent == os.path.normcase(os.path.abspath(signin_dir()))


def forget(choice: SiteLogin | None) -> None:
    """Delete the cookies file behind a sign-in. A file the owner picked is never touched."""
    if choice is not None and is_signin(choice):
        try:
            Path(choice.path).unlink(missing_ok=True)
        except OSError:
            pass
