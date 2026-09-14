"""Reusable themed widgets for the shell."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import set_state


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
    """One job row: thumbnail placeholder, title, stage chip, thin progress bar, details, cancel."""

    def __init__(self) -> None:
        super().__init__()
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

        top.addWidget(self.thumb)
        top.addLayout(text_col, 1)
        top.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
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
