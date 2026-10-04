"""Tests for libs.decimator — the required decimator contract."""

import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import libs.decimator as decimator
from libs.decimator import Result, Rung, available_rungs, decimate, is_available
from libs.mesh_io import Geometry, Kind, Mesh, load, probe


def _sphere(subdivisions=4):
    """An icosphere as (verts, faces) — a real closed mesh, cheaply generated.

    A tetrahedron is too small to decimate meaningfully; this gives a few
    thousand faces with genuine connectivity for edge collapse to work on.
    """
    t = (1.0 + 5.0 ** 0.5) / 2.0
    verts = np.array([
        [-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0],
        [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t],
        [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1],
    ], dtype=np.float64)
    faces = np.array([
        [0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11],
        [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8],
        [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9],
        [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1],
    ], dtype=np.int64)

    for _ in range(subdivisions):
        midpoint = {}
        new_faces = []
        verts = list(verts)

        def mid(a, b):
            key = (min(a, b), max(a, b))
            if key not in midpoint:
                m = (np.asarray(verts[a]) + np.asarray(verts[b])) / 2.0
                m = m / np.linalg.norm(m) * np.linalg.norm(verts[a])
                verts.append(m)
                midpoint[key] = len(verts) - 1
            return midpoint[key]

        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            new_faces += [[a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]]
        verts = np.array(verts, dtype=np.float64)
        faces = np.array(new_faces, dtype=np.int64)

    return verts.astype(np.float32), faces


def _write_stl(path, verts, faces):
    n = len(faces)
    tv = verts[faces].astype(np.float32)
    buf = np.zeros((n, 50), dtype=np.uint8)
    buf[:, 12:48] = tv.reshape(n, 9).view(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', n))
        f.write(buf.tobytes())
    return path


class DecimatorCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.verts, cls.faces = _sphere(subdivisions=4)   # 5120 faces

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='decimator-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def loaded(self):
        """A loaded Mesh built from the shared sphere."""
        path = _write_stl(os.path.join(self.dir, 'sphere.stl'),
                          self.verts, self.faces)
        return load(probe(path, path + '.out'))

    def probed(self):
        path = _write_stl(os.path.join(self.dir, 'sphere.stl'),
                          self.verts, self.faces)
        return probe(path, path + '.out')


class TestAvailability(DecimatorCase):

    def test_is_available_when_a_library_is_present(self):
        self.assertTrue(is_available())

    def test_is_available_is_false_with_nothing_at_all(self):
        """Decimation is a deliverable: a false here means do not start.

        The required decimator missing means the run cannot produce its
        deliverable at all.
        """
        with mock.patch.object(decimator.meshlab, 'is_available', return_value=False):
            self.assertFalse(is_available())

    def test_available_rungs_reports_what_is_missing(self):
        with mock.patch.object(decimator.meshlab, 'is_available', return_value=False):
            self.assertEqual(available_rungs(), ())

    def test_there_is_no_blender_rung(self):
        """There is no alternate decimation rung."""
        self.assertFalse(hasattr(Rung, 'BLENDER'))
        self.assertNotIn('blender', [r.value for r in available_rungs()])


class TestNotNeeded(DecimatorCase):

    def test_a_mesh_within_budget_is_returned_untouched(self):
        mesh = self.loaded()
        result = decimate(mesh, max_faces=999_999)
        self.assertIs(result.rung, Rung.NOT_NEEDED)
        self.assertIs(result.mesh, mesh, "it rebuilt a mesh with nothing to do")
        self.assertFalse(result.ran)

    def test_zero_disables_decimation(self):
        """MAX_FACES = 0 meant disabled in the old script; keep that."""
        result = decimate(self.loaded(), max_faces=0)
        self.assertIs(result.rung, Rung.NOT_NEEDED)

    def test_an_unloaded_mesh_raises(self):
        """A programming error at the call site, not a property of the data."""
        with self.assertRaises(ValueError):
            decimate(self.probed(), max_faces=100)


class TestMeshLab(DecimatorCase):

    def test_it_reduces_to_about_the_target(self):
        mesh = self.loaded()
        before = mesh.triangles
        result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.MESHLAB)
        self.assertLess(result.faces_out, before)
        # PyMeshLab's quadric collapse reaches the target exactly here.
        self.assertEqual(result.faces_out, 1000)

    def test_the_result_is_a_loaded_mesh(self):
        """Mesh in, mesh out — the caller can keep working in memory."""
        result = decimate(self.loaded(), max_faces=1000)
        self.assertTrue(result.mesh.is_loaded)
        self.assertEqual(result.mesh.triangles, len(result.mesh.geometry.faces))

    def test_the_input_mesh_is_not_mutated(self):
        mesh = self.loaded()
        before = mesh.triangles
        decimate(mesh, max_faces=1000)
        self.assertEqual(mesh.triangles, before)
        self.assertEqual(len(mesh.geometry.faces), before)

    def test_the_path_is_carried_through(self):
        mesh = self.loaded()
        result = decimate(mesh, max_faces=1000)
        self.assertEqual(result.mesh.path, mesh.path)

    def test_faces_index_within_the_vertex_array(self):
        """A decimator that returned stale indices would write garbage."""
        result = decimate(self.loaded(), max_faces=1000)
        g = result.mesh.geometry
        self.assertGreaterEqual(int(g.faces.min()), 0)
        self.assertLess(int(g.faces.max()), len(g.verts))

    def test_no_files_are_written(self):
        """The common path must not touch the disk at all."""
        mesh = self.loaded()                    # the fixture's own file first
        before = set(os.listdir(self.dir))
        decimate(mesh, max_faces=1000)
        self.assertEqual(set(os.listdir(self.dir)), before)


class TestMeshLabMissing(DecimatorCase):

    def test_missing_pymeshlab_is_a_failure(self):
        mesh = self.loaded()
        with mock.patch.object(decimator.meshlab, 'is_available', return_value=False):
            result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.FAILED)
        self.assertIs(result.mesh, mesh)
        self.assertIn('not installed', result.attempts[0][1])




class TestMeshLabFailureResult(DecimatorCase):

    def test_the_input_is_returned_unchanged(self):
        """Decimation is a deliverable: the caller must be able to tell
        'not decimated' from 'decimated badly' and mark the file."""
        mesh = self.loaded()
        with mock.patch.object(decimator, '_decimate_meshlab',
                       side_effect=RuntimeError("boom")):
            result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.FAILED)
        self.assertIs(result.mesh, mesh)
        self.assertFalse(result.ran)
        self.assertEqual(result.faces_out, result.faces_in)

    def test_failure_is_recorded(self):
        """The failure explains why the undecimated marker is needed."""
        with mock.patch.object(decimator, '_decimate_meshlab',
                               side_effect=RuntimeError("boom")):
            result = decimate(self.loaded(), max_faces=1000)
        self.assertEqual([r for r, _ in result.attempts],
                         [Rung.MESHLAB])
        self.assertIn('boom', result.attempts[0][1])


class TestShapeIsKept(unittest.TestCase):
    """Outcomes, not internals: the decimated surface stays on the input
    shape. fast_simplification failed both of these (owner decision
    2026-10-04, docs/refactor/reconstruction.md)."""

    def test_a_sphere_stays_round(self):
        """fast_simplification made spheres oblong. Every vertex of the
        icosphere lies on radius R = sqrt(1 + t^2); the decimated one must
        stay within 1% of R and keep equal extents (a fixture-specific
        tolerance, not a general accuracy bound)."""
        verts, faces = _sphere(subdivisions=5)          # 20480 faces
        mesh = Mesh('/s', '/s', Kind.BINARY_STL, len(faces), True, None,
                    Geometry(verts, faces))
        result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.MESHLAB)
        R = float(np.sqrt(1 + ((1 + 5 ** 0.5) / 2) ** 2))
        v = result.mesh.geometry.verts.astype(np.float64)
        self.assertLessEqual(np.abs(np.linalg.norm(v, axis=1) - R).max(), 0.01 * R)
        extent = np.ptp(v, axis=0)
        self.assertLessEqual(extent.max() / extent.min(), 1.01)

    def test_a_rebuilt_thin_rod_keeps_its_tip(self):
        """The e2e failure: after reconstruction the part is decimated back
        to its own face count, and fast_simplification pulled the rod tip
        7.8 units down. The tip must stay within 2h of the truth."""
        from libs import winding
        from tests.tests import defect_spheres as ds
        if not winding.is_available():
            self.skipTest('libigl is needed')
        import igl
        verts, faces = ds.sphere_with_rod()
        h = 0.2
        rebuilt = winding.reconstruct(
            Mesh('/r', '/r', Kind.BINARY_STL, len(faces), True, None,
                 Geometry(verts, faces)), h, 1)
        result = decimate(rebuilt, max_faces=len(faces))
        self.assertIs(result.rung, Rung.MESHLAB)
        g = result.mesh.geometry
        tip = np.array([[0.0, 0.0, 18.0]])
        d = np.sqrt(igl.point_mesh_squared_distance(
            tip, g.verts.astype(np.float64), g.faces.astype(np.int64))[0][0])
        self.assertLessEqual(d, 2 * h)


if __name__ == '__main__':
    unittest.main(verbosity=2)
