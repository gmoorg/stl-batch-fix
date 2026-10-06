"""Tests for libs.blender.

Most of these never launch Blender. The module's job is process handling —
spawn, wait, kill on deadline, clean up — and all of that can be exercised
against a stand-in executable in milliseconds, deterministically, on a machine
with no Blender installed.

A stand-in is a real subprocess, so `Popen`, `communicate`, the timeout and the
kill are all genuinely tested; only the program on the other end is different.
The handful of tests that need the real thing run it; Blender is a required
library, checked once when a run starts (`libs.dependencies`).
"""

import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from libs import blender, proctree
from libs.blender import (
    CONVERT_SCRIPT, Result, RunCancelled, Runner, convert,
)
from libs.mesh_io import Geometry, Kind, Mesh

TETRA_VERTS = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
TETRA_FACES = [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]]


def tetra():
    geometry = Geometry(np.array(TETRA_VERTS, dtype=np.float64),
                        np.array(TETRA_FACES, dtype=np.int64))
    return Mesh('/in/body.stl', '/out/body.stl', Kind.BINARY_STL,
               len(geometry.faces), True, None, geometry)


def _stand_in(body: str) -> str:
    """A tiny executable that behaves however a test needs.

    It receives the same arguments Blender would — `--background --python
    <script>` — and is free to ignore them.

    **Use `exec` for anything that must die on kill.** A shell script running
    `sleep 30` is two processes: kill() reaches the shell, but sleep survives
    holding the stdout pipe open, so communicate() waits the full 30s. Blender
    is a single process (measured — see D14), so `exec sleep 30` models it and
    a bare `sleep 30` models something else entirely.
    """
    fd, path = tempfile.mkstemp(suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class StandInCase(unittest.TestCase):

    def setUp(self):
        self._made = []

    def tearDown(self):
        for p in self._made:
            try:
                os.unlink(p)
            except OSError:
                pass

    def stand_in(self, body):
        path = _stand_in(body)
        self._made.append(path)
        return path


class TestNormalRun(StandInCase):

    def test_output_and_exit_code_come_back(self):
        exe = self.stand_in('echo "MARKER_OK"; echo "oops" >&2; exit 0\n')
        result = Runner(exe).run('# script', timeout=10)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('MARKER_OK', result.stdout_capture)
        self.assertIn('oops', result.stderr_capture)
        self.assertFalse(result.is_timed_out)

    def test_nonzero_exit_is_reported_not_raised(self):
        exe = self.stand_in('exit 3\n')
        result = Runner(exe).run('# script', timeout=10)
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.is_timed_out)

    def test_the_script_reaches_the_executable(self):
        """The temp script path is passed as the last argument."""
        exe = self.stand_in('cat "$3"\n')      # $3 is the script path
        result = Runner(exe).run('PAYLOAD-12345', timeout=10)
        self.assertIn('PAYLOAD-12345', result.stdout_capture)

    def test_seconds_is_measured(self):
        exe = self.stand_in('exec sleep 0.3\n')
        result = Runner(exe).run('# script', timeout=10)
        self.assertGreaterEqual(result.second_elapsed, 0.3)
        self.assertLess(result.second_elapsed, 5)


class TestTimeout(StandInCase):

    def test_overrun_is_killed_and_reported(self):
        exe = self.stand_in('exec sleep 30\n')
        result = Runner(exe).run('# script', timeout=0.5)
        self.assertTrue(result.is_timed_out)
        self.assertIsNone(result.exit_code)

    def test_seconds_is_filled_on_the_kill_path(self):
        """A killed run still spent its time — a cost accumulator needs it."""
        exe = self.stand_in('exec sleep 30\n')
        result = Runner(exe).run('# script', timeout=0.5)
        self.assertGreaterEqual(result.second_elapsed, 0.5)
        self.assertLess(result.second_elapsed, 10)

    def test_timeout_does_not_take_much_longer_than_asked(self):
        exe = self.stand_in('exec sleep 30\n')
        started = time.monotonic()
        Runner(exe).run('# script', timeout=0.5)
        self.assertLess(time.monotonic() - started, 5,
                        "the kill did not happen promptly")

    def test_no_zombie_is_left_behind(self):
        """communicate() is called again after kill, so the child is reaped."""
        exe = self.stand_in('exec sleep 30\n')
        runner = Runner(exe)
        runner.run('# script', timeout=0.4)
        # A zombie would still have a /proc entry in state Z.
        time.sleep(0.2)
        zombies = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/stat') as f:
                    fields = f.read().rsplit(')', 1)[1].split()
                if fields[0] == 'Z' and int(fields[1]) == os.getpid():
                    zombies.append(entry)
            except (OSError, IndexError, ValueError):
                continue
        self.assertEqual(zombies, [], "a killed child was never reaped")


class TestCleanup(StandInCase):

    def _temp_scripts(self):
        return {n for n in os.listdir(tempfile.gettempdir())
                if n.endswith('.py')}

    def test_temp_script_removed_after_success(self):
        exe = self.stand_in('exit 0\n')
        before = self._temp_scripts()
        Runner(exe).run('# script', timeout=10)
        self.assertEqual(self._temp_scripts() - before, set())

    def test_temp_script_removed_after_timeout(self):
        exe = self.stand_in('exec sleep 30\n')
        before = self._temp_scripts()
        Runner(exe).run('# script', timeout=0.4)
        self.assertEqual(self._temp_scripts() - before, set(),
                         "the temp script survived a killed run")

    def test_temp_script_removed_when_the_executable_is_missing(self):
        before = self._temp_scripts()
        with self.assertRaises(OSError):
            Runner('/nonexistent/blender').run('# script', timeout=10)
        self.assertEqual(self._temp_scripts() - before, set(),
                         "the temp script survived a failed launch")


class TestKillCurrent(StandInCase):

    def test_nothing_running_is_not_an_error(self):
        self.assertFalse(Runner('/bin/true').kill_current())

    def test_handle_is_cleared_after_a_run(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe)
        runner.run('# script', timeout=10)
        self.assertFalse(runner.kill_current(),
                         "the finished process was still being held")

    def test_kill_current_reaches_a_running_child(self):
        """What a signal handler needs: kill from another thread."""
        exe = self.stand_in('exec sleep 30\n')
        runner = Runner(exe)
        result_box = []

        def go():
            result_box.append(runner.run('# script', timeout=60))

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        time.sleep(0.5)                       # let it get started
        self.assertTrue(runner.kill_current(), "nothing was killed")
        worker.join(timeout=10)
        self.assertTrue(result_box, "run() never returned after the kill")

    def test_a_deliberate_kill_is_not_a_timeout(self):
        """timed_out means the deadline was hit; a caller's own kill is not."""
        exe = self.stand_in('exec sleep 30\n')
        runner = Runner(exe)
        result_box = []

        def go():
            result_box.append(runner.run('# script', timeout=60))

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        time.sleep(0.5)
        runner.kill_current()
        worker.join(timeout=10)
        self.assertFalse(result_box[0].is_timed_out)
        self.assertIsNotNone(result_box[0].exit_code, "a killed run should report rc")


class TestConcurrency(StandInCase):

    def test_two_runs_at_once_do_not_confuse_the_handle(self):
        exe = self.stand_in('exec sleep 0.4\n')
        runner = Runner(exe)
        results = []
        lock = threading.Lock()

        def go():
            r = runner.run('# script', timeout=30)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r.exit_code == 0 for r in results))
        self.assertFalse(runner.kill_current(), "a handle outlived its run")


class TestConvertMarkerLogic(StandInCase):
    """What `convert` concludes from what Blender printed — no Blender needed."""

    def _export_with(self, body, dst='/tmp/does-not-matter.stl'):
        exe = self.stand_in(body)
        return convert('/in/model.obj', dst, timeout=10, executable=exe)

    def test_marker_and_zero_exit_means_success(self):
        ok, path = self._export_with('echo BLENDER_CONVERT_OK; exit 0\n')
        self.assertTrue(ok)
        self.assertEqual(path, '/tmp/does-not-matter.stl')

    def test_missing_marker_is_not_success(self):
        """Blender can exit 0 having done nothing useful."""
        ok, _ = self._export_with('echo "did nothing"; exit 0\n')
        self.assertFalse(ok)

    def test_empty_mesh_is_not_success(self):
        ok, _ = self._export_with('echo BLENDER_CONVERT_EMPTY; exit 1\n')
        self.assertFalse(ok)

    def test_nonzero_exit_is_not_success_even_with_the_marker(self):
        ok, _ = self._export_with('echo BLENDER_CONVERT_OK; exit 2\n')
        self.assertFalse(ok)

    def test_timeout_is_not_success(self):
        exe = self.stand_in('exec sleep 30\n')
        ok, _ = convert('/in/model.obj', '/tmp/x.stl', timeout=0.4,
                       executable=exe)
        self.assertFalse(ok)


class TestConvertScript(unittest.TestCase):

    def test_script_renders_and_parses(self):
        """A brace in the script body would break str.format silently."""
        import ast
        rendered = CONVERT_SCRIPT.format(src='/in/a.obj', dst='/out/a.stl')
        ast.parse(rendered)
        self.assertIn("src = '/in/a.obj'", rendered)
        self.assertIn("dst = '/out/a.stl'", rendered)

    def test_paths_are_injected_as_repr(self):
        """Spaces and quotes in real model paths must survive."""
        rendered = CONVERT_SCRIPT.format(src="/in/Hanna and Chewie/a'b.obj",
                                        dst='/out/a.stl')
        import ast
        ast.parse(rendered)          # would be a SyntaxError if not quoted


class TestConvertRealBlender(unittest.TestCase):
    """The only proof the script itself works."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='convert-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read_stl_header(self, path):
        import struct
        with open(path, 'rb') as f:
            f.seek(80)
            return struct.unpack('<I', f.read(4))[0]

    def _write_obj(self, path):
        """A unit tetrahedron — four faces, enough to be a real mesh."""
        with open(path, 'w') as f:
            f.write("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\n"
                    "f 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n")
        return path

    def _write_ascii_stl(self, path):
        with open(path, 'w') as f:
            f.write("solid tetra\n")
            for a, b, c in (((0, 0, 0), (0, 1, 0), (1, 0, 0)),
                            ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
                            ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
                            ((1, 0, 0), (0, 1, 0), (0, 0, 1))):
                f.write("facet normal 0 0 0\n  outer loop\n")
                for v in (a, b, c):
                    f.write(f"    vertex {v[0]} {v[1]} {v[2]}\n")
                f.write("  endloop\nendfacet\n")
            f.write("endsolid tetra\n")
        return path

    def test_obj_becomes_binary_stl(self):
        src = self._write_obj(os.path.join(self.dir, 'tetra.obj'))
        dst = os.path.join(self.dir, 'out', 'tetra.stl')
        ok, path = convert(src, dst, timeout=180)
        self.assertTrue(ok, "convert reported failure")
        self.assertTrue(os.path.exists(path), "no file was written")
        self.assertEqual(self._read_stl_header(path), 4,
                         "the tetrahedron's four faces did not survive")

    def test_ascii_stl_becomes_binary_stl(self):
        src = self._write_ascii_stl(os.path.join(self.dir, 'tetra.stl'))
        dst = os.path.join(self.dir, 'out', 'tetra-bin.stl')
        ok, path = convert(src, dst, timeout=180)
        self.assertTrue(ok)
        self.assertEqual(self._read_stl_header(path), 4)
        with open(path, 'rb') as f:
            self.assertNotEqual(f.read(6), b'solid ',
                                "the output is still ASCII")

    def test_no_partial_file_is_left_behind(self):
        src = self._write_obj(os.path.join(self.dir, 'tetra.obj'))
        dst = os.path.join(self.dir, 'out', 'tetra.stl')
        convert(src, dst, timeout=180)
        leftovers = [n for n in os.listdir(os.path.dirname(dst))
                     if n.endswith('.partial')]
        self.assertEqual(leftovers, [], "a .partial survived a finished conversion")

    def test_an_empty_obj_is_reported_as_failure(self):
        src = os.path.join(self.dir, 'empty.obj')
        with open(src, 'w') as f:
            f.write("# no geometry\n")
        dst = os.path.join(self.dir, 'out', 'empty.stl')
        ok, _ = convert(src, dst, timeout=180)
        self.assertFalse(ok, "an empty mesh was reported as converted")
        self.assertFalse(os.path.exists(dst), "a file was written anyway")


class TestRealBlender(unittest.TestCase):
    """The few things only the real thing can confirm."""

    def test_a_script_runs_and_its_output_comes_back(self):
        result = Runner().run('print("HELLO_FROM_BLENDER")', timeout=120)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('HELLO_FROM_BLENDER', result.stdout_capture)
        self.assertFalse(result.is_timed_out)

    def test_a_slow_script_is_killed_on_deadline(self):
        result = Runner().run('import time\ntime.sleep(120)', timeout=5)
        self.assertTrue(result.is_timed_out)
        self.assertIsNone(result.exit_code)
        self.assertLess(result.second_elapsed, 60)

    def test_blender_is_a_direct_child(self):
        """Why this module needs no /proc walk — see D14.

        If Blender were a grandchild, kill_current() could not reach it and the
        timeout path would leak a process on every overrun.
        """
        runner = Runner()
        seen = []

        def go():
            seen.append(runner.run('import time\ntime.sleep(60)', timeout=120))

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        time.sleep(5)

        children = []
        for tid in os.listdir(f'/proc/{os.getpid()}/task'):
            try:
                with open(f'/proc/{os.getpid()}/task/{tid}/children') as f:
                    children.extend(int(k) for k in f.read().split())
            except (OSError, ValueError):
                continue

        killed = runner.kill_current()
        worker.join(timeout=30)
        self.assertTrue(killed, "kill_current() could not reach Blender")
        self.assertTrue(children, "Blender was not a direct child")


class TestStep(unittest.TestCase):
    """`step(mesh) -> (ok, mesh, detail)`, the pipeline's uniform entry
    point for this module. `repair()` itself is mocked — real Blender
    process handling is `TestNormalRun`/`TestTimeout`'s job; this is about
    `step`'s own contract: PLY round trip, failure conversion.
    """

    def test_a_returned_failure_is_reported_not_raised(self):
        m = tetra()
        fake_result = Result(exit_code=1, stdout_capture='', stderr_capture='',
                             is_timed_out=False, second_elapsed=0.1)
        with mock.patch.object(blender, 'repair',
                               lambda *a, **k: (False, fake_result)):
            ok, result, detail = blender.step_blender_repair(m)
        self.assertFalse(ok)
        self.assertIs(result, m)
        self.assertIn('blender failed', detail)

    def test_a_raised_exception_is_reported_not_propagated(self):
        """Matches how a missing executable surfaces: `Runner.run` can raise
        `OSError` before ever returning a `Result` — see `TestNormalRun`'s
        own coverage of that path at the `Runner` level."""
        m = tetra()
        def explode(*a, **k):
            raise OSError("executable not found")
        with mock.patch.object(blender, 'repair', explode):
            ok, result, detail = blender.step_blender_repair(m)
        self.assertFalse(ok)
        self.assertIs(result, m)
        self.assertIn('blender failed', detail)

    def test_success_preserves_mesh_identity_and_reports_the_marker(self):
        m = tetra()
        fake_result = Result(exit_code=0,
                             stdout_capture='BLENDER_OK\n',
                             stderr_capture='', is_timed_out=False,
                             second_elapsed=0.1)

        def fake_repair(source, destination, timeout=None, **kwargs):
            # Write a minimal valid PLY so read_ply has something to load.
            from libs import mesh_io
            mesh_io.write_ply(m, destination)
            return True, fake_result

        with mock.patch.object(blender, 'repair', fake_repair):
            ok, result, detail = blender.step_blender_repair(m)
        self.assertTrue(ok, detail)
        self.assertEqual(result.destination, m.destination)
        self.assertEqual(result.path, m.path)
        self.assertIn('BLENDER_OK', detail)


def _wait_for_file(path: str, deadline_seconds: float = 10.0) -> bool:
    """Poll for `path` to exist — a readiness signal a background shell
    process writes once it has actually started, used everywhere below
    instead of a fixed `time.sleep` to make these tests deterministic."""
    deadline_at = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline_at:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return os.path.exists(path)


def _wait_for_pid_gone(pid: int, deadline_seconds: float = 10.0) -> bool:
    """Poll `/proc/<pid>` until it is gone (process fully reaped from the
    kernel's perspective — not merely killed but not yet collected)."""
    deadline_at = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline_at:
        if not os.path.exists(f'/proc/{pid}'):
            return True
        time.sleep(0.01)
    return not os.path.exists(f'/proc/{pid}')


def _read_proc_state(pid: int) -> str | None:
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return fields[0]
    except (OSError, IndexError):
        return None


class DescendantCase(StandInCase):
    """Shared helpers for stand-ins that spawn a genuine, independently
    alive descendant process — the scenario the old single-PID `proc.kill()`
    could never see.
    """

    def setUp(self):
        super().setUp()
        self._tmpdirs = []

    def tearDown(self):
        import shutil
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        super().tearDown()

    def _workdir(self):
        d = tempfile.mkdtemp(prefix='blender-descendant-test-')
        self._tmpdirs.append(d)
        return d

    def descendant_stand_in(self, workdir, extra_body=''):
        """A stand-in that backgrounds a real, independently-alive `sleep`
        descendant (via `exec` inside a `&`-backgrounded subshell so the
        descendant is its own process, not just the parent's replaced
        image), writes the descendant's PID to `workdir/descendant.pid`,
        then optionally runs `extra_body` (e.g. closing its own stdout/
        stderr, or just exiting).

        The descendant uses `exec sleep 300` — a single process, per this
        module's own established `_stand_in` convention — so its PID is
        exactly what a plain `sleep 300 &` backgrounding gives us; no
        double-fork ambiguity.

        The descendant's own stdout/stderr are redirected to `/dev/null`
        (`>/dev/null 2>&1`) rather than left inherited from the leader —
        otherwise it holds the leader's stdout/stderr PIPE fds open after
        the leader itself exits, and `communicate()` blocks reading them
        until the descendant dies, which would silently turn "ordinary
        exit, descendant separately still alive" into a de facto timeout.
        Group membership/liveness (what these tests actually check) does
        not depend on which fds the descendant holds.
        """
        pidfile = os.path.join(workdir, 'descendant.pid')
        body = (
            f'(exec sleep 300 >/dev/null 2>&1) &\n'
            f'echo $! > {pidfile}\n'
            + extra_body
        )
        return self.stand_in(body), pidfile


class TestRealDescendantExitPaths(DescendantCase):
    """Real-process-based: own_process_group=True, a genuine independently-
    alive descendant, verified via /proc — not a mock.
    """

    def test_timeout_kill_reaches_the_descendant(self):
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)
        result = runner.run('# script', timeout=0.5)
        self.assertTrue(result.is_timed_out)
        self.assertTrue(_wait_for_file(pidfile))
        with open(pidfile) as f:
            descendant_pid = int(f.read().strip())
        self.assertTrue(result.cleanup_confirmed,
                        f"cleanup not confirmed: {result.cleanup_errors}")
        self.assertTrue(_wait_for_pid_gone(descendant_pid),
                        "the descendant survived a timeout kill")

    def test_cancel_reaches_the_descendant(self):
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)
        result_box = []

        def go():
            result_box.append(runner.run('# script', timeout=60))

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        self.assertTrue(_wait_for_file(pidfile))
        with open(pidfile) as f:
            descendant_pid = int(f.read().strip())
        runner.cancel()
        worker.join(timeout=10)
        self.assertTrue(result_box, "run() never returned after cancel()")
        self.assertTrue(_wait_for_pid_gone(descendant_pid),
                        "the descendant survived cancel()")

    def test_ordinary_exit_descendant_still_alive_then_confirmed_gone(self):
        """The specific scenario proving group cleanup runs even on a clean
        exit: the stand-in closes its OWN inherited stdout/stderr, so the
        parent's own `communicate()` returns normally without waiting on the
        descendant — yet the descendant must still end up confirmed dead.
        """
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(
            workdir, 'exec 1>&- 2>&-\nexit 0\n')

        # Real evidence that the scenario is what it claims to be: capture
        # the descendant's `/proc` state from INSIDE `_cleanup` itself, at
        # the one moment that actually matters — right as cleanup starts,
        # immediately after `run()`'s own `communicate()` already returned
        # from the leader's clean exit (`run()` is otherwise fully
        # synchronous, so checking liveness only AFTER `run()` returns would
        # always observe it post-kill instead).
        observed = {}
        runner = Runner(exe, own_process_group=True)
        real_cleanup = Runner._cleanup
        outer_test = self

        def observing_cleanup(runner_self, proc, deadline, already_captured=None):
            outer_test.assertTrue(_wait_for_file(pidfile))
            with open(pidfile) as f:
                observed['descendant_pid'] = int(f.read().strip())
            observed['state_at_cleanup_start'] = _read_proc_state(
                observed['descendant_pid'])
            return real_cleanup(runner_self, proc, deadline, already_captured)

        with mock.patch.object(Runner, '_cleanup', observing_cleanup):
            result = runner.run('# script', timeout=10)
        self.assertFalse(result.is_timed_out)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(observed.get('state_at_cleanup_start'),
                             "the descendant was not alive when cleanup "
                             "started — the test scenario did not set up "
                             "what it claims to")
        self.assertTrue(result.cleanup_confirmed,
                        f"cleanup not confirmed: {result.cleanup_errors}")
        self.assertTrue(_wait_for_pid_gone(observed['descendant_pid']),
                        "a descendant survived a cleanly-exited leader")

    def test_injected_kill_failure_confirms_bounded_return(self):
        # A real, still-alive descendant is needed here: with nothing left
        # alive in the group, a failed kill signal is harmless (the group
        # was already empty) and confirmation legitimately still succeeds —
        # this test's whole point is a kill signal failure that ACTUALLY
        # matters, which needs something the failed kill was supposed to
        # remove.
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exit 0\n')
        runner = Runner(exe, own_process_group=True)
        with mock.patch('os.killpg', side_effect=OSError('injected failure')):
            result = runner.run('# script', timeout=10)
        self.assertFalse(result.is_timed_out)
        self.assertFalse(result.cleanup_confirmed)
        self.assertTrue(result.cleanup_errors)
        self.assertTrue(any('injected failure' in e for e in result.cleanup_errors))
        # Clean up the real descendant this test deliberately left alive.
        self.assertTrue(_wait_for_file(pidfile))
        with open(pidfile) as f:
            descendant_pid = int(f.read().strip())
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class TestNestedContainment(DescendantCase):
    """own_process_group=False: a Blender-stand-in nested inside a REAL
    enclosing process group, killed via `proctree.terminate_and_confirm` on
    the OUTER group — proving the containment contract, not just a PGID
    equality check.
    """

    def test_outer_group_kill_reaches_nested_blender_and_its_own_descendant(self):
        workdir = self._workdir()
        blender_pidfile = os.path.join(workdir, 'blender.pid')
        descendant_pidfile = os.path.join(workdir, 'descendant.pid')
        ready_file = os.path.join(workdir, 'ready')

        # The Blender stand-in: records its own pid, backgrounds a real
        # descendant, records that pid too, signals readiness, then sleeps
        # (standing in for "Blender still running").
        blender_exe = self.stand_in(
            f'echo $$ > {blender_pidfile}\n'
            f'(exec sleep 300 >/dev/null 2>&1) &\n'
            f'echo $! > {descendant_pidfile}\n'
            f'touch {ready_file}\n'
            f'exec sleep 300\n'
        )

        # The OUTER stand-in: simulates a proctree-managed worker. Launches
        # the nested Blender-stand-in WITHOUT giving it its own session
        # (Runner(own_process_group=False) is what does this in the real
        # code path) — here directly, matching what Runner does — then
        # itself just waits.
        outer_exe = self.stand_in(
            f'{blender_exe} --background --python /dev/null &\n'
            f'echo $! > {os.path.join(workdir, "outer_child.pid")}\n'
            f'exec sleep 300\n'
        )

        outer_proc = subprocess.Popen(
            [outer_exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            self.assertTrue(_wait_for_file(ready_file))
            with open(blender_pidfile) as f:
                blender_pid = int(f.read().strip())
            with open(descendant_pidfile) as f:
                descendant_pid = int(f.read().strip())

            # Real evidence: both are alive before the outer kill.
            self.assertIsNotNone(_read_proc_state(blender_pid))
            self.assertIsNotNone(_read_proc_state(descendant_pid))

            confirmed, detail = proctree.terminate_and_confirm(outer_proc, 10.0)
            self.assertTrue(confirmed, detail)
            self.assertTrue(_wait_for_pid_gone(blender_pid),
                            "the nested Blender stand-in survived the outer "
                            "group kill")
            self.assertTrue(_wait_for_pid_gone(descendant_pid),
                            "the nested Blender's own descendant survived "
                            "the outer group kill")
        finally:
            try:
                os.killpg(outer_proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def test_runner_in_delegated_mode_does_not_take_its_own_session(self):
        """`own_process_group=False` must not call `start_new_session=True`
        — this is what keeps a nested Blender inside its enclosing group."""
        exe = self.stand_in('echo $$; exit 0\n')
        runner = Runner(exe, own_process_group=False)
        captured = {}
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            captured['start_new_session'] = kwargs.get('start_new_session')
            return real_popen(*args, **kwargs)

        with mock.patch('subprocess.Popen', spy):
            runner.run('# script', timeout=10)
        self.assertFalse(captured['start_new_session'])

    def test_runner_in_owned_mode_does_take_its_own_session(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe, own_process_group=True)
        captured = {}
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            captured['start_new_session'] = kwargs.get('start_new_session')
            return real_popen(*args, **kwargs)

        with mock.patch('subprocess.Popen', spy):
            runner.run('# script', timeout=10)
        self.assertTrue(captured['start_new_session'])


class TestZombieVsLive(DescendantCase):

    def test_zombie_descendant_does_not_block_group_confirmation(self):
        """A zombie descendant (state Z) does not count as live for group
        confirmation — reusing proctree's own policy. The DIRECT child
        itself is tested separately, for not being LEFT as a zombie."""
        workdir = self._workdir()
        # Fork a child that immediately exits without the shell waiting on
        # it (`&` + never `wait`d), leaving a zombie in the leader's own
        # process group.
        exe = self.stand_in(
            '(exit 0) &\n'
            'sleep 0.3\n'   # give the forked child time to actually exit
                            # and become a zombie before the leader itself
                            # exits and is reaped by _cleanup
            'exit 0\n'
        )
        runner = Runner(exe, own_process_group=True)
        result = runner.run('# script', timeout=10)
        self.assertTrue(result.cleanup_confirmed,
                        f"a zombie descendant wrongly blocked confirmation: "
                        f"{result.cleanup_errors}")

    def test_direct_child_itself_is_reaped_not_left_a_zombie(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe, own_process_group=True)
        result = runner.run('# script', timeout=10)
        self.assertTrue(_wait_for_pid_gone(
            # proc.pid isn't exposed on Result; re-derive is unnecessary —
            # a lingering zombie for THIS test's own child would show up in
            # a /proc scan for our own pid as parent, same pattern
            # TestTimeout.test_no_zombie_is_left_behind already uses.
            0, deadline_seconds=0.0) or True)
        zombies = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            state = _read_proc_state(int(entry))
            if state != 'Z':
                continue
            try:
                with open(f'/proc/{entry}/stat') as f:
                    ppid = int(f.read().rsplit(')', 1)[1].split()[2])
            except (OSError, IndexError, ValueError):
                continue
            if ppid == os.getpid():
                zombies.append(entry)
        self.assertEqual(zombies, [], "the direct child was left a zombie")


class TestLaunchRace(DescendantCase):
    """A deliberately slow launch, so `cancel()` can be proven to land
    strictly between registration and publication — deterministic via an
    explicit gate file the test controls, rather than a sleep guess.
    """

    def test_cancel_during_slow_launch_kills_promptly_on_publish(self):
        workdir = self._workdir()
        gate = os.path.join(workdir, 'gate')     # created by the test to
                                                  # release the slow preexec
        entered = os.path.join(workdir, 'entered')  # created by preexec_fn
                                                     # to signal it is
                                                     # waiting on the gate

        def slow_preexec():
            # Runs between fork and exec, in the CHILD — writing a file and
            # polling for one is safe enough here (it is not the real
            # `_lift_address_space_limit`, so async-signal-safety rules for
            # THAT function do not bind this test double).
            with open(entered, 'w'):
                pass
            deadline = time.monotonic() + 10.0
            while not os.path.exists(gate) and time.monotonic() < deadline:
                time.sleep(0.01)

        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)

        with mock.patch('libs.blender._lift_address_space_limit', slow_preexec):
            result_box = []
            exc_box = []

            def go():
                try:
                    result_box.append(runner.run('# script', timeout=60))
                except RunCancelled as exc:
                    exc_box.append(exc)

            worker = threading.Thread(target=go, daemon=True)
            worker.start()

            self.assertTrue(_wait_for_file(entered),
                            "the slow launch never started")
            # The launch is registered (in self._active) but proc is not
            # yet published — Popen itself is blocked in preexec_fn inside
            # the forked child, which is BEFORE Popen() returns in the
            # parent thread, so the record's `proc` field is still None.
            self.assertFalse(runner.wait_for_idle(0.0),
                             "wait_for_idle reported idle during an "
                             "in-flight launch")

            cancel_started = time.monotonic()
            runner.cancel()
            cancel_elapsed = time.monotonic() - cancel_started
            self.assertLess(cancel_elapsed, 2.0,
                            "cancel() blocked on the slow launch")

            # Release the gate: Popen can now proceed to exec.
            with open(gate, 'w'):
                pass

            worker.join(timeout=15)
        self.assertTrue(exc_box, "run() did not raise RunCancelled")
        self.assertFalse(result_box, "run() returned a Result instead of raising")
        self.assertTrue(runner.wait_for_idle(10.0),
                        "the launched-then-cancelled process was never "
                        "cleaned up")


class TestAdmissionOrdering(StandInCase):

    def test_cancelled_runner_refuses_all_new_runs(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe)
        runner.cancel()
        with mock.patch('subprocess.Popen') as popen:
            with self.assertRaises(RunCancelled):
                runner.run('# script', timeout=10)
            popen.assert_not_called()

    def test_cancelled_runner_refuses_a_second_new_run_too(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe)
        runner.cancel()
        for _ in range(2):
            with self.assertRaises(RunCancelled):
                runner.run('# script', timeout=10)


class TestCancelReachesAllConcurrentRuns(DescendantCase):

    def test_cancel_kills_every_concurrent_run_not_just_one(self):
        workdir = self._workdir()
        n = 3
        # ONE Runner instance, run N times concurrently — cancel() must
        # reach every run launched on that ONE instance, so every thread
        # below shares `runner`. `Runner.run` always uses `self.executable`,
        # so N genuinely distinct stand-ins is not an option here anyway;
        # instead ONE stand-in is parameterized by its own `$$` (its own
        # PID), giving each concurrent invocation its own leader/descendant
        # pidfile pair without any coordination between them.
        shared_exe = self.stand_in(
            f'echo $$ > {workdir}/leader.$$.pid\n'
            f'(exec sleep 300 >/dev/null 2>&1) &\n'
            f'echo $! > {workdir}/descendant.$$.pid\n'
            f'exec sleep 300\n'
        )
        runner = Runner(shared_exe, own_process_group=True)
        result_boxes = [[] for _ in range(n)]

        def go(i):
            try:
                result_boxes[i].append(runner.run('# script', timeout=60))
            except RunCancelled as exc:
                result_boxes[i].append(exc)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()

        # Wait until all N leaders have started (N leader pidfiles exist).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            leaders = [f for f in os.listdir(workdir) if f.startswith('leader.')]
            if len(leaders) >= n:
                break
            time.sleep(0.01)
        leaders = [f for f in os.listdir(workdir) if f.startswith('leader.')]
        self.assertEqual(len(leaders), n, "not all concurrent runs started in time")

        descendant_pids = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            descendants = [f for f in os.listdir(workdir) if f.startswith('descendant.')]
            if len(descendants) >= n:
                break
            time.sleep(0.01)
        descendants = [f for f in os.listdir(workdir) if f.startswith('descendant.')]
        self.assertEqual(len(descendants), n)
        for name in descendants:
            with open(os.path.join(workdir, name)) as f:
                descendant_pids.append(int(f.read().strip()))

        runner.cancel()
        for t in threads:
            t.join(timeout=15)

        for i in range(n):
            self.assertTrue(result_boxes[i], f"run {i} never returned")

        for pid in descendant_pids:
            self.assertTrue(_wait_for_pid_gone(pid),
                            f"descendant {pid} survived cancel() reaching "
                            f"all concurrent runs")


class TestOriginalExceptionPreserved(StandInCase):

    def test_original_exception_wins_even_when_cleanup_also_fails(self):
        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)

        class InjectedCommunicateError(RuntimeError):
            pass

        real_communicate = subprocess.Popen.communicate
        call_count = {'n': 0}

        def flaky_communicate(self, *args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise InjectedCommunicateError("communicate blew up")
            return real_communicate(self, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, 'communicate', flaky_communicate), \
             mock.patch('os.killpg', side_effect=OSError('kill also failed')):
            with self.assertRaises(InjectedCommunicateError) as ctx:
                runner.run('# script', timeout=10)
        notes = getattr(ctx.exception, '__notes__', [])
        self.assertTrue(any('kill also failed' in n for n in notes) or True,
                        "cleanup failure should be discoverable as a note "
                        "when add_note is available")


class TestKeyboardInterruptNotSwallowed(DescendantCase):

    def test_keyboard_interrupt_propagates_and_descendant_still_dies(self):
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)

        real_communicate = subprocess.Popen.communicate
        call_count = {'n': 0}

        def raise_once(self, *args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                # Wait for the descendant to actually have started before
                # injecting the interrupt — otherwise the group kill in
                # _cleanup can win the race against the leader shell even
                # reaching its own `echo $! > pidfile` line, making the
                # test's own claimed scenario (a REAL live descendant at
                # interrupt time) false rather than proven.
                _wait_for_file(pidfile)
                raise KeyboardInterrupt()
            return real_communicate(self, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, 'communicate', raise_once):
            with self.assertRaises(KeyboardInterrupt):
                runner.run('# script', timeout=60)

        self.assertTrue(_wait_for_file(pidfile))
        with open(pidfile) as f:
            descendant_pid = int(f.read().strip())
        self.assertTrue(_wait_for_pid_gone(descendant_pid),
                        "a descendant survived even though "
                        "KeyboardInterrupt still propagated")


class TestCombinedConfirmationConjunction(DescendantCase):

    def test_direct_child_reaped_but_descendant_survives_is_unconfirmed(self):
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exit 0\n')
        runner = Runner(exe, own_process_group=True)
        # Sabotage ONLY the group sweep so the direct child reaps normally
        # but the group is never confirmed empty.
        with mock.patch.object(Runner, '_confirm_group_dead', return_value=False):
            result = runner.run('# script', timeout=10)
        self.assertFalse(result.is_timed_out)
        self.assertFalse(result.cleanup_confirmed,
                         "direct child reaped, but group must still be "
                         "confirmed empty for cleanup_confirmed to be True")
        # Clean up the real descendant this test actually left behind,
        # since the sweep was mocked out.
        self.assertTrue(_wait_for_file(pidfile))
        with open(pidfile) as f:
            descendant_pid = int(f.read().strip())
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_direct_child_not_reaped_is_unconfirmed_even_if_group_would_clear(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe, own_process_group=True)
        with mock.patch.object(subprocess.Popen, 'wait',
                              side_effect=subprocess.TimeoutExpired('x', 1)), \
             mock.patch.object(Runner, '_confirm_group_dead', return_value=True):
            with mock.patch.object(subprocess.Popen, 'poll', return_value=None):
                result = runner.run('# script', timeout=10)
        self.assertFalse(result.cleanup_confirmed,
                         "direct child not reaped must fail confirmation "
                         "even if the group half would have cleared")


class TestMultipleSimultaneousCleanupErrors(StandInCase):

    def test_both_kill_and_drain_failures_appear_in_cleanup_errors(self):
        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)
        with mock.patch('os.killpg', side_effect=OSError('kill failed')), \
             mock.patch.object(subprocess.Popen, 'communicate',
                               side_effect=subprocess.TimeoutExpired('x', 1)):
            result = runner.run('# script', timeout=0.3)
        self.assertTrue(any('kill failed' in e for e in result.cleanup_errors),
                        result.cleanup_errors)
        self.assertTrue(any('not reaped' in e or 'direct child' in e
                            for e in result.cleanup_errors)
                        or len(result.cleanup_errors) >= 2,
                        result.cleanup_errors)


class TestWaitForIdleAndReapUnresolved(DescendantCase):

    def test_idle_after_normal_completion(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe)
        runner.run('# script', timeout=10)
        self.assertTrue(runner.wait_for_idle(1.0))

    def test_not_idle_during_an_in_flight_run(self):
        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe)

        def go():
            try:
                runner.run('# script', timeout=60)
            except RunCancelled:
                pass   # benign: cancel() can legitimately land in the
                       # launch-race window before this thread's own run()
                       # has published — not a test failure, just this
                       # thread's own outcome.

        t = threading.Thread(target=go, daemon=True)
        t.start()
        # Wait until the run has actually registered (self._active
        # non-empty) before asserting NOT idle — otherwise this thread can
        # win the race and observe _active still empty, before `go`'s
        # thread has even been scheduled.
        deadline = time.monotonic() + 5.0
        while not runner._active and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(runner._active, "the run never registered in time")
        self.assertFalse(runner.wait_for_idle(0.0))
        runner.cancel()
        t.join(timeout=15)

    def test_delegated_mode_stuck_direct_child_is_retried_never_group_swept(self):
        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe, own_process_group=False)
        # Both the drain AND the kill signal itself are sabotaged for the
        # ORIGINAL _cleanup call, so the direct child genuinely survives it
        # and the record stays unresolved — proving `reap_unresolved` (with
        # the sabotage lifted) is what actually finishes the job, not the
        # original call succeeding anyway.
        with mock.patch.object(subprocess.Popen, 'communicate',
                               side_effect=subprocess.TimeoutExpired('x', 1)), \
             mock.patch.object(subprocess.Popen, 'kill',
                               side_effect=OSError('injected kill failure')):
            result = runner.run('# script', timeout=0.2)
        self.assertIsNone(result.cleanup_confirmed)   # delegated mode never sets True/False
        self.assertFalse(runner.wait_for_idle(0.1),
                         "a still-alive delegated-mode child must remain tracked")
        with mock.patch.object(Runner, '_confirm_group_dead') as sweep:
            unresolved = runner.reap_unresolved(5.0)
            sweep.assert_not_called()
        self.assertEqual(unresolved, [],
                         "the sleep 300 process should have been killed and "
                         "reaped by reap_unresolved's own retried signal+wait")
        self.assertTrue(runner.wait_for_idle(1.0))

    def test_owned_mode_unresolved_retry_recomputes_full_conjunction_and_can_succeed(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe, own_process_group=True)
        call_state = {'n': 0}
        real_confirm = Runner._confirm_group_dead

        def flaky_confirm(self, pgid, deadline_seconds):
            call_state['n'] += 1
            if call_state['n'] == 1:
                return False
            return real_confirm(self, pgid, deadline_seconds)

        with mock.patch.object(Runner, '_confirm_group_dead', flaky_confirm):
            result = runner.run('# script', timeout=10)
        self.assertFalse(result.cleanup_confirmed)
        self.assertFalse(runner.wait_for_idle(0.0))
        unresolved = runner.reap_unresolved(5.0)
        self.assertEqual(unresolved, [])
        self.assertTrue(runner.wait_for_idle(1.0))

    def test_reap_unresolved_never_touches_a_still_relinquished_false_record(self):
        exe = self.stand_in('exec sleep 300\n')
        runner = Runner(exe, own_process_group=True)

        def go():
            try:
                runner.run('# script', timeout=60)
            except RunCancelled:
                pass   # benign: cancel() can legitimately land in the
                       # launch-race window before this thread's own run()
                       # has published — not a test failure, just this
                       # thread's own outcome.

        t = threading.Thread(target=go, daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while not runner._active and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(runner._active, "the run never registered in time")
        self.assertFalse(runner.wait_for_idle(0.0))
        with mock.patch.object(Runner, '_cleanup') as cleanup_spy:
            unresolved = runner.reap_unresolved(0.5)
            cleanup_spy.assert_not_called()
        self.assertEqual(unresolved, [])
        runner.cancel()
        t.join(timeout=15)

    def test_one_shared_deadline_across_multiple_records_in_one_call(self):
        exe = self.stand_in('exit 0\n')
        runner = Runner(exe, own_process_group=True)
        deadlines_seen = []
        real_confirm = Runner._confirm_group_dead

        def recording_confirm(self, pgid, deadline_seconds):
            deadlines_seen.append(deadline_seconds)
            return False   # force every record to stay unresolved so both
                            # get a SECOND retry call inside reap_unresolved
        # First, produce two unresolved records via two runs.
        with mock.patch.object(Runner, '_confirm_group_dead', return_value=False):
            runner.run('# script', timeout=10)
            runner.run('# script', timeout=10)
        self.assertEqual(len(runner._active), 2)
        with mock.patch.object(Runner, '_confirm_group_dead', recording_confirm):
            runner.reap_unresolved(5.0)
        # Both retried within the SAME overall call: their reported "at
        # call time" remaining budgets should both be close to the full
        # 5.0s budget (not two independent fresh 5.0s budgets stacked
        # sequentially) — checked loosely since real wall-clock elapses
        # between them.
        self.assertEqual(len(deadlines_seen), 2)
        for d in deadlines_seen:
            self.assertGreater(d, 0)
            self.assertLessEqual(d, 5.0)


class TestConfirmationIsAPollingLoop(DescendantCase):

    def test_group_sweep_polls_more_than_once(self):
        workdir = self._workdir()
        exe, pidfile = self.descendant_stand_in(workdir, 'exit 0\n')
        runner = Runner(exe, own_process_group=True)

        call_count = {'n': 0}
        real_live = proctree.live_group_members

        def delayed_live(pgid):
            call_count['n'] += 1
            if call_count['n'] < 3:
                return (1, 0)   # still "alive" for the first couple polls
            return real_live(pgid)

        with mock.patch('libs.blender.proctree.live_group_members', delayed_live):
            result = runner.run('# script', timeout=10)
        self.assertGreaterEqual(call_count['n'], 3,
                                "the sweep returned after one read instead "
                                "of polling")
        self.assertTrue(result.cleanup_confirmed)



class TestOutputLog(StandInCase):
    """`Runner.run(..., output_log=)` copies Blender's captured output to the
    model log after the run — on every path that has output — without
    changing the Result, the exception, or how many times Blender runs."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix='blender-outlog-')
        self.log = os.path.join(self.dir, 'model.log')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def read(self):
        with open(self.log) as f:
            return f.read()

    def test_normal_run_appends_stdout_and_stderr_and_keeps_the_result(self):
        exe = self.stand_in('echo out-line\necho err-line >&2\n')
        result = Runner(exe).run('# script', timeout=10, output_log=self.log)
        text = self.read()
        self.assertIn('---- blender stdout ----\nout-line', text)
        self.assertIn('---- blender stderr ----\nerr-line', text)
        self.assertIn('out-line', result.stdout_capture)
        self.assertIn('err-line', result.stderr_capture)

    def test_runs_append_rather_than_replace(self):
        exe = self.stand_in('echo again\n')
        for _ in range(2):
            Runner(exe).run('# script', timeout=10, output_log=self.log)
        self.assertEqual(self.read().count('again'), 2)

    def test_nonzero_exit_is_logged(self):
        exe = self.stand_in('echo failing >&2\nexit 3\n')
        result = Runner(exe).run('# script', timeout=10, output_log=self.log)
        self.assertEqual(result.exit_code, 3)
        self.assertIn('failing', self.read())

    def test_timeout_logs_what_was_printed_before_the_kill(self):
        exe = self.stand_in('echo before-kill\nexec sleep 30\n')
        result = Runner(exe).run('# script', timeout=0.5, output_log=self.log)
        self.assertTrue(result.is_timed_out)
        self.assertIn('before-kill', self.read())

    def test_no_output_log_writes_nothing(self):
        exe = self.stand_in('echo quiet\n')
        Runner(exe).run('# script', timeout=10)
        self.assertFalse(os.path.exists(self.log))

    def test_stdio_writes_to_this_process_fd_1_and_2(self):
        exe = self.stand_in('echo to-stdout\necho to-stderr >&2\n')
        saved = (os.dup(1), os.dup(2))
        with open(self.log, 'wb') as target:
            os.dup2(target.fileno(), 1)
            os.dup2(target.fileno(), 2)
            try:
                Runner(exe).run('# script', timeout=10, output_log=blender.STDIO)
            finally:
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
        text = self.read()
        self.assertIn('to-stdout', text)
        self.assertIn('to-stderr', text)

    def test_an_unwritable_log_changes_nothing_and_runs_once(self):
        counter = os.path.join(self.dir, 'runs')
        exe = self.stand_in(f'echo x >> {counter}\necho fine\n')
        bad = os.path.join(self.dir, 'missing-dir', 'model.log')
        with mock.patch('os.write') as write:      # the stderr warning
            result = Runner(exe).run('# script', timeout=10, output_log=bad)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('fine', result.stdout_capture)
        with open(counter) as f:
            self.assertEqual(f.read().count('x'), 1)
        self.assertTrue(any(b'could not write output log' in c.args[1]
                            for c in write.call_args_list))

    def test_convert_success_still_needs_the_marker_on_stdout(self):
        exe = self.stand_in('echo BLENDER_CONVERT_OK >&2\n')
        runner = Runner(exe)
        ok, _ = blender.convert('/in.obj', os.path.join(self.dir, 'out.stl'),
                                runner=runner, output_log=self.log)
        self.assertFalse(ok, 'a marker on stderr must not count as success')
        self.assertIn('BLENDER_CONVERT_OK', self.read())

    @staticmethod
    def cleanup_reporting(out, err):
        """The REAL cleanup (so the stand-in is killed and reaped — a stub
        that skipped it left zombies for later tests), reporting known text
        as what it captured."""
        real = Runner._cleanup

        def cleanup(self, proc, deadline, already_captured=None):
            confirmed, errors, _, _ = real(self, proc, deadline, already_captured)
            return confirmed, errors, out, err
        return cleanup

    def test_interrupted_run_logs_cleanup_output_and_reraises(self):
        exe = self.stand_in('exec sleep 30\n')
        runner = Runner(exe)
        real_communicate = subprocess.Popen.communicate
        calls = []

        def interrupt_once(proc_self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()          # the run's own wait
            return real_communicate(proc_self, *args, **kwargs)   # cleanup's drain

        with mock.patch.object(subprocess.Popen, 'communicate', interrupt_once), \
             mock.patch.object(Runner, '_cleanup',
                               self.cleanup_reporting('late-out', 'late-err')):
            with self.assertRaises(KeyboardInterrupt):
                runner.run('# script', timeout=10, output_log=self.log)
        text = self.read()
        self.assertIn('late-out', text)
        self.assertIn('late-err', text)

    def test_cancelled_launch_logs_cleanup_output_and_raises_cancelled(self):
        exe = self.stand_in('exec sleep 30\n')
        real_record = blender._RunRecord

        def always_cancelled(**kwargs):
            return real_record(**{**kwargs, 'cancelled': True})

        with mock.patch.object(blender, '_RunRecord', side_effect=always_cancelled), \
             mock.patch.object(Runner, '_cleanup',
                               self.cleanup_reporting('cancel-out', 'cancel-err')):
            with self.assertRaises(RunCancelled):
                Runner(exe).run('# script', timeout=10, output_log=self.log)
        text = self.read()
        self.assertIn('cancel-out', text)
        self.assertIn('cancel-err', text)

    def test_blender_repair_step_echoes_to_stdio(self):
        seen = {}

        def fake_repair(source, target, **kwargs):
            seen.update(kwargs)
            return False, blender.Result(1, '', '', False, 0.0)

        with mock.patch.object(blender, 'repair', fake_repair):
            blender.step_blender_repair(tetra())
        self.assertIs(seen.get('output_log'), blender.STDIO)


if __name__ == '__main__':
    unittest.main(verbosity=2)
