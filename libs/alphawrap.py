"""Reconstruct a mesh as a watertight, manifold solid via CGAL Alpha Wrapping.

`pip install cgal` (SWIG bindings over the CGAL C++ library; observed as a
prebuilt wheel on this environment, but that is platform- and Python-version
dependent, not a portable guarantee). Not currently installed by `install.sh`
and not wired into `repairer` — this module is experimental and unadopted.

Alpha Wrapping is fundamentally different from every other tool module here:
it *reconstructs* the surface at a resolution controlled by `alpha` rather
than editing the existing triangulation, so the output does not share
vertices, indices, or exact geometry with the input. What it guarantees
unconditionally is topology — watertight, 2-manifold, free of
self-intersections, output triangle count and fidelity are not guaranteed at
all. There is no known formula deriving `alpha`/`offset` from a mesh's own
properties (checked against CGAL's own documentation): two real models
measured outside this module needed ratios of the bounding-box diagonal that
disagreed by more than 2x from each other, so callers must supply both
values themselves rather than rely on a default tuned for one model.
"""

from __future__ import annotations

import math

import numpy as np

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
    surface is allowed from the input. Neither has a known formula; both must
    be chosen per mesh (see the module docstring).

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
