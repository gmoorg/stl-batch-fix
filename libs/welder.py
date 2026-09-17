"""Repair T-junctions by splitting the face that ignores them.

    find(mesh)          -> tuple[TJunction, ...]   where they are
    repair(mesh)        -> Result(mesh, splits, rounds)

A **T-junction** is a vertex lying on the interior of an edge that belongs to a
face not referencing it.  One side of the surface was subdivided and its
neighbour was not, so the two sides occupy the same line as *different* edges:

    A ---- M ---- C        the split side uses A-M and M-C
    A ------------ C       the neighbour still uses A-C

Geometrically flush — the gap is zero — and topologically unjoined.  Measured
on `sphere_tjunction.stl`: vertex 345 lies on edge (339, 358) at t=0.500, at a
distance of **2.6e-23**.

**No vertex merge can fix it**, which is why `MERGE_DIST` and Blender's
`remove_doubles` do not help: nothing is coincident.  M is not on top of
another vertex, it is in the middle of somebody else's edge.

**The repair is one face split.**  Cut the neighbouring face at M, and the
single face becomes two sharing a new edge from M to the opposite corner.
Every edge then has two faces, M's fan closes, and **no vertex moves and
nothing is deleted** — the mesh gains exactly one face per T-junction.

**Why this module exists rather than delegating.**  Measured on a 150-junction
fixture, against every other tool available:

    face split (this module)   910f -> 1060f   0 open   100.00%   clean
    commercial repair service  910f -> 1060f   0 open   100.01%   clean
    PyMeshFix                  910f -> 1000f   0 open    99.78%   2 verts lost
    Blender                    910f ->  824f   0 open    99.73%   118 verts lost

The commercial service reaches an identical face count independently, which is
good evidence the operation is right rather than merely clever.  PyMeshFix and
Blender both treat the open edges as *a hole to close* and move geometry to do
it — but there is no hole, only an edge that needs splitting, and that is where
their losses come from.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .mesh_io import Geometry, Mesh

#: How far a vertex may sit from an edge and still count as lying on it.
#:
#: Generous relative to a true T-junction, which is exact by construction — a
#: subdivision puts the vertex *on* the edge, and the fixture measures
#: 2.6e-23.  A junction arriving from a boolean operation or a decimation sits
#: near rather than on, so the tolerance must absorb float error without
#: splitting faces that merely pass close by.
#:
#: **KNOWN BUG (2026-09-17): this is an absolute distance and it breaks with
#: model scale.**  Measured on one T-junction injected into the same sphere at
#: seven radii — the junction's distance from the edge grows with the model,
#: because float32 carries ~7 significant digits:
#:
#:     r=1     1.9e-08   found
#:     r=10    2.6e-23   found
#:     r=50    1.2e-06   MISSED
#:     r=100   2.5e-06   MISSED
#:     r=200   5.0e-06   MISSED
#:     r=500   0.0       found
#:
#: A 100-200 mm print is an ordinary size, so this is a live gap.  A tolerance
#: of `radius * 1e-4` finds it at all seven.  The fix is to scale by the
#: bounding-box diagonal, as `repairer.CLEAN_FILTERS` already does — see the
#: OPEN BUG entry in REFACTOR_DECISIONS.md.
#:
#: **Not calibrated on real data.**  The fixtures are exact, so any value from
#: 1e-12 upward passes them; this is a starting point, not a measured one.
DEFAULT_TOLERANCE = 1e-6

#: Cap on repair rounds.  Splitting a face changes the edge map, so a vertex
#: found on an edge that no longer exists must be re-found; the loop repeats
#: until a pass finds nothing.  Measured: 150 scattered junctions converge in
#: **2** rounds, the second only confirming none remain.  The cap is a
#: guard against a pathological mesh, not an expected limit.
MAX_ROUNDS = 10


@dataclass(frozen=True)
class TJunction:
    """One vertex sitting on an edge that does not reference it.

    vertex     the index of the offending vertex
    edge       the `(low, high)` edge it lies on
    face       the face owning that edge — the one to split
    position   where along the edge, 0 to 1
    distance   how far off the line it actually sits
    """

    vertex: int
    edge: tuple[int, int]
    face: int
    position: float
    distance: float


@dataclass(frozen=True)
class Result:
    """What the repair did.

    mesh     the repaired mesh, or the input unchanged when nothing was found
    splits   how many faces were split — equal to the junctions repaired
    rounds   passes taken, including the final one that found nothing
    """

    mesh: Mesh
    splits: int
    rounds: int

    @property
    def repaired(self) -> bool:
        return self.splits > 0


def _edge_faces(faces: list[list[int]]) -> dict[tuple[int, int], list[int]]:
    """Map each undirected edge to the faces using it."""
    owners: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (a, b, c) in enumerate(faces):
        for u, w in ((a, b), (b, c), (c, a)):
            owners[(min(u, w), max(u, w))].append(index)
    return owners


def _find_in(verts: np.ndarray,
             faces: list[list[int]],
             tolerance: float) -> dict[int, TJunction]:
    """One junction per face, keyed by face — a face is split once per round.

    Only **open** edges are considered, and only vertices already on an open
    boundary.  A T-junction always produces open edges: the unsplit neighbour's
    full-length edge has one face, and so do the two halves on the split side.
    Searching every vertex against every edge would be quadratic for no gain.

    **This assumes the spanning edge is one of the open ones**, which is not
    stated anywhere else and is worth knowing.  Constructed counter-case: a mesh
    where the full-length edge (a,c) was shared by two faces — so it looked
    properly paired — while its two halves were the open edges.  `find`
    returned 0.  That fixture was artificial and no real mesh has produced it,
    but the assumption is real.

    **The search is also symmetric across the three open edges of a junction**,
    and cannot resolve which vertex is the interior one: the full edge (a,c) and
    its halves (a,M), (M,c) are combinatorially identical under relabelling, so
    a purely topological reading nominates three different vertices as M.  Only
    the distance test below separates them — which is why the tolerance is a
    tie-breaker among plausible candidates rather than a detection threshold.
    """
    owners = _edge_faces(faces)
    open_edges = {edge: owning for edge, owning in owners.items()
                  if len(owning) == 1}
    candidates = {v for edge in open_edges for v in edge}

    found: dict[int, TJunction] = {}
    for (a, b), owning in open_edges.items():
        face = owning[0]
        if face in found:
            continue                       # already splitting this one
        start, along = verts[a], verts[b] - verts[a]
        length_squared = float(along @ along)
        if length_squared == 0.0:
            continue                       # degenerate edge, nothing to lie on
        for vertex in candidates:
            if vertex in (a, b):
                continue
            t = float((verts[vertex] - start) @ along / length_squared)
            if not (1e-6 < t < 1.0 - 1e-6):
                continue                   # at or beyond an end, not interior
            distance = float(np.linalg.norm(
                verts[vertex] - (start + t * along)))
            if distance < tolerance:
                found[face] = TJunction(vertex, (a, b), face, t, distance)
                break
    return found


def _split(triangle: list[int], edge: tuple[int, int],
           vertex: int) -> list[list[int]]:
    """Cut `triangle` at `vertex`, which lies on `edge`.

    Winding is preserved by walking the triangle's own corner order and
    inserting the vertex where the edge is traversed, rather than rebuilding
    from the edge's sorted `(low, high)` form — which would silently reverse
    half the faces.
    """
    for i in range(3):
        first, second = triangle[i], triangle[(i + 1) % 3]
        if {first, second} == set(edge):
            opposite = [v for v in triangle if v not in edge][0]
            return [[first, vertex, opposite], [vertex, second, opposite]]
    raise ValueError(f"edge {edge} is not in face {triangle}")


def find(mesh: Mesh,
         tolerance: float = DEFAULT_TOLERANCE) -> tuple[TJunction, ...]:
    """Every T-junction in the mesh, **one per affected face**.

    Detection only — for reporting, or for deciding whether repair is worth
    running.  `repair` does its own search each round, since splitting changes
    what there is to find.

    **The count is a lower bound, not a total** (measured 2026-09-17).  A face
    whose edge is subdivided more than once carries several junctions and this
    reports one of them — and *which* one is arbitrary, since the search breaks
    on the first match while iterating a set.

    Measured on a sphere with one spanning edge subdivided twice, at t=1/3 and
    t=2/3: `find` returns **1**, the junction at t=0.667, and misses the other.
    `repair` on the same mesh is correct — `splits=2, rounds=3`, ending
    `open=0 nm=0` — because each round re-searches after the edge map changes.
    So `rounds > 2` is the signal that a face carried more than one junction.

    A caller counting defects or deciding "is repair worth running" therefore
    gets a number that can be too low.  It is never too high, so a non-zero
    answer always means there is real work to do.
    """
    if mesh.geometry is None:
        raise ValueError(f"{mesh.path} has no geometry — load it first")
    verts = mesh.geometry.verts.astype(np.float64)
    faces = [list(t) for t in mesh.geometry.faces.tolist()]
    return tuple(_find_in(verts, faces, tolerance).values())


def repair(mesh: Mesh,
           tolerance: float = DEFAULT_TOLERANCE,
           max_rounds: int = MAX_ROUNDS) -> Result:
    """Split every face that ignores a vertex lying on one of its edges.

    Returns the input unchanged when there is nothing to do, so a caller needs
    no branch.

    **Nothing is moved and nothing is deleted.**  The vertex array is passed
    through untouched; only the face list grows, by exactly one face per
    junction repaired.  That is the property distinguishing this from the
    hole-closing repairs, which lose vertices getting to the same face counts.

    Repeats because splitting changes the edge map: a junction on an edge that
    a previous split replaced has to be re-found.  Two rounds suffice on 150
    scattered junctions, the second finding nothing.
    """
    if mesh.geometry is None:
        raise ValueError(f"{mesh.path} has no geometry — load it first")

    verts = mesh.geometry.verts.astype(np.float64)
    faces = [list(t) for t in mesh.geometry.faces.tolist()]
    splits = 0
    rounds = 0

    for rounds in range(1, max_rounds + 1):
        found = _find_in(verts, faces, tolerance)
        if not found:
            break
        rebuilt: list[list[int]] = []
        for index, triangle in enumerate(faces):
            junction = found.get(index)
            if junction is None:
                rebuilt.append(triangle)
            else:
                rebuilt.extend(_split(triangle, junction.edge, junction.vertex))
        faces = rebuilt
        splits += len(found)

    if splits == 0:
        return Result(mesh, 0, rounds)
    return Result(mesh.with_geometry(Geometry(mesh.geometry.verts,
                                              np.array(faces, dtype=np.int64))),
                  splits, rounds)
