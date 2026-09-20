"""Reduce a loaded mesh to a face budget.

Try `fast_simplification`, then PyMeshLab if needed. Both work on arrays and
may introduce defects; downstream repair owns that check. Neither this module
nor its result writes a file.
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

try:
    import pymeshlab as _pymeshlab
    _PYMESHLAB = True
except ImportError:                                   # pragma: no cover
    _PYMESHLAB = False


class Rung(Enum):
    """Which implementation produced a result.

    Recorded rather than inferred: "which decimator ran" shifts the output face
    count, and a later run that picks a different rung produces a different
    mesh from the same input.
    """

    FAST_SIMPLIFICATION = 'fastsimp'
    PYMESHLAB = 'pymeshlab'
    NOT_NEEDED = 'not_needed'        # already within budget; nothing ran
    FAILED = 'failed'                # every rung failed


@dataclass(frozen=True)
class Result:
    """What decimation did, beside the mesh itself.

    mesh        the decimated mesh — or the input unchanged, when nothing ran
    rung        which implementation produced it
    faces_in    face count before
    faces_out   face count after
    attempts    (rung, problem) for every rung that was tried and failed,
                in order.  Empty when the first one worked.

    `attempts` exists because a silent fallback is indistinguishable from a
    first-choice success at the call site, and the fallbacks are slow enough
    that their frequency is worth knowing.
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
    """Whether anything on this machine can decimate.

    For the startup check.  Decimation is a deliverable rather than an
    optimisation — a run that cannot decimate produces meshes the printer will
    re-decimate on its own, reintroducing the defects this tool exists to
    remove — so a false here is a reason not to start, not a reason to skip.
    """
    return _FASTSIMP or _PYMESHLAB


def available_rungs() -> tuple[Rung, ...]:
    """Which rungs this machine can actually run, in ladder order.

    Separate from `is_available` so a startup report can say *what* is missing;
    a machine with only PyMeshLab still passes the check but will be roughly
    eight times slower on every large mesh.
    """
    rungs = []
    if _FASTSIMP:
        rungs.append(Rung.FAST_SIMPLIFICATION)
    if _PYMESHLAB:
        rungs.append(Rung.PYMESHLAB)
    return tuple(rungs)


def _decimate_fastsimp(geometry: Geometry, max_faces: int) -> Geometry:
    """Quadric edge collapse on plain numpy arrays — the fast path."""
    n_in = len(geometry.faces)
    # fast_simplification takes the fraction of faces to REMOVE, not to keep.
    reduction = 1.0 - (float(max_faces) / float(n_in))
    verts, faces = _fastsimp.simplify(
        geometry.verts, geometry.faces.astype(np.uint32), reduction)
    return Geometry(np.asarray(verts, dtype=np.float32),
                    np.asarray(faces, dtype=np.int64))


def _decimate_pymeshlab(geometry: Geometry, max_faces: int) -> Geometry:
    """Same algorithm through PyMeshLab, with its topology guards.

    Arrays in and arrays out — `load_new_mesh`/`save_current_mesh` would round
    trip through the disk for no reason, since PyMeshLab runs in this process.
    """
    ms = _pymeshlab.MeshSet()
    ms.add_mesh(_pymeshlab.Mesh(
        vertex_matrix=geometry.verts.astype(np.float64),
        face_matrix=geometry.faces.astype(np.int32)))
    ms.meshing_decimation_quadric_edge_collapse(
        targetfacenum=max_faces,
        preservetopology=True,
        preserveboundary=True,
        preservenormal=True,
        autoclean=True,
    )
    current = ms.current_mesh()
    return Geometry(
        np.ascontiguousarray(current.vertex_matrix(), dtype=np.float32),
        np.ascontiguousarray(current.face_matrix(), dtype=np.int64))


def decimate(mesh: Mesh, max_faces: int) -> Result:
    """Reduce `mesh` to at most `max_faces` faces.

    Takes a loaded mesh and returns a loaded mesh, without touching the disk.
    Which of the two rungs produced it is reported but not otherwise visible.

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

    attempts: list[tuple[Rung, str]] = []

    if _FASTSIMP:
        try:
            geometry = _decimate_fastsimp(mesh.geometry, max_faces)
            return Result(mesh.with_geometry(geometry),
                          Rung.FAST_SIMPLIFICATION, faces_in,
                          len(geometry.faces), tuple(attempts))
        except Exception as exc:
            attempts.append((Rung.FAST_SIMPLIFICATION,
                             f"{type(exc).__name__}: {exc}"))

    if _PYMESHLAB:
        try:
            geometry = _decimate_pymeshlab(mesh.geometry, max_faces)
            return Result(mesh.with_geometry(geometry), Rung.PYMESHLAB,
                          faces_in, len(geometry.faces), tuple(attempts))
        except Exception as exc:
            attempts.append((Rung.PYMESHLAB, f"{type(exc).__name__}: {exc}"))

    # Both rungs failed.  The input is returned unchanged rather than a
    # partially decimated mesh: decimation is a deliverable, so a caller must
    # be able to tell "not decimated" from "decimated badly" and write the
    # `.undecimated.stl` marker rather than ship the file as finished.
    return Result(mesh, Rung.FAILED, faces_in, faces_in, tuple(attempts))
