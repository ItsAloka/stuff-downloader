"""Pages: Downloads (analyze → preset → queue), History, Tools (health) and Settings."""

from __future__ import annotations

import base64
import binascii
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QImage,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import (
    cookies,
    errors,
    gallery,
    history,
    paths,
    playlist,
    presets,
    router,
    settings,
    spotify,
    tools,
)
from ..core import (
    scheduler as scheduling,
)
from ..core.names import MAX_STEM, safe_output_name
from ..core.protocol import Event, JobSpec, ProtocolError, validate_media_result
from ..core.runner import JobRun, WorkerRuntimeMissing
from . import theme
from .bridge import EventBridge
from .thumbs import PREVIEW_BOX, ThumbnailLoader, decode_image, youtube_thumb_url
from .thumbs import allowed as thumbs_allowed
from .widgets import (
    Card,
    Chip,
    GalleryCard,
    GroupCard,
    JobCard,
    MatchDialog,
    PlaylistCard,
    ResultCard,
    SiteLoginDialog,
    SpotifyCard,
    art_placeholder,
    format_bytes,
    format_duration,
    format_eta,
    page_header,
    plain_tooltip,
    row_icon,
    section_title,
)

# "Clear done ▾" (plan §5.8): what each choice removes from the queue. History keeps all.
CLEAR_CHOICES = (
    ("finished", "Finished"),
    ("failed", "Cancelled && failed"),
    ("all", "Everything not running"),
)
CLEAR_STATES = {
    "finished": frozenset({"completed", "skipped"}),
    "failed": frozenset({"failed", "cancelled"}),
    "all": frozenset({"completed", "skipped", "failed", "cancelled"}),
}

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
    # The server closed the stream before sending anything (yt-dlp's http downloader).
    "did not get any data blocks",
)
# A 403 is retried only when it hit the media stream partway through: YouTube's stream URLs
# are short-lived, and a fresh attempt extracts a new one and usually succeeds (seen live).
# A 403 anywhere else, "unable to download webpage: HTTP Error 403" included, is a private or
# blocked page, and retrying it only hammers the site.
STREAM_403 = re.compile(r"unable to download video data|fragment \d+")
# yt-dlp's final words when a fragment kept failing (the 403 itself is only in the warnings).
FRAGMENT_GAVE_UP = re.compile(r"fragment \d+ not found, unable to continue")


def is_retryable(message: str | None) -> bool:
    """Whether an engine error looks transient enough to be worth an automatic retry."""
    text = (message or "").lower()
    if "http error 403" in text:
        return bool(STREAM_403.search(text))
    if FRAGMENT_GAVE_UP.search(text):
        return True
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
# This used to be a denylist: patterns for the host shapes we could think of, and a list of
# common TLDs. It failed seven times, because enumerating every way to write a location is a
# race you lose — IDN hosts, punycode, Cyrillic homoglyphs, a fullwidth dot, a trailing dot,
# percent-encoding, octal and decimal IPv4, and every TLD absent from the list all walked
# straight through. So it is an allowlist now, and it fails CLOSED: a token is emitted only if
# it is recognisably ordinary text, and anything else is redacted whether or not we can name
# what it is. Over-redacting a toast is a cosmetic bug; under-redacting one puts a download
# location on a lock screen and in Windows notification history.
#
# Structural characters a title has no business containing, and every URL, path and host does.
# Detection runs on an NFKC-normalised copy, so fullwidth ／ ： ？ and the ideographic full stop
# collapse onto the ASCII forms they imitate before any of this is checked.
_STRUCTURAL = frozenset('/\\:@?#%&=<>|"`')
# A dot immediately followed by a letter or digit is what every hostname looks like, in any
# script: example.com, 例え.テスト, examplе.com, xn--r8jz45g.xn--zckzah, 0300.0250.0.1.
# "Vol. 1" and a sentence's final full stop are not, because a space or the end follows.
_DOT_THEN_ALNUM = re.compile(r"\.\w", re.UNICODE)
# Two narrow exemptions, so the common toast text that is not a location still reads properly.
_CLOCK = re.compile(r"^\d{1,2}(?::\d{2}){1,2}$")  # a running time: 1:23, 1:23:45
_NUMBER = re.compile(r"^\d{1,4}(?:\.\d{1,2})?$")  # a count or a size: 12, 3.5
_EDGE_NOISE = re.compile(r"^\W+|\W+$")
# There is deliberately NO exemption for a slash. An earlier version allowed one for band names
# like AC/DC, gated on both sides being short letters and the token not being all lowercase. The
# security review rejected it: "SRV/x" and "Host/Path" satisfied that gate too, and a compact
# internal host and path is exactly what must never reach notification history. A band name
# rendering as [removed] in a toast is a cosmetic loss; a location on a lock screen is not.

# Every pattern here is anchored and free of nested quantifiers: this runs on the GUI thread,
# so a pathological input must not be able to make it backtrack.


def _is_plain_text(token: str) -> bool:
    """Whether one token is ordinary enough to show. Anything unrecognised is not."""
    probe = unicodedata.normalize("NFKC", token)
    # Punctuation on its own names nowhere: the "@" in "Live @ Wembley" is not a userinfo.
    if not any(char.isalnum() for char in probe):
        return True
    # The exemptions are matched on the token's core, so a trailing bracket in "(Set 1:23:45)"
    # does not turn a running time back into something unrecognised.
    core = _EDGE_NOISE.sub("", probe)
    if _CLOCK.match(core) or _NUMBER.match(core):
        return True
    if any(char in _STRUCTURAL for char in probe):
        return False
    return not _DOT_THEN_ALNUM.search(probe)


def _leaks_location(token: str) -> bool:
    """Whether one whitespace-separated token must not be shown. The inverse of the allowlist."""
    return bool(token) and not _is_plain_text(token)


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
# Spotify is read by scraping its web player at ~10 s a call; a long playlist pages through it.
SPOTIFY_ANALYZE_TIMEOUT_MS = 240_000
# Match lookups are short jobs, not queue rows. A few at a time: each one is a Spotify read and a
# YouTube Music search, and both sites throttle bursts.
MATCH_CONCURRENCY = 2
MATCH_TIMEOUT_MS = 180_000

# A Spotify download is its own preset (core.spotify.PRESET_ID). Every place that turns a stored
# preset id back into a label or a kind goes through _preset, so a Spotify job restores, shows
# and re-downloads like any other.
SPOTIFY_PRESET = presets.Preset(
    spotify.PRESET_ID,
    "MP3 — matched from YouTube, tagged from Spotify",
    "audio",
    None,
    "Spotify's metadata and cover on audio matched from YouTube Music",
)


def _preset(preset_id: Any) -> presets.Preset:
    """presets.get, plus the Spotify preset. Raises ValueError for anything unknown."""
    if preset_id == spotify.PRESET_ID:
        return SPOTIFY_PRESET
    return presets.get(preset_id)


def known_options(options: dict[str, Any]) -> bool:
    """True when saved job options name a download this version can run again."""
    if presets.is_row_options(options):
        try:
            presets.row_download_options(
                options.get("tab"),
                options.get("row_id"),
                options.get("container"),
                options.get("edited_title"),
            )
        except (TypeError, ValueError):
            return False
        return True
    try:
        _preset(options.get("preset"))
    except ValueError:
        return False
    return True


def job_label(options: dict[str, Any]) -> str:
    """What a job saves, for its queue card: the row it came from, or its preset."""
    if presets.is_row_options(options):
        return presets.row_label(options)
    return _preset(options.get("preset")).label


def job_kind(options: dict[str, Any]) -> str:
    """"video", "audio", "thumbnail", "file" or "gallery" for a job's options; "" if unknown."""
    if not known_options(options):
        return ""
    if presets.is_row_options(options):
        return presets.row_kind(options)
    return _preset(options.get("preset")).kind

# A generic link's query can be the signed token that makes it work, so history stores the
# link without it (see history.Store._migrate). Such a job can name the page but cannot be
# replayed, and the owner is told that plainly rather than handed a download that will fail.
LINK_REDACTED_REASON = (
    "This link had a private part that was not saved. Paste the original link again to download it."
)
AUTO_HEIGHT = None
# Engines that can use the owner's advanced site login (plan §6.4). The direct HTTP engine
# never gets one: a plain file link has no business needing a session. The social extractor never
# uses one; it only steps aside when one is set, so the engines that can use it read the post.
LOGIN_ENGINES = frozenset({"ytdlp", "gallerydl", "social"})

# The Advanced view (plan §M3): what the site actually offers, read from the metadata the worker
# already sanitized. Every cell is rebuilt here from a known field, never passed through, and the
# table is read-only — the download itself still goes through the preset above.
ADVANCED_COLUMNS = ("Kind", "Quality", "File", "Codecs", "Size")
ADVANCED_MAX_ROWS = 120
TITLE_LIMIT = 300
SITE_LIMIT = 40
_CELL_LIMIT = 40
_NO_CODEC = ("none", "", "null")


def _cell(value: Any, limit: int = _CELL_LIMIT) -> str:
    """One table cell: a short, single-line, printable string or an em dash."""
    if value is None or isinstance(value, bool):
        return "—"
    text = _CONTROL_CHARS.sub(" ", str(value)).strip()
    if not text:
        return "—"
    return text[:limit]


# Worker failure text is redacted in the engine for yt-dlp's own DownloadError, but an
# unexpected exception is reported as str(exc), and a network or HTTP error object routinely
# names what it was fetching. That text is stored in jobs.error_message and shown on the
# History page, so it is redacted again here — at the boundary that owns what reaches disk,
# not only in the process that happens to produce it.
#
# This reuses the notification allowlist rather than matching "scheme://", because the leak
# that prompted it had no scheme: urllib3 reports `Max retries exceeded with url: /v/9?sig=…`,
# a bare path and query. Deciding what is safe to keep beats listing what to remove.
ERROR_MESSAGE_LIMIT = 300


def safe_error_message(text: Any) -> str:
    """Failure text safe to store and show: no locations, no control characters, bounded."""
    cleaned = errors.redact_secrets(_CONTROL_CHARS.sub(" ", str(text or "")))
    kept = [REDACTED if _leaks_location(token) else token for token in cleaned.split()]
    return " ".join(kept).strip()[:ERROR_MESSAGE_LIMIT]


def safe_job_title(title: Any, url: str) -> str:
    """A title safe to store and show, for a link that may carry a token in its query.

    A job needs a name before the site has told us one, and the obvious name is the link. For
    a generic site that link can be the signed URL, and a title is written to history, shown in
    the queue and the History page, searched, and put in a tray notification — so the raw link
    must never become one. Anything carrying a scheme is refused as a title and replaced by the
    durable form, which has already lost its query.
    """
    text = _CONTROL_CHARS.sub(" ", str(title or "")).strip()
    if text and "://" not in text:
        return text[:TITLE_LIMIT]
    durable, _ = router.durable_url(url)
    return durable or "Untitled"


def _codec(value: Any) -> str:
    """A codec name without yt-dlp's profile suffix, or "" when there is no such stream."""
    text = str(value or "").split(".")[0].strip().lower()
    return "" if text in _NO_CODEC else text


def format_kind(fmt: dict[str, Any]) -> str:
    video, audio = _codec(fmt.get("vcodec")), _codec(fmt.get("acodec"))
    if video and audio:
        return "Video + audio"
    if video:
        return "Video only"
    if audio:
        return "Audio only"
    return "Other"


def _quality(fmt: dict[str, Any]) -> str:
    height, fps = fmt.get("height"), fmt.get("fps")
    if _is_number(height) and height:
        return f"{int(height)}p{int(fps)}" if _is_number(fps) and fps else f"{int(height)}p"
    for key in ("abr", "tbr"):
        value = fmt.get(key)
        if _is_number(value) and value:
            return f"{int(value)} kbps"
    return _cell(fmt.get("format_note"))


def _sort_key(fmt: dict[str, Any]) -> tuple[int, float, float]:
    order = {"Video + audio": 0, "Video only": 1, "Audio only": 2, "Other": 3}
    height = fmt.get("height") if _is_number(fmt.get("height")) else 0
    rate = fmt.get("tbr") if _is_number(fmt.get("tbr")) else 0
    return (order[format_kind(fmt)], -float(height or 0), -float(rate or 0))


def advanced_rows(raw: Any) -> list[tuple[str, ...]]:
    """One display row per format the site offers, best first. Never raises on odd input."""
    usable = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
    rows = []
    for fmt in sorted(usable, key=_sort_key)[:ADVANCED_MAX_ROWS]:
        codecs = " / ".join(c for c in (_codec(fmt.get("vcodec")), _codec(fmt.get("acodec"))) if c)
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        rows.append(
            (
                format_kind(fmt),
                _cell(_quality(fmt)),
                _cell(fmt.get("ext")).upper(),
                _cell(codecs),
                format_bytes(size) if _is_number(size) else "—",
            )
        )
    return rows


MAX_SUBTITLE_ROWS = 40
_LANG_CODE = re.compile(r"[A-Za-z0-9-]{1,20}")
_EXT_CODE = re.compile(r"[a-z0-9]{1,8}")


def _exts(track: dict[str, Any]) -> list[str]:
    raw = track.get("exts")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, str) and _EXT_CODE.fullmatch(e)]


def track_rows(info: Any) -> list[tuple[str, ...]]:
    """Subtitle, auto-caption and thumbnail rows for the All formats table (plan §5.5).

    Subtitles get one row per language. Automatic captions are machine translations into
    every language the site supports — often more than a hundred — so they are one summary row
    rather than a wall of near-identical ones. Nothing here is downloadable from the table.
    """
    if not isinstance(info, dict):
        return []
    rows: list[tuple[str, ...]] = []
    subs = info.get("subtitles")
    for track in (subs if isinstance(subs, list) else [])[:MAX_SUBTITLE_ROWS]:
        if not isinstance(track, dict):
            continue
        lang, exts = track.get("lang"), _exts(track)
        if isinstance(lang, str) and _LANG_CODE.fullmatch(lang) and exts:
            rows.append(("Subtitles", lang, " / ".join(exts).upper(), "—", "—"))
    auto = info.get("automatic_captions")
    langs = []
    auto_exts: list[str] = []
    for track in auto if isinstance(auto, list) else []:
        if isinstance(track, dict) and isinstance(track.get("lang"), str):
            if _LANG_CODE.fullmatch(track["lang"]):
                langs.append(track["lang"])
                auto_exts += [e for e in _exts(track) if e not in auto_exts]
    if langs:
        label = f"{len(langs)} languages" if len(langs) > 1 else langs[0]
        rows.append(("Auto captions", label, " / ".join(auto_exts[:4]).upper(), "—", "—"))
    thumbs = info.get("thumbnails")
    for thumb in (thumbs if isinstance(thumbs, list) else [])[:20]:
        if not isinstance(thumb, dict):
            continue
        width, height = thumb.get("width"), thumb.get("height")
        if _is_number(width) and _is_number(height) and width > 0 and height > 0:
            rows.append(("Thumbnail", f"{int(width)}×{int(height)}", "—", "—", "—"))
    return rows


def result_rows(info: dict[str, Any], tab: str) -> list[dict[str, Any]]:
    """A MediaResult tab's rows that name a download this version can queue."""
    rows = []
    for row in info.get(f"{tab}_rows") or []:
        try:
            presets.row_download_options(tab, row.get("id"))
        except (TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def source_audio_text(source: Any) -> str:
    """ "Source audio: Opus ~160 kbps" from a MediaResult, or "" when it is unknown."""
    if not isinstance(source, dict):
        return ""
    codec = _cell(source.get("codec"), 20)
    if codec in ("—", "unknown"):
        return ""
    kbps = source.get("abr_kbps")
    if _is_number(kbps) and kbps > 0:
        return f"Source audio: {codec} ~{int(kbps)} kbps"
    return f"Source audio: {codec}"


def _page_layout(widget: QWidget) -> QVBoxLayout:
    """Create a responsive page whose natural contents scroll instead of being compressed."""
    widget.setObjectName("page")
    outer = QVBoxLayout(widget)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(widget)
    scroll.setObjectName("pageScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    content = QWidget()
    content.setObjectName("pageContent")
    content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    scroll.setWidget(content)
    outer.addWidget(scroll)
    # Keep references both for tests and for callers that need to reveal a control later.
    widget.page_scroll = scroll
    widget.page_content = content
    layout = QVBoxLayout(content)
    layout.setContentsMargins(32, 28, 32, 24)
    layout.setSpacing(14)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    return layout


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


MAX_NOTES = 3
# The worker's notes are fixed sentences (image_convert); anything else is not shown.
KNOWN_NOTE = re.compile(
    r"Animated image: only the first frame was kept\."
    r"|Could not convert to (JPG|PNG); kept the original\."
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "skipped"})
# yt-dlp's in-progress and intermediate names: never what "Open" should open.
_TRANSIENT_FILE = re.compile(
    r"(\.part|\.ytdl|\.part-Frag\d+|\.temp\.\w+|\.f[\w-]+\.\w+)$", re.IGNORECASE
)


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


def unique_names(typed) -> dict:
    """``{key: file name}`` for the (key, typed title) pairs that name a file (plan §5.5).

    Untyped rows are left out, so they keep the default template. Two typed names that
    sanitize to the same file (``a:b`` and ``a?b``) get ``(2)``… here, so the queue shows what
    will be written; the worker still never overwrites.
    """
    names: dict = {}
    used: set[str] = set()
    for key, raw in typed:
        base = safe_output_name(raw)
        if base is None:
            continue
        name, n = base, 1
        while name.casefold() in used:
            n += 1
            suffix = f" ({n})"
            name = base[: MAX_STEM - len(suffix)].rstrip(" .") + suffix
        used.add(name.casefold())
        names[key] = name
    return names


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
        self.scheduler = scheduling.Scheduler(
            self._start_run,
            app_settings.max_concurrent,
            group_of=lambda spec: router.social_group(spec.url),
        )
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
        # Row and queue-card thumbnails. Only URLs built from validated video ids are fetched.
        self.thumbs = ThumbnailLoader(parent=self)
        self.thumbs.loaded.connect(self._on_thumbnail)
        self._playlist_thumb_urls: list[str | None] = []
        self._playlist_cover_url: str | None = None
        self._job_thumbs: dict[str, str] = {}  # job id -> thumbnail url
        self._analyze_timer = QTimer(self)
        self._analyze_timer.setSingleShot(True)
        self._analyze_timer.timeout.connect(self._analyze_timeout)

        layout = _page_layout(self)
        layout.addWidget(
            page_header("Downloads", "Paste a public video link, then pick a format.")
        )

        paste_card = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("urlEdit")
        self.url_edit.setPlaceholderText(
            "🔗  Paste a link — YouTube, a Spotify song or playlist, or another site…"
        )
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
        # Advanced and hidden (plan §6.4): shown only after a failure a site login could fix.
        self.login_button = QPushButton("Advanced: use a site login…")
        self.login_button.setToolTip(
            "Only for media the site shows to signed-in viewers. Public links never need this."
        )
        self.login_button.hide()
        self._login_site = ""
        # Set when the offer came from a failed queue row: saving a login retries that job
        # instead of re-analyzing whatever is in the link box.
        self._login_retry_job_id = ""
        # Engines still to try when the one a link was routed to says "unsupported" (plan §5.3
        # steps 3-5): a page yt-dlp cannot read may be a gallery, or the media file itself.
        self._fallbacks: list[router.Route] = []
        self._gallery: gallery.Gallery | None = None
        # Spotify (plan §6.3): the listing, the matches known so far (track id -> Match), and
        # the match lookups waiting and running. A lookup is a short job, never a queue row.
        self._spotify: spotify.SpotifyListing | None = None
        self._spotify_matches: dict[str, spotify.Match] = {}
        self._match_waiting: list[spotify.SpotifyTrack] = []
        self._match_runs: dict[str, tuple[Any, spotify.SpotifyTrack, QTimer]] = {}
        # Spotify v2 (plan §7 item 4). A ticked song with no match yet is looked up first and
        # downloads as soon as a certain match arrives: _spotify_pending maps it to the queue
        # group it joins. An uncertain one waits in _spotify_held until the owner reviews it or
        # asks for all of them. _spotify_sent is what this listing has already queued.
        self._spotify_pending: dict[str, str] = {}
        self._spotify_held: set[str] = set()
        self._spotify_sent: set[str] = set()
        self._spotify_archive = True
        # Each row's picture: Spotify's own, from the listing or the track's oEmbed lookup.
        self._spotify_art_urls: list[str] = []
        self._spotify_cover_url: str | None = None
        login_row = QHBoxLayout()
        login_row.setContentsMargins(0, 0, 0, 0)
        login_row.addWidget(self.login_button)
        login_row.addStretch(1)
        paste_card.body.addLayout(login_row)
        self.folder_hint = QLabel()
        self.folder_hint.setObjectName("muted")
        paste_card.body.addWidget(self.folder_hint)
        layout.addWidget(paste_card)

        self.result_card = ResultCard()
        self.result_card.hide()
        self._build_advanced_section()
        layout.addWidget(self.result_card)

        self.playlist_card = PlaylistCard()
        self.playlist_card.hide()
        self.playlist_card.table.verticalScrollBar().valueChanged.connect(
            self._request_visible_playlist_thumbs
        )
        self.playlist_card.table.checks_changed.connect(self._update_playlist_selection)
        layout.addWidget(self.playlist_card)

        self.gallery_card = GalleryCard()
        self.gallery_card.hide()
        layout.addWidget(self.gallery_card)

        self.spotify_card = SpotifyCard()
        self.spotify_card.hide()
        self.spotify_card.table.checks_changed.connect(self._update_spotify_selection)
        self.spotify_card.table.verticalScrollBar().valueChanged.connect(
            self._request_visible_spotify_art
        )
        layout.addWidget(self.spotify_card)

        queue_header = QHBoxLayout()
        queue_header.addWidget(section_title("Queue"))
        self.queue_summary = QLabel("Nothing running")
        self.queue_summary.setObjectName("muted")
        self.queue_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.pause_all_button = QPushButton("⏸  Pause all")
        self.pause_all_button.setObjectName("iconButton")
        self.cancel_remaining_button = QPushButton("✕  Cancel remaining")
        self.cancel_remaining_button.setObjectName("iconButton")
        self.cancel_remaining_button.setToolTip("Cancel every job that has not finished")
        # "Clear done ▾" (plan §5.8): a click clears finished jobs; the arrow offers the rest.
        self.clear_queue_button = QToolButton()
        self.clear_queue_button.setText("Clear done")
        self.clear_queue_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.clear_menu = QMenu(self.clear_queue_button)
        self.clear_actions: dict[str, QAction] = {}
        for which, label in CLEAR_CHOICES:
            action = self.clear_menu.addAction(label)
            action.triggered.connect(lambda _=False, w=which: self.clear_finished_jobs(w))
            self.clear_actions[which] = action
        self.clear_queue_button.setMenu(self.clear_menu)
        self.clear_queue_button.setEnabled(False)
        queue_header.addWidget(self.queue_summary, 1)
        queue_header.addWidget(self.pause_all_button)
        queue_header.addWidget(self.cancel_remaining_button)
        queue_header.addWidget(self.clear_queue_button)
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
        self.login_button.clicked.connect(self.offer_site_login)
        self.result_card.download_requested.connect(self.start_row_download)
        self.result_card.playlist_button.clicked.connect(self.open_playlist)
        self.clear_queue_button.clicked.connect(lambda: self.clear_finished_jobs("finished"))
        self.cancel_remaining_button.clicked.connect(self.cancel_remaining)
        self.playlist_card.download_button.clicked.connect(self.start_playlist_download)
        self.gallery_card.download_button.clicked.connect(self.start_gallery_download)
        self.gallery_card.select_all_button.clicked.connect(
            lambda: self._set_gallery_selection(True)
        )
        self.gallery_card.select_none_button.clicked.connect(
            lambda: self._set_gallery_selection(False)
        )
        self.gallery_card.grid.itemChanged.connect(lambda _: self._update_gallery_selection())
        self.spotify_card.download_button.clicked.connect(self.start_spotify_download)
        self.spotify_card.match_button.clicked.connect(self.check_spotify_matches)
        self.spotify_card.change_requested.connect(self.change_spotify_match)
        self.spotify_card.uncertain_button.clicked.connect(self.download_uncertain_anyway)
        self.spotify_card.select_all_button.clicked.connect(
            lambda: self._set_spotify_selection(True)
        )
        self.spotify_card.select_none_button.clicked.connect(
            lambda: self._set_spotify_selection(False)
        )
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

    # ── advanced formats ─────────────────────────────────────────────────────────────────
    def _build_advanced_section(self) -> None:
        """The "All formats" disclosure, added to the bottom of the Result card.

        It is filled from the analyze result and is read-only: it reports every stream the site
        offers. Downloads come from the Video / Audio / Image rows above it.
        """
        self.advanced_button = QPushButton("▸  All formats")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolTip("Show every format this site offers for this video")
        self.advanced_note = QLabel(
            "Every stream the site offers. Download from a row above."
        )
        self.advanced_note.setObjectName("muted")
        self.advanced_note.setWordWrap(True)
        self.advanced_table = QTableWidget(0, len(ADVANCED_COLUMNS))
        self.advanced_table.setHorizontalHeaderLabels(list(ADVANCED_COLUMNS))
        self.advanced_table.verticalHeader().setVisible(False)
        self.advanced_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.advanced_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.advanced_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.advanced_table.setMinimumHeight(180)
        header = self.advanced_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(ADVANCED_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        self.advanced_box = QWidget()
        box = QVBoxLayout(self.advanced_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        box.addWidget(self.advanced_note)
        box.addWidget(self.advanced_table)
        self.advanced_box.hide()

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.advanced_button)
        row.addStretch(1)
        self.result_card.body.addLayout(row)
        self.result_card.body.addWidget(self.advanced_box)
        self.advanced_button.toggled.connect(self._toggle_advanced)

    def _toggle_advanced(self, shown: bool) -> None:
        self.advanced_box.setVisible(shown and self.advanced_table.rowCount() > 0)
        self.advanced_button.setText("▾  All formats" if shown else "▸  All formats")

    @staticmethod
    def _site_label(info: dict[str, Any]) -> str:
        """The MediaResult's site name, as one short plain cell; "" when it has none."""
        site = info.get("site")
        return _cell(site, SITE_LIMIT) if isinstance(site, str) and site.strip() else ""

    def _fill_advanced(self, info: dict[str, Any]) -> None:
        rows = advanced_rows(info.get("formats")) + track_rows(info)
        self.advanced_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate(row):
                self.advanced_table.setItem(index, column, QTableWidgetItem(text))
        self.advanced_button.setVisible(bool(rows))
        self.advanced_button.setChecked(False)  # every analyze starts collapsed
        self.advanced_box.hide()

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
        return JobRun(self._with_site_login(spec), self._bridge.post)

    def _with_site_login(self, spec: JobSpec) -> JobSpec:
        """The spec a worker actually receives: plus the owner's site login, if one applies.

        The login is attached here, at launch, and only to the copy handed to the worker. The
        job's own spec — the one written to the queue database and history — never carries it,
        so a cookies file path or browser profile cannot end up stored with a job.
        """
        if spec.engine not in LOGIN_ENGINES:
            return spec
        choice = cookies.choice_for(spec.url, cookies.load_all(self._settings.site_logins))
        if choice is None:
            return spec
        return replace(spec, options={**spec.options, "site_login": choice.to_dict()})

    # ── advanced: site login (plan §6.4) ─────────────────────────────────────────────────
    def _login_dialog(self, site: str, current: cookies.SiteLogin | None) -> Any:
        return SiteLoginDialog(site, current, self)

    def offer_site_login(self) -> bool:
        """Ask for a login for the site that just refused us; retry the analyze if one is set."""
        site = self._login_site
        if not site:
            return False
        current = cookies.load_all(self._settings.site_logins).get(site)
        dialog = self._login_dialog(site, current)
        if not dialog.exec():
            return False
        if not self.set_site_login(site, dialog.choice()):
            return False
        self.login_button.hide()
        retry_id, self._login_retry_job_id = self._login_retry_job_id, ""
        job = self.jobs.get(retry_id)
        if job is not None and job.state == "failed":
            self._show_message("")
            self.retry_job(retry_id)  # relaunched through _new_run, so it picks the login up
        else:
            self.analyze()
        return True

    def _offer_login_after_download(self, job: QueuedJob, code: Any, message: Any) -> None:
        """A queued download failed with "private on the site": offer the same opt-in login.

        A link can be public when analyzed and private by the time the download runs (an
        expired share, a post made friends-only). Only the host is remembered here; the job's
        stored spec stays free of any login, which is attached at launch as usual.
        """
        if job.spec.engine not in LOGIN_ENGINES or not errors.needs_site_login(code, message):
            return
        site = cookies.site_key(job.spec.url)
        if not site:
            return
        self._login_site = site
        self._login_retry_job_id = job.spec.job_id
        self._show_message(
            f"{safe_error_message(errors.friendly_message(code, message))}"
            f" A site login may help ({site}).",
            error=True,
        )
        self.login_button.show()

    def set_site_login(self, site: str, choice: cookies.SiteLogin | None) -> bool:
        """Save (or with None, remove) the choice for one site. Stores the choice only."""
        site = cookies.site_key(site)
        if not site:
            return False
        choices = cookies.load_all(self._settings.site_logins)
        if choice is None:
            choices.pop(site, None)
        else:
            choices[site] = choice
        previous = self._settings.site_logins
        self._settings.site_logins = cookies.dump_all(choices)
        try:
            settings.save(self._settings)
        except OSError as exc:
            self._settings.site_logins = previous
            QMessageBox.warning(self, "Could not save settings", str(exc))
            return False
        return True

    def _reset_route_preset(self, combo: QComboBox, *, playlist_mode: bool = False) -> None:
        """Choose a fresh default for this analyzed route; never inherit the previous link's UI."""
        preset_id = (
            "mp3_music"
            if playlist_mode or (self._route is not None and self._route.music)
            else presets.DEFAULT_PRESET_ID
        )
        index = combo.findData(preset_id)
        if index >= 0:
            combo.setCurrentIndex(index)

    # ── analyze ──────────────────────────────────────────────────────────────────────────
    def analyze(self) -> None:
        if self._analyze_run is not None:
            return
        route = router.route(self.url_edit.text())
        if not route.ok:
            self.result_card.hide()
            self._show_message(route.reason, error=True)
            return
        if route.note:
            # The link was completed (https:// added); show the owner what is being analyzed.
            self.url_edit.setText(router.complete_link(self.url_edit.text())[0])
        self._route = route
        self._fallbacks = router.analyze_fallbacks(route)
        self._start_analyze(route)

    def _start_analyze(self, route: router.Route) -> None:
        self._info = {}
        self._listing = None
        self.result_card.hide()
        self.playlist_card.hide()
        self.gallery_card.hide()
        self._gallery = None
        self._reset_spotify()
        self.login_button.hide()
        self._login_site = ""
        self._login_retry_job_id = ""
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
        if route.is_spotify:
            self._show_message("Reading Spotify…")
        else:
            reading = "Reading the playlist…" if route.is_playlist else "Analyzing link…"
            self._show_message(f"{route.note} {reading}" if route.note else reading)
        self.analyze_button.setEnabled(False)
        self.analyze_cancel_button.show()
        self._analyze_timer.start(
            SPOTIFY_ANALYZE_TIMEOUT_MS if route.is_spotify else ANALYZE_TIMEOUT_MS
        )
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
            route = self._route
            if router.should_fall_back(code) and route is not None and self._fallbacks:
                # The chain is core's (router.analyze_fallbacks); this only walks it.
                self._route = self._fallbacks.pop(0)
                self._start_analyze(self._route)
                return
            message = event.data.get("message")
            self._show_message(errors.friendly_message(code, message), error=True)
            if (
                route is not None
                and not route.is_file
                and not route.is_spotify  # public share links only; no Spotify login
                and errors.needs_site_login(code, message)
            ):
                self._login_site = cookies.site_key(route.url)
                self.login_button.setVisible(bool(self._login_site))
            return
        try:
            result = validate_media_result(event.data)
        except ProtocolError:
            self._show_message(
                "The downloader sent a result this version cannot read.", error=True
            )
            return
        self._show_message("")
        # Chosen by what the link holds, never by the engine or route that read it (§5.2).
        kind = result["kind"]
        if kind == "playlist" and result.get("spotify_kind"):
            self.show_spotify(result)
        elif kind == "playlist":
            self.show_playlist(result)
        elif kind == "gallery":
            self.show_gallery(result)
        else:
            self.show_result(result)

    def show_result(self, info: dict[str, Any]) -> None:
        """Draw one video, song or image (plan §5.8) from its MediaResult."""
        self._info = info
        card = self.result_card
        title = info.get("title")
        card.title_editor.set_title(
            _cell(title, TITLE_LIMIT) if isinstance(title, str) and title.strip() else "Untitled"
        )
        meta = [
            _cell(info.get("artist") or info.get("uploader"), TITLE_LIMIT)
            if isinstance(info.get("artist") or info.get("uploader"), str)
            else "",
            format_duration(info.get("duration")),
            self._site_label(info),
        ]
        card.meta_label.setText("  ·  ".join(m for m in meta if m and m != "—"))
        card.source_label.setText(source_audio_text(info.get("source_audio")))
        card.source_label.setVisible(bool(card.source_label.text()))
        card.audio_note.setText(
            "Tags and a cover are always added (WAV gets tags only). "
            + (
                "Higher MP3 bitrates do not add quality."
                if card.source_label.text()
                else ""
            )
        )
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
        thumb = info.get("preview")
        image = None
        if isinstance(thumb, dict) and isinstance(thumb.get("data"), str):
            try:
                # Byte- and pixel-capped: a direct image link sends the image itself here.
                image = decode_image(base64.b64decode(thumb["data"], validate=True), PREVIEW_BOX)
            except (binascii.Error, ValueError):
                image = None
            if image is not None:
                self._thumb = QPixmap.fromImage(image)
        card.set_preview(image, info)
        card.set_result(info, {tab: result_rows(info, tab) for tab in ("video", "audio", "image")})
        self._fill_advanced(info)
        card.show()

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
        music = self._listing_is_music(listing)
        card.title_label.setText(listing.title)
        count = len(listing.entries)
        noun = ("song" if music else "video") + ("" if count == 1 else "s")
        meta = [listing.uploader, f"{count} {noun}"]
        total = sum(e.duration or 0 for e in listing.entries)
        if total:
            meta.append(format_duration(total))
        if listing.truncated:
            meta.append(f"showing the first {playlist.MAX_ENTRIES}")
        card.meta_label.setText("  ·  ".join(m for m in meta if m))
        card.set_entries(listing.entries, music=music)
        self._playlist_thumb_urls = [youtube_thumb_url(e.video_id) for e in listing.entries]
        # The list's own cover: its first video's picture (square-cropped for a song list).
        self._playlist_cover_url = next((u for u in self._playlist_thumb_urls if u), None)
        self._show_playlist_cover()
        self._request_visible_playlist_thumbs()
        default = presets.DEFAULT_MUSIC_BATCH if music else presets.DEFAULT_VIDEO_BATCH
        card.format_combo.setCurrentIndex(card.format_combo.findData(default))
        card.filter_edit.clear()
        self._update_playlist_selection()
        self.result_card.hide()
        card.show()

    def _listing_is_music(self, listing: playlist.Listing) -> bool:
        """Songs (square art, MP3) or videos (16:9 art, MP4), from the listing and the link."""
        return listing.music or (self._route is not None and self._route.music)

    def _show_playlist_cover(self) -> None:
        image = self.thumbs.cached(self._playlist_cover_url)
        if image is not None:
            self.playlist_card.header.set_cover(image)
        else:
            self.thumbs.request(self._playlist_cover_url)

    def _set_playlist_selection(self, checked: bool) -> None:
        self.playlist_card.set_all_checked(checked)
        self._update_playlist_selection()

    def _update_playlist_selection(self) -> None:
        self.playlist_card.set_download_count(len(self.playlist_card.selected_rows()))

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
            self.playlist_card.format_combo.currentData() or presets.DEFAULT_MUSIC_BATCH,
            archive=self.playlist_card.archive_check.isChecked(),
            edited_titles=self._playlist_output_names(entries),
        )
        group_id = uuid.uuid4().hex
        source_url = self._route.playlist_url if self._route else ""
        self.store.add_group(group_id, listing.title, source_url, len(specs))
        group_card = GroupCard(listing.title, len(specs))
        self.queue_layout.insertWidget(0, group_card)
        self._groups[group_id] = GroupState(group_card, len(specs), listing.title)
        jobs = []
        table = self.playlist_card.table
        rows = {value.index: i for i, value in enumerate(listing.entries)}
        for spec, entry in zip(specs, entries, strict=True):
            # The row's own picture follows it into the queue and History (plan §5.6a).
            thumb = youtube_thumb_url(entry.video_id)
            job = self._add_job(spec, entry.title, group_id, thumb_url=thumb, music=table.music)
            art = table.art(rows.get(entry.index, -1))
            if art is not None:
                job.card.set_thumbnail(art)
            else:
                self._set_job_thumb(job, thumb)
            jobs.append(job)
        self.empty_state.hide()
        self.scheduler.submit_all(specs)
        self._update_summary()
        return jobs

    def _playlist_output_names(self, entries: list[playlist.PlaylistEntry]) -> dict[int, str]:
        """Only the names the user typed, by entry index; untouched rows keep the default."""
        rows = {value.index: i for i, value in enumerate(self._listing.entries)}
        return unique_names(
            (entry.index, self.playlist_card.output_name(rows.get(entry.index, -1)))
            for entry in entries
        )

    # ── galleries (plan §M4) ─────────────────────────────────────────────────────────────
    def show_gallery(self, data: dict[str, Any]) -> None:
        listing = gallery.parse(data)
        self._gallery = listing
        card = self.gallery_card
        card.title_label.setText(listing.title)
        meta = [listing.uploader, listing.site, f"{len(listing.items)} items"]
        if listing.truncated:
            meta.append(f"showing the first {gallery.MAX_ITEMS}")
        card.meta_label.setText("  ·  ".join(m for m in meta if m))
        card.grid.blockSignals(True)
        card.set_items(listing.items)
        card.grid.blockSignals(False)
        self._update_gallery_selection()
        self.result_card.hide()
        self.playlist_card.hide()
        card.show()

    def _set_gallery_selection(self, checked: bool) -> None:
        self.gallery_card.grid.blockSignals(True)
        self.gallery_card.set_all_checked(checked)
        self.gallery_card.grid.blockSignals(False)
        self._update_gallery_selection()

    def _update_gallery_selection(self) -> None:
        count = len(self.gallery_card.selected_indices())
        self.gallery_card.selection_label.setText(f"{count} selected")
        self.gallery_card.download_button.setEnabled(count > 0)

    def start_gallery_download(self) -> QueuedJob | None:
        """One job for the ticked items. It names positions, never the items' URLs."""
        listing = self._gallery
        route = self._route
        chosen = self.gallery_card.selected_indices()
        known = {item.index for item in listing.items} if listing else set()
        chosen = [i for i in chosen if i in known]
        if listing is None or route is None or not route.ok or not chosen:
            return None
        options = presets.gallery_download_options(
            chosen,
            archive=self.gallery_card.archive_check.isChecked(),
            image_format=self.gallery_card.image_format_combo.currentData(),
        )
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=route.engine,
            url=route.url,
            output_dir=str(self._settings.effective_download_dir()),
            options=options,
        )
        title = listing.title if len(chosen) == len(known) else f"{listing.title} ({len(chosen)})"
        job = self._add_job(spec, safe_job_title(title, route.url))
        self.empty_state.hide()
        self.scheduler.submit(spec)
        self._update_summary()
        return job

    # ── Spotify (plan §6.3, §M5) ─────────────────────────────────────────────────────────
    def show_spotify(self, data: dict[str, Any]) -> None:
        listing = spotify.parse_listing(data)
        self._spotify = listing
        self._spotify_matches = {}
        card = self.spotify_card
        card.title_label.setText(listing.title)
        kind = {"track": "Song", "album": "Album", "playlist": "Playlist"}.get(listing.kind, "")
        count = len(listing.tracks)
        meta = [kind, listing.owner, f"{count} song" if count == 1 else f"{count} songs"]
        if listing.skipped:
            meta.append(f"{listing.skipped} not downloadable (local files or podcasts)")
        if listing.truncated:
            meta.append(f"showing the first {spotify.MAX_TRACKS}")
        card.meta_label.setText("  ·  ".join(m for m in meta if m))
        card.set_tracks(listing.tracks)
        self._forget_spotify_batch()
        card.set_uncertain_count(0)
        # Spotify's own pictures, before any match is looked up (plan §5.6a, §7 item 2).
        card.header.set_music(True)
        self._spotify_art_urls = [t.art_url for t in listing.tracks]
        self._spotify_cover_url = listing.cover or next(
            (t.art for t in listing.tracks if t.art), None
        )
        self._show_spotify_cover()
        self._update_spotify_selection()
        self.result_card.hide()
        self.playlist_card.hide()
        self.gallery_card.hide()
        if not listing.tracks:
            self._show_message("No downloadable songs were found at that link.", error=True)
            card.hide()
            return
        card.show()
        # Once the table is laid out, so only the rows on screen are looked up.
        QTimer.singleShot(0, self._request_visible_spotify_art)

    def _reset_spotify(self) -> None:
        self._cancel_matches()
        self._spotify = None
        self._spotify_matches = {}
        self._forget_spotify_batch()
        self._spotify_art_urls = []
        self._spotify_cover_url = None
        self.spotify_card.hide()

    def _forget_spotify_batch(self) -> None:
        """A new listing starts with nothing waiting to download; queued jobs keep running."""
        for group_id in list(self._spotify_pending.values()):
            self._shrink_group(group_id)
        self._spotify_pending = {}
        self._spotify_held = set()
        self._spotify_sent = set()

    def _show_spotify_cover(self) -> None:
        image = self.thumbs.cached(self._spotify_cover_url)
        if image is not None:
            self.spotify_card.header.set_cover(image)
        else:
            self.thumbs.request(self._spotify_cover_url)

    def _request_visible_spotify_art(self, *_: Any) -> None:
        """Lazy, like a playlist: only the rows on screen look up their picture."""
        if self._spotify is None or not self.spotify_card.isVisible():
            return
        urls = self._spotify_art_urls
        for row in self.spotify_card.visible_rows():
            url = urls[row] if row < len(urls) else None
            image = self.thumbs.cached(url)
            if image is not None:
                self.spotify_card.set_thumbnail(row, image)
            else:
                self.thumbs.request(url)

    def _set_spotify_selection(self, checked: bool) -> None:
        self.spotify_card.set_all_checked(checked)
        self._update_spotify_selection()

    def _update_spotify_selection(self) -> None:
        card = self.spotify_card
        count = len(card.selected_rows())
        checking = len(self._match_waiting) + len(self._match_runs)
        text = f"{count} selected"
        if checking:
            text += f"  ·  checking {checking} match{'es' if checking != 1 else ''}…"
        card.set_download_count(count)
        card.selection_label.setText(text)
        card.match_button.setEnabled(count > 0)

    def selected_spotify_tracks(self) -> list[spotify.SpotifyTrack]:
        if self._spotify is None:
            return []
        tracks = self._spotify.tracks
        return [tracks[r] for r in self.spotify_card.selected_rows() if r < len(tracks)]

    def _spotify_row(self, track: spotify.SpotifyTrack) -> int:
        return track.index - 1  # rows are listed in listing order, one per track

    def check_spotify_matches(self) -> int:
        """Look up the YouTube match for each ticked song that has none yet. Returns how many."""
        busy = {t.track_id for t in self._match_waiting}
        busy |= {track.track_id for _, track, _ in self._match_runs.values()}
        queued = 0
        for track in self.selected_spotify_tracks():
            if track.track_id in self._spotify_matches or track.track_id in busy:
                continue
            self._match_waiting.append(track)
            self.spotify_card.set_status(self._spotify_row(track), "Waiting to check…")
            queued += 1
        self._pump_matches()
        self._update_spotify_selection()
        return queued

    def _pump_matches(self) -> None:
        while self._match_waiting and len(self._match_runs) < MATCH_CONCURRENCY:
            track = self._match_waiting.pop(0)
            spec = spotify.match_spec(track)
            try:
                run = self._new_run(spec)
            except WorkerRuntimeMissing as exc:
                stranded = [track, *self._match_waiting]
                self._match_waiting.clear()
                self._show_message(f"Cannot start the downloader: {exc}", error=True)
                for waiting in stranded:
                    row = self._spotify_row(waiting)
                    self.spotify_card.set_status(row, "Not checked", warn=True)
                    self._settle_pending(waiting)
                break
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda job_id=spec.job_id: self._match_timeout(job_id))
            self._match_runs[spec.job_id] = (run, track, timer)
            self.spotify_card.set_status(self._spotify_row(track), "Checking…")
            timer.start(MATCH_TIMEOUT_MS)
            run.start()

    def _match_timeout(self, job_id: str) -> None:
        entry = self._match_runs.get(job_id)
        if entry is not None:
            entry[0].cancel()

    def _on_match_event(self, event: Event) -> None:
        if not event.is_terminal:
            return
        _, track, timer = self._match_runs.pop(event.job_id)
        timer.stop()
        timer.deleteLater()
        row = self._spotify_row(track)
        listing = self._spotify
        # A late answer for a listing that has since been replaced lands nowhere.
        current = listing is not None and row < len(listing.tracks)
        current = current and listing.tracks[row].track_id == track.track_id
        if current:
            manual = self._spotify_matches.get(track.track_id)
            if manual is not None and manual.manual:
                pass  # the owner pasted a link while this was running; theirs wins
            elif event.type == "result":
                match = spotify.parse_match(event.data, track)
                if match is None:
                    self.spotify_card.set_status(row, "No usable match found", warn=True)
                else:
                    self._spotify_matches[track.track_id] = match
                    self.spotify_card.set_match(row, match)
            else:
                code = event.data.get("code")
                if code == "cancelled":
                    text = "Not checked"
                elif code == "no_match":
                    text = "No match found — paste one with Change…"
                else:
                    text = safe_error_message(
                        errors.friendly_message(code, event.data.get("message"))
                    )
                self.spotify_card.set_status(row, text, warn=True)
            self._settle_pending(track)
            self._update_uncertain()
        self._pump_matches()
        self._update_spotify_selection()

    def _settle_pending(self, track: spotify.SpotifyTrack) -> None:
        """A song the owner asked to download has its lookup back (plan §7 item 4).

        A certain match downloads now, in the batch it was asked for with. An uncertain one is
        held for review, and a song with no match at all is left for Change…; neither holds up
        the batch, which simply counts one song fewer.
        """
        group_id = self._spotify_pending.pop(track.track_id, None)
        if group_id is None:
            return
        match = self._spotify_matches.get(track.track_id)
        if match is not None and not spotify.is_uncertain(match):
            self._send_spotify([track], group_id)
            return
        self._shrink_group(group_id)
        if match is not None:
            self._spotify_held.add(track.track_id)

    def _shrink_group(self, group_id: str) -> None:
        """One song fewer in a queue group, because it waits (or failed) before being queued."""
        group = self._groups.get(group_id)
        if group is None:
            return
        group.total = max(0, group.total - 1)
        members = [j for j in self.jobs.values() if j.group_id == group_id]
        if not members and not group.total:
            self._groups.pop(group_id)
            self.queue_layout.removeWidget(group.card)
            group.card.deleteLater()
            return
        group.card.set_counts(group.done, group.total, group.failed, group.skipped)
        if members and group.done + group.failed + group.skipped >= group.total:
            self._notify_finished(members[-1], members[-1].state, "")

    def _cancel_matches(self) -> None:
        self._match_waiting.clear()
        for run, _, timer in list(self._match_runs.values()):
            timer.stop()
            run.cancel()
        # Their terminal events still arrive and are dropped: the listing they belong to is gone.

    def _ask_match(
        self, track: spotify.SpotifyTrack, candidates: tuple[spotify.Candidate, ...]
    ) -> spotify.Candidate | str | None:
        """The owner's choice for one song: a looked-up result, a pasted link, or None."""
        # Spotify's cover beside the YouTube picture of the selected result (plan §5.6a).
        art = self.spotify_card.art(self._spotify_row(track))
        dialog = MatchDialog(track, candidates, self, art=art, thumbs=self.thumbs)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice()

    def change_spotify_match(self, row: int) -> spotify.Match | None:
        listing = self._spotify
        if listing is None or not 0 <= row < len(listing.tracks):
            return None
        track = listing.tracks[row]
        current = self._spotify_matches.get(track.track_id)
        candidates = current.candidates if current is not None else ()
        if current is not None and not current.manual:
            # The automatic pick stays offered, first, so undoing a change is one click.
            auto = spotify.Candidate(
                current.video_id, current.title, current.channel, current.duration
            )
            candidates = (auto, *[c for c in candidates if c.video_id != auto.video_id])
        choice = self._ask_match(track, candidates)
        if isinstance(choice, spotify.Candidate):
            match = spotify.candidate_match(choice, track)
        elif isinstance(choice, str) and choice.strip():
            try:
                match = spotify.override_match(choice, track)
            except ValueError as exc:
                self._show_message(str(exc), error=True)
                return None
        else:
            return None
        match = spotify.with_candidates(match, candidates)
        self._spotify_matches[track.track_id] = match
        self.spotify_card.set_match(row, match)
        # A held song the owner has now reviewed is what they asked for: it downloads.
        if track.track_id in self._spotify_held:
            self._spotify_held.discard(track.track_id)
            self._send_spotify([track], "")
        self._update_uncertain()
        self._show_message("")
        return match

    def _waiting_uncertain(self) -> list[spotify.SpotifyTrack]:
        """Songs with an uncertain match that have not been queued: they wait for review."""
        listing = self._spotify
        return [
            track
            for track in (listing.tracks if listing is not None else ())
            if track.track_id not in self._spotify_sent
            and (m := self._spotify_matches.get(track.track_id)) is not None
            and spotify.is_uncertain(m)
        ]

    def _update_uncertain(self) -> None:
        """The header count of uncertain matches still waiting, over the current listing."""
        self.spotify_card.set_uncertain_count(len(self._waiting_uncertain()))

    def start_spotify_download(self) -> list[QueuedJob]:
        """Download the ticked songs; uncertain matches wait, never block (plan §7 item 4).

        A song with a certain match (or the owner's own choice) is queued now. A song not
        checked yet is looked up first and joins the same batch the moment its match turns out
        certain. A song whose match is uncertain waits with a ⚠ for Review… or "Download
        uncertain anyway". Returns the jobs queued right now.
        """
        listing = self._spotify
        route = self._route
        tracks = [t for t in self.selected_spotify_tracks() if t.track_id not in self._spotify_sent]
        if listing is None or route is None or not route.is_spotify or not tracks:
            return []
        self._spotify_archive = self.spotify_card.archive_check.isChecked()
        busy = {t.track_id for t in self._match_waiting}
        busy |= {track.track_id for _, track, _ in self._match_runs.values()}
        now, lookup, held = [], [], []
        for track in tracks:
            if track.track_id in self._spotify_pending:
                continue  # already asked for; its lookup is on the way
            match = self._spotify_matches.get(track.track_id)
            if match is None:
                lookup.append(track)
            elif spotify.is_uncertain(match):
                held.append(track)
            else:
                now.append(track)
        group_id = ""
        total = len(now) + len(lookup)
        if total > 1:
            group_id = uuid.uuid4().hex
            self.store.add_group(group_id, listing.title, route.url, total)
            group_card = GroupCard(listing.title, total)
            self.queue_layout.insertWidget(0, group_card)
            self._groups[group_id] = GroupState(group_card, total, listing.title)
        for track in lookup:
            self._spotify_pending[track.track_id] = group_id
            if track.track_id not in busy:
                self._match_waiting.append(track)
                self.spotify_card.set_status(self._spotify_row(track), "Waiting to check…")
        self._spotify_held.update(t.track_id for t in held)
        jobs = self._send_spotify(now, group_id)
        self._pump_matches()
        if held:
            noun = "song waits" if len(held) == 1 else "songs wait"
            self._show_message(f"{len(held)} uncertain {noun} for review; the rest go ahead.")
        self._update_uncertain()
        self._update_spotify_selection()
        return jobs

    def download_uncertain_anyway(self) -> list[QueuedJob]:
        """The one button that sends every waiting uncertain match as it is."""
        tracks = self._waiting_uncertain()
        if not tracks or self._spotify is None:
            return []
        group_id = ""
        if len(tracks) > 1:
            listing = self._spotify
            group_id = uuid.uuid4().hex
            title = f"{listing.title} (uncertain)"
            route_url = self._route.url if self._route is not None else ""
            self.store.add_group(group_id, title, route_url, len(tracks))
            group_card = GroupCard(title, len(tracks))
            self.queue_layout.insertWidget(0, group_card)
            self._groups[group_id] = GroupState(group_card, len(tracks), title)
        self._spotify_held.difference_update(t.track_id for t in tracks)
        jobs = self._send_spotify(tracks, group_id)
        self._update_uncertain()
        self._show_message("")
        return jobs

    def _send_spotify(self, tracks: list[spotify.SpotifyTrack], group_id: str) -> list[QueuedJob]:
        """Queue one tagged-MP3 job per song, each with its match and Spotify's picture."""
        listing = self._spotify
        if listing is None or not tracks:
            return []
        specs = spotify.batch_specs(
            tracks,
            self._spotify_matches,
            str(self._settings.effective_download_dir()),
            archive=self._spotify_archive,
            edited_titles=unique_names(
                (t.track_id, self.spotify_card.table.edited_title(self._spotify_row(t)))
                for t in tracks
            ),
            album_order=listing.kind == "album",
        )
        jobs = []
        for spec, track in zip(specs, tracks, strict=True):
            self._spotify_sent.add(track.track_id)
            title = spec.options.get("edited_title") or (
                f"{track.artist} - {track.title}" if track.artist else track.title
            )
            # Spotify's art, never the YouTube match's picture, follows the song into the
            # queue and History (plan §5.6a).
            job = self._add_job(spec, title, group_id, thumb_url=track.art_url, music=True)
            art = self.spotify_card.art(self._spotify_row(track))
            if art is not None:
                job.card.set_thumbnail(art)
            else:
                self._set_job_thumb(job, track.art_url)
            jobs.append(job)
        self.empty_state.hide()
        self.scheduler.submit_all(specs)
        self._update_summary()
        return jobs

    # ── thumbnails (item 4) ──────────────────────────────────────────────────────────────
    def _request_visible_playlist_thumbs(self, *_: Any) -> None:
        """Lazy: only rows on screen are fetched; scrolling asks for the next ones."""
        urls = self._playlist_thumb_urls
        for row in self.playlist_card.visible_rows():
            url = urls[row] if row < len(urls) else None
            image = self.thumbs.cached(url)
            if image is not None:
                self.playlist_card.set_thumbnail(row, image)
            else:
                self.thumbs.request(url)

    def _set_job_thumb(self, job: QueuedJob, url: str | None) -> None:
        if url is None:
            return
        self._job_thumbs[job.spec.job_id] = url
        image = self.thumbs.cached(url)
        if image is not None:
            job.card.set_thumbnail(image)
        else:
            self.thumbs.request(url)

    def _on_thumbnail(self, url: str, image: QImage) -> None:
        if url == self._playlist_cover_url:
            self.playlist_card.header.set_cover(image)
        for row, row_url in enumerate(self._playlist_thumb_urls):
            if row_url == url:
                self.playlist_card.set_thumbnail(row, image)
        if url == self._spotify_cover_url:
            self.spotify_card.header.set_cover(image)
        for row, row_url in enumerate(self._spotify_art_urls):
            if row_url == url:
                self.spotify_card.set_thumbnail(row, image)
        for job_id, job_url in self._job_thumbs.items():
            job = self.jobs.get(job_id)
            if job_url == url and job is not None:
                job.card.set_thumbnail(image)

    # ── queue ────────────────────────────────────────────────────────────────────────────
    def _clearable(self, which: str = "all") -> list[str]:
        """Finished cards the queue may drop, for one "Clear done ▾" choice (plan §5.8).

        ``which`` is "finished" (done or already had), "failed" (cancelled or failed) or
        "all" (everything not running). A playlist's rows go only once the whole playlist has
        finished: its group row and final notification are counted from the jobs still in
        the queue.
        """
        states = CLEAR_STATES[which]
        open_groups = {
            job.group_id
            for job in self.jobs.values()
            if job.group_id and job.state not in TERMINAL_STATES
        }
        return [
            job_id
            for job_id, job in self.jobs.items()
            if job.state in states and job.group_id not in open_groups
        ]

    def clear_finished_jobs(self, which: str = "all") -> None:
        """Remove finished queue cards only; durable history is intentionally retained."""
        for job_id in self._clearable(which):
            self._cancel_retry_timer(job_id)
            self._job_thumbs.pop(job_id, None)
            job = self.jobs.pop(job_id)
            self.queue_layout.removeWidget(job.card)
            job.card.deleteLater()
        remaining = {job.group_id for job in self.jobs.values()}
        for group_id in [g for g in self._groups if g not in remaining]:
            group = self._groups.pop(group_id)
            self.queue_layout.removeWidget(group.card)
            group.card.deleteLater()
        if not self.jobs:
            self.empty_state.show()
        self._update_summary()

    def cancel_remaining(self) -> int:
        """Cancel every job that has not finished. Returns how many were asked to stop."""
        open_ids = [
            job_id
            for job_id, job in self.jobs.items()
            if job.state in ("active", "queued", "retrying", "paused")
        ]
        for job_id in open_ids:
            self.cancel_job(job_id)
        return len(open_ids)

    def _new_card(self, title: str, job_id: str, music: bool = False) -> JobCard:
        """A queue row wired to the actions for one job id."""
        job_card = JobCard(music)
        job_card.job_id = job_id
        job_card.title_label.setText(title)
        job_card.reorder_requested.connect(self.reorder_queue)
        job_card.cancel_button.clicked.connect(lambda: self.cancel_job(job_id))
        job_card.retry_button.clicked.connect(lambda: self.retry_job(job_id))
        job_card.pause_button.clicked.connect(lambda: self.toggle_pause(job_id))
        job_card.open_button.clicked.connect(lambda: self.open_file(job_id))
        job_card.folder_button.clicked.connect(lambda: self.show_in_folder(job_id))
        return job_card

    def _add_job(
        self,
        spec: JobSpec,
        title: str,
        group_id: str = "",
        thumb_url: str | None = None,
        music: bool | None = None,
    ) -> QueuedJob:
        """Create the row and record the job as queued. The scheduler decides when it runs.

        Every job that reaches history passes through here, so the title is made safe here
        rather than at each call site: one of them forgetting is how a token gets written.
        """
        job_id = spec.job_id
        title = safe_job_title(title, spec.url)
        if music is None:
            music = job_kind(spec.options) == "audio"
        job_card = self._new_card(title, job_id, music)
        job = QueuedJob(spec, title, job_card, state="queued", group_id=group_id)
        self.jobs[job_id] = job
        self.queue_layout.insertWidget(0, job_card)
        job_card.set_state(JOB_CHIPS["queued"], "queued")
        job_card.set_draggable(True)
        job_card.details_label.setText(job_label(spec.options))
        if thumb_url:
            self._job_thumbs[job_id] = thumb_url
        self._record_job(spec, title, group_id)
        return job

    def _record_job(self, spec: JobSpec, title: str, group_id: str = "") -> None:
        """Write one job row. The only place a job is allowed to reach the database.

        Both callers — a new job and a retry, which reuses its queue row and so cannot go
        through _add_job — come here, so the rules about what may be stored exist once. The
        exact URL stays in the spec, which lives only as long as the app is running; what goes
        on disk is the durable form, with a generic link's query and fragment removed, and a
        title that safe_job_title has already refused to let be a link.
        """
        durable, redacted = router.durable_url(spec.url)
        self.store.add_job(
            spec.job_id,
            durable,
            spec.engine,
            spec.options,
            spec.output_dir,
            title=safe_job_title(title, spec.url),
            group_id=group_id,
            url_redacted=redacted,
            # Only a link this page built from a validated video id (thumbs.youtube_thumb_url).
            thumb_url=self._job_thumbs.get(spec.job_id, ""),
        )

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
            if record.url_redacted:
                # Resuming would fetch the link without the part that made it work. Close the
                # row honestly instead of queueing something that can only fail, and do not
                # give it a card: a retry button here would replay the same broken link.
                self.store.set_state(
                    record.job_id, "failed", error_message=LINK_REDACTED_REASON
                )
                continue
            if not known_options(record.options):
                continue
            spec = JobSpec(
                job_id=record.job_id,
                engine=record.engine,
                url=record.url,
                output_dir=record.output_dir,
                options=record.options,
            )
            title = safe_job_title(record.title, record.url)
            music = job_kind(record.options) == "audio"
            job = QueuedJob(
                spec,
                title,
                self._new_card(title, spec.job_id, music),
                state="paused",
                group_id=record.group_id,
            )
            self.jobs[spec.job_id] = job
            if thumbs_allowed(record.thumb_url):
                self._set_job_thumb(job, record.thumb_url)
            self.queue_layout.insertWidget(0, job.card)
            self.scheduler.submit_paused(spec)
            self._apply_finished_card(job, "paused", "Paused — resume to continue")
            jobs.append(job)
        if jobs:
            self.empty_state.hide()
            self._update_summary()
        return jobs

    # ── downloads ────────────────────────────────────────────────────────────────────────
    def start_row_download(self, tab: str, row_id: str) -> QueuedJob | None:
        """Queue one row of the Result card: {tab, row_id, container, edited_title} (§8 R2)."""
        if self._route is None or not self._route.ok or not self._info:
            return None
        card = self.result_card
        if row_id not in {str(r.get("id")) for r in card.rows(tab)}:
            return None
        container = {"video": card.container(), "image": card.image_format()}.get(tab)
        edited = card.title_editor.edited_title()
        try:
            options = presets.row_download_options(tab, row_id, container, edited)
        except ValueError:
            return None
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=self._route.engine,
            url=self._route.url,
            output_dir=str(self._settings.effective_download_dir()),
            options=options,
        )
        title = edited or self._info.get("title")
        job = self._add_job(
            spec, safe_job_title(title, self._route.url), thumb_url=self._result_thumb_url()
        )
        if self._thumb is not None:  # the analyzed cover, or the direct image itself
            job.card.set_thumbnail(self._thumb.toImage())
        self.empty_state.hide()
        self.scheduler.submit(spec)
        return job

    def _result_thumb_url(self) -> str | None:
        """The History picture for a YouTube result: its thumbnail, from the validated id.

        Other sites' pictures reach the app only as worker-fetched bytes, never as a link the
        app may fetch again later, so their History rows keep the placeholder.
        """
        host = (urlsplit(self._route.url).hostname or "").lower() if self._route else ""
        if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
            video_id = self._info.get("id")
            return youtube_thumb_url(video_id if isinstance(video_id, str) else None)
        return None

    def start_download(self) -> QueuedJob | None:
        """Download the ★ row of the tab on show (the first row when none is marked)."""
        tab = self.result_card.current_tab()
        rows = self.result_card.rows(tab) if tab else []
        if not rows:
            return None
        row = next((r for r in rows if r.get("default") is True), rows[0])
        return self.start_row_download(tab, str(row.get("id")))

    def download_again(self, record: history.JobRecord) -> QueuedJob | None:
        """Queue a fresh job from a history row. The historical record is left untouched.

        The new job gets its own id and no group: it is a download of the same link, not a
        re-run of the playlist the original belonged to.
        """
        if not record.url or not known_options(record.options):
            return None
        if record.url_redacted:
            # The stored link names the page but not the video. Hand it back as a starting
            # point and let the owner paste the real one; do not silently download the wrong
            # thing or fail with something they cannot act on.
            self.url_edit.setText(record.url)
            self._show_message(LINK_REDACTED_REASON, error=True)
            return None
        spec = JobSpec(
            job_id=uuid.uuid4().hex,
            engine=record.engine,
            url=record.url,
            output_dir=record.output_dir or str(self._settings.effective_download_dir()),
            options=dict(record.options),
        )
        thumb = record.thumb_url if thumbs_allowed(record.thumb_url) else None
        job = self._add_job(spec, safe_job_title(record.title, record.url), thumb_url=thumb)
        self._set_job_thumb(job, thumb)
        self.empty_state.hide()
        self.scheduler.submit(spec)
        self._update_summary()
        return job

    def _launch(self, job: QueuedJob) -> None:
        self.jobs[job.spec.job_id] = job
        card = job.card
        card.set_paused(False)
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
        card.details_label.setText(job_label(job.spec.options))
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
        # The queue row and its card are reused, so this cannot go through _add_job — but the
        # rules for what may be written are shared with it rather than repeated here.
        self._record_job(job.spec, job.title, job.group_id)
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
        card.set_paused(False)
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

    def _final_file(self, job_id: str) -> tuple[Path | None, str]:
        """The job's finished file (after merge/convert/tag), or ``None`` and why not.

        Never a folder, a ``.part`` or an intermediate stream, and never outside the job's
        download folder, whatever the worker reported.
        """
        job = self.jobs.get(job_id)
        if job is None or job.state != "completed":
            return None, ""
        root = Path(job.spec.output_dir).resolve()
        candidates = []
        for path in job.files:
            resolved = path.resolve()
            if resolved.is_relative_to(root) and not _TRANSIENT_FILE.search(resolved.name):
                candidates.append(resolved)
        if not candidates:
            return None, "The download did not report a file."
        for path in candidates:
            if path.is_file():
                return path, ""
        return None, "The file was moved or deleted."

    def _refresh_file_buttons(self, job: QueuedJob) -> Path | None:
        path, reason = self._final_file(job.spec.job_id)
        for button in (job.card.open_button, job.card.folder_button):
            button.setEnabled(path is not None)
            button.setToolTip(plain_tooltip(reason) if reason else "")
        details = job.card.details_label.text()
        if reason and reason not in details:
            job.card.details_label.setText(f"{details}  ·  {reason}" if details else reason)
        return path

    def open_file(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        path = self._refresh_file_buttons(job) if job else None
        return bool(path) and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_in_folder(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        path = self._refresh_file_buttons(job) if job else None
        if path is None:
            return False
        if sys.platform == "win32":
            # "/select," and the path as separate arguments: explorer mis-parses one quoted
            # "/select,C: b\c.mp3" argument when the path has spaces.
            subprocess.Popen(["explorer.exe", "/select,", str(path)])  # noqa: S603 (validated path)
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
        card.set_paused(True)
        card.retry_button.setVisible(state in ("failed", "cancelled"))
        # A completed job always shows its file actions; they are disabled, with the reason
        # as a tooltip, when the final file is not there.
        card.open_button.setVisible(state == "completed")
        card.folder_button.setVisible(state == "completed")
        if state == "completed":
            self._refresh_file_buttons(job)

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
            error_message=safe_error_message(details) if state == "failed" else "",
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
        self.clear_queue_button.setEnabled(bool(self._clearable()))
        for which, action in self.clear_actions.items():
            action.setEnabled(bool(self._clearable(which)))
        self.cancel_remaining_button.setEnabled(
            any(counts.get(k) for k in ("active", "queued", "retrying", "paused"))
        )

    def _on_event(self, event: Event) -> None:
        if self._analyze_job_id and event.job_id == self._analyze_job_id:
            self._on_analyze_event(event)
            return
        if event.job_id in self._match_runs:
            self._on_match_event(event)
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
            # Worker notes, e.g. "Animated image: only the first frame was kept."
            notes = event.data.get("notes")
            for note in (notes if isinstance(notes, list) else [])[:MAX_NOTES]:
                if isinstance(note, str) and KNOWN_NOTE.fullmatch(note):
                    details += f"  ·  {note}"
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
                message = safe_error_message(
                    errors.friendly_message(code, event.data.get("message"))
                )
                raw = event.data.get("message")
                if not is_retryable(raw) or not self._start_backoff(
                    job, message, str(code or "")
                ):
                    self._finish_job(job, "failed", message, error_code=str(code or ""))
                    self._offer_login_after_download(job, code, raw)

    def shutdown(self) -> None:
        self.scheduler.stop()
        for timer in list(self._retry_timers.values()):
            timer.stop()
        self._retry_timers.clear()
        if self._analyze_run is not None:
            self._analyze_run.cancel()
        self._cancel_matches()
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


HISTORY_ART = QSize(64, 36)
HISTORY_ART_MUSIC = QSize(36, 36)


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
        self.table.setSelectionMode(self.table.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self.COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # The picture the job had in the queue (plan §5.6a), beside its title.
        self.table.setIconSize(HISTORY_ART)
        self.table.verticalHeader().setDefaultSectionSize(HISTORY_ART.height() + 10)
        self.table.setWordWrap(False)
        self.thumbs = ThumbnailLoader(parent=self)
        self.thumbs.loaded.connect(self._on_thumbnail)
        self.table.verticalScrollBar().valueChanged.connect(lambda _: self._request_visible_art())
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
        selected_ids = {record.job_id for record in self.selected_records()}
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
            self.table.item(row, 0).setIcon(self._placeholder(record))
        self._request_visible_art()
        self.table.clearSelection()
        select_row = (
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        )
        for row, record in enumerate(self._records):
            if record.job_id in selected_ids:
                self.table.selectionModel().select(
                    self.table.model().index(row, 0), select_row
                )
        self.empty_label.setVisible(not self._records)

    def _placeholder(self, record: history.JobRecord) -> QIcon:
        music = self._record_type(record) == "audio"
        return QIcon(art_placeholder(self._art_size(record), music))

    def _art_size(self, record: history.JobRecord) -> QSize:
        return HISTORY_ART_MUSIC if self._record_type(record) == "audio" else HISTORY_ART

    def _request_visible_art(self) -> None:
        """Lazy, like the playlist rows: only rows on screen fetch their picture."""
        count = self.table.rowCount()
        if not count:
            return
        first = max(self.table.rowAt(0), 0)
        last = self.table.rowAt(self.table.viewport().height() - 1)
        last = count - 1 if last < 0 else last
        for row in range(first, min(count, last + 6, len(self._records))):
            url = self._records[row].thumb_url
            if not thumbs_allowed(url):
                continue
            image = self.thumbs.cached(url)
            if image is not None:
                self._set_art(row, image)
            else:
                self.thumbs.request(url)

    def _set_art(self, row: int, image: QImage) -> None:
        item = self.table.item(row, 0)
        if item is not None and row < len(self._records):
            item.setIcon(row_icon(image, self._art_size(self._records[row])))

    def _on_thumbnail(self, url: str, image: QImage) -> None:
        for row, record in enumerate(self._records):
            if record.thumb_url == url:
                self._set_art(row, image)

    @staticmethod
    def _record_type(record: history.JobRecord) -> str:
        """Classify a saved job from its persisted options without trusting arbitrary text."""
        options = dict(record.options or {})
        if not presets.is_row_options(options):
            options["preset"] = record.preset
        return job_kind(options)

    def selected_record(self) -> history.JobRecord | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._records) and self.table.selectionModel().isRowSelected(row):
            return self._records[row]
        return None

    def selected_records(self) -> list[history.JobRecord]:
        """Only rows still visible under the active filters are actionable."""
        return [
            self._records[index.row()]
            for index in self.table.selectionModel().selectedRows()
            if 0 <= index.row() < len(self._records)
        ]

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
            subprocess.Popen(["explorer.exe", "/select,", str(path)])  # noqa: S603 (validated path)
            return True
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def forget_selected(self) -> bool:
        """Remove selected history rows. Downloaded files stay where they are."""
        records = self.selected_records()
        if not records:
            return False
        self.store.forget_many(record.job_id for record in records)
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
