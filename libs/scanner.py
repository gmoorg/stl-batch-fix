"""Measure topology from a loaded mesh's vertex and face arrays.

An edge used by one face is open; one used by three or more is non-manifold.
`scan` counts defects, `winding_seams` finds opposing face directions, and
`shells` returns edge-connected components. A missing geometry array raises
instead of producing an unverified zero count. No size ceiling is imposed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .mesh_io import Mesh, require_geometry


@dataclass(frozen=True)
class Scan:
    """What one pass over a mesh's edges found.

    open_edges      edges used by exactly one face — the boundary of a hole
    non_manifold    edges used by three or more faces — the surface branches
    faces           how many faces were scanned
    degenerate      faces with two or more identical corners; they have no
                    area, contribute nothing, and confuse edge counting

    `is_clean` is the pipeline's success condition, and it is deliberately
    *mathematical*: no open edges, no non-manifold edges.  Whether a mesh that
    fails it is nevertheless printable is a separate judgement — see
    `open_loops`, which measures the holes against the layer height.  Keeping
    the two apart is what stopped the pipeline destroying a model to close
    pinholes no printer could express.
    """

    open_edges: int
    non_manifold: int
    faces: int
    degenerate: int = 0

    @property
    def is_clean(self) -> bool:
        """No holes and no branching — the mathematical standard."""
        return self.open_edges == 0 and self.non_manifold == 0

    @property
    def total_defects(self) -> int:
        return self.open_edges + self.non_manifold


@dataclass(frozen=True)
class Loop:
    """One open boundary: a connected chain of open edges.

    vertices    indices of the vertices along it
    diameter    the span of those vertices, in model units

    Diameter is the *span* of the loop, not its edge count or total length.  A
    loop of many short edges can still be a large hole, and one long edge is
    not a hole at all — so counting edges would rank them wrongly.
    """

    vertices: tuple[int, ...]
    diameter: float


def _require_geometry(mesh: Mesh) -> None:
    """Refuse to guess.

    Reporting a clean scan for a mesh that was never loaded would recreate
    precisely the bug `_post_verify`'s three-valued return was added to kill:
    an unscannable mesh returning `(0, 0)`, which every caller tests as
    `nm > 0 or open > 0` and therefore reads as verified-clean.  Files were
    written out as finished having been checked by nothing.  Here that case
    cannot be represented, because it raises instead.
    """
    require_geometry(mesh)


def face_edges(faces: np.ndarray) -> np.ndarray:
    """Every face's three edges as vertex-index pairs, each sorted low-high."""
    edges = np.concatenate([faces[:, [0, 1]],
                            faces[:, [1, 2]],
                            faces[:, [2, 0]]])
    return np.sort(edges, axis=1)


def _edges_of(faces: np.ndarray) -> np.ndarray:
    """Backwards-compatible alias for the project's face-edge primitive."""
    return face_edges(faces)


def _degenerate_mask(faces: np.ndarray) -> np.ndarray:
    """Faces with two or more identical corners — zero area, no normal."""
    return ((faces[:, 0] == faces[:, 1])
            | (faces[:, 1] == faces[:, 2])
            | (faces[:, 0] == faces[:, 2]))


def scan(mesh: Mesh) -> Scan:
    """Count the mesh's open and non-manifold edges.

    The hot path: this runs after decimation, after Blender, and around every
    repair pass, so it is one `np.unique` over the edge array and nothing else.
    """
    _require_geometry(mesh)
    faces = mesh.geometry.faces
    if len(faces) == 0:
        return Scan(0, 0, 0, 0)

    degenerate = int(_degenerate_mask(faces).sum())
    _, counts = np.unique(face_edges(faces), axis=0, return_counts=True)
    return Scan(open_edges=int((counts == 1).sum()),
                non_manifold=int((counts > 2).sum()),
                faces=len(faces),
                degenerate=degenerate)


def open_loops(mesh: Mesh) -> tuple[Loop, ...]:
    """Return open-edge boundary components, largest diameter first.

    This measures hole span without deciding printability or whether to repair.
    """
    _require_geometry(mesh)
    verts, faces = mesh.geometry.verts, mesh.geometry.faces
    if len(faces) == 0:
        return ()

    edges = _edges_of(faces)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique[counts == 1]
    if len(boundary) == 0:
        return ()

    # Union-find over the boundary edges: each connected chain is one loop.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]          # path compression
            x = parent[x]
        return x

    for a, b in boundary:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb

    groups: dict[int, set[int]] = {}
    for a, b in boundary:
        groups.setdefault(find(int(a)), set()).update((int(a), int(b)))

    loops = []
    for members in groups.values():
        indices = np.fromiter(members, dtype=np.int64, count=len(members))
        points = verts[indices]
        span = points.max(axis=0) - points.min(axis=0)
        loops.append(Loop(tuple(sorted(int(i) for i in indices)),
                          float(np.sqrt((span ** 2).sum()))))

    loops.sort(key=lambda loop: -loop.diameter)
    return tuple(loops)


def largest_open_loop(mesh: Mesh) -> float:
    """Diameter of the biggest hole, or 0.0 when the mesh has none."""
    loops = open_loops(mesh)
    return loops[0].diameter if loops else 0.0


def open_loops_are_printable(mesh: Mesh, min_layer: float) -> bool:
    """True when every open boundary is smaller than one layer.

    A hole narrower than the finest layer produces no toolpath: it cannot be
    expressed by the slicer, so closing it changes nothing that reaches the
    plate.  False when there are no open edges at all — a caller with a clean
    mesh is not asking this question — and false when `min_layer` is disabled,
    which restores the strict "open edges must be zero" rule.
    """
    if min_layer <= 0:
        return False
    loops = open_loops(mesh)
    if not loops:
        return False
    return loops[0].diameter < min_layer


def winding_seams(mesh: Mesh) -> tuple[int, int]:
    """Count edges whose two faces traverse them in the same direction.

    Return `(seam_edges, closed_loops)`. A closed loop marks a winding boundary,
    but does not by itself predict whether PyMeshFix will damage the mesh.
    """
    edges = seam_edges(mesh)
    if len(edges) == 0:
        return 0, 0
    return len(edges), _count_closed_loops(edges)


def _count_closed_loops(seam: np.ndarray) -> int:
    """Count seam components whose every vertex has degree two.

    Open chains and branched components are not closed loops.
    """
    adjacency: dict[int, list[int]] = {}
    for a, b in seam:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))

    seen: set[int] = set()
    loops = 0
    for start in list(adjacency):
        if start in seen:
            continue
        stack, group = [start], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            group.append(x)
            stack.extend(adjacency[x])
        if all(len(adjacency[x]) == 2 for x in group):
            loops += 1
    return loops


def seam_edges(mesh: Mesh) -> np.ndarray:
    """The seam edges themselves, as an `(n, 2)` array of vertex indices.

    Separate from `winding_seams` on purpose.  Counting runs on every file;
    the edge list is needed only by the few that are actually split, so the
    common path does not pay to materialise an array it discards.  The old
    `find_winding_seams` always built the list and took `len()` of it.

    Each row is sorted low-index first, which is what `split_at_seams` needs to
    test membership without worrying about direction.
    """
    _require_geometry(mesh)
    faces = mesh.geometry.faces
    if len(faces) == 0:
        return np.zeros((0, 2), dtype=np.int64)

    # Keep each edge's traversal direction: `forward` says whether this face
    # walked the edge low-index to high-index.
    raw = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    forward = raw[:, 0] < raw[:, 1]
    keyed = np.sort(raw, axis=1)

    # Group the rows by edge.  `inverse` maps each row to its edge's id, so
    # every per-edge question below is a bincount over that — no slicing of
    # adjacent rows, which is where a hand-rolled version of this got the
    # index arithmetic wrong in a way that raised on every real mesh.
    unique, inverse, counts = np.unique(keyed, axis=0,
                                        return_inverse=True,
                                        return_counts=True)
    inverse = inverse.ravel()

    # An edge is a seam when exactly two faces use it and both walked it the
    # same way.  A run of three or more is non-manifold — a different defect,
    # counted by `scan` — so only pairs are considered here.
    forward_per_edge = np.bincount(inverse, weights=forward.astype(np.int64),
                                   minlength=len(unique))
    # Of a pair, both forward (2) or neither (0) means they agree: a seam.
    is_seam = (counts == 2) & ((forward_per_edge == 2) | (forward_per_edge == 0))

    found = unique[is_seam]
    # Drop self-edges.  A degenerate face has two identical corners, so it
    # emits an edge (v, v); two such faces sharing it satisfy the pair test
    # above and it arrives here looking like a seam.  It then satisfies the
    # closed-loop test too — `adjacency[v] = [v, v]` has length 2 — so a single
    # point is reported as a closed loop encircling a region.
    #
    # Measured on `Hanna and Chewie/Hair.stl`: 106 "seam edges", all 106 of
    # them self-edges, reported as 106 closed loops.  The mesh has 250
    # degenerate faces and no winding seam at all.  That is the pipeline's
    # strongest "split this mesh" signal, fired by zero-area triangles.
    #
    # The original `find_winding_seams` has the same flaw, so the differential
    # test in `tests/tests/test_scanner.py` cannot catch it — agreement is not correctness
    # when both sides share a mistake.  Degenerate faces are counted by
    # `scan().degenerate`, which is where they belong.
    found = found[found[:, 0] != found[:, 1]]
    return np.ascontiguousarray(found, dtype=np.int64)


def diagonal(mesh: Mesh) -> float:
    """Return the loaded mesh's bounding-box diagonal, or 0 if empty.

    This is the in-memory counterpart of `mesh_io.diagonal(path)`. Relative
    geometric tolerances can use it; absolute thresholds vary with model scale.
    """
    _require_geometry(mesh)
    verts = mesh.geometry.verts
    if len(verts) == 0:
        return 0.0
    span = verts.max(axis=0).astype(np.float64) - \
        verts.min(axis=0).astype(np.float64)
    return float(np.linalg.norm(span))


def volume(mesh: Mesh) -> float:
    """Return signed enclosed volume using the divergence theorem.

    Outward winding is positive. Compare magnitudes before and after repair to
    find gross geometry loss; topology alone can call a partial mesh clean.
    """
    _require_geometry(mesh)
    if len(mesh.geometry.faces) == 0:
        return 0.0
    tri = mesh.geometry.verts[mesh.geometry.faces].astype(np.float64)
    return float(np.einsum('ij,ij->i', tri[:, 0],
                           np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def shells(mesh: Mesh) -> tuple[np.ndarray, ...]:
    """Return edge-connected face components, largest first.

    Faces touching at only one vertex remain separate parts. The scipy sparse
    adjacency implementation is faster than the former Python union-find loop
    on the measured real meshes; see D20 and D22. Return face indices so callers
    that only need a count avoid constructing submeshes.
    """
    _require_geometry(mesh)
    faces = mesh.geometry.faces
    if len(faces) == 0:
        return ()

    # Build face-to-face adjacency through shared edges.  Sorting each edge
    # low-high makes an edge's identity independent of traversal direction, so
    # two faces that walk it in opposite directions still count as adjacent.
    edges = _edges_of(faces)
    owner = np.tile(np.arange(len(faces)), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    edges, owner = edges[order], owner[order]

    # Adjacent rows after the sort are the same edge, so consecutive pairs name
    # two faces sharing it.  A run of three or more (a non-manifold edge) links
    # each face to the next, which still places them all in one component.
    shared = np.all(edges[1:] == edges[:-1], axis=1)
    left, right = owner[:-1][shared], owner[1:][shared]

    graph = coo_matrix((np.ones(len(left), dtype=np.int8), (left, right)),
                       shape=(len(faces), len(faces)))
    _, labels = connected_components(graph, directed=False)

    # Group by sorting the labels and cutting where they change — vectorised,
    # unlike a dict of lists.
    order = np.argsort(labels, kind='stable')
    groups = np.split(order, np.flatnonzero(np.diff(labels[order])) + 1)
    groups.sort(key=len, reverse=True)
    return tuple(groups)


def shell_count(mesh: Mesh, min_faces: int = 0) -> int:
    """How many shells the mesh has, ignoring ones below `min_faces`.

    The floor matters: a collection mesh routinely carries hundreds of specks
    of a few faces each, and treating those as real parts turns one repair into
    hundreds.  Measured, `_MIN_SHELL_FACES = 100` keeps every genuine part with
    7.5x margin — the smallest real shell seen was 750 faces — while rejecting
    the specks; at a floor of 10 one model split into 381 parts.
    """
    return sum(1 for shell in shells(mesh) if len(shell) >= min_faces)
