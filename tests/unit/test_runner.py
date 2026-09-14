import sys
import textwrap
import threading
import time

import psutil
import pytest

from stuff_downloader.core.protocol import JobSpec
from stuff_downloader.core.runner import JobRun, RunState


def _spec(**options):
    return JobSpec("job-1", "fake", "https://example.invalid/", ".", options)


def _collect():
    events = []
    lock = threading.Lock()

    def on_event(event):
        with lock:
            events.append(event)

    return events, on_event


def test_fake_job_completes_with_progress():
    events, on_event = _collect()
    run = JobRun(_spec(steps=5, delay=0), on_event)
    run.start()
    assert run.wait(timeout=30) is RunState.COMPLETED
    types = [e.type for e in events]
    assert types[0] == "stage"
    assert types.count("progress") == 5
    assert types[-1] == "result" and types.count("result") == 1
    assert events[-2].data["stage"] == "completed"
    assert [e for e in events if e.type == "progress"][-1].data["percent"] == 100.0


def test_fake_job_failure_is_reported():
    events, on_event = _collect()
    run = JobRun(_spec(steps=4, delay=0, fail=True), on_event)
    run.start()
    assert run.wait(timeout=30) is RunState.FAILED
    assert events[-1].type == "error" and events[-1].data["code"] == "fake_failure"


def test_cancel_terminates_worker():
    events, on_event = _collect()
    run = JobRun(_spec(steps=1000, delay=0.05), on_event)
    run.start()
    deadline = time.monotonic() + 20
    while not any(e.type == "progress" for e in events) and time.monotonic() < deadline:
        time.sleep(0.02)
    pid = run.pid
    run.cancel()
    assert run.wait(timeout=30) is RunState.CANCELLED
    assert events[-1].type == "error" and events[-1].data["code"] == "cancelled"
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_cancel_kills_child_process_tree(tmp_path):
    """A worker that spawns a long-running child (like ffmpeg) must lose the child too."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "worker.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time, json
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            open({str(pid_file)!r}, "w").write(str(child.pid))
            event = {{"v": 1, "type": "stage", "job_id": "job-1", "stage": "x"}}
            print(json.dumps(event), flush=True)
            time.sleep(120)
            """
        )
    )
    events, on_event = _collect()
    run = JobRun(_spec(), on_event, worker_command=[sys.executable, str(script)])
    run.start()
    deadline = time.monotonic() + 20
    while not events and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(pid_file.read_text())
    assert psutil.pid_exists(child_pid)
    run.cancel()
    assert run.wait(timeout=30) is RunState.CANCELLED
    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(child_pid).wait(timeout=10)


def test_malformed_lines_and_foreign_events_ignored_missing_result_is_error(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            print("not json at all")
            print(json.dumps({"v": 1, "type": "log", "job_id": "someone-else", "msg": "x"}))
            print(json.dumps({"v": 1, "type": "log", "job_id": "job-1", "msg": "hello"}))
            raise SystemExit(3)
            """
        )
    )
    events, on_event = _collect()
    run = JobRun(_spec(), on_event, worker_command=[sys.executable, str(script)])
    run.start()
    assert run.wait(timeout=30) is RunState.FAILED
    assert [e.type for e in events] == ["log", "error"]
    assert events[0].data["msg"] == "hello"
    assert events[1].data["code"] == "worker_exited"


def test_callback_exception_does_not_break_run():
    calls = []

    def bad_callback(event):
        calls.append(event)
        raise RuntimeError("ui bug")

    run = JobRun(_spec(steps=2, delay=0), bad_callback)
    run.start()
    assert run.wait(timeout=30) is RunState.COMPLETED
    assert calls[-1].type == "result"


def test_worker_self_test_and_bad_spec():
    import subprocess

    from stuff_downloader.core.runner import _worker_env

    out = subprocess.run(
        [sys.executable, "-m", "stuff_downloader_worker", "--self-test"],
        capture_output=True,
        text=True,
        env=_worker_env(),
        timeout=30,
    )
    assert out.returncode == 0 and "OK" in out.stdout

    bad = subprocess.run(
        [sys.executable, "-m", "stuff_downloader_worker", "--engine", "fake"],
        input="{}\n",
        capture_output=True,
        text=True,
        env=_worker_env(),
        timeout=30,
    )
    assert bad.returncode == 1 and '"bad_job_spec"' in bad.stdout
