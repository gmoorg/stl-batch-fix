#!/usr/bin/env python3
"""Batch runner: per-file isolation via a worker-thread pool driving isolated
`--one-file` child processes.

See docs/refactor/orchestration.md for the current pipeline.
Each worker thread owns one child's `Popen`;
`communicate(timeout=)` plus `libs.proctree.terminate_and_confirm` are the
timeout/kill/interrupt mechanism, so Ctrl+C can reach and kill an
in-progress alpha-wrap repair that would otherwise ignore it. `libs.runstate.RunState`
is the shared, lock-guarded bookkeeping a `KeyboardInterrupt` in the main
thread needs to find and kill every live child.
"""

import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from libs import alphawrap, blender, childresult, converter, decimator      # noqa: E402
from libs import meshfix, meshlab, mesh_io, processor, publication, runstate, steplog  # noqa: E402
from libs.childresult import ChildResult                                  # noqa: E402
from libs.indicators import Indicator                                     # noqa: E402
from libs.mesh_io import Mesh                                             # noqa: E402
from libs.pool import Pool                                                # noqa: E402
from libs.proctree import terminate_and_confirm                           # noqa: E402
from libs.runstate import RunState                                        # noqa: E402


DEPENDENCIES = (
    ('CGAL', alphawrap),
    ('fast_simplification', decimator),
    ('Blender', blender),
    ('PyMeshFix', meshfix),
    ('PyMeshLab', meshlab),
)

REAP_DEADLINE_DEFAULT = 10.0
PER_FILE_TIMEOUT_DEFAULT = 3600.0
MEMORY_BUDGET_FRACTION_DEFAULT = 0.7


def _log_line(message: str) -> None:
    """Print one stderr line with a local-time timestamp prefix.

    Every progress/diagnostic line this module prints goes through here —
    the single point that adds `HH:MM:SS`, so a line pasted from terminal
    scrollback still carries when it happened without needing the process's
    own start time as a reference.
    """
    stamp = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'{stamp} {message}', file=sys.stderr, flush=True)


# ============================================================================
# --one-file child mode
# ============================================================================

def _process_one_file(source_path: str, destination: str, max_faces: int,
                       step_logger: steplog.StepLogger = steplog.null_logger) -> ChildResult:
    """The exact per-file body the old serial loop ran, now for one file only."""
    stage = 'intake'
    category = 'intake_failure'
    reason = 'invalid intake mesh'
    indicator = None
    written_path = None
    steps: tuple[str, ...] = ()
    source_name = os.path.basename(source_path)
    mesh = mesh_io.probe(source_path, destination)
    if not mesh.is_valid:
        reason = mesh.problem or reason
    else:
        try:
            stage = 'load'
            category = 'load_failure'
            loaded = mesh_io.load(mesh)
            reason = loaded.problem or 'invalid loaded mesh'
            if loaded.is_valid:
                stage = 'process'
                category = 'process_failure'
                outcome = processor.process(loaded, max_faces,
                                           step_logger=step_logger, source_name=source_name)
                if outcome.repair is not None:
                    steps = tuple(f'{s.step.name}: {s.detail}' for s in outcome.repair.steps)
                stage = 'write'
                category = 'write_failure'
                path = processor.write(outcome, source_path, destination)
                if path is None:
                    reason = ('processor.write returned None: no publication '
                              f'for {outcome.indicator.name}')
                else:
                    category = 'published'
                    indicator = outcome.indicator.name
                    written_path = path
                    stage = 'process'
                    reason = outcome.reason
        except Exception as exc:                      # noqa: BLE001 — reported, not raised
            reason = f'{type(exc).__name__}: {exc}'
    return ChildResult(path=source_path, category=category, indicator=indicator,
                       stage=stage, reason=reason, written_path=written_path, steps=steps)


def _run_one_file(args) -> int:
    """`--one-file` entry point. Writes exactly one `ChildResult` to `--result-file`.

    Exit code carries no meaning the parent trusts — it never gates whether
    a result is believed, only whether a result *file* is present, valid,
    and matches this job's own source path (see `libs.childresult`).  A
    child that dies before writing (OOM-killed, segfault inside CGAL,
    SIGKILL from the parent) simply never produces the file, which is
    itself the signal the parent's crash/reconciliation path acts on.
    """
    step_logger = (steplog.open_step_log(args.log_file) if args.log_file
                  else steplog.null_logger)
    result = _process_one_file(args.one_file, args.destination, args.max_faces, step_logger)
    childresult.write(args.result_file, result)
    return 0


# ============================================================================
# Parent: preflight, admission, and the pool-driven per-file loop
# ============================================================================

def _preflight(emitted: list[Mesh]) -> tuple[list[Mesh], list[tuple[Mesh, str]]]:
    """Split `emitted` into dispatchable jobs and pre-rejected ones.

    Rejects every mesh in a cross-job path collision (no "first seen wins" —
    walk order is not guaranteed deterministic), and any mesh whose own
    expected-path set already has something on disk (a residual stray
    marker, distinct from ordinary rerun-skipping which already happened
    inside `converter.prepare`).
    """
    valid = [m for m in emitted if m.is_valid]
    invalid = [(m, m.problem or 'invalid intake mesh') for m in emitted if not m.is_valid]

    collisions = publication.find_collisions(valid)
    mesh_to_claimed_paths: dict[int, set[str]] = {}
    for path, group in collisions.items():
        for m in group:
            mesh_to_claimed_paths.setdefault(id(m), set()).add(path)

    rejected = list(invalid)
    dispatchable = []
    for mesh in valid:
        claimed = mesh_to_claimed_paths.get(id(mesh))
        if claimed:
            rejected.append(
                (mesh, f'destination path collision with another job: {sorted(claimed)}'))
            continue
        existing = publication.preexisting_paths(mesh)
        if existing:
            rejected.append(
                (mesh, f'already exists before this run started: {sorted(existing)}'))
            continue
        dispatchable.append(mesh)
    return dispatchable, rejected


def _spawn_child(python: str, script: str, mesh: Mesh, max_faces: int,
                  result_file: str, log_file: str | None = None) -> subprocess.Popen:
    argv = [python, script, '--one-file', mesh.path, '--destination', mesh.destination,
           '--max-faces', str(max_faces), '--result-file', result_file]
    if log_file:
        argv += ['--log-file', log_file]
    return subprocess.Popen(
        argv, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


#: Every `_build_*`/`_reconcile`/`_write_synthetic_marker` result dict stores
#: `indicator` as `str | None` (an `Indicator` member's `.name`, matching
#: `ChildResult`'s own convention) — never a raw `Indicator` enum — so `_run`'s
#: consumer loop has exactly one place, `_emit_indicator`, that converts.
#:
#: `recovered` is `True` exactly when the parent had to act because the
#: child never delivered a trusted result (crash, timeout, or a partially
#: written result) — including when the outcome it reconstructs is itself
#: a clean `PROCESS`.  A recovered result always adds a diagnostic and
#: forces the run's exit code nonzero, on the grounds that "the child died
#: unexpectedly and we had to reconstruct the outcome from the filesystem"
#: is itself worth an operator's attention even when the file made it out
#: intact — see docs/refactor/orchestration.md.
def _build_cancelled_result(mesh: Mesh) -> dict:
    return {'mesh': mesh, 'category': 'cancelled', 'indicator': None,
            'reason': 'interrupted before completion; rerun to retry', 'recovered': False}


def _build_launch_failure_result(mesh: Mesh, exc: Exception) -> dict:
    return {'mesh': mesh, 'category': 'process_failure', 'indicator': None,
            'reason': f'failed to launch child: {type(exc).__name__}: {exc}', 'recovered': False}


def _write_synthetic_marker(mesh: Mesh, cause: str) -> dict:
    """Nothing was published; the parent writes the fallback marker itself.

    A full copy of the SOURCE file, never empty or a hardlink — this is the
    fallback print an operator would actually use.
    """
    import shutil
    indicator = Indicator.TIMED_OUT if cause == 'timed_out' else Indicator.FAILED
    suffix = '.timeout.stl' if cause == 'timed_out' else '.failed.stl'
    base, _ = os.path.splitext(mesh.destination)
    marker = base + suffix
    try:
        with mesh_io.staged_write(marker) as staged:
            shutil.copy2(mesh.path, staged)
    except OSError as exc:
        return {'mesh': mesh, 'category': 'write_failure', 'indicator': None,
                'reason': f'writing fallback marker for {cause} failed: {exc}', 'recovered': True}
    return {'mesh': mesh, 'category': 'published', 'indicator': indicator.name,
            'reason': f'{cause}: parent wrote fallback marker {marker!r}', 'recovered': True}


def _reconcile(mesh: Mesh, baseline: frozenset[str], cause: str) -> dict:
    recon = publication.reconcile(mesh, baseline)
    if recon.kind == 'RECOVERED':
        indicator = publication.marker_indicator_for_path(mesh.destination, recon.path)
        return {'mesh': mesh, 'category': 'published', 'indicator': indicator.name,
                'reason': f'recovered after child {cause}, confirmed via filesystem: {recon.path}',
                'recovered': True}
    if recon.kind == 'INCONSISTENT':
        return {'mesh': mesh, 'category': 'write_failure', 'indicator': None,
                'reason': f'inconsistent publication state after child {cause}: {recon.detail}',
                'recovered': True}
    return _write_synthetic_marker(mesh, cause)


class _Runner:
    """Owns the queue, the memory budget, and every callback `Pool` needs.

    A plain class rather than closures so the selector/handler/cleanup
    logic can share state explicitly instead of through captured
    variables — the lifecycle here has enough moving parts (see
    docs/refactor/orchestration.md) that implicit closure state
    made the control flow harder to follow during design, not easier.
    """

    def __init__(self, args, python: str, script: str, baselines: dict[str, frozenset[str]]):
        self.args = args
        self.python = python
        self.script = script
        self.baselines = baselines
        self.run_state = RunState()
        self.queue: deque[Mesh] = deque()
        self.results: list[dict] = []
        self._results_lock = threading.Lock()
        self.total_jobs = 0        # set by _run once the queue is built

    def _log(self, message: str) -> None:
        # Under the same lock as `results`, so a dispatch/completion line
        # from one worker thread cannot interleave mid-line with another's,
        # and progress output stays in the same order as `self.results`.
        with self._results_lock:
            _log_line(message)

    def _report(self, result: dict) -> None:
        with self._results_lock:
            self.results.append(result)
            done = len(self.results)
        mesh = result['mesh']
        clean = (result['category'] == 'published'
                and _emit_indicator(result['indicator']) is Indicator.PROCESS)
        status = 'ok' if clean else result['category']
        steps = result.get('steps', ())
        suffix = f' [{"; ".join(steps)}]' if steps else ''
        self._log(f'[{done}/{self.total_jobs}] {status}: {mesh.path}{suffix}')

    def selector(self, done, error):
        if self.run_state.is_cancelled():
            return None
        if not self.queue:
            return None
        mesh = self.queue[0]
        estimate = int((mesh.triangles or 0) * runstate.BUDGET_BYTES_PER_TRIANGLE
                       * runstate.ALPHA_WRAP_SAFETY_FACTOR_UNVALIDATED)
        token = None
        try:
            token, refusal, override = self.run_state.start(estimate, self.args.memory_budget_bytes)
            if token is None:
                return None
            self.queue.popleft()
            if override:
                try:
                    _log_line(f'admitting {mesh.path} alone: estimated {estimate}B '
                             f'exceeds budget {self.args.memory_budget_bytes}B')
                except Exception:
                    pass          # a logging failure must not strand an already-granted reservation
            return (token, mesh)
        except Exception as exc:
            self.run_state.cancel(f'selector error: {type(exc).__name__}: {exc}')
            if token is not None:
                self.run_state.mark_stuck(token, f'selector raised after granting reservation: {exc}')
            raise

    def handler(self, item):
        token, mesh = item
        try:
            self._run_one(token, mesh)
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
            if self.run_state.has_report_attempted(token):
                # This exception came from (or after) a `report_fn` call
                # that already ran once for this token — `complete_once`
                # itself refuses a second attempt (see its docstring), and
                # calling it again here would either violate that guard or,
                # worse, invoke `report_fn` a second time for one job.  The
                # token is left unresolved by design: visible in the final
                # incomplete-run summary rather than silently retried.
                self.run_state.mark_stuck(token, f'reporting failed: {detail}')
                raise
            confirmed = False
            term_detail = None
            proc = self.run_state.proc_for(token)
            if proc is not None:
                try:
                    confirmed, term_detail = terminate_and_confirm(proc, self.args.reap_deadline)
                except Exception as cleanup_exc:      # noqa: BLE001
                    term_detail = f'cleanup raised: {cleanup_exc}'
            if confirmed:
                self.run_state.mark_reaped(token)
                self.run_state.cancel(f'handler error on {mesh.path}: {detail}')
                self.run_state.complete_once(
                    token, self._report,
                    {'mesh': mesh, 'category': 'process_failure', 'indicator': None,
                     'reason': f'handler error: {detail}', 'recovered': False})
            else:
                full = detail if term_detail is None else f'{detail}; cleanup also failed: {term_detail}'
                self.run_state.mark_stuck(token, full)
            raise

    def _run_one(self, token, mesh):
        fd, result_file = tempfile.mkstemp(prefix='.stlfix-result-', suffix='.json')
        os.close(fd)
        try:
            try:
                proc = _spawn_child(self.python, self.script, mesh, self.args.max_faces,
                                   result_file, getattr(self.args, 'log_file', None))
            except Exception as exc:
                self.run_state.mark_launch_failed(token)
                self.run_state.complete_once(token, self._report,
                                             _build_launch_failure_result(mesh, exc))
                return

            if not self.run_state.spawned(token, proc):
                confirmed, detail = terminate_and_confirm(proc, self.args.reap_deadline)
                if not confirmed:
                    self.run_state.mark_stuck(token, detail)
                    return
                self.run_state.mark_reaped(token)
                self.run_state.complete_once(token, self._report, _build_cancelled_result(mesh))
                return

            self._log(f'[start] {mesh.path}')
            cause = 'exited'
            try:
                proc.communicate(timeout=self.args.per_file_timeout)
            except subprocess.TimeoutExpired:
                cause = 'timed_out'
            except Exception:
                cause = 'wait_error'

            confirmed, detail = terminate_and_confirm(proc, self.args.reap_deadline)
            if not confirmed:
                self.run_state.mark_stuck(token, detail)
                return
            self.run_state.mark_reaped(token)

            if cause == 'cancelled' or self.run_state.is_cancelled():
                self.run_state.complete_once(token, self._report, _build_cancelled_result(mesh))
                return

            # Trusting a result never depends on exit code (see
            # childresult.read_and_validate's own docstring) — a child can
            # legitimately publish successfully and then die during its own
            # shutdown, and its returncode carries no information the parent
            # should act on either way.
            result = childresult.read_and_validate(result_file, mesh.path)
            if result is not None:
                self.run_state.complete_once(
                    token, self._report,
                    {'mesh': mesh, 'category': result.category, 'indicator': result.indicator,
                     'reason': result.reason, 'recovered': False, 'steps': result.steps})
                return

            if not self.run_state.authorize_publication(token):
                self.run_state.complete_once(token, self._report, _build_cancelled_result(mesh))
                return
            marker_cause = 'timed_out' if cause == 'timed_out' else 'crashed'
            baseline = self.baselines[mesh.path]
            outcome = _reconcile(mesh, baseline, marker_cause)
            self.run_state.complete_once(token, self._report, outcome)
        finally:
            try:
                os.unlink(result_file)
            except OSError:
                pass


def _emit_indicator(indicator) -> Indicator | None:
    if indicator is None:
        return None
    if isinstance(indicator, Indicator):
        return indicator
    return Indicator[indicator]


def _run(args) -> int:
    emitted: list[Mesh] = []

    def collect(mesh: Mesh) -> None:
        emitted.append(mesh)

    _log_line(f'Scanning {args.input} ...')
    try:
        summary = converter.prepare(
            args.input, args.output, collect,
            copy_extensions={'.png', '.jpg', '.jpeg', '.gif', '.txt'},
            convert=blender.convert, workers=1,
        )
    except Exception as error:
        _log_line(f'Run incomplete: intake raised {type(error).__name__}: {error}')
        return 1

    dispatchable, rejected = _preflight(emitted)
    dispatchable.sort(key=lambda m: (m.triangles is None, m.triangles or 0))
    baselines = {mesh.path: publication.preexisting_paths(mesh) for mesh in dispatchable}

    python = sys.executable
    script = os.path.abspath(__file__)
    runner = _Runner(args, python, script, baselines)
    runner.total_jobs = len(dispatchable)
    runner.queue.extend(dispatchable)
    _log_line(f'Intake done: {len(dispatchable)} job(s) to process '
             f'({len(rejected)} rejected at intake), {args.workers} worker(s).')

    old_handler = signal.getsignal(signal.SIGINT)
    interrupted = False
    try:
        Pool(args.workers, runner.selector, runner.handler).start()
    except KeyboardInterrupt:
        interrupted = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            runner.run_state.cancel('SIGINT')
            deadline = time.monotonic() + 60.0
            while runner.run_state.has_unresolved() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            signal.signal(signal.SIGINT, old_handler)

    terminal = Counter()
    published = Counter()
    diagnostics = []
    cancelled_count = 0
    for mesh, reason in rejected:
        terminal['intake_failure'] += 1
        diagnostics.append((mesh.path, 'intake', reason))
    for result in runner.results:
        mesh = result['mesh']
        category = result['category']
        indicator = _emit_indicator(result['indicator'])
        reason = result['reason']
        recovered = result.get('recovered', False)
        if category == 'cancelled':
            cancelled_count += 1
            continue          # not a terminal outcome — eligible for a plain rerun
        terminal[category] += 1
        clean = category == 'published' and indicator is Indicator.PROCESS
        if category == 'published' and indicator is not None:
            published[indicator] += 1
        # A recovered outcome always gets a diagnostic and counts against a
        # clean run, even when the recovered indicator is itself PROCESS —
        # "the child died unexpectedly" is worth surfacing regardless of
        # whether the file made it out intact. See the comment at
        # _build_cancelled_result.
        if not clean or recovered:
            diagnostics.append((mesh.path, category, reason))

    total = sum(terminal.values())
    incomplete = (interrupted or runner.run_state.has_incomplete_reason()
                  or runner.run_state.has_unresolved() or bool(runner.queue))
    print('Intake: ' + ', '.join(
        f'{field.name}={getattr(summary, field.name)}' for field in fields(summary)))
    print('Terminal: ' + ', '.join(
        f'{name}={terminal[name]}' for name in (
            'intake_failure', 'load_failure', 'process_failure', 'write_failure',
            'published')) + f', total={total}, jobs={len(emitted)}')
    print('Published: ' + (', '.join(
        f'{indicator.name}={published[indicator]}'
        for indicator in Indicator if published[indicator]) or 'none'))
    for path, stage, reason in diagnostics:
        print(f'Diagnostic: path={path!r}, stage={stage}, reason={reason}')
    if incomplete:
        unresolved = runner.run_state.snapshot_unresolved()
        left_in_queue = len(runner.queue)
        print(f'Run INCOMPLETE: {cancelled_count} job(s) interrupted and cleanly '
              f'reaped, {len(unresolved)} unresolved job(s), '
              f'{left_in_queue} still queued and undispatched — all are eligible '
              f'for a plain rerun. Reason: {runner.run_state.incomplete_reason()}')
        for token, state in unresolved.items():
            print(f'Unresolved: job {token} in state {state!r}')
    else:
        print('Run complete.')
    return int(incomplete or bool(diagnostics) or summary.copy_failed > 0)


def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--input', metavar='DIR')
    mode.add_argument('--one-file', metavar='SRC')

    parser.add_argument('--output', metavar='DIR')
    parser.add_argument('--destination', metavar='DST')
    parser.add_argument('--result-file', metavar='PATH')
    parser.add_argument('--log-file', metavar='PATH',
                        help='append before/after-each-step lines here; '
                             'defaults to <output>/batch.log for --input mode')
    parser.add_argument('--max-faces', required=True, metavar='N',
                        help='non-negative integer face budget; 0 disables decimation')
    parser.add_argument('--workers', type=int, default=None, metavar='N')
    parser.add_argument('--per-file-timeout', type=float, default=PER_FILE_TIMEOUT_DEFAULT, metavar='SECONDS')
    parser.add_argument('--reap-deadline', type=float, default=REAP_DEADLINE_DEFAULT, metavar='SECONDS')
    parser.add_argument('--memory-budget-fraction', type=float,
                        default=MEMORY_BUDGET_FRACTION_DEFAULT, metavar='FRACTION')
    parser.add_argument('--memory-budget-bytes', type=int, default=None, metavar='BYTES')
    args = parser.parse_args(argv)

    try:
        args.max_faces = int(args.max_faces)
        if args.max_faces < 0:
            raise ValueError
    except ValueError:
        parser.error('--max-faces must be a non-negative integer')

    if args.one_file is not None:
        if args.destination is None or args.result_file is None:
            parser.error('--one-file requires --destination and --result-file')
        return _run_one_file(args)

    if args.output is None:
        parser.error('--input requires --output')
    if not os.path.isdir(args.input):
        parser.error('--input must exist and be a directory')
    if os.path.isfile(args.output):
        parser.error('--output must be a directory, not an existing file')
    try:
        source = Path(args.input).resolve()
        destination = Path(args.output).resolve()
    except (OSError, RuntimeError) as error:
        parser.error(f'cannot resolve input/output paths: {error}')
    if (source == destination or source in destination.parents
            or destination in source.parents):
        parser.error('--input and --output must not overlap in either direction')

    if args.log_file is None:
        args.log_file = os.path.join(args.output, 'batch.log')

    if args.workers is None:
        args.workers = min(4, os.cpu_count() or 1)
    if args.workers < 1:
        parser.error('--workers must be a positive integer')
    if not (args.per_file_timeout > 0 and args.per_file_timeout == args.per_file_timeout
            and args.per_file_timeout != float('inf')):
        parser.error('--per-file-timeout must be a finite positive number')
    if not (args.reap_deadline > 0 and args.reap_deadline == args.reap_deadline
            and args.reap_deadline != float('inf')):
        parser.error('--reap-deadline must be a finite positive number')

    if args.memory_budget_bytes is not None:
        if args.memory_budget_bytes <= 0:
            parser.error('--memory-budget-bytes must be a positive integer')
    else:
        if not (0 < args.memory_budget_fraction <= 1
                and args.memory_budget_fraction == args.memory_budget_fraction):
            parser.error('--memory-budget-fraction must be a finite number in (0, 1]')
        try:
            page_size = os.sysconf('SC_PAGE_SIZE')
            avail_pages = os.sysconf('SC_AVPHYS_PAGES')
            args.memory_budget_bytes = int(page_size * avail_pages * args.memory_budget_fraction)
        except (ValueError, OSError, AttributeError):
            parser.error('cannot determine available memory on this platform; '
                        'pass --memory-budget-bytes explicitly')

    missing = []
    for name, module in DEPENDENCIES:
        try:
            available = module.is_available()
        except Exception as error:
            missing.append(f'{name} (check raised {type(error).__name__}: {error})')
        else:
            if not available:
                missing.append(name)
    if missing:
        parser.error('missing required dependencies: ' + ', '.join(missing))
    return _run(args)


def main(argv=None):
    """CLI boundary: interruption never reports a completed run."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        _log_line('Run interrupted/incomplete.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
