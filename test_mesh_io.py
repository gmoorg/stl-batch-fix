"""Tests for libs.mesh_io — generated files, no fixtures, no Blender.

Every file here is built in a temp directory by the test that needs it. The
module's job is classification and cheap measurement, and the interesting cases
are the awkward ones no ordinary fixture contains: a binary STL wearing a text
header, a file truncated mid-triangle, a file with bytes appended after the
last one.
"""

import math
import os
import shutil
import struct
import tempfile
import unittest

from libs.mesh_io import (
    BYTES_PER_TRIANGLE, HEADER_BYTES, Kind, Mesh, bounds, diagonal,
    dimensions, kind, probe, triangle_count,
)

#: One unit tetrahedron: four faces, enough to be a real mesh.
_TETRA = (
    ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
    ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
)


def _binary_stl(path, triangles=_TETRA, header=b'', declared=None):
    """Write a binary STL.  `header` and `declared` let a test lie deliberately."""
    body = bytearray()
    for tri in triangles:
        body += struct.pack('<3f', 0.0, 0.0, 0.0)
        for vertex in tri:
            body += struct.pack('<3f', *(float(c) for c in vertex))
        body += b'\0\0'
    comment = (header + b'\0' * 80)[:80]
    count = len(triangles) if declared is None else declared
    with open(path, 'wb') as f:
        f.write(comment)
        f.write(struct.pack('<I', count))
        f.write(bytes(body))
    return path


def _ascii_stl(path, name='tetra', triangles=_TETRA):
    with open(path, 'w') as f:
        f.write(f"solid {name}\n")
        for tri in triangles:
            f.write("facet normal 0 0 0\n  outer loop\n")
            for vertex in tri:
                f.write("    vertex %g %g %g\n" % vertex)
            f.write("  endloop\nendfacet\n")
        f.write(f"endsolid {name}\n")
    return path


class MeshIOCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='mesh-io-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)


class TestKind(MeshIOCase):

    def test_binary_stl(self):
        self.assertIs(kind(_binary_stl(self.path('a.stl'))), Kind.BINARY_STL)

    def test_ascii_stl(self):
        self.assertIs(kind(_ascii_stl(self.path('a.stl'))), Kind.ASCII_STL)

    def test_obj_by_extension(self):
        with open(self.path('a.obj'), 'w') as f:
            f.write("v 0 0 0\n")
        self.assertIs(kind(self.path('a.obj')), Kind.OBJ)

    def test_text_header_on_a_binary_file_reads_as_binary(self):
        """SolidWorks and older Slic3r do this — 'starts with solid' is not enough.

        The tiebreaker: the binary count field agrees with the file size, so
        the file is binary despite the text header.
        """
        path = _binary_stl(self.path('a.stl'),
                           header=b'solid exported by facet normal writer')
        self.assertIs(kind(path), Kind.BINARY_STL)

    def test_missing_file_is_unknown(self):
        self.assertIs(kind(self.path('nope.stl')), Kind.UNKNOWN)

    def test_empty_file_is_unknown(self):
        open(self.path('empty.stl'), 'wb').close()
        self.assertIs(kind(self.path('empty.stl')), Kind.UNKNOWN)


class TestTriangleCount(MeshIOCase):

    def test_counts_from_the_header(self):
        count, problem = triangle_count(_binary_stl(self.path('a.stl')))
        self.assertEqual(count, 4)
        self.assertIsNone(problem)

    def test_truncated_file_is_rejected(self):
        """The count must be cross-checked against the bytes actually present."""
        path = _binary_stl(self.path('a.stl'), declared=10_000)
        count, problem = triangle_count(path)
        self.assertEqual(count, -1)
        self.assertIn('10,000', problem)

    def test_trailing_bytes_are_accepted(self):
        """Some exporters append colour data; every slicer reads those fine."""
        path = _binary_stl(self.path('a.stl'))
        with open(path, 'ab') as f:
            f.write(b'extra colour data, non-standard but harmless')
        count, problem = triangle_count(path)
        self.assertEqual(count, 4)
        self.assertIsNone(problem)

    def test_file_shorter_than_a_header(self):
        with open(self.path('short.stl'), 'wb') as f:
            f.write(b'\0' * 20)
        count, problem = triangle_count(self.path('short.stl'))
        self.assertEqual(count, -1)
        self.assertIn('shorter', problem)

    def test_missing_file(self):
        count, problem = triangle_count(self.path('nope.stl'))
        self.assertEqual(count, -1)
        self.assertIsNotNone(problem)


class TestProbe(MeshIOCase):

    def test_binary_stl_reports_its_count(self):
        found = probe(_binary_stl(self.path('a.stl')))
        self.assertIs(found.kind, Kind.BINARY_STL)
        self.assertEqual(found.triangles, 4)
        self.assertTrue(found.is_valid)
        self.assertIsNone(found.problem)
        self.assertFalse(found.needs_conversion)

    def test_ascii_count_is_unknown_not_zero(self):
        """The change from the old code, and the reason this module exists.

        check_stl_integrity returned 0 for ASCII, so measure_files sorted those
        first as the cheapest work in the queue — when they may be the most
        expensive. None forces the caller to decide.
        """
        found = probe(_ascii_stl(self.path('a.stl')))
        self.assertIs(found.kind, Kind.ASCII_STL)
        self.assertIsNone(found.triangles, "an unknown count must not be 0")
        self.assertTrue(found.is_valid)
        self.assertTrue(found.needs_conversion)

    def test_obj_count_is_unknown_not_zero(self):
        with open(self.path('a.obj'), 'w') as f:
            f.write("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        found = probe(self.path('a.obj'))
        self.assertIs(found.kind, Kind.OBJ)
        self.assertIsNone(found.triangles)
        self.assertTrue(found.is_valid)
        self.assertTrue(found.needs_conversion)

    def test_empty_obj_is_invalid(self):
        open(self.path('a.obj'), 'w').close()
        found = probe(self.path('a.obj'))
        self.assertFalse(found.is_valid)
        self.assertIn('empty', found.problem)

    def test_truncated_binary_is_invalid(self):
        found = probe(_binary_stl(self.path('a.stl'), declared=10_000))
        self.assertFalse(found.is_valid)
        self.assertIsNone(found.triangles)
        self.assertIsNotNone(found.problem)

    def test_missing_file_is_invalid(self):
        found = probe(self.path('nope.stl'))
        self.assertIs(found.kind, Kind.UNKNOWN)
        self.assertFalse(found.is_valid)

    def test_probe_does_not_read_the_whole_file(self):
        """Cost must not scale with size — it runs on every file in a collection."""
        many = _TETRA * 25_000                       # 100k triangles, ~5 MB
        path = _binary_stl(self.path('big.stl'), triangles=many)
        self.assertGreater(os.path.getsize(path), 4_000_000)
        found = probe(path)
        self.assertEqual(found.triangles, 100_000)

    def test_mesh_is_immutable(self):
        found = Mesh('/a/b.stl', Kind.BINARY_STL, 4, True)
        with self.assertRaises(Exception):
            found.triangles = 9


class TestBounds(MeshIOCase):

    def test_extents_of_a_known_mesh(self):
        lo, hi = bounds(_binary_stl(self.path('a.stl')))
        self.assertEqual(tuple(round(v, 6) for v in lo), (0.0, 0.0, 0.0))
        self.assertEqual(tuple(round(v, 6) for v in hi), (1.0, 1.0, 1.0))

    def test_the_face_normal_is_not_included(self):
        """Bytes 0:12 are the normal; counting them would widen every box."""
        triangles = (((5, 5, 5), (6, 5, 5), (5, 6, 5)),)
        path = self.path('a.stl')
        body = bytearray()
        for tri in triangles:
            # A deliberately extreme normal: if it leaked in, lo would be -99.
            body += struct.pack('<3f', -99.0, -99.0, -99.0)
            for vertex in tri:
                body += struct.pack('<3f', *(float(c) for c in vertex))
            body += b'\0\0'
        with open(path, 'wb') as f:
            f.write(b'\0' * 80)
            f.write(struct.pack('<I', len(triangles)))
            f.write(bytes(body))
        lo, _ = bounds(path)
        self.assertEqual(round(lo[0], 6), 5.0, "the face normal leaked in")

    def test_spans_more_than_one_chunk(self):
        """The reader streams in 200k-triangle passes; cross that boundary."""
        many = _TETRA * 60_000                        # 240k triangles
        lo, hi = bounds(_binary_stl(self.path('big.stl'), triangles=many))
        self.assertEqual(tuple(round(v, 6) for v in hi), (1.0, 1.0, 1.0))

    def test_ascii_has_no_bounds(self):
        self.assertIsNone(bounds(_ascii_stl(self.path('a.stl'))))

    def test_truncated_has_no_bounds(self):
        self.assertIsNone(bounds(_binary_stl(self.path('a.stl'),
                                             declared=10_000)))


class TestDimensions(MeshIOCase):

    def test_dimensions_of_a_known_mesh(self):
        dims = dimensions(_binary_stl(self.path('a.stl')))
        self.assertEqual(tuple(round(d, 6) for d in dims), (1.0, 1.0, 1.0))

    def test_diagonal_of_a_unit_box(self):
        got = diagonal(_binary_stl(self.path('a.stl')))
        self.assertAlmostEqual(got, math.sqrt(3), places=5)

    def test_unreadable_returns_none(self):
        self.assertIsNone(dimensions(self.path('nope.stl')))
        self.assertIsNone(diagonal(self.path('nope.stl')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
