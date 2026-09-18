from __future__ import annotations

import logging

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..core import history, settings, tools
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
from .theme import STYLE

log = logging.getLogger(__name__)


def tool_health_text(statuses: list[tools.ToolStatus]) -> str:
    """Footer summary, e.g. 'FFmpeg ✔ · ffprobe ✖ · Deno ✖'. Never claims a missing tool."""
    if not statuses:
        return "Tools not checked"
    return "  ·  ".join(
        f"{TOOL_LABELS.get(s.name, s.name)} {'✔' if s.ok else '✖'}" for s in statuses
    )


class MainWindow(QMainWindow):
    def __init__(
        self,
        app_settings: settings.Settings | None = None,
        store: history.Store | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Stuff Downloader")
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
