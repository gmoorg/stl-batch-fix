"""Run PyMeshLab filters on in-memory project geometry."""

from __future__ import annotations

import numpy as np

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
    """Convert project geometry to PyMeshLab's array contract."""
    return _pymeshlab.Mesh(
        vertex_matrix=np.ascontiguousarray(geometry.verts, dtype=np.float64),
        face_matrix=np.ascontiguousarray(geometry.faces, dtype=np.int32),
    )


def from_mesh(mesh) -> Geometry:
    """Convert a PyMeshLab mesh to the project's canonical array dtypes."""
    return Geometry(
        np.ascontiguousarray(mesh.vertex_matrix(), dtype=np.float32),
        np.ascontiguousarray(mesh.face_matrix(), dtype=np.int64),
    )


def apply_filters(mesh: Mesh,
                  filters: tuple[tuple[str, dict], ...]) -> Mesh:
    """Apply PyMeshLab filters to a loaded mesh without using the filesystem.

    Float parameters are interpreted as percentages because that is the
    contract used by the filter policy in the repair pipeline.
    """
    require_geometry(mesh)
    ms = _pymeshlab.MeshSet()
    ms.add_mesh(to_mesh(mesh.geometry))
    for name, params in filters:
        prepared = {
            key: (_pymeshlab.PercentageValue(value)
                  if isinstance(value, float) else value)
            for key, value in params.items()
        }
        ms.apply_filter(name, **prepared)
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
    return _step(mesh, 'meshing_merge_close_vertices', {'threshold': 0.1})


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
