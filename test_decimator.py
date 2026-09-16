"""Tests for libs.decimator — the ladder, not the algorithms.

The three rungs are third-party quadric edge collapse; testing *that* would be
testing fast_simplification. What is ours is the ladder: which rung runs, what
happens when one fails, that a mesh already within budget is left alone, and
that the Blender rung's temporary files do not survive it.

Rungs are forced by patching the module's availability flags, so a test can
walk to the second or third rung without needing a rung to genuinely break.
"""

import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import libs.decimator as decimator
from libs.decimator import Result, Rung, available_rungs, decimate, is_available
from libs.mesh_io import Geometry, Kind, Mesh, load, probe


def _sphere(subdivisions=4):
    """An icosphere as (verts, faces) — a real closed mesh, cheaply generated.

    A tetrahedron is too small to decimate meaningfully; this gives a few
    thousand faces with genuine connectivity for edge collapse to work on.
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


def _write_stl(path, verts, faces):
    n = len(faces)
    tv = verts[faces].astype(np.float32)
    buf = np.zeros((n, 50), dtype=np.uint8)
    buf[:, 12:48] = tv.reshape(n, 9).view(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', n))
        f.write(buf.tobytes())
    return path


class DecimatorCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.verts, cls.faces = _sphere(subdivisions=4)   # 5120 faces

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='decimator-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def loaded(self):
        """A loaded Mesh built from the shared sphere."""
        path = _write_stl(os.path.join(self.dir, 'sphere.stl'),
                          self.verts, self.faces)
        return load(probe(path))

    def probed(self):
        path = _write_stl(os.path.join(self.dir, 'sphere.stl'),
                          self.verts, self.faces)
        return probe(path)


class TestAvailability(DecimatorCase):

    def test_is_available_when_a_library_is_present(self):
        self.assertTrue(is_available())

    def test_is_available_is_false_with_nothing_at_all(self):
        """Decimation is a deliverable: a false here means do not start."""
        with mock.patch.object(decimator, '_FASTSIMP', False), \
             mock.patch.object(decimator, '_PYMESHLAB', False), \
             mock.patch.object(decimator.blender, 'is_available',
                               return_value=False):
            self.assertFalse(is_available())

    def test_available_rungs_reports_what_is_missing(self):
        with mock.patch.object(decimator, '_FASTSIMP', False), \
             mock.patch.object(decimator.blender, 'is_available',
                               return_value=False):
            self.assertEqual(available_rungs(), (Rung.PYMESHLAB,))


class TestNotNeeded(DecimatorCase):

    def test_a_mesh_within_budget_is_returned_untouched(self):
        mesh = self.loaded()
        result = decimate(mesh, max_faces=999_999)
        self.assertIs(result.rung, Rung.NOT_NEEDED)
        self.assertIs(result.mesh, mesh, "it rebuilt a mesh with nothing to do")
        self.assertFalse(result.ran)

    def test_zero_disables_decimation(self):
        """MAX_FACES = 0 meant disabled in the old script; keep that."""
        result = decimate(self.loaded(), max_faces=0)
        self.assertIs(result.rung, Rung.NOT_NEEDED)

    def test_an_unloaded_mesh_raises(self):
        """A programming error at the call site, not a property of the data."""
        with self.assertRaises(ValueError):
            decimate(self.probed(), max_faces=100)


class TestFastSimplification(DecimatorCase):

    def test_it_reduces_to_about_the_target(self):
        mesh = self.loaded()
        before = mesh.triangles
        result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.FAST_SIMPLIFICATION)
        self.assertLess(result.faces_out, before)
        # Quadric collapse lands near the target, not exactly on it.
        self.assertLessEqual(result.faces_out, 1200)
        self.assertGreater(result.faces_out, 700)

    def test_the_result_is_a_loaded_mesh(self):
        """Mesh in, mesh out — the caller can keep working in memory."""
        result = decimate(self.loaded(), max_faces=1000)
        self.assertTrue(result.mesh.is_loaded)
        self.assertEqual(result.mesh.triangles, len(result.mesh.geometry.faces))

    def test_the_input_mesh_is_not_mutated(self):
        mesh = self.loaded()
        before = mesh.triangles
        decimate(mesh, max_faces=1000)
        self.assertEqual(mesh.triangles, before)
        self.assertEqual(len(mesh.geometry.faces), before)

    def test_the_path_is_carried_through(self):
        mesh = self.loaded()
        result = decimate(mesh, max_faces=1000)
        self.assertEqual(result.mesh.path, mesh.path)

    def test_faces_index_within_the_vertex_array(self):
        """A decimator that returned stale indices would write garbage."""
        result = decimate(self.loaded(), max_faces=1000)
        g = result.mesh.geometry
        self.assertGreaterEqual(int(g.faces.min()), 0)
        self.assertLess(int(g.faces.max()), len(g.verts))

    def test_no_files_are_written(self):
        """The common path must not touch the disk at all."""
        mesh = self.loaded()                    # the fixture's own file first
        before = set(os.listdir(self.dir))
        decimate(mesh, max_faces=1000)
        self.assertEqual(set(os.listdir(self.dir)), before)


@unittest.skipUnless(decimator._PYMESHLAB, "pymeshlab not installed")
class TestPyMeshLabRung(DecimatorCase):

    def test_it_runs_when_fastsimp_is_missing(self):
        with mock.patch.object(decimator, '_FASTSIMP', False):
            result = decimate(self.loaded(), max_faces=1000)
        self.assertIs(result.rung, Rung.PYMESHLAB)
        self.assertLess(result.faces_out, result.faces_in)

    def test_it_runs_when_fastsimp_fails(self):
        """The fallback exists for failure, not only for absence."""
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=RuntimeError("boom")):
            result = decimate(self.loaded(), max_faces=1000)
        self.assertIs(result.rung, Rung.PYMESHLAB)
        self.assertEqual(len(result.attempts), 1)
        self.assertIs(result.attempts[0][0], Rung.FAST_SIMPLIFICATION)
        self.assertIn('boom', result.attempts[0][1])

    def test_it_returns_a_loaded_mesh_too(self):
        with mock.patch.object(decimator, '_FASTSIMP', False):
            result = decimate(self.loaded(), max_faces=1000)
        self.assertTrue(result.mesh.is_loaded)

    def test_it_writes_no_files_either(self):
        """PyMeshLab runs in-process; a disk round trip would be for nothing."""
        mesh = self.loaded()                    # the fixture's own file first
        before = set(os.listdir(self.dir))
        with mock.patch.object(decimator, '_FASTSIMP', False):
            decimate(mesh, max_faces=1000)
        self.assertEqual(set(os.listdir(self.dir)), before)


class TestBlenderRung(DecimatorCase):
    """The forced-disk rung, with Blender itself faked.

    Blender is a separate process; what is tested here is our side of the
    boundary — that a temp gets written, the result read back, and nothing left
    behind.
    """

    def _fake_run(self, ok=True, timed_out=False, write_output=True):
        """Stand in for blender.Runner.run, writing a decimated file itself."""
        verts, faces = self.verts, self.faces

        def run(script, timeout):
            # The script carries the paths; pull the destination back out of it
            # the same way Blender would receive it.
            dst = None
            for line in script.splitlines():
                if line.startswith('dst'):
                    dst = line.split('=', 1)[1].strip().strip("'\"")
            if write_output and dst:
                _write_stl(dst, verts, faces[:500])
            return mock.Mock(
                exit_code=0 if ok else 1,
                stdout_capture='BLENDER_DECIMATE_OK' if ok else 'boom',
                stderr_capture='' if ok else 'blender failed',
                is_timed_out=timed_out,
                second_elapsed=1.0)
        return run

    def _no_python_rungs(self):
        return mock.patch.multiple(decimator, _FASTSIMP=False, _PYMESHLAB=False)

    def test_it_runs_when_both_python_rungs_are_gone(self):
        with self._no_python_rungs(), \
             mock.patch.object(decimator.blender, 'Runner') as Runner:
            Runner.return_value.run = self._fake_run()
            result = decimate(self.loaded(), max_faces=1000)
        self.assertIs(result.rung, Rung.BLENDER)
        self.assertEqual(result.faces_out, 500)
        self.assertTrue(result.mesh.is_loaded)

    def test_its_temporary_files_do_not_survive(self):
        """Scratch, not a step output — nothing downstream may read them."""
        import glob
        with self._no_python_rungs(), \
             mock.patch.object(decimator.blender, 'Runner') as Runner:
            Runner.return_value.run = self._fake_run()
            decimate(self.loaded(), max_faces=1000)
        leftover = glob.glob(os.path.join(tempfile.gettempdir(), 'decimate-*'))
        self.assertEqual(leftover, [], f"temp dirs left behind: {leftover}")

    def test_a_timeout_is_recorded_as_a_failed_attempt(self):
        with self._no_python_rungs(), \
             mock.patch.object(decimator.blender, 'Runner') as Runner:
            Runner.return_value.run = self._fake_run(timed_out=True,
                                                     write_output=False)
            result = decimate(self.loaded(), max_faces=1000, timeout=5)
        self.assertIs(result.rung, Rung.FAILED)
        self.assertIn('timed out', result.attempts[-1][1])

    def test_a_missing_output_file_is_a_failure_not_a_crash(self):
        """Blender can report success and write nothing."""
        with self._no_python_rungs(), \
             mock.patch.object(decimator.blender, 'Runner') as Runner:
            Runner.return_value.run = self._fake_run(write_output=False)
            result = decimate(self.loaded(), max_faces=1000)
        self.assertIs(result.rung, Rung.FAILED)
        self.assertIn('no file', result.attempts[-1][1])


class TestEveryRungFails(DecimatorCase):

    def test_the_input_is_returned_unchanged(self):
        """Decimation is a deliverable: the caller must be able to tell
        'not decimated' from 'decimated badly' and mark the file."""
        mesh = self.loaded()
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=RuntimeError("a")), \
             mock.patch.object(decimator, '_decimate_pymeshlab',
                               side_effect=RuntimeError("b")), \
             mock.patch.object(decimator, '_decimate_blender',
                               side_effect=RuntimeError("c")):
            result = decimate(mesh, max_faces=1000)
        self.assertIs(result.rung, Rung.FAILED)
        self.assertIs(result.mesh, mesh)
        self.assertFalse(result.ran)
        self.assertEqual(result.faces_out, result.faces_in)

    def test_every_attempt_is_recorded_in_order(self):
        """A silent fallback is indistinguishable from a first-choice success."""
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=RuntimeError("a")), \
             mock.patch.object(decimator, '_decimate_pymeshlab',
                               side_effect=RuntimeError("b")), \
             mock.patch.object(decimator, '_decimate_blender',
                               side_effect=RuntimeError("c")):
            result = decimate(self.loaded(), max_faces=1000)
        self.assertEqual([r for r, _ in result.attempts],
                         [Rung.FAST_SIMPLIFICATION, Rung.PYMESHLAB,
                          Rung.BLENDER])


if __name__ == '__main__':
    unittest.main(verbosity=2)
