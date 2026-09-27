from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import data_root, release_version, resource_path
from ..core import engine_update, history, runner, settings, tools, updates
from .pages import (
    ENGINE_LABELS,
    NOTIFICATION_TITLE_LIMIT,
    TOOL_LABELS,
    DownloadsPage,
    HistoryPage,
    SettingsPage,
    ToolsPage,
    library_versions,
    safe_error_message,
    safe_notification_body,
    safe_notification_line,
)
from .theme import DANGER, STYLE, SUCCESS, TEXT_DIM
from .widgets import section_title

log = logging.getLogger(__name__)


def tool_health_text(statuses: list[tools.ToolStatus]) -> str:
    """Footer summary, e.g. 'FFmpeg ✔ · ffprobe ✖ · Deno ✖'. Never claims a missing tool."""
    if not statuses:
        return "Tools not checked"
    return "  ·  ".join(
        f"{TOOL_LABELS.get(s.name, s.name)} {'✔' if s.ok else '✖'}" for s in statuses
    )


LICENCE_FILES = ("LICENSE", "THIRD_PARTY_LICENSES.txt")


def app_icon() -> QIcon:
    """The committed app icon; an empty QIcon (never an exception) if it is missing."""
    path = resource_path("app.ico")
    return QIcon(str(path)) if path.is_file() else QIcon()


def is_first_run(path: Path | None = None) -> bool:
    """No settings file yet. The welcome saves one however it is closed, so it shows once."""
    try:
        return not (path or settings.settings_path()).exists()
    except OSError:
        return False


def engine_runtime_statuses(
    check: updates.CheckResult | None = None,
) -> list[tuple[str, bool, str]]:
    """(engine, usable, detail) for each engine env. Reads active.json; runs nothing.

    With the result of an update check, a usable env's line also says whether it is up to date."""
    root = runner.runtime_root()
    result = []
    for engine in ENGINE_LABELS:
        env_id = runner._active_env(root, engine)
        if env_id is None:
            result.append((engine, False, "not installed"))
        elif not runner.runtime_python(engine).is_file():
            result.append((engine, False, f"env {env_id} is missing its interpreter"))
        else:
            detail = f"env {env_id}"
            if check is not None and check.reached_pypi and engine in check.installed:
                offered = [o for o in check.offers if o.engine == engine and o.selectable]
                if offered:
                    detail += " · " + ", ".join(
                        f"update {o.name} {o.version} available" for o in offered
                    )
                else:
                    detail += " · up to date"
            result.append((engine, True, detail))
    return result


def licence_paths() -> dict[str, Path]:
    root = data_root()
    return {name: root / name for name in LICENCE_FILES}


def _plain(text: str, muted: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if muted:
        label.setObjectName("muted")
    return label


def _status_label(ok: bool, text: str) -> QLabel:
    label = _plain(f"{'✔' if ok else '✖'}  {text}")
    label.setStyleSheet(f"color: {SUCCESS if ok else DANGER};")
    return label


class WelcomeDialog(QDialog):
    """First run: an optional download folder and a look at the tools. No account, no sign-in."""

    def __init__(
        self,
        app_settings: settings.Settings,
        statuses: list[tools.ToolStatus],
        engines: list[tuple[str, bool, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to Stuff Downloader")
        self.setMinimumWidth(520)
        self.chosen_folder: str | None = None
        self.check_labels: list[QLabel] = []
        layout = QVBoxLayout(self)
        layout.addWidget(section_title("Welcome"))
        layout.addWidget(
            _plain(
                "Everything stays on this PC. There is no account and nothing to sign in to. "
                "You can change any of this later in Settings and Tools.",
                muted=True,
            )
        )

        layout.addWidget(section_title("Download folder (optional)"))
        row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(app_settings.effective_download_dir()))
        self.folder_edit.setReadOnly(True)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(choose)
        layout.addLayout(row)

        layout.addWidget(section_title("Tools"))
        self.checks = QVBoxLayout()
        layout.addLayout(self.checks)
        self.set_checks(statuses, engines)

        self.update_status = _plain("", muted=True)
        self.update_status.hide()
        layout.addWidget(self.update_status)

        self.recheck_button = QPushButton("↻  Re-check")
        self.check_updates_button = QPushButton("Check for updates")
        buttons = QDialogButtonBox()
        buttons.addButton(self.recheck_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.check_updates_button, QDialogButtonBox.ButtonRole.ActionRole)
        start = buttons.addButton("Get started", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def set_checks(
        self, statuses: list[tools.ToolStatus], engines: list[tuple[str, bool, str]]
    ) -> None:
        for label in self.check_labels:
            self.checks.removeWidget(label)
            label.deleteLater()
        self.check_labels = []
        for status in statuses:
            detail = (status.version or "found") if status.ok else (status.error or "missing")
            name = TOOL_LABELS.get(status.name, status.name)
            self.check_labels.append(_status_label(status.ok, f"{name} — {detail}"))
        for engine, ok, detail in engines:
            self.check_labels.append(_status_label(ok, f"{ENGINE_LABELS[engine]} — {detail}"))
        for label in self.check_labels:
            self.checks.addWidget(label)

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)
        self.update_status.setVisible(bool(text))

    def _choose(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.folder_edit.text()
        )
        if chosen:
            self.select_folder(chosen)

    def select_folder(self, folder: str) -> None:
        self.chosen_folder = folder
        self.folder_edit.setText(folder)


# ── library updates (plan §2, R8) ───────────────────────────────────────────────────────────
class UpdateService(QObject):
    """Runs the update check, installs, undo and the rollback watchdog off the GUI thread.

    One check, install or undo at a time. Results come back as signals, which Qt queues onto the
    GUI thread because this object lives there. Nothing here raises into Qt: every failure
    becomes a bounded message."""

    checked = pyqtSignal(object, bool)  # CheckResult, or None on an unexpected error; manual
    progress = pyqtSignal(int, int, str)  # steps done, total steps, what is happening
    installed = pyqtSignal(list, list)  # engines updated, [(engine, error)]
    undone = pyqtSignal(list, list)  # engines switched back, [(engine, error)]
    rolled_back = pyqtSignal(str)  # engine the watchdog switched back

    STEPS = ("resolve", "install", "prune")

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        check: Callable[..., updates.CheckResult] = updates.check,
        install: Callable[..., str] = engine_update.update_engine,
        undo: Callable[..., str] = engine_update.undo,
        watchdog: Callable[[], engine_update.Watchdog] = engine_update.Watchdog,
    ) -> None:
        super().__init__(parent)
        self._check, self._install, self._undo, self._watchdog = check, install, undo, watchdog
        self._busy = False
        self.threads: list[threading.Thread] = []

    @property
    def busy(self) -> bool:
        return self._busy

    def _spawn(self, target: Callable[[], None], exclusive: bool = True) -> bool:
        if exclusive:
            if self._busy:
                return False
            self._busy = True

        def run() -> None:
            try:
                target()
            except Exception:  # noqa: BLE001 - a thread must never die with a traceback only
                log.exception("Update task failed")

        thread = threading.Thread(target=run, name="stuff-downloader-updates", daemon=True)
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        thread.start()
        return True

    def _done(self) -> None:
        self._busy = False

    def start_check(self, app_settings: settings.Settings, manual: bool) -> bool:
        """Check PyPI now. The 24 h throttle is the caller's business (updates.is_due)."""
        snapshot = copy.deepcopy(app_settings)  # the thread never touches live settings

        def work() -> None:
            result = None
            try:
                result = self._check(snapshot)
            except Exception:  # noqa: BLE001 - never crash the app over an update check
                log.exception("Update check failed")
            finally:
                self._done()
                self.checked.emit(result, manual)

        return self._spawn(work)

    def start_install(self, offers: Sequence[updates.Offer]) -> bool:
        by_engine: dict[str, dict[str, str]] = {}
        for offer in offers:
            if offer.version is not None:
                by_engine.setdefault(offer.engine, {})[offer.name] = offer.version
        if not by_engine:
            return False
        total = len(by_engine) * len(self.STEPS)

        def work() -> None:
            ok: list[str] = []
            failed: list[tuple[str, str]] = []
            try:
                for index, (engine, selections) in enumerate(by_engine.items()):
                    base = index * len(self.STEPS)

                    def say(stage: str, message: str, base: int = base) -> None:
                        if stage in self.STEPS:
                            step = base + self.STEPS.index(stage)
                            self.progress.emit(step, total, safe_error_message(message))

                    try:
                        self._install(engine, selections, progress=say)
                        ok.append(engine)
                    except engine_update.UpdateError as exc:
                        failed.append((engine, safe_error_message(str(exc))))
                    except Exception as exc:  # noqa: BLE001 - reported, never raised into Qt
                        log.exception("Updating %s failed", engine)
                        failed.append((engine, safe_error_message(str(exc) or "unexpected error")))
                    self.progress.emit(base + len(self.STEPS), total, "")
            finally:
                self._done()
                self.installed.emit(ok, failed)

        return self._spawn(work)

    def start_undo(self, engines: Sequence[str]) -> bool:
        engines = list(engines)
        if not engines:
            return False

        def work() -> None:
            ok: list[str] = []
            failed: list[tuple[str, str]] = []
            try:
                for engine in engines:
                    try:
                        self._undo(engine)
                        ok.append(engine)
                    except engine_update.UpdateError as exc:
                        failed.append((engine, safe_error_message(str(exc))))
                    except Exception as exc:  # noqa: BLE001 - reported, never raised into Qt
                        log.exception("Undoing the %s update failed", engine)
                        failed.append((engine, safe_error_message(str(exc) or "unexpected error")))
            finally:
                self._done()
                self.undone.emit(ok, failed)

        return self._spawn(work)

    def record_start(self, env: str, env_id: object, started: bool) -> bool:
        """One download's engine start. Only a watched (freshly updated) env costs a thread."""
        if not isinstance(env_id, str) or not env:
            return False
        try:
            watchdog = self._watchdog()
            if watchdog.watching(env) != env_id:
                return False
        except Exception:  # noqa: BLE001 - a broken watch file must not touch downloads
            log.exception("Could not read the update watch")
            return False

        def work() -> None:
            if watchdog.record(env, env_id, started) is not None:
                self.rolled_back.emit(env)

        # Not exclusive: the watch file has its own lock, and a rollback that meets a running
        # update is refused by engine_update rather than raced.
        return self._spawn(work, exclusive=False)


def _offer_text(name: str, old: str, new: str) -> str:
    return f"{name}    {old} → {new}"


class UpdatesDialog(QDialog):
    """Updates available: one tick box per library, then a progress bar while it installs."""

    update_requested = pyqtSignal(list)  # the ticked offers
    skip_requested = pyqtSignal(list)  # [(library, version)]

    def __init__(self, offers: Sequence[updates.Offer], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Updates available")
        self.setMinimumWidth(560)
        self.offers = list(offers)
        self.rows: list[tuple[QCheckBox, updates.Offer]] = []
        self.blocked_rows: list[tuple[QLabel, updates.Offer]] = []
        self.installing = False
        layout = QVBoxLayout(self)
        layout.addWidget(section_title("Updates available"))
        layout.addWidget(
            _plain(
                "Newer versions of the download engines' libraries. Each update is tested "
                "before it is used, and Settings can undo it.",
                muted=True,
            )
        )

        list_widget = QWidget()
        rows = QVBoxLayout(list_widget)
        rows.setContentsMargins(0, 4, 0, 4)
        rows.setSpacing(6)
        for offer in self.offers:
            engine = ENGINE_LABELS.get(offer.engine, offer.engine)
            if offer.version is not None:
                text = _offer_text(offer.name, offer.installed, offer.version)
                if offer.recommended:
                    text += "    (recommended)"
                box = QCheckBox(text)
                box.setChecked(True)
                box.setToolTip(engine)
                box.toggled.connect(self._update_buttons)
                rows.addWidget(box)
                self.rows.append((box, offer))
            if offer.newer_major is not None:
                label = _plain(
                    _offer_text(offer.name, offer.installed, offer.newer_major)
                    + "    needs an app update"
                )
                label.setStyleSheet(f"color: {TEXT_DIM}; padding-left: 26px;")
                label.setEnabled(False)
                label.setToolTip(f"{engine}: a new major version this app is not built for yet")
                rows.addWidget(label)
                self.blocked_rows.append((label, offer))
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(list_widget)
        # The dark theme, not the platform's light viewport, behind the rows.
        list_widget.setObjectName("updateRows")
        scroll.setStyleSheet("QScrollArea, QWidget#updateRows { background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setMaximumHeight(320)
        layout.addWidget(scroll)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.status_label = _plain("")
        self.status_label.hide()
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        self.update_button = buttons.addButton(
            "Update selected", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.update_button.setObjectName("primary")
        self.later_button = buttons.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        self.skip_button = buttons.addButton(
            "Skip this version", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.skip_button.setToolTip(
            "Never offer the ticked versions, or the greyed-out ones, again. "
            "A newer version is offered as usual."
        )
        self.close_button = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.close_button.hide()
        # Each button is wired itself; the box's accepted/rejected would double-fire.
        self.update_button.clicked.connect(self._update)
        self.later_button.clicked.connect(self.reject)
        self.skip_button.clicked.connect(self._skip)
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(buttons)
        self._update_buttons()

    def selected_offers(self) -> list[updates.Offer]:
        return [offer for box, offer in self.rows if box.isChecked()]

    def skip_targets(self) -> list[tuple[str, str]]:
        """The ticked versions plus every greyed-out newer major."""
        targets = [(o.name, o.version) for o in self.selected_offers() if o.version]
        targets += [(o.name, o.newer_major) for _, o in self.blocked_rows if o.newer_major]
        return targets

    def _update_buttons(self) -> None:
        self.update_button.setEnabled(not self.installing and bool(self.selected_offers()))
        self.skip_button.setEnabled(not self.installing and bool(self.skip_targets()))

    def _update(self) -> None:
        offers = self.selected_offers()
        if offers and not self.installing:
            self.update_requested.emit(offers)

    def _skip(self) -> None:
        targets = self.skip_targets()
        if targets and not self.installing:
            self.skip_requested.emit(targets)
            self.accept()

    def start_install(self) -> None:
        self.installing = True
        for box, _ in self.rows:
            box.setEnabled(False)
        self.later_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.status_label.setText("Starting the update…")
        self.status_label.show()
        self._update_buttons()

    def set_progress(self, done: int, total: int, text: str) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(max(0, min(done, total)))
        if text:
            self.status_label.setText(text)

    def finish_install(self, ok: bool, text: str) -> None:
        self.installing = False
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if ok else 0)
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {SUCCESS if ok else DANGER};")
        self.status_label.show()
        for button in (self.update_button, self.later_button, self.skip_button):
            button.hide()
        self.close_button.show()

    def reject(self) -> None:
        # Esc or the title-bar close during an install would hide the only progress display.
        if not self.installing:
            super().reject()


def _engines_text(engines: Sequence[str]) -> str:
    return ", ".join(ENGINE_LABELS.get(e, e) for e in engines)


def _errors_text(errors: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"{ENGINE_LABELS.get(e, e)}: {message}" for e, message in errors)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Stuff Downloader")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        icon_path = resource_path("app.png")
        if icon_path.is_file():
            logo = QLabel()
            logo.setPixmap(
                QPixmap(str(icon_path)).scaled(
                    56, 56, transformMode=Qt.TransformationMode.SmoothTransformation
                )
            )
            head.addWidget(logo)
        title = QVBoxLayout()
        title.addWidget(section_title("Stuff Downloader"))
        self.version_label = _plain(f"Version {release_version()}", muted=True)
        title.addWidget(self.version_label)
        title.addWidget(_plain("Private · local · no account. Released under the MIT licence."))
        head.addLayout(title, 1)
        layout.addLayout(head)

        grid = QGridLayout()
        self.open_buttons: dict[str, QPushButton] = {}
        self.copy_buttons: dict[str, QPushButton] = {}
        for row, (name, path) in enumerate(licence_paths().items()):
            grid.addWidget(_plain(name), row, 0)
            open_button = QPushButton("Open")
            copy_button = QPushButton("Copy path")
            exists = path.is_file()
            open_button.setEnabled(exists)
            if not exists:
                open_button.setToolTip("Not found in this copy of the app")
            open_button.clicked.connect(lambda _=False, p=path: self.open_file(p))
            copy_button.clicked.connect(lambda _=False, p=path: self.copy_path(p))
            grid.addWidget(open_button, row, 1)
            grid.addWidget(copy_button, row, 2)
            self.open_buttons[name] = open_button
            self.copy_buttons[name] = copy_button
        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def open_file(path: Path) -> bool:
        return path.is_file() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    @staticmethod
    def copy_path(path: Path) -> None:
        QGuiApplication.clipboard().setText(str(path))


class MainWindow(QMainWindow):
    def __init__(
        self,
        app_settings: settings.Settings | None = None,
        store: history.Store | None = None,
        show_welcome: bool | None = None,
        check_updates: bool | None = None,
        updater: UpdateService | None = None,
    ) -> None:
        super().__init__()
        # Like the welcome: only the owner's real window checks PyPI by itself after it opens.
        self._startup_update_check = (
            app_settings is None if check_updates is None else check_updates
        )
        self.setWindowTitle("Stuff Downloader")
        self.setWindowIcon(app_icon())
        # Only a window that loads the owner's real settings decides first run by itself;
        # one handed its settings (tests, embedding) shows the welcome only when asked.
        if show_welcome is None:
            show_welcome = app_settings is None and is_first_run()
        self.resize(1040, 660)
        self.setMinimumSize(QSize(760, 480))
        self.setStyleSheet(STYLE)
        self.app_settings = app_settings or settings.load()
        # This window owns the store it shares between the Downloads and History pages, and it
        # is the only thing that closes it — see DownloadsPage.owns_store.
        self.owns_store = store is None
        self.store = store if store is not None else history.Store()

        # Sidebar
        sidebar_panel = QWidget()
        sidebar_panel.setObjectName("sidebarPanel")
        sidebar_panel.setFixedWidth(210)
        side = QVBoxLayout(sidebar_panel)
        side.setContentsMargins(0, 0, 0, 12)
        side.setSpacing(0)
        brand = QLabel("⬇  Stuff Downloader")
        brand.setObjectName("brand")
        tag = QLabel("Private · local · no account")
        tag.setObjectName("brandTag")
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        side.addWidget(brand)
        side.addWidget(tag)
        side.addWidget(self.sidebar, 1)
        self.welcome_button = QPushButton("Welcome && checks")
        self.welcome_button.clicked.connect(self.show_welcome)
        self.about_button = QPushButton("About")
        self.about_button.clicked.connect(self.show_about)
        for button in (self.welcome_button, self.about_button):
            button.setObjectName("iconButton")
            wrap = QHBoxLayout()
            wrap.setContentsMargins(12, 4, 12, 0)
            wrap.addWidget(button)
            side.addLayout(wrap)

        # Pages
        self.stack = QStackedWidget()
        self.downloads_page = DownloadsPage(self.app_settings, self.store)
        self.history_page = HistoryPage(self.store)
        self.tools_page = ToolsPage(self.app_settings)
        self.settings_page = SettingsPage(self.app_settings)
        for label, page in (
            ("⬇   Downloads", self.downloads_page),
            ("🕘   History", self.history_page),
            ("🛠   Tools", self.tools_page),
            ("⚙   Settings", self.settings_page),
        ):
            self.sidebar.addItem(label)
            self.stack.addWidget(page)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.currentRowChanged.connect(self._on_page_changed)
        self.sidebar.setCurrentRow(0)

        # Footer
        footer = QWidget()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 0, 0, 0)
        self.footer_label = QLabel()
        self.footer_label.setObjectName("footerText")
        foot.addStretch(1)
        foot.addWidget(self.footer_label)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.stack, 1)
        content_layout.addWidget(footer)

        central = QWidget()
        central.setObjectName("central")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar_panel)
        layout.addWidget(content, 1)
        self.setCentralWidget(central)

        self.history_page.download_again_requested.connect(self._download_again)
        self.tray = self._build_tray()
        self.downloads_page.notification_requested.connect(self._notify)
        self.settings_page.notifications_changed.connect(self._on_notifications_changed)
        self.tools_page.statuses_changed.connect(self._update_footer)
        self.settings_page.folder_changed.connect(
            lambda _: self.downloads_page.refresh_folder_hint()
        )
        self.settings_page.concurrency_changed.connect(
            self.downloads_page.scheduler.set_max_concurrent
        )
        self._update_footer(self.tools_page.statuses)
        self.welcome_dialog: WelcomeDialog | None = None
        self.about_dialog: AboutDialog | None = None
        if show_welcome:
            QTimer.singleShot(0, self.show_welcome)

        # Library updates (plan §2, R8)
        self.updater = updater or UpdateService(self)
        self.updates_dialog: UpdatesDialog | None = None
        self.last_update_check: updates.CheckResult | None = None
        self._shown_once = False
        self.updater.checked.connect(self._on_update_checked)
        self.updater.progress.connect(self._on_update_progress)
        self.updater.installed.connect(self._on_updates_installed)
        self.updater.undone.connect(self._on_update_undone)
        self.updater.rolled_back.connect(self._on_rolled_back)
        self.downloads_page.engine_start.connect(self.updater.record_start)
        self.settings_page.check_updates_requested.connect(self.check_for_updates)
        self.settings_page.undo_update_requested.connect(self.undo_last_update)
        self.tools_page.check_updates_requested.connect(self.check_for_updates)
        self._refresh_undo()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            if self._startup_update_check:
                # After the window has painted; startup never waits for PyPI.
                QTimer.singleShot(1500, self.startup_update_check)

    # ── library updates ──────────────────────────────────────────────────────────────────
    def startup_update_check(self) -> bool:
        """The once-a-day background check. False when it is switched off or not due yet."""
        if not updates.is_due(self.app_settings):
            return False
        return self.updater.start_check(self.app_settings, manual=False)

    def check_for_updates(self) -> bool:
        """The Check for updates buttons: check now, ignoring the 24 h limit."""
        if not self.updater.start_check(self.app_settings, manual=True):
            self._set_update_status("An update task is already running.")
            return False
        self.settings_page.set_updates_busy(True)
        self._set_update_status("Checking for updates…")
        return True

    def _set_update_status(self, text: str) -> None:
        self.settings_page.set_update_status(text)
        if self.welcome_dialog is not None:
            self.welcome_dialog.set_update_status(text)

    def _save_settings(self) -> None:
        try:
            settings.save(self.app_settings)
        except OSError:
            log.exception("Could not save the update settings")

    def _on_update_checked(self, result: object, manual: bool) -> None:
        self.settings_page.set_updates_busy(False)
        self._refresh_undo()
        if not isinstance(result, updates.CheckResult) or not result.reached_pypi:
            # Offline or PyPI trouble: the startup check says nothing at all.
            if manual:
                self._set_update_status(
                    "Could not reach PyPI. Check the internet connection and try again."
                )
            return
        self.last_update_check = result
        self.app_settings.update_last_check = time.time()
        self._save_settings()
        self.settings_page.set_library_versions(result.installed)
        if self.welcome_dialog is not None:
            self.welcome_dialog.set_checks(
                self.tools_page.statuses, engine_runtime_statuses(result)
            )
        selectable = [o for o in result.offers if o.selectable]
        if manual:
            self._set_update_status(
                f"{len(selectable)} update{'s' if len(selectable) != 1 else ''} available."
                if selectable
                else "Everything is up to date."
            )
        # A startup check only interrupts for something the owner can actually install.
        if selectable or (manual and result.offers):
            self.show_updates(result.offers)

    def show_updates(self, offers: Sequence[updates.Offer]) -> UpdatesDialog | None:
        if self.updates_dialog is not None and self.updates_dialog.installing:
            return None  # never replace a dialog that is showing an install
        if self.updates_dialog is not None:
            self.updates_dialog.close()
        dialog = UpdatesDialog(offers, self)
        dialog.update_requested.connect(self.install_updates)
        dialog.skip_requested.connect(self.skip_versions)
        self.updates_dialog = dialog
        dialog.open()
        return dialog

    def install_updates(self, offers: Sequence[updates.Offer]) -> bool:
        if not self.updater.start_install(offers):
            if self.updates_dialog is not None:
                self.updates_dialog.finish_install(
                    False, "Another update task is running. Try again when it finishes."
                )
            return False
        if self.updates_dialog is not None:
            self.updates_dialog.start_install()
        self.settings_page.set_updates_busy(True)
        self._set_update_status("Updating…")
        return True

    def skip_versions(self, targets: Sequence[tuple[str, str]]) -> None:
        for name, version in targets:
            updates.skip_version(self.app_settings, name, version)
        self._save_settings()

    def _on_update_progress(self, done: int, total: int, text: str) -> None:
        if self.updates_dialog is not None:
            self.updates_dialog.set_progress(done, total, text)

    def _on_updates_installed(self, ok: list, failed: list) -> None:
        self.settings_page.set_updates_busy(False)
        if ok:
            self.app_settings.update_last_engines = list(ok)
            self._save_settings()
        lines = []
        if ok:
            lines.append(f"Updated and tested: {_engines_text(ok)}.")
        if failed:
            lines.append(
                "Not updated, the current version is still in use:\n" + _errors_text(failed)
            )
        text = "\n".join(lines) or "Nothing was updated."
        if self.updates_dialog is not None:
            self.updates_dialog.finish_install(bool(ok) and not failed, text)
        self._set_update_status(text)
        self._after_engine_change()

    def undo_last_update(self) -> bool:
        engines = self._undoable()
        if not engines or not self.updater.start_undo(engines):
            return False
        self.settings_page.set_updates_busy(True)
        self._set_update_status(f"Switching back: {_engines_text(engines)}…")
        return True

    def _on_update_undone(self, ok: list, failed: list) -> None:
        self.settings_page.set_updates_busy(False)
        if ok:
            self.app_settings.update_last_engines = [
                e for e in self.app_settings.update_last_engines if e not in ok
            ]
            self._save_settings()
        lines = [f"Switched back: {_engines_text(ok)}."] if ok else []
        if failed:
            lines.append("Could not switch back:\n" + _errors_text(failed))
        self._set_update_status("\n".join(lines))
        self._after_engine_change()

    def _on_rolled_back(self, engine: str) -> None:
        self.app_settings.update_last_engines = [
            e for e in self.app_settings.update_last_engines if e != engine
        ]
        self._save_settings()
        text = (
            f"The {ENGINE_LABELS.get(engine, engine)} failed to start after its update, "
            "so the previous version is back in use."
        )
        self._set_update_status(text)
        self._notify("Update undone", text, "failed")
        self._after_engine_change()

    def _after_engine_change(self) -> None:
        self.settings_page.set_library_versions(library_versions())
        self._refresh_undo()
        if self.welcome_dialog is not None:
            self.welcome_dialog.set_checks(self.tools_page.statuses, engine_runtime_statuses())

    def _undoable(self) -> list[str]:
        return [
            e
            for e in dict.fromkeys(self.app_settings.update_last_engines)
            if e in updates.ENGINES and engine_update.can_undo(e)
        ]

    def _refresh_undo(self) -> None:
        if not self.updater.busy:
            self.settings_page.set_undo_available(bool(self._undoable()))

    # ── welcome and about ────────────────────────────────────────────────────────────────
    def show_welcome(self) -> WelcomeDialog:
        dialog = WelcomeDialog(
            self.app_settings,
            self.tools_page.statuses,
            engine_runtime_statuses(self.last_update_check),
            self,
        )

        def recheck() -> None:
            self.tools_page.refresh()
            dialog.set_checks(
                self.tools_page.statuses, engine_runtime_statuses(self.last_update_check)
            )

        dialog.recheck_button.clicked.connect(recheck)
        dialog.check_updates_button.clicked.connect(self.check_for_updates)
        dialog.finished.connect(lambda _: self._finish_welcome(dialog))
        self.welcome_dialog = dialog
        dialog.open()
        return dialog

    def _finish_welcome(self, dialog: WelcomeDialog) -> None:
        """Keep a chosen folder; either way write settings, so the welcome is not shown again."""
        if dialog.chosen_folder and self.settings_page.set_folder(dialog.chosen_folder):
            return  # set_folder has saved the settings
        try:
            settings.save(self.app_settings)
        except OSError:
            log.exception("Could not save settings after the welcome")

    def show_about(self) -> AboutDialog:
        dialog = AboutDialog(self)
        self.about_dialog = dialog
        dialog.open()
        return dialog

    # ── tray and notifications ───────────────────────────────────────────────────────────
    def _build_tray(self) -> QSystemTrayIcon | None:
        """The tray icon, or None where the desktop has no tray. Never raises either way."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("No system tray on this desktop; notifications are disabled")
            return None
        icon = self.windowIcon()
        if icon.isNull():
            icon = QIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("Stuff Downloader")
        menu = QMenu(self)
        show = QAction("Show window", self)
        show.triggered.connect(self._show_from_tray)
        hide = QAction("Hide window", self)
        hide.triggered.connect(self.hide)
        quit_action = QAction("Quit", self)
        # close(), not qApp.quit(): closeEvent owns the worker and database teardown.
        quit_action.triggered.connect(self.close)
        for action in (show, hide, quit_action):
            menu.addAction(action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _on_notifications_changed(self, enabled: bool) -> None:
        if self.tray is not None and not enabled:
            self.tray.setToolTip("Stuff Downloader  ·  notifications off")
        elif self.tray is not None:
            self.tray.setToolTip("Stuff Downloader")

    def _notify(self, title: str, message: str, state: str) -> bool:
        """Show a toast. False when there is nothing to show it with, or the owner said no."""
        if not self.app_settings.notifications or self.tray is None:
            return False
        if not self.tray.supportsMessages():
            return False
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if state == "failed"
            else QSystemTrayIcon.MessageIcon.Information
        )
        # Last gate before the OS keeps a copy: whatever the page sent, the notification
        # centre only ever sees bounded text with no paths, URLs or control characters.
        safe_title = safe_notification_line(title, NOTIFICATION_TITLE_LIMIT) or "Stuff Downloader"
        self.tray.showMessage(safe_title, safe_notification_body(message), icon, 5000)
        return True

    def _download_again(self, record: history.JobRecord) -> None:
        """History asked for another copy: queue it and show the queue it landed in."""
        if self.downloads_page.download_again(record) is None:
            return
        self.sidebar.setCurrentRow(self.stack.indexOf(self.downloads_page))

    def _on_page_changed(self, row: int) -> None:
        if self.stack.widget(row) is self.history_page:
            self.history_page.refresh()

    def _update_footer(self, statuses: list[tools.ToolStatus]) -> None:
        self.footer_label.setText(tool_health_text(statuses))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Stop the workers first, then close the shared store last, so both pages still have a
        # usable database for the whole of shutdown.
        #
        # Nothing may escape this method. PyQt6 cannot carry a Python exception back through the
        # Qt event loop that called it, so one raised here aborts the process outright and the
        # database is left without a clean checkpoint. A failure to stop the workers is logged
        # and the exit continues: the worker processes die with this one anyway.
        try:
            self.downloads_page.shutdown()
        except Exception:
            log.exception("Stopping downloads failed during shutdown")
        finally:
            if self.owns_store:
                self.store.close()
        super().closeEvent(event)
