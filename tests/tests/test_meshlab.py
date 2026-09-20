"""Tests for the PyMeshLab adapter and in-memory filter boundary."""

import unittest

import numpy as np

from libs import meshlab
from libs.mesh_io import Geometry, Kind, Mesh

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

needs_meshlab = unittest.skipUnless(
    meshlab.is_available(), "pymeshlab is not installed")


def mesh(verts, faces):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
                len(geometry.faces), True, None, geometry)


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
