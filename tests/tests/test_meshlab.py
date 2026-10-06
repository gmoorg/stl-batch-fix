"""Tests for the PyMeshLab adapter and in-memory filter boundary."""

import unittest
from unittest import mock

import numpy as np

from libs import meshlab, scanner
from libs.mesh_io import Geometry, Kind, Mesh

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def clean_filters_combined() -> tuple[tuple[str, dict], ...]:
    """The four CLEAN filters as one combined `apply_filters()` call — a
    test-local reference sequence for comparing against the four separate
    `step_clean_*` calls. `repairer.WHOLE_MESH_STEPS` is empty by default in
    the uniform-step refactor (docs/refactor/TODO.md) — these filters are not
    wired into it; this exists only to give
    `test_running_the_four_steps_separately_matches_one_combined_call` a
    reference order."""
    return (
        ('meshing_remove_null_faces', {}),
        ('meshing_merge_close_vertices', {'threshold': meshlab.percent(0.1)}),
        ('meshing_remove_duplicate_faces', {}),
        ('meshing_remove_unreferenced_vertices', {}),
    )


def mesh(verts, faces):
    geometry = Geometry(np.array(verts, dtype=np.float64),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
                len(geometry.faces), True, None, geometry)


def tetra():
    return mesh(TETRA_VERTS, TETRA_FACES)


class TestMeshLab(unittest.TestCase):

    def test_conversion_helpers_preserve_the_dtype_contract(self):
        """Coordinates come back exactly: 0.1 is not exact in float32, so a
        rounding on the way back would show."""
        geometry = Geometry(np.array(TETRA_VERTS, dtype=np.float64) + 0.1,
                            np.array(TETRA_FACES, dtype=np.int64))
        native = meshlab.to_mesh(geometry)
        self.assertEqual(native.vertex_matrix().dtype, np.float64)
        self.assertEqual(native.face_matrix().dtype, np.int32)

        converted = meshlab.from_mesh(native)
        self.assertEqual(converted.verts.dtype, np.float64)
        self.assertEqual(converted.faces.dtype, np.int64)
        self.assertTrue(converted.verts.flags.c_contiguous)
        self.assertTrue(converted.faces.flags.c_contiguous)
        np.testing.assert_array_equal(converted.verts, geometry.verts)
        np.testing.assert_array_equal(converted.faces, geometry.faces)

    def test_to_mesh_drops_faces_with_a_repeated_corner(self):
        """They segfault quadric decimation on this path
        (docs/errors/decimation-segfault.md). Every other face keeps its
        order, every vertex is kept, and the caller's arrays are untouched."""
        verts = TETRA_VERTS + [[5, 5, 5], [6, 5, 5]]
        faces = TETRA_FACES[:2] + [[4, 5, 4], [0, 0, 1]] + TETRA_FACES[2:] + [[2, 3, 3]]
        geometry = Geometry(np.array(verts, dtype=np.float64),
                            np.array(faces, dtype=np.int64))
        verts_before, faces_before = geometry.verts.copy(), geometry.faces.copy()

        native = meshlab.to_mesh(geometry)

        np.testing.assert_array_equal(native.face_matrix(), TETRA_FACES)
        np.testing.assert_array_equal(native.vertex_matrix(),
                                      np.array(verts, dtype=np.float64))
        np.testing.assert_array_equal(geometry.verts, verts_before)
        np.testing.assert_array_equal(geometry.faces, faces_before)

    def test_a_percentage_threshold_welds_nearby_vertices(self):
        """`percent(0.1)` is 0.1 % of the diagonal: a second tetrahedron
        1e-5 away (distinct vertices, so duplicate-face removal alone could
        not do it) is welded onto the first."""
        near = [[x + 1e-5, y, z] for x, y, z in TETRA_VERTS]
        doubled = mesh(TETRA_VERTS + near,
                       TETRA_FACES + [[a + 4, b + 4, c + 4] for a, b, c in TETRA_FACES])
        result = meshlab.apply_filters(doubled, clean_filters_combined())
        self.assertEqual(len(result.geometry.verts), 4)
        self.assertEqual(len(result.geometry.faces), 4)

    def test_a_percent_is_plain_python_compared_by_value(self):
        self.assertEqual(meshlab.percent(0.1), meshlab.percent(0.1))
        self.assertNotEqual(meshlab.percent(0.1), 0.1)

    def test_a_plain_float_is_passed_as_is(self):
        """Quadric decimation's `qualitythr` is a plain float, not a
        percentage. Passing its default explicitly gives the same result as
        a call with every other parameter matched."""
        from libs import decimator
        from tests.tests.test_decimator import _sphere
        v, f = _sphere(subdivisions=3)
        m = mesh(v, f)
        name = 'meshing_decimation_quadric_edge_collapse'
        params = dict(targetfacenum=200, **decimator.QUADRIC_PARAMS)
        self.assertIsInstance(params['qualitythr'], float)
        explicit = meshlab.apply_filters(m, ((name, params),))
        again = meshlab.apply_filters(m, ((name, dict(params, qualitythr=0.3)),))
        np.testing.assert_array_equal(explicit.geometry.faces, again.geometry.faces)
        self.assertEqual(len(explicit.geometry.faces), 200)

    def test_apply_filters_rejects_unloaded_mesh(self):
        unloaded = Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
                        4, True)
        with self.assertRaises(ValueError):
            meshlab.apply_filters(unloaded, ())


class TestCleanAndOrientSteps(unittest.TestCase):
    """The five uniform steps built from a single PyMeshLab filter: the
    four CLEAN filters and orient, each its own `step(mesh) -> (ok, mesh,
    detail)` — split out from one combined `apply_filters()` call per the
    owner's explicit instruction, accepting the extra float round-trip
    noise that costs (see `tests.tests.test_repairer.TestLostVertices.
    test_float_noise_is_not_a_loss` for the single-round-trip baseline this
    compounds).
    """

    def test_a_filter_exception_is_reported_not_raised(self):
        m = tetra()
        with mock.patch.object(meshlab, 'apply_filters',
                               side_effect=ValueError("boom")):
            ok, result, detail = meshlab.step_clean_null_faces(m)
        self.assertFalse(ok)
        self.assertIs(result, m)
        self.assertIn('failed', detail)

    def test_running_the_four_steps_separately_matches_one_combined_call(self):
        """The regression this split risks: four independent PyMeshLab
        round trips (float64<->float32 each time) instead of one. Compare
        against the combined-call fixture rather than assume the extra
        rounding is harmless.
        """
        doubled_verts = [[x + 1e-5, y, z] for x, y, z in TETRA_VERTS]
        doubled = mesh(
            TETRA_VERTS + doubled_verts,
            TETRA_FACES + [[a + 4, b + 4, c + 4] for a, b, c in TETRA_FACES])

        combined = meshlab.apply_filters(doubled, clean_filters_combined())

        m = doubled
        for step_fn in (meshlab.step_clean_null_faces,
                       meshlab.step_clean_merge_close,
                       meshlab.step_clean_duplicate_faces,
                       meshlab.step_clean_unreferenced):
            ok, m, _ = step_fn(m)
            self.assertTrue(ok)

        self.assertEqual(len(m.geometry.faces), len(combined.geometry.faces))
        self.assertTrue(scanner.scan(m).is_clean)
        self.assertEqual(len(m.geometry.faces), len(TETRA_FACES))
