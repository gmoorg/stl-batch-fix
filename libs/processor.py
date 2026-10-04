"""Decimate, repair, judge, and optionally write one file."""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass

from . import decimator, execstep, indicators, mesh_io, pipeconfig, repairer, scanner, splitter, steplog
from .execstep import Step
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
            repaired: repairer.Result,
            step_logger: steplog.StepLogger = steplog.null_logger,
            source_name: str = '') -> Outcome:
    """Turn the measurements into one indicator.  No I/O.

    Separate from `process` so the judgement can be tested without a
    filesystem, and so the order of the tests is visible in one place.  The
    order matters: a destroyed mesh must be caught before its defect counts
    are consulted, because a half-model can be perfectly clean.

    The whole call runs inside one `logged_step` 'judge' start/end pair
    regardless of which check inside `_judge` produced the outcome — the
    'end' message carries the resulting indicator, since that verdict is
    otherwise invisible outside the final summary line.

    `_judge`'s own `scanner.scan`/`scanner.component_volume` calls (and,
    separately, `execstep.ConditionStep`'s own condition-check cost) stay
    inside their one enclosing timed window rather than getting their own
    nested start/end pair — an accepted granularity: both are cheap,
    already-covered sub-checks of a step whose own window already brackets
    them, and breaking them out separately would multiply logging noise
    for no diagnostic gain.
    """
    with steplog.logged_step(step_logger, source_name, 'judge') as end:
        outcome = _judge(source, decimated, repaired)
        end(outcome.indicator.name)
    return outcome


def _judge(source: Mesh,
          decimated: decimator.Result,
          repaired: repairer.Result) -> Outcome:
    """The actual judgement logic, unwrapped from `_decide`'s logging.

    No second, whole-mesh decimation pass runs any more — `process` used
    to decimate the merged, repaired mesh a second time against the whole
    file's `max_faces` budget, which is what introduced non-manifold edges
    on real models (confirmed 2026-09-23; see
    docs/refactor/TODO.md's "alpha-wrap's second decimation pass
    reintroduces non-manifold edges" entry). `repairer.repair` now
    decimates each PART back to its own pre-alpha-wrap size individually,
    and fixes any part that comes out defective, before merge — so by the
    time a mesh reaches this judge, it has already been through whatever
    decimation it is going to see. There is only one geometry left to
    measure, not two.
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

    final_mesh = repaired.mesh
    faces_out = repaired.faces_out
    volume_kept = repaired.volume_kept
    scan = scanner.scan(final_mesh)
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
    enclosed = scanner.component_volume(final_mesh)
    if not (math.isfinite(enclosed) and enclosed > 0.0):
        return Outcome(
            Indicator.BROKEN, None, 'source',
            f"encloses no volume: {faces_out} faces measuring "
            f"{enclosed}, which is a surface rather than a solid",
            scan=scan, decimation=decimated, repair=repaired)

    return Outcome(Indicator.PROCESS, final_mesh, None,
                   f"clean: {faces_out} faces, "
                   f"{volume_kept * 100:.2f}% of volume",
                   scan=scan, decimation=decimated, repair=repaired)


def _decimation_failure_reason(result: decimator.Result) -> str:
    """Explain every failed decimation attempt for the undecimated marker."""
    attempts = '; '.join(f"{rung.value}: {why}"
                         for rung, why in result.attempts)
    return f"every decimator failed ({attempts})"


def _decimate_logged(mesh: Mesh, max_faces: int, step_name: str,
                     step_logger: steplog.StepLogger, source_name: str) -> decimator.Result:
    """The initial whole-mesh decimation pass, run through the SAME shared
    executor and the SAME `decimator.make_step` implementation that
    `repairer.repair` uses for each part's post-reconstruction `decimate`
    entry — one decimator step implementation in every position (docs/refactor/TODO.md's "Uniform-step refactor" section).

    `evidence` is a fresh local list owned entirely by this call: nothing
    else reads or writes it, and it goes out of scope when this function
    returns having yielded exactly one `decimator.Result` (`make_step`
    guarantees one `append` per call, success or failure). Returns that raw
    `decimator.Result`; the caller decides what a `Rung.FAILED` result means
    for its own `Outcome`.

    Recording-failure precedence: if the shared executor's own post-step
    recording (`StepResult`/scan bookkeeping — a NEW code path this
    function did not have before, since it previously never scanned or
    recorded anything) raises, `Rung.FAILED` is still checked and returned
    first — an unrelated recording failure must not hide a real, meaningful
    UNDECIMATED outcome. A recording failure on top of a SUCCESSFUL
    decimation, by contrast, has no existing classification and is
    re-raised, to be caught by `process`'s own caller the same way
    `repairer.repair`'s own `record_error` re-raise is caught by its outer
    `try`.
    """
    evidence: list[decimator.Result] = []
    step = decimator.make_step(evidence)
    config = pipeconfig.StepConfig(faceCount=max_faces)
    outcome = execstep.run_step(Step.PREP, step_name, step, mesh, [],
                                config=config, step_logger=step_logger,
                                source_name=source_name)
    result = evidence[0]
    if result.rung is decimator.Rung.FAILED:
        return result
    if outcome.record_error is not None:
        raise outcome.record_error
    return result


def process(mesh: Mesh, max_faces: int,
            part_steps=None,
            step_logger: steplog.StepLogger = steplog.null_logger,
            source_name: str = '',
            nested_process_group: bool = False,
            *,
            skip_clean: bool = False,
            reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
            min_shell_faces: int = splitter.MIN_SHELL_FACES
            ) -> Outcome:
    """Decimate, repair and judge one loaded mesh.  Nothing is written.

    Returns the decision and the mesh it applies to; `write` puts it on disk.
    Splitting those apart keeps every judgement testable without a filesystem,
    and means a caller can inspect an `Outcome` before committing to it.

    `max_faces <= 0` disables only the INITIAL whole-mesh decimation pass,
    matching `decimator.decimate`. Per-part post-wrap decimation
    (`repairer.DEFAULT_PART_STEPS`) still runs by default regardless of
    `max_faces`, unless a caller's own `part_steps` omits it.

    Raises `ValueError` if the mesh is not loaded — a programming error at the
    call site.

    `nested_process_group` is threaded straight into `repairer.repair(...)` —
    a caller-known fact about whether this call runs inside an enclosing
    `proctree`-managed process group. See
    `pipeconfig.StepConfig.nested_process_group`.

    `step_logger`, when supplied, is called before and after the initial
    whole-mesh decimation pass — previously invisible to any log, since
    neither `decimator.Result` nor its `Rung`/`attempts` reached anywhere
    outside this function's return value — and is threaded into
    `repairer.repair` so the whole repair sequence logs through the same
    stream. There is no second, whole-mesh decimation pass any more: each
    PART is decimated back to its own pre-alpha-wrap size individually,
    inside `repairer.repair` itself (an ordinary entry in
    `repairer.DEFAULT_PART_STEPS`), and fixed there if that decimation left
    defects — see docs/refactor/TODO.md's "alpha-wrap's second decimation
    pass reintroduces non-manifold edges" entry for why the old whole-mesh
    second pass was removed.

    `_decimate_logged`'s own `StepResult`/scan bookkeeping (from running
    through the shared `execstep` executor) is intentionally transient: it
    is passed a disposable list and discarded once `_decimate_logged`
    returns. Incremental `step_logger` output is this pass's persistent
    record, the same as it already is for the rest of the pipeline;
    `Outcome.decimation` carries the rich `decimator.Result` that callers
    actually consume.

    `skip_clean` is passed straight to `repairer.repair` (opt-in
    `is_already_clean` gate; see its docstring), and so is `min_shell_faces`
    (the shell split's debris floor, `batch_repair.toml` `min_shell_faces`).
    The initial decimation always runs, so the model gate inspects the
    decimated mesh, and a gated mesh is still judged here like any other.
    """
    mesh_io.require_geometry(mesh)
    decimated = _decimate_logged(mesh, max_faces, 'decimate', step_logger, source_name)
    if decimated.rung is decimator.Rung.FAILED:
        # Its own outcome, not a lesser repair failure.  A file that cannot be
        # reduced will be reduced by the printer instead, which reintroduces
        # exactly the defects this tool removes.
        return Outcome(Indicator.UNDECIMATED, None, 'source',
                       _decimation_failure_reason(decimated),
                       decimation=decimated)

    repaired = repairer.repair(decimated.mesh, part_steps=part_steps,
                               step_logger=step_logger, source_name=source_name,
                               nested_process_group=nested_process_group,
                               skip_clean=skip_clean,
                               reconstruct_budget_bytes=reconstruct_budget_bytes,
                               min_shell_faces=min_shell_faces)
    return _decide(mesh, decimated, repaired,
                   step_logger=step_logger, source_name=source_name)


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

