"""Run one task's Codex sessions, keeping each role's history separate."""

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID


PROJECT_DIR = Path(__file__).resolve().parent.parent
ROLES = ("planning", "review", "implementation")


def run_session(project, role, prompt):
    state = project / ".codex-collaboration"
    state.mkdir(mode=0o700, exist_ok=True)
    # Keep the lock file in place, including across resets.
    with (state / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another collaboration call is running; wait before retrying")

        if role == "reset":
            for name in ROLES:
                (state / f"{name}.id").unlink(missing_ok=True)
            print("Codex session pointers reset. Clear Claude separately with /clear.")
            return 0

        session_file = state / f"{role}.id"
        session_id = session_file.read_text().strip() if session_file.exists() else None
        if session_id is not None:
            UUID(session_id)  # Reject corrupt state instead of choosing another session.
        executable = subprocess.check_output(
            [str(project / "tools/find_codex.sh")], text=True
        ).strip()
        command = [executable, "exec", "--cd", str(project), "--json", "--color", "never"]
        command += ["--approve-for-me"] if role == "implementation" else ["--sandbox", "read-only"]
        if session_id:
            command += ["resume", session_id]
        command += ["-"]
        print(f"Codex {role}: {'resuming ' + session_id if session_id else 'starting fresh'}", file=sys.stderr)

        completed = False
        failed = False
        seen_id = None
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    event = json.loads(line)
                    kind = event.get("type")
                    if kind == "thread.started":
                        seen_id = str(UUID(event["thread_id"]))
                        if session_id and seen_id != session_id:
                            raise RuntimeError("Codex resumed a different session; reset explicitly to start fresh")
                        temporary = session_file.with_suffix(".tmp")
                        temporary.write_text(seen_id + "\n")
                        temporary.replace(session_file)
                    elif kind == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            print(item.get("text", ""), flush=True)
                    elif kind == "turn.completed":
                        completed = True
                    elif kind in ("error", "turn.failed"):
                        failed = True
                        print(json.dumps(event), file=sys.stderr)
                result = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if result:
            return result if result > 0 else 128 - result
        if failed or not completed or not seen_id:
            raise RuntimeError("Codex did not report a completed turn and session ID; saved state retained")
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=(*ROLES, "reset"))
    args = parser.parse_args()
    prompt = "" if args.role == "reset" else sys.stdin.read()
    if args.role != "reset" and not prompt.strip():
        parser.error("a non-empty prompt is required on stdin")
    try:
        return run_session(PROJECT_DIR, args.role, prompt)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"codex_session: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
