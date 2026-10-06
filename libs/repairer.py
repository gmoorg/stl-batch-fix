"""Apply the current mesh repair sequence without writing a file."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from . import decimator, execstep, meshfix, pipeconfig, scanner, splitter, steplog, winding
from .execstep import Entry, Step, StepResult, collection_entry, mesh_entry
from .mesh_io import Mesh, require_geometry
from .steplog import StepLogger

#: The whole-mesh steps `repair` runs, in order, before the split — this
#: tuple, read top to bottom, IS the order of operation. Empty by default;
#: add an `execstep.mesh_entry(name, step_fn)` here to run something before
#: the split. Execution, recording, and stop-on-failure are `execstep`'s
#: job — this module only says what runs, in what order.
WHOLE_MESH_STEPS: tuple[Entry, ...] = ()

#: The per-part steps `repair` runs by default for each split part, in
#: order — the SAME visible-tuple pattern as `WHOLE_MESH_STEPS`. A caller
#: supplying its own `part_steps=` to `repair()` replaces this ENTIRELY:
#: nothing from here is appended to a custom sequence, and an empty custom
#: sequence (`part_steps=()`) runs nothing at all. Decimation and
#: conditional MeshFix used to be force-appended after any custom sequence
#: (`_decimate_to_target_and_fix`, removed in the uniform-step refactor);
#: they are now ordinary entries here, like any other step a caller can
#: keep, drop, or reorder — settled by the user, not inferred: "final
#: decimate and meshfix after should be treat as any other stand alone
#: step what we could add or remove from list of steps."
#: Post-wrap decimation targets each part's own pre-wrap face count on a
#: best-effort basis; a count difference between target and actual output
#: must never by itself cause `processor._judge` to reject the mesh.
#: `_judge`'s actual gates are volume ratio, non-manifold, open edges, and
#: enclosed volume — no face-count check exists today, so this comment
#: states a fact already true in code.
#: Decimation is ONE PyMeshLab pass (owner decision 2026-10-04): the second
#: `decimate_again` round existed because fast_simplification plateaued far
#: above extreme targets, and that second round collapsed thin features
#: (docs/refactor/reconstruction.md, "Decimation after reconstruction").
#: The target stays best-effort; MeshFix follows so anything decimation
#: changes is still checked and repaired.
DEFAULT_PART_STEPS: tuple[Entry, ...] = (
    mesh_entry('winding', winding.step_winding_reconstruct),
    mesh_entry('decimate', decimator.make_step()),
    mesh_entry('meshfix', execstep.ConditionStep(
        scanner.scan, scanner.has_defects, meshfix.step_meshfix_repair)),
)

#: NM-only fast path (opt-in with `skip_clean`, part gate only): a part whose
#: ONLY scanned defect is non-manifold edges goes straight to MeshFix,
#: skipping winding and the post decimation, and keeps its own surface.
#: Owner, 2026-10-05: NM only first; both values are provisional and are
#: revisited after the next long run (open edges may join then).
#: Limit: NM edges per 100 faces of the part (an edge-to-face ratio, not
#: the share of edges that are NM), so it scales with the part. It is a
#: heuristic, not a time bound — MeshFix runs in-process and cannot be
#: interrupted, and a percentage does not cap the absolute NM count on a
#: very large part (`max_faces = 0`, or a direct caller). Evidence: a normal
#: decimation leaves 5-45 NM edges on 0.9-2M faces (~0.001-0.005%), which
#: MeshFix clears in seconds; the Aloy (534 on 900k, 0.059%) and Laura
#: (6,968, 0.77%) MeshFix timeouts were damaged post-reconstruction
#: decimations, so they bound nothing here. 0.05% keeps roughly the
#: earlier 500-edge allowance at a 900k-face part.
NM_FAST_PATH_MAX_PERCENT = 0.05
#: Accepted range of component volume out/in. Worst fixture loss was −1.05%
#: (20 fins on a 760-face sphere, far denser NM than a real part); 200 fins
#: on a 12,640-face sphere lost 0.05%. Catches a substantial volume change,
#: not every destructive repair; tighter than the judge's 0.90 because the
#: winding path remains available as the fallback.
NM_FAST_PATH_VOLUME_BAND = (0.98, 1.02)
#: Its own step name, so the log and summary show which route ran.
NM_FAST_PATH_STEPS: tuple[Entry, ...] = (
    mesh_entry('nm_meshfix', meshfix.step_meshfix_repair),
)

#: How far an input vertex may move and still count as retained. This
#: absolute threshold is a known scale risk; see
#: ../stl-batch-fix.old/archive/docs-refactor-2026-09-22/open-issues.md.
LOST_VERTEX_TOLERANCE = 1e-4


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

    Nearest-neighbour rather than set difference, so a vertex a repair moves
    by less than `LOST_VERTEX_TOLERANCE` still counts as kept.

    A repair that *moves* a vertex slightly is counted as keeping it, which is
    the intended reading: the question this answers is "did something get
    deleted", and `volume_kept` is what answers "did something get distorted".
    """
    if len(after) == 0:
        return len(before)
    distance, _ = cKDTree(after).query(before)
    return int((distance > tolerance).sum())


def _split_stage(min_shell_faces: int) -> tuple[Entry, ...]:
    """The split stage as an explicit, ordered tuple of collection entries —
    shell split, then (if present) seam split. This function's body is the
    ONLY place that composes the split stage: reading it top to bottom shows
    exactly which collection steps run and in what order, and a caller's own
    `min_shell_faces` changes only the shell entry's floor, never the set or
    order of entries present. Enabling seam splitting later means adding one
    more `collection_entry(...)` line here, in order — not touching a flag
    (docs/refactor/TODO.md's "Uniform-step refactor" section: "do not enable
    seam splitting... during this refactor").
    """
    return (
        collection_entry('split_shells',
                         splitter.make_shell_split_step(min_faces=min_shell_faces)),
        # seam split intentionally absent — see docstring above.
    )


def is_already_clean(mesh: Mesh) -> bool:
    """True when `mesh` has no non-manifold edges, no open edges, and
    consistent winding — the three checks together, not `Scan.is_clean`
    alone.

    Stateless: takes only the mesh, reads nothing from `pipeconfig` or any
    other module-level state, so whether/how this gates `repair()` is a
    single call site's decision, not something spread across flags. It is
    called from `repair()` when its opt-in `skip_clean` argument is set.

    `Scan.is_clean` (`open_edges == 0 and non_manifold == 0`) is NOT
    sufficient by itself — a mesh can score `is_clean` while having
    inverted normals: Amidara's real `base.stl` has 1,506 of them and 922
    winding-seam edges while reading as fully `is_clean` (measured
    2026-09-21, ../stl-batch-fix.old/archive/docs-refactor-2026-09-22/open-issues.md). Adding
    `winding_seams(mesh) == 0` closes that gap. `scanner.winding_seams`'s
    own known limitation — it only examines edges shared by exactly two
    faces, so it cannot see winding on a non-manifold edge — does not apply
    here: `non_manifold == 0` is already one of this function's own
    conditions, and a mesh that satisfies it by definition has no edge
    shared by three or more faces, so there is nothing left for that
    limitation to miss.

    Self-intersections are deliberately NOT checked: they do not by
    themselves fail a print (see the spike this function was written for,
    docs/refactor/TODO.md's "Algorithm questions" section) and alpha-wrap
    exists specifically to make them go away — a mesh whose only defect is
    self-intersection is exactly the case this gate is meant to skip
    repairing, not exclude.
    """
    return _is_clean(*_gate_scan(mesh))


def _gate_scan(mesh: Mesh) -> tuple[scanner.Scan, int | None]:
    """The gates' one scan, plus the winding-seam count when it can matter:
    `None` when open edges already rule out both a clean mesh and the
    NM-only fast path, so that walk is skipped."""
    scan = scanner.scan(mesh)
    if scan.open_edges:
        return scan, None
    return scan, scanner.winding_seams(mesh)[0]


def _is_clean(scan: scanner.Scan, seams: int | None) -> bool:
    """`is_already_clean` on a `_gate_scan` result."""
    return scan.is_clean and seams == 0


def _nm_only(scan: scanner.Scan, seams: int | None) -> bool:
    """NM edges are the only defect the scan sees. `seams == 0` is a filter,
    not proof: winding across the NM edges themselves is invisible here, and
    is checked on the result instead."""
    return (scan.non_manifold > 0 and scan.open_edges == 0
            and scan.degenerate == 0 and seams == 0)


def repair(mesh: Mesh,
           min_shell_faces: int = splitter.MIN_SHELL_FACES,
           part_steps: tuple[tuple[str, Callable[..., tuple[bool, Mesh, str]]], ...] | None = None,
           step_logger: StepLogger = steplog.null_logger,
           source_name: str = '',
           nested_process_group: bool = False,
           *,
           skip_clean: bool = False,
           reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
           ) -> Result:
    """Split, wrap each part, and merge; no file is written.

    `part_steps`, when supplied, REPLACES `DEFAULT_PART_STEPS` for this call
    entirely — taken exactly as given, in order, with nothing appended or
    removed. An empty `part_steps=()` runs no per-part steps at all. Each
    entry is `(name, step_fn)` with `step_fn` accepting `(mesh, config)`,
    the same shape `DEFAULT_PART_STEPS` itself uses — not an opaque
    callable, so a multi-step sequence still logs against a real name
    instead of a generic fallback.

    Decimation and conditional MeshFix are NOT auto-appended after a custom
    `part_steps` — they were, via the now-removed
    `_decimate_to_target_and_fix`, before the uniform-step refactor; a
    caller that wants them lists them itself, the same as any other step
    (settled by the user, not inferred — see `DEFAULT_PART_STEPS`'s
    docstring). `whole_model_diag`, though, IS still computed and bound
    automatically into every part's `StepConfig` regardless of whether
    `part_steps` is the default or a caller's own — a caller does not need
    to compute or pass it itself just because it wants a different set of
    part steps than the default.

    `step_logger`, when supplied, is called before and after each step —
    including SPLIT/MERGE, which sit outside the plain per-mesh step
    contract — so an incremental record survives even a crash partway
    through. `source_name` identifies which file's steps these are, for a
    caller sharing one log across several files/processes.

    `nested_process_group` is a caller-known fact (matching `min_shell_faces`'s
    own style — supplied by the caller, not measured per mesh), threaded into
    every part's own `StepConfig` so `blender.step_blender_repair`, if it
    appears in `part_steps`, knows whether to give its Blender invocation its
    own process session. See `pipeconfig.StepConfig.nested_process_group`.

    `skip_clean` turns on the `is_already_clean` gate, off by default because
    the gate is not yet validated on real models (docs/refactor/TODO.md). It
    checks twice. First the whole mesh after `WHOLE_MESH_STEPS`: if clean,
    split, part steps and merge are skipped — the result is still measured
    like any repair. Otherwise each part retained by the split (shell-floor
    filtering still applies): a clean one is merged as is, bypassing the part
    sequence whether it is the default or a caller's own `part_steps`. Each
    verdict is logged as a 'clean_gate' info line. A consistently wound but
    globally inverted or self-intersecting mesh passes the gate — see
    `is_already_clean`. A part whose only scanned defect is NM edges takes
    the NM fast path first (`NM_FAST_PATH_STEPS`, MeshFix alone), also ahead
    of a custom `part_steps`, even an empty one; its result is merged only
    when accepted (see `_nm_fast_path`), otherwise the part sequence runs on
    the original part. Each decision is logged as an 'nm_fast_path' line.
    """
    require_geometry(mesh)

    started = time.monotonic()
    faces_in = len(mesh.geometry.faces)
    # Per-component magnitude, not the signed total: this pair is what
    # `volume_kept` divides, and a signed total lets two oppositely wound
    # shells cancel into a denominator near zero (A02).  The per-step `volume`
    # recorded below stays signed — it describes one result and is where an
    # inside-out mesh shows up.
    with steplog.logged_step(step_logger, source_name, 'scan_volume_in',
                             f'{faces_in} faces') as end:
        volume_in = scanner.component_volume(mesh)
        end(f'{volume_in}')
    before = mesh.geometry.verts
    steps: list[StepResult] = []
    destination = mesh.destination

    try:
        # `whole_model_diag` is computed once, here, after any whole-mesh
        # steps and before the split, then carried unchanged into every
        # part's own `StepConfig` below — regardless of whether the default
        # `DEFAULT_PART_STEPS` or a caller's own `part_steps` runs, so a
        # caller does not need to compute or rebind it itself just because
        # it wants a different set of part steps than the default.
        resolved_part_entries = (
            tuple(execstep.as_entry(entry) for entry in part_steps)
            if part_steps is not None else DEFAULT_PART_STEPS)

        # Whole-mesh sequence is empty by default.
        whole_config = pipeconfig.StepConfig()
        whole_outcome = execstep.run_sequence(
            WHOLE_MESH_STEPS, mesh, whole_config, steps,
            step_logger=step_logger, source_name=source_name, step=Step.PREP)
        mesh = whole_outcome.mesh
        if whole_outcome.record_error is not None:
            raise whole_outcome.record_error
        if not whole_outcome.ok:
            return _failed(mesh, whole_outcome.detail, tuple(steps),
                           faces_in, volume_in, time.monotonic() - started)

        # Model gate (opt-in via `skip_clean`): a mesh that is already clean after whole-mesh
        # preparation skips split, part steps and merge entirely. It still
        # gets the same closing measurements as a repaired mesh, so the
        # caller's judge sees real numbers, not an assumed success.
        if skip_clean and _gate_says_clean(mesh, step_logger, source_name, '-')[0]:
            return _closing_result(mesh, before, steps, faces_in, volume_in,
                                   1, started, step_logger, source_name)

        with steplog.timed_info(step_logger, source_name, 'scan_diagonal') as report:
            whole_model_diag = scanner.diagonal(mesh)
            report(f'{whole_model_diag}')

        # Split, repair each retained part, then merge. The split stage is
        # one or more collection entries, run in the order `_split_stage`
        # composes them — see its docstring for why shell split is always
        # present and seam split is not.
        split_config = pipeconfig.StepConfig(whole_model_diag=whole_model_diag)
        parts = (mesh,)
        for entry in _split_stage(min_shell_faces):
            ok, parts, detail = execstep.run_collection_step(
                Step.SPLIT, entry.name, entry.fn, parts, steps,
                config=split_config, step_logger=step_logger, source_name=source_name)
            if not ok:
                return _failed(mesh, f"{entry.name}: {detail}", tuple(steps),
                               faces_in, volume_in, time.monotonic() - started)

        repaired = []
        for index, part in enumerate(parts):
            # Captured BEFORE the part sequence runs (alpha-wrap inflates
            # face count) — the shell's own size immediately after
            # splitting is a size it is already known to have held cleanly,
            # and is what a decimate entry in the sequence targets via
            # `StepConfig.faceCount`.
            target_faces = len(part.geometry.faces)
            part_config = pipeconfig.StepConfig(
                faceCount=target_faces, whole_model_diag=whole_model_diag,
                nested_process_group=nested_process_group,
                reconstruct_memory_budget_bytes=reconstruct_budget_bytes)
            part_id = f'{index + 1}/{len(parts)}'

            # Part gate (same `skip_clean` switch): a part that is already clean is merged
            # as split, bypassing the part sequence — default or custom. A part whose
            # only scanned defect is NM edges first tries MeshFix alone (NM fast path);
            # if that result is not accepted, the sequence runs on the original part.
            if skip_clean:
                gated = _part_gate(part, part_id, steps, step_logger, source_name)
                if gated is not None:
                    repaired.append(gated)
                    continue

            outcome = execstep.run_sequence(
                resolved_part_entries, part, part_config, steps,
                step_logger=step_logger, source_name=source_name,
                step=Step.PART, part=part_id)

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

        merge_outcome = execstep.run_merge_step(
            splitter.merge, repaired, destination, steps,
            step_logger=step_logger, source_name=source_name)
        mesh = merge_outcome.mesh
        if merge_outcome.record_error is not None:
            raise merge_outcome.record_error

        result = _closing_result(mesh, before, steps, faces_in, volume_in,
                                 len(parts), started, step_logger, source_name)

    except Exception as exc:
        return _failed(mesh, f"{type(exc).__name__}: {exc}", tuple(steps),
                       faces_in, volume_in, time.monotonic() - started)

    return result


def _gate_says_clean(mesh: Mesh, step_logger: StepLogger, source_name: str,
                     part: str) -> tuple[bool, scanner.Scan, int | None]:
    """The `is_already_clean` verdict, logged at this call site, with the
    scan and seam count it was made from (the part gate reuses them)."""
    with steplog.timed_info(step_logger, source_name, 'clean_gate', part=part) as report:
        scan, seams = _gate_scan(mesh)
        clean = _is_clean(scan, seams)
        report('clean: steps skipped' if clean else 'not clean')
    return clean, scan, seams


def _part_gate(part: Mesh, part_id: str, steps: list[StepResult],
               step_logger: StepLogger, source_name: str) -> Mesh | None:
    """The mesh to merge for this part without its sequence — the part itself
    when clean, MeshFix's accepted result when NM-only — or None to run the
    sequence. Logs the 'clean_gate' verdict like the model gate does."""
    clean, scan, seams = _gate_says_clean(part, step_logger, source_name, part_id)
    if clean:
        return part
    if not _nm_only(scan, seams):
        return None
    with steplog.timed_info(step_logger, source_name, 'nm_fast_path', part=part_id) as report:
        nm = (f'NM {scan.non_manifold} in {scan.faces} faces '
              f'({scan.non_manifold * 100 / scan.faces:.3f}%)')
        # Cross-multiplied: no division in the comparison; at the limit is tried.
        if scan.non_manifold * 100 > NM_FAST_PATH_MAX_PERCENT * scan.faces:
            report(f'{nm} over limit {NM_FAST_PATH_MAX_PERCENT:.3f}%: not tried')
            return None
        # Anything that goes wrong on this optional route falls back to the
        # part sequence; it never fails the part.
        try:
            fixed, why = _nm_fast_path(part, part_id, steps, step_logger, source_name)
        except Exception as exc:
            fixed, why = None, f'{type(exc).__name__}: {exc}'
        report(f'{nm}: {why}' if fixed is not None else f'{nm}: fallback: {why}')
    return fixed


def _nm_fast_path(part: Mesh, part_id: str, steps: list[StepResult],
                  step_logger: StepLogger, source_name: str) -> tuple[Mesh | None, str]:
    """MeshFix alone on an NM-only part, and whether its result is accepted:
    finite, no NM/open edges or winding seams left, component volume within
    `NM_FAST_PATH_VOLUME_BAND` of the input's. Returns (mesh or None, reason).
    Degenerate faces are not checked on the result, though they make a part
    ineligible: eligibility asks "only NM", acceptance follows the
    pipeline's success condition, which ignores them (`Scan.is_clean`).
    """
    volume_in = scanner.component_volume(part)
    if not (math.isfinite(volume_in) and volume_in > 0.0):
        return None, f'input volume {volume_in} not measurable'
    outcome = execstep.run_sequence(
        NM_FAST_PATH_STEPS, part, None, steps, step_logger=step_logger,
        source_name=source_name, step=Step.PART, part=part_id)
    if outcome.record_error is not None:
        raise outcome.record_error
    if not outcome.ok:
        return None, outcome.detail
    fixed = outcome.mesh
    if not np.isfinite(fixed.geometry.verts).all():
        return None, 'NaN or infinite coordinates'
    scan = scanner.scan(fixed)
    if scan.non_manifold or scan.open_edges:
        return None, f'nm={scan.non_manifold}, open={scan.open_edges} left'
    seams, _ = scanner.winding_seams(fixed)
    if seams:
        return None, f'{seams} winding seam edge(s) left'
    kept = scanner.component_volume(fixed) / volume_in
    low, high = NM_FAST_PATH_VOLUME_BAND
    if not (math.isfinite(kept) and low <= kept <= high):
        return None, f'volume {kept * 100:.2f}% outside {low * 100:.0f}-{high * 100:.0f}%'
    return fixed, f'kept, volume {kept * 100:.2f}%'


def _closing_result(mesh: Mesh, before: np.ndarray, steps: list[StepResult],
                    faces_in: int, volume_in: float, parts: int, started: float,
                    step_logger: StepLogger, source_name: str) -> Result:
    """Measure the final mesh and build the successful `Result`.

    Shared by the normal merge path and the model gate's early return, so a
    skipped repair is measured exactly like a performed one. Raises on
    unmeasurable geometry; `repair` turns that into a failed `Result`.
    """
    # A tool can hand back geometry that is not measurable — PyMeshFix,
    # PyMeshLab and Blender all return arrays this module did not build.
    # Checked before the closing measurements rather than after, because
    # `_count_lost` feeds those arrays to cKDTree, which raises on
    # non-finite input; that raise used to happen below this block and so
    # escaped `repair` entirely, past every verdict the pipeline makes.
    if not np.isfinite(mesh.geometry.verts).all():
        raise ValueError(
            "the repaired mesh has NaN or infinite coordinates")

    # Called inside `repair`'s guard: these are measurements of tool output,
    # and a measurement that fails is a failed repair, not an exception for
    # the caller to discover.
    with steplog.timed_info(step_logger, source_name, 'scan_volume_out') as report:
        volume_out = scanner.component_volume(mesh)
        report(f'{volume_out}')
    return Result(mesh, True, None, tuple(steps), faces_in,
                  len(mesh.geometry.faces), volume_in,
                  volume_out, parts,
                  time.monotonic() - started,
                  lost_vertices=_count_lost(before, mesh.geometry.verts))


def _failed(mesh: Mesh, problem: str, steps: tuple[StepResult, ...] = (),
            faces_in: int | None = None, volume_in: float = 0.0,
            elapsed: float = 0.0) -> Result:
    """A failed `Result` carrying the mesh reached and the reason."""
    faces = faces_in if faces_in is not None else (
        len(mesh.geometry.faces) if mesh.geometry is not None else 0)
    return Result(mesh, False, problem, steps, faces, faces,
                  volume_in, volume_in, 0, elapsed)
