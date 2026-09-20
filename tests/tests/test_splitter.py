"""Tests for libs.splitter — cutting and reassembly, no files, no libraries.

Fixtures are index arrays built by hand, as in test_scanner: a tetrahedron is
four faces and six edges, two disjoint tetrahedra are two components, and every
expected number here is derivable on paper.

The seam fixture is synthetic and deliberately so. **No mesh in the collection
exercises `by_seams`'s cutting path** — Mandy's head turned out to be its own
shell, and Amidara's 129 closed loops are 3-to-6 vertex rings that enclose
nothing and disconnect nothing. So the cut is tested against a tube with one
half's winding reversed: because the surface wraps, the boundary between the
two runs is a closed ring of seam edges with no loose ends. See `_tube` for why
a flat strip does not work, and `test_the_fixture_really_has_a_closed_seam_loop`
for the guard that caught it.
"""

import math
import unittest

import numpy as np

from libs import scanner, splitter
from libs.mesh_io import Geometry, Kind, Mesh
from libs.splitter import MIN_SHELL_FACES, by_seams, by_shells, merge

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts, faces, source='/in/body.stl', destination='/out/body.stl'):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(source, destination, Kind.BINARY_STL, len(geometry.faces),
                True, None, geometry)


def two_tetrahedra():
    """Two components that share nothing — 4 faces each."""
    verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10], [10, 11, 10], [10, 10, 11]]
    faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
    return mesh(verts, faces)


def _strip(n_quads, x0=0):
    """A flat ribbon of `n_quads` quads, two triangles each. One shell."""
    verts, faces = [], []
    for i in range(n_quads + 1):
        verts += [[x0 + i, 0, 0], [x0 + i, 1, 0]]
    for i in range(n_quads):
        a, b, c, d = 2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 3
        faces += [[a, b, c], [b, d, c]]
    return verts, faces


def _tube(n_ring=12, n_len=8, flip_from=None):
    """A cylinder wall — closed around, open at both ends.

    `flip_from` reverses the winding from that ring onward, and because the
    surface **wraps**, the boundary between the two runs is a closed ring of
    seam edges with no loose ends.

    A flat strip will not do, and that was measured rather than assumed: with
    one half reversed it yields 1 seam edge whose two vertices have degree 1 —
    an open chain terminating on the strip's own boundary, which is noise by
    the module's own rule, not a loop encircling anything. The tube gives 12
    seam edges, every vertex degree 2, one closed loop. The guard test below
    exists because the first version of this fixture was the strip and four
    tests passed vacuously.
    """
    verts = []
    for j in range(n_len + 1):
        for i in range(n_ring):
            angle = 2 * math.pi * i / n_ring
            verts.append([math.cos(angle), math.sin(angle), j])
    faces = []
    for j in range(n_len):
        for i in range(n_ring):
            a = j * n_ring + i
            b = j * n_ring + (i + 1) % n_ring
            c = (j + 1) * n_ring + i
            d = (j + 1) * n_ring + (i + 1) % n_ring
            if flip_from is not None and j >= flip_from:
                faces += [[a, c, b], [b, c, d]]      # reversed
            else:
                faces += [[a, b, c], [b, d, c]]
    return verts, faces


class TestByShells(unittest.TestCase):

    def test_a_single_shell_comes_back_unchanged(self):
        """The common path — 763 of 768 files — must cost nothing."""
        m = mesh(TETRA_VERTS, TETRA_FACES)
        parts = by_shells(m, min_faces=0)
        self.assertEqual(len(parts), 1)
        self.assertIs(parts[0], m, "it rebuilt a mesh that was never divided")

    def test_two_components_become_two_parts(self):
        parts = by_shells(two_tetrahedra(), min_faces=0)
        self.assertEqual(len(parts), 2)
        self.assertEqual(sorted(p.triangles for p in parts), [4, 4])

    def test_parts_are_standalone_meshes(self):
        """Vertices narrowed and faces renumbered — a part must not carry
        indices into geometry it no longer has."""
        for part in by_shells(two_tetrahedra(), min_faces=0):
            self.assertEqual(int(part.geometry.faces.max()) + 1,
                             len(part.geometry.verts))
            self.assertEqual(len(part.geometry.verts), 4)

    def test_a_vertex_join_separates(self):
        """D22: a point contact is not a connection.

        PyMeshFix rebuilds one manifold surface; a vertex where two surfaces
        touch is non-manifold, so handing it the pair joined would be handing
        it the input that makes it delete geometry.
        """
        verts = TETRA_VERTS + [[5, 5, 5], [6, 5, 5]]
        faces = TETRA_FACES + [[0, 4, 5]]          # shares vertex 0 only
        self.assertEqual(len(by_shells(mesh(verts, faces), min_faces=0)), 2)

    def test_debris_below_the_floor_is_dropped(self):
        """whole-costume01 is 444 shells of which 443 are under 100 faces."""
        big_verts, big_faces = _strip(80)          # 160 faces
        verts = big_verts + [[100, 0, 0], [101, 0, 0], [100, 1, 0]]
        base = len(big_verts)
        faces = big_faces + [[base, base + 1, base + 2]]
        parts = by_shells(mesh(verts, faces), min_faces=MIN_SHELL_FACES)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].triangles, 160, "the speck was kept")

    def test_a_mesh_of_only_debris_is_returned_whole(self):
        """Dropping everything would lose the file. Returning it is a
        judgement the caller makes, not a split."""
        m = two_tetrahedra()                        # both parts are 4 faces
        parts = by_shells(m, min_faces=MIN_SHELL_FACES)
        self.assertEqual(len(parts), 1)
        self.assertIs(parts[0], m)

    def test_parts_come_back_largest_first(self):
        av, af = _strip(30, x0=0)                   # 60 faces
        bv, bf = _strip(10, x0=100)                 # 20 faces
        offset = len(av)
        verts = av + bv
        faces = af + [[i + offset for i in f] for f in bf]
        parts = by_shells(mesh(verts, faces), min_faces=0)
        self.assertEqual([p.triangles for p in parts], [60, 20])


class TestPartIdentity(unittest.TestCase):
    """Each part needs its own destination, or their markers collide."""

    def test_parts_get_distinct_destinations(self):
        parts = by_shells(two_tetrahedra(), min_faces=0)
        self.assertEqual(len({p.destination for p in parts}), len(parts))

    def test_parts_keep_the_parent_source(self):
        """They came from one file and should say so."""
        m = two_tetrahedra()
        for part in by_shells(m, min_faces=0):
            self.assertEqual(part.path, m.path)

    def test_the_default_name_sits_beside_the_parent_output(self):
        parts = by_shells(two_tetrahedra(), min_faces=0)
        self.assertEqual([p.destination for p in parts],
                         ['/out/body.part.0.stl', '/out/body.part.1.stl'])

    def test_naming_is_injectable(self):
        """Output layout is the caller's policy, not this module's."""
        parts = by_shells(two_tetrahedra(), min_faces=0,
                          name=lambda m, i, n: f'/parts/{i:02d}.stl')
        self.assertEqual([p.destination for p in parts],
                         ['/parts/00.stl', '/parts/01.stl'])

    def test_an_undivided_mesh_keeps_its_own_destination(self):
        """Nothing was split, so nothing should be renamed."""
        m = mesh(TETRA_VERTS, TETRA_FACES)
        self.assertEqual(by_shells(m, min_faces=0)[0].destination,
                         m.destination)


class TestBySeams(unittest.TestCase):

    def seamed(self):
        """A tube whose upper half is wound the other way."""
        return mesh(*_tube(n_ring=12, n_len=8, flip_from=4))

    def test_the_fixture_really_has_a_closed_seam_loop(self):
        """Guard the guard: if the fixture stops being a seam case, every
        other test in this class silently becomes a no-op test."""
        edges, loops = scanner.winding_seams(self.seamed())
        self.assertGreater(edges, 0, 'fixture has no seam edges')
        self.assertGreater(loops, 0, 'fixture has no CLOSED loop')

    def test_a_mesh_with_no_seam_comes_back_unchanged(self):
        m = mesh(*_tube())
        parts = by_seams(m, min_faces=0)
        self.assertEqual(len(parts), 1)
        self.assertIs(parts[0], m)

    def test_a_seamed_mesh_is_cut_in_two(self):
        parts = by_seams(self.seamed(), min_faces=0)
        self.assertEqual(len(parts), 2)
        self.assertEqual([p.triangles for p in parts], [96, 96])

    def test_each_region_is_internally_consistent(self):
        """The property the split exists to produce: PyMeshFix preserves a
        region only when its winding does not contradict itself."""
        for region in by_seams(self.seamed(), min_faces=0):
            self.assertEqual(scanner.winding_seams(region)[1], 0,
                             'a region still contains a closed seam loop')

    def test_no_faces_are_lost(self):
        m = self.seamed()
        parts = by_seams(m, min_faces=0)
        self.assertEqual(sum(p.triangles for p in parts), m.triangles)

    def test_regions_get_their_own_destinations(self):
        parts = by_seams(self.seamed(), min_faces=0)
        self.assertEqual(len({p.destination for p in parts}), len(parts))

    def test_a_seam_that_separates_nothing_leaves_the_mesh_alone(self):
        """Amidara: 129 closed loops of 3-6 vertices each, encircling nothing.
        Removing those edges disconnects no region worth keeping, so the
        honest answer is to return the mesh."""
        verts, faces = _tube()
        faces = [list(f) for f in faces]
        faces[10] = [faces[10][0], faces[10][2], faces[10][1]]   # one triangle
        m = mesh(verts, faces)
        self.assertEqual(len(by_seams(m, min_faces=MIN_SHELL_FACES)), 1)


class TestMerge(unittest.TestCase):

    def test_merging_one_part_returns_it_unchanged(self):
        m = mesh(TETRA_VERTS, TETRA_FACES)
        self.assertIs(merge((m,)), m)

    def test_a_round_trip_preserves_every_face(self):
        m = two_tetrahedra()
        merged = merge(by_shells(m, min_faces=0), destination=m.destination)
        self.assertEqual(merged.triangles, m.triangles)

    def test_a_round_trip_preserves_the_defect_counts(self):
        """Nothing should be introduced by cutting and reassembling."""
        m = two_tetrahedra()
        before = scanner.scan(m)
        after = scanner.scan(merge(by_shells(m, min_faces=0),
                                   destination=m.destination))
        self.assertEqual((after.non_manifold, after.open_edges),
                         (before.non_manifold, before.open_edges))

    def test_the_merged_mesh_takes_the_parent_destination(self):
        """Not part 0's — inheriting that would write the whole model to a
        part's path and hang the parent's markers off it."""
        m = two_tetrahedra()
        merged = merge(by_shells(m, min_faces=0), destination=m.destination)
        self.assertEqual(merged.destination, m.destination)

    def test_face_indices_are_offset_per_part(self):
        """A part's indices are its own; concatenating without shifting them
        would silently fold part 1 onto part 0's vertices."""
        m = two_tetrahedra()
        merged = merge(by_shells(m, min_faces=0), destination=m.destination)
        self.assertEqual(len(merged.geometry.verts), 8)
        self.assertEqual(int(merged.geometry.faces.max()), 7)

    def test_merging_nothing_raises(self):
        with self.assertRaises(ValueError):
            merge(())

    def test_merging_an_unloaded_part_raises(self):
        loaded = mesh(TETRA_VERTS, TETRA_FACES)
        bare = Mesh('/in/x.stl', '/out/x.stl', Kind.BINARY_STL, 4, True)
        with self.assertRaises(ValueError):
            merge((loaded, bare))


class TestThePipelineShape(unittest.TestCase):
    """The four lines the module exists to make possible."""

    def test_split_then_merge_reads_without_a_branch(self):
        for m in (mesh(TETRA_VERTS, TETRA_FACES),      # never splits
                  two_tetrahedra()):                    # always splits
            with self.subTest(faces=m.triangles):
                parts = by_shells(m, min_faces=0)
                parts = [p for part in parts
                         for p in by_seams(part, min_faces=0)]
                out = merge(parts, destination=m.destination)
                self.assertEqual(out.triangles, m.triangles)
                self.assertEqual(out.destination, m.destination)


if __name__ == '__main__':
    unittest.main(verbosity=2)
