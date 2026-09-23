"""Local image, video, and audio conversion page."""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.protocol import JobSpec
from ..core.runner import JobRun, WorkerRuntimeMissing
from .bridge import EventBridge
from .pages import _page_layout
from .widgets import Card, page_header, section_title


class ConvertersPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._run: JobRun | None = None
        self._job_id = ""
        self._temporary_path: Path | None = None
        self._output: Path | None = None
        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)

        layout = _page_layout(self)
        layout.addWidget(page_header("Converters", "Convert files already on your computer."))
        card = Card()
        card.body.addWidget(section_title("Image conversion"))
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText("Choose a local image")
        self.source_button = QPushButton("Choose image…")
        self.source_button.clicked.connect(self._choose_source)
        card.body.addLayout(self._path_row("Source", self.source_edit, self.source_button))

        self.format_combo = QComboBox()
        for label, code in (("JPEG", "jpg"), ("PNG", "png"), ("WebP", "webp"),
                            ("GIF", "gif"), ("BMP", "bmp")):
            self.format_combo.addItem(label, code)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        card.body.addWidget(QLabel("Output format"))
        card.body.addWidget(self.format_combo)

        self.destination_edit = QLineEdit()
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setPlaceholderText("Choose an output folder")
        self.destination_button = QPushButton("Choose folder…")
        self.destination_button.clicked.connect(self._choose_destination)
        card.body.addLayout(
            self._path_row("Destination", self.destination_edit, self.destination_button)
        )

        self.quality_label = QLabel("Quality (JPEG and WebP)")
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(90)
        card.body.addWidget(self.quality_label)
        card.body.addWidget(self.quality_spin)
        self.background_label = QLabel("JPEG background for transparency (#RRGGBB)")
        self.background_edit = QLineEdit("#ffffff")
        self.background_edit.setMaxLength(7)
        card.body.addWidget(self.background_label)
        card.body.addWidget(self.background_edit)
        self.animation_note = QLabel(
            "Animated GIF or WebP inputs keep only their first frame. GIF output is a still image."
        )
        self.animation_note.setWordWrap(True)
        self.animation_note.setObjectName("muted")
        card.body.addWidget(self.animation_note)

        actions = QHBoxLayout()
        self.convert_button = QPushButton("Convert image")
        self.convert_button.setObjectName("primary")
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_conversion)
        self.cancel_button.hide()
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self.open_folder)
        self.open_button.setEnabled(False)
        actions.addWidget(self.convert_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.open_button)
        actions.addStretch(1)
        card.body.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        card.body.addWidget(self.progress)
        self.status_label = QLabel("Choose an image and destination to begin.")
        self.status_label.setWordWrap(True)
        card.body.addWidget(self.status_label)
        layout.addWidget(card)
        self.video = VideoConverterCard()
        layout.addWidget(self.video)
        self.audio = AudioConverterCard()
        layout.addWidget(self.audio)
        layout.addStretch(1)
        self._format_changed()
        for signal in (self.source_edit.textChanged, self.destination_edit.textChanged,
                       self.background_edit.textChanged, self.quality_spin.valueChanged,
                       self.format_combo.currentIndexChanged):
            signal.connect(self._clear_result)

    def _clear_result(self) -> None:
        if self._run is None:
            self._output = None
            self.open_button.setEnabled(False)
            self.status_label.setText("Ready to convert.")

    @staticmethod
    def _path_row(label: str, edit: QLineEdit, button: QPushButton) -> QVBoxLayout:
        column = QVBoxLayout()
        column.addWidget(QLabel(label))
        column.addWidget(edit)
        button.setMaximumWidth(170)
        column.addWidget(button)
        return column

    def _choose_source(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Choose image", "", "Images (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.avif)"
        )
        if file_name:
            self.source_edit.setText(file_name)
            if not self.destination_edit.text():
                self.destination_edit.setText(str(Path(file_name).parent))

    def _choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if folder:
            self.destination_edit.setText(folder)

    def _format_changed(self) -> None:
        fmt = self.format_combo.currentData()
        quality = fmt in {"jpg", "webp"}
        self.quality_label.setVisible(quality)
        self.quality_spin.setVisible(quality)
        self.background_label.setVisible(fmt == "jpg")
        self.background_edit.setVisible(fmt == "jpg")

    def _set_running(self, running: bool) -> None:
        for widget in (self.source_button, self.destination_button, self.format_combo,
                       self.quality_spin, self.background_edit, self.convert_button):
            widget.setEnabled(not running)
        self.cancel_button.setVisible(running)
        self.progress.setVisible(running)

    def start_conversion(self) -> None:
        if self._run is not None:
            return
        source = Path(self.source_edit.text())
        destination = Path(self.destination_edit.text())
        if not source.is_file() or not destination.is_dir():
            self.status_label.setText("Choose an existing image and output folder.")
            return
        background = self.background_edit.text().strip()
        if self.format_combo.currentData() == "jpg" and (
            len(background) != 7 or not background.startswith("#")
            or any(c not in "0123456789abcdefABCDEF" for c in background[1:])
        ):
            self.status_label.setText("Enter a background colour such as #ffffff.")
            return
        self._job_id = uuid.uuid4().hex
        self._temporary_path = None
        self._output = None
        self.open_button.setEnabled(False)
        spec = JobSpec(
            self._job_id, "imageconvert", str(source), str(destination),
            {"format": self.format_combo.currentData(), "quality": self.quality_spin.value(),
             "background": background if self.format_combo.currentData() == "jpg" else "#ffffff"},
        )
        try:
            self._run = JobRun(spec, self._bridge.post)
            self._run.start()
        except (OSError, WorkerRuntimeMissing) as exc:
            self._run = None
            self.status_label.setText(f"Could not start conversion: {exc}")
            return
        self._set_running(True)
        self.status_label.setText("Converting image…")

    def cancel_conversion(self) -> None:
        if self._run is not None:
            self.status_label.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            threading.Thread(target=self._run.cancel, daemon=True).start()

    def _remove_temporary(self) -> None:
        path = self._temporary_path
        if path is None:
            return
        destination = Path(self.destination_edit.text())
        prefix = f".{Path(self.source_edit.text()).stem[:40]}.{self._job_id}."
        if path.parent == destination and path.name.startswith(prefix) and path.name.endswith(
            ".converting"
        ):
            shutil.rmtree(path, ignore_errors=True)

    def _on_event(self, event) -> None:
        if event.job_id != self._job_id:
            return
        if event.type == "stage":
            raw = event.data.get("temporary_path")
            if isinstance(raw, str):
                self._temporary_path = Path(raw)
        elif event.type in {"result", "error"}:
            self._set_running(False)
            self.cancel_button.setEnabled(True)
            if event.type == "result":
                files = event.data.get("files", [])
                if files and isinstance(files[0], str):
                    self._output = Path(files[0])
                    self.open_button.setEnabled(True)
                    note = event.data.get("note")
                    self.status_label.setText(
                        f"Done: {self._output}" + (f" — {note}" if note else "")
                    )
                else:
                    self.status_label.setText("Conversion finished without an output file.")
            else:
                self._remove_temporary()
                self.status_label.setText(str(event.data.get("message", "Conversion failed.")))
            self._run = None
            self._temporary_path = None

    def open_folder(self) -> None:
        if self._output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output.parent)))

    def shutdown(self) -> None:
        self.video.shutdown()
        self.audio.shutdown()
        if self._run is not None:
            self._run.cancel()
            self._run.wait(5)
            self._remove_temporary()
            self._run = None


class VideoConverterCard(Card):
    """Independent video job so the image controls retain their existing behavior."""

    def __init__(self) -> None:
        super().__init__()
        self._run: JobRun | None = None
        self._job_id = ""
        self._temporary_path: Path | None = None
        self._output: Path | None = None
        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)
        self.body.addWidget(section_title("Video conversion"))
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText("Choose a local video")
        self.source_button = QPushButton("Choose video…")
        self.source_button.clicked.connect(self._choose_source)
        self.body.addLayout(ConvertersPage._path_row(
            "Source", self.source_edit, self.source_button))
        self.format_combo = QComboBox()
        for label, code in (("MP4", "mp4"), ("MKV", "mkv"), ("AVI", "avi"),
                            ("MOV", "mov"), ("WebM", "webm"),
                            ("MPEG", "mpeg"), ("MPG", "mpg")):
            self.format_combo.addItem(label, code)
        self.body.addWidget(QLabel("Output format"))
        self.body.addWidget(self.format_combo)
        self.destination_edit = QLineEdit()
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setPlaceholderText("Choose an output folder")
        self.destination_button = QPushButton("Choose folder…")
        self.destination_button.clicked.connect(self._choose_destination)
        self.body.addLayout(ConvertersPage._path_row(
            "Destination", self.destination_edit, self.destination_button))
        actions = QHBoxLayout()
        self.convert_button = QPushButton("Convert video")
        self.convert_button.setObjectName("primary")
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_conversion)
        self.cancel_button.hide()
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self.open_folder)
        self.open_button.setEnabled(False)
        for button in (self.convert_button, self.cancel_button, self.open_button):
            actions.addWidget(button)
        actions.addStretch(1)
        self.body.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.body.addWidget(self.progress)
        self.status_label = QLabel("Choose a video and destination to begin.")
        self.status_label.setWordWrap(True)
        self.body.addWidget(self.status_label)
        for signal in (self.source_edit.textChanged, self.destination_edit.textChanged,
                       self.format_combo.currentIndexChanged):
            signal.connect(self._clear_result)

    def _clear_result(self) -> None:
        if self._run is None:
            self._output = None
            self.open_button.setEnabled(False)
            self.status_label.setText("Ready to convert.")

    def _choose_source(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Choose video", "", "Videos (*.mp4 *.mkv *.avi *.mov *.webm *.mpeg *.mpg)"
        )
        if name:
            self.source_edit.setText(name)
            if not self.destination_edit.text():
                self.destination_edit.setText(str(Path(name).parent))

    def _choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if folder:
            self.destination_edit.setText(folder)

    def _set_running(self, running: bool) -> None:
        for widget in (self.source_button, self.destination_button,
                       self.format_combo, self.convert_button):
            widget.setEnabled(not running)
        self.cancel_button.setVisible(running)
        self.progress.setVisible(running)

    def start_conversion(self) -> None:
        if self._run is not None:
            return
        source = Path(self.source_edit.text())
        destination = Path(self.destination_edit.text())
        if not source.is_file() or not destination.is_dir():
            self.status_label.setText("Choose an existing video and output folder.")
            return
        self._job_id = uuid.uuid4().hex
        self._temporary_path = None
        self._output = None
        self.open_button.setEnabled(False)
        spec = JobSpec(self._job_id, "videoconvert", str(source), str(destination),
                       {"format": self.format_combo.currentData()})
        try:
            self._run = JobRun(spec, self._bridge.post)
            self._run.start()
        except (OSError, WorkerRuntimeMissing) as exc:
            self._run = None
            self.status_label.setText(f"Could not start conversion: {exc}")
            return
        self._set_running(True)
        self.status_label.setText("Converting video…")

    def cancel_conversion(self) -> None:
        if self._run is not None:
            self.status_label.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            threading.Thread(target=self._run.cancel, daemon=True).start()

    def _remove_temporary(self) -> None:
        path = self._temporary_path
        if path is None:
            return
        destination = Path(self.destination_edit.text())
        prefix = f".{Path(self.source_edit.text()).stem[:40]}.{self._job_id}."
        if path.parent == destination and path.name.startswith(prefix) and path.name.endswith(
            ".converting"
        ):
            shutil.rmtree(path, ignore_errors=True)

    def _on_event(self, event) -> None:
        if event.job_id != self._job_id:
            return
        if event.type == "stage":
            raw = event.data.get("temporary_path")
            if isinstance(raw, str):
                self._temporary_path = Path(raw)
        elif event.type in {"result", "error"}:
            self._set_running(False)
            self.cancel_button.setEnabled(True)
            if event.type == "result":
                files = event.data.get("files", [])
                if files and isinstance(files[0], str):
                    self._output = Path(files[0])
                    self.open_button.setEnabled(True)
                    self.status_label.setText(f"Done: {self._output}")
                else:
                    self.status_label.setText("Conversion finished without an output file.")
            else:
                self._remove_temporary()
                self.status_label.setText(str(event.data.get("message", "Conversion failed.")))
            self._run = None
            self._temporary_path = None

    def open_folder(self) -> None:
        if self._output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output.parent)))

    def shutdown(self) -> None:
        if self._run is not None:
            self._run.cancel()
            self._run.wait(5)
            self._remove_temporary()
            self._run = None


class AudioConverterCard(Card):
    """Independent audio job so the image controls retain their existing behavior."""

    def __init__(self) -> None:
        super().__init__()
        self._run: JobRun | None = None
        self._job_id = ""
        self._temporary_path: Path | None = None
        self._output: Path | None = None
        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)
        self.body.addWidget(section_title("Audio conversion"))
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        self.source_edit.setPlaceholderText("Choose a local audio file")
        self.source_button = QPushButton("Choose audio…")
        self.source_button.clicked.connect(self._choose_source)
        self.body.addLayout(ConvertersPage._path_row(
            "Source", self.source_edit, self.source_button))
        self.format_combo = QComboBox()
        for label, code in (("MP3", "mp3"), ("M4A", "m4a"), ("AAC", "aac"),
                            ("Opus", "opus"), ("WAV", "wav"), ("FLAC", "flac")):
            self.format_combo.addItem(label, code)
        self.body.addWidget(QLabel("Output format"))
        self.body.addWidget(self.format_combo)
        self.destination_edit = QLineEdit()
        self.destination_edit.setReadOnly(True)
        self.destination_edit.setPlaceholderText("Choose an output folder")
        self.destination_button = QPushButton("Choose folder…")
        self.destination_button.clicked.connect(self._choose_destination)
        self.body.addLayout(ConvertersPage._path_row(
            "Destination", self.destination_edit, self.destination_button))
        actions = QHBoxLayout()
        self.convert_button = QPushButton("Convert audio")
        self.convert_button.setObjectName("primary")
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_conversion)
        self.cancel_button.hide()
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self.open_folder)
        self.open_button.setEnabled(False)
        for button in (self.convert_button, self.cancel_button, self.open_button):
            actions.addWidget(button)
        actions.addStretch(1)
        self.body.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.body.addWidget(self.progress)
        self.status_label = QLabel("Choose an audio file and destination to begin.")
        self.status_label.setWordWrap(True)
        self.body.addWidget(self.status_label)
        for signal in (self.source_edit.textChanged, self.destination_edit.textChanged,
                       self.format_combo.currentIndexChanged):
            signal.connect(self._clear_result)

    def _clear_result(self) -> None:
        if self._run is None:
            self._output = None
            self.open_button.setEnabled(False)
            self.status_label.setText("Ready to convert.")

    def _choose_source(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Choose audio", "",
            "Audio (*.mp3 *.m4a *.aac *.opus *.wav *.flac *.ogg *.wma *.aiff)"
        )
        if name:
            self.source_edit.setText(name)
            if not self.destination_edit.text():
                self.destination_edit.setText(str(Path(name).parent))

    def _choose_destination(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if folder:
            self.destination_edit.setText(folder)

    def _set_running(self, running: bool) -> None:
        for widget in (self.source_button, self.destination_button,
                       self.format_combo, self.convert_button):
            widget.setEnabled(not running)
        self.cancel_button.setVisible(running)
        self.progress.setVisible(running)

    def start_conversion(self) -> None:
        if self._run is not None:
            return
        source = Path(self.source_edit.text())
        destination = Path(self.destination_edit.text())
        if not source.is_file() or not destination.is_dir():
            self.status_label.setText("Choose an existing audio file and output folder.")
            return
        self._job_id = uuid.uuid4().hex
        self._temporary_path = None
        self._output = None
        self.open_button.setEnabled(False)
        spec = JobSpec(self._job_id, "audioconvert", str(source), str(destination),
                       {"format": self.format_combo.currentData()})
        try:
            self._run = JobRun(spec, self._bridge.post)
            self._run.start()
        except (OSError, WorkerRuntimeMissing) as exc:
            self._run = None
            self.status_label.setText(f"Could not start conversion: {exc}")
            return
        self._set_running(True)
        self.status_label.setText("Converting audio…")

    def cancel_conversion(self) -> None:
        if self._run is not None:
            self.status_label.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            threading.Thread(target=self._run.cancel, daemon=True).start()

    def _remove_temporary(self) -> None:
        path = self._temporary_path
        if path is None:
            return
        destination = Path(self.destination_edit.text())
        prefix = f".{Path(self.source_edit.text()).stem[:40]}.{self._job_id}."
        if path.parent == destination and path.name.startswith(prefix) and path.name.endswith(
            ".converting"
        ):
            shutil.rmtree(path, ignore_errors=True)

    def _on_event(self, event) -> None:
        if event.job_id != self._job_id:
            return
        if event.type == "stage":
            raw = event.data.get("temporary_path")
            if isinstance(raw, str):
                self._temporary_path = Path(raw)
        elif event.type in {"result", "error"}:
            self._set_running(False)
            self.cancel_button.setEnabled(True)
            if event.type == "result":
                files = event.data.get("files", [])
                if files and isinstance(files[0], str):
                    self._output = Path(files[0])
                    self.open_button.setEnabled(True)
                    self.status_label.setText(f"Done: {self._output}")
                else:
                    self.status_label.setText("Conversion finished without an output file.")
            else:
                self._remove_temporary()
                self.status_label.setText(str(event.data.get("message", "Conversion failed.")))
            self._run = None
            self._temporary_path = None

    def open_folder(self) -> None:
        if self._output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output.parent)))

    def shutdown(self) -> None:
        if self._run is not None:
            self._run.cancel()
            self._run.wait(5)
            self._remove_temporary()
            self._run = None
