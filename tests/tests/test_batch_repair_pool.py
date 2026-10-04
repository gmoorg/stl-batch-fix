"""Pool-wiring, admission, and cancellation, against a small fake --one-file
child script — no CGAL/real repair involved, so this layer stays fast.

The fake script mimics the real --one-file contract (writes a JSON
ChildResult to --result-file) but its behavior is driven entirely by the
source filename, so each test can construct exactly the child behavior it
needs (slow, crashing, hanging past its timeout, etc.) without touching the
real pipeline.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from libs import childresult, mesh_io
from libs.indicators import Indicator
from libs.mesh_io import Kind, Mesh
from dataclasses import replace
import batch_repair
from libs.runconfig import RunConfig
from batch_repair import _Runner

FAKE_CHILD = textwrap.dedent('''
    import argparse, json, os, sys, time

    parser = argparse.ArgumentParser()
    parser.add_argument('--one-file', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--max-faces', required=True)
    parser.add_argument('--result-file', required=True)
    parser.add_argument('--managed-child', action='store_true')
    parser.add_argument('--reconstruct-budget-bytes')
    args = parser.parse_args()

    name = os.path.basename(args.one_file)

    if name.startswith('hang_'):
        time.sleep(600)
    if name.startswith('crash_'):
        os._exit(137)          # simulate SIGKILL-style abnormal exit, no result written
    if name.startswith('slow_'):
        time.sleep(0.3)

    result = {
        'path': args.one_file,
        'category': 'published',
        'indicator': 'PROCESS',
        'stage': 'process',
        'reason': 'fake ok',
        'written_path': args.destination,
    }
    if name.startswith('fail_'):
        result['category'] = 'process_failure'
        result['indicator'] = None
        result['reason'] = 'fake failure'

    staged = args.result_file + '.tmp'
    with open(staged, 'w') as f:
        json.dump(result, f)
    os.replace(staged, args.result_file)
''')


class _PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'fake_child.py'
        self.script.write_text(FAKE_CHILD)

    def mesh(self, name, triangles=10):
        return Mesh(str(self.root / 'in' / name), str(self.root / 'out' / name),
                   Kind.BINARY_STL, triangles, True)

    def make_args(self, **overrides):
        return replace(RunConfig(input='/in', output='/out', max_faces=0, workers=4,
                                 per_file_timeout=5.0, reap_deadline=5.0,
                                 memory_budget_bytes=10 ** 15), **overrides)

    def make_runner(self, meshes, **arg_overrides):
        args = self.make_args(**arg_overrides)
        baselines = {m.path: frozenset() for m in meshes}
        runner = _Runner(args, sys.executable, str(self.script), baselines)
        runner.queue.extend(meshes)
        return runner


class TestBasicDispatch(_PoolTestCase):
    def test_single_clean_job(self):
        runner = self.make_runner([self.mesh('a.stl')])
        from libs.pool import Pool
        Pool(2, runner.selector, runner.handler).start()
        self.assertEqual(len(runner.results), 1)
        self.assertEqual(runner.results[0]['category'], 'published')
        self.assertEqual(runner.results[0]['indicator'], 'PROCESS')

    def test_failure_category_reported(self):
        runner = self.make_runner([self.mesh('fail_a.stl')])
        from libs.pool import Pool
        Pool(2, runner.selector, runner.handler).start()
        self.assertEqual(runner.results[0]['category'], 'process_failure')

    def test_crash_with_no_result_publishes_fallback_marker_with_real_source(self):
        mesh = self.mesh('crash_a.stl')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(mesh.path).write_bytes(b'source bytes for the fallback marker')
        runner = self.make_runner([mesh])
        from libs.pool import Pool
        Pool(2, runner.selector, runner.handler).start()
        result = runner.results[0]
        self.assertEqual(result['category'], 'published')
        self.assertEqual(result['indicator'], 'FAILED')
        self.assertTrue(result['recovered'])
        marker = str(self.root / 'out' / 'crash_a.failed.stl')
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(Path(marker).read_bytes(), Path(mesh.path).read_bytes())

    def test_timeout_publishes_fallback_marker_with_timeout_indicator(self):
        mesh = self.mesh('hang_a.stl')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(mesh.path).write_bytes(b'source bytes for the timeout marker')
        runner = self.make_runner([mesh], per_file_timeout=0.2)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        result = runner.results[0]
        self.assertEqual(result['category'], 'published')
        self.assertEqual(result['indicator'], 'TIMED_OUT')
        self.assertTrue(result['recovered'])
        marker = str(self.root / 'out' / 'hang_a.timeout.stl')
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(Path(marker).read_bytes(), Path(mesh.path).read_bytes())

    def test_valid_result_trusted_despite_nonzero_exit_code(self):
        # Regression test for the removed exit-code gate: a child that
        # writes a fully valid result JSON and THEN exits nonzero (e.g. a
        # crash during its own shutdown, after the atomic result write
        # already committed) must have that result trusted, not discarded
        # in favor of crash reconciliation. Without the fix, the earlier
        # `if cause == 'exited' and proc.returncode == 0:` guard would skip
        # validation entirely here and produce a DIFFERENT (crash/
        # reconciliation) outcome instead of this exact published/PROCESS
        # one — so this test fails loudly if that gate is reintroduced.
        script = self.root / 'valid_result_then_nonzero_exit.py'
        script.write_text(textwrap.dedent('''
            import argparse, json, os, sys
            parser = argparse.ArgumentParser()
            parser.add_argument('--one-file', required=True)
            parser.add_argument('--destination', required=True)
            parser.add_argument('--max-faces', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--managed-child', action='store_true')
            parser.add_argument('--reconstruct-budget-bytes')
            args = parser.parse_args()
            os.makedirs(os.path.dirname(args.destination), exist_ok=True)
            with open(args.destination, 'wb') as f:
                f.write(b'real published output')
            result = {
                'path': args.one_file, 'category': 'published', 'indicator': 'PROCESS',
                'stage': 'process', 'reason': 'genuinely clean', 'written_path': args.destination,
            }
            staged = args.result_file + '.tmp'
            with open(staged, 'w') as f:
                json.dump(result, f)
            os.replace(staged, args.result_file)   # result fully committed here
            sys.exit(3)                            # THEN exits nonzero
        '''))
        mesh = self.mesh('a.stl')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(mesh.path).write_bytes(b'source')
        args = self.make_args()
        baselines = {mesh.path: frozenset()}
        runner = _Runner(args, sys.executable, str(script), baselines)
        runner.queue.append(mesh)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        result = runner.results[0]
        self.assertEqual(result['category'], 'published')
        self.assertEqual(result['indicator'], 'PROCESS')
        self.assertEqual(result['reason'], 'genuinely clean')
        self.assertFalse(result.get('recovered', False),
                         'a trusted result must not be marked recovered')

    def test_recovered_clean_publish_still_reports_diagnostic(self):
        # A fake child that writes its real output but crashes before
        # reporting — the parent must recover via the filesystem AND still
        # flag it as a diagnostic-worthy abnormal exit, even though the
        # recovered outcome is a clean PROCESS.
        script = self.root / 'crash_after_publish.py'
        script.write_text(textwrap.dedent('''
            import argparse, os, sys
            parser = argparse.ArgumentParser()
            parser.add_argument('--one-file', required=True)
            parser.add_argument('--destination', required=True)
            parser.add_argument('--max-faces', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--managed-child', action='store_true')
            parser.add_argument('--reconstruct-budget-bytes')
            args = parser.parse_args()
            os.makedirs(os.path.dirname(args.destination), exist_ok=True)
            with open(args.destination, 'wb') as f:
                f.write(b'real published output')
            os._exit(137)   # dies before ever writing --result-file
        '''))
        mesh = self.mesh('a.stl')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(mesh.path).write_bytes(b'source')
        args = self.make_args()
        baselines = {mesh.path: frozenset()}
        runner = _Runner(args, sys.executable, str(script), baselines)
        runner.queue.append(mesh)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        result = runner.results[0]
        self.assertEqual(result['category'], 'published')
        self.assertEqual(result['indicator'], 'PROCESS')
        self.assertTrue(result['recovered'],
                        'a recovered clean publish must still be flagged recovered')


class TestOrdering(_PoolTestCase):
    def test_dispatched_ascending_by_triangle_count(self):
        meshes = [self.mesh('big.stl', triangles=1000),
                 self.mesh('small.stl', triangles=1)]
        args = self.make_args(workers=1)   # serial: dispatch order == completion order
        baselines = {m.path: frozenset() for m in meshes}
        runner = _Runner(args, sys.executable, str(self.script), baselines)
        # Sort exactly as _run does before extending the queue.
        meshes.sort(key=lambda m: (m.triangles is None, m.triangles or 0))
        runner.queue.extend(meshes)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        order = [os.path.basename(r['mesh'].path) for r in runner.results]
        self.assertEqual(order, ['small.stl', 'big.stl'])


class TestParallelism(_PoolTestCase):
    def test_slow_jobs_actually_overlap(self):
        meshes = [self.mesh(f'slow_{i}.stl') for i in range(4)]
        runner = self.make_runner(meshes, workers=4)
        from libs.pool import Pool
        started = time.monotonic()
        Pool(4, runner.selector, runner.handler).start()
        elapsed = time.monotonic() - started
        # Each fake child sleeps 0.3s; serial would take >=1.2s, parallel
        # with 4 workers should finish well under that.
        self.assertLess(elapsed, 1.0, f'took {elapsed}s — jobs did not overlap')
        self.assertEqual(len(runner.results), 4)


class TestAdmission(_PoolTestCase):
    def test_shedding_forces_serial_execution_under_tight_budget(self):
        meshes = [self.mesh(f'slow_{i}.stl', triangles=1000) for i in range(3)]
        # Budget only fits one job's estimate at a time.
        from libs.runstate import BUDGET_BYTES_PER_TRIANGLE, ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED
        one_job = 1000 * BUDGET_BYTES_PER_TRIANGLE * ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED
        runner = self.make_runner(meshes, workers=4, memory_budget_bytes=one_job + 1)
        from libs.pool import Pool
        started = time.monotonic()
        Pool(4, runner.selector, runner.handler).start()
        elapsed = time.monotonic() - started
        # With admission correctly shedding down to ~1 concurrent job, three
        # 0.3s jobs take close to 0.9s serial rather than ~0.3s parallel.
        self.assertGreater(elapsed, 0.7, f'admission did not throttle concurrency: {elapsed}s')
        self.assertEqual(len(runner.results), 3)

    def test_alone_override_admits_oversized_job(self):
        huge = self.mesh('a.stl', triangles=10 ** 12)
        runner = self.make_runner([huge], memory_budget_bytes=1)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        self.assertEqual(len(runner.results), 1)
        self.assertEqual(runner.results[0]['category'], 'published')


class TestCancellation(_PoolTestCase):
    """SIGINT tests run `_run()` in a genuinely separate subprocess.

    `os.kill(os.getpid(), SIGINT)` inside the SAME process as the test
    runner is racy against whatever the main thread happens to be doing at
    delivery time — Python delivers a signal on the next bytecode boundary
    of the MAIN thread specifically, not necessarily the thread that is
    "logically" being tested, so under load the signal can land while an
    unrelated later test is executing instead. A real subprocess makes the
    signal's target unambiguous.
    """

    def test_sigint_kills_in_flight_children_and_marks_incomplete(self):
        # Runs the real _run() in a genuinely separate subprocess so the
        # SIGINT this test sends cannot land on any other test's main
        # thread. converter.prepare and the child script are monkeypatched
        # INSIDE that subprocess (fake meshes, fake fast-hanging child),
        # keeping this fast while still exercising real subprocess spawn,
        # real killpg, and _run's own cleanup path end to end.
        input_dir = self.root / 'in'
        output_dir = self.root / 'out'
        input_dir.mkdir()
        (input_dir / 'hang_a.stl').write_bytes(b'x')
        (input_dir / 'hang_b.stl').write_bytes(b'y')
        # A dedicated fake child (not the shared FAKE_CHILD, which has no
        # readiness signal) that writes a marker the moment it is truly
        # running, so this test can wait for real evidence both jobs are
        # dispatched before sending SIGINT — a fixed sleep proves nothing
        # about whether Pool.start() actually got that far in time.
        marker_a = self.root / 'running_a'
        marker_b = self.root / 'running_b'
        dedicated_child = self.root / 'hang_with_marker.py'
        dedicated_child.write_text(
            'import argparse, os, time\n'
            'p = argparse.ArgumentParser()\n'
            "p.add_argument('--one-file', required=True)\n"
            "p.add_argument('--destination', required=True)\n"
            "p.add_argument('--max-faces', required=True)\n"
            "p.add_argument('--result-file', required=True)\n"
            "p.add_argument('--managed-child', action='store_true')\n"
            "p.add_argument('--reconstruct-budget-bytes')\n"
            'args = p.parse_args()\n'
            f'markers = {{"hang_a.stl": {str(marker_a)!r}, "hang_b.stl": {str(marker_b)!r}}}\n'
            'open(markers[os.path.basename(args.one_file)], "w").close()\n'
            'time.sleep(600)\n'
        )
        helper = self.root / 'sigint_helper.py'
        helper.write_text(
            'import sys, os\n'
            f'sys.path.insert(0, {str(Path(batch_repair.__file__).resolve().parents[0])!r})\n'
            'import batch_repair\n'
            'from libs import converter\n'
            'from libs.mesh_io import Kind, Mesh\n'
            '\n'
            f'INPUT_DIR = {str(input_dir)!r}\n'
            f'OUTPUT_DIR = {str(output_dir)!r}\n'
            f'FAKE_CHILD = {str(dedicated_child)!r}\n'
            '\n'
            'meshes = [Mesh(os.path.join(INPUT_DIR, n), os.path.join(OUTPUT_DIR, n),\n'
            '               Kind.BINARY_STL, 10, True) for n in ("hang_a.stl", "hang_b.stl")]\n'
            '\n'
            'from libs.runconfig import RunConfig\n'
            'config = RunConfig(input=INPUT_DIR, output=OUTPUT_DIR, max_faces=0, workers=2, per_file_timeout=5.0, reap_deadline=5.0, memory_budget_bytes=10 ** 15)\n'
            '\n'
            'def fake_prepare(source, destination, emit, **kwargs):\n'
            '    for m in meshes:\n'
            '        emit(m)\n'
            '    return converter.Summary()\n'
            '\n'
            'converter.prepare = fake_prepare\n'
            '\n'
            'orig_spawn = batch_repair._spawn_child\n'
            'def fake_spawn(python, script, mesh, max_faces, result_file, log_file=None, **kw):\n'
            '    return orig_spawn(sys.executable, FAKE_CHILD, mesh, max_faces, result_file, log_file, **kw)\n'
            'batch_repair._spawn_child = fake_spawn\n'
            '\n'
            'code = batch_repair._run(config)\n'
            'print(f"EXIT_CODE={code}")\n'
        )
        proc = subprocess.Popen([sys.executable, str(helper)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_for(marker_a, timeout=10.0)
        self._wait_for(marker_b, timeout=10.0)
        proc.send_signal(signal.SIGINT)
        try:
            stdout, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail('subprocess did not exit after SIGINT within 15s')
        self.assertIn('Run INCOMPLETE', stdout, stdout)
        self.assertIn('EXIT_CODE=1', stdout, stdout)

    def _wait_for(self, path, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f'{path} never appeared within {timeout}s')

    def test_queue_untouched_by_cancellation_is_not_marker_written(self):
        # Three hanging jobs, one worker: worker 0 takes job 0, jobs 1/2
        # never leave the queue before cancellation. Uses the same
        # _Runner + bare Pool shape as the other pool tests, but drives
        # cancellation through RunState.cancel() directly rather than a
        # real signal — no os.kill() involved, so no cross-test race.
        meshes = [self.mesh(f'hang_{i}.stl') for i in range(3)]
        runner = self.make_runner(meshes, workers=1)
        from libs.pool import Pool

        def cancel_soon():
            time.sleep(0.3)
            runner.run_state.cancel('test-triggered cancellation')

        threading.Thread(target=cancel_soon, daemon=True).start()
        Pool(1, runner.selector, runner.handler).start()

        self.assertGreaterEqual(len(runner.queue), 1)
        for path in [str(self.root / 'out' / f'hang_{i}.stl') for i in range(3)]:
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(path.replace('.stl', '.failed.stl')))


class TestLaunchFailure(_PoolTestCase):
    def test_missing_interpreter_is_process_failure(self):
        args = self.make_args()
        baselines = {}
        mesh = self.mesh('a.stl')
        baselines[mesh.path] = frozenset()
        runner = _Runner(args, '/no/such/interpreter', str(self.script), baselines)
        runner.queue.append(mesh)
        from libs.pool import Pool
        Pool(1, runner.selector, runner.handler).start()
        self.assertEqual(len(runner.results), 1)
        self.assertEqual(runner.results[0]['category'], 'process_failure')
        self.assertIn('launch child', runner.results[0]['reason'])


if __name__ == '__main__':
    unittest.main()
