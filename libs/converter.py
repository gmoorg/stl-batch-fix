"""Walk a source tree and hand every usable mesh to a consumer.

The preparation stage: it decides what each file *is*, copies the things that
are not meshes, converts the ones that cannot be measured as they stand, and
emits the rest.  What the consumer does with them — sort them, queue them, log
them — is not this module's business.

    prepare(source_root, output_root, emit,
            copy_extensions=..., convert=..., workers=4)

`emit(mesh)` is called once per file that resolved to a mesh, **one at a time**
— `prepare` serialises it, so a consumer needs no locking of its own even
though the conversion phase runs several workers.  The lock is uncontended
during the walk and costs microseconds across a whole collection, which beats a
contract reading "serial here, concurrent there, lock accordingly": a caller
cannot forget a lock that is not theirs to take.

**Order is not promised**: binary files arrive during the walk and converted
ones afterwards, so a consumer that needs them sorted sorts what it collects.
That is no loss — the repair queue has to be sorted by triangle count for
memory admission regardless (D13), and only the consumer knows that.

Returning a `queue.Queue` instead was considered and rejected.  It would be
thread-safe for free and would let a consumer start work before the walk
finished — but nothing *can* start early, because the queue has to be complete
before it can be sorted, and `Pool`'s selector shuts a worker down rather than
waiting, so a queue that filled gradually would shed every worker and end the
run.  The overlap a queue buys is overlap this pipeline cannot use, against a
sentinel protocol and a thread for the caller to manage.

Failures are emitted too, as a `Mesh` with `is_valid=False` and a `problem`.
A file that simply cannot be converted would otherwise be the one thing in the
pipeline that vanishes without appearing in any count — the consumer can
ignore it, but it cannot report what it never sees.
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
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
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
            emit_one(Mesh(source, destination, mesh_io.kind(source), None,
                          False,
                          "needs conversion but no converter was supplied"))
        return summary

    def next_item(done, error):
        return pending.pop(0) if pending else None

    def convert_one(item):
        source, export, destination = item
        ok, path = convert(source, export)
        result = (mesh_io.probe(path, destination) if ok
                  else Mesh(source, destination, mesh_io.kind(source), None,
                            False, "conversion failed"))
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
