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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


# ── MediaResult (plan §5.2, §5.3) ──────────────────────────────────────────────────────────
# Every engine's analyze result carries these fields, so the app can tell what a link holds
# without knowing which engine read it. Engine-specific fields the current UI still reads
# (formats, tracks, filesize…) travel alongside until the result card replaces them (R2).
MEDIA_KINDS = ("video", "audio", "image", "gallery", "playlist")
MEDIA_TABS = ("video", "audio", "image", "gallery", "tracks")
_ROW_KEYS = ("video_rows", "audio_rows", "image_rows", "items", "entries")
_TEXT_KEYS = ("title", "uploader", "artist", "album", "site")
# YouTube links are the only ones whose query names the media; everywhere else a query can be
# the signed token that makes a link work, so a durable webpage never keeps one.
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
_YOUTUBE_KEYS = ("v", "list")
# The tab a kind opens on. A video's first tab is Video, an audio's Audio, and so on.
_FIRST_TAB = {
    "video": "video",
    "audio": "audio",
    "image": "image",
    "gallery": "gallery",
    "playlist": "tracks",
}


def durable_webpage(url: str) -> str:
    """``url`` reduced to what may be kept and shown: http(s), host, port and path.

    Credentials, the fragment and the query are dropped; a YouTube link keeps only its ``v``
    and ``list`` parameters, which name the video or playlist. "" when nothing usable is left.
    """
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return ""
    query = ""
    if host in _YOUTUBE_HOSTS:
        pairs = [(k, v) for k, v in parse_qsl(parts.query) if k in _YOUTUBE_KEYS]
        query = urlencode(pairs)
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def media_result(
    kind: str, tabs: list[str], title: str, webpage: str, **fields: Any
) -> dict[str, Any]:
    """A MediaResult dict with every list field present, checked before it is returned.

    ``webpage`` is the analyzed link; it is stored in its durable form (``durable_webpage``).
    """
    result: dict[str, Any] = {key: [] for key in _ROW_KEYS}
    result.update(fields)
    result.update(
        {"kind": kind, "tabs": list(tabs), "title": title, "webpage": durable_webpage(webpage)}
    )
    result.setdefault("preview", None)
    return validate_media_result(result)


def validate_media_result(data: Any) -> dict[str, Any]:
    """``data`` if it is a well-formed MediaResult, else ProtocolError. Extra keys are allowed."""
    if not isinstance(data, dict):
        raise ProtocolError("media result must be an object")
    kind = data.get("kind")
    if kind not in MEDIA_KINDS:
        raise ProtocolError(f"unknown media kind: {kind!r}")
    tabs = data.get("tabs")
    if (
        not isinstance(tabs, list)
        or not tabs
        or any(tab not in MEDIA_TABS for tab in tabs)
        or len(set(tabs)) != len(tabs)
    ):
        raise ProtocolError("media result 'tabs' must be a non-empty list of known tabs")
    if tabs[0] != _FIRST_TAB[kind]:
        raise ProtocolError(f"a {kind} result must open on its {_FIRST_TAB[kind]} tab")
    if not isinstance(data.get("title"), str):
        raise ProtocolError("media result 'title' must be a string")
    webpage = data.get("webpage")
    if not isinstance(webpage, str) or not webpage or durable_webpage(webpage) != webpage:
        raise ProtocolError("media result 'webpage' must be a durable http(s) link")
    for key in _TEXT_KEYS:
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ProtocolError(f"media result {key!r} must be a string")
    duration = data.get("duration")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0
    ):
        raise ProtocolError("media result 'duration' must be a non-negative number")
    preview = data.get("preview")
    if preview is not None and (
        not isinstance(preview, dict) or not isinstance(preview.get("data"), str)
    ):
        raise ProtocolError("media result 'preview' must be null or carry base64 'data'")
    source_audio = data.get("source_audio")
    if source_audio is not None and not isinstance(source_audio, dict):
        raise ProtocolError("media result 'source_audio' must be an object")
    for key in _ROW_KEYS:
        rows = data.get(key)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ProtocolError(f"media result {key!r} must be a list of objects")
    return data
