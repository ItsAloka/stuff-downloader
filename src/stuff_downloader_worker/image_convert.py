"""Local image format conversion (item 6): Original / JPG / PNG, done with the bundled ffmpeg.

Only the runner-provided ffmpeg is used (``engines.ytdlp.trusted_tool``), never one found on
PATH. ffmpeg gets an argument list, no shell, no stdin and a timeout. The output is written to a
hidden temporary file next to the source and then moved into place with
``http.move_into_place``, so an existing file is never overwritten: a taken name becomes
``name (2).jpg``. On success the downloaded original is removed (the owner asked for the other
format); if anything fails, the temporary file is deleted and the original is left as it was.

- JPG has no alpha channel, so a transparent image is flattened onto ``background`` (white by
  default) instead of letting the encoder turn transparency black.
- An animated GIF or WebP keeps only its first frame, and the result says so in ``note``.
- ``original``, or a file that already has the target format, is returned unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .engines.base import EngineError
from .engines.http import move_into_place
from .engines.ytdlp import trusted_tool

FORMATS = ("original", "jpg", "png")
_EXTENSIONS = {"jpg": (".jpg", ".jpeg"), "png": (".png",)}
_BACKGROUND = re.compile(r"#[0-9a-fA-F]{6}")
TIMEOUT = 120
FIRST_FRAME_NOTE = "Animated image: only the first frame was kept."
LOCAL_FORMATS = {"jpg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "bmp": ".bmp"}


@dataclass(frozen=True)
class ConvertResult:
    path: Path
    converted: bool
    note: str | None = None


def is_animated(path: Path) -> bool:
    """True for an animated GIF or WebP, judged from the file's own header bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(1 << 20)
    except OSError:
        return False
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        # Extended WebP: the VP8X chunk's flag byte has bit 1 set for animation.
        return head[12:16] == b"VP8X" and len(head) > 20 and bool(head[20] & 0x02)
    if head[:6] in (b"GIF87a", b"GIF89a"):
        # More than one graphic control extension means more than one frame.
        return head.count(b"\x21\xf9\x04") > 1 or b"NETSCAPE2.0" in head
    return False


def _ffmpeg_args(ffmpeg: Path, source: Path, output: Path, fmt: str, background: str) -> list[str]:
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
    if fmt == "jpg":
        colour = "0x" + background[1:]
        graph = (
            "[0:v]format=rgba,split[fg][ref];"
            f"[ref]format=rgb24,drawbox=x=0:y=0:w=iw:h=ih:c={colour}:t=fill[bg];"
            "[bg][fg]overlay=format=auto,format=yuvj444p"
        )
        args += ["-filter_complex", graph, "-q:v", "2"]
    return args + ["-frames:v", "1", "-update", "1", str(output)]


def convert_image(
    source: Path,
    fmt: str,
    *,
    background: str = "#ffffff",
    ffmpeg: Path | None = None,
    timeout: float = TIMEOUT,
    keep_original: bool = False,
) -> ConvertResult:
    """Convert ``source`` to ``fmt`` next to it; return where the final file is.

    On success the downloaded original is removed unless ``keep_original``; on failure it is
    always kept.
    """
    if fmt not in FORMATS:
        raise EngineError("bad_options", "unknown image format")
    if not _BACKGROUND.fullmatch(background):
        raise EngineError("bad_options", "background must be a #rrggbb colour")
    if fmt == "original" or source.suffix.lower() in _EXTENSIONS[fmt]:
        return ConvertResult(source, converted=False)
    if not source.is_file():
        raise EngineError("convert_error", "the downloaded image is missing")
    ffmpeg = ffmpeg or trusted_tool("ffmpeg")
    if ffmpeg is None or not ffmpeg.is_file():
        raise EngineError("convert_error", "FFmpeg is needed to convert images")

    ext = _EXTENSIONS[fmt][0]
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{source.stem[:40]}.", suffix=f".converting{ext}", dir=source.parent
    )
    os.close(handle)
    temp = Path(temp_name)
    try:
        try:
            proc = subprocess.run(
                _ffmpeg_args(ffmpeg, source, temp, fmt, background),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("convert_error", "image conversion timed out") from exc
        except OSError as exc:
            raise EngineError("convert_error", "FFmpeg could not be started") from exc
        try:
            written = temp.stat().st_size
        except OSError:
            written = 0  # ffmpeg exited without leaving (or keeping) an output file
        if proc.returncode != 0 or written == 0:
            raise EngineError("convert_error", f"could not convert this image to {fmt.upper()}")
        final = move_into_place(temp, source.parent, source.stem + ext)
    finally:
        temp.unlink(missing_ok=True)
    note = FIRST_FRAME_NOTE if is_animated(source) else None
    if not keep_original:
        try:
            source.unlink()
        except OSError:
            pass  # the converted file is in place; a locked original is only clutter
    return ConvertResult(final, converted=True, note=note)


def convert_local_image(
    source: Path,
    destination: Path,
    fmt: str,
    *,
    background: str = "#ffffff",
    quality: int = 90,
    job_id: str = "local",
    ffmpeg: Path | None = None,
    emit=None,
    timeout: float = TIMEOUT,
) -> ConvertResult:
    """Convert a local file without deleting it or replacing an existing output."""
    if fmt not in LOCAL_FORMATS or not _BACKGROUND.fullmatch(background):
        raise EngineError("bad_options", "invalid image format or background colour")
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise EngineError("bad_options", "quality must be between 1 and 100")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise EngineError("bad_options", "invalid conversion job ID")
    if not source.is_file():
        raise EngineError("convert_error", "source image does not exist")
    if not destination.is_dir():
        raise EngineError("convert_error", "destination folder does not exist")
    ffmpeg = ffmpeg or trusted_tool("ffmpeg")
    if ffmpeg is None or not ffmpeg.is_file():
        raise EngineError("convert_error", "FFmpeg is needed to convert images")

    ext = LOCAL_FORMATS[fmt]
    # mkdtemp claims an exclusive private directory. The worker may be killed on cancel,
    # so the parent receives its path and removes that directory after the process exits.
    temp_dir = Path(tempfile.mkdtemp(
        prefix=f".{source.stem[:40]}.{job_id}.", suffix=".converting", dir=destination
    ))
    temp = temp_dir / ("image" + ext)
    animated = is_animated(source)
    args = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
    if fmt == "jpg":
        colour = "0x" + background[1:]
        graph = (
            "[0:v]format=rgba,split[fg][ref];"
            f"[ref]format=rgb24,drawbox=x=0:y=0:w=iw:h=ih:c={colour}:t=fill[bg];"
            "[bg][fg]overlay=format=auto,format=yuvj444p"
        )
        args += ["-filter_complex", graph, "-q:v", str(max(2, round((101 - quality) * 30 / 100)))]
    elif fmt == "webp":
        args += ["-quality", str(quality)]
    args += ["-frames:v", "1", "-update", "1", str(temp)]
    if emit is not None:
        emit("stage", {"stage": "converting", "temporary_path": str(temp_dir)})
    try:
        try:
            proc = subprocess.run(
                args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("convert_error", "image conversion timed out") from exc
        except OSError as exc:
            raise EngineError("convert_error", "FFmpeg could not be started") from exc
        if proc.returncode or not temp.is_file() or temp.stat().st_size == 0:
            raise EngineError("convert_error", f"could not convert this image to {fmt.upper()}")
        final = move_into_place(temp, destination, source.stem + ext)
    finally:
        shutil.rmtree(temp_dir)
    return ConvertResult(final, converted=True, note=FIRST_FRAME_NOTE if animated else None)


# ── job options and the engines' shared finishing step (item 6B) ──────────────────────────
IMAGE_OPTION_KEYS = frozenset({"image_format", "image_background"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"})
DEFAULT_BACKGROUND = "#ffffff"


def parse_image_options(options: dict) -> tuple[str, str]:
    """``(format, background)`` from job options; refuses anything but the listed values."""
    fmt = options.get("image_format", "original")
    background = options.get("image_background", DEFAULT_BACKGROUND)
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise EngineError("bad_options", "'image_format' must be original, jpg or png")
    if not isinstance(background, str) or not _BACKGROUND.fullmatch(background):
        raise EngineError("bad_options", "'image_background' must be a #rrggbb colour")
    return fmt, background


def finish_images(
    paths: list[str], fmt: str, background: str, emit
) -> tuple[list[str], list[str]]:
    """Convert every downloaded image in ``paths``; return the final paths and any notes.

    Videos and other files pass through untouched. A conversion that fails keeps the original
    (convert_image never deletes it then) and says so: the download itself still succeeded.
    """
    if fmt == "original":
        return list(paths), []
    finals: list[str] = []
    notes: list[str] = []
    started = False
    for raw in paths:
        path = Path(raw)
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            finals.append(raw)
            continue
        if not started:
            emit("stage", {"stage": "converting"})
            started = True
        try:
            result = convert_image(path, fmt, background=background)
        except EngineError:
            finals.append(raw)
            note = f"Could not convert to {fmt.upper()}; kept the original."
        else:
            finals.append(str(result.path))
            note = result.note
        if note and note not in notes:
            notes.append(note)
    return finals, notes
