"""Tests for libs.converter — the preparation walk.

No Blender: `convert` is injected, so a fake one records what it was asked to
do and writes whatever the test needs. Meshes are generated binary STLs, as in
test_mesh_io.
"""

import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from libs import converter
from libs.converter import Summary, prepare
from libs.indicators import EXPORT_DIRNAME, export_path
from libs.mesh_io import Kind

_TETRA = (
    ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
    ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
)

COPY_EXTS = frozenset({'.png', '.jpg', '.txt'})


def _binary_stl(path, triangles=_TETRA):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = bytearray()
    for tri in triangles:
        body += struct.pack('<3f', 0.0, 0.0, 0.0)
        for vertex in tri:
            body += struct.pack('<3f', *(float(c) for c in vertex))
        body += b'\0\0'
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', len(triangles)))
        f.write(bytes(body))
    return path


def _ascii_stl(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write("solid t\n")
        for tri in _TETRA:
            f.write("facet normal 0 0 0\n  outer loop\n")
            for v in tri:
                f.write("    vertex %g %g %g\n" % v)
            f.write("  endloop\nendfacet\n")
        f.write("endsolid t\n")
    return path


def _touch(path, text='x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class ConverterCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='converter-')
        self.src = os.path.join(self.root, 'Fixing')
        self.out = os.path.join(self.root, 'Fixed')
        os.makedirs(self.src)
        os.makedirs(self.out)
        self.seen = []
        self.lock = threading.Lock()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def emit(self, mesh):
        with self.lock:
            self.seen.append(mesh)

    def s(self, *parts):
        return os.path.join(self.src, *parts)

    def o(self, *parts):
        return os.path.join(self.out, *parts)

    def run_prepare(self, convert=None, workers=4):
        return prepare(self.src, self.out, self.emit,
                       copy_extensions=COPY_EXTS, convert=convert,
                       workers=workers)

    def by_path(self):
        return {os.path.basename(m.path): m for m in self.seen}


class TestBinaryMeshes(ConverterCase):

    def test_a_binary_stl_is_emitted_measured(self):
        _binary_stl(self.s('Leia', 'head.stl'))
        summary = self.run_prepare()
        self.assertEqual(len(self.seen), 1)
        mesh = self.seen[0]
        self.assertIs(mesh.kind, Kind.BINARY_STL)
        self.assertEqual(mesh.triangles, 4)
        self.assertTrue(mesh.is_valid)
        self.assertEqual(summary.emitted, 1)

    def test_every_mesh_is_emitted_once(self):
        for n in range(12):
            _binary_stl(self.s('Leia', f'part{n}.stl'))
        self.run_prepare()
        names = [os.path.basename(m.path) for m in self.seen]
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 12)

    def test_an_invalid_mesh_is_emitted_not_dropped(self):
        path = self.s('bad.stl')
        with open(path, 'wb') as f:
            f.write(b'\0' * 80 + struct.pack('<I', 9999))
        self.run_prepare()
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0].is_valid)
        self.assertIsNotNone(self.seen[0].problem)


class TestCompanions(ConverterCase):

    def test_an_image_is_copied_not_emitted(self):
        _touch(self.s('Leia', 'render.png'))
        summary = self.run_prepare()
        self.assertEqual(self.seen, [], "a companion reached the consumer")
        self.assertTrue(os.path.exists(self.o('Leia', 'render.png')))
        self.assertEqual(summary.copied, 1)

    def test_relative_path_is_preserved(self):
        _touch(self.s('a', 'b', 'c', 'notes.txt'))
        self.run_prepare()
        self.assertTrue(os.path.exists(self.o('a', 'b', 'c', 'notes.txt')))

    def test_an_existing_copy_is_not_rewritten(self):
        _touch(self.s('Leia', 'render.png'), 'original')
        _touch(self.o('Leia', 'render.png'), 'already here')
        summary = self.run_prepare()
        self.assertEqual(summary.copied, 0)
        self.assertEqual(summary.already_copied, 1)
        with open(self.o('Leia', 'render.png')) as f:
            self.assertEqual(f.read(), 'already here')

    def test_an_unlisted_extension_is_not_treated_as_a_companion(self):
        """Only what the caller nominated gets copied."""
        _touch(self.s('data.csv'))
        summary = self.run_prepare()
        self.assertEqual(summary.copied, 0)


class TestSkipping(ConverterCase):

    def test_an_already_fixed_mesh_is_skipped(self):
        _binary_stl(self.s('Leia', 'head.stl'))
        _binary_stl(self.o('Leia', 'head.stl'))
        summary = self.run_prepare()
        self.assertEqual(self.seen, [])
        self.assertEqual(summary.skipped, 1)

    def test_a_marker_skips_the_mesh(self):
        _binary_stl(self.s('Leia', 'head.stl'))
        _touch(self.o('Leia', 'head.failed.stl'))
        summary = self.run_prepare()
        self.assertEqual(self.seen, [])
        self.assertEqual(summary.skipped, 1)

    def test_the_export_folder_is_not_walked(self):
        """Its contents are outputs of a previous run, not inputs."""
        _binary_stl(os.path.join(self.src, EXPORT_DIRNAME, 'stale.stl'))
        summary = self.run_prepare()
        self.assertEqual(self.seen, [])
        self.assertEqual(summary.scanned, 0)

    def test_appledouble_sidecars_are_ignored(self):
        """macOS writes ._name next to every zipped file: no geometry."""
        _touch(self.s('._head.stl'))
        summary = self.run_prepare()
        self.assertEqual(self.seen, [])
        self.assertEqual(summary.scanned, 0)


class TestConversion(ConverterCase):

    def _fake_convert(self, succeed=True, record=None):
        def convert(source, destination):
            if record is not None:
                with self.lock:
                    record.append((source, destination))
            if not succeed:
                return False, destination
            _binary_stl(destination)
            return True, destination
        return convert

    def test_ascii_is_converted_then_measured(self):
        _ascii_stl(self.s('Leia', 'head.stl'))
        asked = []
        summary = self.run_prepare(convert=self._fake_convert(record=asked))
        self.assertEqual(len(asked), 1, "the converter was not called")
        self.assertEqual(len(self.seen), 1)
        mesh = self.seen[0]
        self.assertIs(mesh.kind, Kind.BINARY_STL,
                      "the CONVERSION should be measured, not the source")
        self.assertEqual(mesh.triangles, 4)
        self.assertEqual(summary.converted, 1)

    def test_conversion_writes_into_the_export_folder(self):
        source = _ascii_stl(self.s('Leia', 'head.stl'))
        asked = []
        self.run_prepare(convert=self._fake_convert(record=asked))
        self.assertEqual(asked[0][1], export_path(source, self.src))
        self.assertIn(EXPORT_DIRNAME, asked[0][1])

    def test_an_existing_export_is_reused_without_converting(self):
        source = _ascii_stl(self.s('Leia', 'head.stl'))
        _binary_stl(export_path(source, self.src))
        asked = []
        summary = self.run_prepare(convert=self._fake_convert(record=asked))
        self.assertEqual(asked, [], "it converted a file that was already done")
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0].triangles, 4)

    def test_a_failed_conversion_is_emitted_not_dropped(self):
        """Otherwise the converter is the one place a file can vanish."""
        _ascii_stl(self.s('Leia', 'head.stl'))
        summary = self.run_prepare(convert=self._fake_convert(succeed=False))
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0].is_valid)
        self.assertEqual(summary.conversion_failed, 1)

    def test_a_failed_companion_copy_does_not_abort_the_walk(self):
        """A06: one unreadable companion must not cost every later file.

        `shutil.copy2` was called inline, so a permission error or a vanished
        file raised straight out of `prepare` — and the walk is a single pass,
        so every source after it was never even scanned.  The orchestration
        contract asks for one result per source; aborting gives none for most
        of them.
        """
        os.makedirs(self.s('Leia'), exist_ok=True)
        with open(self.s('Leia', 'notes.txt'), 'w') as f:
            f.write('companion')
        _binary_stl(self.s('Leia', 'head.stl'))

        def die(src, dst, *args, **kwargs):
            raise PermissionError("cannot read the companion")

        # The walk order decides what this proves.  `os.walk` makes no promise
        # about it, so left to chance the mesh could be visited first and the
        # test would pass against an implementation that still aborts.  Pinned:
        # the failing companion comes first, and the mesh after it.
        ordered = [self.s('Leia', 'notes.txt'), self.s('Leia', 'head.stl')]
        with mock.patch.object(converter, '_walk', lambda root: iter(ordered)), \
                mock.patch.object(converter.shutil, 'copy2', die):
            summary = self.run_prepare()

        self.assertEqual(summary.copy_failed, 1)
        self.assertEqual(summary.copied, 0)
        # The mesh after it was still found: the walk continued.
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.seen[0].path.endswith('head.stl'))

    def test_a_failed_companion_copy_is_counted_once_per_source(self):
        """Two bad companions are two failures, not one abort."""
        os.makedirs(self.s('Leia'), exist_ok=True)
        for name in ('one.txt', 'two.txt'):
            with open(self.s('Leia', name), 'w') as f:
                f.write('companion')

        def die(src, dst, *args, **kwargs):
            raise OSError("no")

        with mock.patch.object(converter.shutil, 'copy2', die):
            summary = self.run_prepare()

        self.assertEqual(summary.copy_failed, 2)

    def test_an_interrupted_companion_copy_leaves_nothing_behind(self):
        """A04, for the companion copy: `check` reads this path back too.

        A half-copied companion at its destination is reported as
        ALREADY_COPIED on the next run, so the partial file becomes permanent.
        The same staging rule as the STL writers applies.
        """
        companion = self.s('Leia', 'notes.txt')
        os.makedirs(os.path.dirname(companion), exist_ok=True)
        with open(companion, 'w') as f:
            f.write('companion payload')

        def die(src, dst, *args, **kwargs):
            with open(dst, 'wb') as f:
                f.write(b'half')
            raise RuntimeError("interrupted mid-copy")

        with mock.patch.object(converter.shutil, 'copy2', die):
            with self.assertRaises(RuntimeError):
                self.run_prepare()

        copied = self.o('Leia', 'notes.txt')
        self.assertFalse(os.path.exists(copied),
                         "a partial companion copy will be called ALREADY_COPIED")

    def test_a_raising_conversion_is_emitted_not_dropped(self):
        """R01/T04: returning False was handled; raising was not.

        A converter that throws is the same event as one that returns False —
        the file was not converted — but the exception reached the pool, which
        reports it through `error`, and `next_item` ignored that argument.  The
        file then left no trace at all: not emitted, not counted, not failed.
        """
        _ascii_stl(self.s('Leia', 'head.stl'))

        def explode(source, export):
            raise RuntimeError("converter fell over")

        summary = self.run_prepare(convert=explode)
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0].is_valid)
        self.assertIn("converter fell over", self.seen[0].problem)
        self.assertEqual(summary.conversion_failed, 1)

    def test_without_a_converter_the_file_is_emitted_invalid(self):
        _ascii_stl(self.s('Leia', 'head.stl'))
        summary = self.run_prepare(convert=None)
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(self.seen[0].is_valid)
        self.assertIn('no converter', self.seen[0].problem)

    def test_many_conversions_are_all_emitted_once(self):
        for n in range(20):
            _ascii_stl(self.s('Leia', f'part{n}.stl'))
        summary = self.run_prepare(convert=self._fake_convert(), workers=4)
        self.assertEqual(len(self.seen), 20)
        self.assertEqual(summary.converted, 20)
        self.assertEqual(summary.emitted, 20)

    def test_emit_is_never_called_concurrently(self):
        """The guarantee the caller relies on — a deliberately unsafe consumer.

        Without this, nothing in the suite would fail if the lock were removed:
        the count tests only check totals, which a racing emit usually still
        gets right.
        """
        for n in range(40):
            _ascii_stl(self.s(f'p{n}.stl'))

        inside = {'now': 0, 'max': 0}
        guard = threading.Lock()

        def unsafe_consumer(mesh):
            with guard:
                inside['now'] += 1
                inside['max'] = max(inside['max'], inside['now'])
            time.sleep(0.002)              # widen the window for an overlap
            with guard:
                inside['now'] -= 1

        prepare(self.src, self.out, unsafe_consumer,
                copy_extensions=COPY_EXTS,
                convert=self._fake_convert(), workers=8)
        self.assertEqual(inside['max'], 1,
                         "emit ran concurrently — the consumer would need its "
                         "own lock, which the contract says it does not")

    def test_counts_are_exact_under_concurrency(self):
        """The counters are touched from worker threads; they must not race."""
        for n in range(40):
            _ascii_stl(self.s(f'ok{n}.stl'))
        summary = self.run_prepare(convert=self._fake_convert(), workers=8)
        self.assertEqual(summary.converted, 40)
        self.assertEqual(summary.conversion_failed, 0)
        self.assertEqual(summary.emitted, 40)
        self.assertEqual(len(self.seen), 40)


class TestMixedTree(ConverterCase):

    def test_a_realistic_folder(self):
        _binary_stl(self.s('Leia', 'body.stl'))
        _binary_stl(self.s('Leia', 'head.stl'))
        _ascii_stl(self.s('Leia', 'hand.stl'))
        _touch(self.s('Leia', 'render.png'))
        _touch(self.s('Leia', 'notes.txt'))
        _binary_stl(self.s('Leia', 'done.stl'))
        _binary_stl(self.o('Leia', 'done.stl'))       # already fixed

        def convert(source, destination):
            _binary_stl(destination)
            return True, destination

        summary = self.run_prepare(convert=convert)
        emitted = sorted(os.path.basename(m.path) for m in self.seen)
        self.assertEqual(emitted, ['body.stl', 'hand.stl', 'head.stl'])
        self.assertEqual(summary.copied, 2)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.converted, 1)
        self.assertTrue(all(m.is_valid for m in self.seen))


if __name__ == '__main__':
    unittest.main(verbosity=2)
