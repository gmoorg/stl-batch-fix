"""Reduce a loaded mesh to a face budget with fast_simplification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np

from . import mesh_io
from .mesh_io import Geometry, Mesh, require_geometry

try:
    import fast_simplification as _fastsimp
    _FASTSIMP = True
except ImportError:                                   # pragma: no cover
    _FASTSIMP = False

class Rung(Enum):
    """Whether the required decimator ran, was unnecessary, or failed."""

    FAST_SIMPLIFICATION = 'fastsimp'
    NOT_NEEDED = 'not_needed'        # already within budget; nothing ran
    FAILED = 'failed'                # fast_simplification failed


@dataclass(frozen=True)
class Result:
    """What decimation did, beside the mesh itself.

    mesh        the decimated mesh — or the input unchanged, when nothing ran
    rung        which implementation produced it
    faces_in    face count before
    faces_out   face count after
    attempts    the required decimator failure, when one occurred
    """

    mesh: Mesh
    rung: Rung
    faces_in: int
    faces_out: int
    attempts: tuple[tuple[Rung, str], ...] = ()

    @property
    def ran(self) -> bool:
        """True when a decimator actually changed the mesh."""
        return self.rung not in (Rung.NOT_NEEDED, Rung.FAILED)


def is_available() -> bool:
    """Whether the required decimator is installed.

    For the startup check.  Decimation is a deliverable rather than an
    optimisation — a run that cannot decimate produces meshes the printer will
    re-decimate on its own, reintroducing the defects this tool exists to
    remove — so a false here is a reason not to start, not a reason to skip.
    """
    return _FASTSIMP


def available_rungs() -> tuple[Rung, ...]:
    """Report whether the required decimator is available."""
    return (Rung.FAST_SIMPLIFICATION,) if _FASTSIMP else ()


def _decimate_fastsimp(geometry: Geometry, max_faces: int) -> Geometry:
    """Quadric edge collapse on plain numpy arrays — the fast path."""
    n_in = len(geometry.faces)
    # fast_simplification takes the fraction of faces to REMOVE, not to keep.
    reduction = 1.0 - (float(max_faces) / float(n_in))
    verts, faces = _fastsimp.simplify(
        geometry.verts, geometry.faces.astype(np.uint32), reduction)
    return Geometry(np.asarray(verts, dtype=np.float32),
                    np.asarray(faces, dtype=np.int64))


def decimate(mesh: Mesh, max_faces: int) -> Result:
    """Reduce `mesh` to at most `max_faces` faces.

    Takes a loaded mesh and returns a loaded mesh, without touching the disk.
    The selected rung or failure is reported in the result.

    `max_faces <= 0` disables decimation, matching the old `MAX_FACES = 0`.
    A mesh already within budget is returned untouched rather than round
    tripped through a decimator that would have nothing to do.

    Raises `ValueError` if the mesh is not loaded — that is a programming error
    at the call site, not a property of the data.
    """
    require_geometry(mesh)

    faces_in = len(mesh.geometry.faces)
    if max_faces <= 0 or faces_in <= max_faces:
        return Result(mesh, Rung.NOT_NEEDED, faces_in, faces_in)

    if not _FASTSIMP:
        return Result(mesh, Rung.FAILED, faces_in, faces_in,
                      ((Rung.FAST_SIMPLIFICATION,
                        'fast_simplification is not installed'),))

    try:
        geometry = _decimate_fastsimp(mesh.geometry, max_faces)
    except Exception as exc:
        return Result(mesh, Rung.FAILED, faces_in, faces_in,
                      ((Rung.FAST_SIMPLIFICATION,
                        f"{type(exc).__name__}: {exc}"),))
    return Result(mesh.with_geometry(geometry), Rung.FAST_SIMPLIFICATION,
                  faces_in, len(geometry.faces))


def _detail_for(result: Result) -> str:
    """The uniform step's own detail string for one `decimate()` result."""
    if result.rung is Rung.FAILED:
        attempts = '; '.join(f'{rung.value}: {why}' for rung, why in result.attempts)
        return f'decimate to {result.faces_in}f failed ({attempts})'
    return f'{result.rung.value}, {result.faces_out} faces out'


def make_step(sink: list[Result] | None = None
             ) -> Callable[[Mesh, object], tuple[bool, Mesh, str]]:
    """Build the uniform step `(mesh, config) -> (ok, mesh, detail)` for
    decimation, reading its target from `config.faceCount`.

    Both places decimation runs in the pipeline — the CLI's initial
    whole-mesh pass in `processor.process`, and each part's post-wrap pass
    in `repairer.repair` — call this same function, so there is one
    implementation of "decimation as a step", not two (docs/refactor/TODO.md's
    "use one decimator step implementation in both positions").

    `sink`, when given, receives the rich `decimator.Result` for this call
    (exactly one `append` per call) — for a caller that needs more than the
    step's own `(ok, mesh, detail)` triple, such as `processor.process`
    building its `Outcome.decimation` field. `sink=None` (the default)
    retains nothing anywhere: no module state, no per-call list a caller
    forgot to drain — the closure created by one call to `make_step()` is
    the only place any state could live, and by default there is none. Two
    independent calls to `make_step()` never share anything, whether or not
    either is given a sink.
    """
    def step(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
        face_count = getattr(config, 'faceCount', 0) if config is not None else 0
        result = decimate(mesh, face_count)
        if sink is not None:
            sink.append(result)
        if result.rung is Rung.FAILED:
            return False, mesh, _detail_for(result)
        return True, result.mesh, _detail_for(result)
    return step
