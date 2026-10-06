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

    def test_an_unloaded_mesh_raises(self):
        with self.assertRaises(ValueError):
            jobmemory.repair_bytes(Mesh('/m', '/o', Kind.BINARY_STL, 4, True), 100, BUDGET)


if __name__ == '__main__':
    unittest.main()
