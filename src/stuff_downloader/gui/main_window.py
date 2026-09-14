from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import settings, tools
from .pages import TOOL_LABELS, DownloadsPage, SettingsPage, ToolsPage
from .theme import STYLE


def tool_health_text(statuses: list[tools.ToolStatus]) -> str:
    """Footer summary, e.g. 'FFmpeg ✔ · ffprobe ✖ · Deno ✖'. Never claims a missing tool."""
    if not statuses:
        return "Tools not checked"
    return "  ·  ".join(
        f"{TOOL_LABELS.get(s.name, s.name)} {'✔' if s.ok else '✖'}" for s in statuses
    )


class MainWindow(QMainWindow):
    def __init__(self, app_settings: settings.Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Stuff Downloader")
        self.resize(1040, 660)
        self.setMinimumSize(QSize(760, 480))
        self.setStyleSheet(STYLE)
        self.app_settings = app_settings or settings.load()

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
        self.downloads_page = DownloadsPage(self.app_settings)
        self.tools_page = ToolsPage(self.app_settings)
        self.settings_page = SettingsPage(self.app_settings)
        for label, page in (
            ("⬇   Downloads", self.downloads_page),
            ("🛠   Tools", self.tools_page),
            ("⚙   Settings", self.settings_page),
        ):
            self.sidebar.addItem(label)
            self.stack.addWidget(page)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
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

        self.tools_page.statuses_changed.connect(self._update_footer)
        self.settings_page.folder_changed.connect(
            lambda _: self.downloads_page.refresh_folder_hint()
        )
        self._update_footer(self.tools_page.statuses)

    def _update_footer(self, statuses: list[tools.ToolStatus]) -> None:
        self.footer_label.setText(tool_health_text(statuses))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.downloads_page.shutdown()
        super().closeEvent(event)
