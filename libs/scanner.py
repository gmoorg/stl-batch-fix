"""Count a mesh's topological defects, from the geometry in memory.

    scan(mesh)                  -> Scan(open_edges, non_manifold, ...)
    open_loops(mesh)            -> the open boundaries, measured
    winding_seams(mesh)         -> where the surface reverses
    shells(mesh)                -> connected components, largest first

Every question here is answered from the same edge map, which is why they are
one module rather than four: building it is the work, and the old code built it
up to four times for one mesh.

**Why this runs so often.** A defect count is the pipeline's decision variable,
not a report — it decides whether a repair is needed, whether one worked, and
whether a file may be written out as finished.  The old pipeline scanned after
decimation, after Blender, and twice around PyMeshFix: eight call sites, each
re-reading the file from disk and rebuilding the map.

**Nothing is trusted to self-report.**  PyMeshFix and Blender both claim
success on meshes that still have defects, so a claim is only ever a hint; the
scan is what decides.  That distrust is load-bearing and predates this module.

**The counting rule**, in one place so it cannot drift between callers:

    an edge used by exactly 1 face   is an OPEN edge (a hole's boundary)
    an edge used by exactly 2 faces  is sound
    an edge used by 3 or more faces  is NON-MANIFOLD (the surface branches)

**On the old byte-level implementation.**  `_build_edge_counts` worked on the
raw 50-byte STL records and packed each vertex into a 192-bit int in a
per-triangle Python loop.  That was not gratuitous: an unwelded STL has no
vertex sharing, so identity had to be recovered from coordinate bytes, and one
int per edge beat seven tuple/float objects five-fold on peak RSS.  With welded
faces the whole problem is gone — the indices *are* the identity — so this is
vectorised numpy, and the 192-bit keys, the `-0.0` folding and the
`_LARGE_MESH_TRI_LIMIT` ceiling all go with it.

**That ceiling mattered.**  `scan_mesh_errors` returned `(-1, -1)` above two
million triangles, and `_post_verify` translated that into "UNVERIFIED" — on
exactly the meshes that are both hardest to scan and most likely to be broken.
There is no ceiling here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .mesh_io import Mesh


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
    if mesh.geometry is None:
        raise ValueError(
            f"{mesh.path} has no geometry to scan — load it first")


def _edges_of(faces: np.ndarray) -> np.ndarray:
    """Every face's three edges as vertex-index pairs, each sorted low-high.

    Sorting each pair makes an edge's identity independent of the direction the
    face traverses it, which is what lets two faces sharing an edge be counted
    as sharing it.  Direction is not lost to the caller that needs it —
    `winding_seams` derives it separately, because there the traversal
    direction is the whole signal.
    """
    edges = np.concatenate([faces[:, [0, 1]],
                            faces[:, [1, 2]],
                            faces[:, [2, 0]]])
    return np.sort(edges, axis=1)


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
    _, counts = np.unique(_edges_of(faces), axis=0, return_counts=True)
    return Scan(open_edges=int((counts == 1).sum()),
                non_manifold=int((counts > 2).sum()),
                faces=len(faces),
                degenerate=degenerate)


def open_loops(mesh: Mesh) -> tuple[Loop, ...]:
    """Group the open edges into boundaries and measure each one.

    Returned largest first.  A caller decides what to do with the sizes; this
    only measures them.

    The measurement exists because the pipeline's success condition used to be
    `open == 0`, a mathematical standard rather than a manufacturing one.  On
    one model that cost the head and torso: four open edges spanning 0.01 mm
    triggered a Blender repair, which closed the pinhole and punched 28 new
    holes, which triggered PyMeshFix again, which finished at 44.94% of the
    original volume — cut ragged at the waist.  Every hole in that cascade was
    smaller than a third of the finest layer the collection prints at, so none
    of them could reach the plate.  The repair destroyed half a model to fix
    nothing that existed at print scale.
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
    """Find edges where two faces disagree about which way the surface faces.

    Returns `(seam_edges, closed_loops)`.

    On a consistently wound surface the two faces sharing an edge traverse it
    in *opposite* directions.  Traversing it the same way means the surface
    reverses there — and when those edges form closed loops, they bound two
    regions whose winding cannot be reconciled: hair over a scalp, cloth over a
    body, a separately sculpted part fused to its host.

    **The loop count is what matters, not the edge count.**  Measured on one
    model:

        head deleted by PyMeshFix    40 seam edges, 7 closed loops, 0 loose ends
        renders and prints fine       5 seam edges, 0 closed loops, 4 loose ends

    A few seam edges with dangling ends are local noise that stops on its own.
    A closed loop encircles something, and PyMeshFix will delete what it
    encircles unless the mesh is split at the seam first.
    """
    edges = seam_edges(mesh)
    if len(edges) == 0:
        return 0, 0
    return len(edges), _count_closed_loops(edges)


def _count_closed_loops(seam: np.ndarray) -> int:
    """How many of these seam edges form closed rings.

    A closed loop is a connected run where every vertex has exactly two seam
    edges — no ends, no branches.  That is the distinction that matters: a few
    seam edges with dangling ends are local noise that stops on its own, while
    a closed loop encircles a region PyMeshFix will delete.
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
    # test in `test_scanner.py` cannot catch it — agreement is not correctness
    # when both sides share a mistake.  Degenerate faces are counted by
    # `scan().degenerate`, which is where they belong.
    found = found[found[:, 0] != found[:, 1]]
    return np.ascontiguousarray(found, dtype=np.int64)


def shells(mesh: Mesh) -> tuple[np.ndarray, ...]:
    """Connected components, as face-index arrays, largest first.

    **Two faces are in the same shell when they share an EDGE** — two vertices,
    not one.  Surfaces that meet at a single point are separate shells.

    That distinction is not pedantic, it is the one that matters here.  A vertex
    where two surfaces touch is non-manifold by construction, and PyMeshFix
    rebuilds *one* manifold surface and discards the rest — so handing it a
    vertex-joined pair as a single component gives it exactly the input that
    makes it delete geometry, which is the failure the split exists to prevent.

    Measured on `Mandy_Body_Dinamuuu3D.stl`: vertex connectivity reports 39
    components with a largest of 1,315,986 faces; edge connectivity reports 40,
    splitting that one into 941,571 + 374,415 joined at a single vertex.  The
    edge-connected answer matches PyMeshLab's
    `generate_splitting_by_connected_components` exactly, component for
    component and face for face — which is the behaviour the pipeline was built
    around.

    Returned as indices into the original face array rather than as meshes, so
    a caller that only wants a count pays nothing for geometry it will not use.

    **Why scipy.**  The predecessor was a per-face Python `find()` loop, and
    5-10x the cost of loading the mesh.  Measured on the same model:

        union-find (vertex-connected)   12.75s raw    5.72s decimated
        scipy, vertex-connected          0.42s        0.14s
        scipy, edge-connected            2.52s        0.92s   <- this code

    Edge connectivity costs more than vertex connectivity — it needs a lexsort
    over `3 * faces` edge rows to find which faces share one — and that is the
    price of the correct answer.  Still 5x faster than the loop it replaced,
    and at 0.92s on the mesh the pipeline actually sees (decimation runs first,
    D13) it remains cheaper than `scan()` itself at 2.58s.

    **A pure-numpy replacement was tried first and was slower on real
    geometry.**  Pointer-jumping label propagation is 15-24x faster than
    union-find on synthetic meshes and **2x slower** on Mandy: it converges in
    O(log diameter) passes only when chains actually collapse, and a 1.3M-face
    organic shell took **194 passes** at 0.13s each.  The synthetic benchmark
    that endorsed it — a long thin strip — has three components and converges
    in 11.  Do not retry it without a fixture whose component structure
    resembles a real model.
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
