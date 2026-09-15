"""Tests for libs.blender.

Most of these never launch Blender. The module's job is process handling —
spawn, wait, kill on deadline, clean up — and all of that can be exercised
against a stand-in executable in milliseconds, deterministically, on a machine
with no Blender installed.

A stand-in is a real subprocess, so `Popen`, `communicate`, the timeout and the
kill are all genuinely tested; only the program on the other end is different.
The handful of tests that need the real thing are guarded by `is_available()`.
"""

import os
import stat
import tempfile
import threading
import time
import unittest

from libs.blender import (
    CONVERT_SCRIPT, Result, Runner, convert, is_available,
)


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


@unittest.skipUnless(is_available(), "blender not installed")
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


class TestAvailability(unittest.TestCase):

    def test_missing_executable_is_not_available(self):
        self.assertFalse(is_available('/nonexistent/blender'))

    def test_non_blender_executable_is_still_reported_by_exit_code(self):
        """It asks --version and trusts the exit code; that is all it claims."""
        self.assertTrue(is_available('/bin/true'))


@unittest.skipUnless(is_available(), "blender not installed")
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
