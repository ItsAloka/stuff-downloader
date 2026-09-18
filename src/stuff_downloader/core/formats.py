"""Resolution grouping (plan §5.5) over the sanitized formats an analyze job returns. No Qt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .presets import HEIGHTS


@dataclass(frozen=True)
class ResolutionChoice:
    height: int
    fps: int | None
    hdr: bool
    size: int | None  # best video at this height + best audio, in bytes
    approx: bool

    @property
    def label(self) -> str:
        text = f"{self.height}p"
        if self.fps and self.fps > 30:
            text += f"{self.fps}"
        if self.hdr:
            text += " HDR"
        if self.size:
            text += f"  ·  {'~' if self.approx else ''}{_mb(self.size)}"
        return text


def _mb(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    return f"{size / 1024**2:.0f} MB"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _size(fmt: dict[str, Any]) -> tuple[int | None, bool]:
    exact = _num(fmt.get("filesize"))
    if exact:
        return int(exact), False
    approx = _num(fmt.get("filesize_approx"))
    return (int(approx), True) if approx else (None, False)


def _bucket(height: float) -> int | None:
    """Snap odd heights (e.g. 1076 for cropped video) up to the nearest standard height."""
    for standard in sorted(HEIGHTS):
        if height <= standard * 1.05:
            return standard
    return None


def _has_video(fmt: dict[str, Any]) -> bool:
    return fmt.get("vcodec") not in (None, "none") and _num(fmt.get("height")) is not None


def _has_audio_only(fmt: dict[str, Any]) -> bool:
    return fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") in (None, "none")


def resolution_choices(formats: list[dict[str, Any]]) -> list[ResolutionChoice]:
    """Only heights that exist, highest first."""
    best_audio: tuple[int | None, bool] = (None, False)
    for fmt in formats:
        if isinstance(fmt, dict) and _has_audio_only(fmt):
            size = _size(fmt)
            if size[0] and (best_audio[0] is None or size[0] > best_audio[0]):
                best_audio = size

    groups: dict[int, list[dict[str, Any]]] = {}
    for fmt in formats:
        if not isinstance(fmt, dict) or not _has_video(fmt):
            continue
        bucket = _bucket(_num(fmt.get("height")) or 0)
        if bucket:
            groups.setdefault(bucket, []).append(fmt)

    choices = []
    for height in sorted(groups, reverse=True):
        group = groups[height]
        fps_values = [int(f) for f in (_num(g.get("fps")) for g in group) if f]
        sizes = [_size(g) for g in group]
        known = [s for s in sizes if s[0]]
        video = max(known, key=lambda s: s[0]) if known else (None, False)
        size = None
        if video[0]:
            size = video[0] + (best_audio[0] or 0)
        choices.append(
            ResolutionChoice(
                height=height,
                fps=max(fps_values) if fps_values else None,
                hdr=any(str(g.get("dynamic_range") or "SDR").upper() != "SDR" for g in group),
                size=size,
                approx=video[1] or best_audio[1],
            )
        )
    return choices
