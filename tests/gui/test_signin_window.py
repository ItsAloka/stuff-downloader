"""Signing in (plan §6.4): the engine's Qt helpers, and the app's wait box around the engine."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QDateTime
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import QDialog

from stuff_downloader.core import cookies
from stuff_downloader.core.protocol import Event
from stuff_downloader.core.runner import WorkerRuntimeMissing
from stuff_downloader.gui import signin
from stuff_downloader_worker.engines import signin as engine


# ── the engine's Qt side, without loading a web page ─────────────────────────────────────
def test_a_session_cookie_is_written_with_no_expiry():
    raw = QNetworkCookie(b"sessionid", b"abc")
    raw.setDomain(".instagram.com")
    raw.setPath("/")
    raw.setSecure(True)
    assert engine.to_cookie(raw) == engine.Cookie(
        ".instagram.com", "/", True, 0, "sessionid", "abc"
    )


def test_a_lasting_cookie_keeps_its_expiry():
    raw = QNetworkCookie(b"ds_user_id", b"42")
    raw.setDomain("www.instagram.com")
    raw.setExpirationDate(QDateTime.fromSecsSinceEpoch(2_000_000_000))
    cookie = engine.to_cookie(raw)
    assert cookie.expires == 2_000_000_000 and cookie.path == "/" and not cookie.secure


def test_the_page_never_offers_passkeys():
    assert "PublicKeyCredential" in engine._NO_PASSKEYS
    assert engine._QT_TOKEN.sub("", "Mozilla/5.0 QtWebEngine/6.11.2 Chrome/140") == (
        "Mozilla/5.0 Chrome/140"
    )


# ── the app's side: start the engine, wait, read its answer ──────────────────────────────
class FakeRun:
    runs: list[FakeRun] = []

    def __init__(self, spec, on_event):
        self.spec, self.on_event = spec, on_event
        self.started = self.cancelled = False
        FakeRun.runs.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def emit(self, event_type, **data):
        self.on_event(Event(event_type, self.spec.job_id, data))


@pytest.fixture
def wait(qtbot):
    FakeRun.runs = []

    def make(site="instagram.com", factory=FakeRun):
        box = signin.SignInWait(site, run_factory=factory)
        qtbot.addWidget(box)
        return box

    return make


def _signed_file(site):
    path = cookies.signin_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsid\tv\n")
    return path


def test_the_engine_job_names_the_site_and_the_sign_ins_folder(wait):
    wait()
    (run,) = FakeRun.runs
    assert run.started and run.spec.engine == "signin"
    assert run.spec.url == "https://instagram.com/"
    assert run.spec.output_dir == str(cookies.signin_dir())
    assert run.spec.options["site"] == "instagram.com"


def test_done_in_the_window_gives_the_login(wait, qtbot):
    box = wait()
    path = _signed_file("instagram.com")
    with qtbot.waitSignal(box.accepted):
        FakeRun.runs[0].emit("result", site="instagram.com", path=str(path), cookies=1)
    assert box.login == cookies.SiteLogin("file", path=str(path))
    assert box.result() == QDialog.DialogCode.Accepted


def test_cancel_in_the_window_closes_the_wait_box(wait):
    box = wait()
    FakeRun.runs[0].emit("error", code="cancelled", message="Cancelled by you")
    assert box.login is None and box.result() == QDialog.DialogCode.Rejected


def test_cancel_in_the_wait_box_closes_the_window(wait):
    box = wait()
    box.cancel_button.click()
    assert FakeRun.runs[0].cancelled and box.login is None


def test_a_missing_browser_says_how_to_get_it(wait):
    box = wait()
    FakeRun.runs[0].emit("error", code="engine_missing", message="no PyQt6.QtWebEngine")
    assert "Reinstall" in box.message.text() and box.cancel_button.text() == "Close"


def test_no_engine_runtime_at_all_says_the_same(wait):
    def missing(spec, on_event):
        raise WorkerRuntimeMissing("engine runtime not found")

    box = wait(factory=missing)
    assert "not installed" in box.message.text()


def test_a_result_pointing_outside_the_sign_ins_folder_is_refused(wait, tmp_path):
    box = wait()
    elsewhere = tmp_path / "x.txt"
    elsewhere.write_text("# Netscape HTTP Cookie File\n")
    FakeRun.runs[0].emit("result", site="instagram.com", path=str(elsewhere), cookies=1)
    assert box.login is None and "could not be read" in box.message.text()
