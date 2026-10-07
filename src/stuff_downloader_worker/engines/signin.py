"""Sign-in engine (plan §6.4): a small private browser where the owner signs in to one site.

It runs in its own engine env (PyQt6-WebEngine), like the download engines, so the in-app
library updater keeps the browser current, with the same self-test and one-click undo.

The owner types their password into the site's own page, so it goes only to the site; nothing
here reads a form. What is kept is the cookie jar the site sets, reported by the profile's
cookie store. On "Done" the site's cookies are written as a Netscape cookies.txt — the format
yt-dlp and gallery-dl read — to ``<output_dir>/<site>.txt``, the only copy kept. The profile is
off the record: the browser itself writes nothing to disk.

Options: ``site`` (a host name such as ``instagram.com``) and optionally ``icon`` (the app's icon
file, for the window). Result: ``{"site", "path", "cookies"}``. Closing the window or pressing
Cancel ends the job with the ``cancelled`` code.

Qt is imported only inside ``download``: the worker package must load in every engine env.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol import JobSpec
from .base import Emit, EngineError

# Where the window opens for well-known sites; any other site opens its home page.
SIGNIN_URLS = {
    "instagram.com": "https://www.instagram.com/accounts/login/",
    "tiktok.com": "https://www.tiktok.com/login",
    "x.com": "https://x.com/i/flow/login",
    "twitter.com": "https://x.com/i/flow/login",
    "facebook.com": "https://www.facebook.com/login/",
    "reddit.com": "https://www.reddit.com/login/",
    "youtube.com": (
        "https://accounts.google.com/ServiceLogin?service=youtube"
        "&continue=https%3A%2F%2Fwww.youtube.com%2F"
    ),
    "pinterest.com": "https://www.pinterest.com/login/",
    "vimeo.com": "https://vimeo.com/log_in",
    "soundcloud.com": "https://soundcloud.com/signin",
    "twitch.tv": "https://www.twitch.tv/login",
}
# One account, two names: cookies for either are kept for both.
_ALIASES = {"x.com": ("twitter.com",), "twitter.com": ("x.com",)}
_SITE = re.compile(r"(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+")
_ICON_EXTS = (".ico", ".png")


@dataclass(frozen=True)
class Cookie:
    """One cookie the window saw, in the fields a cookies.txt line holds."""

    domain: str
    path: str
    secure: bool
    expires: int  # seconds since the epoch; 0 for a session cookie
    name: str
    value: str


def signin_url(site: str) -> str:
    return SIGNIN_URLS.get(site, f"https://{site}/")


def cookie_matches(domain: str, site: str) -> bool:
    host = domain.lstrip(".").lower()
    return any(host == s or host.endswith("." + s) for s in (site, *_ALIASES.get(site, ())))


def cookies_txt(cookies: Iterable[Cookie]) -> str:
    """``cookies`` as a Netscape cookies.txt."""
    lines = ["# Netscape HTTP Cookie File", "# Written by Stuff Downloader's sign-in window."]
    for c in cookies:
        fields = (c.domain, c.path or "/", c.name, c.value)
        if not c.domain or not c.name or any(ch in f for f in fields for ch in "\t\r\n"):
            continue  # a field with a tab or newline would break the line format
        lines.append(
            "\t".join(
                (
                    c.domain,
                    "TRUE" if c.domain.startswith(".") else "FALSE",
                    c.path or "/",
                    "TRUE" if c.secure else "FALSE",
                    str(max(0, int(c.expires))),
                    c.name,
                    c.value,
                )
            )
        )
    return "\n".join(lines) + "\n"


def save_signin(site: str, cookies: Iterable[Cookie], folder: Path) -> tuple[Path, int] | None:
    """Write ``site``'s cookies to ``folder/<site>.txt``: (path, count), or None if none."""
    kept = [c for c in cookies if cookie_matches(c.domain, site)]
    if not kept:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{site}.txt"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(cookies_txt(kept), encoding="utf-8")
    os.replace(tmp, path)
    return path, len(kept)


def parse_options(job: JobSpec) -> tuple[str, Path, Path | None]:
    """(site, output folder, icon or None), validated. Raises bad_options for anything else."""
    opts = dict(job.options)
    unknown = set(opts) - {"site", "icon"}
    if unknown:
        raise EngineError("bad_options", f"unknown options: {sorted(unknown)}")
    site = opts.get("site")
    if not isinstance(site, str) or not _SITE.fullmatch(site):
        raise EngineError("bad_options", "'site' must be a host name like instagram.com")
    folder = Path(job.output_dir)
    if not folder.is_absolute():
        raise EngineError("bad_options", "the sign-in folder must be an absolute path")
    icon = opts.get("icon")
    if icon is not None:
        icon_path = Path(icon) if isinstance(icon, str) else None
        if icon_path is None or icon_path.suffix.lower() not in _ICON_EXTS:
            raise EngineError("bad_options", "'icon' must be an .ico or .png file")
        icon = icon_path if icon_path.is_file() else None  # a missing icon is not worth failing
    return site, folder, icon


# ── the window (Qt from here on) ──────────────────────────────────────────────────────────
_QT_TOKEN = re.compile(r"\s*QtWebEngine/\S+")

# Sites like Instagram open Windows' own "Choose a passkey" box by themselves. It confuses a
# plain sign-in, and while it is open the process cannot exit. Hiding passkey support makes
# those sites show their normal username and password form instead.
_NO_PASSKEYS = """
(() => {
  try {
    delete window.PublicKeyCredential;
    const creds = navigator.credentials;
    if (!creds) return;
    const refuse = () => Promise.reject(new DOMException("No passkeys", "NotAllowedError"));
    const get = creds.get.bind(creds), create = creds.create.bind(creds);
    creds.get = (o) => (o && o.publicKey ? refuse() : get(o));
    creds.create = (o) => (o && o.publicKey ? refuse() : create(o));
  } catch (e) {}
})();
"""

# The app's dark look, for the few widgets around the page.
_STYLE = """
QDialog { background: #1c1c1f; color: #e8e8ea; }
QLabel { color: #e8e8ea; font-size: 10pt; }
QLabel#muted { color: #a0a0a8; }
QPushButton { background: #2c2c31; color: #e8e8ea; border: 1px solid #3a3a40;
              border-radius: 6px; padding: 6px 14px; font-size: 10pt; }
QPushButton:hover { background: #36363c; }
QPushButton#primary { background: #4cc2ff; color: #0b1a24; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #6fcfff; }
"""


def to_cookie(raw: Any) -> Cookie:
    """A QNetworkCookie as the plain record ``save_signin`` writes."""
    expires = 0
    if not raw.isSessionCookie():
        expires = max(0, raw.expirationDate().toSecsSinceEpoch())
    return Cookie(
        domain=raw.domain(),
        path=raw.path() or "/",
        secure=raw.isSecure(),
        expires=expires,
        name=bytes(raw.name()).decode("latin-1"),
        value=bytes(raw.value()).decode("latin-1"),
    )


def _browser_profile(app: Any) -> tuple[Any, dict[tuple[str, str, str], Cookie]]:
    """A private profile with passkeys hidden, and the live jar its cookie store reports."""
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript

    profile = QWebEngineProfile(app)  # no storage name: off the record
    # The default agent names QtWebEngine, which some sign-in pages refuse.
    profile.setHttpUserAgent(_QT_TOKEN.sub("", profile.httpUserAgent()))
    script = QWebEngineScript()
    script.setName("no-passkeys")
    script.setSourceCode(_NO_PASSKEYS)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    profile.scripts().insert(script)
    jar: dict[tuple[str, str, str], Cookie] = {}

    def added(raw: Any) -> None:
        cookie = to_cookie(raw)
        jar[(cookie.domain, cookie.path, cookie.name)] = cookie

    def removed(raw: Any) -> None:
        cookie = to_cookie(raw)
        jar.pop((cookie.domain, cookie.path, cookie.name), None)

    store = profile.cookieStore()
    store.cookieAdded.connect(added)
    store.cookieRemoved.connect(removed)
    return profile, jar


def _window_classes() -> tuple[type, type]:
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    class Page(QWebEnginePage):
        """Opens the site's pop-ups ("Continue with Google", and so on) in their own window."""

        def __init__(self, profile: Any, parent: Any, owner: Any) -> None:
            super().__init__(profile, parent)
            self._owner = owner

        def createWindow(self, _type: Any) -> Any:  # noqa: N802 (Qt override)
            popup = Popup(self.profile(), self._owner)
            popup.show()
            return popup.page

    class Popup(QDialog):
        def __init__(self, profile: Any, owner: Any) -> None:
            super().__init__(owner)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.resize(560, 680)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            view = QWebEngineView(self)
            self.page = Page(profile, view, owner)
            view.setPage(self.page)
            layout.addWidget(view)
            self.page.windowCloseRequested.connect(self.close)
            self.page.titleChanged.connect(self.setWindowTitle)

    class SignInWindow(QDialog):
        """Shows the site's sign-in page; ``finish`` saves the site's cookies and closes."""

        def __init__(self, site: str, folder: Path, profile: Any, jar: dict) -> None:
            super().__init__()
            self.site, self.folder, self.jar = site, folder, jar
            self.saved: tuple[Path, int] | None = None
            self.setWindowTitle(f"Sign in to {site} · Stuff Downloader")
            self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
            self.resize(1000, 780)
            layout = QVBoxLayout(self)
            top = QHBoxLayout()
            steps = QLabel(
                f"1. Sign in to {site} below, the way you normally would.\n"
                "2. When you can see you are signed in, press Done."
            )
            steps.setTextFormat(Qt.TextFormat.PlainText)
            self.done_button = QPushButton("Done, I'm signed in")
            self.done_button.setObjectName("primary")
            cancel = QPushButton("Cancel")
            top.addWidget(steps, 1)
            top.addWidget(self.done_button)
            top.addWidget(cancel)
            layout.addLayout(top)
            # Which site the page is really on, so the owner knows where the password goes.
            self.address_label = QLabel("")
            self.address_label.setObjectName("muted")
            self.address_label.setTextFormat(Qt.TextFormat.PlainText)
            self.error_label = QLabel("")
            self.error_label.setObjectName("muted")
            self.error_label.setWordWrap(True)
            self.error_label.hide()
            layout.addWidget(self.address_label)
            layout.addWidget(self.error_label)
            self.view = QWebEngineView(self)
            self.page = Page(profile, self.view, self)
            self.view.setPage(self.page)
            layout.addWidget(self.view, 1)
            self.page.urlChanged.connect(self._show_address)
            self.done_button.clicked.connect(self.finish)
            cancel.clicked.connect(self.reject)
            self.view.setUrl(QUrl(signin_url(site)))

        def _show_address(self, url: Any) -> None:
            lock = "🔒 " if url.scheme() == "https" else ""
            self.address_label.setText(f"{lock}You are on: {url.host()}")

        def finish(self) -> None:
            self.saved = save_signin(self.site, list(self.jar.values()), self.folder)
            if self.saved is None:
                self.error_label.setText(
                    f"Nothing from {self.site} yet. Sign in on the page first, then press Done."
                )
                self.error_label.show()
                return
            self.accept()

    return SignInWindow, Page


class SignInEngine:
    name = "signin"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        site, folder, icon = parse_options(job)
        try:
            from PyQt6 import sip
            from PyQt6.QtCore import QCoreApplication, Qt
            from PyQt6.QtGui import QIcon
            from PyQt6.QtWidgets import QApplication
        except ImportError as exc:
            raise EngineError(
                "engine_missing", f"the sign-in browser is not installed: {exc}"
            ) from None
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        try:
            from PyQt6 import QtWebEngineWidgets  # noqa: F401 (must load before the app exists)
        except ImportError as exc:
            raise EngineError(
                "engine_missing", f"the sign-in browser is not installed: {exc}"
            ) from None
        app = QApplication.instance() or QApplication(["stuff-downloader-signin"])
        app.setApplicationDisplayName("Stuff Downloader")
        app.setStyleSheet(_STYLE)
        if icon is not None:
            app.setWindowIcon(QIcon(str(icon)))
        profile, jar = _browser_profile(app)
        window_class, _page = _window_classes()
        window = window_class(site, folder, profile, jar)
        emit("stage", {"stage": "signing in"})
        try:
            window.show()
            window.raise_()
            window.activateWindow()
            accepted = window.exec()
            saved = window.saved if accepted else None
        finally:
            # Pages before their profile, while Qt still runs; left to interpreter exit, the
            # profile outlives the application and the process hangs instead of ending.
            sip.delete(window)
            app.processEvents()
            sip.delete(profile)
        if saved is None:
            raise EngineError("cancelled", "Cancelled by you")
        path, count = saved
        return {"site": site, "path": str(path), "cookies": count}
