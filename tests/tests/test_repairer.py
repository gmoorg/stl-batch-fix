"""Tests for libs.repairer — the repair sequence, which is policy not geometry.

The tools this sequences are tested elsewhere: `welder` splits T-junctions,
`splitter` cuts and reassembles, `meshfix` wraps PyMeshFix. What is tested here
is **what runs, in what order, and on what** — so most of these assert about
`Result.steps` and about injected tools rather than about vertex arrays.

`tool=` is the seam that makes that possible. Step 4's repair is injectable, so
a test can substitute a recording stub and assert on the sequence without
PyMeshFix running at all: fast, deterministic, and it does not conflate "the
sequence is right" with "PyMeshFix behaved".

**The two measured behaviours that most need guarding are negative**, and both
have a test here:

  - `volume_kept` must compare magnitudes. A signed ratio reported the
    all-defects sphere at -95% when it went -4292.4 -> +4092.9 — the number
    said catastrophe about the best repair in the suite.
  - `lost_vertices` must use a tolerance. Exact tuple comparison reported 382
    vertices lost on a fixture where nothing was deleted, because PyMeshLab
    round trips coordinates through float64 and hands back float32.

Tests needing PyMeshLab or PyMeshFix are skipped when they are absent, so this
file runs on a machine that cannot repair.
"""

import math
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from libs import blender, meshfix, meshlab, pipeconfig, repairer, scanner
from libs.mesh_io import Geometry, Kind, Mesh
from libs.repairer import Result, Step, StepResult, repair

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

#: The four CLEAN filters as one combined `apply_filters()` call — a
#: test-local reference sequence, not production code (`repair()` runs
#: them as four separate `meshlab.step_clean_*` calls; there is no longer a
#: production constant naming them together). Order matches
#: `repairer.WHOLE_MESH_STEPS`.
CLEAN_FILTERS_COMBINED: tuple[tuple[str, dict], ...] = (
    ('meshing_remove_null_faces', {}),
    ('meshing_merge_close_vertices', {'threshold': 0.1}),
    ('meshing_remove_duplicate_faces', {}),
    ('meshing_remove_unreferenced_vertices', {}),
)

#: PyMeshLab + PyMeshFix only, deliberately not `repairer.is_available()` —
#: that now also depends on `pipeconfig.ENABLE_BLENDER_PART` and Blender's
#: own availability, which would silently widen this guard on a machine
#: without Blender and skip tests that never touch it.
HAVE_TOOLS = meshlab.is_available() and meshfix.is_available()
needs_tools = unittest.skipUnless(
    HAVE_TOOLS, "pymeshlab and pymeshfix are both needed")

needs_blender = unittest.skipUnless(
    blender.is_available(), "blender is needed")


def mesh(verts, faces, source='/in/body.stl', destination='/out/body.stl'):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(source, destination, Kind.BINARY_STL, len(geometry.faces),
                True, None, geometry)


def tetra():
    return mesh(TETRA_VERTS, TETRA_FACES)


def inverted_tetra():
    """The same solid with every face reversed — negative volume."""
    return mesh(TETRA_VERTS, [list(reversed(f)) for f in TETRA_FACES])


def two_tetrahedra(min_faces_ok=True):
    """Two components that share nothing.

    Both are 4 faces, below `MIN_SHELL_FACES`, so `by_shells` returns the mesh
    whole unless the caller lowers the floor — which these tests do, since the
    point is to exercise the split rather than the debris rule.
    """
    verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                           [10, 11, 10], [10, 10, 11]]
    faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]]
    return mesh(verts, faces)


class Recorder:
    """A stand-in for step 4's repair tool that records what it was given.

    Returns each part untouched, so any change in the final mesh came from the
    sequence rather than from the tool.
    """

    def __init__(self, transform=None):
        self.seen = []
        self.transform = transform

    def __call__(self, part):
        self.seen.append(part)
        if self.transform is not None:
            return True, self.transform(part), 'transformed'
        return True, part, 'stub'


class TestContract(unittest.TestCase):
    """What the module promises regardless of which libraries are installed."""

    def test_unloaded_raises(self):
        """A programming error at the call site, not a property of the data."""
        with self.assertRaises(ValueError):
            repair(Mesh('/a.stl', '/b.stl', Kind.BINARY_STL, 4, True))

    def test_result_is_immutable(self):
        result = Result(tetra(), True, None, (), 4, 4, 1.0, 1.0, 1, 0.0)
        with self.assertRaises(Exception):
            result.faces_out = 9

    def test_step_result_is_immutable(self):
        step = StepResult(Step.WELD, 4, 4, 'none', 0.0)
        with self.assertRaises(Exception):
            step.faces_in = 9

    def test_do_not_retry_names_are_documented(self):
        """Each entry must carry why, or it is a list nobody can act on."""
        for name, reason in repairer.DO_NOT_RETRY.items():
            self.assertTrue(reason.strip(), f"{name} has no reason recorded")

    def _clean_filter_calls(self):
        """The (name, params) PyMeshLab filter calls the actual CLEAN steps
        in `repairer.WHOLE_MESH_STEPS` make, in order — observed by mocking
        `meshlab.apply_filters` and running each CLEAN step for real, rather
        than reading a separately-maintained list that could drift from
        what `repair()` actually runs.
        """
        calls = []

        def recording_apply_filters(mesh, filters):
            calls.extend(filters)
            return mesh

        m = tetra()
        with mock.patch.object(meshlab, 'apply_filters',
                               recording_apply_filters):
            for step_enum, step_fn in repairer.WHOLE_MESH_STEPS:
                if step_enum is Step.WELD:
                    continue
                step_fn(m)
        return calls

    def test_remove_t_vertices_is_not_in_the_clean_set(self):
        """It reduced a 910-face mesh to zero faces reporting nm=0 open=0."""
        names = [name for name, _ in self._clean_filter_calls()]
        self.assertNotIn('meshing_remove_t_vertices', names)
        self.assertIn('meshing_remove_t_vertices', repairer.DO_NOT_RETRY)

    def test_clean_keeps_all_four_filters(self):
        """`merge_close_vertices` alone is worse than nothing: it collapsed
        vertices while leaving both face sets, giving 1,140 non-manifold edges
        at 200% volume. The other three are not optional."""
        self.assertEqual(self._clean_filter_calls(),
                         [('meshing_remove_null_faces', {}),
                          ('meshing_merge_close_vertices', {'threshold': 0.1}),
                          ('meshing_remove_duplicate_faces', {}),
                          ('meshing_remove_unreferenced_vertices', {})])


class TestVolumeKept(unittest.TestCase):
    """The reporting bug that called the best repair in the suite a disaster."""

    def result_with(self, volume_in, volume_out):
        return Result(tetra(), True, None, (), 4, 4,
                      volume_in, volume_out, 1, 0.0)

    def test_an_inverted_input_repaired_reads_as_kept(self):
        """-4292.4 -> +4092.9 is the all-defects sphere, correctly repaired.

        A signed ratio calls that -95%.
        """
        self.assertAlmostEqual(
            self.result_with(-4292.4, 4092.9).volume_kept, 0.9535, places=3)

    def test_a_mesh_left_inverted_also_reads_as_kept(self):
        """Magnitude only asks whether the geometry survived; the sign is a
        separate question and `volume_out` still carries it."""
        result = self.result_with(-100.0, -100.0)
        self.assertAlmostEqual(result.volume_kept, 1.0)
        self.assertLess(result.volume_out, 0)

    def test_real_loss_still_shows(self):
        self.assertAlmostEqual(self.result_with(100.0, 50.0).volume_kept, 0.5)

    def test_zero_input_volume_is_unmeasurable_not_a_pass(self):
        """A02: the old `1.0` handed out a success verdict for a non-answer.

        Zero input volume is exactly what oppositely wound shells produce when
        they cancel, so the one case most in need of scrutiny was the one
        awarded a perfect score.  A ratio with no denominator is not a
        measurement; `nan` says so, and `_decide` refuses it.
        """
        self.assertTrue(math.isnan(self.result_with(0.0, 0.0).volume_kept))
        self.assertTrue(math.isnan(self.result_with(0.0, 500.0).volume_kept))


class TestLostVertices(unittest.TestCase):
    """Exact comparison reported 382 losses on a mesh that lost nothing."""

    def test_float_noise_is_not_a_loss(self):
        """PyMeshLab's float64 round trip re-rounds coordinates a repair never
        touched. Measured: the furthest such move was 1.0e-05."""
        before = np.array(TETRA_VERTS, dtype=np.float32)
        after = before + 1e-6
        self.assertEqual(repairer._count_lost(before, after), 0)

    def test_a_deleted_vertex_is_a_loss(self):
        before = np.array(TETRA_VERTS, dtype=np.float32)
        self.assertEqual(repairer._count_lost(before, before[:3]), 1)

    def test_a_real_displacement_is_a_loss(self):
        """The fixtures that genuinely lose geometry move a vertex by ~8.0,
        the sphere's radius — five orders of magnitude above the noise."""
        before = np.array(TETRA_VERTS, dtype=np.float32)
        after = before.copy()
        after[0] = [50, 50, 50]
        self.assertEqual(repairer._count_lost(before, after), 1)

    def test_an_empty_output_loses_everything(self):
        before = np.array(TETRA_VERTS, dtype=np.float32)
        self.assertEqual(repairer._count_lost(before, np.empty((0, 3))), 4)

    def test_the_tolerance_is_adjustable(self):
        before = np.array(TETRA_VERTS, dtype=np.float32)
        after = before + 0.01
        self.assertEqual(repairer._count_lost(before, after, 1e-4), 4)
        self.assertEqual(repairer._count_lost(before, after, 1.0), 0)


@needs_tools
class TestSequence(unittest.TestCase):
    """Which steps run, in what order — with step 4 stubbed out."""

    def test_every_step_is_reported_in_order(self):
        tool = Recorder()
        result = repair(tetra(), min_shell_faces=0, tool=tool)
        self.assertTrue(result.ok, result.problem)
        order = [s.step for s in result.steps]
        self.assertEqual(order[:6],
                         [Step.WELD, Step.CLEAN_NULL_FACES,
                          Step.CLEAN_MERGE_CLOSE, Step.CLEAN_DUPLICATE_FACES,
                          Step.CLEAN_UNREFERENCED, Step.SPLIT])
        self.assertEqual(order[-1], Step.MERGE)

    def test_there_is_no_orientation_step_before_the_split(self):
        """Orientation moved into step 3, per part, and became unconditional.

        Before the split it erased the seam signal the splitters read — on
        `sphere_seam`, 40 seam edges in 1 closed loop became 0/0 — so
        `by_shells` and `by_seams` were reasoning about geometry the
        orientation filter had already flattened.
        """
        result = repair(tetra(), min_shell_faces=0, tool=Recorder())
        self.assertFalse(hasattr(Step, 'ORIENT'),
                         "Step.ORIENT still exists; orientation is per-part")
        split = [s for s in result.steps if s.step is Step.SPLIT][0]
        weld = [s for s in result.steps if s.step is Step.WELD][0]
        last_clean = [s for s in result.steps
                     if s.step is Step.CLEAN_UNREFERENCED][0]
        first_clean = [s for s in result.steps
                       if s.step is Step.CLEAN_NULL_FACES][0]
        self.assertEqual(first_clean.faces_in, weld.faces_out)
        self.assertEqual(split.faces_in, last_clean.faces_out)

    def test_pipeconfig_enable_weld_is_read_at_call_time(self):
        """`repairer` must read `pipeconfig.ENABLE_WELD` live, not import its
        value at module load — a name import would bind the default once and
        silently stop noticing a flag flipped before a later `repair()` call,
        which is the whole point of collecting these switches in one place.
        """
        with mock.patch.object(pipeconfig, 'ENABLE_WELD', False):
            result = repair(tetra(), min_shell_faces=0, tool=Recorder())
        self.assertTrue(result.ok, result.problem)
        weld = [s for s in result.steps if s.step is Step.WELD][0]
        self.assertEqual(weld.faces_in, weld.faces_out)
        self.assertIn('ENABLE_WELD=False', weld.detail)

    @needs_blender
    def test_an_inverted_mesh_comes_back_outward(self):
        """The default tool orients every part unconditionally, so a wholly
        inverted mesh is turned outward without any guard having to detect it.

        The stub tool does not orient, so this uses the real one — the
        default per-part sequence, which now includes Blender.
        """
        result = repair(inverted_tetra(), min_shell_faces=0)
        self.assertTrue(result.ok, result.problem)
        self.assertGreater(scanner.volume(result.mesh), 0)

    def test_the_tool_sees_one_call_per_part(self):
        tool = Recorder()
        result = repair(two_tetrahedra(), min_shell_faces=0, tool=tool)
        self.assertEqual(len(tool.seen), 2)
        self.assertEqual(result.parts, 2)
        self.assertEqual(
            len([s for s in result.steps if s.step is Step.PART]), 2)

    def test_each_part_reaches_the_tool_whole(self):
        """A part is renumbered into its own vertex block, so the tool can
        treat it as a complete model."""
        tool = Recorder()
        repair(two_tetrahedra(), min_shell_faces=0, tool=tool)
        for part in tool.seen:
            self.assertEqual(len(part.geometry.verts), 4)
            self.assertEqual(len(part.geometry.faces), 4)

    def test_the_merged_mesh_keeps_the_parents_destination(self):
        """Inheriting part 0's would write the whole model to
        `<base>.part.0.stl` and hang the parent's markers off a part's path."""
        source = two_tetrahedra()
        result = repair(source, min_shell_faces=0, tool=Recorder())
        self.assertEqual(result.mesh.destination, source.destination)

    def test_a_single_shell_mesh_is_not_split(self):
        result = repair(tetra(), min_shell_faces=0, tool=Recorder())
        self.assertEqual(result.parts, 1)

    def test_face_counts_chain_through_the_steps(self):
        """Each step's `faces_in` must be the previous one's `faces_out`, or
        the record does not describe one sequence."""
        result = repair(tetra(), min_shell_faces=0, tool=Recorder())
        chain = [s for s in result.steps
                 if s.step in (Step.WELD, Step.CLEAN_NULL_FACES,
                              Step.CLEAN_MERGE_CLOSE,
                              Step.CLEAN_DUPLICATE_FACES,
                              Step.CLEAN_UNREFERENCED, Step.SPLIT)]
        for earlier, later in zip(chain, chain[1:]):
            self.assertEqual(later.faces_in, earlier.faces_out)


@needs_tools
class TestFailure(unittest.TestCase):
    """A failure returns the input unchanged, never a partial result."""

    def test_a_raising_tool_is_reported_not_propagated(self):
        def explode(part):
            raise RuntimeError("the tool fell over")
        result = repair(tetra(), min_shell_faces=0, tool=explode)
        self.assertFalse(result.ok)
        self.assertIn("the tool fell over", result.problem)

    def test_a_failure_hands_back_a_usable_mesh(self):
        """The caller must be able to tell "not repaired" from "repaired
        badly" and write the marker rather than ship the file."""
        def explode(part):
            raise RuntimeError("boom")
        source = tetra()
        result = repair(source, min_shell_faces=0, tool=explode)
        self.assertIsNotNone(result.mesh.geometry)
        self.assertEqual(result.faces_in, result.faces_out)

    def test_the_steps_before_the_failure_are_kept(self):
        """Where it got to is the useful part of a failure report."""
        def explode(part):
            raise RuntimeError("boom")
        result = repair(tetra(), min_shell_faces=0, tool=explode)
        self.assertIn(Step.WELD, [s.step for s in result.steps])

    def test_a_tool_that_returns_a_failure_is_not_reported_as_success(self):
        """R02: the tool did not raise — it came back and said it failed.

        PyMeshFix reports a failed repair by returning an unsuccessful result,
        not by raising, so only catching exceptions let the pipeline call a
        failed repair `ok=True`.  The reason has to survive to the caller: a
        marker gets written instead of a broken model being shipped.
        """
        def fails(part):
            return False, part, "pymeshfix failed: it gave up"

        result = repair(tetra(), min_shell_faces=0, tool=fails)
        self.assertFalse(result.ok)
        self.assertIn("it gave up", result.problem)
        # Prefixed exactly once ("part 0: ..."), not doubled by both
        # `_run_step` and the caller separately prepending it.
        self.assertEqual(result.problem, "part 0: pymeshfix failed: it gave up")

    def test_a_failed_part_still_reports_where_it_got_to(self):
        """A failure is only actionable with the steps that preceded it."""
        def fails(part):
            return False, part, "pymeshfix failed: it gave up"

        result = repair(tetra(), min_shell_faces=0, tool=fails)
        self.assertIn(Step.WELD, [s.step for s in result.steps])
        self.assertIsNotNone(result.mesh.geometry)

    def test_a_scan_failure_while_recording_still_advances_the_mesh(self):
        """`_run_step` must hand back the step's own result even when
        recording it afterward raises — the assignment-order bug this
        guards: `mesh = outcome.mesh` only happens once `_run_step`
        *returns* a value, so if a step's result were discarded by a raised
        exception instead of carried on `_StepOutcome.mesh`, the caller
        would still be holding the PRE-step mesh when the outer handler
        builds the failed `Result` — a real behaviour change from before
        this helper existed, where `mesh = step_fn(mesh)[1]` was reassigned
        on its own line, before the (potentially-raising) recording call.

        A fake first whole-mesh step returns a mesh with a distinguishable
        face count; `scanner.scan` is made to raise only once that
        distinguishable mesh reaches it, isolating "did the assignment
        happen" from "did anything at all get scanned".
        """
        advanced = tetra()  # 4 faces — the "post-step" mesh
        original = mesh(TETRA_VERTS + [[9, 9, 9]],
                        TETRA_FACES + [[0, 1, 4]])  # 5 faces — "pre-step"

        def fake_first_step(m):
            return True, advanced, 'advanced'

        def poison_scan(m):
            if len(m.geometry.faces) == len(advanced.geometry.faces):
                raise RuntimeError("scan fell over")
            return scanner.scan(m)

        fake_whole_mesh_steps = ((Step.WELD, fake_first_step),)
        with mock.patch.object(repairer, 'WHOLE_MESH_STEPS',
                               fake_whole_mesh_steps), \
             mock.patch.object(repairer.scanner, 'scan', poison_scan):
            result = repair(original, min_shell_faces=0)

        self.assertFalse(result.ok)
        self.assertIn("scan fell over", result.problem)
        self.assertEqual(len(result.mesh.geometry.faces),
                         len(advanced.geometry.faces),
                         "the failed Result must carry the step's own "
                         "output, not the mesh from before it ran")

    def test_a_tool_returning_non_finite_geometry_fails_it_does_not_crash(self):
        """The final measurements ran outside the guarded sequence.

        `_count_lost` and the closing volume are taken after the try/except,
        so a tool that hands back a NaN vertex escaped as a raw cKDTree
        ValueError instead of a failed `Result` — past every judgement the
        pipeline makes.  Guarding the file entrances does not cover this:
        the geometry is produced mid-repair, not read.
        """
        def poison(part):
            verts = part.geometry.verts.copy()
            verts[0, 0] = float('nan')
            return True, part.with_geometry(
                Geometry(verts, part.geometry.faces)), 'poisoned'

        result = repair(tetra(), min_shell_faces=0, tool=poison)
        self.assertFalse(result.ok)

        # Not merely "something raised": the reason has to name the defect.
        # Moving the measurements inside the guard is enough to turn the crash
        # into a failure, but the message would then be scipy's "data must be
        # finite" — which does not say it was the repaired mesh, and would stop
        # detecting anything at all if `_count_lost` ever changed measure.
        self.assertIn('repaired mesh', result.problem)
        self.assertIn('NaN or infinite', result.problem)

    def test_a_failing_closing_measurement_is_a_result_not_an_exception(self):
        """The structural half of the fix, on geometry that is perfectly finite.

        The NaN tests above are satisfied by the explicit finiteness check, so
        they pass even with the closing `Result` built outside the guard.  This
        one pins the boundary itself: a measurement is something the caller
        asked for, and its failure is a failed repair, not an exception raised
        past every verdict the pipeline makes.
        """
        def explode(before, after, tolerance=None):
            raise RuntimeError("measurement fell over")

        with mock.patch.object(repairer, '_count_lost', explode):
            result = repair(tetra(), min_shell_faces=0,
                            tool=lambda part: (True, part, 'noop'))

        self.assertFalse(result.ok)
        self.assertIn("measurement fell over", result.problem)

    def test_a_failing_closing_volume_is_a_result_not_an_exception(self):
        """The same for the other closing measurement.

        The closing `Result` uses `scanner.component_volume` (not
        `scanner.volume`, which every per-step `record()` call also uses —
        mocking that one would fail mid-sequence, not at the close, and
        would misdescribe what this test targets). `component_volume` is
        also called once for `volume_in`, before any step runs — that call
        must still succeed, so only the second call (the closing one) fails.
        """
        real = repairer.scanner.component_volume
        seen = []

        def flaky(mesh):
            seen.append(mesh)
            if len(seen) > 1:            # the opening measurement still works
                raise RuntimeError("volume fell over")
            return real(mesh)

        with mock.patch.object(repairer.scanner, 'component_volume', flaky):
            result = repair(tetra(), min_shell_faces=0,
                            tool=lambda part: (True, part, 'noop'))

        self.assertFalse(result.ok)
        self.assertIn("volume fell over", result.problem)

    @needs_blender
    def test_the_default_tool_propagates_an_unsuccessful_pymeshfix(self):
        """R02, through the real default tool rather than an injected one.

        The tests above hand `repair` a failing tool and prove it acts on
        one. This one proves `_repair_part` still *produces* that failure:
        PyMeshFix reports failure by returning `ok=False`, and if that were
        ever spelled as a plain detail string again the defect would be back
        while those tests stayed green. `meshfix.repair` is mocked, but
        `meshlab.step_orient` and `blender.step_blender_repair` run for
        real first.
        """
        failed = SimpleNamespace(ok=False, problem="pymeshfix gave up",
                                 mesh=None, stderr_capture='')
        with mock.patch.object(repairer.meshfix, 'repair', return_value=failed):
            result = repair(tetra(), min_shell_faces=0)

        self.assertFalse(result.ok)
        self.assertIn("pymeshfix gave up", result.problem)
        self.assertNotIn(Step.MERGE, [s.step for s in result.steps])


@needs_tools
class TestWhyCleanIsAllFourFilters(unittest.TestCase):
    """The four filters are a set, and the reasons were prose until now.

    `test_clean_keeps_all_four_filters` guards the *list*. These guard the
    *reasons*, so someone trimming it to a "cheaper subset" sees what breaks
    rather than only that a name is missing.
    """

    def doubled(self):
        """Two coincident tetrahedra with **separate** vertices, 1e-5 apart.

        The offset is the point, and a first version of this fixture got it
        wrong: `TETRA_FACES + TETRA_FACES` reuses the same vertex indices, so
        it is already non-manifold before any filter runs and there is nothing
        for `merge_close_vertices` to merge. The real `doubles` case is two
        copies whose vertices are *nearly* coincident — which is why it reads
        completely clean and why merging is what exposes it.
        """
        offset = [[x + 1e-5, y, z] for x, y, z in TETRA_VERTS]
        faces = TETRA_FACES + [[a + 4, b + 4, c + 4] for a, b, c in TETRA_FACES]
        return mesh(TETRA_VERTS + offset, faces)

    def test_merge_close_vertices_alone_is_worse_than_nothing(self):
        """It collapses the vertices and leaves **both** face sets, so the
        surface branches everywhere it used to be doubled.

        Measured on `sphere_doubles`: a mesh reading `nm=0` becomes **1,140
        non-manifold edges** at 200% volume. This is the small version.
        """
        doubled = self.doubled()
        self.assertTrue(scanner.scan(doubled).is_clean,
                        "the fixture must read clean before any filter — "
                        "that is what makes the doubles case dangerous")
        alone = meshlab.apply_filters(
            doubled, (('meshing_merge_close_vertices',
                       {'threshold': 0.1}),))
        self.assertGreater(scanner.scan(alone).non_manifold, 0,
                           "merge alone should leave a branching surface")

    def test_the_full_set_repairs_what_merge_alone_breaks(self):
        """The same input through all four comes out as one tetrahedron."""
        result = meshlab.apply_filters(self.doubled(), CLEAN_FILTERS_COMBINED)
        self.assertEqual(len(result.geometry.faces), len(TETRA_FACES))
        self.assertTrue(scanner.scan(result).is_clean)

    def test_remove_unreferenced_vertices_has_a_job(self):
        """It was argued twice in discussion to protect against nothing.

        `merge_close_vertices` creates orphans — 10 of them on costume01 — and
        this removes exactly those. They are harmless in themselves (no edges,
        no faces, invisible to every check, and `mesh_io.write` drops them
        because it walks faces), but they do occur, so the filter is not dead
        weight.
        """
        merged = meshlab.apply_filters(
            self.doubled(), CLEAN_FILTERS_COMBINED[:3])       # all but the last
        orphans = (len(merged.geometry.verts)
                   - len(np.unique(merged.geometry.faces)))
        full = meshlab.apply_filters(self.doubled(), CLEAN_FILTERS_COMBINED)
        after = (len(full.geometry.verts)
                 - len(np.unique(full.geometry.faces)))
        self.assertEqual(after, 0, "the last filter should leave no orphans")
        # The fixture may or may not produce one at this scale; what must hold
        # is that the filter never leaves any behind.
        self.assertGreaterEqual(orphans, 0)


class TestZeroThicknessSheets(unittest.TestCase):
    """Two coincident faces of opposite winding — and why removing one is a
    repair rather than damage.

    This was reasoned about wrongly twice. `remove_duplicate_faces` takes
    costume01's open edges from 17 to 1,319, which looks like the filter
    tearing the model apart. It is not: of 1,170 duplicate faces there, 1,166
    have **opposite** winding. A closed surface cannot carry such a pair,
    because the flap encloses no volume — it reads as clean only because the
    two faces alibi each other's edges.
    """

    VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
    FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

    def test_a_sheet_reads_as_clean_which_is_the_whole_problem(self):
        """A tetrahedron missing one face, with a coincident sheet in the gap,
        scores `open=0` — the defect is invisible to the success condition."""
        holed = mesh(self.VERTS, self.FACES[1:])
        self.assertGreater(scanner.scan(holed).open_edges, 0)
        masked = mesh(self.VERTS, self.FACES[1:] + [[0, 2, 1], [0, 1, 2]])
        self.assertEqual(scanner.scan(masked).open_edges, 0,
                         "the sheet no longer masks the hole — fixture stale")

    @needs_tools
    def test_removing_a_flap_from_a_sound_mesh_restores_it(self):
        """A flap on intact surface shows as non-manifold, and removing it
        returns the mesh to its control exactly."""
        sound = mesh(self.VERTS, self.FACES)
        flapped = mesh(self.VERTS, self.FACES + [[0, 1, 2]])
        self.assertEqual(scanner.scan(flapped).non_manifold, 3)
        fixed = meshlab.apply_filters(
            flapped, (('meshing_remove_duplicate_faces', {}),))
        self.assertTrue(scanner.scan(fixed).is_clean)
        self.assertEqual(len(fixed.geometry.faces),
                         len(sound.geometry.faces))
        self.assertAlmostEqual(scanner.volume(fixed), scanner.volume(sound),
                               places=5)

    @needs_tools
    def test_removing_a_sheet_that_masked_a_hole_repairs_it(self):
        """The direction that matters: one face of the sheet becomes the
        missing surface and the redundant one goes, so the filter **repairs**
        rather than opening anything."""
        masked = mesh(self.VERTS, self.FACES[1:] + [[0, 2, 1], [0, 1, 2]])
        fixed = meshlab.apply_filters(
            masked, (('meshing_remove_duplicate_faces', {}),))
        self.assertTrue(scanner.scan(fixed).is_clean)
        self.assertEqual(len(fixed.geometry.faces), len(self.FACES))

    @needs_tools
    def test_both_faces_are_never_deleted(self):
        """Deleting both is rejected, not untried: a sheet may be large and
        both faces may be the only surface in that region, so removing both
        would cut a real hole rather than expose one."""
        masked = mesh(self.VERTS, self.FACES[1:] + [[0, 2, 1], [0, 1, 2]])
        fixed = meshlab.apply_filters(
            masked, (('meshing_remove_duplicate_faces', {}),))
        self.assertEqual(len(fixed.geometry.faces), 4,
                         "a face of the sheet survives as real surface")


@needs_tools
@needs_blender
class TestRealTools(unittest.TestCase):
    """The default path, with Blender and PyMeshFix actually running."""

    def test_a_clean_tetrahedron_survives(self):
        result = repair(tetra(), min_shell_faces=0)
        self.assertTrue(result.ok, result.problem)
        self.assertTrue(scanner.scan(result.mesh).is_clean)
        self.assertEqual(result.lost_vertices, 0)
        self.assertAlmostEqual(result.volume_kept, 1.0, places=3)

    def test_an_inverted_tetrahedron_comes_back_outward(self):
        result = repair(inverted_tetra(), min_shell_faces=0)
        self.assertTrue(result.ok, result.problem)
        self.assertGreater(result.volume_out, 0)
        self.assertAlmostEqual(result.volume_kept, 1.0, places=3)

    def test_duplicate_faces_are_removed_before_the_split(self):
        """Deduplication must see both copies: splitting first left the
        `doubles` fixture at 200% volume in 2 shells."""
        doubled = mesh(TETRA_VERTS, TETRA_FACES + TETRA_FACES)
        result = repair(doubled, min_shell_faces=0)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(result.mesh.geometry.faces), 4)


class TestBlenderBeforePymeshfix(unittest.TestCase):
    """`_repair_part`'s default per-part order: orient, then Blender, then
    PyMeshFix. Most tests here mock all three stages and run without any of
    the three real tools installed, proving composition rather than tool
    behaviour; three (`test_disabling_blender_alone_still_runs_pymeshfix`,
    `test_disabling_pymeshfix_alone_still_runs_blender`, and
    `test_disabling_both_runs_neither`) deliberately use one or more real
    step functions to exercise their own flag checks, and are marked with
    `@needs_tools` where that requires PyMeshLab/PyMeshFix to be installed.

    `_repair_part` loops over `repairer.PART_MESH_STEPS`, a tuple built at
    import time from `meshlab.step_orient`/`blender.step_blender_repair`/
    `meshfix.step_meshfix_repair`. Patching those names on their OWN
    modules does not reach the already-built tuple, so every test here that
    substitutes a stage patches `repairer.PART_MESH_STEPS` directly instead.
    """

    def setUp(self):
        self.calls = []

    def _record(self, name, ok=True, note=None):
        def step(mesh):
            self.calls.append(name)
            return ok, mesh, note or f"{name} ran"
        return step

    def test_production_sequence_is_orient_blender_pymeshfix(self):
        """Guards against a mocked test suite concealing a wrong default:
        the actual tuple `_repair_part` uses in production must be exactly
        these three functions, in this order."""
        self.assertEqual(repairer.PART_MESH_STEPS,
                         (meshlab.step_orient, blender.step_blender_repair,
                          meshfix.step_meshfix_repair))

    def test_blender_runs_before_pymeshfix(self):
        fake_steps = (self._record('orient'), self._record('blender'),
                     self._record('pymeshfix'))
        with mock.patch.object(repairer, 'PART_MESH_STEPS', fake_steps):
            ok, _, detail = repairer._repair_part(tetra())
        self.assertTrue(ok, detail)
        self.assertEqual(self.calls, ['orient', 'blender', 'pymeshfix'])

    def test_pymeshfix_receives_blenders_output_not_the_original_part(self):
        """The composition must forward geometry, not just call order."""
        original = tetra()
        blender_output = mesh([[9, 9, 9], [10, 9, 9], [9, 10, 9], [9, 9, 10]],
                              TETRA_FACES)
        seen_by_pymeshfix = []

        def fake_orient(m):
            return True, m, 'oriented'

        def fake_blender(m):
            return True, blender_output, 'blender ran'

        def fake_pymeshfix(m):
            seen_by_pymeshfix.append(m)
            return True, m, 'pymeshfix ran'

        fake_steps = (fake_orient, fake_blender, fake_pymeshfix)
        with mock.patch.object(repairer, 'PART_MESH_STEPS', fake_steps):
            repairer._repair_part(original)

        self.assertEqual(len(seen_by_pymeshfix), 1)
        self.assertIs(seen_by_pymeshfix[0], blender_output)
        self.assertFalse(
            np.array_equal(seen_by_pymeshfix[0].geometry.verts,
                           original.geometry.verts),
            "pymeshfix must see blender's changed geometry, not the input")

    def test_a_hard_blender_failure_stops_before_pymeshfix(self):
        fake_steps = (lambda m: (True, m, 'oriented'),
                     self._record('blender', ok=False),
                     self._record('pymeshfix'))
        with mock.patch.object(repairer, 'PART_MESH_STEPS', fake_steps):
            ok, _, detail = repairer._repair_part(tetra())
        self.assertFalse(ok)
        self.assertEqual(self.calls, ['blender'])
        self.assertNotIn('pymeshfix', self.calls)

    @needs_tools
    def test_disabling_blender_alone_still_runs_pymeshfix(self):
        """Uses the real `PART_MESH_STEPS` (orient, then real PyMeshFix
        since Blender's flag is off) rather than mocking it, so this needs
        real PyMeshLab/PyMeshFix — everything else in this class mocks the
        tuple entirely and needs neither.
        """
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', False):
            ok, _, detail = repairer._repair_part(tetra())
        self.assertTrue(ok, detail)
        self.assertIn('ENABLE_BLENDER_PART=False', detail)
        self.assertIn('pymeshfix', detail)

    def test_disabling_pymeshfix_alone_still_runs_blender(self):
        """The real `meshfix.step_meshfix_repair` (not a mock) checked
        against a real, patched flag — proving PyMeshFix's own flag check,
        not just that `_repair_part` forwards a stub's answer.
        """
        fake_steps = (lambda m: (True, m, 'oriented'),
                     self._record('blender'), meshfix.step_meshfix_repair)
        with mock.patch.object(pipeconfig, 'ENABLE_PART_TOOL', False), \
             mock.patch.object(repairer, 'PART_MESH_STEPS', fake_steps):
            ok, _, detail = repairer._repair_part(tetra())
        self.assertTrue(ok, detail)
        self.assertEqual(self.calls, ['blender'])
        self.assertIn('ENABLE_PART_TOOL=False', detail)

    def test_disabling_both_runs_neither(self):
        """The real `blender.step_blender_repair`/`meshfix.step_meshfix_repair`
        (not mocks) checked against real, patched flags — proving the flag
        checks themselves, not just that `_repair_part` forwards whatever a
        stub says.
        """
        fake_steps = (lambda m: (True, m, 'oriented'),
                     blender.step_blender_repair, meshfix.step_meshfix_repair)
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', False), \
             mock.patch.object(pipeconfig, 'ENABLE_PART_TOOL', False), \
             mock.patch.object(repairer, 'PART_MESH_STEPS', fake_steps):
            ok, part, detail = repairer._repair_part(tetra())
        self.assertTrue(ok, detail)
        self.assertIn('ENABLE_BLENDER_PART=False', detail)
        self.assertIn('ENABLE_PART_TOOL=False', detail)


class TestIsAvailable(unittest.TestCase):
    """`is_available` must follow which steps are actually enabled."""

    def test_blender_missing_but_disabled_is_still_available(self):
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', False), \
             mock.patch.object(blender, 'is_available', lambda: False), \
             mock.patch.object(meshfix, 'is_available', lambda: True), \
             mock.patch.object(meshlab, 'is_available', lambda: True):
            self.assertTrue(repairer.is_available())

    def test_blender_missing_and_enabled_is_unavailable(self):
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', True), \
             mock.patch.object(blender, 'is_available', lambda: False), \
             mock.patch.object(meshfix, 'is_available', lambda: True), \
             mock.patch.object(meshlab, 'is_available', lambda: True):
            self.assertFalse(repairer.is_available())

    def test_pymeshfix_missing_but_disabled_is_still_available(self):
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', False), \
             mock.patch.object(pipeconfig, 'ENABLE_PART_TOOL', False), \
             mock.patch.object(meshfix, 'is_available', lambda: False), \
             mock.patch.object(meshlab, 'is_available', lambda: True):
            self.assertTrue(repairer.is_available())

    def test_meshlab_missing_is_always_unavailable(self):
        """Meshlab runs CLEAN and orient unconditionally — no flag gates it."""
        with mock.patch.object(pipeconfig, 'ENABLE_BLENDER_PART', False), \
             mock.patch.object(pipeconfig, 'ENABLE_PART_TOOL', False), \
             mock.patch.object(meshlab, 'is_available', lambda: False):
            self.assertFalse(repairer.is_available())


if __name__ == '__main__':
    unittest.main(verbosity=2)
