"""Tests for libs.decimator — the required decimator contract."""

import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import libs.decimator as decimator
from libs.decimator import Result, Rung, decimate
from libs import mesh_io
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


class TestRungs(unittest.TestCase):

    def test_there_is_no_blender_rung(self):
        """There is no alternate decimation rung."""
        self.assertFalse(hasattr(Rung, 'BLENDER'))


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


#: Runs in a child interpreter: a segfault there fails one test instead of
#: killing the whole runner. `case` builds `mesh` and sets `target`; the
#: child prints what the tests assert on as one JSON line.
_CHILD = """
import json, sys
import numpy as np
from libs import decimator, scanner
from libs.mesh_io import Geometry, Kind, Mesh, load, probe

def arrays(verts, faces):
    g = Geometry(np.asarray(verts, np.float32), np.asarray(faces, np.int64))
    return Mesh('/in/x.stl', '/out/x.stl', Kind.BINARY_STL, len(g.faces), True, None, g)

def run(mesh, target):
    verts, faces = mesh.geometry.verts.copy(), mesh.geometry.faces.copy()
    r = decimator.decimate(mesh, target)
    g = r.mesh.geometry
    return dict(
        faces_in=len(faces),
        degenerate_in=int(scanner.degenerate_mask(faces).sum()),
        rung=r.rung.value, faces_out=len(g.faces),
        degenerate_out=int(scanner.degenerate_mask(g.faces).sum()),
        input_unchanged=bool(np.array_equal(mesh.geometry.verts, verts)
                             and np.array_equal(mesh.geometry.faces, faces)),
        input_returned=r.mesh is mesh,
        verts=g.verts.tolist(), faces=g.faces.tolist())

{case}
print(json.dumps(out))
"""

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Tetrahedron away from the origin plus one isolated [P, Q, P] face on two
#: new vertices: the 5-face synthetic that segfaulted the array path.
_TETRA_ISOLATED = """
T = [[10, 0, 0], [11, 0, 0], [10, 1, 0], [10, 0, 1], [0, 0, 0], [0.05, 0, 0]]
F = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3], [4, 5, 4]]
"""


class TestDegenerateFacesDoNotCrash(unittest.TestCase):
    """Faces with a repeated corner index segfaulted PyMeshLab's quadric
    decimation on the array path (seven batch models; evidence in
    docs/errors/decimation-segfault.md). Every case runs in a child process,
    so a regression shows up as a failed test, not a dead runner."""

    def child(self, case):
        import json
        import subprocess
        import sys
        env = dict(os.environ, PYTHONPATH=_ROOT)
        proc = subprocess.run(
            [sys.executable, '-X', 'faulthandler', '-c', _CHILD.format(case=case)],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"child exited {proc.returncode}:\n{proc.stderr[-2000:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def assert_decimated_cleanly(self, out):
        self.assertEqual(out['rung'], 'meshlab')
        self.assertEqual(out['degenerate_out'], 0)
        self.assertTrue(out['input_unchanged'], 'the caller\'s arrays changed')

    def test_the_real_crash_crop_decimates(self):
        """16 faces cropped from Base_Pillar_R, one of them [6, 9, 6]."""
        out = self.child(
            "out = run(load(probe('tests/probes/segv_min16.stl', '/nonexistent/out.stl')), 8)")
        # Guard the fixture: a cleaned copy would no longer test anything.
        self.assertEqual(out['faces_in'], 16)
        self.assertEqual(out['degenerate_in'], 1)
        self.assert_decimated_cleanly(out)
        self.assertLessEqual(out['faces_out'], 8)

    def test_an_isolated_degenerate_face_decimates(self):
        out = self.child(_TETRA_ISOLATED + "out = run(arrays(T, F), 2)")
        self.assertEqual(out['degenerate_in'], 1)
        self.assert_decimated_cleanly(out)

    def test_the_result_matches_decimating_without_the_face(self):
        """Dropping the face is lossless: the result is exactly what the same
        mesh gives with that face removed by hand (its two vertices kept)."""
        out = self.child("""
from tests.tests.test_decimator import _sphere
v, f = _sphere(subdivisions=4)
n = len(v)
V = np.vstack([v, [[0, 0, 0], [0.05, 0, 0]]])
with_face = run(arrays(V, np.vstack([f, [[n, n + 1, n]]])), 1000)
by_hand = run(arrays(V, f), 1000)
out = dict(with_face, same=(with_face['verts'] == by_hand['verts']
                            and with_face['faces'] == by_hand['faces']))
""")
        self.assert_decimated_cleanly(out)
        self.assertEqual(out['faces_out'], 1000)
        self.assertTrue(out['same'], 'dropping the face changed the result')

    def test_an_all_degenerate_mesh_fails_without_crashing(self):
        """Nothing is left once the faces are dropped; PyMeshLab refuses an
        empty mesh, and that must come back as an ordinary failure."""
        out = self.child("out = run(arrays([[0, 0, 0], [1, 0, 0], [0, 1, 0]],"
                         " [[0, 1, 0], [1, 2, 2]]), 1)")
        self.assertEqual(out['rung'], 'failed')
        self.assertTrue(out['input_returned'])
        self.assertTrue(out['input_unchanged'])

    def test_a_mesh_within_budget_keeps_its_degenerate_faces(self):
        """Dropping happens only on the way into PyMeshLab; a mesh that needs
        no decimation is returned exactly as it came."""
        verts = np.array([[10, 0, 0], [11, 0, 0], [10, 1, 0], [10, 0, 1],
                          [0, 0, 0], [0.05, 0, 0]], np.float32)
        faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3], [4, 5, 4]])
        mesh = Mesh('/in/x.stl', '/out/x.stl', Kind.BINARY_STL, 5, True, None,
                    Geometry(verts, faces))
        result = decimate(mesh, max_faces=10)
        self.assertIs(result.rung, Rung.NOT_NEEDED)
        self.assertIs(result.mesh, mesh)
        self.assertEqual(len(result.mesh.geometry.faces), 5)


_CONTAMINATION = """
import hashlib
import numpy as np
from libs import decimator, meshlab
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests.test_decimator import _sphere
v, f = _sphere(subdivisions=4)
m = Mesh('/s', '/s', Kind.BINARY_STL, len(f), True, None, Geometry(v, f))
if {contaminate}:
    meshlab.apply_filters(m, (('meshing_decimation_quadric_edge_collapse',
                               dict(targetfacenum=3000, preservetopology=True,
                                    optimalplacement=False, planarquadric=False)),))
g = decimator.decimate(m, 1000).mesh.geometry
print(hashlib.sha1(g.verts.tobytes() + g.faces.tobytes()).hexdigest())
"""


class TestEarlierCallsDoNotLeakIn(unittest.TestCase):
    """PyMeshLab keeps a filter's parameters from its previous call in the
    process. The decimator passes every parameter, so what ran before it in
    the same process cannot change its result."""

    def digest(self, contaminate):
        import subprocess
        import sys
        proc = subprocess.run(
            [sys.executable, '-c', _CONTAMINATION.format(contaminate=contaminate)],
            cwd=_ROOT, env=dict(os.environ, PYTHONPATH=_ROOT),
            capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc.stdout.strip()

    def test_a_previous_call_with_other_flags_changes_nothing(self):
        self.assertEqual(self.digest(True), self.digest(False))


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
