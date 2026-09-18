"""Concurrency, pause/resume and the cancel-versus-pause distinction."""

from __future__ import annotations

import pytest

from stuff_downloader.core import scheduler as scheduling
from stuff_downloader.core.protocol import JobSpec

OPTIONS = {"mode": "download", "preset": "mp3_music"}


def spec(job_id: str) -> JobSpec:
    return JobSpec(job_id, "ytdlp", f"https://www.youtube.com/watch?v={job_id}", "C:/dl", OPTIONS)


@pytest.fixture
def started():
    return []


@pytest.fixture
def sched(started):
    return scheduling.Scheduler(lambda s: started.append(s.job_id), max_concurrent=2)


def test_only_max_concurrent_jobs_start(sched, started):
    sched.submit_all([spec(f"j{i}") for i in range(5)])
    assert started == ["j0", "j1"]
    assert sched.active_count == 2 and sched.pending_count == 3


def test_finishing_a_job_starts_the_next_in_order(sched, started):
    sched.submit_all([spec(f"j{i}") for i in range(4)])
    sched.finished("j0")
    assert started == ["j0", "j1", "j2"]
    sched.finished("j1")
    sched.finished("j2")
    assert started == ["j0", "j1", "j2", "j3"]


def test_concurrency_is_clamped_and_raising_it_starts_more(started):
    sched = scheduling.Scheduler(lambda s: started.append(s.job_id), max_concurrent=99)
    assert sched.max_concurrent == scheduling.MAX_CONCURRENT
    sched.set_max_concurrent(0)
    assert sched.max_concurrent == scheduling.MIN_CONCURRENT
    sched.submit_all([spec(f"j{i}") for i in range(4)])
    assert started == ["j0"]
    sched.set_max_concurrent(3)
    assert started == ["j0", "j1", "j2"]


def test_pausing_a_running_job_asks_the_caller_to_stop_the_worker(sched):
    sched.submit(spec("j0"))
    assert sched.pause(spec("j0")) is True
    assert sched.take_intent("j0") == scheduling.PAUSE


def test_pausing_a_queued_job_never_touches_a_worker(sched, started):
    sched.submit_all([spec("j0"), spec("j1"), spec("j2")])
    assert sched.pause(spec("j2")) is False
    assert sched.state_of("j2") == "paused"
    sched.finished("j0")
    assert started == ["j0", "j1"]  # j2 is paused, so nothing takes its turn


def test_resume_puts_a_paused_job_back_in_the_queue(sched, started):
    sched.submit(spec("j0"))
    sched.pause(spec("j0"))
    sched.finished("j0")
    assert sched.resume("j0") is not None
    assert started == ["j0", "j0"]
    assert sched.resume("nobody") is None


def test_cancel_and_pause_are_distinguishable_intents(sched):
    sched.submit_all([spec("j0"), spec("j1")])
    assert sched.cancel("j0") is True
    assert sched.pause(spec("j1")) is True
    assert sched.take_intent("j0") == scheduling.CANCEL
    assert sched.take_intent("j1") == scheduling.PAUSE


def test_intent_is_taken_once_and_a_self_stopping_job_has_none(sched):
    sched.submit(spec("j0"))
    sched.cancel("j0")
    assert sched.take_intent("j0") == scheduling.CANCEL
    assert sched.take_intent("j0") == ""


def test_cancelling_a_queued_job_removes_it_without_stopping_a_worker(sched, started):
    sched.submit_all([spec("j0"), spec("j1"), spec("j2")])
    assert sched.cancel("j2") is False
    sched.finished("j0")
    assert started == ["j0", "j1"]


def test_pause_all_pauses_the_queue_and_names_the_running_jobs(sched):
    specs = [spec(f"j{i}") for i in range(4)]
    sched.submit_all(specs)
    running = sched.pause_all({s.job_id: s for s in specs[:2]})
    assert [s.job_id for s in running] == ["j0", "j1"]
    assert sched.pending_count == 0 and sched.paused_count == 4


def test_restored_jobs_are_parked_paused_and_start_nothing(sched, started):
    sched.submit_paused(spec("old"))
    assert started == []
    assert sched.state_of("old") == "paused" and sched.paused_count == 1


def test_stop_starts_nothing_further(sched, started):
    sched.submit(spec("j0"))
    sched.stop()
    sched.submit(spec("j1"))
    sched.finished("j0")
    assert started == ["j0"]


def test_a_failing_start_frees_its_slot(started):
    def boom(_spec):
        raise RuntimeError("no runtime")

    sched = scheduling.Scheduler(boom, max_concurrent=1)
    with pytest.raises(RuntimeError):
        sched.submit(spec("j0"))
    assert sched.active_count == 0


# ── reordering ───────────────────────────────────────────────────────────────────────────
def test_reorder_rearranges_the_queue_without_starting_anything(sched, started):
    sched.submit_all([spec(f"j{i}") for i in range(5)])
    assert started == ["j0", "j1"]
    assert sched.reorder(["j4", "j3", "j2"]) is True
    assert sched.queued_ids() == ["j4", "j3", "j2"]
    assert started == ["j0", "j1"]
    sched.finished("j0")
    assert started == ["j0", "j1", "j4"]


def test_reorder_only_permutes_the_named_jobs_slots(sched):
    sched.submit_all([spec(f"j{i}") for i in range(6)])  # j0,j1 active; j2..j5 queued
    assert sched.reorder(["j5", "j3"]) is True
    # j3 and j5 swap the slots they held; j2 and j4 keep theirs.
    assert sched.queued_ids() == ["j2", "j5", "j4", "j3"]


def test_reorder_ignores_unknown_and_no_op_orders(sched):
    sched.submit_all([spec(f"j{i}") for i in range(4)])
    assert sched.reorder(["nobody", "also-nobody"]) is False
    assert sched.reorder(["j2"]) is False
    assert sched.reorder(["j2", "j3"]) is False  # already that order
    assert sched.queued_ids() == ["j2", "j3"]


# ── retry backoff ────────────────────────────────────────────────────────────────────────
def test_retry_backoff_doubles_and_then_gives_up(sched):
    delays = []
    for _ in range(scheduling.MAX_RETRIES):
        delay = sched.schedule_retry(spec("j0"))
        delays.append(delay)
        sched.release_retry("j0")
        sched.finished("j0")
    assert delays == [2.0, 4.0, 8.0]
    assert sched.schedule_retry(spec("j0")) is None
    assert sched.retry_delay("j0") is None
    assert sched.attempts("j0") == scheduling.MAX_RETRIES


def test_a_job_waiting_on_backoff_starts_nothing_until_released(sched, started):
    sched.submit(spec("j0"))
    sched.finished("j0")
    assert sched.schedule_retry(spec("j0")) == 2.0
    assert started == ["j0"]
    assert sched.state_of("j0") == "retrying" and sched.retrying_count == 1
    assert sched.release_retry("j0") is not None
    assert started == ["j0", "j0"]
    assert sched.retrying_count == 0


def test_releasing_an_unknown_or_stopped_retry_starts_nothing(sched, started):
    assert sched.release_retry("nobody") is None
    sched.schedule_retry(spec("j0"))
    sched.stop()
    assert sched.release_retry("j0") is None
    assert sched.schedule_retry(spec("j1")) is None
    assert started == []


def test_cancelling_and_pausing_clear_a_pending_retry(sched, started):
    sched.schedule_retry(spec("j0"))
    assert sched.cancel("j0") is False
    assert sched.retrying_count == 0 and sched.attempts("j0") == 0
    assert sched.release_retry("j0") is None

    sched.schedule_retry(spec("j1"))
    assert sched.pause(spec("j1")) is False
    assert sched.state_of("j1") == "paused" and sched.retrying_count == 0
    assert sched.resume("j1") is not None
    assert started == ["j1"]


def test_a_manual_retry_resets_the_backoff(sched):
    sched.schedule_retry(spec("j0"))
    sched.release_retry("j0")
    assert sched.attempts("j0") == 1
    sched.reset_retries("j0")
    assert sched.attempts("j0") == 0
    assert sched.retry_delay("j0") == scheduling.RETRY_BASE_SECONDS
