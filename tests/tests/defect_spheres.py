"""Defect spheres: test fixtures whose true shape is known.

Each fixture is a mesh with known defects plus its ground truth: the clean,
closed, outward shells whose UNION is the intended printable shape. Outcome
tests (end-to-end through batch_repair.py, and per-step) compare a repair's
output with that truth — never with how the pipeline is assembled.

Truth contract for shape tests:

* compare against the BOUNDARY of the union of `truth` parts, not every
  truth triangle: where shells overlap or touch, their buried faces are not
  part of the printable surface (`union_boundary_samples`);
* the truth is the clean polyhedron (the UV sphere sags ~0.12 mm between
  vertices), not an ideal sphere;
* `truth_volume` is the union's volume, computed numerically
  (`union_volume`);
* every `vanish_boxes` box must be empty in a correct output (debris).

The single-defect builders below (`sphere`, `build_*`, `BUILDERS`) moved here
unchanged from tools/make_probe_meshes.py, which re-exports them; the
committed tests/probes/sphere_*.stl stay byte-reproducible from them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

def sphere(r=10.0, seg=20):
    """A closed UV sphere: watertight, manifold, single-vertex poles."""
    verts = [(0, 0, r)]
    for i in range(1, seg):
        lat = np.pi * i / seg
        for j in range(seg):
            lon = 2 * np.pi * j / seg
            verts.append((r * np.sin(lat) * np.cos(lon),
                          r * np.sin(lat) * np.sin(lon),
                          r * np.cos(lat)))
    verts.append((0, 0, -r))
    south = len(verts) - 1
    faces = []
    for j in range(seg):
        faces.append([0, 1 + j, 1 + (j + 1) % seg])
    for i in range(seg - 2):
        a = 1 + i * seg
        b = 1 + (i + 1) * seg
        for j in range(seg):
            j2 = (j + 1) % seg
            faces.append([a + j, b + j, b + j2])
            faces.append([a + j, b + j2, a + j2])
    a = 1 + (seg - 2) * seg
    for j in range(seg):
        faces.append([a + j, south, a + (j + 1) % seg])
    return np.array(verts, np.float32), np.array(faces, np.int64)


def build_correct(v, f):
    """The control. Every other mesh differs from this by exactly one defect."""
    return v, f


def build_inverted(v, f):
    """Every face reversed: inside-out, and NOTHING detects it.

    Settled 2026-09-16: scanner blind, PyMeshFix a no-op, a commercial repair
    service reports "0 Inverted normals", and Bambu both renders and slices it
    normally. Only signed volume tells it apart (-4094.9 vs +4094.9).
    """
    return v, f[:, ::-1].copy()


def build_seam(v, f):
    """The northern cap reversed: a closed winding-seam loop.

    The partial case, and unlike `inverted` it IS detected — by us (40 seam
    edges, 1 closed loop) and by the online service (40 inverted normals).
    """
    cap = v[f].mean(axis=1)[:, 2] > 5.0
    f = f.copy()
    f[cap] = f[cap][:, ::-1]
    return v, f


def build_inverted_third(v, f):
    """Exactly one third of the faces reversed, chosen by index rather than
    by position.

    Distinct from `seam`, whose cap is defined geometrically (z > 5.0) and
    happens to be 32% — a contiguous region bounded by one closed loop.  This
    one reverses every third face, so the reversed set is **scattered** and its
    boundary is many short loops rather than one ring.

    It exists to separate two things the orientation guard conflates: a mesh
    whose total volume stays positive because the reversed part is a minority
    (both cases), and a reversed region that `by_seams` can isolate as a
    connected component (only `seam`).  Scattered faces cannot be split out,
    so this is the harder case and the one a per-region guard will not catch.
    """
    f = f.copy()
    f[::3] = f[::3][:, ::-1]
    return v, f


def build_two_shells(v, f):
    """Two separate spheres, both correctly wound.

    The plain multi-shell case.  Every other probe is a single sphere, so
    `by_shells` returns them unsplit and step 4's per-part path never runs —
    this is the fixture that makes the split, the per-part loop and `merge`
    reachable at all.

    Volume is the sum of both: +8189.7.
    """
    second = (v + np.float32([40, 0, 0])).astype(np.float32)
    return np.vstack([v, second]), np.vstack([f, f + len(v)])


def build_shell_inverted(v, f):
    """A large correct sphere beside a small inverted one.

    **The fixture for the per-part orientation guard**, and the sizes are the
    point.  The small sphere is half the radius, so it contributes an eighth of
    the volume and the signed total stays **positive** (+3583.0) — step 2's
    guard correctly skips, and only the per-part guard after the split can see
    that one component is inside-out (-511.9 against the host's +4094.9).

    Two equal spheres would not do: their volumes cancel to -0.0, step 2 fires
    on that, and the per-part guard is never reached.  That was measured, not
    assumed.

    Correct result: both spheres outward, +4094.9 + 511.9 = **+4606.8**.
    """
    second = (v * 0.5 + np.float32([40, 0, 0])).astype(np.float32)
    return np.vstack([v, second]), np.vstack([f, f[:, ::-1] + len(v)])


def build_allbad(v, f):
    """Every defect at once, on one sphere.

    Built by composing the single-defect builders in a fixed order, so the
    result is reproducible and each ingredient is traceable — the original
    `sphere_allbad.stl` came from a scratch script and survived only as a file.

    Order matters: the index-based defects (`tjunction_many`, `degenerate`)
    run before `doubles` duplicates the whole thing, and `inverted` runs last
    so the mesh is wholly inside-out, which is what the orientation guard is
    meant to catch.
    """
    v, f = build_tjunction_many(v, f, n=80)
    v, f = build_degenerate(v, f)
    v, f = build_fin(v, f)
    v, f = build_doubles(v, f)
    return build_inverted(v, f)


def build_tjunction(v, f):
    """One vertex at an edge midpoint the neighbouring face does not use.

    Flush but unjoined — no vertex merge closes it, since nothing is
    coincident. scanner sees 3 open edges; PyMeshFix undoes it exactly.
    """
    a, b, c = f[200]
    v = np.vstack([v, ((v[a] + v[b]) / 2.0).astype(np.float32)])
    m = len(v) - 1
    f = f.tolist()
    f[200] = [a, m, c]
    f.append([m, b, c])
    return v, np.array(f, np.int64)


def build_tjunction_many(v, f, n=150, seed=0):
    """150 T-junctions at once, to test the Blender splitter's MAX_ITER=200.

    It is not a real limit: PyMeshFix closed all 450 resulting open edges.
    """
    verts, faces = v.tolist(), f.tolist()
    for t in np.random.default_rng(seed).choice(len(f), n, replace=False):
        a, b, c = faces[t]
        verts.append(((np.array(verts[a]) + np.array(verts[b])) / 2).tolist())
        m = len(verts) - 1
        faces[t] = [a, m, c]
        faces.append([m, b, c])
    return np.array(verts, np.float32), np.array(faces, np.int64)


def build_fin(v, f):
    """A triangle attached along one edge, sticking out into space."""
    v = np.vstack([v, np.array([[18, 0, 0]], np.float32)])
    a, b = f[200][0], f[200][1]
    return v, np.vstack([f, [[a, b, len(v) - 1]]])


def build_degenerate(v, f):
    """A zero-area face: two identical corners. PyMeshFix fixes it exactly."""
    return v, np.vstack([f, [[f[100][0], f[100][1], f[100][1]]]])


def build_doubles(v, f):
    """The same sphere twice, 1e-5 mm apart — the MERGE_DIST case.

    **The one real gap.** scanner reads it completely clean (nm=0, open=0,
    degenerate=0); only the shell count is 2, and two shells is legitimate on a
    real model. PyMeshFix is destructive: 1520 faces become 418 and the volume
    goes from +8189.7 to +2047.4 — half of ONE sphere. It discarded one shell
    and ate half the other.
    """
    return (np.vstack([v, v + np.float32([1e-5, 0, 0])]),
            np.vstack([f, f + len(v)]))


BUILDERS = {
    'correct': build_correct,
    'inverted': build_inverted,
    'inverted_third': build_inverted_third,
    'seam': build_seam,
    'tjunction': build_tjunction,
    'tjunction_many': build_tjunction_many,
    'fin': build_fin,
    'degenerate': build_degenerate,
    'doubles': build_doubles,
    'two_shells': build_two_shells,
    'shell_inverted': build_shell_inverted,
    'allbad': build_allbad,
}


# ---------------------------------------------------------------------------
# Fixture catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fixture:
    """One defect mesh and its ground truth (see module docstring).

    defects  what the self-check verifies: 'open' / 'non_manifold' /
             'seams' (True = must be present, False = must be absent),
             'shells' (exact component count), 'volume_sign' (+1/-1 of the
             mesh's signed volume), plus fixture-specific facts.
    """

    name: str
    verts: np.ndarray
    faces: np.ndarray
    truth: tuple
    truth_volume: float
    vanish_boxes: tuple
    defects: dict
    notes: str


def signed_volume(v, f) -> float:
    t = np.asarray(v, np.float64)[np.asarray(f)]
    return float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def box(lo, hi, n=5):
    """Closed, outward axis-aligned box, each face split into an n×n grid
    (n=5: 300 faces, above the splitter's 100-face floor)."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    verts, faces = [], []
    for axis in range(3):
        for side, value in ((0, lo[axis]), (1, hi[axis])):
            u, w = [a for a in range(3) if a != axis]
            base = len(verts)
            for i in range(n + 1):
                for j in range(n + 1):
                    p = np.empty(3)
                    p[axis] = value
                    p[u] = lo[u] + (hi[u] - lo[u]) * i / n
                    p[w] = lo[w] + (hi[w] - lo[w]) * j / n
                    verts.append(p)
            for i in range(n):
                for j in range(n):
                    a = base + i * (n + 1) + j
                    b, c, d = a + 1, a + n + 1, a + n + 2
                    # (u, w, axis) right-handed for axis 0 and 2, not for 1.
                    outward = (side == 1) != (axis == 1)
                    faces += [[a, c, d], [a, d, b]] if outward else [[a, d, c], [a, b, d]]
    v = np.asarray(verts, np.float64)
    # Merge the duplicated edge/corner vertices so the box is one closed shell.
    key = np.round(v / max(1e-9, float(np.ptp(v, 0).max())) * 1e9).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return v[first].astype(np.float32), inv.ravel()[np.asarray(faces)].astype(np.int64)


def sphere_with_rod(r=10.0, seg=20, rod_radius=0.75, rod_top=18.0):
    """ONE connected closed manifold surface: the UV sphere with its north
    pole fan replaced by a flat annulus down to radius `rod_radius` and a
    tube up to z = `rod_top`, capped. The rod is a thin attached feature,
    not a separate shell."""
    v, f = sphere(r, seg)
    ring = np.arange(1, seg + 1)                        # first latitude ring
    z0 = float(v[1, 2])
    angles = np.arctan2(v[ring, 1], v[ring, 0])
    inner = np.column_stack([rod_radius * np.cos(angles), rod_radius * np.sin(angles),
                             np.full(seg, z0)])
    top = inner.copy(); top[:, 2] = rod_top
    keep = f[seg:]                                      # drop the 20 pole-fan faces
    v_body = v[1:]                                      # drop the pole vertex
    keep = keep - 1
    outer = ring - 1
    n0 = len(v_body)
    inner_i = n0 + np.arange(seg)
    top_i = n0 + seg + np.arange(seg)
    cap = n0 + 2 * seg
    new = []
    for j in range(seg):
        k = (j + 1) % seg
        new += [[inner_i[j], outer[j], outer[k]], [inner_i[j], outer[k], inner_i[k]],
                [inner_i[j], inner_i[k], top_i[k]], [inner_i[j], top_i[k], top_i[j]],
                [cap, top_i[j], top_i[k]]]
    verts = np.vstack([v_body, inner, top, [[0.0, 0.0, rod_top]]]).astype(np.float32)
    return verts, np.vstack([keep, np.asarray(new)]).astype(np.int64)


def _halfspaces(v):
    from scipy.spatial import ConvexHull
    return ConvexHull(np.asarray(v, np.float64)).equations         # a·x + b <= 0 inside


def convex_overlap_volume(v1, v2) -> float:
    """Volume of the intersection of two convex polyhedra (their hulls).
    0 when they are disjoint or only touch (no interior point in common)."""
    from scipy.optimize import linprog
    from scipy.spatial import ConvexHull, HalfspaceIntersection
    hs = np.vstack([_halfspaces(v1), _halfspaces(v2)])
    A, b = hs[:, :3], hs[:, 3]
    norms = np.linalg.norm(A, axis=1)
    scale = float(max(np.ptp(np.vstack([v1, v2]).astype(np.float64), 0).max(), 1e-12))
    # Chebyshev centre: the deepest point inside both. Maximise r subject to
    # A x + r |a_i| <= -b, r >= 0.
    res = linprog(c=[0, 0, 0, -1], A_ub=np.column_stack([A, norms]), b_ub=-b,
                  bounds=[(None, None)] * 3 + [(0, None)], method='highs')
    if res.status == 2:                                  # infeasible: disjoint
        return 0.0
    if res.status != 0:
        raise RuntimeError(f'overlap solver failed: {res.message}')
    if res.x[3] <= 1e-9 * scale:                         # touching only
        return 0.0
    inter = HalfspaceIntersection(hs, res.x[:3])
    return float(ConvexHull(inter.intersections).volume)


def union_volume(parts) -> float:
    """Σ part volumes − Σ pairwise overlaps (convex parts, no triple overlap)."""
    total = sum(abs(signed_volume(v, f)) for v, f in parts)
    overlaps = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            o = convex_overlap_volume(parts[i][0], parts[j][0])
            overlaps.append((i, j, o))
    touched = [k for i, j, o in overlaps if o > 0 for k in (i, j)]
    if len(touched) != len(set(touched)):
        raise ValueError('union_volume supports pairwise overlaps only')
    return total - sum(o for _, _, o in overlaps)


def union_boundary_samples(fixture: Fixture, n: int, seed: int = 0, max_rounds: int = 50,
                           part: int | None = None):
    """Exactly `n` area-weighted points on the union's boundary, with their
    truth part's outward normals. A point of part i is buried — rejected —
    when a tiny step along its OWN outward normal enters another part
    (winding number > 0.5): that covers faces inside another part and faces
    shared with a touching part. `part` restricts proposals to that truth
    part (so a small part is not under-sampled), still rejecting points
    buried in any OTHER part."""
    import igl
    rng = np.random.default_rng(seed)
    parts = [(np.asarray(v, np.float64), np.asarray(f, np.int64)) for v, f in fixture.truth]
    scale = float(max(np.ptp(np.vstack([p[0] for p in parts]), 0).max(), 1e-12))
    areas = []
    for v, f in parts:
        a, b, c = (v[f[:, k]] for k in range(3))
        areas.append(np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2)
    if part is not None:
        areas = [a if i == part else np.zeros_like(a) for i, a in enumerate(areas)]
    weights = np.concatenate(areas) / sum(a.sum() for a in areas)
    owner = np.concatenate([np.full(len(a), i) for i, a in enumerate(areas)])
    local = np.concatenate([np.arange(len(a)) for a in areas])
    kept_p, kept_n = [], []
    for _ in range(max_rounds):
        pick = rng.choice(len(weights), size=n, p=weights)
        u, w = rng.random((2, n)); flip = u + w > 1; u[flip], w[flip] = 1 - u[flip], 1 - w[flip]
        # Filled in proposal order, so truncating to n below keeps a random
        # (area-weighted) subset rather than favouring earlier parts.
        P, N = np.empty((n, 3)), np.empty((n, 3))
        buried = np.zeros(n, bool)
        for i, (v, f) in enumerate(parts):
            sel = np.flatnonzero(owner[pick] == i)
            if not len(sel):
                continue
            tri = v[f[local[pick[sel]]]]
            P[sel] = tri[:, 0] + u[sel, None] * (tri[:, 1] - tri[:, 0]) + w[sel, None] * (tri[:, 2] - tri[:, 0])
            nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            N[sel] = nrm / np.linalg.norm(nrm, axis=1)[:, None]
            for j, (vj, fj) in enumerate(parts):
                if j != i:
                    buried[sel] |= igl.fast_winding_number(vj, fj, P[sel] + N[sel] * 1e-6 * scale) > 0.5
        kept_p.append(P[~buried]); kept_n.append(N[~buried])
        if sum(len(k) for k in kept_p) >= n:
            return np.vstack(kept_p)[:n], np.vstack(kept_n)[:n]
    raise RuntimeError(f'union_boundary_samples: only {sum(len(k) for k in kept_p)} of {n} accepted')


def _with(v, f, *extra):
    vs, fs, off = [np.asarray(v, np.float32)], [np.asarray(f, np.int64)], len(v)
    for ev, ef in extra:
        vs.append(np.asarray(ev, np.float32)); fs.append(np.asarray(ef, np.int64) + off); off += len(ev)
    return np.vstack(vs), np.vstack(fs)


def fixtures() -> dict:
    """Every fixture, keyed by name."""
    v, f = sphere()
    clean = (v, f)
    one = (clean,)
    r = 10.0
    fin_tip = (np.array([16.5, -1.5, -1.5]), np.array([18.5, 1.5, 1.5]))
    out = {}

    def add(name, mv, mf, truth, defects, notes, vanish=()):
        out[name] = Fixture(name, np.asarray(mv, np.float32), np.asarray(mf, np.int64), tuple(truth),
                            union_volume(list(truth)), tuple(vanish), defects, notes)

    add('correct', *build_correct(v, f), one, dict(open=False, non_manifold=False, seams=False, shells=1, volume_sign=1), 'control')
    add('inverted', *build_inverted(v, f), one, dict(open=False, non_manifold=False, seams=False, shells=1, volume_sign=-1), 'every face reversed')
    add('inverted_third', *build_inverted_third(v, f), one, dict(open=False, non_manifold=False, seams=True, shells=1), 'every third face reversed')
    add('seam', *build_seam(v, f), one, dict(open=False, non_manifold=False, seams=True, shells=1), 'northern cap reversed')
    add('tjunction', *build_tjunction(v, f), one, dict(open=True, shells=1), 'one T-junction')
    add('tjunction_many', *build_tjunction_many(v, f), one, dict(open=True, shells=1), '150 T-junctions')
    add('fin', *build_fin(v, f), one, dict(non_manifold=True, shells=1), 'triangle flap to (18,0,0)', vanish=(fin_tip,))
    add('degenerate', *build_degenerate(v, f), one, dict(degenerate=True, shells=1), 'zero-area face')
    add('doubles', *build_doubles(v, f), one, dict(open=False, non_manifold=False, shells=2), 'same sphere twice, 1e-5 apart')
    two = build_two_shells(v, f)
    add('two_shells', *two, (clean, (two[0][len(v):], f)), dict(open=False, shells=2, volume_sign=1), 'two separate spheres')
    si = build_shell_inverted(v, f)
    small = (si[0][len(v):], f)                          # outward truth for the inverted small one
    add('shell_inverted', *si, (clean, small), dict(open=False, shells=2), 'small sphere inside-out')
    add('allbad', *build_allbad(v, f), one, dict(open=True, non_manifold=True, degenerate=True, shells=2, volume_sign=-1),
        'T-junctions, degenerate, fin, doubles, inverted', vanish=(fin_tip,))

    holed = np.delete(f, np.arange(370, 382), axis=0)
    add('hole', v, holed, one, dict(open=True, non_manifold=False, shells=1), '12 equatorial faces removed')
    shifted = (v + np.float32([12, 0, 0])).astype(np.float32)
    add('overlapping_shells', *_with(v, f, (shifted, f)), (clean, (shifted, f)),
        dict(open=False, non_manifold=False, shells=2, overlap=True), 'two spheres, centres 12 apart (r 10): union')
    b1, b2 = box((0, 0, 0), (10, 10, 10)), box((10, 0, 0), (20, 10, 10))
    add('touching_shells', *_with(*b1, b2), (b1, b2),
        dict(open=False, non_manifold=False, shells=2, touching_plane_x=10.0), 'two boxes sharing the plane x = 10')
    sheet_v = np.array([[40, -3, -3], [40, 3, -3], [40, 3, 3], [40, -3, 3]], np.float32)
    add('debris_sheet', *_with(v, f, (sheet_v, [[0, 1, 2], [0, 2, 3]])), one,
        dict(open=True, shells=2), 'open 6×6 sheet at x = 40',
        vanish=((np.array([39, -4, -4]), np.array([41, 4, 4])),))
    speck = box((40, 0, 0), (40.5, 0.5, 0.5), n=1)
    add('debris_speck', *_with(v, f, speck), one,
        dict(open=False, shells=2), 'closed 0.5³ box (12 faces) at x = 40; intent, independent of the splitter floor',
        vanish=((np.array([39.5, -0.5, -0.5]), np.array([41, 1, 1])),))
    rod = sphere_with_rod()
    add('thin_rod', *rod, (rod,), dict(open=False, non_manifold=False, seams=False, shells=1, volume_sign=1,
                                       rod_radius=0.75, rod_top=18.0), 'one shell: sphere + attached rod r 0.75 to z = 18')
    inch = (v / np.float32(25.4)).astype(np.float32)
    add('inch_scale', inch, f, ((inch, f),), dict(open=False, non_manifold=False, shells=1, volume_sign=1, scale=1 / 25.4),
        'correct sphere in inches')
    return out


# ---------------------------------------------------------------------------
# End-to-end composites: several defects per mesh, so the real script runs a
# few times instead of once per defect (each run costs ~90 s).
# ---------------------------------------------------------------------------

def sphere_sag(v, centre, r) -> float:
    """How far this faceted sphere's faces dip below the smooth sphere:
    r − the smallest face-plane distance from the centre."""
    v = np.asarray(v, np.float64) - np.asarray(centre, np.float64)
    f = sphere()[1]
    a, b, c = (v[f[:, i]] for i in range(3))
    nrm = np.cross(b - a, c - a)
    nrm /= np.linalg.norm(nrm, axis=1)[:, None]
    return float(r - np.abs(np.einsum('ij,ij->i', nrm, a)).min())


def _shift(mesh, d):
    v, f = mesh
    return (np.asarray(v, np.float64) + np.asarray(d, np.float64)).astype(np.float32), np.asarray(f, np.int64)


def _box_shift(bx, d):
    lo, hi = bx
    return (np.asarray(lo, float) + d, np.asarray(hi, float) + d)


def composites() -> dict:
    """Four meshes combining defects for the end-to-end tests. Mesh, truth
    parts, vanish boxes and feature points are transformed together.

    defects carries: 'sag' — per truth part, how far its facets dip below
    the smooth shape (the tolerance basis, see docs/refactor/tests.md);
    'parts' — the part ids the batch log should show; 'rod_tip' where
    relevant. Acceptance limits built from 'sag' are engineering limits
    chosen before running, not proven error bounds.
    """
    v, f = sphere()
    s10 = sphere_sag(v, (0, 0, 0), 10.0)
    out = {}

    def add(name, mesh, truth, defects, notes, vanish=()):
        mv, mf = mesh
        out[name] = Fixture(name, np.asarray(mv, np.float32), np.asarray(mf, np.int64), tuple(truth),
                            union_volume(list(truth)), tuple(vanish), defects, notes)

    # 1. Everything at once on one sphere (doubles -> two coincident shells).
    ab = fixtures()['allbad']
    add('c_allbad', (ab.verts, ab.faces), ab.truth,
        dict(open=True, non_manifold=True, shells=2, sag=(s10,), parts=('1/2', '2/2')),
        ab.notes, ab.vanish_boxes)

    # 2. One sphere with a reversed cap AND a hole; debris far away.
    seamed = build_seam(v, f)[1]
    cap = v[f].mean(axis=1)[:, 2] > 5.0
    holed = np.delete(seamed, np.flatnonzero(~cap)[180:192], axis=0)
    sheet = (np.array([[30, -3, -3], [30, 3, -3], [30, 3, 3], [30, -3, 3]], np.float32), np.array([[0, 1, 2], [0, 2, 3]]))
    speck = box((30, 6, 0), (30.5, 6.5, 0.5), n=1)
    add('c_hole_seam_debris', _with(v, holed, sheet, speck), ((v, f),),
        dict(open=True, seams=True, shells=3, sag=(s10,), parts=('1/1',)),
        'reversed cap + 12 faces removed; open sheet and closed speck at x = 30',
        vanish=((np.array([29, -4, -4]), np.array([31, 4, 4])),
                (np.array([29.5, 5.5, -0.5]), np.array([31, 7, 1]))))

    # 3. Big sphere, small inside-out sphere, and a sphere with an attached rod.
    si = build_shell_inverted(v, f)
    small = (si[0][len(v):] - np.float32([15, 0, 0]), f)          # r 5 centred at x = 25
    small_mesh = (small[0], f[:, ::-1])
    rod = _shift(sphere_with_rod(), (-25, 0, 0))
    s5 = sphere_sag(small[0], (25, 0, 0), 5.0)
    add('c_multishell_rod', _with(v, f, small_mesh, rod), ((v, f), small, rod),
        dict(open=False, shells=3, sag=(s10, s5, s10), parts=('1/3', '2/3', '3/3'),
             rod_tip=(-25.0, 0.0, 18.0), rod_radius=0.75),
        'r10 sphere; r5 inside-out at x = 25; sphere + rod (tip z 18) at x = -25')

    # 4. Overlapping shells in inches.
    ov = fixtures()['overlapping_shells']
    k = np.float32(1 / 25.4)
    add('c_inch_overlap', ((ov.verts * k).astype(np.float32), ov.faces),
        tuple(((pv * k).astype(np.float32), pf) for pv, pf in ov.truth),
        dict(open=False, shells=2, sag=(s10 / 25.4, s10 / 25.4), parts=('1/2', '2/2')),
        'overlapping_shells scaled to inches')
    return out
