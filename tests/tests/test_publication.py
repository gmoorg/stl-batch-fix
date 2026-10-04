"""Expected-path collision detection and post-crash reconciliation."""

import tempfile
import unittest
from pathlib import Path

from libs import publication
from libs.indicators import Indicator
from libs.mesh_io import Kind, Mesh


def _mesh(destination):
    return Mesh('/src/x.stl', destination, Kind.BINARY_STL, 10, True)


class TestExpectedPaths(unittest.TestCase):
    def test_includes_destination_and_every_marker(self):
        paths = publication.expected_paths('/out/a.stl')
        self.assertIn('/out/a.stl', paths)
        self.assertIn('/out/a.failed.stl', paths)
        self.assertIn('/out/a.timeout.stl', paths)
        self.assertIn('/out/a.destroyed.stl', paths)


class TestCollisions(unittest.TestCase):
    def test_no_collision_for_distinct_destinations(self):
        meshes = [_mesh('/out/a.stl'), _mesh('/out/b.stl')]
        self.assertEqual(publication.find_collisions(meshes), {})

    def test_direct_destination_collision(self):
        m1, m2 = _mesh('/out/a.stl'), _mesh('/out/a.stl')
        collisions = publication.find_collisions([m1, m2])
        self.assertIn('/out/a.stl', collisions)
        self.assertCountEqual(collisions['/out/a.stl'], [m1, m2])

    def test_destination_vs_marker_collision(self):
        # m1's destination is exactly m2's ".failed.stl" marker path.
        m1 = _mesh('/out/a.failed.stl')
        m2 = _mesh('/out/a.stl')
        collisions = publication.find_collisions([m1, m2])
        self.assertIn('/out/a.failed.stl', collisions)
        self.assertCountEqual(collisions['/out/a.failed.stl'], [m1, m2])


class TestPreexistingPaths(unittest.TestCase):
    def test_empty_when_nothing_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _mesh(str(Path(tmp) / 'a.stl'))
            self.assertEqual(publication.preexisting_paths(mesh), frozenset())

    def test_finds_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a.stl'
            dest.write_bytes(b'x')
            mesh = _mesh(str(dest))
            self.assertEqual(publication.preexisting_paths(mesh), {str(dest)})

    def test_finds_existing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a.stl'
            marker = Path(tmp) / 'a.failed.stl'
            marker.write_bytes(b'x')
            mesh = _mesh(str(dest))
            self.assertEqual(publication.preexisting_paths(mesh), {str(marker)})


class TestReconcile(unittest.TestCase):
    def test_nothing_new_is_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = _mesh(str(Path(tmp) / 'a.stl'))
            recon = publication.reconcile(mesh, frozenset())
            self.assertEqual(recon.kind, 'NOTHING')

    def test_one_new_path_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a.stl'
            dest.write_bytes(b'x')
            mesh = _mesh(str(dest))
            recon = publication.reconcile(mesh, frozenset())
            self.assertEqual(recon.kind, 'RECOVERED')
            self.assertEqual(recon.path, str(dest))

    def test_baseline_path_is_not_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a.stl'
            dest.write_bytes(b'x')
            mesh = _mesh(str(dest))
            recon = publication.reconcile(mesh, frozenset({str(dest)}))
            self.assertEqual(recon.kind, 'NOTHING')

    def test_two_new_paths_is_inconsistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'a.stl'
            marker = Path(tmp) / 'a.failed.stl'
            dest.write_bytes(b'x')
            marker.write_bytes(b'y')
            mesh = _mesh(str(dest))
            recon = publication.reconcile(mesh, frozenset())
            self.assertEqual(recon.kind, 'INCONSISTENT')
            self.assertEqual(set(recon.detail), {str(dest), str(marker)})


class TestMarkerIndicatorForPath(unittest.TestCase):
    def test_destination_is_process(self):
        self.assertEqual(
            publication.marker_indicator_for_path('/out/a.stl', '/out/a.stl'),
            Indicator.PROCESS)

    def test_marker_path_is_its_own_indicator(self):
        self.assertEqual(
            publication.marker_indicator_for_path('/out/a.stl', '/out/a.failed.stl'),
            Indicator.FAILED)

    def test_unrelated_path_raises(self):
        with self.assertRaises(ValueError):
            publication.marker_indicator_for_path('/out/a.stl', '/out/unrelated.stl')


if __name__ == '__main__':
    unittest.main()
