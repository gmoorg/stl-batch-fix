"""Self-checks for tests/tests/defect_spheres.py: every fixture really has
the defects it declares, its truth is clean, and its truth volume and union
boundary are right. A builder that stops reproducing its defect fails here,
before any outcome test silently stops testing anything."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from libs import scanner
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds

PROJECT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ds.fixtures()


def as_mesh(v, f):
    g = Geometry(np.asarray(v, np.float64), np.asarray(f, np.int64))
    return Mesh('/m.stl', '/m.stl', Kind.BINARY_STL, len(g.faces), True, None, g)


class TestDeclaredDefects(unittest.TestCase):

    def test_every_fixture_has_its_declared_topology(self):
        for name, fx in FIXTURES.items():
            m = as_mesh(fx.verts, fx.faces)
            s = scanner.scan(m)
            seams, _ = scanner.winding_seams(m)
            d = fx.defects
            with self.subTest(name):
                if 'open' in d:
                    self.assertEqual(s.open_edges > 0, d['open'], s)
                if 'non_manifold' in d:
                    self.assertEqual(s.non_manifold > 0, d['non_manifold'], s)
                if 'seams' in d:
                    self.assertEqual(seams > 0, d['seams'], seams)
                if 'degenerate' in d:
                    self.assertEqual(s.degenerate > 0, d['degenerate'], s)
                if 'shells' in d:
                    self.assertEqual(len(scanner.shells(m)), d['shells'])
                if 'volume_sign' in d:
                    self.assertEqual(np.sign(ds.signed_volume(fx.verts, fx.faces)), d['volume_sign'])
                if 'nm_edges' in d:
                    self.assertEqual(s.non_manifold, d['nm_edges'], s)
                if 'open_edges' in d:
                    self.assertEqual(s.open_edges, d['open_edges'], s)
                if 'seam_edges' in d:
                    self.assertEqual(seams, d['seam_edges'])

    def test_every_fixture_declares_at_least_its_shell_count(self):
        for name, fx in FIXTURES.items():
            with self.subTest(name):
                self.assertIn('shells', fx.defects)


class TestTruth(unittest.TestCase):

    def test_truth_parts_are_clean_closed_and_outward(self):
        for name, fx in FIXTURES.items():
            for i, (v, f) in enumerate(fx.truth):
                m = as_mesh(v, f)
                s = scanner.scan(m)
                with self.subTest(name=name, part=i):
                    self.assertEqual((s.open_edges, s.non_manifold, s.degenerate), (0, 0, 0))
                    self.assertEqual(scanner.winding_seams(m)[0], 0)
                    self.assertGreater(ds.signed_volume(v, f), 0)

    def test_disjoint_truths_sum_their_volumes(self):
        for name in ('correct', 'two_shells', 'shell_inverted', 'thin_rod', 'inch_scale'):
            fx = FIXTURES[name]
            with self.subTest(name):
                self.assertAlmostEqual(fx.truth_volume,
                                       sum(ds.signed_volume(v, f) for v, f in fx.truth), places=6)

    def test_overlapping_union_volume_matches_a_monte_carlo_estimate(self):
        import igl
        fx = FIXTURES['overlapping_shells']
        rng = np.random.default_rng(7)
        pts = np.vstack([v for v, _ in fx.truth]).astype(np.float64)
        lo, hi = pts.min(0), pts.max(0)
        q = lo + rng.random((200_000, 3)) * (hi - lo)
        inside = np.zeros(len(q), bool)
        for v, f in fx.truth:
            inside |= igl.fast_winding_number(np.asarray(v, np.float64), np.asarray(f, np.int64), q) > 0.5
        estimate = inside.mean() * np.prod(hi - lo)
        self.assertAlmostEqual(estimate / fx.truth_volume, 1.0, delta=0.01)


class TestOverlapHelper(unittest.TestCase):
    """Analytic boxes: the overlap volume is known exactly."""

    def test_disjoint_touching_and_overlapping_boxes(self):
        unit = ds.box((0, 0, 0), (1, 1, 1), n=1)[0]
        for shift, expected in ((2.0, 0.0), (1.0, 0.0), (0.5, 0.5), (0.0, 1.0)):
            other = (unit + np.float32([shift, 0, 0])).astype(np.float32)
            with self.subTest(shift=shift):
                self.assertAlmostEqual(ds.convex_overlap_volume(unit, other), expected, places=9)

    def test_union_of_overlapping_boxes(self):
        a, b = ds.box((0, 0, 0), (2, 2, 2)), ds.box((1, 0, 0), (3, 2, 2))
        self.assertAlmostEqual(ds.union_volume([a, b]), 12.0, places=9)


class TestUnionBoundary(unittest.TestCase):

    def test_overlapping_boxes_keep_exposed_faces_only(self):
        a, b = ds.box((0, 0, 0), (2, 2, 2)), ds.box((1, 0, 0), (3, 2, 2))
        fx = ds.Fixture('boxes', *a, (a, b), 12.0, (), {}, '')
        p, _ = ds.union_boundary_samples(fx, 4000)
        self.assertEqual(len(p), 4000)
        eps = 1e-6
        # A's face x = 2 lies inside B and B's face x = 1 inside A: buried.
        on_buried = ((np.abs(p[:, 0] - 2) < eps) | (np.abs(p[:, 0] - 1) < eps)) & \
                    (p[:, 1] > eps) & (p[:, 1] < 2 - eps) & (p[:, 2] > eps) & (p[:, 2] < 2 - eps)
        self.assertFalse(on_buried.any())
        # Exposed end faces are sampled.
        self.assertTrue((np.abs(p[:, 0]) < eps).any())
        self.assertTrue((np.abs(p[:, 0] - 3) < eps).any())

    def test_touching_boxes_exclude_the_shared_face(self):
        p, _ = ds.union_boundary_samples(FIXTURES['touching_shells'], 3000)
        shared = (np.abs(p[:, 0] - 10) < 1e-6) & (p[:, 1] > 1e-6) & (p[:, 1] < 10 - 1e-6) \
            & (p[:, 2] > 1e-6) & (p[:, 2] < 10 - 1e-6)
        self.assertFalse(shared.any())

    def test_samples_are_area_weighted_across_parts(self):
        """Symmetric touching boxes: half the samples on each (no bias
        toward the first part when rejection needs extra rounds)."""
        for seed in (0, 1, 2):
            p, _ = ds.union_boundary_samples(FIXTURES['touching_shells'], 10000, seed=seed)
            with self.subTest(seed=seed):
                self.assertAlmostEqual((p[:, 0] < 10).mean(), 0.5, delta=0.02)

    def test_overlapping_spheres_keep_no_point_inside_the_other(self):
        p, _ = ds.union_boundary_samples(FIXTURES['overlapping_shells'], 3000)
        # Every face of this UV sphere is at least ~9.85 from its centre, so a
        # point closer than 9.8 to the OTHER centre is buried inside it.
        d0 = np.linalg.norm(p, axis=1)
        d1 = np.linalg.norm(p - [12, 0, 0], axis=1)
        self.assertFalse(((d0 < 9.8) | (d1 < 9.8)).any())


class TestGeometricFacts(unittest.TestCase):

    def test_vanish_boxes_hold_debris_and_no_truth(self):
        for name, fx in FIXTURES.items():
            for lo, hi in fx.vanish_boxes:
                with self.subTest(name):
                    inside = lambda v: np.all((np.asarray(v) >= lo) & (np.asarray(v) <= hi), axis=1)
                    self.assertTrue(inside(fx.verts).any(), 'debris must be in its box')
                    for v, _ in fx.truth:
                        self.assertFalse(inside(v).any(), 'truth must stay outside')

    def test_overlap_touching_doubles_rod_and_scale(self):
        ov = FIXTURES['overlapping_shells']
        self.assertGreater(ds.convex_overlap_volume(ov.truth[0][0], ov.truth[1][0]), 0)
        tb = FIXTURES['touching_shells']
        on_plane = [np.abs(v[:, 0] - 10) < 1e-6 for v, _ in tb.truth]
        self.assertTrue(all(m.sum() >= 4 for m in on_plane))
        self.assertEqual(ds.convex_overlap_volume(tb.truth[0][0], tb.truth[1][0]), 0.0)
        db = FIXTURES['doubles']
        half = len(db.verts) // 2
        np.testing.assert_allclose(db.verts[half:] - db.verts[:half], [[1e-5, 0, 0]] * half, atol=2e-6)
        rod = FIXTURES['thin_rod']
        top = rod.verts[np.abs(rod.verts[:, 2] - 18) < 1e-6]
        self.assertAlmostEqual(float(rod.verts[:, 2].max()), 18.0, places=5)
        np.testing.assert_allclose(np.linalg.norm(top[:, :2], axis=1)[np.linalg.norm(top[:, :2], axis=1) > 0],
                                   0.75, atol=1e-5)
        used = np.unique(rod.faces)
        self.assertEqual(len(used), len(rod.verts), 'no unused vertex (the removed pole)')
        inch = FIXTURES['inch_scale']
        np.testing.assert_allclose(np.ptp(inch.verts, 0), np.ptp(FIXTURES['correct'].verts, 0) / 25.4, rtol=1e-6)


class TestComposites(unittest.TestCase):
    """The end-to-end inputs, checked cheaply before any real run."""

    COMPOSITES = ds.composites()

    def test_declared_defects_truth_and_vanish_boxes(self):
        for name, fx in self.COMPOSITES.items():
            m = as_mesh(fx.verts, fx.faces)
            s = scanner.scan(m)
            d = fx.defects
            with self.subTest(name):
                self.assertEqual(len(scanner.shells(m)), d['shells'])
                if 'open' in d:
                    self.assertEqual(s.open_edges > 0, d['open'])
                if 'non_manifold' in d:
                    self.assertEqual(s.non_manifold > 0, d['non_manifold'])
                if 'seams' in d:
                    self.assertEqual(scanner.winding_seams(m)[0] > 0, d['seams'])
                self.assertEqual(len(d['sag']), len(fx.truth))
                for (v, f), sag in zip(fx.truth, d['sag']):
                    ts = scanner.scan(as_mesh(v, f))
                    self.assertEqual((ts.open_edges, ts.non_manifold), (0, 0))
                    self.assertGreater(ds.signed_volume(v, f), 0)
                    self.assertGreater(sag, 0)
                for lo, hi in fx.vanish_boxes:
                    inside = lambda vv: np.all((np.asarray(vv) >= lo) & (np.asarray(vv) <= hi), axis=1)
                    self.assertTrue(inside(fx.verts).any())
                    self.assertFalse(any(inside(v).any() for v, _ in fx.truth))

    def test_parts_kept_after_an_stl_round_trip(self):
        """The part ids the batch log must show: shells kept by the
        splitter after the mesh is written and read back as STL."""
        from libs import mesh_io, splitter
        tmp = tempfile.mkdtemp(prefix='composite-')
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, fx in self.COMPOSITES.items():
            path = os.path.join(tmp, name + '.stl')
            mesh_io.write(Mesh(path, path, Kind.BINARY_STL, len(fx.faces), True, None,
                               Geometry(fx.verts.astype(np.float64), fx.faces)))
            kept = splitter.by_shells(mesh_io.load(mesh_io.probe(path, path)))
            with self.subTest(name):
                n = len(kept)
                self.assertEqual(fx.defects['parts'], tuple(f'{i}/{n}' for i in range(1, n + 1)))

    def test_rod_tip_lies_on_the_truth_and_the_rod_is_resolvable(self):
        fx = self.COMPOSITES['c_multishell_rod']
        tip = np.asarray(fx.defects['rod_tip'])
        rod_v = fx.truth[2][0]
        self.assertAlmostEqual(float(rod_v[:, 2].max()), tip[2], places=5)
        cap = rod_v[np.abs(rod_v[:, 2] - tip[2]) < 1e-6]
        np.testing.assert_allclose(cap.mean(0), tip, atol=1e-5)

    def test_inch_composite_scales_everything(self):
        fx, ov = self.COMPOSITES['c_inch_overlap'], FIXTURES['overlapping_shells']
        np.testing.assert_allclose(fx.verts, ov.verts / 25.4, rtol=1e-6, atol=1e-6)
        self.assertAlmostEqual(fx.truth_volume, ov.truth_volume / 25.4 ** 3, places=6)


class TestProbeCompatibility(unittest.TestCase):

    def test_committed_probes_are_reproduced_byte_for_byte(self):
        out = tempfile.mkdtemp(prefix='probes-')
        self.addCleanup(shutil.rmtree, out, True)
        subprocess.run([sys.executable, str(PROJECT / 'tools' / 'make_probe_meshes.py'), out],
                       cwd=PROJECT, check=True, capture_output=True)
        for probe in sorted((PROJECT / 'tests' / 'probes').glob('sphere_*.stl')):
            with self.subTest(probe.name):
                self.assertEqual(probe.read_bytes(), (Path(out) / probe.name).read_bytes())

    def test_both_import_styles_still_work(self):
        from tools.make_probe_meshes import build_tjunction, sphere
        self.assertIs(sphere, ds.sphere)
        sys.path.insert(0, str(PROJECT / 'tools'))
        try:
            import make_probe_meshes
            self.assertIs(make_probe_meshes.build_tjunction, ds.build_tjunction)
        finally:
            sys.path.remove(str(PROJECT / 'tools'))


if __name__ == '__main__':
    unittest.main()
