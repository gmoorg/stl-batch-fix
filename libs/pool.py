"""A worker pool: N threads, each pulling work from a caller-supplied function.

Domain-free by construction.  The pool owns a lock, a shutdown flag and the
worker loop.  It owns no items, no ordering and no admission rule — those are
the caller's, expressed as two functions:

    item_selector(done, error) -> T | None   the next item, or None to shut
                                             this worker down
    item_handler(item) -> None               do the work

Usage:

    queue = [...]                            # the caller's list, not the pool's

    def next_item(done, error):
        if done is not None:
            release(done)                    # accounting, if any
            if error is not None:
                log(f"{done} failed: {error}")
        return queue.pop(0) if queue else None

    Pool(4, next_item, repair_one).start()

One call, one place, one outcome: the selector learns what finished, whether it
succeeded, and decides what runs next.  There is deliberately no separate error
callback — see the constructor for what that cost.

Threads, not processes, deliberately: a worker that drives a subprocess spends
its life blocked in `communicate()` rather than computing, so the GIL is not a
constraint, and sharing state in one process means no pickling, no proxy
objects, and no pool-wide failure when a single worker dies.

**The pool owns the loop**, which is what makes the accounting safe.  An
earlier version exposed `get_next()` and left the caller to write:

    item = None
    while (item := pool.get_next(item)) is not None:
        try:
            handle(item)
        except Exception:
            log()

That is correct — `item` stays bound through the `except`, so the next call
reports it — but it is correct only if the caller writes the `try`.  Forget it
and an exception leaves the loop entirely, taking the in-flight item with it:
the policy is never told, and a budget policy silently loses that capacity.
With the loop inside, there is nothing to forget.  The caller supplies work,
not control flow.
"""

from __future__ import annotations

import threading
from typing import Callable


class Pool[T]:
    """Spawns threads, runs the loop, serialises the calls to `select`."""

    def __init__(self, n_workers: int,
                 item_selector: Callable[[T | None, BaseException | None],
                                         T | None],
                 item_handler: Callable[[T], None]) -> None:
        """
        n_workers       ceiling, not a target.  Surplus threads ask the
                        selector once, are told None, and exit — microseconds
                        each, which is why the pool need not know how much work
                        there is.

        item_selector   `(done, error) -> next item, or None to shut this
                        worker down`.  Called WITH the lock held: it mutates
                        the caller's queue and accounting, and serialising it
                        is the pool's entire contribution — the caller's own
                        state then needs no locks.

                        `done` is the item just finished, None on the first
                        call.  `error` is the exception its handler raised, or
                        None if it succeeded.  **One call, one place, one
                        outcome** — which is why there is no separate error
                        callback.  An earlier version had one, and it created a
                        trap: the failed item reached both the error handler
                        and the selector, so a caller that released resources
                        in both freed them twice and a budget policy drifted
                        upward until it admitted work there was no memory for.
                        Collapsing the two removed the possibility instead of
                        documenting the hazard.

        item_handler    called WITHOUT the lock, concurrently across workers.
                        This is where the real work happens (a subprocess, a
                        repair pass), so holding the lock across it would
                        serialise every worker and make the pool pointless.
                        `test_handle_does_run_concurrently` exists to make that
                        failure loud rather than a silent halving of
                        throughput.
        """
        self._n = max(1, n_workers)
        self._select = item_selector
        self._handle = item_handler
        self._lock = threading.Lock()
        self._stopped = False

    def stop(self) -> None:
        """Tell every worker to shut down before it takes another item."""
        with self._lock:
            self._stopped = True

    def start(self) -> None:
        """Spawn the threads and wait for all of them to finish."""
        threads = [threading.Thread(target=self._run, name=f"w{i}", daemon=True)
                   for i in range(self._n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # -- the worker loop, one per thread --------------------------------------

    def _run(self) -> None:
        """Take, work, report — until `select` says to stop.

        `done` is assigned after the try/except rather than inside it, so a
        failed item is reported exactly like a successful one.  Whether a
        failure should release its resources is the policy's business: it sees
        the item either way and can decide.
        """
        done: T | None = None
        error: BaseException | None = None
        while True:
            with self._lock:
                if self._stopped:
                    return
                item = self._select(done, error)
            if item is None:
                return
            error = None
            try:
                self._handle(item)
            except Exception as exc:          # noqa: BLE001 — reported, not raised
                error = exc
            # Both assigned outside the except, so a failed item is reported
            # exactly like a successful one — with its exception alongside.
            done = item
