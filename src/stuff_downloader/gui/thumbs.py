"""Thumbnails for rows and queue cards, loaded off the GUI thread.

The GUI fetches only validated YouTube thumbnail URLs and Spotify cover URLs from approved
CDN hosts. Every other preview — a gallery tile, a direct image link, the analyzed video's
cover — is fetched by the worker, behind its own address checks, and arrives as bytes.
All image bytes, from either side, are decoded here with a byte cap and a pixel cap checked from
the header before any pixels are allocated. Any failure just leaves the placeholder.
"""

from __future__ import annotations

import re
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlsplit

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QRunnable, QSize, Qt, QThreadPool
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QImage, QImageReader

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 40_000_000
TIMEOUT = 10.0
CACHE_SIZE = 400
DECODE_BOX = QSize(320, 320)  # the analyze preview; nothing is shown bigger than this
ROW_BOX = QSize(160, 160)  # rows and queue cards: keeps a full cache near 25 MB

ALLOWED_HOSTS = frozenset({"i.ytimg.com"})
ALLOWED_SUFFIXES = (".scdn.co", ".spotifycdn.com")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

Fetch = Callable[[str], bytes]


class ThumbnailError(Exception):
    pass


def youtube_thumb_url(video_id: str | None) -> str | None:
    """The medium thumbnail for a validated video id, or ``None``. Nothing else is fetched."""
    if not video_id or not _VIDEO_ID.fullmatch(video_id):
        return None
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (
            (parts.hostname or "") in ALLOWED_HOSTS
            or (parts.hostname or "").endswith(ALLOWED_SUFFIXES)
        )
        and parts.port in (None, 443)
        and not parts.username
        and not parts.password
    )


class _NoForeignRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not allowed(newurl):
            raise ThumbnailError("redirect left the image host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> bytes:
    """GET ``url`` with a timeout and a size cap. Only allow-listed https image hosts."""
    if not allowed(url):
        raise ThumbnailError("not an allowed image URL")
    opener = urllib.request.build_opener(_NoForeignRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "StuffDownloader"})
    with opener.open(request, timeout=TIMEOUT) as resp:  # noqa: S310 (allow-listed https)
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_BYTES:
            raise ThumbnailError("image too large")
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ThumbnailError("image too large")
    return data


def decode_image(data: bytes, box: QSize = DECODE_BOX) -> QImage | None:
    """Decode at most ``box`` pixels; ``None`` for anything too big, broken or not an image.

    Safe off the GUI thread (QImage, never QPixmap). The size comes from the header, so a
    tiny file that claims 50000×50000 is refused before any memory is allocated for it.
    """
    if not data or len(data) > MAX_BYTES:
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        return None
    if max(size.width(), size.height()) > MAX_SIDE or size.width() * size.height() > MAX_PIXELS:
        return None
    if size.width() > box.width() or size.height() > box.height():
        reader.setScaledSize(size.scaled(box, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image


class _Job(QRunnable):
    def __init__(self, loader: ThumbnailLoader, url: str) -> None:
        super().__init__()
        self.loader, self.url = loader, url

    def run(self) -> None:
        try:
            image = decode_image(self.loader.fetcher(self.url), ROW_BOX)
        except Exception:  # any failure means "no thumbnail", never a crash
            image = None
        try:
            self.loader._done.emit(self.url, image if image is not None else QImage())
        except RuntimeError:  # the page (and its loader) closed while this was loading
            pass


class ThumbnailLoader(QObject):
    """Loads each URL once, on a thread pool, and announces it with ``loaded(url, image)``.

    A failed load is remembered too (as a null image), so a broken URL is not retried on every
    scroll. ``loaded`` is emitted on the GUI thread.
    """

    loaded = Signal(str, QImage)
    _done = Signal(str, QImage)

    def __init__(self, fetcher: Fetch | None = None, pool: QThreadPool | None = None, parent=None):
        super().__init__(parent)
        # Looked up at call time, so tests can replace the module's ``fetch`` everywhere.
        self.fetcher = fetcher or (lambda url: fetch(url))
        self.pool = pool or QThreadPool.globalInstance()
        self._cache: OrderedDict[str, QImage] = OrderedDict()
        self._pending: set[str] = set()
        self._done.connect(self._finish)

    def cached(self, url: str | None) -> QImage | None:
        """The loaded image, or ``None`` when it is not loaded (yet) or could not be."""
        image = self._cache.get(url or "")
        if image is None:
            return None
        self._cache.move_to_end(url)
        return None if image.isNull() else image

    def request(self, url: str | None) -> bool:
        """Start loading ``url`` unless it is cached, in flight or not allowed."""
        if not url or not allowed(url) or url in self._cache or url in self._pending:
            return False
        self._pending.add(url)
        self.pool.start(_Job(self, url))
        return True

    def _finish(self, url: str, image: QImage) -> None:
        self._pending.discard(url)
        self._cache[url] = image
        self._cache.move_to_end(url)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        if not image.isNull():
            self.loaded.emit(url, image)
