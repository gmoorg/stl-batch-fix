"""Run PyMeshFix on a loaded mesh and capture its output.

The tool works on arrays. `ok` means it returned geometry, not that the mesh is
clean; the caller must rescan it. Input should be split into parts first:
PyMeshFix can discard smaller shells even without an explicit
`remove_smallest_components` call. Capture includes C++ output on both fds.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass

import numpy as np

from . import pipeconfig
from .mesh_io import Geometry, Mesh, require_geometry

try:
    import pymeshfix as _pymeshfix
    _AVAILABLE = True
except ImportError:                                   # pragma: no cover
    _AVAILABLE = False


@dataclass(frozen=True)
class Result:
    """What one repair attempt produced.

    mesh            the repaired mesh, or the input unchanged when it failed
    ok              True when PyMeshFix ran to completion and returned geometry
    problem         why, when `ok` is False; None otherwise
    stdout_capture  progress, captured from fd 1
    stderr_capture  diagnostics, captured from fd 2 — the warnings about cuts
                    and removed triangles arrive here, not on stdout
    second_elapsed  wall time spent, filled in either way

    `ok` says the tool ran, **not** that the mesh is clean — PyMeshFix reports
    success on meshes that still have defects, which is why nothing is written
    out on a library's word alone.  `scanner` decides.
    """

    mesh: Mesh
    ok: bool
    problem: str | None
    stdout_capture: str
    stderr_capture: str
    second_elapsed: float


class _Capture:
    """Redirect file descriptors 1 and 2 to temp files for the duration.

    Narrow by design: those descriptors belong to the whole process while this
    is open, so anything else writing to them on any thread lands in the
    capture too.  Wrap the library calls and nothing else.

    `__exit__` restores both descriptors **first**, before reading, and returns
    False so an exception passes through.  Getting that order wrong would leave
    the process with stdout pointing at a closed temp file after any failure,
    silently swallowing every log line thereafter.
    """

    def __enter__(self) -> _Capture:
        self.out = self.err = ''
        self._files = {}
        self._saved = {}
        for fd in (1, 2):
            self._files[fd] = tempfile.TemporaryFile(mode='w+b')
            self._saved[fd] = os.dup(fd)
            os.dup2(self._files[fd].fileno(), fd)
        return self

    def __exit__(self, *exc_info) -> bool:
        for fd in (1, 2):
            os.dup2(self._saved[fd], fd)
            os.close(self._saved[fd])
        try:
            texts = {}
            for fd in (1, 2):
                self._files[fd].seek(0)
                texts[fd] = self._files[fd].read().decode('utf-8', 'replace')
            self.out, self.err = texts[1], texts[2]
        finally:
            for handle in self._files.values():
                handle.close()
        return False


def is_available() -> bool:
    """Whether PyMeshFix can be used on this machine.

    For the startup check.  Repair has no in-process fallback — Blender is a
    different tool with different failure modes rather than a substitute — so a
    false here means the pipeline can only decimate.
    """
    return _AVAILABLE


#: Arguments to `clean()`.  These match the library default (10, 3), passed
#: explicitly rather than omitted.  `(1, 1)` keeps 13,300 more faces on
#: Amidara base at 99.73% volume but leaves 6,187 self-intersections;
#: everything from inner_loops=3 up converges on the same result.
CLEAN_MAX_ITERS = 10
CLEAN_INNER_LOOPS = 3


def repair(mesh: Mesh, fill_holes: bool = True) -> Result:
    """Run PyMeshFix on a loaded mesh and return its arrays and output.

    `fill_holes=True` fills small boundaries before `clean`; False runs `clean`
    alone. The current repair sequence uses the default. Failure returns the
    input mesh with `ok=False`; successful execution still needs a topology scan.

    `pipeconfig.ENABLE_FILL_BOUNDARIES`/`ENABLE_CLEAN` disable either call
    independently, so what each one costs can be measured rather than argued
    about. With both off this loads and returns the arrays unchanged, which
    is the control.
    """
    require_geometry(mesh)
    if not _AVAILABLE:
        return Result(mesh, False, "pymeshfix is not installed", '', '', 0.0)

    started = time.monotonic()
    capture = _Capture()
    try:
        with capture:
            tin = _pymeshfix.PyTMesh()
            tin.load_array(
                np.ascontiguousarray(mesh.geometry.verts, dtype=np.float64),
                np.ascontiguousarray(mesh.geometry.faces, dtype=np.int32))
            if fill_holes and pipeconfig.ENABLE_FILL_BOUNDARIES:
                tin.fill_small_boundaries(0, True)
            if pipeconfig.ENABLE_CLEAN:
                tin.clean(CLEAN_MAX_ITERS, CLEAN_INNER_LOOPS)
            verts, faces = tin.return_arrays()
    except Exception as exc:
        return Result(mesh, False, f"{type(exc).__name__}: {exc}",
                      capture.out, capture.err, time.monotonic() - started)

    elapsed = time.monotonic() - started
    if len(faces) == 0:
        # A real outcome, not a crash: PyMeshFix collapses some meshes to
        # nothing.  The old code raised "pymeshfix produced empty mesh" here.
        return Result(mesh, False, "pymeshfix produced an empty mesh",
                      capture.out, capture.err, elapsed)

    repaired = mesh.with_geometry(Geometry(
        np.ascontiguousarray(verts, dtype=np.float32),
        np.ascontiguousarray(faces, dtype=np.int64)))
    return Result(repaired, True, None, capture.out, capture.err,
                  elapsed)


def step_meshfix_repair(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """`pipeconfig`'s uniform step contract, wrapping `repair()`.

    `pipeconfig.ENABLE_PART_TOOL` gates PyMeshFix specifically.
    `ENABLE_FILL_BOUNDARIES`/`ENABLE_CLEAN` stay inside `repair()` itself —
    they tune what this one tool call does, not whether a separate tool
    runs, so splitting them into their own steps would mean reloading
    PyMeshFix's state for no behavioural reason. `repair()` raises for a
    caller error (unloaded geometry) rather than returning a `Result` for
    it — that exception is caught here rather than escaping.
    """
    if not pipeconfig.ENABLE_PART_TOOL:
        return True, mesh, 'skipped (ENABLE_PART_TOOL=False)'
    try:
        faces_in = len(mesh.geometry.faces)
        result = repair(mesh)
    except Exception as exc:
        return False, mesh, f"pymeshfix failed: {type(exc).__name__}: {exc}"
    if not result.ok:
        return False, mesh, f"pymeshfix failed: {result.problem}"
    detail = f"pymeshfix {faces_in}f -> {result.mesh.triangles}f"
    if 'WARNING-' in result.stderr_capture:
        # PyMeshFix reports cuts and removed triangles here rather than on
        # stdout, and they are the only warning that a repair was lossy.
        detail += ' (with warnings)'
    return True, result.mesh, detail
