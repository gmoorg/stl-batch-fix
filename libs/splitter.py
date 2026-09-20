"""Split loaded meshes by shells or winding seams, then merge parts.

`by_shells` uses edge connectivity and drops components below `min_faces` when
larger parts exist. `by_seams` is available but the current repair sequence
does not call it. Both return a one-item tuple when nothing splits. Parts get
their own destinations; neither function writes files.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from . import scanner
from .mesh_io import Geometry, Mesh

#: Fixed debris floor; scaling it with the largest shell discarded real small
#: parts. Corpus measurements are in docs/refactor/implementation-evidence.md.
MIN_SHELL_FACES = 100


def _extract(mesh: Mesh, face_indices: np.ndarray, destination: str) -> Mesh:
    """Build a standalone mesh from a subset of `mesh`'s faces.

    The vertex block is narrowed to what those faces actually use and the face
    indices are renumbered into it, so the part carries no trace of the mesh it
    came from — which is what lets a repair tool treat it as a whole model.

    It gets its **own destination**, because a split is one input becoming
    several outputs and every marker the indicator scan looks for hangs off
    that path.  Parts sharing a destination would share a `.failed.stl`.
    """
    sub = mesh.geometry.faces[face_indices]
    used = np.unique(sub)
    remap = np.zeros(len(mesh.geometry.verts), dtype=np.int64)
    remap[used] = np.arange(len(used))
    part = mesh.with_destination(destination)
    return part.with_geometry(Geometry(mesh.geometry.verts[used], remap[sub]))


def _default_name(mesh: Mesh, index: int, total: int) -> str:
    """Where part `index` of `mesh` goes, when the caller names no policy.

    `<base>.part.<N>.stl` beside the parent's own destination.  A caller that
    wants them elsewhere — the old code used a `~parts/<mesh>.<MAX_FACES>/`
    folder — passes its own function instead; output layout is not this
    module's business.
    """
    base, ext = os.path.splitext(mesh.destination)
    return f"{base}.part.{index}{ext or '.stl'}"


def _face_regions_after_cut(faces: np.ndarray,
                            blocked_edges: set[tuple[int, int]]) -> list[np.ndarray]:
    """Return face-connected regions without crossing selected seam edges."""
    all_edges = np.sort(np.concatenate([faces[:, [0, 1]],
                                        faces[:, [1, 2]],
                                        faces[:, [2, 0]]]), axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    order = np.lexsort((all_edges[:, 1], all_edges[:, 0]))
    all_edges, owner = all_edges[order], owner[order]

    shared = np.all(all_edges[1:] == all_edges[:-1], axis=1)
    left, right = owner[:-1][shared], owner[1:][shared]
    pair_edges = all_edges[:-1][shared]
    if len(pair_edges):
        crosses = np.fromiter(
            ((int(a), int(b)) in blocked_edges for a, b in pair_edges),
            dtype=bool, count=len(pair_edges))
        left, right = left[~crosses], right[~crosses]

    graph = coo_matrix((np.ones(len(left), dtype=np.int8), (left, right)),
                       shape=(len(faces), len(faces)))
    _, labels = connected_components(graph, directed=False)
    order = np.argsort(labels, kind='stable')
    regions = np.split(order, np.flatnonzero(np.diff(labels[order])) + 1)
    regions.sort(key=len, reverse=True)
    return regions


def by_shells(mesh: Mesh,
              min_faces: int = MIN_SHELL_FACES,
              name: Callable[[Mesh, int, int], str] = _default_name,
              ) -> tuple[Mesh, ...]:
    """Split into connected components, largest first.

    Returns `(mesh,)` unchanged when there is one component — the common case,
    763 of 768 files in the collection run — so the caller needs no branch and
    pays nothing for the split machinery.

    Components below `min_faces` are **dropped**, not returned.  Pass
    `min_faces=0` to keep everything.

    Two faces belong to the same component when they share an *edge*; a single
    shared vertex is not a connection.  That is `scanner.shells()`'s rule and
    the reason is PyMeshFix — see D22.
    """
    shells = scanner.shells(mesh)
    if len(shells) <= 1:
        return (mesh,)

    kept = [s for s in shells if len(s) >= min_faces]
    if not kept:
        # Every component is debris by the floor.  Returning nothing would lose
        # the mesh entirely, so hand back what there was and let the caller
        # decide; a mesh made only of specks is a judgement, not a split.
        return (mesh,)
    if len(kept) == 1:
        # One real component and some debris: the mesh keeps its own identity,
        # because nothing was divided — the specks were simply dropped.
        return (_extract(mesh, kept[0], mesh.destination),)
    return tuple(_extract(mesh, s, name(mesh, i, len(kept)))
                 for i, s in enumerate(kept))


def by_seams(mesh: Mesh,
             min_faces: int = MIN_SHELL_FACES,
             name: Callable[[Mesh, int, int], str] = _default_name,
             ) -> tuple[Mesh, ...]:
    """Split on closed winding-seam loops when explicitly called.

    Open seam fragments do not justify a cut. A closed loop is only a candidate:
    it does not predict whether PyMeshFix will damage the mesh, so the current
    repair sequence does not invoke this function automatically.
    """
    edges, loops = scanner.winding_seams(mesh)
    if loops == 0:
        return (mesh,)

    seam = scanner.seam_edges(mesh)
    blocked = {(int(a), int(b)) for a, b in seam}
    regions = _face_regions_after_cut(mesh.geometry.faces, blocked)

    kept = [r for r in regions if len(r) >= min_faces]
    if len(kept) <= 1:
        # The seam did not actually separate anything — it may not reach a
        # boundary, or the far side may be debris.  Nothing to gain by cutting.
        return (mesh,)
    return tuple(_extract(mesh, r, name(mesh, i, len(kept)))
                 for i, r in enumerate(kept))


def merge(parts: tuple[Mesh, ...] | list[Mesh],
          destination: str | None = None) -> Mesh:
    """Concatenate parts' arrays into one mesh; do not geometrically union.

    Supply `destination` when parts have their own paths, or markers would attach
    to a part path. A single part is returned without geometry copying. Coincident
    vertices at former cuts are not welded here; the caller must rescan the result.
    """
    parts = tuple(parts)
    if not parts:
        raise ValueError("merge() needs at least one part")
    if len(parts) == 1:
        one = parts[0]
        return one if destination is None else one.with_destination(destination)

    for part in parts:
        if part.geometry is None:
            raise ValueError(f"{part.path} has no geometry to merge")

    verts, faces, offset = [], [], 0
    for part in parts:
        verts.append(part.geometry.verts)
        faces.append(part.geometry.faces + offset)
        offset += len(part.geometry.verts)

    # The merged mesh is the parent again, so it takes the parent's identity
    # rather than part 0's.
    whole = (parts[0] if destination is None
             else parts[0].with_destination(destination))
    return whole.with_geometry(Geometry(np.concatenate(verts),
                                        np.concatenate(faces)))
