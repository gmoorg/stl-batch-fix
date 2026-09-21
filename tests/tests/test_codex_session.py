"""Offline checks for collaboration routing, persistence and failure handling."""

from contextlib import redirect_stdout, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_session import run_session


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        (self.project / "tools").mkdir()
        source = Path(__file__).resolve().parents[2] / "tools"
        for name in ("ask_codex.sh", "run_codex.sh", "reset_codex.sh", "project_python.sh", "codex_session.py"):
            shutil.copy2(source / name, self.project / "tools" / name)
        python_dir = Path(self.temporary.name) / ".venv/bin"
        python_dir.mkdir(parents=True)
        (python_dir / "python").symlink_to(os.sys.executable)
        finder = self.project / "tools/find_codex.sh"
        finder.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEX_BIN"\n')
        finder.chmod(0o755)
        fake = self.project / "fake_codex"
        fake.write_text(f"#!{os.sys.executable}\n" + '''
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
project = Path(args[args.index('--cd') + 1])
with (project / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'args': args, 'prompt': sys.stdin.read()}) + '\\n')
mode = os.environ.get('FAKE_MODE', '')
if mode == 'early_failure':
    sys.exit(7)
session = args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())
if mode == 'wrong_id':
    session = str(uuid.uuid4())
print(json.dumps({'type': 'thread.started', 'thread_id': session}))
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'AGREE'}}))
if mode == 'failure':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'test failure'}}))
    sys.exit(9)
if mode != 'incomplete':
    print(json.dumps({'type': 'turn.completed'}))
''')
        fake.chmod(0o755)
        self.environment = patch.dict(os.environ, CODEX_BIN=str(fake), FAKE_MODE="")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def run_role(self, role, prompt="Task context"):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return run_session(self.project, role, prompt)

    def calls(self):
        return [json.loads(line) for line in (self.project / "calls.jsonl").read_text().splitlines()]

    def saved_id(self, role):
        return (self.project / ".codex-collaboration" / f"{role}.id").read_text().strip()

    def test_role_isolation_and_explicit_resume(self):
        for role in ("planning", "review", "implementation"):
            self.assertEqual(self.run_role(role), 0)
            self.assertEqual(self.run_role(role, "Follow-up"), 0)
        self.assertEqual(len({self.saved_id(role) for role in ("planning", "review", "implementation")}), 3)
        for index, role in enumerate(("planning", "review", "implementation")):
            first, second = self.calls()[index * 2:index * 2 + 2]
            self.assertNotIn("resume", first["args"])
            self.assertIn(self.saved_id(role), second["args"])
            self.assertEqual(second["prompt"], "Follow-up")
            for call in (first, second):
                self.assertNotIn("--last", call["args"])
                self.assertNotIn("--ephemeral", call["args"])
                self.assertEqual("--approve-for-me" in call["args"], role == "implementation")
                self.assertEqual("read-only" in call["args"], role != "implementation")
        self.assertEqual(self.stdout.getvalue(), "AGREE\n" * 6)

    def test_reset_starts_fresh_and_preserves_project(self):
        self.run_role("planning")
        old = self.saved_id("planning")
        self.run_role("review")
        self.run_role("implementation")
        self.run_role("reset")
        self.assertFalse(list((self.project / ".codex-collaboration").glob("*.id")))
        self.assertTrue((self.project / "calls.jsonl").exists())
        self.run_role("planning")
        self.assertNotEqual(old, self.saved_id("planning"))
        self.assertNotIn("resume", self.calls()[-1]["args"])

    def test_failed_turn_preserves_exit_code_and_session_for_retry(self):
        with patch.dict(os.environ, FAKE_MODE="failure"):
            self.assertEqual(self.run_role("planning"), 9)
        old = self.saved_id("planning")
        self.assertEqual(self.run_role("planning"), 0)
        self.assertIn(old, self.calls()[-1]["args"])

    def test_failure_before_session_does_not_create_pointer(self):
        with patch.dict(os.environ, FAKE_MODE="early_failure"):
            self.assertEqual(self.run_role("review"), 7)
        self.assertFalse((self.project / ".codex-collaboration/review.id").exists())

    def test_incomplete_turn_is_not_success(self):
        with patch.dict(os.environ, FAKE_MODE="incomplete"):
            with self.assertRaisesRegex(RuntimeError, "completed turn"):
                self.run_role("planning")

    def test_unexpected_session_does_not_replace_pointer(self):
        self.run_role("planning")
        old = self.saved_id("planning")
        with patch.dict(os.environ, FAKE_MODE="wrong_id"):
            with self.assertRaisesRegex(RuntimeError, "different session"):
                self.run_role("planning")
        self.assertEqual(old, self.saved_id("planning"))

    def test_corrupt_pointer_fails_without_launch(self):
        self.run_role("planning")
        (self.project / ".codex-collaboration/planning.id").write_text("invalid")
        with self.assertRaises(ValueError):
            self.run_role("planning")
        self.assertEqual(len(self.calls()), 1)

    def test_lock_blocks_calls_and_reset(self):
        self.run_role("planning")
        with (self.project / ".codex-collaboration/lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for role in ("review", "reset"):
                with self.assertRaisesRegex(RuntimeError, "another collaboration call"):
                    self.run_role(role)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.saved_id("planning"))

    def test_shell_wrappers_route_first_line_and_work_from_other_directory(self):
        def invoke(name, request=""):
            return subprocess.run(
                [str(self.project / "tools" / name)], input=request,
                text=True, capture_output=True, cwd="/tmp", check=True,
            )

        invoke("ask_codex.sh", "PHASE: INTERPRETATION\nOriginal prompt")
        planning = self.saved_id("planning")
        invoke("ask_codex.sh", "PHASE: PLAN\nQuoted example: PHASE: REVIEW")
        self.assertIn(planning, self.calls()[-1]["args"])
        invoke("ask_codex.sh", "PHASE: REVIEW\nQuoted example: PHASE: PLAN")
        self.assertNotIn("resume", self.calls()[-1]["args"])
        reviewer = self.saved_id("review")
        invoke("ask_codex.sh", "PHASE: REVIEW\nFixes")
        self.assertIn(reviewer, self.calls()[-1]["args"])
        invoke("run_codex.sh", "Agreed plan")
        self.assertIn("--approve-for-me", self.calls()[-1]["args"])
        invoke("reset_codex.sh")
        self.assertFalse(list((self.project / ".codex-collaboration").glob("*.id")))

    def test_invalid_phase_is_rejected_before_cli_launch(self):
        result = subprocess.run(
            [str(self.project / "tools/ask_codex.sh")],
            input="Missing phase\nQuoted PHASE: PLAN", text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.project / "calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
