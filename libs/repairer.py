"""Apply the current mesh repair sequence without writing a file.

`repair` welds T-junctions, runs duplicate cleanup before splitting, splits
edge-connected shells, orients each part, repairs each with PyMeshFix by
default, then merges and reports measurements. `splitter.by_seams` is not in
this sequence. `processor` judges the result; `repairer.ok` only means the
sequence completed. `blender_part` provides the PLY-based Blender operation,
but defect-based routing between it and PyMeshFix is not implemented yet.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np
from scipy.spatial import cKDTree

from . import blender, mesh_io, meshfix, meshlab, scanner, splitter, welder
from .mesh_io import Mesh


class Step(Enum):
    """Which step a `StepResult` is reporting.

    Recorded rather than inferred from position, because steps 2 and 3 are
    conditional and step 4 runs once per part — so "the third entry" is not
    "the third step" on any interesting mesh.
    """

    WELD = 'weld'                    # 1. T-junctions
    CLEAN = 'clean'                  # 2. duplicates
    SPLIT = 'split'                  # 3a. into parts
    PART = 'part'                    # 3b. one part: orient, then repair
    MERGE = 'merge'                  # 3c. back into one mesh


# --------------------------------------------------------------------------
# Step switches.  Every step of the sequence can be turned off here to measure
# what it contributes, which is the only way to tell a step that repairs from
# one that damages: several of these are *recorded as harmful* on real models
# (docs/refactor/discovered-bugs.md) and the argument was settled by running
# the pipeline without them.
#
# All default True, so the shipping behaviour is exactly what it was.  A
# disabled step still appears in `Result.steps`, with `detail` saying it was
# skipped, so a log never silently omits a stage.
#
# These are module-level rather than parameters because they describe an
# experiment on the whole run, not a property of one mesh.  Set them before
# calling `repair`; do not toggle them per part.
# --------------------------------------------------------------------------

#: 7. Split faces at T-junctions.  Adds faces, moves and deletes nothing.
ENABLE_WELD = True

#: 8. Drop zero-area faces.
ENABLE_CLEAN_NULL_FACES = True

#: 9. Weld vertices within 0.1% of the bbox diagonal.  **Measured harmful**:
#: on Amidara base it merges one vertex and creates two non-manifold edges in
#: a mesh that had none, and removing it changes the final result by nothing.
ENABLE_CLEAN_MERGE_CLOSE = True

#: 10. Remove faces duplicated after the merge.
ENABLE_CLEAN_DUPLICATE_FACES = True

#: 11. Drop vertices no face references.
ENABLE_CLEAN_UNREFERENCED = True

#: 12. Separate edge-connected components so PyMeshFix cannot discard all but
#: the largest.  Disabling this sends a multi-shell mesh in whole.
ENABLE_SPLIT_SHELLS = True

#: 13. Separate regions whose winding contradicts itself.  Off because
#: `repairer` has never called `by_seams`; enabling it is an experiment, and
#: repairing the resulting open regions independently is measured as
#: destructive on real models.
ENABLE_SPLIT_SEAMS = False

#: 14. Orient each part outward.  **Measured harmful**: takes Amidara base from
#: 922 winding-seam edges to 7,659.
ENABLE_ORIENT = True

#: 15. Run the part repair tool (PyMeshFix by default).  Disabling this passes
#: every part through untouched, which is the control for measuring what the
#: tool costs.
ENABLE_PART_TOOL = True


def clean_filters() -> tuple[tuple[str, dict], ...]:
    """The CLEAN filters the switches above leave enabled, in order.

    Built per call rather than at import so a switch can be flipped between
    runs in one session, which is the whole point of having them.
    """
    chosen = (
        (ENABLE_CLEAN_NULL_FACES, ('meshing_remove_null_faces', {})),
        (ENABLE_CLEAN_MERGE_CLOSE,
         ('meshing_merge_close_vertices', {'threshold': 0.1})),
        (ENABLE_CLEAN_DUPLICATE_FACES, ('meshing_remove_duplicate_faces', {})),
        (ENABLE_CLEAN_UNREFERENCED,
         ('meshing_remove_unreferenced_vertices', {})),
    )
    return tuple(spec for enabled, spec in chosen if enabled)


#: Run the whole cleanup sequence before splitting: merging alone can leave
#: duplicate faces and apparent non-manifold edges. The threshold is relative
#: to the bounding-box diagonal. See archive/docs-before-compact-2026-09-19/refactor/implementation-evidence.md.
#: Kept as the full default order; `clean_filters()` is what `repair` runs.
CLEAN_FILTERS: tuple[tuple[str, dict], ...] = (
    ('meshing_remove_null_faces', {}),
    ('meshing_merge_close_vertices', {'threshold': 0.1}),
    ('meshing_remove_duplicate_faces', {}),
    ('meshing_remove_unreferenced_vertices', {}),
)

#: Allow float re-rounding when counting retained vertices. This absolute
#: threshold is a known scale risk; see docs/refactor/open-issues.md.
LOST_VERTEX_TOLERANCE = 1e-4

DO_NOT_RETRY = {
    'meshing_remove_t_vertices':
        "a no-op at threshold >= 10; at <= 1 it reduced a 910-face mesh to "
        "ZERO faces while reporting nm=0 open=0 — a clean empty mesh. Use "
        "welder, which is what it exists for.",
    'meshing_re_orient_faces_coherently':
        "unifies the winding but may pick the wrong direction: -4094.9 on the "
        "seam fixture. by_geometry decides which way is out; this only agrees "
        "with itself.",
}


@dataclass(frozen=True)
class PartFailed:
    """A part tool's way of saying "I could not repair this".

    A tool returns `(Mesh, str)` on success and `(Mesh, PartFailed)` on
    failure.  A distinct type rather than a reason string or a bare `None`,
    because the detail is recorded in the step log either way: a failure
    spelled as text reads exactly like a successful note, and that is precisely
    how a failed PyMeshFix call came to be reported as `ok=True` while the log
    said `pymeshfix failed`.  A tool that raises is still handled as before;
    this is for the tool that returns normally and reports bad news.
    """

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True)
class StepResult:
    """Measurements recorded after one repair step.

    `scan` and `volume` describe the resulting mesh; both are None for a split
    that produces several parts. Every executed step is recorded, including a
    no-op, so a caller can attribute changes instead of inferring from face count.
    """

    step: Step
    faces_in: int
    faces_out: int
    detail: str
    second_elapsed: float
    scan: scanner.Scan | None = None
    volume: float | None = None
    orphans: int = 0

    @property
    def changed(self) -> bool:
        return self.faces_in != self.faces_out


@dataclass(frozen=True)
class Result:
    """Mesh and measurements from the repair sequence.

    `ok` reports execution, not cleanliness. `mesh` may be intermediate on
    failure. `lost_vertices` reports input vertices with no nearby output vertex;
    it is diagnostic only, since legitimate removal of a fin can lose its apex.
    """

    mesh: Mesh
    ok: bool
    problem: str | None
    steps: tuple[StepResult, ...]
    faces_in: int
    faces_out: int
    volume_in: float
    volume_out: float
    parts: int
    second_elapsed: float
    lost_vertices: int = 0

    @property
    def volume_kept(self) -> float:
        """Return `volume_out / volume_in`, or `nan` when there is no scale.

        Both terms are `scanner.component_volume`, so an intentional winding
        flip still reads as kept while a deleted shell reads as lost.

        Zero input returns `nan` rather than 1.0.  The old default called an
        unmeasurable mesh perfectly preserved, and the meshes it applied to
        were exactly the ones needing scrutiny: oppositely wound shells
        cancelling to zero.  `nan` is not a score, and `processor._decide`
        refuses it instead of comparing it.

        This ratio detects gross loss but is not a verdict: removing coincident
        duplicate shells can legitimately reduce volume.
        """
        if self.volume_in == 0.0:
            return float('nan')
        return abs(self.volume_out) / abs(self.volume_in)


def is_available() -> bool:
    """Whether this machine can run the sequence.

    PyMeshFix does step 4 and PyMeshLab does steps 2 and 3; neither has an
    in-process substitute.  `welder` and `splitter` are ours and always there.
    """
    return meshfix.is_available() and meshlab.is_available()


def _count_lost(before: np.ndarray, after: np.ndarray,
                tolerance: float = LOST_VERTEX_TOLERANCE) -> int:
    """Input vertices with no output vertex within `tolerance`.

    Nearest-neighbour rather than set difference, because a repair that moves
    nothing still re-rounds everything — see `LOST_VERTEX_TOLERANCE`.

    A repair that *moves* a vertex slightly is counted as keeping it, which is
    the intended reading: the question this answers is "did something get
    deleted", and `volume_kept` is what answers "did something get distorted".
    """
    if len(after) == 0:
        return len(before)
    distance, _ = cKDTree(after.astype(np.float64)).query(
        before.astype(np.float64))
    return int((distance > tolerance).sum())


def _repair_part(part: Mesh) -> tuple[Mesh, str | PartFailed]:
    """Orient one split part, then repair it with PyMeshFix.

    Orientation runs unconditionally after splitting, without a signed-volume
    guard. A PyMeshFix failure returns the oriented part with a `PartFailed`,
    which stops the repair — PyMeshFix reports failure by returning, not by
    raising, so a plain reason string would be indistinguishable from a note.
    """
    notes = []

    # A signed-volume guard misses local inversions; orient each split part.
    if ENABLE_ORIENT:
        part = meshlab.apply_filters(
            part, (('meshing_re_orient_faces_by_geometry', {}),))
        notes.append('oriented')
    else:
        notes.append('orient skipped')

    if not ENABLE_PART_TOOL:
        return part, ', '.join(notes + ['part tool skipped'])

    result = meshfix.repair(part)
    if not result.ok:
        return part, PartFailed(f"pymeshfix failed: {result.problem}")
    notes.append(
        f"pymeshfix {len(part.geometry.faces)}f -> {result.mesh.triangles}f")
    note = ', '.join(notes)
    if 'WARNING-' in result.stderr_capture:
        # PyMeshFix reports cuts and removed triangles here rather than on
        # stdout, and they are the only warning that a repair was lossy.
        note += ' (with warnings)'
    return result.mesh, note


def blender_part(part: Mesh, timeout: float = 600) -> tuple[Mesh, str | PartFailed]:
    """Repair a split part in Blender using a PLY round trip.

    Pass this as `repair(tool=...)` to replace the default PyMeshFix part tool.
    PLY preserves the vertex table; the result is reattached to the original
    part's identity. Failure returns the input part with a reason.
    """
    with tempfile.TemporaryDirectory(prefix='repairer-blender-') as folder:
        source = os.path.join(folder, 'part.ply')
        target = os.path.join(folder, 'fixed.ply')
        mesh_io.write_ply(part, source)
        ok, result = blender.repair(source, target, timeout=timeout)
        if not ok:
            why = ('timed out' if result.is_timed_out
                   else f"exit {result.exit_code}")
            return part, PartFailed(f"blender failed: {why}")
        # `read_ply` rather than `load`: the vertex table survived the round
        # trip, so there is nothing to weld — which is the whole reason this
        # boundary is PLY.  It also carries `part`'s identity across, so the
        # repaired geometry comes back attached to the part rather than to a
        # temp file.
        repaired = mesh_io.read_ply(target, part)

    marker = next((line for line in result.stdout_capture.splitlines()
                   if line.startswith('BLENDER_')), 'BLENDER_OK')
    return (repaired,
            f"blender {len(part.geometry.faces)}f -> "
            f"{repaired.triangles}f ({marker.split(':')[0]})")


def repair(mesh: Mesh,
           min_shell_faces: int = splitter.MIN_SHELL_FACES,
           tool: Callable[[Mesh], tuple[Mesh, str | PartFailed]] = _repair_part,
           ) -> Result:
    """Run weld, clean, split, per-part repair, and merge on a loaded mesh.

    No file is written and `ok` is not a clean-mesh verdict. The current
    default part tool is PyMeshFix; `tool` can replace it, including with
    `blender_part`. This injection point is not the planned defect-based tool
    routing. A failure returns `ok=False` with the mesh reached at failure,
    which may be an intermediate after earlier steps.

    A tool reports a part it could not repair by returning `PartFailed` as its
    detail; that stops the run and yields `ok=False`. Raising works too and is
    reported the same way. Both matter because the real tools do both.
    """
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry — load it before repairing")
    if not meshlab.is_available():
        return _failed(mesh, "pymeshlab is not installed")

    started = time.monotonic()
    faces_in = len(mesh.geometry.faces)
    # Per-component magnitude, not the signed total: this pair is what
    # `volume_kept` divides, and a signed total lets two oppositely wound
    # shells cancel into a denominator near zero (A02).  The per-step `volume`
    # recorded below stays signed — it describes one result and is where an
    # inside-out mesh shows up.
    volume_in = scanner.component_volume(mesh)
    before = mesh.geometry.verts
    steps: list[StepResult] = []
    destination = mesh.destination

    def record(step: Step, was: int, now: int, detail: str, since: float,
               result: Mesh | None = None) -> None:
        """Record one step, scanning `result` when there is a mesh to scan.

        `result` is None for the split, which produces several meshes rather
        than one — each part is then scanned by its own `Step.PART` entry.
        """
        scan = volume = None
        orphans = 0
        if result is not None and result.geometry is not None:
            scan = scanner.scan(result)
            volume = scanner.volume(result)
            orphans = (len(result.geometry.verts)
                       - len(np.unique(result.geometry.faces)))
        steps.append(StepResult(step, was, now, detail,
                                time.monotonic() - since,
                                scan, volume, int(orphans)))

    try:
        # 1. T-junctions.  Ours, because no other tool does this without
        #    denting the surface.  Adds faces, moves nothing.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        if ENABLE_WELD:
            welded = welder.repair(mesh)
            mesh = welded.mesh
            detail = (f"{welded.splits} junction(s) in "
                      f"{welded.rounds} round(s)")
        else:
            detail = 'skipped (ENABLE_WELD=False)'
        record(Step.WELD, was, len(mesh.geometry.faces), detail, mark, mesh)

        # 2. Duplicates, before the split so deduplication sees both copies.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        filters = clean_filters()
        if filters:
            mesh = meshlab.apply_filters(mesh, filters)
        names = ', '.join(name.replace('meshing_', '') for name, _ in filters)
        record(Step.CLEAN, was, len(mesh.geometry.faces),
               f"{len(filters)} of {len(CLEAN_FILTERS)} filters"
               f"{': ' + names if filters else ' (all skipped)'}", mark, mesh)

        # 3. Split, repair each part, merge.  PyMeshFix rebuilds one manifold
        #    surface and discards the rest, so a multi-shell mesh reaching it
        #    whole comes back as its largest shell alone.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        if ENABLE_SPLIT_SHELLS:
            parts = splitter.by_shells(mesh, min_faces=min_shell_faces)
            how = f"{len(parts)} shell part(s)"
        else:
            parts = (mesh,)
            how = 'shell split skipped (ENABLE_SPLIT_SHELLS=False)'
        if ENABLE_SPLIT_SEAMS:
            # Every region, including debris: `by_seams` does not filter, and
            # judging a region is the caller's job.
            parts = tuple(r for part in parts for r in splitter.by_seams(part))
            how += f" -> {len(parts)} region(s) after seams"
        kept = sum(len(p.geometry.faces) for p in parts)
        record(Step.SPLIT, was, kept,
               f"{how}, {kept} of {was} faces kept", mark)

        repaired = []
        for index, part in enumerate(parts):
            mark = time.monotonic()
            part_was = len(part.geometry.faces)
            fixed, detail = tool(part)
            repaired.append(fixed)
            record(Step.PART, part_was, len(fixed.geometry.faces),
                   f"part {index}: {detail}", mark, fixed)
            if isinstance(detail, PartFailed):
                # Stop at the first failed part rather than merging it back in.
                # A part that could not be repaired is still defective geometry,
                # and merging it would produce a mesh that measures plausibly
                # and is not a repair.  The steps already record which part and
                # why, so the reason travels with the failed result.
                return _failed(mesh, f"part {index}: {detail}", tuple(steps),
                               faces_in, volume_in,
                               time.monotonic() - started)

        mark = time.monotonic()
        was = sum(len(p.geometry.faces) for p in repaired)
        mesh = splitter.merge(repaired, destination=destination)
        record(Step.MERGE, was, len(mesh.geometry.faces),
               f"{len(repaired)} part(s) merged", mark, mesh)

        # A tool can hand back geometry that is not measurable — PyMeshFix,
        # PyMeshLab and Blender all return arrays this module did not build.
        # Checked before the closing measurements rather than after, because
        # `_count_lost` feeds those arrays to cKDTree, which raises on
        # non-finite input; that raise used to happen below this block and so
        # escaped `repair` entirely, past every verdict the pipeline makes.
        if not np.isfinite(mesh.geometry.verts).all():
            raise ValueError(
                "the repaired mesh has NaN or infinite coordinates")

        # Inside the guard: these are measurements of tool output, and a
        # measurement that fails is a failed repair, not an exception for the
        # caller to discover.
        result = Result(mesh, True, None, tuple(steps), faces_in,
                        len(mesh.geometry.faces), volume_in,
                        scanner.component_volume(mesh), len(parts),
                        time.monotonic() - started,
                        lost_vertices=_count_lost(before, mesh.geometry.verts))

    except Exception as exc:
        return _failed(mesh, f"{type(exc).__name__}: {exc}", tuple(steps),
                       faces_in, volume_in, time.monotonic() - started)

    return result


def _failed(mesh: Mesh, problem: str, steps: tuple[StepResult, ...] = (),
            faces_in: int | None = None, volume_in: float = 0.0,
            elapsed: float = 0.0) -> Result:
    """A failed `Result` carrying the mesh reached and the reason."""
    faces = faces_in if faces_in is not None else (
        len(mesh.geometry.faces) if mesh.geometry is not None else 0)
    return Result(mesh, False, problem, steps, faces, faces,
                  volume_in, volume_in, 0, elapsed)
