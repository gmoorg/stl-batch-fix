"""Apply the current mesh repair sequence without writing a file.

`repair` welds T-junctions, runs duplicate cleanup before splitting, splits
edge-connected shells, orients each part, repairs each with Blender then
PyMeshFix by default, then merges and reports measurements.
`splitter.by_seams` is not in the default sequence — see
`pipeconfig.ENABLE_SPLIT_SEAMS`. `processor` judges the result;
`repairer.ok` only means the sequence completed. Pass
`tool=blender.step_blender_repair` to `repair()` for Blender-only repair —
it already has the uniform signature `tool=` requires, no wrapper needed.
This module owns operation order, `Step` classification, measurement
recording, and split/merge orchestration; naming a specific tool's filters
or parameters belongs to that tool's own module (`meshlab`, `welder`,
`blender`, `meshfix`) — see each uniform `step_*` function there.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np
from scipy.spatial import cKDTree

from . import blender, meshfix, meshlab, pipeconfig, scanner, splitter, welder
from .mesh_io import Mesh, require_geometry

# Should we move it in to pipe config? 
# Ideally, each step should have it own ID in another enumerator, and the repair should receive tuple[Step, ID] array in constructor and populate WHOLE_MESH_STEPS, PART_MESH_STEPS and SPLIT_STEP from it. 
# This would allow us to move steps around for testing. Default order should be in the pipeconfig.
# Also, this would remove the requirement to provide a custom "tool" lambda into repair
class Step(Enum):
    """Which step a `StepResult` is reporting."""

    PREP = 'prep'
    SPLIT = 'split'
    PART = 'part'
    MERGE = 'merge'

#: The whole-mesh steps `repair` runs, in order, before the split — this
#: tuple, read top to bottom, IS the order of operation. Each function is a
#: uniform `(mesh) -> (ok, mesh, detail)` step owned by the module that
#: knows its mechanics; `repairer` only sequences them.
WHOLE_MESH_STEPS: tuple[tuple[str, Callable[[Mesh], tuple[bool, Mesh, str]]], ...] = (
    ('weild', welder.step_weld_close_tjunctions),
    ('clean_null_faces', meshlab.step_clean_null_faces),
    ('clean_merge_close', meshlab.step_clean_merge_close),
    ('clean_duplicate_faces', meshlab.step_clean_duplicate_faces),
    ('clean_unreferenced', meshlab.step_clean_unreferenced),
)

#: The per-part steps `_repair_part` runs, in order — visible the same way
#: `WHOLE_MESH_STEPS` is, rather than an anonymous tuple inline in the
#: function body. Tests that need to substitute one of these must patch
#: `repairer.PART_MESH_STEPS` itself (e.g. with `mock.patch.object`), not
#: the origin module's attribute: this tuple captures the function objects
#: once, at import time, so patching `blender.step_blender_repair` after
#: import does not change what an already-built tuple holds.
PART_MESH_STEPS: tuple[tuple[str, Callable[[Mesh], tuple[bool, Mesh, str]]], ...] = (
    ('step_orient', meshlab.step_orient),
    ('step_blender_repair', blender.step_blender_repair),
    ('step_meshfix_repair', meshfix.step_meshfix_repair),
)

#: Allow float re-rounding when counting retained vertices. This absolute
#: threshold is a known scale risk; see docs/refactor/open-issues.md.
LOST_VERTEX_TOLERANCE = 1e-4


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


def _record_step(steps_list: list[StepResult], step: Step, was: int,
                 now: int, detail: str, since: float,
                 result: Mesh | None = None) -> None:
    """Record one step, scanning `result` when there is a mesh to scan.

    The one place a `StepResult` is built — `_run_step`, SPLIT, and MERGE
    all call this, so there is exactly one implementation of "scan the
    result and append a `StepResult`", not one per call site. `result` is
    `None` for the split, which produces several meshes rather than one —
    each part is then scanned by its own `Step.PART` entry.
    """
    scan = volume = None
    orphans = 0
    if result is not None and result.geometry is not None:
        scan = scanner.scan(result)
        volume = scanner.volume(result)
        orphans = (len(result.geometry.verts)
                   - len(np.unique(result.geometry.faces)))
    steps_list.append(StepResult(step, was, now, detail,
                                 time.monotonic() - since,
                                 scan, volume, int(orphans)))


@dataclass(frozen=True)
class _StepOutcome:
    """What one uniform step call plus its recording produced.

    `mesh` is always the step's own result, regardless of whether recording
    it afterward succeeded — matching what `repair` did before this was
    extracted: the mesh was reassigned on the line that called the step,
    before anything that could raise while describing the result. If
    recording raises, `record_error` carries it and the caller re-raises it
    itself, after first assigning `mesh = outcome.mesh` — so a scan failure
    still reaches the outer exception handler with the advanced mesh, not
    the one from before this step ran.
    """

    ok: bool
    mesh: Mesh
    detail: str
    record_error: Exception | None = None


def _named_detail(name: str | None, detail: str) -> str:
    """`"{name}: {detail}"`, or bare `detail` when there is no name to
    attach — shared by `_run_step` (one named step) and `_repair_part`
    (several named sub-steps folded into one composite detail string), so
    the two do not format the same "which step said what" text two
    different ways.
    """
    return detail if name is None else f"{name}: {detail}"


def _run_step(step: Step,
             name: str | None,
             step_fn: Callable[[Mesh], tuple[bool, Mesh, str]],
             mesh: Mesh,
             steps_list: list[StepResult],
             detail_prefix: str = '') -> _StepOutcome:
    """Call one uniform step and record it. See `_StepOutcome` for why a
    failure while recording does not lose the step's own result.
    """
    mark = time.monotonic()
    was = len(mesh.geometry.faces)
    ok, mesh, detail = step_fn(mesh)
    full_detail = detail_prefix + _named_detail(name, detail)

    try:
        now = len(mesh.geometry.faces)
        _record_step(steps_list,
                     step,
                     was,
                     now,
                     full_detail,
                     mark,
                     mesh)
    except Exception as exc:
        return _StepOutcome(ok, mesh, full_detail, record_error=exc)

    return _StepOutcome(ok, mesh, full_detail)


def _repair_part(part: Mesh) -> tuple[bool, Mesh, str]:
    """Orient one split part, repair it in Blender, then repair it with
    PyMeshFix — the default per-part sequence, composed from the uniform
    `(mesh) -> (ok, mesh, detail)` calls in `PART_MESH_STEPS`.

    Stops at the first `ok=False`: a part that could not be repaired is
    still defective geometry, and continuing to the next stage or merging it
    back in would produce a mesh that measures plausibly and is not a
    repair. Each stage's own flag (`pipeconfig.ENABLE_ORIENT`,
    `ENABLE_BLENDER_PART`, `ENABLE_PART_TOOL`) is checked inside that
    stage's own step function, not here — this is composition only. The
    caller (`repair`'s per-part loop) records the whole part as ONE
    `Step.PART` entry, not one per sub-step here — `_repair_part` only
    accumulates detail text, it does not call `_record_step` itself.
    """
    notes = []
    for step_name, step_fn in PART_MESH_STEPS:
        ok, part, detail = step_fn(part)
        notes.append(_named_detail(step_name, detail))
        if not ok:
            return False, part, ', '.join(notes)
    return True, part, ', '.join(notes)

# why we allow overriden repair tools but not prep steps?
def repair(mesh: Mesh,
           min_shell_faces: int = splitter.MIN_SHELL_FACES,
           tool: Callable[[Mesh], tuple[bool, Mesh, str]] = _repair_part,
           ) -> Result:
    """Run weld, clean, split, per-part repair, and merge on a loaded mesh.

    No file is written and `ok` is not a clean-mesh verdict. The default
    per-part sequence is orient, then Blender, then PyMeshFix; `tool` can
    replace the whole per-part sequence, including with
    `blender.step_blender_repair` directly for Blender alone — it already
    has the signature this parameter requires. This injection point
    bypasses the built-in sequence
    entirely — a custom `tool` does not get orientation, Blender, or
    PyMeshFix unless it calls them itself. Defect-based routing between
    Blender and PyMeshFix within the default sequence is not implemented; the
    default always runs both, in that order. A failure returns `ok=False`
    with the mesh reached at failure, which may be an intermediate after
    earlier steps.

    A tool reports a part it could not repair with `ok=False` as the first
    element of its `(ok, mesh, detail)` return; that stops the run. Raising
    works too and is reported the same way — every built-in step catches its
    own tool's exceptions and converts them to `ok=False`, so a custom `tool`
    is expected to do the same rather than let one escape uncaught.
    """
    require_geometry(mesh)

    # this should not be here! it should be a the one of the first command in processor module
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

    try:
        #  Whole-mesh steps, in the order WHOLE_MESH_STEPS lists them:
        #  weld T-junctions, then the four CLEAN filters (duplicates
        #  before the split, so deduplication sees both copies). Each is
        #  its own step: its own flag, its own log entry, its own failure
        #  — not one aggregate CLEAN entry, so a filter's contribution can
        #  be attributed rather than inferred from a joined string.
        for step_name, step_fn in WHOLE_MESH_STEPS:
            outcome = _run_step(Step.PREP, step_name, step_fn, mesh, steps)
            mesh = outcome.mesh
            if outcome.record_error is not None:
                raise outcome.record_error
            if not outcome.ok:
                return _failed(mesh, f"{Step.PREP.value}: {step_name} - {outcome.detail}",
                               tuple(steps), faces_in, volume_in,
                               time.monotonic() - started)


        #  Should splitter's steps be moved into splitter and have a ([mesh ...] )  -> ok, [mesh ...], outcome signature or something like that?  

        #  Split, repair each part, merge.  PyMeshFix rebuilds one manifold
        #  surface and discards the rest, so a multi-shell mesh reaching it
        #  whole comes back as its largest shell alone.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        if pipeconfig.ENABLE_SPLIT_SHELLS:
            parts = splitter.by_shells(mesh, min_faces=min_shell_faces)
            how = f"{len(parts)} shell part(s)"
            # No reporting?
        else:
            parts = (mesh,)
            how = 'shell split skipped (ENABLE_SPLIT_SHELLS=False)'
        if pipeconfig.ENABLE_SPLIT_SEAMS:
            # Every region, including debris: `by_seams` does not filter, and
            # judging a region is the caller's job.
            parts = tuple(r for part in parts for r in splitter.by_seams(part))
            how += f" -> {len(parts)} region(s) after seams"
            # No reporting?
        kept = sum(len(p.geometry.faces) for p in parts)

        _record_step(steps, Step.SPLIT, was, kept,
                     f"{how}, {kept} of {was} faces kept", mark)

        repaired = []
        for index, part in enumerate(parts):

            outcome = _run_step(Step.PART, None, tool, part, steps, detail_prefix=f"part {index}: ")

            repaired.append(outcome.mesh)
            if outcome.record_error is not None:
                raise outcome.record_error
            if not outcome.ok:
                # Stop at the first failed part rather than merging it back
                # in. A part that could not be repaired is still defective
                # geometry, and merging it would produce a mesh that
                # measures plausibly and is not a repair. `mesh` here is
                # still the pre-split whole mesh — the steps already record
                # which part and why, so the reason travels with the failed
                # result without needing the failed part itself.
                return _failed(mesh, outcome.detail, tuple(steps),
                               faces_in, volume_in,
                               time.monotonic() - started)

        mark = time.monotonic()
        was = sum(len(p.geometry.faces) for p in repaired)
        mesh = splitter.merge(repaired, destination=destination)
        _record_step(steps, Step.MERGE, was, len(mesh.geometry.faces),
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
