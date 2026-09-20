"""Guards for the four model-loss fixtures added during the `libs/` review.

These tests establish what each input contains. They do not approve the
pipeline's current response; the corresponding review items remain open.
"""

from pathlib import Path
import unittest

from libs import decimator, mesh_io, scanner, splitter, welder
from tests.tests import make_fixtures


PROBES = Path(__file__).resolve().parents[1] / 'probes'


def load(name):
    path = PROBES / f'{name}.stl'
    return mesh_io.load(mesh_io.probe(str(path), '/tmp/unused.stl'))


class TestModelLossFixtures(unittest.TestCase):

    def test_small_valid_shell_is_clean_and_below_the_face_floor(self):
        mesh = load('small_valid_shell')
        self.assertTrue(scanner.scan(mesh).is_clean)
        self.assertEqual(sorted(map(len, scanner.shells(mesh))), [4, 760])

    def test_opposite_shell_volumes_cancel(self):
        mesh = load('opposite_volume_shells')
        self.assertTrue(scanner.scan(mesh).is_clean)
        parts = splitter.by_shells(mesh, min_faces=0)
        volumes = [scanner.volume(part) for part in parts]
        self.assertEqual(sorted(part.triangles for part in parts), [4, 760])
        self.assertLess(volumes[0] * volumes[1], 0.0)
        self.assertLess(abs(sum(volumes)), max(map(abs, volumes)) * 1e-6)

    def test_appendage_is_shortened_by_decimation(self):
        mesh = load('decimation_lost_appendage')
        self.assertTrue(scanner.scan(mesh).is_clean)
        self.assertAlmostEqual(float(mesh.geometry.verts[:, 0].max()), 10.25)
        result = decimator.decimate(mesh, 20)
        if result.rung is not decimator.Rung.FAST_SIMPLIFICATION:
            self.skipTest('fixture is calibrated for fast_simplification')
        self.assertTrue(scanner.scan(result.mesh).is_clean)
        self.assertLess(float(result.mesh.geometry.verts[:, 0].max()), 10.15)

    def test_tjunction_path_runs_backward_along_the_spanning_edge(self):
        verts, faces = make_fixtures.build_reversed_tjunction_chain()
        first, second = len(verts) - 2, len(verts) - 1
        a, b = int(faces[200, 0]), int(faces[-1, 1])
        along = verts[b].astype(float) - verts[a]
        positions = [float((verts[v] - verts[a]) @ along / (along @ along))
                     for v in (first, second)]
        self.assertGreater(positions[0], positions[1])

        mesh = load('reversed_tjunction_chain')
        self.assertEqual(scanner.scan(mesh).open_edges, 4)
        self.assertEqual(len(welder.find(mesh)), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
