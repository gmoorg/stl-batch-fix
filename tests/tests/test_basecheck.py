"""Tests for libs.basecheck — base-layer risk predictions on synthetic solids.

Each solid is a closed height-field slab (print_fixtures.slab) whose bottom
is a known function, so the expected areas and heights are derivable on
paper.  Settings mirror the user's preset: 0.2 mm first layer, 0.24 mm
support top gap, 40 degree threshold — so layer 1 is sliced at 0.1 mm and an
underside below 0.44 mm has no room for support.
"""

import unittest

import numpy as np

from libs import basecheck
from libs.basecheck import Thresholds, clip_above, clip_below
from tests.tests.print_fixtures import slab

TH = Thresholds(first_layer=0.2, layer_height=0.2, support_gap=0.24, support_angle=40.0)


def run(*parts, th=TH):
    return basecheck.check(list(parts), th)


def projected_area(tris):
    u = tris[:, 1, :2] - tris[:, 0, :2]
    v = tris[:, 2, :2] - tris[:, 0, :2]
    return 0.5 * np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]).sum()


class TestClipping(unittest.TestCase):

    def test_pieces_above_and_below_add_up_to_the_triangle(self):
        rng = np.random.default_rng(1)
        tris = rng.uniform(-1, 1, (200, 3, 3))
        above, _ = clip_above(tris, 0.1)
        below, _ = clip_below(tris, 0.1)
        self.assertAlmostEqual(projected_area(above) + projected_area(below),
                               projected_area(tris), places=9)
        self.assertTrue((above[:, :, 2] >= 0.1 - 1e-12).all())
        self.assertTrue((below[:, :, 2] <= 0.1 + 1e-12).all())


class TestFlatAndSunk(unittest.TestCase):

    def test_flat_slab_has_full_contact_and_no_risk(self):
        result = run(slab())
        self.assertEqual(result.risks, ())
        self.assertEqual(result.incomplete, ())
        self.assertAlmostEqual(result.base.contact_fraction, 1.0, places=6)
        self.assertAlmostEqual(result.base.footprint_area, 100.0, delta=1.5)
        self.assertEqual(result.unsupportable, ())

    def test_slab_sunk_into_the_plate_is_flat_contact(self):
        result = run(slab(z_offset=-0.5))
        self.assertEqual(result.risks, ())
        self.assertEqual(result.incomplete, ())
        self.assertAlmostEqual(result.base.contact_fraction, 1.0, places=6)

    def test_overlapping_sunk_solids_do_not_cancel(self):
        # Two copies of one sunk slab: a shared crossing count would be even
        # everywhere (XOR) and lose all contact.  Per-part parity keeps it.
        result = run(slab(z_offset=-0.5), slab(z_offset=-0.5, origin=(5.0, 0.0)))
        self.assertEqual(result.risks, ())
        self.assertAlmostEqual(result.base.contact_area, 150.0, delta=2.0)

    def test_fully_submerged_solid_gives_no_contact(self):
        result = run(slab(top=-1.0, z_offset=-3.0))
        self.assertTrue(any('no plate contact' in r for r in result.risks))

    def test_elevated_slab_over_submerged_one_is_still_elevated(self):
        submerged = slab(top=2.0, z_offset=-3.0)            # z -3 .. -1
        floating = slab(top=2.0, z_offset=0.3)              # z 0.3 .. 2.3
        result = run(submerged, floating)
        self.assertEqual(result.base.contact_area, 0.0)
        self.assertTrue(any('no layer-1 contact' in r for r in result.risks))
        self.assertAlmostEqual(sum(r.area for r in result.unsupportable), 100.0, delta=2.0)
        self.assertIsNone(result.unsupportable[0].reach)

    def test_open_sunk_part_is_approximated_and_incomplete(self):
        vertices, faces = slab(z_offset=-0.5)
        result = run((vertices, faces[2:]))                 # one bottom quad half missing
        self.assertTrue(any('not a closed solid' in r for r in result.incomplete))
        self.assertGreater(result.base.contact_fraction, 0.95)


class TestUnsupportable(unittest.TestCase):

    def test_shallow_recess_is_flagged_with_its_area(self):
        def bottom(x, y):
            inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
            return np.where(inside, 0.3, 0.0)
        # 0.125 mm grid: the recess wall is one grid step wide, so the flat
        # recess floor spans between 4.75 and 5 mm per side.
        result = run(slab(bottom, n=(80, 80)))
        self.assertEqual(len(result.unsupportable), 1)
        region = result.unsupportable[0]
        self.assertGreater(region.area, 4.6 ** 2)
        self.assertLess(region.area, 5.1 ** 2)
        self.assertAlmostEqual(region.z_min, 0.3, delta=0.02)
        self.assertAlmostEqual(region.centroid[0], 5.0, delta=0.1)
        self.assertAlmostEqual(region.reach, 2.5, delta=0.2)
        self.assertTrue(any('too low for support' in r for r in result.risks))

    def test_recess_deep_enough_for_support_is_not_flagged(self):
        def bottom(x, y):
            inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
            return np.where(inside, 1.0, 0.0)
        result = run(slab(bottom, n=(80, 80)))
        self.assertEqual(result.unsupportable, ())

    def test_45_degree_chamfered_edge_is_self_supporting(self):
        def bottom(x, y):
            edge = np.minimum.reduce([x, 10 - x, y, 10 - y])
            return np.maximum(0.0, 1.0 - edge)              # 1 mm, 45 degrees
        result = run(slab(bottom, n=(80, 80)))
        self.assertEqual(result.unsupportable, ())
        self.assertEqual(result.risks, ())

    def test_slight_tilt_lifts_part_of_a_flat_base_off_layer_1(self):
        # 0.2 mm rise over 10 mm: half the base is above the 0.1 mm slice.
        result = run(slab(lambda x, y: 0.02 * x))
        self.assertAlmostEqual(result.base.contact_fraction, 0.5, delta=0.03)
        self.assertTrue(any('uneven base' in r for r in result.risks))
        self.assertAlmostEqual(result.base.sink, 0.19 - 0.1, delta=0.02)

    def test_small_foot_band_does_not_hide_unsupportable_regions(self):
        def bottom(x, y):
            inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
            return np.where(inside, 0.3, 0.0)
        th = Thresholds(first_layer=0.2, support_gap=0.24, support_angle=40.0, foot_height=0.2)
        result = run(slab(bottom, n=(80, 80)), th=th)
        self.assertEqual(len(result.unsupportable), 1)

    def test_supports_off_reports_higher_near_plate_overhangs(self):
        def bottom(x, y):
            inside = (x > 2.5) & (x < 7.5) & (y > 2.5) & (y < 7.5)
            return np.where(inside, 1.0, 0.0)
        th = Thresholds(first_layer=0.2, support_gap=0.24, support_angle=40.0,
                        supports_enabled=False)
        result = run(slab(bottom, n=(80, 80)), th=th)
        self.assertEqual(len(result.unsupported_overhangs), 1)


class TestRipples(unittest.TestCase):

    def test_ripples_crossing_the_slice_plane_fragment_contact(self):
        def bottom(x, y):
            return 0.075 * (1 - np.cos(2 * np.pi * x / 2.0))    # 0 .. 0.15 mm
        result = run(slab(bottom, n=(200, 40)))
        base = result.base
        self.assertGreaterEqual(base.contact_islands, 5)
        self.assertTrue(any('rippled base' in r for r in result.risks))
        xs = np.linspace(0, 10, 2001)
        h = 0.075 * (1 - np.cos(2 * np.pi * xs / 2.0))
        self.assertAlmostEqual(base.sink, np.quantile(h, 0.95) - 0.1, delta=0.02)

    def test_smooth_curved_underside_is_not_called_rippled(self):
        # A cylinder of radius 4 lying on the plate, as a height field.
        def bottom(x, y):
            return 4 - np.sqrt(np.clip(16 - (x - 5) ** 2, 0, None))
        result = run(slab(bottom, n=(200, 40)))
        self.assertFalse(any('rippled' in r for r in result.risks))
        self.assertEqual(result.base.contact_islands, 1)


class TestShallowSurfaces(unittest.TestCase):

    @staticmethod
    def ramp(n=(40, 40)):
        # A 5-degree ramp on top of a 1 mm plinth: top surface rises from 1.0
        # to 1.0 + 10 * tan(5 deg) = 1.875 mm, inside the 3 mm foot band.
        vertices, faces = slab(n=n, top=1.0)
        top = vertices[:, 2] > 0.5
        vertices = vertices.copy()
        vertices[top, 2] = 1.0 + vertices[top, 0] * np.tan(np.radians(5))
        return vertices, faces

    def test_shallow_ramp_above_the_base_is_found(self):
        result = run(self.ramp())
        self.assertEqual(len(result.shallow), 1)
        region = result.shallow[0]
        self.assertAlmostEqual(region.area, 100.0, delta=0.5)
        self.assertEqual(region.facing, 'up')
        self.assertEqual(result.risks, ())        # C alone never makes a risk

    def test_reversed_winding_still_finds_it(self):
        vertices, faces = self.ramp()
        result = run((vertices, faces[:, ::-1]))
        self.assertEqual(len(result.shallow), 1)
        self.assertEqual(result.shallow[0].facing, 'down')

    def test_area_is_clipped_to_the_band_and_tessellation_independent(self):
        th = Thresholds(first_layer=0.2, support_gap=0.24, support_angle=40.0, foot_height=1.5)
        coarse = run(self.ramp(n=(1, 1)), th=th)
        fine = run(self.ramp(n=(50, 50)), th=th)
        # The ramp is below 1.5 mm for x < 0.5 / tan(5 deg) = 5.715 mm.
        expected = 10 * 0.5 / np.tan(np.radians(5))
        self.assertAlmostEqual(coarse.shallow[0].area, expected, delta=0.05)
        self.assertAlmostEqual(fine.shallow[0].area, expected, delta=0.05)

    def test_horizontal_ledge_over_the_base_is_not_flagged(self):
        # A step: the top is flat at 1 mm and 2 mm, never shallow-but-tilted,
        # and the ledge sits over the base, outside check B's plate scope.
        vertices, faces = slab(n=(40, 40), top=1.0)
        vertices = vertices.copy()
        upper = (vertices[:, 2] > 0.5) & (vertices[:, 0] > 5.01)
        vertices[upper, 2] = 2.0
        result = run((vertices, faces))
        self.assertEqual(result.unsupportable, ())
        self.assertEqual(result.shallow, ())


class TestInputs(unittest.TestCase):

    def test_non_finite_coordinates_are_dropped_and_reported(self):
        vertices, faces = slab()
        vertices = vertices.copy()
        vertices[-1] = np.nan
        result = run((vertices, faces))
        self.assertTrue(any('non-finite' in r for r in result.incomplete))

    def test_degenerate_triangles_are_harmless(self):
        vertices, faces = slab()
        faces = np.concatenate([faces, [[0, 0, 1], [3, 3, 3]]])
        self.assertEqual(run((vertices, faces)).risks, ())

    def test_disconnected_feet_each_count_as_footprint(self):
        result = run(slab(size=(3.0, 3.0), n=(12, 12)),
                     slab(size=(3.0, 3.0), n=(12, 12), origin=(10.0, 0.0)))
        self.assertEqual(result.base.contact_islands, 2)
        self.assertEqual(result.base.footprint_regions, 2)
        self.assertEqual(result.risks, ())

    def test_invalid_thresholds_are_refused(self):
        for bad in (dict(cell=0), dict(cell=float('nan')), dict(first_layer=-0.2),
                    dict(support_angle=95), dict(min_contact_fraction=2),
                    dict(max_extra_islands=-1), dict(flat_angle=20, shallow_angle=10)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Thresholds(**bad)

    def test_huge_extent_is_coarsened_not_allocated(self):
        result = run(slab(size=(4000.0, 4000.0), n=(2, 2)))
        self.assertGreater(result.cell, TH.cell)
        self.assertTrue(any('coarsened' in n for n in result.notes))


if __name__ == '__main__':
    unittest.main()
