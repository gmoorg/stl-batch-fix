"""CLI argument validation, dependency checks, and the top-level main() boundary."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from libs import converter
from tools import batch_repair


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
        self.checks = {}
        for name, module in batch_repair.DEPENDENCIES:
            self.checks[name] = self.stack.enter_context(
                mock.patch.object(module, 'is_available', return_value=True))

    def invoke(self, source=None, output=None, budget='0', extra=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = batch_repair.main([
                    '--input', str(source or self.source),
                    '--output', str(output or self.output),
                    '--max-faces', budget, *extra])
            except SystemExit as error:
                code = error.code
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

    def test_workers_must_be_positive(self):
        code, text = self.invoke(extra=['--workers', '0'])
        self.assertEqual(code, 2, text)
        self.assertIn('--workers', text)

    def test_per_file_timeout_must_be_finite_positive(self):
        for value in ('-1', '0', 'inf'):
            with self.subTest(value=value):
                code, text = self.invoke(extra=['--per-file-timeout', value])
                self.assertEqual(code, 2, text)
                self.assertIn('--per-file-timeout', text)

    def test_reap_deadline_must_be_finite_positive(self):
        code, text = self.invoke(extra=['--reap-deadline', '-1'])
        self.assertEqual(code, 2, text)
        self.assertIn('--reap-deadline', text)

    def test_memory_budget_fraction_must_be_in_range(self):
        for value in ('0', '1.5', '-0.1'):
            with self.subTest(value=value):
                code, text = self.invoke(extra=['--memory-budget-fraction', value])
                self.assertEqual(code, 2, text)
                self.assertIn('--memory-budget-fraction', text)

    def test_memory_budget_bytes_overrides_fraction(self):
        with mock.patch.object(converter, 'prepare', return_value=converter.Summary()):
            code, text = self.invoke(extra=['--memory-budget-bytes', '1000'])
        self.assertEqual(code, 0, text)

    def test_each_missing_dependency(self):
        with mock.patch.object(converter, 'prepare') as prepare:
            for name, check in self.checks.items():
                with self.subTest(dependency=name):
                    check.return_value = False
                    code, text = self.invoke()
                    self.assertEqual(code, 2, text)
                    self.assertIn(name, text)
                    self.assertFalse(self.output.exists())
                    check.return_value = True
            prepare.assert_not_called()

    def test_all_missing_dependencies_reported(self):
        for check in self.checks.values():
            check.return_value = False
        code, text = self.invoke()
        self.assertEqual(code, 2)
        for name in self.checks:
            self.assertIn(name, text)

    def test_invalid_binary_is_intake_failure(self):
        import struct
        path = self.source / 'bad.stl'
        path.write_bytes(b'\0' * 80 + struct.pack('<I', 4))
        code, text = self.invoke()
        self.assertEqual(code, 1, text)
        for fragment in ('intake_failure=1', 'conversion_failed=0',
                         'total=1, jobs=1', str(path)):
            self.assertIn(fragment, text)
        self.assertFalse(self.output.exists())

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

    def test_companion_copy_failure(self):
        (self.source / 'notes.txt').write_text('notes')
        with mock.patch.object(converter.shutil, 'copy2', side_effect=OSError('denied')):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('copy_failed=1', text)
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
        with mock.patch.object(converter, 'prepare', side_effect=KeyboardInterrupt):
            code, text = self.invoke()
        self.assertEqual(code, 1, text)
        self.assertIn('interrupted/incomplete', text)
        self.assertNotIn('Run complete.', text)

    def test_direct_script_help_and_required_arguments(self):
        script = Path(batch_repair.__file__).resolve()
        for argv, expected in ((['--help'], 0), ([], 2)):
            result = subprocess.run([sys.executable, str(script), *argv],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_one_file_requires_destination_and_result_file(self):
        script = Path(batch_repair.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), '--one-file', 'x.stl', '--max-faces', '0'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--destination', result.stderr)


if __name__ == '__main__':
    unittest.main()
