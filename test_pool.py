"""Tests for libs.pool — no meshes, no subprocesses, no domain at all.

The pool is the half of the refactor the 36 pipeline tests cannot reach, so it
gets its own. Everything here is pure Python and deterministic: fake work is a
short sleep, and admission rules are plain predicates.
"""

import threading
import time
import unittest

from libs.pool import Pool


def _collector():
    """A worker that records what it handled, safely across threads."""
    seen, lock = [], threading.Lock()

    def work(pool):
        while (item := pool.get_next()) is not None:
            time.sleep(0.01)
            with lock:
                seen.append(item)

    return work, seen


class TestBasics(unittest.TestCase):

    def test_every_item_handled_exactly_once(self):
        items = [f"i{n}" for n in range(50)]
        work, seen = _collector()
        Pool(items, n_workers=4).start(work)
        self.assertCountEqual(seen, items)

    def test_empty_queue_stops_immediately(self):
        work, seen = _collector()
        Pool([], n_workers=4).start(work)
        self.assertEqual(seen, [])

    def test_worker_count_capped_by_queue_length(self):
        pool = Pool(["only"], n_workers=8)
        names = set()
        lock = threading.Lock()

        def work(p):
            while p.get_next() is not None:
                with lock:
                    names.add(threading.current_thread().name)

        pool.start(work)
        self.assertEqual(len(names), 1, "more threads than items")

    def test_pending_drains(self):
        pool = Pool(list(range(10)), n_workers=2)
        self.assertEqual(pool.pending(), 10)
        work, _ = _collector()
        pool.start(work)
        self.assertEqual(pool.pending(), 0)


class TestAdmission(unittest.TestCase):

    def test_admit_is_consulted(self):
        asked = []
        lock = threading.Lock()

        def admit(item, running, alone):
            with lock:
                asked.append(item)
            return True

        work, seen = _collector()
        Pool(["a", "b", "c"], n_workers=2, admit=admit).start(work)
        self.assertCountEqual(seen, ["a", "b", "c"])
        self.assertTrue(asked, "admit was never called")

    def test_exclusive_item_never_runs_beside_another(self):
        """The real case: one item that must not share the machine."""
        concurrent, peak, lock = [], [0], threading.Lock()

        def admit(item, running, alone):
            if item == "big":
                return not running          # big runs only when alone
            return "big" not in running     # nothing starts beside big

        def work(pool):
            while (item := pool.get_next()) is not None:
                with lock:
                    concurrent.append(item)
                    peak[0] = max(peak[0], len(concurrent))
                    beside_big = "big" in concurrent and len(concurrent) > 1
                time.sleep(0.02)
                with lock:
                    concurrent.remove(item)
                self.assertFalse(beside_big, "something ran beside 'big'")

        items = ["s1", "s2", "big", "s3", "s4"]
        Pool(items, n_workers=3, admit=admit).start(work)
        self.assertGreater(peak[0], 1, "never ran concurrently at all")

    def test_alone_true_lets_policy_run_it_solo(self):
        """Refused beside others, accepted alone -> it still gets handled."""
        calls = []
        lock = threading.Lock()

        def admit(item, running, alone):
            with lock:
                calls.append(alone)
            return alone or not running

        work, seen = _collector()
        Pool(["x"], n_workers=1, admit=admit).start(work)
        self.assertEqual(seen, ["x"])

    def test_alone_false_sheds_the_worker(self):
        """A policy that refuses even when alone must not deadlock."""
        def admit(item, running, alone):
            return False                    # never admissible

        work, seen = _collector()
        pool = Pool(["never"], n_workers=2, admit=admit)

        done = threading.Event()
        threading.Thread(target=lambda: (pool.start(work), done.set()),
                         daemon=True).start()
        self.assertTrue(done.wait(timeout=5), "pool deadlocked on a refused item")
        self.assertEqual(seen, [], "a refused item was handled anyway")
        self.assertEqual(pool.pending(), 1, "the item should stay queued")


class TestSelectOverride(unittest.TestCase):
    """The injected selection policy — the reason get_next_default exists."""

    def test_default_is_used_when_none_passed(self):
        from libs.pool import get_next_default
        self.assertIs(Pool([1, 2], 2)._select, get_next_default)

    def test_custom_select_controls_order(self):
        """LIFO instead of FIFO, purely by passing a different select."""
        def newest_first(items, running, admit):
            return items.pop(), True

        order, lock = [], threading.Lock()

        def work(pool):
            while (item := pool.get_next()) is not None:
                with lock:
                    order.append(item)

        # One worker, so the order recorded is the order handed out.
        Pool([1, 2, 3, 4, 5], n_workers=1, select=newest_first).start(work)
        self.assertEqual(order, [5, 4, 3, 2, 1])

    def test_custom_select_may_ignore_admit_entirely(self):
        """A select that never consults admit is free to do so."""
        def blind(items, running, admit):
            return items.pop(0), True

        def refuse_everything(item, running, alone):
            raise AssertionError("admit should not have been consulted")

        work, seen = _collector()
        Pool(["a", "b"], n_workers=1,
             admit=refuse_everything, select=blind).start(work)
        self.assertCountEqual(seen, ["a", "b"])

    def test_custom_select_can_shed(self):
        """(None, False) shuts the worker down without a deadlock."""
        def never(items, running, admit):
            return None, False

        work, seen = _collector()
        pool = Pool(["x"], n_workers=2, select=never)
        done = threading.Event()
        threading.Thread(target=lambda: (pool.start(work), done.set()),
                         daemon=True).start()
        self.assertTrue(done.wait(timeout=5), "select-driven shed deadlocked")
        self.assertEqual(seen, [])
        self.assertEqual(pool.pending(), 1)

    def test_custom_select_may_add_to_the_queue(self):
        """It holds the real list under the lock, so it can grow it."""
        def expand_once(items, running, admit):
            if items == ["seed"]:
                items.extend(["grown-1", "grown-2"])
            return items.pop(0), True

        work, seen = _collector()
        Pool(["seed"], n_workers=1, select=expand_once).start(work)
        self.assertCountEqual(seen, ["seed", "grown-1", "grown-2"])


class TestControl(unittest.TestCase):

    def test_stop_ends_the_run_early(self):
        pool = Pool(list(range(500)), n_workers=3)
        handled, lock = [], threading.Lock()

        def work(p):
            while (item := p.get_next()) is not None:
                with lock:
                    handled.append(item)
                    if len(handled) == 5:
                        p.stop()
                time.sleep(0.005)

        pool.start(work)
        self.assertLess(len(handled), 500, "stop() did not stop anything")
        self.assertGreater(pool.pending(), 0)

    def test_status_reports_work_in_flight(self):
        pool = Pool(["a", "b", "c", "d"], n_workers=2)
        saw_busy = threading.Event()

        def work(p):
            while (item := p.get_next()) is not None:
                if p.status():
                    saw_busy.set()
                time.sleep(0.02)

        pool.start(work)
        self.assertTrue(saw_busy.is_set(), "status() never showed an item in flight")
        self.assertEqual(pool.status(), {}, "status() not empty after the run")


class TestThreadSafety(unittest.TestCase):

    def test_no_item_handed_out_twice_under_contention(self):
        items = list(range(400))
        work, seen = _collector()
        Pool(items, n_workers=16).start(work)
        self.assertEqual(len(seen), len(set(seen)), "an item was handed out twice")
        self.assertCountEqual(seen, items)

    def test_worker_exception_does_not_hang_the_pool(self):
        """One thread raising must not strand the others."""
        def work(pool):
            while (item := pool.get_next()) is not None:
                if item == 13:
                    raise RuntimeError("worker blew up")

        pool = Pool(list(range(60)), n_workers=4)
        done = threading.Event()
        threading.Thread(target=lambda: (pool.start(work), done.set()),
                         daemon=True).start()
        self.assertTrue(done.wait(timeout=5), "pool hung after a worker raised")


if __name__ == '__main__':
    unittest.main(verbosity=2)
