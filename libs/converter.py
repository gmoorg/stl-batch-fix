"""Prepare a source tree and emit each mesh to a consumer.

`prepare` copies companion files, converts ASCII STL/OBJ when needed, and
emits valid and failed meshes. Emission is serialized even when conversion
uses workers. Arrival order is unspecified; the consumer owns sorting.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from . import indicators, mesh_io
from .indicators import Indicator
from .mesh_io import Kind, Mesh
from .pool import Pool


@dataclass
class Summary:
    """What the walk did, for a caller that wants to report on it.

    Counts only — the interesting detail reached `emit` as it happened.
    """

    scanned: int = 0
    copied: int = 0
    copy_failed: int = 0
    already_copied: int = 0
    skipped: int = 0          # already fixed, or a marker said do not retry
    emitted: int = 0          # handed to the consumer, valid or not
    converted: int = 0
    conversion_failed: int = 0


def _walk(root: str) -> Iterator[str]:
    """Every file under `root`, skipping the trees we write ourselves."""
    skip = {indicators.EXPORT_DIRNAME, '~parts', '__MACOSX'}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in names:
            # AppleDouble sidecars: metadata, no geometry, and they match *.stl.
            if name.startswith('._'):
                continue
            yield os.path.join(base, name)


def _output_for(source: str, source_root: str, output_root: str) -> str:
    """Where `source` would land in the output tree, same relative path."""
    rel = os.path.relpath(os.path.abspath(source), os.path.abspath(source_root))
    base, ext = os.path.splitext(rel)
    # OBJ becomes STL on the way out; everything else keeps its extension.
    out_ext = '.stl' if ext.lower() == '.obj' else ext
    return os.path.join(output_root, base + out_ext)


def _conversion_failure(source: str, destination: str, reason: str) -> Mesh:
    """Build the invalid mesh record used when normalization did not happen."""
    return Mesh(source, destination, mesh_io.kind(source), None, False, reason)


def prepare(source_root: str,
            output_root: str,
            emit: Callable[[Mesh], None],
            copy_extensions: Iterable[str] = (),
            convert: Callable[[str, str], tuple[bool, str]] | None = None,
            workers: int = 4) -> Summary:
    """Classify everything under `source_root`, then feed `emit`.

    `convert(source, destination) -> (ok, path)` does the format conversion —
    `libs.blender.convert` has this shape.  Without it, files needing one are
    emitted as invalid rather than silently dropped.

    Two phases, because a converted file's triangle count does not exist until
    Blender has written it:

    1. **Walk.** Classify every file.  Copy the companions, emit the binary
       STLs, set the rest aside.  Copies happen here rather than in the pool:
       they are fast enough not to matter and keeping them inline is less
       machinery.
    2. **Convert.** Drain the set-aside files through a pool, probing each
       result and emitting it.
    """
    copy_set = frozenset(e.lower() for e in copy_extensions)
    summary = Summary()
    pending: list[tuple[str, str, str]] = []   # (source, export path, destination)

    # One lock for both the consumer and the counters.  The conversion phase
    # runs several workers and `Pool` serialises only its selector, so
    # everything shared goes through here.  Uncontended during the walk.
    emit_lock = threading.Lock()

    def emit_one(mesh: Mesh) -> None:
        with emit_lock:
            summary.emitted += 1
            emit(mesh)

    for source in _walk(source_root):
        summary.scanned += 1
        destination = _output_for(source, source_root, output_root)
        found = indicators.check(source, source_root, destination,
                                 copy_extensions=copy_set or None)

        if found.indicator is Indicator.ALREADY_COPIED:
            summary.already_copied += 1
            continue

        if found.indicator is Indicator.COPY_AS_IS:
            try:
                # Staged: `check` reads this same path back as ALREADY_COPIED,
                # so a copy interrupted partway would retire it for good.
                with mesh_io.staged_write(destination) as staged:
                    shutil.copy2(source, staged)
            except OSError:
                # One unreadable companion costs one companion.  Raising here
                # cost every file after it too: the walk is a single pass, so
                # sources it had not reached yet were never even scanned.
                summary.copy_failed += 1
            else:
                summary.copied += 1
            continue

        if found.indicator is Indicator.EXPORT_READY:
            # Converted on an earlier run; measure the conversion, not the
            # source — but it is still bound for the output tree, not for the
            # export folder it happens to be sitting in.
            emit_one(mesh_io.probe(found.path, destination))
            continue

        if found.indicator is not Indicator.PROCESS:
            summary.skipped += 1          # already fixed, or a marker
            continue

        probed = mesh_io.probe(source, destination)
        if probed.needs_conversion:
            pending.append((source, indicators.export_path(source, source_root),
                            destination))
            continue

        emit_one(probed)

    if not pending:
        return summary

    if convert is None:
        for source, _, destination in pending:
            summary.conversion_failed += 1
            emit_one(_conversion_failure(
                source, destination,
                "needs conversion but no converter was supplied"))
        return summary

    def next_item(done, error):
        return pending.pop(0) if pending else None

    def convert_one(item):
        source, export, destination = item
        try:
            ok, path = convert(source, export)
        except Exception as exc:          # noqa: BLE001 — emitted, not raised
            # A converter that throws and one that returns False report the
            # same event: this file was not converted.  Handled here rather
            # than through the pool's `error` argument, because the pool hands
            # that to the *selector*, which knows only that an item finished —
            # not which result it should have produced.  Catching it beside the
            # call keeps the failure attached to its own source.
            ok, path = False, export
            reason = f"conversion raised {type(exc).__name__}: {exc}"
        else:
            reason = "conversion failed"
        result = (mesh_io.probe(path, destination) if ok else
              _conversion_failure(source, destination, reason))
        # Everything shared goes through emit_one's lock, including these
        # counters.  Two cleverer arrangements were tried first and both were
        # wrong: counting inside the pool's selector looks free, since the pool
        # serialises that call — but the selector is told only *that* an item
        # finished, not which result it produced, so pairing a completion with
        # its outcome meant popping a shared list, and workers finishing out of
        # order attributed the wrong ones.  One lock needs no reasoning about
        # which call the pool happens to serialise.
        with emit_lock:
            if ok:
                summary.converted += 1
            else:
                summary.conversion_failed += 1
        emit_one(result)

    Pool(workers, next_item, convert_one).start()
    return summary
