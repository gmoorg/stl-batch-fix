"""Run PyMeshLab filters on in-memory project geometry."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import scanner
from .mesh_io import Geometry, Mesh, require_geometry

try:
    import pymeshlab as _pymeshlab
    _AVAILABLE = True
except ImportError:                                   # pragma: no cover
    _AVAILABLE = False


def is_available() -> bool:
    """Whether PyMeshLab is importable."""
    return _AVAILABLE


def to_mesh(geometry: Geometry):
    """Convert project geometry to PyMeshLab's array contract, without
    index-degenerate faces.

    A face with a repeated corner index (`[a, b, a]`) segfaults quadric edge
    collapse when it arrives through this array path: seven models crashed
    the batch run this way, and a 16-face crop of one
    (`tests/probes/segv_min16.stl`) still does. PyMeshLab's own file importer
    drops such faces, which is why the same files decimate fine when loaded
    from disk. So they are dropped here, the one hand-off every array into
    PyMeshLab passes (`apply_filters`, hence the decimator and every `step_*`
    filter). Lossless: such a face has zero area.

    Vertices are passed unchanged, even ones only a dropped face used, as the
    importer does; unreferenced vertices were measured harmless to decimation.
    Faces with three distinct indices but zero area (coincident vertices) are
    not dropped: whether they crash is untested. Evidence:
    docs/errors/decimation-segfault.md.
    """
    faces = geometry.faces[~scanner.degenerate_mask(geometry.faces)]
    return _pymeshlab.Mesh(
        vertex_matrix=np.ascontiguousarray(geometry.verts, dtype=np.float64),
        face_matrix=np.ascontiguousarray(faces, dtype=np.int32),
    )


def from_mesh(mesh) -> Geometry:
    """Convert a PyMeshLab mesh to the project's canonical array dtypes."""
    return Geometry(
        np.ascontiguousarray(mesh.vertex_matrix(), dtype=np.float32),
        np.ascontiguousarray(mesh.face_matrix(), dtype=np.int64),
    )


@dataclass(frozen=True)
class Percent:
    """A percentage parameter (of the bounding-box diagonal), for filters that
    take one, such as merge_close's `threshold`. Plain Python, so building it
    never needs PyMeshLab and two compare equal by value; `apply_filters`
    turns it into PyMeshLab's own type."""
    value: float


def percent(value: float) -> Percent:
    """A percentage parameter for `apply_filters`; see `Percent`."""
    return Percent(float(value))


def apply_filters(mesh: Mesh,
                  filters: tuple[tuple[str, dict], ...]) -> Mesh:
    """Apply PyMeshLab filters to a loaded mesh without using the filesystem.

    Parameters are passed unchanged: a percentage must be wrapped with
    `percent` (PyMeshLab rejects a plain float where it wants one), and a
    plain float stays a plain float, as quadric decimation's `qualitythr`
    needs. Floats used to be turned into percentages wholesale, which made
    such parameters impossible to pass.

    PyMeshLab keeps a filter's parameters from its previous call in the
    process, even on a new MeshSet: a parameter left out is not reset to its
    default. A caller whose result must not depend on earlier calls passes
    every parameter (see `decimator.QUADRIC_PARAMS`).
    """
    require_geometry(mesh)
    ms = _pymeshlab.MeshSet()
    ms.add_mesh(to_mesh(mesh.geometry))
    for name, params in filters:
        ms.apply_filter(name, **{
            key: (_pymeshlab.PercentageValue(value.value)
                  if isinstance(value, Percent) else value)
            for key, value in params.items()})
    return mesh.with_geometry(from_mesh(ms.current_mesh()))


def _step(mesh: Mesh, filter_name: str, params: dict) -> tuple[bool, Mesh, str]:
    """Shared body for the five uniform steps built from a single PyMeshLab
    filter (the four CLEAN filters and orient): run its filter, catch what
    `apply_filters` raises — `apply_filters` raises rather than returning a
    failure, so it is this function's job to convert that into
    `pipeconfig`'s uniform step contract.
    """
    try:
        result = apply_filters(mesh, ((filter_name, params),))
    except Exception as exc:
        return False, mesh, f"{filter_name} failed: {exc}"
    return True, result, filter_name.replace('meshing_', '')


def step_clean_null_faces(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """Drop zero-area faces. Uniform step: see `_step`."""
    return _step(mesh, 'meshing_remove_null_faces', {})


def step_clean_merge_close(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """Weld vertices within 0.1% of the bbox diagonal. Uniform step: see
    `_step`. **Measured harmful** on Amidara base — see
    docs/refactor/modules.md's meshlab entry."""
    return _step(mesh, 'meshing_merge_close_vertices', {'threshold': percent(0.1)})


def step_clean_duplicate_faces(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """Remove faces duplicated after the merge. Uniform step: see `_step`."""
    return _step(mesh, 'meshing_remove_duplicate_faces', {})


def step_clean_unreferenced(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """Drop vertices no face references. Uniform step: see `_step`."""
    return _step(mesh, 'meshing_remove_unreferenced_vertices', {})


def step_orient(mesh: Mesh, config: object | None = None) -> tuple[bool, Mesh, str]:
    """Orient one part outward, by geometry. Uniform step: see `_step`.
    Runs unconditionally after splitting, without a signed-volume guard —
    a signed-volume guard misses local inversions. **Measured harmful** on
    Amidara base — see docs/refactor/modules.md's meshlab entry.
    """
    ok, result, detail = _step(mesh, 'meshing_re_orient_faces_by_geometry', {})
    return ok, result, ('oriented' if ok else detail)
