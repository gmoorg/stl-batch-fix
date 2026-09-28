"""Shared, lock-guarded state for a pool of worker threads each owning a subprocess."""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field


#: Confirmed-real decimator (fast_simplification) peak-memory figure. See
#: archive/docs-before-compact-2026-09-19/refactor/pool/d5.md.
#: Does not cover alpha-wrap's own memory use.
BUDGET_BYTES_PER_TRIANGLE = 890

#: Unvalidated placeholder. Alpha-wrap can multiply triangle count well past
#: its input (see docs/refactor/orchestration.md) and no measured
#: ratio exists anywhere in this project. This is a conservative guess, not a
#: calibration — replace once real alpha-wrap peak-RSS-vs-input-triangle
#: measurements exist. Track in docs/refactor/TODO.md.
ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED = 3


@dataclass
class _Job:
    state: str                         # 'reserved'|'running'|'reaped'|'publishing'|'stuck'|'launch_failed'
    proc: object | None = None         # subprocess.Popen, once spawned
    estimate_bytes: int = 0


class RunState:
    """One lock, one owner, for everything a batch run's threads must share.

    A `Pool` worker thread knows only about the Python-level item it was
    handed; it has no visibility into the OS subprocess a handler spawns to
    do the actual work.  `RunState` is that missing layer: it tracks which
    child processes are alive so a `KeyboardInterrupt` landing in the main
    thread — which owns no worker's `Popen` directly — can find and kill
    every one of them, and it tracks memory reservations so admission can
    reason about more than a bare worker-count ceiling.

    See docs/refactor/orchestration.md for the current batch lifecycle;
    tools/batch_repair.py owns the callers of these transitions.
    """

    _ALLOWED_BEFORE_COMPLETE = frozenset({'reaped', 'publishing', 'launch_failed'})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, _Job] = {}
        self._completed: set[int] = set()
        self._report_attempted: set[int] = set()
        self._next_id = 0
        self._cancelled = False
        self._incomplete_reason: str | None = None

    # -- admission / reservation --------------------------------------------

    def start(self, estimate_bytes: int, budget_bytes: float
              ) -> tuple[int | None, str | None, bool]:
        """Admit-and-reserve in one locked call, or refuse.

        Returns `(token, refusal, override)`. `token is None` means refused
        — `refusal` is `'cancelled'` or `'shed'`.  `override` is True when
        this job was admitted despite exceeding the budget because nothing
        else is running (D5's "alone" rule: an unrunnable-alone job cannot
        be helped by waiting, so it runs anyway rather than deadlocking).
        One locked call so there is no window between checking and
        reserving for another thread — or a cancellation — to invalidate
        the decision.
        """
        with self._lock:
            if self._cancelled:
                return None, 'cancelled', False
            running = sum(j.estimate_bytes for j in self._jobs.values())
            alone = not self._jobs
            if not alone and running + estimate_bytes > budget_bytes:
                return None, 'shed', False
            override = alone and estimate_bytes > budget_bytes
            token = self._next_id
            self._next_id += 1
            self._jobs[token] = _Job(state='reserved', estimate_bytes=estimate_bytes)
            return token, None, override

    # -- lifecycle transitions ------------------------------------------------

    def spawned(self, token: int, proc) -> bool:
        """Record that `proc` now exists for `token`.

        Returns False when cancellation arrived in the gap between `start()`
        granting this reservation and `Popen()` actually returning — the
        caller must kill-and-reap `proc` itself; nothing else owns it yet.
        """
        with self._lock:
            job = self._jobs[token]
            job.proc = proc
            job.state = 'running'
            return not self._cancelled

    def mark_reaped(self, token: int) -> None:
        with self._lock:
            self._jobs[token].state = 'reaped'

    def has_report_attempted(self, token: int) -> bool:
        """True once `complete_once` has called (or started calling)
        `report_fn` for this token — whether or not it raised."""
        with self._lock:
            return token in self._report_attempted

    def proc_for(self, token: int):
        """The `Popen` registered for `token`, or None if never spawned."""
        with self._lock:
            job = self._jobs.get(token)
            return job.proc if job is not None else None

    def mark_launch_failed(self, token: int) -> None:
        """`Popen()` itself raised — no process ever existed to reap."""
        with self._lock:
            self._jobs[token].state = 'launch_failed'

    def mark_stuck(self, token: int, detail: str) -> None:
        """Cleanup could not confirm this job's process tree is dead.

        This is a run-cancelling event: nothing else should start fresh
        work while a process's fate is unknown.  The token is never removed
        by `complete_once()` from this state — it stays visible in
        `snapshot_unresolved()` for the rest of the run.
        """
        with self._lock:
            self._jobs[token].state = 'stuck'
            already_cancelled = self._cancelled
            self._cancelled = True
            if self._incomplete_reason is None:
                self._incomplete_reason = f'job {token} stuck: {detail}'
            live = [(t, j.proc) for t, j in self._jobs.items()
                    if t != token and j.proc is not None]
        if not already_cancelled:
            _killpg_all(live)

    def authorize_publication(self, token: int) -> bool:
        """True iff this job may write a marker or trust a recovered result.

        Must be called immediately before any parent-side filesystem write.
        False means cancellation won the race — the caller must report a
        cancelled outcome and touch nothing on disk.  This is a commitment
        point, not a real-time write-in-progress guarantee: cancellation
        blocks *new* authorizations, but an already-authorized write may
        still be in flight when cancellation happens.
        """
        with self._lock:
            if self._cancelled:
                return False
            self._jobs[token].state = 'publishing'
            return True

    def complete_once(self, token: int, report_fn, result) -> None:
        """The only way a job ends.  Idempotent per token.

        Calls `report_fn(result)` first; the token is marked completed (and
        its bookkeeping removed) only if that succeeds.  A raising
        `report_fn` leaves the token genuinely unresolved — visible in the
        final incomplete-run summary — rather than silently retried, since
        retrying could call `report_fn` twice for one job.

        A second call for a token whose `report_fn` was already *attempted*
        (whether or not it succeeded) raises `AssertionError` rather than
        trying again — a caller must not react to a reporting failure by
        calling this a second time for the same token. The
        one legitimate "second call" this method tolerates is a genuine
        no-op repeat after the FIRST call already succeeded, which the
        `token in self._completed` check below still returns early on.
        """
        with self._lock:
            if token in self._completed:
                return
            if token in self._report_attempted:
                raise AssertionError(
                    f'complete_once({token}) called again after its report_fn '
                    f'already raised once — the token is unresolved by design, '
                    f'not eligible for a retry')
            job = self._jobs.get(token)
            if job is None or job.state not in self._ALLOWED_BEFORE_COMPLETE:
                raise AssertionError(
                    f'complete_once({token}) called from state '
                    f'{job.state if job else "<missing>"!r}')
            self._report_attempted.add(token)
        report_fn(result)             # outside the lock — may do I/O
        with self._lock:
            self._completed.add(token)
            self._jobs.pop(token, None)

    # -- cancellation ---------------------------------------------------------

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self, reason: str) -> list[tuple[int, Exception]]:
        """Stop all future admission and signal every live child once.

        Idempotent: a second call is a no-op and returns no errors, since
        the first call already signalled everything it could see.
        """
        with self._lock:
            if self._cancelled:
                return []
            self._cancelled = True
            self._incomplete_reason = reason
            live = [(t, j.proc) for t, j in self._jobs.items() if j.proc is not None]
        return _killpg_all(live)

    # -- run-end reporting ------------------------------------------------------

    def has_unresolved(self) -> bool:
        with self._lock:
            return bool(self._jobs)

    def has_incomplete_reason(self) -> bool:
        with self._lock:
            return self._incomplete_reason is not None

    def incomplete_reason(self) -> str | None:
        with self._lock:
            return self._incomplete_reason

    def snapshot_unresolved(self) -> dict[int, str]:
        with self._lock:
            return {token: job.state for token, job in self._jobs.items()}


def _killpg_all(entries: list[tuple[int, object]]) -> list[tuple[int, Exception]]:
    """`os.killpg(SIGKILL)` every `(token, Popen)` pair; collect, don't stop."""
    errors = []
    for token, proc in entries:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass                        # already gone — nothing to do
        except Exception as exc:        # noqa: BLE001 — collected, not fatal to the loop
            errors.append((token, exc))
    return errors
