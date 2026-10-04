"""Kill and confirm the death of a whole process group."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def live_group_members(pgid: int) -> tuple[int, int]:
    """`(live, uncertain)` process counts sharing `pgid`, from `/proc`.

    A zombie (state `Z`) does not count as live — it cannot write to
    anything, it is only waiting to be reaped.  A `/proc/<pid>` entry that
    vanishes mid-read (`ENOENT`/`ESRCH`) is ordinary process exit racing the
    scan, not counted at all.  A permission error or content that does not
    parse as a stat line is real uncertainty — per this project's
    confirmed policy, uncertainty blocks confirmation exactly like a live
    process would, rather than being silently skipped.
    """
    live = uncertain = 0
    try:
        candidates = [name for name in os.listdir('/proc') if name.isdigit()]
    except OSError:
        return 0, 1
    for name in candidates:
        stat_path = f'/proc/{name}/stat'
        try:
            with open(stat_path, 'r') as f:
                content = f.read()
        except (FileNotFoundError, ProcessLookupError):
            continue                           # vanished mid-scan — ordinary exit
        except OSError:
            uncertain += 1
            continue
        try:
            # comm (field 2) is parenthesised and may itself contain ')' or
            # spaces, so split on the LAST ')' to find the fixed fields that
            # follow it reliably, per the conventional /proc/<pid>/stat
            # parsing approach.
            after_comm = content.rsplit(')', 1)[1].split()
            state = after_comm[0]
            entry_pgid = int(after_comm[2])
        except (IndexError, ValueError):
            uncertain += 1
            continue
        if entry_pgid == pgid:
            if state == 'Z':
                continue                       # zombie — cannot write, doesn't block confirmation
            live += 1
    return live, uncertain


def terminate_and_confirm(proc: subprocess.Popen, deadline: float
                           ) -> tuple[bool, str | None]:
    """Kill `proc`'s whole process group and confirm nothing in it survives.

    `proc` must have been spawned with `start_new_session=True`, making its
    PID also its process group ID — `os.killpg(proc.pid, ...)` then reaches
    it and every descendant that has not itself called `setsid`/`setpgid`
    (a documented, accepted limitation: this project's only descendant,
    Blender via `libs/blender.py`'s `Runner`, does not do that).

    Confirmation requires BOTH the direct child being reaped (a `/proc`
    sweep alone can miss an unreaped zombie that is nonetheless the direct
    child) AND no other live process sharing its process group ID.  Bounded
    by `deadline` seconds; returns `(False, detail)` rather than blocking
    forever when the kernel has not caught up within that budget.
    """
    kill_error: str | None = None
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass                                # group already gone — nothing to record
    except Exception as exc:               # noqa: BLE001 — recorded, cleanup still attempted
        kill_error = str(exc)

    deadline_at = time.monotonic() + deadline
    reaped = False
    while time.monotonic() < deadline_at:
        try:
            proc.wait(timeout=0.02)
            reaped = True
            break
        except subprocess.TimeoutExpired:
            continue
    if not reaped:
        detail = f'direct child not reaped within {deadline}s'
        if kill_error:
            detail += f'; kill error: {kill_error}'
        return False, detail

    while time.monotonic() < deadline_at:
        live, uncertain = live_group_members(proc.pid)
        if live == 0 and uncertain == 0:
            return True, None
        time.sleep(0.02)
    live, uncertain = live_group_members(proc.pid)
    if live == 0 and uncertain == 0:
        return True, None
    detail = f'{live} live + {uncertain} unparseable group member(s) after {deadline}s'
    if kill_error:
        detail += f'; kill error: {kill_error}'
    return False, detail
