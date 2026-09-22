"""Repair T-junctions by splitting the face that spans an open-edge path.

A T-junction is a subdivided edge (A-M-C) beside a face still using A-C.
`find` requires the open-edge path and adjacent face strip to agree; a nearby
vertex on a separate surface is not enough. `repair` adds faces without moving
vertices. The search uses topology and edge position, not an absolute gap.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from . import pipeconfig
from .mesh_io import Geometry, Mesh, require_geometry

#: Search open-edge paths instead of comparing coordinates to an absolute
#: distance. A changed model scale must not change whether a junction exists.
#: Measurements and rejected distance tests: archive/docs-before-compact-2026-09-19/refactor/implementation-evidence.md.

#: Repeat after a split because the edge map changes; cap pathological cases.
MAX_ROUNDS = 10

#: Bound the open-edge search. Observed junction chains had at most two
#: interior vertices; twelve leaves room without exploring a whole boundary.
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
    r"""One junction per face, keyed by face — a face is split once per round.

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
        # A valid interior path vertex has one incoming and one outgoing open
        # edge. A higher degree is an ambiguous pinch; endpoints may be shared
        # by adjacent junctions and are deliberately exempt.
        if any(len(open_at[vertex]) != 2 for vertex in chain):
            continue
        # The walk admits any interior vertex, so the path it found may run
        # backward along the edge it claims to follow.  Sorting was silently
        # discarding that: `_split` fans faces along the *chain's* order, so a
        # reordered chain builds edges the path does not have, and the measured
        # result was two new faces with all four open edges still open —
        # geometry stirred for nothing.  A path that does not progress is not
        # a T-junction this can repair, so it is refused and left intact for
        # the final scan to report honestly (A01).
        positions = [position(vertex) for vertex in chain]
        if positions != sorted(positions):
            continue
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
    require_geometry(mesh)
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
    require_geometry(mesh)
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


def step_weld_close_tjunctions(mesh: Mesh) -> tuple[bool, Mesh, str]:
    """`pipeconfig`'s uniform step contract, wrapping `repair()`.

    `repair()` has no partial-failure return of its own — it either
    produces a `Result` or raises (e.g. on unloaded geometry) — so any
    exception here is caught and converted rather than escaping.
    """
    if not pipeconfig.ENABLE_WELD:
        return True, mesh, 'skipped (ENABLE_WELD=False)'
    try:
        result = repair(mesh)
    except Exception as exc:
        return False, mesh, f"weld failed: {type(exc).__name__}: {exc}"
    detail = f"{result.splits} junction(s) in {result.rounds} round(s)"
    return True, result.mesh, detail
