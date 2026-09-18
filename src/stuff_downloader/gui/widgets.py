"""Reusable themed widgets for the shell."""

from __future__ import annotations

from PyQt6.QtCore import QMimeData, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import set_state

# Drags carry the job id only: a drop from another application can never be mistaken for a
# queue reorder.
QUEUE_MIME = "application/x-stuff-downloader-job"


def format_bytes(value: float | int | None) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_eta(seconds: float | int | None) -> str:
    if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds < 0:
        return ""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s left"
    return f"{seconds // 60}m {seconds % 60:02d}s left"


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)


class Chip(QLabel):
    def __init__(self, text: str = "", state: str = "idle") -> None:
        super().__init__(text)
        self.setObjectName("chip")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_state(self, state)

    def set(self, text: str, state: str) -> None:
        self.setText(text)
        set_state(self, state)


def page_header(title: str, subtitle: str) -> QWidget:
    header = QWidget()
    layout = QVBoxLayout(header)
    layout.setContentsMargins(0, 0, 0, 6)
    layout.setSpacing(2)
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("pageSubtitle")
    subtitle_label.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(subtitle_label)
    return header


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


class JobCard(Card):
    """One job row: thumbnail placeholder, title, stage chip, thin progress bar, details, cancel.

    A row is draggable only while its job is still queued — a running, paused or finished job
    has no place in the pending order, so ``draggable`` stays False and both the drag and the
    drop are refused.
    """

    reorder_requested = pyqtSignal(str, str)  # dragged job id, the id of the row it was dropped on

    def __init__(self) -> None:
        super().__init__()
        self.job_id = ""
        self.draggable = False
        self._press_pos = None
        self.setAcceptDrops(True)
        top = QHBoxLayout()
        top.setSpacing(12)

        self.thumb = QLabel("⬇")
        self.thumb.setFixedSize(44, 44)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            "background:#133247; color:#4cc2ff; border-radius:8px; font-size:16pt;"
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600;")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label = QLabel("")
        self.details_label.setObjectName("muted")
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.details_label)

        self.chip = Chip("Idle")
        self.cancel_button = QPushButton("✕  Cancel")
        self.cancel_button.setObjectName("iconButton")
        self.cancel_button.setToolTip("Cancel this job")
        self.pause_button = QPushButton("⏸  Pause")
        self.pause_button.setObjectName("iconButton")
        self.pause_button.setToolTip("Pause this job; the partial file is kept")
        self.retry_button = QPushButton("↻  Retry")
        self.retry_button.setObjectName("iconButton")
        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("iconButton")
        self.folder_button = QPushButton("Show in folder")
        self.folder_button.setObjectName("iconButton")
        for button in (self.retry_button, self.open_button, self.folder_button):
            button.hide()

        top.addWidget(self.thumb)
        top.addLayout(text_col, 1)
        top.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        for button in (self.open_button, self.folder_button, self.retry_button):
            top.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.pause_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignTop)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("muted")
        self.percent_label.setMinimumWidth(40)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar_row = QHBoxLayout()
        bar_row.addWidget(self.progress, 1)
        bar_row.addWidget(self.percent_label)

        self.body.addLayout(top)
        self.body.addLayout(bar_row)

    def set_state(self, text: str, state: str) -> None:
        self.chip.set(text, state)
        set_state(self.progress, state)

    def set_progress(self, percent: float) -> None:
        percent = max(0.0, min(100.0, percent))
        self.progress.setValue(int(percent))
        self.percent_label.setText(f"{percent:.0f}%")

    # ── drag to reorder ──────────────────────────────────────────────────────────────────
    def set_draggable(self, draggable: bool) -> None:
        self.draggable = bool(draggable)
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if self.draggable else Qt.CursorShape.ArrowCursor
        )

    def _dragged_id(self, event) -> str:
        """The job id a drop carries, or "" when it is not one of our queue drags."""
        data = event.mimeData()
        if not self.draggable or not self.job_id or not data.hasFormat(QUEUE_MIME):
            return ""
        job_id = bytes(data.data(QUEUE_MIME)).decode("utf-8", "replace")
        return "" if job_id == self.job_id else job_id

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if (
            self.draggable
            and self.job_id
            and self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._press_pos = None
            data = QMimeData()
            data.setData(QUEUE_MIME, self.job_id.encode("utf-8"))
            drag = QDrag(self)
            drag.setMimeData(data)
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._dragged_id(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        job_id = self._dragged_id(event)
        if not job_id:
            event.ignore()
            return
        event.acceptProposedAction()
        self.reorder_requested.emit(job_id, self.job_id)


def format_duration(seconds: float | int | None) -> str:
    if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds < 0:
        return ""
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def square_crop(pixmap: QPixmap) -> QPixmap:
    side = min(pixmap.width(), pixmap.height())
    x = (pixmap.width() - side) // 2
    y = (pixmap.height() - side) // 2
    return pixmap.copy(x, y, side, side)


class PreviewCard(Card):
    """Analyze result: cover, title, playlist prompt and the preset/quality pickers."""

    def __init__(self) -> None:
        super().__init__()
        top = QHBoxLayout()
        top.setSpacing(14)
        self.cover = QLabel("🎞")
        self.cover.setFixedSize(160, 90)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet("background:#133247; color:#4cc2ff; border-radius:8px;")
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.title_label.setWordWrap(True)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.playlist_label = QLabel("This link is part of a playlist.")
        self.playlist_label.setObjectName("muted")
        self.playlist_label.setWordWrap(True)
        self.playlist_label.hide()
        self.playlist_button = QPushButton("☰  Whole playlist…")
        self.playlist_button.setToolTip("Open the playlist and choose which songs to download")
        self.playlist_button.hide()
        playlist_row = QHBoxLayout()
        playlist_row.setContentsMargins(0, 0, 0, 0)
        playlist_row.addWidget(self.playlist_label, 1)
        playlist_row.addWidget(self.playlist_button)
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.meta_label)
        text_col.addLayout(playlist_row)
        text_col.addStretch(1)
        top.addWidget(self.cover)
        top.addLayout(text_col, 1)
        self.body.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.preset_combo = QComboBox()
        self.resolution_combo = QComboBox()
        self.compatible_check = QCheckBox("Most compatible (H.264/AAC MP4, plays everywhere)")
        self.compatible_check.setChecked(True)
        self.crop_check = QCheckBox("Crop cover to a square")
        self.crop_check.setChecked(True)
        self.download_button = QPushButton("⬇  Download")
        self.download_button.setObjectName("primary")
        grid.addWidget(QLabel("Preset"), 0, 0)
        grid.addWidget(self.preset_combo, 0, 1)
        grid.addWidget(QLabel("Quality"), 1, 0)
        grid.addWidget(self.resolution_combo, 1, 1)
        grid.addWidget(self.compatible_check, 2, 1)
        grid.addWidget(self.crop_check, 3, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.download_button)
        self.body.addLayout(buttons)


class GroupCard(Card):
    """The aggregate row for one playlist batch."""

    def __init__(self, title: str, count: int) -> None:
        super().__init__()
        self.setObjectName("card")
        top = QHBoxLayout()
        self.title_label = QLabel(f"☰  Playlist: {title}")
        self.title_label.setStyleSheet("font-weight:600;")
        self.title_label.setWordWrap(True)
        self.summary_label = QLabel(f"0 / {count} done")
        self.summary_label.setObjectName("muted")
        top.addWidget(self.title_label, 1)
        top.addWidget(self.summary_label, 0, Qt.AlignmentFlag.AlignTop)
        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, count))
        self.progress.setTextVisible(False)
        self.body.addLayout(top)
        self.body.addWidget(self.progress)

    def set_counts(self, done: int, total: int, failed: int = 0, skipped: int = 0) -> None:
        parts = [f"{done} / {total} done"]
        if skipped:
            parts.append(f"{skipped} already downloaded")
        if failed:
            parts.append(f"{failed} failed")
        self.summary_label.setText("  ·  ".join(parts))
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done + failed + skipped, max(1, total)))


class PlaylistCard(Card):
    """The playlist expansion table: checkbox · # · title · artist · duration · state."""

    COLUMNS = ("", "#", "Title", "Artist", "Length", "")

    def __init__(self) -> None:
        super().__init__()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.title_label.setWordWrap(True)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setWordWrap(True)
        self.body.addWidget(self.title_label)
        self.body.addWidget(self.meta_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(220)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.body.addWidget(self.table)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by title…")
        self.filter_edit.setClearButtonEnabled(True)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.select_none_button)
        controls.addWidget(self.filter_edit, 1)
        self.body.addLayout(controls)

        options = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.archive_check = QCheckBox("Skip songs already downloaded to this folder")
        self.archive_check.setChecked(True)
        options.addWidget(QLabel("Preset"))
        options.addWidget(self.preset_combo)
        options.addWidget(self.archive_check, 1)
        self.body.addLayout(options)

        buttons = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setObjectName("muted")
        self.download_button = QPushButton("⬇  Download selected")
        self.download_button.setObjectName("primary")
        buttons.addWidget(self.selection_label, 1)
        buttons.addWidget(self.download_button)
        self.body.addLayout(buttons)

    def checkbox(self, row: int) -> QCheckBox | None:
        widget = self.table.cellWidget(row, 0)
        return widget if isinstance(widget, QCheckBox) else None

    def set_entries(self, entries) -> None:
        """Fill the table. Unavailable entries are listed with their reason, never dropped."""
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            box = QCheckBox()
            box.setChecked(entry.selectable)
            box.setEnabled(entry.selectable)
            self.table.setCellWidget(row, 0, box)
            cells = (
                str(entry.index),
                entry.title,
                entry.uploader,
                format_duration(entry.duration),
                entry.unavailable,
            )
            for column, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                if not entry.selectable:
                    item.setForeground(QColor("#8aa0b4"))
                self.table.setItem(row, column, item)

    def selected_rows(self) -> list[int]:
        rows = []
        for row in range(self.table.rowCount()):
            box = self.checkbox(row)
            if box is not None and box.isChecked() and box.isEnabled():
                rows.append(row)
        return rows

    def set_all_checked(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            box = self.checkbox(row)
            if box is not None and box.isEnabled() and not self.table.isRowHidden(row):
                box.setChecked(checked)

    def apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            title = item.text().lower() if item is not None else ""
            self.table.setRowHidden(row, bool(needle) and needle not in title)
