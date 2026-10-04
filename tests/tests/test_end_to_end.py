"""End-to-end: the real batch_repair.py on defect composites, checked
against their known true shape — what a print gets, not how the pipeline is
assembled. Slow (~3-4 min): one real run of four composites, two workers.

Acceptance limits (chosen before the first run; see docs/refactor/tests.md):
h = min(input diagonal / 800, 0.15) is the reconstruction grid; each truth
part's sag s is how far its facets dip below the smooth shape. A correct
output, rebuilt within ~1 cell and decimated back to about the part's own
face count, should stay within 2h + s (99th percentile) and 2h + 2s (max)
of the truth. These are engineering limits, not proven bounds: a failure is
investigated, never fixed by loosening them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from libs import mesh_io, scanner
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds

try:
    import igl
    HAVE_IGL = True
except ImportError:                                     # pragma: no cover
    HAVE_IGL = False

PROJECT = Path(__file__).resolve().parent.parent.parent


def _write(path, verts, faces):
    mesh_io.write(Mesh(str(path), str(path), Kind.BINARY_STL, len(faces), True, None,
                       Geometry(np.asarray(verts, np.float32), np.asarray(faces, np.int64))))


def _surface_samples(v, f, n, seed):
    rng = np.random.default_rng(seed)
    a, b, c = (v[f[:, i]] for i in range(3))
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2
    pick = rng.choice(len(f), size=n, p=area / area.sum())
    u, w = rng.random((2, n)); flip = u + w > 1; u[flip], w[flip] = 1 - u[flip], 1 - w[flip]
    return a[pick] + u[:, None] * (b - a)[pick] + w[:, None] * (c - a)[pick]


@unittest.skipUnless(HAVE_IGL, 'libigl is needed')
class TestEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='e2e-'))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        app, cls.inp, cls.out = cls.tmp / 'app', cls.tmp / 'input', cls.tmp / 'output'
        app.mkdir(); cls.inp.mkdir()
        for name in ('batch_repair.py', 'batch_repair_child.py'):
            shutil.copy2(PROJECT / name, app / name)
        (app / 'libs').symlink_to(PROJECT / 'libs', target_is_directory=True)
        cls.composites = ds.composites()
        for name, fx in cls.composites.items():
            _write(cls.inp / f'{name}.stl', fx.verts, fx.faces)
        (app / 'batch_repair.toml').write_text(
            f'input = "{cls.inp}"\noutput = "{cls.out}"\nmax_faces = 900000\nworkers = 2\n')
        cls.run_result = subprocess.run(
            [sys.executable, str(app / 'batch_repair.py')], cwd=cls.tmp,
            capture_output=True, text=True, timeout=1800)
        cls.jobs = {}
        progress = cls.out / 'progress.log'
        if progress.exists():
            for line in progress.read_text().splitlines():
                rec = json.loads(line)
                if rec.get('kind') == 'job':
                    cls.jobs[Path(rec['mesh']).stem] = rec
        cls.log_parts = {}
        batch_log = cls.out / 'batch.log'
        if batch_log.exists():
            for line in batch_log.read_text().splitlines():
                f = line.split('\t')
                if len(f) >= 5 and f[2] == 'end' and f[3] == 'winding':
                    cls.log_parts.setdefault(Path(f[1]).stem, set()).add(f[4])

    def _output(self, name):
        path = self.out / f'{name}.stl'
        self.assertTrue(path.exists(), f'{name} was not published: {self.run_result.stdout[-2000:]}')
        g = mesh_io.load(mesh_io.probe(str(path), str(path))).geometry
        return g.verts.astype(np.float64), g.faces.astype(np.int64)

    def _h(self, fx):
        diag = float(np.linalg.norm(np.ptp(fx.verts.astype(np.float64), 0)))
        return min(diag / 800, 0.15)

    def test_the_run_succeeds(self):
        self.assertEqual(self.run_result.returncode, 0,
                         self.run_result.stdout[-3000:] + self.run_result.stderr[-3000:])

    def test_published_clean_outward_and_non_empty(self):
        for name in self.composites:
            with self.subTest(name):
                self.assertEqual(self.jobs.get(name, {}).get('indicator'), 'PROCESS', self.jobs.get(name))
                markers = [p.name for p in self.out.glob(f'{name}.*.stl')]
                self.assertEqual(markers, [])
                V, F = self._output(name)
                self.assertGreater(len(F), 0)
                self.assertTrue(np.isfinite(V).all())
                mesh = Mesh('/o', '/o', Kind.BINARY_STL, len(F), True, None, Geometry(V.astype(np.float32), F))
                s = scanner.scan(mesh)
                self.assertEqual((s.open_edges, s.non_manifold), (0, 0))
                for shell in scanner.shells(mesh):
                    self.assertGreater(ds.signed_volume(V, F[shell]), 0, 'every output shell outward')

    def test_every_truth_part_is_preserved(self):
        for name, fx in self.composites.items():
            V, F = self._output(name)
            h = self._h(fx)
            for i, sag in enumerate(fx.defects['sag']):
                p, _ = ds.union_boundary_samples(fx, 2000, seed=i, part=i)
                d = np.sqrt(igl.point_mesh_squared_distance(p, V, F)[0])
                with self.subTest(name=name, part=i):
                    self.assertLessEqual(np.percentile(d, 99), 2 * h + sag,
                                         f'p99 {np.percentile(d, 99):.4f} h {h:.4f} sag {sag:.4f}')
                    self.assertLessEqual(d.max(), 2 * h + 2 * sag,
                                         f'max {d.max():.4f} h {h:.4f} sag {sag:.4f}')

    def test_no_invented_geometry(self):
        for name, fx in self.composites.items():
            V, F = self._output(name)
            h = self._h(fx)
            q = _surface_samples(V, F, 20000, seed=1)
            per_part = np.column_stack([
                np.sqrt(igl.point_mesh_squared_distance(q, np.asarray(tv, np.float64),
                                                        np.asarray(tf, np.int64))[0])
                for tv, tf in fx.truth])
            nearest = per_part.argmin(1)
            excess = per_part.min(1) - (2 * h + 2 * np.asarray(fx.defects['sag']))[nearest]
            with self.subTest(name):
                self.assertLessEqual(excess.max(), 0, f'worst excess {excess.max():.4f} (h {h:.4f})')

    def test_debris_is_gone(self):
        for name, fx in self.composites.items():
            V, _ = self._output(name)
            for lo, hi in fx.vanish_boxes:
                with self.subTest(name=name, box=(tuple(lo), tuple(hi))):
                    self.assertFalse(np.all((V >= lo) & (V <= hi), axis=1).any())

    def test_enclosed_volume_matches_the_truth(self):
        for name, fx in self.composites.items():
            V, F = self._output(name)
            pts = np.vstack([V] + [np.asarray(tv, np.float64) for tv, _ in fx.truth])
            lo, hi = pts.min(0), pts.max(0)
            pad = 0.02 * (hi - lo)
            lo, hi = lo - pad, hi + pad
            n = 1_000_000
            q = lo + np.random.default_rng(3).random((n, 3)) * (hi - lo)
            frac = (igl.fast_winding_number(V, F, q) > 0.5).mean()
            box_volume = float(np.prod(hi - lo))
            estimate = frac * box_volume
            rel_se = np.sqrt(frac * (1 - frac) / n) * box_volume / fx.truth_volume
            with self.subTest(name):
                self.assertLess(rel_se, 0.005)
                self.assertAlmostEqual(estimate / fx.truth_volume, 1.0, delta=0.02,
                                       msg=f'estimate {estimate:.5g} truth {fx.truth_volume:.5g}')

    def test_the_rod_tip_survives(self):
        fx = self.composites['c_multishell_rod']
        V, F = self._output('c_multishell_rod')
        h = self._h(fx)
        self.assertLess(2 * h, fx.defects['rod_radius'], 'the grid must resolve the rod')
        tip = np.asarray([fx.defects['rod_tip']], np.float64)
        self.assertLessEqual(np.sqrt(igl.point_mesh_squared_distance(tip, V, F)[0][0]), 2 * h)

    def test_log_attributes_each_part(self):
        for name, fx in self.composites.items():
            with self.subTest(name):
                self.assertEqual(self.log_parts.get(name), set(fx.defects['parts']))


if __name__ == '__main__':
    unittest.main()
