"""Tests for libs.ribsupport — breakaway rib geometry.

Underside maps come from basecheck run on closed height-field slabs
(print_fixtures.slab), so the expected rib heights follow from the slab's
bottom function on paper.  Settings mirror the user's preset (layer 1 sliced
at 0.1 mm, nothing supportable below 0.44 mm).
"""

import unittest

import numpy as np

from libs import basecheck, ribsupport
from libs.basecheck import Thresholds
from libs.ribsupport import RibSettings
from tests.tests.print_fixtures import slab

TH = Thresholds(first_layer=0.2, support_gap=0.24, support_angle=40.0)
RS = RibSettings(pitch=2.0, width=0.42, overlap=0.05)


def grid_of(bottom, n=(80, 80), th=TH):
    return basecheck.check([slab(bottom, n=n)], th, keep_grid=True).grid


def make(grid, mask=None, settings=RS):
    return ribsupport.ribs(grid.height, grid.flagged if mask is None else mask,
                           grid.x0, grid.y0, grid.cell, settings)


def recess(depth, floor=None):
    def bottom(x, y):
        inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
        return np.where(inside, depth if floor is None else floor(x, y), 0.0)
    return bottom


def signed_volume(v, f):
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6)


class Solid(unittest.TestCase):

    def assertSolid(self, ribs):
        v, f = ribs.vertices, ribs.faces
        self.assertTrue(np.isfinite(v).all())
        self.assertTrue(basecheck._closed(f), 'every edge must belong to exactly two faces')
        area = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1)
        self.assertTrue((area > 1e-12).all(), 'no degenerate triangles')
        self.assertGreater(signed_volume(v, f), 0, 'outward winding')
        self.assertAlmostEqual(v[:, 2].min(), 0.0)


class TestRecess(Solid):

    def test_ribs_fill_a_shallow_recess_from_plate_to_underside(self):
        ribs = make(grid_of(recess(0.3)))
        self.assertSolid(ribs)
        tops = ribs.vertices[ribs.vertices[:, 2] > 0, 2]
        np.testing.assert_allclose(tops, 0.3 + RS.overlap, atol=1e-9)
        xy = ribs.vertices[:, :2]
        self.assertTrue((xy > 2.5 - RS.width).all() and (xy < 7.5 + RS.width).all())

    def test_ribs_are_one_pitch_apart(self):
        ribs = make(grid_of(recess(0.3)))
        # Square region: ribs run along x, so each wall has two y faces.
        lines = np.unique(np.round(ribs.vertices[:, 1], 6))
        centres = (lines[0::2] + lines[1::2]) / 2
        np.testing.assert_allclose(np.diff(centres), RS.pitch, atol=1e-9)
        np.testing.assert_allclose(lines[1::2] - lines[0::2], RS.width, atol=1e-9)
        self.assertGreaterEqual(len(centres), 2)

    def test_slope_across_the_rib_keeps_both_edges_in_contact(self):
        def floor(x, y):
            return 0.2 + 0.04 * (y - 2.5)               # rises 0.04 mm per mm in y
        grid = grid_of(recess(None, floor))
        ribs = make(grid)
        self.assertSolid(ribs)
        top = ribs.vertices[ribs.vertices[:, 2] > 0]
        # Square region: ribs run along x, each wall's faces at y = line +- width/2.
        faces_y = np.unique(np.round(top[:, 1], 6))
        self.assertGreaterEqual(len(faces_y), 4)
        for low, high in zip(faces_y[0::2], faces_y[1::2]):
            z = top[np.isclose(top[:, 1], low) | np.isclose(top[:, 1], high), 2]
            # The top is the higher edge's underside plus the overlap, to
            # within one cell of slope, so the low edge is inside the model too.
            np.testing.assert_allclose(z, floor(0, high) + RS.overlap,
                                       atol=0.04 * grid.cell + 1e-9)
            self.assertTrue((z > floor(0, low)).all())

    def test_a_step_across_the_rib_splits_it_instead_of_lifting_it(self):
        # Recess at 0.3 next to contact at 0: samples spanning the wall are
        # dropped, so no rib top is anywhere but the recess floor.
        ribs = make(grid_of(recess(0.3)))
        tops = np.unique(np.round(ribs.vertices[ribs.vertices[:, 2] > 0, 2], 9))
        np.testing.assert_allclose(tops, [0.35])


class TestShapes(Solid):

    def synthetic(self, mask, height=0.3, cell=0.05):
        h = np.where(mask, height, np.nan)
        return ribsupport.ribs(h, mask, 0.0, 0.0, cell, RS)

    def test_a_single_cell_still_makes_a_closed_wall(self):
        mask = np.zeros((40, 40), bool)
        mask[20, 20] = True
        ribs = ribsupport.ribs(np.where(mask, 0.3, np.nan), mask, 0.0, 0.0, 0.05,
                               RibSettings(min_area=0))
        self.assertEqual(ribs.walls, 1)
        self.assertSolid(ribs)
        self.assertAlmostEqual(signed_volume(ribs.vertices, ribs.faces),
                               0.42 * 0.05 * 0.35, places=9)

    def test_a_narrow_strip_between_rib_positions_gets_a_rib(self):
        mask = np.zeros((200, 200), bool)
        mask[10:190, 100:112] = True                      # 0.6 mm wide, 9 mm long, along y
        ribs = self.synthetic(mask)
        self.assertGreaterEqual(ribs.walls, 1)
        self.assertSolid(ribs)
        # Runs along the strip (y), centred across it.
        self.assertGreater(np.ptp(ribs.vertices[:, 1]), 8.5)
        self.assertAlmostEqual(ribs.vertices[:, 0].mean(), (100 + 6) * 0.05, delta=0.05)

    def test_regions_below_min_area_get_nothing(self):
        mask = np.zeros((40, 40), bool)
        mask[5:8, 5:8] = True                             # 0.0225 mm²
        self.assertTrue(self.synthetic(mask).empty)

    def test_no_mask_no_ribs(self):
        ribs = self.synthetic(np.zeros((10, 10), bool))
        self.assertTrue(ribs.empty)
        self.assertEqual(ribs.faces.shape, (0, 3))


class TestSettings(unittest.TestCase):

    def test_invalid_settings_are_refused(self):
        for bad in (dict(width=2.0, pitch=2.0), dict(width=0), dict(pitch=float('nan')),
                    dict(overlap=-0.1), dict(jump=0), dict(min_area=-1)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    RibSettings(**bad)


if __name__ == '__main__':
    unittest.main()
