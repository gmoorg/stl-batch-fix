"""A worker pool: N threads pull from a shared queue, with optional admission.

Domain-free by construction.  The pool never inspects an item beyond passing it
to the caller's callbacks, so items can be paths, URLs, job records, anything.
All policy lives in two callables the caller supplies:

    admit(item, running, alone) -> bool     may this item start now?
    work(pool)                             what a worker thread does

Usage:

    pool = Pool(items, n_workers=4, admit=my_rule)

    def worker(pool):
        while (item := pool.get_next()) is not None:
            handle(item)

    pool.start(worker)

Threads, not processes, deliberately: a worker that drives a subprocess spends
its life blocked in `communicate()` rather than computing, so the GIL is not a
constraint, and sharing one queue in one process means no pickling, no proxy
objects, and no pool-wide failure when a single worker dies.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterable, Protocol


class Admit[T](Protocol):
    """Decides whether `item` may start while `running` are in flight.

    `alone` is True on a second ask, made only when the pool has nothing else
    running and would otherwise wait forever.  A resource rule normally says
    yes to that (nothing is competing); a rule that must never run this item
    says no, and the pool sheds the worker instead of deadlocking.
    """

    def __call__(self, item: T, running: list[T], alone: bool) -> bool: ...


class Pool[T]:
    """Owns the queue and the threads.  Workers pull; nobody pushes to them."""

    def __init__(self, items: Iterable[T], n_workers: int,
                 admit: Admit[T] | None = None) -> None:
        self._items: list[T] = list(items)
        self._n = max(1, min(n_workers, len(self._items) or 1))
        self._admit = admit
        self._lock = threading.Lock()
        self._free = threading.Condition(self._lock)
        self._running: dict[str, T] = {}      # thread name -> item in flight
        self._stopped = False

    # -- the synchronisation point -------------------------------------------

    def get_next(self) -> T | None:
        """The next item, or None meaning *this worker should shut down*.

        None has exactly two causes, and the caller need not tell them apart:

          1. The queue is drained (or stop() was called) — ordinary shutdown.
          2. `admit` will not let the head item run beside the work already in
             flight, and will not let it run alone either.  The worker exits so
             its resources are released; the item stays at the head of the queue
             for whoever is still running.

        Case 2 is why this returns None rather than blocking.  A worker parked
        on a condition variable still holds a stack and a status slot — and, in
        the case that motivates admission control, the very resources the
        blocked item is waiting for.  Shedding converts that standoff into
        "fewer workers, then the big item runs".

        Blocking still happens, but only while the situation can improve: some
        other worker is in flight and may release what is needed.  When nothing
        is in flight there is nothing to wait for, so the pool asks `admit` one
        final time with alone=True and shuts the worker down if refused.
        """
        me = threading.current_thread().name
        with self._free:
            self._running.pop(me, None)
            self._free.notify_all()           # releasing may unblock someone
            while True:
                if self._stopped or not self._items:
                    return None
                head = self._items[0]
                if self._admit is None:
                    return self._take(me)
                running = list(self._running.values())
                if self._admit(head, running, False):
                    return self._take(me)
                if not running:
                    # Nothing in flight: waiting cannot change the answer.
                    # The caller's policy decides between running it solo and
                    # refusing it outright; the pool does not override either.
                    if self._admit(head, [], True):
                        return self._take(me)
                    return None
                self._free.wait(0.25)

    def _take(self, me: str) -> T:
        """Pop the head and record it.  Caller must hold the lock."""
        item = self._items.pop(0)
        self._running[me] = item
        return item

    # -- observation ---------------------------------------------------------

    def status(self) -> dict[str, T]:
        """What each worker is on right now, for a progress display."""
        with self._lock:
            return dict(self._running)

    def pending(self) -> int:
        """How many items have not been handed out yet."""
        with self._lock:
            return len(self._items)

    def stop(self) -> None:
        """Tell every worker to shut down after its current item."""
        with self._free:
            self._stopped = True
            self._free.notify_all()

    # -- running -------------------------------------------------------------

    def start(self, work: Callable[[Pool[T]], None]) -> None:
        """Spawn the threads, each running work(self), and wait for them.

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
