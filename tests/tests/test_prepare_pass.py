"""The batch prepare pass: initial decimation, the decimation cache, and the
handoff to the repair pass (batch_repair._prepare_one_file)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import batch_repair
from libs import childresult, decimator, mesh_io, processor
from libs.indicators import Indicator
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds


class _Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='prepare-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.inp, self.out = self.root / 'in', self.root / 'out'
        self.source = self.inp / 'sub' / 'ball.stl'
        self.source.parent.mkdir(parents=True)
        v, f = ds.sphere(10.0, 40)                     # 3,040 faces
        self.faces = len(f)
        mesh_io.write(Mesh(str(self.source), str(self.source), Kind.BINARY_STL, len(f), True,
                           None, Geometry(np.asarray(v, np.float32), np.asarray(f, np.int64))))
        self.destination = str(self.out / 'sub' / 'ball.stl')

    def cache(self, max_faces):
        return batch_repair.decimated_path(str(self.inp), str(self.source), max_faces)

    def prepare(self, max_faces):
        return batch_repair._prepare_one_file(str(self.source), self.destination, max_faces,
                                              self.cache(max_faces))


class TestCachePath(_Case):
    def test_target_and_settings_are_in_the_name_under_the_input_tree(self):
        tag = decimator.settings_tag()
        self.assertEqual(self.cache(900000),
                         str(self.inp / 'stl-decimated' / 'sub' / f'ball.stl.900000.{tag}.stl'))

    def test_new_decimator_settings_get_their_own_file(self):
        before = self.cache(1000)
        with mock.patch.dict(decimator.QUADRIC_PARAMS, planarquadric=False):
            self.assertNotEqual(self.cache(1000), before)

    def test_each_target_has_its_own_file(self):
        self.assertNotEqual(self.cache(1000), self.cache(2000))

    def test_a_converted_source_never_shares_a_native_sources_cache(self):
        """Both bound for out/a.stl, possibly in different runs: the
        converted job loads stl-exported/a.stl, the native one a.stl."""
        native = batch_repair.decimated_path('/in', '/in/a.stl', 9)
        converted = batch_repair.decimated_path('/in', '/in/stl-exported/a.stl', 9)
        self.assertNotEqual(native, converted)
        self.assertEqual(converted,
                         f'/in/stl-decimated/stl-exported/a.stl.9.{decimator.settings_tag()}.stl')


class TestPrepare(_Case):
    def test_no_decimation_needed_hands_off_the_source(self):
        for max_faces in (0, self.faces):
            with self.subTest(max_faces=max_faces):
                result = self.prepare(max_faces)
                self.assertTrue(result.is_handoff, result.reason)
                self.assertEqual(result.prepared_path, str(self.source))
                self.assertFalse(os.path.exists(self.cache(max_faces)))

    def test_decimation_is_cached_and_estimated_on_the_cache(self):
        result = self.prepare(1000)
        self.assertTrue(result.is_handoff, result.reason)
        self.assertEqual(result.prepared_path, self.cache(1000))
        cached = mesh_io.load(mesh_io.probe(self.cache(1000), '/x'))
        self.assertLessEqual(cached.triangles, 1000)
        self.assertGreater(result.estimate_bytes, 0)
        self.assertFalse(os.path.exists(self.destination), 'prepare publishes nothing')

    def test_a_cached_decimation_is_reused(self):
        self.prepare(1000)
        with mock.patch.object(decimator, '_decimate_meshlab',
                               side_effect=AssertionError('decimated again')):
            result = self.prepare(1000)
        self.assertTrue(result.is_handoff, result.reason)
        self.assertTrue(any('cached' in step for step in result.steps), result.steps)

    def test_a_cache_from_before_the_settings_tag_is_not_reused(self):
        """Untagged files were decimated with other settings: the job
        decimates again and writes the tagged file; the old one stays."""
        old = Path(str(self.inp / 'stl-decimated' / 'sub' / 'ball.stl.1000.stl'))
        old.parent.mkdir(parents=True)
        old.write_bytes(b'stale cache, must not be read')
        result = self.prepare(1000)
        self.assertTrue(result.is_handoff, result.reason)
        self.assertEqual(result.prepared_path, self.cache(1000))
        self.assertFalse(any('cached' in step for step in result.steps), result.steps)
        self.assertEqual(old.read_bytes(), b'stale cache, must not be read')

    def test_an_unreadable_cache_is_rebuilt(self):
        Path(self.cache(1000)).parent.mkdir(parents=True)
        Path(self.cache(1000)).write_bytes(b'not an stl')
        result = self.prepare(1000)
        self.assertTrue(result.is_handoff, result.reason)
        self.assertLessEqual(mesh_io.load(mesh_io.probe(self.cache(1000), '/x')).triangles, 1000)

    def test_a_decimator_failure_publishes_undecimated_and_ends_the_job(self):
        with mock.patch.object(decimator, '_decimate_meshlab', side_effect=RuntimeError('boom')):
            result = self.prepare(1000)
        self.assertFalse(result.is_handoff)
        self.assertEqual((result.category, result.indicator), ('published', 'UNDECIMATED'))
        self.assertEqual(Path(result.written_path).read_bytes(), self.source.read_bytes())
        self.assertFalse(os.path.exists(self.cache(1000)))

    def test_a_directory_at_the_cache_path_fails_the_job_visibly(self):
        """An input folder named like a cache file: never read as a cache,
        never silently replaced; the job fails and says why."""
        os.makedirs(os.path.join(self.cache(1000), 'b.stl'))
        result = self.prepare(1000)
        self.assertFalse(result.is_handoff)
        self.assertEqual(result.category, 'write_failure')
        self.assertIn('is a directory', result.reason)

    def test_an_invalid_source_ends_the_job(self):
        self.source.write_bytes(b'garbage')
        result = self.prepare(1000)
        self.assertFalse(result.is_handoff)
        self.assertEqual(result.category, 'intake_failure')

    def test_the_handoff_passes_parent_validation(self):
        """What the child writes is what the parent's strict read accepts."""
        result = self.prepare(1000)
        path = str(self.root / 'r.json')
        childresult.write(path, result)
        probed = mesh_io.probe(str(self.source), self.destination)
        expected = batch_repair.expected_prepared_path(probed, self.cache(1000), 1000)
        got = childresult.read_and_validate(path, str(self.source), mode='prepare',
                                            expected_prepared=expected)
        self.assertEqual(got, result)


class TestRepairFromPrepared(_Case):
    def test_markers_copy_the_original_source_not_the_cache(self):
        prepared = self.prepare(1000)
        seen = {}

        def failing(mesh, max_faces, **kwargs):
            seen['faces'], seen['max_faces'] = mesh.triangles, max_faces
            return processor.Outcome(Indicator.FAILED, None, 'source', 'forced')

        with mock.patch.object(processor, 'process', failing):
            result = batch_repair._process_one_file(str(self.source), self.destination, 0,
                                                    load_path=prepared.prepared_path)
        self.assertLessEqual(seen['faces'], 1000, 'repair loaded the decimated cache')
        self.assertEqual(seen['max_faces'], 0)
        self.assertEqual(result.indicator, 'FAILED')
        self.assertEqual(Path(result.written_path).read_bytes(), self.source.read_bytes())


if __name__ == '__main__':
    unittest.main()
