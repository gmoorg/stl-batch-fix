"""Rebuild a mesh as a watertight solid from a winding-number signed distance.

The default per-part reconstruction (replacing alpha-wrap): a signed-distance
field on one global grid — magnitude from the exact distance to the input
surface, sign from libigl's fast generalized winding number, which stays
reliable on meshes with holes, flipped faces and overlapping shells — and the
surface extracted with marching cubes, block by block. See
docs/refactor/reconstruction.md for the algorithm's evidence and limits and
docs/refactor/modules.md for its contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import pipeconfig, scanner
from .mesh_io import Geometry, Mesh, require_geometry

try:
    import igl as _igl
    from scipy import ndimage as _ndimage
    _AVAILABLE = True
except ImportError:                                    # pragma: no cover
    _AVAILABLE = False


#: Grid spacing = the alpha alpha-wrap would use: diag / 800, capped at 0.15.
GRID_DIVISOR = 800.0
GRID_CAP = 0.15
#: Half-width, in cells, of the band where the exact distance is computed.
#: Every surface point is within h/√2 of a sample, and a sample within
#: h·√3/2 of the grid point it marks: 1.57 cells together. Dilating the
#: marks by BAND (Chebyshev) therefore covers every grid point within
#: (BAND − 1.57)·h = 3.43·h of the surface. Marching cubes reads only cells
#: the surface crosses, whose corners lie within √3·h = 1.73·h.
BAND = 5
#: Far from the surface only the sign matters: the winding number on every
#: K-th grid point, each far point taking its nearest lattice point's sign.
#: That lattice point is at most K/2·√3·h = 2.6·h away (K = 3), inside the
#: 3.43·h a far point keeps from the surface, so the segment between them
#: cannot cross it. (K = 4 would allow 3.46·h — not inside the margin.)
K = 3
#: Upper bound on surface samples generated at once (bounds transient memory).
SAMPLE_CHUNK = 2_000_000


class EmptyResult(RuntimeError):
    """The input encloses no volume: every grid point is outside (winding
    number <= 0.5) — a one-sided open sheet, typically debris. An open
    sheet's winding number is the solid angle it covers over 4π, at most
    0.5, so a sheet is never inside."""


class BudgetError(ValueError):
    """The estimated memory cannot fit the budget at any block count."""

    def __init__(self, floor: int, minimum: int, budget: int):
        super().__init__(
            f'estimated memory does not fit the budget: floor {floor / 1e9:.2f} GB, '
            f'best attainable {minimum / 1e9:.2f} GB, budget {budget / 1e9:.2f} GB')
        self.floor, self.minimum, self.budget = floor, minimum, budget


@dataclass(frozen=True)
class Plan:
    """How one reconstruction will run: grid, block split, memory estimate."""

    h: float
    lo: np.ndarray            # grid origin
    shape: tuple[int, int, int]
    blocks_per_axis: int
    samples_bound: int        # upper bound on surface samples
    estimate_bytes: int


def is_available() -> bool:
    """Whether libigl and scipy imported."""
    return _AVAILABLE


def grid_spacing(whole_model_diag: float) -> float:
    """The grid spacing for a model: `min(diag / 800, 0.15)`."""
    diagonal = float(whole_model_diag)
    if not math.isfinite(diagonal) or diagonal <= 0:
        raise ValueError('whole_model_diag must be finite and positive')
    return min(diagonal / GRID_DIVISOR, GRID_CAP)


# ---------------------------------------------------------------------------
# Memory model. Constants are calibrated on this implementation (see
# docs/refactor/reconstruction.md, "Memory model"); they are a sizing
# estimate fitted to measured runs, not a guaranteed upper bound.
# ---------------------------------------------------------------------------

# Least-squares fit to 8 measured runs (2026-10-03): ~100 MB base,
# ~710 B per input face, ~0 per sample, ~510 B per A/h², ~55 B per block
# point. Each constant below is ~20 % above the fit; every calibration run
# stays at or below its estimate with >= 25 % margin.
BASE_BYTES = 300_000_000           # interpreter, libraries, sample-chunk transients
INPUT_BYTES_PER_FACE = 800         # V/F arrays + exact-distance AABB + FWN octree
SAMPLE_BYTES = 10                  # per bounded sample: deduplicated grid keys
OUTPUT_BYTES_PER_UNIT = 600        # per A/h²: retained output, weld, validation scan
POINT_BYTES = 65                   # per grid point of the largest block


def estimate_bytes(faces_in: int, area: float, h: float, samples_bound: int,
                   block_shape: tuple[int, int, int]) -> int:
    """Estimated peak memory for one reconstruction."""
    points = int(np.prod(block_shape))
    padded = int(np.prod([s + 2 * BAND for s in block_shape]))
    return int(BASE_BYTES + INPUT_BYTES_PER_FACE * faces_in
               + SAMPLE_BYTES * samples_bound
               + OUTPUT_BYTES_PER_UNIT * area / h ** 2
               + POINT_BYTES * points + 3 * padded)


def _cuts(n: int, blocks: int) -> np.ndarray:
    return np.linspace(0, n - 1, blocks + 1).round().astype(np.int64)


def _largest_block(shape, blocks: int) -> tuple[int, int, int]:
    return tuple(int(np.diff(_cuts(n, blocks)).max()) + 1 for n in shape)


def _check_spacing(h) -> None:
    if isinstance(h, bool) or not (isinstance(h, (int, float)) and math.isfinite(h) and h > 0):
        raise ValueError('grid spacing must be finite and positive')


def _grid(V: np.ndarray, h: float) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Grid origin and points per axis: the bounding box padded by 2.5h.

    The extra half cell keeps the model's extreme faces — e.g. a flat base —
    off the grid planes. With the origin at `min - 2h` those faces always lay
    exactly on grid points, where the winding number is ~0.5 and marching
    cubes can only bevel edges by a full half cell; midway between planes the
    surface is placed by interpolation.
    """
    lo = V.min(0) - 2.5 * h
    shape = tuple(int(n) for n in np.ceil((V.max(0) + 2.5 * h - lo) / h).astype(np.int64) + 1)
    if max(shape) >= 2 ** 31 - 1 or np.prod(shape, dtype=np.float64) >= 2 ** 62:
        raise ValueError(f'grid {shape} is too large to index')
    return lo, shape


def max_blocks_per_axis(shape) -> int:
    """The finest split that keeps every block at least 2·BAND + 2 points
    wide; 1 is always valid, even for a grid already narrower than that."""
    return max(1, min((n - 1) // (2 * BAND + 1) for n in shape))


# ---------------------------------------------------------------------------
# Input validation and surface sampling
# ---------------------------------------------------------------------------

def _validated(mesh: Mesh) -> tuple[np.ndarray, np.ndarray]:
    require_geometry(mesh)
    V = np.asarray(mesh.geometry.verts, dtype=np.float64)
    F = np.asarray(mesh.geometry.faces, dtype=np.int64)
    if len(F) == 0:
        raise ValueError('mesh has no faces')
    if not np.isfinite(V).all():
        raise ValueError('mesh has NaN or infinite coordinates')
    if F.min() < 0 or F.max() >= len(V):
        raise ValueError('mesh has face indices outside its vertex array')
    return V, F


def _triangle_frames(V, F):
    """Per triangle: A, B (longest edge), C (apex), samples along AB (n1) and
    rows across the height (n2, 0 for a flat triangle)."""
    tri = V[F]                                               # (f, 3, 3)
    edges = np.stack([np.linalg.norm(tri[:, (k + 1) % 3] - tri[:, k], axis=1)
                      for k in range(3)], axis=1)            # edge k: vertex k -> k+1
    k = edges.argmax(1)
    rows = np.arange(len(F))
    A, B, C = tri[rows, k], tri[rows, (k + 1) % 3], tri[rows, (k + 2) % 3]
    L = edges[rows, k]
    area = np.linalg.norm(np.cross(B - A, C - A), axis=1) / 2
    H = np.where(L > 0, 2 * area / np.where(L > 0, L, 1), 0.0)
    return A, B, C, area, L, H


def _sample_counts(L, H, h):
    n1 = np.maximum(1, np.ceil(2 * L / h)).astype(np.int64)
    n2 = np.ceil(2 * H / h).astype(np.int64)                 # 0 for a flat triangle
    return n1, n2, (n1 + 1) * (n2 + 1)                       # upper bound per triangle


def _surface_keys(A, B, C, n1, n2, bound, lo, h, shape) -> np.ndarray:
    """Sorted unique grid keys (x-major) of every surface sample's nearest
    grid point. Samples per triangle: rows j = 0..n2 across the height,
    points i = 0..n1 along the longest edge, kept where i/n1 + j/n2 <= 1 —
    rows are <= h/2 apart and points within a row <= h/2 apart, so every
    triangle point is within h/√2 of a sample."""
    ny, nz = shape[1], shape[2]
    offsets = np.concatenate([[0], np.cumsum(bound)])
    keys = []
    start_tri = 0
    while start_tri < len(bound):
        # Triangles whose bounds fit one chunk; a single larger triangle is
        # generated alone, in flat-index ranges of at most SAMPLE_CHUNK.
        end_tri = int(np.searchsorted(offsets, offsets[start_tri] + SAMPLE_CHUNK, 'right')) - 1
        end_tri = max(end_tri, start_tri + 1)
        tris = np.arange(start_tri, end_tri)
        first, last = offsets[start_tri], offsets[end_tri]
        for k0 in range(first, last, SAMPLE_CHUNK):
            flat = np.arange(k0, min(k0 + SAMPLE_CHUNK, last))
            t = tris[np.searchsorted(offsets[tris + 1], flat, 'right')]
            local = flat - offsets[t]
            i = local % (n1[t] + 1)
            j = local // (n1[t] + 1)
            u = i / n1[t]
            v = np.where(n2[t] > 0, j / np.maximum(n2[t], 1), 0.0)
            keep = (u + v <= 1 + 1e-9) & ((n2[t] > 0) | (j == 0))
            t, u, v = t[keep], u[keep], v[keep]
            P = A[t] + u[:, None] * (B[t] - A[t]) + v[:, None] * (C[t] - A[t])
            cell = np.rint((P - lo) / h).astype(np.int64)
            np.clip(cell, 0, np.array(shape) - 1, out=cell)
            keys.append(np.unique((cell[:, 0] * ny + cell[:, 1]) * nz + cell[:, 2]))
        start_tri = end_tri
    return np.unique(np.concatenate(keys))


# ---------------------------------------------------------------------------
# Planning and reconstruction
# ---------------------------------------------------------------------------

def plan(mesh: Mesh, h: float, budget_bytes: int) -> Plan:
    """Choose the fewest blocks whose estimated peak fits `budget_bytes`."""
    _check_spacing(h)
    if isinstance(budget_bytes, bool) or not (isinstance(budget_bytes, int) and budget_bytes > 0):
        raise ValueError('memory budget must be a positive integer of bytes')
    V, F = _validated(mesh)
    lo, shape = _grid(V, h)
    _, _, _, area, L, H = _triangle_frames(V, F)
    samples = int(_sample_counts(L, H, h)[2].sum())
    total_area = float(area.sum())
    floor = estimate_bytes(len(F), total_area, h, samples, (0, 0, 0))
    best = None
    for blocks in range(1, max_blocks_per_axis(shape) + 1):
        est = estimate_bytes(len(F), total_area, h, samples, _largest_block(shape, blocks))
        best = est if best is None else min(best, est)
        if est <= budget_bytes:
            return Plan(h, lo, shape, blocks, samples, est)
    raise BudgetError(floor, best, budget_bytes)


def reconstruct(mesh: Mesh, h: float, blocks_per_axis: int) -> Mesh:
    """Rebuild `mesh` as a closed surface on a grid of spacing `h`,
    processed in `blocks_per_axis`³ blocks (the result does not depend on the
    block count beyond float rounding at shared block faces).

    The result may contain non-manifold edges: they are passed on for the
    following repair steps rather than failing the part (owner decision
    2026-10-04, docs/errors/winding-non-manifold.md). A direct caller must
    expect them; `processor`'s judge flags any left in a final output.

    Raises ValueError for invalid input and RuntimeError when the result is
    not a non-empty, finite, closed, non-degenerate surface.
    """
    if not _AVAILABLE:
        raise RuntimeError('libigl/scipy are not installed')
    _check_spacing(h)
    V, F = _validated(mesh)
    lo, shape = _grid(V, h)
    if isinstance(blocks_per_axis, bool) or not (isinstance(blocks_per_axis, int) and 1 <= blocks_per_axis <= max_blocks_per_axis(shape)):
        raise ValueError(f'blocks_per_axis must be 1..{max_blocks_per_axis(shape)}')
    ny, nz = shape[1], shape[2]

    A, B, C, _, L, H = _triangle_frames(V, F)
    n1, n2, bound = _sample_counts(L, H, h)
    keys = _surface_keys(A, B, C, n1, n2, bound, lo, h, shape)
    del A, B, C, L, H, n1, n2, bound

    # Inside is decided by the winding number, which follows face orientation:
    # an inside-out solid reads about -1 inside and a shell with some faces
    # flipped can read about 0 — both would come out EMPTY. So the faces are
    # first made consistent (bfs_orient: agree across shared edges) and each
    # consistent patch is turned outward (orient_outward). Distances do not
    # depend on orientation; only the sign does.
    F = _oriented(V, F)
    tree = _igl.AABB()
    tree.init(V, F)                                  # built once, reused by every block
    cuts = [_cuts(n, blocks_per_axis) for n in shape]
    far_value = (BAND + 1) * h
    pieces_v, pieces_f, pieces_e, offset = [], [], [], 0
    for bx in range(blocks_per_axis):
        for by in range(blocks_per_axis):
            for bz in range(blocks_per_axis):
                r0 = np.array([cuts[0][bx], cuts[1][by], cuts[2][bz]])
                r1 = np.array([cuts[0][bx + 1], cuts[1][by + 1], cuts[2][bz + 1]])
                block_shape = tuple(int(n) for n in r1 - r0 + 1)
                if math.prod(block_shape) >= 2 ** 32:
                    # Marching cubes packs an edge's two corner indices into
                    # 32 bits each (see `_edge_ids`).
                    raise RuntimeError(f'block {block_shape} has too many grid points '
                                       'for marching cubes edge keys')
                field = _block_field(V, F, tree, keys, r0, r1, lo, h, ny, nz, far_value)
                axes = [lo[a] + h * np.arange(r0[a], r1[a] + 1) for a in range(3)]
                grid = np.column_stack([g.ravel('F') for g in np.meshgrid(*axes, indexing='ij')])
                mv, mf, e2v = _igl.marching_cubes(field.ravel('F'), grid, *field.shape, 0.0)
                del grid, field
                if len(mf):
                    pieces_v.append(mv)
                    pieces_f.append(mf + offset)
                    pieces_e.append(_edge_ids(e2v, len(mv), block_shape, r0))
                    offset += len(mv)
    if not pieces_f:
        _raise_empty(mesh, V, F)
    out = mesh.with_geometry(_weld(np.vstack(pieces_v), np.vstack(pieces_f),
                                   np.vstack(pieces_e)))
    _check(out)
    return out


def _block_field(V, F, tree, keys, r0, r1, lo, h, ny, nz, far_value) -> np.ndarray:
    """Signed field on grid points r0..r1 (inclusive): exact signed distance
    in the band, ±far_value with the winding-number sign elsewhere."""
    shape = tuple(int(s) for s in r1 - r0 + 1)
    g0, g1 = r0 - BAND, r1 + BAND                     # band cells that can reach this block
    span = keys[np.searchsorted(keys, g0[0] * ny * nz):
                np.searchsorted(keys, (g1[0] + 1) * ny * nz)]
    x, rem = np.divmod(span, ny * nz)
    y, z = np.divmod(rem, nz)
    near = (y >= g0[1]) & (y <= g1[1]) & (z >= g0[2]) & (z <= g1[2])
    padded = np.zeros(tuple(s + 2 * BAND for s in shape), bool)
    padded[x[near] - g0[0], y[near] - g0[1], z[near] - g0[2]] = True
    del span, x, y, z, rem, near
    band = _ndimage.binary_dilation(padded, np.ones((3, 3, 3), bool), iterations=BAND)
    band = band[BAND:-BAND, BAND:-BAND, BAND:-BAND]
    del padded

    # Far field: the sign of the nearest coarse-lattice point (every K-th grid index).
    lat = [np.rint(np.arange(r0[a], r1[a] + 1) / K).astype(np.int64) for a in range(3)]
    coarse_axes = [np.arange(l.min(), l.max() + 1) for l in lat]
    coarse = np.stack(np.meshgrid(*coarse_axes, indexing='ij'), -1).reshape(-1, 3)
    inside_coarse = (_igl.fast_winding_number(V, F, lo + coarse * K * h) > 0.5).reshape(
        tuple(len(c) for c in coarse_axes))
    inside = inside_coarse[np.ix_(*(l - l.min() for l in lat))]
    field = np.where(inside, -far_value, far_value)
    del inside, inside_coarse, coarse

    idx = np.nonzero(band)
    if len(idx[0]):
        points = lo + (np.column_stack(idx) + r0) * h
        sq, nearest, _ = tree.squared_distance(V, F, points)
        distance = np.sqrt(sq)
        inside = _igl.fast_winding_number(V, F, points) > 0.5
        # A grid point lying ON the input surface sits exactly where the
        # winding number jumps, so its own value is a coin toss (0.5 for a
        # lone face, ~1.0 on a face shared by two touching solids, ~1.5 on a
        # face buried in another part) and float noise leaves ragged sheets.
        # Decide it from both sides instead: a tiny step along the nearest
        # face's normal each way. Inside only if BOTH sides are inside — then
        # the point is within the solid (shared or buried face), not on its
        # boundary; an outer face or a lone sheet counts as outside.
        on_surface = np.flatnonzero(distance < h * 1e-6)
        if len(on_surface):
            inside[on_surface] = _both_sides_inside(
                V, F, points[on_surface], np.asarray(nearest)[on_surface], h)
        # No value is exactly 0, so marching cubes never has to break a tie.
        field[idx] = np.maximum(distance, h * 1e-6) * np.where(inside, -1.0, 1.0)
    return field


def _both_sides_inside(V, F, points, faces, h) -> np.ndarray:
    """For points on the input surface: inside on both sides of the nearest
    face (winding number > 0.5 a step of h·1e-3 along its normal each way)."""
    a, b, c = (V[F[faces, i]] for i in range(3))
    normal = np.cross(b - a, c - a)
    length = np.linalg.norm(normal, axis=1)
    usable = length > 0
    normal[usable] /= length[usable, None]
    step = normal * (h * 1e-3)
    w_plus = _igl.fast_winding_number(V, F, points + step)
    w_minus = _igl.fast_winding_number(V, F, points - step)
    return usable & (w_plus > 0.5) & (w_minus > 0.5)


def _oriented(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    """Faces made consistently wound and turned outward, patch by patch."""
    FF, patches = _igl.bfs_orient(F)
    FF, _ = _igl.orient_outward(V, FF, np.asarray(patches, dtype=np.int64).reshape(-1, 1))
    return np.asarray(FF, dtype=np.int64)


def _raise_empty(mesh: Mesh, V: np.ndarray, F: np.ndarray) -> None:
    """No surface came out, so the part is dropped (`EmptyResult`) and the
    model is merged without it — owner policy (2026-10-03), as the splitter
    already discards tiny shells. The reason says what was dropped: an open
    sheet (its winding number never exceeds 0.5) or a closed part thinner
    than the grid. No volume threshold decides it — models may be in inches
    or millimetres. Gross loss is still caught for the whole model by the
    judge's retained-volume check (`processor.MIN_VOLUME_KEPT`)."""
    scan = scanner.scan(mesh)
    if scan.open_edges or scan.non_manifold:
        raise EmptyResult('encloses no volume (open sheet or debris)')
    a, b, c = (V[F[:, i]] for i in range(3))
    enclosed = abs(float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6))
    raise EmptyResult(f'closed part thinner than the grid (volume {enclosed:.4g})')


def _edge_ids(e2v: dict, n_verts: int, block_shape, r0) -> np.ndarray:
    """The global grid edge of each vertex marching cubes made for one block:
    rows of (axis, x, y, z), the edge's axis and its lower corner, in vertex
    order.

    Marching cubes places every vertex on one grid edge and reports which in
    `e2v`: key `(i << 32) | j` for the edge's two corners, indexed
    `x + y·nx + z·nx·ny` within the block, mapped to the vertex index. Edge
    identity therefore comes as integers — no coordinates are rounded. Raises
    RuntimeError if the map does not describe one unit grid edge per vertex.
    """
    if len(e2v) != n_verts:
        raise RuntimeError(f'marching cubes reported {len(e2v)} edges for {n_verts} vertices')
    keys = np.fromiter(e2v.keys(), dtype=np.uint64, count=n_verts)
    verts = np.fromiter(e2v.values(), dtype=np.int64, count=n_verts)
    if not np.array_equal(np.sort(verts), np.arange(n_verts)):
        raise RuntimeError('marching cubes edge map does not cover every vertex once')
    nx, ny, nz = block_shape
    corners = []
    for packed in (keys >> np.uint64(32), keys & np.uint64(0xFFFFFFFF)):
        index = packed.astype(np.int64)
        if (index >= nx * ny * nz).any():
            raise RuntimeError('marching cubes edge corner outside its block')
        z, rest = np.divmod(index, nx * ny)
        y, x = np.divmod(rest, nx)
        corners.append(np.column_stack([x, y, z]))
    step = np.abs(corners[1] - corners[0])
    if not (step.sum(axis=1) == 1).all():
        raise RuntimeError('marching cubes edge is not one grid step on one axis')
    ids = np.empty((n_verts, 4), np.int64)
    ids[verts, 0] = np.argmax(step, axis=1)
    ids[verts, 1:] = np.minimum(corners[0], corners[1]) + r0
    return ids


def _weld(Vo: np.ndarray, Fo: np.ndarray, edges: np.ndarray) -> Geometry:
    """Merge the vertices that lie on the same grid edge and drop faces that
    collapsed.

    Neighbouring blocks share their boundary planes, so a vertex on a shared
    edge is computed by both, equal up to float rounding (measured ≤ 4e-15).
    Merging by the edge each vertex lies on (`_edge_ids`) joins those copies
    exactly, and never merges two vertices on different edges however close
    they are. The earlier weld rounded coordinates to h·1e-4; where the field
    is ~0 at a grid node, the vertices of all edges meeting there cluster
    within that distance, and a cluster split by a rounding boundary was
    merged partially, pinching the surface into non-manifold edges
    (docs/errors/winding-non-manifold.md).

    One marching-cubes triangle never has two corners on one edge, so no face
    should collapse; the filter is a guard.
    """
    _, first, inv = np.unique(edges, axis=0, return_index=True, return_inverse=True)
    Vo, Fo = Vo[first], inv.ravel()[Fo]
    Fo = Fo[(Fo[:, 0] != Fo[:, 1]) & (Fo[:, 1] != Fo[:, 2]) & (Fo[:, 0] != Fo[:, 2])]
    return Geometry(Vo.astype(np.float32), Fo.astype(np.int64))


def _check(mesh: Mesh) -> None:
    """The success contract: non-empty, finite, closed, no degenerate faces.
    Non-manifold edges are not checked: they pass on to the following repair
    steps instead of failing the part (owner decision 2026-10-04)."""
    if len(mesh.geometry.faces) == 0:
        raise RuntimeError('reconstruction produced no faces')
    if not np.isfinite(mesh.geometry.verts).all():
        raise RuntimeError('reconstruction produced non-finite coordinates')
    scan = scanner.scan(mesh)
    if scan.open_edges or scan.degenerate:
        raise RuntimeError(
            f'reconstruction is not closed: open={scan.open_edges}, '
            f'degenerate={scan.degenerate}, non_manifold={scan.non_manifold}')


def step_winding_reconstruct(mesh: Mesh, config: "pipeconfig.StepConfig | None" = None
                             ) -> tuple[bool, Mesh, str]:
    """Uniform step: rebuild one part as a solid.

    Reads `config.whole_model_diag` (grid spacing, as alpha-wrap reads it for
    alpha) and `config.reconstruct_memory_budget_bytes` (block count).

    A part that rebuilds to nothing — an open sheet such as the far-away
    debris in fixture foot1.stl, or a closed part thinner than the grid — is
    DROPPED (owner policy, 2026-10-03): the step succeeds with an empty mesh,
    which merge leaves out, and says why in its detail; the model is merged
    without it. Any other failure — including a budget no block count can
    meet — returns `(False, mesh, reason)`.
    """
    try:
        whole_diagonal = config.whole_model_diag if config is not None else None
        if whole_diagonal is None:
            raise ValueError('whole_model_diag is required for reconstruction')
        budget = (config.reconstruct_memory_budget_bytes if config is not None
                  else pipeconfig.StepConfig().reconstruct_memory_budget_bytes)
        h = grid_spacing(whole_diagonal)
        p = plan(mesh, h, budget)
        try:
            result = reconstruct(mesh, h, p.blocks_per_axis)
        except EmptyResult as exc:
            empty = mesh.with_geometry(Geometry(np.zeros((0, 3), np.float32),
                                                np.zeros((0, 3), np.int64)))
            return True, empty, f'dropped: {exc}; {len(mesh.geometry.faces)} faces removed'
        # A second scan (reconstruct's own is internal): seconds, against a
        # reconstruction of tens to hundreds of seconds.
        non_manifold = scanner.scan(result).non_manifold
        passed_on = f', nm={non_manifold} passed on' if non_manifold else ''
        return True, result, (f'h={h:g}, blocks={p.blocks_per_axis}^3, '
                              f'est={p.estimate_bytes / 1e9:.2f} GB, '
                              f'{len(result.geometry.faces)} faces{passed_on}')
    except Exception as exc:
        return False, mesh, f'{type(exc).__name__}: {exc}'
