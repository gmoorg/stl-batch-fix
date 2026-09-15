"""Tests for libs.pool — no meshes, no subprocesses, no domain at all.

The pool is the half of the refactor the 36 pipeline tests cannot reach, so it
gets its own. Everything here is pure Python and deterministic: fake work is a
short sleep, and policies are plain closures over the caller's own queue.

Note the shape: the pool owns no queue. Every test builds its own list and
closes over it, which is exactly how the pipeline will use it.
"""

import threading
import time
import unittest

from libs.pool import Pool


def _fifo_over(queue):
    """The simplest policy: hand out the head of the caller's list."""
    def select(done):
        return queue.pop(0) if queue else None
    return select


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
        queue = list(items)
        work, seen = _collector()
        Pool(4, _fifo_over(queue)).start(work)
        self.assertCountEqual(seen, items)
        self.assertEqual(queue, [], "queue not drained")

    def test_empty_queue_stops_immediately(self):
        work, seen = _collector()
        Pool(4, _fifo_over([])).start(work)
        self.assertEqual(seen, [])

    def test_more_threads_than_work_is_harmless(self):
        """n_workers is a ceiling, not a target — surplus threads just exit."""
        queue = ["only"]
        work, seen = _collector()
        Pool(10, _fifo_over(queue)).start(work)
        self.assertEqual(seen, ["only"])

    def test_holding_is_empty_after_the_run(self):
        queue = list(range(20))
        work, _ = _collector()
        pool = Pool(3, _fifo_over(queue))
        pool.start(work)
        self.assertEqual(pool.holding(), {})


class TestDoneReporting(unittest.TestCase):
    """`done` is what makes resource accounting possible."""

    def test_first_call_reports_none(self):
        firsts, lock = [], threading.Lock()
        queue = ["a"]

        def select(done):
            with lock:
                firsts.append(done)
            return queue.pop(0) if queue else None

        work, _ = _collector()
        Pool(1, select).start(work)
        self.assertIsNone(firsts[0], "first call should report done=None")

    def test_each_item_is_reported_before_the_next_is_taken(self):
        """A single worker must report item N before receiving item N+1."""
        events, lock = [], threading.Lock()
        queue = ["a", "b", "c"]

        def select(done):
            with lock:
                if done is not None:
                    events.append(("done", done))
            item = queue.pop(0) if queue else None
            if item is not None:
                with lock:
                    events.append(("take", item))
            return item

        work, _ = _collector()
        Pool(1, select).start(work)
        self.assertEqual(
            events,
            [("take", "a"), ("done", "a"), ("take", "b"),
             ("done", "b"), ("take", "c"), ("done", "c")])

    def test_every_item_is_reported_including_the_last(self):
        """No item goes unreported, which is what keeps accounting exact.

        The worker's final call still runs `select(done)` before being told
        None and exiting, so the last item of each worker comes back too. An
        earlier design had the pool call select itself in start()'s finally to
        achieve this; that turned out unnecessary — and it was the thing making
        stop() over-commit, since a combined select cannot release without also
        acquiring.
        """
        queue = list(range(12))
        reported, lock = [], threading.Lock()

        def select(done):
            with lock:
                if done is not None:
                    reported.append(done)
            return queue.pop(0) if queue else None

        work, seen = _collector()
        Pool(3, select).start(work)
        self.assertCountEqual(reported, seen,
                              "an item was handled but never reported back")

    def test_budget_returns_to_zero_after_a_full_run(self):
        """The practical consequence: a running total ends where it started."""
        queue = [("a", 10), ("b", 20), ("c", 30), ("d", 40)]
        committed = {"total": 0}
        lock = threading.Lock()

        def budget(done):
            with lock:
                if done is not None:
                    committed["total"] -= done[1]
                if not queue:
                    return None
                item = queue.pop(0)
                committed["total"] += item[1]
                return item

        work, _ = _collector()
        Pool(2, budget).start(work)
        self.assertEqual(committed["total"], 0, "capacity leaked")


class TestPolicyControl(unittest.TestCase):

    def test_policy_controls_order(self):
        queue = [1, 2, 3, 4, 5]

        def newest_first(done):
            return queue.pop() if queue else None

        order, lock = [], threading.Lock()

        def work(pool):
            while (item := pool.get_next()) is not None:
                with lock:
                    order.append(item)

        Pool(1, newest_first).start(work)
        self.assertEqual(order, [5, 4, 3, 2, 1])

    def test_returning_none_sheds_the_worker(self):
        queue = ["x"]

        def never(done):
            return None

        work, seen = _collector()
        done_flag = threading.Event()
        pool = Pool(2, never)
        threading.Thread(target=lambda: (pool.start(work), done_flag.set()),
                         daemon=True).start()
        self.assertTrue(done_flag.wait(timeout=5), "shedding deadlocked")
        self.assertEqual(seen, [])
        self.assertEqual(queue, ["x"], "the item should stay in the caller's queue")

    def test_policy_may_grow_its_own_queue(self):
        queue = ["seed"]

        def expand_once(done):
            if queue == ["seed"]:
                queue.extend(["grown-1", "grown-2"])
            return queue.pop(0) if queue else None

        work, seen = _collector()
        Pool(1, expand_once).start(work)
        self.assertCountEqual(seen, ["seed", "grown-1", "grown-2"])


class TestBudgetPolicy(unittest.TestCase):
    """End to end on the case the whole design exists for."""

    def test_costly_items_never_exceed_capacity(self):
        CAPACITY = 100
        committed = {"total": 0}
        peak = {"value": 0}
        # (name, cost), cheapest first — the ordering the real queue uses.
        queue = [("a", 10), ("b", 20), ("c", 30), ("d", 60), ("e", 90)]

        def budget(done):
            if done is not None:
                committed["total"] -= done[1]
            if not queue:
                return None
            item = queue[0]
            if committed["total"] + item[1] > CAPACITY:
                return None        # will not fit, and nothing smaller is coming
            committed["total"] += item[1]
            peak["value"] = max(peak["value"], committed["total"])
            return queue.pop(0)

        handled, lock = [], threading.Lock()

        def work(pool):
            while (item := pool.get_next()) is not None:
                time.sleep(0.02)
                with lock:
                    handled.append(item[0])

        Pool(4, budget).start(work)
        self.assertLessEqual(peak["value"], CAPACITY, "capacity was exceeded")
        self.assertCountEqual(handled, ["a", "b", "c", "d", "e"])

    def test_item_larger_than_capacity_is_left_queued(self):
        CAPACITY = 50
        committed = {"total": 0}
        queue = [("huge", 500)]

        def budget(done):
            if done is not None:
                committed["total"] -= done[1]
            if not queue:
                return None
            item = queue[0]
            if committed["total"] + item[1] > CAPACITY:
                return None
            committed["total"] += item[1]
            return queue.pop(0)

        work, seen = _collector()
        done_flag = threading.Event()
        pool = Pool(2, budget)
        threading.Thread(target=lambda: (pool.start(work), done_flag.set()),
                         daemon=True).start()
        self.assertTrue(done_flag.wait(timeout=5), "oversized item deadlocked")
        self.assertEqual(seen, [])
        self.assertEqual(queue, [("huge", 500)])


class TestControl(unittest.TestCase):

    def test_stop_ends_the_run_early(self):
        queue = list(range(500))
        pool = Pool(3, _fifo_over(queue))
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
        self.assertGreater(len(queue), 0, "queue should still hold work")

    def test_stop_hands_out_nothing_further(self):
        """After stop(), select is not called again — nothing is committed
        and then discarded, which is what made the earlier design leak."""
        queue = list(range(200))
        calls = {"n": 0}
        lock = threading.Lock()

        def counting(done):
            with lock:
                calls["n"] += 1
            return queue.pop(0) if queue else None

        pool = Pool(3, counting)
        handled, hlock = [], threading.Lock()

        def work(p):
            while (item := p.get_next()) is not None:
                with hlock:
                    handled.append(item)
                    if len(handled) == 5:
                        p.stop()
                time.sleep(0.005)

        pool.start(work)
        with lock:
            # Every call handed out an item that was worked, except the ones
            # that returned None at the very end.
            self.assertLessEqual(calls["n"], len(handled) + 3)


class TestThreadSafety(unittest.TestCase):

    def test_no_item_handed_out_twice_under_contention(self):
        items = list(range(400))
        queue = list(items)
        work, seen = _collector()
        Pool(16, _fifo_over(queue)).start(work)
        self.assertEqual(len(seen), len(set(seen)), "an item was handed out twice")
        self.assertCountEqual(seen, items)

    def test_policy_is_never_called_concurrently(self):
        """The pool's whole contribution: one worker inside select at a time."""
        inside = {"now": 0, "max": 0}
        lock = threading.Lock()
        queue = list(range(80))

        def policy(done):
            with lock:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.001)
            result = queue.pop(0) if queue else None
            with lock:
                inside["now"] -= 1
            return result

        work, _ = _collector()
        Pool(8, policy).start(work)
        self.assertEqual(inside["max"], 1, "select ran concurrently")


if __name__ == '__main__':
    unittest.main(verbosity=2)
