from pathlib import Path

import pytest

from stuff_downloader.core import protocol
from stuff_downloader_worker import protocol as worker_protocol

SRC = Path(__file__).resolve().parents[2] / "src"


def test_protocol_files_identical():
    core = (SRC / "stuff_downloader" / "core" / "protocol.py").read_bytes()
    worker = (SRC / "stuff_downloader_worker" / "protocol.py").read_bytes()
    assert core == worker
    assert protocol.PROTOCOL_VERSION == worker_protocol.PROTOCOL_VERSION


def test_job_spec_round_trip():
    spec = protocol.JobSpec("j1", "fake", "https://x.test/", "C:/out", {"steps": 2})
    assert protocol.JobSpec.from_json(spec.to_json()) == spec


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "[]",
        '{"v": 99, "job_id": "a", "engine": "fake", "url": "u", "output_dir": "o"}',
        '{"v": 1, "job_id": "", "engine": "fake", "url": "u", "output_dir": "o"}',
        '{"v": 1, "job_id": "a", "engine": "fake", "url": "u", "output_dir": "o", "options": 3}',
    ],
)
def test_job_spec_rejects_invalid(text):
    with pytest.raises(protocol.ProtocolError):
        protocol.JobSpec.from_json(text)


def test_event_round_trip_and_terminal():
    event = protocol.Event("progress", "j1", {"percent": 50.0, "title": "ünïcode"})
    line = event.to_line()
    assert line.endswith("\n") and line.count("\n") == 1
    parsed = protocol.Event.from_line(line)
    assert parsed == event and not parsed.is_terminal
    assert protocol.Event("result", "j1").is_terminal
    assert protocol.Event("error", "j1").is_terminal


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        "42",
        '{"v": 1, "type": "bogus", "job_id": "j"}',
        '{"v": 2, "type": "log", "job_id": "j"}',
        '{"v": 1, "type": "log", "job_id": 7}',
    ],
)
def test_event_rejects_invalid(line):
    with pytest.raises(protocol.ProtocolError):
        protocol.Event.from_line(line)
