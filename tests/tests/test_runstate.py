"""RunState: admission, lifecycle transitions, cancellation, idempotent completion."""

import unittest
from unittest import mock

from libs.runstate import RunState


class _FakeProc:
    def __init__(self, pid=1234):
        self.pid = pid


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.state = RunState()

    def test_first_job_admitted_within_budget(self):
        token, refusal, override = self.state.start(100, 1000)
        self.assertIsNotNone(token)
        self.assertIsNone(refusal)
        self.assertFalse(override)

    def test_shed_when_over_budget_with_something_running(self):
        self.state.start(900, 1000)
        token, refusal, override = self.state.start(200, 1000)
        self.assertIsNone(token)
        self.assertEqual(refusal, 'shed')

    def test_alone_override_admits_regardless_of_budget(self):
        token, refusal, override = self.state.start(5000, 1000)
        self.assertIsNotNone(token)
        self.assertIsNone(refusal)
        self.assertTrue(override)

    def test_cancelled_refuses_admission(self):
        self.state.cancel('test')
        token, refusal, override = self.state.start(1, 1000)
        self.assertIsNone(token)
        self.assertEqual(refusal, 'cancelled')

    def test_running_total_frees_up_after_completion(self):
        token1, _, _ = self.state.start(900, 1000)
        token2, refusal, _ = self.state.start(200, 1000)
        self.assertIsNone(token2)
        self.state.mark_reaped(token1)
        self.state.complete_once(token1, lambda r: None, 'result')
        token3, refusal, _ = self.state.start(200, 1000)
        self.assertIsNotNone(token3)


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.state = RunState()

    def test_spawned_true_when_not_cancelled(self):
        token, _, _ = self.state.start(1, 1000)
        self.assertTrue(self.state.spawned(token, _FakeProc()))

    def test_spawned_false_when_cancellation_raced_it(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.cancel('raced')
        self.assertFalse(self.state.spawned(token, _FakeProc()))

    def test_proc_for_returns_registered_proc(self):
        token, _, _ = self.state.start(1, 1000)
        proc = _FakeProc()
        self.state.spawned(token, proc)
        self.assertIs(self.state.proc_for(token), proc)

    def test_proc_for_none_before_spawn(self):
        token, _, _ = self.state.start(1, 1000)
        self.assertIsNone(self.state.proc_for(token))

    def test_complete_once_rejects_wrong_state(self):
        token, _, _ = self.state.start(1, 1000)   # state is 'reserved'
        with self.assertRaises(AssertionError):
            self.state.complete_once(token, lambda r: None, 'result')

    def test_complete_once_accepts_reaped(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        self.state.complete_once(token, lambda r: None, 'result')   # does not raise

    def test_complete_once_accepts_launch_failed(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_launch_failed(token)
        self.state.complete_once(token, lambda r: None, 'result')   # does not raise

    def test_complete_once_accepts_publishing(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        self.state.authorize_publication(token)
        self.state.complete_once(token, lambda r: None, 'result')   # does not raise

    def test_complete_once_is_idempotent(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        calls = []
        self.state.complete_once(token, calls.append, 'result')
        # A genuine second call for a token that already completed
        # successfully is a silent no-op — report_fn is called exactly
        # once, not twice, even if a caller (incorrectly) calls this again.
        self.state.complete_once(token, calls.append, 'result')
        self.assertEqual(calls, ['result'])

    def test_complete_once_report_failure_leaves_token_unresolved(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)

        def raising_report(result):
            raise RuntimeError('sink broke')

        with self.assertRaises(RuntimeError):
            self.state.complete_once(token, raising_report, 'result')
        self.assertTrue(self.state.has_unresolved())
        self.assertIn(token, self.state.snapshot_unresolved())

    def test_complete_once_refuses_retry_after_report_failure(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        calls = []

        def raising_once(result):
            calls.append(result)
            raise RuntimeError('sink broke')

        with self.assertRaises(RuntimeError):
            self.state.complete_once(token, raising_once, 'first')
        # A caller that reacts to the failure by calling complete_once
        # again for the SAME token must be refused, not allowed to invoke
        # report_fn a second time for one job.
        with self.assertRaises(AssertionError):
            self.state.complete_once(token, raising_once, 'second')
        self.assertEqual(calls, ['first'])
        self.assertTrue(self.state.has_report_attempted(token))
        self.assertIn(token, self.state.snapshot_unresolved())


class TestAuthorizePublication(unittest.TestCase):
    def setUp(self):
        self.state = RunState()

    def test_true_when_not_cancelled(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        self.assertTrue(self.state.authorize_publication(token))

    def test_false_when_cancelled(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.mark_reaped(token)
        self.state.cancel('test')
        self.assertFalse(self.state.authorize_publication(token))


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.state = RunState()

    def test_kills_every_live_process(self):
        token1, _, _ = self.state.start(1, 1000)
        proc1 = _FakeProc(pid=111)
        self.state.spawned(token1, proc1)
        token2, _, _ = self.state.start(1, 1000)
        proc2 = _FakeProc(pid=222)
        self.state.spawned(token2, proc2)

        with mock.patch('libs.runstate.os.killpg') as killpg:
            self.state.cancel('test')
            self.assertEqual(killpg.call_count, 2)
            killed_pids = {call.args[0] for call in killpg.call_args_list}
            self.assertEqual(killed_pids, {111, 222})

    def test_idempotent_second_call_kills_nothing_new(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.spawned(token, _FakeProc())
        with mock.patch('libs.runstate.os.killpg') as killpg:
            self.state.cancel('first')
            killpg.reset_mock()
            self.state.cancel('second')
            killpg.assert_not_called()

    def test_process_lookup_error_is_swallowed(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.spawned(token, _FakeProc())
        with mock.patch('libs.runstate.os.killpg', side_effect=ProcessLookupError):
            errors = self.state.cancel('test')
        self.assertEqual(errors, [])

    def test_other_exception_is_collected_not_raised(self):
        token, _, _ = self.state.start(1, 1000)
        self.state.spawned(token, _FakeProc())
        with mock.patch('libs.runstate.os.killpg', side_effect=PermissionError('nope')):
            errors = self.state.cancel('test')
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], token)


class TestMarkStuck(unittest.TestCase):
    def test_cancels_the_run_and_kills_other_live_children(self):
        state = RunState()
        stuck_token, _, _ = state.start(1, 1000)
        state.spawned(stuck_token, _FakeProc(pid=111))
        other_token, _, _ = state.start(1, 1000)
        state.spawned(other_token, _FakeProc(pid=222))

        with mock.patch('libs.runstate.os.killpg') as killpg:
            state.mark_stuck(stuck_token, 'zombie would not die')
            # Only the OTHER live child is killed by mark_stuck itself —
            # the stuck one is already presumed unkillable/unconfirmed.
            self.assertEqual(killpg.call_count, 1)
            self.assertEqual(killpg.call_args.args[0], 222)

        self.assertTrue(state.is_cancelled())
        self.assertIn(stuck_token, state.snapshot_unresolved())
        self.assertEqual(state.snapshot_unresolved()[stuck_token], 'stuck')

    def test_stuck_token_never_removed_by_complete_once(self):
        state = RunState()
        token, _, _ = state.start(1, 1000)
        state.mark_stuck(token, 'detail')
        with self.assertRaises(AssertionError):
            state.complete_once(token, lambda r: None, 'result')
        self.assertIn(token, state.snapshot_unresolved())


class TestRunEndReporting(unittest.TestCase):
    def test_has_unresolved_false_on_empty_state(self):
        self.assertFalse(RunState().has_unresolved())

    def test_has_incomplete_reason_false_until_cancelled(self):
        state = RunState()
        self.assertFalse(state.has_incomplete_reason())
        state.cancel('why')
        self.assertTrue(state.has_incomplete_reason())
        self.assertEqual(state.incomplete_reason(), 'why')


if __name__ == '__main__':
    unittest.main()
