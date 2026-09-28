"""Process-group kill and confirmation, against real subprocess fixtures."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from libs.proctree import terminate_and_confirm


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


if __name__ == '__main__':
    unittest.main()
