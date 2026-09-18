"""Pages: Downloads (analyze → preset → queue), History, Tools (health) and Settings."""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import (
    errors,
    formats,
    history,
    paths,
    playlist,
    presets,
    router,
    settings,
    tools,
)
from ..core import (
    scheduler as scheduling,
)
from ..core.protocol import Event, JobSpec
from ..core.runner import JobRun, WorkerRuntimeMissing
from . import theme
from .bridge import EventBridge
from .widgets import (
    Card,
    Chip,
    GroupCard,
    JobCard,
    PlaylistCard,
    PreviewCard,
    format_bytes,
    format_duration,
    format_eta,
    page_header,
    section_title,
    square_crop,
)

JOB_CHIPS = {
    "queued": "Queued",
    "retrying": "Retrying",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "paused": "Paused",
    "skipped": "Already downloaded",
}
# Only a failure that might genuinely go away on its own is retried. A private, removed or
# region-blocked video, a rejected format or a missing FFmpeg will fail identically three times
# in a row, so retrying those just hammers the site and delays an honest error message.
RETRYABLE_HINTS = (
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "unable to download webpage",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "connection reset",
    "connection aborted",
    "connection refused",
    "timed out",
    "read timeout",
)


def is_retryable(message: str | None) -> bool:
    """Whether an engine error looks transient enough to be worth an automatic retry."""
    text = (message or "").lower()
    return any(hint in text for hint in RETRYABLE_HINTS)


# ── notification hygiene ────────────────────────────────
# An OS toast outlives the app: it lands in the notification centre, where other people
# and other apps can read it. Everything that reaches one here is untrusted — titles come
# from the site, failure text comes from the engine — so it is redacted, not just trimmed.
NOTIFICATION_LINE_LIMIT = 80
NOTIFICATION_TITLE_LIMIT = 60
NOTIFICATION_LINES = 2
REDACTED = "[removed]"
FAILURE_SUMMARY = "The download did not finish. Open Stuff Downloader for the reason."

_CONTROL_CHARS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e]"
)
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# A location does not stop being one because a sentence wrapped it: "(example.com)" and
# "example.com." must be judged on the host inside, so every non-word edge is trimmed first.
_EDGE_NOISE = re.compile(r"^\W+|\W+$")
_HOSTISH = re.compile(r"^(?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,24})(?=$|[:/?#])")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?=$|[:/?#])")
# A port only counts as one when a path follows it, so a running time like 1:23 survives.
_HOST_PORT = re.compile(r"^[A-Za-z0-9-]+:\d{2,5}(?=[/?#])")
# A schemeless host still names where the file came from, and one path segment is enough
# to identify it, so cdn.example.com/private-video must not survive either.
COMMON_TLDS = frozenset(
    {
        "com", "net", "org", "io", "co", "tv", "me", "fm", "to", "cc",
        "ru", "de", "fr", "uk", "jp", "cn", "in", "br", "nl", "eu",
        "xyz", "info", "biz", "dev", "app", "site", "online", "live",
        "watch", "stream", "video", "media", "cloud", "link", "page",
    }
)
_FILE_SUFFIX = re.compile(r"[\\/][^\\/]*\.[A-Za-z0-9]{1,5}$")


def _is_host(probe: str) -> bool:
    """Whether an already-trimmed, lowercased token starts with somewhere you can reach."""
    host = _HOSTISH.match(probe)
    if host is not None and (len(probe) > host.end() or host.group(1) in COMMON_TLDS):
        return True
    return bool(_IPV4.match(probe) or _HOST_PORT.match(probe))


def _leaks_location(token: str) -> bool:
    """Whether one whitespace-separated token looks like a URL, a query string or a path."""
    if not token:
        return False
    lowered = token.lower()
    if "://" in lowered or lowered.startswith(("www.", "file:", "http:", "https:")):
        return True
    if "?" in token and "=" in token.split("?", 1)[1]:
        return True  # a query string carries ids, tokens and keys
    if "\\" in token or _DRIVE_PATH.match(token):
        return True  # any Windows path, UNC included
    if token.startswith(("/", "~/", "./", "../")):
        return True
    if token.startswith("[") and "]" in token and ":" in token:
        return True  # a bracketed IPv6 literal
    probe = _EDGE_NOISE.sub("", token).lower()
    if "@" in probe and _is_host(probe.rsplit("@", 1)[1]):
        return True  # userinfo@host hides the host from a start-anchored match
    if _is_host(probe):
        return True  # a schemeless URL: cdn.example.com/private-video, example.com
    return token.count("/") >= 2 or bool(_FILE_SUFFIX.search(token))


def safe_notification_line(text: object, limit: int = NOTIFICATION_LINE_LIMIT) -> str:
    """One toast line: no control characters, no locations, bounded length."""
    cleaned = _CONTROL_CHARS.sub(" ", str(text or ""))
    kept = [REDACTED if _leaks_location(token) else token for token in cleaned.split()]
    line = " ".join(kept).strip()
    if len(line) > limit:
        line = line[: max(1, limit - 1)].rstrip() + "…"
    return line


def safe_notification_body(message: object) -> str:
    """The whole toast body, line by line — the last gate before the OS sees it."""
    lines = [
        safe_notification_line(line)
        for line in str(message or "").splitlines()[:NOTIFICATION_LINES]
    ]
    return "\n".join(line for line in lines if line)


SUMMARY_ORDER = (
    ("active", "active"),
    ("queued", "queued"),
    ("retrying", "retrying"),
    ("completed", "done"),
    ("failed", "failed"),
    ("paused", "paused"),
    ("skipped", "skipped"),
    ("cancelled", "cancelled"),
)

STAGE_LABELS = {
    "analyzing": "Analyzing",
    "downloading": "Downloading",
    "downloading video": "Downloading video",
    "downloading audio": "Downloading audio",
    "merging": "Merging",
    "converting": "Converting",
    "tagging": "Tagging",
    "completed": "Completed",
}
ANALYZE_TIMEOUT_MS = 90_000
AUTO_HEIGHT = None


def _page_layout(widget: QWidget) -> QVBoxLayout:
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(32, 28, 32, 24)
    layout.setSpacing(14)
    return layout


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass
class QueuedJob:
    spec: JobSpec
    title: str
    card: JobCard
    run: Any = None
    # active | queued | retrying | paused | completed | failed | cancelled | skipped
    state: str = "active"
    files: list[Path] = field(default_factory=list)
    group_id: str = ""


@dataclass
class GroupState:
    card: GroupCard
    total: int
    title: str = ""
    done: int = 0
    failed: int = 0
    skipped: int = 0


class DownloadsPage(QWidget):
    # (title, message, state) for whoever owns a tray icon. The page never reaches for one
    # itself: it is constructed standalone in tests and must work without a window.
    notification_requested = pyqtSignal(str, str, str)

    def __init__(
        self, app_settings: settings.Settings, store: history.Store | None = None
    ) -> None:
        super().__init__()
        self._settings = app_settings
        # A store passed in belongs to the caller (MainWindow shares one with HistoryPage), so
        # only a store this page opened for itself is closed by shutdown().
        self.owns_store = store is None
        self.store = store if store is not None else history.Store()
        self.scheduler = scheduling.Scheduler(self._start_run, app_settings.max_concurrent)
        self._groups: dict[str, GroupState] = {}
        self._listing: playlist.Listing | None = None
        self._bridge = EventBridge(self)
        self._bridge.event_received.connect(self._on_event)
        self._analyze_run: Any = None
        self._analyze_job_id = ""
        self._analyze_timed_out = False
        self._route: router.Route | None = None
        self._info: dict[str, Any] = {}
        self._thumb: QPixmap | None = None
        self.jobs: dict[str, QueuedJob] = {}
        # Backoff timers are children of this page, so they die with it rather than firing into
        # a half-torn-down window.
        self._retry_timers: dict[str, QTimer] = {}
        self._analyze_timer = QTimer(self)
        self._analyze_timer.setSingleShot(True)
        self._analyze_timer.timeout.connect(self._analyze_timeout)

        layout = _page_layout(self)
        layout.addWidget(
            page_header("Downloads", "Paste a YouTube or YouTube Music link, then pick a format.")
        )

        paste_card = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlEdit")
        self.url_edit.setPlaceholderText("🔗  Paste a YouTube or YouTube Music link…")
        self.url_edit.setClearButtonEnabled(True)
        self.paste_button = QPushButton("Paste")
        self.paste_button.setToolTip("Paste a link from the clipboard")
        self.analyze_button = QPushButton("🔍  Analyze")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.setDefault(True)
        self.analyze_cancel_button = QPushButton("✕  Stop")
        self.analyze_cancel_button.hide()
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.paste_button)
        row.addWidget(self.analyze_button)
        row.addWidget(self.analyze_cancel_button)
        paste_card.body.addLayout(row)
        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.hide()
        paste_card.body.addWidget(self.message_label)
        self.folder_hint = QLabel()
        self.folder_hint.setObjectName("muted")
        paste_card.body.addWidget(self.folder_hint)
        layout.addWidget(paste_card)

        self.preview = PreviewCard()
        self.preview.hide()
        for preset in presets.PRESETS:
            self.preview.preset_combo.addItem(preset.label, preset.id)
        self.preview.preset_combo.setCurrentIndex(
            self.preview.preset_combo.findData(presets.DEFAULT_PRESET_ID)
        )
        layout.addWidget(self.preview)

        self.playlist_card = PlaylistCard()
        self.playlist_card.hide()
        for preset in presets.PRESETS:
            if preset.kind == "audio":
                self.playlist_card.preset_combo.addItem(preset.label, preset.id)
        self.playlist_card.preset_combo.setCurrentIndex(
            self.playlist_card.preset_combo.findData("mp3_music")
        )
        layout.addWidget(self.playlist_card)

        queue_header = QHBoxLayout()
        queue_header.addWidget(section_title("Queue"))
        queue_header.addStretch(1)
        self.queue_summary = QLabel("Nothing running")
        self.queue_summary.setObjectName("muted")
        self.pause_all_button = QPushButton("⏸  Pause all")
        self.pause_all_button.setObjectName("iconButton")
        queue_header.addWidget(self.queue_summary)
        queue_header.addWidget(self.pause_all_button)
        layout.addLayout(queue_header)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("emptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(20, 28, 20, 28)
        empty_text = QLabel("No downloads yet.\nPaste a link above and press Analyze.")
        empty_text.setObjectName("muted")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_state)

        self.queue_layout = QVBoxLayout()
        self.queue_layout.setSpacing(10)
        layout.addLayout(self.queue_layout)
        layout.addStretch(1)

        self.analyze_button.clicked.connect(self.analyze)
        self.url_edit.returnPressed.connect(self.analyze)
        self.paste_button.clicked.connect(self._paste)
        self.analyze_cancel_button.clicked.connect(self.cancel_analyze)
        self.preview.preset_combo.currentIndexChanged.connect(self._update_options)
        self.preview.crop_check.toggled.connect(self._update_cover)
        self.preview.download_button.clicked.connect(self.start_download)
        self.preview.playlist_button.clicked.connect(self.open_playlist)
        self.playlist_card.download_button.clicked.connect(self.start_playlist_download)
        self.playlist_card.select_all_button.clicked.connect(
            lambda: self._set_playlist_selection(True)
        )
        self.playlist_card.select_none_button.clicked.connect(
            lambda: self._set_playlist_selection(False)
        )
        self.playlist_card.filter_edit.textChanged.connect(self.playlist_card.apply_filter)
        self.pause_all_button.clicked.connect(self.pause_all)
        self.refresh_folder_hint()
        self.restore_unfinished()

    # ── helpers ──────────────────────────────────────────────────────────────────────────
    def refresh_folder_hint(self) -> None:
        self.folder_hint.setText(f"Saving to  {self._settings.effective_download_dir()}")

    def _paste(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        if text:
            self.url_edit.setText(text)

    def _show_message(self, text: str, error: bool = False) -> None:
        self.message_label.setText(text)
        self.message_label.setStyleSheet(f"color:{theme.DANGER};" if error else "")
        self.message_label.setVisible(bool(text))

    def _new_run(self, spec: JobSpec) -> Any:
        return JobRun(spec, self._bridge.post)

    def selected_preset(self) -> presets.Preset:
        return presets.get(self.preview.preset_combo.currentData())

    # ── analyze ──────────────────────────────────────────────────────────────────────────
    def analyze(self) -> None:
        if self._analyze_run is not None:
            return
        route = router.route(self.url_edit.text())
        if not route.ok:
            self.preview.hide()
            self._show_message(route.reason, error=True)
            return
        self._route = route
        self._info = {}
        self._listing = None
        self.preview.hide()
        self.playlist_card.hide()
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=route.engine,
            url=route.url,
            output_dir=str(self._settings.effective_download_dir()),
            options=(
                presets.playlist_options() if route.is_playlist else presets.analyze_options()
            ),
        )
        try:
            run = self._new_run(spec)
        except WorkerRuntimeMissing as exc:
            self._show_message(f"Cannot start the downloader: {exc}", error=True)
            return
        self._analyze_run = run
        self._analyze_job_id = spec.job_id
        self._analyze_timed_out = False
        self._show_message("Reading the playlist…" if route.is_playlist else "Analyzing link…")
        self.analyze_button.setEnabled(False)
        self.analyze_cancel_button.show()
        self._analyze_timer.start(ANALYZE_TIMEOUT_MS)
        run.start()

    def cancel_analyze(self) -> None:
        if self._analyze_run is not None:
            self._analyze_run.cancel()

    def _analyze_timeout(self) -> None:
        if self._analyze_run is not None:
            self._analyze_timed_out = True
            self._analyze_run.cancel()

    def _finish_analyze(self) -> None:
        self._analyze_timer.stop()
        self._analyze_run = None
        self._analyze_job_id = ""
        self.analyze_button.setEnabled(True)
        self.analyze_cancel_button.hide()

    def _on_analyze_event(self, event: Event) -> None:
        if not event.is_terminal:
            return
        timed_out = self._analyze_timed_out
        self._finish_analyze()
        if event.type == "error":
            code = "timeout" if timed_out else event.data.get("code")
            if code == "cancelled":
                self._show_message("")
                return
            self._show_message(errors.friendly_message(code, event.data.get("message")), error=True)
            return
        self._show_message("")
        if event.data.get("kind") == "playlist":
            self.show_playlist(event.data)
        else:
            self.show_preview(event.data)

    def show_preview(self, info: dict[str, Any]) -> None:
        self._info = info
        card = self.preview
        card.title_label.setText(str(info.get("title") or "Untitled"))
        meta = [
            str(info.get("artist") or info.get("uploader") or ""),
            format_duration(info.get("duration")),
        ]
        card.meta_label.setText("  ·  ".join(m for m in meta if m))
        route = self._route
        has_playlist = bool(route and route.playlist_id)
        refusal = route.playlist_reason if route else ""
        card.playlist_button.setVisible(has_playlist)
        card.playlist_label.setVisible(has_playlist or bool(refusal))
        if has_playlist:
            card.playlist_label.setText("This link is part of a playlist.")
        elif refusal:
            card.playlist_label.setText(refusal)

        self._thumb = None
        thumb = info.get("thumbnail")
        if isinstance(thumb, dict) and isinstance(thumb.get("data"), str):
            pixmap = QPixmap()
            try:
                if pixmap.loadFromData(base64.b64decode(thumb["data"], validate=True)):
                    self._thumb = pixmap
            except (binascii.Error, ValueError):
                pass

        raw_formats = info.get("formats")
        self._choices = formats.resolution_choices(
            raw_formats if isinstance(raw_formats, list) else []
        )
        if self._route and self._route.music:
            card.preset_combo.setCurrentIndex(card.preset_combo.findData("mp3_music"))
        self._update_options()
        card.show()

    def _update_options(self) -> None:
        preset = presets.get(self.preview.preset_combo.currentData())
        combo = self.preview.resolution_combo
        combo.clear()
        combo.addItem("Auto / Best", AUTO_HEIGHT)
        if preset.picks_resolution:
            for choice in getattr(self, "_choices", []):
                if preset.max_height is None or choice.height <= preset.max_height:
                    combo.addItem(choice.label, choice.height)
        combo.setEnabled(preset.picks_resolution)
        self.preview.compatible_check.setVisible(preset.picks_resolution)
        self.preview.crop_check.setVisible(preset.id in ("mp3_music", "thumbnail"))
        self._update_cover()

    def _update_cover(self) -> None:
        cover = self.preview.cover
        if self._thumb is None:
            cover.setPixmap(QPixmap())
            cover.setText("🎞")
            return
        preset = presets.get(self.preview.preset_combo.currentData())
        pixmap = self._thumb
        if preset.id in ("mp3_music", "thumbnail") and self.preview.crop_check.isChecked():
            pixmap = square_crop(pixmap)
        cover.setPixmap(
            pixmap.scaled(
                cover.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


    # ── playlists ────────────────────────────────────────────────────────────────────────
    def open_playlist(self) -> None:
        """The 'this song / whole playlist' choice: re-analyze the playlist this link is in."""
        if self._route is None or not self._route.playlist_url:
            return
        self.url_edit.setText(self._route.playlist_url)
        self.analyze()

    def show_playlist(self, data: dict[str, Any]) -> None:
        listing = playlist.parse_listing(data)
        self._listing = listing
        card = self.playlist_card
        card.title_label.setText(listing.title)
        meta = [listing.uploader, f"{len(listing.entries)} items"]
        if listing.truncated:
            meta.append(f"showing the first {playlist.MAX_ENTRIES}")
        card.meta_label.setText("  ·  ".join(m for m in meta if m))
        card.set_entries(listing.entries)
        card.filter_edit.clear()
        self._update_playlist_selection()
        for row in range(card.table.rowCount()):
            box = card.checkbox(row)
            if box is not None:
                box.toggled.connect(self._update_playlist_selection)
        self.preview.hide()
        card.show()

    def _set_playlist_selection(self, checked: bool) -> None:
        self.playlist_card.set_all_checked(checked)
        self._update_playlist_selection()

    def _update_playlist_selection(self) -> None:
        count = len(self.playlist_card.selected_rows())
        self.playlist_card.selection_label.setText(f"{count} selected")
        self.playlist_card.download_button.setEnabled(count > 0)

    def selected_playlist_entries(self) -> list[playlist.PlaylistEntry]:
        if self._listing is None:
            return []
        rows = self.playlist_card.selected_rows()
        return [self._listing.entries[r] for r in rows if r < len(self._listing.entries)]

    def start_playlist_download(self) -> list[QueuedJob]:
        listing = self._listing
        entries = self.selected_playlist_entries()
        if listing is None or not entries:
            return []
        output_dir = str(self._settings.effective_download_dir())
        specs = playlist.batch_specs(
            listing,
            entries,
            output_dir,
            self.playlist_card.preset_combo.currentData() or "mp3_music",
            archive=self.playlist_card.archive_check.isChecked(),
        )
        group_id = uuid.uuid4().hex
        source_url = self._route.playlist_url if self._route else ""
        self.store.add_group(group_id, listing.title, source_url, len(specs))
        group_card = GroupCard(listing.title, len(specs))
        self.queue_layout.insertWidget(0, group_card)
        self._groups[group_id] = GroupState(group_card, len(specs), listing.title)
        jobs = []
        for spec, entry in zip(specs, entries, strict=True):
            jobs.append(self._add_job(spec, entry.title, group_id))
        self.empty_state.hide()
        self.scheduler.submit_all(specs)
        self._update_summary()
        return jobs

    # ── queue ────────────────────────────────────────────────────────────────────────────
    def _new_card(self, title: str, job_id: str) -> JobCard:
        """A queue row wired to the actions for one job id."""
        job_card = JobCard()
        job_card.job_id = job_id
        job_card.title_label.setText(title)
        job_card.reorder_requested.connect(self.reorder_queue)
        job_card.cancel_button.clicked.connect(lambda: self.cancel_job(job_id))
        job_card.retry_button.clicked.connect(lambda: self.retry_job(job_id))
        job_card.pause_button.clicked.connect(lambda: self.toggle_pause(job_id))
        job_card.open_button.clicked.connect(lambda: self.open_file(job_id))
        job_card.folder_button.clicked.connect(lambda: self.show_in_folder(job_id))
        return job_card

    def _add_job(self, spec: JobSpec, title: str, group_id: str = "") -> QueuedJob:
        """Create the row and record the job as queued. The scheduler decides when it runs."""
        job_id = spec.job_id
        job_card = self._new_card(title, job_id)
        job = QueuedJob(spec, title, job_card, state="queued", group_id=group_id)
        self.jobs[job_id] = job
        self.queue_layout.insertWidget(0, job_card)
        job_card.set_state(JOB_CHIPS["queued"], "queued")
        job_card.set_draggable(True)
        job_card.details_label.setText(presets.get(spec.options["preset"]).label)
        self.store.add_job(
            job_id,
            spec.url,
            spec.engine,
            spec.options,
            spec.output_dir,
            title=title,
            group_id=group_id,
        )
        return job

    def _start_run(self, spec: JobSpec) -> None:
        """Called by the scheduler when a slot is free."""
        job = self.jobs.get(spec.job_id)
        if job is None:
            return
        job.spec = spec
        self._launch(job)

    def toggle_pause(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        if job.state == "paused":
            self.resume_job(job_id)
        elif job.state in ("active", "queued", "retrying"):
            self.pause_job(job_id)

    def pause_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.state not in ("active", "queued", "retrying"):
            return
        self._cancel_retry_timer(job_id)
        must_stop = self.scheduler.pause(job.spec)
        if must_stop and job.run is not None:
            job.card.set_state("Pausing", "active")
            job.run.cancel()
            return
        self._finish_job(job, "paused", "Paused — the partial file is kept")

    def resume_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.state != "paused":
            return
        job.card.set_state(JOB_CHIPS["queued"], "queued")
        job.card.set_draggable(True)
        job.state = "queued"
        self.store.set_state(job_id, "queued")
        if self.scheduler.resume(job_id) is None:
            self.scheduler.submit(job.spec)
        self._update_summary()

    def pause_all(self) -> None:
        active = {
            job_id: job.spec
            for job_id, job in self.jobs.items()
            if job.state in ("active", "queued", "retrying")
        }
        for spec in list(active.values()):
            self.pause_job(spec.job_id)

    def restore_unfinished(self) -> list[QueuedJob]:
        """Unfinished jobs from the last run come back paused. Nothing restarts on its own."""
        self.store.restore_unfinished()
        jobs = []
        for record in self.store.unfinished():
            if not isinstance(record.options.get("preset"), str):
                continue
            try:
                presets.get(record.options["preset"])
            except ValueError:
                continue
            spec = JobSpec(
                job_id=record.job_id,
                engine=record.engine,
                url=record.url,
                output_dir=record.output_dir,
                options=record.options,
            )
            title = record.title or record.url
            job = QueuedJob(
                spec,
                title,
                self._new_card(title, spec.job_id),
                state="paused",
                group_id=record.group_id,
            )
            self.jobs[spec.job_id] = job
            self.queue_layout.insertWidget(0, job.card)
            self.scheduler.submit_paused(spec)
            self._apply_finished_card(job, "paused", "Paused — resume to continue")
            jobs.append(job)
        if jobs:
            self.empty_state.hide()
            self._update_summary()
        return jobs

    # ── downloads ────────────────────────────────────────────────────────────────────────
    def start_download(self) -> QueuedJob | None:
        if self._route is None or not self._route.ok or not self._info:
            return None
        card = self.preview
        options = presets.download_options(
            card.preset_combo.currentData(),
            card.resolution_combo.currentData(),
            compatible=card.compatible_check.isChecked(),
            crop_cover=card.crop_check.isChecked(),
        )
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=self._route.engine,
            url=self._route.url,
            output_dir=str(self._settings.effective_download_dir()),
            options=options,
        )
        title = str(self._info.get("title") or self._route.url)
        job = self._add_job(spec, title)
        self.empty_state.hide()
        self.scheduler.submit(spec)
        return job

    def download_again(self, record: history.JobRecord) -> QueuedJob | None:
        """Queue a fresh job from a history row. The historical record is left untouched.

        The new job gets its own id and no group: it is a download of the same link, not a
        re-run of the playlist the original belonged to.
        """
        preset_id = record.options.get("preset")
        if not record.url or not isinstance(preset_id, str):
            return None
        try:
            presets.get(preset_id)
        except ValueError:
            return None
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=record.engine,
            url=record.url,
            output_dir=record.output_dir or str(self._settings.effective_download_dir()),
            options=dict(record.options),
        )
        job = self._add_job(spec, record.title or record.url)
        self.empty_state.hide()
        self.scheduler.submit(spec)
        self._update_summary()
        return job

    def _launch(self, job: QueuedJob) -> None:
        self.jobs[job.spec.job_id] = job
        card = job.card
        card.pause_button.setText("⏸  Pause")
        card.pause_button.show()
        job.files = []
        card.set_progress(0)
        card.retry_button.hide()
        card.open_button.hide()
        card.folder_button.hide()
        try:
            job.run = self._new_run(job.spec)
        except WorkerRuntimeMissing as exc:
            self.scheduler.finished(job.spec.job_id)
            self._finish_job(job, "failed", f"Cannot start the downloader: {exc}")
            return
        job.state = "active"
        card.set_draggable(False)
        card.set_state("Starting", "active")
        card.details_label.setText(presets.get(job.spec.options["preset"]).label)
        card.cancel_button.setEnabled(True)
        card.cancel_button.show()
        self.store.set_state(job.spec.job_id, "active")
        self._update_summary()
        job.run.start()

    def cancel_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.state not in ("active", "queued", "retrying", "paused"):
            return
        self._cancel_retry_timer(job_id)
        must_stop = self.scheduler.cancel(job_id)
        if must_stop and job.run is not None:
            job.card.set_state("Cancelling", "active")
            job.card.cancel_button.setEnabled(False)
            job.run.cancel()
            return
        self._finish_job(job, "cancelled", "Cancelled by you")

    def retry_job(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job is None or job.state == "active":
            if job is not None:
                self.jobs[job_id] = job
            return
        # A retry the owner asked for starts the backoff over: it must not eat an automatic
        # attempt, and the new job id would not carry the old one's count anyway.
        self._cancel_retry_timer(job_id)
        self.scheduler.reset_retries(job_id)
        old = job.spec
        job.spec = JobSpec(uuid.uuid4().hex, old.engine, old.url, old.output_dir, old.options)
        self.store.add_job(
            job.spec.job_id,
            job.spec.url,
            job.spec.engine,
            job.spec.options,
            job.spec.output_dir,
            title=job.title,
            group_id=job.group_id,
        )
        # Through the scheduler, not straight to _launch: otherwise the retried run is invisible
        # to it and a later cancel would leave the worker process running.
        self.jobs[job.spec.job_id] = job
        self.scheduler.submit(job.spec)

    # ── reordering ───────────────────────────────────────────────────────────────────────
    def reorder_queue(self, job_id: str, before_job_id: str) -> bool:
        """Move a queued job in front of another one. Returns True if the order changed.

        The whole queued order is handed to the scheduler and the store, not just the pair that
        moved: both permute only the slots the named jobs already hold, so a job that arrived
        while the drag was in flight keeps its place.
        """
        order = self.scheduler.queued_ids()
        if job_id == before_job_id or job_id not in order or before_job_id not in order:
            return False
        order.remove(job_id)
        order.insert(order.index(before_job_id), job_id)
        if not self.scheduler.reorder(order):
            return False
        self.store.set_queue_order(self.scheduler.queued_ids())
        self._relayout_queue()
        return True

    def _relayout_queue(self) -> None:
        """Redraw the rows so the queue reads the way it will run."""
        order = [job_id for job_id in self.scheduler.queued_ids() if job_id in self.jobs]
        cards = {job_id: self.jobs[job_id].card for job_id in order}
        widgets = [
            self.queue_layout.itemAt(i).widget() for i in range(self.queue_layout.count())
        ]
        widgets = [w for w in widgets if w is not None]
        queued_slots = [i for i, w in enumerate(widgets) if w in cards.values()]
        if len(queued_slots) != len(order):
            return
        # Rows are added newest-first, so the run order reads bottom-up.
        for slot, job_id in zip(queued_slots, reversed(order), strict=True):
            widgets[slot] = cards[job_id]
        for widget in widgets:
            self.queue_layout.addWidget(widget)  # re-adding moves it to the end, in order

    # ── retry backoff ────────────────────────────────────────────────────────────────────
    def _cancel_retry_timer(self, job_id: str) -> None:
        timer = self._retry_timers.pop(job_id, None)
        if timer is not None:
            timer.stop()

    def _start_backoff(self, job: QueuedJob, message: str, error_code: str) -> bool:
        """Park a failed job for an automatic retry. False means it has no retries left."""
        job_id = job.spec.job_id
        delay = self.scheduler.schedule_retry(job.spec)
        if delay is None:
            return False
        attempt = self.scheduler.attempts(job_id)
        job.state = "retrying"
        job.run = None
        card = job.card
        card.set_draggable(False)
        card.set_state(JOB_CHIPS["retrying"], "queued")
        card.details_label.setText(
            f"{message}  ·  retrying in {int(delay)}s ({attempt} of {scheduling.MAX_RETRIES})"
        )
        card.cancel_button.setVisible(True)
        card.cancel_button.setEnabled(True)
        card.pause_button.setVisible(True)
        card.pause_button.setText("⏸  Pause")
        card.retry_button.hide()
        card.open_button.hide()
        card.folder_button.hide()
        # The row is waiting to run again, so it is persisted as queued rather than failed: a
        # crash mid-backoff must bring it back as unfinished work, not as a finished failure.
        self.store.set_state(job_id, "queued", error_code=error_code, error_message=message)
        self._cancel_retry_timer(job_id)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._release_retry(job_id))
        self._retry_timers[job_id] = timer
        timer.start(int(delay * 1000))
        self._update_summary()
        return True

    def _release_retry(self, job_id: str) -> None:
        """The backoff elapsed — or the owner resumed early: put the job back in the queue."""
        # Stopped, not just forgotten: a timer released early is still armed, and one that
        # outlives this page fires into a torn-down window.
        self._cancel_retry_timer(job_id)
        job = self.jobs.get(job_id)
        if job is None or job.state != "retrying":
            return
        job.state = "queued"
        job.card.set_state(JOB_CHIPS["queued"], "queued")
        job.card.set_draggable(True)
        if self.scheduler.release_retry(job_id) is None:
            # Shutting down: leave the row queued rather than starting anything.
            self._update_summary()
            return
        self._update_summary()

    def _job_file(self, job_id: str) -> Path | None:
        job = self.jobs.get(job_id)
        if job is None or job.state != "completed":
            return None
        root = Path(job.spec.output_dir).resolve()
        for path in job.files:
            resolved = path.resolve()
            if resolved.is_file() and resolved.is_relative_to(root):
                return resolved
        return None

    def open_file(self, job_id: str) -> bool:
        path = self._job_file(job_id)
        return bool(path) and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_in_folder(self, job_id: str) -> bool:
        path = self._job_file(job_id)
        if path is None:
            return False
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", f"/select,{path}"])  # noqa: S603 (validated path)
            return True
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _apply_finished_card(self, job: QueuedJob, state: str, details: str) -> None:
        job.state = state
        job.run = None
        card = job.card
        card.set_draggable(False)
        card.set_state(JOB_CHIPS[state], state)
        card.details_label.setText(details)
        card.cancel_button.setVisible(state == "paused")
        card.cancel_button.setEnabled(True)
        card.pause_button.setVisible(state == "paused")
        card.pause_button.setText("▶  Resume")
        card.retry_button.setVisible(state in ("failed", "cancelled"))
        has_file = state == "completed" and self._job_file(job.spec.job_id) is not None
        card.open_button.setVisible(has_file)
        card.folder_button.setVisible(has_file)

    def _finish_job(
        self,
        job: QueuedJob,
        state: str,
        details: str,
        error_code: str = "",
        total_bytes: int = 0,
    ) -> None:
        self._apply_finished_card(job, state, details)
        self.store.set_state(
            job.spec.job_id,
            state,
            error_code=error_code,
            error_message=details if state == "failed" else "",
            total_bytes=total_bytes,
            files=[str(f) for f in job.files],
        )
        self._update_group(job)
        self._update_summary()
        self._notify_finished(job, state, details)

    def _notify_finished(self, job: QueuedJob, state: str, details: str) -> None:
        """One notification per finished thing — per playlist, not per track."""
        if state not in ("completed", "failed"):
            return
        group = self._groups.get(job.group_id)
        if group is None:
            title = "Download finished" if state == "completed" else "Download failed"
            # Engine failure text can name the file it was writing or the URL it fetched;
            # a fixed summary says the same thing to the notification centre safely.
            detail = FAILURE_SUMMARY if state == "failed" else safe_notification_line(details)
            name = safe_notification_line(job.title) or "Untitled"
            self.notification_requested.emit(title, f"{name}\n{detail}", state)
            return
        # A 40-track playlist must not produce 40 toasts: wait for its last entry.
        if group.done + group.failed + group.skipped < group.total:
            return
        parts = [f"{group.done} of {group.total} downloaded"]
        if group.failed:
            parts.append(f"{group.failed} failed")
        if group.skipped:
            parts.append(f"{group.skipped} already had")
        name = safe_notification_line(group.title) or "Untitled playlist"
        self.notification_requested.emit(
            "Playlist finished",
            f"{name}\n{'  ·  '.join(parts)}",
            "failed" if group.failed else "completed",
        )

    def _update_group(self, job: QueuedJob) -> None:
        group = self._groups.get(job.group_id)
        if group is None:
            return
        group.done = sum(
            1 for j in self.jobs.values() if j.group_id == job.group_id and j.state == "completed"
        )
        group.failed = sum(
            1
            for j in self.jobs.values()
            if j.group_id == job.group_id and j.state in ("failed", "cancelled")
        )
        group.skipped = sum(
            1 for j in self.jobs.values() if j.group_id == job.group_id and j.state == "skipped"
        )
        group.card.set_counts(group.done, group.total, group.failed, group.skipped)

    def _update_summary(self) -> None:
        counts: dict[str, int] = {}
        for job in self.jobs.values():
            counts[job.state] = counts.get(job.state, 0) + 1
        parts = [f"{counts[k]} {label}" for k, label in SUMMARY_ORDER if counts.get(k)]
        self.queue_summary.setText("  ·  ".join(parts) if parts else "Nothing running")
        self.pause_all_button.setEnabled(
            bool(counts.get("active") or counts.get("queued"))
        )

    def _on_event(self, event: Event) -> None:
        if self._analyze_job_id and event.job_id == self._analyze_job_id:
            self._on_analyze_event(event)
            return
        job = self.jobs.get(event.job_id)
        if job is None or job.state != "active":
            return
        card = job.card
        if event.type == "stage":
            stage = str(event.data.get("stage", ""))
            if stage != "completed":
                card.set_state(STAGE_LABELS.get(stage, stage.capitalize()), "active")
        elif event.type == "progress":
            data = event.data
            percent = data.get("percent")
            if _is_number(percent):
                card.set_progress(percent)
            done = format_bytes(data.get("downloaded_bytes"))
            parts = [f"{done} / {format_bytes(data.get('total_bytes'))}"]
            if _is_number(data.get("speed")):
                parts.append(f"{format_bytes(data['speed'])}/s")
            eta = format_eta(data.get("eta"))
            if eta:
                parts.append(eta)
            card.details_label.setText("  ·  ".join(parts))
        elif event.type == "result":
            self.scheduler.finished(event.job_id)
            if event.data.get("skipped"):
                job.files = []
                job.card.set_progress(100)
                reason = str(event.data.get("skipped_reason") or "Already downloaded")
                self._finish_job(job, "skipped", reason)
                return
            raw_files = event.data.get("files")
            job.files = (
                [Path(f) for f in raw_files if isinstance(f, str)]
                if isinstance(raw_files, list)
                else []
            )
            card.set_progress(100)
            total = event.data.get("total_bytes")
            details = f"Done  ·  {format_bytes(total)}"
            tags = event.data.get("tags")
            if isinstance(tags, dict) and tags.get("checked") and not tags.get("cover"):
                details += "  ·  no cover embedded"
            self._finish_job(
                job,
                "completed",
                details,
                total_bytes=int(total) if _is_number(total) else 0,
            )
        elif event.type == "error":
            self.scheduler.finished(event.job_id)
            code = event.data.get("code")
            intent = self.scheduler.take_intent(event.job_id)
            if intent == scheduling.PAUSE:
                self._finish_job(job, "paused", "Paused — the partial file is kept")
            elif intent == scheduling.CANCEL or code == "cancelled":
                self._finish_job(job, "cancelled", "Cancelled by you")
            else:
                message = errors.friendly_message(code, event.data.get("message"))
                raw = event.data.get("message")
                if not is_retryable(raw) or not self._start_backoff(
                    job, message, str(code or "")
                ):
                    self._finish_job(job, "failed", message, error_code=str(code or ""))

    def shutdown(self) -> None:
        self.scheduler.stop()
        for timer in list(self._retry_timers.values()):
            timer.stop()
        self._retry_timers.clear()
        if self._analyze_run is not None:
            self._analyze_run.cancel()
        for job in self.jobs.values():
            if job.state == "active" and job.run is not None:
                job.run.cancel()
        if self.owns_store:
            self.store.close()


HISTORY_STATE_LABELS = {
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "skipped": "Already downloaded",
}


class HistoryPage(QWidget):
    """Finished jobs: search, open the file, or remove the entry (never the file)."""

    download_again_requested = pyqtSignal(object)

    COLUMNS = ("Title", "Result", "Size", "When")

    def __init__(self, store: history.Store) -> None:
        super().__init__()
        self.store = store
        layout = _page_layout(self)
        layout.addWidget(
            page_header(
                "History",
                "Everything that finished. Removing an entry never deletes the file.",
            )
        )

        search_card = Card()
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍  Search by title or link…")
        self.search_edit.setClearButtonEnabled(True)
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        self.type_filter.addItem("Audio", "audio")
        self.type_filter.addItem("Video", "video")
        self.site_filter = QComboBox()
        self.site_filter.addItem("All sites", "")
        self.site_filter.addItem("YouTube", "youtube.com")
        self.site_filter.addItem("YouTube Music", "music.youtube.com")
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses", "")
        for state, label in HISTORY_STATE_LABELS.items():
            self.status_filter.addItem(label, state)
        self.refresh_button = QPushButton("Refresh")
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.type_filter)
        row.addWidget(self.site_filter)
        row.addWidget(self.status_filter)
        row.addWidget(self.refresh_button)
        search_card.body.addLayout(row)
        layout.addWidget(search_card)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.open_button = QPushButton("Open")
        self.folder_button = QPushButton("Show in folder")
        self.download_again_button = QPushButton("Download again")
        self.forget_button = QPushButton("Remove from history")
        self.empty_label = QLabel("Nothing here yet.")
        self.empty_label.setObjectName("muted")
        actions.addWidget(self.empty_label, 1)
        actions.addWidget(self.open_button)
        actions.addWidget(self.folder_button)
        actions.addWidget(self.download_again_button)
        actions.addWidget(self.forget_button)
        layout.addLayout(actions)

        self._records: list[history.JobRecord] = []
        self.search_edit.textChanged.connect(lambda _: self.refresh())
        self.type_filter.currentIndexChanged.connect(lambda _: self.refresh())
        self.site_filter.currentIndexChanged.connect(lambda _: self.refresh())
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh())
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(self.open_selected)
        self.folder_button.clicked.connect(self.show_selected_in_folder)
        self.download_again_button.clicked.connect(self.download_again_selected)
        self.forget_button.clicked.connect(self.forget_selected)
        self.refresh()

    def refresh(self) -> None:
        records = self.store.search(self.search_edit.text())
        selected_type = self.type_filter.currentData()
        selected_site = self.site_filter.currentData()
        selected_status = self.status_filter.currentData()
        self._records = [
            record
            for record in records
            if (not selected_type or self._record_type(record) == selected_type)
            and (not selected_site or (urlsplit(record.url).hostname or "").endswith(selected_site))
            and (not selected_status or record.state == selected_status)
        ]
        self.table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.updated_at))
            cells = (
                record.title or record.url,
                HISTORY_STATE_LABELS.get(record.state, record.state),
                format_bytes(record.total_bytes) if record.total_bytes else "—",
                when,
            )
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.empty_label.setVisible(not self._records)

    @staticmethod
    def _record_type(record: history.JobRecord) -> str:
        """Classify a saved job from its persisted preset without trusting arbitrary text."""
        try:
            return presets.get(record.preset).kind
        except ValueError:
            return ""

    def selected_record(self) -> history.JobRecord | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def selected_file(self) -> Path | None:
        """A file the record actually produced, inside the folder it was downloaded to."""
        record = self.selected_record()
        if record is None or record.state != "completed" or not record.output_dir:
            return None
        root = Path(record.output_dir).resolve()
        for raw in record.files:
            path = Path(raw).resolve()
            if path.is_file() and path.is_relative_to(root):
                return path
        return None

    def open_selected(self) -> bool:
        path = self.selected_file()
        return bool(path) and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_selected_in_folder(self) -> bool:
        path = self.selected_file()
        if path is None:
            return False
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", f"/select,{path}"])  # noqa: S603 (validated path)
            return True
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def forget_selected(self) -> bool:
        """Remove the row. The downloaded file stays where it is."""
        record = self.selected_record()
        if record is None:
            return False
        self.store.forget(record.job_id)
        self.refresh()
        return True

    def download_again_selected(self) -> bool:
        """Ask the owning window to enqueue a new job; the historical record stays untouched."""
        record = self.selected_record()
        if record is None:
            return False
        self.download_again_requested.emit(record)
        return True


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
    concurrency_changed = pyqtSignal(int)
    notifications_changed = pyqtSignal(bool)

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

        queue_card = Card()
        queue_card.body.addWidget(section_title("Queue"))
        queue_row = QHBoxLayout()
        queue_caption = QLabel("Downloads at the same time")
        queue_caption.setObjectName("muted")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(scheduling.MIN_CONCURRENT, scheduling.MAX_CONCURRENT)
        self.concurrency_spin.setValue(app_settings.max_concurrent)
        queue_row.addWidget(queue_caption)
        queue_row.addWidget(self.concurrency_spin)
        queue_row.addStretch(1)
        queue_card.body.addLayout(queue_row)
        layout.addWidget(queue_card)

        notify_card = Card()
        notify_card.body.addWidget(section_title("Notifications"))
        self.notifications_check = QCheckBox("Tell me when a download finishes")
        self.notifications_check.setChecked(app_settings.notifications)
        notify_card.body.addWidget(self.notifications_check)
        layout.addWidget(notify_card)
        layout.addStretch(1)

        change.clicked.connect(self._choose_folder)
        reset.clicked.connect(lambda: self.set_folder(""))
        self.concurrency_spin.valueChanged.connect(self.set_max_concurrent)
        self.notifications_check.toggled.connect(self.set_notifications)

    def set_max_concurrent(self, value: int) -> bool:
        """How many downloads run at once. Lowering it never stops a job already running."""
        value = max(scheduling.MIN_CONCURRENT, min(scheduling.MAX_CONCURRENT, int(value)))
        self._settings.max_concurrent = value
        try:
            settings.save(self._settings)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save settings", str(exc))
            return False
        self.concurrency_changed.emit(value)
        return True

    def set_notifications(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        self._settings.notifications = enabled
        try:
            settings.save(self._settings)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save settings", str(exc))
            return False
        self.notifications_changed.emit(enabled)
        return True

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
