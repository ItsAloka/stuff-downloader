"""JSON settings with a schema version. No Qt imports."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


@dataclass
class Settings:
    download_dir: str = ""
    max_concurrent: int = 3
    tool_paths: dict[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def effective_download_dir(self) -> Path:
        return paths.resolve_download_dir(self.download_dir)


def settings_path() -> Path:
    return paths.config_dir() / "settings.json"


def load(path: Path | None = None) -> Settings:
    """Load settings; a missing or corrupt file yields defaults rather than an exception."""
    path = path or settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable settings file %s: %s", path, exc)
        return Settings()
    if not isinstance(raw, dict):
        log.warning("Ignoring settings file %s: not a JSON object", path)
        return Settings()

    settings = Settings()
    if isinstance(raw.get("download_dir"), str):
        settings.download_dir = raw["download_dir"]
    concurrent = raw.get("max_concurrent")
    if isinstance(concurrent, int) and not isinstance(concurrent, bool):
        settings.max_concurrent = min(5, max(1, concurrent))
    tools = raw.get("tool_paths")
    if isinstance(tools, dict):
        settings.tool_paths = {k: v for k, v in tools.items() if isinstance(v, str)}
    return settings


def save(settings: Settings, path: Path | None = None) -> None:
    """Write atomically: temp file in the same folder, then replace."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
