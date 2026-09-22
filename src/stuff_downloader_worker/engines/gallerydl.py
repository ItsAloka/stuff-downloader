"""gallery-dl engine (plan §2, §M4): photo posts, carousels, stories, albums and media timelines.

gallery-dl is GPLv2-only (plan §8.3), so it lives only in its own engine env and is imported
only here, inside the worker process. core/ and gui/ never import it; they see only the
JSON-lines events below.

Options: ``mode`` "analyze" (default) or "download". Download takes ``preset`` =
"gallery_original", ``items`` (the 1-based positions from analyze to fetch, at least one) and
optionally ``archive``. Any mode may carry ``site_login`` (plan §6.4).

Three gallery-dl behaviours are switched off on purpose:
- its console output would print file paths onto stdout, which is our protocol channel;
- ``cookies-update`` would rewrite the owner's cookies.txt at the end of every job;
- a cookie source that fails to load is only a warning there, so it is loaded here first and a
  failure is reported as ``cookies_unavailable`` with fixed text.

Nothing site-supplied leaves as-is: analyze returns rebuilt rows (position, kind, extension,
size) and small previews as bytes, never an item URL, and failure text is URL- and
secret-redacted.
"""

from __future__ import annotations

import base64
import collections
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import site_login
from ..protocol import JobSpec
from .base import Emit, EngineError
from .http import is_public_name
from .ytdlp import redact_urls

PRESET_ID = "gallery_original"
MAX_ITEMS = 500
MAX_PREVIEWS = 60
MAX_PREVIEW_BYTES = 2_000_000
MAX_TEXT = 300
ARCHIVE_FILENAME = ".stuff-downloader-gallery-archive.sqlite3"
PREVIEW_TIMEOUT = 15

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "heic"})
VIDEO_EXTS = frozenset({"mp4", "webm", "mov", "m4v", "mkv"})
_EXT = re.compile(r"[a-z0-9]{1,6}")

# Social sites watch request rates; a logged-in owner's account is what pays for a burst
# (plan §6.1, §12). Seconds, as gallery-dl's "a-b" random range.
SOCIAL_CATEGORIES = frozenset({"instagram", "tiktok", "twitter", "facebook"})
SOCIAL_SLEEP_REQUEST = "2.0-4.0"
SOCIAL_SLEEP_DOWNLOAD = "1.0-2.0"

# (substring of gallery-dl's error text, lower-case) -> the phrase errors.friendly_message and
# errors.needs_site_login already understand. First match wins.
_ERROR_PHRASES: tuple[tuple[str, str, str], ...] = (
    ("noextractorerror", "unsupported", "unsupported url"),
    ("no suitable extractor", "unsupported", "unsupported url"),
    ("authrequired", "download_error", "login required"),
    ("authorizationerror", "download_error", "login required"),
    ("authenticationerror", "download_error", "login required"),
    ("login required", "download_error", "login required"),
    ("redirect to login", "download_error", "login required"),  # Instagram, anonymous
    ("private", "download_error", "account is private"),
    ("notfounderror", "download_error", "http error 404"),
    ("could not be found", "download_error", "http error 404"),
    ("challengeerror", "download_error", "confirm you're not a bot"),
    ("429", "download_error", "http error 429"),
)


class _ErrorLog(logging.Handler):
    """Keeps gallery-dl's error lines (it reports failures by logging, not raising)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.errors: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            try:
                self.errors.append(record.getMessage())
            except Exception:  # a bad format string must not break the job
                self.errors.append(str(record.msg))


def describe_error(text: str) -> tuple[str, str]:
    """(code, safe message) for gallery-dl failure text. The message never carries a URL."""
    safe = site_login.redact_secrets(redact_urls(text or "")).strip()[:500]
    lowered = safe.lower()
    for needle, code, phrase in _ERROR_PHRASES:
        if needle in lowered:
            return code, f"{phrase}: {safe}" if phrase not in lowered else safe
    return "download_error", safe or "the gallery could not be read"


def _short(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_TEXT] or None


def _dimension(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100_000:
        return value
    return None


def _ext(kwdict: dict[str, Any], url: str) -> str:
    ext = kwdict.get("extension")
    if isinstance(ext, str) and _EXT.fullmatch(ext.lower()):
        return ext.lower()
    tail = urlsplit(url).path.rsplit(".", 1)
    return tail[1].lower() if len(tail) == 2 and _EXT.fullmatch(tail[1].lower()) else ""


def item_row(position: int, url: str, kwdict: dict[str, Any]) -> dict[str, Any]:
    """One gallery item as the GUI may see it. Rebuilt; the item URL is never included."""
    ext = _ext(kwdict, url)
    kind = "video" if ext in VIDEO_EXTS else "image" if ext in IMAGE_EXTS else "file"
    row: dict[str, Any] = {"index": position, "kind": kind, "ext": ext}
    for key in ("width", "height"):
        value = _dimension(kwdict.get(key))
        if value:
            row[key] = value
    return row


def _title(post: dict[str, Any], extractor_name: str) -> str:
    for key in ("title", "description", "content", "caption", "gallery_title", "album"):
        text = _short(post.get(key))
        if text:
            return text
    return f"{extractor_name} gallery"


def _uploader(post: dict[str, Any]) -> str | None:
    for key in ("username", "author_name", "fullname", "uploader", "user"):
        value = post.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("username")
        text = _short(value)
        if text:
            return text
    return None


def parse_items(options: dict[str, Any]) -> list[int]:
    items = options.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise EngineError("bad_options", "'items' must list 1 to 500 positions")
    clean: list[int] = []
    for value in items:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ITEMS:
            raise EngineError("bad_options", f"invalid item position: {value!r}")
        if value not in clean:
            clean.append(value)
    return sorted(clean)


def image_range(items: list[int]) -> str:
    """gallery-dl's image-range for exactly these positions, e.g. [1, 2, 3, 7] -> "1-3,7"."""
    parts, start, prev = [], items[0], items[0]
    for value in items[1:] + [None]:  # type: ignore[list-item]
        if value is not None and value == prev + 1:
            prev = value
            continue
        parts.append(f"{start}-{prev}" if prev != start else str(start))
        if value is not None:
            start = prev = value
    return ",".join(parts)


class GalleryDlEngine:
    name = "gallerydl"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        try:
            import gallery_dl  # noqa: F401 - probe, the submodules are imported below
            from gallery_dl import config, extractor, version
            from gallery_dl import job as gjob
            from gallery_dl.extractor.message import Message
        except ImportError as exc:
            raise EngineError("engine_missing", f"gallery-dl is not installed here: {exc}") from exc

        opts = dict(job.options)
        login = opts.pop("site_login", None)
        login_source = self._login_source(login) if login is not None else None
        mode = opts.get("mode", "analyze")
        if mode == "analyze":
            if set(opts) - {"mode"}:
                raise EngineError("bad_options", "analyze takes no options")
            items: list[int] = []
        elif mode == "download":
            from ..image_convert import IMAGE_OPTION_KEYS, parse_image_options

            if (
                set(opts) - ({"mode", "preset", "items", "archive"} | IMAGE_OPTION_KEYS)
                or opts.get("preset") != PRESET_ID
            ):
                raise EngineError("bad_options", "gallery download takes preset=gallery_original")
            if not isinstance(opts.get("archive", False), bool):
                raise EngineError("bad_options", "'archive' must be true or false")
            parse_image_options(opts)
            items = parse_items(opts)
        else:
            raise EngineError("bad_options", f"unknown mode {mode!r}")

        extr = extractor.find(job.url)
        if extr is None:
            raise EngineError("unsupported", "unsupported url: no gallery found at that link")
        category = str(getattr(extr, "category", "") or "")

        self._configure(config, category, login_source)
        log = _ErrorLog()
        root = logging.getLogger()
        root.addHandler(log)
        previous_level = root.level
        root.setLevel(logging.WARNING)
        try:
            if mode == "analyze":
                return self._analyze(gjob, Message, extr, category, emit, version.__version__)
            archive = bool(opts.get("archive"))
            return self._download(gjob, config, job, extr, items, archive, log, emit)
        finally:
            root.removeHandler(log)
            root.setLevel(previous_level)

    # ── setup ──────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _login_source(login: Any) -> Any:
        """The cookie source gallery-dl should use, loaded once here so a failure is ours."""
        ydl = site_login.ydl_options(login)  # the same validation the yt-dlp engine applies
        if "cookiefile" in ydl:
            path = ydl["cookiefile"]
            try:
                from gallery_dl import util

                with open(path, encoding="utf-8") as fp:
                    util.cookiestxt_load(fp)
            except Exception:
                raise EngineError("cookies_unavailable", site_login.COOKIES_FAILED) from None
            return path
        browser, profile = ydl["cookiesfrombrowser"][:2]
        source = [browser] if not profile else [browser, profile]
        try:
            from gallery_dl.cookies import load_cookies
            from gallery_dl.extractor import common

            cookies = load_cookies(source)
        except Exception:
            raise EngineError("cookies_unavailable", site_login.COOKIES_FAILED) from None
        common.CACHE_COOKIES[tuple(source)] = cookies  # the extractor reuses this load
        return source

    @staticmethod
    def _configure(config: Any, category: str, login_source: Any) -> None:
        config.clear()  # never read the owner's own gallery-dl config files
        config.set(("output",), "mode", "null")  # stdout is the protocol channel
        config.set(("output",), "progress", False)
        config.set(("output",), "skip", False)
        config.set(("extractor",), "path-restrict", "windows")
        config.set(("extractor",), "path-remove", "\u0000-\u001f\u007f")
        config.set(("extractor",), "cookies-update", False)
        config.set(("extractor",), "image-range", f"1-{MAX_ITEMS}")
        config.set(("extractor",), "retries", 2)
        config.set(("extractor",), "timeout", 30)
        config.set(("downloader",), "part", True)
        config.set(("downloader",), "progress", None)
        if category in SOCIAL_CATEGORIES:
            config.set(("extractor",), "sleep-request", SOCIAL_SLEEP_REQUEST)
            config.set(("extractor",), "sleep", SOCIAL_SLEEP_DOWNLOAD)
        if login_source is not None:
            config.set(("extractor",), "cookies", login_source)

    # ── analyze ────────────────────────────────────────────────────────────────────────────
    def _analyze(
        self, gjob: Any, message: Any, extr: Any, category: str, emit: Emit, engine_version: str
    ) -> dict[str, Any]:
        emit("stage", {"stage": "analyzing"})
        data = gjob.DataJob(extr, file=None, resolve=True)
        data.run()
        if data.exception is not None and not data.data_urls:
            name = data.exception.__class__.__name__
            raise EngineError(*describe_error(f"{name}: {data.exception}"))
        posts = [
            m[1] for m in data.data if m and m[0] == message.Directory and isinstance(m[1], dict)
        ]
        post = posts[0] if posts else {}
        rows = []
        urls: list[str] = []
        for msg in data.data:
            if not msg or msg[0] != message.Url or len(msg) < 3:
                continue  # Message.Url only; unresolved queue entries are not downloadable
            url, kwdict = msg[1], msg[2] if isinstance(msg[2], dict) else {}
            if not isinstance(url, str):
                continue
            rows.append(item_row(len(rows) + 1, url, kwdict))
            urls.append(url)
            if len(rows) >= MAX_ITEMS:
                break
        if not rows:
            # A queue-resolved child job (e.g. a profile link) records its failure in the shared
            # data as (-1, {"error", "message"}); the parent's .exception never sees it.
            failures = [
                m[1] for m in data.data if m and m[0] == -1 and isinstance(m[-1], dict)
            ]
            if failures:
                last = failures[-1]
                raise EngineError(*describe_error(f"{last.get('error')}: {last.get('message')}"))
            raise EngineError("unsupported", "no media information found in that gallery")
        self._attach_previews(extr, rows, urls, emit)
        emit("stage", {"stage": "completed"})
        return {
            "kind": "gallery",
            "title": _title(post, category.capitalize() or "Gallery"),
            "uploader": _uploader(post),
            "extractor": category.capitalize() or "Gallery",
            "items": rows,
            "truncated": len(data.data_urls) > MAX_ITEMS,
            "engine_version": engine_version,
        }

    @staticmethod
    def _attach_previews(
        extr: Any, rows: list[dict[str, Any]], urls: list[str], emit: Emit
    ) -> None:
        """Small previews for the image grid, as bytes. A missing preview never fails analyze."""
        session = getattr(extr, "session", None)
        if session is None:
            return
        fetched = 0
        for row, url in zip(rows, urls, strict=True):
            if fetched >= MAX_PREVIEWS or row["kind"] != "image":
                continue
            parts = urlsplit(url)
            if parts.scheme != "https" or not is_public_name((parts.hostname or "").lower()):
                continue
            fetched += 1
            try:
                with session.get(url, stream=True, timeout=PREVIEW_TIMEOUT) as resp:
                    if resp.status_code != 200:
                        continue
                    data = resp.raw.read(MAX_PREVIEW_BYTES + 1, decode_content=True)
            except Exception:
                emit("log", {"level": "warning", "message": "a gallery preview could not load"})
                continue
            if data and len(data) <= MAX_PREVIEW_BYTES:
                row["thumbnail"] = {"data": base64.b64encode(data).decode("ascii")}

    # ── download ───────────────────────────────────────────────────────────────────────────
    def _download(
        self,
        gjob: Any,
        config: Any,
        job: JobSpec,
        extr: Any,
        items: list[int],
        archive: bool,
        log: _ErrorLog,
        emit: Emit,
    ) -> dict[str, Any]:
        folder = Path(job.output_dir)
        if not folder.is_dir():
            raise EngineError("download_error", "the download folder does not exist")
        config.set(("extractor",), "base-directory", str(folder))
        config.set(("extractor",), "directory", [])
        config.set(("extractor",), "image-range", image_range(items))
        if archive:
            # Derived from the trusted output folder; the job never carries a path.
            config.set(("extractor",), "archive", str(folder / ARCHIVE_FILENAME))

        files: list[str] = []
        skipped = [0]
        total = len(items)

        def progress() -> None:
            done = len(files) + skipped[0]
            emit(
                "progress",
                {
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "percent": round(100 * done / total, 1),
                    "speed": None,
                    "eta": None,
                },
            )

        def after(pathfmt: Any) -> None:
            path = getattr(pathfmt, "path", "")
            if path and path not in files:
                files.append(path)
            progress()

        def skip(pathfmt: Any) -> None:
            skipped[0] += 1
            progress()

        emit("stage", {"stage": "downloading"})
        dl = gjob.DownloadJob(extr)
        # hooks is an empty tuple until gallery-dl sets up postprocessors, which we never
        # configure, so it is given the mapping register_hooks expects.
        dl.hooks = collections.defaultdict(list)
        dl.register_hooks({"after": after, "skip": skip})
        status = dl.run()

        existing = [p for p in files if Path(p).is_file()]
        if not existing and skipped[0] == 0:
            raise EngineError(*describe_error(log.errors[-1] if log.errors else "no_output"))
        if status and existing:
            emit(
                "log", {"level": "warning", "message": "some gallery items could not be downloaded"}
            )
        if not existing:
            emit("stage", {"stage": "completed"})
            return {
                "title": "Gallery",
                "preset": PRESET_ID,
                "files": [],
                "total_bytes": 0,
                "skipped": True,
                "skipped_reason": "Already downloaded",
            }
        from ..image_convert import finish_images, parse_image_options

        fmt, background = parse_image_options(job.options)
        existing, notes = finish_images(existing, fmt, background, emit)
        emit("stage", {"stage": "completed"})
        result = {
            "title": Path(existing[0]).stem,
            "preset": PRESET_ID,
            "files": existing,
            "total_bytes": sum(os.path.getsize(p) for p in existing),
            "item_count": len(existing),
        }
        if notes:
            result["notes"] = notes
        return result
