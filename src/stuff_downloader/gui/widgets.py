"""Reusable themed widgets for the shell."""

from __future__ import annotations

import html

from PyQt6.QtCore import QMimeData, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont, QIcon, QImage, QPainter, QPixmap
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
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import cookies
from ..core.gallery import GalleryItem
from ..core.spotify import (
    UNCERTAIN_RULE,
    Candidate,
    Match,
    SpotifyTrack,
    duration_diff,
    is_uncertain,
    match_score,
)
from .theme import ACCENT, BORDER, SURFACE, SURFACE_HOVER, TEXT, TEXT_DIM, set_state
from .thumbs import decode_image

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
        # Cards live in a scrollable page. Their contents define their natural height; shrinking
        # them is what used to turn queued jobs into thin, empty stripes.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.body = QVBoxLayout(self)
        self.body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        """Cards keep their complete natural layout; the owning page supplies scrolling."""
        return self.sizeHint()


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
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)  # carries worker text
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

    def set_thumbnail(self, image: QImage | None) -> None:
        """Show the item's picture in the square icon; ``None`` keeps the placeholder."""
        if image is None or image.isNull():
            return
        size = self.thumb.size()
        self.thumb.setPixmap(row_icon(image, size).pixmap(size))
        self.thumb.setText("")

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


class ElidedLabel(QLabel):
    """A one-line label that shows as much of its text as fits, then "…" (plan §5.8).

    The full text is kept, reported by ``full_text()`` and shown in the tooltip, and the size
    hint is the full text's width, so a layout gives it room up to what the text needs.
    """

    def __init__(self) -> None:
        super().__init__("")
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full = text
        self.setToolTip(plain_tooltip(text))
        self._elide()
        self.updateGeometry()

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.width())
        super().setText(shown)


class TitleEditor(QWidget):
    """The title as a label with a small ✎ (plan §5.5).

    One click turns it into a text box in the same place. Enter or clicking away saves, Esc
    cancels, and ↺ restores the original. The edited title only names the file; tags keep the
    source's real title.
    """

    edited = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._original = ""
        self._current = ""
        self._cancelled = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.label = ElidedLabel()
        self.label.setObjectName("resultTitle")
        self.label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setCursor(Qt.CursorShape.IBeamCursor)
        self.label.setToolTip("Click to change the file name")
        self.label.mousePressEvent = lambda _event: self.start_editing()  # type: ignore[method-assign]
        self.edit = _TitleLineEdit(self)
        self.edit.setMaxLength(300)
        self.edit.hide()
        self.pencil = QPushButton("✎")
        self.pencil.setObjectName("iconButton")
        self.pencil.setToolTip("Edit the title used for the file name")
        self.reset = QPushButton("↺")
        self.reset.setObjectName("iconButton")
        self.reset.setToolTip("Restore the original title")
        self.reset.hide()
        # The label takes the width its text needs, so ✎ and ↺ sit right after the title.
        row.addWidget(self.label)
        row.addWidget(self.edit, 1)
        row.addWidget(self.pencil, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.reset, 0, Qt.AlignmentFlag.AlignTop)
        self._spacer = QWidget()  # the rest of the row, hidden while the text box fills it
        row.addWidget(self._spacer, 1)
        self.pencil.clicked.connect(self.start_editing)
        self.reset.clicked.connect(self.restore)
        self.edit.editingFinished.connect(self._finish)

    def set_title(self, title: str) -> None:
        self._original = self._current = title
        self._show_label()

    def title(self) -> str:
        return self._current

    def edited_title(self) -> str | None:
        """The owner's title, or None while it is the original."""
        text = self._current.strip()
        return text if text and text != self._original else None

    def is_editing(self) -> bool:
        return not self.edit.isHidden()

    def start_editing(self) -> None:
        self._cancelled = False
        self.edit.setText(self._current)
        self.label.hide()
        self.pencil.hide()
        self._spacer.hide()
        self.edit.show()
        self.edit.setFocus()
        self.edit.selectAll()

    def cancel(self) -> None:
        self._cancelled = True
        self._show_label()

    def restore(self) -> None:
        self._current = self._original
        self._show_label()
        self.edited.emit(self._current)

    def _finish(self) -> None:
        if self.edit.isHidden():
            return
        if not self._cancelled:
            self._current = self.edit.text().strip() or self._original
            self.edited.emit(self._current)
        self._show_label()

    def _show_label(self) -> None:
        self.edit.hide()
        self.label.setText(self._current)
        self.label.show()
        self._spacer.show()
        self.pencil.show()
        self.reset.setVisible(self.edited_title() is not None)


class _TitleLineEdit(QLineEdit):
    def __init__(self, owner: TitleEditor) -> None:
        super().__init__()
        self._owner = owner

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self._owner.cancel()
            return
        super().keyPressEvent(event)


RESULT_TABS = {"video": "Video", "audio": "Audio", "image": "Image"}
ROW_COLUMNS = ("Format", "Quality", "Size", "")
VIDEO_CONTAINER_LABELS = (
    ("MP4", "mp4"),
    ("MKV", "mkv"),
    ("WebM", "webm"),
    ("MOV (re-encodes)", "mov"),
    ("AVI (re-encodes)", "avi"),
)
IMAGE_SAVE_LABELS = (("Original", "original"), ("JPG", "jpg"), ("PNG", "png"), ("WebP", "webp"))
_ROW_TEXT_LIMIT = 60


def _row_text(value: object) -> str:
    """One printable, single-line cell from a MediaResult field; "" for anything else."""
    if value is None or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    return "".join(ch for ch in text if ch.isprintable())[:_ROW_TEXT_LIMIT]


def _whole(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return int(value)
    return None


def row_size(row: dict) -> str:
    size = _whole(row.get("size"))
    if size is None:
        return "unknown"
    return ("~" if row.get("size_is_estimate") else "") + format_bytes(size)


def video_row_cells(row: dict, container: str) -> tuple[str, str]:
    """(Format, Quality) for a Video-tab row saved as ``container`` (plan §5.4)."""
    source = _row_text(row.get("container")).lower()
    if row.get("original"):
        quality = f"Original file (as served){' · ' + source.upper() if source else ''}"
        fmt = source.upper() if row.get("fixed_container") else container.upper()
        return fmt or "Original", quality
    height = _whole(row.get("height"))
    fps = _whole(row.get("fps"))
    parts = [f"{height}p{fps if fps and fps > 30 else ''}" if height else "Video"]
    if row.get("hdr") is True:
        parts.append("HDR")
    codec = _row_text(row.get("vcodec"))
    if codec:
        parts.append(codec)
    if codec == "H.264":
        parts.append("plays everywhere")
    elif codec == "AV1":
        parts.append("(not supported by older players)")
    quality = " · ".join(parts)
    if row.get("default") is True:
        quality += " ★"
    reencode = container in ("mov", "avi") or (container == "webm" and source != "webm")
    if reencode:
        quality += " (re-encodes, slower)"
    return container.upper(), quality


def audio_row_cells(row: dict) -> tuple[str, str]:
    """(Format, Quality) for an Audio-tab row (plan §5.4)."""
    fmt = _row_text(row.get("label")) or "Audio"
    codec = _row_text(row.get("codec")).lower()
    bitrate = _whole(row.get("bitrate"))
    if row.get("original"):
        quality = "Original file (as served)"
    elif codec == "mp3":
        quality = f"{bitrate} kbps" if bitrate else "MP3"
    elif codec in ("aac", "opus"):
        name = "AAC" if codec == "aac" else "Opus"
        if row.get("copy") is True:
            quality = f"{name} original (no re-encode)"
            if bitrate:
                quality += f" · ~{bitrate} kbps"
        else:
            quality = f"{name} {bitrate} kbps" if bitrate else name
    else:
        quality = _row_text(row.get("lossless_note")) or fmt
    if row.get("no_cover") is True and "cover" not in quality:
        quality += " · no embedded cover"
    if row.get("default") is True:
        quality += " ★"
    return fmt, quality


def image_row_cells(row: dict, fmt: str) -> tuple[str, str]:
    """(Format, Quality) for an Image-tab row saved as ``fmt`` (plan §5.4)."""
    width, height = _whole(row.get("width")), _whole(row.get("height"))
    ext = _row_text(row.get("ext")).upper()
    if width and height:
        quality = f"{width}×{height}"
    elif row.get("original"):
        quality = "Original image"
    else:
        quality = "Best available"
    if fmt == "original" and ext:
        quality += f" · {ext}"
    if row.get("default") is True:
        quality += " ★"
    return ("Original" if fmt == "original" else fmt.upper()), quality


PREVIEW_VIDEO = QSize(480, 270)
PREVIEW_MUSIC = QSize(300, 300)


def preview_pixmap(image: QImage, box: QSize, corner: str = "") -> QPixmap:
    """``image`` fitted inside ``box`` (letterboxed, never stretched) with ``corner`` text,
    such as the duration, drawn on a dark badge in the bottom-right corner (plan §5.6)."""
    canvas = QPixmap(box)
    canvas.fill(QColor("#133247"))
    scaled = image.scaled(
        box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage((box.width() - scaled.width()) // 2, (box.height() - scaled.height()) // 2,
                      scaled)  # fmt: skip
    if corner:
        font = QFont(painter.font())
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        text = painter.fontMetrics().boundingRect(corner)
        width, height = text.width() + 12, text.height() + 6
        x, y = box.width() - width - 8, box.height() - height - 8
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(x, y, width, height, 4, 4)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(x, y, width, height, Qt.AlignmentFlag.AlignCenter, corner)
    painter.end()
    return canvas


class ResultCard(Card):
    """One analyzed link (plan §5.8): preview, editable title, and Video / Audio / Image tabs
    of rows, each with its own Download button. Drawn only from the MediaResult."""

    download_requested = pyqtSignal(str, str)  # tab, row id

    def __init__(self) -> None:
        super().__init__()
        self._result: dict = {}
        self._rows: dict[str, list[dict]] = {}
        top = QHBoxLayout()
        top.setSpacing(14)
        self.cover = QLabel("🎞")
        self.cover.setFixedSize(PREVIEW_VIDEO)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet("background:#133247; color:#4cc2ff; border-radius:8px;")
        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        self.title_editor = TitleEditor()
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label = QLabel("")
        self.source_label.setObjectName("muted")
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.hide()
        self.playlist_label = QLabel("This link is part of a playlist.")
        self.playlist_label.setObjectName("muted")
        self.playlist_label.setWordWrap(True)
        self.playlist_label.setTextFormat(Qt.TextFormat.PlainText)
        self.playlist_label.hide()
        self.playlist_button = QPushButton("☰  Whole playlist…")
        self.playlist_button.setToolTip("Open the playlist and choose which songs to download")
        self.playlist_button.hide()
        # A widget, not a bare layout: an empty box layout (both children hidden) reports a
        # maximum width of 0, which clamped the whole title column to a sliver.
        playlist_box = QWidget()
        playlist_row = QHBoxLayout(playlist_box)
        playlist_row.setContentsMargins(0, 0, 0, 0)
        playlist_row.addWidget(self.playlist_label, 1)
        playlist_row.addWidget(self.playlist_button)
        for widget in (self.title_editor, self.meta_label, self.source_label, playlist_box):
            text_col.addWidget(widget)
        text_col.addStretch(1)
        top.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(text_col, 1)
        self.body.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)  # the pane's top border is the only line
        # The theme has no tab rules; without these the tabs are Fusion's light grey.
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; }}"
            f"QTabBar::tab {{ background: {SURFACE}; color: {TEXT_DIM};"
            f" border: 1px solid {BORDER}; border-bottom: none; padding: 6px 14px;"
            " margin-right: 4px; border-top-left-radius: 6px; border-top-right-radius: 6px; }"
            f"QTabBar::tab:selected {{ background: {SURFACE_HOVER}; color: {TEXT};"
            f" border-color: {ACCENT}; }}"
            f"QTabBar::tab:hover {{ color: {TEXT}; }}"
        )
        self.tables: dict[str, QTableWidget] = {}
        self.pages: dict[str, QWidget] = {}
        self.container_combo = QComboBox()
        for label, value in VIDEO_CONTAINER_LABELS:
            self.container_combo.addItem(label, value)
        self.container_combo.setToolTip(
            "MP4, MKV and WebM are a quick remux when the codecs allow it. "
            "MOV and AVI are re-encoded, which is slower."
        )
        self.image_format_combo = QComboBox()
        for label, value in IMAGE_SAVE_LABELS:
            self.image_format_combo.addItem(label, value)
        self.audio_note = QLabel("")
        self.audio_note.setObjectName("muted")
        self.audio_note.setWordWrap(True)
        self.audio_note.setTextFormat(Qt.TextFormat.PlainText)
        extras = {
            "video": ("Save video as", self.container_combo),
            "image": ("Save image as", self.image_format_combo),
            "audio": ("", self.audio_note),
        }
        for tab in RESULT_TABS:
            page = QWidget()
            box = QVBoxLayout(page)
            box.setContentsMargins(0, 8, 0, 0)
            box.setSpacing(6)
            caption, widget = extras[tab]
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            if caption:
                row.addStretch(1)
                row.addWidget(QLabel(caption))
            row.addWidget(widget, 0 if caption else 1)
            box.addLayout(row)
            table = QTableWidget(0, len(ROW_COLUMNS))
            table.setObjectName("formatTable")
            # The theme's 8px item padding also shrinks cell widgets, clipping the buttons.
            table.setStyleSheet("QTableWidget::item { padding: 0px 8px; }")
            table.setHorizontalHeaderLabels(list(ROW_COLUMNS))
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            table.setWordWrap(False)
            table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # set from the buttons
            box.addWidget(table)
            box.addStretch(1)  # spare height goes below the rows, never above them
            self.tables[tab] = table
            self.pages[tab] = page
        self.body.addWidget(self.tabs)
        self.container_combo.currentIndexChanged.connect(lambda _: self._fill("video"))
        self.image_format_combo.currentIndexChanged.connect(lambda _: self._fill("image"))

    # ── filling ───────────────────────────────────────────────────────────────────────────
    def set_preview(self, image: QImage | None, result: dict) -> None:
        """The MediaResult's picture: about 480×270 for video, 300×300 for music, with the
        duration over its corner. The placeholder shows only when there is no picture."""
        music = result.get("kind") == "audio"
        box = PREVIEW_MUSIC if music else PREVIEW_VIDEO
        self.cover.setFixedSize(box)
        if image is None or image.isNull():
            self.cover.setPixmap(QPixmap())
            self.cover.setText("🎵" if music else "🎞")
            return
        self.cover.setText("")
        self.cover.setPixmap(preview_pixmap(image, box, format_duration(result.get("duration"))))

    def set_result(self, result: dict, rows: dict[str, list[dict]]) -> None:
        """Show ``result``'s tabs, in its order, with the rows the page already checked."""
        self._result = result
        self._rows = rows
        self.container_combo.blockSignals(True)
        self.container_combo.setCurrentIndex(0)  # each link starts as MP4
        self.container_combo.blockSignals(False)
        self.image_format_combo.blockSignals(True)
        self.image_format_combo.setCurrentIndex(0)
        self.image_format_combo.blockSignals(False)
        self.tabs.clear()
        for tab in result.get("tabs") or []:
            if tab in RESULT_TABS and rows.get(tab):
                self._fill(tab)
                count = len(rows[tab])
                self.tabs.addTab(self.pages[tab], f"{RESULT_TABS[tab]} {count}")
        self.tabs.setVisible(self.tabs.count() > 0)
        if self.tabs.count():
            self.tabs.setCurrentIndex(0)

    def tab_names(self) -> list[str]:
        """The tabs shown, in order, as MediaResult tab names."""
        names = {page: tab for tab, page in self.pages.items()}
        return [names[self.tabs.widget(i)] for i in range(self.tabs.count())]

    def current_tab(self) -> str | None:
        names = self.tab_names()
        index = self.tabs.currentIndex()
        return names[index] if 0 <= index < len(names) else None

    def rows(self, tab: str) -> list[dict]:
        return list(self._rows.get(tab) or [])

    def container(self) -> str:
        return self.container_combo.currentData() or "mp4"

    def image_format(self) -> str:
        return self.image_format_combo.currentData() or "original"

    def download_button(self, tab: str, row: int) -> QPushButton | None:
        widget = self.tables[tab].cellWidget(row, 3)
        return widget if isinstance(widget, QPushButton) else None

    def cell_text(self, tab: str, row: int, column: int) -> str:
        item = self.tables[tab].item(row, column)
        return item.text() if item is not None else ""

    def _fill(self, tab: str) -> None:
        rows = self._rows.get(tab) or []
        table = self.tables[tab]
        table.setRowCount(len(rows))
        fixed = bool(rows) and all(r.get("fixed_container") is True for r in rows)
        self.container_combo.setEnabled(not fixed)
        for index, row in enumerate(rows):
            if tab == "video":
                fmt, quality = video_row_cells(row, self.container())
            elif tab == "audio":
                fmt, quality = audio_row_cells(row)
            else:
                fmt, quality = image_row_cells(row, self.image_format())
            for column, text in enumerate((fmt, quality, row_size(row))):
                item = QTableWidgetItem(text)
                item.setToolTip(plain_tooltip(text))
                table.setItem(index, column, item)
            button = table.cellWidget(index, 3)
            if not isinstance(button, QPushButton):
                button = QPushButton("⬇  Download")
                button.setObjectName("rowButton")  # the theme's compact in-table button
                table.setCellWidget(index, 3, button)
            try:
                button.clicked.disconnect()
            except TypeError:
                pass  # a new button has nothing connected yet
            row_id = str(row.get("id"))
            button.clicked.connect(
                lambda _=False, t=tab, r=row_id: self.download_requested.emit(t, r)
            )
        self._fit_buttons(table)
        height = table.horizontalHeader().height() + 4
        height += sum(table.rowHeight(r) for r in range(table.rowCount()))
        table.setFixedHeight(min(height, 420))

    @staticmethod
    def _fit_buttons(table: QTableWidget) -> None:
        """Rows and the button column sized from the (styled) Download buttons.

        Contents-sizing ignores cell widgets, and a button's size hint is only right once the
        stylesheet has been applied, so each button is polished before it is measured.
        """
        table.resizeRowsToContents()
        width = 0
        for index in range(table.rowCount()):
            button = table.cellWidget(index, 3)
            if button is None:
                continue
            button.ensurePolished()
            hint = button.sizeHint()
            width = max(width, hint.width())
            table.setRowHeight(index, max(table.rowHeight(index), hint.height() + 10, 34))
        if width:
            table.horizontalHeader().resizeSection(3, width + 24)  # 0 8px padding + grid

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        # The theme may only reach the buttons once the card is shown; measure again then.
        super().showEvent(event)
        for tab in self.tab_names():
            self._fill(tab)


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

    COLUMNS = ("", "#", "Title (click to edit)", "Artist", "Length", "")
    TITLE_COLUMN = 2

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
        self.table.setObjectName("playlistTable")  # compact rows for the name editors
        self.table.setIconSize(ROW_THUMB)
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Only the Title cell is editable (its item flags); a click on it starts editing.
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.CurrentChanged
        )
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
            cells = (str(entry.index), entry.title, entry.uploader)
            for column, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == self.TITLE_COLUMN:
                    item.setIcon(_placeholder_icon())
                    item.setData(Qt.ItemDataRole.UserRole, entry.title)
                    item.setToolTip("Click to change the file name")
                    if entry.selectable:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                if not entry.selectable:
                    item.setForeground(QColor("#8aa0b4"))
                self.table.setItem(row, column, item)
            for column, text in ((4, format_duration(entry.duration)), (5, entry.unavailable)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
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

    def set_thumbnail(self, row: int, image: QImage) -> None:
        item = self.table.item(row, self.TITLE_COLUMN)
        if item is not None and not image.isNull():
            item.setIcon(row_icon(image))

    def visible_rows(self) -> range:
        """Rows currently on screen (plus a few below), for lazy thumbnail loading."""
        count = self.table.rowCount()
        if not count:
            return range(0)
        first = max(self.table.rowAt(0), 0)
        last = self.table.rowAt(self.table.viewport().height() - 1)
        last = count - 1 if last < 0 else last
        return range(first, min(count, last + 6))

    def output_name(self, row: int) -> str | None:
        """The title the owner edited for ``row``, or ``None`` to keep the default name.

        An untouched (or restored) title is not a name: music keeps "Artist - Title".
        """
        item = self.table.item(row, self.TITLE_COLUMN)
        if item is None:
            return None
        text = item.text().strip()
        original = item.data(Qt.ItemDataRole.UserRole)
        return text if text and text != original else None


class MatchDialog(QDialog):
    """Change one Spotify song's recording: pick a looked-up result, or paste a YouTube link."""

    COLUMNS = ("Title", "Channel", "Length", "Diff", "Score")

    def __init__(self, track: SpotifyTrack, candidates=(), parent=None) -> None:
        super().__init__(parent)
        self.track = track
        self.candidates: tuple[Candidate, ...] = tuple(candidates)
        self.setWindowTitle("Choose the recording")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        heading = QLabel(f"{track.artist} — {track.title}" if track.artist else track.title)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setStyleSheet("font-weight:600;")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        info = QLabel(
            f"Spotify length {format_duration(track.duration)}. "
            f"Results with a ⚠ are uncertain: {UNCERTAIN_RULE}."
        )
        info.setObjectName("muted")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget(len(self.candidates), len(self.COLUMNS))
        self.table.setObjectName("matchTable")
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, cand in enumerate(self.candidates):
            match = Match(
                track_id=track.track_id,
                video_id=cand.video_id,
                duration_diff=duration_diff(cand.duration, track.duration),
                score=match_score(track, cand.title, cand.channel, cand.duration),
            )
            score = f"{match.score:.0f}%"
            cells = (
                cand.title or "Untitled",
                cand.channel,
                format_duration(cand.duration),
                format_diff(match.duration_diff),
                f"⚠ {score}" if is_uncertain(match) else score,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column < 2:
                    item.setToolTip(plain_tooltip(text))  # site-written text
                self.table.setItem(row, column, item)
        self.table.doubleClicked.connect(lambda _: self.accept())
        if self.candidates:
            layout.addWidget(self.table, 1)
        else:
            self.table.hide()
            none = QLabel("No other results were found for this song.")
            none.setObjectName("muted")
            layout.addWidget(none)

        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("…or paste a YouTube / YouTube Music link to one song")
        self.link_edit.setClearButtonEnabled(True)
        layout.addWidget(self.link_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this recording")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> Candidate | str | None:
        """A pasted link wins; otherwise the selected result; ``None`` when nothing was chosen."""
        text = self.link_edit.text().strip()
        if text:
            return text
        rows = self.table.selectionModel().selectedRows()
        return self.candidates[rows[0].row()] if rows else None


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
        self.grid.setObjectName("galleryGrid")
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
        self.image_format_combo = image_format_combo()
        controls.addWidget(QLabel("Save images as"))
        controls.addWidget(self.image_format_combo)
        self.body.addLayout(controls)

        buttons = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setObjectName("muted")
        self.download_button = QPushButton("⬇  Download selected (original quality)")
        self.download_button.setObjectName("primary")
        buttons.addWidget(self.selection_label, 1)
        buttons.addWidget(self.download_button)
        self.body.addLayout(buttons)
        self.image_format_combo.currentIndexChanged.connect(self._update_button_label)

    def _update_button_label(self) -> None:
        # "Original quality" stops being true once images are re-encoded.
        fmt = self.image_format_combo.currentData()
        suffix = "original quality" if fmt == "original" else f"images as {str(fmt).upper()}"
        self.download_button.setText(f"⬇  Download selected ({suffix})")

    def set_items(self, items: tuple[GalleryItem, ...] | list[GalleryItem]) -> None:
        self.grid.clear()
        for item in items:
            tile = QListWidgetItem(item.label)
            tile.setData(Qt.ItemDataRole.UserRole, item.index)
            tile.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            tile.setCheckState(Qt.CheckState.Checked)
            tile.setToolTip(plain_tooltip(item.label))  # a site-written name
            image = decode_image(item.preview) if item.preview else None
            if image is not None:
                tile.setIcon(
                    QIcon(
                        QPixmap.fromImage(image).scaled(
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


ROW_THUMB = QSize(48, 27)


def _placeholder_icon(size: QSize = ROW_THUMB) -> QIcon:
    pixmap = QPixmap(size)
    pixmap.fill(QColor("#133247"))
    return QIcon(pixmap)


def row_icon(image: QImage, size: QSize = ROW_THUMB) -> QIcon:
    """A row thumbnail: the image scaled to fill ``size`` and centre-cropped."""
    scaled = QPixmap.fromImage(image).scaled(
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - size.width()) // 2)
    y = max(0, (scaled.height() - size.height()) // 2)
    return QIcon(scaled.copy(x, y, size.width(), size.height()))


IMAGE_FORMAT_CHOICES = (
    ("Original format", "original"),
    ("JPG (transparency on white)", "jpg"),
    ("PNG", "png"),
)


def image_format_combo() -> QComboBox:
    combo = QComboBox()
    for label, value in IMAGE_FORMAT_CHOICES:
        combo.addItem(label, value)
    combo.setToolTip("Animated images keep only their first frame when converted.")
    return combo


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
        # "3 uncertain matches — …": counted before anything downloads (item 9).
        self.uncertain_label = QLabel("")
        self.uncertain_label.setObjectName("warning")
        self.uncertain_label.setWordWrap(True)
        self.uncertain_label.setTextFormat(Qt.TextFormat.PlainText)
        self.uncertain_label.hide()
        self.body.addWidget(self.uncertain_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setObjectName("spotifyTable")  # compact rows for the Change… buttons
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(260)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setIconSize(ROW_THUMB)
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
            change.setToolTip("Pick another YouTube Music result, or paste a link")
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
        if match.manual and not match.title:
            label = "Your link"
        else:
            label = match.title or "YouTube Music result"
            if match.channel:
                label = f"{label}  ·  {match.channel}"
            if match.manual:
                label = f"{label}  (your choice)"
        warn = is_uncertain(match)
        score = f"{match.score:.0f}%" if match.score is not None else "—"
        if warn:
            score = f"⚠ {score}"  # the row badge
        self._set_match_cells(row, label, format_diff(match.duration_diff), score, warn)
        item = self.table.item(row, self.MATCH_COLUMN)
        if item is not None:
            item.setIcon(_placeholder_icon())
            # The video id, not a link: one more copyable URL is one more way around the router.
            item.setToolTip(plain_tooltip(f"{label}\nYouTube video {match.video_id}"))
        badge = self.table.item(row, self.SCORE_COLUMN)
        if badge is not None:
            tip = f"Uncertain: {UNCERTAIN_RULE}. Use Change… to pick another." if warn else ""
            if match.confidence is not None:
                tip = f"{tip}\nspotDL's own score: {match.confidence:.0f}%".strip()
            badge.setToolTip(plain_tooltip(tip) if tip else "")

    def set_uncertain_count(self, count: int) -> None:
        if count:
            noun = "match" if count == 1 else "matches"
            self.uncertain_label.setText(
                f"⚠  {count} uncertain {noun} — {UNCERTAIN_RULE}. "
                "Check them with Change… before downloading."
            )
        self.uncertain_label.setVisible(bool(count))

    def set_thumbnail(self, row: int, image: QImage) -> None:
        item = self.table.item(row, self.MATCH_COLUMN)
        if item is not None and not image.isNull():
            item.setIcon(row_icon(image))

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
