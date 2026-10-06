"""Convert text meshes (ASCII STL, OBJ) to binary STL, in this process.

Replaces Blender at intake (owner, 2026-10-06; evidence in TODO.md history and
`blender`'s docstring): Blender rotated OBJ coordinates (Y-up -> Z-up) with
float noise, dropped degenerate facets silently, and outlived a killed runner.
This writes the coordinates the source has, rounded to float32 once, and
applies `mesh_io.filter_triangles` — the same drop rule `mesh_io.load` uses —
counting what it drops.

What a source may contain (owner, 2026-10-06):

- ASCII STL: `solid` blocks of `facet normal` / `outer loop` / `vertex` x3 /
  `endloop` / `endfacet`, keywords in any case, any whitespace, several
  solids, the last `endsolid` optional. Normal values are ignored (`write`
  derives normals from the winding).
- OBJ: `v` (first three numbers; a w or colour after them is ignored) and `f`
  in the forms `v`, `v/vt`, `v//vn`, `v/vt/vn`, with negative (relative) and
  forward indices; `#` comments and `\\` continuations; every other statement
  (vt, vn, o, g, s, usemtl, ...) is ignored, so all groups become one mesh.
  Triangles and quads only. A quad is split along a diagonal whose two
  triangles face the same way: a-c first (Blender's choice for a convex
  quad), else b-d (through the reflex corner of a concave quad). A quad where
  neither does — a bowtie, or one folded over — is split along a-c anyway and
  counted, as Blender does (owner, 2026-10-06, after a Blender probe).

Anything else in the content raises `Malformed`, and the batch run writes the
model's FAILED marker (a full source copy): a facet without exactly three
vertices, a facet cut off before `endloop`/`endfacet`, an OBJ face with fewer
than three or more than four vertices, an index that is 0 or outside the
vertex table, a coordinate that is not a number, an unexpected keyword.
`ConversionError` — and any I/O error — is a failure without a marker: the
source is retried on the next run.

Memory: ASCII STL holds one chunk of facets; OBJ holds its vertex table
(24 B per vertex) and one chunk of faces, reading the file twice so faces never
accumulate. The output goes through `mesh_io.staged_write`, so an exception or
Ctrl+C at any point publishes nothing.
"""

from __future__ import annotations

import os
import struct
from array import array
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from . import mesh_io
from .mesh_io import CHUNK_TRIANGLES, HEADER_BYTES, Kind, LoadDrops

#: The largest count a binary STL header can hold.
_MAX_TRIANGLES = 0xFFFFFFFF


class Malformed(Exception):
    """The source's content is not a mesh this converter accepts."""


class ConversionError(Exception):
    """The source could not be converted, for a reason a rerun may not repeat
    or one `mesh_io.load` also treats as invalid (nothing left to write)."""


@dataclass(frozen=True)
class Stats:
    """What one conversion wrote and dropped."""

    triangles: int                    # written to the binary STL
    drops: LoadDrops                  # removed by `mesh_io.filter_triangles`
    quads: int = 0                    # OBJ quads split into two triangles
    quads_without_inside_diagonal: int = 0   # of those, split along a-c anyway

    def summary(self) -> str:
        """One line for the model log."""
        text = (f'{self.triangles:,} triangles written; dropped '
                f'{self.drops.nonfinite:,} non-finite, '
                f'{self.drops.degenerate:,} degenerate')
        if self.quads:
            text += (f'; {self.quads:,} quads split, '
                     f'{self.quads_without_inside_diagonal:,} without an '
                     f'inside diagonal')
        return text


def convert(source: str, export: str, *,
            chunk_triangles: int = CHUNK_TRIANGLES) -> Stats:
    """Write `source` (OBJ or ASCII STL) to `export` as binary STL.

    `chunk_triangles` is how many facets/faces are held before writing; a
    parameter so tests can force chunk boundaries on small fixtures.
    """
    file_kind = mesh_io.kind(source)
    if file_kind is Kind.OBJ:
        def produce(writer):
            return _convert_obj(source, writer, chunk_triangles)
    elif file_kind is Kind.ASCII_STL:
        def produce(writer):
            return _convert_ascii_stl(source, writer, chunk_triangles)
    else:
        raise ValueError(f'{source}: only OBJ and ASCII STL are converted, '
                         f'not {file_kind.value}')

    with mesh_io.staged_write(export) as staged:
        with open(staged, 'wb') as f:
            writer = _Writer(f)
            quads, without = produce(writer)
            if writer.written == 0:
                # Raised inside the staging context, so nothing is published.
                raise ConversionError(_nothing_kept(writer))
            writer.finish()
    return Stats(writer.written, LoadDrops(writer.nonfinite, writer.degenerate),
                 quads, without)


def _nothing_kept(writer: _Writer) -> str:
    if writer.nonfinite + writer.degenerate == 0:
        return 'no triangles: the file holds no faces'
    return (f'no triangles kept: {writer.nonfinite:,} non-finite and '
            f'{writer.degenerate:,} degenerate dropped')


class _Writer:
    """Appends filtered triangle records to an open binary STL, whose count
    is patched into the header by `finish`."""

    def __init__(self, f):
        self._f = f
        self.written = self.nonfinite = self.degenerate = 0
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', 0))

    def add(self, corners: np.ndarray) -> None:
        """Write the kept triangles of float64 `corners` (n, 9)."""
        if not len(corners):
            return
        # Beyond float32's range becomes inf here and is dropped as non-finite,
        # as an inf in a binary STL would be.
        with np.errstate(over='ignore'):
            tri32 = corners.astype(np.float32)
        good, nonfinite, degenerate = mesh_io.filter_triangles(tri32)
        self.nonfinite += nonfinite
        self.degenerate += degenerate
        if self.written + len(good) > _MAX_TRIANGLES:
            raise ConversionError(
                f'more than {_MAX_TRIANGLES:,} triangles: a binary STL '
                f'cannot hold them')
        self._f.write(mesh_io.stl_records(good.reshape(-1, 3, 3)))
        self.written += len(good)

    def finish(self) -> None:
        self._f.seek(HEADER_BYTES - 4)
        self._f.write(struct.pack('<I', self.written))
        self._f.flush()
        os.fsync(self._f.fileno())


def _number(token: bytes, where: str) -> float:
    try:
        return float(token)
    except ValueError:
        raise Malformed(f'{where}: {token[:40]!r} is not a number') from None


# -- ASCII STL ---------------------------------------------------------------

# Parser states: what the next keyword must be.
_OUTSIDE, _SOLID, _FACET, _LOOP, _END_FACET = range(5)


def _convert_ascii_stl(source: str, writer: _Writer,
                       chunk: int) -> tuple[int, int]:
    corners = np.empty((chunk, 9))
    n = 0
    state = _OUTSIDE
    vertices = 0                     # in the current loop
    lineno = 0
    with open(source, 'rb') as f:
        for lineno, line in enumerate(f, 1):
            tokens = line.split()
            if not tokens:
                continue
            word = tokens[0].lower()
            where = f'line {lineno}'
            if state == _LOOP and word == b'vertex':
                if len(tokens) != 4:
                    raise Malformed(f'{where}: a vertex needs three '
                                    f'coordinates, found {len(tokens) - 1}')
                if vertices < 3:
                    corners[n, 3 * vertices:3 * vertices + 3] = [
                        _number(t, where) for t in tokens[1:]]
                vertices += 1
            elif state == _LOOP and word == b'endloop':
                if vertices != 3:
                    raise Malformed(f'{where}: a facet with {vertices} '
                                    f'vertices (exactly three required)')
                state = _END_FACET
            elif state == _END_FACET and word == b'endfacet':
                n += 1
                if n == chunk:
                    writer.add(corners)
                    n = 0
                state = _SOLID
            elif state == _FACET and [t.lower() for t in tokens] == [b'outer', b'loop']:
                state, vertices = _LOOP, 0
            elif state == _SOLID and word == b'facet':
                if len(tokens) < 2 or tokens[1].lower() != b'normal':
                    raise Malformed(f'{where}: expected "facet normal"')
                state = _FACET
            elif state == _SOLID and word == b'endsolid':
                state = _OUTSIDE
            elif state == _OUTSIDE and word == b'solid':
                state = _SOLID
            else:
                raise Malformed(f'{where}: unexpected {tokens[0][:40]!r}')
    if state not in (_OUTSIDE, _SOLID):
        # A missing final `endsolid` is accepted; a facet cut off is not.
        raise Malformed(f'truncated: the file ends inside a facet '
                        f'(after line {lineno})')
    writer.add(corners[:n])
    return 0, 0


# -- OBJ ---------------------------------------------------------------------

def _obj_statements(path: str) -> Iterator[tuple[int, list[bytes]]]:
    """(first line number, tokens) per logical OBJ line: comments removed,
    `\\` continuations joined, blank lines skipped."""
    with open(path, 'rb') as f:
        pending: list[bytes] = []
        start = 0
        for lineno, line in enumerate(f, 1):
            line = line.split(b'#', 1)[0].rstrip()
            if not pending:
                start = lineno
            if line.endswith(b'\\'):
                pending.append(line[:-1])
                continue
            pending.append(line)
            tokens = b' '.join(pending).split()
            pending = []
            if tokens:
                yield start, tokens
        if pending:
            tokens = b' '.join(pending).split()
            if tokens:
                yield start, tokens


def _convert_obj(source: str, writer: _Writer, chunk: int) -> tuple[int, int]:
    # Pass 1: the vertex table, which a face may index anywhere in.
    table = array('d')
    for lineno, tokens in _obj_statements(source):
        if tokens[0] == b'v':
            if len(tokens) < 4:
                raise Malformed(f'line {lineno}: a vertex needs three '
                                f'coordinates, found {len(tokens) - 1}')
            table.extend(_number(t, f'line {lineno}') for t in tokens[1:4])
    verts = np.frombuffer(table, dtype=np.float64).reshape(-1, 3)
    total = len(verts)

    # Pass 2: faces, a chunk at a time. -1 in the fourth column: a triangle.
    faces = np.empty((chunk, 4), dtype=np.int64)
    n = quads = without = 0
    seen = 0                          # vertices defined so far, for negatives
    for lineno, tokens in _obj_statements(source):
        if tokens[0] == b'v':
            seen += 1
            continue
        if tokens[0] != b'f':
            continue
        where = f'line {lineno}'
        size = len(tokens) - 1
        if size < 3 or size > 4:
            raise Malformed(f'{where}: a face with {size} vertices (only '
                            f'triangles and quads are accepted)')
        row = faces[n]
        row[3] = -1
        for k, token in enumerate(tokens[1:]):
            row[k] = _obj_index(token, seen, total, where)
        n += 1
        if n == chunk:
            q, w = _add_faces(writer, verts, faces)
            quads, without, n = quads + q, without + w, 0
    q, w = _add_faces(writer, verts, faces[:n])
    return quads + q, without + w


def _obj_index(token: bytes, seen: int, total: int, where: str) -> int:
    """0-based vertex index of one `f` token, validated before storing."""
    text = token.split(b'/', 1)[0]
    try:
        index = int(text)
    except ValueError:
        raise Malformed(f'{where}: {token[:40]!r} is not a vertex index') from None
    if index > 0 and index <= total:
        return index - 1
    if index < 0 and -index <= seen:
        return seen + index
    raise Malformed(f'{where}: vertex index {text[:40].decode(errors="replace")} '
                    f'is outside the vertex table ({total:,} vertices; '
                    f'{seen:,} before this face)')


def _add_faces(writer: _Writer, verts: np.ndarray,
               faces: np.ndarray) -> tuple[int, int]:
    """Split the quads among `faces` and write every triangle, in face order.
    Returns (quads, quads without an inside diagonal)."""
    if not len(faces):
        return 0, 0
    is_quad = faces[:, 3] >= 0
    first = faces[:, :3].copy()
    q = faces[is_quad]
    second = q[:, [0, 2, 3]]                       # a-c: (a,b,c) + (a,c,d)
    without = 0
    if len(q):
        p = verts[q]                                # (q, 4, 3)
        a, b, c, d = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
        # A diagonal is inside when its two triangles face the same way:
        # for a planar simple quad that holds for both diagonals when convex
        # and only for the one through the reflex corner when concave. A
        # zero-area triangle gives 0 and qualifies; the filter decides it.
        ac = np.einsum('ij,ij->i', np.cross(b - a, c - a), np.cross(c - a, d - a)) >= 0
        bd = np.einsum('ij,ij->i', np.cross(b - a, d - a), np.cross(c - b, d - b)) >= 0
        use_bd = ~ac & bd
        without = int((~ac & ~bd).sum())
        rows = np.flatnonzero(is_quad)[use_bd]
        first[rows] = q[use_bd][:, [0, 1, 3]]      # b-d: (a,b,d) + (b,c,d)
        second[use_bd] = q[use_bd][:, [1, 2, 3]]
    # Each face's triangles stay together and in face order.
    position = np.arange(len(faces)) + np.cumsum(is_quad) - is_quad
    triangles = np.empty((len(faces) + len(q), 3), dtype=np.int64)
    triangles[position] = first
    triangles[position[is_quad] + 1] = second
    writer.add(verts[triangles].reshape(-1, 9))
    return len(q), without
