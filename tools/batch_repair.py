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
import uuid
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


class ProgressReporter:
    """Persist run/progress/job/final records to `progress.log`, alongside
    the incremental `batch.log` step log.

    Constructed BEFORE intake starts (`converter.prepare` in `_run`) so
    intake-stage messages and the intake-exception early-return path can use
    it too. `Runner.__init__` receives this already-open writer instead of
    creating its own.

    Concurrency model: writes happen from PARENT WORKER THREADS (not
    separate OS processes), so this is a plain in-process lock, not an
    `O_APPEND` cross-process atomicity argument. `Runner._report` still
    holds its OWN `_results_lock` around a call to `write()` (so a job's
    entry into `self.results` and its progress-log record stay ordered
    together) — but `write()` additionally holds this class's OWN internal
    lock, so `ProgressReporter` is safe to call correctly regardless of
    what lock, if any, a caller already holds. This matters specifically
    for shutdown: `_run`'s finalization (the `final` record plus `close()`)
    happens OUTSIDE any `Runner`-owned lock, after a bounded wait for
    in-flight workers that can time out while a worker thread is still
    inside `_report`. Without an internal lock, that worker's write could
    interleave with or follow the `final` record, or run against an
    already-closed file handle. With it: `close()` acquires the same lock,
    sets `self._closed = True` inside it, and a `write()` that arrives
    after that point (from a genuinely still-running straggler worker) is a
    documented, safe no-op — reported to stderr, not raised into a
    finalization path that has already moved on — rather than touching a
    closed fd or corrupting the `final` record's position as the file's
    last line.

    A write/flush exception here (while still open) is NOT a new failure
    mode: it propagates to the caller (typically from inside
    `Runner._report`, itself called from `RunState.complete_once`'s
    `report_fn`), which is already handled by the existing
    `complete_once`/`mark_stuck` machinery — see `handler`'s existing
    "reporting failed" branch. No parallel/different failure path is added
    for this writer.
    """

    def __init__(self, path: str, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self._closed = False
        self.recovery_errors: list[dict] = self._recover(path)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self._f = open(path, 'ab', buffering=0)
        for error in self.recovery_errors:
            self._write_locked(error)

    @staticmethod
    def _recover(path: str) -> list[dict]:
        """The crash-safe reopen/recovery pass — see module docs (spec 5e).

        Runs ONCE, at construction time, before opening the file for
        append. Reads the existing file in BINARY mode, all offsets in
        bytes (matters for non-ASCII paths). Two phases:

        Phase 1 — every newline-terminated fragment except possibly the
        last is preserved UNCONDITIONALLY, whether or not it parses as
        valid JSON; a malformed one is collected to report as a
        `recovery_error`, never deleted.

        Phase 2 — the final fragment (after the last `b'\\n'`; empty if the
        file ends with one) is inspected independently: empty -> no action;
        valid JSON -> preserved, with one `b'\\n'` appended after it so the
        next append cannot concatenate directly onto it; NOT valid JSON ->
        THE ONLY CASE THAT TRUNCATES ANYTHING — the file is truncated back
        to immediately after the last `b'\\n'` in phase 1 (or offset 0 if
        there is none), removing exactly this unterminated fragment.

        This is not assumed to make truncation permanently impossible — a
        future kill during a later write is exactly as possible as during a
        prior run's, so this same pass runs identically on every restart.
        """
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except FileNotFoundError:
            return []
        if not raw:
            return []

        fragments = raw.split(b'\n')
        # `fragments[:-1]` are every newline-terminated record (the split
        # drops the trailing `\n` itself); `fragments[-1]` is the final
        # fragment — empty when `raw` ends with `\n`.
        terminated = fragments[:-1]
        final = fragments[-1]

        last_newline_offset = raw.rfind(b'\n')
        boundary = last_newline_offset + 1 if last_newline_offset != -1 else 0

        recovery_errors: list[dict] = []
        offset = 0
        for fragment in terminated:
            record_len = len(fragment) + 1   # + the newline this fragment ends with
            try:
                json.loads(fragment.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                recovery_errors.append({
                    'kind': 'recovery_error', 'offset': offset,
                    'raw': fragment.decode('utf-8', errors='replace'),
                })
            offset += record_len

        if not final:
            pass                                   # empty: no action
        else:
            try:
                json.loads(final.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                # The only truncating case: trim back to the last complete
                # newline-terminated record (or 0 if there was none).
                with open(path, 'r+b') as f:
                    f.truncate(boundary)
            else:
                # Valid JSON with no trailing newline: preserve as-is, but
                # guarantee the newline that separates it from the next
                # append — otherwise two JSON objects would concatenate on
                # one line (e.g. `{"kind":"job"}{"kind":"run_start"}`).
                with open(path, 'r+b') as f:
                    f.seek(0, os.SEEK_END)
                    f.write(b'\n')

        # Attach run_id lazily: recovery runs before run_id-specific state
        # exists in some call paths, so the caller (constructor) stamps
        # `run_id` onto each collected error just before writing it. Stash
        # unstamped here; the constructor fills it in.
        return recovery_errors

    def _write_locked(self, record: dict) -> None:
        """Write one record, assuming the caller already holds the shared
        lock (see class docstring)."""
        record = {**record}
        record.setdefault('run_id', self.run_id)
        line = (json.dumps(record) + '\n').encode('utf-8')
        self._f.write(line)
        self._f.flush()
        os.fsync(self._f.fileno())

    def write(self, record: dict) -> None:
        """Write one record, under this reporter's own lock.

        Safe to call whether or not the caller ALSO holds an outer lock
        (`Runner._report` holds `_results_lock` around its own call so a
        job's `self.results` entry and its progress record stay paired) —
        this method's own locking is what actually keeps the file's bytes
        intact and what makes `close()` race-free against a concurrent
        writer, not the caller's discipline alone. A write that arrives
        after `close()` is a documented no-op (see class docstring), not an
        exception into a finalization path that has already moved on.
        """
        with self._lock:
            if self._closed:
                _log_line(f'progress log already closed; dropped record: {record.get("kind")}')
                return
            self._write_locked(record)

    def finalize(self, record: dict) -> None:
        """Write the `final` record and close, as ONE atomic operation
        under this reporter's own lock.

        `write()` followed by a separate `close()` call has a real gap
        between them: `write()` releases the lock once its own line is on
        disk, and a worker thread's own `write()` can acquire the lock in
        that window and append a `job` record AFTER `final` — reported by
        REVIEW, reproduced with a read-only probe (`['final', 'job']`).
        Holding `self._lock` across BOTH the final write and the `_closed`
        flip closes that gap: no other `write()` can observe an
        intermediate state where `final` is on disk but `_closed` is not
        yet `True`, so nothing can land after it.
        """
        with self._lock:
            if self._closed:
                _log_line(f'progress log already closed; dropped final record')
                return
            self._write_locked(record)
            self._closed = True
            self._f.close()

    def close(self) -> None:
        """Mark closed and close the file handle, under the same lock a
        concurrent `write()` uses — so a `write()` either completes fully
        before this runs, or observes `_closed` and safely no-ops, never
        half-executing against a handle this call is in the middle of
        closing. Used when there is no `final` record to write atomically
        with the close (e.g. a caller that never got far enough to build
        one) — `_run`'s own finalization uses `finalize()` instead."""
        with self._lock:
            self._closed = True
            self._f.close()


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
    # Absolute, not basename: `converter._walk`/`os.walk(root)` does not
    # normalize `root`, and standalone `--one-file` also accepts a relative
    # path — a basename alone would make two same-named files in different
    # subfolders indistinguishable in a shared step log.
    source_name = os.path.abspath(source_path)
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

    def __init__(self, args, python: str, script: str, baselines: dict[str, frozenset[str]],
                 reporter: "ProgressReporter | None" = None):
        self.args = args
        self.python = python
        self.script = script
        self.baselines = baselines
        self.run_state = RunState()
        self.queue: deque[Mesh] = deque()
        self.results: list[dict] = []
        self._results_lock = threading.Lock()
        self.total_jobs = 0        # set by _run once the queue is built
        # `None` here (rather than a reporter constructed by `_Runner`
        # itself) matches every existing test's direct `_Runner(args,
        # python, script, baselines)` call, which predates progress.log and
        # supplies no reporter — those tests keep working with progress
        # logging simply disabled. `_run` always passes a real one.
        self.reporter = reporter
        # Populated by `handler`, read by `_elapsed`; see spec 5c. Under
        # the same `_results_lock` as everything else this class shares
        # across worker threads.
        self._started: dict[int, float] = {}

    def _log(self, message: str) -> None:
        # Under the same lock as `results`, so a dispatch/completion line
        # from one worker thread cannot interleave mid-line with another's,
        # and progress output stays in the same order as `self.results`.
        with self._results_lock:
            _log_line(message)
            if self.reporter is not None:
                self.reporter.write({'kind': 'progress', 'message': message})

    def _elapsed(self, token: int) -> float | None:
        """Pop `token`'s start time and return elapsed seconds, or `None`
        if this token never had one recorded (never dispatched)."""
        with self._results_lock:
            started = self._started.pop(token, None)
        return None if started is None else time.monotonic() - started

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
        if self.reporter is not None:
            with self._results_lock:
                self.reporter.write({
                    'kind': 'job', 'mesh': mesh.path, 'category': result['category'],
                    'indicator': result['indicator'], 'reason': result['reason'],
                    'elapsed_seconds': result.get('elapsed_seconds'),
                    'recovered': result.get('recovered', False),
                })

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
        # First line: the outermost per-job entry point reached by BOTH
        # `_run_one`'s own several return paths AND this except-block's own
        # reporting path — see spec 5c. `_elapsed` pops this on whichever
        # path resolves the token; a token that never resolves (reaches
        # `mark_stuck` and stays there) simply never gets `_elapsed` called
        # for it, so it carries no fabricated `elapsed_seconds`.
        with self._results_lock:
            self._started[token] = time.monotonic()
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
                result = {'mesh': mesh, 'category': 'process_failure', 'indicator': None,
                          'reason': f'handler error: {detail}', 'recovered': False}
                elapsed = self._elapsed(token)
                if elapsed is not None:
                    result['elapsed_seconds'] = elapsed
                self.run_state.complete_once(token, self._report, result)
            else:
                full = detail if term_detail is None else f'{detail}; cleanup also failed: {term_detail}'
                self.run_state.mark_stuck(token, full)
            raise

    def _complete(self, token, result: dict) -> None:
        """`complete_once` wrapper that stamps `elapsed_seconds` from the
        SAME `_started` bookkeeping `handler` seeded, for every result-dict
        construction site inside `_run_one` — see spec 5c."""
        elapsed = self._elapsed(token)
        if elapsed is not None:
            result['elapsed_seconds'] = elapsed
        self.run_state.complete_once(token, self._report, result)

    def _run_one(self, token, mesh):
        fd, result_file = tempfile.mkstemp(prefix='.stlfix-result-', suffix='.json')
        os.close(fd)
        try:
            try:
                proc = _spawn_child(self.python, self.script, mesh, self.args.max_faces,
                                   result_file, getattr(self.args, 'log_file', None))
            except Exception as exc:
                self.run_state.mark_launch_failed(token)
                self._complete(token, _build_launch_failure_result(mesh, exc))
                return

            if not self.run_state.spawned(token, proc):
                confirmed, detail = terminate_and_confirm(proc, self.args.reap_deadline)
                if not confirmed:
                    self.run_state.mark_stuck(token, detail)
                    return
                self.run_state.mark_reaped(token)
                self._complete(token, _build_cancelled_result(mesh))
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
                self._complete(token, _build_cancelled_result(mesh))
                return

            # Trusting a result never depends on exit code (see
            # childresult.read_and_validate's own docstring) — a child can
            # legitimately publish successfully and then die during its own
            # shutdown, and its returncode carries no information the parent
            # should act on either way.
            result = childresult.read_and_validate(result_file, mesh.path)
            if result is not None:
                self._complete(
                    token,
                    {'mesh': mesh, 'category': result.category, 'indicator': result.indicator,
                     'reason': result.reason, 'recovered': False, 'steps': result.steps})
                return

            if not self.run_state.authorize_publication(token):
                self._complete(token, _build_cancelled_result(mesh))
                return
            marker_cause = 'timed_out' if cause == 'timed_out' else 'crashed'
            baseline = self.baselines[mesh.path]
            outcome = _reconcile(mesh, baseline, marker_cause)
            self._complete(token, outcome)
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


def _progress_log_path(args) -> str:
    """`<output>/progress.log` by default; alongside `--log-file` when that
    was customized to a different directory (spec 5d's Location note).

    `getattr` rather than `args.log_file` directly: several existing unit
    tests build a bare `Args`-like object for `_run` without a `log_file`
    attribute at all (this tool's `--one-file` mode and `_run`'s own
    signature never required one before progress.log existed).
    """
    log_file = getattr(args, 'log_file', None)
    log_dir = os.path.dirname(log_file) if log_file else args.output
    return os.path.join(log_dir or args.output, 'progress.log')


def _run(args) -> int:
    emitted: list[Mesh] = []
    run_id = str(uuid.uuid4())

    # Constructed BEFORE `converter.prepare` runs — i.e. before intake
    # starts — so intake-stage messages and the intake-exception
    # early-return path below can use it too (spec 5d).
    reporter = ProgressReporter(_progress_log_path(args), run_id)
    reporter.write({'kind': 'run_start', 'run_id': run_id,
                    'output': args.output, 'max_faces': args.max_faces})

    def collect(mesh: Mesh) -> None:
        emitted.append(mesh)

    scanning_message = f'Scanning {args.input} ...'
    _log_line(scanning_message)
    reporter.write({'kind': 'progress', 'message': scanning_message})
    try:
        summary = converter.prepare(
            args.input, args.output, collect,
            copy_extensions={'.png', '.jpg', '.jpeg', '.gif', '.txt'},
            convert=blender.convert, workers=1,
        )
    except Exception as error:
        message = f'Run incomplete: intake raised {type(error).__name__}: {error}'
        _log_line(message)
        reporter.write({'kind': 'progress', 'message': message})
        reporter.finalize({'kind': 'final', 'intake_exception': f'{type(error).__name__}: {error}',
                           'incomplete': True})
        return 1

    dispatchable, rejected = _preflight(emitted)
    dispatchable.sort(key=lambda m: (m.triangles is None, m.triangles or 0))
    baselines = {mesh.path: publication.preexisting_paths(mesh) for mesh in dispatchable}

    # Rejected files get their `job` record written AS SOON AS the
    # rejection is known (here, right after `_preflight` returns) — not
    # deferred to end-of-run — so a resumed/monitored run sees them before
    # any dispatched job's own `job` record completes. No `elapsed_seconds`:
    # these were never dispatched.
    for mesh, reason in rejected:
        reporter.write({'kind': 'job', 'mesh': mesh.path, 'category': 'intake_failure',
                        'indicator': None, 'reason': reason, 'recovered': False})

    python = sys.executable
    script = os.path.abspath(__file__)
    runner = _Runner(args, python, script, baselines, reporter=reporter)
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
    unresolved = runner.run_state.snapshot_unresolved()
    left_in_queue = len(runner.queue)
    incomplete_reason = runner.run_state.incomplete_reason()
    if incomplete:
        print(f'Run INCOMPLETE: {cancelled_count} job(s) interrupted and cleanly '
              f'reaped, {len(unresolved)} unresolved job(s), '
              f'{left_in_queue} still queued and undispatched — all are eligible '
              f'for a plain rerun. Reason: {incomplete_reason}')
        for token, state in unresolved.items():
            print(f'Unresolved: job {token} in state {state!r}')
    else:
        print('Run complete.')

    # A strict superset of every value the printout above shows — built from
    # the EXACT SAME already-computed values, not reconstructed.
    # `finalize()`, not `write()` + `close()`: those two calls have a real
    # gap between them where a still-running worker's own `write()` can
    # land after `final` is on disk but before `_closed` is set — see
    # `finalize()`'s own docstring for how it closes that gap atomically.
    reporter.finalize({
        'kind': 'final',
        'intake': {field.name: getattr(summary, field.name) for field in fields(summary)},
        'terminal': {**{name: terminal[name] for name in terminal}, 'total': total},
        'jobs': len(emitted),
        'published': {indicator.name: count for indicator, count in published.items()},
        'diagnostics': [[path, stage, reason] for path, stage, reason in diagnostics],
        'incomplete': incomplete,
        'incomplete_reason': incomplete_reason,
        'cancelled_count': cancelled_count,
        'unresolved': unresolved,
        'left_in_queue': left_in_queue,
    })
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
                        help='non-negative integer face budget for the INITIAL '
                             'whole-mesh decimation pass only; 0 disables that '
                             'pass. Per-part post-wrap decimation always runs '
                             'by default regardless of this value.')
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
