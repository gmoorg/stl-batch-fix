"""Tests for libs.welder — T-junction repair by face splitting.

Fixtures are built by hand, as elsewhere in this suite: a T-junction is made by
splitting one face at an edge midpoint and leaving its neighbour alone, so the
expected counts are derivable without running anything — one junction means one
extra face, three open edges before and none after.

The property that matters most is negative: **no vertex is ever lost**. That is
what separates this repair from the hole-closing ones. PyMeshFix and Blender
reach comparable face counts on the same input while deleting 2 and 118
vertices respectively.
"""

import unittest

import numpy as np

from libs import scanner, welder
from libs.mesh_io import Geometry, Kind, Mesh
from libs.welder import Result, TJunction, find, repair

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts, faces, path='/in/body.stl'):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(path, '/out/body.stl', Kind.BINARY_STL, len(geometry.faces),
                True, None, geometry)


def tetra():
    return mesh(TETRA_VERTS, TETRA_FACES)


def with_tjunction(at=0.5):
    """A tetrahedron with one face split at a point on edge (1, 2).

    Face 0 is `[0, 2, 1]` and face 3 is `[1, 2, 3]`; both use edge 1-2. Split
    face 3 at a point along that edge and leave face 0 spanning it whole, and
    face 0 is the T-junction's victim.
    """
    verts = [list(v) for v in TETRA_VERTS]
    a, b = 1, 2
    point = [verts[a][i] + at * (verts[b][i] - verts[a][i]) for i in range(3)]
    verts.append(point)
    m = len(verts) - 1
    faces = [list(f) for f in TETRA_FACES]
    faces[3] = [a, m, 3]
    faces.append([m, b, 3])
    return mesh(verts, faces), m


def vertex_set(m):
    return {tuple(np.round(p, 5)) for p in m.geometry.verts.tolist()}


class TestFind(unittest.TestCase):

    def test_a_clean_mesh_has_none(self):
        self.assertEqual(find(tetra()), ())

    def test_one_junction_is_found(self):
        m, expected_vertex = with_tjunction()
        found = find(m)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].vertex, expected_vertex)

    def test_it_reports_where_on_the_edge(self):
        m, _ = with_tjunction(at=0.25)
        self.assertAlmostEqual(find(m)[0].position, 0.25, places=5)

    def test_the_distance_is_essentially_zero(self):
        """A constructed junction sits exactly on the edge. The fixture's real
        counterpart measured 2.6e-23."""
        m, _ = with_tjunction()
        self.assertLess(find(m)[0].distance, 1e-9)

    def test_the_reported_face_is_the_one_spanning_the_edge(self):
        """The victim is the neighbour that was never split — the face still
        using the full-length edge."""
        m, _ = with_tjunction()
        junction = find(m)[0]
        triangle = m.geometry.faces[junction.face].tolist()
        self.assertIn(junction.edge[0], triangle)
        self.assertIn(junction.edge[1], triangle)
        self.assertNotIn(junction.vertex, triangle)

    def test_a_vertex_at_an_endpoint_is_not_a_junction(self):
        """Every vertex lies on the edges it belongs to; only interior
        positions count."""
        self.assertEqual(find(tetra()), ())

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            find(Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 4, True))


class TestRepair(unittest.TestCase):

    def test_one_junction_costs_exactly_one_face(self):
        m, _ = with_tjunction()
        result = repair(m)
        self.assertEqual(result.splits, 1)
        self.assertEqual(result.mesh.triangles, m.triangles + 1)

    def test_the_open_edges_close(self):
        m, _ = with_tjunction()
        self.assertGreater(scanner.scan(m).open_edges, 0)
        self.assertEqual(scanner.scan(repair(m).mesh).open_edges, 0)

    def test_no_vertex_is_lost(self):
        """The property that distinguishes this from hole-closing repairs.

        On the 150-junction fixture PyMeshFix loses 2 vertices and Blender 118,
        reaching comparable face counts by deleting geometry. This moves
        nothing and deletes nothing.
        """
        m, _ = with_tjunction()
        result = repair(m)
        self.assertEqual(vertex_set(m) - vertex_set(result.mesh), set())

    def test_the_vertex_array_is_passed_through_untouched(self):
        m, _ = with_tjunction()
        result = repair(m)
        self.assertIs(result.mesh.geometry.verts, m.geometry.verts)

    def test_winding_is_preserved(self):
        """Rebuilding from the edge's sorted (low, high) form would reverse
        half the faces; the split walks the triangle's own corner order."""
        m, _ = with_tjunction()
        result = repair(m)
        self.assertEqual(scanner.winding_seams(result.mesh), (0, 0))
        self.assertGreater(scanner.volume(result.mesh), 0)

    def test_volume_is_unchanged(self):
        """Splitting a face coplanar with itself encloses nothing new."""
        m, _ = with_tjunction()
        self.assertAlmostEqual(scanner.volume(repair(m).mesh),
                               scanner.volume(m), places=5)

    def test_a_clean_mesh_comes_back_untouched(self):
        m = tetra()
        result = repair(m)
        self.assertEqual(result.splits, 0)
        self.assertIs(result.mesh, m, "it rebuilt a mesh with nothing to do")
        self.assertFalse(result.repaired)

    def test_it_converges(self):
        """Splitting changes the edge map, so the search repeats — but the
        final round must find nothing rather than run to the cap."""
        m, _ = with_tjunction()
        self.assertLess(repair(m).rounds, welder.MAX_ROUNDS)

    def test_a_junction_off_the_edge_beyond_tolerance_is_ignored(self):
        """A vertex merely passing near an edge must not split it."""
        verts = [list(v) for v in TETRA_VERTS]
        verts.append([0.5, 0.5, 0.2])          # near edge (1,2), well off it
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [1, 4, 3]
        faces.append([4, 2, 3])
        m = mesh(verts, faces)
        self.assertEqual(find(m, tolerance=1e-6), ())

    def test_tolerance_is_adjustable(self):
        """The default is not calibrated on real data; a caller must be able
        to widen it for junctions arriving from a boolean or a decimation."""
        verts = [list(v) for v in TETRA_VERTS]
        verts.append([0.5, 0.5, 0.001])        # 1 micron off the edge
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [1, 4, 3]
        faces.append([4, 2, 3])
        m = mesh(verts, faces)
        self.assertEqual(find(m, tolerance=1e-6), ())
        self.assertEqual(len(find(m, tolerance=1e-2)), 1)

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            repair(Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 4, True))


class TestManyJunctions(unittest.TestCase):
    """The case the scratch implementation was validated on.

    A strip subdivided at many points, each neighbour left unsplit. Measured on
    the real fixture: 150 junctions, 910f to 1060f, 2 rounds, 0 vertices lost,
    and the same 1060 faces a commercial repair service produced independently.
    """

    def strip(self, n_quads=40, n_junctions=10):
        verts, faces = [], []
        for i in range(n_quads + 1):
            verts += [[i, 0, 0], [i, 1, 0]]
        for i in range(n_quads):
            a, b, c, d = 2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 3
            faces += [[a, b, c], [b, d, c]]
        # Skip face 0: its edge (0,1) is the strip's leftmost boundary, used
        # by no other face. Splitting there creates a vertex nobody fails to
        # reference — not a T-junction, and the module is right to ignore it.
        for k in range(1, n_junctions + 1):
            index = 2 * k
            a, b, c = faces[index]
            verts.append([(verts[a][i] + verts[b][i]) / 2 for i in range(3)])
            m = len(verts) - 1
            faces[index] = [a, m, c]
            faces.append([m, b, c])
        return mesh(verts, faces), n_junctions

    def test_every_junction_is_found(self):
        m, expected = self.strip()
        self.assertEqual(len(find(m)), expected)

    def test_all_are_repaired_in_one_call(self):
        m, expected = self.strip()
        result = repair(m)
        self.assertEqual(result.splits, expected)
        self.assertEqual(result.mesh.triangles, m.triangles + expected)

    def test_none_remain(self):
        m, _ = self.strip()
        self.assertEqual(find(repair(m).mesh), ())

    def test_no_vertex_is_lost_at_scale(self):
        m, _ = self.strip()
        self.assertEqual(vertex_set(m) - vertex_set(repair(m).mesh), set())

    def test_it_is_idempotent(self):
        """A rerun must cost one search and change nothing — the pipeline
        reruns files."""
        m, _ = self.strip()
        once = repair(m).mesh
        twice = repair(once)
        self.assertEqual(twice.splits, 0)
        self.assertIs(twice.mesh, once)


class TestResult(unittest.TestCase):

    def test_result_is_immutable(self):
        result = Result(tetra(), 1, 2)
        with self.assertRaises(Exception):
            result.splits = 9

    def test_tjunction_is_immutable(self):
        junction = TJunction(1, (2, 3), 4, 0.5, 0.0)
        with self.assertRaises(Exception):
            junction.vertex = 9


if __name__ == '__main__':
    unittest.main(verbosity=2)
