"""Prepare a source tree and emit each mesh to a consumer."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from . import indicators, mesh_io
from .indicators import Indicator
from .mesh_io import Mesh
from .textmesh import Malformed

#: What the walk treats as a mesh, compared case-insensitively.  Content is
#: never used to decide this: `mesh_io.kind` sniffs, and anything that does
#: not look like ASCII STL sniffs as binary STL — a stray .zip or .3mf in the
#: input tree would be parsed as one.
MESH_EXTENSIONS = frozenset({'.stl', '.obj'})


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
    ignored: int = 0          # neither a mesh nor a companion; not touched
    emitted: int = 0          # handed to the consumer, valid or not
    converted: int = 0
    conversion_failed: int = 0   # malformed ones included
    malformed: int = 0           # the batch run writes their FAILED markers


def _walk(root: str) -> Iterator[str]:
    """Every file under `root`, skipping the trees we write ourselves."""
    skip = {indicators.EXPORT_DIRNAME, indicators.DECIMATED_DIRNAME, '~parts', '__MACOSX'}
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


def _conversion_failure(source: str, destination: str, reason: str, *,
                        malformed: bool = False) -> Mesh:
    """Build the invalid mesh record used when normalization did not happen."""
    return Mesh(source, destination, mesh_io.kind(source), None, False, reason,
                malformed=malformed)


def prepare(source_root: str,
            output_root: str,
            emit: Callable[[Mesh], None],
            copy_extensions: Iterable[str] = (),
            convert: Callable[..., None] | None = None,
            *,
            mesh_extensions: Iterable[str] = MESH_EXTENSIONS) -> Summary:
    """Classify everything under `source_root`, then feed `emit`.

    Only files whose extension (any letter case) is in `mesh_extensions` are
    meshes, and only those in `copy_extensions` are companions.  Everything
    else is counted as `ignored` and never looked at again — no probe, no
    conversion, no copy, and no output, marker or export is consulted for it.
    An extension may not be in both sets.

    `convert(source, export, *, model_destination)` does the format
    conversion, writing `export`, and raises when it could not:
    `textmesh.Malformed` emits an invalid mesh marked `malformed` (the batch
    run writes its FAILED marker once no other job claims the same paths);
    any other `Exception` emits a plain invalid mesh. `model_destination` is
    the output path the converted file will be repaired to, so the converter
    can log against that model (`libs.modellog`). Without `convert`, files
    needing one are emitted as invalid rather than silently dropped.

    Two phases, because a converted file's triangle count does not exist until
    its conversion has been written:

    1. **Walk.** Classify every file.  Copy the companions, emit the binary
       STLs, set the rest aside.
    2. **Convert.** Convert the set-aside files one at a time in this thread,
       probing each result and emitting it. Not a thread pool: the converter
       is Python holding the GIL, so threads would not run it faster, and in
       this thread a Ctrl+C stops the conversion itself (its staged export is
       removed) instead of leaving a worker publishing after the caller gave up.
    """
    copy_set = frozenset(e.lower() for e in copy_extensions)
    mesh_set = frozenset(e.lower() for e in mesh_extensions)
    if copy_set & mesh_set:
        raise ValueError('extensions both copied and processed as meshes: '
                         + ', '.join(sorted(copy_set & mesh_set)))
    summary = Summary()
    pending: list[tuple[str, str, str]] = []   # (source, export path, destination)

    def emit_one(mesh: Mesh) -> None:
        summary.emitted += 1
        emit(mesh)

    for source in _walk(source_root):
        summary.scanned += 1
        ext = os.path.splitext(source)[1].lower()
        if ext not in copy_set and ext not in mesh_set:
            summary.ignored += 1
            continue
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

    for source, export, destination in pending:
        if convert is None:
            summary.conversion_failed += 1
            emit_one(_conversion_failure(
                source, destination,
                "needs conversion but no converter was supplied"))
            continue
        try:
            convert(source, export, model_destination=destination)
        except Malformed as exc:
            summary.conversion_failed += 1
            summary.malformed += 1
            emit_one(_conversion_failure(source, destination,
                                         f"malformed source: {exc}",
                                         malformed=True))
        except Exception as exc:          # noqa: BLE001 — emitted, not raised
            # One file that cannot be converted costs that file only; it is
            # emitted invalid, so it is reported rather than vanishing.
            summary.conversion_failed += 1
            emit_one(_conversion_failure(
                source, destination,
                f"conversion failed: {type(exc).__name__}: {exc}"))
        else:
            summary.converted += 1
            emit_one(mesh_io.probe(export, destination))
    return summary
