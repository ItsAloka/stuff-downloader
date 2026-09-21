"""Queue scheduling: concurrency, pause/resume and cancel intent (plan §6.1). No Qt imports.

The scheduler owns *order and intent*, never processes. It calls ``start_job`` when a job may
run and is told ``finished`` when one stops; the caller owns the worker.

It is driven from a single thread (the GUI marshals worker events onto its own thread before
calling in), so it holds no locks.

``JobRun.cancel`` produces the same terminal event whether the owner cancelled or paused a job,
so the distinction is kept here: ``pause`` and ``cancel`` record an intent that ``take_intent``
hands back when that terminal event arrives.

Some sites get one job at a time (plan §6.1): ``group_of`` names a job's rate-limited site, and
while a job from that site runs, later jobs from it wait — without holding up jobs for any other
site behind them.

Retry backoff is *decided* here and *timed* by the caller: the scheduler owns no clock and no
thread, so ``schedule_retry`` parks the job and returns the delay for the caller to arm a timer
with, and ``release_retry`` puts it back in the queue when that timer fires.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .protocol import JobSpec

MIN_CONCURRENT = 1
MAX_CONCURRENT = 5
DEFAULT_CONCURRENT = 3

PAUSE = "pause"
CANCEL = "cancel"

# Backoff: 2s, 4s, 8s, then the job is left failed. Bounded so a site that is down for the day
# cannot leave a job retrying forever.
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0


@dataclass
class Scheduler:
    start_job: Callable[[JobSpec], None]
    max_concurrent: int = DEFAULT_CONCURRENT
    # A job's rate-limited site ("instagram", "x", …), or "" for no per-site limit.
    group_of: Callable[[JobSpec], str] = field(default=lambda spec: "")
    _pending: list[JobSpec] = field(default_factory=list)
    _active: set[str] = field(default_factory=set)
    _active_groups: dict[str, str] = field(default_factory=dict)  # job id -> its site group
    _paused: dict[str, JobSpec] = field(default_factory=dict)
    _intent: dict[str, str] = field(default_factory=dict)
    _retrying: dict[str, JobSpec] = field(default_factory=dict)
    _attempts: dict[str, int] = field(default_factory=dict)
    _stopped: bool = False

    def __post_init__(self) -> None:
        self.max_concurrent = self._clamp(self.max_concurrent)

    @staticmethod
    def _clamp(value: int) -> int:
        return max(MIN_CONCURRENT, min(MAX_CONCURRENT, int(value)))

    # ── state ────────────────────────────────────────────────────────────────────────────
    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def paused_count(self) -> int:
        return len(self._paused)

    @property
    def retrying_count(self) -> int:
        return len(self._retrying)

    def attempts(self, job_id: str) -> int:
        """How many automatic retries this job has already been given."""
        return self._attempts.get(job_id, 0)

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active

    def is_queued(self, job_id: str) -> bool:
        return any(spec.job_id == job_id for spec in self._pending)

    def state_of(self, job_id: str) -> str:
        if job_id in self._active:
            return "active"
        if job_id in self._paused:
            return "paused"
        if job_id in self._retrying:
            return "retrying"
        return "queued" if self.is_queued(job_id) else ""

    # ── submitting ───────────────────────────────────────────────────────────────────────
    def submit(self, spec: JobSpec) -> None:
        self._pending.append(spec)
        self.pump()

    def submit_all(self, specs: list[JobSpec]) -> None:
        self._pending.extend(specs)
        self.pump()

    def submit_paused(self, spec: JobSpec) -> None:
        """Put a job in the queue without starting it, as restored jobs arrive."""
        self._paused[spec.job_id] = spec

    def set_max_concurrent(self, value: int) -> None:
        self.max_concurrent = self._clamp(value)
        self.pump()

    def pump(self) -> None:
        """Start jobs until the concurrency limit is reached.

        Jobs start in queue order, except that one whose site already has a job running is
        passed over (it keeps its place) so the next eligible job can use the slot.
        """
        while not self._stopped and len(self._active) < self.max_concurrent:
            busy = set(self._active_groups.values())
            index = next(
                (i for i, spec in enumerate(self._pending) if self._group(spec) not in busy),
                None,
            )
            if index is None:
                return
            spec = self._pending.pop(index)
            group = self._group(spec)
            self._active.add(spec.job_id)
            if group:
                self._active_groups[spec.job_id] = group
            try:
                self.start_job(spec)
            except Exception:
                self._active.discard(spec.job_id)
                self._active_groups.pop(spec.job_id, None)
                raise

    def _group(self, spec: JobSpec) -> str:
        try:
            return self.group_of(spec) or ""
        except Exception:  # a bad classifier must not stall the queue
            return ""

    def reorder(self, job_ids: list[str]) -> bool:
        """Rearrange queued jobs into the given order.

        Only the slots already held by the named jobs are permuted, so anything the caller did
        not name — a job queued since the drag began — keeps its place in the queue.
        """
        named = [job_id for job_id in job_ids if self.is_queued(job_id)]
        if len(named) < 2:
            return False
        wanted = set(named)
        by_id = {spec.job_id: spec for spec in self._pending if spec.job_id in wanted}
        slots = [i for i, spec in enumerate(self._pending) if spec.job_id in wanted]
        if len(slots) != len(named):
            return False
        before = [spec.job_id for spec in self._pending]
        for slot, job_id in zip(slots, named, strict=True):
            self._pending[slot] = by_id[job_id]
        return [spec.job_id for spec in self._pending] != before

    def queued_ids(self) -> list[str]:
        """The queued jobs in the order they will start."""
        return [spec.job_id for spec in self._pending]

    # ── retry ────────────────────────────────────────────────────────────────────────────
    def retry_delay(self, job_id: str) -> float | None:
        """The delay the next automatic retry would wait, or None once retries are used up."""
        attempt = self._attempts.get(job_id, 0)
        if attempt >= MAX_RETRIES:
            return None
        return min(RETRY_BASE_SECONDS * (2**attempt), RETRY_MAX_SECONDS)

    def schedule_retry(self, spec: JobSpec) -> float | None:
        """Park a failed job for an automatic retry.

        Returns the seconds the caller must wait before calling ``release_retry``, or None when
        the job has used its retries and should stay failed.
        """
        job_id = spec.job_id
        delay = self.retry_delay(job_id)
        if delay is None or self._stopped:
            return None
        self._attempts[job_id] = self._attempts.get(job_id, 0) + 1
        self._retrying[job_id] = spec
        return delay

    def release_retry(self, job_id: str) -> JobSpec | None:
        """The caller's backoff timer fired: put the job back in the queue."""
        spec = self._retrying.pop(job_id, None)
        if spec is None or self._stopped:
            return None
        self.submit(spec)
        return spec

    def reset_retries(self, job_id: str) -> None:
        """A manual retry, or a success: the next failure starts the backoff again."""
        self._attempts.pop(job_id, None)
        self._retrying.pop(job_id, None)

    # ── finishing ────────────────────────────────────────────────────────────────────────
    def finished(self, job_id: str) -> None:
        """A job stopped for any reason; free its slot and start the next one."""
        self._active.discard(job_id)
        self._active_groups.pop(job_id, None)
        self.pump()

    def take_intent(self, job_id: str) -> str:
        """Why this job was stopped by us: ``PAUSE``, ``CANCEL``, or "" when it stopped itself."""
        return self._intent.pop(job_id, "")

    # ── owner actions ────────────────────────────────────────────────────────────────────
    def pause(self, spec: JobSpec) -> bool:
        """Pause a queued or running job. Returns True if the caller must stop the worker."""
        job_id = spec.job_id
        if self._retrying.pop(job_id, None) is not None:
            self._paused[job_id] = spec
            return False
        if job_id in self._active:
            self._intent[job_id] = PAUSE
            self._paused[job_id] = spec
            return True
        if self.is_queued(job_id):
            self._pending = [s for s in self._pending if s.job_id != job_id]
            self._paused[job_id] = spec
        return False

    def resume(self, job_id: str) -> JobSpec | None:
        spec = self._paused.pop(job_id, None)
        if spec is None:
            return None
        self.submit(spec)
        return spec

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. Returns True if the caller must stop the worker."""
        self._paused.pop(job_id, None)
        self._retrying.pop(job_id, None)
        self._attempts.pop(job_id, None)
        if job_id in self._active:
            self._intent[job_id] = CANCEL
            return True
        self._pending = [s for s in self._pending if s.job_id != job_id]
        return False

    def pause_all(self, active_specs: dict[str, JobSpec]) -> list[JobSpec]:
        """Pause everything queued and return the running jobs the caller must stop."""
        for spec in list(self._pending):
            self.pause(spec)
        # Driven by the caller's order, not by set iteration order, so the result is stable.
        running = []
        for job_id, spec in active_specs.items():
            if job_id in self._active and self.pause(spec):
                running.append(spec)
        return running

    def stop(self) -> None:
        """Shutting down: start nothing more."""
        self._stopped = True
        self._pending.clear()
        self._retrying.clear()
