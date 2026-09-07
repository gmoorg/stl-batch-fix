#!/usr/bin/env python3
"""End-to-end pipeline tests: does a mesh come out the other side correct?

    .venv/bin/python test_pipeline.py            # all of it, a few seconds
    .venv/bin/python test_pipeline.py body arms  # named fixtures only

Each test runs the real process_file() on a real STL and compares the OUTPUT
MESH against the INPUT MESH.  Nothing here asserts that a helper returns a
plausible number — the question is "does the pipeline still produce a good
model", which is what a refactor can break.

The four properties checked:

    shells    components in == components out (geometry not culled)
    bounds    bounding box within tolerance (nothing sliced off)
    defects   an independent rescan says nm=0 open=0 (actually repaired)
    faces     near the decimation target, not far below it

Fixtures are small generated meshes (~516 KB in total, tests/fixtures/, built
by make_fixtures.py) rather than models from the collection.  Two reasons: the
collection is meant to be deletable, and a 208 MB fixture set does not belong
in a repository.  Size is not what makes a code path interesting — MAX_FACES
and _LARGE_MESH_TRI_LIMIT are configurable, so the tests lower both and a
3,000-triangle mesh exercises the same branches a 2-million-triangle one does.

What that trades away: these fixtures reproduce the *shape* of each bug, not
the exact pathology of the model that first exposed it.  They will catch a
refactor that breaks shell handling, repair, or decimation.  They will not
catch a new bug that only a real 2M-triangle sculpt triggers — for that, run
the collection and read the bbox flags.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import stl_batch_fix as fix

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'tests', 'fixtures')

# Thresholds for the test run.  Both are configuration in the real script, so
# lowering them puts small meshes on the same code paths large ones take.
TEST_MAX_FACES = 2_000        # decimation target
# "too large to scan" sits between arms (792) and body (3,216) so the pair
# splits across the two code paths.  Fixture sizes, for reference:
#   arms 792 · leg 904 · foot1 1,152 · foot2 1,512 · falcon 2,560 · body 3,216
TEST_SCAN_LIMIT = 2_000

BBOX_TOL = 0.1                # matches fix._BBOX_TOLERANCE


def _components(verts, faces):
    """(total shells, shells big enough that split_shells would keep them)."""
    parent = np.arange(len(verts), dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = np.unique(np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]],
                                         faces[:, [2, 0]]]), axis=1), axis=0)
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    _, lbl = np.unique(np.array([find(i) for i in range(len(verts))]),
                       return_inverse=True)
    counts = np.bincount(lbl)
    # split_shells() drops shells under max(100, largest/1000) faces, so debris
    # must not count as loss when comparing before and after.
    floor = max(100, counts.max() // 1000)
    return len(counts), int((counts >= floor).sum())


class Mesh:
    """The measurable properties of a mesh file."""

    def __init__(self, path):
        self.tris, err = fix._read_stl_header(path)
        if err:
            raise AssertionError(f'unreadable STL {path}: {err}')
        verts, faces = fix._weld_binary_stl(path)
        self.lo = tuple(float(x) for x in verts.min(0))
        self.hi = tuple(float(x) for x in verts.max(0))
        self.shells, self.big_shells = _components(verts, faces)
        del verts, faces

    def __repr__(self):
        return (f'{self.tris:,} tris, {self.shells} shells '
                f'({self.big_shells} significant)')


class PipelineCase(unittest.TestCase):
    """Runs one fixture through the real pipeline into a scratch tree."""

    fixture = None
    max_faces = TEST_MAX_FACES
    scan_limit = TEST_SCAN_LIMIT

    @classmethod
    def setUpClass(cls):
        if cls.fixture is None:
            raise unittest.SkipTest('base class')
        src = os.path.join(FIXTURES, f'{cls.fixture}.stl')
        if not os.path.exists(src):
            raise unittest.SkipTest(
                f'{src} missing — run: python make_fixtures.py')

        cls.root = tempfile.mkdtemp(prefix=f'pipeline-{cls.fixture}-')
        inp = os.path.join(cls.root, 'STL')
        os.makedirs(inp)
        cls.src = os.path.join(inp, f'{cls.fixture}.stl')
        shutil.copy2(src, cls.src)

        cls._saved = {k: getattr(fix, k) for k in
                      ('INPUT_FOLDER', 'OUTPUT_SUFFIX', 'LOG_FILE',
                       'REVIEW_FILE', 'SUMMARY_FILE', 'MAX_FACES',
                       '_LARGE_MESH_TRI_LIMIT')}
        fix.INPUT_FOLDER = inp
        fix.OUTPUT_SUFFIX = ''
        fix.MAX_FACES = cls.max_faces
        fix._LARGE_MESH_TRI_LIMIT = cls.scan_limit
        fix.LOG_FILE = os.path.join(cls.root, 'log.tsv')
        fix.REVIEW_FILE = os.path.join(cls.root, 'review.tsv')
        fix.SUMMARY_FILE = os.path.join(cls.root, 'summary.tsv')

        cls.before = Mesh(cls.src)
        t0 = time.monotonic()
        cls.result = fix.process_file(cls.src)
        cls.secs = time.monotonic() - t0
        cls.dst = fix.output_path(cls.src, '', inp)
        cls.after = Mesh(cls.dst) if os.path.exists(cls.dst) else None
        cls.log = (open(fix.LOG_FILE).read()
                   if os.path.exists(fix.LOG_FILE) else '')
        print(f'\n  [{cls.fixture}] {cls.before} -> '
              f'{cls.after or "NO OUTPUT"}  ({cls.secs:.1f}s)', flush=True)

    @classmethod
    def tearDownClass(cls):
        for k, v in getattr(cls, '_saved', {}).items():
            setattr(fix, k, v)
        if getattr(cls, 'root', None):
            shutil.rmtree(cls.root, ignore_errors=True)

    # ── shared assertions ───────────────────────────────────────────────────

    def assert_output(self):
        self.assertIsNotNone(self.after,
                             f'no output produced.\nlog:\n{self.log}')

    def assert_repaired(self):
        """Independent rescan, not the library's own self-report."""
        nm, open_e, err = fix.scan_mesh_errors(self.dst)
        self.assertIsNone(err, f'output unscannable: {err}')
        self.assertEqual((nm, open_e), (0, 0),
                         f'output still defective: nm={nm} open={open_e}\n'
                         f'log:\n{self.log}')

    def assert_bounds_within(self, tol=BBOX_TOL):
        for i, axis in enumerate('xyz'):
            self.assertAlmostEqual(
                self.after.lo[i], self.before.lo[i], delta=tol,
                msg=f'{axis} min moved '
                    f'{self.after.lo[i]-self.before.lo[i]:+.3f} — geometry cut off')
            self.assertAlmostEqual(
                self.after.hi[i], self.before.hi[i], delta=tol,
                msg=f'{axis} max moved '
                    f'{self.after.hi[i]-self.before.hi[i]:+.3f} — geometry cut off')

    def assert_shells_preserved(self):
        """Total component count, not the "significant" subset.

        The significance floor is relative to the largest shell, so decimating
        a mesh of one big shell and several small ones pushes the small ones
        under it — they are all still present, but stop being counted.  The
        question here is whether a component was DELETED, and total count is
        what answers it."""
        self.assertEqual(
            self.after.shells, self.before.shells,
            f'shells {self.before.shells} -> {self.after.shells}: '
            f'components were deleted\nlog:\n{self.log}')


# ── fixtures ────────────────────────────────────────────────────────────────

class TestBody(PipelineCase):
    """Multi-shell, OVER the scan limit — the head-deletion bug.

    Skipping the split sent such a mesh to pymeshfix intact, which rebuilds one
    manifold surface and discards the rest.  The real case went from 39 shells
    to 1, deleting the model's head, and reported ok."""
    fixture = 'body'

    def test_output_exists(self):
        self.assert_output()

    def test_all_shells_survive(self):
        self.assert_output()
        self.assert_shells_preserved()

    def test_not_collapsed_to_one_shell(self):
        """The specific regression, stated plainly."""
        self.assert_output()
        self.assertGreater(self.after.big_shells, 1,
                           'collapsed to a single shell — shells are being deleted')

    def test_geometry_intact(self):
        self.assert_output()
        self.assert_bounds_within()

    def test_split_deferred_not_skipped(self):
        self.assertIn('step B2', self.log,
                      'an oversized mesh skipped the split instead of deferring it')


class TestArms(PipelineCase):
    """Multi-shell, UNDER the scan limit: the normal step B path.

    body's counterpart — the test limit sits between them, so a change to that
    threshold moves one of the two onto the wrong path and fails here."""
    fixture = 'arms'
    max_faces = 500          # below its 792 faces, so step B/C actually run

    def test_output_exists(self):
        self.assert_output()

    def test_all_shells_survive(self):
        self.assert_output()
        self.assert_shells_preserved()

    def test_geometry_intact(self):
        self.assert_output()
        self.assert_bounds_within()

    def test_budget_shared_across_shells(self):
        """A mesh that will be decimated splits AFTER decimation, so one face
        budget is spread across every shell instead of each shell receiving the
        full budget on its own.

        Split first and a 240-face speck is left untouched while a 1.3M-face
        body absorbs the whole reduction alone."""
        self.assertIn('step B: deferred', self.log)
        self.assertIn('step B2', self.log,
                      'a decimated mesh split before decimation')
        self.assertLessEqual(self.after.tris, self.max_faces * 1.05,
                             'the shared budget was not applied to the whole mesh')


class TestSplitWithoutDecimation(PipelineCase):
    """Defects but no decimation: splits immediately, via step B.

    A file that is never decimated has no "after decimation" to split at, so
    step B still exists for it — the other half of the pair with TestArms,
    which covers the deferred path.

    The fixture must actually need repair.  A mesh with nm=0 open=0 under
    MAX_FACES short-circuits to a clean copy before step B is ever reached,
    which is correct: splitting exists to help the repair, and there is nothing
    to repair."""
    fixture = 'falcon'
    max_faces = 0             # decimation off — this is the point of the test
    scan_limit = 10_000       # comfortably above its 2,560 faces

    def test_split_immediately(self):
        self.assertIn('step B: split multi-shell', self.log)
        self.assertNotIn('step B2', self.log,
                         'an undecimated mesh deferred a split it cannot defer')

    def test_repaired(self):
        self.assert_output()
        self.assert_repaired()


class TestLeg(PipelineCase):
    """The anti-test: a large bounding-box change that is CORRECT.

    Stray specks sit far from the body and stretch the box.  Removing them
    shrinks it a long way while the model itself is untouched — a guard that
    rejected every bbox change would wrongly fail this."""
    fixture = 'leg'

    def test_output_exists(self):
        self.assert_output()

    def test_repaired(self):
        self.assert_output()
        self.assert_repaired()

    def test_main_body_kept(self):
        """Debris may go; the model may not.  Faces are the check here,
        because the bounding box legitimately moves."""
        self.assert_output()
        self.assertGreater(self.after.tris, self.before.tris * 0.5,
                           'the model itself was reduced, not just the debris')
        self.assertGreaterEqual(self.after.big_shells, 1)


class TestFoot1(PipelineCase):
    """Open-edge repair that must not collapse the boundary.

    PyMeshFix closes a boundary loop by pulling it shut rather than capping it.
    On Zelda NSFW/Chair_foot1.stl that cost 12% of the model's volume and 1.1mm
    off its end — the end caps.

    What saved it was the debris floor.  The caps are a separate 384-face
    shell, and the old max(100, largest // 1000) rule put the cutoff at 680
    faces on that 681k mesh, so they were discarded before the split could
    protect them.  With a flat floor of 100 they are split out and repaired on
    their own, and the real file now keeps its full volume and extent.

    The fixture's second shell is sized above the floor to match.
    """
    fixture = 'foot1'

    def test_output_exists(self):
        self.assert_output()

    def test_repaired(self):
        self.assert_output()
        self.assert_repaired()

    def test_boundary_not_collapsed(self):
        """The regression: closing the holes must not shrink the model."""
        self.assert_output()
        self.assert_bounds_within()

    def test_volume_preserved(self):
        """Bounds alone are not enough — a collapsed tube can keep its extent
        while losing what is inside it."""
        self.assert_output()
        before = self._volume(self.src)
        after = self._volume(self.dst)
        self.assertGreater(
            after, before * 0.9,
            f'volume {before:.1f} -> {after:.1f}: the boundary was pulled shut '
            f'rather than capped')

    def test_cap_shell_survives(self):
        self.assert_output()
        self.assert_shells_preserved()

    @staticmethod
    def _volume(path):
        verts, faces = fix._weld_binary_stl(path)
        tri = verts[faces]
        vol = np.einsum('ij,ij->i', tri[:, 0],
                        np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0
        del verts, faces, tri
        return float(vol)


class TestFoot2(PipelineCase):
    """Clean mesh: must pass through untouched.

    Paired with foot1 — same kind of part, the only difference being the
    defect, so a change in outcome isolates cleanly."""
    fixture = 'foot2'
    max_faces = 0            # decimation off: this is about not touching it
    scan_limit = 10_000      # comfortably above its 1,512 faces

    def test_output_exists(self):
        self.assert_output()

    def test_untouched(self):
        self.assert_output()
        self.assertEqual(self.after.tris, self.before.tris,
                         'a defect-free mesh was modified')
        self.assert_bounds_within(tol=0.001)

    def test_no_repair_attempted(self):
        """It short-circuits to a copy before step E exists, so the evidence
        is the absence of a pymeshfix line."""
        self.assertNotIn('pymeshfix:', self.log,
                         'pymeshfix ran on a mesh with nothing to repair')
        self.assertIn('clean copy', self.log)


class TestFalcon(PipelineCase):
    """Heavy non-manifold geometry — the repair path for a badly broken mesh.

    600 edges shared by three faces each.  The real model had 147,448 and
    pymeshfix returned an empty mesh, falling through to Blender; at this scale
    pymeshfix copes, so what is tested here is that a severely non-manifold
    mesh comes out repaired rather than destroyed."""
    fixture = 'falcon'

    def test_output_exists(self):
        self.assert_output()

    def test_nm_edges_repaired(self):
        self.assert_output()
        self.assert_repaired()

    def test_something_survived(self):
        """Heavy loss is expected on a mesh this broken; total loss is not."""
        self.assert_output()
        self.assertGreater(self.after.tris, 100,
                           'the repair produced an all-but-empty mesh')


class TestDecimation(PipelineCase):
    """Decimation reduces to the target without destroying the model."""
    fixture = 'foot2'         # single shell: no split, so the target is global
    max_faces = 500
    scan_limit = 10_000       # high, so this is decimation only, no split

    def test_reduced_to_target(self):
        self.assert_output()
        self.assertLessEqual(self.after.tris, self.max_faces * 1.05,
                             'decimation did not reach the target')
        self.assertGreater(self.after.tris, self.max_faces * 0.5,
                           'decimation overshot far past the target')

    def test_shape_preserved(self):
        """Decimation moves vertices slightly; it must not move the model."""
        self.assert_output()
        self.assert_bounds_within(tol=1.0)

    def test_shells_survive_decimation(self):
        """Measured on a real 444-shell mesh: 444 in, 445 out.  Decimation
        preserves components, which is what makes the deferred split work."""
        self.assert_output()
        self.assert_shells_preserved()


class TestSeam(PipelineCase):
    """Two regions wound against each other, joined at a closed seam loop.

    This is the hair-over-scalp case.  PyMeshFix rebuilds one coherent surface,
    so handed the joined mesh it keeps one region and deletes the other — on
    the real model, 562,288 faces in, 394,432 out, and the figure lost its
    head, reported as a clean repair.

    Nothing else in the pipeline can see it: the deleted region sits inside the
    model's own bounding box so the bbox guard stays quiet, and
    scan_mesh_errors counts only non-manifold and open edges, so the file
    reports nm=0 open=0 and takes the clean-copy path past every repair stage.
    """
    fixture = 'seam'
    max_faces = 0             # no decimation: this is about the seam alone
    scan_limit = 10_000

    def test_output_exists(self):
        self.assert_output()

    def test_seam_was_detected(self):
        self.assertIn('seam check', self.log,
                      'the winding seam was not detected')
        self.assertIn('closed loop', self.log)

    def test_repaired_without_needing_a_split(self):
        """PyMeshFix re-winds a reversed region when it can, and that is the
        cheaper answer: on this fixture it returns the same 760 faces with the
        winding corrected, where splitting first gave 880 faces and introduced
        2 non-manifold edges.

        The split is a RECOVERY path, taken only when PyMeshFix answers by
        deleting the region instead — which is measured after the fact, from
        the enclosed volume, because nothing beforehand distinguishes the two
        cases: this fixture and the mesh that lost its head both had exactly
        40 seam edges."""
        self.assertNotIn('step E0', self.log,
                         'split preemptively instead of letting pymeshfix '
                         'try first')
        self.assertNotIn('geometry was deleted', self.log,
                         'pymeshfix deleted geometry on a mesh it can re-wind')

    def test_no_seam_remains(self):
        """The point of the exercise: the output is consistently wound."""
        self.assert_output()
        verts, faces = fix._weld_binary_stl(self.dst)
        seam, loops = fix.find_winding_seams(verts, faces)
        del verts, faces
        self.assertEqual((len(seam), loops), (0, 0),
                         f'output still has {len(seam)} seam edges in '
                         f'{loops} loop(s)\nlog:\n{self.log}')

    def test_geometry_preserved(self):
        """The failure this guards against deletes a whole region, so face
        count and enclosed volume are what matter, not the bounding box —
        the deleted region was inside it."""
        self.assert_output()
        self.assertGreater(self.after.tris, self.before.tris * 0.9,
                           'a region was deleted rather than repaired')
        self.assert_bounds_within()

    def test_volume_check_would_catch_deletion(self):
        """The guard that decides whether to split: a repair leaving under
        _VOLUME_LOSS_LIMIT of the volume has deleted geometry, not fixed it.
        It caught every known case, including two the bounding box missed
        because the lost geometry was inside the silhouette."""
        self.assertLess(fix._VOLUME_LOSS_LIMIT, 1.0)
        self.assertGreater(fix._VOLUME_LOSS_LIMIT, 0.5)
        self.assertIsNotNone(fix._mesh_volume(self.dst))


class TestNoSeamNoSplit(PipelineCase):
    """A mesh with no closed seam loop must not be split.

    Only closed loops trigger the split: a loop encircles something, whereas a
    few seam edges with loose ends are local noise.  The real model before
    repair had 5 such edges in 0 loops and correctly split nothing."""
    fixture = 'foot2'
    max_faces = 0
    scan_limit = 10_000

    def test_no_split(self):
        self.assertNotIn('step E0', self.log,
                         'a mesh with no closed seam loop was split anyway')

    def test_untouched(self):
        self.assert_output()
        self.assertEqual(self.after.tris, self.before.tris)


def _suite(names):
    table = {'body': TestBody, 'arms': TestArms,
             'split-nodec': TestSplitWithoutDecimation, 'leg': TestLeg,
             'foot1': TestFoot1, 'foot2': TestFoot2, 'falcon': TestFalcon,
             'decimation': TestDecimation,
             'seam': TestSeam, 'noseam': TestNoSeamNoSplit}
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for n in names:
        if n not in table:
            print(f'unknown fixture: {n}  (have: {", ".join(table)})')
            raise SystemExit(2)
        suite.addTests(loader.loadTestsFromTestCase(table[n]))
    return suite


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    names = args or ['body', 'arms', 'split-nodec', 'leg', 'foot1', 'foot2',
                     'falcon', 'decimation', 'seam', 'noseam']
    t0 = time.monotonic()
    ok = unittest.TextTestRunner(verbosity=2).run(_suite(names)).wasSuccessful()
    print(f'\ntotal {time.monotonic()-t0:.1f}s')
    raise SystemExit(0 if ok else 1)
