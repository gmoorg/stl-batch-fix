#!/usr/bin/env python3
"""Experiment: the grid-edge weld against the coordinate-rounding weld it
replaced, on one shell of a real model. See docs/errors/winding-non-manifold.md.

    PYTHONPATH=. tools/project_python.sh -u tools/experiments/winding_edge_weld_check.py FILE [PART]

PART is the 1-based shell index (default 1). Uses the batch's grid spacing
(whole-model diagonal) and the 10 GB plan; for a model decimated in the
batch, pass its `stl-decimated/...900000.stl` cache. Reconstructs once,
captures marching cubes' output before the weld, and reports faces,
NM/open/degenerate edges and component volume for both welds.

Read-only. Not wired into the pipeline.
"""

import sys
import time

import numpy as np

from libs import mesh_io, scanner, splitter, winding
from libs.mesh_io import Geometry


def rounding_weld(Vo, Fo, h):
    """The weld before 2026-10-05: coordinates rounded to h·1e-4."""
    key = np.rint(Vo / (h * 1e-4)).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    Vo, Fo = Vo[first], inv.ravel()[Fo]
    Fo = Fo[(Fo[:, 0] != Fo[:, 1]) & (Fo[:, 1] != Fo[:, 2]) & (Fo[:, 0] != Fo[:, 2])]
    return Geometry(Vo.astype(np.float64), Fo.astype(np.int64))


def report(tag, mesh):
    s = scanner.scan(mesh)
    print(f"  {tag:15} faces={s.faces} nm={s.non_manifold} open={s.open_edges} "
          f"degenerate={s.degenerate} volume={scanner.component_volume(mesh):.6f}", flush=True)


def main():
    path = sys.argv[1]
    part_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    mesh = mesh_io.load(mesh_io.probe(path, '/nonexistent/out.stl'))
    part = splitter.by_shells(mesh, min_faces=100)[part_no - 1]
    h = winding.grid_spacing(scanner.diagonal(mesh))
    plan = winding.plan(part, h, 10 * 10**9)
    captured = {}
    edge_weld = winding._weld

    def capture(Vo, Fo, edges):
        captured['args'] = (Vo, Fo)
        return edge_weld(Vo, Fo, edges)

    winding._weld = capture
    t = time.monotonic()
    out = winding.reconstruct(part, h, plan.blocks_per_axis)
    print(f"{path} part {part_no}: h={h:g} blocks={plan.blocks_per_axis} "
          f"{time.monotonic() - t:.0f}s", flush=True)
    report('rounding weld', out.with_geometry(rounding_weld(*captured['args'], h)))
    report('edge weld', out)


if __name__ == '__main__':
    main()
