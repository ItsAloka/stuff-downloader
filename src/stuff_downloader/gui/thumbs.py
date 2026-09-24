"""Thumbnails for rows and queue cards, loaded off the GUI thread.

The GUI fetches only images whose URL it built itself from a validated id: a YouTube video's
thumbnail (``i.ytimg.com``), a Spotify picture by its hash (``i.scdn.co/image/<hash>``), or a
Spotify track's oEmbed lookup (``open.spotify.com/oembed``), whose answer is read only for its
picture's hash. Every other preview — a gallery tile, a direct image link, the analyzed video's
cover — is fetched by the worker, behind its own address checks, and arrives as bytes.
All image bytes, from either side, are decoded here with a byte cap and a pixel cap checked from
the header before any pixels are allocated. Any failure just leaves the placeholder.
"""

from __future__ import annotations

import json
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
DECODE_BOX = QSize(320, 320)  # gallery tiles and other small previews
# The Result card preview (480×270 video, 300×300 music), with room for 2× display scaling.
PREVIEW_BOX = QSize(960, 960)
ROW_BOX = QSize(160, 160)  # rows and queue cards: keeps a full cache near 25 MB

ALLOWED_HOSTS = frozenset({"i.ytimg.com"})
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# Spotify: exactly the two forms core.spotify builds, nothing else on those hosts.
_SPOTIFY_IMAGE = re.compile(r"https://i\.scdn\.co/image/[0-9a-f]{40}")
_SPOTIFY_OEMBED = re.compile(
    r"https://open\.spotify\.com/oembed\?url=https://open\.spotify\.com/track/[A-Za-z0-9]{22}"
)
# The oEmbed answer holds a few hundred bytes of JSON.
MAX_OEMBED_BYTES = 64 * 1024
# Spotify's picture hosts in an oEmbed answer; the picture is re-fetched from i.scdn.co by hash.
_OEMBED_THUMB = re.compile(
    r"https://(?:i\.scdn\.co|image-cdn-[a-z]{2}\.spotifycdn\.com)/image/([0-9a-f]{40})"
)

Fetch = Callable[[str], bytes]


class ThumbnailError(Exception):
    pass


def youtube_thumb_url(video_id: str | None) -> str | None:
    """The medium thumbnail for a validated video id, or ``None``. Nothing else is fetched."""
    if not video_id or not _VIDEO_ID.fullmatch(video_id):
        return None
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def allowed(url: str) -> bool:
    if not isinstance(url, str):
        return False
    if _SPOTIFY_IMAGE.fullmatch(url) or _SPOTIFY_OEMBED.fullmatch(url):
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "") in ALLOWED_HOSTS
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
    """GET ``url`` with a timeout and a size cap. Only allow-listed https image hosts.

    A Spotify oEmbed URL is followed through to its picture: only the picture's hash is read
    from the answer, and the image itself is fetched from i.scdn.co.
    """
    if not allowed(url):
        raise ThumbnailError("not an allowed image URL")
    if _SPOTIFY_OEMBED.fullmatch(url):
        return fetch(spotify_oembed_picture(_get(url, MAX_OEMBED_BYTES)))
    return _get(url, MAX_BYTES)


def spotify_oembed_picture(answer: bytes) -> str:
    """The i.scdn.co picture an oEmbed answer names; ThumbnailError for anything else."""
    try:
        data = json.loads(answer)
    except ValueError:
        raise ThumbnailError("oEmbed answer is not JSON") from None
    thumb = data.get("thumbnail_url") if isinstance(data, dict) else None
    found = _OEMBED_THUMB.fullmatch(thumb) if isinstance(thumb, str) else None
    if found is None:
        raise ThumbnailError("oEmbed answer names no Spotify picture")
    return f"https://i.scdn.co/image/{found.group(1)}"


def _get(url: str, cap: int) -> bytes:
    opener = urllib.request.build_opener(_NoForeignRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "StuffDownloader"})
    with opener.open(request, timeout=TIMEOUT) as resp:  # noqa: S310 (allow-listed https)
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > cap:
            raise ThumbnailError("response too large")
        data = resp.read(cap + 1)
    if len(data) > cap:
        raise ThumbnailError("response too large")
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
