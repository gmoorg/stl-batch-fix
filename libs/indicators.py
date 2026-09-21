"""Read existing outputs and markers for a source file.

An export in `stl-exported/` replaces the source path; an output or marker
means a previous attempt already dealt with the file. Markers are full mesh
copies. This module reports the finding; the caller chooses what to do.
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
    BROKEN = 'broken'                # unusable mesh; never retried
    FAILED = 'failed'                # transient failure; delete to retry
    UNREPAIRED = 'unrepaired'        # non-manifold edges remained
    OPEN_EDGES = 'open_edges'        # nm clean, open edges remained
    DESTROYED = 'destroyed'          # the repair deleted geometry
    TIMED_OUT = 'timed_out'          # killed by the watchdog
    UNDECIMATED = 'undecimated'      # every decimator failed; still oversized


#: Output markers take precedence over exports. `.original.stl` is absent:
#: it accompanies a successful output. DESTROYED carries a source fallback;
#: UNREPAIRED and OPEN_EDGES carry the repaired mesh.
_OUTPUT_MARKERS: tuple[tuple[str, Indicator], ...] = (
    ('.broken.stl', Indicator.BROKEN),
    ('.failed.stl', Indicator.FAILED),
    ('.unrepaired.stl', Indicator.UNREPAIRED),
    ('.open.stl', Indicator.OPEN_EDGES),
    ('.destroyed.stl', Indicator.DESTROYED),
    ('.timeout.stl', Indicator.TIMED_OUT),
    ('.undecimated.stl', Indicator.UNDECIMATED),
)

_MARKER_SUFFIX: dict[Indicator, str] = {
    indicator: suffix for suffix, indicator in _OUTPUT_MARKERS
}

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


def marker_suffix(indicator: Indicator) -> str | None:
    """Return the output filename suffix for a marker indicator."""
    return _MARKER_SUFFIX.get(indicator)


def check(source: str, input_folder: str, output_file: str,
          copy_extensions: frozenset[str] | set[str] | None = None) -> Finding:
    """Report the first applicable filesystem finding for `source`.

    Companion extensions, when supplied, yield COPY_AS_IS or ALREADY_COPIED.
    Otherwise output markers and the output file take precedence over an export;
    `output_file` is supplied so this module does not own output layout.
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
