#!/usr/bin/env python3
"""Experiment: does post-reconstruction decimation throw vertices off the
surface? See docs/errors/post-wrap-meshfix-timeout.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/decimation_sliver_probe.py FILE [--save OUT.npz]

Rebuilds shell 1 of FILE with winding (batch grid spacing, 10 GB plan) and
reports its sliver content (edges < 0.001·h, faces < 1e-6·h²). Then it
decimates to the part's own face count (the pipeline's target) twice, with PyMeshLab defaults (the batch) and with
`optimalplacement=False` (diagnostic only, not a pipeline setting), and
reports for each: NM edges, duplicate-face groups, faces with an edge > 10·h,
vertices more than 1·h from the winding surface (point-to-triangle), and
the furthest one; the nearest-vertex distance is printed beside it for
comparison with the first measurement.
`--save` stores the default-decimated arrays (verts, faces, h) for a
separate MeshFix run. For a model decimated in the batch, pass its
`stl-decimated/...900000.stl` cache.

Read-only on inputs. Not wired into the pipeline.
"""

import sys

import igl
import numpy as np
from scipy.spatial import cKDTree

from libs import decimator, meshlab, mesh_io, scanner, splitter, winding


def edge_stats(V, F):
    t = V[F]
    el = np.stack([np.linalg.norm(t[:, (k + 1) % 3] - t[:, k], axis=1) for k in range(3)], 1)
    area = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1) / 2
    return el, area


def main():
    path = sys.argv[1]
    mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
    part = splitter.by_shells(mesh, min_faces=100)[0]
    h = winding.grid_spacing(scanner.diagonal(mesh))
    rec = winding.reconstruct(part, h, winding.plan(part, h, 10 * 10**9).blocks_per_axis)
    RV, RF = rec.geometry.verts.astype(np.float64), rec.geometry.faces
    el, area = edge_stats(RV, RF)
    # el holds each face's three edges, so interior edges appear twice; count
    # distinct short edges by their sorted vertex pair.
    E = np.stack([RF, np.roll(RF, -1, axis=1)], 2).reshape(-1, 2)
    short = np.unique(np.sort(E[(el < 1e-3 * h).ravel()], axis=1), axis=0)
    print(f"winding: faces={len(RF)} h={h:g} distinct edges<0.001h: {len(short)} "
          f"faces with area<1e-6 h^2: {int((area < 1e-6 * h * h).sum())} "
          f"max edge {el.max() / h:.2f}h", flush=True)
    tree = cKDTree(RV)

    def report(tag, G):
        V, F = G.verts.astype(np.float64), G.faces
        el, _ = edge_stats(V, F)
        long_faces = el.max(1) > 10 * h
        dv, _ = tree.query(V)                  # nearest winding vertex (upper bound)
        d = np.sqrt(igl.point_mesh_squared_distance(V, RV, RF.astype(np.int64))[0])
        s = scanner.scan(rec.with_geometry(G))
        _, fc = np.unique(np.sort(F, axis=1), axis=0, return_counts=True)
        print(f"{tag}: faces={len(F)} nm={s.non_manifold} dup-face groups={int((fc > 1).sum())} "
              f"faces with edge>10h={int(long_faces.sum())} max edge={el.max() / h:.1f}h | "
              f"verts >1h off surface={int((d > h).sum())} max off={d.max() / h:.1f}h "
              f"(nearest-vertex: {int((dv > h).sum())}, {dv.max() / h:.1f}h)",
              flush=True)

    target = len(part.geometry.faces)
    d = decimator.decimate(rec, target)
    print(f"target={target} rung={d.rung.value}", flush=True)
    report('defaults (batch)        ', d.mesh.geometry)
    if '--save' in sys.argv:
        G = d.mesh.geometry
        np.savez(sys.argv[sys.argv.index('--save') + 1], verts=G.verts, faces=G.faces, h=h)
    G = meshlab.apply_filters(rec, (('meshing_decimation_quadric_edge_collapse',
                                     {'targetfacenum': target, 'optimalplacement': False}),)).geometry
    report('optimalplacement=False  ', G)


if __name__ == '__main__':
    main()
