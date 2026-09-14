"""Versioned JSON-lines protocol between the runner and a worker process.

This file is kept byte-identical to ``stuff_downloader_worker/protocol.py``; a test enforces it.
Stdlib only: no Qt, no engine imports.

Job spec: one JSON object on the worker's stdin.
Events: one JSON object per line on the worker's stdout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

EVENT_TYPES = frozenset({"stage", "progress", "log", "result", "error"})
TERMINAL_EVENT_TYPES = frozenset({"result", "error"})


class ProtocolError(ValueError):
    """A job spec or event line does not match the protocol."""


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    engine: str
    url: str
    output_dir: str
    options: dict[str, Any] = field(default_factory=dict)
    v: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "job_id": self.job_id,
                "engine": self.engine,
                "url": self.url,
                "output_dir": self.output_dir,
                "options": self.options,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> JobSpec:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"job spec is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProtocolError("job spec must be a JSON object")
        if data.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {data.get('v')!r}")
        for key in ("job_id", "engine", "url", "output_dir"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ProtocolError(f"job spec field {key!r} must be a non-empty string")
        options = data.get("options", {})
        if not isinstance(options, dict):
            raise ProtocolError("job spec field 'options' must be an object")
        return cls(
            job_id=data["job_id"],
            engine=data["engine"],
            url=data["url"],
            output_dir=data["output_dir"],
            options=options,
        )


@dataclass(frozen=True)
class Event:
    type: str
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)
    v: int = PROTOCOL_VERSION

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENT_TYPES

    def to_line(self) -> str:
        payload = {"v": self.v, "type": self.type, "job_id": self.job_id, **self.data}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @classmethod
    def from_line(cls, line: str) -> Event:
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"event line is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProtocolError("event must be a JSON object")
        if data.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {data.get('v')!r}")
        event_type = data.get("type")
        if event_type not in EVENT_TYPES:
            raise ProtocolError(f"unknown event type: {event_type!r}")
        job_id = data.get("job_id")
        if not isinstance(job_id, str):
            raise ProtocolError("event field 'job_id' must be a string")
        rest = {k: v for k, v in data.items() if k not in ("v", "type", "job_id")}
        return cls(type=event_type, job_id=job_id, data=rest)
