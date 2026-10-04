"""Predict base-layer print risks from placed geometry — no slicer involved.

Input is one instance's printable parts in world millimetres with the plate at
z = 0 (see `bambu3mf`).  Everything here is a PREDICTION from geometry and a
handful of settings; the thresholds are provisional until checked against
sliced output.

Three checks:

A. Uneven base.  How much of the near-plate footprint actually lands in
   layer 1, how fragmented that contact is, how rough the underside is, and
   (optionally) how far the instance would have to be lowered into the plate
   for most of its footprint to reach layer 1.
B. Unsupportable near-plate undersides.  Layer 1 is the slice at
   `first_layer / 2`; anything higher misses it.  A support needs at least a
   first layer plus `support_top_z_distance` of room, so an underside lower
   than `first_layer + support_gap` cannot be supported either.  Material in
   that band, shallower than the support threshold angle, is printed over
   air.  Only clearance to the PLATE is measured: undersides that sit above
   other model geometry are outside this check's scope.
C. Shallow near-horizontal surfaces around the foot, which slice into visible
   banding.  Face-based, so a surface hidden above a lower one in XY is still
   seen.  Informational: it never makes an instance a risk on its own.

How the plate is modelled.  Bambu discards material below z = 0, so every
triangle is clipped there.  The lowest remaining surface over each XY cell
(the "underside map") is where material starts in that column — except where
a part is cut by the plate, which a per-part crossing parity detects: an odd
number of surface crossings below z = 0 means z = 0 is inside that solid, so
the column has material from the plate up.  Parity is computed per part and
the cut masks are unioned (a shared count would XOR overlapping solids); it is
only meaningful for closed parts, so a sunk open part makes the analysis
incomplete.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

#: Largest grid an instance may use before the cell is coarsened.
MAX_CELLS = 25_000_000
#: Rasterisation tile: triangles up to this many cells across are processed
#: together by looping over in-box offsets; bigger ones one at a time.
_SMALL = (4, 8, 16, 32)
#: Cell centres are nudged by this fraction of a cell so that grid-aligned
#: edges of axis-aligned models never pass exactly through a sample point,
#: where a shared edge would be counted by both triangles (parity) or none.
_JITTER = (0.000123, 0.000271)


@dataclass(frozen=True)
class Thresholds:
    """Everything the checks compare against, in millimetres and degrees.

    first_layer         initial_layer_print_height
    layer_height        layer_height; sets the ripple RMS cutoff default
    support_gap         support_top_z_distance
    support_angle       support_threshold_angle: undersides shallower than
                        this (from horizontal) are overhangs
    supports_enabled    automatic supports will be generated
    cell                underside-map resolution
    foot_height         height of the band examined by check C (and by B when
                        supports are off)
    shallow_angle       C: faces within this of horizontal...
    flat_angle          ...but not within this — truly flat faces slice cleanly
    min_region_area     smaller regions are not reported (mm²)
    min_contact_fraction  A: risk below this fraction of the footprint
    max_extra_islands   A: risk when layer-1 contact splits into more than
                        this many islands beyond one per footprint region —
                        the signature of ripples crossing the slice plane
    ripple_sigma        A: smoothing radius for the (informational) ripple
                        RMS; it also measures curvature on this scale, which
                        is why RMS alone is not used to flag a risk
    sink_quantile       A: the suggested sink brings this fraction into layer 1
    """

    first_layer: float = 0.2
    layer_height: float = 0.2
    support_gap: float = 0.2
    support_angle: float = 30.0
    supports_enabled: bool = True
    cell: float = 0.05
    foot_height: float = 3.0
    shallow_angle: float = 10.0
    flat_angle: float = 0.5
    min_region_area: float = 0.5
    min_contact_fraction: float = 0.9
    max_extra_islands: int = 2
    ripple_sigma: float = 1.0
    sink_quantile: float = 0.95

    def __post_init__(self):
        positive = ('first_layer', 'layer_height', 'cell', 'foot_height', 'ripple_sigma')
        for name in positive:
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f'{name} must be a positive number, got {value!r}')
        if not (isinstance(self.max_extra_islands, int) and self.max_extra_islands >= 0):
            raise ValueError(f'max_extra_islands must be a whole number >= 0, '
                             f'got {self.max_extra_islands!r}')
        for name in ('support_gap', 'min_region_area'):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f'{name} must be zero or positive, got {value!r}')
        for name in ('support_angle', 'shallow_angle', 'flat_angle'):
            value = getattr(self, name)
            if not (math.isfinite(value) and 0 <= value < 90):
                raise ValueError(f'{name} must be in [0, 90) degrees, got {value!r}')
        for name in ('min_contact_fraction', 'sink_quantile'):
            value = getattr(self, name)
            if not (math.isfinite(value) and 0 <= value <= 1):
                raise ValueError(f'{name} must be in [0, 1], got {value!r}')
        if self.flat_angle >= self.shallow_angle:
            raise ValueError('flat_angle must be smaller than shallow_angle')

    @property
    def contact_z(self) -> float:
        """Height of the layer-1 slice plane."""
        return self.first_layer / 2

    @property
    def unsupportable_top(self) -> float:
        """Below this an underside has no room for a support layer."""
        return self.first_layer + self.support_gap


@dataclass(frozen=True)
class Region:
    """A connected patch of cells or faces a check flagged.

    area        projected area, mm²
    z_min/z_max height range above the plate
    centroid    world XY
    offset      centroid relative to the instance's bounding-box minimum XY,
                which is easier to find on the model than plate coordinates
    reach       B only: distance from the nearest layer-1 contact (mm), a
                proxy for how far the slicer must span — None when there is
                no contact at all
    facing      C only: 'up' or 'down' by the faces' geometric normals, so it
                flips on a mesh with reversed winding
    """

    area: float
    z_min: float
    z_max: float
    centroid: tuple[float, float]
    offset: tuple[float, float]
    reach: float | None = None
    facing: str | None = None


@dataclass(frozen=True)
class BaseReport:
    """Check A's measurements for one instance."""

    footprint_area: float
    contact_area: float
    contact_islands: int
    footprint_regions: int
    largest_island: float
    roughness: float
    ripple_rms: float | None
    sink: float

    @property
    def extra_islands(self) -> int:
        """Contact islands beyond one per footprint region."""
        return max(0, self.contact_islands - self.footprint_regions)

    @property
    def contact_fraction(self) -> float:
        return self.contact_area / self.footprint_area if self.footprint_area else 0.0


@dataclass(frozen=True)
class Result:
    base: BaseReport | None
    unsupportable: tuple[Region, ...]
    unsupported_overhangs: tuple[Region, ...]
    shallow: tuple[Region, ...]
    risks: tuple[str, ...]
    incomplete: tuple[str, ...]
    notes: tuple[str, ...]
    cell: float
    min_z: float
    grid: 'Grid | None' = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Grid:
    """The underside map, kept for plotting.  NaN = no material below foot_height."""

    x0: float
    y0: float
    cell: float
    height: np.ndarray
    contact: np.ndarray
    flagged: np.ndarray


# ---------------------------------------------------------------- clipping

def clip_above(tris: np.ndarray, z: float, index: np.ndarray | None = None):
    """The part of each triangle with height >= z, as triangles.

    tris is (n, 3, 3).  A triangle with one corner below z becomes a quad
    (two triangles), with two corners below a smaller triangle.  Returns the
    pieces and, for each, the index of the triangle it came from.
    """
    if index is None:
        index = np.arange(len(tris))
    below = tris[:, :, 2] < z
    count = below.sum(axis=1)
    keep = [tris[count == 0]]
    keep_index = [index[count == 0]]

    one = count == 1
    if one.any():
        t, i = tris[one], index[one]
        # Rotate so the lone low corner is first.
        k = np.argmax(below[one], axis=1)
        a, b, c = (t[np.arange(len(t)), (k + s) % 3] for s in range(3))
        ab = _cut(a, b, z)
        ac = _cut(a, c, z)
        keep += [np.stack([ab, b, c], axis=1), np.stack([ab, c, ac], axis=1)]
        keep_index += [i, i]

    two = count == 2
    if two.any():
        t, i = tris[two], index[two]
        k = np.argmin(below[two], axis=1)           # the lone high corner
        a, b, c = (t[np.arange(len(t)), (k + s) % 3] for s in range(3))
        keep.append(np.stack([a, _cut(a, b, z), _cut(a, c, z)], axis=1))
        keep_index.append(i)
    return np.concatenate(keep), np.concatenate(keep_index)


def clip_below(tris: np.ndarray, z: float, index: np.ndarray | None = None):
    """The part of each triangle with height <= z (mirror of `clip_above`)."""
    mirrored = tris * np.array([1.0, 1.0, -1.0])
    pieces, source = clip_above(mirrored, -z, index)
    return pieces * np.array([1.0, 1.0, -1.0]), source


def _cut(p: np.ndarray, q: np.ndarray, z: float) -> np.ndarray:
    """Where segment p->q crosses height z (p and q are on opposite sides)."""
    t = (z - p[:, 2]) / (q[:, 2] - p[:, 2])
    point = p + (q - p) * t[:, None]
    point[:, 2] = z
    return point


# ----------------------------------------------------------- rasterisation

@dataclass(frozen=True)
class _Lattice:
    x0: float
    y0: float
    cell: float
    nx: int
    ny: int

    def centres(self, i, j):
        return (self.x0 + (i + 0.5 + _JITTER[0]) * self.cell,
                self.y0 + (j + 0.5 + _JITTER[1]) * self.cell)


def _rasterise(tris: np.ndarray, lattice: _Lattice, out: np.ndarray, mode: str) -> None:
    """Sample every triangle at the cell centres it covers.

    mode 'min'    out = min(out, z of the triangle at the centre)
    mode 'count'  out += 1 for each covering triangle

    Triangles vertical in XY cover no area and are skipped.  A triangle too
    small to contain any centre contributes nothing: on a tiling surface its
    neighbours cover the centres around it.
    """
    if not len(tris):
        return
    x, y, z = tris[:, :, 0], tris[:, :, 1], tris[:, :, 2]
    det = (y[:, 1] - y[:, 2]) * (x[:, 0] - x[:, 2]) + (x[:, 2] - x[:, 1]) * (y[:, 0] - y[:, 2])
    scale = np.maximum(np.ptp(x, axis=1), np.ptp(y, axis=1)) ** 2
    usable = np.abs(det) > 1e-12 * np.maximum(scale, 1e-30)
    tris, x, y, z, det = tris[usable], x[usable], y[usable], z[usable], det[usable]
    if not len(tris):
        return
    jx, jy = 0.5 + _JITTER[0], 0.5 + _JITTER[1]
    i0 = np.ceil((x.min(axis=1) - lattice.x0) / lattice.cell - jx).astype(np.int64)
    i1 = np.floor((x.max(axis=1) - lattice.x0) / lattice.cell - jx).astype(np.int64)
    j0 = np.ceil((y.min(axis=1) - lattice.y0) / lattice.cell - jy).astype(np.int64)
    j1 = np.floor((y.max(axis=1) - lattice.y0) / lattice.cell - jy).astype(np.int64)
    i0, j0 = np.maximum(i0, 0), np.maximum(j0, 0)
    i1, j1 = np.minimum(i1, lattice.nx - 1), np.minimum(j1, lattice.ny - 1)
    span = np.maximum(i1 - i0 + 1, j1 - j0 + 1)
    live = (i1 >= i0) & (j1 >= j0)

    def sample(sel, ii, jj):
        """Test centres (ii, jj) against triangles `sel`.

        `sel` is an array the same length as ii/jj (one triangle per centre),
        or a single triangle index tested against every centre — broadcast,
        not repeated, since one large triangle can cover millions of cells.
        """
        px, py = lattice.centres(ii, jj)
        if np.ndim(sel) == 0:
            sel = slice(sel, sel + 1)
        xs, ys, zs, d = x[sel], y[sel], z[sel], det[sel]
        w0 = ((ys[:, 1] - ys[:, 2]) * (px - xs[:, 2]) + (xs[:, 2] - xs[:, 1]) * (py - ys[:, 2])) / d
        w1 = ((ys[:, 2] - ys[:, 0]) * (px - xs[:, 2]) + (xs[:, 0] - xs[:, 2]) * (py - ys[:, 2])) / d
        w2 = 1.0 - w0 - w1
        inside = (w0 > 0) & (w1 > 0) & (w2 > 0)
        if not inside.any():
            return
        flat = jj[inside] * lattice.nx + ii[inside]
        if mode == 'min':
            value = w0 * zs[:, 0] + w1 * zs[:, 1] + w2 * zs[:, 2]
            np.minimum.at(out.reshape(-1), flat, value[inside])
        else:
            np.add.at(out.reshape(-1), flat, 1)

    lower = 0
    for size in _SMALL:
        group = np.flatnonzero(live & (span > lower) & (span <= size))
        lower = size
        for start in range(0, len(group), 200_000):
            chunk = group[start:start + 200_000]
            for di in range(size):
                for dj in range(size):
                    sel = chunk[(i0[chunk] + di <= i1[chunk]) & (j0[chunk] + dj <= j1[chunk])]
                    if len(sel):
                        sample(sel, i0[sel] + di, j0[sel] + dj)

    for t in np.flatnonzero(live & (span > _SMALL[-1])):
        rows = np.arange(j0[t], j1[t] + 1)
        cols = np.arange(i0[t], i1[t] + 1)
        step = max(1, 2_000_000 // len(cols))
        for r in range(0, len(rows), step):
            jj, ii = np.meshgrid(rows[r:r + step], cols, indexing='ij')
            ii, jj = ii.reshape(-1), jj.reshape(-1)
            sample(t, ii, jj)


def _closed(faces: np.ndarray) -> bool:
    """Every edge used by exactly two faces."""
    if not len(faces):
        return False
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return bool(np.all(counts == 2))


# ------------------------------------------------------------------ checks

def check(parts, thresholds: Thresholds, keep_grid: bool = False) -> Result:
    """Run checks A, B and C on one instance.

    parts is a sequence of (vertices, faces) pairs: the instance's printable
    parts in world millimetres.
    """
    th = thresholds
    notes, incomplete = [], []
    soups = []
    dropped = 0
    for vertices, faces in parts:
        tris = np.asarray(vertices, dtype=np.float64)[np.asarray(faces)]
        finite = np.isfinite(tris).all(axis=(1, 2))
        if not finite.all():
            dropped += int((~finite).sum())
        soups.append((tris[finite], faces[finite], np.asarray(vertices)))
    if dropped:
        incomplete.append(f'{dropped} triangles with non-finite coordinates were ignored')
    if not soups or not any(len(s[0]) for s in soups):
        return Result(None, (), (), (), (), ('no printable geometry',), (), th.cell, math.nan)

    every = np.concatenate([s[0] for s in soups])
    min_z = float(every[:, :, 2].min())
    if min_z > 0.01:
        notes.append(f'instance floats {min_z:.3f} mm above the plate')

    # A and B need everything up to the unsupportable band whatever the C
    # band is set to, or a small foot_height would hide their risks.
    top = max(th.foot_height, th.unsupportable_top)
    band = every[every[:, :, 2].min(axis=1) < top]
    band, _ = clip_above(band, 0.0) if len(band) else (band, None)
    if not len(band):
        return Result(None, (), (), (), ('no plate contact: nothing within '
                                         f'{top:g} mm of the plate',),
                      tuple(incomplete), tuple(notes), th.cell, min_z)

    lo = band[:, :, :2].reshape(-1, 2).min(axis=0)
    hi = band[:, :, :2].reshape(-1, 2).max(axis=0)
    cell = th.cell
    extent = np.maximum(hi - lo, cell)
    if (extent[0] / cell) * (extent[1] / cell) > MAX_CELLS:
        cell = float(math.sqrt(extent[0] * extent[1] / MAX_CELLS)) * 1.001
        notes.append(f'grid coarsened to {cell:.3f} mm cells to stay under {MAX_CELLS:,} cells')
    lattice = _Lattice(float(lo[0]), float(lo[1]), cell,
                       int(math.ceil(extent[0] / cell)) + 1, int(math.ceil(extent[1] / cell)) + 1)

    height = np.full((lattice.ny, lattice.nx), np.inf)
    _rasterise(band, lattice, height, 'min')

    cut = np.zeros(height.shape, dtype=bool)
    if min_z < 0:
        for tris, faces, _ in soups:
            sunk = tris[tris[:, :, 2].min(axis=1) < 0]
            if not len(sunk):
                continue
            below, _ = clip_below(sunk, 0.0)
            count = np.zeros(height.shape, dtype=np.int32)
            _rasterise(below, lattice, count, 'count')
            if _closed(faces):
                cut |= (count % 2) == 1
            else:
                # Parity means nothing on an open surface.  Treating every
                # column with surface below the plate as cut is right for the
                # usual case (a base pushed into the plate) and wrong for a
                # sunk piece under an elevated one, hence incomplete.
                cut |= count > 0
                incomplete.append('a part is sunk below the plate but is not a closed '
                                  'solid; where the plate cuts it is approximated')

    height[cut] = 0.0
    height[height >= top] = np.inf
    valid = np.isfinite(height)
    height = np.where(valid, np.maximum(height, 0.0), np.nan)

    slope = _slope(height, valid, cell)
    shallow_under = valid & (slope < th.support_angle)
    contact = valid & (height <= th.contact_z)
    near = valid & (height < th.unsupportable_top)
    footprint = contact | (near & shallow_under)
    gap = footprint & ~contact

    bbox_min = (float(band[:, :, 0].min()), float(band[:, :, 1].min()))
    risks = []

    base = None
    if footprint.any():
        base = _base_report(height, footprint, contact, cell, th)
        if base.contact_area == 0:
            risks.append('no layer-1 contact: no part of the base reaches the first-layer slice')
        elif base.contact_fraction < th.min_contact_fraction:
            risks.append(f'uneven base: only {base.contact_fraction:.0%} of the near-plate '
                         f'footprint is in layer 1')
        if base.extra_islands > th.max_extra_islands:
            risks.append(f'rippled base: layer-1 contact is broken into '
                         f'{base.contact_islands} islands over {base.footprint_regions} '
                         f'footprint region(s)')
    else:
        risks.append('no layer-1 contact: no near-plate footprint')

    unsupportable = _regions(gap, height, contact, lattice, bbox_min, th.min_region_area)
    if unsupportable:
        risks.append(f'{len(unsupportable)} underside region(s) between '
                     f'{th.contact_z:g} and {th.unsupportable_top:g} mm: too low for '
                     f'support, above layer 1')

    overhangs = ()
    if not th.supports_enabled:
        free = valid & shallow_under & (height >= th.unsupportable_top)
        overhangs = _regions(free, height, contact, lattice, bbox_min, th.min_region_area)
        if overhangs:
            risks.append(f'{len(overhangs)} near-plate overhang region(s) and supports are off')

    shallow = _shallow_faces(soups, th, bbox_min)

    grid = None
    if keep_grid:
        grid = Grid(lattice.x0, lattice.y0, cell, height, contact, gap)
    return Result(base, unsupportable, overhangs, shallow, tuple(risks),
                  tuple(incomplete), tuple(notes), cell, min_z, grid)


def _slope(height: np.ndarray, valid: np.ndarray, cell: float) -> np.ndarray:
    """Underside angle from horizontal, degrees, from valid neighbours only.

    Central differences where both neighbours exist, one-sided where one
    does.  A cell with no valid neighbour along an axis has no slope along it
    (0); a cell with none along either is treated as flat, which flags rather
    than excuses it.
    """
    filled = np.where(valid, height, 0.0)

    def derivative(axis):
        fwd = np.roll(filled, -1, axis)
        back = np.roll(filled, 1, axis)
        fwd_ok = np.roll(valid, -1, axis)
        back_ok = np.roll(valid, 1, axis)
        edge = [slice(None)] * 2
        edge[axis] = -1
        fwd_ok[tuple(edge)] = False
        edge[axis] = 0
        back_ok[tuple(edge)] = False
        d = np.zeros_like(filled)
        both = fwd_ok & back_ok
        d[both] = (fwd[both] - back[both]) / (2 * cell)
        only_f = fwd_ok & ~back_ok
        d[only_f] = (fwd[only_f] - filled[only_f]) / cell
        only_b = back_ok & ~fwd_ok
        d[only_b] = (filled[only_b] - back[only_b]) / cell
        return d

    gradient = np.hypot(derivative(0), derivative(1))
    return np.degrees(np.arctan(gradient))


def _base_report(height, footprint, contact, cell, th) -> BaseReport:
    cell_area = cell * cell
    labels, islands = ndimage.label(contact, structure=np.ones((3, 3)))
    sizes = np.bincount(labels.reshape(-1))[1:] if islands else np.zeros(0)
    regions, count = ndimage.label(footprint, structure=np.ones((3, 3)))
    # Only footprint regions that hold some contact can be split by ripples.
    touching = len(np.unique(regions[contact])) if islands else 0
    values = height[footprint]
    # Normalised convolution: smooth only over the footprint, so missing
    # cells neither drag the mean to zero nor spread NaN.
    sigma = th.ripple_sigma / cell
    weight = ndimage.gaussian_filter(footprint.astype(float), sigma, mode='constant')
    smooth = ndimage.gaussian_filter(np.where(footprint, height, 0.0), sigma, mode='constant')
    smooth = np.divide(smooth, weight, out=np.zeros_like(smooth), where=weight > 1e-9)
    ripple = float(np.sqrt(np.mean((height[footprint] - smooth[footprint]) ** 2)))
    if footprint.sum() * cell_area < 4 * math.pi * th.ripple_sigma ** 2:
        ripple = None                       # smaller than the smoothing kernel
    sink = max(0.0, float(np.quantile(values, th.sink_quantile)) - th.contact_z)
    return BaseReport(footprint_area=float(footprint.sum() * cell_area),
                      contact_area=float(contact.sum() * cell_area),
                      contact_islands=int(islands),
                      footprint_regions=touching,
                      largest_island=float(sizes.max() * cell_area) if islands else 0.0,
                      roughness=float(values.std()),
                      ripple_rms=ripple,
                      sink=sink)


def _regions(mask, height, contact, lattice, bbox_min, min_area) -> tuple[Region, ...]:
    if not mask.any():
        return ()
    labels, count = ndimage.label(mask, structure=np.ones((3, 3)))
    reach_map = None
    if contact.any():
        reach_map = ndimage.distance_transform_edt(~contact) * lattice.cell
    cell_area = lattice.cell ** 2
    regions = []
    for index, where in enumerate(ndimage.find_objects(labels), start=1):
        member = labels[where] == index
        area = float(member.sum() * cell_area)
        if area < min_area:
            continue
        rows, cols = np.nonzero(member)
        rows, cols = rows + where[0].start, cols + where[1].start
        cx = float(lattice.x0 + (cols.mean() + 0.5) * lattice.cell)
        cy = float(lattice.y0 + (rows.mean() + 0.5) * lattice.cell)
        heights = height[rows, cols]
        reach = float(reach_map[rows, cols].max()) if reach_map is not None else None
        regions.append(Region(area, float(heights.min()), float(heights.max()),
                              (cx, cy), (cx - bbox_min[0], cy - bbox_min[1]), reach))
    regions.sort(key=lambda r: -r.area)
    return tuple(regions)


def _shallow_faces(soups, th, bbox_min) -> tuple[Region, ...]:
    """Check C: near-horizontal faces in the foot band, clipped to the band."""
    lo, hi = th.contact_z, th.foot_height
    shallow_cos = math.cos(math.radians(th.shallow_angle))
    flat_cos = math.cos(math.radians(th.flat_angle))
    regions = []
    for tris, faces, vertices in soups:
        if not len(tris):
            continue
        zmin, zmax = tris[:, :, 2].min(axis=1), tris[:, :, 2].max(axis=1)
        normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        length = np.linalg.norm(normal, axis=1)
        with np.errstate(invalid='ignore', divide='ignore'):
            nz = normal[:, 2] / length
        tilt = np.abs(nz)
        pick = np.flatnonzero((length > 0) & (zmax > lo) & (zmin < hi)
                              & (tilt >= shallow_cos) & (tilt < flat_cos))
        if not len(pick):
            continue
        pieces, source = clip_above(tris[pick], lo, pick)
        if len(pieces):
            pieces, source = clip_below(pieces, hi, source)
        if not len(pieces):
            continue
        u = pieces[:, 1, :2] - pieces[:, 0, :2]
        v = pieces[:, 2, :2] - pieces[:, 0, :2]
        area = 0.5 * np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0])
        face_area = np.bincount(source, weights=area, minlength=len(tris))
        # Connected through shared vertices.
        chosen = np.unique(source)
        corner = faces[chosen].reshape(-1)
        owner = np.repeat(np.arange(len(chosen)), 3)
        _, vertex_label = np.unique(corner, return_inverse=True)
        incidence = coo_matrix((np.ones(len(owner)), (owner, vertex_label)),
                               shape=(len(chosen), vertex_label.max() + 1)).tocsr()
        _, label = connected_components(incidence @ incidence.T, directed=False)
        centroid = pieces[:, :, :2].mean(axis=1)
        for group in np.unique(label):
            members = chosen[label == group]
            part_mask = np.isin(source, members)
            total = float(face_area[members].sum())
            if total < th.min_region_area:
                continue
            weights = area[part_mask]
            cx, cy = (centroid[part_mask] * weights[:, None]).sum(axis=0) / max(weights.sum(), 1e-30)
            zs = pieces[part_mask][:, :, 2]
            up = float((face_area[members] * (nz[members] > 0)).sum())
            regions.append(Region(total, float(zs.min()), float(zs.max()),
                                  (float(cx), float(cy)),
                                  (float(cx) - bbox_min[0], float(cy) - bbox_min[1]),
                                  facing='up' if up >= total / 2 else 'down'))
    regions.sort(key=lambda r: -r.area)
    return tuple(regions)
