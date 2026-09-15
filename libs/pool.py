"""A worker pool: N threads, each pulling work from a caller-supplied function.

Domain-free by construction.  The pool owns a lock, a shutdown flag and the
worker loop.  It owns no items, no ordering and no admission rule — those are
the caller's, expressed as two functions:

    item_selector(done) -> T | None   the next item, or None to shut this
                                      worker down
    item_handler(item) -> None        do the work

Usage:

    queue = [...]                     # the caller's list, never the pool's

    def next_item(done):
        if done is not None:
            release(done)             # accounting, if the caller needs any
        return queue.pop(0) if queue else None

    Pool(4, next_item, repair_one).start()

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
                 item_selector: Callable[[T | None], T | None],
                 item_handler: Callable[[T], None],
                 item_error_handler: Callable[[T, BaseException], None] | None = None
                 ) -> None:
        """
        n_workers           ceiling, not a target.  Surplus threads ask the
                            selector once, are told None, and exit —
                            microseconds each, which is why the pool need not
                            know how much work there is.

        item_selector       called WITH the lock held.  It mutates the caller's
                            queue and accounting, so serialising it is the
                            pool's entire contribution — the caller's own state
                            then needs no locks.

        item_handler        called WITHOUT the lock, concurrently across
                            workers.  This is where the real work happens
                            (a subprocess, a repair pass), so holding the lock
                            across it would serialise every worker and make the
                            pool pointless.  `test_handle_does_run_concurrently`
                            exists to make that failure loud rather than a
                            silent halving of throughput.

        item_error_handler  called if the handler raises.  Bookkeeping, and
                            cheap, so it runs under the lock too: the caller's
                            error log needs no lock of its own, same argument as
                            the selector.  Without this, exceptions are
                            swallowed silently.

                            **It must not release the item.**  A failed item is
                            still passed to the selector as `done` on the next
                            call, exactly like a successful one — so a caller
                            that frees resources here *and* in the selector
                            frees them twice.  For a budget policy that means
                            the running total drifts upward until the pool
                            admits work there is no memory for.  Log here;
                            release in the selector, which sees every item
                            regardless of outcome.
        """
        self._n = max(1, n_workers)
        self._select = item_selector
        self._handle = item_handler
        self._on_error = item_error_handler
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
        while True:
            with self._lock:
                if self._stopped:
                    return
                item = self._select(done)
            if item is None:
                return
            try:
                self._handle(item)
            except Exception as exc:          # noqa: BLE001 — see the ctor
                if self._on_error is not None:
                    with self._lock:
                        self._on_error(item, exc)
            # Assigned outside the except, so a failed item is reported to the
            # selector exactly like a successful one.  This is also why the
            # error handler must not release the item itself: it would be
            # released here too, and a budget policy would drift upward.
            done = item
