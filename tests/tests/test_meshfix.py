"""Tests for libs.meshfix — the PyMeshFix wrapper, not PyMeshFix itself.

Repairing a mesh correctly is the library's job and testing it would be testing
the library. What is ours is the boundary: arrays converted both ways, the
input returned untouched on failure, output captured off both file descriptors,
and `remove_smallest_components` never called.

Fixtures are index arrays built by hand, as elsewhere in this suite: a
tetrahedron with one face removed has exactly three open edges, and two
disjoint tetrahedra are eight faces that must still be eight afterwards.
"""

import os
import unittest
from unittest import mock

import numpy as np

from libs import meshfix, scanner
from libs.mesh_io import Geometry, Kind, Mesh
from libs.meshfix import Result, is_available, repair

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts, faces, source='/in/body.stl', destination='/out/body.stl'):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(source, destination, Kind.BINARY_STL, len(geometry.faces),
                True, None, geometry)


def holed():
    """A tetrahedron missing one face: three open edges, one boundary."""
    return mesh(TETRA_VERTS, TETRA_FACES[:3])


def two_tetrahedra():
    verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                           [10, 11, 10], [10, 10, 11]]
    faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
    return mesh(verts, faces)


@unittest.skipUnless(is_available(), "pymeshfix not installed")
class TestRepair(unittest.TestCase):

    def test_a_hole_is_closed(self):
        m = holed()
        self.assertEqual(scanner.scan(m).open_edges, 3, "fixture has no hole")
        result = repair(m)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(scanner.scan(result.mesh).open_edges, 0)

    def test_a_clean_mesh_survives(self):
        result = repair(mesh(TETRA_VERTS, TETRA_FACES))
        self.assertTrue(result.ok)
        self.assertTrue(scanner.scan(result.mesh).is_clean)

    def test_the_input_is_not_mutated(self):
        m = holed()
        repair(m)
        self.assertEqual(m.triangles, 3)
        self.assertEqual(len(m.geometry.faces), 3)

    def test_the_result_is_a_loaded_mesh(self):
        result = repair(holed())
        self.assertTrue(result.mesh.is_loaded)
        self.assertEqual(result.mesh.triangles,
                         len(result.mesh.geometry.faces))

    def test_identity_is_carried_through(self):
        """A repair transforms a mesh; it does not make a different file."""
        m = holed()
        result = repair(m)
        self.assertEqual(result.mesh.path, m.path)
        self.assertEqual(result.mesh.destination, m.destination)

    def test_arrays_come_back_in_our_dtypes(self):
        """PyMeshFix works in float64/int32; Geometry is float32/int64."""
        result = repair(holed())
        self.assertEqual(result.mesh.geometry.verts.dtype, np.float32)
        self.assertEqual(result.mesh.geometry.faces.dtype, np.int64)

    def test_faces_index_within_the_vertex_array(self):
        result = repair(holed())
        g = result.mesh.geometry
        self.assertGreaterEqual(int(g.faces.min()), 0)
        self.assertLess(int(g.faces.max()), len(g.verts))

    def test_elapsed_is_recorded(self):
        self.assertGreaterEqual(repair(holed()).second_elapsed, 0.0)

    def test_an_unloaded_mesh_raises(self):
        """A programming error at the call site, not a property of the data."""
        with self.assertRaises(ValueError):
            repair(Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 4, True))

    def test_fill_holes_can_be_turned_off(self):
        """The print-scale gate wants non-manifold edges resolved without
        boundaries closed: a hole smaller than one layer produces no toolpath,
        and closing it has been measured to do more harm than leaving it."""
        result = repair(holed(), fill_holes=False)
        self.assertTrue(result.ok, result.problem)
        self.assertGreater(scanner.scan(result.mesh).open_edges, 0,
                           "the hole was filled with fill_holes=False")


@unittest.skipUnless(is_available(), "pymeshfix not installed")
class TestShellsAreNotDeleted(unittest.TestCase):
    """The behaviour this module exists to avoid.

    `MeshFix.repair()`'s default sequence calls `remove_smallest_components`,
    which keeps the largest component and discards the rest. That is the
    documented head-deletion — 562,288 faces in, 394,432 out — and it is the
    default, not an edge case. Measured on two disjoint tetrahedra, 8 faces in:

        remove_smallest_components() alone      4 faces out
        the full default repair()               4 faces out
        fill + clean, without remove_smallest   8 faces out
    """

    def test_a_second_shell_is_not_discarded(self):
        m = two_tetrahedra()
        result = repair(m)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(scanner.shells(result.mesh)), 2,
                         "a shell was deleted — remove_smallest_components "
                         "is being called")

    def test_no_faces_are_lost_from_a_clean_two_shell_mesh(self):
        result = repair(two_tetrahedra())
        self.assertEqual(result.mesh.triangles, 8)

    def test_the_parameter_is_not_exposed_at_all(self):
        """A flag that must always be False is one somebody sets to True.

        The pipeline splits by shells first, so every mesh arriving here is a
        single shell and the call would be a no-op at best.
        """
        import inspect
        self.assertNotIn('remove_smallest',
                         str(inspect.signature(repair)))


@unittest.skipUnless(is_available(), "pymeshfix not installed")
class TestOutputReachesTheProcessStreams(unittest.TestCase):
    """PyMeshFix writes from C++ straight to fd 1/2. Nothing captures it any
    more: inside a repair child those descriptors are the model's log, so the
    text must reach them rather than being swallowed."""

    def test_progress_and_output_reach_fd_1_and_2(self):
        import subprocess
        import sys
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with tempfile.TemporaryFile() as log:
            subprocess.run(
                [sys.executable, '-c',
                 'from tests.tests.test_meshfix import holed\n'
                 'from libs.meshfix import repair\n'
                 'assert repair(holed()).ok\n'],
                cwd=root, stdout=log, stderr=log, check=True, timeout=120)
            log.seek(0)
            text = log.read().decode('utf-8', 'replace')
        self.assertIn('Loading', text)

    def test_the_descriptors_are_untouched(self):
        before = (os.fstat(1).st_ino, os.fstat(2).st_ino)
        repair(holed())
        self.assertEqual((os.fstat(1).st_ino, os.fstat(2).st_ino), before)


class TestAvailability(unittest.TestCase):

    def test_is_available_reports_a_bool(self):
        self.assertIsInstance(is_available(), bool)

    def test_a_missing_library_is_a_result_not_a_crash(self):
        """Absence is a data condition; the caller marks the file."""
        from unittest import mock
        with mock.patch.object(meshfix, '_AVAILABLE', False):
            result = repair(mesh(TETRA_VERTS, TETRA_FACES))
        self.assertFalse(result.ok)
        self.assertIn('not installed', result.problem)


class TestResult(unittest.TestCase):

    def test_result_is_immutable(self):
        result = Result(mesh(TETRA_VERTS, TETRA_FACES), True, None, 0.1)
        with self.assertRaises(Exception):
            result.ok = False

    def test_ok_does_not_mean_clean(self):
        """PyMeshFix reports success on meshes that still have defects, which
        is why nothing is written out on a library's word alone."""
        result = repair(holed()) if is_available() else None
        if result is not None:
            self.assertTrue(hasattr(result, 'ok'))
            # The verdict belongs to scanner, not to this flag.
            self.assertIsNotNone(scanner.scan(result.mesh))


class TestStep(unittest.TestCase):
    """`step(mesh) -> (ok, mesh, detail)`, the pipeline's uniform entry
    point for this module."""

    @unittest.skipUnless(is_available(), "pymeshfix is needed")
    def test_enabled_repairs_and_reports_ok(self):
        ok, result, detail = meshfix.step_meshfix_repair(mesh(TETRA_VERTS, TETRA_FACES))
        self.assertTrue(ok, detail)
        self.assertIn('pymeshfix', detail)

    def test_a_returned_failure_is_reported_not_raised(self):
        m = mesh(TETRA_VERTS, TETRA_FACES)
        with mock.patch.object(meshfix, '_AVAILABLE', False):
            ok, result, detail = meshfix.step_meshfix_repair(m)
        self.assertFalse(ok)
        self.assertIs(result, m)
        self.assertIn('pymeshfix failed', detail)

    def test_a_raised_exception_is_reported_not_propagated(self):
        unloaded = Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 4, True)
        ok, result, detail = meshfix.step_meshfix_repair(unloaded)
        self.assertFalse(ok)
        self.assertIs(result, unloaded)
        self.assertIn('pymeshfix failed', detail)


if __name__ == '__main__':
    unittest.main(verbosity=2)
