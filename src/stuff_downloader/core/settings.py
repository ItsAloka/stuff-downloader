"""JSON settings with a schema version. No Qt imports."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cookies, paths

SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


@dataclass
class Settings:
    download_dir: str = ""
    max_concurrent: int = 3
    notifications: bool = True
    tool_paths: dict[str, str] = field(default_factory=dict)
    # Advanced, empty by default (plan §6.4): site -> {"source": ...}. Choices only, never cookies.
    site_logins: dict[str, dict[str, str]] = field(default_factory=dict)
    # Library updates (plan §2, R8): startup check switch, last successful check (epoch seconds)
    # and skipped versions per normalized library name.
    update_check_on_start: bool = True
    update_last_check: float = 0.0
    update_skips: dict[str, list[str]] = field(default_factory=dict)
    # Engines changed by the last update, so Settings → Undo last update knows what to undo.
    update_last_engines: list[str] = field(default_factory=list)
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
    notifications = raw.get("notifications")
    if isinstance(notifications, bool):
        settings.notifications = notifications
    tools = raw.get("tool_paths")
    if isinstance(tools, dict):
        settings.tool_paths = {k: v for k, v in tools.items() if isinstance(v, str)}
    check_on_start = raw.get("update_check_on_start")
    if isinstance(check_on_start, bool):
        settings.update_check_on_start = check_on_start
    last_check = raw.get("update_last_check")
    if isinstance(last_check, (int, float)) and not isinstance(last_check, bool):
        if math.isfinite(last_check) and last_check >= 0:
            settings.update_last_check = float(last_check)
    skips = raw.get("update_skips")
    if isinstance(skips, dict):
        settings.update_skips = {
            name: [v for v in versions if isinstance(v, str)]
            for name, versions in skips.items()
            if isinstance(name, str) and isinstance(versions, list)
        }
    last_engines = raw.get("update_last_engines")
    if isinstance(last_engines, list):
        settings.update_last_engines = [e for e in last_engines if isinstance(e, str)]
    settings.site_logins = cookies.dump_all(cookies.load_all(raw.get("site_logins")))
    return settings


def save(settings: Settings, path: Path | None = None) -> None:
    """Write atomically: temp file in the same folder, then replace."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    os.replace(tmp, path)
