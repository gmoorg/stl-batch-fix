"""Real end-to-end smoke test of fix_files.py: the actual script, reading a
real batch_repair.toml beside it and running the real pipeline on defect
spheres selected in one folder, as the right-click entries do.

The scripts are COPIED into a temp folder (with `libs` linked beside them),
as in test_batch_repair_smoke.py, so the config is never the user's own.
Slow (real repair).
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from libs import besidesource, mesh_io
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds

PROJECT = Path(__file__).resolve().parent.parent.parent


class TestFixFilesEndToEnd(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.folder = root / 'my models'
        self.folder.mkdir()
        self.app = root / 'app'
        self.app.mkdir()
        for name in ('batch_repair.py', 'batch_repair_child.py', 'fix_files.py'):
            shutil.copy2(PROJECT / name, self.app / name)
        (self.app / 'libs').symlink_to(PROJECT / 'libs', target_is_directory=True)
        # input/output are required keys but replaced for every run.
        (self.app / 'batch_repair.toml').write_text(
            'input = "unused-in"\noutput = "unused-out"\nmax_faces = 0\nworkers = 2\n')

    def add_stl(self, fixture, name):
        fx = ds.fixtures()[fixture]
        path = str(self.folder / name)
        mesh_io.write(Mesh(path, path, Kind.BINARY_STL, len(fx.faces), True, None,
                           Geometry(np.asarray(fx.verts, np.float64),
                                    np.asarray(fx.faces, np.int64))))
        return path

    def add_obj(self, fixture, name):
        fx = ds.fixtures()[fixture]
        path = self.folder / name
        path.write_text(''.join(f'v {x} {y} {z}\n' for x, y, z in fx.verts)
                        + ''.join(f'f {a + 1} {b + 1} {c + 1}\n' for a, b, c in fx.faces))
        return str(path)

    def fix(self, *paths):
        return subprocess.run([sys.executable, str(self.app / 'fix_files.py'), '--', *paths],
                              cwd=self.app, capture_output=True, text=True, timeout=600)

    def test_results_markers_and_logs_land_beside_the_sources(self):
        stl = self.add_stl('hole', 'foot one.stl')
        obj = self.add_obj('seam', 'leg.OBJ')
        broken = self.folder / 'arm.obj'
        broken.write_text('v 0 0 0\nf 1 2 3\n')               # malformed: no such vertices
        sources = {p: Path(p).read_bytes() for p in (stl, obj, str(broken))}

        result = self.fix(stl, obj, str(broken))
        self.assertIn('foot one.stl -> foot one_fixed.stl', result.stdout, result.stderr)
        self.assertIn('leg.OBJ -> leg_fixed.stl', result.stdout)
        self.assertIn('arm.obj -> arm_fixed.failed.stl', result.stdout)
        self.assertNotEqual(result.returncode, 0)               # the malformed file
        for name in ('foot one_fixed.stl', 'leg_fixed.stl'):
            result_path = str(self.folder / name)
            self.assertIs(mesh_io.kind(result_path), Kind.BINARY_STL, name)
            self.assertGreater(mesh_io.triangle_count(result_path)[0], 0, name)
        # A FAILED marker is a full copy of the source: the fallback print.
        self.assertEqual((self.folder / 'arm_fixed.failed.stl').read_bytes(),
                         sources[str(broken)])
        self.assertIn('repair: ', (self.folder / 'foot one_fixed.log').read_text())
        self.assertIn('Reconstruction: winding', (self.folder / 'batch.log').read_text())
        self.assertTrue((self.folder / 'progress.log').exists())
        for path, data in sources.items():
            self.assertEqual(Path(path).read_bytes(), data, path)
        self.assertEqual([p for p in self.folder.iterdir()
                          if p.name.startswith(besidesource.STAGING_PREFIX)], [])

        # A second run replaces the result and appends to the logs.
        result = self.fix(stl)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runs = set(re.findall(r'^==== .*  run (\S+)  ',
                              (self.folder / 'foot one_fixed.log').read_text(), re.M))
        self.assertEqual(len(runs), 2)
        self.assertEqual((self.folder / 'batch.log').read_text().count('Reconstruction: '), 2)


if __name__ == '__main__':
    unittest.main()
