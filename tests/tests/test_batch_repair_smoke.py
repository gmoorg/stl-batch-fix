"""Real end-to-end smoke tests: the actual batch_repair.py script, reading a
real batch_repair.toml beside it and spawning real batch_repair_child.py
processes that run the real pipeline (decimate, alpha-wrap, write) against
real fixtures.

The scripts are COPIED into a temp folder (with `libs` linked beside them)
so the config sits next to the script under test — never the user's own
batch_repair.toml.  Slow (CGAL import + real repair, tens of seconds per
file) — kept to a couple of cases, not a substitute for the fast unit/pool
layers in test_batch_repair_unit.py and test_batch_repair_pool.py.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
PROJECT = Path(__file__).resolve().parent.parent.parent


@unittest.skipUnless(FIXTURES.exists(), 'fixtures directory not found')
class TestRealEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input'
        self.output = self.root / 'output'
        self.input.mkdir()
        self.app = self.root / 'app'
        self.app.mkdir()
        for name in ('batch_repair.py', 'batch_repair_child.py'):
            shutil.copy2(PROJECT / name, self.app / name)
        (self.app / 'libs').symlink_to(PROJECT / 'libs', target_is_directory=True)

    def run_batch(self, workers):
        # Relative paths: they must resolve against the config's folder,
        # not the working directory (cwd is deliberately elsewhere).
        (self.app / 'batch_repair.toml').write_text(
            'input = "../input"\n'
            'output = "../output"\n'
            'max_faces = 0\n'
            f'workers = {workers}\n')
        return subprocess.run(
            [sys.executable, str(self.app / 'batch_repair.py')],
            cwd=self.input, capture_output=True, text=True, timeout=300)

    def test_single_small_fixture_publishes(self):
        shutil.copy2(FIXTURES / 'foot1.stl', self.input / 'foot1.stl')
        result = self.run_batch(workers=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Run complete.', result.stdout)
        self.assertTrue((self.output / 'foot1.stl').exists())

    def test_two_fixtures_run_in_parallel_and_both_publish(self):
        shutil.copy2(FIXTURES / 'arms.stl', self.input / 'arms.stl')
        shutil.copy2(FIXTURES / 'leg.stl', self.input / 'leg.stl')
        result = self.run_batch(workers=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output / 'arms.stl').exists())
        self.assertTrue((self.output / 'leg.stl').exists())


if __name__ == '__main__':
    unittest.main()
