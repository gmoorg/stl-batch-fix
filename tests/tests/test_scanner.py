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

import os
import unittest
from collections import defaultdict

import numpy as np

from libs import scanner
from libs.mesh_io import Geometry, Kind, Mesh, ensure_parent_dir, require_geometry
from libs.scanner import (
    Loop, Scan, component_volume, face_edges, largest_open_loop, open_loops,
    open_loops_are_printable, scan, seam_edges, shell_count, shells, volume,
    winding_seams,
)

#: A unit tetrahedron, consistently wound: 4 faces, 6 edges, every edge shared.
TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts, faces, path='/test.stl'):
    """A loaded Mesh from plain lists."""
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(path, path.replace('.stl', '.out.stl'), Kind.BINARY_STL,
                len(geometry.faces), True, None, geometry)


def tetra(faces=None):
    return mesh(TETRA_VERTS, TETRA_FACES if faces is None else faces)


def unloaded():
    return Mesh('/test.stl', '/test.out.stl', Kind.BINARY_STL, 4, True)


class TestHelpers(unittest.TestCase):

    def test_face_edges_matches_the_project_edge_contract(self):
        edges = face_edges(np.array(TETRA_FACES, dtype=np.int64))
        self.assertEqual(edges.shape, (12, 2))
        self.assertTrue(np.all(edges[:, 0] <= edges[:, 1]))

    def test_require_geometry_raises_on_unloaded_mesh(self):
        with self.assertRaises(ValueError):
            require_geometry(unloaded())

    def test_ensure_parent_dir_creates_missing_directories(self):
        target = '/tmp/layer1/layer2/test.stl'
        ensure_parent_dir(target)
        self.assertTrue(os.path.isdir('/tmp/layer1/layer2'))


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
    def _original_edges(faces):
        """The oracle's seam EDGES, as (min, max) tuples.

        Same edge-direction logic as `_original` below, stopping before the
        loop counting — so the edge list and the counts are checked against one
        implementation rather than two that could drift apart.
        """
        edge_dir = defaultdict(list)
        for a, b, c in faces:
            for u, w in ((a, b), (b, c), (c, a)):
                edge_dir[(min(u, w), max(u, w))].append(u < w)
        seam = [k for k, dirs in edge_dir.items()
                if len(dirs) == 2 and dirs[0] == dirs[1]]
        # The oracle is corrected here, deliberately. The original counts
        # self-edges (u == w) as seams: a degenerate face emits (v, v), two
        # such faces satisfy its pair test, and `adjacency[v] = [v, v]` then
        # satisfies its closed-loop test — so one point is reported as a loop
        # encircling a region. Measured on Hair.stl: 106 "seam edges", all 106
        # self-edges, 106 phantom loops, on a mesh with no winding seam at all.
        #
        # Left uncorrected this class of bug is invisible to a differential
        # test, because both sides share it. Agreement is not correctness.
        # See TestSelfEdges below, which pins the corrected behaviour directly.
        return [(a, b) for a, b in seam if a != b]

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
        # Corrected for self-edges, as in `_original_edges` above — see the
        # comment there. Without this the oracle reports a phantom closed loop
        # for every pair of degenerate faces sharing a vertex.
        seam = [(a, b) for a, b in seam if a != b]
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


class TestSelfEdges(unittest.TestCase):
    """Degenerate faces must not masquerade as winding seams.

    A face with two identical corners emits an edge (v, v). Two such faces
    sharing it pass the "exactly two faces, same direction" seam test, and then
    `adjacency[v] = [v, v]` — length 2 — passes the "every vertex has exactly
    two seam edges" closed-loop test. So a single point is reported as a closed
    loop, which is the pipeline's strongest signal to split a mesh.

    Found on real data, not by inspection: `Hanna and Chewie/Hair.stl` reported
    106 seam edges in 106 closed loops. All 106 were self-edges. The mesh has
    250 degenerate faces and no winding seam.

    The original implementation has the same flaw, so the differential tests
    above cannot catch it — they are pinned against a corrected oracle instead,
    and these tests pin the behaviour directly.
    """

    #: One tetrahedron plus two degenerate faces sharing the self-edge (1, 1).
    DEGENERATE_FACES = TETRA_FACES + [[1, 1, 2], [1, 1, 4]]
    VERTS = TETRA_VERTS + [[1, 1, 1]]

    def mesh(self):
        return mesh(self.VERTS, self.DEGENERATE_FACES)

    def test_a_self_edge_is_not_a_seam(self):
        self.assertEqual(len(seam_edges(self.mesh())), 0)

    def test_a_self_edge_is_not_a_closed_loop(self):
        """The consequential half: a loop means 'split this mesh'."""
        self.assertEqual(winding_seams(self.mesh()), (0, 0))

    def test_no_returned_edge_ever_joins_a_vertex_to_itself(self):
        rng = np.random.default_rng(7)
        for trial in range(100):
            n_verts = int(rng.integers(3, 12))
            faces = rng.integers(0, n_verts, size=(int(rng.integers(2, 30)), 3))
            verts = rng.random((n_verts, 3)).astype(np.float32)
            edges = seam_edges(mesh(verts, faces.astype(np.int64)))
            with self.subTest(trial=trial):
                if len(edges):
                    self.assertTrue((edges[:, 0] != edges[:, 1]).all())

    def test_degenerate_faces_are_still_counted_as_degenerate(self):
        """Dropped from the seam count, not from the defect report — they are
        a real defect, just a different one."""
        self.assertEqual(scan(self.mesh()).degenerate, 2)

    def test_a_real_seam_is_still_found_alongside_degenerate_faces(self):
        """The filter must not suppress genuine seams on a messy mesh.

        The degenerate pair is placed on vertex 4, which the reversed face does
        not touch. An earlier version of this test put them on vertex 1 and
        expected 3 seam edges; it got 2, and the code was right. A degenerate
        face `[1, 1, 2]` also emits the ordinary edge (1, 2), which pushed that
        edge to four users and so out of the "exactly two faces" seam test.
        The mesh genuinely had two seam edges. The fixture was the bug.
        """
        verts = TETRA_VERTS + [[5, 5, 5], [6, 5, 5]]
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]   # a real seam
        faces += [[4, 4, 5], [4, 4, 5]]        # degenerates, clear of the seam
        edges, loops = winding_seams(mesh(verts, faces))
        self.assertEqual(edges, 3, "the filter suppressed a genuine seam")
        self.assertEqual(loops, 1)


class TestSeamEdges(unittest.TestCase):
    """The edge list itself — what `split_at_seams` cuts on.

    `winding_seams` now derives its count from `len(seam_edges(...))`, so the
    differential test above proves the count is unchanged and says nothing
    about whether the rows are right. An array with the correct length and the
    wrong rows would pass every test in that class and cut the mesh in the
    wrong place.
    """

    def test_a_clean_mesh_has_an_empty_array(self):
        edges = seam_edges(tetra())
        self.assertEqual(edges.shape, (0, 2))

    def test_the_shape_and_dtype_are_what_split_expects(self):
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        edges = seam_edges(mesh(TETRA_VERTS, faces))
        self.assertEqual(edges.ndim, 2)
        self.assertEqual(edges.shape[1], 2)
        self.assertEqual(edges.dtype, np.int64)

    def test_rows_are_sorted_low_index_first(self):
        """`split_at_seams` tests membership with (min, max) keys."""
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        edges = seam_edges(mesh(TETRA_VERTS, faces))
        self.assertTrue((edges[:, 0] < edges[:, 1]).all())

    def test_the_edges_are_the_reversed_face_s_own_edges(self):
        """Reversing face 3 (vertices 1,2,3) must flag exactly its edges."""
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        edges = seam_edges(mesh(TETRA_VERTS, faces))
        got = {tuple(int(v) for v in row) for row in edges}
        self.assertEqual(got, {(1, 2), (1, 3), (2, 3)})

    def test_count_matches_winding_seams(self):
        """The two functions must not drift apart."""
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        m = mesh(TETRA_VERTS, faces)
        self.assertEqual(len(seam_edges(m)), winding_seams(m)[0])

    def test_it_agrees_with_the_original_edge_set(self):
        """400 random meshes: the same EDGES, not merely the same count.

        The oracle returns (min, max) tuples; compare as sets so ordering
        differences between the two implementations do not register as
        disagreement while a genuinely wrong edge would.
        """
        rng = np.random.default_rng(1)
        for trial in range(400):
            n_verts = int(rng.integers(4, 20))
            n_faces = int(rng.integers(2, 40))
            verts = rng.random((n_verts, 3)).astype(np.float32)
            faces = rng.integers(0, n_verts, size=(n_faces, 3)).astype(np.int64)
            with self.subTest(trial=trial):
                got = {tuple(int(v) for v in row)
                       for row in seam_edges(mesh(verts, faces))}
                expected = set(TestWindingSeams._original_edges(faces))
                self.assertEqual(got, expected)

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            seam_edges(unloaded())


class TestVolume(unittest.TestCase):
    """Signed volume — the only check that sees several things nothing else does."""

    def test_a_closed_mesh_has_positive_volume(self):
        self.assertGreater(volume(tetra()), 0)

    def test_reversing_every_face_flips_the_sign(self):
        """The inside-out case. Identical on every other check — nm, open,
        degenerate, seams, shells — and no other tool sees it either:
        PyMeshFix is a no-op, a commercial service reports 0 inverted normals,
        and Bambu renders and slices it normally."""
        faces = np.array(TETRA_FACES, dtype=np.int64)[:, ::-1]
        forward, backward = volume(tetra()), volume(tetra(faces))
        self.assertAlmostEqual(forward, -backward, places=4)
        self.assertLess(backward, 0)

    def test_an_empty_mesh_has_zero_volume(self):
        self.assertEqual(volume(mesh(TETRA_VERTS,
                                     np.zeros((0, 3), dtype=np.int64))), 0.0)

    def test_it_is_the_tetrahedron_s_actual_volume(self):
        """A corner tetrahedron with legs of 1 encloses 1/6."""
        self.assertAlmostEqual(abs(volume(tetra())), 1.0 / 6.0, places=5)

    def test_scaling_cubes_the_volume(self):
        """Guards against an area- or length-like measure sneaking in."""
        big = mesh([[c * 2 for c in v] for v in TETRA_VERTS], TETRA_FACES)
        self.assertAlmostEqual(volume(big), volume(tetra()) * 8, places=4)

    def test_translation_does_not_change_it(self):
        """The sum is over tetrahedra from the origin, so a mesh far from the
        origin must still report its own enclosed volume."""
        far = mesh([[c + 100 for c in v] for v in TETRA_VERTS], TETRA_FACES)
        self.assertAlmostEqual(volume(far), volume(tetra()), places=3)

    def test_two_disjoint_shells_add_up(self):
        """The doubles case: two coincident spheres read as twice one. That is
        what showed PyMeshFix had destroyed geometry when every local check —
        ours and a commercial service's — called its output clean."""
        verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                               [10, 11, 10], [10, 10, 11]]
        faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
        self.assertAlmostEqual(volume(mesh(verts, faces)),
                               volume(tetra()) * 2, places=4)

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            volume(unloaded())


class TestComponentVolume(unittest.TestCase):
    """A02: the measure that cannot cancel.

    `volume` is signed, so two oppositely wound shells sum to nothing. The
    measured case: a 760-face sphere at +4094.863122 beside a 4-face
    tetrahedron at -4094.863180 totalled -0.00006, and deleting the
    tetrahedron then reported 7,049,393,791% of volume kept.
    """

    def test_oppositely_wound_shells_do_not_cancel(self):
        inverted = [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
        verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                               [10, 11, 10], [10, 10, 11]]
        faces = TETRA_FACES + [f[::-1] for f in inverted]
        both = mesh(verts, faces)

        self.assertAlmostEqual(volume(both), 0.0, places=4)
        self.assertAlmostEqual(component_volume(both),
                               abs(volume(tetra())) * 2, places=4)

    def test_it_agrees_with_volume_on_one_shell(self):
        """No new disagreement on the ordinary case: one closed body."""
        self.assertAlmostEqual(component_volume(tetra()),
                               abs(volume(tetra())), places=6)

    def test_an_inverted_mesh_measures_the_same_as_an_outward_one(self):
        """Magnitude, so an intentional winding flip is not loss."""
        faces = np.array(TETRA_FACES, dtype=np.int64)[:, ::-1]
        self.assertAlmostEqual(component_volume(tetra(faces)),
                               component_volume(tetra()), places=6)

    def test_an_empty_mesh_measures_zero(self):
        self.assertEqual(
            component_volume(mesh(TETRA_VERTS,
                                  np.zeros((0, 3), dtype=np.int64))), 0.0)

    def test_unloaded_raises(self):
        with self.assertRaises(ValueError):
            component_volume(unloaded())


class TestShellsAgainstUnionFind(unittest.TestCase):
    """`shells()` delegates to scipy; this pins it to a known-correct oracle.

    The oracle is a textbook union-find — the implementation `shells()` used
    before scipy replaced it. Unlike the `winding_seams` oracle it carries no
    inherited flaw, so agreement here is meaningful rather than merely mutual.

    It earns its place: a pure-numpy replacement tried before scipy passed 500
    random meshes and was still wrong for the job, being 2x SLOWER than
    union-find on a real 2M-face model while 15-24x faster on synthetic ones.
    Correctness and fitness are different questions; this class covers the
    first.
    """

    @staticmethod
    def _union_find(faces):
        """Union-find over faces joined by a shared EDGE.

        An earlier version of this oracle unioned vertices, which made two
        surfaces meeting at a point one component. The implementation did the
        same, so 500 random meshes agreed and both were wrong — the third time
        in this suite that a differential test confirmed self-consistency
        rather than correctness. It is only ever real geometry that catches
        that: PyMeshLab split Mandy's largest shell in two where we did not.
        """
        parent = list(range(len(faces)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        owners = defaultdict(list)
        for i, (a, b, c) in enumerate(faces):
            for u, w in ((a, b), (b, c), (c, a)):
                owners[(min(int(u), int(w)), max(int(u), int(w)))].append(i)
        for sharers in owners.values():
            for other in sharers[1:]:
                union(sharers[0], other)

        groups = defaultdict(list)
        for i in range(len(faces)):
            groups[find(i)].append(i)
        return {frozenset(v) for v in groups.values()}

    def test_it_agrees_on_random_meshes(self):
        rng = np.random.default_rng(11)
        for trial in range(500):
            n_verts = int(rng.integers(3, 25))
            n_faces = int(rng.integers(1, 40))
            faces = rng.integers(0, n_verts, size=(n_faces, 3)).astype(np.int64)
            verts = rng.random((n_verts, 3)).astype(np.float32)
            with self.subTest(trial=trial):
                got = {frozenset(int(i) for i in g)
                       for g in shells(mesh(verts, faces))}
                self.assertEqual(got, self._union_find(faces))

    def test_groups_come_back_largest_first(self):
        """The contract callers rely on to take the main shell."""
        rng = np.random.default_rng(12)
        for trial in range(100):
            n_verts = int(rng.integers(3, 25))
            faces = rng.integers(0, n_verts,
                                 size=(int(rng.integers(1, 40)), 3)).astype(np.int64)
            verts = rng.random((n_verts, 3)).astype(np.float32)
            sizes = [len(g) for g in shells(mesh(verts, faces))]
            with self.subTest(trial=trial):
                self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_awkward_meshes(self):
        """Cases where implementations diverge if they are going to."""
        for name, faces in (
                ('single face', [[0, 1, 2]]),
                ('degenerate face', [[0, 0, 0]]),
                ('two disjoint', [[0, 1, 2], [3, 4, 5]]),
                ('repeated face', [[0, 1, 2], [0, 1, 2]]),
                ('chain', [[0, 1, 2], [2, 3, 4], [4, 5, 6]]),
        ):
            faces = np.array(faces, dtype=np.int64)
            verts = np.zeros((int(faces.max()) + 1, 3), dtype=np.float32)
            with self.subTest(case=name):
                got = {frozenset(int(i) for i in g)
                       for g in shells(mesh(verts, faces))}
                self.assertEqual(got, self._union_find(faces))


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

    def test_faces_sharing_only_a_vertex_are_separate_shells(self):
        """A point contact is NOT a connection — and this is load-bearing.

        An earlier version of this test asserted the opposite, and the
        implementation agreed with it. Both were wrong for the job. A vertex
        where two surfaces touch is non-manifold by construction, and PyMeshFix
        rebuilds one manifold surface and discards the rest — so treating a
        vertex-joined pair as a single component hands it exactly the input
        that makes it delete geometry.

        Measured on Mandy_Body_Dinamuuu3D.stl: vertex connectivity gives 39
        components with a largest of 1,315,986 faces; edge connectivity gives
        40, splitting that into 941,571 + 374,415. PyMeshLab, which the
        pipeline was built around, reports the edge-connected answer.
        """
        verts = TETRA_VERTS + [[5, 5, 5], [6, 5, 5]]
        faces = TETRA_FACES + [[0, 4, 5]]          # shares vertex 0 only
        self.assertEqual(len(shells(mesh(verts, faces))), 2)

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
