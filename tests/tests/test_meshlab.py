"""Tests for the PyMeshLab adapter and in-memory filter boundary."""

import unittest
from unittest import mock

import numpy as np

from libs import meshlab, pipeconfig, scanner
from libs.mesh_io import Geometry, Kind, Mesh

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

needs_meshlab = unittest.skipUnless(
    meshlab.is_available(), "pymeshlab is not installed")

#: The four CLEAN filters as one combined `apply_filters()` call — a
#: test-local reference sequence for comparing against the four separate
#: `step_clean_*` calls. Order matches `repairer.WHOLE_MESH_STEPS`.
CLEAN_FILTERS_COMBINED: tuple[tuple[str, dict], ...] = (
    ('meshing_remove_null_faces', {}),
    ('meshing_merge_close_vertices', {'threshold': 0.1}),
    ('meshing_remove_duplicate_faces', {}),
    ('meshing_remove_unreferenced_vertices', {}),
)


def mesh(verts, faces):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
                len(geometry.faces), True, None, geometry)


def tetra():
    return mesh(TETRA_VERTS, TETRA_FACES)


@needs_meshlab
class TestMeshLab(unittest.TestCase):

    def test_conversion_helpers_preserve_the_dtype_contract(self):
        geometry = Geometry(np.array(TETRA_VERTS, dtype=np.float32),
                            np.array(TETRA_FACES, dtype=np.int64))
        native = meshlab.to_mesh(geometry)
        self.assertEqual(native.vertex_matrix().dtype, np.float64)
        self.assertEqual(native.face_matrix().dtype, np.int32)

        converted = meshlab.from_mesh(native)
        self.assertEqual(converted.verts.dtype, np.float32)
        self.assertEqual(converted.faces.dtype, np.int64)
        self.assertTrue(converted.verts.flags.c_contiguous)
        self.assertTrue(converted.faces.flags.c_contiguous)
        np.testing.assert_array_equal(converted.verts, geometry.verts)
        np.testing.assert_array_equal(converted.faces, geometry.faces)

    def test_apply_filters_wraps_float_parameters(self):
        doubled = mesh(TETRA_VERTS, TETRA_FACES + TETRA_FACES)
        filters = (
            ('meshing_remove_null_faces', {}),
            ('meshing_merge_close_vertices', {'threshold': 0.1}),
            ('meshing_remove_duplicate_faces', {}),
            ('meshing_remove_unreferenced_vertices', {}),
        )
        result = meshlab.apply_filters(doubled, filters)
        self.assertEqual(len(result.geometry.faces), 4)

    def test_apply_filters_rejects_unloaded_mesh(self):
        unloaded = Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
                        4, True)
        with self.assertRaises(ValueError):
            meshlab.apply_filters(unloaded, ())


@needs_meshlab
class TestCleanAndOrientSteps(unittest.TestCase):
    """The five uniform steps built from a single PyMeshLab filter: the
    four CLEAN filters and orient, each its own `step(mesh) -> (ok, mesh,
    detail)` — split out from one combined `apply_filters()` call per the
    owner's explicit instruction, accepting the extra float round-trip
    noise that costs (see `tests.tests.test_repairer.TestLostVertices.
    test_float_noise_is_not_a_loss` for the single-round-trip baseline this
    compounds).
    """

    def test_each_clean_step_runs_its_own_flag(self):
        for step_fn, flag_name in (
            (meshlab.step_clean_null_faces, 'ENABLE_CLEAN_NULL_FACES'),
            (meshlab.step_clean_merge_close, 'ENABLE_CLEAN_MERGE_CLOSE'),
            (meshlab.step_clean_duplicate_faces,
             'ENABLE_CLEAN_DUPLICATE_FACES'),
            (meshlab.step_clean_unreferenced, 'ENABLE_CLEAN_UNREFERENCED'),
        ):
            with self.subTest(flag=flag_name):
                m = tetra()
                with mock.patch.object(pipeconfig, flag_name, False):
                    ok, result, detail = step_fn(m)
                self.assertTrue(ok)
                self.assertIs(result, m)
                self.assertIn(f'{flag_name}=False', detail)

    def test_orient_runs_its_own_flag(self):
        m = tetra()
        with mock.patch.object(pipeconfig, 'ENABLE_ORIENT', False):
            ok, result, detail = meshlab.step_orient(m)
        self.assertTrue(ok)
        self.assertIs(result, m)
        self.assertIn('orient skipped', detail)

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

        combined = meshlab.apply_filters(doubled, CLEAN_FILTERS_COMBINED)

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
