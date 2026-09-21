"""Run caller-selected work in threads.

`Pool(n, select, handle).start()` calls `select(done, error)` under a lock;
`handle(item)` runs outside it. A selector returns None to retire that worker.
Each completed item, including a failed one, is reported exactly once at its
next selection. The caller owns queue order, admission, and error policy.

A handler exception is delivered to `select` as `error`. A `select` exception
is different in kind — it breaks the very channel failures are reported on —
so it stops the pool and is re-raised from `start()` once the workers have
joined.
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
        """Configure a worker ceiling, selector, and handler.

        `item_selector(done, error)` runs with the pool lock held. `done` is None on
        the first call; `error` is the handler exception or None. The handler runs
        without the lock. Keep selector calls short and do not release the same item
        in another error callback.
        """
        self._n = max(1, n_workers)
        self._select = item_selector
        self._handle = item_handler
        self._lock = threading.Lock()
        self._stopped = False
        self._selector_error: BaseException | None = None

    def stop(self) -> None:
        """Tell every worker to shut down before it takes another item.

        Only prevents the *next* selection — a worker already inside its
        handler runs to completion.  That makes this the wrong answer to an
        interrupt (see `start`) and the right one for a graceful limit, such as
        "stop after N items".
        """
        with self._lock:
            self._stopped = True

    def start(self) -> None:
        """Spawn the threads and wait for all of them to finish.

        **KeyboardInterrupt is deliberately not caught.**  It propagates to the
        caller, which installs the signal handler and owns the whole shutdown
        sequence — killing subprocesses, sweeping child PIDs, deciding what a
        second Ctrl+C means.  None of that is knowable here.

        Catching it and calling `stop()` would be worse than useless: `stop()`
        waits for in-flight handlers, so an interrupt would appear to do
        nothing for as long as the slowest item takes — the opposite of what
        pressing it means.  Killing mid-work is safe in this project because an
        unfinished item leaves its pending marker and the next run redoes it.
        """
        threads = [threading.Thread(target=self._run, name=f"w{i}", daemon=True)
                   for i in range(self._n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if self._selector_error is not None:
            # A handler failure is reported *through* the selector, which is
            # why `_run` swallows it.  A failure *of* the selector has no such
            # channel: the workers are gone and nothing was processed, so
            # returning normally would present an empty run as a complete one.
            # Raised after the join so every worker has already stopped.
            raise self._selector_error

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
                try:
                    item = self._select(done, error)
                except Exception as exc:      # noqa: BLE001 — re-raised in start
                    # Keep the first one: later workers calling the same broken
                    # selector would only overwrite it with the same fault, and
                    # the first is the one with the untouched state behind it.
                    if self._selector_error is None:
                        self._selector_error = exc
                    self._stopped = True      # already holding the lock
                    return
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
