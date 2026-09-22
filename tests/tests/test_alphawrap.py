"""Tests for libs.alphawrap — the CGAL Alpha Wrapping module.

Unlike every other tool module, alpha wrapping reconstructs the surface
rather than editing it, so there is no vertex/index correspondence to check
between input and output — only the topology guarantee CGAL itself makes
(watertight, manifold) and this module's own boundary contract (dtypes,
shapes, identity, no mutation, no silent-empty result).
"""

import math
import unittest
from unittest import mock

import numpy as np

import libs.alphawrap as alphawrap
from libs.alphawrap import is_available, wrap
from libs.mesh_io import Geometry, Kind, Mesh
from libs.scanner import scan


def _sphere(subdivisions=1):
    """A small icosphere as (verts, faces) — real closed geometry.

    Alpha wrapping is far slower than decimation per face, so this stays at
    subdivisions=1 (80 faces) rather than test_decimator's 4 (5120 faces).
    """
    t = (1.0 + 5.0 ** 0.5) / 2.0
    verts = np.array([
        [-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0],
        [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t],
        [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1],
    ], dtype=np.float64)
    faces = np.array([
        [0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11],
        [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8],
        [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9],
        [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1],
    ], dtype=np.int64)

    for _ in range(subdivisions):
        midpoint = {}
        new_faces = []
        verts = list(verts)

        def mid(a, b):
            key = (min(a, b), max(a, b))
            if key not in midpoint:
                m = (np.asarray(verts[a]) + np.asarray(verts[b])) / 2.0
                m = m / np.linalg.norm(m) * np.linalg.norm(verts[a])
                verts.append(m)
                midpoint[key] = len(verts) - 1
            return midpoint[key]

        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            new_faces += [[a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]]
        verts = np.array(verts, dtype=np.float64)
        faces = np.array(new_faces, dtype=np.int64)

    return verts.astype(np.float32), faces


def _mesh(verts, faces):
    return Mesh(path='sphere.stl', destination='sphere.out.stl',
                kind=Kind.BINARY_STL, triangles=len(faces), is_valid=True,
                geometry=Geometry(verts, faces))


def _unloaded():
    return Mesh(path='sphere.stl', destination='sphere.out.stl',
                kind=Kind.BINARY_STL, triangles=None, is_valid=True)


class TestAvailability(unittest.TestCase):

    def test_is_available_when_the_package_is_present(self):
        with mock.patch.object(alphawrap, '_CGAL', True):
            self.assertTrue(is_available())

    def test_is_available_is_false_with_nothing_at_all(self):
        with mock.patch.object(alphawrap, '_CGAL', False):
            self.assertFalse(is_available())


class TestParameterValidation(unittest.TestCase):

    def setUp(self):
        self.verts, self.faces = _sphere(subdivisions=1)
        self.mesh = _mesh(self.verts, self.faces)

    def test_an_unloaded_mesh_raises(self):
        with self.assertRaises(ValueError):
            wrap(_unloaded(), alpha=0.5, offset=0.1)

    def test_zero_alpha_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=0.0, offset=0.1)

    def test_negative_alpha_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=-0.5, offset=0.1)

    def test_nan_alpha_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=math.nan, offset=0.1)

    def test_infinite_alpha_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=math.inf, offset=0.1)

    def test_zero_offset_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=0.5, offset=0.0)

    def test_negative_offset_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=0.5, offset=-0.1)

    def test_nan_offset_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=0.5, offset=math.nan)

    def test_infinite_offset_raises(self):
        with self.assertRaises(ValueError):
            wrap(self.mesh, alpha=0.5, offset=math.inf)

    def test_missing_cgal_raises(self):
        with mock.patch.object(alphawrap, '_CGAL', False):
            with self.assertRaises(ValueError) as ctx:
                wrap(self.mesh, alpha=0.5, offset=0.1)
        self.assertIn('cgal', str(ctx.exception).lower())


@unittest.skipUnless(is_available(), 'cgal is not installed')
class TestWrap(unittest.TestCase):

    def setUp(self):
        self.verts, self.faces = _sphere(subdivisions=1)
        self.mesh = _mesh(self.verts, self.faces)
        # A loose alpha/offset relative to this small sphere's own scale
        # (diameter ~2.0) -- not tuned for fidelity, only fast and non-empty.
        self.alpha = 0.5
        self.offset = 0.2

    def test_output_is_watertight_and_manifold(self):
        """CGAL's own unconditional guarantee -- fair to assert directly."""
        result = wrap(self.mesh, self.alpha, self.offset)
        s = scan(result)
        self.assertEqual(s.open_edges, 0)
        self.assertEqual(s.non_manifold, 0)

    def test_output_boundary_contract(self):
        result = wrap(self.mesh, self.alpha, self.offset)
        g = result.geometry
        self.assertEqual(g.verts.ndim, 2)
        self.assertEqual(g.verts.shape[1], 3)
        self.assertEqual(g.faces.ndim, 2)
        self.assertEqual(g.faces.shape[1], 3)
        self.assertEqual(g.verts.dtype, np.float32)
        self.assertEqual(g.faces.dtype, np.int64)
        self.assertGreater(len(g.verts), 0)
        self.assertGreater(len(g.faces), 0)
        self.assertGreaterEqual(int(g.faces.min()), 0)
        self.assertLess(int(g.faces.max()), len(g.verts))
        self.assertEqual(result.triangles, len(result.geometry.faces))

    def test_identity_is_preserved(self):
        result = wrap(self.mesh, self.alpha, self.offset)
        self.assertEqual(result.path, self.mesh.path)
        self.assertEqual(result.destination, self.mesh.destination)
        self.assertEqual(result.kind, self.mesh.kind)

    def test_the_input_mesh_is_not_mutated(self):
        verts_before = self.mesh.geometry.verts.copy()
        faces_before = self.mesh.geometry.faces.copy()
        wrap(self.mesh, self.alpha, self.offset)
        np.testing.assert_array_equal(self.mesh.geometry.verts, verts_before)
        np.testing.assert_array_equal(self.mesh.geometry.faces, faces_before)

    def test_empty_cgal_result_raises(self):
        """`alpha_wrap_3` itself does not raise on a non-positive parameter
        -- it silently returns an empty mesh (confirmed by direct probing,
        not assumed).  No positive alpha/offset was found that reproduces
        an empty result on this fixture, so the guard is exercised directly
        against a faked CGAL return rather than left unverified.
        """
        empty = mock.Mock()
        empty.size_of_facets.return_value = 0
        empty.size_of_vertices.return_value = 0
        with mock.patch.object(alphawrap, '_Polyhedron_3', return_value=empty):
            with mock.patch.object(alphawrap, '_alpha_wrap_3'):
                with self.assertRaises(ValueError):
                    wrap(self.mesh, self.alpha, self.offset)


if __name__ == '__main__':
    unittest.main(verbosity=2)
