"""Offline fake engine: emits realistic stages and progress without touching the network or disk.

Options: ``steps`` (int, 1-1000), ``delay`` (seconds per step, 0-5), ``fail`` (bool),
``total_bytes`` (int).
"""

from __future__ import annotations

import time
from typing import Any

from ..protocol import JobSpec
from .base import Emit, EngineError


def _bounded(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return min(high, max(low, value))


class FakeEngine:
    name = "fake"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        opts = job.options
        steps = int(_bounded(opts.get("steps"), 10, 1, 1000))
        delay = _bounded(opts.get("delay"), 0.05, 0, 5)
        total = int(_bounded(opts.get("total_bytes"), 1_000_000, 1, 10**12))

        emit("stage", {"stage": "downloading"})
        for i in range(1, steps + 1):
            time.sleep(delay)
            done = total * i // steps
            emit(
                "progress",
                {
                    "downloaded_bytes": done,
                    "total_bytes": total,
                    "percent": round(100 * i / steps, 1),
                    "speed": total / steps / delay if delay else None,
                    "eta": round((steps - i) * delay, 2),
                },
            )
            if opts.get("fail") is True and i * 2 >= steps:
                raise EngineError("fake_failure", "Fake engine failed as requested")

        emit("stage", {"stage": "completed"})
        return {"title": f"Fake download of {job.url}", "files": [], "total_bytes": total}
