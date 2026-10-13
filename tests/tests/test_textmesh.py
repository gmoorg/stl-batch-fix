"""Tests for libs.textmesh — ASCII STL and OBJ to binary STL, no Blender.

Every source is written by the test that needs it; outcomes are read back
from the binary STL written (coordinates, triangle count, winding), not from
the converter's internals. Quad splits are checked with independent planar
geometry: shoelace area and point-in-polygon, not the converter's own test.
"""

import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from libs import mesh_io, textmesh
from libs.mesh_io import Kind, LoadDrops
from libs.textmesh import ConversionError, Malformed


def read_stl(path):
    """(n, 3, 3) float32 corners of a binary STL, checking its header count."""
    with open(path, 'rb') as f:
        data = f.read()
    count = struct.unpack_from('<I', data, 80)[0]
    assert len(data) == 84 + 50 * count, (len(data), count)
    records = np.frombuffer(data, dtype=mesh_io._RECORD, count=count, offset=84)
    return records['corners'].reshape(-1, 3, 3).copy()


def facet(*vertices, normal='0 0 0'):
    lines = [f'facet normal {normal}', '  outer loop']
    lines += [f'    vertex {v}' for v in vertices]
    lines += ['  endloop', 'endfacet']
    return '\n'.join(lines) + '\n'


TETRA = (('0 0 0', '0 1 0', '1 0 0'), ('0 0 0', '1 0 0', '0 0 1'),
         ('0 0 0', '0 0 1', '0 1 0'), ('1 0 0', '0 1 0', '0 0 1'))


def ascii_stl(*facets, name='t', end=True):
    text = f'solid {name}\n' + ''.join(facets)
    return text + (f'endsolid {name}\n' if end else '')


def shoelace(points):
    x, y = np.asarray(points, dtype=float).T
    return 0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def inside(point, polygon):
    """Strictly inside a simple polygon (ray casting); points used here are
    never on the boundary."""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


class Case(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='textmesh-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.export = os.path.join(self.dir, 'out', 'model.stl')

    def source(self, text, name='model.stl'):
        path = os.path.join(self.dir, name)
        with open(path, 'w', newline='') as f:
            f.write(text)
        return path

    def convert(self, text, name='model.stl', **kwargs):
        stats = textmesh.convert(self.source(text, name), self.export, **kwargs)
        return stats, read_stl(self.export)

    def assert_nothing_published(self):
        folder = os.path.dirname(self.export)
        left = os.listdir(folder) if os.path.isdir(folder) else []
        self.assertEqual(left, [], 'a failed conversion left a file behind')

    def assert_malformed(self, text, fragment, name='model.stl'):
        with self.assertRaises(Malformed) as caught:
            textmesh.convert(self.source(text, name), self.export)
        self.assertIn(fragment, str(caught.exception))
        self.assert_nothing_published()


class TestAsciiStl(Case):

    def test_a_tetrahedron_keeps_its_coordinates_and_order(self):
        stats, tri = self.convert(ascii_stl(*(facet(*f) for f in TETRA)))
        self.assertEqual(stats.triangles, 4)
        self.assertEqual(stats.drops, LoadDrops(0, 0))
        expected = np.array([[[float(c) for c in v.split()] for v in f] for f in TETRA],
                            dtype=np.float32)
        np.testing.assert_array_equal(tri, expected)
        self.assertIs(mesh_io.kind(self.export), Kind.BINARY_STL)

    def test_coordinates_are_rounded_to_float32_once_without_axis_change(self):
        _, tri = self.convert(ascii_stl(facet('0.1 -2.5e-3 3', '1e3 0.3 -7', '0.7 8 9.123456789')))
        expected = np.array([[0.1, -2.5e-3, 3], [1e3, 0.3, -7], [0.7, 8, 9.123456789]],
                            dtype=np.float32)
        np.testing.assert_array_equal(tri[0], expected)

    def test_several_solids_are_one_mesh(self):
        text = (ascii_stl(facet(*TETRA[0]), facet(*TETRA[1]), name='a')
                + ascii_stl(facet(*TETRA[2]), facet(*TETRA[3]), name='b'))
        stats, _ = self.convert(text)
        self.assertEqual(stats.triangles, 4)

    def test_case_whitespace_and_crlf_are_tolerated(self):
        text = ('solid odd name with spaces\r\n'
                + facet(*TETRA[0]).replace('\n', '\r\n').replace('facet normal', 'facet \t normal')
                + 'FACET\tNORMAL 0 0 1\r\n\t OUTER   Loop\r\n'
                  'VERTEX\t0 0 0\r\n  Vertex 1  0 0\r\n vertex 0\t0 1\r\n'
                  '\r\n EndLoop\r\nENDFACET\r\n'
                + 'ENDSOLID odd\r\n')
        stats, tri = self.convert(text)
        self.assertEqual(stats.triangles, 2)
        np.testing.assert_array_equal(tri[1], [[0, 0, 0], [1, 0, 0], [0, 0, 1]])

    def test_a_missing_final_endsolid_is_accepted(self):
        stats, _ = self.convert(ascii_stl(*(facet(*f) for f in TETRA), end=False))
        self.assertEqual(stats.triangles, 4)

    def test_a_file_cut_inside_a_facet_is_malformed(self):
        whole = facet(*TETRA[0])
        for cut in ('    vertex 1 0 0\n', '  endloop\n'):
            with self.subTest(cut=cut):
                text = ascii_stl(whole, end=False) + whole[:whole.index(cut) + len(cut)]
                self.assert_malformed(text, 'truncated')

    def test_a_facet_without_three_vertices_is_malformed(self):
        for vertices in (TETRA[0][:2], TETRA[0] + ('5 5 5',)):
            with self.subTest(n=len(vertices)):
                self.assert_malformed(ascii_stl(facet(*TETRA[1]), facet(*vertices)),
                                      f'a facet with {len(vertices)} vertices')

    def test_a_bad_number_or_keyword_is_malformed(self):
        self.assert_malformed(ascii_stl(facet('0 0 0', '1 x 0', '0 1 0')), 'not a number')
        self.assert_malformed(ascii_stl(facet('0 0 0', '1 0', '0 1 0')), 'three coordinates')
        self.assert_malformed(ascii_stl(facet(*TETRA[0])) + 'vertex 0 0 0\n', 'unexpected')
        self.assert_malformed(ascii_stl(facet(*TETRA[0]).replace('outer loop', 'loop')),
                              'unexpected')

    def test_bad_triangles_are_dropped_and_counted(self):
        stats, tri = self.convert(ascii_stl(
            facet(*TETRA[0]),
            facet('nan 0 0', '1 0 0', '0 1 0'),
            facet('inf 0 0', '1 0 0', '0 1 0'),
            facet('1e39 0 0', '1 0 0', '0 1 0'),      # beyond float32: inf
            facet('0 0 0', '-0 0 0', '0 1 0'),        # coincident after the fold
            facet('0 0 0', '1 0 0', '2 0 0'),         # collinear, distinct: kept
        ))
        self.assertEqual(stats.drops, LoadDrops(nonfinite=3, degenerate=1))
        self.assertEqual(stats.triangles, 2)
        np.testing.assert_array_equal(tri[1], [[0, 0, 0], [1, 0, 0], [2, 0, 0]])

    def test_nothing_kept_publishes_nothing(self):
        with self.assertRaises(ConversionError):
            textmesh.convert(self.source(ascii_stl(facet('0 0 0', '0 0 0', '1 1 1'))),
                             self.export)
        self.assert_nothing_published()

    def test_chunk_boundaries_do_not_change_the_output(self):
        text = ascii_stl(*(facet(*f) for f in TETRA * 3),
                         facet('nan 0 0', '1 0 0', '0 1 0'))
        self.convert(text)
        whole = Path(self.export).read_bytes()
        for chunk in (1, 2, 5):
            with self.subTest(chunk=chunk):
                stats, _ = self.convert(text, chunk_triangles=chunk)
                self.assertEqual(Path(self.export).read_bytes(), whole)
                self.assertEqual(stats.drops.nonfinite, 1)


class TestObj(Case):

    TRIANGLE = 'v 0 0 0\nv 1 0 0\nv 0 1 0\n'

    def obj(self, text, **kwargs):
        return self.convert(text, name='model.obj', **kwargs)

    def assert_obj_malformed(self, text, fragment):
        self.assert_malformed(text, fragment, name='model.obj')

    def test_every_face_form_gives_the_same_triangle(self):
        expected = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]], dtype=np.float32)
        extra = 'vt 0 0\nvt 1 0\nvt 0 1\nvn 0 0 1\n'
        for face in ('f 1 2 3', 'f 1/1 2/2 3/3', 'f 1//1 2//1 3//1',
                     'f 1/1/1 2/2/1 3/3/1', 'f -3 -2 -1', 'f -3/-3 -2/-2 -1/-1/-1'):
            with self.subTest(face=face):
                _, tri = self.obj(self.TRIANGLE + extra + face + '\n')
                np.testing.assert_array_equal(tri, expected)

    def test_negative_indices_are_relative_to_the_face(self):
        text = (self.TRIANGLE + 'f -3 -2 -1\n'
                + 'v 0 0 5\nv 1 0 5\nv 0 1 5\nf -3 -2 -1\n')
        _, tri = self.obj(text)
        self.assertEqual(tri[0, :, 2].tolist(), [0, 0, 0])
        self.assertEqual(tri[1, :, 2].tolist(), [5, 5, 5])

    def test_a_face_may_come_before_its_vertices(self):
        _, tri = self.obj('f 1 2 3\n' + self.TRIANGLE)
        np.testing.assert_array_equal(tri[0], [[0, 0, 0], [1, 0, 0], [0, 1, 0]])

    def test_groups_comments_continuations_and_extras(self):
        text = ('# a comment\nmtllib model.mtl\no first\ng a b\nusemtl red\ns 1\n'
                'v 0 0 0 1.0\nv 1 0 0 0.5 0.5 0.5\nv 0 1 0   # trailing comment\n'
                'f 1 2 \\\n  3\n'
                'o second\nv 0 0 2\nv 1 0 2\nv 0 1 2\nl 1 2\np 1\nf 4 5 6 # done\n')
        stats, tri = self.obj(text)
        self.assertEqual(stats.triangles, 2)
        np.testing.assert_array_equal(tri[0], [[0, 0, 0], [1, 0, 0], [0, 1, 0]])
        np.testing.assert_array_equal(tri[1], [[0, 0, 2], [1, 0, 2], [0, 1, 2]])

    def test_malformed_faces_and_vertices(self):
        cases = [
            ('f 1 2\n', 'a face with 2 vertices'),
            ('v 1 1 0\nv -1 0.5 0\nf 1 2 3 4 5\n', 'a face with 5 vertices'),
            ('f 0 1 2\n', 'outside the vertex table'),
            ('f 1 2 4\n', 'outside the vertex table'),
            ('f -4 -2 -1\n', 'outside the vertex table'),
            (f'f 1 2 {10 ** 30}\n', 'outside the vertex table'),
            ('f 1 2 x\n', 'not a vertex index'),
            ('f 1 2 /3\n', 'not a vertex index'),
            ('v 1 2\n', 'three coordinates'),
            ('v 1 2 z\n', 'not a number'),
        ]
        for tail, fragment in cases:
            with self.subTest(tail=tail):
                self.assert_obj_malformed(self.TRIANGLE + tail, fragment)

    def test_a_negative_index_cannot_reach_a_later_vertex(self):
        self.assert_obj_malformed('v 0 0 0\nv 1 0 0\nf -1 -2 -3\nv 0 1 0\n',
                                  'outside the vertex table')

    def test_bad_triangles_are_dropped_and_counted(self):
        text = self.TRIANGLE + 'v nan 0 0\nf 1 2 3\nf 4 2 3\nf 1 1 2\n'
        stats, tri = self.obj(text)
        self.assertEqual(stats.drops, LoadDrops(nonfinite=1, degenerate=1))
        self.assertEqual(len(tri), 1)

    def test_no_faces_publishes_nothing(self):
        with self.assertRaises(ConversionError) as caught:
            textmesh.convert(self.source(self.TRIANGLE, 'model.obj'), self.export)
        self.assertIn('no triangles', str(caught.exception))
        self.assert_nothing_published()

    def test_face_order_is_kept_across_quads_and_chunks(self):
        text = ('v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 0 1 1\n'
                'f 5 6 7\nf 1 2 3 4\nf 7 6 5\n')
        _, whole = self.obj(text)
        self.assertEqual(whole[:, :, 2].tolist(),
                         [[1, 1, 1], [0, 0, 0], [0, 0, 0], [1, 1, 1]])
        for chunk in (1, 2):
            with self.subTest(chunk=chunk):
                _, tri = self.obj(text, chunk_triangles=chunk)
                np.testing.assert_array_equal(tri, whole)


class TestQuadSplit(Case):
    """Planar quads in z = 0, checked against the polygon itself."""

    #: A dart: corner 1 (2, 1) is reflex, so only the 1-3 diagonal is inside.
    DART = [(0, 0), (2, 1), (4, 0), (2, 3)]

    def split(self, polygon):
        text = ''.join(f'v {x} {y} 0\n' for x, y in polygon) + 'f 1 2 3 4\n'
        stats, tri = self.convert(text, name='quad.obj')
        return stats, tri

    def assert_split_inside(self, polygon):
        stats, tri = self.split(polygon)
        self.assertEqual((stats.triangles, stats.quads), (2, 1))
        self.assertEqual(stats.quads_without_inside_diagonal, 0)
        area = shoelace(polygon)
        xy = tri[:, :, :2].astype(float)
        areas = [shoelace(t) for t in xy]
        # Same winding as the polygon, together covering exactly its area.
        self.assertTrue(all(np.sign(a) == np.sign(area) for a in areas), areas)
        self.assertAlmostEqual(sum(areas), area, places=5)
        for t in xy:
            self.assertTrue(inside(t.mean(axis=0), polygon), t)
        # The shared edge is the diagonal; its midpoint must be inside.
        shared = {tuple(p) for p in xy[0].tolist()} & {tuple(p) for p in xy[1].tolist()}
        self.assertEqual(len(shared), 2)
        self.assertTrue(inside(np.mean(list(shared), axis=0), polygon), shared)

    def test_a_convex_quad_splits_on_the_first_diagonal(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        self.assert_split_inside(square)
        _, tri = self.split(square)
        np.testing.assert_array_equal(tri[:, :, :2], [[(0, 0), (1, 0), (1, 1)],
                                                       [(0, 0), (1, 1), (0, 1)]])

    def test_the_reflex_corner_in_every_position_and_both_windings(self):
        for shift in range(4):
            polygon = self.DART[shift:] + self.DART[:shift]
            for winding in (polygon, polygon[::-1]):
                with self.subTest(shift=shift, reversed=winding is not polygon):
                    self.assert_split_inside(winding)

    def test_a_bowtie_is_split_anyway_and_counted(self):
        """The quad probed in Blender (2026-10-06), which split it too."""
        stats, tri = self.split([(0, 0), (1, 1), (1, 0), (0, 1)])
        self.assertEqual((stats.triangles, stats.quads), (2, 1))
        self.assertEqual(stats.quads_without_inside_diagonal, 1)
        self.assertIn('1 without an inside diagonal', stats.summary())

    def test_a_collinear_quad_keeps_its_distinct_triangles_uncounted(self):
        stats, _ = self.split([(0, 0), (1, 0), (2, 0), (3, 0)])
        self.assertEqual(stats.triangles, 2)
        self.assertEqual(stats.drops, LoadDrops(0, 0))
        self.assertEqual(stats.quads_without_inside_diagonal, 0)

    def test_a_repeated_corner_leaves_one_triangle(self):
        text = 'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3 3\n'
        stats, tri = self.convert(text, name='quad.obj')
        self.assertEqual((stats.triangles, stats.drops), (1, LoadDrops(0, 1)))
        np.testing.assert_array_equal(tri[0], [[0, 0, 0], [1, 0, 0], [0, 1, 0]])


class TestPublication(Case):

    def test_an_interrupt_mid_write_publishes_nothing(self):
        text = ascii_stl(*(facet(*f) for f in TETRA))
        real_add = textmesh._Writer.add
        calls = []

        def add(writer, corners):
            calls.append(len(corners))
            if len(calls) == 2:
                raise KeyboardInterrupt
            real_add(writer, corners)

        with mock.patch.object(textmesh._Writer, 'add', add):
            with self.assertRaises(KeyboardInterrupt):
                textmesh.convert(self.source(text), self.export, chunk_triangles=1)
        self.assert_nothing_published()

    def test_only_text_formats_are_accepted(self):
        mesh_io.write(mesh_io.Mesh('/in', self.export, Kind.BINARY_STL, 1, True, None,
                                   mesh_io.Geometry(np.eye(3), np.array([[0, 1, 2]]))))
        with self.assertRaises(ValueError):
            textmesh.convert(self.export, os.path.join(self.dir, 'again.stl'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
