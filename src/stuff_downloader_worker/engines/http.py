"""Direct HTTP engine (plan §2, §M3): a plain media file link, streamed to disk with resume.

Stdlib only, so it runs in any engine env. Options: ``mode`` "analyze" (default) or
"download"; download also takes ``preset`` = "original_file". Nothing else is accepted.

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

import http.client
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import time
import unicodedata
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from ..protocol import JobSpec
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


def check_url(url: str) -> Target:
    """Parse and vet one URL (the pasted one or a redirect). Raises EngineError when refused."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise EngineError("unsupported", "that is not a valid link") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
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
    url: str, headers: dict[str, str] | None = None, method: str = "GET"
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, str]:
    """Follow up to MAX_REDIRECTS, vetting every hop. Returns (conn, response, final url)."""
    for _ in range(MAX_REDIRECTS + 1):
        target = check_url(url)
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
    if "." not in name:
        guessed = mimetypes.guess_extension(_content_type(resp)) or ""
        if guessed.lower() in MEDIA_EXTENSIONS:
            name += guessed
    return name


def is_media(name: str, content_type: str) -> bool:
    """Whether a response is a media file: a media type, or a generic one with a media name."""
    if content_type.split("/")[0] in ("video", "audio", "image"):
        return content_type not in ("image/svg+xml",)
    ext = os.path.splitext(name)[1].lower()
    return content_type in _GENERIC_TYPES and ext in MEDIA_EXTENSIONS


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
        if set(opts) - {"mode", "preset"} or opts.get("preset") != PRESET_ID:
            raise EngineError("bad_options", "the direct engine takes only preset=original_file")
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
        info: dict[str, Any] = {
            "kind": "file",
            "title": stem or name,
            "extractor": "Direct file",
            "ext": ext.lstrip(".").lower(),
            "content_type": ctype[:60],
            "resumable": resumable,
            "formats": [],
        }
        if total is not None:
            info["filesize"] = total
        return info

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
            if not is_media(name, _content_type(resp)):
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
                    return self._finish(folder, name, part, meta_path, offset, emit)
                part.unlink(missing_ok=True)
                raise EngineError("download_error", "the partial file no longer matches; retry")
            if resp.status not in (200, 206):
                raise http_error(resp.status, resp.reason)
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
        return self._finish(folder, name, part, meta_path, size, emit)

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
        folder: Path, name: str, part: Path, meta_path: Path, size: int, emit: Emit
    ) -> dict[str, Any]:
        final = move_into_place(part, folder, name)
        meta_path.unlink(missing_ok=True)
        emit("stage", {"stage": "completed"})
        stem = os.path.splitext(final.name)[0]
        return {
            "title": stem,
            "extractor": "Direct file",
            "preset": PRESET_ID,
            "files": [str(final)],
            "total_bytes": size,
        }


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
