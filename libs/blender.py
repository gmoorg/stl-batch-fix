"""Run a script in headless Blender, with a deadline.

Domain-free: this module knows how to launch Blender, wait for it, kill it if
it overruns, and clean up after itself.  It knows nothing about meshes, about
what the script does, or about what its output means.  The caller renders the
script and interprets the result.

    result = run(script_text, timeout=420)
    if result.is_timed_out:
        ...
    elif result.exit_code == 0 and 'MY_MARKER' in result.stdout_capture:
        ...

Two things it deliberately does not do:

**No budget arithmetic.** `timeout` is seconds, supplied by the caller. How
much of a mesh's remaining time Blender may have — and what to reserve for the
steps after it — is pipeline policy that changes with the pipeline.

**No `/proc` walking.** Measured: Blender is a *direct* child of the process
that spawns it and has no children of its own, so `Popen.kill()` reaches it.
Walking `/proc` for grandchildren is what a supervisor needs when it kills an
intermediate process — killing that middle process alone leaves Blender alive
and reparented to init, holding its memory. That belongs with whatever
supervises child processes, not here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    """What one Blender invocation produced.

    exit_code       process exit code, or None if it was killed
    stdout_capture  captured output — the caller's protocol lives in here
    stderr_capture  captured errors
    is_timed_out    True when the deadline was hit and the process killed
    second_elapsed  wall time actually spent, including a killed run

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


class Runner:
    """Launches Blender and keeps track of the one it currently has running.

    A plain function would be enough to run a script, but a signal handler
    needs to reach a Blender that is already in flight — so the handle has to
    live somewhere.  Holding it on an instance rather than in a module global
    means two runners do not fight, and the caller decides what is shared.

    The instance is safe to use from several threads: each `run` publishes its
    own process while it waits and clears it afterwards, under a lock.
    """

    def __init__(self, executable: str = 'blender') -> None:
        self.executable = executable
        self._lock = threading.Lock()
        self._current: subprocess.Popen | None = None

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """Kill `proc`.  The only place a Blender is killed.

        **It does not reap.**  Reaping means `communicate()`, which closes the
        pipes — and when a kill arrives from a signal handler or another
        thread, the thread inside `run` is mid-`os.read` on exactly those
        descriptors.  Measured: it raises `OSError: [Errno 9] Bad file
        descriptor`, that propagates out of `run`, and no `Result` is ever
        built.

        So reaping belongs to whoever is already waiting: the `communicate()`
        in `run` returns as soon as the process dies and reaps it there.  The
        timeout path is the one exception, and calls `communicate()` itself
        immediately after this — it *is* the waiter.
        """
        proc.kill()

    def kill_current(self) -> bool:
        """Kill the Blender running right now, if any.  Safe from a handler.

        Returns True if something was killed — not a `Result`, because the
        thread blocked in `run` is the one that builds it.  Handing one back
        here would mean two callers each holding a result for the same run, and
        a signal handler waiting on a thread to produce it.

        The waiting `run` sees the death as an ordinary exit rather than a
        timeout: a caller that kills deliberately already knows why.
        """
        with self._lock:
            proc = self._current
        if proc is None:
            return False
        try:
            self._kill(proc)
            return True
        except Exception:
            return False

    def run(self, script: str, timeout: float) -> Result:
        """Write `script` to a temp file, run it headless, return what happened.

        The temp file is always removed and the in-flight handle is always
        cleared, however this exits.
        """
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         delete=False) as tmp:
            tmp.write(script)
            script_path = tmp.name
        try:
            proc = subprocess.Popen(
                [self.executable, '--background', '--python', script_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=_lift_address_space_limit,
            )
            with self._lock:
                self._current = proc
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                # This thread is the waiter, so it reaps — see _kill.
                stdout, stderr = proc.communicate()
                return Result(exit_code=None, stdout_capture=stdout,
                              stderr_capture=stderr, is_timed_out=True,
                              second_elapsed=time.monotonic() - started)
            finally:
                with self._lock:
                    self._current = None
            return Result(exit_code=proc.returncode, stdout_capture=stdout,
                          stderr_capture=stderr, is_timed_out=False,
                          second_elapsed=time.monotonic() - started)
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass


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
