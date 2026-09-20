#!/usr/bin/env python3
"""Generate the synthetic test meshes used by tests/tests/test_pipeline.py.

    ../.venv/bin/python tests/tests/make_fixtures.py

Writes legacy pipeline inputs to `tests/fixtures/` and refactor regression
models to `tests/probes/`. Both sets are small, committed, and independent of
the STL collection, which is meant to be deletable.

Each fixture reproduces the *property* that its real counterpart's bug depended
on, not the model itself:

    body     many separate shells, over the scan limit  (head deletion)
    arms     several shells, under the scan limit        (normal split path)
    leg      one real shell plus far-away stray specks   (benign bbox change)
    foot1    one shell with a real boundary hole         (open-edge repair)
    foot2    one clean closed shell                      (passthrough)
    falcon   heavy non-manifold edges                    (blender fallback)
    seam     two regions wound against each other        (PyMeshFix deletion)
    inverted a closed sphere with EVERY face reversed     (no check but volume)
    small_valid_shell          meaningful shell below the debris face floor
    opposite_volume_shells     signed volumes cancel while one shell is small
    decimation_lost_appendage  thin feature shortened by face reduction
    reversed_tjunction_chain   open-edge path runs backward along its span

Verify with `--check`, which reports each fixture's measured properties so a
generator change that stops reproducing a defect is visible immediately.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(TEST_ROOT)
sys.path.insert(0, PROJECT_ROOT)
import stl_batch_fix as fix

FIXTURE_DIR = os.path.join(TEST_ROOT, 'fixtures')
PROBE_DIR = os.path.join(TEST_ROOT, 'probes')


# ── primitives ───────────────────────────────────────────────────────────────

def sphere(cx, cy, cz, r, seg=16):
    """A closed UV sphere: watertight, manifold, no boundary, no degenerates.

    The poles get ONE vertex each, not `seg` coincident ones.  Emitting a full
    ring at each pole is the obvious way to write this and it is wrong: the
    ring collapses to a point, every triangle in the top and bottom bands has
    zero area, and the mesh scans as non-manifold.  A first attempt at these
    fixtures did exactly that and produced a "clean" sphere with nm=29."""
    verts = [(cx, cy, cz + r)]                       # north pole
    for i in range(1, seg):                          # interior latitude rings
        lat = np.pi * i / seg
        for j in range(seg):
            lon = 2 * np.pi * j / seg
            verts.append((cx + r * np.sin(lat) * np.cos(lon),
                          cy + r * np.sin(lat) * np.sin(lon),
                          cz + r * np.cos(lat)))
    verts.append((cx, cy, cz - r))                   # south pole
    south = len(verts) - 1

    def ring(i, j):                                  # index into ring i (1-based)
        return 1 + (i - 1) * seg + (j % seg)

    faces = []
    for j in range(seg):                             # north cap
        faces.append((0, ring(1, j), ring(1, j + 1)))
    for i in range(1, seg - 1):                      # quad bands
        for j in range(seg):
            a, b = ring(i, j), ring(i, j + 1)
            c, d = ring(i + 1, j), ring(i + 1, j + 1)
            faces.append((a, c, b))
            faces.append((b, c, d))
    for j in range(seg):                             # south cap
        faces.append((south, ring(seg - 1, j + 1), ring(seg - 1, j)))
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


def tube_open_ends(cx, cy, cz, r, h, seg=24):
    """A cylinder wall with NO end caps — two genuine boundary loops.

    This is foot1's defect: real open edges that a repair will try to close,
    and closing them by pulling the boundary shut is what destroyed the caps."""
    verts, faces = [], []
    for k, z in enumerate((cz, cz + h)):
        for j in range(seg):
            a = 2 * np.pi * j / seg
            verts.append((cx + r * np.cos(a), cy + r * np.sin(a), z))
    for j in range(seg):
        a = j
        b = (j + 1) % seg
        c = seg + j
        d = seg + (j + 1) % seg
        faces.append((a, c, b))
        faces.append((b, c, d))
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


def combine(*parts):
    """Concatenate meshes, offsetting face indices — shells stay separate."""
    verts, faces, base = [], [], 0
    for v, f in parts:
        verts.append(v)
        faces.append(f + base)
        base += len(v)
    return np.vstack(verts), np.vstack(faces)


def tetrahedron(origin, edge, inverted=False):
    """Closed four-face shell; useful because it falls below the debris floor."""
    x, y, z = origin
    verts = np.array([
        (x, y, z), (x + edge, y, z),
        (x, y + edge, z), (x, y, z + edge),
    ], dtype=np.float32)
    faces = np.array([(0, 2, 1), (0, 1, 3),
                      (0, 3, 2), (1, 2, 3)], dtype=np.int64)
    if inverted:
        faces = faces[:, ::-1].copy()
    return verts, faces


def _quads(*rings):
    """Triangulate ordered four-corner surface patches."""
    faces = []
    for a, b, c, d in rings:
        faces.extend(((a, b, c), (a, c, d)))
    return np.asarray(faces, dtype=np.int64)


def subdivide(verts, faces, times=1):
    """Split every triangle into four.  Used only to push a fixture over the
    2M-triangle scan limit, which is the whole point of the body fixture."""
    for _ in range(times):
        v = list(map(tuple, verts))
        index = {p: i for i, p in enumerate(v)}
        new_faces = []

        def mid(i, j):
            p = tuple((verts[i] + verts[j]) / 2.0)
            if p not in index:
                index[p] = len(v)
                v.append(p)
            return index[p]

        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            new_faces += [(a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)]
        verts = np.array(v, dtype=np.float32)
        faces = np.array(new_faces, dtype=np.int64)
    return verts, faces


def non_manifold_fan(cx, cy, cz, n_edges=400):
    """Edges shared by three faces — the defect falcon has 147,448 of.

    Three triangles hinged on one shared edge is the textbook non-manifold
    edge: no orientation makes it a surface boundary."""
    verts, faces = [], []
    for k in range(n_edges):
        o = k * 3.0
        base = len(verts)
        verts += [(cx + o, cy, cz), (cx + o, cy + 1, cz),          # shared edge
                  (cx + o + 1, cy, cz), (cx + o - 1, cy, cz),
                  (cx + o, cy, cz + 1)]
        faces += [(base, base + 1, base + 2),
                  (base, base + 1, base + 3),
                  (base, base + 1, base + 4)]     # third face on the same edge
    return np.array(verts, dtype=np.float32), np.array(faces, dtype=np.int64)


# ── fixtures ─────────────────────────────────────────────────────────────────

def build_body():
    """Many real shells, one much larger than the rest.

    The head-deletion bug needed two things: the mesh over the scan limit (so
    the split was skipped) and several genuine shells (so there was something
    to delete).  The test supplies the first by lowering
    _LARGE_MESH_TRI_LIMIT and MAX_FACES rather than by shipping a 2M-triangle
    file — the code path is what matters, not the triangle count.  Building
    the real size took 119 MB, larger than the model it stood in for.

    The size spread matters: pymeshfix kept the largest shell and dropped the
    others, so a fixture of equal-sized blobs would not show the same loss."""
    parts = [sphere(0, 0, 0, 20, seg=24)]           # the "body"
    for i in range(8):                              # eight smaller shells
        parts.append(sphere(45 + i * 14, 0, 0, 5, seg=12))
    return combine(*parts)


def build_arms():
    """Several real shells, deliberately SMALLER than body.

    body and arms are a matched pair either side of the scan limit: the test
    sets that limit between them, so arms takes the normal step B split and
    body takes the deferred step B2.  Keep arms the smaller of the two."""
    return combine(sphere(0, 0, 0, 10, seg=12),
                   sphere(30, 0, 0, 8, seg=12),
                   sphere(-30, 0, 0, 8, seg=12))


def build_leg():
    """One real shell plus stray specks far away.

    The specks stretch the bounding box; removing them shrinks it a long way
    while the model is untouched.  That is leg's 89mm 'change' being correct."""
    parts = [sphere(0, 0, 0, 10, seg=20)]
    # Small enough that split_shells() drops them as debris (under its
    # max(100, largest/1000) face floor), far enough out that removing them
    # moves the bounding box a long way — which is the point: a large bbox
    # change that is CORRECT.
    for i in range(6):
        parts.append(sphere(200 + i * 30, 150, -120, 0.4, seg=4))
    return combine(*parts)


def build_foot1():
    """A closed body plus an open-ended tube: real boundary holes, nm=0.

    The tube is `seg=64` so it lands above _MIN_SHELL_FACES and is split out as
    its own shell, which is what the real Chair_foot1.stl does — its second
    shell is 384 faces.  An earlier version used seg=24, giving 48 faces, which
    fell under the floor and was discarded as debris; the test then pinned
    damage the real model no longer suffers.  The debris floor is what saved
    that file: the old max(100, largest // 1000) rule put the cutoff at 680
    faces on a 681k mesh and threw the cap away."""
    return combine(sphere(0, 0, 0, 10, seg=24),
                   tube_open_ends(0, 0, 20, 4, 6, seg=64))


def build_foot2():
    """Clean, closed, defect-free: must pass through untouched."""
    return sphere(0, 0, 0, 10, seg=28)


def build_seam():
    """Two surfaces joined along a closed loop, wound against each other.

    This is the hair-over-scalp case: one connected component containing two
    regions that disagree about which way is out.  PyMeshFix handed the joined
    mesh keeps one region and deletes the other — on the real model, 562,288
    faces in, 394,432 out, and the figure lost its head.

    Built by welding a smaller sphere onto a larger one at a shared ring of
    vertices and reversing the smaller one's winding.  The shared ring is the
    closed seam loop that find_winding_seams() looks for."""
    verts, faces = sphere(0, 0, 0, 10, seg=20)
    # Reverse every face in the northern cap.  The mesh stays one connected
    # component — no vertex moves and no edge is removed — but the boundary
    # between the reversed cap and the rest becomes a closed ring of edges
    # whose two faces traverse them the same way.  That ring is the seam.
    centroid = verts[faces].mean(axis=1)
    cap = centroid[:, 2] > 5.0
    faces = faces.copy()
    faces[cap] = faces[cap][:, ::-1]
    return verts, faces


def build_inverted():
    """A closed sphere with every face reversed: inside-out, and nothing but
    signed volume can tell.

    The case `find_winding_seams` provably cannot see. A seam is two regions
    disagreeing *with each other*; this mesh is uniformly backwards, so no two
    neighbours disagree and the seam count is zero. Measured against the same
    sphere wound correctly — identical on every check the pipeline has:

        correct    760 faces  nm=0 open=0 deg=0 seams=0/0 shells=1  vol +4094.9
        inverted   760 faces  nm=0 open=0 deg=0 seams=0/0 shells=1  vol -4094.9

    PyMeshFix is a no-op on it: 760 faces in, 760 out, volume unchanged at
    -4094.9, `ok=True`. So this fixture exists to pin a defect that currently
    reaches the clean-copy shortcut and is written out as `ok` — the design
    doc's "renders black in viewers while every defect count reads zero".
    """
    verts, faces = sphere(0, 0, 0, 10, seg=20)
    return verts, faces[:, ::-1].copy()


def build_falcon():
    """Heavy non-manifold edges: pymeshfix's failure case."""
    return combine(sphere(0, 0, 0, 10, seg=20),
                   non_manifold_fan(40, 0, 0, n_edges=600))


def build_small_valid_shell():
    """A sound four-face part beside a large shell.

    The tetrahedron is deliberately below ``MIN_SHELL_FACES``. It represents
    a meaningful button, pin, or tooth, so a clean pipeline result must retain
    both components rather than silently classify the small one as debris.
    """
    return combine(sphere(0, 0, 0, 10, seg=20),
                   tetrahedron((15, 0, 0), 4))


def build_opposite_volume_shells():
    """A sphere plus a four-face shell with equal, opposite signed volume.

    The small-face shell is geometrically large. Dropping it while computing
    retention from the signed total makes the denominator approximately zero,
    which previously allowed a partial model to be reported as clean.
    """
    body_v, body_f = sphere(0, 0, 0, 10, seg=20)
    tri = body_v[body_f].astype(np.float64)
    body_volume = float(np.einsum(
        'ij,ij->i', tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
    edge = (6.0 * abs(body_volume)) ** (1.0 / 3.0)
    return combine((body_v, body_f),
                   tetrahedron((30, -edge / 2, -edge / 2), edge,
                               inverted=True))


def build_decimation_lost_appendage():
    """One watertight body with a thin 0.25-unit appendage.

    At a 20-face budget the installed fast decimator shortens the protrusion
    from 0.25 to about 0.10 units while returning a clean closed mesh. The
    pipeline must compare that result with this source, rather than beginning
    its preservation check after the loss occurred.
    """
    w = 0.25
    verts = np.asarray([
        (-10, -10, -10), (-10, 10, -10),
        (-10, 10, 10), (-10, -10, 10),
        (10, -10, -10), (10, 10, -10),
        (10, 10, 10), (10, -10, 10),
        (10, -w, -w), (10, w, -w), (10, w, w), (10, -w, w),
        (10.25, -w, -w), (10.25, w, -w),
        (10.25, w, w), (10.25, -w, w),
    ], dtype=np.float32)
    faces = _quads(
        # Five outer faces of the box.
        (0, 3, 2, 1), (0, 1, 5, 4), (3, 7, 6, 2),
        (0, 4, 7, 3), (1, 2, 6, 5),
        # The +X face is an annulus around the appendage opening.
        (4, 5, 9, 8), (5, 6, 10, 9),
        (6, 7, 11, 10), (7, 4, 8, 11),
        # Appendage sides and end cap; its start is the box opening.
        (8, 9, 13, 12), (11, 15, 14, 10),
        (8, 12, 15, 11), (9, 10, 14, 13), (12, 13, 14, 15),
    )
    return verts, faces


def build_reversed_tjunction_chain():
    """Two vertices whose open-edge path runs backward along a spanning edge.

    Topology orders the path ``a -> M(2/3) -> M(1/3) -> b``. Sorting those
    vertices by projected position before splitting creates faces that do not
    match the actual path, so the welder must reject or handle it explicitly.
    """
    verts, faces = sphere(0, 0, 0, 10, seg=20)
    verts, changed = verts.tolist(), faces.tolist()
    a, b, c = changed[200]
    along = np.asarray(verts[b]) - np.asarray(verts[a])
    verts.append((np.asarray(verts[a]) + 2.0 * along / 3.0).tolist())
    verts.append((np.asarray(verts[a]) + along / 3.0).tolist())
    first, second = len(verts) - 2, len(verts) - 1
    changed[200] = [a, first, c]
    changed.extend(([first, second, c], [second, b, c]))
    return (np.asarray(verts, dtype=np.float32),
            np.asarray(changed, dtype=np.int64))


PIPELINE_BUILDERS = {
    'body': build_body, 'arms': build_arms, 'leg': build_leg,
    'foot1': build_foot1, 'foot2': build_foot2, 'falcon': build_falcon,
    'seam': build_seam, 'inverted': build_inverted,
}


PROBE_BUILDERS = {
    'small_valid_shell': build_small_valid_shell,
    'opposite_volume_shells': build_opposite_volume_shells,
    'decimation_lost_appendage': build_decimation_lost_appendage,
    'reversed_tjunction_chain': build_reversed_tjunction_chain,
}


BUILDERS = {**PIPELINE_BUILDERS, **PROBE_BUILDERS}


REGRESSION_SHAPES = {
    # faces, non-manifold edges, open edges, edge-connected shells
    'small_valid_shell': (764, 0, 0, 2),
    'opposite_volume_shells': (764, 0, 0, 2),
    'decimation_lost_appendage': (28, 0, 0, 1),
    'reversed_tjunction_chain': (762, 0, 4, 1),
}


def generate():
    for directory, builders in ((FIXTURE_DIR, PIPELINE_BUILDERS),
                                (PROBE_DIR, PROBE_BUILDERS)):
        os.makedirs(directory, exist_ok=True)
        for name, build in builders.items():
            v, f = build()
            path = os.path.join(directory, f'{name}.stl')
            fix._write_binary_stl(path, v, f)
            print(f'  {name:28} {len(f):>9,} tris  '
                  f'{os.path.getsize(path)/1048576:6.2f} MB', flush=True)


def check():
    """Report what each fixture actually is, so a generator change that stops
    reproducing a defect shows up here rather than as a confusing test pass."""
    print(f'{"name":8} {"tris":>10} {"nm":>8} {"open":>6} {"shells":>7} '
          f'{"volume":>12}  note')
    targets = [(name, FIXTURE_DIR) for name in PIPELINE_BUILDERS]
    targets += [(name, PROBE_DIR) for name in PROBE_BUILDERS]
    for name, directory in targets:
        path = os.path.join(directory, f'{name}.stl')
        if not os.path.exists(path):
            print(f'{name:8}  MISSING — run without --check first')
            continue
        n, err = fix._read_stl_header(path)
        nm, op, _ = fix.scan_mesh_errors(path, n)
        v, f = fix._weld_binary_stl(path)
        p = np.arange(len(v), dtype=np.int64)

        def find(x):
            while p[x] != x:
                p[x] = p[p[x]]
                x = p[x]
            return x
        e = np.unique(np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]],
                                         f[:, [2, 0]]]), axis=1), axis=0)
        for a, b in e:
            ra, rb = find(a), find(b)
            if ra != rb:
                p[ra] = rb
        shells = len(np.unique([find(i) for i in range(len(v))]))
        over = ' OVER SCAN LIMIT' if n > fix._LARGE_MESH_TRI_LIMIT else ''
        # Signed volume, because it is the ONLY check that distinguishes an
        # inside-out mesh from a correct one — see build_inverted.
        tri = v[f].astype(np.float64)
        volume = float(np.einsum('ij,ij->i', tri[:, 0],
                                 np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
        observed = (n, nm, op, shells)
        if name in REGRESSION_SHAPES and observed != REGRESSION_SHAPES[name]:
            raise AssertionError(
                f'{name}: expected {REGRESSION_SHAPES[name]}, got {observed}')
        if name == 'opposite_volume_shells' and abs(volume) > 0.01:
            raise AssertionError(
                f'{name}: signed volumes no longer cancel ({volume:+.6f})')
        if name == 'decimation_lost_appendage':
            maximum_x = float(v[:, 0].max())
            if not np.isclose(maximum_x, 10.25):
                raise AssertionError(
                    f'{name}: appendage tip moved to x={maximum_x}')
        flag = ' INSIDE-OUT' if volume < 0 else ''
        print(f'{name:8} {n:>10,} {nm:>8} {op:>6} {shells:>7} '
              f'{volume:>+12,.1f}{over}{flag}')
        del v, f, tri


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
    else:
        print(f'writing {FIXTURE_DIR} and {PROBE_DIR}')
        generate()
        print()
        check()
