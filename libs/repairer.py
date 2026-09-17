"""Run the repair sequence: the four steps, in the order that was measured.

    is_available()      -> at startup: can this machine repair at all?
    repair(mesh)        -> Result(mesh, steps, faces_in, faces_out, ...)

**Policy, not a tool.**  `welder`, `meshfix`, `splitter` and PyMeshLab each do
one thing and decide nothing; this decides what runs, in what order, and on
what.  It is the one module in the repair set that is allowed to have an
opinion.

The sequence, validated end to end on two real models and an all-defects
sphere:

    1. welder.repair          T-junctions
    2. CLEAN                  duplicates, before the split
    3. split -> per part: by_geometry, then repair -> merge

**Orientation is unconditional, and runs once, after the split.**  It was
previously guarded by `volume < 0` in two places; both are gone.  A signed
total only goes negative when **more than half the model is inverted**, which
is not how real models break — the guard fired *zero times* on Mandy's 38 parts
and costume01's 2, both of which contain genuinely inverted faces.  What it
blocked on Mandy: 15 inward-facing faces fixed against 1 broken.

The position is what makes it safe.  Run before the split it also erased the
seam signal the splitters read (`sphere_seam`: 40 edges / 1 closed loop -> 0/0),
blinding `by_shells` and `by_seams`.  After the split there is nothing left to
blind.

**Why this order**, each answer measured rather than reasoned:

- **welder first** — it only adds faces, never moves or deletes one, so nothing
  downstream is disturbed by having run it.  It is also the only tool that
  repairs a T-junction correctly: PyMeshFix and Blender both treat the open
  edges as a hole to close and dent the surface doing it, and PyMeshLab's
  `meshing_remove_t_vertices` destroys the mesh outright (see DO_NOT_RETRY).
- **orientation early** — a backwards surface makes PyMeshFix delete regions
  rather than flip them, which is the head-deletion case.  Fix it before
  anything else acts on the geometry.
- **duplicates before the split** — deduplication has to see both copies.
  Measured: splitting first left the `doubles` fixture at 200% volume in 2
  shells, because each copy became its own part and neither could see the
  other.
- **split before the repair tool** — PyMeshFix rebuilds *one* manifold surface
  and discards the rest.  Unsplit, it ate 4,525 support pillars off a resin
  model, and took a figure's head off another.

**Results on real input:**

    all-defects sphere   1604f nm=4 open=246 2 shells vol -4292.4
                      ->  836f, every count zero, vol +4092.9
    Mandy...-simp     188,940f nm=29 open=34 seams 4/1
                      -> 188,432f, every count zero, 99.99% volume, 4s
    costume01         900,000f nm=2,263 open=20 491 shells
                      -> 834,582f nm=0 open=28 99.99% volume, ~260s

**PyMeshFix is chaotically sensitive on a defect-dense mesh, and the number to
quote is the topology, not the face count.**  Yesterday's run of this sequence
reached 835,466f on costume01; this module reaches 834,582f, and the entire
difference traces to `welder` splitting **one** T-junction beforehand.  That
single extra face — 891,342 vs 891,343 entering step 4 — moves PyMeshFix's
output by **884 faces**.  Reproduced deliberately by running the sequence with
and without the weld step: everything else was identical.

Both results are correct (nm=0, open=28, 99.99% volume) and neither is a
regression.  On a mesh carrying 1,871 non-manifold edges PyMeshFix's cut
choices are not stable under a one-face perturbation, so a face-count
comparison across versions means nothing here.

**One figure here is below what was recorded**: the all-defects sphere reached
**840f at +4094.9** when step 4 routed to Blender, against 836f at +4092.9
through PyMeshFix.  That is the routing table's small-single-shell row, and
this module does not implement it — see `_repair_part`.  Four faces on one
synthetic fixture, recorded so it is not rediscovered as a regression.

On `Mandy...-simp` a commercial repair service kept 94.9% of faces; this keeps
**99.7%**.  It removed 5% of the model to fix 63 defects.  Our detection matched
that service exactly on both counts we share: nm=29, open=34.

**Nothing here touches the disk.**  A loaded mesh in, a loaded mesh out, as
with `decimator` and `meshfix` — the caller decides whether the result is worth
writing, and `scanner` decides whether it is sound.  This module does not judge
its own output.
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

from . import blender, mesh_io, meshfix, scanner, splitter, welder
from .mesh_io import Geometry, Mesh

try:
    import pymeshlab as _pymeshlab
    _PYMESHLAB = True
except ImportError:                                   # pragma: no cover
    _PYMESHLAB = False


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


#: The four PyMeshLab filters that remove duplicate geometry, run together.
#:
#: **All four, always.**  `merge_close_vertices` alone is not a cheaper
#: subset — it is actively worse: measured on the `doubles` fixture it collapsed
#: the vertices while leaving both face sets behind, giving **1,140
#: non-manifold edges at 200% volume**.  It needs the duplicate-face removal
#: after it to be a repair at all.
#:
#: The threshold is a `PercentageValue` of the bounding-box diagonal, not an
#: absolute distance.  `MERGE_DIST = 0.01mm`, tuned for Blender's
#: `remove_doubles`, was the one setting here that *increased* non-manifold
#: edges (2,263 -> 2,359).
CLEAN_FILTERS: tuple[tuple[str, dict], ...] = (
    ('meshing_remove_null_faces', {}),
    ('meshing_merge_close_vertices', {'threshold': 0.1}),
    ('meshing_remove_duplicate_faces', {}),
    ('meshing_remove_unreferenced_vertices', {}),
)

#: Filters that were tried and must not be tried again, with what they did.
#:
#: Kept in code rather than only in the design doc because each one *looks*
#: like the obvious thing to reach for next, and the failure of two of them is
#: invisible to every check this project has except a volume comparison.
#: How far a vertex may move and still count as the same vertex.
#:
#: **A tolerance is not optional here, and exact matching is not the strict
#: version of this check — it is a broken one.**  PyMeshLab round trips
#: coordinates through float64 and hands back float32, so a vertex nothing
#: touched comes back re-rounded.  Measured on `sphere_doubles`, where nothing
#: is deleted: exact tuple comparison reported **382 vertices lost**, while the
#: furthest any input vertex sat from its nearest output vertex was 1.0e-05.
#:
#: At 1e-4 the same fixture reports 0, and the fixtures that genuinely lose
#: geometry still report it: 6 on the all-defects sphere, 3 on `fin`.  The gap
#: between float noise (1e-5) and a real deletion (8.0, the sphere's radius) is
#: five orders of magnitude, so the threshold is not a fine judgement.
#:
#: **KNOWN BUG (2026-09-17): this is an absolute distance and will break with
#: model scale**, the same way `welder.DEFAULT_TOLERANCE` was measured to —
#: both the noise floor and the real-deletion distance scale with the model,
#: and every fixture here is r=10.  Untested above that.  The fix is to scale
#: by the bounding-box diagonal, as `CLEAN_FILTERS` already does.  See the OPEN
#: BUG entry in REFACTOR_DECISIONS.md.
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
    """What one step did, for the record.

    step            which step
    faces_in/out    face counts either side of it
    detail          what it found or changed, in one line
    second_elapsed  wall time

    Every step reports even when it changed nothing, because "the orientation
    guard did not fire" is as much a fact about a mesh as "it did" — and a run
    that skipped a step silently is indistinguishable from one where the step
    was never wired up.
    """

    step: Step
    faces_in: int
    faces_out: int
    detail: str
    second_elapsed: float

    @property
    def changed(self) -> bool:
        return self.faces_in != self.faces_out


@dataclass(frozen=True)
class Result:
    """What the sequence produced.

    mesh            the repaired mesh, or the input unchanged when it failed
    ok              True when every step ran; False when one could not
    problem         why, when `ok` is False; None otherwise
    steps           a `StepResult` per step actually run, in order
    faces_in/out    face counts for the whole sequence
    volume_in/out   signed volume either side — the only measure that sees a
                    repair having destroyed geometry
    parts           how many pieces the split produced
    second_elapsed  wall time for the sequence
    lost_vertices   input vertices absent from the output — see below

    `ok` says the sequence ran, **not** that the mesh is clean.  Nothing here
    judges its own output: `scanner.scan()` is the verdict, and the caller asks
    for it.  That separation is what stopped an earlier version of this project
    writing out meshes a library had called successfully repaired.

    **`lost_vertices` is the check that caught what four numeric checks
    missed** — a mesh scoring perfectly on non-manifold, open edge, degenerate,
    seam and volume counts while visibly dented, because PyMeshFix had deleted
    two vertices from a sound surface to close a hole that was not there.

    It needs one refinement before it can gate anything, and that is not built:
    the rule is *lost a vertex belonging to the sound surface*.  A fin apex
    does not count — it hangs off a single non-manifold edge, so removing the
    fin legitimately removes its tip.  Reported, not acted on.
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
        """Output volume as a fraction of input, **by magnitude**.  1.0 is
        perfect.

        The general check for a repair having destroyed geometry.  Every other
        measure asks whether a mesh is self-consistent; a half sphere is
        perfectly watertight and passes all of them.  Only the comparison
        against what the file was before sees the loss.

        **Magnitudes, because an inverted input has a negative volume and the
        repair's whole job is to flip the sign.**  A signed ratio reports the
        all-defects sphere as -95% when it went -4292.4 -> +4092.9, which is a
        correct repair — the number said "catastrophe" about the best result in
        the suite.  `volume_out` keeps its sign for a caller that wants it.

        It is a ratio, not a verdict: `doubles` legitimately reports 0.50,
        because two coincident spheres becoming one *is* the repair.
        """
        if self.volume_in == 0.0:
            return 1.0
        return abs(self.volume_out) / abs(self.volume_in)


def is_available() -> bool:
    """Whether this machine can run the sequence.

    PyMeshFix does step 4 and PyMeshLab does steps 2 and 3; neither has an
    in-process substitute.  `welder` and `splitter` are ours and always there.
    """
    return meshfix.is_available() and _PYMESHLAB


def _run_filters(mesh: Mesh,
                 filters: tuple[tuple[str, dict], ...]) -> Mesh:
    """Apply PyMeshLab filters to the arrays and hand back a new `Mesh`.

    Arrays in and arrays out: `load_new_mesh`/`save_current_mesh` would round
    trip through the filesystem inside our own process for no reason, the same
    reasoning as in `decimator`.

    A `float` threshold is wrapped as a `PercentageValue` here rather than at
    the call site, so `CLEAN_FILTERS` stays plain data that can be read and
    compared without importing PyMeshLab.  **There is no `AbsoluteValue`** —
    the absolute form is `PureValue`, and passing a bare float raises.
    """
    ms = _pymeshlab.MeshSet()
    ms.add_mesh(_pymeshlab.Mesh(
        vertex_matrix=mesh.geometry.verts.astype(np.float64),
        face_matrix=mesh.geometry.faces.astype(np.int32)))
    for name, params in filters:
        prepared = {
            key: (_pymeshlab.PercentageValue(value)
                  if isinstance(value, float) else value)
            for key, value in params.items()
        }
        ms.apply_filter(name, **prepared)
    current = ms.current_mesh()
    return mesh.with_geometry(Geometry(
        np.ascontiguousarray(current.vertex_matrix(), dtype=np.float32),
        np.ascontiguousarray(current.face_matrix(), dtype=np.int64)))


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


def _repair_part(part: Mesh) -> tuple[Mesh, str]:
    """Step 4's repair tool, run on one part.  Returns `(mesh, detail)`.

    **PyMeshFix only — this is a one-rung ladder, and the missing rung is
    known.**

    The measurements record a routing decision this does not implement:

        small, single-shell   Blender     allbad step 4 lost 1 vertex (the fin
                                          apex, a defect) at exactly 100.00%
                                          volume, where PyMeshFix lost 7 at
                                          99.95%; `fin_bl` was face-identical
                                          to the control
        large, multi-shell    PyMeshFix   costume01: 2,263 nm -> 0, 28 open,
                                          100% volume, ~200s, against
                                          Blender's 493 open edges at 488s and
                                          a 420s timeout in the real pipeline

    Blender is not wired in because it is a subprocess with a file boundary and
    a repair script that does not exist in `blender_fx/` yet — `convert` is the
    only one there.

    **Re-confirmed 2026-09-17**, with Blender's repair loop given an
    already-welded, oriented, cleaned, single-shell part — which is what step 4
    hands it, and is *not* what the frozen `stl_batch_fix.blender` does to a raw
    file: Blender reaches `allbad` 840f at +4094.9 against PyMeshFix's 836f at
    +4092.9, and `fin` at 760f, 100.00% volume, 1 vertex lost (the apex).  So
    the routing table is right and the omission is a real if small cost.

    **The routing rule also cannot be applied as written from here.**  This
    runs per part, after `by_shells`, so every mesh reaching it is single-shell
    by construction and "multi-shell" never occurs.  What the table really
    separates is small fixtures from large models, and no measurement says
    where the boundary is.  Adding Blender means answering that first.

    Injectable via `repair(tool=)` so that answer can be supplied without this
    module growing a threshold nobody has measured.
    """
    notes = []

    # Orientation, **unconditionally**, and this is the one place it runs.
    #
    # It used to be guarded by `volume < 0` here and in a step before the
    # split.  Both are gone, because a signed total only goes negative when
    # **more than half the model is inverted** — which is not how a real model
    # breaks.  Measured: the guard fired **zero times** on Mandy's 38 parts and
    # costume01's 2, on models that demonstrably contain inverted faces.  A
    # guard that never fires on the case it exists for is not a safety measure.
    #
    # What it was blocking, measured on Mandy: **15 inward-facing faces fixed
    # against 1 outward-facing face broken**, scattered across four parts of a
    # 188,940-face model.  Stray reversed faces are the common defect and no
    # volume test can see them.
    #
    # `by_geometry` needs no trigger because it is idempotent on correct
    # geometry — it decides outward by ray casting, so on a sound part it is a
    # no-op.  The guard was protecting against a cost, not a risk: +50% on
    # Mandy (6s to 9s) and +15% on costume01 (266s to 307s).
    #
    # It does move seam counts — 19 on Mandy, 151 on costume01 — which is the
    # effect the record warned about.  Measured consequence: **none**.  Final
    # results are identical with and without, because re-winding a face changes
    # its agreement with its neighbours and PyMeshFix then resolves it.  On
    # Mandy's part 0 the closed-loop count went 1 -> 0, an improvement.
    #
    # **Position matters and is the reason this is safe.**  Before the split it
    # also erased the seam signal that `by_shells` and `by_seams` read
    # (`sphere_seam` went 40 edges/1 loop -> 0/0).  Here the split has already
    # happened, so there is nothing left to blind.
    part = _run_filters(part, (('meshing_re_orient_faces_by_geometry', {}),))
    notes.append('oriented')

    result = meshfix.repair(part)
    if not result.ok:
        return part, f"pymeshfix failed: {result.problem}"
    notes.append(
        f"pymeshfix {len(part.geometry.faces)}f -> {result.mesh.triangles}f")
    note = ', '.join(notes)
    if 'WARNING-' in result.stderr_capture:
        # PyMeshFix reports cuts and removed triangles here rather than on
        # stdout, and they are the only warning that a repair was lossy.
        note += ' (with warnings)'
    return result.mesh, note


def blender_part(part: Mesh, timeout: float = 600) -> tuple[Mesh, str]:
    """Step 4 through Blender instead of PyMeshFix.  Pass as `repair(tool=)`.

    **Costs a file boundary**, which is the whole argument against it: the part
    is written out, Blender is launched, and the result read back.  PyMeshFix
    runs on the arrays in this process.

    **And the boundary is STL, which the user has flagged as the wrong format
    for it (2026-09-17).**  STL carries no vertex table, so each write splits
    the mesh into loose triangles — measured at exactly **6.0x** duplication of
    every vertex — and each read has to re-weld it.  On Mandy that is 38 round
    trips per repair.

    **The round-trip cost has not been measured**, and that is the first thing
    to settle: if it is not a real fraction of the 9s repair, the argument for
    changing format is only a correctness one.  PLY carries the vertex table
    and was recorded as surviving Blender intact, but that check is from
    2026-09-15 and has not been re-run.  See the re-opened PLY entry in
    REFACTOR_DECISIONS.md, where the claims are labelled by provenance.

    What it buys, measured on prepared single-shell parts — which is what step
    4 hands it, and is *not* what the frozen script does to a raw file:

        all-defects sphere   840f at +4094.9   (PyMeshFix: 836f at +4092.9)
        fin                  760f at 100.00%, losing only the fin's own apex

    Two of the script's six steps are disabled in `blender_fx/repair.blender`,
    because `repairer` already did them and did them better — see
    `blender.REPAIR_SCRIPT`.

    Returns the part unchanged with a reason when Blender fails, so a caller
    can tell "not repaired" from "repaired badly".
    """
    with tempfile.TemporaryDirectory(prefix='repairer-blender-') as folder:
        source = os.path.join(folder, 'part.ply')
        target = os.path.join(folder, 'fixed.ply')
        mesh_io.write_ply(part, source)
        ok, result = blender.repair(source, target, timeout=timeout)
        if not ok:
            why = ('timed out' if result.is_timed_out
                   else f"exit {result.exit_code}")
            return part, f"blender failed: {why}"
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
           tool: Callable[[Mesh], tuple[Mesh, str]] = _repair_part,
           ) -> Result:
    """Run the four-step repair sequence over `mesh`.

    Takes a loaded mesh and returns a loaded mesh.  Nothing is written, and
    nothing here decides whether the result is good enough — ask
    `scanner.scan()`, and compare `Result.volume_kept` against what the file
    was before.

    `tool` is step 4's repair, run per part.  It defaults to PyMeshFix and is
    injectable so a caller can substitute one without this module growing a
    routing table for a decision that has one measured answer.

    A failure returns the **input mesh unchanged** with `ok=False`, never a
    partial result: the caller must be able to tell "not repaired" from
    "repaired badly" and write the marker rather than ship the file.

    Raises `ValueError` if the mesh is not loaded — a programming error at the
    call site, not a property of the data.
    """
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry — load it before repairing")
    if not _PYMESHLAB:
        return _failed(mesh, "pymeshlab is not installed")

    started = time.monotonic()
    faces_in = len(mesh.geometry.faces)
    volume_in = scanner.volume(mesh)
    before = mesh.geometry.verts
    steps: list[StepResult] = []
    destination = mesh.destination

    def record(step: Step, was: int, now: int, detail: str,
               since: float) -> None:
        steps.append(StepResult(step, was, now, detail,
                                time.monotonic() - since))

    try:
        # 1. T-junctions.  Ours, because no other tool does this without
        #    denting the surface.  Adds faces, moves nothing.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        welded = welder.repair(mesh)
        mesh = welded.mesh
        record(Step.WELD, was, len(mesh.geometry.faces),
               f"{welded.splits} junction(s) in {welded.rounds} round(s)", mark)

        # 2. Duplicates, before the split so deduplication sees both copies.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        mesh = _run_filters(mesh, CLEAN_FILTERS)
        record(Step.CLEAN, was, len(mesh.geometry.faces),
               f"{len(CLEAN_FILTERS)} filters", mark)

        # 3. Split, repair each part, merge.  PyMeshFix rebuilds one manifold
        #    surface and discards the rest, so a multi-shell mesh reaching it
        #    whole comes back as its largest shell alone.
        mark = time.monotonic()
        was = len(mesh.geometry.faces)
        parts = splitter.by_shells(mesh, min_faces=min_shell_faces)
        kept = sum(len(p.geometry.faces) for p in parts)
        record(Step.SPLIT, was, kept,
               f"{len(parts)} part(s), {kept} of {was} faces kept", mark)

        repaired = []
        for index, part in enumerate(parts):
            mark = time.monotonic()
            part_was = len(part.geometry.faces)
            fixed, detail = tool(part)
            repaired.append(fixed)
            record(Step.PART, part_was, len(fixed.geometry.faces),
                   f"part {index}: {detail}", mark)

        mark = time.monotonic()
        was = sum(len(p.geometry.faces) for p in repaired)
        mesh = splitter.merge(repaired, destination=destination)
        record(Step.MERGE, was, len(mesh.geometry.faces),
               f"{len(repaired)} part(s) merged", mark)

    except Exception as exc:
        return _failed(mesh, f"{type(exc).__name__}: {exc}", tuple(steps),
                       faces_in, volume_in, time.monotonic() - started)

    return Result(mesh, True, None, tuple(steps), faces_in,
                  len(mesh.geometry.faces), volume_in, scanner.volume(mesh),
                  len(parts), time.monotonic() - started,
                  lost_vertices=_count_lost(before, mesh.geometry.verts))


def _failed(mesh: Mesh, problem: str, steps: tuple[StepResult, ...] = (),
            faces_in: int | None = None, volume_in: float = 0.0,
            elapsed: float = 0.0) -> Result:
    """A `Result` carrying the input mesh unchanged and the reason."""
    faces = faces_in if faces_in is not None else (
        len(mesh.geometry.faces) if mesh.geometry is not None else 0)
    return Result(mesh, False, problem, steps, faces, faces,
                  volume_in, volume_in, 0, elapsed)
