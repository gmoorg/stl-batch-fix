#!/usr/bin/env python3
"""Experiment: are winding's sliver clusters what makes post-wrap decimation
throw vertices off the surface, and do PyMeshLab's own options stop it? See
docs/errors/post-wrap-meshfix-timeout.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/sliver_cause_probe.py FILE

Rebuilds shell 1 of FILE with winding (batch grid spacing, 10 GB plan), then
decimates that same output to the pipeline's target (the part's own face
count) in several ways:

    defaults           PyMeshLab defaults (the batch)
    slivers collapsed  defaults, after merging the endpoints of every edge
                       shorter than 0.001·h (each connected group of short
                       edges becomes one vertex at its mean); the causal test
    planarquadric      defaults + planarquadric=True
    preservetopology   defaults + preservetopology=True

For each: faces, NM edges, duplicate-face groups, vertices more than 1·h
from the winding surface (point-to-triangle) and the furthest one, time.

Read-only. Not wired into the pipeline.
"""

import sys
import time

import igl
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from libs import meshlab, mesh_io, scanner, splitter, winding
from libs.mesh_io import Geometry

DECIMATE = 'meshing_decimation_quadric_edge_collapse'


def collapse_short_edges(G, limit):
    """Merge the endpoints of edges shorter than `limit`, group by group."""
    V, F = G.verts.astype(np.float64), G.faces
    E = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), 1), axis=0)
    short = E[np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1) < limit]
    n = len(V)
    graph = coo_matrix((np.ones(len(short)), (short[:, 0], short[:, 1])), shape=(n, n))
    count, label = connected_components(graph, directed=False)
    sums = np.zeros((count, 3))
    np.add.at(sums, label, V)
    Vn = sums / np.bincount(label, minlength=count)[:, None]
    Fn = label[F]
    Fn = Fn[(Fn[:, 0] != Fn[:, 1]) & (Fn[:, 1] != Fn[:, 2]) & (Fn[:, 0] != Fn[:, 2])]
    return Geometry(Vn.astype(np.float32), Fn.astype(np.int64)), len(short), n - count


def main():
    path = sys.argv[1]
    mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
    part = splitter.by_shells(mesh, min_faces=100)[0]
    h = winding.grid_spacing(scanner.diagonal(mesh))
    t = time.monotonic()
    rec = winding.reconstruct(part, h, winding.plan(part, h, 10 * 10**9).blocks_per_axis)
    RV, RF = rec.geometry.verts.astype(np.float64), rec.geometry.faces.astype(np.int64)
    target = len(part.geometry.faces)
    print(f"{path}: h={h:g} winding faces={len(RF)} target={target} "
          f"{time.monotonic() - t:.0f}s", flush=True)

    def report(tag, G, seconds):
        s = scanner.scan(rec.with_geometry(G))
        _, fc = np.unique(np.sort(G.faces, axis=1), axis=0, return_counts=True)
        d = np.sqrt(igl.point_mesh_squared_distance(G.verts.astype(np.float64), RV, RF)[0])
        print(f"  {tag:18} faces={s.faces} nm={s.non_manifold} open={s.open_edges} "
              f"dup-groups={int((fc > 1).sum())} off>1h={int((d > h).sum())} "
              f"max off={d.max() / h:.1f}h  {seconds:.1f}s", flush=True)

    def decimate(source, extra):
        t = time.monotonic()
        G = meshlab.apply_filters(source, ((DECIMATE, dict(targetfacenum=target, **extra)),)).geometry
        return G, time.monotonic() - t

    report('defaults', *decimate(rec, {}))
    collapsed, n_short, merged = collapse_short_edges(rec.geometry, 1e-3 * h)
    s = scanner.scan(rec.with_geometry(collapsed))
    print(f"  (collapsed {n_short} short edges, {merged} vertices merged: "
          f"nm={s.non_manifold} open={s.open_edges})", flush=True)
    report('slivers collapsed', *decimate(rec.with_geometry(collapsed), {}))
    report('planarquadric', *decimate(rec, {'planarquadric': True}))
    report('preservetopology', *decimate(rec, {'preservetopology': True}))


if __name__ == '__main__':
    main()
