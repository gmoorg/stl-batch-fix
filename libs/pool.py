"""A worker pool: N threads, each pulling work from a caller-supplied function.

Domain-free by construction — and queue-free too.  The pool owns no items,
no ordering and no admission rule.  It owns exactly three things: a lock, a
shutdown flag, and which item each thread currently holds.  Everything else
belongs to the caller:

    select(done) -> T | None     the next item, or None to shut this worker down
    work(pool)                   what a worker thread does

Usage:

    queue = [...]                       # the caller's list, never the pool's

    def select(done):
        if done is not None:
            release(done)               # accounting, if the caller needs any
        return queue.pop(0) if queue else None

    def worker(pool):
        while (item := pool.get_next()) is not None:
            handle(item)

    Pool(n_workers=4, select=select).start(worker)

Threads, not processes, deliberately: a worker that drives a subprocess spends
its life blocked in `communicate()` rather than computing, so the GIL is not a
constraint, and sharing state in one process means no pickling, no proxy
objects, and no pool-wide failure when a single worker dies.
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol


class Select[T](Protocol):
    """Hands out the next item, and is told which one just finished.

    Called with the pool's lock held, so implementations never need to lock
    their own queue or accounting.

        done    the item this worker just finished, or None on its first call

    Returns the next item for this worker, or None meaning *this worker should
    shut down*.

    Every item is reported, including each worker's last: a worker that has
    finished its final item still calls `select(done)` once more, is told None,
    and exits.  So a running total returns to where it started.

    The two jobs are combined deliberately.  An earlier design had the pool
    call `select` itself — in `start()`'s `finally`, to release a worker's last
    item — and that is what made `stop()` over-commit: a combined call cannot
    release without also acquiring, so every compensating call re-committed
    what it had just freed.  The pool now calls `select` only on behalf of a
    worker asking for work, which removes the problem rather than patching it.

    Returning None is not failure; it is how a pool shrinks.  With a
    cheapest-first queue, an item that does not fit now will never fit —
    everything after it is larger and nothing smaller is coming — so shutting
    the worker down frees its share for the workers still running.
    """

    def __call__(self, done: T | None) -> T | None: ...


class Pool[T]:
    """Spawns threads and serialises their calls to `select`.  Nothing more."""

    def __init__(self, n_workers: int, select: Select[T]) -> None:
        self._n = max(1, n_workers)
        self._select = select
        self._lock = threading.Lock()
        self._stopped = False
        self._holding: dict[str, T] = {}   # thread -> item, to report as `done`

    # -- the synchronisation point -------------------------------------------

    def get_next(self) -> T | None:
        """The next item for this worker, or None meaning *shut down*.

        The pool's entire contribution is serialisation: one worker inside
        `select` at a time, so the caller's queue and accounting need no locks
        of their own.
        """
        me = threading.current_thread().name
        with self._lock:
            done = self._holding.pop(me, None)
            if self._stopped:
                return None
            item = self._select(done)
            if item is not None:
                self._holding[me] = item
            return item

    def stop(self) -> None:
        """Tell every worker to shut down when it next asks for work."""
        with self._lock:
            self._stopped = True

    def holding(self) -> dict[str, T]:
        """What each worker currently holds, for a progress display."""
        with self._lock:
            return dict(self._holding)

    # -- running -------------------------------------------------------------

    def start(self, work: Callable[[Pool[T]], None]) -> None:
        """Spawn the threads, each running work(self), and wait for them.

        `n_workers` is a ceiling, not a target.  Spawning more threads than
        there is work costs microseconds: each one asks `select` once, is told
        None, and exits.  Sizing the pool to the queue would mean the pool
        knowing the queue, which is the whole thing this interface avoids.

        `work` is an ordinary callable running in this process, so it may be a
        lambda, a closure or a bound method — nothing is pickled and nothing
        needs to be importable by name.
        """
        threads = [threading.Thread(target=work, args=(self,), name=f"w{i}",
                                    daemon=True)
                   for i in range(self._n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
