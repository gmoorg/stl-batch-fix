"""The raw per-model log beside each output: every tool's own output.

`out/sub/foot.stl` gets `out/sub/foot.log`. A repair child's stdout and
stderr are pointed straight at it, so whatever any library prints — CGAL,
fast_simplification, PyMeshFix, a crash message, a faulthandler traceback —
lands there as it is written. Blender copies its captured output there after
each run. The file is appended to across runs; each attempt starts with a
header line, and each step inside a repair is bracketed by separator lines.

This is raw tool output, not the structured step log (`steplog`,
`batch.log`), and nothing reads it back: no outcome, rerun or recovery
decision depends on it. It is never an expected publication path.
"""

from __future__ import annotations

import datetime
import os


def path_for(destination: str, reserved: tuple[str, ...] = ()) -> str:
    """The model log beside `destination`: same base name, `.log`.

    `reserved` are the run's own log files (`batch.log`, `progress.log`, a
    custom `log_file`). A model whose log would land on one of them — e.g.
    `progress.stl` at the output root — gets `<base>.model.log` instead
    (then `<base>.model.2.log`, … should that be reserved too), so raw tool
    output never corrupts a structured log.
    """
    base = os.path.splitext(destination)[0]
    taken = {os.path.abspath(p) for p in reserved}
    candidates = [base + '.log', base + '.model.log']
    n = 2
    while True:
        for path in candidates:
            if os.path.abspath(path) not in taken:
                return path
        candidates = [f'{base}.model.{n}.log']
        n += 1


def header(run_id: str, stage: str, source: str) -> str:
    """One attempt's header line, starting a visibly new block."""
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return f'\n==== {stamp}  run {run_id}  {stage}: {source} ====\n'


def write_header(path: str, run_id: str, stage: str, source: str) -> None:
    """Append `header(...)` to `path`, creating its folder. Raises OSError;
    the caller decides what a log that cannot be written means."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8', errors='replace') as handle:
        handle.write(header(run_id, stage, source))


def separator(event: str, step: str, part: str, duration: float | None,
              detail: str) -> str:
    """The line bracketing one step's output: `start` before, `end` after."""
    stamp = datetime.datetime.now().strftime('%H:%M:%S')
    if event == 'start':
        return f'---- {stamp}  start {step} [{part}]\n'
    took = '' if duration is None else f' {duration:.2f}s'
    detail = ' '.join(detail.split())          # one line, whatever the detail holds
    return f'---- {stamp}  {event:<5} {step} [{part}]{took}  {detail}\n'
