"""Fast, in-process unit tests for the --one-file child's own per-file logic.

`_process_one_file` is the exact per-category logic the child runs — testing
it directly (mocking `processor.process`/`processor.write` in-process, same
as the pipeline's other unit tests do) covers every terminal-category branch
without spawning a real subprocess or running real CGAL/alpha-wrap.
"""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from libs import mesh_io, processor
from libs.indicators import Indicator
from libs.mesh_io import Geometry, Kind, Mesh
from batch_repair import _process_one_file


class TestProcessOneFile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, name='a.stl'):
        path = self.root / name
        geometry = Geometry(
            np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64),
            np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=np.int64))
        mesh_io.write(Mesh(str(path), str(path), Kind.BINARY_STL, 4, True, geometry=geometry))
        return path

    def test_invalid_intake_is_intake_failure(self):
        path = self.root / 'bad.stl'
        path.write_bytes(b'\0' * 80 + struct.pack('<I', 4))
        result = _process_one_file(str(path), str(self.root / 'out.stl'), 0)
        self.assertEqual(result.category, 'intake_failure')
        self.assertEqual(result.stage, 'intake')
        self.assertIsNone(result.indicator)
        self.assertIsNone(result.written_path)

    def test_a_nonfinite_triangle_is_dropped_and_reported(self):
        """Owner, 2026-10-05: garbage in, garbage out — the triangle with a
        NaN coordinate is dropped, the rest is repaired, and the steps say so."""
        path = self.fixture()
        data = bytearray(path.read_bytes())
        struct.pack_into('<f', data, 84 + 12, float('nan'))
        path.write_bytes(data)
        with mock.patch.object(processor, 'process', return_value=processor.Outcome(
                Indicator.FAILED, None, 'source', 'forced')):
            result = _process_one_file(str(path), str(self.root / 'out.stl'), 0)
        self.assertEqual(result.category, 'published', result.reason)
        self.assertIn('load: dropped 1 triangles with NaN/inf coordinates', result.steps)

    def test_a_file_with_no_finite_triangle_is_a_load_failure(self):
        path = self.fixture()
        data = bytearray(path.read_bytes())
        count = struct.unpack_from('<I', data, 80)[0]
        for i in range(count):
            struct.pack_into('<f', data, 84 + 50 * i + 12, float('nan'))
        path.write_bytes(data)
        result = _process_one_file(str(path), str(self.root / 'out.stl'), 0)
        self.assertEqual(result.category, 'load_failure')
        self.assertEqual(result.stage, 'load')
        self.assertIn('no finite triangles', result.reason)

    def test_clean_publish(self):
        path = self.fixture()
        dest = self.root / 'out.stl'
        with mock.patch.object(processor, 'process', return_value=processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(path), str(dest))),
                None, 'clean')):
            result = _process_one_file(str(path), str(dest), 0)
        self.assertEqual(result.category, 'published')
        self.assertEqual(result.indicator, 'PROCESS')
        self.assertTrue(result.clean)
        self.assertEqual(result.written_path, str(dest))
        self.assertTrue(dest.exists())

    def test_failed_repair_publishes_marker(self):
        path = self.fixture()
        dest = self.root / 'out.stl'
        with mock.patch.object(processor, 'process', return_value=processor.Outcome(
                Indicator.DESTROYED, None, 'source', 'lost geometry')):
            result = _process_one_file(str(path), str(dest), 0)
        self.assertEqual(result.category, 'published')
        self.assertEqual(result.indicator, 'DESTROYED')
        self.assertFalse(result.clean)
        self.assertEqual(result.reason, 'lost geometry')
        self.assertEqual((self.root / 'out.destroyed.stl').read_bytes(), path.read_bytes())

    def test_process_exception_is_process_failure(self):
        path = self.fixture()
        dest = self.root / 'out.stl'
        with mock.patch.object(processor, 'process', side_effect=RuntimeError('repair threw')):
            result = _process_one_file(str(path), str(dest), 0)
        self.assertEqual(result.category, 'process_failure')
        self.assertEqual(result.stage, 'process')
        self.assertIn('repair threw', result.reason)

    def test_write_returning_none_is_write_failure(self):
        path = self.fixture()
        dest = self.root / 'out.stl'
        with mock.patch.object(processor, 'process', return_value=processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(path), str(dest))),
                None, 'clean')), \
             mock.patch.object(processor, 'write', return_value=None):
            result = _process_one_file(str(path), str(dest), 0)
        self.assertEqual(result.category, 'write_failure')
        self.assertIn('returned None', result.reason)

    def test_write_exception_is_write_failure(self):
        path = self.fixture()
        dest = self.root / 'out.stl'
        with mock.patch.object(processor, 'process', return_value=processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(path), str(dest))),
                None, 'clean')), \
             mock.patch.object(processor, 'write', side_effect=OSError('disk failure')):
            result = _process_one_file(str(path), str(dest), 0)
        self.assertEqual(result.category, 'write_failure')
        self.assertIn('disk failure', result.reason)

    def test_nested_process_group_reaches_processor_process(self):
        """`--managed-child`'s ultimate destination: `_process_one_file`'s
        own `nested_process_group` parameter must reach `processor.process`
        as the SAME keyword, unmodified."""
        path = self.fixture()
        dest = self.root / 'out.stl'
        captured = {}

        def fake_process(mesh, max_faces, **kwargs):
            captured['nested_process_group'] = kwargs.get('nested_process_group')
            return processor.Outcome(
                Indicator.PROCESS, mesh_io.load(mesh_io.probe(str(path), str(dest))),
                None, 'clean')

        with mock.patch.object(processor, 'process', fake_process):
            _process_one_file(str(path), str(dest), 0, nested_process_group=True)
        self.assertTrue(captured['nested_process_group'])

        with mock.patch.object(processor, 'process', fake_process):
            _process_one_file(str(path), str(dest), 0, nested_process_group=False)
        self.assertFalse(captured['nested_process_group'])

        with mock.patch.object(processor, 'process', fake_process):
            _process_one_file(str(path), str(dest), 0)   # default
        self.assertFalse(captured['nested_process_group'])


if __name__ == '__main__':
    unittest.main()
