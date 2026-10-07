"""Signing in to a site (plan §6.4), from the app's side.

The browser itself is the ``signin`` engine: it runs in its own engine env, like the download
engines, so the library updater keeps it current. This module starts that engine and waits in a
small box until the owner presses Done (or Cancel) in the sign-in window; the event loop keeps
running meanwhile, so the rest of the app never freezes.
"""

from __future__ import annotations

import uuid
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import resource_path
from ..core import cookies
from ..core.protocol import Event, JobSpec
from ..core.runner import JobRun, WorkerRuntimeMissing
from .bridge import EventBridge

ENGINE = "signin"

_FAILURES = {
    "engine_missing": (
        "The sign-in browser is not installed. Reinstall Stuff Downloader to add it."
    ),
    "engine_crashed": "The sign-in window closed unexpectedly. Try again.",
    "worker_exited": "The sign-in window closed unexpectedly. Try again.",
}


def signin_spec(site: str) -> JobSpec:
    """The job that opens the sign-in window for ``site`` and saves into the sign-ins folder."""
    options: dict[str, Any] = {"site": site}
    icon = resource_path("app.ico")
    if icon.is_file():
        options["icon"] = str(icon)
    return JobSpec(uuid.uuid4().hex, ENGINE, f"https://{site}/", str(cookies.signin_dir()), options)


class SignInWait(QDialog):
    """ "Finish signing in in the other window", with Cancel. ``login`` is set on success."""

    def __init__(self, site: str, parent: QWidget | None = None, run_factory: Any = JobRun):
        super().__init__(parent)
        self.site = site
        self.login: cookies.SiteLogin | None = None
        self._run: Any = None
        self._finished = False
        self.setWindowTitle(f"Sign in to {site}")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self.message = QLabel(
            f"The sign-in window for {site} is opening.\n"
            "Sign in there, then press Done in that window."
        )
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.cancel_button.clicked.connect(self.reject)

        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)
        try:
            self._run = run_factory(signin_spec(site), self._bridge.post)
        except WorkerRuntimeMissing:
            self._fail(_FAILURES["engine_missing"])
            return
        self._run.start()

    def _fail(self, text: str) -> None:
        self._finished = True
        self.message.setText(text)
        self.cancel_button.setText("Close")

    def _on_event(self, event: Event) -> None:
        if not event.is_terminal or self._finished:
            return
        self._finished = True
        if event.type == "result":
            path = event.data.get("path")
            login = cookies.parse({"source": "file", "path": path}) if path else None
            if login is not None and cookies.is_signin(login) and not cookies.check_file(path):
                self.login = login
                self.accept()
                return
            self._fail("The sign-in finished, but its file could not be read. Try again.")
            return
        code = event.data.get("code")
        if code == "cancelled":
            super().reject()
            return
        self._fail(_FAILURES.get(code) or f"Signing in failed: {event.data.get('message')}")

    def reject(self) -> None:
        if not self._finished and self._run is not None:
            self._run.cancel()  # closes the sign-in window too
        super().reject()


def sign_in(site: str, parent: QWidget | None = None) -> cookies.SiteLogin | None:
    """Open the sign-in window for ``site``; the new login, or None if it did not happen."""
    wait = SignInWait(site, parent)
    try:
        return wait.login if wait.exec() else None
    finally:
        wait.deleteLater()
