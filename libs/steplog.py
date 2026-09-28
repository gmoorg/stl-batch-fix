"""A shared, append-only, before/after-per-step log for one batch run.

Every `--one-file` child (a separate OS process) writes to the SAME file at
the output tree's root, so a crash mid-repair still leaves a record of
which step was running and for how long — the whole reason this exists:
`ChildResult`'s own step summary is only written once, atomically, at the
very end of a successful run, so it is invisible on exactly the file most
worth investigating.

Concurrent-process safety: each write is one line, appended with `O_APPEND`.
POSIX guarantees a single `write()` under `PIPE_BUF` (typically 4096 bytes)
is atomic even across processes sharing the same open file description
mode — so independent child processes, each with their own `open(path,
'a')`, can interleave complete lines without a lock or without truncating
each other's output. A line here is always short (one step, one message),
so this holds in practice; no line-length enforcement is added because a
pathological detail string would already be a bug elsewhere.

Line format, tab-delimited, one line per event:

    HH:MM:SS\tsource\tevent\tstep\tduration\tdetail

`step` is a stable identifier (`decimate`, `split`, `alpha_wrap`, `merge`,
`judge`, `scan`, ...) — always the same string for the same kind of work,
never embedding a free-text detail, so a separate tool can filter/group by
it directly (`awk -F'\t' '$4 == "alpha_wrap"'`) without regexing prose.
`duration` is empty on a `start` line and a plain float (seconds) on `end`;
`detail` is free text and may itself contain no tabs or newlines (single
line, tab-free strings enforced by `log()` below) but can contain anything
else, including colons/parens, since it is always the LAST field.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import time
from collections.abc import Callable, Iterator

#: (source_name, event, step, duration, detail) -> None.
#: `event` is 'start' or 'end' (a before/after pair) or 'info' (a
#: single-line, no-duration event for work that happens inline within an
#: already-logged step's window rather than getting its own timed pair).
#: `duration` is None on 'start'/'info', a float number of seconds on 'end'.
StepLogger = Callable[[str, str, str, float | None, str], None]


def open_step_log(path: str) -> StepLogger:
    """Return a logger appending timestamped lines to `path`.

    Opened once per child process, in append mode, line-buffered so each
    call's line reaches disk before the next step can begin (and therefore
    before that step has a chance to crash the process).
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    f = open(path, 'a', buffering=1)

    def log(source_name: str, event: str, step: str,
            duration: float | None, detail: str) -> None:
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        duration_field = '' if duration is None else f'{duration:.3f}'
        # A tab or newline in a field would silently corrupt the column
        # count for every downstream row — replaced rather than left to
        # break another tool's parser far from where it happened.
        clean_detail = detail.replace('\t', ' ').replace('\n', ' ')
        f.write(f'{stamp}\t{source_name}\t{event}\t{step}\t{duration_field}\t{clean_detail}\n')

    return log


def null_logger(source_name: str, event: str, step: str,
                duration: float | None, detail: str) -> None:
    """A `StepLogger` that discards everything — the default when no log
    file was configured, so callers never need an `if step_logger:` guard."""


@contextlib.contextmanager
def logged_step(step_logger: StepLogger, source_name: str, step: str,
                start_detail: str = '') -> Iterator[Callable[[str], None]]:
    """Log `step`'s 'start' now; yield a function the caller calls to log
    its 'end', with the elapsed time computed automatically.

    Every `start`/`end` pair in `repairer.py`/`processor.py` used to
    hand-write `mark = time.monotonic()` before the work and
    `time.monotonic() - mark` at each of its (possibly several, one per
    branch) exit points — repeated at every call site, and a chance for one
    branch to be missed or to use the wrong `mark`. This captures `mark`
    once, on entry, and closes over it so a caller can only ever compute
    the SAME start time's elapsed duration, however many end-message
    branches it has:

        with logged_step(step_logger, source_name, 'decimate', 'N faces in') as end:
            result = do_work()
            end(f'{result} faces out')            # or, on another branch:
            end(f'FAILED, {reason}')

    Deliberately not a single wrapper that also runs the work and logs
    'end' by itself: every real call site's 'end' message depends on the
    work's own result (or which of several failure branches it took), so
    the message has to be built by the caller, after the work returns —
    this only removes the timing bookkeeping around that, not the message.
    """
    mark = time.monotonic()
    step_logger(source_name, 'start', step, None, start_detail)

    def end(detail: str) -> None:
        step_logger(source_name, 'end', step, time.monotonic() - mark, detail)

    yield end


@contextlib.contextmanager
def timed_info(step_logger: StepLogger, source_name: str,
              step: str) -> Iterator[Callable[[str], None]]:
    """Time one block of work; yield a function to log it as a single
    'info' line (not a start/end pair) once the caller has a result to
    describe.

    For work that happens INSIDE an already-logged step's own start/end
    window (e.g. `splitter.by_shells`'s scan, called from inside SPLIT's
    window in `repair`) — logging a separate start/end pair for it would
    misleadingly suggest it is its own top-level step, when it is really a
    sub-event worth timing but not worth bracketing.
    """
    mark = time.monotonic()

    def report(detail: str) -> None:
        step_logger(source_name, 'info', step, time.monotonic() - mark, detail)

    yield report
