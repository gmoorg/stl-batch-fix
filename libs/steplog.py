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
each other's output.

Line format, tab-delimited, one line per event:

    HH:MM:SS\tsource\tevent\tstep\tpart\tduration\tdetail

`step` is a stable identifier (`decimate`, `split`, `alpha_wrap`, `merge`,
`judge`, `scan`, ...). `part` identifies which split part this event is
about — `f'{index+1}/{n}'` (e.g. `'1/2'`) for a per-part event, or `'-'`
for a whole-model event (no single part, e.g. split/merge/scan_diagonal).
`duration` is empty on a `start` line and a plain float (seconds) on `end`.
`source` and `detail` are escaped via `_escape` below so they can never
corrupt the column count, however `detail` is always the LAST field so it
may still contain tabs/newlines in escaped form.

**Format migration — self-describing by field count, not a fresh-file
requirement.** `batch.log` is opened in append mode and may already contain
old-format (6-field / 5-tab) lines from a previous run reusing the same
`--output`, written before the `part` column existed. New-format (7-field /
6-tab) lines are appended alongside them without rewriting or migrating old
ones. A reader distinguishes old vs. new rows by `len(line.split('\t'))`
(6 vs 7) — no project code currently parses `batch.log` programmatically
(verified via grep), so no existing reader needs updating for this; a
future one just needs to branch on field count.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import time
from collections.abc import Callable, Iterator

#: (source_name, event, step, part, duration, detail) -> None.
#: `event` is 'start' or 'end' (a before/after pair) or 'info' (a
#: single-line, no-duration event for work that happens inline within an
#: already-logged step's window rather than getting its own timed pair).
#: `part` is REQUIRED (no default at this type level — see module
#: docstring's format-migration note): `f'{index+1}/{n}'` for a per-part
#: event, `'-'` for a whole-model event. `duration` is None on
#: 'start'/'info', a float number of seconds on 'end'.
StepLogger = Callable[[str, str, str, str, float | None, str], None]


def _escape(field: str) -> str:
    """Reversibly escape `field` for one tab-delimited log column.

    Single-pass, left-to-right, in this exact order: `\\` -> `\\\\` FIRST
    (so a literal backslash is never re-escaped by a later replacement),
    then `\t` -> `\\t`, `\n` -> `\\n`, `\r` -> `\\r`. A literal `\`
    immediately followed by a literal `t` becomes `\\t`, distinguishable
    from an actual tab, which becomes `\t` — so `_escape("a\tb.stl")` and
    `_escape("a b.stl")` remain distinguishable, unlike the old lossy
    tab/newline-to-space replacement. There is no production `_unescape` —
    no current reader needs one; tests get their own small decoder.
    """
    return (field.replace('\\', '\\\\')
                .replace('\t', '\\t')
                .replace('\n', '\\n')
                .replace('\r', '\\r'))


def open_step_log(path: str) -> StepLogger:
    """Return a logger appending timestamped lines to `path`.

    Opened once per child process, in append mode, line-buffered so each
    call's line reaches disk before the next step can begin (and therefore
    before that step has a chance to crash the process).
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    f = open(path, 'a', buffering=1)

    def log(source_name: str, event: str, step: str, part: str,
            duration: float | None, detail: str) -> None:
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        duration_field = '' if duration is None else f'{duration:.3f}'
        f.write(f'{stamp}\t{_escape(source_name)}\t{event}\t{step}\t{part}\t'
                f'{duration_field}\t{_escape(detail)}\n')

    return log


def null_logger(source_name: str, event: str, step: str, part: str,
                duration: float | None, detail: str) -> None:
    """A `StepLogger` that discards everything — the default when no log
    file was configured, so callers never need an `if step_logger:` guard."""


@contextlib.contextmanager
def logged_step(step_logger: StepLogger, source_name: str, step: str,
                start_detail: str = '', part: str = '-'
                ) -> Iterator[Callable[[str], None]]:
    """Log `step`'s 'start' now; yield a function the caller calls to log
    its 'end', with the elapsed time computed automatically.

        with logged_step(step_logger, source_name, 'decimate', 'N faces in') as end:
            result = do_work()
            end(f'{result} faces out')

    `part` defaults to `'-'` (whole-model) here — this helper's own default,
    not the raw `StepLogger` callback type's, which requires it explicitly.
    """
    mark = time.monotonic()
    step_logger(source_name, 'start', step, part, None, start_detail)

    def end(detail: str) -> None:
        step_logger(source_name, 'end', step, part, time.monotonic() - mark, detail)

    yield end


@contextlib.contextmanager
def timed_info(step_logger: StepLogger, source_name: str,
              step: str, part: str = '-') -> Iterator[Callable[[str], None]]:
    """Time one block of work; yield a function to log it as a single
    'info' line (not a start/end pair) once the caller has a result to
    describe.

    `part` defaults to `'-'` (whole-model) here, same rationale as
    `logged_step`'s default.
    """
    mark = time.monotonic()

    def report(detail: str) -> None:
        step_logger(source_name, 'info', step, part, time.monotonic() - mark, detail)

    yield report
