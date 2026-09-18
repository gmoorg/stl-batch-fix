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

**The repair is one face split**, fanned from the corner opposite the edge.
Cut the neighbouring face at every M along it and the single face becomes
`n + 1`, sharing new edges from each M to that corner.  Every edge then has two
faces, the Ms' fans close, and **no vertex moves and nothing is deleted**.

A single M is the common case and costs one face.  **It is the n=1 case, not
the definition** — an edge subdivided twice gives `a-M1-M2-c` and costs two,
in a single split rather than two rounds.

**The split creates no winding seams**, and getting there took a correction
worth recording.  An earlier version of this search reported 19 junctions on
Mandy and took the seam count from 4/1 to **16/5**.  That was explained away as
"the repair uncovers a pre-existing disagreement" — all 12 new seam edges had
indeed been open before, and an open edge cannot be a seam.

The explanation was true and the conclusion was wrong.  Those 19 included the
`Uncovered` arrangement: faces split *across a hole*, where the far side never
reaches the spanning edge.  With the strip test below rejecting them, Mandy
reports **2** junctions and the seam count does not move at all — 4/1 before
and after, on both real models.

Winding itself was never the problem.  For one junction the bottom faces
traverse (31816, 31443) and (31486, 31816) while the new faces traverse
(31443, 31816) and (31816, 31486): opposite, which is what a correctly wound
shared edge looks like.  `test_winding_is_preserved` guards that.

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

#: **There is no tolerance constant, and that is the point.**  `DEFAULT_TOLERANCE`,
#: `MAX_GAP` and `SIDE_FLOOR` all lived here and are gone: the search is
#: topological, and the one geometric quantity it uses is `t`, a ratio.
#:
#: The history is worth keeping because the same mistake is easy to repeat.
#: The original test measured M's perpendicular distance to the a-c line
#: against an absolute threshold of `1e-6` mm.  float32 carries ~7 significant
#: digits, so a coordinate's own rounding error grows with the model:
#:
#:     r=1     1.9e-08   found
#:     r=10    2.6e-23   found
#:     r=50    1.2e-06   MISSED
#:     r=100   2.5e-06   MISSED
#:     r=200   5.0e-06   MISSED
#:
#: Making it relative to the edge length fixed the scale bug but kept the wrong
#: question: it still asked whether M lies *on* the line, when a T-junction's
#: M is often well off it — Mandy carries one at **half an edge-length** away
#: whose open-edge path plainly runs from one end of the spanning edge to the
#: other.
#:
#: Walking that path answers the right question with no measurement at all.
#: Verified identical to the calibrated version on every fixture (150, 160, 1,
#: and zero on each negative), across eight radii from 0.5 mm to 5000 mm, and
#: it additionally handles the multi-vertex case the distance test could not.

#: Cap on repair rounds.  Splitting a face changes the edge map, so a vertex
#: found on an edge that no longer exists must be re-found; the loop repeats
#: until a pass finds nothing.  Measured: 150 scattered junctions converge in
#: **2** rounds, the second only confirming none remain.  The cap is a
#: guard against a pathological mesh, not an expected limit.
MAX_ROUNDS = 10

#: Longest path of open edges accepted between the ends of a spanning edge.
#:
#: A subdivided edge normally yields one or two interior vertices, so this is a
#: guard against a pathological boundary rather than an expected limit — the
#: walk is a search, and without a cap a long open boundary could be explored
#: exhaustively.
#:
#: Measured: every chain found on `tjunction_many`, `allbad`, Mandy and
#: costume01 has **one or two** vertices.  Twelve is six times the largest
#: seen.
MAX_CHAIN = 12


@dataclass(frozen=True)
class TJunction:
    """One vertex sitting on an edge that does not reference it.

    vertex     the index of the offending vertex — the first of `chain` when
               the edge was subdivided more than once
    edge       the `(low, high)` edge it lies on
    face       the face owning that edge — the one to split
    position   where along the edge, 0 to 1
    distance   how far off the line it actually sits
    chain      every offending vertex on this edge, in order from the low end
               to the high one.  Usually a single vertex; an edge subdivided
               twice gives two, and the face then splits into three rather
               than two.

    `vertex`, `position` and `distance` describe `chain[0]` and are kept
    because a caller reporting one junction per face wants one vertex, not a
    tuple — but `chain` is what `repair` acts on.
    """

    vertex: int
    edge: tuple[int, int]
    face: int
    position: float
    distance: float
    chain: tuple[int, ...] = ()


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
    """Map each undirected edge to the faces using it.

    **Faces without three distinct corners are skipped**, and that is not
    tidiness — including them corrupts the ownership count in two ways at once.
    Measured on `sphere_allbad`, which carries two degenerate faces:

        face 840 = (302, 302, 280)

        (302, 302)   a self-edge, an edge that is really a point
        (280, 302)   emitted TWICE by this one face, so its owner list is
                     [100, 840, 840] -- length 3, and the edge reads as
                     *paired* when it is genuinely open

    The second is the damaging one: an open edge that looks closed is invisible
    to anything searching the boundary.  `scanner` learned the same lesson on
    real data (D18) — `Hair.stl` reported 106 "winding seam edges", all 106 of
    them self-edges from 250 degenerate faces, which is the pipeline's
    strongest "split this mesh" signal fired by zero-area triangles.

    A degenerate face has no area, contributes nothing to the surface, and is
    removed by CLEAN's `remove_null_faces` — but CLEAN runs *after* this
    module, so the faces are here when `welder` looks.
    """
    owners: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (a, b, c) in enumerate(faces):
        if len({a, b, c}) < 3:
            continue
        for u, w in ((a, b), (b, c), (c, a)):
            owners[(min(u, w), max(u, w))].append(index)
    return owners


def _find_in(verts: np.ndarray,
             faces: list[list[int]],
             max_chain: int = MAX_CHAIN) -> dict[int, TJunction]:
    """One junction per face, keyed by face — a face is split once per round.

    **Topological, with no tolerance.**  The structure is visible in the open
    edges alone, and every triangle involved has exactly one:

              B                  X spans the open edge (a, c)
             / \
            /   \               a -- M1 -- M2 -- c   are open edges too,
        a--M1-M2--c              one per bottom triangle
            \| |/
              K                  the spokes to K are paired, not open

    So where X sees a single edge, the other side of the surface has a *path*
    of open edges from a to c, and its interior vertices are the ones X should
    have been using.  The search is that walk.  Nothing in it is metric, so
    nothing measures a distance against a threshold.

    **A single interior vertex is the n=1 case, not the definition.**  An
    earlier rule required a face holding `a` and `M` and another holding `M`
    and `c`; on `a-M1-M2-c` neither M has both, and it found nothing.

    Two dimensionless conditions do the work:

    **Every vertex on the path must project strictly inside (a, c)**, `0 < t <
    1`.  That is what breaks the symmetry: a junction has *three* open edges —
    the spanning one and the path — and all three look alike topologically.
    Only for the spanning edge do the others' vertices fall between its ends.
    `t` is a ratio, not a tolerance.

    **The path may not be the edge itself**, so the walk never steps a to c
    directly.

    The bottom faces need not share a single apex.  That is the tidy case; on
    Mandy a real two-vertex chain runs across three faces with three different
    third corners (8738, 9164, 9526), and the walk is indifferent to it.

    **This assumes the spanning edge is open.**  Constructed counter-case: a
    mesh where (a, c) was shared by two faces — so it looked properly paired —
    while its halves were the open ones; nothing was found.  Artificial, and no
    real mesh has produced it, but the assumption is real.
    """
    owners = _edge_faces(faces)
    open_edges = {edge for edge, owning in owners.items() if len(owning) == 1}
    if not open_edges:
        return {}
    open_at: dict[int, set[int]] = defaultdict(set)
    for low, high in open_edges:
        open_at[low].add(high)
        open_at[high].add(low)

    found: dict[int, TJunction] = {}
    for (a, c) in open_edges:
        face = owners[(a, c)][0]
        if face in found:
            continue                       # already splitting this one
        origin, along = verts[a], verts[c] - verts[a]
        length_squared = float(along @ along)
        if length_squared == 0.0:
            continue                       # degenerate edge, nothing to lie on

        def position(vertex: int) -> float:
            return float((verts[vertex] - origin) @ along / length_squared)

        def is_strip(path: tuple[int, ...]) -> bool:
            """Do the faces owning this path's links form a connected strip?

            **This is what separates a subdivided edge from a hole**, and it
            is the whole reason `welder` does not fire on the `Uncovered`
            arrangement.  Consecutive links must share an *edge*, not merely a
            vertex: a subdivided side is one surface walked across, while the
            far side of a hole is two pieces meeting at a point.

            Measured on the two sketches.  Covered: the chain's faces are
            `[6,0,5]` and `[3,6,5]`, sharing edge (6,5) — the M-K spoke.
            Uncovered: `[0,1,5]` and `[2,3,5]`, sharing only vertex 5, on
            opposite sides of the gap.

            It also resolves the pattern's three-way symmetry for free.  A
            junction has three open edges and all look alike topologically;
            only the spanning one's chain is a strip, so the two twins fail
            here rather than needing a geometric tie-break.
            """
            links = [owners[(min(path[i], path[i + 1]),
                             max(path[i], path[i + 1]))][0]
                     for i in range(len(path) - 1)]
            if face in links:
                return False               # the chain ran back through X
            return all(len(set(faces[links[i]]) & set(faces[links[i + 1]])) == 2
                       for i in range(len(links) - 1))

        # Walk open edges outward from `a`, admitting only vertices that fall
        # strictly inside (a, c), until one of them reaches `c` by a strip.
        stack = [(a, (a,))]
        chain: tuple[int, ...] = ()
        while stack and not chain:
            node, path = stack.pop()
            for nxt in open_at[node]:
                if nxt == c:
                    if node != a and is_strip(path + (c,)):
                        chain = path[1:]
                        break
                    continue               # never the (a, c) edge itself
                if nxt in path or len(path) > max_chain:
                    continue
                if not (0.0 < position(nxt) < 1.0):
                    continue
                stack.append((nxt, path + (nxt,)))

        if not chain:
            continue
        # **The spanning edge and its chain must form a simple closed loop.**
        # Every interior vertex therefore sits on exactly two open edges: the
        # link behind it and the link ahead.  A vertex with more is a pinch
        # where two boundary curves cross, and splitting there is a guess about
        # which curve to follow.
        #
        # Measured on costume01: vertex 246331 carries **four** open edges and
        # was claimed by two separate hits, each nominating it on a different
        # spanning edge.  Both are rejected here.
        #
        # **The endpoints are exempt, and that is not a loosening.**  Two
        # adjacent junctions legitimately share an endpoint, and it then
        # carries both loops.  Measured on `tjunction_many`, which subdivides
        # 150 random faces: requiring degree 2 on the endpoints too rejects
        # **107 of the 150**, all of them genuine by construction.
        if any(len(open_at[vertex]) != 2 for vertex in chain):
            continue
        chain = tuple(sorted(chain, key=position))
        first = chain[0]
        where = position(first)
        distance = float(np.linalg.norm(
            verts[first] - (origin + where * along)))
        found[face] = TJunction(first, (a, c), face, where, distance, chain)
    return found


def _split(triangle: list[int], edge: tuple[int, int],
           chain: tuple[int, ...]) -> list[list[int]]:
    """Cut `triangle` at every vertex of `chain`, which lie along `edge`.

    Returns `len(chain) + 1` triangles, fanned from the corner opposite the
    edge.  One vertex is the common case and gives two faces; an edge
    subdivided twice gives three.

    Winding is preserved by walking the triangle's own corner order and
    inserting the vertices where the edge is traversed, rather than rebuilding
    from the edge's sorted `(low, high)` form — which would silently reverse
    half the faces.  `chain` arrives ordered from the edge's low end, so it is
    reversed when the triangle happens to traverse the edge the other way.
    """
    for i in range(3):
        first, second = triangle[i], triangle[(i + 1) % 3]
        if {first, second} != set(edge):
            continue
        opposite = [v for v in triangle if v not in edge][0]
        ordered = chain if first == edge[0] else tuple(reversed(chain))
        walk = (first, *ordered, second)
        return [[walk[n], walk[n + 1], opposite]
                for n in range(len(walk) - 1)]
    raise ValueError(f"edge {edge} is not in face {triangle}")


def find(mesh: Mesh) -> tuple[TJunction, ...]:
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
    return tuple(_find_in(verts, faces).values())


def repair(mesh: Mesh, max_rounds: int = MAX_ROUNDS) -> Result:
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
        found = _find_in(verts, faces)
        if not found:
            break
        rebuilt: list[list[int]] = []
        for index, triangle in enumerate(faces):
            junction = found.get(index)
            if junction is None:
                rebuilt.append(triangle)
            else:
                rebuilt.extend(_split(triangle, junction.edge, junction.chain))
        faces = rebuilt
        splits += len(found)

    if splits == 0:
        return Result(mesh, 0, rounds)
    return Result(mesh.with_geometry(Geometry(mesh.geometry.verts,
                                              np.array(faces, dtype=np.int64))),
                  splits, rounds)
