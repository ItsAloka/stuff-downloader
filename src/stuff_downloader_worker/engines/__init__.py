"""Engine registry."""

from __future__ import annotations

from .audioconvert import AudioConvertEngine
from .base import Engine
from .fake import FakeEngine
from .gallerydl import GalleryDlEngine
from .http import HttpEngine
from .imageconvert import ImageConvertEngine
from .probe import ProbeEngine
from .spotdl import SpotDlEngine
from .videoconvert import VideoConvertEngine
from .ytdlp import YtDlpEngine

ENGINES: dict[str, type] = {
    "audioconvert": AudioConvertEngine,
    "fake": FakeEngine,
    "gallerydl": GalleryDlEngine,
    "http": HttpEngine,
    "imageconvert": ImageConvertEngine,
    "videoconvert": VideoConvertEngine,
    "probe": ProbeEngine,
    "spotdl": SpotDlEngine,
    "ytdlp": YtDlpEngine,
}


def get_engine(name: str) -> Engine | None:
    cls = ENGINES.get(name)
    return cls() if cls else None
