"""What the filesystem already says about a source file.

Detection only.  This module decides nothing: it looks for the marker files a
previous run may have left, reports the last one it finds, and stops there.
Whether a finding means skip, convert or process is the caller's judgement.

Two trees are involved, which is the reason this is a module rather than a
handful of `os.path.exists` calls at a call site:

    SOURCE tree                 <input>/stl-exported/<rel>.stl
    OUTPUT tree                 <output>/<rel>.stl        and its markers

The source tree holds exports — an ASCII STL or OBJ converted to binary by a
previous run.  Finding one means "use this file instead of the original", not
"skip".  The output tree holds the repaired file and the markers describing how
a previous attempt ended; any of those means the file has been dealt with.

Markers are named `<base>.<signal>.stl` and are full copies of the mesh, so
they open in any STL viewer.  Deleting one is how a file is retried.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class Indicator(Enum):
    """What was found on disk, in the order the checks run.

    The order matters only in that the **last** match is reported.  Among the
    output-tree markers that choice is cosmetic: every one of them means the
    file has already been dealt with, so which is named affects the message and
    not the outcome.
    """

    PROCESS = 'process'              # nothing found — use the source as it is
    EXPORT_READY = 'export_ready'    # a converted binary exists; use that path
    ALREADY_FIXED = 'already_fixed'  # the repaired output is present
    BROKEN = 'broken'                # unreadable mesh; never retried
    FAILED = 'failed'                # transient failure; delete to retry
    UNREPAIRED = 'unrepaired'        # non-manifold edges remained
    OPEN_EDGES = 'open_edges'        # nm clean, open edges remained
    TIMED_OUT = 'timed_out'          # killed by the watchdog


#: Output-tree markers, checked in this order.  `.original.stl` is deliberately
#: absent: it sits beside a *successful* output as evidence that the repair
#: moved the bounding box, so treating it as an indicator would skip files that
#: actually worked.
_OUTPUT_MARKERS: tuple[tuple[str, Indicator], ...] = (
    ('.broken.stl', Indicator.BROKEN),
    ('.failed.stl', Indicator.FAILED),
    ('.unrepaired.stl', Indicator.UNREPAIRED),
    ('.open.stl', Indicator.OPEN_EDGES),
    ('.timeout.stl', Indicator.TIMED_OUT),
)

EXPORT_DIRNAME = 'stl-exported'


@dataclass(frozen=True)
class Finding:
    """What was found, and the file that says so.

    source     the file that was asked about
    indicator  what the filesystem says about it
    path       the file that produced the finding — the export to use, the
               output already written, or the marker left behind.  None when
               the indicator is PROCESS, because nothing was found.
    """

    source: str
    indicator: Indicator
    path: str | None = None

    @property
    def found(self) -> bool:
        """True when anything at all was found."""
        return self.indicator is not Indicator.PROCESS


def export_path(source: str, input_folder: str) -> str:
    """Where a converted binary for `source` would live.

    Inside the source tree, in one dedicated folder, at the same relative path
    with a `.stl` extension.  A dedicated folder is one directory to delete and
    is obviously not originals — see D7.
    """
    input_folder = os.path.abspath(input_folder)
    rel = os.path.relpath(os.path.abspath(source), input_folder)
    base, _ = os.path.splitext(rel)
    return os.path.join(input_folder, EXPORT_DIRNAME, base + '.stl')


def check(source: str, input_folder: str, output_file: str) -> Finding:
    """Report what the filesystem already says about `source`.

    `output_file` is where the repaired result would be written; the markers
    are its siblings, named from the same base.  It is passed in rather than
    derived so this module needs no opinion about output layout.

    Checks run in `Indicator` order and the **last** match is reported.  The
    export is therefore outranked by anything in the output tree, which is
    correct: converting a file that is not going to be processed is wasted
    work.
    """
    found = Finding(source, Indicator.PROCESS)

    export = export_path(source, input_folder)
    if os.path.exists(export):
        found = Finding(source, Indicator.EXPORT_READY, export)

    if os.path.exists(output_file):
        found = Finding(source, Indicator.ALREADY_FIXED, output_file)

    base, _ = os.path.splitext(output_file)
    for suffix, indicator in _OUTPUT_MARKERS:
        marker = base + suffix
        if os.path.exists(marker):
            found = Finding(source, indicator, marker)

    return found


def needs_export(source: str) -> bool:
    """True when `source` is a format that must be converted before processing.

    A name test only — no file is opened.  Whether an `.stl` is ASCII cannot be
    answered without reading it, so that decision belongs with whatever does
    the reading; this covers only the case the extension settles.
    """
    return os.path.splitext(source)[1].lower() == '.obj'
