"""Process-group kill and confirmation, against real subprocess fixtures."""

import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from libs.proctree import (RUNNER_GONE_EXIT, exit_with_parent, live_group_members,
                           terminate_and_confirm)


class TestTerminateAndConfirm(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _spawn(self, code):
        return subprocess.Popen([sys.executable, '-c', code], start_new_session=True)

    def _wait_for_marker(self, path, deadline=5.0):
        """Block until `path` exists — a handshake so a test never races the
        very condition it's trying to prove terminate_and_confirm handles."""
        deadline_at = time.monotonic() + deadline
        while time.monotonic() < deadline_at:
            if os.path.exists(path):
                return
            time.sleep(0.01)
        self.fail(f'{path} never appeared within {deadline}s — fixture setup itself failed')

    def test_simple_child_confirmed_dead(self):
        proc = self._spawn('import time; time.sleep(30)')
        confirmed, detail = terminate_and_confirm(proc, deadline=5.0)
        self.assertTrue(confirmed, detail)

    def test_child_with_surviving_grandchild_blocks_until_grandchild_dies(self):
        # The direct child spawns a grandchild in the SAME process group
        # (no setsid of its own), signals it is alive via a marker file, and
        # then exits immediately, leaving the grandchild as the thing that
        # must still be reached by killpg.  The marker handshake is required
        # here: without it, the test could pass even if the group scan never
        # ran, simply because the grandchild had not been spawned yet.
        marker = os.path.join(self.temp.name, 'grandchild_alive')
        code = (
            'import subprocess, sys, time\n'
            f'p = subprocess.Popen([sys.executable, "-c",\n'
            f'    "import time; open({marker!r}, \'w\').close(); time.sleep(30)"])\n'
            'sys.exit(0)\n'
        )
        proc = self._spawn(code)
        self._wait_for_marker(marker)
        confirmed, detail = terminate_and_confirm(proc, deadline=5.0)
        self.assertTrue(confirmed, detail)
        # If the group scan had not actually reached the grandchild, its
        # sleep(30) would still be running — confirm it is really gone by
        # checking terminate_and_confirm again reports immediate success
        # (an already-dead group), not a fresh 5s wait finding a survivor.
        started = time.monotonic()
        confirmed_again, _ = terminate_and_confirm(proc, deadline=5.0)
        self.assertTrue(confirmed_again)
        self.assertLess(time.monotonic() - started, 1.0,
                        'second confirm took as long as a fresh wait — grandchild may have survived')

    def test_already_dead_child_confirms_immediately(self):
        proc = self._spawn('pass')
        proc.wait()
        confirmed, detail = terminate_and_confirm(proc, deadline=5.0)
        self.assertTrue(confirmed, detail)

    def test_zombie_child_does_not_block_confirmation(self):
        # A grandchild that exits but whose direct-child parent never waits
        # on it becomes a zombie.  It must not block confirmation, since a
        # zombie cannot write to anything.  Marker handshake for the same
        # reason as above — must not race the fork.
        marker = os.path.join(self.temp.name, 'forked')
        code = (
            'import os, sys, time\n'
            'pid = os.fork()\n'
            'if pid == 0:\n'
            '    sys.exit(0)\n'
            'else:\n'
            f'    open({marker!r}, "w").close()\n'
            '    time.sleep(30)\n'   # parent never reaps the zombie child
        )
        proc = self._spawn(code)
        self._wait_for_marker(marker)
        confirmed, detail = terminate_and_confirm(proc, deadline=5.0)
        self.assertTrue(confirmed, detail)


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Arms with the runner PID in argv[1], announces itself via the marker in
#: argv[2] only after arming, then waits to be killed.
_ARMED_CHILD = (
    'import os, sys, time\n'
    f'sys.path.insert(0, {_REPO!r})\n'
    'from libs import proctree\n'
    'proctree.exit_with_parent(int(sys.argv[1]))\n'
    'open(sys.argv[2] + ".tmp", "w").write(str(os.getpid()))\n'
    'os.replace(sys.argv[2] + ".tmp", sys.argv[2])\n'
    'time.sleep(600)\n'
)


class TestExitWithParent(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.marker = os.path.join(self.temp.name, 'armed')

    def _wait_for_marker(self, deadline=20.0):
        deadline_at = time.monotonic() + deadline
        while time.monotonic() < deadline_at:
            if os.path.exists(self.marker):
                return
            time.sleep(0.01)
        self.fail(f'{self.marker} never appeared within {deadline}s')

    def test_child_dies_when_its_runner_is_sigkilled(self):
        # The runner leads its own group and the child stays in it, so the
        # cleanup (registered before anything can fail) reaches both, ready
        # or not, and the assertion can check the whole group.
        runner = subprocess.Popen(
            [sys.executable, '-c',
             'import os, subprocess, sys, time\n'
             'subprocess.Popen([sys.executable, "-c", sys.argv[1], str(os.getpid()), sys.argv[2]])\n'
             'time.sleep(600)\n',
             _ARMED_CHILD, self.marker],
            start_new_session=True)
        self.addCleanup(runner.wait)
        self.addCleanup(_killpg_quietly, runner.pid)
        self._wait_for_marker()

        os.kill(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)
        deadline_at = time.monotonic() + 5.0
        while live_group_members(runner.pid) != (0, 0) and time.monotonic() < deadline_at:
            time.sleep(0.02)
        self.assertEqual(live_group_members(runner.pid), (0, 0),
                         'the child outlived its SIGKILLed runner')

    def test_runner_already_gone_exits_before_any_work(self):
        # A PID that is not the child's parent stands for a runner that died
        # before the child armed: the child must not reach its own work.
        result = subprocess.run(
            [sys.executable, '-c', _ARMED_CHILD, str(os.getppid()), self.marker],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, RUNNER_GONE_EXIT, result.stderr)
        self.assertIn('already gone', result.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_prctl_failure_raises_instead_of_running_unprotected(self):
        with mock.patch('libs.proctree.ctypes') as fake_ctypes:
            fake_ctypes.CDLL.return_value.prctl.return_value = -1
            fake_ctypes.get_errno.return_value = errno.EINVAL
            with self.assertRaises(OSError) as caught:
                exit_with_parent(os.getppid())
        self.assertEqual(caught.exception.errno, errno.EINVAL)


def _killpg_quietly(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == '__main__':
    unittest.main()
