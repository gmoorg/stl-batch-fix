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
    raster, covered, ceiling, limit, eligible, no_room, base_ok = _prepare(
        tris, candidates, cx0, cy0, ccell, max_height, s, s.margin + s.width)
    area = raster.cell * raster.cell
    cell = raster.cell
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

    solid = _solid(base, (_wall(run, raster, s) for run in walls.runs), raster, s)
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


def _prepare(tris, candidates, cx0, cy0, ccell, max_height, s: GridSettings, reach: float):
    """Raster and clearance shared by both layouts.

    `reach` is how far the generated base may spread past a candidate's
    lattice square (margin + width for the sheet, the foot for keel feet);
    joining strips and their routing room come on top of it.
    Returns (raster, covered, ceiling, limit, eligible, no_room, base_ok).
    """
    if not candidates.any():
        raise GridError('nothing in the band needs support')
    cell = max(s.cell, ccell)
    rows, cols = np.nonzero(candidates)
    lo = np.array([cx0 + cols.min() * ccell, cy0 + rows.min() * ccell])
    hi = np.array([cx0 + (cols.max() + 1) * ccell, cy0 + (rows.max() + 1) * ccell])
    # Every lattice square holding a candidate lies inside the snapped box,
    # so the pad below is measured from its walls, whatever the pitch.
    lo, hi = np.floor(lo / s.pitch) * s.pitch, np.ceil(hi / s.pitch) * s.pitch
    pad = reach + MAX_CLOSING + s.connector_width + ROUTE_PAD
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

    eligible = covered & (limit >= s.base_height + s.min_wall)
    no_room = covered & ~eligible
    if not eligible.any():
        raise GridError(f'no room under the candidates for base ({s.base_height:g}) + wall '
                        f'({s.min_wall:g}) + gap ({s.gap:g}) clear of the model')
    return raster, covered, ceiling, limit, eligible, no_room, limit >= s.base_height


@dataclass(frozen=True)
class KeelPatch:
    """How one candidate patch was supported.

    keel        True: a keel along `direction` with `ribs` across it;
                False: the regular world lattice, `reason` says why
    direction   keel direction in plan, degrees from +x in [0, 180) (nan
                for the lattice)
    area        the patch's candidate area, mm²
    centre      the patch's centroid (x, y), world mm
    """

    keel: bool
    direction: float
    ribs: int
    area: float
    centre: tuple[float, float]
    reason: str = ''


@dataclass(frozen=True)
class KeelSupport:
    """The keel-layout solid and what it covers (see GridSupport for the
    shared fields).  `foot` is the foot width; `base_area` is feet plus
    joining strips; `left_out_area` is eligible candidate dust (patches
    under min_area after grouping) given no wall of its own and farther than
    pitch/2 from every wall (part of `missed_area`)."""

    vertices: np.ndarray
    faces: np.ndarray
    patches: tuple[KeelPatch, ...]
    walls: int
    wall_length: float
    foot: float
    base_area: float
    base_pieces: int
    connectors: int
    connector_area: float
    candidate_area: float
    no_room_area: float
    eligible_area: float
    near_area: float
    missed_area: float
    left_out_area: float
    unbraced_runs: int
    unbraced_length: float
    gap_median: float
    gap_p95: float
    top_range: tuple[float, float]


def build_keel(tris: np.ndarray, candidates: np.ndarray, height: np.ndarray, cx0: float,
               cy0: float, ccell: float, max_height: float, settings: GridSettings,
               foot: float) -> KeelSupport:
    """Support oriented to the part: per elongated candidate patch one keel
    wall under the lowest line of its underside, ribs across it every pitch;
    other patches get the regular lattice.  Walls stand on feet `foot` wide
    instead of a sheet; joining strips keep the result one piece.

    height      basecheck's underside map on the candidates' grid (NaN = none);
                it orients the keels.  Clearance comes from `tris` as in build().
    """
    s = settings
    if not (math.isfinite(foot) and foot >= s.width):
        raise ValueError(f'foot ({foot!r}) must be at least the wall width ({s.width})')
    raster, covered, ceiling, limit, eligible, no_room, base_ok = _prepare(
        tris, candidates, cx0, cy0, ccell, max_height, s, foot)
    cell, area = raster.cell, raster.cell * raster.cell
    top_cap = max_height - s.gap

    # On real meshes the candidate map is speckled (slopes hover around the
    # support angle), so patches are grouped by closing gaps up to
    # KEEL_CLOSING; the closed shape plans the keel, the candidates in it
    # set what the walls must reach.
    # Undersides too low for base + wall + gap (around plate contacts) take
    # no wall whatever the layout; left in, they ring the contact and split
    # the band a keel follows.
    room = np.isfinite(height) & (height >= s.base_height + s.min_wall + s.gap)
    grouped = _close(candidates, KEEL_CLOSING / ccell) & room
    labels, count = ndimage.label(grouped, structure=_EIGHT)
    patches, owalls = [], []
    # Only patches that cannot take a keel get the lattice; candidate dust
    # (grouped patches under min_area) gets nothing — a whole lattice square
    # around a speck costs more than the speck needs — and is reported.
    lattice_cells = np.zeros(eligible.shape, dtype=bool)
    left_out = np.zeros(eligible.shape, dtype=bool)
    for index, where in enumerate(ndimage.find_objects(labels), start=1):
        shape = np.zeros(candidates.shape, dtype=bool)
        shape[where] = labels[where] == index
        mask = shape & candidates
        patch_area = float(mask.sum() * ccell * ccell)
        mine = _resample(mask, cx0, cy0, ccell, raster) & eligible
        if patch_area < s.min_area:
            left_out |= mine
            continue
        plan = _keel_plan(shape, height, cx0, cy0, ccell, s)
        centre = plan.centre
        if not plan.ok:
            patches.append(KeelPatch(False, math.nan, 0, patch_area, centre, plan.reason))
            lattice_cells |= mine
            continue
        if not mine.any():
            patches.append(KeelPatch(False, math.nan, 0, patch_area, centre,
                                     'no room for a wall under it'))
            continue
        jj, ii = np.nonzero(mine)
        rel = np.c_[raster.x0 + (ii + 0.5) * cell, raster.y0 + (jj + 0.5) * cell] - plan.origin
        cell_s, cell_lim = rel @ plan.u, np.minimum(limit[jj, ii], top_cap)
        built = []
        # Keel: each step capped by the patch's highest eligible limit within
        # pitch/2 along it (the stretch its ribs serve), so it never rises
        # above what that stretch needs.  A narrower window breaks the keel
        # into dashes on speckled maps (girl_130 Arm_Left, 0.25 mm: sliced
        # keel in pieces from Z 0.6 up).
        def keel_cap(step_s, half=s.pitch / 2):
            return _window_max(cell_s, cell_lim, step_s, half)
        built += _oriented(plan.point(plan.s0, plan.keel_t), plan.u, plan.s1 - plan.s0,
                           keel_cap, plan.s0, raster, limit, ceiling, base_ok, s)
        ribs = 0
        for rib_s, t0, t1 in plan.ribs:
            cap = _window_max(cell_s, cell_lim, np.array([rib_s]), s.pitch / 2)[0]
            runs = _oriented(plan.point(rib_s, t0), plan.v, t1 - t0,
                             lambda steps, c=cap: np.full(len(steps), c), 0.0,
                             raster, limit, ceiling, base_ok, s)
            ribs += bool(runs)
            built += runs
        owalls += built
        patches.append(KeelPatch(True, plan.degrees, ribs, patch_area, centre))

    # Lattice for the rest, standing on any cell with base clearance (feet
    # are added under it afterwards, as under the keels).
    walls = _Walls(raster, limit, ceiling, s)
    if lattice_cells.any():
        square, capmap = _squares(lattice_cells, limit, raster, s, max_height)
        walls.lattice(_reach(capmap, raster, s), base_ok)
        walls.fallback(lattice_cells, square, capmap, base_ok)
    if not walls.runs and not owalls:
        raise GridError('no wall fits under the candidates')

    wall_cells = walls.cells.copy()
    for run in owalls:
        wall_cells.reshape(-1)[run.cells] = True
    spread = _square(max(foot - s.width, 0.0) / 2 / cell)
    base = (ndimage.binary_dilation(wall_cells, structure=spread) & base_ok) | wall_cells
    base = _keep_supporting(base, wall_cells)
    pieces = int(ndimage.label(base, structure=_FOUR)[1])
    base, connectors, connector_cells = _join(base, base_ok, wall_cells, raster, s)
    base = _remove_pinches(base, base_ok, wall_cells)
    if ndimage.label(base, structure=_FOUR)[1] != 1:
        raise GridError('base could not be made one piece without diagonal-only contacts')

    solid = _solid(base, _keel_wall_solids(walls, owalls, raster, s), raster, s)
    mesh = solid.to_mesh64()
    footprints = ([_Footprint.of_run(run, raster, s.width) for run in walls.runs]
                  + [run.footprint(s.width) for run in owalls])
    unbraced = _unbraced(footprints)
    near = eligible & ndimage.binary_dilation(wall_cells, structure=_square(s.pitch / 2 / cell))
    gaps = np.concatenate([np.asarray(walls.gaps, dtype=float)]
                          + [run.gaps for run in owalls])
    gaps = gaps[np.isfinite(gaps)]
    tops = [r.tops for r in walls.runs] + [r.tops for r in owalls]
    return KeelSupport(
        vertices=np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3],
        faces=np.asarray(mesh.tri_verts, dtype=np.int64),
        patches=tuple(patches), walls=len(footprints),
        wall_length=float(sum(f.length for f in footprints)), foot=foot,
        base_area=float(base.sum() * area), base_pieces=pieces, connectors=connectors,
        connector_area=float(connector_cells * area),
        candidate_area=float(covered.sum() * area), no_room_area=float(no_room.sum() * area),
        eligible_area=float(eligible.sum() * area), near_area=float(near.sum() * area),
        missed_area=float((eligible & ~near).sum() * area),
        left_out_area=float((left_out & ~near).sum() * area),
        unbraced_runs=len(unbraced), unbraced_length=float(sum(f.length for f in unbraced)),
        gap_median=float(np.median(gaps)) if len(gaps) else math.nan,
        gap_p95=float(np.percentile(gaps, 95)) if len(gaps) else math.nan,
        top_range=(float(min(t.min() for t in tops)), float(max(t.max() for t in tops))))


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


# ------------------------------------------------------------------ keel

#: Length of the slices a keel patch is cut into along its direction, mm.
KEEL_BIN = 0.5
#: A patch is elongated enough for a keel when its principal second moments
#: differ at least this much (λ1/λ2; 4 = twice as long as wide).
KEEL_MIN_ELONGATION = 4.0
#: Share of slices that must hold one unbroken band with the keel inside it.
#: Provisional: girl_130's arms score 85-90 % (speckled maps, a band that
#: narrows at the ends), a torso strip 65 %.  Ribs go only on good slices.
KEEL_GOOD_SLICES = 0.8
#: Larger breaks across a slice than this many map cells split the band.
KEEL_MAX_BREAK = 2.5
#: Walls at least this far apart in direction count as crossing (bracing).
CROSSING_DEGREES = 20.0
#: Candidate cells closer than this are one patch for keel planning, mm.
KEEL_CLOSING = 1.0


def _close(mask: np.ndarray, radius_cells: float) -> np.ndarray:
    """Morphological closing by a disc (distance transforms: cost does not
    grow with the radius), united with the mask itself."""
    if not mask.any():
        return mask.copy()
    pad = int(math.ceil(radius_cells)) + 1
    padded = np.pad(mask, pad)
    grown = ndimage.distance_transform_edt(~padded) <= radius_cells
    closed = ndimage.distance_transform_edt(grown) > radius_cells
    return (closed[pad:-pad, pad:-pad]) | mask


@dataclass(frozen=True)
class _Plan:
    """A patch's keel frame: s along `u`, t along `v`, both from `origin`.

    ribs: (s, t0, t1) per rib — its position along the keel and its extent
    across.  `ok` False: lattice instead, `reason` says why.
    """

    ok: bool
    reason: str
    centre: tuple[float, float]
    origin: np.ndarray | None = None
    u: np.ndarray | None = None
    keel_t: float = 0.0
    s0: float = 0.0
    s1: float = 0.0
    ribs: tuple[tuple[float, float, float], ...] = ()

    @property
    def v(self) -> np.ndarray:
        return np.array([-self.u[1], self.u[0]])

    @property
    def degrees(self) -> float:
        return float(math.degrees(math.atan2(self.u[1], self.u[0])) % 180.0)

    def point(self, s: float, t: float) -> np.ndarray:
        return self.origin + s * self.u + t * self.v


def _keel_plan(mask, height, cx0, cy0, ccell, s: GridSettings) -> _Plan:
    """Keel direction, position and ribs for one candidate patch, or why not.

    The direction is the patch's principal axis, turned once by the slope of
    its lowest-point trace; the keel is the straight line at the trace's
    median offset.  Lowest points come from the sampled underside map, not
    from the conservative clearance bound (which errs low near obstacles).
    """
    rows, cols = np.nonzero(mask)
    points = np.c_[cx0 + (cols + 0.5) * ccell, cy0 + (rows + 0.5) * ccell]
    centre = points.mean(axis=0)
    label = (float(centre[0]), float(centre[1]))
    if len(points) * ccell * ccell < s.min_area:
        return _Plan(False, f'smaller than {s.min_area:g} mm²', label)
    h = height[rows, cols]
    if not np.all(np.isfinite(h)):
        return _Plan(False, 'underside height unknown', label)
    rel = points - centre
    moments, axes = np.linalg.eigh(rel.T @ rel / len(rel))
    if not moments[1] > 0:
        return _Plan(False, 'degenerate shape', label)
    elongation = moments[1] / max(moments[0], 1e-12)
    if elongation < KEEL_MIN_ELONGATION:
        return _Plan(False, f'not elongated (second moments {elongation:.1f}:1, '
                            f'keel needs {KEEL_MIN_ELONGATION:g}:1)', label)
    u = axes[:, 1]
    along, _, valley, _ = _slices(rel, h, u, ccell)
    slope = np.polyfit(along, valley, 1)[0] if len(along) > 1 else 0.0
    angle = math.atan2(u[1], u[0]) + math.atan(slope)
    u = np.array([math.cos(angle), math.sin(angle)])
    if u[0] < 0 or (u[0] == 0 and u[1] < 0):    # independent of eigh's sign
        u = -u
    along, _, valley, bands = _slices(rel, h, u, ccell)
    keel_t = float(np.median(valley))
    good = np.array([np.all(np.diff(b) <= KEEL_MAX_BREAK * ccell)
                     and b[0] - ccell <= keel_t <= b[-1] + ccell for b in bands])
    if good.mean() < KEEL_GOOD_SLICES:
        return _Plan(False, f'a straight keel stays inside one unbroken band in only '
                            f'{good.mean():.0%} of the patch length', label)
    s0, s1 = float(along.min() - KEEL_BIN / 2), float(along.max() + KEEL_BIN / 2)
    ends = rel @ u
    s0, s1 = float(min(s0, ends.min() - ccell / 2)), float(max(s1, ends.max() + ccell / 2))
    count = max(1, math.ceil((s1 - s0) / s.pitch))
    spacing = (s1 - s0) / count
    ribs = []
    for k in range(count):
        target = s0 + (k + 0.5) * spacing
        usable = np.flatnonzero(good & (np.abs(along - target) <= spacing / 2))
        if not len(usable):
            continue
        pick = usable[np.argmin(np.abs(along[usable] - target))]
        band = bands[pick]
        rib_s = target if abs(along[pick] - target) <= KEEL_BIN / 2 else float(along[pick])
        ribs.append((rib_s, float(min(band[0], keel_t) - ccell / 2),
                     float(max(band[-1], keel_t) + ccell / 2)))
    return _Plan(True, '', label, centre, u, keel_t, s0, s1, tuple(ribs))


def _slices(rel, h, u, ccell):
    """Cut a patch into KEEL_BIN slices along u.

    Returns per non-empty slice: its mean position along u, the offset across
    (along v) of its lowest underside point, and its sorted offsets across.
    Ties for the lowest point go to the one nearest the previous slice's.
    """
    v = np.array([-u[1], u[0]])
    along, across = rel @ u, rel @ v
    index = np.floor((along - along.min()) / KEEL_BIN).astype(np.int64)
    order = np.lexsort((across, index))
    index, along, across, h = index[order], along[order], across[order], h[order]
    starts = np.flatnonzero(np.r_[True, np.diff(index) != 0])
    stops = np.r_[starts[1:], len(index)]
    means, valley, bands, previous = [], [], [], None
    for a, b in zip(starts, stops):
        low = np.flatnonzero(h[a:b] <= h[a:b].min() + 1e-6) + a
        if previous is None:
            pick = low[len(low) // 2]
        else:
            pick = low[np.argmin(np.abs(across[low] - previous))]
        previous = across[pick]
        means.append(along[a:b].mean())
        valley.append(previous)
        bands.append(across[a:b])
    return np.array(means), across, np.array(valley), bands


def _window_max(positions, values, at, half):
    """Per `at`, the largest value whose position is within `half` (-inf if none)."""
    order = np.argsort(positions)
    positions, values = positions[order], values[order]
    lo = np.searchsorted(positions, at - half, side='left')
    hi = np.searchsorted(positions, at + half, side='right')
    return np.array([values[a:b].max() if b > a else -np.inf for a, b in zip(lo, hi)])


@dataclass(frozen=True)
class _ORun:
    """A kept stretch of an oriented wall: centre line p0 + along·u, stepped
    every `step`; `cells` are the raster cells its footprint touches."""

    p0: np.ndarray
    u: np.ndarray
    step: float
    tops: np.ndarray
    cells: np.ndarray
    gaps: np.ndarray

    @property
    def length(self) -> float:
        return len(self.tops) * self.step

    def footprint(self, width: float) -> '_Footprint':
        v = np.array([-self.u[1], self.u[0]]) * width / 2
        end = self.p0 + self.u * self.length
        return _Footprint(np.array([self.p0 - v, end - v, end + v, self.p0 + v]),
                          math.degrees(math.atan2(self.u[1], self.u[0])) % 180.0, self.length)

    def solid(self, width: float) -> manifold3d.Manifold:
        prism = _stepped_prism(np.arange(len(self.tops) + 1) * self.step, self.tops, width)
        ux, uy = self.u
        vx, vy = -uy, ux
        x0, y0 = self.p0 - np.array([vx, vy]) * width / 2
        # (along, h, w) -> p0 - v·width/2 + along·u + w·v, z = h
        return prism.transform(np.array([[ux, 0, vx, x0], [uy, 0, vy, y0], [0, 1, 0, 0]],
                                        dtype=np.float64))


def _oriented(start, u, length, cap, s_offset, raster: _Raster, limit, ceiling, base_ok,
              s: GridSettings) -> list[_ORun]:
    """The kept runs of one oriented wall from `start` along unit `u`.

    Each step (about one cell long) is capped by `cap(positions)` — positions
    measured as s_offset + distance along — and by the lowest `limit` of every
    raster cell its footprint rectangle touches (separating-axis test, so a
    touched corner counts).  A step is kept when all those cells have base
    clearance and its top clears base + min_wall.
    """
    c = raster.cell
    n = max(1, math.ceil(length / c))
    step = length / n
    u = np.asarray(u, dtype=np.float64)
    v = np.array([-u[1], u[0]])
    centres = np.asarray(start, dtype=np.float64) + np.outer((np.arange(n) + 0.5) * step, u)
    half_a, half_b, half_c = step / 2, s.width / 2, c / 2
    ex = half_a * abs(u[0]) + half_b * abs(v[0])
    ey = half_a * abs(u[1]) + half_b * abs(v[1])
    i0 = np.floor((centres[:, 0] - ex - raster.x0) / c).astype(np.int64)
    j0 = np.floor((centres[:, 1] - ey - raster.y0) / c).astype(np.int64)
    i1 = np.floor((centres[:, 0] + ex - raster.x0) / c).astype(np.int64)
    j1 = np.floor((centres[:, 1] + ey - raster.y0) / c).astype(np.int64)
    span_i, span_j = int((i1 - i0).max()) + 1, int((j1 - j0).max()) + 1
    di, dj = np.meshgrid(np.arange(span_i), np.arange(span_j), indexing='ij')
    ii = i0[:, None] + di.reshape(-1)[None, :]
    jj = j0[:, None] + dj.reshape(-1)[None, :]
    use = (ii <= i1[:, None]) & (jj <= j1[:, None])
    d = np.stack([raster.x0 + (ii + 0.5) * c - centres[:, :1],
                  raster.y0 + (jj + 0.5) * c - centres[:, 1:]], axis=-1)
    tol = 1e-9
    use &= np.abs(d[..., 0]) <= half_c + ex + tol
    use &= np.abs(d[..., 1]) <= half_c + ey + tol
    use &= np.abs(d @ u) <= half_a + half_c * (abs(u[0]) + abs(u[1])) + tol
    use &= np.abs(d @ v) <= half_b + half_c * (abs(v[0]) + abs(v[1])) + tol
    inside = (ii >= 0) & (jj >= 0) & (ii < raster.nx) & (jj < raster.ny)
    whole = np.all(inside | ~use, axis=1)
    flat = np.where(use & inside, jj * raster.nx + ii, 0)
    lim = np.where(use & inside, limit.reshape(-1)[flat], np.inf)
    ceil = np.where(use & inside, ceiling.reshape(-1)[flat], np.inf)
    ok_base = np.all(base_ok.reshape(-1)[flat] | ~use, axis=1)
    caps = np.asarray(cap(s_offset + (np.arange(n) + 0.5) * step), dtype=np.float64)
    tops = np.minimum(lim.min(axis=1), caps)
    keep = whole & ok_base & np.isfinite(caps) & np.isfinite(tops) & \
        (tops >= s.base_height + s.min_wall)
    out = []
    for a, b in _runs(keep):
        cells = np.unique(flat[a:b][use[a:b] & inside[a:b]])
        out.append(_ORun(np.asarray(start, dtype=np.float64) + u * a * step, u, step,
                         tops[a:b].copy(), cells, ceil[a:b].min(axis=1) - tops[a:b]))
    return out


@dataclass(frozen=True)
class _Footprint:
    """A wall run's plan rectangle (corners counter-clockwise), direction in
    degrees [0, 180) and length."""

    corners: np.ndarray
    degrees: float
    length: float

    @staticmethod
    def of_run(run: _Run, raster: _Raster, width: float) -> '_Footprint':
        x_lo, x_hi, y_lo, y_hi = run.box(raster, width)
        return _Footprint(np.array([[x_lo, y_lo], [x_hi, y_lo], [x_hi, y_hi], [x_lo, y_hi]]),
                          0.0 if run.along_x else 90.0, run.length)


def _unbraced(footprints: list[_Footprint]) -> list[_Footprint]:
    """Footprints overlapping (positive area) no footprint that crosses them."""
    if not footprints:
        return []
    boxes = np.array([np.r_[f.corners.min(axis=0), f.corners.max(axis=0)] for f in footprints])
    angles = np.array([f.degrees for f in footprints])
    shapes = [manifold3d.CrossSection([f.corners.tolist()], manifold3d.FillRule.NonZero)
              for f in footprints]
    out = []
    for k, f in enumerate(footprints):
        turn = np.abs(angles - angles[k]) % 180.0
        crossing = np.minimum(turn, 180.0 - turn) >= CROSSING_DEGREES
        near = (crossing & (boxes[:, 0] < boxes[k, 2]) & (boxes[:, 2] > boxes[k, 0])
                & (boxes[:, 1] < boxes[k, 3]) & (boxes[:, 3] > boxes[k, 1]))
        if not any((shapes[k] ^ shapes[m]).area() > 1e-9 for m in np.flatnonzero(near)):
            out.append(f)
    return out


def _keel_wall_solids(walls: _Walls, owalls: list[_ORun], raster: _Raster, s: GridSettings):
    for run in walls.runs:
        yield _wall(run, raster, s)
    for run in owalls:
        yield run.solid(s.width)


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

def _solid(base, walls, raster: _Raster, s: GridSettings) -> manifold3d.Manifold:
    """Union of the base (sheet or feet) and every wall solid as one manifold.

    `walls` is iterated only after the sheet exists: manifold3d numbers
    solids in creation order and the union's vertex order follows it.
    """
    c = raster.cell
    rects = []
    for j in range(raster.ny):
        for start, stop in _runs(base[j]):
            x_a, x_b = raster.x0 + start * c, raster.x0 + stop * c
            y_a, y_b = raster.y0 + j * c, raster.y0 + (j + 1) * c
            rects.append([(x_a, y_a), (x_b, y_a), (x_b, y_b), (x_a, y_b)])
    sheet = manifold3d.CrossSection(rects, manifold3d.FillRule.NonZero)
    parts = [sheet.extrude(s.base_height)] + list(walls)
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


def _stepped_prism(u: np.ndarray, tops: np.ndarray, width: float) -> manifold3d.Manifold:
    """Profile in (along, z) — step k spans u[k]..u[k+1] at tops[k], equal
    neighbouring tops merged — extruded `width` along the third axis."""
    change = np.flatnonzero(np.diff(tops) != 0) + 1
    starts = np.concatenate([[0], change])
    stops = np.concatenate([change, [len(tops)]])
    profile = [(u[0], 0.0), (u[-1], 0.0)]
    for a, b in zip(starts[::-1], stops[::-1]):
        profile += [(u[b], tops[a]), (u[a], tops[a])]
    return manifold3d.CrossSection([profile]).extrude(width)


def _wall(run: _Run, raster: _Raster, s: GridSettings) -> manifold3d.Manifold:
    """One wall run: a stepped profile in (along, z) extruded across `width`."""
    origin = raster.x0 if run.along_x else raster.y0
    u = origin + (run.start + np.arange(len(run.tops) + 1)) * run.cell
    prism = _stepped_prism(u, run.tops, s.width)
    lo = run.position - s.width / 2
    if run.along_x:     # (u, h, w) -> (u, lo + w, h)
        matrix = [[1, 0, 0, 0], [0, 0, 1, lo], [0, 1, 0, 0]]
    else:               # (u, h, w) -> (lo + w, u, h)
        matrix = [[0, 0, 1, lo], [1, 0, 0, 0], [0, 1, 0, 0]]
    return prism.transform(np.array(matrix, dtype=np.float64))
