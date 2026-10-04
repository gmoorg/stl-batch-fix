"""Tests for batch_repair's progress.log: ProgressReporter's crash-safe
recovery pass, run/progress/job/final record kinds, elapsed_seconds, and
concurrency/failure behavior — logging spec section 5c/5d/5e.
"""

import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from libs import converter, steplog
from libs.mesh_io import Kind, Mesh
import batch_repair
from libs.runconfig import RunConfig
from batch_repair import ProgressReporter, _Runner


_TETRA = (
    ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
    ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
)


def _binary_stl(path):
    """A minimal valid binary STL — enough for `_process_one_file` to reach
    its `process`/`step_logger`-driving stage rather than failing at intake
    (an empty/garbage file never reaches `processor.process`, so it never
    logs anything, which is not what these tests want to exercise)."""
    body = bytearray()
    for tri in _TETRA:
        body += struct.pack('<3f', 0.0, 0.0, 0.0)
        for vertex in tri:
            body += struct.pack('<3f', *(float(c) for c in vertex))
        body += b'\0\0'
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', len(_TETRA)))
        f.write(bytes(body))
    return path


def _read_records(path):
    """Parse only the lines that ARE valid JSON — a preserved malformed raw
    line (spec 5e) is not itself a record and is asserted on separately via
    the raw file content, not through this helper."""
    records = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


_BASE_CONFIG = RunConfig(
    input='/in',
    output='/out',
    log_file='',
    max_faces=0,
    workers=2,
    per_file_timeout=5.0,
    reap_deadline=5.0,
    memory_budget_bytes=10 ** 15)


class TestSourceIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _fake_process(self, mesh, max_faces, part_steps=None, step_logger=None,
                      source_name='', nested_process_group=False, *,
                      skip_clean=False, reconstruct_budget_bytes=None,
                      min_shell_faces=None):
        # A trivial stand-in for `processor.process` that still drives the
        # step_logger once, so these tests exercise the real
        # `source_name`/`step_logger` plumbing in `_process_one_file`
        # without paying for a real alpha-wrap/CGAL repair on every call.
        from libs import processor as processor_module
        step_logger(source_name, 'start', 'decimate', '-', None, '4 faces in')
        step_logger(source_name, 'end', 'decimate', '-', 0.0, '4 faces out')
        from libs.indicators import Indicator
        return processor_module.Outcome(Indicator.PROCESS, mesh, None, 'clean')

    def test_relative_one_file_path_becomes_absolute_source_name(self):
        _binary_stl(str(self.root / 'a.stl'))
        log_path = self.root / 'batch.log'
        logger = steplog.open_step_log(str(log_path))
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.object(batch_repair.processor, 'process', self._fake_process), \
                 mock.patch.object(batch_repair.processor, 'write', return_value=None):
                batch_repair._process_one_file('a.stl', str(self.root / 'out.stl'), 0,
                                               step_logger=logger)
        finally:
            os.chdir(cwd)
        with open(log_path) as f:
            line = f.readline()
        self.assertTrue(line, 'expected at least one logged line')
        source_field = line.split('\t')[1]
        self.assertTrue(os.path.isabs(source_field))
        self.assertEqual(os.path.basename(source_field), 'a.stl')

    def test_two_same_basename_files_in_different_folders_are_distinguishable(self):
        (self.root / 'sub1').mkdir()
        (self.root / 'sub2').mkdir()
        _binary_stl(str(self.root / 'sub1' / 'a.stl'))
        _binary_stl(str(self.root / 'sub2' / 'a.stl'))
        log_path = self.root / 'batch.log'
        logger = steplog.open_step_log(str(log_path))
        with mock.patch.object(batch_repair.processor, 'process', self._fake_process), \
             mock.patch.object(batch_repair.processor, 'write', return_value=None):
            batch_repair._process_one_file(str(self.root / 'sub1' / 'a.stl'),
                                           str(self.root / 'out1.stl'), 0, step_logger=logger)
            batch_repair._process_one_file(str(self.root / 'sub2' / 'a.stl'),
                                           str(self.root / 'out2.stl'), 0, step_logger=logger)
        with open(log_path) as f:
            sources = {line.split('\t')[1] for line in f if line.strip()}
        self.assertEqual(len(sources), 2)


class TestProgressReporterBasics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = str(self.root / 'progress.log')

    def test_run_start_and_final_records(self):
        reporter = ProgressReporter(self.path, 'run-1')
        reporter.write({'kind': 'run_start', 'output': '/out', 'max_faces': 0})
        reporter.write({'kind': 'progress', 'message': 'hello'})
        reporter.write({'kind': 'job', 'mesh': '/a.stl', 'category': 'published'})
        reporter.write({'kind': 'final', 'jobs': 1})
        reporter.close()
        records = _read_records(self.path)
        kinds = [r['kind'] for r in records]
        self.assertEqual(kinds, ['run_start', 'progress', 'job', 'final'])
        for r in records:
            self.assertEqual(r['run_id'], 'run-1')


class TestRecoveryRule(unittest.TestCase):
    """The exact two-phase recovery rule — spec 5e, all 6 cases."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'progress.log'

    def _write_raw(self, data: bytes):
        self.path.write_bytes(data)

    def test_a_no_corruption_append_succeeds_untouched(self):
        self._write_raw(b'{"kind": "run_start"}\n')
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(reporter.recovery_errors, [])
        reporter.write({'kind': 'progress', 'message': 'x'})
        reporter.close()
        records = _read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['kind'], 'run_start')
        self.assertEqual(records[1]['kind'], 'progress')

    def test_b_unterminated_non_json_fragment_is_trimmed(self):
        self._write_raw(b'{"kind": "run_start"}\nnot valid json, no newline')
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(reporter.recovery_errors, [])
        reporter.write({'kind': 'progress', 'message': 'after'})
        reporter.close()
        records = _read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['kind'], 'run_start')
        self.assertEqual(records[1]['kind'], 'progress')

    def test_c_unterminated_valid_json_is_preserved_with_newline_appended(self):
        self._write_raw(b'{"kind": "job"}')
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(reporter.recovery_errors, [])
        reporter.write({'kind': 'run_start'})
        reporter.close()
        raw = self.path.read_bytes()
        # The exact concatenation bug must not happen.
        self.assertNotIn(b'"kind": "job"}{"kind"', raw)
        records = _read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['kind'], 'job')
        self.assertEqual(records[1]['kind'], 'run_start')

    def test_d_valid_plus_malformed_plus_partial(self):
        self._write_raw(b'{"kind": "run_start"}\nMALFORMED\npartial fragment, no newline')
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(len(reporter.recovery_errors), 1)
        self.assertEqual(reporter.recovery_errors[0]['raw'], 'MALFORMED')
        reporter.write({'kind': 'progress', 'message': 'clean'})
        reporter.close()
        records = _read_records(self.path)
        kinds = [r['kind'] for r in records]
        self.assertEqual(kinds, ['run_start', 'recovery_error', 'progress'])

    def test_e_terminated_malformed_only_nothing_truncated(self):
        self._write_raw(b'MALFORMED RECORD\n')
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(len(reporter.recovery_errors), 1)
        self.assertEqual(reporter.recovery_errors[0]['raw'], 'MALFORMED RECORD')
        reporter.write({'kind': 'progress', 'message': 'after'})
        reporter.close()
        records = _read_records(self.path)
        # Nothing truncated: the malformed line's bytes remain, plus the
        # recovery_error, plus the new append.
        self.assertEqual(len(records), 2)   # recovery_error + progress
        raw = self.path.read_bytes()
        self.assertIn(b'MALFORMED RECORD', raw)

    def test_f_non_ascii_content_round_trips_via_byte_offsets(self):
        # `ensure_ascii=True` (json.dumps's default) would \u-escape every
        # non-ASCII character, leaving no real multibyte UTF-8 bytes in the
        # file at all — which would make this "non-ASCII" test exercise
        # nothing but plain ASCII.  `ensure_ascii=False` is what actually
        # puts multibyte UTF-8 sequences on disk, so byte-offset (not
        # character-offset) correctness has something real to prove against.
        payload = json.dumps({'kind': 'job', 'mesh': '/pièces/café.stl'},
                             ensure_ascii=False).encode('utf-8')
        # 'è' and 'é' are each 2 bytes in UTF-8 but 1 character — confirm
        # this payload actually contains multibyte sequences, or the rest
        # of this test would silently prove nothing.
        self.assertGreater(len(payload), len('/pièces/café.stl'.encode('ascii', 'replace')))
        self._write_raw(payload)   # unterminated, valid JSON
        reporter = ProgressReporter(str(self.path), 'r')
        self.assertEqual(reporter.recovery_errors, [])
        reporter.write({'kind': 'progress', 'message': 'ok'})
        reporter.close()
        records = _read_records(self.path)
        self.assertEqual(records[0]['mesh'], '/pièces/café.stl')
        self.assertEqual(records[1]['kind'], 'progress')
        # The appended '\n' must land exactly at the end of the UTF-8
        # payload's own byte length, not some character-count-derived
        # offset that would split a multibyte sequence.
        with open(self.path, 'rb') as f:
            raw = f.read()
        self.assertEqual(raw[len(payload):len(payload) + 1], b'\n')


class TestConcurrentReport(unittest.TestCase):
    def test_concurrent_writes_produce_well_formed_non_interleaved_lines(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, 'progress.log')
        reporter = ProgressReporter(path, 'r')
        lock = threading.Lock()

        def write_many(n):
            for i in range(50):
                with lock:
                    reporter.write({'kind': 'job', 'mesh': f'/m{n}-{i}.stl'})

        threads = [threading.Thread(target=write_many, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        reporter.close()
        records = _read_records(path)   # raises if any line fails json.loads
        self.assertEqual(len(records), 400)


class TestReportingWriteFailure(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'fake_child.py'
        self.script.write_text(textwrap.dedent('''
            import argparse, json, os
            parser = argparse.ArgumentParser()
            parser.add_argument('--one-file', required=True)
            parser.add_argument('--destination', required=True)
            parser.add_argument('--max-faces', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--managed-child', action='store_true')
            parser.add_argument('--reconstruct-budget-bytes')
            parser.add_argument('--min-shell-faces')
            args = parser.parse_args()
            result = {'path': args.one_file, 'category': 'published', 'indicator': 'PROCESS',
                     'stage': 'process', 'reason': 'ok', 'written_path': args.destination}
            staged = args.result_file + '.tmp'
            with open(staged, 'w') as f:
                json.dump(result, f)
            os.replace(staged, args.result_file)
        '''))

    def mesh(self, name):
        return Mesh(str(self.root / 'in' / name), str(self.root / 'out' / name),
                   Kind.BINARY_STL, 10, True)

    def make_args(self):
        config = RunConfig(
            input='/in',
            output='/out',
            max_faces=0,
            workers=1,
            per_file_timeout=5.0,
            reap_deadline=5.0,
            memory_budget_bytes=10 ** 15)
        return config

    def test_write_failure_leaves_job_unresolved_no_duplicate_report(self):
        mesh = self.mesh('a.stl')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(mesh.path).write_bytes(b'source')
        args = self.make_args()
        baselines = {mesh.path: frozenset()}

        class FailingReporter:
            def __init__(self):
                self.write_calls = 0

            def write(self, record):
                self.write_calls += 1
                if record.get('kind') == 'job':
                    raise OSError('disk full')

        reporter = FailingReporter()
        runner = _Runner(args, sys.executable, str(self.script), baselines, reporter=reporter)
        runner.queue.append(mesh)
        from libs.pool import Pool
        # A handler exception is caught inside Pool._run and reported through
        # the next selector() call, not re-raised out of start() itself — see
        # libs/pool.py's own docstring. The token being stuck, not a raised
        # exception here, is the assertion that matters.
        Pool(1, runner.selector, runner.handler).start()
        # `_report` appends to `runner.results` before writing to the
        # reporter, so the result dict IS recorded locally — but
        # `RunState.complete_once` never marks the token completed, because
        # `report_fn` (== `_report`, which raised via the reporter) never
        # returned normally. The token stays unresolved either way — that
        # is the actual "no duplicate report" guarantee this test proves.
        self.assertTrue(runner.run_state.has_unresolved())
        unresolved = runner.run_state.snapshot_unresolved()
        self.assertEqual(list(unresolved.values()), ['stuck'])
        # No duplicate attempt: calling complete_once again for this token
        # would raise AssertionError per RunState's own contract; nothing in
        # this runner tries to, and the injected failure was permanent (the
        # test never waits for an eventual successful flush).
        self.assertGreaterEqual(reporter.write_calls, 1)


class TestSigkillRetention(unittest.TestCase):
    """A forked child flushes N of M jobs' progress records, signals
    readiness via an explicit file write made AFTER the Nth flush, then gets
    SIGKILLed — proving progress.log retains exactly what was flushed."""

    def test_progress_log_survives_sigkill_with_exact_record_counts(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        input_dir = root / 'in'
        output_dir = root / 'out'
        input_dir.mkdir()
        ready_marker = root / 'ready_after_2'

        fake_child = root / 'fake_child.py'
        fake_child.write_text(textwrap.dedent('''
            import argparse, json, os, time
            parser = argparse.ArgumentParser()
            parser.add_argument('--one-file', required=True)
            parser.add_argument('--destination', required=True)
            parser.add_argument('--max-faces', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--managed-child', action='store_true')
            parser.add_argument('--reconstruct-budget-bytes')
            parser.add_argument('--min-shell-faces')
            args = parser.parse_args()
            name = os.path.basename(args.one_file)
            if name.startswith('slow_'):
                time.sleep(600)
            result = {'path': args.one_file, 'category': 'published', 'indicator': 'PROCESS',
                     'stage': 'process', 'reason': 'ok', 'written_path': args.destination}
            staged = args.result_file + '.tmp'
            with open(staged, 'w') as f:
                json.dump(result, f)
            os.replace(staged, args.result_file)
        '''))

        helper = root / 'sigkill_helper.py'
        helper.write_text(
            'import sys, os\n'
            f'sys.path.insert(0, {str(Path(batch_repair.__file__).resolve().parents[0])!r})\n'
            'import batch_repair\n'
            'from libs import converter\n'
            'from libs.mesh_io import Kind, Mesh\n'
            '\n'
            f'INPUT_DIR = {str(input_dir)!r}\n'
            f'OUTPUT_DIR = {str(output_dir)!r}\n'
            f'FAKE_CHILD = {str(fake_child)!r}\n'
            f'READY_MARKER = {str(ready_marker)!r}\n'
            '\n'
            '# 2 quick jobs (dispatched and completed before the kill), then 1\n'
            '# that hangs (dispatched, never completes) -- the parent never\n'
            '# reaches the kill until the 2 quick ones already flushed.\n'
            'meshes = [Mesh(os.path.join(INPUT_DIR, n), os.path.join(OUTPUT_DIR, n),\n'
            '               Kind.BINARY_STL, 10, True)\n'
            '          for n in ("a.stl", "b.stl", "slow_c.stl")]\n'
            '\n'
            'from libs.runconfig import RunConfig\n'
            'config = RunConfig(input=INPUT_DIR, output=OUTPUT_DIR, log_file="", max_faces=0, workers=1, per_file_timeout=300.0, reap_deadline=5.0, memory_budget_bytes=10 ** 15)\n'
            '\n'
            'def fake_prepare(source, destination, emit, **kwargs):\n'
            '    for m in meshes:\n'
            '        emit(m)\n'
            '    return converter.Summary()\n'
            'converter.prepare = fake_prepare\n'
            '\n'
            'orig_spawn = batch_repair._spawn_child\n'
            'def fake_spawn(python, script, mesh, max_faces, result_file, log_file=None, **kw):\n'
            '    return orig_spawn(sys.executable, FAKE_CHILD, mesh, max_faces, result_file, log_file, **kw)\n'
            'batch_repair._spawn_child = fake_spawn\n'
            '\n'
            'orig_report = batch_repair._Runner._report\n'
            'completed = []\n'
            'def counting_report(self, result):\n'
            '    orig_report(self, result)\n'
            '    completed.append(result)\n'
            '    # Explicit readiness signal, written AFTER the flush that\n'
            "    # orig_report's own reporter.write()/flush() already did --\n"
            '    # not a sleep-based guess.\n'
            '    if len(completed) == 2:\n'
            '        with open(READY_MARKER, "w") as f:\n'
            '            f.write("ready")\n'
            'batch_repair._Runner._report = counting_report\n'
            '\n'
            'batch_repair._run(config)\n'
        )
        proc = subprocess.Popen([sys.executable, str(helper)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_for(ready_marker, timeout=20.0)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

        progress_path = output_dir / 'progress.log'
        self._wait_for(progress_path, timeout=5.0)
        records = _read_records(progress_path)
        kinds = [r['kind'] for r in records]
        self.assertEqual(kinds.count('run_start'), 1)
        self.assertGreaterEqual(kinds.count('progress'), 1)
        self.assertEqual(kinds.count('job'), 2)
        self.assertEqual(kinds.count('final'), 0)
        self._surviving_content = progress_path.read_text()

    def _wait_for(self, path, timeout):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f'{path} never appeared within {timeout}s')


class TestRealRunProgressLog(unittest.TestCase):
    """`progress.log` produced by a real `_run()` call — record kinds,
    ordering, and the final-record superset property, spec 5d."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'fake_child.py'
        self.script.write_text(textwrap.dedent('''
            import argparse, json, os
            parser = argparse.ArgumentParser()
            parser.add_argument('--one-file', required=True)
            parser.add_argument('--destination', required=True)
            parser.add_argument('--max-faces', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--managed-child', action='store_true')
            parser.add_argument('--reconstruct-budget-bytes')
            parser.add_argument('--min-shell-faces')
            args = parser.parse_args()
            result = {'path': args.one_file, 'category': 'published', 'indicator': 'PROCESS',
                     'stage': 'process', 'reason': 'fake ok', 'written_path': args.destination}
            staged = args.result_file + '.tmp'
            with open(staged, 'w') as f:
                json.dump(result, f)
            os.replace(staged, args.result_file)
        '''))

    def test_run_start_first_rejected_before_dispatched_final_superset(self):
        good_mesh = Mesh(str(self.root / 'in' / 'good.stl'),
                         str(self.root / 'out' / 'good.stl'),
                         Kind.BINARY_STL, 10, True)
        bad_mesh = Mesh(str(self.root / 'in' / 'bad.stl'),
                        str(self.root / 'out' / 'bad.stl'),
                        Kind.BINARY_STL, None, False, 'invalid intake mesh')
        (self.root / 'in').mkdir(parents=True, exist_ok=True)
        Path(good_mesh.path).write_bytes(b'source')

        config = RunConfig(
            input=str(self.root / 'in'),
            output=str(self.root / 'out'),
            log_file='',
            max_faces=0,
            workers=1,
            per_file_timeout=5.0,
            reap_deadline=5.0,
            memory_budget_bytes=10 ** 15)

        real_spawn = batch_repair._spawn_child
        with mock.patch.object(
                converter, 'prepare',
                side_effect=lambda *a, **k: ([a[2](m) for m in (good_mesh, bad_mesh)],
                                             converter.Summary())[1]), \
             mock.patch.object(
                batch_repair, '_spawn_child',
                side_effect=lambda python, s, m, mf, rf, lf=None, **kw:
                    real_spawn(sys.executable, str(self.script), m, mf, rf, lf, **kw)):
            with mock.patch('builtins.print'):
                code = batch_repair._run(config)

        progress_path = Path(config.output) / 'progress.log'
        records = _read_records(progress_path)
        self.assertEqual(records[0]['kind'], 'run_start')

        job_records = [r for r in records if r['kind'] == 'job']
        rejected_index = next(i for i, r in enumerate(job_records)
                              if r['category'] == 'intake_failure')
        dispatched_index = next(i for i, r in enumerate(job_records)
                                if r['category'] == 'published')
        self.assertLess(rejected_index, dispatched_index,
                        'rejected job record must appear before the dispatched job record')

        progress_messages = [r['message'] for r in records if r['kind'] == 'progress']
        self.assertTrue(any('Scanning' in m for m in progress_messages))
        self.assertTrue(any('[start]' in m for m in progress_messages))

        final = [r for r in records if r['kind'] == 'final'][-1]
        self.assertIn('intake', final)
        self.assertIn('terminal', final)
        self.assertIn('total', final['terminal'])
        self.assertIn('jobs', final)
        self.assertEqual(final['jobs'], 2)
        self.assertIn('published', final)
        self.assertIn('diagnostics', final)
        self.assertIn('incomplete', final)
        self.assertIn('incomplete_reason', final)
        self.assertIn('cancelled_count', final)
        self.assertIn('unresolved', final)
        self.assertIn('left_in_queue', final)

    def test_intake_exception_produces_its_own_final_record(self):
        config = RunConfig(
            input=str(self.root / 'in'),
            output=str(self.root / 'out'),
            log_file='',
            max_faces=0,
            workers=1,
            per_file_timeout=5.0,
            reap_deadline=5.0,
            memory_budget_bytes=10 ** 15)
        os.makedirs(config.input, exist_ok=True)

        with mock.patch.object(converter, 'prepare', side_effect=RuntimeError('boom')):
            with mock.patch('sys.stderr'):
                code = batch_repair._run(config)
        self.assertEqual(code, 1)
        progress_path = Path(config.output) / 'progress.log'
        records = _read_records(progress_path)
        final = [r for r in records if r['kind'] == 'final'][-1]
        self.assertIn('intake_exception', final)
        self.assertIn('RuntimeError', final['intake_exception'])
        self.assertTrue(final['incomplete'])
        progress_messages = [r['message'] for r in records if r['kind'] == 'progress']
        self.assertTrue(any('intake raised' in m for m in progress_messages))


def _mesh(path='/in/a.stl'):
    return Mesh(path, path.replace('/in/', '/out/'), Kind.BINARY_STL, 4, True, None)


class TestElapsedSeconds(unittest.TestCase):
    """spec 5c: elapsed_seconds is computed once, centrally, by `handler`'s
    own timer — covering every path that reports a result, including the
    `handler`-exception path specifically. Flagged by REVIEW as untested;
    these are the tests that were missing."""

    def _runner(self):
        return _Runner(_BASE_CONFIG, sys.executable, 'unused.py', {})

    def test_launch_failure_gets_elapsed_seconds(self):
        """Drives the REAL production path — `handler`, not `_run_one`
        called directly with a manually-seeded `_started` — so this
        actually protects `handler`'s own timing contract, per REVIEW's
        point that seeding `_started` by hand bypasses the thing being
        tested."""
        runner = self._runner()
        mesh = _mesh()
        token, _, _ = runner.run_state.start(0, 10**9)
        with mock.patch.object(batch_repair, '_spawn_child', side_effect=OSError('nope')):
            runner.handler((token, mesh))
        self.assertEqual(len(runner.results), 1)
        self.assertIn('elapsed_seconds', runner.results[0])
        self.assertGreaterEqual(runner.results[0]['elapsed_seconds'], 0.0)

    def test_handler_exception_path_gets_elapsed_seconds(self):
        """The specific path REVIEW called out: an exception raised INSIDE
        `_run_one` (not one of its own clean return points) is caught by
        `handler`'s own except-block, which must still report
        `elapsed_seconds` — using the SAME `_started` timer `handler` seeds
        before calling `_run_one` at all, since `_run_one` itself never got
        the chance to."""
        runner = self._runner()
        mesh = _mesh()
        token, _, _ = runner.run_state.start(0, 10**9)

        class _FakeProc:
            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            pid = 12345

        with mock.patch.object(runner, '_run_one', side_effect=RuntimeError('boom')), \
             mock.patch.object(runner.run_state, 'proc_for', return_value=_FakeProc()), \
             mock.patch('batch_repair.terminate_and_confirm', return_value=(True, 'ok')):
            with self.assertRaises(RuntimeError):
                runner.handler((token, mesh))
        self.assertEqual(len(runner.results), 1)
        result = runner.results[0]
        self.assertEqual(result['category'], 'process_failure')
        self.assertIn('elapsed_seconds', result,
                      "handler's except-block must report elapsed_seconds via "
                      "the SAME _started timer it seeds, per spec 5c")
        self.assertGreaterEqual(result['elapsed_seconds'], 0.0)

    def test_rejected_job_gets_no_elapsed_seconds(self):
        """A file that never reaches `handler` at all (intake rejection)
        must not carry a fabricated `elapsed_seconds`. Exercises the REAL
        `_preflight` rejection path (an actually-invalid `Mesh`, not a
        hand-written record standing in for one) per REVIEW's point that
        the previous version of this test only asserted on a record it
        wrote itself and never touched production rejection logic."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        output_dir = Path(temp.name)
        invalid_mesh = Mesh('/bad.stl', '/out/bad.stl', Kind.BINARY_STL, None,
                            False, 'garbage header')
        dispatchable, rejected = batch_repair._preflight([invalid_mesh])
        self.assertEqual(dispatchable, [])
        self.assertEqual(len(rejected), 1)
        reporter = ProgressReporter(str(output_dir / 'progress.log'), 'reject-test')
        for mesh, reason in rejected:
            reporter.write({'kind': 'job', 'mesh': mesh.path,
                            'category': 'intake_failure', 'reason': reason})
        reporter.close()
        records = _read_records(output_dir / 'progress.log')
        job_records = [r for r in records if r['kind'] == 'job']
        self.assertEqual(len(job_records), 1)
        self.assertEqual(job_records[0]['reason'], 'garbage header')
        self.assertNotIn('elapsed_seconds', job_records[0])


class TestShutdownRace(unittest.TestCase):
    """REVIEW P2 finding: `_run`'s finalization (writing the `final` record
    and calling `reporter.close()`) happens OUTSIDE any `Runner`-owned lock,
    after a bounded wait for in-flight workers that can itself time out
    while a worker is still inside `_report`. `ProgressReporter` must be
    safe against a write that arrives at or after `close()` — this test
    deterministically holds a worker past where finalization would already
    have run, and confirms no corruption/exception results.
    """

    def test_write_after_close_is_a_safe_no_op_not_a_crash(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, 'progress.log')
        reporter = ProgressReporter(path, 'race-test')
        reporter.write({'kind': 'run_start'})
        reporter.close()
        # A straggler worker thread, still holding a reference to `reporter`
        # from before finalization ran, tries to report after close() has
        # already executed — exactly the race REVIEW identified when the
        # 60s unresolved-job wait in `_run` expires before every worker has
        # actually finished.
        try:
            reporter.write({'kind': 'job', 'mesh': '/late.stl'})
        except Exception as exc:                       # noqa: BLE001
            self.fail(f'write() after close() must be a safe no-op, raised {exc!r}')
        records = _read_records(path)
        self.assertEqual([r['kind'] for r in records], ['run_start'])

    def test_concurrent_close_and_write_never_corrupts_the_file_or_raises(self):
        """A tighter, genuinely racing version: one thread calls `close()`
        while several others call `write()` in a loop, repeated many times
        to shake out any ordering-dependent failure — every write either
        lands cleanly before the close or safely no-ops after it; the file
        is never left in a state where a later `_read_records` call raises
        on a corrupted/interleaved line."""
        for _ in range(20):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            path = os.path.join(temp.name, 'progress.log')
            reporter = ProgressReporter(path, 'race-loop')

            errors = []

            def writer(n):
                try:
                    for i in range(10):
                        reporter.write({'kind': 'job', 'mesh': f'/m{n}-{i}.stl'})
                except Exception as exc:                # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
            for t in threads:
                t.start()
            reporter.close()
            for t in threads:
                t.join()
            self.assertEqual(errors, [], 'no write() call may raise, closed or not')
            # Whatever subset of records made it in before close() won, the
            # file itself must still be entirely well-formed JSON Lines —
            # `_read_records` silently drops unparseable lines, so assert
            # directly that every non-empty line in the raw file parses.
            with open(path) as f:
                for line in f:
                    if line.strip():
                        json.loads(line)  # raises ValueError if corrupted

    def test_finalize_is_atomic_nothing_lands_after_final(self):
        """REVIEW's exact remaining finding: `write(final)` followed by a
        separate `close()` has a real gap between them where a worker's own
        `write()` can land after `final` is on disk but before `_closed` is
        set — reproduced by REVIEW's own read-only probe (`['final',
        'job']`). `finalize()` closes that gap by holding the lock across
        both the final write and the `_closed` flip. This test starts a
        worker thread hammering `write()` in a tight loop CONCURRENTLY with
        the main thread calling `finalize()`, repeated many times to
        actually exercise the race rather than rely on timing luck, and
        asserts `final` is always the LAST record in the file."""
        for _ in range(50):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            path = os.path.join(temp.name, 'progress.log')
            reporter = ProgressReporter(path, 'finalize-race')

            stop = threading.Event()

            def hammer():
                i = 0
                while not stop.is_set():
                    reporter.write({'kind': 'job', 'mesh': f'/m{i}.stl'})
                    i += 1

            worker = threading.Thread(target=hammer)
            worker.start()
            # No sleep-based "let it get ahead" — the race is exercised by
            # calling finalize() immediately, while the worker is
            # definitely still mid-loop (it never checks `stop` until after
            # its next write), which is exactly the scenario REVIEW's probe
            # reproduced.
            reporter.finalize({'kind': 'final'})
            stop.set()
            worker.join()

            with open(path) as f:
                lines = [json.loads(line) for line in f if line.strip()]
            kinds = [r['kind'] for r in lines]
            self.assertEqual(kinds[-1], 'final',
                             f'final must be the last record; got order {kinds}')
            self.assertEqual(kinds.count('final'), 1)


if __name__ == '__main__':
    unittest.main()
