"""Thin breakaway ribs under near-plate undersides — geometry only.

Bambu cannot place support under an underside lower than first layer +
support_top_z_distance (see basecheck check B), so that material is printed
over air.  These ribs are modelled support instead: single-line walls
standing on the plate and rising to the underside, added to the object as an
ordinary part so the slicer prints them with it.  They touch the model along
a strip one rib wide, which is meant to snap off — whether it does is a
property of the print, not of this geometry.

All coordinates are world millimetres with the plate at z = 0; the caller
converts to object coordinates.

Rib top.  A rib is `width` wide, and the underside can slope across it, so
the top at each sample is the HIGHEST underside height among the cells across
the rib's width, plus `overlap`.  Every part of the top then reaches the
model; the low side pokes up to (slope × width + overlap) into it, which is
harmless where the model is solid above its underside (the part is unioned
with it).  Where those heights differ by more than `jump` — a step in the
underside, e.g. a recess wall — the sample is dropped and the rib split
there, rather than raising one side of the rib far into the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage


@dataclass(frozen=True)
class RibSettings:
    """pitch     distance between ribs, mm
    width       rib wall thickness, mm — about one extrusion line
    overlap     how far each rib top rises into the model, mm
    jump        largest underside step allowed across one rib sample, mm
    min_area    regions smaller than this get no ribs, mm²
    """

    pitch: float = 2.0
    width: float = 0.42
    overlap: float = 0.05
    jump: float = 0.2
    min_area: float = 0.5

    def __post_init__(self):
        for name in ('pitch', 'width', 'jump'):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value > 0):
                raise ValueError(f'{name} must be a positive number, got {value!r}')
        for name in ('overlap', 'min_area'):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value) and value >= 0):
                raise ValueError(f'{name} must be zero or positive, got {value!r}')
        if self.width >= self.pitch:
            raise ValueError(f'width ({self.width}) must be smaller than pitch ({self.pitch}): '
                             'ribs that touch are a solid block, not breakaway support')


@dataclass(frozen=True)
class Ribs:
    vertices: np.ndarray        # (n, 3) float64, world mm
    faces: np.ndarray           # (m, 3) int64, outward winding
    walls: int                  # closed wall solids
    regions: int                # regions that received at least one wall

    @property
    def empty(self) -> bool:
        return self.walls == 0


def ribs(height: np.ndarray, mask: np.ndarray, x0: float, y0: float, cell: float,
         settings: RibSettings) -> Ribs:
    """Walls under every `mask` region, from z = 0 to the underside.

    height  underside map, rows = y, columns = x (NaN = nothing there)
    mask    cells that need support
    x0, y0  world position of the grid's lower-left corner; cell = cell size
    """
    labels, _ = ndimage.label(mask, structure=np.ones((3, 3)))
    vertices, faces = [], []
    walls = covered = 0
    offset = 0
    for index, where in enumerate(ndimage.find_objects(labels), start=1):
        member = labels == index
        if member.sum() * cell * cell < settings.min_area:
            continue
        rows, cols = where[0], where[1]
        span_x = (cols.stop - cols.start) * cell
        span_y = (rows.stop - rows.start) * cell
        along_x = span_x >= span_y
        if along_x:
            lo, hi = y0 + rows.start * cell, y0 + rows.stop * cell
        else:
            lo, hi = x0 + cols.start * cell, x0 + cols.stop * cell
        span = hi - lo
        position = lo + min(settings.pitch / 2, span / 2)
        before = walls
        while position <= hi:
            for v, f in _walls_on_line(height, member, x0, y0, cell, settings,
                                       along_x, position):
                vertices.append(v)
                faces.append(f + offset)
                offset += len(v)
                walls += 1
            position += settings.pitch
        covered += walls > before
    if not vertices:
        return Ribs(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), 0, 0)
    return Ribs(np.concatenate(vertices), np.concatenate(faces), walls, covered)


def _walls_on_line(height, member, x0, y0, cell, settings, along_x, position):
    """Closed walls along one rib line through one region."""
    half = settings.width / 2
    if along_x:
        line_index = int(math.floor((position - y0) / cell))
        across_origin, along_origin = y0, x0
        n_along = height.shape[1]
        line_index = min(max(line_index, 0), height.shape[0] - 1)
        in_region = member[line_index, :]
    else:
        line_index = int(math.floor((position - x0) / cell))
        across_origin, along_origin = x0, y0
        n_along = height.shape[0]
        line_index = min(max(line_index, 0), height.shape[1] - 1)
        in_region = member[:, line_index]

    # Cells across the rib's width: centres within half a width of the line,
    # and always the cell the line runs through.
    first = math.ceil((position - half - across_origin) / cell - 0.5)
    last = math.floor((position + half - across_origin) / cell - 0.5)
    first, last = min(first, line_index), max(last, line_index)
    limit = height.shape[0] if along_x else height.shape[1]
    first, last = max(first, 0), min(last, limit - 1)

    tops = np.full(n_along, np.nan)
    for k in np.flatnonzero(in_region):
        across = height[first:last + 1, k] if along_x else height[k, first:last + 1]
        values = across[np.isfinite(across)]
        if not len(values) or values.max() - values.min() > settings.jump:
            continue
        tops[k] = values.max() + settings.overlap

    # Runs of consecutive usable samples, also split at steps along the line.
    run = []
    for k in range(n_along + 1):
        usable = k < n_along and np.isfinite(tops[k])
        if usable and run and abs(tops[k] - tops[run[-1]]) > settings.jump:
            yield _wall(run, tops, along_origin, cell, position, half, along_x)
            run = []
        if usable:
            run.append(k)
        elif run:
            yield _wall(run, tops, along_origin, cell, position, half, along_x)
            run = []


def _wall(run, tops, along_origin, cell, position, half, along_x):
    """One closed wall over consecutive samples `run`.

    Sections sit at each sample centre plus half a cell beyond both ends, so
    even a one-sample run has three sections and a nonzero length.  Each
    section has four corners: bottom/top on both faces of the wall.
    """
    centres = along_origin + (np.asarray(run) + 0.5) * cell
    u = np.concatenate([[centres[0] - cell / 2], centres, [centres[-1] + cell / 2]])
    t = np.concatenate([[tops[run[0]]], tops[run], [tops[run[-1]]]])
    n = len(u)
    # Local frame: u along the rib, v across it, z up.
    local = np.empty((n, 4, 3))
    local[:, :, 0] = u[:, None]
    local[:, 0, 1] = local[:, 2, 1] = position - half      # bl, tl
    local[:, 1, 1] = local[:, 3, 1] = position + half      # br, tr
    local[:, 0, 2] = local[:, 1, 2] = 0.0
    local[:, 2, 2] = local[:, 3, 2] = t
    bl, br, tl, tr = (np.arange(n) * 4 + c for c in range(4))
    a, b = slice(0, n - 1), slice(1, n)
    faces = np.concatenate([
        np.stack([bl[a], br[b], bl[b]], 1), np.stack([bl[a], br[a], br[b]], 1),   # bottom
        np.stack([tl[a], tl[b], tr[b]], 1), np.stack([tl[a], tr[b], tr[a]], 1),   # top
        np.stack([bl[a], bl[b], tl[b]], 1), np.stack([bl[a], tl[b], tl[a]], 1),   # -v side
        np.stack([br[a], tr[b], br[b]], 1), np.stack([br[a], tr[a], tr[b]], 1),   # +v side
        [[bl[0], tl[0], tr[0]], [bl[0], tr[0], br[0]]],                           # start cap
        [[bl[-1], br[-1], tr[-1]], [bl[-1], tr[-1], tl[-1]]],                     # end cap
    ]).astype(np.int64)
    vertices = local.reshape(-1, 3)
    if not along_x:
        # u is world y and v world x: swapping two axes mirrors the solid, so
        # the winding is reversed to stay outward.
        vertices = vertices[:, [1, 0, 2]]
        faces = faces[:, ::-1]
    return vertices, faces
