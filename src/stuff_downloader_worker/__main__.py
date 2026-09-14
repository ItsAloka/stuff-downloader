"""Worker entry point: ``python -m stuff_downloader_worker --engine NAME``.

Reads one JSON job spec from stdin, writes JSON-lines events to stdout, and always ends with
exactly one terminal event (``result`` or ``error``). Exit code 0 on result, 1 on error.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any

from .engines import ENGINES, get_engine
from .engines.base import EngineError
from .protocol import PROTOCOL_VERSION, Event, JobSpec, ProtocolError


def _write(event: Event) -> None:
    sys.stdout.write(event.to_line())
    sys.stdout.flush()


def run_job(engine_name: str, spec_text: str) -> int:
    try:
        job = JobSpec.from_json(spec_text)
    except ProtocolError as exc:
        _write(Event("error", "", {"code": "bad_job_spec", "message": str(exc)}))
        return 1

    if job.engine != engine_name:
        _write(
            Event(
                "error",
                job.job_id,
                {
                    "code": "engine_mismatch",
                    "message": f"spec engine {job.engine!r} != {engine_name!r}",
                },
            )
        )
        return 1
    engine = get_engine(engine_name)
    if engine is None:
        _write(Event("error", job.job_id, {"code": "unknown_engine", "message": engine_name}))
        return 1

    def emit(event_type: str, data: dict[str, Any]) -> None:
        _write(Event(event_type, job.job_id, data))

    try:
        result = engine.download(job, emit)
    except EngineError as exc:
        _write(Event("error", job.job_id, {"code": exc.code, "message": exc.message}))
        return 1
    except Exception as exc:  # report, never crash silently
        traceback.print_exc(file=sys.stderr)
        _write(Event("error", job.job_id, {"code": "engine_crashed", "message": str(exc)}))
        return 1
    _write(Event("result", job.job_id, result))
    return 0


def self_test() -> int:
    import io

    captured = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, captured
    try:
        code = run_job(
            "fake",
            JobSpec(
                "self-test", "fake", "https://example.invalid/", ".", {"steps": 2, "delay": 0}
            ).to_json(),
        )
    finally:
        sys.stdout = real_stdout
    events = [Event.from_line(line) for line in captured.getvalue().splitlines()]
    ok = code == 0 and events and events[-1].type == "result"
    print(f"worker self-test: {'OK' if ok else 'FAILED'} (protocol v{PROTOCOL_VERSION})")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stuff_downloader_worker")
    parser.add_argument("--engine", choices=sorted(ENGINES))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.engine:
        parser.error("--engine is required")
    return run_job(args.engine, sys.stdin.readline())


if __name__ == "__main__":
    sys.exit(main())
