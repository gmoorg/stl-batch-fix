"""Decimate, repair, judge, and optionally write one file.

`process` returns an `Outcome` without I/O. It checks destructive volume loss
before topology: a partial model can have zero mesh defects. `write` commits a
clean mesh or a full-mesh marker. A marker uses the source for destructive or
failed work, and the repaired mesh when geometry survives but defects remain.
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass, field

from . import decimator, indicators, mesh_io, repairer, scanner
from .indicators import Indicator
from .mesh_io import Mesh

#: Compare volume magnitudes: repairing an inverted mesh can flip its sign.
#: 0.90 detects observed catastrophic loss; coincident duplicate shells can
#: legitimately fall below it. See archive/docs-before-compact-2026-09-19/refactor/implementation-evidence.md.
MIN_VOLUME_KEPT = 0.90


@dataclass(frozen=True)
class Outcome:
    """What happened to one file, and the evidence for it.

    indicator    what to record — the same vocabulary `indicators` reads back
    mesh         the mesh to write, or None when nothing should be written
    marker       which mesh belongs in the marker file: 'repaired', 'source',
                 or None when there is no marker
    scan         `scanner.Scan` of the final mesh, or None if it never got one
    decimation   the `decimator.Result`, or None if decimation was skipped
    repair       the `repairer.Result`, or None if repair never ran
    reason       one line saying why, for the log

    `indicator is Indicator.PROCESS` is the success case: nothing is wrong and
    `mesh` is the file to write.  Every other value names a marker.
    """

    indicator: Indicator
    mesh: Mesh | None
    marker: str | None
    reason: str
    scan: scanner.Scan | None = None
    decimation: decimator.Result | None = None
    repair: repairer.Result | None = None

    @property
    def is_clean(self) -> bool:
        """True when the output is provably sound and should be written."""
        return self.indicator is Indicator.PROCESS


def _decide(source: Mesh,
            decimated: decimator.Result,
            repaired: repairer.Result) -> Outcome:
    """Turn the measurements into one indicator.  No I/O.

    Separate from `process` so the judgement can be tested without a
    filesystem, and so the order of the tests is visible in one place.  The
    order matters: a destroyed mesh must be caught before its defect counts
    are consulted, because a half-model can be perfectly clean.
    """
    if not repaired.ok:
        return Outcome(Indicator.FAILED, None, 'source',
                       f"repair failed: {repaired.problem}",
                       decimation=decimated, repair=repaired)

    # An unmeasurable volume is not a passing measurement.  Every guard below
    # is a `<` comparison, and those are all False against NaN, so a NaN would
    # be waved through by each test in turn and arrive at PROCESS.  Stated as
    # its own check rather than folded into the next one, because "we could not
    # measure this" and "this lost too much" are different answers.
    if not (math.isfinite(repaired.volume_in)
            and math.isfinite(repaired.volume_out)
            # The ratio, not only its terms: zero input volume is finite and
            # divides into nothing, and that is exactly what oppositely wound
            # shells produce when they cancel (A02).
            and math.isfinite(repaired.volume_kept)):
        return Outcome(
            Indicator.FAILED, None, 'source',
            f"volume could not be measured: in={repaired.volume_in}, "
            f"out={repaired.volume_out}",
            decimation=decimated, repair=repaired)

    # Destruction first.  A half-model scores nm=0 and open=0 — it is a valid
    # closed surface, just not the one that went in — so asking "is it clean"
    # before "is it still the model" gets the answer backwards.
    if repaired.volume_kept < MIN_VOLUME_KEPT:
        return Outcome(
            Indicator.DESTROYED, None, 'source',
            f"repair destroyed geometry: {repaired.volume_kept * 100:.1f}% "
            f"of volume kept, {repaired.faces_in} faces in, "
            f"{repaired.faces_out} out",
            decimation=decimated, repair=repaired)

    scan = scanner.scan(repaired.mesh)
    if scan.non_manifold:
        return Outcome(
            Indicator.UNREPAIRED, None, 'repaired',
            f"{scan.non_manifold} non-manifold edge(s) remain",
            scan=scan, decimation=decimated, repair=repaired)
    if scan.open_edges:
        return Outcome(
            Indicator.OPEN_EDGES, None, 'repaired',
            f"{scan.open_edges} open edge(s) remain",
            scan=scan, decimation=decimated, repair=repaired)

    # Topology is satisfied; ask geometry whether there is a model here.  Every
    # check above reads indices or reported numbers, and neither establishes a
    # solid: four faces whose vertices lie on one line own every edge twice and
    # enclose nothing, scoring nm=0, open=0, is_clean=True (A07).  Measured from
    # `repaired.mesh` itself rather than trusting `volume_out`, because this is
    # the last point at which the thing being approved can still be inspected.
    enclosed = scanner.component_volume(repaired.mesh)
    if not (math.isfinite(enclosed) and enclosed > 0.0):
        return Outcome(
            Indicator.BROKEN, None, 'source',
            f"encloses no volume: {repaired.faces_out} faces measuring "
            f"{enclosed}, which is a surface rather than a solid",
            scan=scan, decimation=decimated, repair=repaired)

    return Outcome(Indicator.PROCESS, repaired.mesh, None,
                   f"clean: {repaired.faces_out} faces, "
                   f"{repaired.volume_kept * 100:.2f}% of volume",
                   scan=scan, decimation=decimated, repair=repaired)


def _decimation_failure_reason(result: decimator.Result) -> str:
    """Explain every failed decimation attempt for the undecimated marker."""
    attempts = '; '.join(f"{rung.value}: {why}"
                         for rung, why in result.attempts)
    return f"every decimator failed ({attempts})"


def process(mesh: Mesh, max_faces: int,
            tool=None) -> Outcome:
    """Decimate, repair and judge one loaded mesh.  Nothing is written.

    Returns the decision and the mesh it applies to; `write` puts it on disk.
    Splitting those apart keeps every judgement testable without a filesystem,
    and means a caller can inspect an `Outcome` before committing to it.

    `max_faces <= 0` disables decimation, matching `decimator.decimate`.

    Raises `ValueError` if the mesh is not loaded — a programming error at the
    call site.
    """
    mesh_io.require_geometry(mesh)
    decimated = decimator.decimate(mesh, max_faces)
    if decimated.rung is decimator.Rung.FAILED:
        # Its own outcome, not a lesser repair failure.  A file that cannot be
        # reduced will be reduced by the printer instead, which reintroduces
        # exactly the defects this tool removes.
        return Outcome(Indicator.UNDECIMATED, None, 'source',
                       _decimation_failure_reason(decimated),
                       decimation=decimated)

    repaired = (repairer.repair(decimated.mesh, tool=tool) if tool
                else repairer.repair(decimated.mesh))
    return _decide(mesh, decimated, repaired)


def write(outcome: Outcome, source_path: str, output_file: str) -> str | None:
    """Put the outcome on disk and return the path written, or None.

    A clean outcome writes the mesh to `output_file`.  Anything else writes a
    marker beside it — `<base>.<signal>.stl` — whose contents are **a full
    copy of a real mesh**, never an empty file, so it opens in any viewer and
    can be printed as a fallback.

    Which mesh depends on the outcome's `marker` field: the repaired result
    when it kept the geometry, the source when it did not.  A destroyed repair
    hands back the source untouched, because a model missing its body is worse
    than one that is merely unrepaired.
    """
    if outcome.is_clean:
        # `Outcome.mesh` is `Mesh | None` — this is the only path that reads
        # it, so the guard belongs here. `outcome.repair.mesh` below is a
        # different field, on `repairer.Result`, which is not Optional, so
        # it needs no equivalent check.
        if outcome.mesh is None:                       # pragma: no cover
            raise ValueError("a clean outcome must carry a mesh")
        mesh_io.write(outcome.mesh.with_destination(output_file))
        return output_file

    suffix = indicators.marker_suffix(outcome.indicator)
    if suffix is None:                                 # pragma: no cover
        return None
    base, extension = os.path.splitext(output_file)
    marker = f"{base}{suffix}"

    mesh_io.ensure_parent_dir(marker)

    if outcome.marker == 'repaired' and outcome.repair is not None:
        mesh_io.write(outcome.repair.mesh.with_destination(marker))
    else:
        # The source, byte for byte.  Copied rather than re-written so a file
        # this pipeline could not parse still reaches the user unchanged.
        #
        # Staged for the same reason `mesh_io.write` is: a marker is what tells
        # the next run this file was already dealt with, so a half-copied one
        # would retire the work while leaving a truncated fallback print.
        with mesh_io.staged_write(marker) as staged:
            shutil.copy2(source_path, staged)
    return marker

