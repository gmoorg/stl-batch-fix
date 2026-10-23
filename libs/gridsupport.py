"""A removable grid of thin walls under elevated bottom undersides — geometry only.

Bambu's tree supports hold a curved underside (a shin, a thigh) only at their
tips, so the first lines of that underside are laid over air, sag and never
fuse into a shell.  This grid is modelled support instead: single-line walls
in X and in Y on one lattice, standing on a thin base sheet on the plate.
Every wall top stays `gap` below the model and `xy_distance` away from it
sideways, so nothing fuses; the base is joined into ONE piece (strips routed
around the model's plate contact when needed) so the whole grid comes off
together.  Whether the gap survives slicing, the walls print as single lines
and the grid comes off cleanly are properties of the print, not of this
geometry (owner request, 2026-10-12: mesh first, skin later if needed).

All coordinates are world millimetres with the plate at z = 0; the caller
converts to object coordinates.

Clearance comes from the ORIGINAL triangles, not from a sampled height map.
For every raster cell, `_ceiling` gives a lower bound of the model's lowest
point over that cell: for each triangle overlapping the cell's square,
max(the triangle's lowest vertex, the triangle's plane minimised over the
square) — both bounds hold for the part of the triangle above the square,
so their maximum does too.  Any model material above a cell is bounded below
by some surface triangle over it, so the bound covers flared connectors and
vertical faces as well.  A solid over a cell may then reach
    limit = min(ceiling of the cell and every cell within xy_distance) - gap - EPS
i.e. `gap` below anything directly above it or beside it.  The gap applies
beside too: walls run past the model's edges (whole lattice squares), and a
top level with a lower edge just outside it would touch that edge.
Every generated solid is a union of boxes whose footprint is a set of whole
cells, each box no higher than the limit of every cell it touches; wall tops
are therefore stepped per cell, never interpolated.

The candidate area (what needs support) comes from `basecheck`'s underside
map; only clearance uses the triangles.

Walls follow whole lattice squares (pitch x pitch, between consecutive
lattice lines): every square holding an eligible cell gets all four of its
edges as walls, each running on past the corners far enough to overlap the
crossing wall, so the walls form closed cells that brace each other.  Walls
placed only over the eligible cells stood alone as pins wherever the
supported strip was thinner than the pitch (torso.3mf, 2026-10-16: 175 of
177 wall pieces under 2.5 mm long, up to 29 mm tall).  The cost is wall
up to one pitch beyond the area that needs it, near surfaces that did not.
A wall's top is the clearance limit, capped at its square's highest
eligible limit (never above max_height - gap), so walls beyond the model's
footprint (no ceiling) stay finite and no taller than their square needs.
Clearance can still cut a wall short of its crossing wall; such runs are
kept and counted (`unbraced_runs`), not dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

import manifold3d

#: Clearance budget subtracted from every limit, mm: covers float rounding and
#: manifold3d's merge tolerance, so a reported gap is never short.
EPS = 1e-4
#: Largest raster an instance may use.
MAX_CELLS = 20_000_000
#: Largest number of (triangle, cell) pairs `_ceiling` may visit.
MAX_TRIANGLE_WORK = 300_000_000
#: Largest output mesh, triangles.
MAX_TRIANGLES = 5_000_000
#: Extra plate area around the candidates where connecting strips may run, mm.
ROUTE_PAD = 10.0
#: Largest closing radius used to merge nearby candidate patches, mm.
MAX_CLOSING = 2.0


class GridError(Exception):
    """The grid cannot be built for this instance; the message says why."""


@dataclass(frozen=True)
class GridSettings:
    """pitch          distance between walls (both directions), mm
    width           wall thickness, mm — about one extrusion line
    gap             vertical clearance between a wall top and the model, mm
    xy_distance     sideways clearance between any support and the model, mm
    base_height     thickness of the base sheet, mm (first layer + one layer)
    min_wall        a wall must rise at least this far above the base, mm
    margin          how far the base reaches past the supported area, mm
    connector_width width of strips that join base pieces, mm
    cell            raster resolution, mm
    min_area        candidate patches smaller than this are not supported, mm²
    """

    pitch: float = 2.0
    width: float = 0.42
    gap: float = 0.2
    xy_distance: float = 0.35
    base_height: float = 0.4
    min_wall: float = 0.2
    margin: float = 1.0
    connector_width: float = 2.0
    cell: float = 0.1
    min_area: float = 0.5

    def __post_init__(self):
        for name in ('pitch', 'width', 'base_height', 'min_wall', 'connector_width', 'cell'):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f'{name} must be a positive number, got {value!r}')
        for name in ('gap', 'xy_distance', 'margin', 'min_area'):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value >= 0):
                raise ValueError(f'{name} must be zero or positive, got {value!r}')
        if self.width >= self.pitch:
            raise ValueError(f'width ({self.width}) must be smaller than pitch ({self.pitch}): '
                             'walls that touch are a solid block, not a grid')
        if self.cell > self.width:
            raise ValueError(f'cell ({self.cell}) must not exceed the wall width ({self.width})')


@dataclass(frozen=True)
class GridSupport:
    """The generated solid and what it covers (areas in mm², lengths in mm).

    candidate_area  undersides that need support in the band
    no_room_area    candidates too low or too close to the model for base +
                    wall + gap — left unsupported
    eligible_area   the rest: candidates a wall could stand under
    near_area       eligible area within pitch/2 of a generated wall (a
                    proximity measure, not proof the wall holds that point)
    missed_area     eligible area farther than that from every wall
    unbraced_runs/unbraced_length  wall runs that meet no crossing wall
                    (expected where the model leaves no room for one) — kept
    gap_median/gap_p95  vertical CAD clearance between wall tops and the
                    model's lower bound directly above them, over wall cells
                    with model overhead (nan when none has)
    """

    vertices: np.ndarray
    faces: np.ndarray
    walls: int
    wall_length: float
    base_area: float
    base_pieces: int
    connectors: int
    connector_area: float
    candidate_area: float
    no_room_area: float
    eligible_area: float
    near_area: float
    missed_area: float
    fallback_crosses: int
    unbraced_runs: int
    unbraced_length: float
    gap_median: float
    gap_p95: float
    top_range: tuple[float, float]


@dataclass(frozen=True)
class _Raster:
    x0: float
    y0: float
    cell: float
    nx: int
    ny: int


def build(tris: np.ndarray, candidates: np.ndarray, cx0: float, cy0: float, ccell: float,
          max_height: float, settings: GridSettings) -> GridSupport:
    """The grid for one instance.

    tris        (n, 3, 3) printable model triangles, world mm
    candidates  underside cells that need support (rows = y, columns = x) on
                a map with lower-left corner (cx0, cy0) and cell size ccell;
                every candidate's underside is below max_height
    Raises GridError when no single-piece grid can be built.
    """
    s = settings
    if not candidates.any():
        raise GridError('nothing in the band needs support')
    cell = max(s.cell, ccell)
    rows, cols = np.nonzero(candidates)
    lo = np.array([cx0 + cols.min() * ccell, cy0 + rows.min() * ccell])
    hi = np.array([cx0 + (cols.max() + 1) * ccell, cy0 + (rows.max() + 1) * ccell])
    # Every lattice square holding a candidate lies inside the snapped box,
    # so the pad below is measured from its walls, whatever the pitch.
    lo, hi = np.floor(lo / s.pitch) * s.pitch, np.ceil(hi / s.pitch) * s.pitch
    pad =s.margin + s.width + MAX_CLOSING + s.connector_width + ROUTE_PAD
    origin = np.floor((lo - pad) / cell) * cell
    nx, ny = (int(v) for v in np.ceil((hi + pad - origin) / cell))
    if nx * ny > MAX_CELLS:
        raise GridError(f'support area too large for the {cell:g} mm raster '
                        f'({nx * ny:,} cells > {MAX_CELLS:,})')
    raster = _Raster(float(origin[0]), float(origin[1]), cell, nx, ny)

    covered = _resample(candidates, cx0, cy0, ccell, raster)
    # Every wall top is capped at max_height - gap (`_squares`), and the base
    # is lower still: triangles wholly above max_height can never come within
    # the gap of the grid and are left out.
    low = tris[tris[:, :, 2].min(axis=1) < max_height] if len(tris) else tris
    ceiling = _ceiling(low, raster)
    limit = _limit(ceiling, raster, s)

    area = cell * cell
    eligible = covered & (limit >= s.base_height + s.min_wall)
    no_room = covered & ~eligible
    if not eligible.any():
        raise GridError(f'no room under the candidates for base ({s.base_height:g}) + wall '
                        f'({s.min_wall:g}) + gap ({s.gap:g}) clear of the model')

    base_ok = limit >= s.base_height
    square, capmap = _squares(eligible, limit, raster, s, max_height)
    reach = _reach(capmap, raster, s)
    closing = _square(min(s.pitch, MAX_CLOSING) / cell)
    footprint = ndimage.binary_closing(eligible, structure=closing) | eligible
    footprint = ndimage.binary_dilation(footprint, structure=_square((s.margin + s.width) / cell))
    # The wall zone joins the base explicitly: with no margin the walls'
    # overlap past each square edge reaches beyond the dilated footprint.
    # One cell more keeps wall ends inside the base outline, not flush with
    # its edge (a flush end leaves manifold3d a stray vertex on that face).
    zone = ndimage.binary_dilation(np.isfinite(reach), structure=_EIGHT)
    base = (footprint | zone) & base_ok

    walls = _Walls(raster, limit, ceiling, s)
    walls.lattice(reach, base)
    crosses = walls.fallback(eligible, square, capmap, base)
    if not walls.runs:
        raise GridError('no wall fits under the candidates')
    unbraced = walls.unbraced()

    base = _keep_supporting(base, walls.cells)
    pieces = int(ndimage.label(base, structure=_FOUR)[1])
    base, connectors, connector_cells = _join(base, base_ok, walls.cells, raster, s)
    base = _remove_pinches(base, base_ok, walls.cells)
    if ndimage.label(base, structure=_FOUR)[1] != 1:
        raise GridError('base could not be made one piece without diagonal-only contacts')

    solid = _solid(base, walls, raster, s)
    mesh = solid.to_mesh64()
    vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    faces = np.asarray(mesh.tri_verts, dtype=np.int64)

    near = eligible & ndimage.binary_dilation(walls.cells,
                                              structure=_square(s.pitch / 2 / cell))
    gaps = np.asarray(walls.gaps)
    gaps = gaps[np.isfinite(gaps)]
    return GridSupport(
        vertices=vertices, faces=faces, walls=len(walls.runs),
        wall_length=float(sum(r.length for r in walls.runs)),
        base_area=float(base.sum() * area), base_pieces=pieces,
        connectors=connectors, connector_area=float(connector_cells * area),
        candidate_area=float(covered.sum() * area), no_room_area=float(no_room.sum() * area),
        eligible_area=float(eligible.sum() * area), near_area=float(near.sum() * area),
        missed_area=float((eligible & ~near).sum() * area), fallback_crosses=crosses,
        unbraced_runs=len(unbraced), unbraced_length=float(sum(r.length for r in unbraced)),
        gap_median=float(np.median(gaps)) if len(gaps) else math.nan,
        gap_p95=float(np.percentile(gaps, 95)) if len(gaps) else math.nan,
        top_range=(float(min(r.tops.min() for r in walls.runs)),
                   float(max(r.tops.max() for r in walls.runs))))


# ------------------------------------------------------------- clearance

_FOUR = ndimage.generate_binary_structure(2, 1)
_EIGHT = np.ones((3, 3), dtype=bool)


def _square(radius_cells: float) -> np.ndarray:
    k = int(math.ceil(radius_cells - 1e-9))
    return np.ones((2 * k + 1, 2 * k + 1), dtype=bool)


def _resample(mask, x0, y0, cell, raster: _Raster) -> np.ndarray:
    """Raster cells containing the centre of a `mask` cell (raster.cell >= cell)."""
    out = np.zeros((raster.ny, raster.nx), dtype=bool)
    rows, cols = np.nonzero(mask)
    i = np.floor((x0 + (cols + 0.5) * cell - raster.x0) / raster.cell).astype(np.int64)
    j = np.floor((y0 + (rows + 0.5) * cell - raster.y0) / raster.cell).astype(np.int64)
    out[j, i] = True
    return out


def _ceiling(tris: np.ndarray, raster: _Raster) -> np.ndarray:
    """Per cell, a lower bound of the model's lowest point above it (inf = none).

    Every triangle overlapping a cell's square contributes
    max(lowest vertex, its plane's minimum over the square); a cell its
    bounding box overlaps but the triangle misses gets a value that is still
    a lower bound of that triangle, so the result only errs low.
    """
    out = np.full((raster.ny, raster.nx), np.inf)
    if not len(tris):
        return out
    c = raster.cell
    x, y, z = tris[:, :, 0], tris[:, :, 1], tris[:, :, 2]
    i0 = np.floor((x.min(axis=1) - raster.x0) / c).astype(np.int64)
    i1 = np.floor((x.max(axis=1) - raster.x0) / c).astype(np.int64)
    j0 = np.floor((y.min(axis=1) - raster.y0) / c).astype(np.int64)
    j1 = np.floor((y.max(axis=1) - raster.y0) / c).astype(np.int64)
    inside = (i1 >= 0) & (j1 >= 0) & (i0 < raster.nx) & (j0 < raster.ny)
    i0, j0 = np.maximum(i0, 0), np.maximum(j0, 0)
    i1, j1 = np.minimum(i1, raster.nx - 1), np.minimum(j1, raster.ny - 1)
    work = int(((i1 - i0 + 1) * (j1 - j0 + 1))[inside].sum())
    if work > MAX_TRIANGLE_WORK:
        raise GridError(f'model too detailed for the clearance raster ({work:,} triangle-cell '
                        f'pairs > {MAX_TRIANGLE_WORK:,})')

    zmin = z.min(axis=1)
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(normal, axis=1)
    sloped = np.abs(normal[:, 2]) > 1e-9 * np.maximum(length, 1e-300)
    nz = np.where(sloped, normal[:, 2], 1.0)
    a = np.where(sloped, -normal[:, 0] / nz, 0.0)
    b = np.where(sloped, -normal[:, 1] / nz, 0.0)
    offset = np.where(sloped, z[:, 0] - a * x[:, 0] - b * y[:, 0], -np.inf)

    flat = out.reshape(-1)

    def visit(sel, ii, jj):
        xa, ya = raster.x0 + ii * c, raster.y0 + jj * c
        low = (offset[sel] + np.minimum(a[sel] * xa, a[sel] * (xa + c))
               + np.minimum(b[sel] * ya, b[sel] * (ya + c)))
        np.minimum.at(flat, jj * raster.nx + ii, np.maximum(zmin[sel], low))

    span = np.maximum(i1 - i0 + 1, j1 - j0 + 1)
    lower = 0
    for size in (1, 2, 4, 8, 16, 32):
        group = np.flatnonzero(inside & (span > lower) & (span <= size))
        lower = size
        for start in range(0, len(group), 500_000):
            chunk = group[start:start + 500_000]
            for di in range(size):
                for dj in range(size):
                    sel = chunk[(i0[chunk] + di <= i1[chunk]) & (j0[chunk] + dj <= j1[chunk])]
                    if len(sel):
                        visit(sel, i0[sel] + di, j0[sel] + dj)
    for t in np.flatnonzero(inside & (span > 32)):
        jj, ii = np.mgrid[j0[t]:j1[t] + 1, i0[t]:i1[t] + 1]
        ii, jj = ii.reshape(-1), jj.reshape(-1)
        visit(np.full(len(ii), t), ii, jj)
    return out


def _limit(ceiling: np.ndarray, raster: _Raster, s: GridSettings) -> np.ndarray:
    """Highest top a solid over each cell may have (see the module docstring).

    The raster's outer ring is closed: the ceiling there misses model parts
    just outside the raster.
    """
    k = int(math.ceil(s.xy_distance / raster.cell - 1e-9))
    beside = ndimage.minimum_filter(ceiling, size=2 * k + 1, mode='constant', cval=-np.inf)
    limit = beside - s.gap - EPS        # `beside` includes the cell itself
    ring = k + 1
    limit[:ring, :] = limit[-ring:, :] = -np.inf
    limit[:, :ring] = limit[:, -ring:] = -np.inf
    return limit


# ----------------------------------------------------------------- walls

def _squares(eligible, limit, raster: _Raster, s: GridSettings, max_height: float):
    """The lattice squares that hold an eligible cell, and their wall cap.

    Returns (square, capmap): `square` numbers each cell's lattice square
    (the cell's centre decides); `capmap` is, per cell, its square's cap —
    the highest `limit` among that square's eligible cells, never above
    max_height - gap — or -inf when the square holds no eligible cell.
    The cap bounds walls standing where nothing is overhead (limit = inf)
    and keeps every wall no taller than the support its square needs.
    """
    c, p = raster.cell, s.pitch
    sx = np.floor((raster.x0 + (np.arange(raster.nx) + 0.5) * c) / p).astype(np.int64)
    sy = np.floor((raster.y0 + (np.arange(raster.ny) + 0.5) * c) / p).astype(np.int64)
    sx -= sx.min()
    sy -= sy.min()
    square = sy[:, None] * (int(sx.max()) + 1) + sx[None, :]
    cap = np.full(int(square.max()) + 1, -np.inf)
    np.maximum.at(cap, square[eligible], limit[eligible])
    cap = np.minimum(cap, max_height - s.gap)
    return square, cap[square]


def _reach(capmap, raster: _Raster, s: GridSettings) -> np.ndarray:
    """`capmap` spread by half a wall width (rounded up to whole cells).

    A wall on a square's edge then sees that square from either side, and
    runs on past the square's corners far enough to cover the crossing
    wall's full width, so the two overlap.
    """
    k = int(math.ceil((s.width / 2) / raster.cell - 1e-9))
    return ndimage.maximum_filter(capmap, size=2 * k + 1, mode='constant', cval=-np.inf)


@dataclass(frozen=True)
class _Run:
    along_x: bool       # the wall runs along world x (else along y)
    position: float     # world coordinate of the wall's centre line
    start: int          # first cell index along the wall
    tops: np.ndarray    # top height per cell along the wall
    cell: float

    @property
    def length(self) -> float:
        return len(self.tops) * self.cell

    def box(self, raster: _Raster, width: float) -> tuple[float, float, float, float]:
        """The run's footprint as built by `_wall`: (x_lo, x_hi, y_lo, y_hi)."""
        across = (self.position - width / 2, self.position + width / 2)
        origin = raster.x0 if self.along_x else raster.y0
        along = (origin + self.start * self.cell, origin + (self.start + len(self.tops)) * self.cell)
        return (*along, *across) if self.along_x else (*across, *along)


class _Walls:
    """Wall runs on the lattice plus their raster footprint and clearances."""

    def __init__(self, raster: _Raster, limit, ceiling, s: GridSettings):
        self.raster, self.limit, self.ceiling, self.s = raster, limit, ceiling, s
        self.runs: list[_Run] = []
        self.cells = np.zeros(limit.shape, dtype=bool)
        self.gaps: list[float] = []

    def lattice(self, reach, base):
        """Every lattice line, wherever it borders a selected square."""
        r, p = self.raster, self.s.pitch
        for along_x in (False, True):
            first = r.y0 if along_x else r.x0
            count = r.ny if along_x else r.nx
            k = math.ceil(first / p)
            while k * p < first + count * r.cell:
                self.line(along_x, k * p, reach, base)
                k += 1

    def fallback(self, eligible, square, capmap, base) -> int:
        """A cross through each eligible patch no wall touches.

        Its arms run edge to edge across the patch's own lattice square, so
        they end in that square's walls instead of standing alone.
        """
        labels, n = ndimage.label(eligible, structure=_EIGHT)
        added = 0
        r = self.raster
        min_cells = self.s.min_area / (r.cell ** 2)
        k = int(math.ceil((self.s.width / 2) / r.cell - 1e-9))
        own = np.full(capmap.shape, -np.inf)
        for index, where in enumerate(ndimage.find_objects(labels), start=1):
            patch = labels[where] == index
            if patch.sum() < min_cells or (self.cells[where] & patch).any():
                continue
            inner = ndimage.distance_transform_edt(np.pad(patch, 1))[1:-1, 1:-1]
            jj, ii = np.unravel_index(np.argmax(inner), patch.shape)
            j, i = where[0].start + jj, where[1].start + ii
            rows, cols = np.nonzero(square == square[j, i])
            box = (slice(max(rows.min() - k, 0), rows.max() + k + 1),
                   slice(max(cols.min() - k, 0), cols.max() + k + 1))
            own[box] = np.where(square[box] == square[j, i], capmap[box], -np.inf)
            own[box] = _reach(own[box], r, self.s)
            before = len(self.runs)
            self.line(False, r.x0 + (i + 0.5) * r.cell, own, base)
            self.line(True, r.y0 + (j + 0.5) * r.cell, own, base)
            own[box] = -np.inf
            added += len(self.runs) > before
        return added

    def unbraced(self) -> list[_Run]:
        """Runs whose footprint overlaps no perpendicular run's (positive area)."""
        w, r = self.s.width, self.raster
        boxes = {along: np.array([run.box(r, w) for run in self.runs if run.along_x == along])
                 .reshape(-1, 4) for along in (False, True)}
        out = []
        for run in self.runs:
            other = boxes[not run.along_x]
            x_lo, x_hi, y_lo, y_hi = run.box(r, w)
            meets = ((np.minimum(x_hi, other[:, 1]) - np.maximum(x_lo, other[:, 0]) > 1e-9)
                     & (np.minimum(y_hi, other[:, 3]) - np.maximum(y_lo, other[:, 2]) > 1e-9))
            if not meets.any():
                out.append(run)
        return out

    def line(self, along_x, position, reach, base):
        """Wall runs on one lattice line, cell by cell, capped by `reach`."""
        r, s = self.raster, self.s
        across0 = r.y0 if along_x else r.x0
        a = int(math.floor((position - s.width / 2 - across0) / r.cell))
        b = int(math.ceil((position + s.width / 2 - across0) / r.cell)) - 1
        limit_across = r.ny if along_x else r.nx
        if a < 0 or b >= limit_across:
            return
        if along_x:
            lim, ceil = self.limit[a:b + 1, :], self.ceiling[a:b + 1, :]
            cap, on_base = reach[a:b + 1, :], base[a:b + 1, :]
        else:
            lim, ceil = self.limit[:, a:b + 1].T, self.ceiling[:, a:b + 1].T
            cap, on_base = reach[:, a:b + 1].T, base[:, a:b + 1].T
        cap = cap.max(axis=0)
        tops = np.minimum(lim.min(axis=0), cap)
        keep = (np.isfinite(cap) & on_base.all(axis=0)
                & (tops >= s.base_height + s.min_wall))
        over = ceil.min(axis=0)
        for start, stop in _runs(keep):
            run = _Run(along_x, position, start, tops[start:stop].copy(), r.cell)
            self.runs.append(run)
            self.gaps.extend((over[start:stop] - run.tops).tolist())
            if along_x:
                self.cells[a:b + 1, start:stop] = True
            else:
                self.cells[start:stop, a:b + 1] = True


def _runs(keep: np.ndarray):
    """(start, stop) of each run of True values."""
    edges = np.diff(np.concatenate([[0], keep.astype(np.int8), [0]]))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


# ------------------------------------------------------------------ base

def _keep_supporting(base, wall_cells):
    """Base pieces (4-connected) that carry at least one wall."""
    labels, _ = ndimage.label(base, structure=_FOUR)
    used = np.unique(labels[wall_cells & base])
    return np.isin(labels, used[used > 0])


def _join(base, base_ok, wall_cells, raster: _Raster, s: GridSettings):
    """Join base pieces with strips `connector_width` wide; one at a time.

    A strip is the square dilation of a 4-connected path of cells whose whole
    square fits in `base_ok`, so every strip cell keeps the base's clearance.
    Returns (base, strips added, cells added).
    """
    labels, count = ndimage.label(base, structure=_FOUR)
    if count <= 1:
        return base, 0, 0
    square = _square((s.connector_width / 2) / raster.cell)
    fits = ndimage.binary_erosion(base_ok, structure=square, border_value=0)
    index = -np.ones(fits.shape, dtype=np.int64)
    nodes = np.flatnonzero(fits)
    index.reshape(-1)[nodes] = np.arange(len(nodes))
    graph = _grid_graph(fits, index)

    sizes = np.bincount(labels.reshape(-1), weights=wall_cells.reshape(-1).astype(float))
    joined = labels == int(np.argmax(sizes[1:]) + 1)
    strips = added = 0
    base = base.copy()
    while True:
        rest = base & ~joined
        if not rest.any():
            return base, strips, added
        reach_joined = ndimage.binary_dilation(joined, structure=square) & fits
        reach_rest = ndimage.binary_dilation(rest, structure=square) & fits
        sources = index[reach_joined]
        if not len(sources) or not reach_rest.any():
            raise GridError('support cannot be joined into one piece: no room for a '
                            f'{s.connector_width:g} mm strip next to a base piece')
        dist, pred, _ = dijkstra(graph, directed=False, indices=sources, unweighted=True,
                                 min_only=True, return_predecessors=True)
        targets = index[reach_rest]
        reachable = targets[np.isfinite(dist[targets])]
        if not len(reachable):
            raise GridError('support cannot be joined into one piece: no strip route '
                            'within the search area clears the model')
        node = int(reachable[np.argmin(dist[reachable])])
        path = np.zeros(base.shape, dtype=bool)
        while node >= 0:
            path.reshape(-1)[nodes[node]] = True
            node = int(pred[node])
        strip = ndimage.binary_dilation(path, structure=square)
        added += int((strip & ~base).sum())
        base |= strip
        strips += 1
        labels, _ = ndimage.label(base, structure=_FOUR)
        joined = np.isin(labels, np.unique(labels[joined]))


def _grid_graph(fits, index):
    """Undirected 4-neighbour graph over the `fits` cells."""
    rows, cols = [], []
    for a, b in ((index[:, :-1], index[:, 1:]), (index[:-1, :], index[1:, :])):
        both = (a >= 0) & (b >= 0)
        rows.append(a[both])
        cols.append(b[both])
    rows, cols = np.concatenate(rows), np.concatenate(cols)
    n = int(index.max()) + 1
    return coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)).tocsr()


def _remove_pinches(base, base_ok, wall_cells):
    """Fill or cut cells that touch only at a corner (not printable as one piece).

    A diagonal pair is filled with one of its two empty cells when clearance
    allows, else a non-wall cell of the pair is removed.
    """
    base = base.copy()
    for _ in range(100):
        a, b = base[:-1, :-1], base[1:, 1:]
        c, d = base[:-1, 1:], base[1:, :-1]
        main = a & b & ~c & ~d          # (j,i) and (j+1,i+1)
        anti = c & d & ~a & ~b          # (j,i+1) and (j+1,i)
        if not (main.any() or anti.any()):
            return base
        for jj, ii in zip(*np.nonzero(main)):
            _resolve(base, base_ok, wall_cells, (jj, ii + 1), (jj + 1, ii), (jj, ii), (jj + 1, ii + 1))
        for jj, ii in zip(*np.nonzero(anti)):
            _resolve(base, base_ok, wall_cells, (jj, ii), (jj + 1, ii + 1), (jj, ii + 1), (jj + 1, ii))
    raise GridError('base outline did not settle without corner-only contacts')


def _resolve(base, base_ok, wall_cells, empty1, empty2, full1, full2):
    if base[empty1] or base[empty2] or not (base[full1] and base[full2]):
        return
    for cell in (empty1, empty2):
        if base_ok[cell]:
            base[cell] = True
            return
    for cell in (full1, full2):
        if not wall_cells[cell]:
            base[cell] = False
            return
    raise GridError('two walls meet only at a corner and the gap between them cannot be filled')


# ----------------------------------------------------------------- solid

def _solid(base, walls: _Walls, raster: _Raster, s: GridSettings) -> manifold3d.Manifold:
    """Union of the base sheet and every wall run as one manifold solid."""
    c = raster.cell
    rects = []
    for j in range(raster.ny):
        for start, stop in _runs(base[j]):
            x_a, x_b = raster.x0 + start * c, raster.x0 + stop * c
            y_a, y_b = raster.y0 + j * c, raster.y0 + (j + 1) * c
            rects.append([(x_a, y_a), (x_b, y_a), (x_b, y_b), (x_a, y_b)])
    sheet = manifold3d.CrossSection(rects, manifold3d.FillRule.NonZero)
    parts = [sheet.extrude(s.base_height)]
    for run in walls.runs:
        parts.append(_wall(run, raster, s))
    if sum(p.num_tri() for p in parts) > MAX_TRIANGLES:
        raise GridError(f'grid mesh too large (> {MAX_TRIANGLES:,} triangles)')
    solid = manifold3d.Manifold.batch_boolean(parts, manifold3d.OpType.Add)
    if solid.status() != manifold3d.Error.NoError or solid.is_empty():
        raise GridError(f'grid solid could not be built ({solid.status()})')
    if len(solid.decompose()) != 1:
        raise GridError(f'grid solid is {len(solid.decompose())} pieces, not one')
    if not solid.volume() > 0:
        raise GridError('grid solid has no volume')
    return solid


def _wall(run: _Run, raster: _Raster, s: GridSettings) -> manifold3d.Manifold:
    """One wall run: a stepped profile in (along, z) extruded across `width`."""
    origin = raster.x0 if run.along_x else raster.y0
    u = origin + (run.start + np.arange(len(run.tops) + 1)) * run.cell
    # Merge equal neighbouring tops into one step.
    change = np.flatnonzero(np.diff(run.tops) != 0) + 1
    starts = np.concatenate([[0], change])
    stops = np.concatenate([change, [len(run.tops)]])
    profile = [(u[0], 0.0), (u[-1], 0.0)]
    for a, b in zip(starts[::-1], stops[::-1]):
        profile += [(u[b], run.tops[a]), (u[a], run.tops[a])]
    prism = manifold3d.CrossSection([profile]).extrude(s.width)
    lo = run.position - s.width / 2
    if run.along_x:     # (u, h, w) -> (u, lo + w, h)
        matrix = [[1, 0, 0, 0], [0, 0, 1, lo], [0, 1, 0, 0]]
    else:               # (u, h, w) -> (lo + w, u, h)
        matrix = [[0, 0, 1, lo], [1, 0, 0, 0], [0, 1, 0, 0]]
    return prism.transform(np.array(matrix, dtype=np.float64))
