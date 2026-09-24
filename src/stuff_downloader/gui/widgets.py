"""Reusable themed widgets for the shell."""

from __future__ import annotations

import html
from dataclasses import dataclass

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
)
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
from ..core.presets import BATCH_CHOICES
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


class FitLabel(QLabel):
    """A one-line label that paints "…" when it is short of room, and never asks for more.

    ``text()`` is always the whole text (tests and copy read it); only the painting is cut, so
    a long title can never push its card wider than the window (plan §5.8, P18).
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self.setToolTip(plain_tooltip(text) if text else "")

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        elide = Qt.TextElideMode.ElideRight
        shown = self.fontMetrics().elidedText(self.text(), elide, rect.width())
        painter.drawText(rect, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter), shown)


# Queue-card art: a video's 16:9 frame or a song's square cover, as on its playlist row.
QUEUE_ART_VIDEO = QSize(64, 36)
QUEUE_ART_MUSIC = QSize(36, 36)


def _icon_button(text: str, tip: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("queueIcon")
    button.setToolTip(tip)
    button.setFixedSize(28, 26)
    button.setStyleSheet("padding:0px;")
    return button


class JobCard(Card):
    """One job: art · title · state · percent · small icon buttons, then details and a thin bar.

    It is one row plus a progress bar (plan §5.8, P11); every text is cut to fit, so the card
    never needs more width than the queue has (P18). A row is draggable only while its job is
    still queued — a running, paused or finished job has no place in the pending order, so
    ``draggable`` stays False and both the drag and the drop are refused.
    """

    reorder_requested = pyqtSignal(str, str)  # dragged job id, the id of the row it was dropped on

    def __init__(self, music: bool = False) -> None:
        super().__init__()
        self.job_id = ""
        self.draggable = False
        self._press_pos = None
        self.setAcceptDrops(True)
        self.body.setContentsMargins(10, 6, 10, 6)
        self.body.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)

        self.thumb = QLabel("🎵" if music else "🎞")
        self.thumb.setFixedSize(QUEUE_ART_MUSIC if music else QUEUE_ART_VIDEO)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(f"background:{ART_PLACEHOLDER}; color:#4cc2ff; border-radius:4px;")

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        self.title_label = FitLabel("")
        self.title_label.setStyleSheet("font-weight:600;")
        self.title_label.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        copy = QAction("Copy title", self.title_label)
        copy.triggered.connect(lambda: QApplication.clipboard().setText(self.title_label.text()))
        self.title_label.addAction(copy)
        self.details_label = FitLabel("")
        self.details_label.setObjectName("muted")  # carries worker text: plain text only
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.details_label)

        self.chip = Chip("Idle")
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("muted")
        self.percent_label.setFixedWidth(36)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.open_button = _icon_button("▶", "Open the file")
        self.folder_button = _icon_button("📂", "Show in folder")
        self.retry_button = _icon_button("↻", "Try again")
        self.pause_button = _icon_button("⏸", "Pause; the partial file is kept")
        self.cancel_button = _icon_button("✕", "Cancel this job")
        for button in (self.retry_button, self.open_button, self.folder_button):
            button.hide()

        top.addWidget(self.thumb)
        top.addLayout(text_col, 1)
        top.addWidget(self.chip)
        top.addWidget(self.percent_label)
        for button in (
            self.open_button,
            self.folder_button,
            self.retry_button,
            self.pause_button,
            self.cancel_button,
        ):
            top.addWidget(button)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.body.addLayout(top)
        self.body.addWidget(self.progress)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        """Full height, but only the width the fixed parts need: the texts give way (P18)."""
        return QSize(super(Card, self).minimumSizeHint().width(), self.sizeHint().height())

    def set_state(self, text: str, state: str) -> None:
        self.chip.set(text, state)
        set_state(self.progress, state)

    def set_paused(self, paused: bool) -> None:
        """The pause button shows what a click does: pause a run, or resume a paused one."""
        self.pause_button.setText("▶" if paused else "⏸")
        self.pause_button.setToolTip(
            "Resume this job" if paused else "Pause; the partial file is kept"
        )

    def set_thumbnail(self, image: QImage | None) -> None:
        """Show the item's picture, cropped to the art box; ``None`` keeps the placeholder."""
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
        self.body.setContentsMargins(10, 6, 10, 6)
        self.body.setSpacing(3)
        top = QHBoxLayout()
        self.title_label = FitLabel(f"☰  Playlist: {title}")
        self.title_label.setStyleSheet("font-weight:600;")
        self.summary_label = QLabel(f"0 / {count} done")
        self.summary_label.setObjectName("muted")
        top.addWidget(self.title_label, 1)
        top.addWidget(self.summary_label, 0, Qt.AlignmentFlag.AlignTop)
        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, count))
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.body.addLayout(top)
        self.body.addWidget(self.progress)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(super(Card, self).minimumSizeHint().width(), self.sizeHint().height())

    def set_counts(self, done: int, total: int, failed: int = 0, skipped: int = 0) -> None:
        parts = [f"{done} / {total} done"]
        if skipped:
            parts.append(f"{skipped} already downloaded")
        if failed:
            parts.append(f"{failed} failed")
        self.summary_label.setText("  ·  ".join(parts))
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done + failed + skipped, max(1, total)))


# ── the shared track table (plan §5.6a, §5.8, §8 R5) ─────────────────────────────────────
ART_PLACEHOLDER = "#133247"
ROW_ART_VIDEO = QSize(96, 54)  # a video list: the 16:9 frame
ROW_ART_MUSIC = QSize(56, 56)  # a song list: the cover, centre-cropped square
HEADER_ART_VIDEO = QSize(192, 108)
HEADER_ART_MUSIC = QSize(160, 160)
ART_TOOLTIP_SIDE = 240


@dataclass(frozen=True)
class TrackRow:
    """What one table row shows. Every list — YouTube, YouTube Music, Spotify — is this."""

    index: int
    title: str
    artist: str = ""
    duration: float | None = None
    unavailable: str = ""  # why it cannot be ticked; "" when it can
    explicit: bool = False


def art_placeholder(size: QSize, music: bool) -> QPixmap:
    """The fixed grey box a row shows until its picture arrives, so rows never jump."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor(ART_PLACEHOLDER))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#4cc2ff"))
    font = QFont(painter.font())
    font.setPointSizeF(max(8.0, size.height() / 3.2))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "🎵" if music else "🎞")
    painter.end()
    return pixmap


def art_tooltip(image: QImage) -> str:
    """The row picture again at about 240 px, to check a cover without opening anything.

    The picture is our own decoded image, re-encoded here; nothing from the site is quoted.
    """
    scaled = image.scaled(
        ART_TOOLTIP_SIDE,
        ART_TOOLTIP_SIDE,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buffer, "PNG")
    encoded = bytes(data.toBase64()).decode("ascii")
    return f"<img src='data:image/png;base64,{encoded}' width='{scaled.width()}'>"


class TrackTable(QTableWidget):
    """Tick · # · picture · Title · Artist · Length · Status, plus any extra columns.

    One table for every list (plan §8 R5). Ticks are the model's own check states, drawn by the
    style in full, never checkbox widgets that a narrow cell can clip (P8). Cells are selectable
    and Ctrl+C copies them (P16). An editable Title is edited in place — click it once it is
    selected, double-click, or F2 — and the edit names the file only (plan §5.5, P4).
    """

    CHECK, INDEX, ART, TITLE, ARTIST, LENGTH, STATUS = range(7)
    BASE_COLUMNS = ("", "#", "", "Title", "Artist", "Length", "Status")
    EDIT_HINT = "Click the title again, double-click or press F2 to rename the file"

    checks_changed = pyqtSignal()

    EXPLICIT = "  🅴"

    def __init__(self, extra_columns: tuple[str, ...] = ()) -> None:
        columns = self.BASE_COLUMNS + extra_columns
        super().__init__(0, len(columns))
        self.setObjectName("trackTable")
        self.music = False
        self._art: dict[int, QImage] = {}
        labels = list(columns)
        labels[self.TITLE] = "Title (click to edit)"
        self.setHorizontalHeaderLabels(labels)
        self.verticalHeader().setVisible(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMinimumHeight(240)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(24)
        for column in range(len(columns)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.ARTIST, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(self.ARTIST, 150)
        header.setSectionResizeMode(self.ART, QHeaderView.ResizeMode.Fixed)
        self.set_music(False)
        self.itemChanged.connect(self._item_changed)

    # ── layout ───────────────────────────────────────────────────────────────────────────
    def art_size(self) -> QSize:
        return ROW_ART_MUSIC if self.music else ROW_ART_VIDEO

    def set_music(self, music: bool) -> None:
        """Square song art or 16:9 video art; the row height follows the picture."""
        self.music = bool(music)
        size = self.art_size()
        self.setIconSize(size)
        self.horizontalHeader().resizeSection(self.ART, size.width() + 12)
        self.verticalHeader().setMinimumSectionSize(size.height() + 8)
        self.verticalHeader().setDefaultSectionSize(size.height() + 8)

    # ── rows ─────────────────────────────────────────────────────────────────────────────
    def set_rows(self, rows: list[TrackRow] | tuple[TrackRow, ...], music: bool) -> None:
        self.blockSignals(True)
        self.clearContents()
        self._art = {}
        self.set_music(music)
        self.setRowCount(len(rows))
        placeholder = QIcon(art_placeholder(self.art_size(), self.music))
        for row, track in enumerate(rows):
            ok = not track.unavailable
            check = QTableWidgetItem()
            flags = Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable
            check.setFlags(flags | Qt.ItemFlag.ItemIsEnabled if ok else flags)
            check.setCheckState(Qt.CheckState.Checked if ok else Qt.CheckState.Unchecked)
            self.setItem(row, self.CHECK, check)
            art = QTableWidgetItem()
            art.setFlags(Qt.ItemFlag.ItemIsEnabled)
            art.setIcon(placeholder)
            self.setItem(row, self.ART, art)
            title = f"{track.title}{self.EXPLICIT}" if track.explicit else track.title
            cells = {
                self.INDEX: str(track.index),
                self.TITLE: title,
                self.ARTIST: track.artist,
                self.LENGTH: format_duration(track.duration),
                self.STATUS: track.unavailable,
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column in (self.TITLE, self.ARTIST) and text:
                    item.setToolTip(plain_tooltip(text))
                if column == self.TITLE:
                    item.setData(Qt.ItemDataRole.UserRole, title)
                    if ok:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                        item.setToolTip(plain_tooltip(f"{text}\n{self.EDIT_HINT}"))
                if not ok:
                    item.setForeground(QColor("#8aa0b4"))
                self.setItem(row, column, item)
        self.blockSignals(False)
        self.checks_changed.emit()

    def is_checkable(self, row: int) -> bool:
        item = self.item(row, self.CHECK)
        return item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)

    def is_checked(self, row: int) -> bool:
        item = self.item(row, self.CHECK)
        return item is not None and item.checkState() == Qt.CheckState.Checked

    def set_checked(self, row: int, checked: bool) -> None:
        item = self.item(row, self.CHECK)
        if item is not None and self.is_checkable(row):
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def checked_rows(self) -> list[int]:
        return [r for r in range(self.rowCount()) if self.is_checkable(r) and self.is_checked(r)]

    def set_all_checked(self, checked: bool) -> None:
        """Tick or untick every row the filter shows."""
        self.blockSignals(True)
        for row in range(self.rowCount()):
            if not self.isRowHidden(row):
                self.set_checked(row, checked)
        self.blockSignals(False)
        self.viewport().update()
        self.checks_changed.emit()

    def apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self.rowCount()):
            haystack = f"{self.cell_text(row, self.TITLE)} {self.cell_text(row, self.ARTIST)}"
            self.setRowHidden(row, bool(needle) and needle not in haystack.casefold())

    def cell_text(self, row: int, column: int) -> str:
        item = self.item(row, column)
        return item.text() if item is not None else ""

    def set_status(self, row: int, text: str, warn: bool = False, tip: str = "") -> None:
        self.set_cell(row, self.STATUS, text, warn, tip)

    def set_cell(self, row: int, column: int, text: str, warn: bool = False, tip: str = "") -> None:
        """A read-only, selectable cell. ``tip`` (or the text) is shown literally on hover."""
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        if warn:
            item.setForeground(QColor("#e0a040"))
        if tip or text:
            item.setToolTip(plain_tooltip(tip or text))
        self.setItem(row, column, item)

    def edited_title(self, row: int) -> str | None:
        """The title the owner typed for ``row``, or ``None`` to keep the default name.

        An untouched (or restored) title is not a name: music keeps "Artist - Title".
        """
        item = self.item(row, self.TITLE)
        if item is None:
            return None
        original = str(item.data(Qt.ItemDataRole.UserRole) or "")
        text = item.text()
        if original.endswith(self.EXPLICIT):  # the badge is ours, never part of a name
            original = original.removesuffix(self.EXPLICIT)
            text = text.removesuffix(self.EXPLICIT)
        text = text.strip()
        return text if text and text != original else None

    # ── pictures ─────────────────────────────────────────────────────────────────────────
    def set_art(self, row: int, image: QImage) -> None:
        item = self.item(row, self.ART)
        if item is None or image.isNull():
            return
        self._art[row] = image
        item.setIcon(row_icon(image, self.art_size()))
        item.setToolTip(art_tooltip(image))

    def has_art(self, row: int) -> bool:
        return row in self._art

    def art(self, row: int) -> QImage | None:
        """The picture ``row`` shows, once it has arrived."""
        return self._art.get(row)

    def visible_rows(self) -> range:
        """Rows on screen (plus a few below), for lazy picture loading."""
        count = self.rowCount()
        if not count:
            return range(0)
        first = max(self.rowAt(0), 0)
        last = self.rowAt(self.viewport().height() - 1)
        last = count - 1 if last < 0 else last
        return range(first, min(count, last + 6))

    # ── copy ─────────────────────────────────────────────────────────────────────────────
    def copy_selection(self) -> str:
        """Selected cells as tab-separated lines, like a spreadsheet; ticks and art skipped."""
        cells: dict[int, dict[int, str]] = {}
        for index in self.selectedIndexes():
            if index.column() in (self.CHECK, self.ART):
                continue
            cells.setdefault(index.row(), {})[index.column()] = self.cell_text(
                index.row(), index.column()
            )
        text = "\n".join(
            "\t".join(row[c] for c in sorted(row)) for _, row in sorted(cells.items())
        )
        if text:
            QApplication.clipboard().setText(text)
        return text

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        editing = self.state() == QAbstractItemView.State.EditingState
        if event.matches(QKeySequence.StandardKey.Copy) and not editing:
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == self.CHECK:
            self.checks_changed.emit()


class CollectionHeader(QWidget):
    """A list's own cover beside its title, owner and count, like the Result card (§5.6a)."""

    def __init__(self) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        self.cover = QLabel()
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.music = False
        self.has_cover = False
        text = QVBoxLayout()
        text.setSpacing(4)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-weight:600; font-size:12pt;")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("muted")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.title_label)
        text.addWidget(self.meta_label)
        text.addStretch(1)
        row.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)
        self.set_music(False)

    def art_size(self) -> QSize:
        return HEADER_ART_MUSIC if self.music else HEADER_ART_VIDEO

    def set_music(self, music: bool) -> None:
        """Back to the placeholder, square for songs and 16:9 for videos."""
        self.music = bool(music)
        self.cover.setFixedSize(self.art_size())
        self.cover.setPixmap(art_placeholder(self.art_size(), self.music))
        self.has_cover = False

    def set_cover(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            return
        self.cover.setPixmap(row_icon(image, self.art_size()).pixmap(self.art_size()))
        self.has_cover = True


def batch_combo() -> QComboBox:
    """"Download selected as": the §5.4 formats every YouTube entry has."""
    combo = QComboBox()
    for label, _tab, row_id, _container in BATCH_CHOICES:
        combo.addItem(label, row_id)
    return combo


def _add_download_rows(card: Card) -> None:
    """The rows under a track table: format, skip-existing and the download button.

    Three short rows rather than one long one, so a list still fits a 1280×720 window at
    150% scaling with no sideways scrolling (plan §5.8).
    """
    card.format_combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    card.format_combo.setMinimumContentsLength(20)
    formats = QHBoxLayout()
    formats.addWidget(QLabel("Download selected as:"))
    formats.addWidget(card.format_combo)
    formats.addStretch(1)
    card.body.addLayout(formats)
    card.body.addWidget(card.archive_check)
    buttons = QHBoxLayout()
    card.selection_label = QLabel("")
    card.selection_label.setObjectName("muted")
    card.download_button = QPushButton("⬇  Download selected")
    card.download_button.setObjectName("primary")
    buttons.addWidget(card.selection_label, 1)
    buttons.addWidget(card.download_button)
    card.body.addLayout(buttons)


class PlaylistCard(Card):
    """A YouTube or YouTube Music list: cover header, the track table, "Download selected as"."""

    TITLE_COLUMN = 3  # TrackTable.TITLE

    def __init__(self) -> None:
        super().__init__()
        self.header = CollectionHeader()
        self.title_label = self.header.title_label
        self.meta_label = self.header.meta_label
        self.body.addWidget(self.header)
        self.table = TrackTable()
        self.body.addWidget(self.table)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.setClearButtonEnabled(True)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.select_none_button)
        controls.addWidget(self.filter_edit, 1)
        self.body.addLayout(controls)

        self.format_combo = batch_combo()
        self.archive_check = QCheckBox("Skip songs already in this folder")
        self.archive_check.setChecked(True)
        _add_download_rows(self)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(0, self.sizeHint().height())

    def set_entries(self, entries, music: bool = False) -> None:
        """Fill the table. Unavailable entries are listed with their reason, never dropped."""
        rows = [
            TrackRow(e.index, e.title, e.uploader, e.duration, e.unavailable) for e in entries
        ]
        self.header.set_music(music)
        self.table.set_rows(rows, music)

    def selected_rows(self) -> list[int]:
        return self.table.checked_rows()

    def set_all_checked(self, checked: bool) -> None:
        self.table.set_all_checked(checked)

    def apply_filter(self, text: str) -> None:
        self.table.apply_filter(text)

    def set_thumbnail(self, row: int, image: QImage) -> None:
        self.table.set_art(row, image)

    def visible_rows(self) -> range:
        return self.table.visible_rows()

    def output_name(self, row: int) -> str | None:
        return self.table.edited_title(row)

    def set_download_count(self, count: int) -> None:
        self.selection_label.setText(f"{count} selected")
        self.download_button.setText(f"⬇  Download {count} selected")
        self.download_button.setEnabled(count > 0)


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
_KIND_GLYPH = {"image": "🖼", "video": "🎞", "audio": "🎵", "file": "📄"}


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
            # Videos and sounds carry their badge even with a picture (plan §5.4): a video's
            # preview is only its poster frame, and it always saves as the original file.
            badge = "" if item.kind == "image" else f"{_KIND_GLYPH.get(item.kind, '📄')} "
            tile = QListWidgetItem(badge + item.label)
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
    pixmap.fill(QColor(ART_PLACEHOLDER))
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
    ("WebP", "webp"),
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
    whole point of the match columns is that the audio comes from somewhere else. The rows are
    the shared track table; a row's picture is Spotify's art, never the YouTube match's (§5.6a).
    """

    EXTRA = ("YouTube match", "Diff", "Score", "")
    MATCH_COLUMN, DIFF_COLUMN, SCORE_COLUMN, CHANGE_COLUMN = 7, 8, 9, 10
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
        self.header = CollectionHeader()
        self.header.set_music(True)
        self.title_label = self.header.title_label
        self.meta_label = self.header.meta_label
        self.disclosure_label = QLabel(self.DISCLOSURE)
        self.disclosure_label.setWordWrap(True)
        self.disclosure_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(self.header)
        self.body.addWidget(self.disclosure_label)
        # "3 uncertain matches — …": counted before anything downloads (item 9).
        self.uncertain_label = QLabel("")
        self.uncertain_label.setObjectName("warning")
        self.uncertain_label.setWordWrap(True)
        self.uncertain_label.setTextFormat(Qt.TextFormat.PlainText)
        self.uncertain_label.hide()
        self.body.addWidget(self.uncertain_label)

        self.table = TrackTable(self.EXTRA)
        header = self.table.horizontalHeader()
        header.setSectionHidden(TrackTable.STATUS, True)
        header.setSectionResizeMode(self.MATCH_COLUMN, QHeaderView.ResizeMode.Stretch)
        # ResizeToContents measures items, not cell widgets, so the button column is sized here.
        header.setSectionResizeMode(self.CHANGE_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(self.CHANGE_COLUMN, 96)
        self.body.addWidget(self.table)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.select_none_button = QPushButton("Select none")
        self.match_button = QPushButton("🔎  Check matches")
        self.match_button.setToolTip(
            "Look up the YouTube recording for each selected song (about half a minute each)"
        )
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.select_none_button)
        controls.addWidget(self.match_button)
        controls.addStretch(1)
        self.body.addLayout(controls)

        self.archive_check = QCheckBox("Skip songs already in this folder")
        self.archive_check.setChecked(True)
        # Spotify songs are tagged MP3s (§7); other formats come with Spotify v2 (R6).
        self.format_combo = QComboBox()
        self.format_combo.addItem("MP3", "mp3")
        self.format_combo.setEnabled(False)
        _add_download_rows(self)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(0, self.sizeHint().height())

    def change_button(self, row: int) -> QPushButton | None:
        widget = self.table.cellWidget(row, self.CHANGE_COLUMN)
        return widget if isinstance(widget, QPushButton) else None

    def set_tracks(self, tracks: tuple[SpotifyTrack, ...] | list[SpotifyTrack]) -> None:
        rows = [
            TrackRow(t.index, t.title, t.artist, t.duration, explicit=t.explicit) for t in tracks
        ]
        self.table.set_rows(rows, music=True)
        for row in range(len(rows)):
            change = QPushButton("Change…")
            change.setObjectName("rowButton")
            change.setToolTip("Pick another YouTube Music result, or paste a link")
            change.clicked.connect(lambda _=False, r=row: self.change_requested.emit(r))
            self.table.setCellWidget(row, self.CHANGE_COLUMN, change)
            self.set_status(row, self.NOT_CHECKED, tip=self.NOT_CHECKED_TIP)

    def set_status(self, row: int, text: str, warn: bool = False, tip: str = "") -> None:
        """A row with no match to show: not checked yet, checking, or why none was found."""
        self._set_match_cells(row, text, "", "", warn)
        self.table.set_cell(row, self.MATCH_COLUMN, text, warn, tip or text)

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
        # The video id, not a link: one more copyable URL is one more way around the router.
        self.table.set_cell(
            row, self.MATCH_COLUMN, label, warn, f"{label}\nYouTube video {match.video_id}"
        )
        tip = f"Uncertain: {UNCERTAIN_RULE}. Use Change… to pick another." if warn else ""
        if match.confidence is not None:
            tip = f"{tip}\nspotDL's own score: {match.confidence:.0f}%".strip()
        self.table.set_cell(row, self.SCORE_COLUMN, score, warn, tip)
        if not tip:
            item = self.table.item(row, self.SCORE_COLUMN)
            if item is not None:
                item.setToolTip("")

    def set_uncertain_count(self, count: int) -> None:
        if count:
            noun = "match" if count == 1 else "matches"
            self.uncertain_label.setText(
                f"⚠  {count} uncertain {noun} — {UNCERTAIN_RULE}. "
                "Check them with Change… before downloading."
            )
        self.uncertain_label.setVisible(bool(count))

    def set_thumbnail(self, row: int, image: QImage) -> None:
        """Spotify's own art for the row. A YouTube match's picture never comes here."""
        self.table.set_art(row, image)

    def _set_match_cells(self, row: int, label: str, diff: str, score: str, warn: bool) -> None:
        for column, text in (
            (self.MATCH_COLUMN, label),
            (self.DIFF_COLUMN, diff),
            (self.SCORE_COLUMN, score),
        ):
            self.table.set_cell(row, column, text, warn)

    def cell_text(self, row: int, column: int) -> str:
        return self.table.cell_text(row, column)

    def selected_rows(self) -> list[int]:
        return self.table.checked_rows()

    def set_all_checked(self, checked: bool) -> None:
        self.table.set_all_checked(checked)

    def set_download_count(self, count: int) -> None:
        self.selection_label.setText(f"{count} selected")
        self.download_button.setText(f"⬇  Download {count} selected")
        self.download_button.setEnabled(count > 0)
