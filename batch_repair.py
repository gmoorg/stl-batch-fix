#!/usr/bin/env python3
"""Batch runner: per-file isolation via a worker-thread pool driving isolated
`batch_repair_child.py` processes.

Takes no command-line arguments. Every option is read from
`batch_repair.toml` beside this script (documented in
`batch_repair.example.toml`; loaded by `libs.runconfig`).

See docs/refactor/orchestration.md for the current pipeline.
Each worker thread owns one child's `Popen`;
`communicate(timeout=)` plus `libs.proctree.terminate_and_confirm` are the
timeout/kill/interrupt mechanism, so Ctrl+C can reach and kill an
in-progress alpha-wrap repair that would otherwise ignore it. `libs.runstate.RunState`
is the shared, lock-guarded bookkeeping a `KeyboardInterrupt` in the main
thread needs to find and kill every live child.
"""

import datetime
import functools
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

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from libs import blender, childresult, converter, decimator, dependencies, pipeconfig, winding  # noqa: E402
from libs import meshfix, meshlab, mesh_io, modellog, processor, publication, runconfig, runstate, steplog  # noqa: E402
from libs import indicators, jobmemory, splitter                          # noqa: E402
from libs.childresult import ChildResult                                  # noqa: E402
from libs.indicators import Indicator                                     # noqa: E402
from libs.mesh_io import Mesh                                             # noqa: E402
from libs.pool import Pool                                                # noqa: E402
from libs.proctree import terminate_and_confirm                           # noqa: E402
from libs.runconfig import ConfigError, RunConfig                         # noqa: E402
from libs.runstate import RunState                                        # noqa: E402


#: The run configuration: always beside the script, never chosen per run.
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'batch_repair.toml')
#: The documented template a user copies to `CONFIG_PATH`.
EXAMPLE_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'batch_repair.example.toml')
#: The per-file child the parent spawns; see its module docstring.
CHILD_SCRIPT = os.path.join(SCRIPT_DIR, 'batch_repair_child.py')


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
# Per-file body, run inside batch_repair_child.py
# ============================================================================

def _process_one_file(source_path: str, destination: str, max_faces: int,
                       step_logger: steplog.StepLogger = steplog.null_logger,
                       nested_process_group: bool = False,
                       *,
                       skip_clean: bool = False,
                       reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
                       min_shell_faces: int = splitter.MIN_SHELL_FACES,
                       load_path: str | None = None
                       ) -> ChildResult:
    """The exact per-file body the old serial loop ran, now for one file only.

    `load_path` is the mesh to repair when it is not `source_path` itself:
    the repair pass loads what the prepare pass saved. Markers still copy
    `source_path`, the job's own source.

    `nested_process_group` is threaded straight into `processor.process(...)` —
    see `batch_repair_child.run_one_file`'s docstring for where it comes from.
    `skip_clean` comes from the opt-in `--skip-clean` flag; see
    `repairer.repair`.
    """
    stage = 'intake'
    category = 'intake_failure'
    reason = 'invalid intake mesh'
    indicator = None
    written_path = None
    steps: tuple[str, ...] = ()
    # Absolute, not basename: `converter._walk`/`os.walk(root)` does not
    # normalize `root`, and a direct child invocation may pass a relative
    # path — a basename alone would make two same-named files in different
    # subfolders indistinguishable in a shared step log.
    source_name = os.path.abspath(source_path)
    mesh = mesh_io.probe(load_path or source_path, destination)
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
                                           step_logger=step_logger, source_name=source_name,
                                           nested_process_group=nested_process_group,
                                           skip_clean=skip_clean,
                                           reconstruct_budget_bytes=reconstruct_budget_bytes,
                                           min_shell_faces=min_shell_faces)
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


#: Decimated copies, kept between runs beside `indicators.EXPORT_DIRNAME`
#: in the input tree; the face target is part of each file name.
DECIMATED_DIRNAME = indicators.DECIMATED_DIRNAME


def decimated_path(input_root: str, source: str, max_faces: int) -> str:
    """Where the prepare pass keeps the decimated mesh of job `source`.

    Keyed by the job's own source path relative to the input tree — the
    file the job loads, so a converted model is keyed by its
    `stl-exported/` copy and never shares a cache with a native STL bound
    for the same output (`stl-decimated/stl-exported/a.stl.N.stl` vs
    `stl-decimated/a.stl.N.T.stl`). A new `max_faces` gets its own file, and
    so do new decimator settings: `T` is `decimator.settings_tag()`, so a
    cache made with other parameters is never read (files from before the
    tag, `a.stl.N.stl`, are left on disk unread). Sources are assumed
    unmodified; a changed source or PyMeshLab version is not detected.

    The one clash left needs an input directory literally named like a cache
    file (`a.stl.900000.T.stl/`); writing that cache then fails, and the job
    fails with the reason rather than reading the wrong geometry.
    """
    rel = os.path.relpath(os.path.abspath(source), os.path.abspath(input_root))
    return os.path.join(os.path.abspath(input_root), DECIMATED_DIRNAME,
                        f'{rel}.{int(max_faces)}.{decimator.settings_tag()}.stl')


def expected_prepared_path(mesh: Mesh, cache_path: str, max_faces: int) -> str:
    """The mesh the repair pass will load: the cache when the initial
    decimation has work to do (the decimator's own guard), else the source."""
    needs = max_faces > 0 and (mesh.triangles or 0) > max_faces
    return cache_path if needs else mesh.path


def _load_cache(cache_path: str, destination: str) -> Mesh | None:
    """A usable cached decimation, or None to rebuild it."""
    if not os.path.isfile(cache_path):
        return None
    try:
        cached = mesh_io.probe(cache_path, destination)
        if not cached.is_valid:
            return None
        loaded = mesh_io.load(cached)
        return loaded if loaded.is_valid and loaded.triangles > 0 else None
    except Exception:                                 # noqa: BLE001 — rebuild instead
        return None


def _prepare_one_file(source_path: str, destination: str, max_faces: int,
                      cache_path: str,
                      step_logger: steplog.StepLogger = steplog.null_logger,
                      *,
                      reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
                      min_shell_faces: int = splitter.MIN_SHELL_FACES) -> ChildResult:
    """The prepare pass for one file: initial decimation, cache, estimate.

    Either hands the job to the repair pass (`childresult.PREPARED`, with the
    mesh to load and its `jobmemory.repair_bytes` estimate) or ends it with
    an ordinary terminal result: an unreadable file, or UNDECIMATED published
    exactly as `processor.process` would publish it.

    A fresh cache is reloaded from disk before estimating, through the same
    path a cache hit takes, so the estimate is computed on exactly the mesh
    the repair pass will load (binary STL keeps no vertex identity; loading
    welds identical coordinates).
    """
    stage, category, reason = 'intake', 'intake_failure', 'invalid intake mesh'
    source_name = os.path.abspath(source_path)
    steps: list[str] = []

    def terminal(**fields) -> ChildResult:
        return ChildResult(path=source_path, mode='prepare', steps=tuple(steps),
                           **{'category': category, 'indicator': None, 'stage': stage,
                              'reason': reason, 'written_path': None, **fields})

    probed = mesh_io.probe(source_path, destination)
    if not probed.is_valid:
        reason = probed.problem or reason
        return terminal()
    try:
        prepared_path = expected_prepared_path(probed, cache_path, max_faces)
        mesh = None
        if prepared_path == cache_path:
            mesh = _load_cache(cache_path, destination)
            if mesh is not None:
                steps.append(f'decimate: cached, {mesh.triangles} faces ({cache_path})')
                step_logger(source_name, 'info', 'decimate', '-', 0.0,
                            f'cached, {mesh.triangles} faces out ({cache_path})')
        if mesh is None:
            stage, category = 'load', 'load_failure'
            loaded = mesh_io.load(probed)
            if not loaded.is_valid:
                reason = loaded.problem or 'invalid loaded mesh'
                return terminal()
            if prepared_path == source_path:
                mesh = loaded
            else:
                stage, category = 'process', 'process_failure'
                failed, decimated = processor.decimate_initial(
                    loaded, max_faces, step_logger, source_name)
                steps.append(f'decimate: {decimated.rung.value}, {decimated.faces_out} faces out')
                if failed is not None:
                    stage, category = 'write', 'write_failure'
                    written = processor.write(failed, source_path, destination)
                    if written is None:
                        reason = 'processor.write returned None: no publication for UNDECIMATED'
                        return terminal()
                    category, stage, reason = 'published', 'process', failed.reason
                    return terminal(indicator=failed.indicator.name, written_path=written)
                stage, category = 'write', 'write_failure'
                if os.path.isdir(cache_path):
                    reason = (f'decimated cache {cache_path} is a directory in the input '
                              f'tree; rename that directory to repair this file')
                    return terminal()
                mesh_io.write(decimated.mesh.with_destination(cache_path))
                mesh = _load_cache(cache_path, destination)
                if mesh is None:
                    reason = f'decimated cache {cache_path} could not be read back'
                    return terminal()
        stage, category = 'process', 'process_failure'
        estimate = jobmemory.repair_bytes(mesh, min_shell_faces, reconstruct_budget_bytes)
    except Exception as exc:                          # noqa: BLE001 — reported, not raised
        reason = f'{type(exc).__name__}: {exc}'
        return terminal()
    return ChildResult(path=source_path, category=childresult.PREPARED, indicator=None,
                       stage='prepare', mode='prepare', written_path=None,
                       reason=f'{mesh.triangles} faces, repair estimate {estimate / 1e9:.2f} GB',
                       steps=tuple(steps), prepared_path=prepared_path,
                       estimate_bytes=int(estimate))


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
                  result_file: str, log_file: str | None = None,
                  *,
                  skip_clean: bool = False,
                  output_log=None,
                  reconstruct_budget_bytes: int = pipeconfig.StepConfig.reconstruct_memory_budget_bytes,
                  min_shell_faces: int = splitter.MIN_SHELL_FACES,
                  mode: str = 'repair',
                  cache_path: str | None = None,
                  load_from: str | None = None
                  ) -> subprocess.Popen:
    """Start one child. `output_log`, when given, is an already-open binary
    file (the model log, opened for append by the caller) that receives the
    child's stdout and stderr directly, so every tool's output lands there as
    it is written and survives the child crashing; None sends them to
    DEVNULL. Opening is the caller's job, so a log that cannot be opened is
    never mistaken for a failed launch."""
    argv = [python, script, '--one-file', mesh.path, '--destination', mesh.destination,
           '--max-faces', str(max_faces), '--result-file', result_file,
           '--managed-child']
    if log_file:
        argv += ['--log-file', log_file]
    if skip_clean:
        argv.append('--skip-clean')
    argv += ['--reconstruct-budget-bytes', str(reconstruct_budget_bytes)]
    argv += ['--min-shell-faces', str(min_shell_faces)]
    if mode != 'repair':
        argv += ['--mode', mode]
    if cache_path is not None:
        argv += ['--cache-path', cache_path]
    if load_from is not None:
        argv += ['--load-from', load_from]
    output = subprocess.DEVNULL if output_log is None else output_log
    return subprocess.Popen(
        argv, start_new_session=True, stdout=output, stderr=output,
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

    def __init__(self, config: RunConfig, python: str, script: str,
                 baselines: dict[str, frozenset[str]],
                 reporter: "ProgressReporter | None" = None,
                 run_id: str = '-'):
        self.config = config
        self.run_id = run_id      # stamped into each model log's attempt header
        self.python = python
        self.script = script
        self.baselines = baselines
        self.run_state = RunState()
        self.queue: deque[Mesh] = deque()
        self.results: list[dict] = []
        self._results_lock = threading.Lock()
        self.total_jobs = 0        # set by _run once the queue is built
        # `None` here (rather than a reporter constructed by `_Runner`
        # itself) matches every existing test's direct `_Runner(config,
        # python, script, baselines)` call, which predates progress.log and
        # supplies no reporter — those tests keep working with progress
        # logging simply disabled. `_run` always passes a real one.
        self.reporter = reporter
        # Populated by `handler`, read by `_elapsed`; see spec 5c. Under
        # the same `_results_lock` as everything else this class shares
        # across worker threads.
        self._started: dict[int, float] = {}
        # Which child the next dispatch launches: 'prepare' (pass 1) or
        # 'repair' (pass 2); `_run` switches it between the two passes.
        # A runner used directly (tests) defaults to the single repair pass.
        self.mode = 'repair'
        # Pass-1 handoffs by job source path: prepared_path, estimate_bytes,
        # steps and elapsed_seconds. A job present here has no terminal
        # result yet; pass 2 gives it one.
        self.prepared: dict[str, dict] = {}

    def _cache_path(self, mesh: Mesh) -> str:
        return decimated_path(self.config.input, mesh.path, self.config.max_faces)

    def _estimate(self, mesh: Mesh) -> int:
        """This pass's reservation for `mesh` (libs/jobmemory.py)."""
        if self.mode == 'prepare':
            return jobmemory.prepare_bytes(mesh.triangles or 0)
        handoff = self.prepared.get(mesh.path)
        if handoff is not None:
            return handoff['estimate_bytes']
        # A repair pass without a prepare pass (a runner driven directly):
        # the same reservation a prepare child would get, at least.
        return jobmemory.prepare_bytes(mesh.triangles or 0)

    def _handoff(self, result: dict) -> None:
        """`report_fn` for a successful prepare: record it for pass 2.
        Not a job report — the job's one terminal result comes later."""
        mesh = result['mesh']
        with self._results_lock:
            self.prepared[mesh.path] = result
        self._log(f'[prepared] {mesh.path}: {result["reason"]}')

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
        estimate = self._estimate(mesh)
        token = None
        try:
            token, refusal, override = self.run_state.start(estimate, self.config.memory_budget_bytes)
            if token is None:
                return None
            self.queue.popleft()
            if override:
                try:
                    _log_line(f'admitting {mesh.path} alone: estimated {estimate}B '
                             f'exceeds budget {self.config.memory_budget_bytes}B')
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
                    confirmed, term_detail = terminate_and_confirm(proc, self.config.reap_deadline)
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
        handoff = self.prepared.get(result['mesh'].path) if self.mode == 'repair' else None
        if handoff is not None:
            # One terminal result per job: it carries both passes.
            result['steps'] = tuple(handoff.get('steps', ())) + tuple(result.get('steps', ()))
            if 'elapsed_seconds' in result and handoff.get('elapsed_seconds') is not None:
                result['elapsed_seconds'] += handoff['elapsed_seconds']
        self.run_state.complete_once(token, self._report, result)

    def _start_model_log(self, mesh: Mesh):
        """Write this attempt's header to the model log and return the log
        opened for binary append, for the child's stdout/stderr — or None,
        with a visible warning, when either step fails. Both happen here, in
        one guarded place, so logging can never stop a repair."""
        path = modellog.path_for(mesh.destination, _reserved_logs(self.config))
        try:
            modellog.write_header(path, self.run_id, 'repair', mesh.path)
            return open(path, 'ab')
        except OSError as exc:
            self._log(f'[warning] cannot write model log {path}: {exc}; '
                      f'tool output for {mesh.path} will not be logged')
            return None

    def _run_one(self, token, mesh):
        fd, result_file = tempfile.mkstemp(prefix='.stlfix-result-', suffix='.json')
        os.close(fd)
        try:
            output_log = self._start_model_log(mesh)
            mode = self.mode
            cache_path = self._cache_path(mesh)
            handoff = self.prepared.get(mesh.path) if mode == 'repair' else None
            try:
                # The repair pass loads what prepare saved and gets
                # max_faces 0: the initial decimation already ran there.
                proc = _spawn_child(self.python, self.script, mesh,
                                   0 if handoff is not None else self.config.max_faces,
                                   result_file, self.config.log_file or None,
                                   skip_clean=self.config.skip_clean,
                                   output_log=output_log,
                                   reconstruct_budget_bytes=runconfig.budget_bytes(
                                       self.config.reconstruct_memory_budget_gb),
                                   min_shell_faces=self.config.min_shell_faces,
                                   mode=mode,
                                   cache_path=cache_path if mode == 'prepare' else None,
                                   load_from=(handoff['prepared_path']
                                              if handoff is not None else None))
            except Exception as exc:
                if output_log is not None:
                    output_log.close()
                self.run_state.mark_launch_failed(token)
                self._complete(token, _build_launch_failure_result(mesh, exc))
                return
            if output_log is not None:
                output_log.close()          # the child holds its own copy

            if not self.run_state.spawned(token, proc):
                confirmed, detail = terminate_and_confirm(proc, self.config.reap_deadline)
                if not confirmed:
                    self.run_state.mark_stuck(token, detail)
                    return
                self.run_state.mark_reaped(token)
                self._complete(token, _build_cancelled_result(mesh))
                return

            self._log(f'[start] {mesh.path} ({mode})')
            cause = 'exited'
            try:
                proc.communicate(timeout=self.config.per_file_timeout)
            except subprocess.TimeoutExpired:
                cause = 'timed_out'
            except Exception:
                cause = 'wait_error'

            confirmed, detail = terminate_and_confirm(proc, self.config.reap_deadline)
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
            result = childresult.read_and_validate(
                result_file, mesh.path, mode=mode,
                expected_prepared=(expected_prepared_path(mesh, cache_path, self.config.max_faces)
                                   if mode == 'prepare' else None))
            if result is not None and result.is_handoff:
                self.run_state.complete_once(token, self._handoff, {
                    'mesh': mesh, 'prepared_path': result.prepared_path,
                    'estimate_bytes': result.estimate_bytes, 'steps': result.steps,
                    'reason': result.reason, 'elapsed_seconds': self._elapsed(token)})
                return
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


def _reserved_logs(config: RunConfig) -> tuple[str, ...]:
    """The run's own log files, which no model log may share."""
    reserved = [_progress_log_path(config), os.path.join(config.output, 'batch.log'),
                os.path.join(config.output, 'progress.log')]
    if config.log_file:
        reserved.append(config.log_file)
    return tuple(reserved)


def _convert_logged(source: str, export: str, *, model_destination: str,
                    runner: blender.Runner, run_id: str,
                    reserved: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Intake conversion, with Blender's output appended to the model log.

    Writes a 'conversion' header to the log beside `model_destination`, then
    lets `blender.convert` copy Blender's output there after the run. A log
    that cannot be written only loses the logging, with a visible warning;
    it never stops or repeats the conversion.
    """
    log = modellog.path_for(model_destination, reserved)
    try:
        modellog.write_header(log, run_id, 'conversion', source)
    except OSError as exc:
        _log_line(f'[warning] cannot write model log {log}: {exc}; '
                  f'conversion output for {source} will not be logged')
        log = None
    return blender.convert(source, export, runner=runner, output_log=log)


def _progress_log_path(config: RunConfig) -> str:
    """`progress.log` beside the step log — `<output>/progress.log` by
    default, or beside a customized `log_file` (spec 5d's Location note)."""
    log_dir = os.path.dirname(config.log_file) if config.log_file else config.output
    return os.path.join(log_dir or config.output, 'progress.log')


def _dispatch(runner: "_Runner", config: RunConfig) -> bool:
    """Run one pass of `runner.queue` through the pool; True if Ctrl+C
    interrupted it (every child is cancelled and waited for, bounded)."""
    old_handler = signal.getsignal(signal.SIGINT)
    try:
        Pool(config.workers, runner.selector, runner.handler).start()
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            runner.run_state.cancel('SIGINT')
            deadline = time.monotonic() + 60.0
            while runner.run_state.has_unresolved() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            signal.signal(signal.SIGINT, old_handler)
        return True
    return False


def _run(config: RunConfig) -> int:
    emitted: list[Mesh] = []
    run_id = str(uuid.uuid4())

    # Constructed BEFORE `converter.prepare` runs — i.e. before intake
    # starts — so intake-stage messages and the intake-exception
    # early-return path below can use it too (spec 5d).
    reporter = ProgressReporter(_progress_log_path(config), run_id)
    reporter.write({'kind': 'run_start', 'run_id': run_id,
                    'output': config.output, 'max_faces': config.max_faces})

    def collect(mesh: Mesh) -> None:
        emitted.append(mesh)

    scanning_message = f'Scanning {config.input} ...'
    _log_line(scanning_message)
    reporter.write({'kind': 'progress', 'message': scanning_message})
    # `intake_runner` is shared across every conversion `converter.prepare`
    # drives during this call — `own_process_group=True` by default: intake
    # is definitionally top-level, never nested inside another
    # `proctree`-managed group. Sharing one `Runner` (rather than the
    # per-call default `blender.convert` would otherwise construct) is what
    # lets a single `cancel()` reach every conversion in flight, including
    # ones launched after this call started but before it returns.
    intake_runner = blender.Runner()
    try:
        summary = converter.prepare(
            config.input, config.output, collect,
            copy_extensions={'.png', '.jpg', '.jpeg', '.gif', '.txt', ".pdf", ".tif", ".tiff", ".url", ".webp"},
            convert=functools.partial(_convert_logged, runner=intake_runner, run_id=run_id,
                                      reserved=_reserved_logs(config)),
            workers=1,
            mesh_extensions=converter.MESH_EXTENSIONS,
        )
    except KeyboardInterrupt:
        # Reuses the EXISTING pattern the main dispatch loop below already
        # applies around its own `Pool(...).start()` call, applied here to
        # `converter.prepare`'s internal `Pool.start()`. `Pool.start()`'s own
        # docstring: `KeyboardInterrupt` (main-thread-only signal delivery)
        # propagates out of `converter.prepare` uncaught, deliberately.
        intake_runner.cancel()
        idle = intake_runner.wait_for_idle(60.0)   # same 60s constant the
                                                    # dispatch loop's own
                                                    # post-cancel wait uses
        if not idle:
            intake_runner.reap_unresolved(10.0)
            idle = intake_runner.wait_for_idle(0.0)
        message = (f'Intake interrupted; Blender cleanup '
                  f"{'confirmed' if idle else 'NOT confirmed within budget'}.")
        _log_line(message)
        reporter.write({'kind': 'progress', 'message': message})
        # `collect` (this closure) may still be invoked by the daemon worker
        # thread AFTER this function returns via the early return below —
        # CONFIRMED HARMLESS: `collect`/`emit_one` only touch the local
        # `emitted` list, never `ProgressReporter`/any finalized-reporting
        # write (those only happen in this function's OWN code, after
        # `converter.prepare` returns) — so a late callback call cannot race
        # any reporting write. `Pool`'s own philosophy ("killing mid-work is
        # safe... next run redoes it") already covers the abandoned pending
        # item itself; no attempt is made here to join the daemon worker
        # thread (no API exists to do so without changing `Pool`, out of
        # scope).
        reporter.finalize({'kind': 'final', 'incomplete': True,
                           'intake_interrupted': True, 'cleanup_confirmed': idle})
        return 1   # early return — NEVER reaches _preflight/dispatch: an
                   # interrupted intake must not proceed to repair dispatch.
    except blender.BlenderCleanupUnconfirmed as error:
        # A conversion's own cleanup came back unconfirmed even without a
        # KeyboardInterrupt. `converter.prepare`'s own internal
        # `try/except Exception` around `convert_one` already converts this
        # (like any other raised exception) into the standard
        # `_conversion_failure` path FOR THAT ONE FILE — so this outer catch
        # cannot actually be reached from inside the pool-driven conversion
        # phase; it exists only for defense (e.g. a future call site that
        # invokes `blender.convert` directly, outside `convert_one`'s guard)
        # and to stop further intake launches via `cancel()`, not to
        # duplicate a failure `converter.prepare` already recorded.
        intake_runner.cancel()
        idle = intake_runner.wait_for_idle(60.0)
        if not idle:
            intake_runner.reap_unresolved(10.0)
            idle = intake_runner.wait_for_idle(0.0)
        message = (f'Run incomplete: intake raised '
                  f'{type(error).__name__}: {error}; Blender cleanup '
                  f"{'confirmed' if idle else 'NOT confirmed within budget'}.")
        _log_line(message)
        reporter.write({'kind': 'progress', 'message': message})
        reporter.finalize({'kind': 'final',
                           'intake_exception': f'{type(error).__name__}: {error}',
                           'incomplete': True, 'cleanup_confirmed': idle})
        return 1
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
    runner = _Runner(config, python, CHILD_SCRIPT, baselines, reporter=reporter,
                     run_id=run_id)
    runner.total_jobs = len(dispatchable)
    runner.queue.extend(dispatchable)
    _log_line(f'Intake done: {len(dispatchable)} job(s) to process '
             f'({len(rejected)} rejected at intake), {config.workers} worker(s).')

    # Two passes over the same runner (docs/refactor/orchestration.md):
    # prepare (initial decimation, cache, exact estimate), then repair, each
    # job reserved with its own pass's estimate. Sequential, so no child ever
    # waits for a bigger reservation while holding one.
    runner.mode = 'prepare'
    interrupted = _dispatch(runner, config)
    stopped = (interrupted or runner.run_state.is_cancelled()
               or runner.run_state.has_incomplete_reason() or runner.run_state.has_unresolved())
    if not stopped and runner.prepared:
        runner.mode = 'repair'
        runner.queue.extend(sorted((h['mesh'] for h in runner.prepared.values()),
                                   key=lambda m: runner.prepared[m.path]['estimate_bytes']))
        barrier = (f'Prepare done: {len(runner.prepared)} job(s) to repair, '
                   f'{len(runner.results)} finished in prepare.')
        _log_line(barrier)
        reporter.write({'kind': 'progress', 'message': barrier})
        interrupted = _dispatch(runner, config)
    finished = {result['mesh'].path for result in runner.results}
    left_prepared = [path for path in runner.prepared if path not in finished]

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
                  or runner.run_state.has_unresolved() or bool(runner.queue)
                  or bool(left_prepared))
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
    # A prepared job still queued for repair is already in `left_in_queue`.
    queued = {mesh.path for mesh in runner.queue}
    prepared_only = len([path for path in left_prepared if path not in queued])
    incomplete_reason = runner.run_state.incomplete_reason()
    if incomplete:
        print(f'Run INCOMPLETE: {cancelled_count} job(s) interrupted and cleanly '
              f'reaped, {len(unresolved)} unresolved job(s), '
              f'{left_in_queue} still queued and undispatched, '
              f'{prepared_only} prepared but never queued for repair — all are eligible '
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
        'prepared_not_repaired': prepared_only,
    })
    return int(incomplete or bool(diagnostics) or summary.copy_failed > 0)


def _fail(message: str) -> int:
    print(f'batch_repair: error: {message}', file=sys.stderr)
    return 2


def _check_environment(config: RunConfig) -> str | None:
    """What the filesystem and installed tools must satisfy before `_run`.

    Returns a message for the first problem, or None. Runs before any
    output or log file is created.
    """
    if not os.path.isdir(config.input):
        return f'input must exist and be a directory: {config.input}'
    if os.path.isfile(config.output):
        return f'output must be a directory, not an existing file: {config.output}'
    try:
        source = Path(config.input).resolve()
        destination = Path(config.output).resolve()
    except (OSError, RuntimeError) as error:
        return f'cannot resolve input/output paths: {error}'
    if (source == destination or source in destination.parents
            or destination in source.parents):
        return 'input and output must not overlap in either direction'

    # Libraries: one line each (`libs.dependencies`); a missing required
    # package usually failed already when this module imported it.
    missing = dependencies.check_all(_log_line)
    if missing:
        return 'missing required libraries: ' + ', '.join(missing)
    return None


def _main(argv) -> int:
    if argv:
        return _fail(f'batch_repair.py takes no arguments (got: {" ".join(argv)}); '
                     f'set options in {CONFIG_PATH}')
    if not os.path.exists(CONFIG_PATH):
        return _fail(f'config file not found: {CONFIG_PATH}\n'
                     f'Copy {EXAMPLE_CONFIG_PATH} to it and edit the copy.')
    try:
        config = runconfig.resolve(runconfig.load(CONFIG_PATH))
    except ConfigError as error:
        return _fail(f'{CONFIG_PATH}: {error}')
    problem = _check_environment(config)
    if problem is not None:
        return _fail(f'{CONFIG_PATH}: {problem}')
    return _run(config)


def main(argv=None):
    """CLI boundary: interruption never reports a completed run.

    `argv=None` means the real command line; a test passes a list.
    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _main(argv)
    except KeyboardInterrupt:
        _log_line('Run interrupted/incomplete.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
