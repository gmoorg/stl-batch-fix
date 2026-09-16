"""Cut a mesh into independently-repairable pieces, and put them back.

    by_shells(mesh)   -> tuple[Mesh, ...]    parts that do not touch
    by_seams(mesh)    -> tuple[Mesh, ...]    regions whose winding disagrees
    merge(parts)      -> Mesh                one mesh again

Geometry only.  Nothing here decides *whether* to split, repairs anything, or
touches the filesystem — `scanner` finds what could be cut, this does the
cutting, and the caller sequences them.

**A mesh that does not split comes back as a list of one.**  Both functions
promise that, and it is the point rather than a convenience: it removes the
"did it split?" branch from every caller.  The pipeline reads as one path —

    parts = splitter.by_shells(mesh)
    parts = [p for part in parts for p in splitter.by_seams(part)]
    parts = [repairer.repair(p) for p in parts]
    mesh  = splitter.merge(parts)

— with no conditionals, and nothing downstream ever learns whether it is
looking at a whole model or a fragment.  The old code threaded an `is_part`
flag through nineteen call sites to answer that question; there is nothing left
to ask.

**Shells first, then seams.**  Shell components are maximal, so a shell part
can never need shell-splitting again.  Seam regions are cut on a different
criterion — winding, not connectivity — so a single shell can still hold
several, which is why the second pass runs over the parts of the first.  The
reverse order would have seam detection reasoning across pieces that are not
even touching.

**Why split at all**, measured: PyMeshFix rebuilds *one* manifold surface and
discards the rest, so a multi-shell mesh reaching it unsplit comes back as its
largest shell alone — 562,288 faces in, 394,432 out, a model's head gone.  Cut
first and each piece is preserved: 100.0% and 100.2% of volume on the two
regions of that same mesh.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from . import scanner
from .mesh_io import Geometry, Mesh

#: Shells below this many faces are debris rather than parts.
#:
#: Flat, not a fraction of the largest shell.  The old rule was
#: `max(100, largest // 1000)`, and the ratio is what went wrong: it discards
#: more the bigger the model gets.  On a 2M-face figure it set the floor at
#: 1,315 faces, and 562 after decimation — a magnet peg or a locating pin is
#: smaller than that and is a part, not debris.
#:
#: Measured on the collection, this is a wide gap rather than a fine judgement:
#:
#:     Mandy_Body_Dinamuuu3D   39 real shells, smallest 750 faces
#:     whole-costume01         444 shells, of which 443 are under 100 faces
#:
#: So 100 keeps every real part with 7.5x margin and still rejects the specks.
#: Lower is not free: at a floor of 10, whole-costume01 splits into 381 parts,
#: each one a separate repair and merge.
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
    """Split along closed winding-seam loops, largest first.

    Returns `(mesh,)` when there is nothing to cut on — no seam edges, or no
    closed loop among them.

    **Only closed loops justify a cut.**  A few seam edges with loose ends are
    local noise that stops on its own; a closed loop encircles a region whose
    winding cannot be reconciled with its host — hair over a scalp, cloth over
    a body, a separately sculpted part fused on.  Measured on one model: the
    mesh PyMeshFix answered by deleting the head had 40 seam edges in 7 closed
    loops, while one that repairs cleanly had 5 edges in 0 loops.

    **This is not a reliable predictor of what PyMeshFix will do**, and must
    not be used as one.  A sphere with its cap reversed has 40 seam edges in 1
    closed loop and PyMeshFix re-winds it correctly, returning the same 760
    faces, where splitting first gives 880 faces and 2 new non-manifold edges.
    The mesh that genuinely needed splitting had 40 seam edges too.  Nothing
    measurable beforehand separates the two cases, which is why the live
    pipeline splits on *measured volume loss after the fact* rather than on
    this signal.  See the re-opened split-upfront decision.
    """
    edges, loops = scanner.winding_seams(mesh)
    if loops == 0:
        return (mesh,)

    faces = mesh.geometry.faces
    seam = scanner.seam_edges(mesh)
    blocked = {(int(a), int(b)) for a, b in seam}

    # Face adjacency through shared edges, with the seam edges left out — so
    # the flood fill cannot cross the cut line.  Same construction as
    # `scanner.shells`, minus the blocked edges.
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
            ((int(a), int(b)) in blocked for a, b in pair_edges),
            dtype=bool, count=len(pair_edges))
        left, right = left[~crosses], right[~crosses]

    graph = coo_matrix((np.ones(len(left), dtype=np.int8), (left, right)),
                       shape=(len(faces), len(faces)))
    _, labels = connected_components(graph, directed=False)

    order = np.argsort(labels, kind='stable')
    regions = np.split(order, np.flatnonzero(np.diff(labels[order])) + 1)
    regions.sort(key=len, reverse=True)

    kept = [r for r in regions if len(r) >= min_faces]
    if len(kept) <= 1:
        # The seam did not actually separate anything — it may not reach a
        # boundary, or the far side may be debris.  Nothing to gain by cutting.
        return (mesh,)
    return tuple(_extract(mesh, r, name(mesh, i, len(kept)))
                 for i, r in enumerate(kept))


def merge(parts: tuple[Mesh, ...] | list[Mesh],
          destination: str | None = None) -> Mesh:
    """Reassemble parts into one mesh.

    `destination` is where the whole file belongs.  It must be given whenever
    the parts carry part-destinations, because the merged mesh is the parent
    again and inheriting `parts[0]`'s `<base>.part.0.stl` would write the whole
    model to a part's path — and hang the parent's markers off it.  Omit it
    only when the parts were never renamed.

    `merge((mesh,))` returns that mesh unchanged — no copying, no library call.
    That matters because it is the common path: most files never split, and the
    single-part case must not pay for machinery it does not need.

    Concatenation, not a geometric union: each part's vertices are appended and
    its face indices shifted by the running offset.  Parts that were split
    apart are not re-joined, and coincident vertices at an old cut line are not
    welded — the next `mesh_io.load` does that, and `scanner` is what decides
    whether the result is sound.

    The old code merged through PyMeshLab's
    `generate_by_merging_visible_meshes` on *files*, which meant writing every
    part out and reading them back.  With arrays in hand it is two
    `concatenate` calls.
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
