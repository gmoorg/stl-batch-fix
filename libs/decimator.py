"""Reduce a mesh to a face budget, by whichever library is available.

    is_available()                      -> at startup: can anything decimate?
    decimate(mesh, max_faces, ...)      -> Mesh in, Mesh out

One entry point, three implementations behind it.  All three run the same
Garland-Heckbert quadric edge collapse and differ only in how much machinery
sits around it, which is where the cost is.  Measured, 2.55M triangles -> 900k:

    fast_simplification   ~5.6s    ~915 MB    numpy arrays
    pymeshlab             42.0s    1557 MB    full mesh database
    blender               43s      OOM'd under a worker RLIMIT_AS

Size does not select the decimator.  fast_simplification handles a 2.55M mesh
in less memory than PyMeshLab needs for 1.2M, so there is no band where a mesh
is too big for Python and must go out to Blender; Blender is a last resort for
when the other two *fail*, not for when the mesh is large.

**Mesh in, mesh out, whichever rung ran.**  The first two work on the arrays
directly and never touch the disk.  Blender is a separate process and can only
speak files, so that rung writes a temp, runs, loads the result back, and
deletes both — inside this module, where the caller cannot see it.  That is the
one asymmetry, and it is inherent: our numpy arrays do not exist in Blender's
address space.

**Decimation may leave non-manifold edges.**  That is expected and is not this
module's problem to solve — the repair steps downstream exist for it.  What is
returned is a decimated mesh, not a clean one.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum

import numpy as np

from . import blender, mesh_io
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
    BLENDER = 'blender'
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


def is_available(blender_executable: str = 'blender') -> bool:
    """Whether anything on this machine can decimate.

    For the startup check.  Decimation is a deliverable rather than an
    optimisation — a run that cannot decimate produces meshes the printer will
    re-decimate on its own, reintroducing the defects this tool exists to
    remove — so a false here is a reason not to start, not a reason to skip.
    """
    return _FASTSIMP or _PYMESHLAB or blender.is_available(blender_executable)


def available_rungs(blender_executable: str = 'blender') -> tuple[Rung, ...]:
    """Which rungs this machine can actually run, in ladder order.

    Separate from `is_available` so a startup report can say *what* is missing;
    a machine with only the Blender fallback still passes the check but will be
    eight times slower on every large mesh.
    """
    rungs = []
    if _FASTSIMP:
        rungs.append(Rung.FAST_SIMPLIFICATION)
    if _PYMESHLAB:
        rungs.append(Rung.PYMESHLAB)
    if blender.is_available(blender_executable):
        rungs.append(Rung.BLENDER)
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


def _decimate_blender(mesh: Mesh, max_faces: int, timeout: float,
                      executable: str, keep_temp_on_failure: bool) -> Geometry:
    """Last resort: hand the mesh to Blender as a file and read the result back.

    The only rung that touches the disk, because Blender is a separate process
    with its own interpreter and its own mesh database — there is no channel
    for numpy arrays, and a binary STL is what serialising them looks like.

    Both temporaries are deleted before returning.  They are scratch: nothing
    outside this function learns the paths exist, so there is no step later in
    the pipeline that could read one back.  On failure the input temp may be
    kept, since it is the exact reproducer and it is a mesh that already
    defeated two other decimators.
    """
    scratch = tempfile.mkdtemp(prefix='decimate-')
    source = os.path.join(scratch, 'in.stl')
    destination = os.path.join(scratch, 'out.stl')
    kept = False
    try:
        mesh_io.write(mesh, source)
        script = _decimate_script().format(
            src=source, dst=destination, max_faces=max_faces)
        result = blender.Runner(executable).run(script, timeout=timeout)

        if result.is_timed_out:
            kept = keep_temp_on_failure
            raise RuntimeError(f"blender timed out after {timeout:.0f}s")
        if result.exit_code != 0 or 'BLENDER_DECIMATE_OK' not in result.stdout_capture:
            kept = keep_temp_on_failure
            raise RuntimeError(
                f"blender exit {result.exit_code}: "
                f"{result.stderr_capture.strip()[-200:] or 'no stderr'}")
        if not os.path.exists(destination):
            kept = keep_temp_on_failure
            raise RuntimeError("blender reported success but wrote no file")

        # Blender's own face count is printed but not used: the header of the
        # file it wrote is authoritative, and a disagreement between the two
        # must not be able to corrupt anything downstream.
        loaded = mesh_io.load(mesh_io.probe(destination))
        if loaded.geometry is None:
            kept = keep_temp_on_failure
            raise RuntimeError(f"blender output unreadable: {loaded.problem}")
        return loaded.geometry
    finally:
        if not kept:
            _remove_tree(scratch)


def _remove_tree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


_SCRIPT_CACHE: list[str] = []


def _decimate_script() -> str:
    """The Blender-side script, read once and cached."""
    if not _SCRIPT_CACHE:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'blender_fx', 'decimate.blender')) as f:
            _SCRIPT_CACHE.append(f.read())
    return _SCRIPT_CACHE[0]


def decimate(mesh: Mesh,
             max_faces: int,
             timeout: float = 600.0,
             blender_executable: str = 'blender',
             keep_temp_on_failure: bool = False) -> Result:
    """Reduce `mesh` to at most `max_faces` faces.

    Takes a loaded mesh and returns a loaded mesh.  Whether that happened in
    numpy, in PyMeshLab, or by way of a Blender subprocess is not visible here.

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

    try:
        geometry = _decimate_blender(mesh, max_faces, timeout,
                                     blender_executable, keep_temp_on_failure)
        return Result(mesh.with_geometry(geometry), Rung.BLENDER, faces_in,
                      len(geometry.faces), tuple(attempts))
    except Exception as exc:
        attempts.append((Rung.BLENDER, f"{type(exc).__name__}: {exc}"))

    # Every rung failed.  The input is returned unchanged rather than a
    # partially decimated mesh: decimation is a deliverable, so a caller must
    # be able to tell "not decimated" from "decimated badly" and mark the file
    # rather than ship it.
    return Result(mesh, Rung.FAILED, faces_in, faces_in, tuple(attempts))
