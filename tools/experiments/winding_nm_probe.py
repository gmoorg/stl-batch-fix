"""Winding NM probe: rebuild one part as the batch does, capture marching
cubes output before `_weld`, skip `_check`, and locate the NM edges.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/winding_nm_probe.py FILE [PART]

PART is the 1-based shell index (default 1). For a model that was decimated
in the batch, pass its `stl-decimated/...900000.stl` cache, the mesh the
repair pass actually loaded.

Prints NM/open counts for the raw marching-cubes output (exact merge only)
and for `_weld`'s output, then each NM edge with its grid position. See
docs/errors/winding-non-manifold.md. "raw verts merged" is approximate: it
re-keys float32 output coordinates, which can land in a neighbouring bin.
NM/open counts are edge incidence only: bow-tie vertices, orientation and
self-intersections aren't checked.

Reproduces the coordinate-rounding weld, replaced on 2026-10-05 by the
grid-edge weld: run it from commit 5971d69 (`git worktree add`), where
`_weld(Vo, Fo, h)` still has that signature. For the current weld use
`winding_edge_weld_check.py`.

Read-only. Uses the batch's grid spacing (whole-model diagonal) and the
10 GB plan. Not wired into the pipeline.
"""
import sys
import time

import numpy as np

from libs import mesh_io, scanner, splitter, winding
from libs.mesh_io import Geometry, Mesh

path = sys.argv[1]
part_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
diag = scanner.diagonal(mesh)
parts = splitter.by_shells(mesh, min_faces=100)
part = parts[part_no - 1]
h = winding.grid_spacing(diag)
plan = winding.plan(part, h, 10 * 10**9)
print(f"parts={len(parts)} part {part_no}: faces={len(part.geometry.faces)} "
      f"h={h:g} blocks={plan.blocks_per_axis}", flush=True)

captured = {}
real_weld = winding._weld


def capture_weld(Vo, Fo, h_):
    captured['V'], captured['F'] = Vo.copy(), Fo.copy()
    return real_weld(Vo, Fo, h_)


winding._weld = capture_weld
winding._check = lambda m: None
t = time.monotonic()
out = winding.reconstruct(part, h, plan.blocks_per_axis)
print(f"reconstruct {time.monotonic() - t:.1f}s", flush=True)


def nm_edges(F):
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    u, c = np.unique(e, axis=0, return_counts=True)
    return u[c > 2], u[c == 1]


# Raw MC output, merged on exact coordinate duplicates only.
V, F = captured['V'], captured['F']
_, first, inv = np.unique(V, axis=0, return_index=True, return_inverse=True)
Fx = inv.ravel()[F]
deg = (Fx[:, 0] == Fx[:, 1]) | (Fx[:, 1] == Fx[:, 2]) | (Fx[:, 0] == Fx[:, 2])
nm_x, open_x = nm_edges(Fx[~deg])
print(f"raw MC: verts={len(V)} faces={len(F)} | exact-merge: verts={len(first)} "
      f"degenerate={int(deg.sum())} nm={len(nm_x)} open={len(open_x)}", flush=True)

# Welded output (what the pipeline checks).
G = out.geometry
s = scanner.scan(out)
nm_w, open_w = nm_edges(G.faces)
print(f"welded: verts={len(G.verts)} faces={len(G.faces)} nm={s.non_manifold} "
      f"open={s.open_edges} degenerate={s.degenerate}", flush=True)

# How many raw vertices merged into each welded vertex (as _weld keys them).
key = np.rint(V / (h * 1e-4)).astype(np.int64)
_, winv, wcount = np.unique(key, axis=0, return_inverse=True, return_counts=True)
for a, b in nm_w[:10]:
    fa = np.flatnonzero((G.faces == a).any(1) & (G.faces == b).any(1))
    pa, pb = G.verts[a], G.verts[b]
    # raw vertices mapping to these welded vertices
    ka = np.rint(pa.astype(np.float64) / (h * 1e-4)).astype(np.int64)
    kb = np.rint(pb.astype(np.float64) / (h * 1e-4)).astype(np.int64)
    ra = int((key == ka).all(1).sum())
    rb = int((key == kb).all(1).sum())
    grid_a = (pa - plan.lo) / h
    grid_b = (pb - plan.lo) / h
    print(f"NM edge {a}-{b}: faces={len(fa)} len={np.linalg.norm(pa - pb) / h:.4f}h "
          f"raw verts merged a={ra} b={rb} "
          f"grid a={np.round(grid_a, 3)} b={np.round(grid_b, 3)}", flush=True)
