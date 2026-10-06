"""Config-file validation, the top-level main() boundary,
and the internal per-file child script."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
import signal
import threading
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from libs import converter, textmesh
import batch_repair
import batch_repair_child
from libs.runconfig import RunConfig


class TestBatchRepairCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Libraries are checked for real at startup, not under test (owner,
        # 2026-10-05): bypass the check.
        self.stack.enter_context(mock.patch.object(
            batch_repair.dependencies, 'check_all', return_value=[]))

    def invoke(self, source=None, output=None, max_faces='0', extra=None, argv=()):
        """Write a TOML config (values are raw TOML literals), point
        `CONFIG_PATH` at it, and call `main` with `argv` (normally none)."""
        lines = [f'input = {json.dumps(str(source or self.source))}',
                 f'output = {json.dumps(str(output or self.output))}',
                 f'max_faces = {max_faces}']
        lines += [f'{key} = {value}' for key, value in (extra or {}).items()]
        config = self.root / 'batch_repair.toml'
        config.write_text('\n'.join(lines) + '\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             mock.patch.object(batch_repair, 'CONFIG_PATH', str(config)):
            code = batch_repair.main(list(argv))
        return code, stdout.getvalue() + stderr.getvalue()

    def test_configuration_rejected_before_intake(self):
        regular_file = self.root / 'file'
        regular_file.touch()
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        cases = [
            (self.source, self.source / 'out', '0', 'overlap'),
            (self.source, self.root, '0', 'overlap'),
            (self.source, self.source, '0', 'overlap'),
            (self.source, alias / 'out', '0', 'overlap'),
            (self.root / 'missing', self.output, '0', 'input must exist'),
            (regular_file, self.output, '0', 'input must exist'),
            (self.source, regular_file, '0', 'output must be a directory'),
            (self.source, self.output, '-1', 'max_faces'),
            (self.source, self.output, '"abc"', 'max_faces'),
            (self.source, self.output, '1.5', 'max_faces'),
            (self.source, self.output, 'true', 'max_faces'),
            (self.root / 'missing', regular_file, '"bad"', 'max_faces'),
        ]
        with mock.patch.object(converter, 'prepare') as prepare:
            for source, output, max_faces, message in cases:
                with self.subTest(source=source, output=output, max_faces=max_faces):
                    code, text = self.invoke(source, output, max_faces)
                    self.assertEqual(code, 2, text)
                    self.assertIn(message, text)
            prepare.assert_not_called()

    def test_output_overlapping_the_decimation_cache_is_rejected(self):
        """The cache is `<input>.decimated`, beside the input. (An output
        that is an ancestor of it is an ancestor of the input too, so the
        input check already rejects that case.)"""
        cache = self.root / 'input.decimated'
        with mock.patch.object(converter, 'prepare') as prepare:
            for output in (cache, cache / 'out'):
                with self.subTest(output=output):
                    code, text = self.invoke(self.source, output)
                    self.assertEqual(code, 2, text)
                    self.assertIn('decimation cache', text)
            prepare.assert_not_called()

    def test_a_cache_folder_symlinked_into_the_input_is_rejected(self):
        inside = self.source / 'cache'
        inside.mkdir()
        (self.root / 'input.decimated').symlink_to(inside, target_is_directory=True)
        with mock.patch.object(converter, 'prepare') as prepare:
            code, text = self.invoke()
        self.assertEqual(code, 2, text)
        self.assertIn('resolves inside the input folder', text)
        prepare.assert_not_called()

    def test_a_looping_cache_symlink_is_a_configuration_error(self):
        cache = self.root / 'input.decimated'
        cache.symlink_to(cache)
        with mock.patch.object(converter, 'prepare') as prepare:
            code, text = self.invoke()
        self.assertEqual(code, 2, text)
        self.assertIn('cannot resolve', text)
        prepare.assert_not_called()

    def test_any_argument_is_rejected_before_anything_is_written(self):
        with mock.patch.object(converter, 'prepare') as prepare:
            for argv in (['--help'], ['--input', str(self.source)], ['x']):
                with self.subTest(argv=argv):
                    code, text = self.invoke(argv=argv)
                    self.assertEqual(code, 2, text)
                    self.assertIn('takes no arguments', text)
            prepare.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_config_names_the_example(self):
        with mock.patch.object(batch_repair, 'CONFIG_PATH', str(self.root / 'absent.toml')), \
             redirect_stderr(io.StringIO()) as stderr:
            code = batch_repair.main([])
        self.assertEqual(code, 2)
        self.assertIn('batch_repair.example.toml', stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_bad_config_value_writes_nothing(self):
        for extra in ({'workers': '-1'}, {'wrokers': '2'}, {'skip_clean': '1'},
                      {'log_file': '"a\\u0000b"'}, {'reap_deadline': str(10 ** 400)},
                      {'per_file_timeout': 'inf'}, {'memory_budget_fraction': '1.5'}):
            with self.subTest(extra=extra):
                code, text = self.invoke(extra=extra)
                self.assertEqual(code, 2, text)
                self.assertIn(next(iter(extra)), text)
                self.assertFalse(self.output.exists())

    def test_malformed_toml_is_a_clean_error(self):
        config = self.root / 'batch_repair.toml'
        config.write_text('input = "unterminated\n')
        with mock.patch.object(batch_repair, 'CONFIG_PATH', str(config)), \
             redirect_stderr(io.StringIO()) as stderr:
            code = batch_repair.main([])
        self.assertEqual(code, 2)
        self.assertIn('not valid TOML', stderr.getvalue())

    def test_memory_budget_bytes_overrides_fraction(self):
        with mock.patch.object(converter, 'prepare', return_value=converter.Summary()):
            code, text = self.invoke(extra={'memory_budget_bytes': '1000'})
        self.assertEqual(code, 0, text)

    def test_invalid_binary_is_intake_failure(self):
        import struct
        path = self.source / 'bad.stl'
        path.write_bytes(b'\0' * 80 + struct.pack('<I', 4))
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        for fragment in ('intake_failure=1', 'conversion_failed=0',
                         'total=1, jobs=1', str(path)):
            self.assertIn(fragment, text)
        # `--output` now always exists after `_run` starts: `progress.log`
        # is written there before intake even begins (spec 5d), so a run
        # with zero dispatched jobs still creates the directory. Nothing
        # published is still true — the directory holds only progress.log.
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ['progress.log'])

    def test_conversion_failure_is_a_diagnostic_without_a_marker(self):
        """A source that converts to nothing (no faces) is retried on every
        run: no export, no marker, one model-log note per attempt."""
        (self.source / 'body.obj').write_text('v 0 0 0\n')
        for _ in range(2):
            code, text = self.invoke()
            self.assertEqual(code, 1, text)
            for fragment in ('intake_failure=1', 'conversion_failed=1',
                             'converted=0', 'malformed=0', 'total=1, jobs=1',
                             'no triangles'):
                self.assertIn(fragment, text)
        exported = self.source / 'stl-exported'
        self.assertEqual([p for p in exported.rglob('*') if p.is_file()]
                         if exported.exists() else [], [])
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ['body.log', 'progress.log'])
        log = (self.output / 'body.log').read_text()
        self.assertEqual(log.count(f'conversion: {self.source / "body.obj"}'), 2)
        self.assertEqual(log.count('conversion failed: ConversionError'), 2)

    _PENTAGON = 'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv -1 0.5 0\nf 1 2 3 4 5\n'

    def test_a_malformed_source_gets_a_failed_marker_once(self):
        """Owner, 2026-10-06: a full source copy as the FAILED marker; the
        next run skips the source."""
        source = self.source / 'body.obj'
        source.write_text(self._PENTAGON)
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        marker = self.output / 'body.failed.stl'
        self.assertEqual(marker.read_bytes(), source.read_bytes())
        for fragment in ('malformed=1', 'intake_failure=1', '5 vertices',
                         'FAILED marker'):
            self.assertIn(fragment, text)
        self.assertIn('conversion failed: Malformed',
                      (self.output / 'body.log').read_text())

        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn('skipped=1', text)
        self.assertIn('total=0, jobs=0', text)
        self.assertEqual(marker.read_bytes(), source.read_bytes())

    def test_a_malformed_source_colliding_with_another_job_writes_nothing(self):
        """`foo.obj` and `foo.stl` both publish to `foo*.stl`: both rejected,
        whatever the walk order, and no marker is written."""
        import struct
        (self.source / 'body.obj').write_text(self._PENTAGON)
        tri = struct.pack('<12f', 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0) + b'\0\0'
        (self.source / 'body.stl').write_bytes(
            b'\0' * 80 + struct.pack('<I', 1) + tri)
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('intake_failure=2', text)
        self.assertEqual(text.count('destination path collision'), 2)
        self.assertIn('no FAILED marker written', text)
        self.assertFalse((self.output / 'body.failed.stl').exists())
        self.assertFalse((self.output / 'body.stl').exists())

    def test_a_marker_that_cannot_be_written_is_reported(self):
        (self.source / 'body.obj').write_text(self._PENTAGON)
        with mock.patch.object(batch_repair.shutil, 'copy2',
                               side_effect=OSError('disk full')):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('writing its FAILED marker failed: disk full', text)
        self.assertFalse((self.output / 'body.failed.stl').exists())

    def test_an_unwritable_model_log_does_not_stop_the_conversion(self):
        source = self.source / 'body.obj'
        source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        blocked = self.root / 'blocked'
        blocked.write_text('a file where the log folder would be')
        export = self.root / 'export.stl'
        with redirect_stderr(io.StringIO()) as out:
            batch_repair._convert_logged(
                str(source), str(export),
                model_destination=str(blocked / 'body.stl'), run_id='r')
        self.assertIn('cannot write model log', out.getvalue())
        self.assertEqual(export.stat().st_size, 84 + 50)

    def test_an_obj_is_converted_and_repaired(self):
        (self.source / 'body.obj').write_text(
            'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0.5 0.5 1\n'
            'f 1 4 3 2\nf 1 2 5\nf 2 3 5\nf 3 4 5\nf 4 1 5\n')
        code, text = self.invoke(extra={'skip_clean': 'true'})
        self.assertEqual(code, 0, text)
        self.assertIn('converted=1', text)
        self.assertTrue((self.source / 'stl-exported' / 'body.stl').exists())
        self.assertTrue((self.output / 'body.stl').exists())

    def test_companion_copy_failure(self):
        (self.source / 'notes.txt').write_text('notes')
        with mock.patch.object(converter.shutil, 'copy2', side_effect=OSError('denied')):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('copy_failed=1', text)
        self.assertIn('ignored=0', text)
        self.assertIn('total=0, jobs=0', text)

    def test_intake_exception_is_incomplete(self):
        def prepare(source, destination, emit, **kwargs):
            raise RuntimeError('walk failed')

        with mock.patch.object(converter, 'prepare', side_effect=prepare):
            code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn('Run incomplete: intake', text)
        self.assertIn('walk failed', text)
        self.assertNotIn('Intake:', text)
        self.assertNotIn('Run complete.', text)

    def test_keyboard_interrupt_at_intake_boundary(self):
        """`_run` catches `KeyboardInterrupt` from `converter.prepare` itself
        (spec section 4d) and returns 1 directly, WITHOUT reaching
        `_preflight`/dispatch. `main()`'s own outer `except KeyboardInterrupt`
        (for an interrupt anywhere else) is a separate, still-present path,
        exercised by `test_direct_script_help_and_required_arguments`-style
        subprocess tests rather than here.
        """
        with mock.patch.object(converter, 'prepare', side_effect=KeyboardInterrupt):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('Intake interrupted', text)
        self.assertNotIn('Run complete.', text)
        self.assertNotIn('Intake done:', text,
                         "an interrupted intake must not reach dispatch")

    def test_direct_script_rejects_arguments(self):
        # Never run the real script WITHOUT arguments here: it would read the
        # user's own batch_repair.toml and start a real batch.
        script = Path(batch_repair.__file__).resolve()
        result = subprocess.run([sys.executable, str(script), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('takes no arguments', result.stderr)

    def test_child_requires_destination_and_result_file(self):
        script = Path(batch_repair_child.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), '--one-file', 'x.stl', '--max-faces', '0'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--destination', result.stderr)

    def test_spawn_child_passes_the_runner_pid(self):
        """`_spawn_child` tells the child this runner's PID, so the child
        dies with it (`proctree.exit_with_parent`)."""
        from libs.mesh_io import Kind, Mesh
        mesh = Mesh(str(self.source / 'a.stl'), str(self.output / 'a.stl'),
                   Kind.BINARY_STL, 4, True)
        captured = {}
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            captured['argv'] = argv
            return real_popen([sys.executable, '-c', 'pass'])

        with mock.patch('subprocess.Popen', spy):
            proc = batch_repair._spawn_child(
                sys.executable, str(Path(batch_repair.__file__)), mesh, 0, '/tmp/result.json')
            proc.wait()
        argv = captured['argv']
        self.assertEqual(argv[argv.index('--parent-pid') + 1], str(os.getpid()))

    def test_child_whose_runner_is_gone_exits_before_any_work(self):
        """The real child script, told a runner PID that is not its parent
        (as when the runner died before the child armed), exits at once
        with `RUNNER_GONE_EXIT` and writes neither result nor output."""
        from libs import proctree
        script = Path(batch_repair_child.__file__).resolve()
        source = self.root / 'a.stl'
        source.write_bytes(b'')
        destination = self.root / 'b.stl'
        result_file = self.root / 'r.json'
        not_my_parent = os.getppid()        # the child's parent is this process
        result = subprocess.run(
            [sys.executable, str(script), '--one-file', str(source),
             '--destination', str(destination), '--result-file', str(result_file),
             '--max-faces', '0', '--parent-pid', str(not_my_parent)],
            cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, proctree.RUNNER_GONE_EXIT, result.stderr)
        self.assertIn('already gone', result.stderr)
        self.assertFalse(result_file.exists())
        self.assertFalse(destination.exists())

    def test_child_arms_only_when_given_a_runner_pid(self):
        """A direct diagnostic run (no `--parent-pid`) arms nothing; a
        runner-managed one arms with the given PID; a bad PID is refused."""
        from libs import proctree
        base = ['--one-file', 'a.stl', '--destination', 'b.stl',
                '--result-file', 'r.json', '--max-faces', '0']
        for extra, expected in (([], []), (['--parent-pid', '4242'], [mock.call(4242)])):
            with self.subTest(extra=extra), \
                    mock.patch.object(proctree, 'exit_with_parent') as arm, \
                    mock.patch.object(batch_repair_child, 'run_one_file', return_value=0):
                batch_repair_child.main(base + extra)
                self.assertEqual(arm.call_args_list, expected)
        for bad in ('0', '-1', 'x'):
            with self.subTest(bad=bad), redirect_stderr(io.StringIO()), \
                    mock.patch.object(proctree, 'exit_with_parent') as arm, \
                    self.assertRaises(SystemExit):
                batch_repair_child.main(base + ['--parent-pid', bad])
            arm.assert_not_called()


class TestCleanGateFlags(unittest.TestCase):
    """`skip_clean`: forwarded to the child argv, and through the child's
    `run_one_file` to `processor.process`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _tetra_file(self):
        from libs import mesh_io
        from libs.mesh_io import Geometry, Kind, Mesh
        import numpy as np
        source = self.root / 'body.stl'
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64))
        mesh_io.write(Mesh(str(source), str(source), Kind.BINARY_STL, 4, True, geometry=geometry))
        return source

    def _spawn_argv(self, **flags):
        from libs.mesh_io import Kind, Mesh
        mesh = Mesh(str(self.root / 'a.stl'), str(self.root / 'out' / 'a.stl'),
                   Kind.BINARY_STL, 4, True)
        captured = {}
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            captured['argv'] = argv
            return real_popen([sys.executable, '-c', 'pass'])

        with mock.patch('subprocess.Popen', spy):
            batch_repair._spawn_child(sys.executable, 'script.py', mesh, 0,
                                      '/tmp/result.json', **flags).wait()
        return captured['argv']

    def test_spawn_child_adds_gate_flag_only_when_set(self):
        self.assertNotIn('--skip-clean', self._spawn_argv())
        self.assertIn('--skip-clean', self._spawn_argv(skip_clean=True))

    def test_run_one_file_forwards_gate_flag_to_process(self):
        from libs import processor
        source = self._tetra_file()
        captured = []
        real_process = processor.process

        def spy(mesh, max_faces, **kwargs):
            captured.append(kwargs['skip_clean'])
            return real_process(mesh, max_faces, **kwargs)

        class FakeArgs:
            pass

        for flag in (False, True):
            args = FakeArgs()
            args.one_file = str(source)
            args.destination = str(self.root / 'out' / f'{flag}.stl')
            args.max_faces = 0
            args.log_file = None
            args.result_file = str(self.root / 'result.json')
            args.skip_clean = flag
            args.reconstruct_budget_bytes = 10 ** 10
            args.min_shell_faces = 100
            args.mode, args.cache_path, args.load_from = 'repair', None, None
            with mock.patch.object(processor, 'process', spy):
                batch_repair_child.run_one_file(args)
        self.assertEqual(captured, [False, True])

    def test_real_child_script_with_gate_logs_and_publishes(self):
        """Real child script and argparse: the flags parse, and the gate's verdict lands in the step log."""
        source = self._tetra_file()
        dest = self.root / 'out' / 'body.stl'
        log = self.root / 'steps.log'
        script = Path(batch_repair_child.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), '--one-file', str(source),
             '--destination', str(dest), '--max-faces', '0',
             '--result-file', str(self.root / 'result.json'),
             '--log-file', str(log), '--skip-clean'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = log.read_text()
        self.assertIn('clean_gate', text)
        self.assertIn('clean: steps skipped', text)
        self.assertTrue(dest.exists())


class TestReconstructBudgetPlumbing(unittest.TestCase):
    """`reconstruct_memory_budget_gb` -> child argv -> processor -> repairer
    -> every part's StepConfig."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_spawn_child_passes_the_budget(self):
        from libs.mesh_io import Kind, Mesh
        mesh = Mesh(str(self.root / 'a.stl'), str(self.root / 'out' / 'a.stl'), Kind.BINARY_STL, 4, True)
        captured = {}
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            captured['argv'] = argv
            return real_popen([sys.executable, '-c', 'pass'])

        with mock.patch('subprocess.Popen', spy):
            batch_repair._spawn_child(sys.executable, 'c.py', mesh, 0, '/tmp/r.json',
                                      reconstruct_budget_bytes=2_500_000_000).wait()
        argv = captured['argv']
        self.assertEqual(argv[argv.index('--reconstruct-budget-bytes') + 1], '2500000000')

    def test_child_parses_the_budget_and_hands_it_to_processor(self):
        from libs import processor
        seen = {}

        def fake_run(args):
            seen['budget'] = args.reconstruct_budget_bytes
            return 0

        with mock.patch.object(batch_repair_child, 'run_one_file', fake_run):
            batch_repair_child.main(['--one-file', 'a.stl', '--destination', 'b.stl',
                                     '--result-file', 'r.json', '--max-faces', '0',
                                     '--reconstruct-budget-bytes', '7000000000'])
        self.assertEqual(seen['budget'], 7_000_000_000)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            batch_repair_child.main(['--one-file', 'a', '--destination', 'b', '--result-file', 'r',
                                     '--max-faces', '0', '--reconstruct-budget-bytes', '0'])

    def test_budget_reaches_every_part_step_config(self):
        from libs import repairer
        from libs.mesh_io import Geometry, Kind, Mesh
        import numpy as np
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
                          [10, 10, 10], [11, 10, 10], [10, 11, 10], [10, 10, 11]], np.float64)
        faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3],
                          [4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]], np.int64)
        m = Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 8, True, None, Geometry(verts, faces))
        seen = []

        def record(part, config=None):
            seen.append(config.reconstruct_memory_budget_bytes)
            return True, part, 'recorded'

        repairer.repair(m, min_shell_faces=0, part_steps=(('record', record),),
                        reconstruct_budget_bytes=3_000_000_000)
        self.assertEqual(seen, [3_000_000_000, 3_000_000_000])

    def test_runner_converts_the_configured_gb_for_each_child(self):
        from libs.mesh_io import Kind, Mesh
        mesh = Mesh(str(self.root / 'a.stl'), str(self.root / 'out' / 'a.stl'), Kind.BINARY_STL, 4, True)
        Path(mesh.path).write_bytes(b'x')
        config = RunConfig(input=str(self.root), output=str(self.root / 'out'), max_faces=0,
                           workers=1, memory_budget_bytes=10 ** 15, reconstruct_memory_budget_gb=2.5)
        captured = []

        def refuse(*a, **kw):
            captured.append(kw)
            raise OSError('not launched in this test')

        with mock.patch.object(converter, 'prepare',
                               side_effect=lambda *a, **k: (a[2](mesh), converter.Summary())[1]), \
             mock.patch.object(batch_repair, '_spawn_child', side_effect=refuse), \
             mock.patch('builtins.print'):
            batch_repair._run(config)
        self.assertEqual(captured[0]['reconstruct_budget_bytes'], 2_500_000_000)


class TestMinShellFacesPlumbing(unittest.TestCase):
    """`min_shell_faces` -> child argv -> child parse -> `_process_one_file`
    -> `processor.process`. What the floor does to a mesh is tested in
    test_processor.TestShellFloor."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _mesh(self):
        from libs.mesh_io import Kind, Mesh
        return Mesh(str(self.root / 'a.stl'), str(self.root / 'out' / 'a.stl'), Kind.BINARY_STL, 4, True)

    def test_spawn_child_passes_the_floor(self):
        captured = {}
        real_popen = subprocess.Popen

        def spy(argv, **kwargs):
            captured['argv'] = argv
            return real_popen([sys.executable, '-c', 'pass'])

        with mock.patch('subprocess.Popen', spy):
            batch_repair._spawn_child(sys.executable, 'c.py', self._mesh(), 0, '/tmp/r.json',
                                      min_shell_faces=250).wait()
        argv = captured['argv']
        self.assertEqual(argv[argv.index('--min-shell-faces') + 1], '250')

    def test_child_parses_the_floor(self):
        from libs import splitter
        base = ['--one-file', 'a.stl', '--destination', 'b.stl',
                '--result-file', 'r.json', '--max-faces', '0']
        for extra, expected in (([], splitter.MIN_SHELL_FACES),
                                (['--min-shell-faces', '0'], 0),
                                (['--min-shell-faces', '250'], 250)):
            seen = {}

            def fake_run(args):
                seen['floor'] = args.min_shell_faces
                return 0

            with self.subTest(extra=extra), mock.patch.object(batch_repair_child, 'run_one_file', fake_run):
                batch_repair_child.main(base + extra)
                self.assertEqual(seen['floor'], expected)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            batch_repair_child.main(base + ['--min-shell-faces', '-1'])

    def test_child_hands_the_floor_to_processor(self):
        from libs import mesh_io, processor
        from libs.mesh_io import Geometry, Kind, Mesh
        import numpy as np
        source = self.root / 'a.stl'
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64))
        mesh_io.write(Mesh(str(source), str(source), Kind.BINARY_STL, 4, True, geometry=geometry))
        dest = self.root / 'out' / 'a.stl'
        captured = {}

        def fake_process(mesh, max_faces, **kwargs):
            captured['floor'] = kwargs.get('min_shell_faces')
            from libs.indicators import Indicator
            return processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(source), str(dest))),
                None, 'clean')

        class FakeArgs:
            pass

        args = FakeArgs()
        args.one_file, args.destination = str(source), str(dest)
        args.max_faces, args.log_file = 0, None
        args.result_file = str(self.root / 'result.json')
        args.skip_clean = False
        args.reconstruct_budget_bytes = 10 ** 10
        args.min_shell_faces = 250
        args.mode, args.cache_path, args.load_from = 'repair', None, None
        with mock.patch.object(processor, 'process', fake_process):
            batch_repair_child.run_one_file(args)
        self.assertEqual(captured['floor'], 250)

    def test_runner_passes_the_configured_floor(self):
        mesh = self._mesh()
        Path(mesh.path).write_bytes(b'x')
        config = RunConfig(input=str(self.root), output=str(self.root / 'out'), max_faces=0,
                           workers=1, memory_budget_bytes=10 ** 15, min_shell_faces=250)
        captured = []

        def refuse(*a, **kw):
            captured.append(kw)
            raise OSError('not launched in this test')

        with mock.patch.object(converter, 'prepare',
                               side_effect=lambda *a, **k: (a[2](mesh), converter.Summary())[1]), \
             mock.patch.object(batch_repair, '_spawn_child', side_effect=refuse), \
             mock.patch('builtins.print'):
            batch_repair._run(config)
        self.assertEqual(captured[0]['min_shell_faces'], 250)


class TestModelLog(unittest.TestCase):
    """The per-model raw log: the parent's attempt header, the child's
    stdout/stderr pointed at it, step separators, and crash output."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def runner(self):
        config = RunConfig(input=str(self.root), output=str(self.root / 'out'),
                           max_faces=0, workers=1, memory_budget_bytes=10 ** 9)
        return batch_repair._Runner(config, sys.executable, 'unused.py', {}, run_id='R1')

    def mesh(self, name='body.stl'):
        from libs.mesh_io import Kind, Mesh
        return Mesh(str(self.root / name), str(self.root / 'out' / 'sub' / name),
                    Kind.BINARY_STL, 4, True)

    def test_each_attempt_appends_its_own_header(self):
        runner, mesh = self.runner(), self.mesh()
        for _ in range(2):
            handle = runner._start_model_log(runner._model_log_path(mesh), mesh)
            self.assertEqual(handle.name, str(self.root / 'out' / 'sub' / 'body.log'))
            handle.close()
        text = (self.root / 'out' / 'sub' / 'body.log').read_text()
        self.assertEqual(text.count('run R1  repair: '), 2)

    def test_an_unwritable_log_warns_and_returns_none(self):
        (self.root / 'out').mkdir()
        (self.root / 'out' / 'sub').write_text('a file where the folder should be')
        runner = self.runner()
        with mock.patch.object(runner, '_log') as log:
            mesh = self.mesh()
            self.assertIsNone(runner._start_model_log(runner._model_log_path(mesh), mesh))
        self.assertIn('cannot write model log', log.call_args.args[0])

    def test_header_written_but_log_cannot_be_opened_still_repairs(self):
        """The failure Codex found: a header write that succeeds followed by
        an open that fails must only warn, then launch once with DEVNULL —
        not be reported as a launch failure."""
        runner, mesh = self.runner(), self.mesh()
        real_open = open

        def open_fails_for_append(path, mode='r', *args, **kwargs):
            if mode == 'ab':
                raise PermissionError('denied')
            return real_open(path, mode, *args, **kwargs)

        with mock.patch('builtins.open', open_fails_for_append), \
             mock.patch.object(runner, '_log') as log:
            self.assertIsNone(runner._start_model_log(runner._model_log_path(mesh), mesh))
        self.assertIn('cannot write model log', log.call_args.args[0])
        # ...and the repair still launches, exactly once, without a log.
        spawned = []

        def spawn_and_stop(*args, **kwargs):
            spawned.append(kwargs)
            raise OSError('launch stubbed out here')

        token, _, _ = runner.run_state.start(0, 10 ** 9)
        with mock.patch.object(runner, '_start_model_log', return_value=None), \
             mock.patch.object(batch_repair, '_spawn_child', side_effect=spawn_and_stop):
            runner._run_one(token, mesh)
        self.assertEqual(len(spawned), 1)
        self.assertIsNone(spawned[0]['output_log'])

    def test_model_log_never_lands_on_a_run_log(self):
        from libs import modellog
        out = self.root / 'out'
        config = RunConfig(input=str(self.root), output=str(out), max_faces=0,
                           log_file=str(out / 'batch.log'), workers=1,
                           memory_budget_bytes=10 ** 9)
        reserved = batch_repair._reserved_logs(config)
        self.assertEqual(modellog.path_for(str(out / 'progress.stl'), reserved),
                         str(out / 'progress.model.log'))
        self.assertEqual(modellog.path_for(str(out / 'batch.stl'), reserved),
                         str(out / 'batch.model.log'))
        self.assertEqual(modellog.path_for(str(out / 'foot.stl'), reserved),
                         str(out / 'foot.log'))
        custom = batch_repair._reserved_logs(
            RunConfig(input=str(self.root), output=str(out), max_faces=0,
                      log_file=str(out / 'logs' / 'steps.log')))
        self.assertEqual(modellog.path_for(str(out / 'logs' / 'steps.stl'), custom),
                         str(out / 'logs' / 'steps.model.log'))
        self.assertEqual(modellog.path_for(str(out / 'logs' / 'progress.obj'), custom),
                         str(out / 'logs' / 'progress.model.log'))
        # The fallback name itself reserved by a custom log_file.
        tricky = batch_repair._reserved_logs(
            RunConfig(input=str(self.root), output=str(out), max_faces=0,
                      log_file=str(out / 'progress.model.log')))
        self.assertEqual(modellog.path_for(str(out / 'progress.stl'), tricky),
                         str(out / 'progress.model.2.log'))

    def test_child_output_and_crash_traceback_land_in_the_log(self):
        """A child that prints, then dies natively: both its output and the
        faulthandler traceback must be in the log, written by the child
        itself into the file the parent handed it."""
        log = self.root / 'body.log'
        script = self.root / 'crashing_child.py'
        script.write_text(
            'import os, sys\n'
            f'sys.path.insert(0, {str(Path(batch_repair_child.__file__).resolve().parent)!r})\n'
            'import batch_repair_child, batch_repair\n'
            'def die(*a, **k):\n'
            '    os.write(2, b"about to crash\\n")\n'
            '    os.abort()\n'
            'batch_repair._process_one_file = die\n'
            'batch_repair_child.main(sys.argv[1:])\n')
        mesh = self.mesh()
        with open(log, 'ab') as handle:
            proc = batch_repair._spawn_child(sys.executable, str(script), mesh, 0,
                                             str(self.root / 'result.json'), output_log=handle)
        proc.wait(timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        text = log.read_text()
        self.assertIn('about to crash', text)
        self.assertIn('Fatal Python error', text)       # faulthandler
        self.assertFalse((self.root / 'result.json').exists())

    def test_real_child_brackets_each_step_with_separators(self):
        from libs import mesh_io
        from libs.mesh_io import Geometry, Kind, Mesh
        import numpy as np
        source = self.root / 'body.stl'
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64))
        mesh_io.write(Mesh(str(source), str(source), Kind.BINARY_STL, 4, True, geometry=geometry))
        log = self.root / 'out' / 'body.log'
        log.parent.mkdir()
        mesh = Mesh(str(source), str(self.root / 'out' / 'body.stl'), Kind.BINARY_STL, 4, True)
        with open(log, 'ab') as handle:
            proc = batch_repair._spawn_child(
                sys.executable, str(Path(batch_repair_child.__file__).resolve()), mesh, 0,
                str(self.root / 'result.json'), skip_clean=True, output_log=handle)
        proc.wait(timeout=300)
        self.assertEqual(proc.returncode, 0)
        lines = [l for l in log.read_text().splitlines() if l.startswith('---- ')]
        events = [(l.split()[2], l.split()[3]) for l in lines]
        starts = [step for event, step in events if event == 'start']
        self.assertIn('decimate', starts)
        # Steps nest (a scan runs inside decimate), so ends come in completion
        # order; what must hold is a matching end after every start.
        self.assertEqual(sorted(starts),
                         sorted(step for event, step in events if event == 'end'))
        for step in set(starts):
            self.assertLess(events.index(('start', step)), events.index(('end', step)))


class TestInterruptedIntakeIntegration(unittest.TestCase):
    """A real conversion, genuinely interrupted by a real `SIGINT`-delivered
    `KeyboardInterrupt` during `converter.prepare` — confirms `_run` returns
    early, nonzero, without reaching dispatch, and that the conversion's
    staged export is gone (checked on disk, not by a mock assertion).
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(
            batch_repair.dependencies, 'check_all', return_value=[]))

    def test_keyboard_interrupt_during_a_conversion_leaves_no_export(self):
        (self.source / 'body.obj').write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        config = RunConfig(input=str(self.source), output=str(self.output),
                           max_faces=0, workers=1, per_file_timeout=30.0,
                           reap_deadline=5.0, memory_budget_bytes=10 ** 15)
        writing = threading.Event()
        real_add = textmesh._Writer.add

        def slow_add(writer, corners):
            # The staged export exists now; hold the conversion here until
            # the interrupt arrives (sleep returns early on a signal).
            real_add(writer, corners)
            writing.set()
            time.sleep(30)

        def interrupt_once_writing():
            self.assertTrue(writing.wait(10), "the conversion never started writing")
            os.kill(os.getpid(), signal.SIGINT)

        interrupter = threading.Thread(target=interrupt_once_writing, daemon=True)
        started = time.monotonic()
        with mock.patch.object(textmesh._Writer, 'add', slow_add), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as out:
            interrupter.start()
            try:
                code = batch_repair._run(config)
            finally:
                interrupter.join(timeout=15)

        self.assertEqual(code, 1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn('Intake interrupted', out.getvalue())
        self.assertNotIn('Intake done:', out.getvalue())
        exported = self.source / 'stl-exported'
        self.assertEqual(list(exported.iterdir()) if exported.exists() else [], [],
                         "an interrupted conversion left a file behind")


if __name__ == '__main__':
    unittest.main()
