#!/usr/bin/env python3
"""Experiment: per-shell report for the volume-loss investigation. See
docs/errors/volume-loss-rejected.md.

    PYTHONPATH=. tools/project_python.sh tools/experiments/shell_report.py FILE [FILE ...]

For each shell (largest 12 listed): faces, signed and absolute
divergence volume, open/NM edges, bounding-box size, gap to the largest
shell's bbox. Then, for shells of >= 100 faces: the divergence volume with
the model shifted (+100 mm x, -100 mm z), which is stable only for closed
shells, and how closely each shell's open-boundary vertices meet the other
shells' boundaries (seams that the exact weld didn't join).

Both are indicators only. Boundary proximity doesn't show matching seam
curves or compatible orientation (close facing walls also match), and a
volume unchanged by two shifts doesn't prove a shell closed.

Read-only. Not wired into the pipeline.
"""

import sys

import numpy as np
from scipy.spatial import cKDTree

from libs import mesh_io, scanner
from libs.mesh_io import Geometry


def main():
    for path in sys.argv[1:]:
        m = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
        V, F = m.geometry.verts.astype(np.float64), m.geometry.faces

        def vol(Vs, s):
            t = Vs[F[s]]
            return np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6

        sh = scanner.shells(m)
        p0 = V[np.unique(F[sh[0]])]
        lo0, hi0 = p0.min(0), p0.max(0)
        print(f"== {path.split('/')[-1]}: faces={len(F)} shells={len(sh)} "
              f"diag={np.linalg.norm(V.max(0) - V.min(0)):.1f} "
              f"component_volume={scanner.component_volume(m):.1f}")
        print(f"{'#':>3} {'faces':>7} {'signed vol':>11} {'|vol|':>9} {'open':>5} {'nm':>4} "
              f"{'bbox size':>22} {'gap to #1':>9}")
        for i, s in enumerate(sh[:12]):
            sc = scanner.scan(m.with_geometry(Geometry(m.geometry.verts, F[s])))
            pts = V[np.unique(F[s])]
            lo, hi = pts.min(0), pts.max(0)
            gap = np.linalg.norm(np.maximum(0, np.maximum(lo0 - hi, lo - hi0)))
            print(f"{i + 1:>3} {len(s):>7} {vol(V, s):>11.1f} {abs(vol(V, s)):>9.1f} "
                  f"{sc.open_edges:>5} {sc.non_manifold:>4} {str(np.round(hi - lo, 1)):>22} {gap:>9.2f}")
        if len(sh) > 12:
            rest = sh[12:]
            print(f"... {len(rest)} more shells, {sum(len(s) for s in rest)} faces, "
                  f"|vol| {sum(abs(vol(V, s)) for s in rest):.1f}")

        big = [s for s in sh if len(s) >= 100]
        print("origin dependence (closed shells don't move):")
        for i, s in enumerate(big):
            print(f"  shell {i + 1}: {vol(V, s):9.1f} | +100x {vol(V + [100, 0, 0], s):9.1f} "
                  f"| -100z {vol(V + [0, 0, -100], s):9.1f}")
        bverts = []
        for s in big:
            f = F[s]
            e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), 1)
            u, c = np.unique(e, axis=0, return_counts=True)
            bverts.append(np.unique(u[c == 1]))
        print("boundary coincidence (nearest boundary vertex of the other shell):")
        for i in range(len(big)):
            for j in range(len(big)):
                if i == j or not len(bverts[i]) or not len(bverts[j]):
                    continue
                d, _ = cKDTree(V[bverts[j]]).query(V[bverts[i]])
                print(f"  {i + 1} -> {j + 1}: median {np.median(d):.4f}  "
                      f"<0.01mm {np.mean(d < 0.01) * 100:5.1f}%  <0.1mm {np.mean(d < 0.1) * 100:5.1f}%")


if __name__ == '__main__':
    main()
