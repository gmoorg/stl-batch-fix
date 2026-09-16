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
    """What was found on disk.

    Only one ordering rule matters: the output tree is checked before the
    export, because anything there means the file is not going to be processed
    and converting it would be wasted work.  Among the output-tree markers the
    order is arbitrary — every one of them means the file has already been
    dealt with, so which is named changes the message and not the outcome.
    """

    PROCESS = 'process'              # nothing found — use the source as it is
    COPY_AS_IS = 'copy_as_is'        # not a mesh; copy it to the output tree
    ALREADY_COPIED = 'already_copied'  # not a mesh, and the copy is there
    EXPORT_READY = 'export_ready'    # a converted binary exists; use that path
    ALREADY_FIXED = 'already_fixed'  # the repaired output is present
    BROKEN = 'broken'                # unreadable mesh; never retried
    FAILED = 'failed'                # transient failure; delete to retry
    UNREPAIRED = 'unrepaired'        # non-manifold edges remained
    OPEN_EDGES = 'open_edges'        # nm clean, open edges remained
    TIMED_OUT = 'timed_out'          # killed by the watchdog
    UNDECIMATED = 'undecimated'      # every decimator failed; still oversized


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
    ('.undecimated.stl', Indicator.UNDECIMATED),
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


def check(source: str, input_folder: str, output_file: str,
          copy_extensions: frozenset[str] | set[str] | None = None) -> Finding:
    """Report what the filesystem already says about `source`.

    `output_file` is where the repaired result would be written; the markers
    are its siblings, named from the same base.  It is passed in rather than
    derived so this module needs no opinion about output layout.

    `copy_extensions` is the set of suffixes that are **not meshes** — images,
    READMEs, archives shipped alongside a model.  Pass it and a matching file
    short-circuits everything else, reporting COPY_AS_IS or ALREADY_COPIED.
    The set is injected rather than hardcoded because which extensions count is
    the caller's policy, not a fact about the filesystem.

    That branch runs first, and not merely for speed: none of the mesh markers
    can exist for a `.png`, so testing for a `.broken.stl` beside it is
    meaningless work.  ALREADY_COPIED is deliberately separate from
    ALREADY_FIXED — the same existence test, but "copied" and "repaired" are
    different claims, and collapsing them would make any count of repaired
    files wrong.

    **Otherwise the output tree is checked first and the first match wins.**
    Only one ordering rule matters: anything in the output tree outranks the
    export, because converting a file that is not going to be processed is
    wasted work.  Beyond that the order is arbitrary — every output-tree
    finding means the file has already been dealt with, so which one is named
    changes the message and not the outcome.

    Returning on the first match is the point: once the answer is known, the
    remaining `os.path.exists` calls cannot change it.
    """
    if copy_extensions:
        suffix = os.path.splitext(source)[1].lower()
        if suffix in copy_extensions:
            if os.path.exists(output_file):
                return Finding(source, Indicator.ALREADY_COPIED, output_file)
            return Finding(source, Indicator.COPY_AS_IS, output_file)

    base, _ = os.path.splitext(output_file)
    for suffix, indicator in _OUTPUT_MARKERS:
        marker = base + suffix
        if os.path.exists(marker):
            return Finding(source, indicator, marker)

    if os.path.exists(output_file):
        return Finding(source, Indicator.ALREADY_FIXED, output_file)

    export = export_path(source, input_folder)
    if os.path.exists(export):
        return Finding(source, Indicator.EXPORT_READY, export)

    return Finding(source, Indicator.PROCESS)
