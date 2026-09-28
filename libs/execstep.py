"""Shared execution machinery for the uniform pipeline step contract.

Owns config delivery, step_logger invocation, `StepResult` recording, and
stop-on-failure — the parts of running a step sequence that are identical
regardless of which tool or which stage (whole-mesh, split, per-part) is
being run. See docs/refactor/modules.md's `execstep` entry and
docs/refactor/TODO.md's "Uniform-step refactor" section for why this exists
as its own module: it must not import `repairer`, `processor`, or any
concrete pipeline composition — those import THIS module, not the reverse,
so there is one direction of dependency between "how to run a step" and
"which steps to run".

A `MeshStep` is `(mesh, config) -> (ok, mesh, detail)` — one mesh in, one
mesh out. A `CollectionStep` is `(tuple[mesh, ...], config) -> (ok,
tuple[mesh, ...], detail)` — the shape a split step needs, since it turns
one input into several outputs (or several inputs into a flattened set of
outputs). The two are never mixed within one `run_sequence` call — see
`run_sequence`'s docstring.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import steplog
from .mesh_io import Mesh
from .steplog import StepLogger

try:
    from .pipeconfig import StepConfig
except ImportError:                                    # pragma: no cover
    StepConfig = None  # type: ignore[assignment,misc]


class Step(Enum):
    """Which stage a `StepResult` is reporting."""

    PREP = 'prep'
    SPLIT = 'split'
    PART = 'part'
    MERGE = 'merge'


MeshStep = Callable[[Mesh, "StepConfig | None"], tuple[bool, Mesh, str]]
CollectionStep = Callable[
    [tuple[Mesh, ...], "StepConfig | None"], tuple[bool, tuple[Mesh, ...], str]]


class StepKind(Enum):
    """Which call shape an `Entry.fn` expects — decides which runner a
    sequence uses, not which module owns the step. See `run_sequence`.
    """

    MESH = 'mesh'
    COLLECTION = 'collection'


@dataclass(frozen=True)
class Entry:
    """One named step in a visible pipeline composition.

    `kind` says whether `fn` is a `MeshStep` or a `CollectionStep` — decided
    once, at the point the entry is built (`mesh_entry`/`collection_entry`),
    not inferred by introspecting `fn` at call time.
    """

    name: str
    kind: StepKind
    fn: MeshStep | CollectionStep


def mesh_entry(name: str, fn: MeshStep) -> Entry:
    return Entry(name, StepKind.MESH, fn)


def collection_entry(name: str, fn: CollectionStep) -> Entry:
    return Entry(name, StepKind.COLLECTION, fn)


@dataclass(frozen=True)
class ConditionStep:
    """Wrap a `MeshStep` so it only runs when a condition holds.

    `condition_provider` and `predicate` must be both `None` (the step
    always runs — same as not wrapping it at all) or both set — a mismatched
    pair (exactly one `None`) raises `ValueError` at construction, since a
    provider with no predicate (or vice versa) cannot mean anything: there
    would be nothing to evaluate it against.

    Calling this (it is itself a `MeshStep`, so it can appear directly in an
    `Entry`) with `(mesh, config)`:
      - both `None`             -> unconditionally `operation(mesh, config)`.
      - otherwise               -> `condition_provider(mesh)` then
                                    `predicate(value)`, in one `try`; either
                                    raising fails the step with
                                    `(False, mesh, "condition failed: ...")`.
      - predicate is falsy      -> `(True, mesh, "skipped: condition not met")`
                                    — an explicit pass-through, still a real
                                    step outcome that gets recorded, not a
                                    silent no-op.
      - predicate is truthy     -> `operation(mesh, config)`.

    Because this receives the executor's own `mesh` argument for THIS call,
    fresh from whatever the immediately preceding step in the sequence
    produced, `condition_provider` always observes that preceding step's
    live output — there is no separate wiring needed for that to hold.
    """

    condition_provider: Callable[[Mesh], object] | None
    predicate: Callable[[object], bool] | None
    operation: MeshStep

    def __post_init__(self) -> None:
        has_provider = self.condition_provider is not None
        has_predicate = self.predicate is not None
        if has_provider != has_predicate:
            raise ValueError(
                "ConditionStep needs both condition_provider and predicate, "
                "or neither — got only one")

    def __call__(self, mesh: Mesh, config: "StepConfig | None" = None
                ) -> tuple[bool, Mesh, str]:
        if self.condition_provider is None:
            return self.operation(mesh, config)
        try:
            value = self.condition_provider(mesh)
            met = bool(self.predicate(value))
        except Exception as exc:
            return False, mesh, f"condition failed: {type(exc).__name__}: {exc}"
        if not met:
            return True, mesh, "skipped: condition not met"
        return self.operation(mesh, config)


@dataclass(frozen=True)
class StepResult:
    """Measurements recorded after one repair step.

    `scan` and `volume` describe the resulting mesh; both are `None` for a
    split that produces several parts. Every executed step is recorded,
    including a no-op, so a caller can attribute changes instead of
    inferring from face count.
    """

    step: Step
    faces_in: int
    faces_out: int
    detail: str
    second_elapsed: float
    scan: object | None = None
    volume: float | None = None
    orphans: int = 0

    @property
    def changed(self) -> bool:
        return self.faces_in != self.faces_out


@dataclass(frozen=True)
class _StepOutcome:
    """What one step call plus its recording produced.

    `mesh` is always the step's own result, regardless of whether recording
    it afterward succeeded — the mesh is assigned before anything that could
    raise while describing the result runs. If recording raises,
    `record_error` carries it and the caller re-raises it itself, after
    first using `outcome.mesh` — so a scan failure still reaches the outer
    exception handler with the advanced mesh, not the one from before this
    step ran.
    """

    ok: bool
    mesh: Mesh
    detail: str
    record_error: Exception | None = None


def _named_detail(name: str | None, detail: str) -> str:
    """`"{name}: {detail}"`, or bare `detail` when there is no name to
    attach — shared so different call sites do not format the same
    "which step said what" text two different ways.
    """
    return detail if name is None else f"{name}: {detail}"


def _record_step(steps_list: list[StepResult], step: Step, was: int,
                 now: int, detail: str, since: float,
                 result: Mesh | None = None,
                 step_logger: StepLogger = steplog.null_logger,
                 source_name: str = '') -> None:
    """Record one step, scanning `result` when there is a mesh to scan.

    The one place a `StepResult` is built — mesh steps, collection steps,
    and merge all call this, so there is exactly one implementation of
    "scan the result and append a `StepResult`". `result` is `None` for a
    collection step producing several meshes rather than one.

    Logs its own nested 'scan' start/end, separate from the enclosing
    step's own start/end — `scanner.scan`/`scanner.volume` are real,
    sometimes non-trivial work (a full edge walk) that would otherwise be
    silently folded into whichever step happened to call this.
    """
    from . import scanner  # local import: keeps this module's own import
                            # surface minimal for callers that never scan.
    import numpy as np

    scan = volume = None
    orphans = 0
    if result is not None and result.geometry is not None:
        with steplog.logged_step(step_logger, source_name, 'scan',
                                 f'{len(result.geometry.faces)} faces') as end:
            scan = scanner.scan(result)
            volume = scanner.volume(result)
            orphans = (len(result.geometry.verts)
                       - len(np.unique(result.geometry.faces)))
            end(f'nm={scan.non_manifold}, open={scan.open_edges}')
    steps_list.append(StepResult(step, was, now, detail,
                                 time.monotonic() - since,
                                 scan, volume, int(orphans)))


def run_step(step: Step,
            name: str | None,
            step_fn: MeshStep,
            mesh: Mesh,
            steps_list: list[StepResult],
            config: "StepConfig | None" = None,
            detail_prefix: str = '',
            step_logger: StepLogger = steplog.null_logger,
            source_name: str = '',
            log_step_name: str | None = None) -> _StepOutcome:
    """Call one uniform mesh step and record it. See `_StepOutcome` for why
    a failure while recording does not lose the step's own result.

    `step_logger` is called immediately before and after `step_fn` runs —
    "immediately" matters here: this is the one place in the sequence
    positioned to log a step BEFORE it has a chance to crash the process,
    which is the whole reason this parameter exists (a crash inside
    `step_fn`, e.g. a CGAL segfault, must still leave a record of which
    step was running).

    `name` and `log_step_name` serve two different purposes that happen to
    coincide for whole-mesh steps but must not for PART: `name` controls
    `_named_detail`'s prefix on the STORED `StepResult.detail` text (`None`
    for a composite PART call whose detail already names its own sub-steps).
    `log_step_name`, when given, overrides ONLY the `step_logger` field with
    a more specific identifier without touching detail formatting.
    """
    log_step = log_step_name if log_step_name is not None else (
        name if name is not None else step.value)
    mark = time.monotonic()
    was = len(mesh.geometry.faces)
    step_logger(source_name, 'start', log_step, None, f'{was} faces in')
    ok, mesh, detail = step_fn(mesh, config)
    full_detail = detail_prefix + _named_detail(name, detail)

    # `detail` can already start with `"{log_step}: "` — stripped only for
    # the log line; `full_detail`/`StepResult` keeps `detail` exactly as
    # returned, unmodified.
    log_detail = detail
    prefix = f'{log_step}: '
    if log_detail.startswith(prefix):
        log_detail = log_detail[len(prefix):]

    record_error = None
    try:
        now = len(mesh.geometry.faces)
        _record_step(steps_list, step, was, now, full_detail, mark, mesh,
                     step_logger=step_logger, source_name=source_name)
    except Exception as exc:
        record_error = exc
        log_detail = f'{log_detail} (recording raised {type(exc).__name__})'

    step_logger(source_name, 'end', log_step, time.monotonic() - mark, log_detail)
    if record_error is not None:
        return _StepOutcome(ok, mesh, full_detail, record_error=record_error)
    return _StepOutcome(ok, mesh, full_detail)


def run_collection_step(step: Step,
                        name: str | None,
                        step_fn: CollectionStep,
                        parts: tuple[Mesh, ...],
                        steps_list: list[StepResult],
                        config: "StepConfig | None" = None,
                        step_logger: StepLogger = steplog.null_logger,
                        source_name: str = '') -> tuple[bool, tuple[Mesh, ...], str]:
    """Call one collection step (N meshes in, M meshes out) and record it.

    Records ONE aggregate `StepResult` — total input faces vs. total output
    faces across all parts — matching how the split stage has always been
    recorded (there is no single mesh to scan for an N-to-M step, and
    recording one entry per output part would make "how many faces did this
    stage keep" require summing across records instead of reading one).
    """
    log_step = name if name is not None else step.value
    mark = time.monotonic()
    was = sum(len(p.geometry.faces) for p in parts)
    step_logger(source_name, 'start', log_step, None, f'{was} faces in')
    ok, result_parts, detail = step_fn(parts, config)
    kept = sum(len(p.geometry.faces) for p in result_parts)
    _record_step(steps_list, step, was, kept, detail, mark,
                step_logger=step_logger, source_name=source_name)
    step_logger(source_name, 'end', log_step, time.monotonic() - mark, detail)
    return ok, result_parts, detail


def as_entry(item: "Entry | tuple[str, MeshStep]") -> Entry:
    """Accept a plain `(name, step_fn)` tuple wherever an `Entry` is
    expected, normalizing it to a mesh-kind `Entry` — the shape callers
    (tests, and any external `part_steps=` caller) already build directly,
    without needing to import `mesh_entry` themselves for the common case.
    """
    return item if isinstance(item, Entry) else mesh_entry(item[0], item[1])


def run_sequence(entries: tuple[Entry, ...],
                 initial: Mesh,
                 config: "StepConfig | None",
                 steps_list: list[StepResult],
                 step_logger: StepLogger = steplog.null_logger,
                 source_name: str = '',
                 step: Step = Step.PREP,
                 log_step_name: str | None = None) -> _StepOutcome:
    """Run a homogeneous sequence of MESH-kind entries in order, stopping at
    the first failure.

    Every entry in `entries` must be `StepKind.MESH` — a sequence mixing
    mesh-kind and collection-kind entries is rejected with `ValueError`
    before anything runs, since a mesh step and a collection step do not
    share an input/output shape and dispatching per-entry would need to
    convert between `Mesh` and `tuple[Mesh, ...]` with no defined rule for
    doing so. A stage that needs collection steps (the split stage) composes
    them itself and calls `run_collection_step` directly instead of
    `run_sequence` — see `repairer._split_stage`.

    An empty `entries` returns the input mesh unchanged with `ok=True`.
    """
    entries = tuple(as_entry(entry) for entry in entries)
    if any(entry.kind is not StepKind.MESH for entry in entries):
        raise ValueError(
            "run_sequence only accepts StepKind.MESH entries; a stage "
            "needing collection steps must run them separately")
    mesh = initial
    outcome = _StepOutcome(True, mesh, '')
    for entry in entries:
        outcome = run_step(step, entry.name, entry.fn, mesh, steps_list,
                           config=config, step_logger=step_logger,
                           source_name=source_name, log_step_name=log_step_name)
        mesh = outcome.mesh
        if outcome.record_error is not None:
            return outcome
        if not outcome.ok:
            return outcome
    return outcome


def run_merge_step(merge_fn: Callable[..., Mesh],
                   parts,
                   destination: str | None,
                   steps_list: list[StepResult],
                   step_logger: StepLogger = steplog.null_logger,
                   source_name: str = '') -> _StepOutcome:
    """Merge parts into one mesh and record it, through the same recording
    primitive every other stage uses.

    `mesh` is assigned from `merge_fn`'s own result BEFORE recording runs —
    same ordering `_StepOutcome` documents for `run_step` — so a recording
    failure still returns the merged mesh, not the pre-merge parts. Merge
    itself has no `(ok, mesh, detail)` shape of its own (`splitter.merge`
    raises on invalid input rather than returning a failure), so this is not
    `run_step` over a `MeshStep` — it is merge's own small adapter onto the
    same `_record_step`/`_StepOutcome` backbone.
    """
    mark = time.monotonic()
    was = sum(len(p.geometry.faces) for p in parts)
    log_step = Step.MERGE.value
    step_logger(source_name, 'start', log_step, None, f'{len(parts)} part(s)')
    mesh = merge_fn(parts, destination=destination)
    detail = f"{len(parts)} part(s) merged"
    record_error = None
    try:
        _record_step(steps_list, Step.MERGE, was, len(mesh.geometry.faces),
                     detail, mark, mesh, step_logger=step_logger,
                     source_name=source_name)
    except Exception as exc:
        record_error = exc
    step_logger(source_name, 'end', log_step, time.monotonic() - mark,
               f'{detail}, {len(mesh.geometry.faces)} faces out')
    if record_error is not None:
        return _StepOutcome(True, mesh, detail, record_error=record_error)
    return _StepOutcome(True, mesh, detail)


#: Reused by callers with no interest in logging.
null_logger = steplog.null_logger
