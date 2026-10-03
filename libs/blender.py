"""Run headless Blender with a deadline and captured output, optionally
copying that output to a log after each run."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import mesh_io, proctree
from .mesh_io import Mesh


@dataclass(frozen=True)
class Result:
    """What one Blender invocation produced.

    exit_code       process exit code, or None if it was killed
    stdout_capture  captured output — the caller's protocol lives in here
    stderr_capture  captured errors
    is_timed_out    True when the deadline was hit and the process killed
    second_elapsed  wall time actually spent, including a killed run
    cleanup_confirmed  True/False when this Runner owns the process group
                    (whether every descendant was confirmed gone within
                    budget); None in delegated mode (own_process_group=False),
                    where group confirmation is not this Runner's job.
    cleanup_errors  every error `_cleanup` collected, as strings — may be
                    non-empty even when `cleanup_confirmed` is True (an
                    error that was still resolved before the deadline).

    A killed run still spent its time and still cost a launch, so
    `second_elapsed` is filled either way; callers that accumulate cost should
    use it rather than timing the call themselves, which would miss the kill
    path.
    """

    exit_code: int | None
    stdout_capture: str
    stderr_capture: str
    is_timed_out: bool
    second_elapsed: float
    cleanup_confirmed: bool | None = None
    cleanup_errors: tuple[str, ...] = ()


class RunCancelled(Exception):
    """Raised by `run()` when the owning `Runner` has been (or becomes)
    cancelled — either before launch even begins, or while the launch was
    still in flight when `cancel()` arrived."""

    def __init__(self, message: str, cleanup_errors: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.cleanup_errors = cleanup_errors


class BlenderCleanupUnconfirmed(Exception):
    """Raised by `convert()`/`repair()` when the underlying `Runner.run()`
    came back with `Result.cleanup_confirmed is False` — a descendant was not
    confirmed dead within the cleanup budget. Raised regardless of whether
    the conversion/repair itself otherwise reported `ok=True`."""

    def __init__(self, result: Result, destination: str | None = None) -> None:
        super().__init__(
            f"Blender cleanup not confirmed"
            + (f" for {destination}" if destination is not None else ""))
        self.result = result
        self.destination = destination


class _Stdio:
    def __repr__(self) -> str:
        return 'blender.STDIO'


#: `output_log` value meaning "write Blender's captured output to this
#: process's own stdout/stderr". Used inside a repair child, whose fd 1/2 are
#: already the model's log, so no log path has to travel through the pipeline.
STDIO = _Stdio()


def _write_output(output_log: "str | _Stdio | None", stdout: str | None,
                  stderr: str | None) -> None:
    """Append one Blender run's captured output to `output_log`.

    Called after the run, on every path that has output — Blender keeps its
    own PIPE capture because success is decided from its stdout, so this is
    a copy, never the evidence. Never raises: a log that cannot be written
    must not change the run's outcome or cause a rerun. A failure is reported
    on stderr, and if even that fails it is dropped.
    """
    if output_log is None:
        return
    out = f'---- blender stdout ----\n{stdout or ""}'
    err = f'---- blender stderr ----\n{stderr or ""}'
    out, err = (t if t.endswith('\n') else t + '\n' for t in (out, err))
    try:
        if output_log is STDIO:
            os.write(1, out.encode('utf-8', 'replace'))
            os.write(2, err.encode('utf-8', 'replace'))
        else:
            with open(output_log, 'a', encoding='utf-8', errors='replace') as handle:
                handle.write(out + err)
    except Exception as exc:
        try:
            os.write(2, f'blender: could not write output log {output_log!r}: {exc}\n'
                        .encode('utf-8', 'replace'))
        except Exception:
            pass


#: One shared cleanup budget: computed once per `_cleanup` invocation as an
#: absolute deadline, then every stage inside it derives its own remaining
#: time from that SAME deadline — never a fresh full budget per stage.
CLEANUP_BUDGET_DEFAULT = 30.0


def _lift_address_space_limit() -> None:
    """preexec_fn: undo an RLIMIT_AS inherited from the caller.

    RLIMIT_AS caps *virtual* address space, not resident memory, and it is
    inherited across fork/exec.  Blender reserves far more VA than it ever
    resides — thread stacks, mmap'd arenas, driver mappings — so a cap sized
    for a Python worker's own allocations aborts it at a fraction of that:
    observed as `Malloc returns null: ... total 1.2 GB` under a 3 GiB cap with
    14 GB of real memory free.

    Runs between fork and exec, so it must stay async-signal safe: no logging,
    no allocation beyond the resource call itself.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if soft != resource.RLIM_INFINITY:
            resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, hard))
    except Exception:
        pass        # a child that keeps the cap is still better than no child


@dataclass
class _RunRecord:
    """Per-run bookkeeping, tracked under `Runner._lock` for the lifetime of
    one `run()` call.

    proc               the `Popen`, or `None` before it has been published
                       (registered-but-not-yet-launched, or launch failed).
    cancelled          set by `cancel()` if it saw this record before `proc`
                       was published — `run()` checks this right after
                       publishing to catch a launch-race cancel.
    cleanup_confirmed  the last `_cleanup` outcome for this record, or
                       `None` if cleanup has not run (or is in delegated
                       mode and direct-child reap has not yet succeeded).
    relinquished       True once the record's own waiter thread has finished
                       its own `_cleanup`/handling and will never touch this
                       record's pipes again — the record is then eligible for
                       `reap_unresolved` to retry independently.
    """

    proc: subprocess.Popen | None
    cancelled: bool
    cleanup_confirmed: bool | None
    relinquished: bool = False


class Runner:
    """Launches Blender, tracking every concurrently in-flight run.

    A plain function would be enough to run a script, but a signal handler
    needs to reach a Blender that is already in flight — so the handle has to
    live somewhere.  Holding it on an instance rather than in a module global
    means two runners do not fight, and the caller decides what is shared.

    The instance is safe to use from several threads: `run()` genuinely
    supports concurrent calls on one instance, each tracked under its own
    `_RunRecord` in `self._active`, keyed by an incrementing `run_id`.

    `own_process_group=True` (the default) makes every unmodified direct
    caller (`convert()`, `repair()`, a bare `Runner()`) launch Blender in its
    own session (`start_new_session=True`) and kill/confirm via the whole
    group on any exit path — closing the gap where a descendant Blender
    spawns (e.g. via a driver subprocess) and outlives a killed or even
    cleanly-exited parent. `own_process_group=False` is for a caller that
    KNOWS this Blender invocation runs nested inside another, enclosing
    `proctree`-managed process group (e.g. a batch worker) — giving it its
    own session would take it OUT of that enclosing group, and the worker's
    own `terminate_and_confirm` group-kill would then never reach it.
    """

    def __init__(self, executable: str = 'blender',
                own_process_group: bool = True) -> None:
        self.executable = executable
        self.own_process_group = own_process_group
        self._lock = threading.Lock()
        self._active: dict[int, _RunRecord] = {}
        self._next_run_id: int = 0
        self._cancel_requested: bool = False

    # ------------------------------------------------------------------
    # Cleanup — one function, called from every exit path.
    # ------------------------------------------------------------------

    def _confirm_group_dead(self, pgid: int, deadline_seconds: float) -> bool:
        """Bounded polling loop around `proctree.live_group_members`,
        reusing `terminate_and_confirm`'s own poll-until-deadline SHAPE —
        not a single one-shot read."""
        deadline_at = time.monotonic() + deadline_seconds
        while True:
            live, uncertain = proctree.live_group_members(pgid)
            if live == 0 and uncertain == 0:
                return True
            if time.monotonic() >= deadline_at:
                return False
            time.sleep(0.02)

    def _cleanup(self, proc: subprocess.Popen, deadline: float,
                already_captured: tuple[str, str] | None = None,
                ) -> tuple[bool | None, list[Exception], str, str]:
        """Returns (cleanup_confirmed, errors, stdout, stderr).

        Every stage is independently try/except'd and chained — a failure in
        one stage (e.g. the kill signal itself raising) does NOT skip later
        stages (drain, reap, confirm). Every error encountered is collected
        in `errors`, not just the first.
        """
        errors: list[Exception] = []

        # Stage 1: signal delivery — ALWAYS attempted when own_process_group,
        # regardless of the leader's own exit status (a descendant can
        # survive a cleanly-exited Blender). Not gated on proc.poll().
        try:
            if self.own_process_group:
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.poll() is None:
                proc.kill()
        except ProcessLookupError:
            pass   # already gone — success, not an error
        except Exception as exc:
            errors.append(exc)

        # Stage 2: drain — keyed on whether output was ALREADY captured by
        # the caller, never on proc.poll(). A process reaped via wait()
        # without communicate() still needs its pipes drained/closed.
        if already_captured is not None:
            stdout, stderr = already_captured
        else:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                stdout, stderr = proc.communicate(timeout=remaining)
            except Exception as exc:
                stdout = stderr = ''
                errors.append(exc)
                # Close on ANY drain failure, not only TimeoutExpired — the
                # owning waiter (this thread, the only one that ever touches
                # these pipes) explicitly relinquishes them rather than
                # leaving them open indefinitely.
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception as close_exc:
                        errors.append(close_exc)

        # Stage 3: direct-child reap — independent of drain outcome (a drain
        # timeout does not by itself mean the CHILD is stuck; a descendant
        # holding the pipes open is a different failure than the child
        # itself not exiting).
        direct_child_reaped = proc.poll() is not None
        if not direct_child_reaped:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
                direct_child_reaped = True
            except subprocess.TimeoutExpired:
                errors.append(RuntimeError(
                    f'direct child (pid {proc.pid}) not reaped within the '
                    f'remaining {remaining:.1f}s budget'))

        # Stage 4: group confirmation — bounded POLLING loop, only when this
        # Runner owns the process group.
        group_confirmed_empty: bool | None = None
        if self.own_process_group:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                group_confirmed_empty = self._confirm_group_dead(proc.pid, remaining)
            except Exception as exc:
                errors.append(exc)
                group_confirmed_empty = False
            if group_confirmed_empty is False:
                errors.append(RuntimeError(
                    f'process group {proc.pid} not confirmed empty within budget'))

        cleanup_confirmed = (
            None if not self.own_process_group else
            (direct_child_reaped and bool(group_confirmed_empty)))

        return cleanup_confirmed, errors, stdout, stderr

    def _record_is_resolved(self, record: _RunRecord) -> bool:
        """Whether `record` no longer needs to be tracked in `self._active`.

        Delegated mode (own_process_group=False): resolved iff the direct
        child was reaped — `_cleanup` reports this via `cleanup_confirmed`
        being anything other than `None` is NOT how delegated mode signals
        reap; delegated mode's `cleanup_confirmed` is always `None`, so
        resolution there is decided by the process itself having exited.
        Owned mode: resolved iff `cleanup_confirmed is True`.
        """
        if not self.own_process_group:
            proc = record.proc
            return proc is not None and proc.poll() is not None
        return record.cleanup_confirmed is True

    def _finish_record(self, run_id: int, cleanup_confirmed: bool | None) -> None:
        with self._lock:
            record = self._active.get(run_id)
            if record is None:
                return
            record.cleanup_confirmed = cleanup_confirmed
            record.relinquished = True
            if self._record_is_resolved(record):
                del self._active[run_id]

    # ------------------------------------------------------------------
    # Launch / run.
    # ------------------------------------------------------------------

    def run(self, script: str, timeout: float,
            output_log: "str | _Stdio | None" = None) -> Result:
        """Write `script` to a temp file, run it headless, return what
        happened.

        `output_log`: a path to append Blender's captured stdout/stderr to,
        `STDIO` for this process's own stdout/stderr, or None (nothing
        written). Written after the run — including timeout, cancellation
        and interruption, whatever output cleanup captured — and never
        affects the Result or the exception raised.

        The temp file is always removed. Every exit path — timeout, any
        other exception (including `KeyboardInterrupt`), and ordinary
        success — runs `_cleanup` exactly once and records the outcome
        against this run's own `_RunRecord` before returning/raising.
        """
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         delete=False) as tmp:
            tmp.write(script)
            script_path = tmp.name
        try:
            return self._run_launched(script_path, timeout, started, output_log)
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    def _run_launched(self, script_path: str, timeout: float,
                      started: float,
                      output_log: "str | _Stdio | None" = None) -> Result:
        with self._lock:
            if self._cancel_requested:
                raise RunCancelled("Runner was already cancelled")
            run_id = self._next_run_id
            self._next_run_id += 1
            self._active[run_id] = _RunRecord(proc=None, cancelled=False,
                                              cleanup_confirmed=None)

        try:
            proc = subprocess.Popen(
                [self.executable, '--background', '--python', script_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=_lift_address_space_limit,
                start_new_session=self.own_process_group,
            )
        except BaseException:
            # BaseException, not Exception — a KeyboardInterrupt arriving
            # DURING Popen itself must still release the reservation, not
            # leave a stale record with proc=None forever.
            with self._lock:
                del self._active[run_id]
            raise

        with self._lock:
            record = self._active[run_id]
            record.proc = proc          # ALWAYS published, unconditionally —
                                         # a normal, uncancelled run must also
                                         # become visible to a LATER cancel()
                                         # call while communicate() runs.
            was_cancelled = record.cancelled

        if was_cancelled:
            # A `cancel()` call ran between registration and publish: it saw
            # `proc is None` and could only flag, not kill. Kill now.
            deadline = time.monotonic() + CLEANUP_BUDGET_DEFAULT
            cleanup_confirmed, errors, out, err = self._cleanup(proc, deadline)
            _write_output(output_log, out, err)
            with self._lock:
                record.cleanup_confirmed = cleanup_confirmed
                record.relinquished = True
                if self._record_is_resolved(record):
                    del self._active[run_id]
            raise RunCancelled(
                "Runner was cancelled while this run was launching",
                cleanup_errors=tuple(str(e) for e in errors))

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            deadline = time.monotonic() + CLEANUP_BUDGET_DEFAULT
            cleanup_confirmed, errors, out2, err2 = self._cleanup(proc, deadline)
            self._finish_record(run_id, cleanup_confirmed)
            _write_output(output_log, out2, err2)
            return Result(exit_code=None, stdout_capture=out2, stderr_capture=err2,
                         is_timed_out=True, second_elapsed=time.monotonic() - started,
                         cleanup_confirmed=cleanup_confirmed,
                         cleanup_errors=tuple(str(e) for e in errors))
        except BaseException:
            # Covers KeyboardInterrupt (a BaseException — a plain `except
            # Exception` would let this slip past with NO cleanup at all)
            # and any other real exception from communicate() itself.
            deadline = time.monotonic() + CLEANUP_BUDGET_DEFAULT
            cleanup_confirmed, errors, out, err = self._cleanup(proc, deadline)
            self._finish_record(run_id, cleanup_confirmed)
            _write_output(output_log, out, err)
            exc = sys.exc_info()[1]
            for e in errors:
                try:
                    exc.add_note(f'cleanup also failed: {e}')   # 3.11+
                except Exception:
                    pass
            raise   # bare `raise` — the ORIGINAL exception propagates
                    # unmodified; cleanup failures are attached as notes,
                    # never replace it, never become the primary exception.
        else:
            deadline = time.monotonic() + CLEANUP_BUDGET_DEFAULT
            # Ordinary exit: this communicate() already drained/captured
            # stdout/stderr successfully. _cleanup is still called — but
            # ONLY for its group-kill-regardless-of-leader-exit-code side
            # effect (a descendant can outlive a cleanly-exited Blender) —
            # passing the ALREADY-CAPTURED output so _cleanup never re-reads
            # the pipes.
            cleanup_confirmed, errors, _, _ = self._cleanup(
                proc, deadline, already_captured=(stdout, stderr))
            self._finish_record(run_id, cleanup_confirmed)
            _write_output(output_log, stdout, stderr)
            return Result(exit_code=proc.returncode, stdout_capture=stdout,
                         stderr_capture=stderr, is_timed_out=False,
                         second_elapsed=time.monotonic() - started,
                         cleanup_confirmed=cleanup_confirmed,
                         cleanup_errors=tuple(str(e) for e in errors))

    # ------------------------------------------------------------------
    # Cancellation.
    # ------------------------------------------------------------------

    def _kill_signal_only(self, proc: subprocess.Popen) -> None:
        try:
            if self.own_process_group:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass   # best-effort from cancel(); _cleanup's own kill stage
                   # (run on the WAITER's own thread) will retry and record
                   # any error.

    def cancel(self) -> None:
        """Permanently cancel this Runner: no future `run()` call will
        launch, and every CURRENTLY tracked run (launching or in-flight) is
        killed. Once called, cancellation stays permanent — there is no
        un-cancel."""
        with self._lock:
            self._cancel_requested = True
            for record in self._active.values():
                record.cancelled = True
                if record.proc is not None:
                    self._kill_signal_only(record.proc)

    def kill_current(self) -> bool:
        """Kill an arbitrary one currently-active run, best-effort — kept
        UNCHANGED in behavior/signature for its one real caller (a signal
        handler wanting "kill whatever's running"). With today's only real
        usage pattern (one concurrent run), this is identical to the old
        `_current`-based behavior. Returns True if something was targeted.

        Does NOT set cancellation — only `cancel()` does. This preserves
        `kill_current`'s existing, weaker, one-shot semantic for its
        existing caller while `cancel()` is the new, stronger, permanent
        operation intake uses.
        """
        with self._lock:
            for record in self._active.values():
                if record.proc is not None:
                    self._kill_signal_only(record.proc)
                    return True
            return False

    # ------------------------------------------------------------------
    # Bounded shutdown wait, for intake.
    # ------------------------------------------------------------------

    def wait_for_idle(self, deadline_seconds: float) -> bool:
        """Poll until `self._active` is empty or the deadline passes.
        Returns whether it actually reached empty."""
        deadline_at = time.monotonic() + deadline_seconds
        while True:
            with self._lock:
                if not self._active:
                    return True
            if time.monotonic() >= deadline_at:
                return False
            time.sleep(0.02)

    def reap_unresolved(self, deadline_seconds: float) -> list[int]:
        """Retry cleanup for every RELINQUISHED, still-unresolved record —
        never touches a record whose own waiter thread might still be
        reading its pipes (`relinquished=False`). Retries signal delivery,
        direct-child reap, AND group confirmation (a previously-failed kill
        signal or previously-timed-out wait() may succeed now) —
        recomputes the FULL conjunction fresh each retry, not just the
        group sweep. ONE shared deadline across every record retried in
        this call. Returns the PIDs still unresolved after this attempt.
        """
        deadline_at = time.monotonic() + deadline_seconds
        with self._lock:
            candidates = [(run_id, record) for run_id, record in self._active.items()
                         if record.relinquished and record.cleanup_confirmed is not True]
        unresolved_pids: list[int] = []
        for run_id, record in candidates:
            proc = record.proc
            remaining = max(0.0, deadline_at - time.monotonic())
            cleanup_confirmed, errors, _, _ = self._cleanup(
                proc, time.monotonic() + remaining, already_captured=('', ''))
            with self._lock:
                if run_id not in self._active:
                    continue   # already resolved/removed by someone else meanwhile
                self._active[run_id].cleanup_confirmed = cleanup_confirmed
                resolved = self._record_is_resolved(self._active[run_id])
                if resolved:
                    del self._active[run_id]
                else:
                    unresolved_pids.append(proc.pid)
        return unresolved_pids


#: Where the Blender scripts live.  They are data, not modules — read at
#: import time, rendered with `str.format`, and handed to Blender.  Kept as
#: files rather than string literals because a Blender script is Python that an
#: editor should be able to read, and burying it in a quoted block makes it
#: unreadable and unlintable.
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'blender_fx')


def load_script(name: str) -> str:
    """Read `<name>.blender` from the script folder."""
    with open(os.path.join(SCRIPT_DIR, f'{name}.blender')) as f:
        return f.read()


CONVERT_SCRIPT = load_script('convert')

#: The repair script, lifted from the frozen `stl_batch_fix.blender` with two
#: of its six steps disabled — see the comments at those sites in
#: `blender_fx/repair.blender`.  Both were measured to be redundant or wrong
#: once the mesh arrives through `repairer`:
#:
#:   step 3, T-junction split  scans NON-MANIFOLD edges; a T-junction produces
#:                             OPEN edges, so it found 0 of 80 on the
#:                             all-defects sphere.  `welder` owns this.
#:   step 4, normal vote       compares each face's normal against the stored
#:                             one, but `mesh_io.write` derives normals from
#:                             the winding, so they always agree: measured
#:                             "agree: 801, disagree: 0".
#:
#: What remains is the hole-filling repair loop, which is what Blender is
#: actually better at: given a prepared single-shell part it reaches the
#: all-defects sphere at 840f/+4094.9 against PyMeshFix's 836f/+4092.9, and
#: `fin` at 760f/100.00% volume losing only the fin's own apex.
REPAIR_SCRIPT = load_script('repair')


def convert(source: str, destination: str, timeout: float = 600,
            executable: str = 'blender', *,
            runner: "Runner | None" = None,
            output_log: "str | _Stdio | None" = None) -> tuple[bool, str]:
    """Convert `source` to a binary STL at `destination`.

    Accepts OBJ or STL in either encoding; always writes binary STL.  A
    lossless container change — same triangles, same coordinates — so it is
    generic Blender work rather than anything this project invented, which is
    why it lives here and the repair and decimation scripts do not.

    Returns `(ok, destination)`.  The full `Result` is deliberately not
    returned: a caller almost always wants to know whether the file is there
    now, and anyone who needs stdout can render `CONVERT_SCRIPT` and call
    `Runner.run` directly.

    The script writes to `<destination>.partial` and renames, so an interrupted
    export cannot leave a file that a later run mistakes for a finished one —
    existence is what decides whether an export is reused.

    `runner`, when supplied, is used instead of constructing a fresh
    `Runner(executable)` — letting a caller (e.g. intake) share one `Runner`
    across many conversions so `cancel()` reaches all of them.

    `output_log` is passed to `Runner.run` (Blender's output copied there
    after the run). Success is still decided from the captured stdout only.

    Raises `BlenderCleanupUnconfirmed` whenever the underlying `Runner.run()`
    result has `cleanup_confirmed is False` — regardless of whether the
    conversion itself would otherwise report `ok=True` or `ok=False`.
    `cleanup_confirmed is None` (delegated mode) never raises this.
    """
    script = CONVERT_SCRIPT.format(src=source, dst=destination)
    active_runner = runner if runner is not None else Runner(executable)
    result = active_runner.run(script, timeout=timeout, output_log=output_log)
    ok = (not result.is_timed_out
          and result.exit_code == 0
          and 'BLENDER_CONVERT_OK' in result.stdout_capture)
    if result.cleanup_confirmed is False:
        raise BlenderCleanupUnconfirmed(result, destination)
    return ok, destination


def repair(source: str, destination: str,
           timeout: float = 600, executable: str = 'blender', *,
           runner: "Runner | None" = None,
           output_log: "str | _Stdio | None" = None,
           ) -> tuple[bool, Result]:
    """Run the repair script from binary PLY `source` to `destination`.

    Return `(ok, Result)`. `ok` means Blender wrote output with an accepted exit
    code, not that the result is clean. The caller rescans and judges volume loss.
    Both paths must be PLY; `convert` separately writes binary STL.

    `runner`, when supplied, is used instead of constructing a fresh
    `Runner(executable)`. `output_log` is passed to `Runner.run`.

    Raises `BlenderCleanupUnconfirmed` whenever the underlying `Runner.run()`
    result has `cleanup_confirmed is False`, regardless of `ok`.
    """
    script = REPAIR_SCRIPT.format(src=source, dst=destination)
    active_runner = runner if runner is not None else Runner(executable)
    result = active_runner.run(script, timeout=timeout, output_log=output_log)
    ok = (not result.is_timed_out
          and result.exit_code in (0, 2)
          and os.path.exists(destination))
    if result.cleanup_confirmed is False:
        raise BlenderCleanupUnconfirmed(result)
    return ok, result


#: `repair()`'s own timeout default, reused by `step_blender_repair()` —
#: its signature cannot carry a timeout parameter without breaking the
#: uniform pipeline contract every step shares. Raise this here if a
#: slower machine needs it.
STEP_TIMEOUT = 600


def step_blender_repair(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """`pipeconfig`'s uniform step contract, wrapping `repair()`.

    Takes only a mesh — `timeout` is `STEP_TIMEOUT`, not a parameter, so
    this matches every other step's signature exactly. `repair()`'s own
    `ok` already distinguishes a real failure from an incomplete-but-usable
    result: exit 2 (BLENDER_UNREPAIRED) is accepted here as long as output
    exists, since the script writes its best effort before signalling that
    non-manifold edges remain, rather than discarding it. `ok=False` here
    covers what `repair()` rejects — timeout, an unaccepted exit code, or
    accepted exit code with no output — plus any exception raised anywhere
    in this step (launch failure, PLY I/O, unloaded geometry), caught as a
    whole rather than only around the `repair()` call.

    Reads `config.nested_process_group` (a `pipeconfig.StepConfig` field) to
    decide which mode to construct its own internal `Runner` in: `True`
    means this Blender invocation runs nested inside another, enclosing
    `proctree`-managed process group, so it must NOT get its own session —
    `own_process_group=False`. Absent/`False` (the default; matches
    `Runner`'s own safe-by-default) -> `own_process_group=True`.

    Blender's output is written to this process's own stdout/stderr
    (`STDIO`) after the run. Inside a repair child those are the model's log,
    so the step logs itself without a path travelling through the pipeline.
    """
    try:
        nested = getattr(config, 'nested_process_group', False) if config is not None else False
        runner = Runner(own_process_group=not nested)
        faces_in = len(mesh.geometry.faces)
        with tempfile.TemporaryDirectory(prefix='blender-step-') as folder:
            source = os.path.join(folder, 'part.ply')
            target = os.path.join(folder, 'fixed.ply')
            mesh_io.write_ply(mesh, source)
            ok, result = repair(source, target, timeout=STEP_TIMEOUT, runner=runner,
                                output_log=STDIO)
            if not ok:
                why = ('timed out' if result.is_timed_out
                       else f"exit {result.exit_code}")
                return False, mesh, f"blender failed: {why}"
            # `read_ply` rather than `load`: the vertex table survived the
            # round trip, so there is nothing to weld — which is the whole
            # reason this boundary is PLY. It also carries `mesh`'s identity
            # across, so the repaired geometry comes back attached to the
            # part rather than to a temp file.
            repaired = mesh_io.read_ply(target, mesh)
    except Exception as exc:
        return False, mesh, f"blender failed: {type(exc).__name__}: {exc}"

    marker = next((line for line in result.stdout_capture.splitlines()
                   if line.startswith('BLENDER_')), 'BLENDER_OK')
    return (True, repaired,
            f"blender {faces_in}f -> {repaired.triangles}f "
            f"({marker.split(':')[0]})")


def is_available(executable: str = 'blender') -> bool:
    """True when `executable` can be found and reports a version.

    Cheap enough for a startup check and does not launch a scene.
    """
    try:
        done = subprocess.run([executable, '--version'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=30)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
