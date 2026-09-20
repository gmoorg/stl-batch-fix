"""Run PyMeshLab filters on in-memory project geometry.

This module owns the PyMeshLab boundary: dependency availability, dtype
conversion, and filter parameter preparation. Pipeline policy, such as which
filters to run, stays with the caller.
"""

from __future__ import annotations

import numpy as np

from .mesh_io import Geometry, Mesh

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
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry — load it before filtering")
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
