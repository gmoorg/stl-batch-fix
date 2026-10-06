"""Run PyMeshFix on a loaded mesh.

Its output is not captured: PyMeshFix writes progress to stdout and its
warnings about cuts and removed triangles to stderr, and inside a repair
child both are the model's log, so they land there as they are printed.
Nothing here decides success from that text."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .mesh_io import Geometry, Mesh, require_geometry

import pymeshfix as _pymeshfix


@dataclass(frozen=True)
class Result:
    """What one repair attempt produced.

    mesh            the repaired mesh, or the input unchanged when it failed
    ok              True when PyMeshFix ran to completion and returned geometry
    problem         why, when `ok` is False; None otherwise
    second_elapsed  wall time spent, filled in either way

    `ok` says the tool ran, **not** that the mesh is clean — PyMeshFix reports
    success on meshes that still have defects, which is why nothing is written
    out on a library's word alone.  `scanner` decides.
    """

    mesh: Mesh
    ok: bool
    problem: str | None
    second_elapsed: float


#: Arguments to `clean()`.  These match the library default (10, 3), passed
#: explicitly rather than omitted.  `(1, 1)` keeps 13,300 more faces on
#: Amidara base at 99.73% volume but leaves 6,187 self-intersections;
#: everything from inner_loops=3 up converges on the same result.
CLEAN_MAX_ITERS = 10
CLEAN_INNER_LOOPS = 3


def repair(mesh: Mesh, fill_holes: bool = True) -> Result:
    """Run PyMeshFix on a loaded mesh and return the repaired mesh.

    `fill_holes=True` fills small boundaries before `clean`; False runs `clean`
    alone. The current repair sequence uses the default. Failure returns the
    input mesh with `ok=False`; successful execution still needs a topology scan.
    """
    require_geometry(mesh)

    started = time.monotonic()
    try:
        tin = _pymeshfix.PyTMesh()
        tin.load_array(
            np.ascontiguousarray(mesh.geometry.verts, dtype=np.float64),
            np.ascontiguousarray(mesh.geometry.faces, dtype=np.int32))
        if fill_holes:
            tin.fill_small_boundaries(0, True)
        tin.clean(CLEAN_MAX_ITERS, CLEAN_INNER_LOOPS)
        verts, faces = tin.return_arrays()
    except Exception as exc:
        return Result(mesh, False, f"{type(exc).__name__}: {exc}",
                      time.monotonic() - started)

    elapsed = time.monotonic() - started
    if len(faces) == 0:
        # A real outcome, not a crash: PyMeshFix collapses some meshes to
        # nothing.  The old code raised "pymeshfix produced empty mesh" here.
        return Result(mesh, False, "pymeshfix produced an empty mesh", elapsed)

    repaired = mesh.with_geometry(Geometry(
        np.ascontiguousarray(verts, dtype=np.float64),
        np.ascontiguousarray(faces, dtype=np.int64)))
    return Result(repaired, True, None, elapsed)


def step_meshfix_repair(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """The uniform step contract, wrapping `repair()`.

    Ignores `config` — MeshFix needs no per-call context, unlike decimation
    or alpha wrap.

    `repair()` raises for a caller error (unloaded geometry) rather than
    returning a `Result` for it — that exception is caught here rather than
    escaping.
    """
    try:
        faces_in = len(mesh.geometry.faces)
        result = repair(mesh)
    except Exception as exc:
        return False, mesh, f"pymeshfix failed: {type(exc).__name__}: {exc}"
    if not result.ok:
        return False, mesh, f"pymeshfix failed: {result.problem}"
    # PyMeshFix's own warnings (cuts, removed triangles) are not summarized
    # here: they go straight to the model's log, beside this step's output.
    return True, result.mesh, f"pymeshfix {faces_in}f -> {result.mesh.triangles}f"
