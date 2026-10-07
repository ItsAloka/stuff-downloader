"""Engine registry."""

from __future__ import annotations

from .base import Engine
from .fake import FakeEngine
from .gallerydl import GalleryDlEngine
from .http import HttpEngine
from .music import MusicEngine
from .probe import ProbeEngine
from .signin import SignInEngine
from .social import SocialEngine
from .spotdl import SpotDlEngine
from .ytdlp import YtDlpEngine

ENGINES: dict[str, type] = {
    "fake": FakeEngine,
    "gallerydl": GalleryDlEngine,
    "http": HttpEngine,
    "music": MusicEngine,
    "probe": ProbeEngine,
    "signin": SignInEngine,
    "social": SocialEngine,
    "spotdl": SpotDlEngine,
    "ytdlp": YtDlpEngine,
}


def get_engine(name: str) -> Engine | None:
    cls = ENGINES.get(name)
    return cls() if cls else None
