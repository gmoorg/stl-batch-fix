"""Tests for libs.winding — winding-number signed distance + marching cubes.

Geometry checks compare against analytic volumes and the input surface, not
only topology (docs/refactor/tests.md: assert geometry survives).
"""

import math
import unittest
from unittest import mock

import numpy as np

from libs import pipeconfig, scanner, winding
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests.test_decimator import _sphere



def mesh(verts, faces):
    g = Geometry(np.asarray(verts, np.float64), np.asarray(faces, np.int64).reshape(-1, 3))
    return Mesh('/in/m.stl', '/out/m.stl', Kind.BINARY_STL, len(g.faces), True, None, g)


def sphere(radius=10.0, centre=(0, 0, 0), subdivisions=3):
    v, f = _sphere(subdivisions=subdivisions)
    v = v / np.linalg.norm(v, axis=1)[:, None] * radius + np.asarray(centre, float)
    return v, f


def box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
         [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
         [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return np.asarray(v, float), np.asarray(f)


def combine(*parts):
    vs, fs, offset = [], [], 0
    for v, f in parts:
        vs.append(v); fs.append(np.asarray(f) + offset); offset += len(v)
    return mesh(np.vstack(vs), np.vstack(fs))


def volume(m):
    v = m.geometry.verts.astype(np.float64)
    a, b, c = (v[m.geometry.faces[:, i]] for i in range(3))
    return np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6


def canonical(m, h):
    """Triangles as sorted, quantized corner sets — order-independent."""
    q = np.rint(m.geometry.verts.astype(np.float64)[m.geometry.faces] / (h * 1e-3)).astype(np.int64)
    q = np.sort(q.reshape(len(q), 3, 3).view([('', np.int64)] * 3).reshape(len(q), 3), axis=1)
    return np.sort(q.view(np.int64).reshape(len(q), 9), axis=0)


def nm_pair(scale=0.5):
    """Two tetrahedra sharing one edge: closed (no open edges), one
    non-manifold edge (0-1, used by four faces), no degenerate faces. The
    second is the first turned 180° about x, so both wind outward."""
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [0, -1, 0], [0, 0, -1]],
                 float) * scale
    f = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3],
         [0, 4, 1], [0, 1, 5], [0, 5, 4], [1, 4, 5]]
    return v, np.asarray(f)


def with_nm_pair(weld):
    """`_weld` wrapped to add `nm_pair` (inside a radius-10 sphere) to its
    output: a reconstruction that is closed but non-manifold."""
    def wrapped(Vo, Fo, keys):
        g = weld(Vo, Fo, keys)
        pv, pf = nm_pair()
        return Geometry(np.vstack([g.verts, pv.astype(np.float64)]),
                        np.vstack([g.faces, pf + len(g.verts)]))
    return wrapped


def assert_closed(test, m):
    s = scanner.scan(m)
    test.assertEqual((s.open_edges, s.non_manifold, s.degenerate), (0, 0, 0))


class TestGeometry(unittest.TestCase):

    def test_sphere_is_closed_keeps_volume_and_surface(self):
        m = mesh(*sphere())
        out = winding.reconstruct(m, 0.5, 1)
        assert_closed(self, out)
        self.assertAlmostEqual(volume(out) / volume(m), 1.0, delta=0.005)
        r = np.linalg.norm(out.geometry.verts.astype(np.float64), axis=1)
        self.assertLess(np.abs(r - 10).max(), 0.5)

    def test_block_count_does_not_change_the_surface(self):
        m = mesh(*sphere())
        one, many = winding.reconstruct(m, 0.5, 1), winding.reconstruct(m, 0.5, 3)
        self.assertTrue(np.array_equal(canonical(one, 0.5), canonical(many, 0.5)))

    def test_seams_close_exactly_at_any_position_and_scale(self):
        """Block seams are welded by grid edge, not by coordinate rounding:
        the result is closed and manifold with the same face count for 1, 2
        and 3 blocks, far from the origin and at small and large scales.
        (Translation stays within float32 input precision: 1e5 units has a
        float32 step of ~0.008, well under h.)"""
        cases = {'far from origin': (sphere(10, (1e5, -1e5, 1e5)), 0.5),
                 'tiny': (sphere(0.01), 0.0005),
                 'huge': (sphere(1000), 50.0),
                 'shifted box': (box((0.37, 0.11, 0.29), (10.37, 6.11, 8.29)), 0.2)}
        for name, ((v, f), h) in cases.items():
            with self.subTest(name):
                counts = []
                for blocks in (1, 2, 3):
                    out = winding.reconstruct(mesh(v, f), h, blocks)
                    assert_closed(self, out)
                    counts.append(len(out.geometry.faces))
                self.assertEqual(len(set(counts)), 1, counts)

    def test_large_triangles_are_covered_by_the_band(self):
        """A cube of 12 large triangles, also shifted off the grid: the band
        must cover every triangle, not only near the vertices."""
        for shift in ((0, 0, 0), (0.37, 0.11, 0.29)):
            with self.subTest(shift=shift):
                out = winding.reconstruct(mesh(*box(np.add((0, 0, 0), shift),
                                                    np.add((10, 10, 10), shift))), 0.5, 2)
                assert_closed(self, out)
                self.assertAlmostEqual(volume(out), 1000, delta=10)

    def test_long_skinny_triangles(self):
        """12 long, thin triangles, on and off the grid planes."""
        for shift in ((0, 0, 0), (0.037, 0.053, 0.071)):
            with self.subTest(shift=shift):
                m = mesh(*box(np.add((0, 0, 0), shift), np.add((50, 0.8, 0.8), shift)))
                out = winding.reconstruct(m, 0.2, 1)
                assert_closed(self, out)
                # Marching cubes bevels sharp 90° edges. The grid starts half a
                # cell off the model's extreme faces, so the bevel is h²/8 per
                # unit edge length (measured 0.128); the bound allows 0.2·h².
                loss = 50 * 0.64 - volume(out)
                self.assertGreater(loss, 0)
                self.assertLess(loss, 4 * 50 * 0.2 * 0.2 ** 2)

    def test_sample_count_is_area_proportional_for_skinny_triangles(self):
        V, F = box((0, 0, 0), (50, 0.1, 0.1))
        _, _, _, area, L, H = winding._triangle_frames(V, F)
        n1, n2, bound = winding._sample_counts(L, H, 0.05)
        h = 0.05
        self.assertLessEqual(bound.sum(), 8 * area.sum() / h ** 2
                             + 2 * (L.sum() + H.sum()) / h * 2 + 4 * len(F))
        # The longest-edge-squared alternative would need ~(2L/h)^2/2 per triangle.
        self.assertLess(bound.sum(), ((2 * L / h) ** 2 / 2).sum() / 50)

    def test_an_open_mesh_comes_back_closed(self):
        v, f = sphere()
        out = winding.reconstruct(mesh(v, f[1:]), 0.5, 1)
        assert_closed(self, out)
        self.assertAlmostEqual(volume(out) / (4 / 3 * math.pi * 1000), 1.0, delta=0.02)

    def test_overlapping_shells_become_their_union(self):
        r, d = 10.0, 10.0
        lens = math.pi * (4 * r + d) * (2 * r - d) ** 2 / 12
        union = 2 * 4 / 3 * math.pi * r ** 3 - lens
        a, b = sphere(r, (0, 0, 0)), sphere(r, (d, 0, 0))
        out = winding.reconstruct(combine(a, b), 0.5, 2)
        assert_closed(self, out)
        self.assertAlmostEqual(volume(out) / union, 1.0, delta=0.02)
        # The buried halves are gone; the outer surface is kept.
        outer = np.vstack([a[0][np.linalg.norm(a[0] - (d, 0, 0), axis=1) > r + 0.5],
                           b[0][np.linalg.norm(b[0], axis=1) > r + 0.5]])
        d2 = np.array([np.min(np.linalg.norm(out.geometry.verts - p, axis=1)) for p in outer[::20]])
        self.assertLess(d2.max(), 0.6)

    def test_overlapping_shells_with_a_buried_face_on_a_grid_plane(self):
        """Codex's reproduction: box 2's minimum face (x = 4.75) lies on a grid
        plane AND inside box 1. Its grid points must stay inside the union."""
        for x0 in (4.75, 5.0):
            with self.subTest(x0=x0):
                m = combine(box((0, 0, 0), (10, 10, 10)), box((x0, 0, 0), (15, 10, 10)))
                out = winding.reconstruct(m, 0.5, 1)
                assert_closed(self, out)
                self.assertAlmostEqual(volume(out), 1500, delta=20)

    def test_touching_solids_sharing_a_face_on_a_grid_plane(self):
        """Codex's reproduction: two boxes sharing the face x = 4.75 (on a
        grid plane) or x = 5.0 (between planes) must unite into one solid."""
        for x in (4.75, 5.0):
            with self.subTest(x=x):
                m = combine(box((0, 0, 0), (x, 10, 10)), box((x, 0, 0), (10, 10, 10)))
                out = winding.reconstruct(m, 0.5, 1)
                assert_closed(self, out)
                self.assertAlmostEqual(volume(out), 1000, delta=15)

    def test_surface_exactly_on_a_block_boundary(self):
        """Box 2's face lies exactly on the 2-block cut plane (grid from
        x = -1.25 in steps of 0.5; cut at index 12 = x 4.75): seams must
        still weld."""
        m = combine(box((0, 0, 0), (4, 10, 10)), box((4.75, 0, 0), (10, 10, 10)))
        p = winding.plan(m, 0.5, 10 ** 12)
        cut = winding._cuts(p.shape[0], 2)[1]
        self.assertEqual(float(p.lo[0] + cut * 0.5), 4.75)
        one, two = winding.reconstruct(m, 0.5, 1), winding.reconstruct(m, 0.5, 2)
        assert_closed(self, two)
        self.assertAlmostEqual(volume(two), 400 + 525, delta=15)
        self.assertTrue(np.array_equal(canonical(one, 0.5), canonical(two, 0.5)))


class TestOrientation(unittest.TestCase):
    """Inside is decided by the winding number, which follows face
    orientation: parts are oriented consistently and outward first, so an
    inside-out or partly flipped solid is rebuilt, never dropped."""

    def test_an_inside_out_solid_is_rebuilt(self):
        v, f = box((0, 0, 0), (10, 10, 10))
        out = winding.reconstruct(mesh(v, f[:, ::-1]), 0.5, 1)
        assert_closed(self, out)
        self.assertAlmostEqual(volume(out), 1000, delta=15)

    def test_a_partly_flipped_shell_is_rebuilt(self):
        v, f = sphere()
        flipped = f.copy()
        top = v[f[:, 0]][:, 2] > 0
        flipped[top] = flipped[top][:, ::-1]
        out = winding.reconstruct(mesh(v, flipped), 0.5, 1)
        assert_closed(self, out)
        self.assertAlmostEqual(volume(out) / (4 / 3 * math.pi * 1000), 1.0, delta=0.02)

    def test_a_model_with_one_inverted_shell_keeps_both(self):
        """Codex's reproduction: two separate spheres, one inside out, through
        the whole processor — both kept, PROCESS, volume kept."""
        from libs import processor
        from libs.indicators import Indicator
        a, (bv, bf) = sphere(10, (0, 0, 0)), sphere(10, (30, 0, 0))
        m = combine(a, (bv, bf[:, ::-1]))
        outcome = processor.process(m, 0, part_steps=(('winding', winding.step_winding_reconstruct),))
        self.assertIs(outcome.indicator, Indicator.PROCESS, outcome.reason)
        self.assertAlmostEqual(abs(volume(outcome.mesh)) / (2 * 4 / 3 * math.pi * 1000), 1.0, delta=0.02)
        xs = outcome.mesh.geometry.verts[:, 0]
        self.assertLess(xs.min(), -9)
        self.assertGreater(xs.max(), 39)

    def test_a_closed_plate_thinner_than_the_grid_is_reported_empty(self):
        plate = mesh(*box((0.13, 0.13, 0.13), (10.13, 10.13, 0.18)))   # 0.05 thick, h 0.5
        with self.assertRaisesRegex(winding.EmptyResult, 'closed part thinner than the grid'):
            winding.reconstruct(plate, 0.5, 1)

    def test_a_dropped_thin_part_leaves_the_rest_of_the_model(self):
        """Owner policy: any part that rebuilds to nothing is dropped and the
        model merged without it — here a closed 0.001-thick plate next to a
        sphere (grid spacing ~0.07 from the whole model's diagonal)."""
        from libs import repairer
        m = combine(sphere(), box((30.13, 0.13, 0.13), (40.13, 10.13, 0.131)))
        result = repairer.repair(m, min_shell_faces=0,
                                 part_steps=(('winding', winding.step_winding_reconstruct),))
        self.assertTrue(result.ok, result.problem)
        dropped = [s.detail for s in result.steps if 'dropped' in s.detail]
        self.assertEqual(len(dropped), 1)
        self.assertIn('closed part thinner than the grid', dropped[0])
        self.assertLess(result.mesh.geometry.verts[:, 0].max(), 10.6)


class TestPlan(unittest.TestCase):

    def setUp(self):
        self.m = mesh(*sphere())

    def test_a_large_budget_uses_one_block(self):
        self.assertEqual(winding.plan(self.m, 0.5, 10 ** 12).blocks_per_axis, 1)

    def test_the_fewest_blocks_that_fit_are_chosen(self):
        p = winding.plan(self.m, 0.1, 10 ** 12)
        need_two = winding.estimate_bytes(len(self.m.geometry.faces), 1256.6, 0.1, p.samples_bound,
                                          winding._largest_block(p.shape, 2))
        chosen = winding.plan(self.m, 0.1, need_two + 1)
        self.assertGreaterEqual(chosen.blocks_per_axis, 2)
        self.assertLessEqual(chosen.estimate_bytes, need_two + 1)

    def test_an_impossible_budget_reports_floor_minimum_and_budget(self):
        with self.assertRaises(winding.BudgetError) as ctx:
            winding.plan(self.m, 0.5, 1000)
        e = ctx.exception
        self.assertEqual(e.budget, 1000)
        self.assertGreater(e.floor, 1000)
        self.assertGreaterEqual(e.minimum, e.floor)
        self.assertIn('budget', str(e))

    def test_block_limit_keeps_blocks_wide_and_allows_one_for_tiny_grids(self):
        self.assertEqual(winding.max_blocks_per_axis((5, 5, 5)), 1)
        self.assertEqual(winding.max_blocks_per_axis((111, 200, 300)), 10)

    def test_invalid_arguments(self):
        for h in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(h=h), self.assertRaises(ValueError):
                winding.plan(self.m, h, 10 ** 9)
        for budget in (0, -5, 1.5, True):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                winding.plan(self.m, 0.5, budget)


class TestContract(unittest.TestCase):

    def test_distance_tree_is_built_once_and_winding_queried_per_block(self):
        m = mesh(*sphere())
        real_aabb = winding._igl.AABB
        inits = []

        class Counting(real_aabb):
            def init(self, *a):
                inits.append(1)
                return super().init(*a)

        with mock.patch.object(winding._igl, 'AABB', Counting), \
             mock.patch.object(winding._igl, 'fast_winding_number',
                               wraps=winding._igl.fast_winding_number) as fwn:
            winding.reconstruct(m, 0.5, 3)
        self.assertEqual(len(inits), 1)
        self.assertGreaterEqual(fwn.call_count, 27)

    def welded_to(self, verts, faces):
        """Patch `_weld` so a reconstruction comes out as the given arrays."""
        g = Geometry(np.asarray(verts, np.float64), np.asarray(faces, np.int64).reshape(-1, 3))
        return mock.patch.object(winding, '_weld', return_value=g)

    def test_an_open_result_is_rejected(self):
        with self.welded_to([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]]), \
             self.assertRaisesRegex(RuntimeError, 'not closed: open=3'):
            winding.reconstruct(mesh(*sphere()), 0.5, 1)

    def test_empty_non_finite_and_degenerate_results_are_rejected(self):
        v, f = nm_pair()
        nan = v.copy(); nan[0, 0] = np.nan
        cases = {'no faces': (v, np.zeros((0, 3))),
                 'non-finite': (nan, f),
                 'degenerate=1': (v, np.vstack([f, [[2, 3, 2]]]))}
        for message, (verts, faces) in cases.items():
            with self.subTest(message), self.welded_to(verts, faces), \
                 self.assertRaisesRegex(RuntimeError, message):
                winding.reconstruct(mesh(*sphere()), 0.5, 1)

    def test_non_manifold_edges_are_passed_on_not_rejected(self):
        """Owner decision 2026-10-04: NM edges go on to decimation and the
        conditional MeshFix instead of failing the part. The step says so."""
        with mock.patch.object(winding, '_weld', with_nm_pair(winding._weld)):
            out = winding.reconstruct(mesh(*sphere()), 0.5, 1)
            ok, _, detail = winding.step_winding_reconstruct(
                mesh(*sphere()), pipeconfig.StepConfig(whole_model_diag=40.0))
        s = scanner.scan(out)
        self.assertEqual((s.open_edges, s.non_manifold, s.degenerate), (0, 1, 0))
        self.assertTrue(ok, detail)
        self.assertIn('nm=1 passed on', detail)

    def test_a_clean_result_does_not_mention_non_manifold_edges(self):
        ok, _, detail = winding.step_winding_reconstruct(
            mesh(*sphere()), pipeconfig.StepConfig(whole_model_diag=40.0))
        self.assertTrue(ok, detail)
        self.assertNotIn('nm=', detail)

    def test_non_manifold_left_in_a_final_output_is_flagged(self):
        """A custom sequence with no repair after winding: the judge, not
        winding, catches the NM edges it passed on."""
        from libs import processor
        from libs.indicators import Indicator
        with mock.patch.object(winding, '_weld', with_nm_pair(winding._weld)):
            outcome = processor.process(
                mesh(*sphere()), 0, min_shell_faces=0,
                part_steps=(('winding', winding.step_winding_reconstruct),))
        self.assertIs(outcome.indicator, Indicator.UNREPAIRED, outcome.reason)
        self.assertIn('1 non-manifold edge(s) remain', outcome.reason)

    def test_invalid_input_is_rejected_before_native_calls(self):
        v, f = sphere()
        bad_coords = v.copy(); bad_coords[0, 0] = np.nan
        for m in (mesh(bad_coords, f), mesh(v, np.vstack([f, [[0, 1, len(v)]]])),
                  mesh(v, np.zeros((0, 3), int))):
            with self.subTest(), mock.patch.object(winding._igl, 'AABB') as aabb, \
                 self.assertRaises(ValueError):
                winding.reconstruct(m, 0.5, 1)
            aabb.assert_not_called()
        for blocks in (0, 999, True, 1.0):
            with self.subTest(blocks=blocks), self.assertRaises(ValueError):
                winding.reconstruct(mesh(v, f), 0.5, blocks)


def edge_keys(rows, shape):
    """Edge keys from (axis, x, y, z) rows, computed independently of
    `_edge_ids`: the C-order index into a (3, NX, NY, NZ) array."""
    return np.ravel_multi_index(np.asarray(rows).T, (3, *shape)).astype(np.int64)


def row_weld(Vo, Fo, rows):
    """The weld as it was before int64 keys — row-wise unique over (axis, x,
    y, z) — kept as the oracle the keyed weld must match array for array."""
    _, first, inv = np.unique(rows, axis=0, return_index=True, return_inverse=True)
    Vo, Fo = Vo[first], inv.ravel()[Fo]
    Fo = Fo[(Fo[:, 0] != Fo[:, 1]) & (Fo[:, 1] != Fo[:, 2]) & (Fo[:, 0] != Fo[:, 2])]
    return Geometry(Vo, Fo.astype(np.int64, copy=False))


class TestWeld(unittest.TestCase):
    """The weld merges vertices on the same grid edge, never by distance."""

    def test_seam_copies_merge_and_close_neighbours_stay_apart(self):
        node = np.array([1.0, 1.0, 1.0])
        # a and b: vertices on two different edges meeting at one grid node,
        # 1e-12 apart; a2: a's copy from the neighbouring block, 1e-15 off.
        a, b = node + [1e-12, 0, 0], node + [0, 1e-12, 0]
        a2, c = a + [1e-15, 0, 0], node + [0, 0, 0.5]
        verts = np.array([a, b, a2, c])
        keys = edge_keys([[0, 1, 1, 1], [1, 1, 1, 1], [0, 1, 1, 1], [2, 1, 1, 1]], (4, 4, 4))
        g = winding._weld(verts, np.array([[0, 1, 3], [2, 3, 1]]), keys)
        self.assertEqual(len(g.verts), 3, 'a and a2 merge; b stays separate')
        self.assertEqual(len(g.faces), 2)
        self.assertEqual(sorted(map(sorted, g.faces.tolist())), [[0, 1, 2], [0, 1, 2]])
        # Kept exactly: a, b and the node are one point in float32.
        self.assertEqual(sorted(map(tuple, g.verts.tolist())), sorted(map(tuple, [a, b, c])))

    def test_keyed_weld_matches_the_row_wise_weld_exactly(self):
        """Same vertices, faces and order as the row-wise weld it replaced,
        keeping the same (first) copy of each edge. Duplicates have distinct
        coordinates, so keeping any other copy would show; random faces
        include some with two corners on one edge, exercising the filter."""
        rng = np.random.default_rng(7)
        shape = (5, 7, 3)
        pool = np.column_stack([rng.integers(0, 3, 300)] +
                               [rng.integers(0, n, 300) for n in shape])
        rows = pool[rng.integers(0, len(pool), 2000)]
        verts = rng.normal(size=(len(rows), 3))
        faces = rng.integers(0, len(rows), (3000, 3))
        old = row_weld(verts, faces, rows)
        new = winding._weld(verts, faces, edge_keys(rows, shape))
        self.assertLess(len(old.verts), len(verts), 'the data must contain duplicates')
        self.assertLess(len(old.faces), len(faces), 'the data must contain collapsed faces')
        for got, want in ((new.verts, old.verts), (new.faces, old.faces)):
            self.assertEqual((got.dtype, got.shape), (want.dtype, want.shape))
            self.assertEqual(got.tobytes(), want.tobytes())

    def test_edge_ids_are_global_keys_of_each_vertex_edge(self):
        """Unequal global dimensions, a nonzero block offset, all three axes,
        corners packed in either order and vertices in shuffled order: every
        key decodes to the vertex's axis and global lower corner."""
        rng = np.random.default_rng(3)
        block, r0, shape = (3, 4, 5), np.array([10, 20, 30]), (17, 29, 41)
        nx, ny = block[0], block[1]
        index = lambda c: int(c[0] + c[1] * nx + c[2] * nx * ny)
        e2v, rows, n = {}, {}, 0
        for axis in range(3):
            for x in range(block[0]):
                for y in range(block[1]):
                    for z in range(block[2]):
                        c = np.array([x, y, z])
                        d = c.copy(); d[axis] += 1
                        if d[axis] < block[axis]:
                            i, j = index(c), index(d)
                            if rng.random() < 0.5:
                                i, j = j, i
                            e2v[(i << 32) | j] = n
                            rows[n] = [axis, *(c + r0)]
                            n += 1
        order = rng.permutation(n)                     # shuffled vertex indices
        e2v = {k: int(order[v]) for k, v in e2v.items()}
        want = np.empty((n, 4), np.int64)
        for v, row in rows.items():
            want[order[v]] = row
        ids = winding._edge_ids(e2v, n, block, r0, shape)
        self.assertEqual(ids.dtype, np.int64)
        self.assertEqual(ids.shape, (n,))
        np.testing.assert_array_equal(np.column_stack(np.unravel_index(ids, (3, *shape))), want)

    def test_an_edge_map_that_is_not_one_unit_edge_per_vertex_is_rejected(self):
        shape, r0 = (4, 4, 4), np.zeros(3, np.int64)
        def key(i, j):
            return (i << 32) | j
        good = {key(0, 1): 0, key(0, 4): 1}            # +x and +y from corner 0
        ids = winding._edge_ids(good, 2, shape, r0, shape)
        self.assertEqual(ids.tolist(), edge_keys([[0, 0, 0, 0], [1, 0, 0, 0]], shape).tolist())
        bad = {'missing a vertex': ({key(0, 1): 0}, 2),
               'vertex twice': ({key(0, 1): 0, key(0, 4): 0}, 2),
               'not one step': ({key(0, 5): 0}, 1),
               'outside the block': ({key(0, 64): 0}, 1)}
        for name, (e2v, n) in bad.items():
            with self.subTest(name), self.assertRaises(RuntimeError):
                winding._edge_ids(e2v, n, shape, r0, shape)

    def test_corners_are_placed_by_block_offset(self):
        key = (21 << 32) | 22                           # (1,1,1) -> (2,1,1) in a 4³ block
        shape = (40, 50, 60)
        ids = winding._edge_ids({key: 0}, 1, (4, 4, 4), np.array([10, 20, 30]), shape)
        self.assertEqual(ids.tolist(), edge_keys([[0, 11, 21, 31]], shape).tolist())


class TestEdgeKeyBound(unittest.TestCase):
    """Edge keys must fit int64: 3·P ≤ 2**63 for P grid points."""

    LARGEST = 2 ** 63 // 3                              # largest P whose keys fit

    def test_the_bound_on_both_sides(self):
        winding._check_edge_keys((self.LARGEST, 1, 1))
        self.assertLessEqual(3 * self.LARGEST - 1, 2 ** 63 - 1)
        with self.assertRaisesRegex(ValueError, 'edge keys'):
            winding._check_edge_keys((self.LARGEST + 1, 1, 1))

    def test_edge_ids_refuses_a_grid_whose_keys_overflow(self):
        key = (0 << 32) | 1
        winding._edge_ids({key: 0}, 1, (4, 4, 4), np.zeros(3, np.int64), (self.LARGEST, 1, 1))
        with self.assertRaisesRegex(ValueError, 'edge keys'):
            winding._edge_ids({key: 0}, 1, (4, 4, 4), np.zeros(3, np.int64),
                              (self.LARGEST + 1, 1, 1))

    def test_reconstruct_refuses_before_any_work(self):
        """A unit cube at h = 1/1.5e6: about 1.5e6 points per axis, P ≈ 3.4e18
        — inside `_grid`'s index bound (P < 2**62), beyond the key bound. It
        raises from the grid (the message is the key bound's, not the index
        bound's) before sampling allocates anything."""
        cube = mesh(*box((0, 0, 0), (1, 1, 1)))
        for call in (lambda: winding.reconstruct(cube, 1 / 1.5e6, 1),
                     lambda: winding.plan(cube, 1 / 1.5e6, 10 ** 9)):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, 'edge keys'):
                call()


class TestStep(unittest.TestCase):

    def test_a_part_enclosing_no_volume_is_dropped_not_failed(self):
        """A one-sided open sheet has no inside: the step succeeds with an
        empty mesh (merge leaves it out) and says why (owner decision)."""
        sheet = mesh([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]], [[0, 1, 2], [0, 2, 3]])
        with self.assertRaises(winding.EmptyResult):
            winding.reconstruct(sheet, 0.5, 1)
        ok, out, detail = winding.step_winding_reconstruct(
            sheet, pipeconfig.StepConfig(whole_model_diag=40.0))
        self.assertTrue(ok, detail)
        self.assertEqual(len(out.geometry.faces), 0)
        self.assertIn('dropped', detail)
        self.assertIn('2 faces removed', detail)

    def test_a_dropped_part_is_left_out_of_the_merged_model(self):
        from libs import repairer
        v, f = sphere()
        sheet_v = np.array([[40, 0, 0], [50, 0, 0], [50, 10, 0], [40, 10, 0]], float)
        m = combine((v, f), (sheet_v, [[0, 1, 2], [0, 2, 3]]))
        result = repairer.repair(m, min_shell_faces=0,
                                 part_steps=(('winding', winding.step_winding_reconstruct),))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.parts, 2)
        r = np.linalg.norm(result.mesh.geometry.verts.astype(np.float64), axis=1)
        self.assertLess(r.max(), 10.6, 'the sheet must not be in the merged output')
        assert_closed(self, result.mesh)

    def test_step_rebuilds_with_spacing_from_the_whole_diagonal(self):
        m = mesh(*sphere())
        ok, out, detail = winding.step_winding_reconstruct(
            m, pipeconfig.StepConfig(whole_model_diag=400.0))
        self.assertTrue(ok, detail)
        self.assertIn('h=0.15', detail)
        self.assertIn('blocks=1^3', detail)
        assert_closed(self, out)

    def test_the_configured_budget_is_used(self):
        m = mesh(*sphere())
        seen = []
        real = winding.plan

        def spy(mesh_, h, budget):
            seen.append(budget)
            return real(mesh_, h, budget)

        with mock.patch.object(winding, 'plan', spy):
            winding.step_winding_reconstruct(
                m, pipeconfig.StepConfig(whole_model_diag=40.0,
                                         reconstruct_memory_budget_bytes=123_456_789_012))
        self.assertEqual(seen, [123_456_789_012])

    def test_failures_are_step_results_not_exceptions(self):
        m = mesh(*sphere())
        cases = [
            (pipeconfig.StepConfig(), 'whole_model_diag'),
            (pipeconfig.StepConfig(whole_model_diag=40.0, reconstruct_memory_budget_bytes=1000),
             'BudgetError'),
        ]
        for config, expected in cases:
            with self.subTest(expected=expected):
                ok, out, detail = winding.step_winding_reconstruct(m, config)
                self.assertFalse(ok)
                self.assertIs(out, m)
                self.assertIn(expected, detail)


if __name__ == '__main__':
    unittest.main()
