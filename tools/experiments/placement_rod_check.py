#!/usr/bin/env python3
"""Experiment: thin-feature accuracy of PyMeshLab quadric decimation with
`optimalplacement` on (default) and off. See docs/errors/post-wrap-meshfix-timeout.md
and the method in docs/refactor/reconstruction.md ("Decimation after reconstruction").

    PYTHONPATH=. tools/project_python.sh tools/experiments/placement_rod_check.py

Rod: `defect_spheres.sphere_with_rod` rebuilt by winding at h = 0.092,
decimated back to its 840 input faces; reports rod-tip error, p99/max distance
rebuilt->decimated, and the furthest decimated vertex from the rebuilt surface.
Distances are point-to-triangle, sampled at vertices only: p99/max are
sample statistics, not a whole-surface bound, and self-intersections or lost
components aren't checked.
Icosphere (20,480 -> 1,000): vertex radius deviation and extent ratio. With
placement off, every kept vertex is an original on the sphere, so this check
is uninformative there.

Not wired into the pipeline.
"""

import igl
import numpy as np

from libs import meshlab, scanner, winding
from libs.mesh_io import Geometry, Kind, Mesh
from tests.tests import defect_spheres as ds
from tests.tests.test_decimator import _sphere


def mk(v, f):
    return Mesh('/r', '/r', Kind.BINARY_STL, len(f), True, None, Geometry(v, f))


def dist(P, G):
    return np.sqrt(igl.point_mesh_squared_distance(
        P.astype(np.float64), G.verts.astype(np.float64), G.faces.astype(np.int64))[0])


def dec(m, target, opt):
    return meshlab.apply_filters(m, (('meshing_decimation_quadric_edge_collapse',
                                      {'targetfacenum': target, 'optimalplacement': opt}),)).geometry


def main():
    v, f = ds.sphere_with_rod()
    rebuilt = winding.reconstruct(mk(v, f), 0.092, 1)
    print(f"rod part: input {len(f)} faces, rebuilt {len(rebuilt.geometry.faces)}")
    tip = np.array([[0.0, 0.0, 18.0]])
    for opt in (True, False):
        G = dec(rebuilt, len(f), opt)
        d_in = dist(rebuilt.geometry.verts, G)
        d_out = dist(G.verts, rebuilt.geometry)
        s = scanner.scan(mk(G.verts, G.faces))
        print(f"  optimalplacement={opt!s:5}: faces={len(G.faces)} tip={dist(tip, G)[0]:.3f} "
              f"p99={np.percentile(d_in, 99):.3f} max={d_in.max():.3f} "
              f"verts-off max={d_out.max():.3f} nm={s.non_manifold} open={s.open_edges}")

    sv, sf = _sphere(subdivisions=5)
    R = float(np.sqrt(1 + ((1 + 5 ** 0.5) / 2) ** 2))
    print(f"icosphere: {len(sf)} faces -> 1000")
    for opt in (True, False):
        G = dec(mk(sv, sf), 1000, opt)
        vv = G.verts.astype(np.float64)
        r = np.linalg.norm(vv, axis=1)
        ext = np.ptp(vv, axis=0)
        print(f"  optimalplacement={opt!s:5}: faces={len(G.faces)} "
              f"radius dev max={np.abs(r - R).max() / R * 100:.3f}% extent ratio={ext.max() / ext.min():.4f}")


if __name__ == '__main__':
    main()
