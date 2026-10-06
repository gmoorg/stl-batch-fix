"""Config-file validation, the top-level main() boundary,
and the internal per-file child script."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
import signal
import stat
import threading
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from libs import blender, converter
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

    def test_conversion_failure_and_invalid_success(self):
        (self.source / 'body.obj').write_text('v 0 0 0\n')
        for ok in (False, True):
            with self.subTest(ok=ok):
                def convert(source, export, **kwargs):
                    Path(export).parent.mkdir(parents=True, exist_ok=True)
                    Path(export).write_bytes(b'invalid')
                    return ok, export
                with mock.patch.object(batch_repair.blender, 'convert', side_effect=convert):
                    code, text = self.invoke()
                self.assertEqual(code, 1, text)
                self.assertIn('intake_failure=1', text)
                self.assertIn(f'conversion_failed={int(not ok)}', text)
                self.assertIn(f'converted={int(ok)}', text)
                self.assertIn('total=1, jobs=1', text)
                (self.source / 'stl-exported/body.stl').unlink()
        # See test_invalid_binary_is_intake_failure's comment: `--output`
        # now always exists once `_run` starts (progress.log precedes
        # intake), even though nothing was ever published. The model log
        # holds one 'conversion' header per run — appended, not replaced.
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ['body.log', 'progress.log'])
        log = (self.output / 'body.log').read_text()
        self.assertEqual(log.count('conversion: '), 2)
        self.assertIn(str(self.source / 'body.obj'), log)

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
        """`_run` now catches `KeyboardInterrupt` from `converter.prepare`
        itself (spec section 4d) — `cancel()`s the shared intake `Runner`,
        waits for it to go idle, and returns 1 directly, WITHOUT reaching
        `_preflight`/dispatch. `main()`'s own outer `except KeyboardInterrupt`
        (for an interrupt anywhere else) is a separate, still-present path,
        exercised by `test_direct_script_help_and_required_arguments`-style
        subprocess tests rather than here.
        """
        with mock.patch.object(converter, 'prepare', side_effect=KeyboardInterrupt):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('Intake interrupted', text)
        self.assertIn('cleanup confirmed', text)
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

    def test_spawn_child_argv_includes_managed_child_flag(self):
        """`_spawn_child` (the ONLY code that knows the child will live
        inside a proctree-owned group) must always add `--managed-child`."""
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
        self.assertIn('--managed-child', captured['argv'])

    def test_managed_child_flag_present_reaches_processor_as_true(self):
        """`batch_repair_child.run_one_file` invoked with `--managed-child` present (simulating
        a `_spawn_child`-launched batch child) confirms `nested_process_group=True`
        reaches `processor.process`."""
        from libs import mesh_io, processor
        from libs.mesh_io import Geometry, Kind, Mesh
        import numpy as np

        source = self.source / 'body.stl'
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64))
        mesh_io.write(Mesh(str(source), str(source), Kind.BINARY_STL, 4, True, geometry=geometry))
        dest = self.output / 'out.stl'

        class FakeArgs:
            pass

        captured = {}

        def fake_process(mesh, max_faces, **kwargs):
            captured['nested_process_group'] = kwargs.get('nested_process_group')
            from libs.indicators import Indicator
            return processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(source), str(dest))),
                None, 'clean')

        for flag_present, expected in ((True, True), (False, False)):
            with self.subTest(flag_present=flag_present):
                args = FakeArgs()
                args.one_file = str(source)
                args.destination = str(dest)
                args.max_faces = 0
                args.log_file = None
                fd_result, result_path = tempfile.mkstemp()
                import os
                os.close(fd_result)
                args.result_file = result_path
                args.managed_child = flag_present
                args.skip_clean = False
                args.reconstruct_budget_bytes = 10 ** 10
                args.min_shell_faces = 100
                args.mode, args.cache_path, args.load_from = 'repair', None, None
                with mock.patch.object(processor, 'process', fake_process):
                    batch_repair_child.run_one_file(args)
                os.unlink(result_path)
                self.assertEqual(captured['nested_process_group'], expected)


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
            args.managed_child = False
            args.skip_clean = flag
            args.reconstruct_budget_bytes = 10 ** 10
            args.min_shell_faces = 100
            args.mode, args.cache_path, args.load_from = 'repair', None, None
            with mock.patch.object(processor, 'process', spy):
                batch_repair_child.run_one_file(args)
        self.assertEqual(captured, [False, True])

    def test_real_child_script_with_gate_logs_and_publishes(self):
        """Real child script and argparse: the flags parse, `--managed-child`
        is accepted, and the gate's verdict lands in the step log."""
        source = self._tetra_file()
        dest = self.root / 'out' / 'body.stl'
        log = self.root / 'steps.log'
        script = Path(batch_repair_child.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), '--one-file', str(source),
             '--destination', str(dest), '--max-faces', '0',
             '--result-file', str(self.root / 'result.json'),
             '--managed-child', '--log-file', str(log), '--skip-clean'],
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
        args.managed_child, args.skip_clean = False, False
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
            handle = runner._start_model_log(mesh)
            self.assertEqual(handle.name, str(self.root / 'out' / 'sub' / 'body.log'))
            handle.close()
        text = (self.root / 'out' / 'sub' / 'body.log').read_text()
        self.assertEqual(text.count('run R1  repair: '), 2)

    def test_an_unwritable_log_warns_and_returns_none(self):
        (self.root / 'out').mkdir()
        (self.root / 'out' / 'sub').write_text('a file where the folder should be')
        runner = self.runner()
        with mock.patch.object(runner, '_log') as log:
            self.assertIsNone(runner._start_model_log(self.mesh()))
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
            self.assertIsNone(runner._start_model_log(mesh))
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
    """A real, slow Blender-stand-in conversion, genuinely interrupted by a
    real `SIGINT`-delivered `KeyboardInterrupt` during `converter.prepare` —
    confirms `_run` returns early, nonzero, without reaching dispatch, and
    that the in-flight Blender-stand-in process is actually killed (checked
    via `/proc`, not a mock assertion).
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

    def _slow_blender_stand_in(self, pidfile: str) -> str:
        fd, path = tempfile.mkstemp(suffix='.sh')
        with os.fdopen(fd, 'w') as f:
            f.write(
                "#!/bin/sh\n"
                f"echo $$ > {pidfile}\n"
                "exec sleep 300\n"
            )
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_keyboard_interrupt_during_real_slow_conversion_kills_it_and_stops_early(self):
        (self.source / 'body.obj').write_text('v 0 0 0\n')
        pidfile = str(self.root / 'blender.pid')
        exe = self._slow_blender_stand_in(pidfile)

        config = RunConfig(input=str(self.source), output=str(self.output),
                           max_faces=0, workers=1, per_file_timeout=30.0,
                           reap_deadline=5.0, memory_budget_bytes=10 ** 15)

        # `_run` constructs its own `intake_runner = blender.Runner()`
        # internally (spec section 4d) with no way to inject our stand-in
        # executable from outside — and `blender.convert(..., runner=...)`
        # ignores its own `executable` argument entirely once a `runner` is
        # supplied (the runner's OWN `.executable`, fixed at construction,
        # is what is actually used). So the stand-in has to be installed by
        # patching `Runner.__init__` to force it, while preserving
        # everything else about the constructor (in particular
        # `own_process_group`, which stays at its real default here — this
        # test is proving real top-level intake behavior, not the nested
        # containment case).
        orig_init = blender.Runner.__init__

        def patched_init(self, executable='blender', own_process_group=True):
            orig_init(self, exe, own_process_group)

        def deliver_interrupt_once_running():
            deadline = time.monotonic() + 10.0
            while not os.path.exists(pidfile) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(pidfile),
                            "the slow Blender stand-in never started")
            # A real SIGINT into THIS process's main thread — signal
            # delivery to the main thread is what actually raises
            # KeyboardInterrupt inside `converter.prepare`'s own
            # `Pool.start()`/`.join()`, matching how Ctrl+C really arrives.
            os.kill(os.getpid(), signal.SIGINT)

        interrupter = threading.Thread(target=deliver_interrupt_once_running, daemon=True)

        with mock.patch.object(blender.Runner, '__init__', patched_init):
            interrupter.start()
            try:
                code = batch_repair._run(config)
            finally:
                interrupter.join(timeout=15)

        self.assertEqual(code, 1)
        with open(pidfile) as f:
            blender_pid = int(f.read().strip())
        # Real evidence: the Blender-stand-in process is actually gone.
        deadline = time.monotonic() + 10.0
        while os.path.exists(f'/proc/{blender_pid}') and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(os.path.exists(f'/proc/{blender_pid}'),
                         "the in-flight Blender stand-in survived the "
                         "interrupted intake")


if __name__ == '__main__':
    unittest.main()
