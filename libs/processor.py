"""Decide what happens to one file: decimate, repair, judge, write.

    process(mesh, max_faces, ...) -> Outcome(indicator, mesh, ...)

**This is the module that judges.**  Everything below it reports and repairs
without an opinion — `repairer.ok` says the sequence ran, not that the mesh is
good, and `scanner` counts defects without deciding what they mean.  Somebody
has to turn those numbers into one of `indicators.Indicator`, and that is here.

    decimate -> repair -> scan -> decide -> write

**The output is written only when the result is provably clean**, and that is
the user's rule rather than an inference.  `scanner.scan().is_clean` is the
whole test: no non-manifold edges, no open edges.  A mesh that merely looks
finished has been wrong three times in this project's history — meshes have
passed non-manifold, open-edge, degenerate, seam *and* volume checks while
visibly damaged — so "the library said it worked" is never enough.

**Which mesh the marker carries depends on the defect**, and the distinction
matters more than it first appears:

    DESTROYED    the repair deleted geometry.  The SOURCE is the fallback,
                 because a half-model is worse than an unrepaired one, and the
                 marker is not `FAILED` because the failure is not transient —
                 retrying gives the same result.
    UNREPAIRED   non-manifold edges remain.  The REPAIRED mesh is the fallback:
    OPEN_EDGES   it is decimated, within the face budget, and every vertex that
                 went in came out.

Measured on a real file: decimating Mandy ourselves and repairing produced a
mesh at **46% of the input volume** — PyMeshFix found the surface
non-orientable and cut two thirds away.  Writing that as the fallback print
would be handing over a model with its body missing.  The same pipeline on a
differently-decimated copy of the same model finished at 99.99%.

**Decimation is never skipped**, and its failure is its own marker.  A file
that cannot be reduced is one the printer will reduce itself, reintroducing
the defects this tool exists to remove — so `UNDECIMATED` is a distinct
outcome from a repair failure, not a lesser one.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from . import decimator, indicators, mesh_io, repairer, scanner
from .indicators import Indicator
from .mesh_io import Mesh

#: How much of the input volume must survive for the repair to count as
#: non-destructive, as a fraction.
#:
#: **Magnitudes, not signed values** — an inverted input has a negative volume
#: and turning it outward is the repair, so a signed ratio calls a correct
#: result negative.
#:
#: Measured on real files.  A sound repair keeps essentially everything:
#: Mandy 99.99%, costume01 99.99%, the all-defects sphere 95.35% (its lost 5%
#: being the duplicate geometry it was built with).  A destroyed one is not
#: near the line: Mandy's own decimation came back at **46.27%** after
#: PyMeshFix cut a non-orientable surface apart.  The gap is wide, so 0.90 is
#: not a fine judgement — anything under it has lost a limb, not a detail.
#:
#: `doubles`-style inputs are the known exception: two coincident copies
#: legitimately halve, and such a file will be marked destroyed.  That is the
#: safe direction to be wrong in, and no real model in the collection has the
#: defect.
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

    return Outcome(Indicator.PROCESS, repaired.mesh, None,
                   f"clean: {repaired.faces_out} faces, "
                   f"{repaired.volume_kept * 100:.2f}% of volume",
                   scan=scan, decimation=decimated, repair=repaired)


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
    if mesh.geometry is None:
        raise ValueError(f"{mesh.path} has no geometry — load it first")

    decimated = decimator.decimate(mesh, max_faces)
    if decimated.rung is decimator.Rung.FAILED:
        # Its own outcome, not a lesser repair failure.  A file that cannot be
        # reduced will be reduced by the printer instead, which reintroduces
        # exactly the defects this tool removes.
        attempts = '; '.join(f"{rung.value}: {why}"
                             for rung, why in decimated.attempts)
        return Outcome(Indicator.UNDECIMATED, None, 'source',
                       f"every decimator failed ({attempts})",
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
        if outcome.mesh is None:                       # pragma: no cover
            raise ValueError("a clean outcome must carry a mesh")
        mesh_io.write(outcome.mesh.with_destination(output_file))
        return output_file

    suffix = _MARKER_SUFFIX.get(outcome.indicator)
    if suffix is None:                                 # pragma: no cover
        return None
    base, extension = os.path.splitext(output_file)
    marker = f"{base}{suffix}"

    parent = os.path.dirname(marker)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if outcome.marker == 'repaired' and outcome.repair is not None:
        mesh_io.write(outcome.repair.mesh.with_destination(marker))
    else:
        # The source, byte for byte.  Copied rather than re-written so a file
        # this pipeline could not parse still reaches the user unchanged.
        shutil.copy2(source_path, marker)
    return marker


#: Which marker file each outcome writes.  The names are `indicators`' own, so
#: a marker written here is found by the next run's scan.
_MARKER_SUFFIX: dict[Indicator, str] = {
    Indicator.FAILED: '.failed.stl',
    Indicator.DESTROYED: '.destroyed.stl',
    Indicator.UNREPAIRED: '.unrepaired.stl',
    Indicator.OPEN_EDGES: '.open.stl',
    Indicator.UNDECIMATED: '.undecimated.stl',
    Indicator.BROKEN: '.broken.stl',
    Indicator.TIMED_OUT: '.timeout.stl',
}
