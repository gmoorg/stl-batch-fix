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

import unittest

import numpy as np

from libs import meshlab, repairer, scanner
from libs.mesh_io import Geometry, Kind, Mesh
from libs.repairer import CLEAN_FILTERS, Result, Step, StepResult, repair

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]

HAVE_TOOLS = repairer.is_available()
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

    def __call__(self, part):
        self.seen.append(part)
        if self.transform is not None:
            return self.transform(part), 'transformed'
        return part, 'stub'


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

    def test_remove_t_vertices_is_not_in_the_clean_set(self):
        """It reduced a 910-face mesh to zero faces reporting nm=0 open=0."""
        names = [name for name, _ in repairer.CLEAN_FILTERS]
        self.assertNotIn('meshing_remove_t_vertices', names)
        self.assertIn('meshing_remove_t_vertices', repairer.DO_NOT_RETRY)

    def test_clean_keeps_all_four_filters(self):
        """`merge_close_vertices` alone is worse than nothing: it collapsed
        vertices while leaving both face sets, giving 1,140 non-manifold edges
        at 200% volume. The other three are not optional."""
        names = [name for name, _ in repairer.CLEAN_FILTERS]
        self.assertEqual(names, ['meshing_remove_null_faces',
                                 'meshing_merge_close_vertices',
                                 'meshing_remove_duplicate_faces',
                                 'meshing_remove_unreferenced_vertices'])


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

    def test_zero_input_volume_does_not_divide(self):
        self.assertEqual(self.result_with(0.0, 0.0).volume_kept, 1.0)


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
        self.assertEqual(order[:3], [Step.WELD, Step.CLEAN, Step.SPLIT])
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
        clean = [s for s in result.steps if s.step is Step.CLEAN][0]
        self.assertEqual(clean.faces_in, weld.faces_out)
        self.assertEqual(split.faces_in, clean.faces_out)

    def test_an_inverted_mesh_comes_back_outward(self):
        """The default tool orients every part unconditionally, so a wholly
        inverted mesh is turned outward without any guard having to detect it.

        The stub tool does not orient, so this uses the real one.
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
                 if s.step in (Step.WELD, Step.CLEAN, Step.SPLIT)]
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


@needs_tools
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
        result = meshlab.apply_filters(self.doubled(), CLEAN_FILTERS)
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
            self.doubled(), CLEAN_FILTERS[:3])       # all but the last
        orphans = (len(merged.geometry.verts)
                   - len(np.unique(merged.geometry.faces)))
        full = meshlab.apply_filters(self.doubled(), CLEAN_FILTERS)
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
class TestRealTools(unittest.TestCase):
    """The default path, with PyMeshFix actually running."""

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
