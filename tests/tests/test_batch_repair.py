"""Serial runner contracts, using generated meshes and controlled tool outcomes."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from libs import converter, mesh_io, processor
from libs.indicators import Indicator
from libs.mesh_io import Geometry, Kind, Mesh
from tools import batch_repair


class TestBatchRepair(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.checks = {}
        for name, module in batch_repair.DEPENDENCIES:
            self.checks[name] = self.stack.enter_context(
                mock.patch.object(module, 'is_available', return_value=True))
        self.process = self.stack.enter_context(mock.patch.object(
            processor, 'process', side_effect=lambda mesh, budget:
            processor.Outcome(Indicator.PROCESS, mesh, None, 'clean')))

    def fixture(self, name='body.stl'):
        path = self.source / name
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                     dtype=np.float32),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]],
                     dtype=np.int64))
        mesh_io.write(Mesh(str(path), str(path), Kind.BINARY_STL, 4,
                           True, geometry=geometry))
        return path

    def invoke(self, source=None, output=None, budget='0'):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = batch_repair.main([
                    '--input', str(source or self.source),
                    '--output', str(output or self.output),
                    '--max-faces', budget])
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue() + stderr.getvalue()

    def test_clean_and_rerun(self):
        for name in ('a.stl', 'nested/b.stl', 'c.stl'):
            self.fixture(name)
        for suffix in ('.txt', '.png', '.jpg'):
            (self.source / ('companion' + suffix)).write_bytes(b'companion')
        real_prepare = converter.prepare
        intake_complete = False

        def prepare(*args, **kwargs):
            nonlocal intake_complete
            self.assertEqual(kwargs['workers'], 1)
            result = real_prepare(*args, **kwargs)
            self.process.assert_not_called()
            intake_complete = True
            return result

        def process(mesh, budget):
            self.assertTrue(intake_complete)
            self.assertEqual(budget, 0)
            self.assertTrue(mesh.is_loaded)
            return processor.Outcome(Indicator.PROCESS, mesh, None, 'clean')

        self.process.side_effect = process
        with mock.patch.object(converter, 'prepare', side_effect=prepare):
            code, text = self.invoke()
        self.assertEqual(code, 0, text)
        for fragment in ('scanned=6', 'copied=3', 'copy_failed=0',
                         'already_copied=0', 'skipped=0', 'emitted=3',
                         'converted=0', 'conversion_failed=0',
                         'total=3, jobs=3', 'PROCESS=3'):
            self.assertIn(fragment, text)
        for name in ('a.stl', 'nested/b.stl', 'c.stl'):
            path = self.output / name
            self.assertTrue(mesh_io.load(mesh_io.probe(str(path), str(path))).is_valid)
        self.process.reset_mock()
        code, text = self.invoke()
        self.assertEqual(code, 0, text)
        self.assertIn('skipped=3', text)
        self.assertIn('already_copied=3', text)
        self.assertIn('total=0, jobs=0', text)
        self.process.assert_not_called()

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
            (self.root / 'missing', self.output, '0', '--input'),
            (regular_file, self.output, '0', '--input'),
            (self.source, regular_file, '0', '--output'),
            (self.source, self.output, '-1', '--max-faces'),
            (self.source, self.output, 'abc', '--max-faces'),
            (self.source, self.output, '1.5', '--max-faces'),
            (self.root / 'missing', regular_file, 'bad', '--input'),
        ]
        with mock.patch.object(converter, 'prepare') as prepare:
            for source, output, budget, message in cases:
                with self.subTest(source=source, output=output, budget=budget):
                    code, text = self.invoke(source, output, budget)
                    self.assertEqual(code, 2, text)
                    self.assertIn(message, text)
            prepare.assert_not_called()
        self.process.assert_not_called()
        for check in self.checks.values():
            check.assert_not_called()

    def test_each_missing_dependency(self):
        original = self.fixture().read_bytes()
        with mock.patch.object(converter, 'prepare') as prepare:
            for name, check in self.checks.items():
                with self.subTest(dependency=name):
                    check.return_value = False
                    code, text = self.invoke()
                    self.assertEqual(code, 2, text)
                    self.assertIn(name, text)
                    self.assertFalse(self.output.exists())
                    self.assertEqual((self.source / 'body.stl').read_bytes(), original)
                    check.return_value = True
            prepare.assert_not_called()
        for check in self.checks.values():
            self.assertEqual(check.call_count, 5)

    def test_all_missing_dependencies_reported(self):
        for check in self.checks.values():
            check.return_value = False
        code, text = self.invoke()
        self.assertEqual(code, 2)
        for name in self.checks:
            self.assertIn(name, text)

    def test_invalid_binary_is_intake_failure(self):
        path = self.source / 'bad.stl'
        path.write_bytes(b'\0' * 80 + struct.pack('<I', 4))
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        for fragment in ('intake_failure=1', 'conversion_failed=0',
                         'total=1, jobs=1', 'stage=intake', str(path)):
            self.assertIn(fragment, text)
        self.assertFalse(self.output.exists())
        self.process.assert_not_called()

    def test_conversion_failure_and_invalid_success(self):
        (self.source / 'body.obj').write_text('v 0 0 0\n')
        for ok in (False, True):
            with self.subTest(ok=ok):
                def convert(source, export):
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
        self.assertFalse(self.output.exists())

    def test_nonfinite_is_load_failure(self):
        path = self.fixture()
        data = bytearray(path.read_bytes())
        struct.pack_into('<f', data, 84 + 12, float('nan'))
        path.write_bytes(data)
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        for fragment in ('intake_failure=0', 'load_failure=1', 'stage=load',
                         'not finite', 'total=1, jobs=1'):
            self.assertIn(fragment, text)
        self.assertFalse(self.output.exists())
        self.process.assert_not_called()

    def test_failed_repair_publishes_marker(self):
        source = self.fixture('nested/body.stl')
        self.process.side_effect = None
        self.process.return_value = processor.Outcome(
            Indicator.DESTROYED, None, 'source', 'lost geometry')
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('published=1', text)
        self.assertIn('DESTROYED=1', text)
        self.assertIn('reason=lost geometry', text)
        self.assertEqual((self.output / 'nested/body.destroyed.stl').read_bytes(),
                         source.read_bytes())
        self.assertFalse((self.output / 'nested/body.stl').exists())

    def test_write_exception_continues_without_double_counting(self):
        self.fixture('a.stl')
        self.fixture('b.stl')
        real_write = processor.write
        calls = []

        def write(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('disk failure')
            return real_write(*args)

        with mock.patch.object(processor, 'write', side_effect=write):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        for fragment in ('write_failure=1', 'process_failure=0', 'published=1',
                         'PROCESS=1', 'total=2, jobs=2', 'stage=write', 'disk failure'):
            self.assertIn(fragment, text)
        self.assertEqual(self.process.call_count, 2)
        self.assertTrue(Path(calls[1][2]).exists())
        self.assertEqual(text.count('Diagnostic:'), 1)

    def test_write_none_is_failure(self):
        self.fixture()
        with mock.patch.object(processor, 'write', return_value=None):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('write_failure=1', text)
        self.assertIn('returned None', text)
        self.assertIn('published=0', text)

    def test_process_exception_continues(self):
        self.fixture('a.stl')
        self.fixture('b.stl')
        clean = self.process.side_effect
        count = 0

        def process(mesh, budget):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError('repair threw')
            return clean(mesh, budget)

        self.process.side_effect = process
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('process_failure=1', text)
        self.assertIn('stage=process', text)
        self.assertIn('total=2, jobs=2', text)
        self.assertIn('PROCESS=1', text)

    def test_companion_copy_failure(self):
        (self.source / 'notes.txt').write_text('notes')
        with mock.patch.object(converter.shutil, 'copy2', side_effect=OSError('denied')):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('copy_failed=1', text)
        self.assertIn('total=0, jobs=0', text)

    def test_intake_exception_is_incomplete(self):
        path = self.fixture()

        def prepare(source, destination, emit, **kwargs):
            emit(mesh_io.probe(str(path), str(self.output / path.name)))
            raise RuntimeError('walk failed')

        with mock.patch.object(converter, 'prepare', side_effect=prepare):
            code, text = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn('Run incomplete: intake', text)
        self.assertIn('walk failed', text)
        self.assertNotIn('Intake:', text)
        self.assertNotIn('Run complete.', text)
        self.process.assert_not_called()

    def test_keyboard_interrupt_at_outer_boundary(self):
        self.fixture()
        for module, method in ((converter, 'prepare'), (mesh_io, 'load'),
                               (processor, 'process'), (processor, 'write')):
            with self.subTest(stage=method):
                with mock.patch.object(module, method, side_effect=KeyboardInterrupt):
                    code, text = self.invoke()
                self.assertEqual(code, 1, text)
                self.assertIn('interrupted/incomplete', text)
                self.assertNotIn('Run complete.', text)
                self.assertNotIn('Terminal:', text)

    def test_direct_script_help_and_required_arguments(self):
        script = Path(batch_repair.__file__).resolve()
        for argv, expected in ((['--help'], 0), ([], 2)):
            result = subprocess.run([sys.executable, str(script), *argv],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            if expected == 0:
                self.assertIn('0 disables decimation', result.stdout)


if __name__ == '__main__':
    unittest.main()
