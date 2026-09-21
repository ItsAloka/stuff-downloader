"""Reusable themed widgets for the shell."""

from __future__ import annotations

import html

from PyQt6.QtCore import QMimeData, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import cookies
from ..core.gallery import GalleryItem
from ..core.spotify import Match, SpotifyTrack
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


class SiteLoginDialog(QDialog):
    """The advanced site-login choice for one site (plan §6.4). Offered only after a failure.

    The dialog returns a choice; it never reads, copies or shows a cookie. ``choice()`` is
    ``None`` for "No login", which removes any saved choice for the site.
    """

    def __init__(self, site: str, current: cookies.SiteLogin | None = None, parent=None) -> None:
        super().__init__(parent)
        self.site = site
        self.setWindowTitle("Advanced: site login")
        layout = QVBoxLayout(self)
        heading = QLabel(f"Use a login for {site}?")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setStyleSheet("font-weight:600; font-size:11pt;")
        guidance = QLabel(cookies.GUIDANCE)
        guidance.setTextFormat(Qt.TextFormat.PlainText)
        guidance.setWordWrap(True)
        guidance.setObjectName("muted")
        layout.addWidget(heading)
        layout.addWidget(guidance)

        self.none_radio = QRadioButton("No login (default)")
        self.browser_radio = QRadioButton("Use a browser session")
        self.file_radio = QRadioButton("Use a cookies.txt file")
        self.group = QButtonGroup(self)
        for radio in (self.none_radio, self.browser_radio, self.file_radio):
            self.group.addButton(radio)
        self.browser_combo = QComboBox()
        for browser in cookies.BROWSERS:
            self.browser_combo.addItem(browser.capitalize(), browser)
        self.profile_edit = QLineEdit()
        self.profile_edit.setPlaceholderText("Profile name (optional)")
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText(r"C:\path\to\cookies.txt")
        self.file_button = QPushButton("Browse…")
        self.error_label = QLabel("")
        self.error_label.setObjectName("muted")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        browser_row = QHBoxLayout()
        browser_row.setContentsMargins(24, 0, 0, 0)
        browser_row.addWidget(self.browser_combo)
        browser_row.addWidget(self.profile_edit, 1)
        file_row = QHBoxLayout()
        file_row.setContentsMargins(24, 0, 0, 0)
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.file_button)
        layout.addWidget(self.none_radio)
        layout.addWidget(self.browser_radio)
        layout.addLayout(browser_row)
        layout.addWidget(self.file_radio)
        layout.addLayout(file_row)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if current is not None and current.source == "browser":
            self.browser_radio.setChecked(True)
            self.browser_combo.setCurrentIndex(self.browser_combo.findData(current.browser))
            self.profile_edit.setText(current.profile)
        elif current is not None:
            self.file_radio.setChecked(True)
            self.file_edit.setText(current.path)
        else:
            self.none_radio.setChecked(True)
        self.file_button.clicked.connect(self._browse)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a cookies.txt file", "", "Cookies file (*.txt)"
        )
        if chosen:
            self.file_edit.setText(chosen)
            self.file_radio.setChecked(True)

    def choice(self) -> cookies.SiteLogin | None:
        """The selected choice, or None for No login. Raises ValueError when it is invalid."""
        if self.none_radio.isChecked():
            return None
        if self.browser_radio.isChecked():
            raw = {
                "source": "browser",
                "browser": self.browser_combo.currentData(),
                "profile": self.profile_edit.text(),
            }
            parsed = cookies.parse(raw)
            if parsed is None:
                raise ValueError("A profile is a name only: letters, digits, spaces, . _ -")
            return parsed
        path = self.file_edit.text().strip()
        problem = cookies.check_file(path) if path else "Pick a cookies.txt file."
        parsed = cookies.parse({"source": "file", "path": path}) if not problem else None
        if parsed is None:
            raise ValueError(problem or "Pick a cookies.txt file by its full path.")
        return parsed

    def _accept(self) -> None:
        try:
            self.choice()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        self.accept()


GALLERY_ICON = QSize(128, 128)
_KIND_GLYPH = {"image": "🖼", "video": "🎞", "file": "📄"}


class GalleryCard(Card):
    """An analyzed gallery: a grid of checkable previews, then download the ones ticked.

    Each tile's data is the item's 1-based position. That position is all a download sends,
    so what is ticked here is exactly what the worker fetches.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self.title_label)
        self.body.addWidget(self.meta_label)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setIconSize(GALLERY_ICON)
        self.grid.setGridSize(QSize(GALLERY_ICON.width() + 24, GALLERY_ICON.height() + 40))
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid.setMinimumHeight(300)
        self.body.addWidget(self.grid)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.archive_check = QCheckBox("Skip items already downloaded to this folder")
        self.archive_check.setChecked(True)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.select_none_button)
        controls.addWidget(self.archive_check, 1)
        self.body.addLayout(controls)

        buttons = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setObjectName("muted")
        self.download_button = QPushButton("⬇  Download selected (original quality)")
        self.download_button.setObjectName("primary")
        buttons.addWidget(self.selection_label, 1)
        buttons.addWidget(self.download_button)
        self.body.addLayout(buttons)

    def set_items(self, items: tuple[GalleryItem, ...] | list[GalleryItem]) -> None:
        self.grid.clear()
        for item in items:
            tile = QListWidgetItem(item.label)
            tile.setData(Qt.ItemDataRole.UserRole, item.index)
            tile.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            tile.setCheckState(Qt.CheckState.Checked)
            tile.setToolTip(item.label)
            pixmap = QPixmap()
            if item.preview and pixmap.loadFromData(item.preview):
                tile.setIcon(
                    QIcon(
                        pixmap.scaled(
                            GALLERY_ICON,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                )
            else:
                tile.setText(f"{_KIND_GLYPH.get(item.kind, '📄')}  {item.label}")
            self.grid.addItem(tile)

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.grid.count()):
            self.grid.item(row).setCheckState(state)

    def selected_indices(self) -> list[int]:
        chosen = []
        for row in range(self.grid.count()):
            tile = self.grid.item(row)
            if tile.checkState() == Qt.CheckState.Checked:
                chosen.append(int(tile.data(Qt.ItemDataRole.UserRole)))
        return chosen


def plain_tooltip(text: str) -> str:
    """A tooltip that shows ``text`` literally.

    Qt renders a tooltip as rich text whenever it looks like HTML, and these carry titles written
    by whoever uploaded the song. Escaping inside an explicit paragraph keeps them as characters.
    """
    return f"<p style='white-space:pre-wrap'>{html.escape(text)}</p>"


def format_diff(seconds: float | None) -> str:
    """A match's duration difference: "+3s", "−12s", "0s", or "—" when unknown."""
    if not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return "—"
    whole = round(seconds)
    if whole == 0:
        return "0s"
    return f"+{whole}s" if whole > 0 else f"−{-whole}s"


# A match whose length is further off than this is flagged: it is probably a live cut, a remix,
# an extended edit or a video with a long intro rather than the recording Spotify lists.
MATCH_DIFF_WARN = 10
MATCH_SCORE_WARN = 70.0


class SpotifyCard(Card):
    """A Spotify track/album/playlist: tick tracks, review their YouTube matches, download.

    Spotify's audio is DRM-protected and never downloaded. The card says so up front, because the
    whole point of the match columns is that the audio comes from somewhere else.
    """

    COLUMNS = ("", "#", "Title", "Artist", "Length", "YouTube match", "Diff", "Score", "")
    MATCH_COLUMN, DIFF_COLUMN, SCORE_COLUMN, CHANGE_COLUMN = 5, 6, 7, 8
    DISCLOSURE = (
        "Spotify's own audio is protected and is never downloaded. Each song is matched from "
        "YouTube Music, then tagged with Spotify's title, artist, album and cover. A match can "
        "be the wrong recording, so check the ones that matter to you."
    )
    NOT_CHECKED = "Not checked"
    NOT_CHECKED_TIP = "No match looked up yet. The best one is picked when the song downloads."

    change_requested = pyqtSignal(int)  # row

    def __init__(self) -> None:
        super().__init__()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.disclosure_label = QLabel(self.DISCLOSURE)
        self.disclosure_label.setWordWrap(True)
        self.disclosure_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self.title_label)
        self.body.addWidget(self.meta_label)
        self.body.addWidget(self.disclosure_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(260)
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # Title and match share the spare width; a long artist list must not starve them.
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(3, 170)
        header.setSectionResizeMode(self.MATCH_COLUMN, QHeaderView.ResizeMode.Stretch)
        # ResizeToContents measures items, not cell widgets, so the button column is sized here.
        header.setSectionResizeMode(self.CHANGE_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(self.CHANGE_COLUMN, 104)
        self.body.addWidget(self.table)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.match_button = QPushButton("🔎  Check matches")
        self.match_button.setToolTip(
            "Look up the YouTube recording for each selected song (about half a minute each)"
        )
        self.archive_check = QCheckBox("Skip songs already downloaded to this folder")
        self.archive_check.setChecked(True)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.select_none_button)
        controls.addWidget(self.match_button)
        controls.addWidget(self.archive_check, 1)
        self.body.addLayout(controls)

        buttons = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setObjectName("muted")
        self.download_button = QPushButton("⬇  Download selected as MP3")
        self.download_button.setObjectName("primary")
        buttons.addWidget(self.selection_label, 1)
        buttons.addWidget(self.download_button)
        self.body.addLayout(buttons)

    def checkbox(self, row: int) -> QCheckBox | None:
        widget = self.table.cellWidget(row, 0)
        return widget if isinstance(widget, QCheckBox) else None

    def change_button(self, row: int) -> QPushButton | None:
        widget = self.table.cellWidget(row, self.CHANGE_COLUMN)
        return widget if isinstance(widget, QPushButton) else None

    def set_tracks(self, tracks: tuple[SpotifyTrack, ...] | list[SpotifyTrack]) -> None:
        self.table.setRowCount(len(tracks))
        for row, track in enumerate(tracks):
            box = QCheckBox()
            box.setChecked(True)
            self.table.setCellWidget(row, 0, box)
            title = f"{track.title}  🅴" if track.explicit else track.title
            cells = (str(track.index), title, track.artist, format_duration(track.duration))
            for column, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                if column in (2, 3):
                    item.setToolTip(plain_tooltip(text))  # the columns that get cut short
                self.table.setItem(row, column, item)
            change = QPushButton("Change…")
            change.setObjectName("rowButton")
            change.setToolTip("Paste a different YouTube link for this song")
            change.clicked.connect(lambda _=False, r=row: self.change_requested.emit(r))
            self.table.setCellWidget(row, self.CHANGE_COLUMN, change)
            self.set_status(row, self.NOT_CHECKED, tip=self.NOT_CHECKED_TIP)

    def set_status(self, row: int, text: str, warn: bool = False, tip: str = "") -> None:
        """A row with no match to show: not checked yet, checking, or why none was found."""
        self._set_match_cells(row, text, "", "", warn)
        item = self.table.item(row, self.MATCH_COLUMN)
        if item is not None:
            item.setToolTip(plain_tooltip(tip or text))

    def set_match(self, row: int, match: Match) -> None:
        if match.manual:
            label = "Your link"
            score = "—"
        else:
            label = match.title or "YouTube Music result"
            if match.channel:
                label = f"{label}  ·  {match.channel}"
            score = f"{match.confidence:.0f}%" if match.confidence is not None else "—"
        diff = match.duration_diff
        warn = (diff is not None and abs(diff) > MATCH_DIFF_WARN) or (
            match.confidence is not None and match.confidence < MATCH_SCORE_WARN
        )
        self._set_match_cells(row, label, format_diff(diff), score, warn)
        item = self.table.item(row, self.MATCH_COLUMN)
        if item is not None:
            # The video id, not a link: one more copyable URL is one more way around the router.
            item.setToolTip(plain_tooltip(f"{label}\nYouTube video {match.video_id}"))

    def _set_match_cells(self, row: int, label: str, diff: str, score: str, warn: bool) -> None:
        for column, text in (
            (self.MATCH_COLUMN, label),
            (self.DIFF_COLUMN, diff),
            (self.SCORE_COLUMN, score),
        ):
            item = QTableWidgetItem(text)
            if warn:
                item.setForeground(QColor("#e0a040"))
            self.table.setItem(row, column, item)

    def cell_text(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text() if item is not None else ""

    def selected_rows(self) -> list[int]:
        rows = []
        for row in range(self.table.rowCount()):
            box = self.checkbox(row)
            if box is not None and box.isChecked():
                rows.append(row)
        return rows

    def set_all_checked(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            box = self.checkbox(row)
            if box is not None:
                box.setChecked(checked)
