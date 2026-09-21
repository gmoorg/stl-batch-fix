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

import struct as _struct

import numpy as np

from libs.mesh_io import (
    BYTES_PER_TRIANGLE, HEADER_BYTES, Geometry, Kind, Mesh, bounds, diagonal,
    dimensions, kind, load, probe, read_ply, triangle_count, write, write_ply,
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
        found = probe(_binary_stl(self.path('a.stl')), self.path('out.stl'))
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
        found = probe(_ascii_stl(self.path('a.stl')), self.path('out.stl'))
        self.assertIs(found.kind, Kind.ASCII_STL)
        self.assertIsNone(found.triangles, "an unknown count must not be 0")
        self.assertTrue(found.is_valid)
        self.assertTrue(found.needs_conversion)

    def test_obj_count_is_unknown_not_zero(self):
        with open(self.path('a.obj'), 'w') as f:
            f.write("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        found = probe(self.path('a.obj'), self.path('out.stl'))
        self.assertIs(found.kind, Kind.OBJ)
        self.assertIsNone(found.triangles)
        self.assertTrue(found.is_valid)
        self.assertTrue(found.needs_conversion)

    def test_empty_obj_is_invalid(self):
        open(self.path('a.obj'), 'w').close()
        found = probe(self.path('a.obj'), self.path('out.stl'))
        self.assertFalse(found.is_valid)
        self.assertIn('empty', found.problem)

    def test_truncated_binary_is_invalid(self):
        found = probe(_binary_stl(self.path('a.stl'), declared=10_000),
                      self.path('out.stl'))
        self.assertFalse(found.is_valid)
        self.assertIsNone(found.triangles)
        self.assertIsNotNone(found.problem)

    def test_missing_file_is_invalid(self):
        found = probe(self.path('nope.stl'), self.path('out.stl'))
        self.assertIs(found.kind, Kind.UNKNOWN)
        self.assertFalse(found.is_valid)

    def test_probe_does_not_read_the_whole_file(self):
        """Cost must not scale with size — it runs on every file in a collection."""
        many = _TETRA * 25_000                       # 100k triangles, ~5 MB
        path = _binary_stl(self.path('big.stl'), triangles=many)
        self.assertGreater(os.path.getsize(path), 4_000_000)
        found = probe(path, self.path('out.stl'))
        self.assertEqual(found.triangles, 100_000)

    def test_mesh_is_immutable(self):
        found = Mesh('/a/b.stl', '/out/b.stl', Kind.BINARY_STL, 4, True)
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


class TestLoad(MeshIOCase):

    def test_a_probed_mesh_has_no_geometry(self):
        """The cheap object must stay cheap — this is the whole split."""
        found = probe(_binary_stl(self.path('a.stl')), self.path('out.stl'))
        self.assertIsNone(found.geometry)
        self.assertFalse(found.is_loaded)

    def test_load_returns_a_new_mesh_and_leaves_the_original_alone(self):
        probed = probe(_binary_stl(self.path('a.stl')), self.path('out.stl'))
        loaded = load(probed)
        self.assertIsNot(loaded, probed)
        self.assertTrue(loaded.is_loaded)
        self.assertFalse(probed.is_loaded,
                         "load mutated the mesh it was given")

    def test_vertices_are_welded_not_duplicated(self):
        """A tetrahedron has 12 corner slots on disk but only 4 vertices."""
        loaded = load(probe(_binary_stl(self.path('a.stl')), self.path('out.stl')))
        self.assertEqual(len(loaded.geometry.faces), 4)
        self.assertEqual(len(loaded.geometry.verts), 4,
                         "the vertices were not welded")

    def test_faces_index_the_right_vertices(self):
        """Welding must preserve the geometry, not just the counts."""
        loaded = load(probe(_binary_stl(self.path('a.stl')), self.path('out.stl')))
        g = loaded.geometry
        rebuilt = {tuple(sorted(tuple(round(c, 6) for c in g.verts[i])
                                for i in face)) for face in g.faces}
        expected = {tuple(sorted(tuple(float(c) for c in v) for v in tri))
                    for tri in _TETRA}
        self.assertEqual(rebuilt, expected)

    def test_negative_zero_welds_with_positive_zero(self):
        """-0.0 and 0.0 are equal as floats but differ in bits.

        Without the fold a shared vertex splits in two and leaves a crack that
        quadric edge collapse cannot close.
        """
        triangles = (
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((-0.0, -0.0, -0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )
        loaded = load(probe(_binary_stl(self.path('z.stl'),
                                        triangles=triangles),
                            self.path('out.stl')))
        self.assertEqual(len(loaded.geometry.verts), 4,
                         "-0.0 did not weld with 0.0")

    def test_triangle_count_comes_from_the_geometry(self):
        many = _TETRA * 1000
        loaded = load(probe(_binary_stl(self.path('m.stl'), triangles=many),
                            self.path('out.stl')))
        self.assertEqual(loaded.triangles, 4000)

    def test_a_truncated_file_loads_as_invalid_not_an_exception(self):
        """Bad data is a result; only a bad request raises."""
        path = _binary_stl(self.path('a.stl'), declared=10_000)
        result = load(Mesh(path, self.path('out.stl'), Kind.BINARY_STL,
                           10_000, True))
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.geometry)
        self.assertIsNotNone(result.problem)

    def test_a_zero_triangle_file_is_invalid_not_a_crash(self):
        """R03: an 84-byte STL is well-formed but holds no model.

        `probe` used to call it valid, and `load` then indexed an empty array.
        A file with nothing in it is bad data, so it is a result, not a raise —
        and it must be caught at `probe`, before a worker commits to loading it.
        """
        path = _binary_stl(self.path('empty.stl'), triangles=[])
        self.assertEqual(os.path.getsize(path), 84)

        probed = probe(path, self.path('out.stl'))
        self.assertFalse(probed.is_valid)
        self.assertIsNotNone(probed.problem)

        # Even asked directly, with the header's own count, load must not crash.
        result = load(Mesh(path, self.path('out.stl'), Kind.BINARY_STL,
                           0, True))
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.geometry)
        self.assertIsNotNone(result.problem)

    def test_loading_a_non_binary_mesh_raises(self):
        """Asking is a programming error — it should have been converted."""
        ascii_mesh = probe(_ascii_stl(self.path('a.stl')), self.path('out.stl'))
        with self.assertRaises(ValueError):
            load(ascii_mesh)


class TestWrite(MeshIOCase):

    def _round_trip(self, triangles=_TETRA):
        src = _binary_stl(self.path('in.stl'), triangles=triangles)
        dst = self.path('out.stl')
        loaded = load(probe(src, dst))
        write(loaded)
        return loaded, probe(dst, dst)

    def test_round_trip_preserves_the_triangle_count(self):
        loaded, written = self._round_trip()
        self.assertEqual(written.triangles, loaded.triangles)
        self.assertTrue(written.is_valid)

    def test_round_trip_preserves_the_geometry(self):
        _, written = self._round_trip()
        lo, hi = bounds(written.path)
        self.assertEqual(tuple(round(v, 6) for v in lo), (0.0, 0.0, 0.0))
        self.assertEqual(tuple(round(v, 6) for v in hi), (1.0, 1.0, 1.0))

    def test_a_written_mesh_reloads_identically(self):
        """The strongest round-trip statement: weld, write, weld again."""
        loaded, written = self._round_trip()
        again = load(written)
        self.assertEqual(len(again.geometry.verts),
                         len(loaded.geometry.verts))
        self.assertTrue(np.array_equal(np.sort(again.geometry.verts, axis=0),
                                       np.sort(loaded.geometry.verts, axis=0)))

    def test_normals_are_real_not_zeroed(self):
        """Viewers disagree about a zero normal — some use the winding, some
        guess — so two outputs could not be compared honestly."""
        _, written = self._round_trip()
        with open(written.path, 'rb') as f:
            f.seek(HEADER_BYTES)
            record = f.read(BYTES_PER_TRIANGLE)
        normal = _struct.unpack_from('<3f', record, 0)
        self.assertNotEqual(normal, (0.0, 0.0, 0.0), "the normal was zeroed")
        self.assertAlmostEqual(math.sqrt(sum(c * c for c in normal)), 1.0,
                               places=5, msg="the normal is not unit length")

    def test_a_degenerate_face_gets_a_zero_normal_not_a_nan(self):
        """A zero-area face has no normal; NaNs in the file break slicers."""
        degenerate = (((0, 0, 0), (1, 0, 0), (1, 0, 0)),)
        src = _binary_stl(self.path('d.stl'), triangles=degenerate)
        write(load(probe(src, self.path('out.stl'))))
        with open(self.path('out.stl'), 'rb') as f:
            f.seek(HEADER_BYTES)
            normal = _struct.unpack_from('<3f', f.read(BYTES_PER_TRIANGLE), 0)
        self.assertEqual(normal, (0.0, 0.0, 0.0))
        self.assertFalse(any(math.isnan(c) for c in normal))

    def test_write_creates_the_parent_directory(self):
        dst = self.path(os.path.join('deep', 'deeper', 'out.stl'))
        loaded = load(probe(_binary_stl(self.path('in.stl')), dst))
        write(loaded)
        self.assertTrue(os.path.exists(dst))

    def test_writing_an_unloaded_mesh_raises(self):
        """Rather than silently writing a zero-triangle file."""
        probed = probe(_binary_stl(self.path('in.stl')), self.path('out.stl'))
        with self.assertRaises(ValueError):
            write(probed)


class TestWithGeometry(MeshIOCase):
    """How a decimator or repairer reports a changed mesh."""

    def test_with_geometry_recounts_the_triangles(self):
        loaded = load(probe(_binary_stl(self.path('a.stl')), self.path('out.stl')))
        fewer = Geometry(loaded.geometry.verts, loaded.geometry.faces[:2])
        changed = loaded.with_geometry(fewer)
        self.assertEqual(changed.triangles, 2,
                         "the count was carried over, not re-derived")
        self.assertEqual(loaded.triangles, 4, "the original was mutated")

    def test_the_path_is_carried_over(self):
        """A changed mesh still knows which file it came from."""
        loaded = load(probe(_binary_stl(self.path('a.stl')), self.path('out.stl')))
        changed = loaded.with_geometry(loaded.geometry)
        self.assertEqual(changed.path, loaded.path)


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


class TestPly(MeshIOCase):
    """The Blender scratch boundary.

    PLY exists here for one reason: STL stores no vertex table, so a mesh
    written as STL arrives as loose triangles and has to be welded back by
    proximity — which was measured deleting sub-millimetre detail. These tests
    assert the property that makes PLY worth the second format: **the vertex
    table survives, so nothing is reconstructed.**
    """

    def loaded(self):
        src = _binary_stl(self.path('in.stl'))
        return load(probe(src, self.path('out.stl')))

    def test_the_round_trip_is_bit_identical(self):
        """Not merely equivalent — the same bytes back.

        `load` has to weld an STL because each triangle carries its own
        corners; a PLY needs no such step, so there is no rounding, no merge
        and nothing to go wrong.
        """
        mesh = self.loaded()
        ply = self.path('mesh.ply')
        write_ply(mesh, ply)
        back = read_ply(ply, mesh)
        self.assertTrue(np.array_equal(mesh.geometry.verts,
                                       back.geometry.verts))
        self.assertTrue(np.array_equal(mesh.geometry.faces,
                                       back.geometry.faces))

    def test_no_vertex_is_duplicated(self):
        """The whole point. An STL of this mesh would carry 3 vertices per
        face; the PLY carries the welded table."""
        mesh = self.loaded()
        ply = self.path('mesh.ply')
        write_ply(mesh, ply)
        back = read_ply(ply, mesh)
        self.assertLess(len(back.geometry.verts),
                        3 * len(back.geometry.faces))
        self.assertEqual(len(back.geometry.verts), len(mesh.geometry.verts))

    def test_the_result_keeps_the_meshs_identity(self):
        """The repaired geometry comes back attached to the part that was
        sent, not to the temp file it travelled in."""
        mesh = self.loaded()
        ply = self.path('mesh.ply')
        write_ply(mesh, ply)
        back = read_ply(ply, mesh)
        self.assertEqual(back.path, mesh.path)
        self.assertEqual(back.destination, mesh.destination)

    def test_the_triangle_count_is_rederived(self):
        mesh = self.loaded()
        ply = self.path('mesh.ply')
        write_ply(mesh, ply)
        back = read_ply(ply, mesh)
        self.assertEqual(back.triangles, len(back.geometry.faces))

    def test_writing_an_unloaded_mesh_raises(self):
        with self.assertRaises(ValueError):
            write_ply(probe(_binary_stl(self.path('in.stl')),
                            self.path('out.stl')), self.path('x.ply'))

    def test_a_missing_parent_directory_is_created(self):
        mesh = self.loaded()
        ply = self.path('nested/deeper/mesh.ply')
        write_ply(mesh, ply)
        self.assertTrue(os.path.exists(ply))

    def test_an_stl_is_rejected(self):
        """A silent fallback would turn an unreadable scratch file into a
        plausible-looking mesh, in the middle of a repair."""
        mesh = self.loaded()
        with self.assertRaises(ValueError):
            read_ply(_binary_stl(self.path('other.stl')), mesh)

    def test_an_ascii_ply_is_rejected(self):
        """This reader handles one dialect deliberately — what `write_ply`
        emits and Blender's exporter produces."""
        mesh = self.loaded()
        path = self.path('ascii.ply')
        with open(path, 'wb') as f:
            f.write(b'ply\nformat ascii 1.0\nelement vertex 1\n'
                    b'property float x\nend_header\n0.0\n')
        with self.assertRaises(ValueError):
            read_ply(path, mesh)

    def test_a_truncated_header_is_rejected(self):
        mesh = self.loaded()
        path = self.path('trunc.ply')
        with open(path, 'wb') as f:
            f.write(b'ply\nformat binary_little_endian 1.0\nelement vertex 1\n')
        with self.assertRaises(ValueError):
            read_ply(path, mesh)

    def test_extra_float_vertex_properties_are_dropped(self):
        """Blender may append normals depending on export flags. Extra float
        columns shift the stride and must be skipped, not misread as
        coordinates."""
        mesh = self.loaded()
        verts = mesh.geometry.verts
        faces = mesh.geometry.faces.astype(np.uint32)
        path = self.path('withnormals.ply')
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            f"element vertex {len(verts)}\n"
            "property float x\nproperty float y\nproperty float z\n"
            "property float nx\nproperty float ny\nproperty float nz\n"
            f"element face {len(faces)}\n"
            "property list uchar uint vertex_indices\nend_header\n"
        ).encode('ascii')
        padded = np.hstack([verts, np.zeros_like(verts)]).astype(np.float32)
        records = np.zeros(len(faces), dtype=[('n', 'u1'), ('v', '<u4', 3)])
        records['n'] = 3
        records['v'] = faces
        with open(path, 'wb') as f:
            f.write(header)
            f.write(padded.tobytes())
            f.write(records.tobytes())
        back = read_ply(path, mesh)
        self.assertTrue(np.array_equal(back.geometry.verts, verts))

    def test_a_non_triangular_face_is_rejected(self):
        """Blender triangulates before export, so a quad means the file did
        not come from where it claims to have."""
        mesh = self.loaded()
        verts = mesh.geometry.verts
        path = self.path('quad.ply')
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            f"element vertex {len(verts)}\n"
            "property float x\nproperty float y\nproperty float z\n"
            "element face 1\n"
            "property list uchar uint vertex_indices\nend_header\n"
        ).encode('ascii')
        record = np.zeros(1, dtype=[('n', 'u1'), ('v', '<u4', 3)])
        record['n'] = 4                       # claims a quad
        with open(path, 'wb') as f:
            f.write(header)
            f.write(verts.astype(np.float32).tobytes())
            f.write(record.tobytes())
        with self.assertRaises(ValueError):
            read_ply(path, mesh)


if __name__ == '__main__':
    unittest.main(verbosity=2)
