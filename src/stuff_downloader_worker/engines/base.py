"""Engine interface shared by all worker engines."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..protocol import JobSpec

Emit = Callable[[str, dict[str, Any]], None]


class Engine(Protocol):
    name: str

    def download(self, job: JobSpec, emit: Emit) -> dict[str, Any]:
        """Run the job, emitting stage/progress/log events; return the result payload."""
        ...


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
