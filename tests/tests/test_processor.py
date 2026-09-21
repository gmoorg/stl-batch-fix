"""Tests for libs.processor — the module that judges.

Everything below `processor` reports without an opinion. This turns those
reports into one `indicators.Indicator`, and that judgement is what is tested
here — mostly through `_decide`, which is pure, so no filesystem is involved.

**The order of the tests inside `_decide` is itself load-bearing.** A destroyed
mesh scores `nm=0 open=0` — a half model is a perfectly valid closed surface —
so asking "is it clean" before "is it still the model" returns the wrong answer
with total confidence. `test_destruction_is_caught_before_cleanliness` is the
guard, and it is the one that would have caught the real case: decimating Mandy
ourselves produced a repair at 46.27% of volume that every topological check
called clean.
"""

import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

from libs import decimator, indicators, mesh_io as mesh_io_module
from libs import processor, repairer, scanner
from libs.indicators import Indicator
from libs.mesh_io import Geometry, Kind, Mesh
from libs.processor import MIN_VOLUME_KEPT, Outcome, process, write

PROBES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'probes')

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def mesh(verts=TETRA_VERTS, faces=TETRA_FACES,
         path='/in/body.stl', destination='/out/body.stl'):
    geometry = Geometry(np.array(verts, dtype=np.float32),
                        np.array(faces, dtype=np.int64).reshape(-1, 3))
    return Mesh(path, destination, Kind.BINARY_STL, len(geometry.faces),
                True, None, geometry)


def decimation(rung=decimator.Rung.NOT_NEEDED, attempts=()):
    m = mesh()
    return decimator.Result(m, rung, 4, 4, attempts)


def repair_result(ok=True, problem=None, volume_in=1.0, volume_out=1.0,
                  result_mesh=None, faces_out=4):
    return repairer.Result(result_mesh or mesh(), ok, problem, (), 4,
                           faces_out, volume_in, volume_out, 1, 0.0)


class TestTheDecision(unittest.TestCase):
    """`_decide` alone — no decimation, no repair, no disk."""

    def decide(self, **kwargs):
        return processor._decide(mesh(), decimation(),
                                 repair_result(**kwargs))

    def test_a_clean_repair_is_written(self):
        outcome = self.decide()
        self.assertIs(outcome.indicator, Indicator.PROCESS)
        self.assertTrue(outcome.is_clean)
        self.assertIsNotNone(outcome.mesh)
        self.assertIsNone(outcome.marker)

    def test_a_failed_repair_falls_back_to_the_source(self):
        outcome = self.decide(ok=False, problem='pymeshfix exploded')
        self.assertIs(outcome.indicator, Indicator.FAILED)
        self.assertEqual(outcome.marker, 'source')
        self.assertIn('exploded', outcome.reason)

    def test_destruction_is_its_own_indicator_not_failed(self):
        """`FAILED` means *transient, delete to retry*. A destructive repair is
        not transient — the same input gives the same result — so retrying
        wastes the time and produces the same broken mesh."""
        outcome = self.decide(volume_in=100.0, volume_out=46.0)
        self.assertIs(outcome.indicator, Indicator.DESTROYED)
        self.assertIsNot(outcome.indicator, Indicator.FAILED)

    def test_a_destroyed_repair_falls_back_to_the_source(self):
        """A half model is worse than an unrepaired one."""
        outcome = self.decide(volume_in=100.0, volume_out=46.0)
        self.assertEqual(outcome.marker, 'source')
        self.assertIsNone(outcome.mesh)

    def test_destruction_is_caught_before_cleanliness(self):
        """**The load-bearing ordering.**

        A half model scores `nm=0 open=0` — it is a valid closed surface, just
        not the one that went in. The real case: decimating Mandy ourselves
        gave a repair at 46.27% of volume that every topological check called
        clean, because PyMeshFix cut a non-orientable surface apart and kept
        one piece.
        """
        flawless = mesh()                      # a clean tetrahedron
        self.assertTrue(scanner.scan(flawless).is_clean,
                        "the fixture must be topologically clean")
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=100.0, volume_out=46.0,
                          result_mesh=flawless))
        self.assertIs(outcome.indicator, Indicator.DESTROYED)

    def test_remaining_non_manifold_edges_keep_the_repaired_mesh(self):
        """The repair kept the geometry and did not finish, so its output is
        the better fallback: decimated, within budget, nothing lost."""
        holed = mesh(TETRA_VERTS, TETRA_FACES + [[0, 1, 2]])   # a flap
        self.assertGreater(scanner.scan(holed).non_manifold, 0)
        outcome = processor._decide(mesh(), decimation(),
                                    repair_result(result_mesh=holed))
        self.assertIs(outcome.indicator, Indicator.UNREPAIRED)
        self.assertEqual(outcome.marker, 'repaired')

    def test_remaining_open_edges_keep_the_repaired_mesh(self):
        opened = mesh(TETRA_VERTS, TETRA_FACES[1:])            # a hole
        scan = scanner.scan(opened)
        self.assertEqual(scan.non_manifold, 0)
        self.assertGreater(scan.open_edges, 0)
        outcome = processor._decide(mesh(), decimation(),
                                    repair_result(result_mesh=opened))
        self.assertIs(outcome.indicator, Indicator.OPEN_EDGES)
        self.assertEqual(outcome.marker, 'repaired')

    def test_non_manifold_is_reported_before_open_edges(self):
        """Both can be present; the more serious one names the marker."""
        both = mesh(TETRA_VERTS, TETRA_FACES[1:] + [[1, 2, 3]])
        scan = scanner.scan(both)
        self.assertGreater(scan.non_manifold, 0)
        self.assertGreater(scan.open_edges, 0)
        outcome = processor._decide(mesh(), decimation(),
                                    repair_result(result_mesh=both))
        self.assertIs(outcome.indicator, Indicator.UNREPAIRED)

    def test_volume_is_compared_by_magnitude(self):
        """An inverted input has a negative volume and turning it outward is
        the repair, so a signed ratio calls a correct result destroyed."""
        outcome = self.decide(volume_in=-100.0, volume_out=100.0)
        self.assertIs(outcome.indicator, Indicator.PROCESS)

    def test_the_threshold_is_where_the_constant_says(self):
        just_under = MIN_VOLUME_KEPT - 0.01
        just_over = MIN_VOLUME_KEPT + 0.01
        self.assertIs(self.decide(volume_in=1.0, volume_out=just_under
                                  ).indicator, Indicator.DESTROYED)
        self.assertIs(self.decide(volume_in=1.0, volume_out=just_over
                                  ).indicator, Indicator.PROCESS)

    def test_every_outcome_carries_its_evidence(self):
        """A log line saying "failed" without the numbers is not a report."""
        for kwargs in ({}, {'ok': False, 'problem': 'x'},
                       {'volume_in': 100.0, 'volume_out': 46.0}):
            with self.subTest(**kwargs):
                outcome = self.decide(**kwargs)
                self.assertTrue(outcome.reason.strip())
                self.assertIsNotNone(outcome.repair)
                self.assertIsNotNone(outcome.decimation)


class TestDecimationFailure(unittest.TestCase):

    def test_undecimated_is_its_own_outcome(self):
        """A file that cannot be reduced will be reduced by the printer
        instead, reintroducing the defects this tool removes. That is not a
        lesser repair failure."""
        failed = decimator.Result(
            mesh(), decimator.Rung.FAILED, 4, 4,
            ((decimator.Rung.FAST_SIMPLIFICATION, 'boom'),))
        original = decimator.decimate
        try:
            decimator.decimate = lambda m, n: failed
            outcome = process(mesh(), 2)
        finally:
            decimator.decimate = original
        self.assertIs(outcome.indicator, Indicator.UNDECIMATED)
        self.assertEqual(outcome.marker, 'source')
        self.assertIn('boom', outcome.reason)

    def test_repair_never_runs_when_decimation_failed(self):
        failed = decimator.Result(mesh(), decimator.Rung.FAILED, 4, 4, ())
        original = decimator.decimate
        try:
            decimator.decimate = lambda m, n: failed
            outcome = process(mesh(), 2)
        finally:
            decimator.decimate = original
        self.assertIsNone(outcome.repair)


class TestWriting(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='processor-')
        self.source = os.path.join(self.dir, 'in.stl')
        with open(self.source, 'wb') as f:                  # a 1-face STL
            f.write(b'\0' * 80 + struct.pack('<I', 1)
                    + b'\0' * 50)
        self.output = os.path.join(self.dir, 'out', 'body.stl')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_clean_outcome_writes_the_mesh(self):
        outcome = processor._decide(mesh(), decimation(), repair_result())
        written = write(outcome, self.source, self.output)
        self.assertEqual(written, self.output)
        self.assertTrue(os.path.exists(self.output))

    def test_a_marker_is_a_full_mesh_never_an_empty_file(self):
        """Markers are fallback prints. An empty file is not one."""
        holed = mesh(TETRA_VERTS, TETRA_FACES + [[0, 1, 2]])
        outcome = processor._decide(mesh(), decimation(),
                                    repair_result(result_mesh=holed))
        written = write(outcome, self.source, self.output)
        self.assertTrue(written.endswith('.unrepaired.stl'))
        self.assertGreater(os.path.getsize(written), 84)

    def test_a_destroyed_marker_is_the_source_byte_for_byte(self):
        """Copied rather than re-written, so a file this pipeline could not
        parse still reaches the user unchanged."""
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=100.0, volume_out=46.0))
        written = write(outcome, self.source, self.output)
        self.assertTrue(written.endswith('.destroyed.stl'))
        with open(self.source, 'rb') as a, open(written, 'rb') as b:
            self.assertEqual(a.read(), b.read())

    def test_no_output_file_is_written_for_a_marker(self):
        """Only a provably clean result reaches the output path."""
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=100.0, volume_out=46.0))
        write(outcome, self.source, self.output)
        self.assertFalse(os.path.exists(self.output))

    def test_an_unmeasurable_volume_is_never_a_clean_verdict(self):
        """A03, at the gate: `NaN < MIN_VOLUME_KEPT` is False, so the
        destruction check waves NaN through and every later test agrees.

        Non-finite geometry is refused at load, so a file cannot arrive here
        any more.  This guards the other direction: a repair step that returns
        unmeasurable geometry must not be reported as a clean model.
        """
        for bad in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(volume_out=bad):
                outcome = processor._decide(
                    mesh(), decimation(),
                    repair_result(volume_in=100.0, volume_out=bad))
                self.assertFalse(
                    outcome.is_clean,
                    f"volume_out={bad} was reported as a clean result")
                # "not clean" is not enough: an unmeasurable result must be
                # classified as a failure that falls back to the source, not
                # as some other marker that ships repaired geometry.
                self.assertIs(outcome.indicator, Indicator.FAILED)
                self.assertEqual(outcome.marker, 'source')
                self.assertIsNone(outcome.mesh)

    def test_a_repair_returning_non_finite_geometry_is_a_failed_outcome(self):
        """End to end: the crash must become a verdict, not an exception.

        A tool that returns a NaN vertex used to escape `process` entirely,
        through `_count_lost`, before any judgement was made.
        """
        def poison(part):
            verts = part.geometry.verts.copy()
            verts[0, 0] = float('nan')
            return part.with_geometry(
                Geometry(verts, part.geometry.faces)), 'poisoned'

        outcome = processor.process(mesh(), max_faces=0, tool=poison)
        self.assertIs(outcome.indicator, Indicator.FAILED)
        self.assertEqual(outcome.marker, 'source')
        self.assertFalse(outcome.is_clean)

    def test_cancelling_shells_cannot_report_success(self):
        """A02 end to end, on the committed fixture that demonstrated it.

        A 760-face sphere at +4094.863122 beside a 4-face tetrahedron at
        -4094.863180: the signed total was -0.00006, the small shell was
        dropped by the face floor, and dividing by that denominator reported
        7,049,393,791% of volume kept — passing the loss check by a margin no
        real repair could produce.  The component is still dropped; what
        changed is that the run can no longer call that a success.
        """
        probe = os.path.join(PROBES, 'opposite_volume_shells.stl')
        if not os.path.exists(probe):                  # pragma: no cover
            self.skipTest('probe fixture not generated')
        loaded = mesh_io_module.load(
            mesh_io_module.probe(probe, self.output))

        outcome = processor.process(loaded, max_faces=900_000)

        self.assertFalse(outcome.is_clean)
        self.assertIs(outcome.indicator, Indicator.DESTROYED)
        self.assertEqual(outcome.marker, 'source')
        self.assertAlmostEqual(outcome.repair.volume_kept, 0.5, places=2)

    def test_zero_area_geometry_is_not_a_printable_solid(self):
        """A07: closed edges do not make a solid.

        Four faces whose vertices all lie on one line have every edge owned
        twice, so `Scan.is_clean` is True, `nm=0` and `open=0` — and the thing
        encloses nothing.  `is_clean` answers a question about indices; whether
        there is a model here is a question about geometry, and something has
        to ask it before the result is called printable.
        """
        collinear = mesh([[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]])
        scan = scanner.scan(collinear)
        self.assertTrue(scan.is_clean, "fixture no longer reproduces A07")
        self.assertEqual(scanner.volume(collinear), 0.0)

        # Measurements that would otherwise pass every gate.
        outcome = processor._decide(
            collinear, decimation(),
            repair_result(result_mesh=collinear,
                          volume_in=100.0, volume_out=100.0))

        self.assertFalse(outcome.is_clean,
                         "a mesh enclosing no volume was called printable")
        # Which rejection matters: BROKEN is never retried, because running the
        # same repair again will not make a flat mesh into a solid, and the
        # source is what belongs in the marker since the repaired result is not
        # a model at all.
        self.assertIs(outcome.indicator, Indicator.BROKEN)
        self.assertEqual(outcome.marker, 'source')
        self.assertIsNone(outcome.mesh)

    def test_a_zero_scale_mesh_is_not_a_clean_verdict(self):
        """A02: both volumes are finite, and the ratio between them is not.

        Zero input volume is what oppositely wound shells produce when they
        cancel exactly, so the A03 finiteness gate waves it through — 0.0 is a
        perfectly good number — and the loss check then compares against NaN,
        which is False for `<` like every other comparison.  The ratio has to
        be tested, not just its terms.
        """
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=0.0, volume_out=0.0))
        self.assertFalse(outcome.is_clean)
        self.assertIs(outcome.indicator, Indicator.FAILED)
        self.assertEqual(outcome.marker, 'source')

    def test_an_unmeasurable_input_volume_is_not_a_clean_verdict(self):
        """The denominator has to be a number too."""
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=float('nan'), volume_out=100.0))
        self.assertFalse(outcome.is_clean)
        self.assertIs(outcome.indicator, Indicator.FAILED)
        self.assertEqual(outcome.marker, 'source')

    def test_an_interrupted_write_leaves_no_output_to_mistake_for_work(self):
        """A04: a half-written file at the final path is worse than none.

        The next run asks `indicators.check` what is already done, and that
        question is answered by the path existing.  A write that died partway
        through used to leave a file there, so the run that would have redone
        it skipped it instead and the truncated STL became the deliverable.
        """
        outcome = processor._decide(mesh(), decimation(), repair_result())

        def die_before_committing(src, dst, *args, **kwargs):
            # The bytes are written and about to be published under the final
            # name.  Dying here is the interruption that matters: everything
            # before it is staged work, and nothing after it can be partial.
            raise RuntimeError("interrupted mid-write")

        with mock.patch.object(mesh_io_module.os, 'replace',
                               die_before_committing):
            with self.assertRaises(RuntimeError):
                write(outcome, self.source, self.output)

        self.assertFalse(
            os.path.exists(self.output),
            "a partial write left a file the next run will call ALREADY_FIXED")
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.output))
             if n.endswith('.part')], [],
            "the staged file was left behind to accumulate")

    def test_an_interrupt_mid_write_leaves_the_previous_result_intact(self):
        """The stronger property: a rerun that dies cannot damage what exists.

        Interrupting *during* the writing, with the signal that actually
        arrives in practice, rather than at the publication step.  The old
        implementation wrote straight to the destination, so this would have
        truncated a good file from a previous run.
        """
        outcome = processor._decide(mesh(), decimation(), repair_result())
        write(outcome, self.source, self.output)
        with open(self.output, 'rb') as f:
            previous = f.read()
        self.assertGreater(len(previous), 84)

        real_open = open

        def die_while_writing(path, *args, **kwargs):
            if str(path).endswith('.part'):
                raise KeyboardInterrupt("Ctrl+C mid-write")
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', die_while_writing):
            with self.assertRaises(KeyboardInterrupt):
                write(outcome, self.source, self.output)

        with open(self.output, 'rb') as f:
            self.assertEqual(f.read(), previous,
                             "an interrupted rerun damaged the previous result")
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.output))
             if n.endswith('.part')], [])

    def test_an_interrupted_marker_copy_leaves_no_marker(self):
        """The same rule for the copied source marker, which `check` also reads."""
        outcome = processor._decide(
            mesh(), decimation(),
            repair_result(volume_in=100.0, volume_out=46.0))

        def die(src, dst, *args, **kwargs):
            with open(dst, 'wb') as f:      # a partial copy reaches the path
                f.write(b'half')
            raise RuntimeError("interrupted mid-copy")

        with mock.patch.object(processor.shutil, 'copy2', die):
            with self.assertRaises(RuntimeError):
                write(outcome, self.source, self.output)

        base, _ = os.path.splitext(self.output)
        self.assertFalse(os.path.exists(f"{base}.destroyed.stl"),
                         "a partial copy left a marker the next run trusts")
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(self.output))
             if n.endswith('.part')], [],
            "the staged copy was left behind")

    def test_every_marker_indicator_has_a_suffix(self):
        """A missing entry would silently write nothing."""
        for indicator in (Indicator.FAILED, Indicator.DESTROYED,
                      Indicator.UNREPAIRED, Indicator.OPEN_EDGES,
                      Indicator.UNDECIMATED, Indicator.BROKEN,
                      Indicator.TIMED_OUT):
            with self.subTest(indicator=indicator):
                self.assertIsNotNone(indicators.marker_suffix(indicator))

    def test_the_marker_names_match_what_indicators_reads(self):
        """Written here, found by the next run's scan — or the pipeline
        reprocesses every failed file forever."""
        for suffix, indicator in indicators._OUTPUT_MARKERS:
            with self.subTest(indicator=indicator):
                self.assertEqual(indicators.marker_suffix(indicator), suffix)


if __name__ == '__main__':
    unittest.main(verbosity=2)
