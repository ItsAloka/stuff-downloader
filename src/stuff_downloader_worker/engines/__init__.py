"""Engine registry."""

from __future__ import annotations

from .base import Engine
from .fake import FakeEngine
from .gallerydl import GalleryDlEngine
from .http import HttpEngine
from .probe import ProbeEngine
from .spotdl import SpotDlEngine
from .ytdlp import YtDlpEngine

ENGINES: dict[str, type] = {
    "fake": FakeEngine,
    "gallerydl": GalleryDlEngine,
    "http": HttpEngine,
    "probe": ProbeEngine,
    "spotdl": SpotDlEngine,
    "ytdlp": YtDlpEngine,
}


def get_engine(name: str) -> Engine | None:
    cls = ENGINES.get(name)
    return cls() if cls else None
