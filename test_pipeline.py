#!/usr/bin/env python3
"""End-to-end pipeline tests: does a real mesh come out correct?

    .venv/bin/python test_pipeline.py            # full suite, ~8-12 min
    .venv/bin/python test_pipeline.py --quick    # the fast three, ~90s
    .venv/bin/python test_pipeline.py body arms  # named fixtures only

Every test runs the actual process_file() on an actual model from the
collection and compares the OUTPUT MESH against the INPUT MESH.  Nothing here
asserts that a helper returns a plausible number — the question each test
answers is "is the model still right?", which is the only question that
matters after a refactor.

The four properties checked, for every fixture:

    shells      components in == components out (geometry not culled)
    bounds      bounding box within tolerance (nothing sliced off)
    defects     an independent rescan says nm=0 open=0 (actually repaired)
    faces       near the decimation target, not far below it

Each fixture is a bug that actually happened.  A failure names which one came
back rather than only reporting a moved assertion.

Fixtures are read from the collection in place — they are 200MB of real models
and are not copied.  A missing file skips its test rather than failing, so the
suite still runs on a machine without the collection.
"""
import os
import sys
import shutil
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import stl_batch_fix as fix


FIXING = '/mnt/sda2/STL/Fixing'

# name -> (path, what this file is for)
FIXTURES = {
    'body': (
        f'{FIXING}/Mandy Pinup Figurine/Mandy Dinamuuu3D/'
        f'Complete model (Thingiverse version)/Mandy_Body_Dinamuuu3D.stl',
        '2,061,994 tris / 39 genuine shells, just over the 2M scan limit. '
        'Skipping the split sent it to pymeshfix intact, which kept the '
        'largest shell and deleted the other 38 — including the head.'),
    'arms': (
        f'{FIXING}/Transhuman_Girl/Arms.stl',
        '414,210 tris, multi-shell, UNDER the scan limit: the normal step B '
        'split-and-merge path, and the counterpart to body.'),
    'leg': (
        f'{FIXING}/Transhuman_Girl/Leg1.stl',
        '140,698 tris with stray artifacts far from the body. Repair removes '
        'them and the bbox moves 89mm — correctly. The anti-test: a guard '
        'that rejects all bbox change would wrongly fail this.'),
    'foot1': (
        f'{FIXING}/Zelda NSFW/Chair_foot1.stl',
        '681,169 tris, nm=0 open=47. Known unfixed damage: pymeshfix closes '
        'the open boundary by pulling the end caps shut. Must stay FLAGGED.'),
    'foot2': (
        f'{FIXING}/Zelda NSFW/Chair_foot2.stl',
        '675,738 tris, nm=0 open=0. Near-identical part to foot1 with no '
        'defects: must pass through untouched.'),
    'falcon': (
        f'{FIXING}/Hanna and Chewie/Millenium_Falcon.stl',
        '393,198 tris with nm=147,448 — 37% of edges non-manifold. pymeshfix '
        'returns an empty mesh and Blender must recover it: the only fixture '
        'that exercises the fallback chain.'),
}

QUICK = ('leg', 'foot2', 'arms')

# Bounding-box tolerance, mm.  Calibrated by inspection: 0.033mm was invisible,
# 1.077mm was destroyed end caps.  Matches fix._BBOX_TOLERANCE.
BBOX_TOL = 0.1


def shell_count(verts, faces):
    """Connected components, via union-find on the edge list."""
    parent = np.arange(len(verts), dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = np.unique(np.sort(np.vstack([faces[:, [0, 1]],
                                         faces[:, [1, 2]],
                                         faces[:, [2, 0]]]), axis=1), axis=0)
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    roots = np.array([find(i) for i in range(len(verts))])
    return len(np.unique(roots))


def significant_shells(verts, faces, labels=None):
    """Shells big enough that split_shells() would keep them.

    It drops anything under max(100, largest/1000) faces, so debris must not be
    counted when comparing before and after — whole-costume01 has 444 shells of
    which 443 are 3-100 vertex specks, and losing those is not damage."""
    parent = np.arange(len(verts), dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = np.unique(np.sort(np.vstack([faces[:, [0, 1]],
                                         faces[:, [1, 2]],
                                         faces[:, [2, 0]]]), axis=1), axis=0)
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    roots = np.array([find(i) for i in range(len(verts))])
    _, lbl = np.unique(roots, return_inverse=True)
    counts = np.bincount(lbl)
    floor = max(100, counts.max() // 1000)
    return int((counts >= floor).sum())


class Mesh:
    """The measurable properties of a mesh file."""

    def __init__(self, path):
        self.path = path
        self.tris, err = fix._read_stl_header(path)
        if err:
            raise AssertionError(f'unreadable STL {path}: {err}')
        verts, faces = fix._weld_binary_stl(path)
        self.lo = tuple(float(x) for x in verts.min(0))
        self.hi = tuple(float(x) for x in verts.max(0))
        self.shells = shell_count(verts, faces)
        self.big_shells = significant_shells(verts, faces)
        del verts, faces

    def __repr__(self):
        return (f'{self.tris:,} tris, {self.shells} shells '
                f'({self.big_shells} significant)')


class PipelineCase(unittest.TestCase):
    """Runs one fixture through the real pipeline into a scratch tree."""

    fixture = None          # set by subclasses
    max_faces = 900_000

    @classmethod
    def setUpClass(cls):
        if cls.fixture is None:
            raise unittest.SkipTest('base class')
        src_path, cls.why = FIXTURES[cls.fixture]
        if not os.path.exists(src_path):
            raise unittest.SkipTest(f'fixture not present: {src_path}')

        cls.root = tempfile.mkdtemp(prefix=f'pipeline-{cls.fixture}-')
        inp = os.path.join(cls.root, 'STL')
        os.makedirs(inp)
        # Symlinked, not copied: these are up to 98MB each.
        cls.src = os.path.join(inp, f'{cls.fixture}.stl')
        os.symlink(src_path, cls.src)

        cls._saved = {k: getattr(fix, k) for k in
                      ('INPUT_FOLDER', 'OUTPUT_SUFFIX', 'LOG_FILE',
                       'REVIEW_FILE', 'SUMMARY_FILE', 'MAX_FACES')}
        fix.INPUT_FOLDER = inp
        fix.OUTPUT_SUFFIX = ''
        fix.MAX_FACES = cls.max_faces
        fix.LOG_FILE = os.path.join(cls.root, 'log.tsv')
        fix.REVIEW_FILE = os.path.join(cls.root, 'review.tsv')
        fix.SUMMARY_FILE = os.path.join(cls.root, 'summary.tsv')

        cls.before = Mesh(src_path)
        t0 = time.monotonic()
        cls.result = fix.process_file(cls.src)
        cls.secs = time.monotonic() - t0

        cls.dst = fix.output_path(cls.src, '', inp)
        cls.after = Mesh(cls.dst) if os.path.exists(cls.dst) else None
        cls.log = (open(fix.LOG_FILE).read()
                   if os.path.exists(fix.LOG_FILE) else '')
        print(f'\n  [{cls.fixture}] {cls.before} -> '
              f'{cls.after or "NO OUTPUT"}  ({cls.secs:.0f}s)', flush=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, '_saved', None):
            for k, v in cls._saved.items():
                setattr(fix, k, v)
        if getattr(cls, 'root', None):
            shutil.rmtree(cls.root, ignore_errors=True)

    # ── assertions shared by every fixture ──────────────────────────────────

    def assert_produced_output(self):
        self.assertIsNotNone(
            self.after,
            f'no output produced.\nwhy this fixture exists: {self.why}\n'
            f'log:\n{self.log}')

    def assert_repaired(self):
        """Independent rescan, not the library's self-report."""
        nm, open_e, err = fix.scan_mesh_errors(self.dst)
        self.assertIsNone(err, f'output unscannable: {err}')
        self.assertEqual((nm, open_e), (0, 0),
                         f'output still defective: nm={nm} open={open_e}\n'
                         f'log:\n{self.log}')

    def assert_bounds_within(self, tol=BBOX_TOL):
        for i, axis in enumerate('xyz'):
            self.assertAlmostEqual(
                self.after.lo[i], self.before.lo[i], delta=tol,
                msg=f'{axis} min moved {self.after.lo[i]-self.before.lo[i]:+.3f}mm '
                    f'— geometry was cut off.\n{self.why}')
            self.assertAlmostEqual(
                self.after.hi[i], self.before.hi[i], delta=tol,
                msg=f'{axis} max moved {self.after.hi[i]-self.before.hi[i]:+.3f}mm '
                    f'— geometry was cut off.\n{self.why}')

    def assert_shells_preserved(self):
        """Significant shells only — debris under split_shells()' floor does
        not count, and pymeshfix removing it is not a loss."""
        self.assertEqual(
            self.after.big_shells, self.before.big_shells,
            f'shell count changed {self.before.big_shells} -> '
            f'{self.after.big_shells}: components were deleted.\n{self.why}\n'
            f'log:\n{self.log}')

    def assert_faces_near_target(self, floor=0.75):
        """Decimation lands AT the target; landing far below means geometry
        was deleted after it.  394,432 against a 900,000 target was the head
        bug's clearest signal, and face count alone would have missed it — but
        combined with the shell check it localises the failure."""
        expected = min(self.before.tris, self.max_faces)
        self.assertGreater(
            self.after.tris, expected * floor,
            f'{self.after.tris:,} faces out against a {expected:,} target '
            f'— {100*(1-self.after.tris/expected):.0f}% short.\n{self.why}')


# ── the fixtures ────────────────────────────────────────────────────────────

class TestBody(PipelineCase):
    """Over the 2M scan limit with 39 real shells — the head-deletion bug.

    Before the deferred split this produced 394,432 tris in a single shell:
    the head, a 121,537-face component, silently deleted and reported ok."""
    fixture = 'body'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_shells_survive(self):
        self.assert_produced_output()
        self.assert_shells_preserved()

    def test_head_still_there(self):
        """The specific regression: 39 shells in, 39 out — not 1."""
        self.assert_produced_output()
        self.assertGreater(self.after.big_shells, 30,
                           'collapsed to a single shell: the head is gone again')

    def test_no_geometry_cut_off(self):
        self.assert_produced_output()
        self.assert_bounds_within()

    def test_actually_repaired(self):
        self.assert_produced_output()
        self.assert_repaired()

    def test_split_was_deferred_not_skipped(self):
        self.assertIn('step B2', self.log,
                      'the oversized split was skipped rather than deferred')


class TestArms(PipelineCase):
    """Multi-shell UNDER the limit: the normal step B split-and-merge path.

    Paired with body — same repair, opposite sides of the 2M threshold, so a
    change to that limit breaks one of them."""
    fixture = 'arms'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_shells_survive(self):
        self.assert_produced_output()
        self.assert_shells_preserved()

    def test_no_geometry_cut_off(self):
        self.assert_produced_output()
        self.assert_bounds_within()

    def test_actually_repaired(self):
        self.assert_produced_output()
        self.assert_repaired()

    def test_took_the_normal_split_path(self):
        self.assertIn('step B: split multi-shell', self.log)
        self.assertNotIn('step B2', self.log,
                         'a file under the limit should not need the deferred split')


class TestLeg(PipelineCase):
    """The anti-test: a large bbox change that is CORRECT.

    Stray artifacts sit far from the body and stretch the bounding box by
    89mm.  Repair removes them, the box shrinks, and the model is fine — a
    guard that rejected every bbox change would wrongly fail this file."""
    fixture = 'leg'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_actually_repaired(self):
        self.assert_produced_output()
        self.assert_repaired()

    def test_body_geometry_kept(self):
        """The artifacts may go; the model itself may not.  Face count is the
        check here, since the bbox legitimately moves."""
        self.assert_produced_output()
        self.assert_faces_near_target(floor=0.90)


class TestFoot1(PipelineCase):
    """Known, still-unfixed damage: nm=0 open=47.

    pymeshfix closes the open boundary by pulling the end caps shut, losing
    ~1.1mm of real geometry.  Bambu asks for those holes to be filled, so
    skipping the repair is not obviously right and this is parked.

    The test asserts the CURRENT state and that the damage is still FLAGGED.
    If a future change fixes it, this test fails — which is the notification
    that it can be tightened, not a regression."""
    fixture = 'foot1'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_damage_is_still_flagged(self):
        self.assert_produced_output()
        drift = fix.compare_bounds((self.before.lo, self.before.hi),
                                   (self.after.lo, self.after.hi))
        self.assertIsNotNone(
            drift,
            'foot1 no longer loses geometry — if that is a real fix, tighten '
            'this test to assert_bounds_within() instead')
        self.assertIn('bbox changed', self.log,
                      'geometry was lost without the run flagging it')

    def test_original_kept_for_fallback(self):
        """A flagged repair saves the source beside it, so the full-size mesh
        is still printable if the repair turns out wrong."""
        self.assert_produced_output()
        original = os.path.splitext(self.dst)[0] + '.original.stl'
        self.assertTrue(os.path.exists(original),
                        'no .original.stl saved next to a flagged repair')
        self.assertEqual(os.path.getsize(original),
                         os.path.getsize(os.path.realpath(self.src)),
                         '.original.stl is not a full copy of the source')


class TestFoot2(PipelineCase):
    """Clean mesh, nm=0 open=0: must pass through untouched.

    Near-identical part to foot1, so the only difference between them is the
    defect — a change in outcome isolates cleanly."""
    fixture = 'foot2'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_unchanged(self):
        """A clean mesh under the face limit is copied, not reprocessed."""
        self.assert_produced_output()
        self.assertEqual(self.after.tris, self.before.tris,
                         'a defect-free mesh was modified')
        self.assert_bounds_within(tol=0.001)

    def test_no_repair_attempted(self):
        """A defect-free mesh under the face limit short-circuits to a clean
        copy — it never reaches step E at all, so the evidence is the absence
        of a pymeshfix line, not a 'skip E' one."""
        self.assertNotIn('pymeshfix:', self.log,
                         'pymeshfix ran on a mesh with nothing to repair')
        self.assertIn('clean copy', self.log,
                      'a defect-free mesh took a repair path instead of being copied')


class TestFalcon(PipelineCase):
    """nm=147,448 — 37% of edges non-manifold.

    pymeshfix returns an empty mesh here, and the run must fall through to
    Blender rather than writing nothing.  The only fixture that exercises the
    fallback chain."""
    fixture = 'falcon'

    def test_output_exists(self):
        self.assert_produced_output()

    def test_recovered_by_blender(self):
        self.assertIn('empty mesh', self.log,
                      'pymeshfix no longer fails here — fixture may be stale')
        self.assertIn('blender', self.log.lower(),
                      'pymeshfix failed and Blender was never tried')

    def test_actually_repaired(self):
        self.assert_produced_output()
        self.assert_repaired()

    def test_model_not_destroyed(self):
        """Heavy merging is expected on a degenerate mesh; losing the model is
        not.  Bounds are the check — deduplication does not move them."""
        self.assert_produced_output()
        self.assert_bounds_within(tol=0.5)


def _select(names):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    table = {'body': TestBody, 'arms': TestArms, 'leg': TestLeg,
             'foot1': TestFoot1, 'foot2': TestFoot2, 'falcon': TestFalcon}
    for n in names:
        if n not in table:
            print(f'unknown fixture: {n}   (have: {", ".join(table)})')
            raise SystemExit(2)
        suite.addTests(loader.loadTestsFromTestCase(table[n]))
    return suite


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if '--quick' in sys.argv:
        names = list(QUICK)
        print(f'quick mode: {", ".join(names)}')
    elif args:
        names = args
    else:
        names = list(FIXTURES)
    t0 = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(_select(names))
    print(f'\ntotal {time.monotonic()-t0:.0f}s')
    raise SystemExit(0 if result.wasSuccessful() else 1)
