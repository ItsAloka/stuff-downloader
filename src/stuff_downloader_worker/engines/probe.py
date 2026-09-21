"""Reports which interpreter and engine modules this worker actually runs with. No network.

Options: ``modules`` (list of import names, max 20). Used by self-tests and the §8.1 spike to
prove a worker runs in the expected engine env.
"""

from __future__ import annotations

import importlib
import sys
from importlib import metadata
from typing import Any

from ..protocol import JobSpec
from .base import Emit, EngineError

DIST_NAMES = {
    "yt_dlp": "yt-dlp",
    "yt_dlp_ejs": "yt-dlp-ejs",
    "curl_cffi": "curl_cffi",
    "gallery_dl": "gallery-dl",
}


class ProbeEngine:
    name = "probe"

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        modules = job.options.get("modules", [])
        if not isinstance(modules, list) or len(modules) > 20:
            raise EngineError("bad_options", "'modules' must be a list of at most 20 names")
        found: dict[str, str | None] = {}
        for name in modules:
            if not isinstance(name, str) or not name.isidentifier():
                raise EngineError("bad_options", f"invalid module name: {name!r}")
            try:
                importlib.import_module(name)
            except ImportError:
                found[name] = None
                continue
            try:
                found[name] = metadata.version(DIST_NAMES.get(name, name))
            except metadata.PackageNotFoundError:
                found[name] = "unknown"
        return {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "python": sys.version.split()[0],
            "modules": found,
        }
