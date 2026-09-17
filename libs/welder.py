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

**No vertex merge can fix a real one**, which is why `MERGE_DIST` and
Blender's `remove_doubles` do not help: nothing is coincident.  M is not on top
of another vertex, it is in the middle of somebody else's edge.

**But a merge fixes the thing that looks like one**, and telling them apart
matters (measured 2026-09-17).  A vertex can sit on an edge geometrically while
belonging to a *separate piece of surface* — no shared edge, no shared face.
That is two surfaces that should be joined and are not, and
`meshing_merge_close_vertices` stitches them: on costume01 it took the affected
edge from one face to two with every vertex surviving.  The topological test
below is what keeps this module from claiming that case.

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

#: How far M may sit **out of X's plane**, as a fraction of the edge's length.
#:
#: Not the distance from the a-c *line* — that was the old meaning and it was
#: wrong.  A vertex can lie well off the line while still being in the plane of
#: the face, which is precisely the T-junction where the other side of the
#: surface bulges away from the edge:
#:
#:         D                     M is off the A-C line, but in the ADC plane,
#:        / \                    and between A and C.  Splitting X at M and
#:       /  M\                   joining D-M is the repair.
#:      /  /  \
#:     A--------C
#:
#: Measured on costume01, which shows both cases in one model: one candidate
#: sits 4.4e-03 off the line but only 4.4e-06 out of plane — a thousand times
#: closer to the plane than to the line, and a junction the old gap test
#: discarded as "garbage".
#:
#: **A fraction, because an absolute distance cannot work** (the old value was
#: `1e-6` mm).  float32 carries ~7 significant digits, so a coordinate's own
#: rounding error grows with the model while the ratio stays flat.  The same
#: junction injected into the same sphere at seven radii, against that absolute
#: value:
#:
#:     r=1     1.9e-08   found
#:     r=10    2.6e-23   found
#:     r=50    1.2e-06   MISSED
#:     r=100   2.5e-06   MISSED
#:     r=200   5.0e-06   MISSED
#:     r=500   0.0       found
#:
#: A 100-200 mm print is an ordinary size, so that was a live gap.  Relative to
#: the edge, all nine radii tested (0.5 mm to 5000 mm) find it and repair clean.
#:
#: **The edge's own length rather than the bounding-box diagonal**, which is
#: what `repairer.CLEAN_FILTERS` uses.  On a model with 0.0088 mm edges beside
#: 90 mm features a diagonal-relative value means two very different things.
#:
#: **The fixtures cannot calibrate this**, and that is the honest limit.
#: Sweeping it across four orders of magnitude:
#:
#:     plane tol   tj_many  allbad  tjunction  fin  correct  costume01
#:     1e-5            150     160          1    0        0          0
#:     1e-4            150     160          1    0        0          0
#:     1e-3            150     160          1    0        0          2
#:     1e-2            150     160          1    0        0          4
#:
#: Every fixture is flat — they build their junctions as exact midpoints, so
#: any value finds them and no value admits a false one.  Only costume01 moves,
#: and **whether its candidates are genuine T-junctions is unverified**.  One
#: was traced by index and turned out to be a different defect entirely: a
#: separate piece of surface passing through nearly the same point, which
#: `merge_close_vertices` repairs.  See the `welder` entry in
#: REFACTOR_DECISIONS.md.
#:
#: So `1e-4` is chosen as the loosest value that admits nothing new on any
#: fixture, rather than as a measured optimum.  Loosening it to catch
#: costume01's candidates would be loosening it to catch defects nobody has
#: confirmed are ours to fix.
DEFAULT_TOLERANCE = 1e-4

#: Below this fraction of the edge length, M's offset from the line is float
#: noise and its **direction is meaningless**, so the which-side-of-the-edge
#: test is skipped.
#:
#: Measured, not guessed: 84 of `tjunction_many`'s 150 junctions have an offset
#: of 4.8e-07 whose dot product with the apex direction is -5.9e-09.  Those are
#: flush junctions — the offset is rounding error pointing nowhere in
#: particular — and applying the side test to them rejects two thirds of the
#: real defects at random.
SIDE_FLOOR = 1e-4

#: How far M may sit from the a-c **line**, as a fraction of the edge length.
#:
#: Deliberately loose — two orders of magnitude looser than the out-of-plane
#: limit — because being off the line is the *normal* case for a T-junction
#: where the other side of the surface bulges away from the edge.  This exists
#: only to reject a vertex that is in X's plane and between a and c but
#: nowhere near it, which on a curved surface is a large set: many vertices of
#: a sphere lie in the plane of any given triangle.
#:
#: Measured on `tjunction_many`, plane test only against plane test plus this:
#:
#:     no gap limit          163 junctions   3 of them 40% of an edge away
#:     gap <= 0.50 x edge    161
#:     gap <= 0.30 x edge    151
#:     gap <= 0.20 x edge    150   <- correct, and stable below this
#:     gap <= 0.01 x edge    150
#:
#: The three false picks sat 1.367 mm from a 3.4 mm edge, in plane and between
#: the endpoints, and splitting at them left 22 non-manifold edges where the
#: mesh had been clean.  0.2 is the loosest value that is still correct, so
#: 0.1 leaves a factor of two in hand.
MAX_GAP = 0.1

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

    **The test is topological and dimensionless.**  M is a T-junction on the
    open edge (a, c) of face X when it lies *between* a and c and on X's side
    of that edge — not when it lies *on* the line.  An earlier version measured
    the perpendicular gap from M down to a-c and rejected anything beyond a
    threshold, which was wrong twice over: it missed junctions where the other
    side of the surface bulges away from the line, and the threshold was an
    absolute distance that broke with model scale.

        X = [a, b, c]     spans the full open edge (a, c)
        M                 sits on an open edge reaching a or c
                          projects strictly between them, 0 < t < 1
                          and lies on the same side as X's apex

    **One end, not both.**  An edge subdivided twice gives (a,M1), (M1,M2),
    (M2,c), so neither interior vertex connects straight to the far end;
    requiring both was tried and missed that case entirely.

    **`t` is what breaks the pattern's symmetry.**  The full edge (a,c) and its
    halves are combinatorially identical under relabelling, so three different
    vertices get nominated as M.  Measured on one junction of `tjunction_many`:

        X=170  M=285  t= 2.0000   beyond an end
        X=838  M=283  t= 0.5000   <- the junction
        X=849  M=281  t=-1.0000   beyond an end

    Only one lies between a and c.  Picking by smallest face index instead was
    tried and is wrong — 170 < 838 here, and it chooses a vertex outside the
    edge.

    **The side test needs a floor, and this is measured rather than guessed.**
    A flush junction's offset from the line is float noise pointing in a random
    direction: 84 of `tjunction_many`'s 150 junctions have
    `offset . toward_apex = -5.9e-09` with an offset of 4.8e-07, so asking
    which side they sit on rejects them at random.  Below `SIDE_FLOOR` of the
    edge length the offset carries no direction and the side test is skipped.

    **`tolerance` no longer gates the distance from the line** — it is the
    out-of-plane limit, which is a different measurement.  A vertex can sit
    well off the a-c *line* while still lying in X's plane, which is exactly
    the case the gap test used to discard.

    **This assumes the spanning edge is open.**  Constructed counter-case: a
    mesh where (a,c) was shared by two faces — so it looked properly paired —
    while its two halves were the open edges; nothing was found.  Artificial,
    and no real mesh has produced it, but the assumption is real.
    """
    owners = _edge_faces(faces)
    open_edges = {edge: owning for edge, owning in owners.items()
                  if len(owning) == 1}
    if not open_edges:
        return {}

    # For each vertex, which other vertices it shares an open edge with.
    open_at: dict[int, set[int]] = defaultdict(set)
    for u, w in open_edges:
        open_at[u].add(w)
        open_at[w].add(u)

    found: dict[int, TJunction] = {}
    for (a, c), owning in open_edges.items():
        face = owning[0]
        if face in found:
            continue                       # already splitting this one
        apex = [v for v in faces[face] if v not in (a, c)]
        if not apex:
            continue                       # degenerate face, no third corner
        apex = apex[0]

        start, along = verts[a], verts[c] - verts[a]
        length_squared = float(along @ along)
        if length_squared == 0.0:
            continue                       # degenerate edge, nothing to lie on
        edge_length = float(np.sqrt(length_squared))

        # X's plane, and the in-plane direction from the edge to its apex.
        normal = np.cross(along, verts[apex] - start)
        normal_length = float(np.linalg.norm(normal))
        if normal_length == 0.0:
            continue                       # X has no plane; nothing to be on
        normal = normal / normal_length
        to_apex = verts[apex] - (
            start + (float((verts[apex] - start) @ along) / length_squared)
            * along)
        to_apex = to_apex / float(np.linalg.norm(to_apex))

        for vertex in open_at[a] | open_at[c]:
            if vertex in (a, c) or vertex == apex:
                continue
            if any(face == owner
                   for end in (a, c)
                   for owner in owners.get(
                       (min(end, vertex), max(end, vertex)), ())):
                continue                   # X's own corner, not a junction

            t = float((verts[vertex] - start) @ along / length_squared)
            if not (0.0 < t < 1.0):
                continue                   # beyond an end — the symmetric twin

            offset = verts[vertex] - (start + t * along)
            distance = float(np.linalg.norm(offset))
            if abs(float(offset @ normal)) > tolerance * edge_length:
                continue                   # out of X's plane entirely
            if distance > MAX_GAP * edge_length:
                continue                   # in the plane, but nowhere near
            # The side test asks about the IN-PLANE component only.  An offset
            # perpendicular to the plane has no side — it is neither toward the
            # apex nor away from it — and testing the raw offset rejects it,
            # which is wrong: a vertex displaced straight out of the plane is
            # still between a and c.
            in_plane = float(offset @ to_apex)
            if (abs(in_plane) > SIDE_FLOOR * edge_length
                    and in_plane <= 0.0):
                continue                   # off the line and on the far side
            found[face] = TJunction(vertex, (a, c), face, t, distance)
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
