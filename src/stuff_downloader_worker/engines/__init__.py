"""Engine registry. Full yt-dlp, gallery-dl, spotDL and HTTP engines arrive in later milestones."""

from __future__ import annotations

from .base import Engine
from .fake import FakeEngine
from .probe import ProbeEngine
from .ytdlp import YtDlpEngine

ENGINES: dict[str, type] = {"fake": FakeEngine, "probe": ProbeEngine, "ytdlp": YtDlpEngine}


def get_engine(name: str) -> Engine | None:
    cls = ENGINES.get(name)
    return cls() if cls else None
