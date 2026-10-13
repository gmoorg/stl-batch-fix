"""Tests for the one-piece support grid (support_3mf.py --layout grid).

Outcomes on analytic fixtures: every clearance claim is checked against the
fixture's own geometry (a parabola, a step, an axis-aligned frustum), never
against the raster the generator used, so its blind spots cannot hide.
"""

import io
import os
import tempfile
import unittest
import zipfile

import numpy as np

try:
    import manifold3d
except ImportError as exc:
    # manifold3d is optional (only the grid layout uses it): without it this
    # module has nothing to test. Any other import error is a real failure.
    if not (exc.name or '').startswith('manifold3d'):
        raise
    raise unittest.SkipTest('manifold3d (optional) is not installed') from exc

import support_3mf
from libs import bambu3mf, basecheck, gridsupport
from tests.tests.print_fixtures import slab, translate, write_3mf

SETTINGS = {'initial_layer_print_height': '0.2', 'layer_height': '0.2',
            'support_top_z_distance': '0.24', 'support_threshold_angle': '40',
            'enable_support': '1', 'support_on_build_plate_only': '1',
            'support_object_xy_distance': '0.35', 'outer_wall_line_width': '0.42'}
GAP, XY = 0.2, 0.35
TOL = 1e-6


def box(lo, hi):
    """Closed axis-aligned box."""
    return frustum(lo, hi, hi[2], (hi[0] - lo[0]) / 2, (hi[1] - lo[1]) / 2)


def frustum(lo, hi_xy, top, half_x_top, half_y_top):
    """Closed axis-aligned square frustum from z=lo[2] (footprint lo..hi_xy)
    to z=top (same centre, half sizes at the top)."""
    cx, cy = (lo[0] + hi_xy[0]) / 2, (lo[1] + hi_xy[1]) / 2
    bx, by = (hi_xy[0] - lo[0]) / 2, (hi_xy[1] - lo[1]) / 2
    z0 = lo[2]
    v = np.array([[cx - bx, cy - by, z0], [cx + bx, cy - by, z0], [cx + bx, cy + by, z0],
                  [cx - bx, cy + by, z0],
                  [cx - half_x_top, cy - half_y_top, top], [cx + half_x_top, cy - half_y_top, top],
                  [cx + half_x_top, cy + half_y_top, top], [cx - half_x_top, cy + half_y_top, top]])
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def shin_bottom(x, y):
    """A rounded underside: lowest (z = 10) along y = 5, rising to 15 at the edges."""
    return 10.0 + (y - 5.0) ** 2 / 5.0


SHIN = slab(shin_bottom, size=(20.0, 10.0), n=(80, 80), top=18.0)


def closed_and_positive(vertices, faces):
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    t = vertices[faces]
    volume = np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6
    return bool(np.all(counts == 2)), volume


def pieces(vertices, faces):
    mesh = manifold3d.Mesh(vert_properties=vertices.astype(np.float32),
                           tri_verts=faces.astype(np.uint32))
    return len(manifold3d.Manifold(mesh).decompose())


def has_cells(vertices, faces, z):
    """A horizontal slice of a crossing grid encloses cells (holes)."""
    mesh = manifold3d.Mesh(vert_properties=vertices.astype(np.float32),
                           tri_verts=faces.astype(np.uint32))
    polygons = manifold3d.Manifold(mesh).slice(z).to_polygons()

    def area(p):
        p = np.asarray(p)
        return 0.5 * np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
    return any(area(p) < 0 for p in polygons)


def sample_points(vertices, faces):
    """Every vertex plus every face centroid of the grid."""
    return np.concatenate([vertices, vertices[faces].mean(axis=1)])


def build(parts, max_height=30.0, **overrides):
    """gridsupport.build on world-space parts, candidates from basecheck."""
    th = basecheck.Thresholds(first_layer=0.2, layer_height=0.2, support_gap=0.24,
                              support_angle=40.0, foot_height=max_height)
    result = basecheck.check(parts, th, keep_grid=True)
    grid = result.grid
    candidates = grid.overhang & (grid.height < max_height)
    settings = gridsupport.GridSettings(**dict(dict(gap=GAP, xy_distance=XY), **overrides))
    tris = np.concatenate([v[f] for v, f in parts])
    return gridsupport.build(tris, candidates, grid.x0, grid.y0, grid.cell, max_height, settings)


def shifted(mesh, dx=0.0, dy=0.0, dz=0.0):
    v, f = mesh
    return v + np.array([dx, dy, dz]), f


class Geometry(unittest.TestCase):

    def test_shin_on_a_post_gets_one_crossing_grid_clear_of_both(self):
        post = box((9.0, 4.0, 0.0), (11.0, 6.0, 10.5))
        support = build([SHIN, post])
        v, f = support.vertices, support.faces
        closed, volume = closed_and_positive(v, f)
        self.assertTrue(closed)
        self.assertGreater(volume, 0)
        self.assertEqual(pieces(v, f), 1)
        self.assertAlmostEqual(v[:, 2].min(), 0.0, places=6)
        self.assertTrue(has_cells(v, f, 5.0), 'walls must cross: X and Y')
        p = sample_points(v, f)
        # Vertical gap below the parabola wherever the shin is overhead.  The
        # mesh's flat facets lie up to (0.125 mm)² / 8 × 0.4 = 0.00078 mm
        # above the parabola they sample (chords of a convex curve).
        over = (p[:, 0] >= 0) & (p[:, 0] <= 20) & (p[:, 1] >= 0) & (p[:, 1] <= 10)
        self.assertTrue(np.all(p[over, 2] <= shin_bottom(p[over, 0], p[over, 1]) - GAP + 1e-3))
        # Sideways clearance from the post at every height (it reaches the plate).
        dx = np.maximum(np.maximum(9.0 - p[:, 0], p[:, 0] - 11.0), 0)
        dy = np.maximum(np.maximum(4.0 - p[:, 1], p[:, 1] - 6.0), 0)
        self.assertTrue(np.all(np.hypot(dx, dy) >= XY - TOL))
        self.assertGreater(support.near_area, 0.9 * support.eligible_area)
        self.assertGreaterEqual(support.gap_median, GAP)

    def test_flared_post_and_obstacle_beside_a_wall_end_keep_clearance(self):
        # Square frustum: 1 mm wide on the plate, 5 mm wide at z = 12, so its
        # sides overhang the grid below the shin.
        flare = frustum((9.5, 4.5, 0.0), (10.5, 5.5), 12.0, 2.5, 2.5)
        support = build([SHIN, flare])
        p = sample_points(support.vertices, support.faces)
        half = 0.5 + (2.5 - 0.5) * np.clip(p[:, 2] / 12.0, 0, 1)   # half size at each height
        half_up = 0.5 + 2.0 * np.clip((p[:, 2] + GAP) / 12.0, 0, 1)
        dx = np.maximum(np.abs(p[:, 0] - 10.0) - half, 0)
        dy = np.maximum(np.abs(p[:, 1] - 5.0) - half, 0)
        self.assertTrue(np.all(np.hypot(dx, dy) >= XY - TOL), 'beside the flare')
        inside_up = (np.abs(p[:, 0] - 10.0) < half_up) & (np.abs(p[:, 1] - 5.0) < half_up)
        self.assertFalse(np.any(inside_up & (p[:, 2] > 0)), 'under the flare within the gap')
        self.assertEqual(pieces(support.vertices, support.faces), 1)

    def test_step_in_the_underside_keeps_walls_below_the_lower_side(self):
        stepped = slab(lambda x, y: np.where(x < 10.0, 8.0, 12.0), size=(20.0, 10.0),
                       n=(200, 20), top=16.0)
        support = build([stepped])
        p = sample_points(support.vertices, support.faces)
        low_side = p[:, 0] < 10.0
        near_step = (p[:, 0] >= 10.0) & (p[:, 0] - 10.0 < XY)
        self.assertTrue(np.all(p[low_side, 2] <= 8.0 - GAP + TOL))
        self.assertTrue(np.all(p[near_step, 2] <= 8.0 + TOL))
        self.assertTrue(np.all(p[:, 2] <= 12.0 - GAP + TOL))
        self.assertGreater(p[~low_side, 2].max(), 11.0, 'the high side is still supported')

    def test_tiny_patch_between_lattice_lines_gets_its_own_cross(self):
        # 0.9 mm square at z = 5, centred between the x and y lines at 0 and 2.
        patch = box((0.55, 0.55, 5.0), (1.45, 1.45, 8.0))
        support = build([patch])
        self.assertGreaterEqual(support.fallback_crosses, 1)
        self.assertEqual(support.missed_area, 0.0)
        p = sample_points(support.vertices, support.faces)
        self.assertGreater(p[:, 2].max(), 4.0)
        self.assertTrue(np.all(p[:, 2] <= 5.0 - GAP + TOL))

    def test_fin_splitting_the_base_is_routed_around(self):
        # The fin reaches the plate and runs 3 mm past both edges of the
        # underside, farther than the base's own margin, so only a joining
        # strip routed around one end can make the grid one piece.
        flat = slab(lambda x, y: np.full_like(x, 10.0), size=(20.0, 10.0), n=(20, 10), top=14.0)
        fin = box((9.8, -3.0, 0.0), (10.2, 13.0, 10.5))
        support = build([flat, fin])
        self.assertGreater(support.base_pieces, 1)
        self.assertGreaterEqual(support.connectors, 1)
        self.assertEqual(pieces(support.vertices, support.faces), 1)
        p = sample_points(support.vertices, support.faces)
        dx = np.maximum(np.maximum(9.8 - p[:, 0], p[:, 0] - 10.2), 0)
        dy = np.maximum(np.maximum(-3.0 - p[:, 1], p[:, 1] - 13.0), 0)
        self.assertTrue(np.all(np.hypot(dx, dy) >= XY - TOL))

    def test_corridor_narrower_than_a_strip_is_not_used(self):
        # Two undersides 8 mm apart (too far for the base to merge them) with
        # a plate-reaching barrier between them.  Its 1.5 mm slot on the
        # straight line is too narrow for a 2 mm strip plus clearance, so the
        # strip must go around an end of the barrier, not through the slot.
        flat = slab(lambda x, y: np.full_like(x, 10.0), size=(6.0, 10.0), n=(6, 10), top=14.0)
        lower = box((9.8, -3.0, 0.0), (10.2, 4.25, 10.5))
        upper = box((9.8, 5.75, 0.0), (10.2, 13.0, 10.5))
        support = build([flat, shifted(flat, dx=14.0), lower, upper])
        self.assertGreaterEqual(support.connectors, 1)
        self.assertEqual(pieces(support.vertices, support.faces), 1)
        p = sample_points(support.vertices, support.faces)
        in_slot = (np.abs(p[:, 0] - 10.0) < 0.2 + XY) & (p[:, 1] > 4.0) & (p[:, 1] < 6.0)
        self.assertFalse(np.any(in_slot), 'strip squeezed through the slot')
        dx = np.maximum(np.abs(p[:, 0] - 10.0) - 0.2, 0)
        dy = np.where(p[:, 1] < 5.0, np.maximum(p[:, 1] - 4.25, 0), np.maximum(5.75 - p[:, 1], 0))
        dy = np.where((p[:, 1] < -3.0) | (p[:, 1] > 13.0),
                      np.minimum(np.abs(p[:, 1] + 3.0), np.abs(p[:, 1] - 13.0)), dy)
        self.assertTrue(np.all(np.hypot(dx, dy) >= XY - TOL), 'clearance at the strip bends')

    def test_enclosed_region_cannot_be_joined(self):
        flat = slab(lambda x, y: np.full_like(x, 10.0), size=(20.0, 10.0), n=(20, 10), top=14.0)
        ring = [box((3.0, 2.0, 0.0), (9.0, 2.4, 10.5)), box((3.0, 7.6, 0.0), (9.0, 8.0, 10.5)),
                box((3.0, 2.0, 0.0), (3.4, 8.0, 10.5)), box((8.6, 2.0, 0.0), (9.0, 8.0, 10.5))]
        with self.assertRaisesRegex(gridsupport.GridError, 'one piece'):
            build([flat, *ring])

    def test_too_low_for_base_wall_and_gap(self):
        low = slab(lambda x, y: np.full_like(x, 0.7), size=(10.0, 10.0), n=(10, 10), top=3.0)
        with self.assertRaisesRegex(gridsupport.GridError, 'no room'):
            build([low])

    def test_lattice_phase_and_thicker_base(self):
        for dx, dy in ((0.0, 0.0), (0.37, 1.13), (1.0, 1.0)):
            with self.subTest(offset=(dx, dy)):
                support = build([shifted(SHIN, dx, dy)], base_height=0.45, min_wall=0.2)
                v = support.vertices
                self.assertTrue(np.any(np.isclose(v[:, 2], 0.45)))
                self.assertFalse(np.any((v[:, 2] > 1e-9) & (v[:, 2] < 0.45 - 1e-9)))
                self.assertEqual(support.missed_area, 0.0)
                self.assertEqual(pieces(v, support.faces), 1)

    def test_settings_are_validated(self):
        for bad in (dict(pitch=0.4, width=0.42), dict(gap=-0.1), dict(cell=0.5),
                    dict(xy_distance=float('nan'))):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    gridsupport.GridSettings(**bad)


class Cli(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'plate.3mf')
        self.out = os.path.join(self.dir.name, 'plate.grid.3mf')

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, parts, *args, settings=SETTINGS, overrides=None, extra_member=None):
        objects = {'2': dict(parts=[dict(id=str(i + 1), mesh=m, **(dict(overrides=o) if o else {}))
                                    for i, (m, o) in enumerate(parts)],
                             overrides=overrides or {})}
        write_3mf(self.path, objects, [('2', translate(x=60.0, y=50.0, rotate_z_deg=20), True)],
                  settings=settings)
        if extra_member:
            with zipfile.ZipFile(self.path, 'a') as archive:
                archive.writestr(extra_member, 'object_id=2|0|0.2\n')
        out = io.StringIO()
        code = support_3mf.main([self.path, '--layout', 'grid', *args], out=out)
        return code, out.getvalue()

    def shin_parts(self, **post_overrides):
        return [(SHIN, None), (box((9.0, 4.0, 0.0), (11.0, 6.0, 10.5)), post_overrides or None)]

    def test_writes_one_grid_part_and_leaves_the_model_unchanged(self):
        code, text = self.run_cli(self.shin_parts(), '--max-height', '30')
        self.assertEqual(code, support_3mf.EXIT_WRITTEN, text)
        self.assertIn('[GRID]', text)
        self.assertIn('base: one piece', text)
        before = bambu3mf.read(self.path).instances[0]
        after = bambu3mf.read(self.out).instances[0]
        grids = [p for p in after.parts if p.name == support_3mf.GRID_PART_NAME]
        self.assertEqual(len(grids), 1)
        kept = [p for p in after.parts if p.name != support_3mf.GRID_PART_NAME]
        self.assertEqual(len(kept), len(before.parts))
        for a, b in zip(before.parts, kept):
            np.testing.assert_allclose(a.vertices, b.vertices, atol=1e-6)
            np.testing.assert_array_equal(a.faces, b.faces)
        g = grids[0]
        self.assertAlmostEqual(g.vertices[:, 2].min(), 0.0, places=5)
        self.assertEqual(pieces(g.vertices, g.faces), 1)

    def test_layout_options_are_checked(self):
        cases = [('grid without max-height', ['--layout', 'grid']),
                 ('overlap with grid', ['--layout', 'grid', '--max-height', '30', '--overlap', '0.1']),
                 ('gap with ribs', ['--gap', '0.2'])]
        write_3mf(self.path, {'2': dict(parts=[dict(id='1', mesh=SHIN)])},
                  [('2', np.eye(4), True)], settings=SETTINGS)
        for label, args in cases:
            with self.subTest(label):
                self.assertEqual(support_3mf.main([self.path, *args], out=io.StringIO()),
                                 support_3mf.EXIT_ERROR)

    def test_untrustworthy_clearance_settings_are_skipped(self):
        cases = {
            'part override': dict(parts=self.shin_parts(support_object_xy_distance='0.1')),
            'bad object value': dict(parts=self.shin_parts(),
                                     overrides={'support_object_xy_distance': '-1'}),
            'variable layers': dict(parts=self.shin_parts(),
                                    extra_member='Metadata/layer_heights_profile.txt'),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                code, text = self.run_cli(kwargs.pop('parts'), '--max-height', '30', **kwargs)
                self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
                self.assertIn('[SKIPPED]', text)
                self.assertFalse(os.path.exists(self.out))

    def test_unjoinable_support_is_skipped_not_split(self):
        flat = slab(lambda x, y: np.full_like(x, 10.0), size=(20.0, 10.0), n=(20, 10), top=14.0)
        ring = [box((3.0, 2.0, 0.0), (9.0, 2.4, 10.5)), box((3.0, 7.6, 0.0), (9.0, 8.0, 10.5)),
                box((3.0, 2.0, 0.0), (3.4, 8.0, 10.5)), box((8.6, 2.0, 0.0), (9.0, 8.0, 10.5))]
        code, text = self.run_cli([(flat, None)] + [(r, None) for r in ring], '--max-height', '30')
        self.assertEqual(code, support_3mf.EXIT_PARTIAL, text)
        self.assertIn('one piece', text)
        self.assertFalse(os.path.exists(self.out))

    def test_flat_model_needs_nothing(self):
        code, text = self.run_cli([(slab(n=(20, 20)), None)], '--max-height', '30')
        self.assertEqual(code, support_3mf.EXIT_NOTHING, text)
        self.assertFalse(os.path.exists(self.out))


if __name__ == '__main__':
    unittest.main()
