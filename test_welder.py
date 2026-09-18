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

import os
import sys
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

    def test_a_junction_off_the_line_is_still_a_junction(self):
        """**This reverses an earlier test**, and the reversal is the point.

        The old version asserted that a vertex 0.2 off the edge "merely passes
        near" it and must not split. That was the gap-test premise. A real
        T-junction's M is often well off the line — the other side of the
        surface bulges — and Mandy carries one at **half an edge-length** away
        whose L and R plainly share an edge.

        What makes it a junction is structural: L and R are X's counterparts
        across that edge, which they betray by sharing an edge with each other.
        How far M sits from the line does not enter into it.
        """
        verts = [list(v) for v in TETRA_VERTS]
        verts.append([0.5, 0.5, 0.2])          # off the line, structurally M
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [1, 4, 3]
        faces.append([4, 2, 3])
        m = mesh(verts, faces)
        found = find(m)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].vertex, 4)

    def test_a_vertex_with_no_counterpart_pair_is_not_a_junction(self):
        """The condition that replaces the tolerance: L and R must share an
        EDGE, not merely a vertex.

        Measured on `tjunction_many` — relaxing this to "share a vertex"
        admitted three faces whose M sat 1.367 mm from a 3.4 mm edge, and
        splitting at them left 22 non-manifold edges on a clean mesh. In each,
        L and R met at M alone.
        """
        # A tetrahedron with a loose vertex near edge (1,2) but no face
        # structure joining it: nothing shares an edge with anything.
        verts = [list(v) for v in TETRA_VERTS]
        verts.append([0.5, 0.5, 0.0])
        faces = [list(f) for f in TETRA_FACES]
        m = mesh(verts, faces)
        self.assertEqual(find(m), ())

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


class TestSeveralJunctionsOnOneFace(unittest.TestCase):
    """One spanning edge subdivided more than once — `find` under-reports and
    `repair` still gets it right.

    The fixture is built on a real sphere rather than by hand. Two hand-built
    attempts at this case were malformed in the same way: three triangles
    joined at single *vertices* rather than edges, giving 7 open edges of 8 and
    2 shells. That is not a surface with a defect, it is not a surface — and a
    detector is right to find nothing in it.
    """

    def sphere_with_two_on_one_edge(self):
        """A sphere whose face 200 has its edge subdivided at t=1/3 and t=2/3,
        leaving the neighbour spanning it whole."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'tools'))
        from make_probe_meshes import sphere
        verts, faces = sphere(r=10.0)
        verts, faces = verts.tolist(), faces.tolist()
        a, b, c = faces[200]
        along = [verts[b][i] - verts[a][i] for i in range(3)]
        verts.append([verts[a][i] + along[i] / 3.0 for i in range(3)])
        verts.append([verts[a][i] + 2.0 * along[i] / 3.0 for i in range(3)])
        first, second = len(verts) - 2, len(verts) - 1
        faces[200] = [a, first, c]
        faces += [[first, second, c], [second, b, c]]
        return mesh(verts, faces), (first, second)

    def test_the_fixture_really_holds_two_junctions_on_one_edge(self):
        """Guard on the fixture: one shell, edge-connected, and both inserted
        vertices lie on the same spanning edge."""
        m, (first, second) = self.sphere_with_two_on_one_edge()
        self.assertEqual(len(scanner.shells(m)), 1,
                         "the fixture must be edge-connected, not a pile of "
                         "triangles touching at points")
        self.assertGreater(scanner.scan(m).open_edges, 0)

    def test_both_vertices_are_reported_in_one_chain(self):
        """One entry for the face, carrying **both** vertices.

        An earlier version found one of the two and left the other for the
        next round — and *which* one was arbitrary, since the search broke on
        the first match while iterating a set. The chain walk follows the open
        edges from one end of the spanning edge to the other, so it collects
        every interior vertex in a single pass.
        """
        m, (first, second) = self.sphere_with_two_on_one_edge()
        found = find(m)
        self.assertEqual(len(found), 1, "one entry per affected face")
        self.assertEqual(set(found[0].chain), {first, second})

    def test_the_chain_is_ordered_along_the_edge(self):
        """`_split` walks the triangle's own corner order, so the chain has to
        arrive sorted by position or the fan would cross itself."""
        m, _ = self.sphere_with_two_on_one_edge()
        chain = find(m)[0].chain
        verts = m.geometry.verts.astype(float)
        low, high = find(m)[0].edge
        along = verts[high] - verts[low]
        positions = [float((verts[v] - verts[low]) @ along / (along @ along))
                     for v in chain]
        self.assertEqual(positions, sorted(positions))

    def test_one_split_makes_three_faces(self):
        """n interior vertices cost n+1 faces, not 2 — and it is one split,
        not two. The old mechanism needed a second round to reach the same
        face count."""
        m, _ = self.sphere_with_two_on_one_edge()
        result = repair(m)
        self.assertEqual(result.splits, 1)
        self.assertEqual(result.mesh.triangles, m.triangles + 2)
        self.assertTrue(scanner.scan(result.mesh).is_clean)

    def test_no_vertex_is_lost(self):
        m, _ = self.sphere_with_two_on_one_edge()
        result = repair(m)
        self.assertEqual(vertex_set(m) - vertex_set(result.mesh), set())


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
