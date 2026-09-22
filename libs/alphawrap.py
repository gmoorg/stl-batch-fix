"""Reconstruct a mesh as a watertight, manifold solid via CGAL Alpha Wrapping.

The default repair step uses the owner-selected whole-mesh diagonal recipe:
alpha=diag/800, offset=diag/2000. See docs/refactor/discovered-bugs.md,
"Settled diagonal-ratio recipe", for measurements and visual confirmation.
Reconstruction guarantees topology, not fidelity or a triangle budget.
CGAL Alpha Wrapping is a required dependency installed by install.sh.
"""

from __future__ import annotations

import math

import numpy as np

from . import pipeconfig
from .mesh_io import Geometry, Mesh, require_geometry

try:
    from CGAL.CGAL_Alpha_wrap_3 import alpha_wrap_3 as _alpha_wrap_3
    from CGAL.CGAL_Kernel import Point_3 as _Point_3
    from CGAL.CGAL_Polyhedron_3 import Polyhedron_3 as _Polyhedron_3
    _CGAL = True
except ImportError:                                    # pragma: no cover
    _CGAL = False


def is_available() -> bool:
    """Whether the `cgal` package's Alpha Wrapping bindings imported."""
    return _CGAL


def _mesh_to_cgal(mesh: Mesh):
    """`mesh.geometry` -> `(points, polygons)` in the shapes `alpha_wrap_3` takes.

    `alpha_wrap_3`'s SWIG typemap wants a plain list of `Point_3` and a plain
    list of plain `int` lists — not the `*_Vector` wrapper classes the same
    bindings also expose, which look like the natural choice but raise
    `TypeError` from the overload resolver at real mesh sizes.
    """
    verts = mesh.geometry.verts
    faces = mesh.geometry.faces
    points = [_Point_3(float(x), float(y), float(z)) for x, y, z in verts]
    polygons = [[int(i) for i in f] for f in faces]
    return points, polygons


def _polyhedron_to_geometry(poly) -> Geometry:
    """Walk a `Polyhedron_3`'s vertices and facets into a `Geometry`.

    Alpha Wrapping's output is documented, and confirmed here by direct
    inspection, to be a pure triangle mesh, so this does not fan-triangulate
    — a facet of any other degree is a contract break worth raising on, not
    silently patching over.
    """
    verts = []
    index_of = {}
    for i, v in enumerate(poly.vertices()):
        p = v.point()
        verts.append((p.x(), p.y(), p.z()))
        index_of[v] = i

    faces = []
    for f in poly.facets():
        start = h = f.halfedge()
        tri = []
        while True:
            tri.append(index_of[h.vertex()])
            h = h.next()
            if h == start:
                break
        if len(tri) != 3:
            raise ValueError(
                f"alpha_wrap_3 produced a non-triangular facet "
                f"(degree {len(tri)}) — expected a pure triangle mesh")
        faces.append(tri)

    return Geometry(np.array(verts, dtype=np.float32),
                    np.array(faces, dtype=np.int64))


def wrap(mesh: Mesh, alpha: float, offset: float) -> Mesh:
    """Reconstruct `mesh` as a watertight, manifold solid.

    `alpha` is, loosely, the radius of the largest probe that can still fit
    through a gap or feature — smaller keeps more detail, at a steep cost in
    triangle count and runtime. `offset` is the maximum distance the output
    surface is allowed from the input. The low-level caller supplies both; the default step uses the
    measured whole-mesh recipe (see the module docstring).

    Raises `ValueError` if `mesh` has no loaded geometry, if `alpha` or
    `offset` is not a finite positive number, if the `cgal` package is not
    importable, or if CGAL's own call produces an empty result — confirmed by
    direct probing that `alpha_wrap_3` does *not* raise on a non-positive
    parameter, it silently returns an empty mesh, which this function refuses
    to hand back as if it were a successful repair.
    """
    require_geometry(mesh)
    if not (math.isfinite(alpha) and alpha > 0):
        raise ValueError(f"alpha must be a finite positive number, got {alpha!r}")
    if not (math.isfinite(offset) and offset > 0):
        raise ValueError(f"offset must be a finite positive number, got {offset!r}")
    if not _CGAL:
        raise ValueError(
            "the cgal package is not installed — `pip install cgal` "
            "(see the alphawrap module docstring)")

    points, polygons = _mesh_to_cgal(mesh)
    out = _Polyhedron_3()
    _alpha_wrap_3(points, polygons, alpha, offset, out)
    if out.size_of_facets() == 0 or out.size_of_vertices() == 0:
        raise ValueError(
            f"alpha_wrap_3 returned an empty mesh for alpha={alpha}, "
            f"offset={offset} — these parameters produced nothing, not a "
            f"repair")

    geometry = _polyhedron_to_geometry(out)
    return mesh.with_geometry(geometry)


def step_alpha_wrap(mesh: Mesh, *, whole_diagonal: float | None = None
                    ) -> tuple[bool, Mesh, str]:
    """Wrap one part using the pre-split whole mesh's diagonal."""
    if not pipeconfig.ENABLE_ALPHA_WRAP:
        return True, mesh, 'skipped (ENABLE_ALPHA_WRAP=False)'
    try:
        if whole_diagonal is None:
            raise ValueError('whole_diagonal is required for alpha wrapping')
        diagonal = float(whole_diagonal)
        if not math.isfinite(diagonal) or diagonal <= 0:
            raise ValueError('whole_diagonal must be finite and positive')
        alpha, offset = diagonal / 800.0, diagonal / 2000.0
        result = wrap(mesh, alpha, offset)
        return True, result, f'alpha={alpha}, offset={offset}'
    except Exception as exc:
        return False, mesh, f'{type(exc).__name__}: {exc}'
