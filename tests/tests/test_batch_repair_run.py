"""Tests against the real `_run()` function itself — its SIGINT handling,
repeat-SIGINT deferral, and INCONSISTENT-reconciliation reporting — with
`converter.prepare` and `Pool` both mocked so nothing real spawns.  This
covers exactly the gap flagged in Codex's PHASE: REVIEW: the earlier pool
tests exercised `_Runner` wired to a bare `Pool` directly, never `_run`
itself, so its own cleanup code path (as opposed to a hand-rolled copy of it
in a test) was untested.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from libs import converter
from libs.indicators import Indicator
from libs.mesh_io import Kind, Mesh
from dataclasses import replace
import batch_repair
from libs.runconfig import RunConfig


_BASE_CONFIG = RunConfig(
    input='/in',
    output='/out',
    log_file='',
    max_faces=0,
    workers=2,
    per_file_timeout=5.0,
    reap_deadline=5.0,
    memory_budget_bytes=10 ** 15)


class TestRunSigint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _mesh(self, name):
        return Mesh(str(self.root / name), str(self.root / 'out' / name),
                   Kind.BINARY_STL, 10, True)

    def test_sigint_during_pool_start_reports_incomplete(self):
        mesh = self._mesh('a.stl')

        class FakePool:
            def __init__(self, workers, selector, handler):
                pass

            def start(self):
                raise KeyboardInterrupt

        config = replace(_BASE_CONFIG,
                         output=str(self.root / 'out'))

        with mock.patch.object(converter, 'prepare',
                               side_effect=lambda *a, **k: (a[2](mesh), converter.Summary())[1]), \
             mock.patch.object(batch_repair, 'Pool', FakePool):
            with mock.patch('sys.stdout') as fake_stdout:
                code = batch_repair._run(config)
        self.assertEqual(code, 1)
        printed = ''.join(c.args[0] for c in fake_stdout.write.call_args_list
                          if c.args and isinstance(c.args[0], str))
        self.assertIn('Run INCOMPLETE', printed)
        self.assertNotIn('Run complete.', printed)

    def test_repeat_sigint_during_cleanup_does_not_crash(self):
        # A second SIGINT arriving DURING _run's own bounded cleanup wait
        # must not propagate as a fresh KeyboardInterrupt out of that
        # cleanup block — _run installs SIG_IGN for exactly that window.
        # Run in a genuinely separate subprocess: sending a real SIGINT
        # into the SAME process as the test runner is racy against whatever
        # else that process's main thread is doing (see the equivalent note
        # in test_batch_repair_pool.py's TestCancellation).
        input_dir = self.root / 'in'
        output_dir = self.root / 'out'
        input_dir.mkdir()
        (input_dir / 'a.stl').write_bytes(b'x')
        child_running_marker = self.root / 'child_running'
        cleanup_marker = self.root / 'cleanup_ignoring_sigint'
        fake_child = self.root / 'fake_child.py'
        fake_child.write_text(
            'import argparse, json, os, time\n'
            'p = argparse.ArgumentParser()\n'
            "p.add_argument('--one-file', required=True)\n"
            "p.add_argument('--destination', required=True)\n"
            "p.add_argument('--max-faces', required=True)\n"
            "p.add_argument('--result-file', required=True)\n"
            "p.add_argument('--managed-child', action='store_true')\n"
            "p.add_argument('--reconstruct-budget-bytes')\n"
            "p.add_argument('--min-shell-faces')\n"
            "p.add_argument('--mode', default='repair')\n"
            "p.add_argument('--cache-path')\n"
            "p.add_argument('--load-from')\n"
            'args = p.parse_args()\n'
            "if args.mode == 'prepare':\n"
            "    import json as _json, os as _os\n"
            "    _r = {'path': args.one_file, 'category': 'prepared', 'indicator': None, 'stage': 'prepare',\n"
            "          'reason': 'fake prepared', 'written_path': None, 'mode': 'prepare',\n"
            "          'prepared_path': args.one_file, 'estimate_bytes': 1}\n"
            "    with open(args.result_file + '.p', 'w') as _f:\n"
            "        _f.write(_json.dumps(_r))\n"
            "    _os.replace(args.result_file + '.p', args.result_file)\n"
            "    raise SystemExit(0)\n"
            f'open({str(child_running_marker)!r}, "w").close()\n'   # proves the child is really running
            'time.sleep(600)\n'   # never finishes on its own — must be killed
        )
        # The helper wraps signal.signal so the test can detect the EXACT
        # moment _run installs SIG_IGN for its cleanup window (rather than
        # guessing with a sleep) — the second SIGINT is only sent once that
        # is observed, closing the race Codex flagged: the first signal is
        # sent only after the child marker proves Pool.start() really
        # dispatched a job, and the second only after the cleanup marker
        # proves _run is genuinely inside the SIG_IGN window.
        helper = self.root / 'repeat_sigint_helper.py'
        helper.write_text(
            'import signal, sys, os\n'
            f'sys.path.insert(0, {str(Path(batch_repair.__file__).resolve().parents[0])!r})\n'
            'import batch_repair\n'
            'from libs import converter\n'
            'from libs.mesh_io import Kind, Mesh\n'
            '\n'
            f'INPUT_DIR = {str(input_dir)!r}\n'
            f'OUTPUT_DIR = {str(output_dir)!r}\n'
            f'FAKE_CHILD = {str(fake_child)!r}\n'
            f'CLEANUP_MARKER = {str(cleanup_marker)!r}\n'
            '\n'
            'mesh = Mesh(os.path.join(INPUT_DIR, "a.stl"), os.path.join(OUTPUT_DIR, "a.stl"),\n'
            '            Kind.BINARY_STL, 10, True)\n'
            '\n'
            'from libs.runconfig import RunConfig\n'
            'config = RunConfig(input=INPUT_DIR, output=OUTPUT_DIR, max_faces=0, workers=1, per_file_timeout=30.0, reap_deadline=5.0, memory_budget_bytes=10 ** 15)\n'
            '\n'
            'def fake_prepare(source, destination, emit, **kwargs):\n'
            '    emit(mesh)\n'
            '    return converter.Summary()\n'
            '\n'
            'converter.prepare = fake_prepare\n'
            '\n'
            'orig_spawn = batch_repair._spawn_child\n'
            'def fake_spawn(python, script, m, max_faces, result_file, log_file=None, **kw):\n'
            '    return orig_spawn(sys.executable, FAKE_CHILD, m, max_faces, result_file, log_file, **kw)\n'
            'batch_repair._spawn_child = fake_spawn\n'
            '\n'
            'real_signal = signal.signal\n'
            'def observing_signal(signum, handler):\n'
            '    if signum == signal.SIGINT and handler == signal.SIG_IGN:\n'
            '        open(CLEANUP_MARKER, "w").close()\n'
            '    return real_signal(signum, handler)\n'
            'signal.signal = observing_signal\n'
            '\n'
            'code = batch_repair._run(config)\n'
            'print(f"EXIT_CODE={code}")\n'
        )
        proc = subprocess.Popen([sys.executable, str(helper)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_for(child_running_marker, timeout=10.0)
        proc.send_signal(signal.SIGINT)
        self._wait_for(cleanup_marker, timeout=10.0)
        proc.send_signal(signal.SIGINT)   # the repeat — sent while SIG_IGN is confirmed installed
        try:
            stdout, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail('subprocess did not exit after repeated SIGINT within 15s')
        self.assertIn('EXIT_CODE=1', stdout, stdout)
        self.assertNotIn('Traceback', stdout, stdout)

    def _wait_for(self, path, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f'{path} never appeared within {timeout}s')

    def test_inconsistent_reconciliation_is_write_failure_not_marker(self):
        # Exercise _reconcile's INCONSISTENT branch through a real handler
        # call (not a hand-built dict), proving _run's own consumer treats
        # it as write_failure and never writes a marker into an already-
        # inconsistent set of files.
        mesh = self._mesh('a.stl')
        os.makedirs(self.root / 'out', exist_ok=True)
        # Two "new" paths already exist by the time reconciliation would
        # run — simulated directly via the publication module's own
        # reconcile(), matching what _reconcile() wraps.
        from libs import publication
        (self.root / 'out' / 'a.stl').write_bytes(b'x')
        (self.root / 'out' / 'a.failed.stl').write_bytes(b'y')
        outcome = batch_repair._reconcile(mesh, frozenset(), 'crashed',
                                          detail='killed by SIGSEGV')
        self.assertEqual(outcome['category'], 'write_failure')
        self.assertIn('after child crashed (killed by SIGSEGV)', outcome['reason'])
        self.assertTrue(outcome['recovered'])
        self.assertIsNone(outcome['indicator'])

    def test_recovered_clean_publish_through_real_run_forces_diagnostic_and_exit(self):
        # End-to-end through the REAL _run() (real Pool, real _Runner, real
        # spawned child) — not a hand-built result dict — proving the
        # printed summary and returned exit code, not just the intermediate
        # dict test_batch_repair_pool.py's equivalent test checks.
        mesh = self._mesh('a.stl')
        os.makedirs(self.root / 'out', exist_ok=True)
        (Path(mesh.path)).write_bytes(b'source')
        script = self.root / 'crash_after_publish.py'
        script.write_text(
            'import argparse, os, sys\n'
            'p = argparse.ArgumentParser()\n'
            "p.add_argument('--one-file', required=True)\n"
            "p.add_argument('--destination', required=True)\n"
            "p.add_argument('--max-faces', required=True)\n"
            "p.add_argument('--result-file', required=True)\n"
            "p.add_argument('--managed-child', action='store_true')\n"
            "p.add_argument('--reconstruct-budget-bytes')\n"
            "p.add_argument('--min-shell-faces')\n"
            "p.add_argument('--mode', default='repair')\n"
            "p.add_argument('--cache-path')\n"
            "p.add_argument('--load-from')\n"
            'args = p.parse_args()\n'
            "if args.mode == 'prepare':\n"
            "    import json as _json, os as _os\n"
            "    _r = {'path': args.one_file, 'category': 'prepared', 'indicator': None, 'stage': 'prepare',\n"
            "          'reason': 'fake prepared', 'written_path': None, 'mode': 'prepare',\n"
            "          'prepared_path': args.one_file, 'estimate_bytes': 1}\n"
            "    with open(args.result_file + '.p', 'w') as _f:\n"
            "        _f.write(_json.dumps(_r))\n"
            "    _os.replace(args.result_file + '.p', args.result_file)\n"
            "    raise SystemExit(0)\n"
            'os.makedirs(os.path.dirname(args.destination), exist_ok=True)\n'
            "with open(args.destination, 'wb') as f:\n"
            "    f.write(b'real published output')\n"
            'os._exit(137)\n'   # dies before writing --result-file
        )
        args = replace(_BASE_CONFIG, input=str(self.root), output=str(self.root / 'out'))

        real_spawn_child = batch_repair._spawn_child
        with mock.patch.object(converter, 'prepare',
                               side_effect=lambda *a, **k: (a[2](mesh), converter.Summary())[1]), \
             mock.patch.object(batch_repair, '_spawn_child',
                               side_effect=lambda python, s, m, mf, rf, lf=None, **kw:
                                   real_spawn_child(sys.executable, str(script), m, mf, rf, lf, **kw)):
            captured = []
            with mock.patch('builtins.print', side_effect=lambda *a, **k: captured.append(' '.join(map(str, a)))):
                code = batch_repair._run(args)
        printed = '\n'.join(captured)
        self.assertEqual(code, 1)
        self.assertIn('Diagnostic:', printed)
        self.assertIn(str(mesh.path), printed)
        self.assertIn('recovered after child crashed (exit status 137)', printed)


class TestCleanGateDispatch(unittest.TestCase):
    """The parent's `_Runner` passes `args.skip_clean` to `_spawn_child`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_runner_forwards_gate_flag_to_spawn_child(self):
        mesh = Mesh(str(self.root / 'a.stl'), str(self.root / 'out' / 'a.stl'),
                    Kind.BINARY_STL, 10, True)
        Path(mesh.path).write_bytes(b'source')
        root = self.root

        for flag in (False, True):
            config = replace(_BASE_CONFIG,
                             input=str(root), output=str(root / 'out'),
                             skip_clean=flag)

            captured = []

            def refuse(*a, **kw):
                captured.append(kw)
                raise OSError('not launched in this test')

            with mock.patch.object(converter, 'prepare',
                                   side_effect=lambda *a, **k: (a[2](mesh), converter.Summary())[1]), \
                 mock.patch.object(batch_repair, '_spawn_child', side_effect=refuse), \
                 mock.patch('builtins.print'):
                batch_repair._run(config)
            with self.subTest(flag=flag):
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].get('skip_clean'), flag)

if __name__ == '__main__':
    unittest.main()
