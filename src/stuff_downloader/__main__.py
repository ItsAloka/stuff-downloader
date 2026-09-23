"""``python -m stuff_downloader`` launches the GUI; ``--self-test`` checks core without a window."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid


def self_test() -> int:
    from .core import settings, tools
    from .core.protocol import JobSpec
    from .core.runner import JobRun, RunState, WorkerRuntimeMissing

    ok = True
    folder = settings.load().effective_download_dir()
    print(f"download folder: {folder}")
    for status in tools.check_all():
        detail = f"OK {status.version or ''}" if status.ok else status.error
        print(f"tool {status.name}: {detail}")

    events = []
    spec = JobSpec(
        uuid.uuid4().hex, "fake", "https://example.invalid/", str(folder), {"steps": 3, "delay": 0}
    )
    try:
        run = JobRun(spec, events.append)
    except WorkerRuntimeMissing as exc:
        print(f"worker round-trip: FAILED ({exc})")
        print("self-test: FAILED")
        return 1
    run.start()
    state = run.wait(timeout=30)
    worker_ok = state is RunState.COMPLETED and events and events[-1].type == "result"
    print(f"worker round-trip: {'OK' if worker_ok else 'FAILED (' + state.value + ')'}")
    ok = ok and bool(worker_ok)
    print("self-test:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stuff_downloader")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check-gui-import", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.self_test:
        return self_test()

    if args.check_gui_import:
        from .app import run_app  # noqa: F401 - import the complete GUI dependency chain

        return 0

    from .app import run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
