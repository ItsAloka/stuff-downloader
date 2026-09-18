r"""Running a real worker job in a test, without YouTube's mood deciding whether the suite is green.

Both network modules share this. It exists because `pytest -m network` was not a usable gate:
the same code produced 15 passed, then 14 passed / 1 failed, then 13 passed / 1 failed / 1 error,
purely on HTTP 403s from repeated pulls. A gate that fails at random teaches people to ignore it,
which is worse than not having it.

The rule here is the important part, so it is stated once, plainly:

    A provider refusing to serve us is NOT a test failure, and it is NOT a pass either.
    It is a skip, with the provider's own words attached.

So a transient refusal (403, 429, throttling, a network wobble) is retried a few times with
backoff, and if it still will not serve, the test SKIPS and says why. Anything else -- a real
assertion, a missing file, a wrong codec, "video unavailable" -- fails exactly as before. The
retry is deliberately narrow: it never re-runs an assertion, only the download that fed it, so a
genuine defect cannot be retried into a pass.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stuff_downloader.core import tools
from stuff_downloader.core.protocol import Event, JobSpec
from stuff_downloader.core.runner import JobRun, default_worker_command

# Refusals that say "not now" rather than "not ever". Anything absent from this list is treated
# as a real failure, on purpose: "video unavailable", "private video" and a wrong codec must
# never be retried away.
_TRANSIENT = re.compile(
    r"""
    HTTP\ Error\ (403|429|500|502|503|504)
    | Too\ Many\ Requests
    | Sign\ in\ to\ confirm
    | throttl
    | rate[-\ ]?limit
    | temporary\ failure
    | connection\ (reset|aborted|refused)
    | timed?\ ?out
    | read\ operation\ timed\ out
    | remote\ end\ closed
    """,
    re.IGNORECASE | re.VERBOSE,
)

ATTEMPTS = 3
BACKOFF_SECONDS = (10, 30)  # between attempt 1-2 and 2-3


def worker_command() -> list[str]:
    """The engine runtime's worker, resolved before conftest redirects LOCALAPPDATA.

    tests/conftest.py redirects LOCALAPPDATA for every test and the yt-dlp engine runtime lives
    under the real one, so a worker started from inside a test would run in the dev venv, which
    deliberately has no yt_dlp. Every JobRun is given this command explicitly.
    """
    return default_worker_command("ytdlp")


def require_engine_runtime() -> list[str]:
    """The worker command, or skip the whole module if the engine runtime was never built."""
    command = worker_command()
    if Path(command[0]).resolve() == Path(sys.executable).resolve():
        pytest.skip(
            "the yt-dlp engine runtime is not installed; build it before running network tests",
            allow_module_level=True,
        )
    return command


def is_transient(data: object) -> bool:
    """Whether a terminal error is the provider declining to serve us right now."""
    if not isinstance(data, dict):
        return False
    return bool(_TRANSIENT.search(str(data.get("message") or "")))


def run_job_once(spec: JobSpec, command: list[str], timeout: float) -> tuple[Event, list[Event]]:
    """One real worker job to its terminal event. Returns (terminal, all events)."""
    events: list[Event] = []
    done = threading.Event()

    def on_event(event: Event) -> None:
        events.append(event)
        if event.is_terminal:
            done.set()

    run = JobRun(spec, on_event, worker_command=command)
    run.start()
    if not done.wait(timeout):
        run.cancel()
        raise AssertionError(f"job did not finish within {timeout}s: {spec.url}")
    return events[-1], events


def run_job(
    spec: JobSpec,
    command: list[str],
    timeout: float,
    *,
    expect_result: bool = True,
) -> tuple[Event, list[Event]]:
    """Run a job, retrying only a transient provider refusal, then skipping if it persists.

    expect_result=False is for the callers that want to inspect a terminal error themselves;
    they still get the transient handling, so a 403 does not masquerade as the error they meant
    to assert on.
    """
    refusals: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        terminal, events = run_job_once(spec, command, timeout)
        if terminal.type == "result" or not is_transient(terminal.data):
            if expect_result:
                assert terminal.type == "result", f"job failed: {terminal.data}"
            return terminal, events

        refusals.append(f"attempt {attempt}: {terminal.data.get('message')}")
        if attempt < ATTEMPTS:
            time.sleep(BACKOFF_SECONDS[attempt - 1])

    pytest.skip(
        "the provider refused to serve this request, so the pipeline could not be exercised. "
        "This is not a failure of this project's code, and not a pass either:\n  "
        + "\n  ".join(refusals)
    )


def ffprobe(path: Path) -> dict:
    """What actually landed on disk, according to ffprobe rather than to yt-dlp."""
    exe = tools.app_tools_dir() / "ffprobe.exe"
    assert exe.is_file(), f"ffprobe not found at {exe}; see tools/SOURCES.txt"
    out = subprocess.run(  # noqa: S603 (fixed, app-owned tool path)
        [
            str(exe),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)
