"""Reduce a loaded mesh to a face budget with fast_simplification.

Decimation is a required deliverable. If the required decimator is unavailable
or fails, this module returns the input unchanged so the caller can write an
undecimated marker rather than silently shipping an over-budget mesh.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from . import mesh_io
from .mesh_io import Geometry, Mesh

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
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry — load it before decimating")

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
