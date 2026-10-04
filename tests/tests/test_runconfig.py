"""Tests for libs.runconfig — loading and validating batch_repair.toml, and
for the shipped batch_repair.example.toml that documents it."""

import os
import tempfile
import tomllib
import unittest
from dataclasses import FrozenInstanceError, MISSING, fields
from pathlib import Path
from unittest import mock

from libs import runconfig
from libs.runconfig import ConfigError, RunConfig

EXAMPLE = Path(__file__).resolve().parent.parent.parent / 'batch_repair.example.toml'
REQUIRED = 'input = "in"\noutput = "out"\nmax_faces = 0\n'


class _TempDir(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load_text(self, text: str) -> RunConfig:
        path = self.root / 'batch_repair.toml'
        path.write_text(text)
        return runconfig.load(str(path))


class TestExampleFile(unittest.TestCase):
    """The example is the documentation; it must not drift from the code."""

    def test_example_parses(self):
        runconfig.load(str(EXAMPLE))

    def test_example_lists_every_field(self):
        with open(EXAMPLE, 'rb') as handle:
            raw = tomllib.load(handle)
        self.assertEqual(set(raw), {f.name for f in fields(RunConfig)})

    def test_example_values_equal_the_code_defaults(self):
        # Raw TOML, not `load()`: `load` resolves relative paths, which would
        # make "" vs a resolved path a false mismatch.
        with open(EXAMPLE, 'rb') as handle:
            raw = tomllib.load(handle)
        for f in fields(RunConfig):
            if f.default is not MISSING:
                with self.subTest(key=f.name):
                    self.assertEqual(raw[f.name], f.default)
                    self.assertIs(type(raw[f.name]), type(f.default))

    def test_max_faces_comment_says_initial_pass_only(self):
        text = EXAMPLE.read_text()
        self.assertIn('INITIAL', text)
        self.assertIn('per-part decimation', text)


class TestLoad(_TempDir):
    def test_required_only_gets_defaults(self):
        config = self.load_text(REQUIRED)
        self.assertEqual(config.max_faces, 0)
        self.assertFalse(config.skip_clean)
        self.assertEqual(config.workers, 0)
        self.assertEqual(config.log_file, '')

    def test_relative_paths_resolve_against_the_config_folder(self):
        cwd = os.getcwd()
        os.chdir('/')
        try:
            config = self.load_text(REQUIRED + 'log_file = "logs/b.log"\n')
        finally:
            os.chdir(cwd)
        self.assertEqual(config.input, str(self.root / 'in'))
        self.assertEqual(config.output, str(self.root / 'out'))
        self.assertEqual(config.log_file, str(self.root / 'logs' / 'b.log'))

    def test_absolute_paths_are_kept(self):
        config = self.load_text('input = "/a"\noutput = "/b"\nmax_faces = 1\n')
        self.assertEqual((config.input, config.output), ('/a', '/b'))

    def test_int_is_accepted_for_a_float_key(self):
        config = self.load_text(REQUIRED + 'per_file_timeout = 60\n')
        self.assertEqual(config.per_file_timeout, 60.0)
        self.assertIsInstance(config.per_file_timeout, float)

    def test_config_is_frozen(self):
        config = self.load_text(REQUIRED)
        with self.assertRaises(FrozenInstanceError):
            config.workers = 3

    def test_unknown_key_is_an_error_naming_it(self):
        with self.assertRaisesRegex(ConfigError, 'wrokers'):
            self.load_text(REQUIRED + 'wrokers = 2\n')

    def test_a_table_is_an_unknown_key(self):
        with self.assertRaisesRegex(ConfigError, 'unknown key'):
            self.load_text(REQUIRED + '[extra]\nx = 1\n')

    def test_each_missing_required_key_is_named(self):
        for key in runconfig.required_keys():
            lines = [line for line in REQUIRED.splitlines() if not line.startswith(key)]
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                self.load_text('\n'.join(lines) + '\n')

    def test_wrong_types_are_errors_naming_the_key(self):
        cases = [
            ('workers', 'true'),          # bool is not an int
            ('workers', '"4"'),
            ('workers', '2.0'),
            ('max_faces', '1.5'),
            ('skip_clean', '1'),          # int is not a bool
            ('skip_clean', '"true"'),
            ('per_file_timeout', 'true'),
            ('per_file_timeout', '"60"'),
            ('log_file', '0'),
        ]
        for key, value in cases:
            text = REQUIRED.replace(f'{key} = 0\n', '') + f'{key} = {value}\n'
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, key):
                self.load_text(text)

    def test_out_of_range_values_are_errors(self):
        cases = [
            ('max_faces', '-1'), ('workers', '-1'), ('memory_budget_bytes', '-1'),
            ('per_file_timeout', '0'), ('per_file_timeout', '-1'),
            ('per_file_timeout', 'inf'), ('per_file_timeout', 'nan'),
            ('reap_deadline', '0'), ('reap_deadline', '-1'),
            ('memory_budget_fraction', '0'), ('memory_budget_fraction', '1.5'),
            ('memory_budget_fraction', '-0.1'),
        ]
        for key, value in cases:
            text = REQUIRED.replace(f'{key} = 0\n', '') + f'{key} = {value}\n'
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ConfigError, key):
                self.load_text(text)

    def test_empty_input_or_output_is_an_error(self):
        for key in ('input', 'output'):
            for blank in ('""', '"  "'):
                text = '\n'.join(line if not line.startswith(key) else f'{key} = {blank}'
                                 for line in REQUIRED.splitlines()) + '\n'
                with self.subTest(key=key, blank=blank), \
                     self.assertRaisesRegex(ConfigError, key):
                    self.load_text(text)


    def test_nul_in_a_path_is_an_error(self):
        for key in ('input', 'output', 'log_file'):
            line = f'{key} = "a\\u0000b"'
            text = '\n'.join(l for l in REQUIRED.splitlines() if not l.startswith(key))
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                self.load_text(text + '\n' + line + '\n')

    def test_an_overflowing_float_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, 'per_file_timeout'):
            self.load_text(REQUIRED + f'per_file_timeout = {10 ** 400}\n')


class TestReconstructBudget(_TempDir):
    def test_default_is_10_gb(self):
        config = self.load_text(REQUIRED)
        self.assertEqual(config.reconstruct_memory_budget_gb, 10.0)
        self.assertEqual(runconfig.budget_bytes(config.reconstruct_memory_budget_gb), 10_000_000_000)

    def test_accepts_int_and_float(self):
        for text, gb in (('20', 20.0), ('2.5', 2.5)):
            with self.subTest(text=text):
                self.assertEqual(self.load_text(REQUIRED + f'reconstruct_memory_budget_gb = {text}\n')
                                 .reconstruct_memory_budget_gb, gb)

    def test_rejects_values_that_are_not_a_usable_byte_count(self):
        for text in ('0', '-1', '1e-12', '1e300', 'inf', 'true', '"10"'):
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, 'reconstruct_memory_budget_gb'):
                self.load_text(REQUIRED + f'reconstruct_memory_budget_gb = {text}\n')


class TestUnreadableFiles(_TempDir):
    def test_missing_file(self):
        with self.assertRaisesRegex(ConfigError, 'not found'):
            runconfig.load(str(self.root / 'absent.toml'))

    def test_directory(self):
        with self.assertRaisesRegex(ConfigError, 'directory'):
            runconfig.load(str(self.root))

    def test_malformed_toml(self):
        with self.assertRaisesRegex(ConfigError, 'not valid TOML'):
            self.load_text('input = "unterminated\n')

    def test_invalid_utf8(self):
        path = self.root / 'batch_repair.toml'
        path.write_bytes(b'input = "\xff"\n')
        with self.assertRaises(ConfigError):
            runconfig.load(str(path))

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_unreadable_file(self):
        path = self.root / 'batch_repair.toml'
        path.write_text(REQUIRED)
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaisesRegex(ConfigError, 'cannot read'):
            runconfig.load(str(path))


class TestResolve(unittest.TestCase):
    def base(self, **values):
        return RunConfig(input='/in', output='/out', max_faces=0, **values)

    def test_automatic_values_become_concrete(self):
        with mock.patch('os.cpu_count', return_value=16):
            config = runconfig.resolve(self.base())
        self.assertEqual(config.workers, 4)
        self.assertEqual(config.log_file, os.path.join('/out', 'batch.log'))
        self.assertGreater(config.memory_budget_bytes, 0)

    def test_explicit_values_are_kept(self):
        config = runconfig.resolve(self.base(workers=2, log_file='/l/x.log',
                                             memory_budget_bytes=1000))
        self.assertEqual((config.workers, config.log_file, config.memory_budget_bytes),
                         (2, '/l/x.log', 1000))

    def test_budget_is_the_fraction_of_available_memory(self):
        sysconf = {'SC_PAGE_SIZE': 4096, 'SC_AVPHYS_PAGES': 1000}
        with mock.patch('os.sysconf', side_effect=sysconf.__getitem__):
            config = runconfig.resolve(self.base(memory_budget_fraction=0.5))
        self.assertEqual(config.memory_budget_bytes, 4096 * 1000 // 2)

    def test_a_zero_derived_budget_is_an_error(self):
        sysconf = {'SC_PAGE_SIZE': 4096, 'SC_AVPHYS_PAGES': 0}
        with mock.patch('os.sysconf', side_effect=sysconf.__getitem__), \
             self.assertRaisesRegex(ConfigError, 'memory_budget_bytes'):
            runconfig.resolve(self.base())

    def test_unavailable_sysconf_is_an_error(self):
        with mock.patch('os.sysconf', side_effect=ValueError), \
             self.assertRaisesRegex(ConfigError, 'memory_budget_bytes'):
            runconfig.resolve(self.base())


if __name__ == '__main__':
    unittest.main()
