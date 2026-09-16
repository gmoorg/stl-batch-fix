"""Tests for libs.scanner — defect counting on meshes in memory.

Fixtures are built by hand from index arrays, not read from files: every
expected count here is derivable on paper, which is the point. A tetrahedron
has 4 faces and 6 edges each used twice; remove one face and exactly 3 edges
become open. If a test's expected number cannot be justified without running
the code, it is not testing anything.

`winding_seams` additionally carries a differential test against the original
dict-based implementation, kept verbatim here. The vectorised rewrite was wrong
on the first attempt — it raised on every mesh with a seam candidate — and the
differential check is what caught it, before any test encoded the bug.
"""

import unittest
from collections import defaultdict

import numpy as np

from libs import scanner
from libs.mesh_io import Geometry, Kind, Mesh
from libs.scanner import (
    Loop, Scan, largest_open_loop, open_loops, open_loops_are_printable, scan,
    shell_count, shells, winding_seams,
)

#: A unit tetrahedron, consistently wound: 4 faces, 6 edges, every edge shared.
TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts, faces, path='/test.stl'):
    """A loaded Mesh from plain lists."""
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(path, Kind.BINARY_STL, len(geometry.faces), True, None,
                geometry)


def tetra(faces=None):
    return mesh(TETRA_VERTS, TETRA_FACES if faces is None else faces)


def unloaded():
    return Mesh('/test.stl', Kind.BINARY_STL, 4, True)


class TestScan(unittest.TestCase):

    def test_a_closed_mesh_is_clean(self):
        """4 faces, 6 edges, each shared by exactly 2 — nothing wrong."""
        found = scan(tetra())
        self.assertEqual(found.open_edges, 0)
        self.assertEqual(found.non_manifold, 0)
        self.assertEqual(found.faces, 4)
        self.assertTrue(found.is_clean)

    def test_removing_a_face_opens_exactly_its_three_edges(self):
        found = scan(tetra(TETRA_FACES[:3]))
        self.assertEqual(found.open_edges, 3)
        self.assertEqual(found.non_manifold, 0)
        self.assertFalse(found.is_clean)

    def test_a_third_face_on_one_edge_is_non_manifold(self):
        """A fin: edge 0-1 now carries three faces, so the surface branches."""
        found = scan(mesh(TETRA_VERTS + [[1, 1, 1]],
                          TETRA_FACES + [[0, 1, 4]]))
        self.assertEqual(found.non_manifold, 1)

    def test_a_degenerate_face_is_counted(self):
        """Two identical corners: no area, no normal, nothing to print."""
        found = scan(mesh(TETRA_VERTS, [[0, 1, 1], [0, 2, 1]]))
        self.assertEqual(found.degenerate, 1)

    def test_an_empty_mesh_scans_clean_rather_than_crashing(self):
        found = scan(mesh(TETRA_VERTS, np.zeros((0, 3), dtype=np.int64)))
        self.assertEqual(found.faces, 0)
        self.assertTrue(found.is_clean)

    def test_total_defects_adds_both_kinds(self):
        found = scan(mesh(TETRA_VERTS + [[1, 1, 1]],
                          TETRA_FACES[:3] + [[0, 1, 4]]))
        self.assertEqual(found.total_defects,
                         found.open_edges + found.non_manifold)

    def test_an_unloaded_mesh_raises_rather_than_reporting_clean(self):
        """The bug _post_verify's three-valued return existed to kill.

        An unscannable mesh returning (0, 0) reads as verified-clean at every
        call site, and files were written out as finished having been checked
        by nothing. Here the case cannot be represented.
        """
        with self.assertRaises(ValueError):
            scan(unloaded())

    def test_there_is_no_triangle_ceiling(self):
        """scan_mesh_errors gave up above _LARGE_MESH_TRI_LIMIT and reported
        (-1, -1), which _post_verify turned into UNVERIFIED — on exactly the
        meshes most likely to be broken."""
        n = 2_100_000                       # just past the old 2M limit
        faces = np.tile(np.array(TETRA_FACES, dtype=np.int64), (n // 4, 1))
        found = scan(mesh(TETRA_VERTS, faces))
        self.assertEqual(found.faces, n)
        self.assertGreaterEqual(found.non_manifold, 0)   # a real answer


class TestOpenLoops(unittest.TestCase):

    def test_one_missing_face_makes_one_loop(self):
        loops = open_loops(tetra(TETRA_FACES[:3]))
        self.assertEqual(len(loops), 1)
        self.assertEqual(len(loops[0].vertices), 3)

    def test_the_diameter_is_the_span_not_the_edge_count(self):
        """A loop of many short edges can still be a large hole."""
        loops = open_loops(tetra(TETRA_FACES[:3]))
        # The open face is (1,2,3): corners at (1,0,0), (0,1,0), (0,0,1),
        # whose bounding box is the unit cube, diagonal sqrt(3).
        self.assertAlmostEqual(loops[0].diameter, 3 ** 0.5, places=4)

    def test_a_closed_mesh_has_no_loops(self):
        self.assertEqual(open_loops(tetra()), ())
        self.assertEqual(largest_open_loop(tetra()), 0.0)

    def test_loops_come_back_largest_first(self):
        """Two separate holes, one much bigger; the caller judges the biggest."""
        verts = TETRA_VERTS + [[100, 100, 100], [200, 100, 100],
                               [100, 200, 100], [100, 100, 200]]
        faces = TETRA_FACES[:3] + [[4, 6, 5], [4, 5, 7], [4, 7, 6]]
        loops = open_loops(mesh(verts, faces))
        self.assertEqual(len(loops), 2)
        self.assertGreater(loops[0].diameter, loops[1].diameter)

    def test_printable_when_every_hole_is_under_one_layer(self):
        m = tetra(TETRA_FACES[:3])
        self.assertTrue(open_loops_are_printable(m, min_layer=10.0))

    def test_not_printable_when_a_hole_exceeds_the_layer(self):
        m = tetra(TETRA_FACES[:3])
        self.assertFalse(open_loops_are_printable(m, min_layer=0.1))

    def test_a_clean_mesh_is_not_printable_by_this_question(self):
        """False means 'do not accept on these grounds' — a caller with no
        open edges is not asking whether its holes are small."""
        self.assertFalse(open_loops_are_printable(tetra(), min_layer=10.0))

    def test_a_disabled_layer_restores_the_strict_rule(self):
        m = tetra(TETRA_FACES[:3])
        self.assertFalse(open_loops_are_printable(m, min_layer=0))

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            open_loops(unloaded())


class TestWindingSeams(unittest.TestCase):

    def test_a_consistently_wound_mesh_has_no_seams(self):
        self.assertEqual(winding_seams(tetra()), (0, 0))

    def test_reversing_one_face_creates_a_closed_loop(self):
        """Its three edges all reverse, and they form a ring around the face.

        The loop count is what matters: a closed loop encircles a region whose
        winding cannot be reconciled, and PyMeshFix deletes what it encircles.
        """
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        edges, loops = winding_seams(mesh(TETRA_VERTS, faces))
        self.assertEqual(edges, 3)
        self.assertEqual(loops, 1)

    def test_an_empty_mesh_has_no_seams(self):
        self.assertEqual(
            winding_seams(mesh(TETRA_VERTS, np.zeros((0, 3), dtype=np.int64))),
            (0, 0))

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            winding_seams(unloaded())

    # -- differential test against the original implementation --------------

    @staticmethod
    def _original(faces):
        """`find_winding_seams` from stl_batch_fix.py, verbatim.

        Kept as the oracle: it is slow but it ran on the whole collection, so
        agreement with it is the strongest correctness statement available.
        """
        edge_dir = defaultdict(list)
        for a, b, c in faces:
            for u, w in ((a, b), (b, c), (c, a)):
                edge_dir[(min(u, w), max(u, w))].append(u < w)
        seam = [k for k, dirs in edge_dir.items()
                if len(dirs) == 2 and dirs[0] == dirs[1]]
        if not seam:
            return 0, 0
        adj = defaultdict(list)
        for a, b in seam:
            adj[a].append(b)
            adj[b].append(a)
        seen, loops = set(), 0
        for start in list(adj):
            if start in seen:
                continue
            stack, group = [start], []
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                group.append(x)
                stack.extend(adj[x])
            if all(len(adj[x]) == 2 for x in group):
                loops += 1
        return len(seam), loops

    def test_it_agrees_with_the_original_on_random_meshes(self):
        """400 random meshes, including non-manifold and degenerate ones.

        This caught a real bug: the first vectorised version sliced adjacent
        rows and raised ValueError on every mesh that had any seam candidate —
        200 of 200 trials. Tests written before this check would have encoded
        the broken behaviour.
        """
        rng = np.random.default_rng(0)
        for trial in range(400):
            n_verts = int(rng.integers(4, 20))
            n_faces = int(rng.integers(2, 40))
            verts = rng.random((n_verts, 3)).astype(np.float32)
            faces = rng.integers(0, n_verts, size=(n_faces, 3)).astype(np.int64)
            with self.subTest(trial=trial):
                self.assertEqual(winding_seams(mesh(verts, faces)),
                                 self._original(faces))


class TestShells(unittest.TestCase):

    def test_one_connected_mesh_is_one_shell(self):
        self.assertEqual(len(shells(tetra())), 1)

    def test_two_disjoint_meshes_are_two_shells(self):
        verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                               [10, 11, 10], [10, 10, 11]]
        faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
        self.assertEqual(len(shells(mesh(verts, faces))), 2)

    def test_shells_come_back_largest_first(self):
        verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10], [10, 11, 10]]
        faces = TETRA_FACES + [[4, 5, 6]]          # 4 faces, then 1
        found = shells(mesh(verts, faces))
        self.assertEqual([len(s) for s in found], [4, 1])

    def test_faces_sharing_only_a_vertex_are_one_shell(self):
        """Touching at a point still makes them connected for this purpose."""
        verts = TETRA_VERTS + [[5, 5, 5], [6, 5, 5]]
        faces = TETRA_FACES + [[0, 4, 5]]          # shares vertex 0
        self.assertEqual(len(shells(mesh(verts, faces))), 1)

    def test_shell_count_ignores_specks_below_the_floor(self):
        """A collection mesh carries hundreds of few-face specks; treating
        those as real parts turns one repair into hundreds."""
        verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10], [10, 11, 10]]
        faces = TETRA_FACES + [[4, 5, 6]]
        m = mesh(verts, faces)
        self.assertEqual(shell_count(m), 2)
        self.assertEqual(shell_count(m, min_faces=2), 1)

    def test_an_empty_mesh_has_no_shells(self):
        self.assertEqual(shells(mesh(TETRA_VERTS,
                                     np.zeros((0, 3), dtype=np.int64))), ())

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            shells(unloaded())


class TestDataclasses(unittest.TestCase):

    def test_scan_is_immutable(self):
        found = Scan(1, 2, 3)
        with self.assertRaises(Exception):
            found.open_edges = 9

    def test_loop_is_immutable(self):
        loop = Loop((1, 2, 3), 0.5)
        with self.assertRaises(Exception):
            loop.diameter = 9.0


if __name__ == '__main__':
    unittest.main(verbosity=2)
