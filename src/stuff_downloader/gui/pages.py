"""M0 pages: Downloads (fake job demo), Tools (health) and Settings (folder)."""

from __future__ import annotations

import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import paths, settings, tools
from ..core.protocol import Event, JobSpec
from ..core.runner import JobRun, WorkerRuntimeMissing
from . import theme
from .bridge import EventBridge
from .widgets import Card, Chip, JobCard, format_bytes, format_eta, page_header, section_title

STAGE_LABELS = {"downloading": "Downloading", "completed": "Completed"}


def _page_layout(widget: QWidget) -> QVBoxLayout:
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(32, 28, 32, 24)
    layout.setSpacing(14)
    return layout


class DownloadsPage(QWidget):
    def __init__(self, app_settings: settings.Settings) -> None:
        super().__init__()
        self._settings = app_settings
        self._run: JobRun | None = None
        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)

        layout = _page_layout(self)
        layout.addWidget(
            page_header("Downloads", "Paste a link to start. This build runs an offline demo job.")
        )

        paste_card = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlEdit")
        self.url_edit.setPlaceholderText("🔗  Paste a link from YouTube, Spotify, TikTok…")
        self.url_edit.setClearButtonEnabled(True)
        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Paste a link from the clipboard")
        self.start_button = QPushButton("⬇  Run demo job")
        self.start_button.setObjectName("primary")
        self.start_button.setDefault(True)
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.paste_button)
        row.addWidget(self.start_button)
        paste_card.body.addLayout(row)
        self.folder_hint = QLabel()
        self.folder_hint.setObjectName("muted")
        paste_card.body.addWidget(self.folder_hint)
        layout.addWidget(paste_card)

        queue_header = QHBoxLayout()
        queue_header.addWidget(section_title("Queue"))
        queue_header.addStretch(1)
        self.queue_summary = QLabel("Nothing running")
        self.queue_summary.setObjectName("muted")
        queue_header.addWidget(self.queue_summary)
        layout.addLayout(queue_header)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("emptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(20, 28, 20, 28)
        empty_text = QLabel("No downloads yet.\nPaste a link above and press Run demo job.")
        empty_text.setObjectName("muted")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_state)

        self.job_card = JobCard()
        self.job_card.hide()
        layout.addWidget(self.job_card)
        layout.addStretch(1)

        # Public handles kept stable for callers and tests.
        self.progress = self.job_card.progress
        self.status_label = self.job_card.chip
        self.cancel_button = self.job_card.cancel_button
        self.cancel_button.setEnabled(False)

        self.start_button.clicked.connect(self.start_fake_job)
        self.url_edit.returnPressed.connect(self.start_fake_job)
        self.paste_button.clicked.connect(self._paste)
        self.cancel_button.clicked.connect(self.cancel_job)
        self.refresh_folder_hint()

    def refresh_folder_hint(self) -> None:
        self.folder_hint.setText(f"Saving to  {self._settings.effective_download_dir()}")

    def _paste(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        if text:
            self.url_edit.setText(text)

    def start_fake_job(self) -> None:
        if self._run is not None:
            return
        url = self.url_edit.text().strip() or "https://example.invalid/demo"
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine="fake",
            url=url,
            output_dir=str(self._settings.effective_download_dir()),
            options={"steps": 50, "delay": 0.05},
        )
        self.empty_state.hide()
        self.job_card.show()
        self.job_card.title_label.setText(url)
        try:
            self._run = JobRun(spec, self._bridge.post)
        except WorkerRuntimeMissing as exc:
            self.job_card.set_progress(0)
            self.job_card.set_state("Failed", "failed")
            self.job_card.details_label.setText(f"Cannot start worker: {exc}")
            self.queue_summary.setText("1 failed")
            return
        self.job_card.details_label.setText("Starting worker…")
        self.job_card.set_progress(0)
        self.job_card.set_state("Starting", "active")
        self.queue_summary.setText("1 active")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._run.start()

    def cancel_job(self) -> None:
        if self._run is not None:
            self.job_card.set_state("Cancelling", "active")
            self.cancel_button.setEnabled(False)
            self._run.cancel()

    def _on_event(self, event: Event) -> None:
        if self._run is None or event.job_id != self._run.spec.job_id:
            return
        card = self.job_card
        if event.type == "stage":
            stage = str(event.data.get("stage", ""))
            if stage != "completed":
                card.set_state(STAGE_LABELS.get(stage, stage.capitalize()), "active")
        elif event.type == "progress":
            data = event.data
            percent = data.get("percent")
            if isinstance(percent, int | float) and not isinstance(percent, bool):
                card.set_progress(percent)
            done = format_bytes(data.get("downloaded_bytes"))
            parts = [f"{done} / {format_bytes(data.get('total_bytes'))}"]
            speed = data.get("speed")
            if isinstance(speed, int | float) and not isinstance(speed, bool):
                parts.append(f"{format_bytes(speed)}/s")
            eta = format_eta(data.get("eta"))
            if eta:
                parts.append(eta)
            card.details_label.setText("  ·  ".join(parts))
        elif event.is_terminal:
            if event.type == "result":
                card.set_progress(100)
                card.set_state("Completed", "completed")
                card.details_label.setText(
                    f"Done  ·  {format_bytes(event.data.get('total_bytes'))}"
                )
                summary = "1 done"
            elif event.data.get("code") == "cancelled":
                card.set_state("Cancelled", "cancelled")
                card.details_label.setText("Cancelled by you")
                summary = "1 cancelled"
            else:
                card.set_state("Failed", "failed")
                card.details_label.setText(str(event.data.get("message", "Unknown error")))
                summary = "1 failed"
            self.queue_summary.setText(summary)
            self._run = None
            self.start_button.setEnabled(True)
            self.cancel_button.setEnabled(False)

    def shutdown(self) -> None:
        if self._run is not None:
            self._run.cancel()


TOOL_LABELS = {"ffmpeg": "FFmpeg", "ffprobe": "ffprobe", "deno": "Deno"}


class ToolsPage(QWidget):
    statuses_changed = pyqtSignal(list)

    def __init__(self, app_settings: settings.Settings) -> None:
        super().__init__()
        self._settings = app_settings
        self.statuses: list[tools.ToolStatus] = []

        layout = _page_layout(self)
        layout.addWidget(
            page_header("Tools", "External programs used to merge, convert and tag media.")
        )

        summary_row = QHBoxLayout()
        self.summary_chip = Chip()
        summary_row.addWidget(self.summary_chip)
        summary_row.addStretch(1)
        refresh = QPushButton("↻  Re-check")
        refresh.clicked.connect(self.refresh)
        summary_row.addWidget(refresh)
        layout.addLayout(summary_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Tool", "Status", "Version", "Path"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.table, 1)

        hint = QLabel(
            "Missing tools are not needed for the demo job. They arrive with real downloads."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.refresh()

    def refresh(self) -> None:
        self.statuses = tools.check_all(self._settings.tool_paths)
        self.table.setRowCount(len(statuses := self.statuses))
        for row, status in enumerate(statuses):
            state = (
                "✔  Found"
                if status.ok
                else f"✖  {status.error.capitalize() if status.error else 'Error'}"
            )
            cells = (
                TOOL_LABELS.get(status.name, status.name),
                state,
                status.version or "—",
                status.path or "—",
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setForeground(QColor(theme.SUCCESS if status.ok else theme.DANGER))
                self.table.setItem(row, col, item)
        found = sum(s.ok for s in statuses)
        total = len(statuses)
        self.summary_chip.set(
            f"{found} of {total} tools found", "ok" if total and found == total else "missing"
        )
        self.statuses_changed.emit(list(statuses))


class SettingsPage(QWidget):
    folder_changed = pyqtSignal(str)

    def __init__(self, app_settings: settings.Settings) -> None:
        super().__init__()
        self._settings = app_settings

        layout = _page_layout(self)
        layout.addWidget(page_header("Settings", "Changes are saved automatically."))

        card = Card()
        card.body.addWidget(section_title("Storage"))
        caption = QLabel("Download folder")
        caption.setObjectName("muted")
        card.body.addWidget(caption)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.folder_edit = QLineEdit(str(app_settings.effective_download_dir()))
        self.folder_edit.setReadOnly(True)
        change = QPushButton("Change…")
        change.setObjectName("primary")
        reset = QPushButton("Use Downloads")
        row.addWidget(self.folder_edit, 1)
        row.addWidget(change)
        row.addWidget(reset)
        card.body.addLayout(row)
        layout.addWidget(card)
        layout.addStretch(1)

        change.clicked.connect(self._choose_folder)
        reset.clicked.connect(lambda: self.set_folder(""))

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.folder_edit.text()
        )
        if chosen:
            self.set_folder(chosen)

    def set_folder(self, folder: str) -> bool:
        if folder and not paths.is_writable_dir(Path(folder)):
            QMessageBox.warning(self, "Folder not usable", f"Cannot write to:\n{folder}")
            return False
        self._settings.download_dir = folder
        try:
            settings.save(self._settings)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save settings", str(exc))
            return False
        effective = str(self._settings.effective_download_dir())
        self.folder_edit.setText(effective)
        self.folder_changed.emit(effective)
        return True
