"""libs.jobmemory: the reservations batch admission makes per pass."""

import unittest

import numpy as np

from libs import jobmemory, winding
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds

BUDGET = 10_000_000_000


def mesh_of(*parts):
    verts, faces, offset = [], [], 0
    for v, f in parts:
        verts.append(np.asarray(v, np.float64)); faces.append(np.asarray(f, np.int64) + offset)
        offset += len(v)
    V, F = np.vstack(verts), np.vstack(faces)
    return Mesh('/m.stl', '/o.stl', Kind.BINARY_STL, len(F), True, None, Geometry(V, F))


def sphere(r=10.0, at=(0, 0, 0), seg=20):
    v, f = ds.sphere(r, seg)
    return np.asarray(v, np.float64) + at, f


class TestPrepare(unittest.TestCase):
    def test_grows_with_source_faces(self):
        small, big = jobmemory.prepare_bytes(1000), jobmemory.prepare_bytes(4_000_000)
        self.assertGreater(big, small)
        self.assertEqual(big - small,
                         jobmemory.PREPARE_BYTES_PER_FACE * (4_000_000 - 1000))

    def test_never_below_the_child_base(self):
        self.assertEqual(jobmemory.prepare_bytes(0), jobmemory.CHILD_BASE_BYTES)


class TestRepair(unittest.TestCase):
    def test_covers_the_part_reconstruction_plan(self):
        m = mesh_of(sphere())
        h = winding.grid_spacing(float(np.linalg.norm(np.ptp(m.geometry.verts, 0))))
        plan = winding.plan(m, h, BUDGET)
        self.assertGreaterEqual(jobmemory.repair_bytes(m, 100, BUDGET), plan.estimate_bytes)

    def test_covers_decimating_the_rebuilt_surface(self):
        """The term nothing else bounds: a big-area part's rebuilt surface
        costs more to decimate than its reconstruction plan."""
        m = mesh_of(sphere(132.0, seg=56))
        estimate = jobmemory.repair_bytes(m, 100, BUDGET)
        area = 4 * np.pi * 132.0 ** 2
        rebuilt = jobmemory.REBUILT_FACES_PER_UNIT * area / 0.15 ** 2
        self.assertGreaterEqual(estimate, jobmemory.DECIMATE_BYTES_PER_FACE * rebuilt * 0.98)

    def test_parts_are_a_maximum_not_a_sum(self):
        """Parts are repaired one after another: a second, equal part adds
        only what stays resident, never a second reconstruction."""
        one = jobmemory.repair_bytes(mesh_of(sphere(), sphere(at=(30, 0, 0))), 100, BUDGET)
        far = mesh_of(sphere(), sphere(at=(30, 0, 0)), sphere(at=(60, 0, 0)))
        self.assertLess(jobmemory.repair_bytes(far, 100, BUDGET), 1.5 * one)

    def test_shells_below_the_floor_are_not_rebuilt(self):
        """A debris shell dropped by the split must not reserve its own
        rebuild; with floor 0 it is kept and does."""
        big = sphere(10.0)
        debris = sphere(25.0, at=(60, 0, 0), seg=4)       # few faces, more area than `big`
        m = mesh_of(big, debris)
        n_debris = len(debris[1])
        dropped = jobmemory.repair_bytes(m, n_debris + 1, BUDGET)
        kept = jobmemory.repair_bytes(m, 0, BUDGET)
        self.assertLess(dropped, kept)

    def test_a_part_over_the_reconstruction_budget_reserves_the_budget(self):
        m = mesh_of(sphere())
        tiny = 1_000_000                               # below any plan's floor
        with self.assertRaises(winding.BudgetError):
            h = winding.grid_spacing(float(np.linalg.norm(np.ptp(m.geometry.verts, 0))))
            winding.plan(m, h, tiny)
        self.assertGreaterEqual(jobmemory.repair_bytes(m, 100, tiny), tiny)

    def test_a_ceiling_reserves_the_fallback_plan_the_step_will_use(self):
        m = mesh_of(sphere())
        tiny = 1_000_000
        h = winding.grid_spacing(float(np.linalg.norm(np.ptp(m.geometry.verts, 0))))
        fallback = winding.plan(m, h, tiny, 10 ** 12)
        self.assertGreater(fallback.estimate_bytes, tiny)
        self.assertGreaterEqual(
            jobmemory.repair_bytes(m, 100, tiny, 'winding', 10 ** 12), fallback.estimate_bytes)

    def test_an_unloaded_mesh_raises(self):
        with self.assertRaises(ValueError):
            jobmemory.repair_bytes(Mesh('/m', '/o', Kind.BINARY_STL, 4, True), 100, BUDGET)


class TestRepairAlphaWrap(unittest.TestCase):
    """`reconstruction='alpha-wrap'`: no block plan and no budget; the wrap
    follows the part's A/alpha² and its own face count."""

    def alpha(self, m, budget=BUDGET, floor=100):
        return jobmemory.repair_bytes(m, floor, budget, 'alpha-wrap')

    def test_ignores_the_winding_plan_and_its_budget(self):
        m = mesh_of(sphere())
        tiny = 1_000_000                               # winding would plan, then fail
        self.assertEqual(self.alpha(m, budget=tiny), self.alpha(m))
        self.assertLess(self.alpha(m, budget=tiny), jobmemory.repair_bytes(m, 100, tiny))

    def test_covers_the_wrap_of_a_part(self):
        """A large-area part: the wrap's own term, from the part's area and
        faces at the whole model's alpha, is inside the estimate."""
        v, f = sphere(132.0, seg=56)
        m = mesh_of((v, f))
        alpha = 0.15                                   # diag > 120 mm: capped
        units = 4 * np.pi * 132.0 ** 2 / alpha ** 2
        wrap = (jobmemory.ALPHA_BYTES_PER_UNIT * units
                + jobmemory.ALPHA_BYTES_PER_INPUT_FACE * len(f))
        self.assertGreaterEqual(self.alpha(m), wrap * 0.98)

    def test_grows_with_the_parts_own_faces_at_the_same_area(self):
        coarse, fine = mesh_of(sphere(seg=20)), mesh_of(sphere(seg=80))
        self.assertGreater(self.alpha(fine) - self.alpha(coarse),
                           jobmemory.ALPHA_BYTES_PER_INPUT_FACE
                           * (fine.triangles - coarse.triangles) * 0.9)

    def test_every_part_uses_the_whole_models_alpha(self):
        """A small part next to a large one wraps at the large model's
        alpha: scaling the model up (alpha capped) changes its estimate,
        a lone small part at its own diagonal would not."""
        small = sphere(5.0)
        alone = self.alpha(mesh_of(small))
        beside_big = self.alpha(mesh_of(small, sphere(60.0, at=(200, 0, 0))))
        big_only = self.alpha(mesh_of(sphere(60.0, at=(200, 0, 0))))
        # The small part's wrap at the whole model's (larger) alpha costs
        # less than at its own small alpha: its share is below `alone`.
        self.assertLess(beside_big - big_only, alone)

    def test_parts_are_a_maximum_not_a_sum(self):
        # Same extent (so the same alpha): the middle sphere is the only change.
        one = self.alpha(mesh_of(sphere(), sphere(at=(60, 0, 0))))
        three = self.alpha(mesh_of(sphere(), sphere(at=(30, 0, 0)), sphere(at=(60, 0, 0))))
        self.assertLess(three, 1.5 * one)
        self.assertGreater(three, one)                 # finished parts stay resident

    def test_an_unknown_reconstruction_raises(self):
        with self.assertRaises(ValueError):
            jobmemory.repair_bytes(mesh_of(sphere()), 100, BUDGET, 'marching-cubes')


if __name__ == '__main__':
    unittest.main()
