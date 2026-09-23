from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import (
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
    QPushButton,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import data_root, release_version, resource_path
from ..core import history, runner, settings, tools
from .converters import ConvertersPage
from .pages import (
    NOTIFICATION_TITLE_LIMIT,
    TOOL_LABELS,
    DownloadsPage,
    HistoryPage,
    SettingsPage,
    ToolsPage,
    safe_notification_body,
    safe_notification_line,
)
from .theme import DANGER, STYLE, SUCCESS
from .widgets import section_title

log = logging.getLogger(__name__)


def tool_health_text(statuses: list[tools.ToolStatus]) -> str:
    """Footer summary, e.g. 'FFmpeg ✔ · ffprobe ✖ · Deno ✖'. Never claims a missing tool."""
    if not statuses:
        return "Tools not checked"
    return "  ·  ".join(
        f"{TOOL_LABELS.get(s.name, s.name)} {'✔' if s.ok else '✖'}" for s in statuses
    )


ENGINE_LABELS = {
    "ytdlp": "yt-dlp engine",
    "gallerydl": "gallery-dl engine",
    "spotdl": "spotDL engine",
}
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


def engine_runtime_statuses() -> list[tuple[str, bool, str]]:
    """(engine, usable, detail) for each engine env. Reads active.json; runs nothing."""
    root = runner.runtime_root()
    result = []
    for engine in ENGINE_LABELS:
        env_id = runner._active_env(root, engine)
        if env_id is None:
            result.append((engine, False, "not installed"))
        elif not runner.runtime_python(engine).is_file():
            result.append((engine, False, f"env {env_id} is missing its interpreter"))
        else:
            result.append((engine, True, f"env {env_id}"))
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

        self.recheck_button = QPushButton("↻  Re-check")
        buttons = QDialogButtonBox()
        buttons.addButton(self.recheck_button, QDialogButtonBox.ButtonRole.ActionRole)
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

    def _choose(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.folder_edit.text()
        )
        if chosen:
            self.select_folder(chosen)

    def select_folder(self, folder: str) -> None:
        self.chosen_folder = folder
        self.folder_edit.setText(folder)


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
    ) -> None:
        super().__init__()
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
        self.converters_page = ConvertersPage()
        self.tools_page = ToolsPage(self.app_settings)
        self.settings_page = SettingsPage(self.app_settings)
        for label, page in (
            ("⬇   Downloads", self.downloads_page),
            ("🕘   History", self.history_page),
            ("🔄   Converters", self.converters_page),
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

    # ── welcome and about ────────────────────────────────────────────────────────────────
    def show_welcome(self) -> WelcomeDialog:
        dialog = WelcomeDialog(
            self.app_settings, self.tools_page.statuses, engine_runtime_statuses(), self
        )

        def recheck() -> None:
            self.tools_page.refresh()
            dialog.set_checks(self.tools_page.statuses, engine_runtime_statuses())

        dialog.recheck_button.clicked.connect(recheck)
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
            try:
                self.converters_page.shutdown()
            except Exception:
                log.exception("Stopping conversion failed during shutdown")
            if self.owns_store:
                self.store.close()
        super().closeEvent(event)
