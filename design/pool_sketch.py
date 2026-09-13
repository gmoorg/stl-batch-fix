"""A worker pool: threads pull from a shared queue, each drives a subprocess.

Runnable sketch, not production code.  Run it:

    python pool.py

The point of the shape: the mesh work already happens in a --one-file child
process, so the pool itself does not need to be a process pool.  Threads
sharing a queue in one process means no pickling, no Manager proxy, no
BrokenProcessPool, and a worker that can kill its own child directly because
it holds the Popen object.
"""

import threading
import time


class Pool:
    """Owns the queue and the threads.  Workers pull; nobody pushes to them."""

    def __init__(self, items, n_workers, admit=None, log=print):
        self._items = list(items)
        self._n = max(1, min(n_workers, len(self._items) or 1))
        self._admit = admit
        self._log = log
        self._lock = threading.Lock()
        self._free = threading.Condition(self._lock)
        self._running = {}          # thread name -> item, for admit() and status
        self._attempts = {}         # item -> times it has been handed out
        self._stopped = False

    # -- the synchronisation point -------------------------------------------

    def get_next(self, timeout=None):
        """Next item, or None when the queue is drained or the pool is stopped.

        Blocks while `admit` refuses the head item -- that is where a
        memory-aware rule lives.  The timeout is the escape hatch: if nothing
        can be admitted for that long, the caller is handed the item anyway
        rather than deadlocking on a file too big to ever fit.
        """
        deadline = time.monotonic() + timeout if timeout else None
        me = threading.current_thread().name
        with self._free:
            self._running.pop(me, None)
            self._free.notify_all()          # someone else may now fit
            while True:
                if self._stopped or not self._items:
                    return None
                head = self._items[0]
                ok = self._admit is None or self._admit(head, list(self._running.values()))
                forced = deadline is not None and time.monotonic() >= deadline
                if ok or forced or not self._running:
                    # `not self._running` is the other escape: if nothing else
                    # is in flight, this item runs alone or never runs at all.
                    if not ok:
                        self._log(f"  admit: running {head} anyway "
                                  f"({'timed out' if forced else 'nothing else in flight'})")
                    self._items.pop(0)
                    self._running[me] = head
                    self._attempts[head] = self._attempts.get(head, 0) + 1
                    return head
                self._free.wait(0.25)

    def requeue(self, item):
        """Put a failed item back -- at the BACK.

        Front would make the next worker retry the poisonous file immediately
        and spend the whole budget on it.
        """
        with self._free:
            self._items.append(item)
            self._free.notify_all()

    def attempts(self, item):
        with self._lock:
            return self._attempts.get(item, 0)

    def status(self):
        """What each worker is on, for a progress display."""
        with self._lock:
            return dict(self._running)

    def stop(self):
        with self._free:
            self._stopped = True
            self._free.notify_all()

    # -- running -------------------------------------------------------------

    def start(self, work):
        """Spawn threads, each running work(self), and wait for them.

        `work` is an ordinary callable.  It runs in this process, so it can be
        a lambda, a closure, a bound method -- nothing is pickled.
        """
        threads = [threading.Thread(target=work, args=(self,), name=f"w{i}",
                                    daemon=True)
                   for i in range(self._n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


# ---------------------------------------------------------------------------
# Demo: stand-ins for the real thing.

def _fake_repair(src):
    """Where run_one_file() would go: spawn --one-file, wait, read the rc."""
    time.sleep({'big': 0.9, 'huge': 1.2}.get(src.split('-')[0], 0.3))
    if src == 'flaky-3':
        return 'failed'
    if src == 'poison-7':
        return 'failed'
    return 'ok'


def _demo():
    files = ['small-1', 'small-2', 'flaky-3', 'big-4', 'small-5',
             'huge-6', 'poison-7', 'small-8']

    lock = threading.Lock()
    def log(msg):
        with lock:
            print(msg)

    def admit(item, running):
        """Memory-aware: only one 'huge' at a time, and not beside a 'big'."""
        if item.startswith('huge'):
            return not any(r.startswith(('huge', 'big')) for r in running)
        return True

    pool = Pool(files, n_workers=3, admit=admit, log=log)

    def repair_one(pool):
        me = threading.current_thread().name
        while (src := pool.get_next(timeout=5)) is not None:
            log(f"{me}: -> {src}")
            outcome = _fake_repair(src)
            if outcome == 'failed' and pool.attempts(src) < 2:
                log(f"{me}:    {src} failed, requeued (attempt {pool.attempts(src)})")
                pool.requeue(src)
            else:
                log(f"{me}:    {src} {outcome}"
                    + ("  [set aside]" if outcome == 'failed' else ""))

    t0 = time.monotonic()
    pool.start(repair_one)
    log(f"\ndone in {time.monotonic() - t0:.1f}s")


if __name__ == '__main__':
    _demo()
