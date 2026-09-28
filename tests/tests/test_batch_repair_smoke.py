"""Real end-to-end smoke tests: the actual batch_repair.py CLI, spawning real
--one-file children that run the real pipeline (decimate, alpha-wrap, write)
against real fixtures.  Slow (CGAL import + real repair, tens of seconds per
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
SCRIPT = Path(__file__).resolve().parent.parent.parent / 'tools' / 'batch_repair.py'


@unittest.skipUnless(FIXTURES.exists(), 'fixtures directory not found')
class TestRealEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / 'input'
        self.output = self.root / 'output'
        self.input.mkdir()

    def test_single_small_fixture_publishes(self):
        shutil.copy2(FIXTURES / 'foot1.stl', self.input / 'foot1.stl')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--input', str(self.input),
             '--output', str(self.output), '--max-faces', '0', '--workers', '1'],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Run complete.', result.stdout)
        self.assertTrue((self.output / 'foot1.stl').exists())

    def test_two_fixtures_run_in_parallel_and_both_publish(self):
        shutil.copy2(FIXTURES / 'arms.stl', self.input / 'arms.stl')
        shutil.copy2(FIXTURES / 'leg.stl', self.input / 'leg.stl')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--input', str(self.input),
             '--output', str(self.output), '--max-faces', '0', '--workers', '2'],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output / 'arms.stl').exists())
        self.assertTrue((self.output / 'leg.stl').exists())


if __name__ == '__main__':
    unittest.main()
