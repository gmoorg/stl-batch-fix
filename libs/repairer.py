"""Apply the current mesh repair sequence without writing a file.

`repair` welds T-junctions, runs duplicate cleanup before splitting, splits
edge-connected shells, orients each part, repairs each with PyMeshFix by
default, then merges and reports measurements. `splitter.by_seams` is not in
the default sequence — see `pipeconfig.ENABLE_SPLIT_SEAMS`. `processor`
judges the result; `repairer.ok` only means the sequence completed.
`blender_part` provides the PLY-based Blender operation, but defect-based
routing between it and PyMeshFix is not implemented yet.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np
from scipy.spatial import cKDTree

from . import blender, meshfix, meshlab, pipeconfig, scanner, splitter, \
    welder
from .mesh_io import Mesh


class Step(Enum):
    """Which step a `StepResult` is reporting.

    Recorded rather than inferred from position, because steps 2 and 3 are
    conditional and step 4 runs once per part — so "the third entry" is not
    "the third step" on any interesting mesh.
    """

    WELD = 'weld'                              # 1/7. T-junctions
    CLEAN_NULL_FACES = 'clean_null_faces'       # 2/8. zero-area faces
    CLEAN_MERGE_CLOSE = 'clean_merge_close'     # 2/9. close vertices
    CLEAN_DUPLICATE_FACES = 'clean_duplicate_faces'  # 2/10. duplicate faces
    CLEAN_UNREFERENCED = 'clean_unreferenced'   # 2/11. unreferenced verts
    SPLIT = 'split'                             # 3a. into parts
    PART = 'part'                    # 3b. one part: orient, blender, pymeshfix
    MERGE = 'merge'                             # 3c. back into one mesh


def _meshlab_step(mesh: Mesh, enabled: bool, flag_name: str,
                  filter_name: str, params: dict) -> tuple[bool, Mesh, str]:
    """Shared body for the five steps built from a single PyMeshLab filter
    (the four CLEAN filters and orient): check its flag, run its filter,
    catch what `meshlab.apply_filters` raises — `apply_filters` raises
    rather than returning a failure, so it is this function's job to
    convert that into `pipeconfig`'s uniform step contract.
    """
    if not enabled:
        return True, mesh, f'skipped ({flag_name}=False)'
    try:
        result = meshlab.apply_filters(mesh, ((filter_name, params),))
    except Exception as exc:
        return False, mesh, f"{filter_name} failed: {exc}"
    return True, result, filter_name.replace('meshing_', '')


def step_clean_null_faces(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """Drop zero-area faces. Uniform step: see `_meshlab_step`."""
    return _meshlab_step(mesh, pipeconfig.ENABLE_CLEAN_NULL_FACES,
                         'ENABLE_CLEAN_NULL_FACES',
                         'meshing_remove_null_faces', {})


def step_clean_merge_close(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """Weld vertices within 0.1% of the bbox diagonal. Uniform step: see
    `_meshlab_step`. **Measured harmful** on Amidara base — see
    `pipeconfig.ENABLE_CLEAN_MERGE_CLOSE`."""
    return _meshlab_step(mesh, pipeconfig.ENABLE_CLEAN_MERGE_CLOSE,
                         'ENABLE_CLEAN_MERGE_CLOSE',
                         'meshing_merge_close_vertices', {'threshold': 0.1})


def step_clean_duplicate_faces(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """Remove faces duplicated after the merge. Uniform step: see
    `_meshlab_step`."""
    return _meshlab_step(mesh, pipeconfig.ENABLE_CLEAN_DUPLICATE_FACES,
                         'ENABLE_CLEAN_DUPLICATE_FACES',
                         'meshing_remove_duplicate_faces', {})


def step_clean_unreferenced(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """Drop vertices no face references. Uniform step: see
    `_meshlab_step`."""
    return _meshlab_step(mesh, pipeconfig.ENABLE_CLEAN_UNREFERENCED,
                         'ENABLE_CLEAN_UNREFERENCED',
                         'meshing_remove_unreferenced_vertices', {})


def step_orient(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """Orient one part outward, by geometry. Uniform step: see
    `_meshlab_step`. Runs unconditionally after splitting, without a
    signed-volume guard — a signed-volume guard misses local inversions.
    **Measured harmful** on Amidara base — see `pipeconfig.ENABLE_ORIENT`.
    """
    if not pipeconfig.ENABLE_ORIENT:
        return True, mesh, 'orient skipped (ENABLE_ORIENT=False)'
    ok, result, detail = _meshlab_step(
        mesh, True, 'ENABLE_ORIENT',
        'meshing_re_orient_faces_by_geometry', {})
    return ok, result, ('oriented' if ok else detail)


#: The four CLEAN steps, in order. `repair`'s CLEAN phase runs them in this
#: sequence, stopping at the first failure like the per-part sequence does.
CLEAN_STEPS: tuple[Callable[[Mesh], tuple[bool, Mesh, str]], ...] = (
    step_clean_null_faces, step_clean_merge_close,
    step_clean_duplicate_faces, step_clean_unreferenced,
)

#: The `Step` each entry in `CLEAN_STEPS` records under, same order.
CLEAN_STEP_ENUMS: tuple[Step, ...] = (
    Step.CLEAN_NULL_FACES, Step.CLEAN_MERGE_CLOSE,
    Step.CLEAN_DUPLICATE_FACES, Step.CLEAN_UNREFERENCED,
)


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
    """Whether this machine can run the sequence pipeconfig has enabled.

    PyMeshLab does the CLEAN and orient steps and has no in-process
    substitute, so it is required unconditionally. Blender and PyMeshFix are
    each required only when their own switch is on — the sequence should not
    report itself unavailable over a tool a disabled step would never call.
    `welder` and `splitter` are ours and always there.
    """
    return ((not pipeconfig.ENABLE_BLENDER_PART or blender.is_available())
            and (not pipeconfig.ENABLE_PART_TOOL or meshfix.is_available())
            and meshlab.is_available())


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


def _repair_part(part: Mesh) -> tuple[bool, Mesh, str]:
    """Orient one split part, repair it in Blender, then repair it with
    PyMeshFix — the default per-part sequence, composed from three uniform
    `step(mesh) -> (ok, mesh, detail)` calls.

    Stops at the first `ok=False`: a part that could not be repaired is
    still defective geometry, and continuing to the next stage or merging it
    back in would produce a mesh that measures plausibly and is not a
    repair. Each stage's own flag (`pipeconfig.ENABLE_ORIENT`,
    `ENABLE_BLENDER_PART`, `ENABLE_PART_TOOL`) is checked inside that
    stage's own step function, not here — this is composition only.
    """
    notes = []
    for step_fn in (step_orient, blender.step_blender_repair,
                   meshfix.step_meshfix_repair):
        ok, part, detail = step_fn(part)
        notes.append(detail)
        if not ok:
            return False, part, ', '.join(notes)
    return True, part, ', '.join(notes)


def blender_part(part: Mesh) -> tuple[bool, Mesh, str]:
    """Repair a split part in Blender alone.

    Pass this as `repair(tool=...)` to replace the default per-part sequence
    with Blender-only repair. Thin: the PLY round trip and identity
    preservation live in `blender.step_blender_repair`, which this just
    calls.
    """
    return blender.step_blender_repair(part)


def repair(mesh: Mesh,
           min_shell_faces: int = splitter.MIN_SHELL_FACES,
           tool: Callable[[Mesh], tuple[bool, Mesh, str]] = _repair_part,
           ) -> Result:
    """Run weld, clean, split, per-part repair, and merge on a loaded mesh.

    No file is written and `ok` is not a clean-mesh verdict. The default
    per-part sequence is orient, then Blender, then PyMeshFix; `tool` can
    replace the whole per-part sequence, including with `blender_part` for
    Blender alone. This injection point bypasses the built-in sequence
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
        weld_ok, mesh, detail = welder.step_weld_close_tjunctions(mesh)
        record(Step.WELD, was, len(mesh.geometry.faces), detail, mark, mesh)
        if not weld_ok:
            return _failed(mesh, f"weld: {detail}", tuple(steps),
                           faces_in, volume_in, time.monotonic() - started)

        # 2. Duplicates, before the split so deduplication sees both copies.
        #    Each filter is its own step: its own flag, its own log entry,
        #    its own failure — not one aggregate CLEAN entry, so a filter's
        #    contribution can be attributed rather than inferred from a
        #    joined string.
        for clean_step_enum, clean_step in zip(CLEAN_STEP_ENUMS, CLEAN_STEPS):
            mark = time.monotonic()
            was = len(mesh.geometry.faces)
            clean_ok, mesh, clean_detail = clean_step(mesh)
            record(clean_step_enum, was, len(mesh.geometry.faces),
                   clean_detail, mark, mesh)
            if not clean_ok:
                return _failed(mesh, f"{clean_step_enum.value}: "
                               f"{clean_detail}", tuple(steps),
                               faces_in, volume_in,
                               time.monotonic() - started)

        # 3. Split, repair each part, merge.  PyMeshFix rebuilds one manifold
        #    surface and discards the rest, so a multi-shell mesh reaching it
        #    whole comes back as its largest shell alone.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        if pipeconfig.ENABLE_SPLIT_SHELLS:
            parts = splitter.by_shells(mesh, min_faces=min_shell_faces)
            how = f"{len(parts)} shell part(s)"
        else:
            parts = (mesh,)
            how = 'shell split skipped (ENABLE_SPLIT_SHELLS=False)'
        if pipeconfig.ENABLE_SPLIT_SEAMS:
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
            part_ok, fixed, detail = tool(part)
            repaired.append(fixed)
            record(Step.PART, part_was, len(fixed.geometry.faces),
                   f"part {index}: {detail}", mark, fixed)
            if not part_ok:
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
