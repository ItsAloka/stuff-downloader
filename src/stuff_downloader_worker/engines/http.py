"""Direct HTTP engine (plan §2, §M3): a plain media file link, streamed to disk with resume.

Stdlib only, so it runs in any engine env. Options: ``mode`` "analyze" (default) or
"download"; download takes ``preset`` = "original_file", or a Result-card row request
(``tab``, ``row_id`` = ``v:orig`` / ``a:orig`` / ``i:orig``, ``container``, ``edited_title``),
or from a direct video its audio (``a:mp3:<kbps>``, ``a:m4a``…) or one frame (``i:frame``),
made by the runner's ffmpeg from the downloaded file. Nothing else is accepted.

The URL comes from the owner, and every redirect from the site, so each hop is checked before
it is fetched: http/https only, no credentials, a public host name, and every address that name
resolves to must be a global one. The connection is then made to exactly the address that was
checked, so a name that resolves differently a moment later cannot swap in a private one.

Resume: bytes go to ``<name>.<job>.part`` next to the final file, with the server's validator in
``<name>.<job>.part.json``. The job id is in the name so two jobs that resolve to the same file
never share (and corrupt) one partial; pause, resume and automatic retries keep their job id.
A later run sends ``Range`` plus ``If-Range``; a 206 that starts where the part ends is
appended, and anything else restarts from zero. Cancel is the runner killing this process,
which leaves the part file for the next run. The finished file is moved into place with a hard
link, so a name another job took a moment earlier is never overwritten.

Failure text never carries the URL, a header or a path: messages are built here from the status
code and a fixed vocabulary, so a signed link cannot reach the log or history.
"""

from __future__ import annotations

import base64
import html
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from .. import presets
from ..names import safe_output_name
from ..protocol import JobSpec, media_result
from .base import Emit, EngineError

PRESET_ID = "original_file"
MAX_REDIRECTS = 5
TIMEOUT = 30
CHUNK = 256 * 1024
PROGRESS_INTERVAL = 0.25
MAX_NAME = 150
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StuffDownloader"

# What counts as a direct media file. A type outside this list (an HTML page, JSON, a script) is
# refused rather than saved: this engine only takes over when the link really is the file.
MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".3gp", ".ts",
        ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".wma",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp",
    }
)  # fmt: skip
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"})
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".3gp", ".ts"}
)
# The image types we save, each with the extension a file of that type gets. An image is judged
# by its Content-Type, not its URL: a `?format=jpg` link has no extension at all, and a `.png`
# path can serve a JPEG. Any other image/* type (SVG above all) is refused, not saved.
IMAGE_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif", "image/bmp": ".bmp",
}  # fmt: skip
MAX_PREVIEW_BYTES = 5 * 1024 * 1024
PREVIEW_SECONDS = 15.0
_GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream", ""})
_NON_PUBLIC_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa", ".lan", ".test")

_UNSAFE_NAME_CHARS = re.compile(
    "[" + "".join(chr(c) for c in range(32)) + re.escape('<>:"/|?*' + chr(92)) + chr(127) + "]"
)
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)  # fmt: skip
FALLBACK_NAME = "download"


# ── URL safety ─────────────────────────────────────────────────────────────────────────────
def _looks_like_ip(host: str) -> bool:
    """Any spelling a resolver could read as an address (mirrors core.router.looks_like_ip)."""
    probe = host.strip("[]")
    if ":" in probe:
        return True
    try:
        ipaddress.ip_address(probe)
        return True
    except ValueError:
        pass
    labels = probe.split(".")
    if not probe or len(labels) > 4:
        return False
    for label in labels:
        text = label.lower()
        try:
            if text.startswith("0x"):
                int(text, 16)
            elif text.startswith("0") and len(text) > 1:
                int(text, 8)
            else:
                int(text, 10)
        except ValueError:
            return False
    return True


def is_public_name(host: str) -> bool:
    if not host or "_" in host:
        return False
    probe = unicodedata.normalize("NFKC", host).rstrip(".").lower()
    if not probe or probe.startswith(".") or "." not in probe or probe == "localhost":
        return False
    if _looks_like_ip(probe):
        return False
    return not probe.endswith(_NON_PUBLIC_SUFFIXES)


def _resolve(host: str, port: int) -> str:
    """One global address for ``host``. Refuses if ANY address it resolves to is not global."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise EngineError("download_error", "getaddrinfo failed: could not reach the site") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise EngineError("unsupported", "that link points at a private network address")
        addresses.append(str(address))
    if not addresses:
        raise EngineError("download_error", "getaddrinfo failed: could not reach the site")
    return addresses[0]


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str  # path plus query, as sent on the request line


def check_url(url: str, https_only: bool = False) -> Target:
    """Parse and vet one URL (the pasted one or a redirect). Raises EngineError when refused.

    ``https_only`` is for pictures the page names (thumbnails, og:image): the site chose that
    link, not the owner, so plain http is refused there, on every hop."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise EngineError("unsupported", "that is not a valid link") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or (https_only and scheme != "https"):
        raise EngineError("unsupported", "only http and https links are supported")
    if parts.username or parts.password:
        raise EngineError("unsupported", "links with credentials are not supported")
    host = (parts.hostname or "").lower()
    if not is_public_name(host):
        raise EngineError("unsupported", "that link does not point at a public website")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if any(ch.isspace() or ord(ch) < 32 for ch in path):
        raise EngineError("unsupported", "that is not a valid link")
    return Target(scheme, host, port or (443 if scheme == "https" else 80), path)


# ── connection ─────────────────────────────────────────────────────────────────────────────
def _connect(target: Target) -> http.client.HTTPConnection:
    """A connection to the checked address only. TLS still verifies the real host name."""
    address = _resolve(target.host, target.port)
    if target.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            target.host, target.port, timeout=TIMEOUT, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(target.host, target.port, timeout=TIMEOUT)

    def pinned(_address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        return socket.create_connection((address, target.port), *args, **kwargs)

    conn._create_connection = pinned  # type: ignore[attr-defined]
    return conn


def open_url(
    url: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    https_only: bool = False,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, str]:
    """Follow up to MAX_REDIRECTS, vetting every hop. Returns (conn, response, final url)."""
    for _ in range(MAX_REDIRECTS + 1):
        target = check_url(url, https_only)
        conn = _connect(target)
        sent = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
        try:
            conn.request(method, target.path, headers=sent)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise EngineError("download_error", _network_message(exc)) from exc
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location") or ""
            conn.close()
            if not location:
                raise EngineError("download_error", "the site sent a redirect with no target")
            url = urljoin(url, location)
            continue
        return conn, resp, url
    raise EngineError("download_error", "too many redirects")


def _network_message(exc: BaseException) -> str:
    if isinstance(exc, socket.timeout | TimeoutError):
        return "the connection timed out"
    if isinstance(exc, ssl.SSLError):
        return "the site's secure connection could not be verified"
    if isinstance(exc, ConnectionResetError):
        return "connection reset by the site"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused by the site"
    return "could not reach the site"


def http_error(status: int, reason: str) -> EngineError:
    """A status as the error text errors.friendly_message already understands. No URL."""
    safe_reason = re.sub(r"[^A-Za-z \-]", "", reason or "")[:40].strip()
    code = "unsupported" if status == 404 else "download_error"
    return EngineError(code, f"HTTP Error {status}: {safe_reason}".rstrip(": "))


# ── naming ─────────────────────────────────────────────────────────────────────────────────
def safe_file_name(name: str) -> str:
    """One Windows-safe file name from untrusted text (header or URL), extension kept."""
    name = unicodedata.normalize("NFC", name or "")
    name = _UNSAFE_NAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8]
    stem = stem[: MAX_NAME - len(ext) - 1].strip().rstrip(".").strip()
    if not stem or stem.split(".")[0].upper() in _WINDOWS_RESERVED:
        stem = FALLBACK_NAME
    return f"{stem}.{ext}" if ext else stem


def _content_type(resp: http.client.HTTPResponse) -> str:
    return (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()


def _disposition_name(resp: http.client.HTTPResponse) -> str:
    header = resp.getheader("Content-Disposition")
    if not header:
        return ""
    msg = Message()
    msg["Content-Disposition"] = header
    name = msg.get_filename() or ""
    return os.path.basename(name.replace("\\", "/"))


def file_name(url: str, resp: http.client.HTTPResponse) -> str:
    """Content-Disposition, else the last URL path segment; extension from the type if absent."""
    raw = _disposition_name(resp) or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = safe_file_name(raw)
    image_ext = IMAGE_TYPES.get(_content_type(resp))
    if image_ext:
        # The file is named by what it is: a PNG served from ".../photo.jpg" is saved as .png.
        stem, ext = os.path.splitext(name)
        valid_exts = {".jpg", ".jpeg"} if image_ext == ".jpg" else {image_ext}
        if ext.lower() not in valid_exts:
            name = (stem if ext.lower() in MEDIA_EXTENSIONS else name) + image_ext
    if "." not in name:
        guessed = mimetypes.guess_extension(_content_type(resp)) or ""
        if guessed.lower() in MEDIA_EXTENSIONS:
            name += guessed
    return name


def is_media(name: str, content_type: str) -> bool:
    """Whether a response is a media file: a media type, or a generic one with a media name."""
    if content_type.startswith("image/"):
        return content_type in IMAGE_TYPES
    if content_type.split("/")[0] in ("video", "audio"):
        return True
    ext = os.path.splitext(name)[1].lower()
    return content_type in _GENERIC_TYPES and ext in MEDIA_EXTENSIONS


def media_kind(name: str, content_type: str) -> tuple[str, list[str]]:
    """(kind, tabs) for a file ``is_media`` accepted: by MIME, by extension only when generic."""
    major = content_type.split("/")[0]
    if content_type in _GENERIC_TYPES:
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            major = "image"
        else:
            major = "video" if ext in VIDEO_EXTENSIONS else "audio"
    if major == "image":
        return "image", ["image"]
    if major == "audio":
        return "audio", ["audio", "image"]
    return "video", ["video", "audio", "image"]


def _candidates(folder: Path, name: str):
    stem, ext = os.path.splitext(name)
    yield folder / name
    n = 2
    while True:
        yield folder / f"{stem} ({n}){ext}"
        n += 1


MAX_COLLISIONS = 1000


def move_into_place(part: Path, folder: Path, name: str) -> Path:
    """Move ``part`` to the first free ``name`` / ``name (n)``, never replacing a file.

    Checking ``exists()`` and then moving would race another job finishing the same name, and
    ``os.replace`` would then silently overwrite its file. A hard link fails atomically when the
    target exists, on NTFS and POSIX alike, so a lost race just moves on to the next name.
    """
    for index, candidate in enumerate(_candidates(folder, name)):
        if index >= MAX_COLLISIONS:
            break
        try:
            os.link(part, candidate)
        except FileExistsError:
            continue
        except OSError:
            # No hard links here (FAT, some network drives). Windows rename also refuses an
            # existing target, so it is still never an overwrite there.
            if candidate.exists():
                continue
            try:
                os.rename(part, candidate)
            except FileExistsError:
                continue
            return candidate
        part.unlink()
        return candidate
    raise EngineError("download_error", "too many files with that name in the folder")


_JOB_TAG = re.compile(r"[^A-Za-z0-9_-]")


def part_names(name: str, job_id: str) -> tuple[str, str]:
    """This job's partial-data and validator file names. The job id is sanitized to a tag."""
    tag = _JOB_TAG.sub("", job_id)[:32] or "job"
    return f"{name}.{tag}.part", f"{name}.{tag}.part.json"


def _total_from(resp: http.client.HTTPResponse, offset: int) -> int | None:
    if resp.status == 206:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.getheader("Content-Range") or "")
        if match and match.group(3) != "*":
            return int(match.group(3))
    length = resp.getheader("Content-Length")
    if length and length.isdigit():
        return int(length) + (offset if resp.status == 206 else 0)
    return None


def _range_start(resp: http.client.HTTPResponse) -> int | None:
    match = re.match(r"bytes (\d+)-", resp.getheader("Content-Range") or "")
    return int(match.group(1)) if match else None


# ── engine ─────────────────────────────────────────────────────────────────────────────────


def fetch_bytes(
    url: str,
    limit: int,
    seconds: float = PREVIEW_SECONDS,
    https_only: bool = False,
    accept: tuple[str, ...] = (),
) -> bytes | None:
    """At most ``limit`` bytes of one checked URL within ``seconds``, or None.

    Every hop is vetted and pinned by ``open_url``. ``accept`` lists the Content-Type prefixes
    that may be read (empty: any). A body over the limit is refused, not truncated.
    """
    deadline = time.monotonic() + seconds
    conn, resp, _ = open_url(url, https_only=https_only)
    try:
        if resp.status != 200:
            return None
        if accept and not _content_type(resp).startswith(accept):
            return None
        chunks, size = [], 0
        while size <= limit:
            if time.monotonic() > deadline:
                return None
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        conn.close()
    return b"".join(chunks) if 0 < size <= limit else None


def _image_preview(url: str, emit: Emit) -> bytes | None:
    """The image itself for the analyze preview: same URL checks, ≤5 MB, ≤15 s, or nothing.

    The GUI decodes it with its own pixel cap; a failure here only costs the preview.
    """
    try:
        return fetch_bytes(url, MAX_PREVIEW_BYTES)
    except (EngineError, OSError, http.client.HTTPException) as exc:
        emit("log", {"level": "warning", "message": f"image preview failed: {type(exc).__name__}"})
        return None


# ── previews from a web page (plan §5.6) ───────────────────────────────────────────────────
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGE_IMAGE_BYTES = 3 * 1024 * 1024
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_JSON_LD = re.compile(
    r"""<script\b[^>]*type\s*=\s*["']?application/ld\+json[^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_META_KEYS = ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")


def _json_ld_thumbnail(node: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(node, list):
        for item in node[:50]:
            found = _json_ld_thumbnail(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    value = node.get("thumbnailUrl")
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str)), None)
    if isinstance(value, str):
        return value
    for key in ("@graph", "video", "mainEntity", "associatedMedia"):
        found = _json_ld_thumbnail(node.get(key), depth + 1)
        if found:
            return found
    return None


def page_image_url(page: str, base: str) -> str | None:
    """The picture a page names for itself: og:image, twitter:image, then JSON-LD thumbnailUrl.

    Only an https URL is returned (made absolute against ``base``); the fetch re-checks it.
    """
    found: dict[str, str] = {}
    for tag in _META_TAG.findall(page):
        attrs = {m[0].lower(): m[1] or m[2] or m[3] for m in _ATTR.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in _META_KEYS and attrs.get("content") and key not in found:
            found[key] = html.unescape(attrs["content"]).strip()
    candidates = [found[k] for k in _META_KEYS if k in found]
    for block in _JSON_LD.findall(page)[:20]:
        try:
            value = _json_ld_thumbnail(json.loads(block))
        except ValueError:
            continue
        if value:
            candidates.append(value.strip())
    for candidate in candidates:
        absolute = urljoin(base, candidate)
        if urlsplit(absolute).scheme == "https":
            return absolute
    return None


def page_preview(page_url: str, emit: Emit) -> bytes | None:
    """The page's own picture when yt-dlp gave none: one GET for the page, one for the image."""
    try:
        page = fetch_bytes(page_url, MAX_PAGE_BYTES, accept=("text/html", "application/xhtml"))
        if not page:
            return None
        image_url = page_image_url(page.decode("utf-8", "replace"), page_url)
        if not image_url:
            return None
        return fetch_bytes(image_url, MAX_PAGE_IMAGE_BYTES, https_only=True, accept=("image/",))
    except (EngineError, OSError, http.client.HTTPException, ValueError) as exc:
        emit("log", {"level": "warning", "message": f"page preview failed: {type(exc).__name__}"})
        return None


# ── ffprobe / ffmpeg on a direct file (plan §5.6) ──────────────────────────────────────────
# ffmpeg would resolve the host again and follow redirects on its own, outside every check
# above. So it never sees the link: a loopback relay with a random path serves the file to it
# through ``open_url`` (checked and pinned on every hop), and ffmpeg may speak only http/tcp.
RELAY_BYTE_CAP = 64 * 1024 * 1024
PROBE_TIMEOUT = 30
RELAY_SECONDS = 2 * PROBE_TIMEOUT + 5  # the probe, then the frame or cover
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_PROBE_JSON = 1024 * 1024
_CODEC_LABELS = {"opus": "Opus", "vorbis": "Vorbis", "h264": "H.264", "hevc": "H.265"}
_LOCAL_ONLY = ["-protocol_whitelist", "http,tcp"]


class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # nothing reaches stderr
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.server.relay.serve(self)  # type: ignore[attr-defined]


class Relay:
    """``http://127.0.0.1:<port>/<token>`` → Range GETs of one checked URL, capped in total."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.token = secrets.token_urlsafe(24)
        self.served = 0
        self.deadline = time.monotonic() + RELAY_SECONDS
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        self.server.daemon_threads = True
        self.server.relay = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{self.token}"

    def __enter__(self) -> Relay:
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def serve(self, handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True
        if handler.path != "/" + self.token or time.monotonic() > self.deadline:
            handler.send_error(404)
            return
        headers = {}
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", handler.headers.get("Range") or "")
        if match:
            headers["Range"] = match.group(0)
        try:
            conn, resp, _ = open_url(self.url, headers)
        except EngineError:
            handler.send_error(502)
            return
        try:
            if resp.status not in (200, 206):
                handler.send_error(502)
                return
            status, skip, length = resp.status, 0, resp.getheader("Content-Length")
            names = ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]
            total = _total_from(resp, 0) if resp.status == 200 else None
            emulated = bool(match) and total is not None
            if emulated:
                # The site ignored the range and sent the whole file: honour the range here,
                # or ffmpeg reads the start of the file where it asked for the middle.
                start = int(match.group(1))
                end = min(int(match.group(2)) if match.group(2) else total - 1, total - 1)
                if start > end:
                    handler.send_error(416)
                    return
                status, skip, length = 206, start, str(end - start + 1)
                names = ["Content-Type"]
            handler.send_response(status)
            for name in names:
                value = resp.getheader(name)
                if value:
                    handler.send_header(name, value)
            if emulated:
                last = skip + int(length) - 1
                handler.send_header("Content-Range", f"bytes {skip}-{last}/{total}")
                handler.send_header("Content-Length", length)
            handler.send_header("Connection", "close")
            handler.end_headers()
            remaining = int(length) if length and length.isdigit() else None
            while time.monotonic() <= self.deadline and remaining != 0:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                with self.lock:
                    self.served += len(chunk)
                    over = self.served > RELAY_BYTE_CAP
                if over:
                    break
                if skip:
                    dropped = min(skip, len(chunk))
                    chunk, skip = chunk[dropped:], skip - dropped
                if remaining is not None:
                    chunk = chunk[:remaining]
                    remaining -= len(chunk)
                if chunk:
                    handler.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            pass  # ffmpeg hung up once it had the bytes it needed, or the site did
        finally:
            conn.close()


def _run_tool(args: list[str], limit: int) -> bytes | None:
    """stdout of one trusted tool run, or None on failure, timeout or oversized output."""
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) > limit:
        return None
    return proc.stdout


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < float("inf") else None


def parse_probe(raw: bytes) -> dict[str, Any]:
    """The facts a Result card shows, from ffprobe's JSON. Nothing else leaves."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    facts: dict[str, Any] = {}
    fmt = data.get("format")
    if isinstance(fmt, dict):
        duration = _positive(fmt.get("duration"))
        if duration and duration < 10 * 24 * 3600:
            facts["duration"] = round(duration, 3)
        bitrate = _positive(fmt.get("bit_rate"))
        if bitrate:
            facts["tbr"] = round(bitrate / 1000)
    streams = data.get("streams")
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        codec = re.sub(r"[^A-Za-z0-9_.-]", "", str(stream.get("codec_name") or ""))[:20]
        disposition = stream.get("disposition")
        cover = isinstance(disposition, dict) and bool(disposition.get("attached_pic"))
        if stream.get("codec_type") == "video" and cover:
            facts["has_cover"] = True
        elif stream.get("codec_type") == "video" and "vcodec" not in facts:
            facts["vcodec"] = codec or "video"
            for key in ("width", "height"):
                value = stream.get(key)
                if _is_size(value):
                    facts[key] = value
        elif stream.get("codec_type") == "audio" and "acodec" not in facts:
            facts["acodec"] = codec or "audio"
            abr = _positive(stream.get("bit_rate"))
            if abr:
                facts["abr"] = round(abr / 1000)
    return facts


def _is_size(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 20000


def probe_file(url: str, kind: str, emit: Emit) -> tuple[dict[str, Any], bytes | None]:
    """(ffprobe facts, preview image bytes) for a direct video or audio file.

    Video: a frame at about 10% of the duration. Audio: the embedded cover, if any. Without the
    runner's ffprobe/ffmpeg, or on any failure, whatever was learned so far is returned.
    """
    from .ytdlp import trusted_tool  # (import cycle)

    ffprobe, ffmpeg = trusted_tool("ffprobe"), trusted_tool("ffmpeg")
    if ffprobe is None:
        emit("log", {"level": "warning", "message": "ffprobe is missing: no file details"})
        return {}, None
    image = None
    with Relay(url) as relay:
        raw = _run_tool(
            [str(ffprobe), "-v", "error", *_LOCAL_ONLY, "-print_format", "json",
             "-show_format", "-show_streams", relay.local_url],
            MAX_PROBE_JSON,
        )  # fmt: skip
        facts = parse_probe(raw) if raw else {}
        base = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", *_LOCAL_ONLY]
        out = ["-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
        if ffmpeg is not None and kind == "video" and facts.get("vcodec"):
            seek = f"{(facts.get('duration') or 0) * 0.1:.3f}"
            scale = ["-vf", "scale='min(960,iw)':-2"]
            image = _run_tool(
                [*base, "-ss", seek, "-i", relay.local_url, *scale, *out], MAX_FRAME_BYTES
            )
        elif ffmpeg is not None and kind == "audio" and facts.get("has_cover"):
            image = _run_tool(
                [*base, "-i", relay.local_url, "-map", "0:v:0", "-an", *out], MAX_FRAME_BYTES
            )
    if raw is None:
        emit("log", {"level": "warning", "message": "ffprobe could not read the file"})
    return facts, image


class HttpEngine:
    name = "http"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        opts = job.options
        mode = opts.get("mode", "analyze")
        if mode == "analyze":
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", "analyze takes no options")
            return self._analyze(job.url, emit)
        if mode != "download":
            raise EngineError("bad_options", f"unknown mode {mode!r}")
        from ..image_convert import IMAGE_OPTION_KEYS, parse_image_options  # (import cycle)

        if presets.is_row_request(opts):
            opts = row_options(presets.parse_row_request(opts, original_only=True))
            job = replace(job, options=opts)
        allowed = {"mode", "preset", "output_name", "video_container", "extract",
                   "frame_format"} | IMAGE_OPTION_KEYS
        if set(opts) - allowed or opts.get("preset") != PRESET_ID:
            raise EngineError("bad_options", "the direct engine takes only preset=original_file")
        if "output_name" in opts and not isinstance(opts["output_name"], str):
            raise EngineError("bad_options", "'output_name' must be a string")
        if opts.get("video_container", "original") not in VIDEO_CONTAINERS:
            raise EngineError("bad_options", "unknown video container")
        extract = opts.get("extract")
        if extract is not None and not (isinstance(extract, str) and _EXTRACT.fullmatch(extract)):
            raise EngineError("bad_options", "unknown audio or frame choice")
        if opts.get("frame_format", "original") not in FRAME_FORMATS:
            raise EngineError("bad_options", "'frame_format' must be original, jpg, png or webp")
        parse_image_options(opts)  # refused before any request
        return self._download(job, emit)

    def _analyze(self, url: str, emit: Emit) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        # A one-byte range GET, not HEAD: many file hosts answer HEAD wrongly or not at all.
        conn, resp, final = open_url(url, {"Range": "bytes=0-0"})
        try:
            if resp.status not in (200, 206):
                raise http_error(resp.status, resp.reason)
            name = file_name(final, resp)
            ctype = _content_type(resp)
            if not is_media(name, ctype):
                raise EngineError("unsupported", "unsupported url: that link is not a media file")
            total = _total_from(resp, 0)
            resumable = resp.status == 206
        finally:
            conn.close()
        emit("stage", {"stage": "completed"})
        stem, ext = os.path.splitext(name)
        kind, tabs = media_kind(name, ctype)
        info: dict[str, Any] = {
            "extractor": "Direct file",
            "site": "Direct file",
            "ext": ext.lstrip(".").lower(),
            "content_type": ctype[:60],
            "resumable": resumable,
            "formats": [],
        }
        if total is not None:
            info["filesize"] = total
        preview = None
        if kind == "image" and (total is None or total <= MAX_PREVIEW_BYTES):
            data = _image_preview(url, emit)
            if data is not None:
                preview = {"data": base64.b64encode(data).decode("ascii")}
        elif kind in ("video", "audio"):
            facts, image = probe_file(url, kind, emit)
            facts.pop("has_cover", None)
            info.update(facts)
            if facts.get("acodec"):
                codec = _CODEC_LABELS.get(facts["acodec"], facts["acodec"].upper())
                info["source_audio"] = {"codec": codec, "abr_kbps": facts.get("abr")}
            if image is not None:
                preview = {"data": base64.b64encode(image).decode("ascii")}
        info.update(file_rows(kind, info["ext"], total))
        if kind == "video":
            if info.get("height"):
                info["video_rows"][0]["height"] = info["height"]
            info.update(extract_rows(info))
        # A tab is offered only when it has a row (no ffmpeg: a video has no audio/frame rows).
        tabs = [t for t in tabs if info.get(f"{t}_rows")]
        return media_result(kind, tabs, stem or name, url, preview=preview, **info)

    def _download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        folder = Path(job.output_dir)
        if not folder.is_dir():
            raise EngineError("download_error", "the download folder does not exist")
        emit("stage", {"stage": "analyzing"})
        conn, resp, final = open_url(job.url, {"Range": "bytes=0-0"})
        try:
            if resp.status not in (200, 206):
                raise http_error(resp.status, resp.reason)
            name = file_name(final, resp)
            probe_type = _content_type(resp)
            chosen = safe_output_name(job.options.get("output_name"))
            if chosen:
                name = chosen + Path(name).suffix
            if not is_media(name, probe_type):
                raise EngineError("unsupported", "unsupported url: that link is not a media file")
        finally:
            conn.close()

        part_name, meta_name = part_names(name, job.job_id)
        part = folder / part_name
        meta_path = folder / meta_name
        offset = part.stat().st_size if part.is_file() else 0
        validator = ""
        if offset:
            try:
                validator = str(json.loads(meta_path.read_text("utf-8")).get("validator") or "")
            except (OSError, ValueError, AttributeError):
                validator = ""
        headers: dict[str, str] = {}
        if offset and validator:
            headers = {"Range": f"bytes={offset}-", "If-Range": validator}
        else:
            offset = 0  # no validator means we cannot prove the part is the same file

        emit("stage", {"stage": "downloading"})
        conn, resp, final = open_url(final, headers)
        try:
            if resp.status == 416 and offset:
                # The part may already be the whole file; otherwise start clean next time.
                match = re.search(r"/(\d+)$", resp.getheader("Content-Range") or "")
                if match and int(match.group(1)) == offset:
                    return self._finish(folder, name, part, meta_path, emit, job.options)
                part.unlink(missing_ok=True)
                raise EngineError("download_error", "the partial file no longer matches; retry")
            if resp.status not in (200, 206):
                raise http_error(resp.status, resp.reason)
            if probe_type in IMAGE_TYPES and _content_type(resp) != probe_type:
                # The name was chosen from the first answer's type; a different image type now
                # would be saved under the wrong extension.
                raise EngineError("unsupported", "unsupported url: the image type changed")
            if resp.status == 206 and _range_start(resp) != offset:
                raise EngineError("download_error", "the site sent the wrong part of the file")
            if resp.status == 200:
                offset = 0  # the server ignored Range or the file changed: restart
            new_validator = resp.getheader("ETag") or resp.getheader("Last-Modified") or ""
            if new_validator.startswith("W/"):
                new_validator = ""  # a weak ETag is not allowed in If-Range
            meta_path.write_text(json.dumps({"validator": new_validator}), "utf-8")
            total = _total_from(resp, offset)
            self._stream(resp, part, offset, total, emit)
        finally:
            conn.close()
        size = part.stat().st_size
        if total is not None and size != total:
            raise EngineError("download_error", "connection reset: the file arrived incomplete")
        return self._finish(folder, name, part, meta_path, emit, job.options)

    @staticmethod
    def _stream(
        resp: http.client.HTTPResponse, part: Path, offset: int, total: int | None, emit: Emit
    ) -> None:
        done = offset
        started = last = time.monotonic()
        with open(part, "ab" if offset else "wb") as out:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except (OSError, http.client.HTTPException) as exc:
                    raise EngineError("download_error", _network_message(exc)) from exc
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last >= PROGRESS_INTERVAL:
                    last = now
                    emit("progress", progress(done, total, offset, now - started))
        emit("progress", progress(done, total, offset, time.monotonic() - started))

    @staticmethod
    def _finish(
        folder: Path,
        name: str,
        part: Path,
        meta_path: Path,
        emit: Emit,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        final = move_into_place(part, folder, name)
        meta_path.unlink(missing_ok=True)
        from ..image_convert import finish_images, parse_image_options  # (import cycle)

        fmt, background = parse_image_options(options)
        (final_name,), notes = finish_images([str(final)], fmt, background, emit)
        final = Path(final_name)  # the converted file when there was a conversion
        container = options.get("video_container", "original")
        if options.get("extract"):
            emit("stage", {"stage": "converting"})
            frame_format = options.get("frame_format", "original")
            final = extract_media(final, options["extract"], frame_format)
        elif container != "original" and final.suffix.lower() != "." + container:
            emit("stage", {"stage": "converting"})
            final = convert_video(final, container)
        emit("stage", {"stage": "completed"})
        stem = os.path.splitext(final.name)[0]
        result: dict[str, Any] = {
            "title": stem,
            "extractor": "Direct file",
            "preset": PRESET_ID,
            "files": [str(final)],
            "total_bytes": final.stat().st_size,
        }
        if notes:
            result["notes"] = notes
        return result


# ── Result-card rows for a direct file (plan §5.4) ─────────────────────────────────────────
VIDEO_CONTAINERS = ("original", "mp4", "mkv", "webm", "mov", "avi")
CONVERT_TIMEOUT = 3600


def file_rows(kind: str, ext: str, size: int | None) -> dict[str, list[dict[str, Any]]]:
    """The one row a direct file has: the file as the site serves it."""
    row: dict[str, Any] = {
        "id": presets.ORIGINAL_ROW_IDS[kind],
        "original": True,
        "ext": ext,
        "size": size,
        "size_is_estimate": False,
        "default": True,
    }
    if kind == "video":
        row["container"] = ext
    elif kind == "audio":
        row.update({"label": ext.upper() or "Audio", "codec": ext})
    return {f"{kind}_rows": [row]}


def row_options(request: presets.RowRequest) -> dict[str, Any]:
    """A row request in the engine's own option names."""
    opts: dict[str, Any] = {"mode": "download", "preset": PRESET_ID}
    if request.edited_title:
        opts["output_name"] = request.edited_title
    extract = request.row_id.split(":", 1)[1]
    if request.row_id not in presets.ORIGINAL_ROW_IDS.values():
        opts["extract"] = extract  # audio from a video, or one frame of it
        if request.tab == "image":
            opts["frame_format"] = request.container or "original"
        return opts
    if request.tab == "image" and request.container not in (None, "original"):
        opts["image_format"] = request.container
    if request.tab == "video" and request.container:
        opts["video_container"] = request.container
    return opts


def convert_video(source: Path, container: str) -> Path:
    """``source`` saved as ``container`` next to it; the original is removed on success.

    MP4, MKV and WebM are tried as a stream copy first; anything that cannot be copied, and
    MOV/AVI, is re-encoded with ffmpeg's defaults for that container. Only the runner-provided
    ffmpeg is used, with an argument list and a timeout.
    """
    from .ytdlp import trusted_tool  # (import cycle)

    ffmpeg = trusted_tool("ffmpeg")
    if ffmpeg is None:
        raise EngineError("convert_error", "FFmpeg is needed to change the video container")
    temp = source.with_name(f".{source.stem[:40]}.converting.{container}")
    attempts = [["-c", "copy"]] if container not in presets.REENCODE_CONTAINERS else []
    attempts.append([])
    try:
        for codec_args in attempts:
            args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            args += ["-i", str(source), "-map", "0:v?", "-map", "0:a?", *codec_args, str(temp)]
            try:
                proc = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=CONVERT_TIMEOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise EngineError("convert_error", "video conversion timed out") from exc
            except OSError as exc:
                raise EngineError("convert_error", "FFmpeg could not be started") from exc
            if proc.returncode == 0 and temp.is_file() and temp.stat().st_size:
                final = move_into_place(temp, source.parent, f"{source.stem}.{container}")
                source.unlink(missing_ok=True)
                return final
        raise EngineError("convert_error", f"could not save this video as {container.upper()}")
    finally:
        temp.unlink(missing_ok=True)


# ── a direct video's audio and frame rows (plan §5.3: Audio (extract), Image (frame)) ──────
FRAME_FORMATS = {"original": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}
_EXTRACT = re.compile(r"mp3:(320|256|192|128|64)|m4a|opus|flac|wav|frame")
EXTRACT_TIMEOUT = 1800
# (extension, ffmpeg attempts): copying the stream is tried first where the codec may allow it.
_AUDIO_TARGETS = {
    "m4a": ("m4a", [["-c:a", "copy"], ["-c:a", "aac", "-b:a", "256k"]]),
    "opus": ("opus", [["-c:a", "copy"], ["-c:a", "libopus", "-b:a", "160k"]]),
    "flac": ("flac", [["-c:a", "flac"]]),
    "wav": ("wav", [["-c:a", "pcm_s16le"]]),
}


def extract_rows(facts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Audio rows (when the video has sound) and a frame row, for a probed direct video.

    Offered only with the runner's ffmpeg, which does the work after the download."""
    from .ytdlp import audio_rows, trusted_tool  # (import cycle)

    if trusted_tool("ffmpeg") is None or not facts.get("vcodec"):
        return {}
    rows: dict[str, list[dict[str, Any]]] = {}
    if facts.get("acodec"):
        source = {"vcodec": "none", "acodec": facts["acodec"], "abr": facts.get("abr")}
        rows["audio_rows"] = audio_rows([source], facts.get("duration"))
    frame: dict[str, Any] = {"id": "i:frame", "frame": True, "ext": "jpg", "default": True}
    frame.update({k: facts[k] for k in ("width", "height") if k in facts})
    rows["image_rows"] = [frame]
    return rows


def extract_media(video: Path, what: str, image_format: str) -> Path:
    """``what`` (a row's ``mp3:<kbps>``, ``m4a``, ``opus``, ``flac``, ``wav`` or ``frame``) from
    the downloaded ``video``, saved next to it; the video is removed on success.

    The runner's ffmpeg reads only this local file, with an argument list and a timeout."""
    from .ytdlp import trusted_tool  # (import cycle)

    ffmpeg = trusted_tool("ffmpeg")
    if ffmpeg is None:
        raise EngineError("convert_error", "FFmpeg is needed to take the audio or a frame")
    if what == "frame":
        ext = FRAME_FORMATS[image_format]
        facts = {}
        ffprobe = trusted_tool("ffprobe")
        if ffprobe is not None:
            raw = _run_tool(
                [str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
                 str(video)],
                MAX_PROBE_JSON,
            )  # fmt: skip
            facts = parse_probe(raw) if raw else {}
        seek = f"{(facts.get('duration') or 0) * 0.1:.3f}"
        attempts = [["-ss", seek, "-i", str(video), "-frames:v", "1", "-update", "1"]]
    elif what.startswith("mp3:"):
        ext = "mp3"
        kbps = what.split(":")[1]
        attempts = [["-i", str(video), "-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k"]]
    else:
        ext, codecs = _AUDIO_TARGETS[what]
        attempts = [["-i", str(video), "-vn", *codec] for codec in codecs]
    temp = video.with_name(f".{video.stem[:40]}.extracting.{ext}")
    try:
        for args in attempts:
            cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
            if what != "frame":
                cmd += ["-map_metadata", "0"]  # the file's own tags, if it has any
            try:
                proc = subprocess.run(
                    [*cmd, str(temp)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=EXTRACT_TIMEOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise EngineError("convert_error", "taking the audio or frame timed out") from exc
            except OSError as exc:
                raise EngineError("convert_error", "FFmpeg could not be started") from exc
            if proc.returncode == 0 and temp.is_file() and temp.stat().st_size:
                final = move_into_place(temp, video.parent, f"{video.stem}.{ext}")
                video.unlink(missing_ok=True)
                return final
        target = "a frame" if what == "frame" else ext.upper()
        raise EngineError("convert_error", f"could not take {target} from this video")
    finally:
        temp.unlink(missing_ok=True)


def progress(done: int, total: int | None, offset: int, elapsed: float) -> dict[str, Any]:
    """One progress event body. Speed counts only this run's bytes, not the resumed part."""
    speed = (done - offset) / elapsed if elapsed > 0 else None
    eta = (total - done) / speed if speed and total is not None and total >= done else None
    return {
        "downloaded_bytes": done,
        "total_bytes": total,
        "percent": round(100 * done / total, 1) if total else None,
        "speed": speed,
        "eta": round(eta, 1) if eta is not None else None,
    }
