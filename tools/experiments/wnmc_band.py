#!/usr/bin/env python3
"""Experiment: rebuild a mesh as a solid with winding-number signed distance +
marching cubes on one global grid, processed in blocks. A candidate
replacement for alpha-wrap; see docs/refactor/reconstruction.md.

    tools/project_python.sh tools/experiments/wnmc_band.py IN.stl OUT.stl H BLOCKS [--check]

H is the grid spacing in mm; BLOCKS is the block count per axis (BLOCKS^3
blocks in total). Prints one JSON line: reconstruction time and peak memory,
per-phase seconds, output faces, and with --check also topology, volume and
the original-to-new surface distance (the check is not part of the cost).

Not wired into the pipeline. Block size is deliberately not fixed yet.
"""

import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))

import igl                                                                # noqa: E402
import numpy as np                                                        # noqa: E402
from scipy import ndimage                                                 # noqa: E402

from libs import mesh_io, scanner                                         # noqa: E402
from libs.mesh_io import Geometry, Kind, Mesh                             # noqa: E402

#: Half-width of the exact-distance band, in grid cells. Marching cubes only
#: reads cells the surface crosses, whose corners lie within one cell
#: diagonal (< 2 cells) of it; 5 leaves room for the coarse-sign rule below.
BAND = 5
#: Far-point signs come from the winding number on every K-th grid point.
#: A far point is > BAND cells from the surface and its nearest lattice point
#: is <= K/2 * sqrt(3) ~ 3.5 cells away, so both lie on the same side.
K = 4


class Phases:
    """Accumulated seconds per named phase: `with phases('name'): ...`."""

    def __init__(self):
        self.seconds = {}

    def __call__(self, name):
        phases = self

        class _Timer:
            def __enter__(self):
                self.start = time.monotonic()

            def __exit__(self, *exc):
                phases.seconds[name] = phases.seconds.get(name, 0.0) + time.monotonic() - self.start
        return _Timer()


def surface_sample_cells(V, F, lo, h, rng):
    """Nearest grid index of every vertex and of area-proportional random
    surface points (>= 4 per h^2), so no surface patch is farther than about
    h/2 from a marked cell."""
    a, b, c = (V[F[:, i]] for i in range(3))
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2
    tri = np.repeat(np.arange(len(F)), np.ceil(area * 4 / h ** 2).astype(np.int64) + 1)
    u, w = rng.random((2, len(tri)))
    flip = u + w > 1
    u[flip], w[flip] = 1 - u[flip], 1 - w[flip]
    points = np.vstack([V, a[tri] + u[:, None] * (b - a)[tri] + w[:, None] * (c - a)[tri]])
    return np.round((points - lo) / h).astype(np.int64)


def block_field(r0, r1, cells, V, F, lo, h, phases, stats):
    """Signed-distance-like field on grid indices r0..r1 (inclusive): exact
    in the band, +-(BAND+1)*h with the winding-number sign elsewhere."""
    shape = tuple(r1 - r0 + 1)
    with phases('band_mask'):
        near = np.all((cells >= r0 - BAND) & (cells <= r1 + BAND), axis=1)
        band = np.zeros(tuple(s + 2 * BAND for s in shape), bool)
        local = cells[near] - (r0 - BAND)
        band[local[:, 0], local[:, 1], local[:, 2]] = True
        band = ndimage.binary_dilation(band, np.ones((3, 3, 3), bool), iterations=BAND)
        band = band[BAND:-BAND, BAND:-BAND, BAND:-BAND].reshape(-1)
    with phases('indices'):
        idx = np.indices(shape).reshape(3, -1).T + r0                      # global grid indices
    S = np.empty(len(idx))
    if band.any():
        with phases('distance'):
            S[band], *_ = igl.signed_distance(
                lo + idx[band] * h, V, F,
                sign_type=igl.SignedDistanceType.SIGNED_DISTANCE_TYPE_FAST_WINDING_NUMBER)
    far = ~band
    if far.any():
        # The lattice this block needs is a small regular grid: build it
        # directly and look values up by arithmetic (sorting the far points'
        # lattice indices instead cost 195 of 255 s at 0.15 mm).
        with phases('lattice'):
            lat = np.round(idx[far] / K).astype(np.int64)
            l0 = lat.min(0)
            dims = lat.max(0) - l0 + 1
            coarse = np.indices(dims).reshape(3, -1).T + l0
        with phases('winding'):
            inside = (igl.fast_winding_number(V, F, lo + coarse * K * h) > 0.5).reshape(dims)
        d = lat - l0
        S[far] = np.where(inside[d[:, 0], d[:, 1], d[:, 2]], -1.0, 1.0) * (BAND + 1) * h
        stats['lattice_queries'] += len(coarse)
    stats['band_points'] += int(band.sum())
    stats['far_points'] += int(far.sum())
    return S.reshape(shape)


def reconstruct(V, F, h, blocks, phases, stats):
    pad = 2 * h
    lo = V.min(0) - pad
    n = np.ceil((V.max(0) + pad - lo) / h).astype(int) + 1
    # Neighbouring blocks share their boundary layer of grid points, so both
    # compute identical values there and their surfaces meet vertex for vertex.
    cuts = [np.linspace(0, n[i] - 1, blocks + 1).round().astype(int) for i in range(3)]
    with phases('sampling'):
        cells = surface_sample_cells(V, F, lo, h, np.random.default_rng(0))
    pieces_v, pieces_f, offset = [], [], 0
    for bx in range(blocks):
        for by in range(blocks):
            for bz in range(blocks):
                r0 = np.array([cuts[0][bx], cuts[1][by], cuts[2][bz]])
                r1 = np.array([cuts[0][bx + 1], cuts[1][by + 1], cuts[2][bz + 1]])
                S = block_field(r0, r1, cells, V, F, lo, h, phases, stats)
                with phases('grid_coords'):
                    # igl.marching_cubes wants x varying fastest.
                    GV = np.column_stack([g.ravel('F') for g in np.meshgrid(
                        *[lo[i] + h * np.arange(r0[i], r1[i] + 1) for i in range(3)], indexing='ij')])
                with phases('marching_cubes'):
                    mv, mf, _ = igl.marching_cubes(S.ravel('F'), GV, *S.shape, 0.0)
                if len(mf):
                    pieces_v.append(mv)
                    pieces_f.append(mf + offset)
                    offset += len(mv)
    with phases('weld'):
        Vo, Fo = np.vstack(pieces_v), np.vstack(pieces_f)
        key = np.round(Vo / (h * 1e-4)).astype(np.int64)
        _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
        Vo, Fo = Vo[first], inv.ravel()[Fo]
        Fo = Fo[(Fo[:, 0] != Fo[:, 1]) & (Fo[:, 1] != Fo[:, 2]) & (Fo[:, 0] != Fo[:, 2])]
    return Vo, Fo, n


def volume(v, f):
    a, b, c = (v[f[:, i]].astype(np.float64) for i in range(3))
    return float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6)


def main(argv):
    src, dst, h, blocks = argv[0], argv[1], float(argv[2]), int(argv[3])
    started = time.monotonic()
    loaded = mesh_io.load(mesh_io.probe(src, dst))
    V = loaded.geometry.verts.astype(np.float64)
    F = loaded.geometry.faces.astype(np.int64)
    phases = Phases()
    stats = {'band_points': 0, 'far_points': 0, 'lattice_queries': 0}
    Vo, Fo, n = reconstruct(V, F, h, blocks, phases, stats)
    out = Mesh(dst, dst, Kind.BINARY_STL, len(Fo), True, None,
               Geometry(Vo.astype(np.float64), Fo.astype(np.int64)))
    mesh_io.write(out)
    result = {
        'h': h, 'blocks': blocks ** 3, 'grid': n.tolist(), 'faces_out': int(len(Fo)),
        'recon_seconds': round(time.monotonic() - started, 1),
        'recon_peak_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        'phases_s': {k: round(v, 1) for k, v in phases.seconds.items()},
        # Shared boundary layers are counted once per block, so these
        # counts slightly exceed the grid size.
        **stats}
    if '--check' in argv:
        scan = scanner.scan(out)
        d2, _, _ = igl.point_mesh_squared_distance(V, Vo, Fo)
        result.update({'nm': scan.non_manifold, 'open': scan.open_edges,
                       'volume_in': round(volume(V, F)), 'volume_out': round(volume(Vo, Fo)),
                       'dist_orig_to_new_rms': round(float(np.sqrt(d2.mean())), 4),
                       'dist_orig_to_new_max': round(float(np.sqrt(d2.max())), 4)})
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
