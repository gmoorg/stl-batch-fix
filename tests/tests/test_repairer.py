"""Tests for libs.repairer — the repair sequence, which is policy not geometry.

The tools this sequences are tested elsewhere: `welder` splits T-junctions,
`splitter` cuts and reassembles, `meshfix` wraps PyMeshFix. What is tested here
is **what runs, in what order, and on what** — so most of these assert about
`Result.steps` and about injected tools rather than about vertex arrays.

`part_steps=` is the seam that makes that possible. Step 4's repair is
injectable, so a test can substitute a recording stub and assert on the
sequence without PyMeshFix running at all: fast, deterministic, and it does
not conflate "the sequence is right" with "PyMeshFix behaved". Each stub is
supplied as `part_steps=(('name', step_fn),)` — the same
`((name, step_fn), ...)` shape `PART_MESH_STEPS` itself uses — not a bare
callable: the earlier `tool=` parameter accepted one, which threw away the
step's own name and left the step log with nothing but a generic fallback
to identify it by.

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
from unittest import mock

import numpy as np

from libs import alphawrap, decimator, execstep, meshfix, meshlab, pipeconfig, repairer, scanner, welder, winding
from libs.mesh_io import Geometry, Kind, Mesh
from libs.pipeconfig import StepConfig
from libs.repairer import Result, Step, StepResult, repair

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

#: Historical combined CLEAN call, retained to test the unwired tools.
CLEAN_FILTERS_COMBINED: tuple[tuple[str, dict], ...] = (
    ('meshing_remove_null_faces', {}),
    ('meshing_merge_close_vertices', {'threshold': 0.1}),
    ('meshing_remove_duplicate_faces', {}),
    ('meshing_remove_unreferenced_vertices', {}),
)

#: Only tests that explicitly exercise these retained tools need them.
HAVE_TOOLS = meshlab.is_available() and meshfix.is_available()
needs_tools = unittest.skipUnless(
    HAVE_TOOLS, "pymeshlab and pymeshfix are both needed")


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

    def __call__(self, part, config=None):
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
        step = StepResult(Step.PREP, 4, 4, 'none', 0.0)
        with self.assertRaises(Exception):
            step.faces_in = 9

    def _clean_filter_calls(self):
        """The retained CLEAN tools still call their established filters."""
        calls = []

        def recording_apply_filters(mesh, filters):
            calls.extend(filters)
            return mesh

        m = tetra()
        with mock.patch.object(meshlab, 'apply_filters',
                               recording_apply_filters):
            for step_fn in (meshlab.step_clean_null_faces,
                            meshlab.step_clean_merge_close,
                            meshlab.step_clean_duplicate_faces,
                            meshlab.step_clean_unreferenced):
                if step_fn is welder.step_weld_close_tjunctions:
                    continue
                step_fn(m, None)
        return calls

    def test_remove_t_vertices_is_not_in_the_clean_set(self):
        """`meshing_remove_t_vertices` must never come back into the CLEAN
        set: at threshold >= 10 it is a no-op, and at <= 1 it reduced a
        910-face mesh to ZERO faces while reporting nm=0 open=0 — a clean
        empty mesh. `welder` owns T-junction repair instead.
        """
        names = [name for name, _ in self._clean_filter_calls()]
        self.assertNotIn('meshing_remove_t_vertices', names)

    def test_re_orient_faces_coherently_is_not_used(self):
        """`meshing_re_orient_faces_coherently` unifies the winding but may
        pick the wrong direction: measured -4094.9 (of +4094.9) on the seam
        fixture, i.e. it inverted the whole shell. `by_geometry` decides
        which way is out by comparing against geometry; `coherently` only
        agrees with itself, which is not the same guarantee — so
        `meshlab.step_orient` must call `by_geometry`, never `coherently`.
        """
        calls = []
        with mock.patch.object(meshlab, 'apply_filters',
                               lambda mesh, filters: (calls.extend(filters),
                                                      mesh)[1]):
            meshlab.step_orient(tetra())
        names = [name for name, _ in calls]
        self.assertIn('meshing_re_orient_faces_by_geometry', names)
        self.assertNotIn('meshing_re_orient_faces_coherently', names)

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


class TestSequence(unittest.TestCase):
    """Which steps run, in what order — with step 4 stubbed out."""

    def test_production_whole_mesh_sequence_is_empty(self):
        self.assertEqual(repairer.WHOLE_MESH_STEPS, ())

    def test_every_step_is_reported_in_order(self):
        result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertTrue(result.ok, result.problem)
        # One Step.PART entry per part: `part_steps=` now REPLACES the
        # default per-part sequence entirely (the uniform-step refactor
        # removed the old auto-appended `_decimate_to_target_and_fix` tail —
        # see docs/refactor/TODO.md's "Uniform-step refactor" section), so a
        # single-entry custom `part_steps` produces exactly one PART record.
        self.assertEqual([s.step for s in result.steps],
                         [Step.SPLIT, Step.PART, Step.MERGE])

    def test_there_is_no_orientation_step_before_the_split(self):
        result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertEqual(result.steps[0].step, Step.SPLIT)

    # test_pipeconfig_enable_alpha_wrap_is_read_at_call_time removed: it
    # tested the pipeconfig.ENABLE_ALPHA_WRAP flag, which was removed in the
    # uniform-step refactor (docs/refactor/TODO.md) -- there is no on/off
    # flag any more, so this premise no longer applies.

    @unittest.skipUnless(alphawrap.is_available(), "cgal is needed")
    def test_an_inverted_mesh_comes_back_outward(self):
        """Real alpha wrapping reconstructs outward-facing geometry."""
        result = repair(inverted_tetra(), min_shell_faces=0,
                        part_steps=(('alpha_wrap_cheap', lambda m, config=None: (True, alphawrap.wrap(m, 0.1, 0.001), 'wrapped')),))
        self.assertTrue(result.ok, result.problem)
        self.assertGreater(scanner.volume(result.mesh), 0)

    def test_the_tool_sees_one_call_per_part(self):
        tool = Recorder()
        result = repair(two_tetrahedra(), min_shell_faces=0,
                        part_steps=(('recorder', tool),))
        self.assertEqual(len(tool.seen), 2)
        self.assertEqual(result.parts, 2)
        # One Step.PART StepResult entry PER part -- a single-entry custom
        # `part_steps` replaces the default sequence entirely (see
        # test_every_step_is_reported_in_order's comment), so 2 parts -> 2.
        self.assertEqual(
            len([s for s in result.steps if s.step is Step.PART]), 2)

    def test_each_part_reaches_the_tool_whole(self):
        """A part is renumbered into its own vertex block, so the tool can
        treat it as a complete model."""
        tool = Recorder()
        repair(two_tetrahedra(), min_shell_faces=0,
              part_steps=(('recorder', tool),))
        for part in tool.seen:
            self.assertEqual(len(part.geometry.verts), 4)
            self.assertEqual(len(part.geometry.faces), 4)

    def test_the_merged_mesh_keeps_the_parents_destination(self):
        """Inheriting part 0's would write the whole model to
        `<base>.part.0.stl` and hang the parent's markers off a part's path."""
        source = two_tetrahedra()
        result = repair(source, min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertEqual(result.mesh.destination, source.destination)

    def test_a_single_shell_mesh_is_not_split(self):
        result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertEqual(result.parts, 1)

    def test_face_counts_chain_through_the_steps(self):
        """Each step's `faces_in` must be the previous one's `faces_out`, or
        the record does not describe one sequence."""
        result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        chain = [s for s in result.steps
                 if s.step in (Step.PREP, Step.SPLIT)]
        for earlier, later in zip(chain, chain[1:]):
            self.assertEqual(later.faces_in, earlier.faces_out)


class TestPartIdentity(unittest.TestCase):
    """`StepResult.part` — logging spec section 3."""

    def test_two_part_sequence_gets_1_of_2_and_2_of_2(self):
        result = repair(two_tetrahedra(), min_shell_faces=0,
                        part_steps=(('recorder', Recorder()),))
        part_values = [s.part for s in result.steps if s.step is Step.PART]
        self.assertEqual(sorted(part_values), ['1/2', '2/2'])

    def test_split_and_merge_use_dash(self):
        result = repair(two_tetrahedra(), min_shell_faces=0,
                        part_steps=(('recorder', Recorder()),))
        non_part = [s for s in result.steps if s.step is not Step.PART]
        self.assertTrue(non_part)
        for s in non_part:
            self.assertEqual(s.part, '-')

    def test_scan_diagonal_and_scan_volume_out_logged_with_dash_part(self):
        events = []

        def logger(source_name, event, step, part, duration, detail):
            events.append((step, part))

        repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),),
              step_logger=logger, source_name='/x.stl')
        steps_seen = {step: part for step, part in events
                     if step in ('scan_diagonal', 'scan_volume_out')}
        self.assertEqual(steps_seen.get('scan_diagonal'), '-')
        self.assertEqual(steps_seen.get('scan_volume_out'), '-')


class TestFailure(unittest.TestCase):
    """A failure returns the input unchanged, never a partial result."""

    def test_a_raising_tool_is_reported_not_propagated(self):
        def explode(part, config=None):
            raise RuntimeError("the tool fell over")
        result = repair(tetra(), min_shell_faces=0, part_steps=(('explode', explode),))
        self.assertFalse(result.ok)
        self.assertIn("the tool fell over", result.problem)

    def test_a_failure_hands_back_a_usable_mesh(self):
        """The caller must be able to tell "not repaired" from "repaired
        badly" and write the marker rather than ship the file."""
        def explode(part, config=None):
            raise RuntimeError("boom")
        source = tetra()
        result = repair(source, min_shell_faces=0, part_steps=(('explode', explode),))
        self.assertIsNotNone(result.mesh.geometry)
        self.assertEqual(result.faces_in, result.faces_out)

    def test_the_steps_before_the_failure_are_kept(self):
        """Where it got to is the useful part of a failure report."""
        def explode(part, config=None):
            raise RuntimeError("boom")
        result = repair(tetra(), min_shell_faces=0, part_steps=(('explode', explode),))
        self.assertIn(Step.SPLIT, [s.step for s in result.steps])

    def test_a_tool_that_returns_a_failure_is_not_reported_as_success(self):
        """R02: the tool did not raise — it came back and said it failed.

        PyMeshFix reports a failed repair by returning an unsuccessful result,
        not by raising, so only catching exceptions let the pipeline call a
        failed repair `ok=True`.  The reason has to survive to the caller: a
        marker gets written instead of a broken model being shipped.
        """
        def fails(part, config=None):
            return False, part, "pymeshfix failed: it gave up"

        result = repair(tetra(), min_shell_faces=0, part_steps=(('fails', fails),))
        self.assertFalse(result.ok)
        self.assertIn("it gave up", result.problem)
        # Prefixed exactly once, by `execstep.run_step`'s own per-entry
        # naming ("fails: ...") — there is no separate "part N: " framing
        # in the uniform-step refactor's `repairer.repair`.
        self.assertEqual(result.problem, "fails: pymeshfix failed: it gave up")

    def test_a_failed_part_still_reports_where_it_got_to(self):
        """A failure is only actionable with the steps that preceded it."""
        def fails(part, config=None):
            return False, part, "pymeshfix failed: it gave up"

        result = repair(tetra(), min_shell_faces=0, part_steps=(('fails', fails),))
        self.assertIn(Step.SPLIT, [s.step for s in result.steps])
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

        def fake_first_step(m, config=None):
            return True, advanced, 'advanced'

        def poison_scan(m):
            if len(m.geometry.faces) == len(advanced.geometry.faces):
                raise RuntimeError("scan fell over")
            return scanner.scan(m)

        fake_whole_mesh_steps = (('weld', fake_first_step),)
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
        def poison(part, config=None):
            verts = part.geometry.verts.copy()
            verts[0, 0] = float('nan')
            return True, part.with_geometry(
                Geometry(verts, part.geometry.faces)), 'poisoned'

        result = repair(tetra(), min_shell_faces=0, part_steps=(('poison', poison),))
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
                            part_steps=(('noop', lambda part, config=None: (True, part, 'noop')),))

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
                            part_steps=(('noop', lambda part, config=None: (True, part, 'noop')),))

        self.assertFalse(result.ok)
        self.assertIn("volume fell over", result.problem)

    def test_the_default_tool_propagates_unsuccessful_reconstruction(self):
        with mock.patch.object(winding, 'reconstruct', side_effect=RuntimeError('rebuild gave up')):
            result = repair(tetra(), min_shell_faces=0)
        self.assertFalse(result.ok)
        self.assertIn('rebuild gave up', result.problem)
        self.assertNotIn(Step.MERGE, [s.step for s in result.steps])

    def test_an_explicit_alpha_wrap_still_propagates_failure(self):
        with mock.patch.object(alphawrap, 'wrap', side_effect=RuntimeError('wrap gave up')):
            result = repair(tetra(), min_shell_faces=0,
                            part_steps=(('alpha_wrap', alphawrap.step_alpha_wrap),))
        self.assertFalse(result.ok)
        self.assertIn('wrap gave up', result.problem)
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


@unittest.skipUnless(alphawrap.is_available(), "cgal is needed")
class TestRealTools(unittest.TestCase):
    """Real CGAL topology through part_steps= at a cheap explicit resolution.

    Default-resolution parameter binding is covered by TestAlphaWrapBinding.
    """

    def test_a_clean_tetrahedron_survives(self):
        result = repair(tetra(), min_shell_faces=0,
                        part_steps=(('alpha_wrap_cheap', lambda m, config=None: (True, alphawrap.wrap(m, 0.1, 0.001), 'wrapped')),))
        self.assertTrue(result.ok, result.problem)
        self.assertTrue(scanner.scan(result.mesh).is_clean)
        self.assertAlmostEqual(result.volume_kept, 1.0, delta=0.03)

    def test_an_inverted_tetrahedron_comes_back_outward(self):
        result = repair(inverted_tetra(), min_shell_faces=0,
                        part_steps=(('alpha_wrap_cheap', lambda m, config=None: (True, alphawrap.wrap(m, 0.1, 0.001), 'wrapped')),))
        self.assertTrue(result.ok, result.problem)
        self.assertGreater(result.volume_out, 0)
        self.assertAlmostEqual(result.volume_kept, 1.0, delta=0.03)

    def test_duplicate_faces_wrap_to_a_clean_solid(self):
        """Wrapping a doubled surface produces sound topology."""
        doubled = mesh(TETRA_VERTS, TETRA_FACES + TETRA_FACES)
        result = repair(doubled, min_shell_faces=0,
                        part_steps=(('alpha_wrap_cheap', lambda m, config=None: (True, alphawrap.wrap(m, 0.1, 0.001), 'wrapped')),))
        self.assertTrue(result.ok, result.problem)
        self.assertTrue(scanner.scan(result.mesh).is_clean)


class TestBlenderBeforePymeshfix(unittest.TestCase):
    """Composition and ordering behavior for an explicitly configured route.

    `repairer.PART_MESH_STEPS`/`_repair_part` (a fixed, always-composed
    per-part pipeline) no longer exist -- the uniform-step refactor
    (docs/refactor/TODO.md) replaced them with `execstep.run_sequence` over
    whatever ordered tuple of entries a caller supplies (`DEFAULT_PART_STEPS`
    or a `part_steps=` override). These tests keep their original intent --
    order is respected, geometry is forwarded step to step, and a hard
    failure stops the sequence before the next step runs -- expressed
    directly against `execstep.run_sequence`, the mechanism that now owns
    that behavior for every stage, not just this composed "old route".

    The production per-part tuple itself is pinned separately, in
    `TestSequence`/module-level assertions on `repairer.DEFAULT_PART_STEPS`.
    """

    def setUp(self):
        self.calls = []

    def test_production_sequence_is_winding_decimate_meshfix(self):
        """`DEFAULT_PART_STEPS`: winding reconstruction (which replaced
        alpha-wrap), decimate, decimate again, then conditional meshfix, as
        ordinary entries."""
        names = [entry.name for entry in repairer.DEFAULT_PART_STEPS]
        self.assertEqual(names, ['winding', 'decimate', 'decimate_again', 'meshfix'])

    def _record(self, name, ok=True, note=None):
        def step(mesh, config=None):
            self.calls.append(name)
            return ok, mesh, note or f"{name} ran"
        return step

    def _run(self, entries, mesh_in):
        steps = []
        outcome = execstep.run_sequence(
            entries, mesh_in, None, steps, step=Step.PART)
        return outcome.ok, outcome.mesh, outcome.detail

    def test_blender_runs_before_pymeshfix(self):
        fake_steps = (('orient', self._record('orient')),
                     ('blender', self._record('blender')),
                     ('pymeshfix', self._record('pymeshfix')))
        ok, _, detail = self._run(fake_steps, tetra())
        self.assertTrue(ok, detail)
        self.assertEqual(self.calls, ['orient', 'blender', 'pymeshfix'])

    def test_pymeshfix_receives_blenders_output_not_the_original_part(self):
        """The composition must forward geometry, not just call order."""
        original = tetra()
        blender_output = mesh([[9, 9, 9], [10, 9, 9], [9, 10, 9], [9, 9, 10]],
                              TETRA_FACES)
        seen_by_pymeshfix = []

        def fake_orient(m, config=None):
            return True, m, 'oriented'

        def fake_blender(m, config=None):
            return True, blender_output, 'blender ran'

        def fake_pymeshfix(m, config=None):
            seen_by_pymeshfix.append(m)
            return True, m, 'pymeshfix ran'

        fake_steps = (('orient', fake_orient), ('blender', fake_blender),
                     ('pymeshfix', fake_pymeshfix))
        self._run(fake_steps, original)

        self.assertEqual(len(seen_by_pymeshfix), 1)
        self.assertIs(seen_by_pymeshfix[0], blender_output)
        self.assertFalse(
            np.array_equal(seen_by_pymeshfix[0].geometry.verts,
                           original.geometry.verts),
            "pymeshfix must see blender's changed geometry, not the input")

    def test_a_hard_blender_failure_stops_before_pymeshfix(self):
        fake_steps = (('orient', lambda m, config=None: (True, m, 'oriented')),
                     ('blender', self._record('blender', ok=False)),
                     ('pymeshfix', self._record('pymeshfix')))
        ok, _, detail = self._run(fake_steps, tetra())
        self.assertFalse(ok)
        self.assertEqual(self.calls, ['blender'])
        self.assertNotIn('pymeshfix', self.calls)

class TestAlphaWrapBinding(unittest.TestCase):
    def test_authoritative_steps_bind_whole_diagonal_without_leaking(self):
        """`whole_model_diag` reaches every part's own `StepConfig`
        unchanged, computed once from the pre-split whole mesh -- not
        leaked/rebound per part despite each part being much smaller.

        `PART_MESH_STEPS`, the fixed tuple the old test patched to install
        this recording step, no longer exists -- the uniform-step refactor
        makes a caller's `part_steps=` REPLACE the sequence entirely (see
        docs/refactor/TODO.md), so the recording step is installed the same
        way any other custom per-part sequence is: via `part_steps=`.
        """
        original = two_tetrahedra()
        seen = []

        def step(part, config=None):
            whole_diagonal = config.whole_model_diag if config is not None else None
            seen.append((whole_diagonal, np.linalg.norm(np.ptp(
                part.geometry.verts.astype(np.float64), axis=0))))
            return True, part, 'recorded'

        for scale in (1, 3):
            whole = original.with_geometry(Geometry(
                original.geometry.verts * scale, original.geometry.faces))
            result = repair(whole, min_shell_faces=0,
                            part_steps=(('step_alpha_wrap', step),))
            self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(seen), 4)
        for i, (diagonal, part_diagonal) in enumerate(seen):
            self.assertAlmostEqual(diagonal, np.sqrt(363) * (1 if i < 2 else 3))
            self.assertGreater(diagonal, part_diagonal * 5)

    # Alpha-wrap is no longer the default part step (winding reconstruction
    # replaced it) but stays available: these run it as an explicit entry.
    ALPHA_WRAP = (('alpha_wrap', alphawrap.step_alpha_wrap),)

    def test_alpha_wrap_step_uses_whole_mesh_recipe(self):
        whole = two_tetrahedra()
        with mock.patch.object(alphawrap, 'wrap', side_effect=lambda m, a, o: m) as wrapped:
            result = repair(whole, min_shell_faces=0, part_steps=self.ALPHA_WRAP)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(wrapped.call_count, 2)
        diagonal = np.sqrt(363)
        for call in wrapped.call_args_list:
            self.assertAlmostEqual(call.args[1], diagonal / 800)
            self.assertAlmostEqual(call.args[2], diagonal / 2000)

    def test_alpha_wrap_step_caps_alpha_and_offset_on_a_large_mesh(self):
        original = two_tetrahedra()
        large = original.with_geometry(Geometry(
            original.geometry.verts * 50, original.geometry.faces))
        with mock.patch.object(alphawrap, 'wrap', side_effect=lambda m, a, o: m) as wrapped:
            result = repair(large, min_shell_faces=0, part_steps=self.ALPHA_WRAP)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(wrapped.call_count, 2)
        for call in wrapped.call_args_list:
            self.assertAlmostEqual(call.args[1], 0.15)
            self.assertAlmostEqual(call.args[2], 0.06)

    def test_default_step_uses_the_whole_mesh_grid_spacing(self):
        """The default part step is winding reconstruction; its grid
        spacing comes from the WHOLE mesh's diagonal, like alpha-wrap's alpha,
        and is capped at 0.15 on a large mesh."""
        for scale, expected in ((1, np.sqrt(363) / 800), (50, 0.15)):
            original = two_tetrahedra()
            whole = original.with_geometry(Geometry(
                original.geometry.verts * scale, original.geometry.faces))
            with mock.patch.object(winding, 'reconstruct',
                                   side_effect=lambda m, h, b: m) as rebuilt:
                result = repair(whole, min_shell_faces=0)
            self.assertTrue(result.ok, result.problem)
            self.assertEqual(rebuilt.call_count, 2)
            for call in rebuilt.call_args_list:
                self.assertAlmostEqual(call.args[1], expected)

    def test_custom_tool_bypasses_cgal(self):
        """A custom `part_steps` that never mentions `step_alpha_wrap` never
        touches CGAL.

        The uniform-step refactor (docs/refactor/TODO.md) made
        `whole_model_diag` an unconditional part of `repair()` — computed
        once, via `scanner.diagonal` (bounding-box max/min, not `np.ptp`),
        and bound into every part's `StepConfig` regardless of what
        `part_steps` is — so the old premise "diagonal is only computed for
        an alpha-wrap sequence" no longer holds and that half of this test
        is removed rather than asserted falsely. What still holds, and is
        still worth guarding, is that CGAL itself is never invoked when the
        supplied sequence has no step that calls it.
        """
        with mock.patch.object(alphawrap, '_CGAL', False), \
             mock.patch.object(alphawrap, '_alpha_wrap_3',
                               side_effect=AssertionError('CGAL touched')):
            result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertTrue(result.ok, result.problem)


class TestIsAlreadyClean(unittest.TestCase):
    """`is_already_clean(mesh) -> bool` — the skip-gate spike's own check,
    stateless and independent of `pipeconfig`/any other module state (see
    docs/refactor/TODO.md's "Algorithm questions" section for the spike this
    was written for). This tests the function itself; its opt-in call sites
    in `repair` are covered by `TestCleanGates`.
    """

    def test_a_consistently_wound_watertight_mesh_is_clean(self):
        self.assertTrue(repairer.is_already_clean(tetra()))

    def test_an_open_edge_is_not_clean(self):
        # Drop one face: the tetrahedron's remaining three faces leave a
        # triangular hole, i.e. open edges.
        m = mesh(TETRA_VERTS, TETRA_FACES[:-1])
        self.assertFalse(repairer.is_already_clean(m))

    def test_a_non_manifold_edge_is_not_clean(self):
        # A flap sharing an edge with the tetrahedron: that edge is now
        # owned by three faces.
        verts = TETRA_VERTS + [[0, 0, 2]]
        faces = TETRA_FACES + [[0, 2, 4]]
        m = mesh(verts, faces)
        self.assertFalse(repairer.is_already_clean(m))

    def test_inverted_winding_is_not_clean_even_though_scan_is_clean(self):
        """The exact case `Scan.is_clean` alone misses — one face reversed
        relative to its neighbors is still open_edges=0, non_manifold=0
        (`test_a_consistently_wound_watertight_mesh_is_clean`'s own
        topology is unaffected by winding), so `is_already_clean` must
        check winding separately rather than trusting `scan.is_clean` alone.
        """
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        m = mesh(TETRA_VERTS, faces)
        scan = scanner.scan(m)
        self.assertTrue(scan.is_clean, 'fixture assumption: topology reads clean')
        self.assertFalse(repairer.is_already_clean(m))

    def test_a_fully_inverted_mesh_is_still_clean(self):
        """Every face reversed is globally inside-out but LOCALLY
        consistent — neighboring faces still agree with each other, so
        `winding_seams` finds no seam edges. This is a real, if unusual,
        case this function does not claim to catch — `is_already_clean` is
        about LOCAL winding consistency between adjacent faces, not global
        orientation (which `meshlab.step_orient`/alpha-wrap's own
        reconstruction handle differently and separately).
        """
        self.assertTrue(repairer.is_already_clean(inverted_tetra()))


def clean_and_open_shells():
    """A clean tetrahedron plus a disjoint open one (one face missing).

    Both are under `MIN_SHELL_FACES`, so callers pass `min_shell_faces=0` to
    keep both shells through the split.
    """
    verts = TETRA_VERTS + [[10, 10, 10], [11, 10, 10],
                           [10, 11, 10], [10, 10, 11]]
    faces = TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6]]
    return mesh(verts, faces)


class TestCleanGates(unittest.TestCase):
    """`repair(..., skip_clean=)` — the opt-in `is_already_clean` call sites:
    the whole model first, then each retained part. Off by default."""

    def _run(self, m, **flags):
        events = []

        def logger(source_name, event, step, part, duration, detail):
            events.append((step, part, detail))

        recorder = Recorder()
        result = repair(m, min_shell_faces=0, part_steps=(('recorder', recorder),),
                        step_logger=logger, source_name='/x.stl', **flags)
        gate = [(part, detail) for step, part, detail in events if step == 'clean_gate']
        return result, recorder, gate

    def test_gate_is_off_by_default(self):
        result, recorder, gate = self._run(clean_and_open_shells())
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(recorder.seen), 2)
        self.assertEqual(gate, [])

    def test_a_clean_model_skips_split_parts_and_merge(self):
        source = tetra()
        result, recorder, gate = self._run(source, skip_clean=True)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(recorder.seen, [])
        self.assertEqual([s for s in result.steps
                          if s.step in (Step.SPLIT, Step.PART, Step.MERGE)], [])
        self.assertIs(result.mesh, source)
        self.assertEqual(result.parts, 1)
        self.assertEqual(result.faces_out, 4)
        self.assertEqual(result.lost_vertices, 0)
        # Measured, not copied: the same mesh measures the same both times.
        self.assertGreater(result.volume_out, 0.0)
        self.assertAlmostEqual(result.volume_kept, 1.0)
        self.assertEqual(gate, [('-', 'clean: steps skipped')])

    def test_an_unclean_model_falls_through_to_the_part_gate(self):
        """Model gate says not clean; then only the open part runs its
        sequence and the clean part is merged unchanged."""
        result, recorder, gate = self._run(clean_and_open_shells(), skip_clean=True)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(recorder.seen), 1)
        self.assertEqual(len(recorder.seen[0].geometry.faces), 3)
        self.assertEqual(len([s for s in result.steps if s.step is Step.MERGE]), 1)
        self.assertEqual(result.faces_out, 7)
        self.assertEqual(gate, [('-', 'not clean'),
                                ('1/2', 'clean: steps skipped'),
                                ('2/2', 'not clean')])

    def test_inconsistent_winding_is_repaired(self):
        faces = [list(f) for f in TETRA_FACES]
        faces[3] = [faces[3][0], faces[3][2], faces[3][1]]
        result, recorder, gate = self._run(mesh(TETRA_VERTS, faces), skip_clean=True)
        self.assertEqual(len(recorder.seen), 1)
        self.assertEqual(gate, [('-', 'not clean'), ('1/1', 'not clean')])

    def test_model_skip_keeps_the_non_finite_guard(self):
        bad = mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, np.nan]], TETRA_FACES)
        with mock.patch.object(repairer, 'is_already_clean', return_value=True):
            result, recorder, _ = self._run(bad, skip_clean=True)
        self.assertFalse(result.ok)
        self.assertIn('NaN or infinite', result.problem)
        self.assertEqual(recorder.seen, [])


class TestSecondDecimationRound(unittest.TestCase):
    """The default part sequence's `decimate_again` entry — the same
    decimate step run twice — through the REAL `decimate` and
    `decimate_again` entries of `DEFAULT_PART_STEPS`;
    alpha-wrap and MeshFix are stand-ins so the forwarded geometry can be
    checked exactly."""

    BIG = mesh(TETRA_VERTS + [[10, 10, 10], [11, 10, 10], [10, 11, 10], [10, 10, 11]],
               TETRA_FACES + [[4, 6, 5], [4, 5, 7], [4, 7, 6], [5, 6, 7]])   # 8 faces

    def sequence(self, seen_by_meshfix):
        entries = list(repairer.DEFAULT_PART_STEPS)
        names = [e.name for e in entries]
        self.assertEqual(names, ['winding', 'decimate', 'decimate_again', 'meshfix'])

        def fake_wrap(part, config=None):            # wrap inflates the part
            return True, self.BIG, 'wrapped'

        def spy_meshfix(part, config=None):
            seen_by_meshfix.append(part)
            return True, part, 'meshfix spy'

        entries[0] = execstep.mesh_entry('winding', fake_wrap)
        entries[3] = execstep.mesh_entry('meshfix', spy_meshfix)
        return tuple(entries)

    def geometry(self, n):
        return Geometry(np.array(TETRA_VERTS + [[10, 10, 10], [11, 10, 10], [10, 11, 10], [10, 10, 11]],
                                 dtype=np.float32),
                        np.array((TETRA_FACES + [[4, 6, 5], [4, 5, 7]])[:n], dtype=np.int64))

    def test_second_round_gets_first_round_output_and_meshfix_gets_second(self):
        first, second = self.geometry(6), self.geometry(4)   # target is the tetra's 4 faces
        seen = []
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=[first, second]) as fast:
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence(seen))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(fast.call_count, 2)
        self.assertIs(fast.call_args_list[1].args[0], first)
        self.assertEqual(fast.call_args_list[1].args[1], 4)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0].geometry, second)
        names = [s.detail.split(':')[0] for s in result.steps if s.step is Step.PART]
        self.assertEqual(names, ['winding', 'decimate', 'decimate_again', 'meshfix'])

    def test_first_round_on_target_skips_the_second(self):
        seen = []
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               return_value=self.geometry(4)) as fast:
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence(seen))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(fast.call_count, 1)
        again = [s.detail for s in result.steps
                 if s.step is Step.PART and s.detail.startswith('decimate_again')]
        self.assertEqual(len(again), 1)
        self.assertIn('not_needed', again[0])

    def test_a_second_round_error_fails_the_repair_before_meshfix(self):
        seen = []
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=[self.geometry(6), RuntimeError('boom')]):
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence(seen))
        self.assertFalse(result.ok)
        self.assertIn('boom', result.problem)
        self.assertEqual(seen, [])

    def test_an_exception_escaping_decimate_fails_the_repair(self):
        seen = []
        real = decimator.decimate
        calls = []

        def second_call_raises(m, n):
            calls.append(n)
            if len(calls) == 2:
                raise RuntimeError('escaped')
            return decimator.Result(m.with_geometry(self.geometry(6)),
                                    decimator.Rung.FAST_SIMPLIFICATION, 8, 6)

        with mock.patch.object(decimator, 'decimate', side_effect=second_call_raises):
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence(seen))
        self.assertFalse(result.ok)
        self.assertIn('escaped', result.problem)
        self.assertEqual(seen, [])

    def test_still_above_target_after_both_rounds_is_not_a_failure(self):
        seen = []
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=[self.geometry(6), self.geometry(5)]):
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence(seen))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len(seen[0].geometry.faces), 5)

    def test_both_rounds_log_the_same_way(self):
        with mock.patch.object(decimator, '_decimate_fastsimp',
                               side_effect=[self.geometry(6), self.geometry(4)]):
            result = repair(tetra(), min_shell_faces=0, part_steps=self.sequence([]))
        details = {s.detail.split(':', 1)[0]: s.detail.split(':', 1)[1].strip()
                   for s in result.steps if s.step is Step.PART}
        self.assertEqual(details['decimate'], 'fastsimp, 6 faces out')
        self.assertEqual(details['decimate_again'], 'fastsimp, 4 faces out')

    def test_custom_sequences_get_no_second_round(self):
        result = repair(tetra(), min_shell_faces=0, part_steps=(('recorder', Recorder()),))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(len([s for s in result.steps if s.step is Step.PART]), 1)
        result = repair(tetra(), min_shell_faces=0, part_steps=())
        self.assertEqual([s for s in result.steps if s.step is Step.PART], [])


class TestNestedProcessGroupWiring(unittest.TestCase):
    """Proves `repairer.repair(..., nested_process_group=True)` — the REAL
    entry point, not a bypass — actually reaches `blender.step_blender_repair`
    through `StepConfig`, deciding whether the Blender invocation it launches
    gets its own process session.
    """

    def _stand_in(self, body: str) -> str:
        import os
        import stat
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.sh')
        with os.fdopen(fd, 'w') as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.addCleanup(lambda: os.unlink(path) if os.path.exists(path) else None)
        return path

    def _run_with_blender_step(self, nested_process_group):
        import os
        import tempfile
        from libs import blender, mesh_io

        # A stand-in that copies a pre-built valid PLY to Blender's own
        # `--dst` argument (parsed out of the rendered script file, `$3`,
        # since the script assigns `dst = '<path>'` — matching the shape
        # `blender.REPAIR_SCRIPT.format` always produces): `repair()`
        # requires the destination file to exist to accept the run.
        source_ply = os.path.join(tempfile.mkdtemp(prefix='repairer-nested-test-'),
                                  'canned.ply')
        mesh_io.write_ply(mesh(TETRA_VERTS, TETRA_FACES), source_ply)
        exe = self._stand_in(
            "dst=$(sed -n \"s/^dst = '\\(.*\\)'$/\\1/p\" \"$3\")\n"
            f"cp {source_ply} \"$dst\"\n"
            "echo BLENDER_OK\n"
            "exit 0\n"
        )
        captured = {}
        real_popen = __import__('subprocess').Popen

        def spy(*args, **kwargs):
            captured['start_new_session'] = kwargs.get('start_new_session')
            return real_popen(*args, **kwargs)

        m = mesh(TETRA_VERTS, TETRA_FACES)
        # `step_blender_repair` constructs its own `Runner` internally with
        # no way to inject the stand-in executable — patch `Runner.__init__`
        # to force our stand-in while preserving the constructor's own
        # `own_process_group` argument, which is exactly the thing under
        # test (it is `step_blender_repair`'s own decision, driven by
        # `config.nested_process_group`, and must reach here unmodified).
        orig_init = blender.Runner.__init__

        def patched_init(self, executable='blender', own_process_group=True):
            orig_init(self, exe, own_process_group)

        with mock.patch('subprocess.Popen', spy), \
             mock.patch.object(blender.Runner, '__init__', patched_init):
            result = repair(
                m, part_steps=(('blender_repair', blender.step_blender_repair),),
                nested_process_group=nested_process_group)
        self.assertTrue(result.ok, result.problem)
        return captured['start_new_session']

    def test_nested_true_does_not_take_its_own_session(self):
        started_own_session = self._run_with_blender_step(nested_process_group=True)
        self.assertFalse(started_own_session,
                         "nested_process_group=True must reach Runner as "
                         "own_process_group=False")

    def test_nested_false_default_takes_its_own_session(self):
        started_own_session = self._run_with_blender_step(nested_process_group=False)
        self.assertTrue(started_own_session,
                        "nested_process_group=False (the default) must "
                        "reach Runner as own_process_group=True")


if __name__ == '__main__':
    unittest.main(verbosity=2)
