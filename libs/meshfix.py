"""Repair a mesh with PyMeshFix, on the arrays, capturing what it says.

    is_available()          -> at startup: can this machine repair at all?
    repair(mesh)            -> Result(mesh, ok, problem, stdout_capture,
                                      stderr_capture, second_elapsed)

Peer of `libs/blender.py`: one tool, no policy.  It does not decide whether a
mesh needs repairing, whether the result is good enough, or what to do next —
`repairer` owns the ladder and `scanner` owns the verdict.

**Named `meshfix` rather than `pymeshfix`** so the module does not shadow the
library it imports.

**Arrays in, arrays out.**  `PyTMesh.load_array` / `return_arrays` keep this
in-process, so there is no file boundary as there is with Blender.  The library
wants float64 vertices and int32 faces and returns the same; `Geometry` is
float32/int64, so both directions convert.

**`remove_smallest_components` is deliberately not called.**  It is in
`MeshFix.repair()`'s default sequence, and it is the documented head-deletion
behaviour rather than an edge case — measured here on two disjoint tetrahedra,
8 faces in:

    remove_smallest_components() alone      4 faces out
    the full default repair()               4 faces out
    fill + clean, without remove_smallest   8 faces out

Under this pipeline `splitter.by_shells()` runs first, so every mesh arriving
here is a single shell and the call would be a no-op at best.  It is omitted
rather than exposed as an off-by-default flag: a parameter that must always be
False is one somebody eventually sets to True.

**Omitting it does not make shell deletion impossible**, and an earlier version
of this docstring implied it did.  On a real 4,526-shell mesh
(`platform_supported.stl`, a resin model whose supports are separate shells)
`clean()` alone returned **1 shell and 266,918 of 525,254 faces** — it kept the
body and discarded 4,525 support pillars.  The two-tetrahedra test above is too
small to show that.  So: splitting first is what prevents the loss, not this
omission.  Hand this function a multi-shell mesh and it may still eat it.

**The output is captured at file-descriptor level, on both descriptors**,
because PyMeshFix writes from C++ straight to the fds rather than through
Python.  `contextlib.redirect_stdout` swaps a Python object and catches none of
it (measured: empty string).

**Both**, not just stdout, and that was measured rather than assumed: progress
(`Loading ..0%`) goes to fd 1, but every diagnostic goes to fd 2 —
`INFO- No intersections detected.`, and on a real Mandy shell
`WARNING- Some cuts were necessary to cope with non manifold configuration` and
`WARNING- 29 double-triangles have been removed`.  Capturing only stdout left
the most useful output escaping to the terminal, which under the TUI is the
alternate screen.

Captured rather than discarded, for the same reason `blender.Result` carries
`stdout_capture` and `stderr_capture`: PyMeshFix can run for 3,000 seconds
holding the GIL, and this is the only sign of life.

Verified on a real Mandy shell: 7,193 chars on stdout, 123 on stderr, and the
`WARNING-` lines land in the stderr capture.

**One known leak, not worth more effort.**  A single line —
`INFO- No intersections detected.` — escapes to the terminal on small meshes,
after the capture has been restored and after the interpreter's last statement.
Three explanations were tested and all were wrong: it is not flushed at object
destruction (forcing `del` + `gc.collect()` inside the window captures
nothing), it is not buffered stderr (capture is 0 chars), and it is not a
cached `FILE*` from import time (redirecting fd 2 *before* importing pymeshfix
also captures nothing).  It arrives at interpreter teardown by some route none
of those describe.  It carries no information, it is constant text, and it
appears once as a process ends — so it is recorded here rather than chased
further.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass

import numpy as np

from .mesh_io import Geometry, Mesh

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


def repair(mesh: Mesh, fill_holes: bool = True) -> Result:
    """Run PyMeshFix over `mesh` and return what it produced.

    Takes a loaded mesh and returns a loaded mesh; nothing touches the disk.

    The sequence is `MeshFix.repair()`'s own, minus
    `remove_smallest_components` (see the module docstring):

        fill_small_boundaries(0, True)    # fill_holes=True
        clean()                           # degeneracies, self-intersections

    `fill_holes=False` runs `clean()` alone, for a caller that wants
    non-manifold edges resolved without boundaries being closed — the
    print-scale gate makes that distinction, since a hole smaller than one
    layer produces no toolpath and closing it has been measured to do more
    harm than leaving it.

    A failure returns the **input mesh unchanged** with `ok=False`, never a
    partial result: a caller must be able to tell "not repaired" from "repaired
    badly" and mark the file rather than ship it.
    """
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry — load it before repairing")
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
            if fill_holes:
                tin.fill_small_boundaries(0, True)
            tin.clean()
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
