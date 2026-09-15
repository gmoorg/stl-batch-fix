"""Tests for libs.pool — no meshes, no subprocesses, no domain at all.

The pool is the half of the refactor the 36 pipeline tests cannot reach, so it
gets its own. Everything here is pure Python and deterministic: fake work is a
short sleep, and policies are plain closures over each test's own list.

Note the shape: the pool owns no queue and no loop is written here. Every test
builds its own list, closes over it in `select`, and supplies a `handle` —
which is exactly how the pipeline will use it.
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
    """A handler that records what it was given, safely across threads."""
    seen, lock = [], threading.Lock()

    def handle(item):
        time.sleep(0.01)
        with lock:
            seen.append(item)

    return handle, seen


class TestBasics(unittest.TestCase):

    def test_every_item_handled_exactly_once(self):
        items = [f"i{n}" for n in range(50)]
        queue = list(items)
        handle, seen = _collector()
        Pool(4, _fifo_over(queue), handle).start()
        self.assertCountEqual(seen, items)
        self.assertEqual(queue, [], "queue not drained")

    def test_empty_queue_stops_immediately(self):
        handle, seen = _collector()
        Pool(4, _fifo_over([]), handle).start()
        self.assertEqual(seen, [])

    def test_more_threads_than_work_is_harmless(self):
        """n_workers is a ceiling, not a target — surplus threads just exit."""
        handle, seen = _collector()
        Pool(10, _fifo_over(["only"]), handle).start()
        self.assertEqual(seen, ["only"])

    def test_start_returns_only_when_all_work_is_done(self):
        queue = list(range(20))
        handle, seen = _collector()
        Pool(3, _fifo_over(queue), handle).start()
        self.assertEqual(len(seen), 20, "start() returned early")


class TestDoneReporting(unittest.TestCase):
    """`done` is what makes resource accounting possible."""

    def test_first_call_reports_none(self):
        firsts, lock = [], threading.Lock()
        queue = ["a"]

        def select(done):
            with lock:
                firsts.append(done)
            return queue.pop(0) if queue else None

        handle, _ = _collector()
        Pool(1, select, handle).start()
        self.assertIsNone(firsts[0], "first call should report done=None")

    def test_each_item_is_reported_before_the_next_is_taken(self):
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

        handle, _ = _collector()
        Pool(1, select, handle).start()
        self.assertEqual(
            events,
            [("take", "a"), ("done", "a"), ("take", "b"),
             ("done", "b"), ("take", "c"), ("done", "c")])

    def test_every_item_is_reported_including_the_last(self):
        """No item goes unreported, which is what keeps accounting exact."""
        queue = list(range(12))
        reported, lock = [], threading.Lock()

        def select(done):
            with lock:
                if done is not None:
                    reported.append(done)
            return queue.pop(0) if queue else None

        handle, seen = _collector()
        Pool(3, select, handle).start()
        self.assertCountEqual(reported, seen,
                              "an item was handled but never reported back")

    def test_budget_returns_to_zero_after_a_full_run(self):
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

        handle, _ = _collector()
        Pool(2, budget, handle).start()
        self.assertEqual(committed["total"], 0, "capacity leaked")


class TestFailureHandling(unittest.TestCase):
    """The reason the loop moved inside the pool."""

    def test_a_raising_handler_still_reports_its_item(self):
        """The property a caller-written loop could silently omit."""
        queue = ["a", "boom", "c"]
        reported, lock = [], threading.Lock()

        def select(done):
            with lock:
                if done is not None:
                    reported.append(done)
            return queue.pop(0) if queue else None

        def handle(item):
            if item == "boom":
                raise RuntimeError("handler blew up")

        Pool(1, select, handle).start()
        self.assertIn("boom", reported,
                      "an item that raised was never reported to select")
        self.assertCountEqual(reported, ["a", "boom", "c"])

    def test_a_raising_handler_does_not_stop_the_worker(self):
        queue = list(range(10))
        handled, lock = [], threading.Lock()

        def handle(item):
            if item == 3:
                raise RuntimeError("blew up")
            with lock:
                handled.append(item)

        Pool(1, _fifo_over(queue), handle).start()
        self.assertCountEqual(handled, [0, 1, 2, 4, 5, 6, 7, 8, 9],
                              "the loop did not survive a failure")

    def test_on_error_receives_the_item_and_the_exception(self):
        errors, lock = [], threading.Lock()

        def handle(item):
            raise ValueError(f"no good: {item}")

        def on_error(item, exc):
            with lock:
                errors.append((item, type(exc), str(exc)))

        Pool(1, _fifo_over(["x"]), handle,
             item_error_handler=on_error).start()
        self.assertEqual(len(errors), 1)
        item, kind, msg = errors[0]
        self.assertEqual(item, "x")
        self.assertIs(kind, ValueError)
        self.assertEqual(msg, "no good: x")

    def test_a_failed_item_is_reported_once_not_twice(self):
        """The error handler must not release — the selector already will.

        A failed item reaches the selector as `done` exactly like a successful
        one. A caller that also frees resources in the error handler frees them
        twice, and a budget policy drifts upward until it admits work there is
        no memory for.
        """
        queue = [("ok", 10), ("boom", 30), ("fine", 20)]
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

        def handle(item):
            if item[0] == "boom":
                raise RuntimeError("blew up")

        seen_errors = []

        def on_error(item, exc):
            # Correct: log only. Releasing here would double-count.
            seen_errors.append(item)

        Pool(1, budget, handle, item_error_handler=on_error).start()
        self.assertEqual(seen_errors, [("boom", 30)])
        self.assertEqual(committed["total"], 0,
                         "a failed item was released a different number of "
                         "times than it was committed")

    def test_errors_are_swallowed_without_on_error(self):
        def handle(item):
            raise RuntimeError("silent")

        # Must not raise out of start().
        Pool(2, _fifo_over(["a", "b"]), handle).start()


class TestPolicyControl(unittest.TestCase):

    def test_policy_controls_order(self):
        queue = [1, 2, 3, 4, 5]

        def newest_first(done):
            return queue.pop() if queue else None

        order, lock = [], threading.Lock()

        def handle(item):
            with lock:
                order.append(item)

        Pool(1, newest_first, handle).start()
        self.assertEqual(order, [5, 4, 3, 2, 1])

    def test_returning_none_sheds_the_worker(self):
        queue = ["x"]

        def never(done):
            return None

        handle, seen = _collector()
        done_flag = threading.Event()
        pool = Pool(2, never, handle)
        threading.Thread(target=lambda: (pool.start(), done_flag.set()),
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

        handle, seen = _collector()
        Pool(1, expand_once, handle).start()
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

        def handle(item):
            time.sleep(0.02)
            with lock:
                handled.append(item[0])

        Pool(4, budget, handle).start()
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

        handle, seen = _collector()
        done_flag = threading.Event()
        pool = Pool(2, budget, handle)
        threading.Thread(target=lambda: (pool.start(), done_flag.set()),
                         daemon=True).start()
        self.assertTrue(done_flag.wait(timeout=5), "oversized item deadlocked")
        self.assertEqual(seen, [])
        self.assertEqual(queue, [("huge", 500)])


class TestControl(unittest.TestCase):

    def test_stop_ends_the_run_early(self):
        queue = list(range(500))
        handled, lock = [], threading.Lock()
        pool = None

        def handle(item):
            with lock:
                handled.append(item)
                if len(handled) == 5:
                    pool.stop()
            time.sleep(0.005)

        pool = Pool(3, _fifo_over(queue), handle)
        pool.start()
        self.assertLess(len(handled), 500, "stop() did not stop anything")
        self.assertGreater(len(queue), 0, "queue should still hold work")


class TestConcurrency(unittest.TestCase):

    def test_select_is_never_called_concurrently(self):
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

        handle, _ = _collector()
        Pool(8, policy, handle).start()
        self.assertEqual(inside["max"], 1, "select ran concurrently")

    def test_handle_does_run_concurrently(self):
        """`handle` is called without the lock — otherwise the pool is pointless.

        Without this, a pool that serialised every handler would pass every
        other test in this file.
        """
        inside = {"now": 0, "max": 0}
        lock = threading.Lock()
        queue = list(range(40))

        def handle(item):
            with lock:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.01)
            with lock:
                inside["now"] -= 1

        Pool(4, _fifo_over(queue), handle).start()
        self.assertGreater(inside["max"], 1,
                           "handlers never overlapped — the lock is held too long")

    def test_no_item_handed_out_twice_under_contention(self):
        items = list(range(400))
        queue = list(items)
        handle, seen = _collector()
        Pool(16, _fifo_over(queue), handle).start()
        self.assertEqual(len(seen), len(set(seen)), "an item was handed out twice")
        self.assertCountEqual(seen, items)


if __name__ == '__main__':
    unittest.main(verbosity=2)
