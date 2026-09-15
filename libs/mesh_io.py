"""Read what a mesh file says about itself, without loading the mesh.

Everything here answers a question from the header, or by streaming — nothing
welds a mesh or builds connectivity.  That is deliberate: these answers decide
whether a file is queued at all and in what order, so they must be cheap enough
to ask about every file in a collection before any work starts.

    probe(path) -> Mesh(path, kind, triangles, is_valid, problem)

The one rule worth stating up front: **an unknown count is None, never zero.**
An ASCII STL has no triangle count in its header and an OBJ has no header at
all, so the honest answer is "ask Blender". Returning 0 would make those files
sort as the cheapest work in the queue when they may be the most expensive.
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass
from enum import Enum

import numpy as np

#: Bytes per triangle in a binary STL: 12 normal + 36 vertices + 2 attribute.
BYTES_PER_TRIANGLE = 50

#: 80-byte comment + 4-byte triangle count.
HEADER_BYTES = 84

#: How much of the file `kind()` reads to tell ASCII from binary.
_SNIFF_BYTES = 256


class Kind(Enum):
    """What sort of file this is, by inspection rather than by extension."""

    BINARY_STL = 'binary_stl'
    ASCII_STL = 'ascii_stl'
    OBJ = 'obj'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class Mesh:
    """What a file says about itself.

    path        the file asked about
    kind        binary STL, ASCII STL, OBJ, or unknown
    triangles   count from the header, or **None when it cannot be known
                without parsing** — ASCII STL and OBJ always report None
    is_valid    False when the file cannot be read as what it claims to be
    problem     why, when is_valid is False; None otherwise
    """

    path: str
    kind: Kind
    triangles: int | None
    is_valid: bool
    problem: str | None = None

    @property
    def needs_conversion(self) -> bool:
        """True when this must become a binary STL before it can be measured."""
        return self.kind in (Kind.ASCII_STL, Kind.OBJ)


def kind(path: str) -> Kind:
    """Classify `path` by content, falling back to extension for OBJ.

    An OBJ is identified by name — its content has no reliable magic, and
    Blender validates it at import.  STL is sniffed, because the extension says
    nothing about the encoding.

    **The ASCII tiebreaker matters.** Some exporters (SolidWorks, older Slic3r)
    write a `solid <name>` text header onto a *binary* file, so "starts with
    solid" is not enough.  When the header looks like text but the binary
    triangle count is consistent with the file size, the file is binary.

    Known limit, deliberately kept: only the first 256 bytes are read, so a
    valid ASCII STL whose solid name runs past ~240 characters before the first
    `facet normal` is misread as binary.  The failure is one-directional — such
    a file is then parsed as binary, where the count field is garbage and the
    size cross-check rejects it as invalid rather than repairing wrong bytes.
    So the cost is a false "invalid" report on a file nobody produces, against
    a larger read on every file in a collection.  Revisit only if a real file
    is ever reported invalid with a long solid name.
    """
    if path.lower().endswith('.obj'):
        return Kind.OBJ
    try:
        with open(path, 'rb') as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return Kind.UNKNOWN
    if not head:
        return Kind.UNKNOWN

    looks_ascii = (head.lstrip()[:5].lower() == b'solid'
                   and b'facet normal' in head.lower())
    if not looks_ascii:
        return Kind.BINARY_STL
    if len(head) >= HEADER_BYTES:
        declared = struct.unpack_from('<I', head, 80)[0]
        try:
            size = os.path.getsize(path)
        except OSError:
            return Kind.ASCII_STL
        if size >= HEADER_BYTES + declared * BYTES_PER_TRIANGLE:
            return Kind.BINARY_STL     # text header on a binary file
    return Kind.ASCII_STL


def triangle_count(path: str) -> tuple[int, str | None]:
    """Triangles in a binary STL, cross-checked against the file size.

    Returns `(count, problem)`.  The header's count is authoritative — never
    trust one supplied by a caller, which may come from a different tool's face
    counter and disagree with the bytes on disk.

    A file *larger* than the count implies is accepted: some exporters append
    colour data after the triangles, which is non-standard but read fine by
    every slicer.  Only a file too short to hold what it claims is a problem.
    """
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(HEADER_BYTES)
    except OSError as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    if len(head) < HEADER_BYTES:
        return -1, "file shorter than an STL header"
    count = struct.unpack_from('<I', head, 80)[0]
    needed = HEADER_BYTES + count * BYTES_PER_TRIANGLE
    if size < needed:
        return -1, (f"header claims {count:,} triangles ({needed:,} bytes) "
                    f"but the file is {size:,} bytes")
    return count, None


def probe(path: str) -> Mesh:
    """Everything a queue needs to know about a file, cheaply.

    Reads at most a few hundred bytes.  Never parses geometry, so the cost is
    the same for a 5 MB file and a 700 MB one — which is what makes it usable
    on a whole collection before any work begins.
    """
    file_kind = kind(path)

    if file_kind is Kind.UNKNOWN:
        return Mesh(path, file_kind, None, False, "unreadable or empty")

    if file_kind is Kind.OBJ:
        try:
            empty = os.path.getsize(path) == 0
        except OSError as exc:
            return Mesh(path, file_kind, None, False, str(exc))
        if empty:
            return Mesh(path, file_kind, None, False, "OBJ file is empty")
        # No count without parsing; Blender validates at import.
        return Mesh(path, file_kind, None, True)

    if file_kind is Kind.ASCII_STL:
        # Counting would mean scanning the whole file for `facet` lines, which
        # is the expense this module exists to avoid.  It is converted before
        # it is queued, and the conversion's output is what gets measured.
        return Mesh(path, file_kind, None, True)

    count, problem = triangle_count(path)
    if problem is not None:
        return Mesh(path, file_kind, None, False, problem)
    return Mesh(path, file_kind, count, True)


def bounds(path: str) -> tuple[tuple[float, float, float],
                               tuple[float, float, float]] | None:
    """Extents of a binary STL as `((minx, miny, minz), (maxx, maxy, maxz))`.

    Streams the vertex block in chunks rather than welding the mesh: extents
    need no connectivity, and welding a 900k-face mesh to answer this would
    cost hundreds of MB inside a worker already near its budget.

    Returns None when the file cannot be read as a binary STL.
    """
    count, problem = triangle_count(path)
    if problem is not None or count <= 0:
        return None
    try:
        lo = np.full(3, np.inf, dtype=np.float64)
        hi = np.full(3, -np.inf, dtype=np.float64)
        chunk = 200_000                       # triangles per pass, ~10 MB
        with open(path, 'rb') as f:
            f.seek(HEADER_BYTES)
            remaining = count
            while remaining > 0:
                take = min(chunk, remaining)
                buf = f.read(take * BYTES_PER_TRIANGLE)
                if len(buf) < take * BYTES_PER_TRIANGLE:
                    return None
                block = np.frombuffer(buf, dtype=np.uint8).reshape(
                    take, BYTES_PER_TRIANGLE)
                # Bytes 12:48 are the three vertices; 0:12 is the face normal,
                # which must not be included in the extents.
                verts = block[:, 12:48].copy().view(np.float32).reshape(-1, 3)
                np.minimum(lo, verts.min(axis=0), out=lo)
                np.maximum(hi, verts.max(axis=0), out=hi)
                remaining -= take
        if not np.all(np.isfinite(lo)) or not np.all(np.isfinite(hi)):
            return None
        return tuple(lo), tuple(hi)
    except (OSError, ValueError):
        return None


def dimensions(path: str) -> tuple[float, float, float] | None:
    """Size of the model along each axis, or None if it cannot be read."""
    extents = bounds(path)
    if extents is None:
        return None
    lo, hi = extents
    return tuple(hi[i] - lo[i] for i in range(3))


def diagonal(path: str) -> float | None:
    """Length of the bounding box's diagonal — one number for "how big".

    Useful for expressing an absolute tolerance as a fraction of the model,
    which is how a fixed millimetre constant stops meaning two different things
    on a 4 mm part and a 200 mm one.
    """
    dims = dimensions(path)
    if dims is None:
        return None
    return math.sqrt(sum(d * d for d in dims))
