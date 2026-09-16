"""Read what a mesh file says about itself, without loading the mesh.

Everything here answers a question from the header, or by streaming — nothing
welds a mesh or builds connectivity.  That is deliberate: these answers decide
whether a file is queued at all and in what order, so they must be cheap enough
to ask about every file in a collection before any work starts.

    probe(path, destination) -> Mesh(path, destination, kind, triangles, ...)

The one rule worth stating up front: **an unknown count is None, never zero.**
An ASCII STL has no triangle count in its header and an OBJ has no header at
all, so the honest answer is "ask Blender". Returning 0 would make those files
sort as the cheapest work in the queue when they may be the most expensive.

Geometry — the expensive half — lives behind an explicit step:

    loaded = load(mesh)            # a NEW Mesh, with .geometry attached
    write(loaded)                  # to its own destination

`Mesh` is frozen and every operation returns a new one, so a mesh in a queue
can never quietly have become a 400 MB object while it sat there.  The two
sizes are worth keeping in mind: a probed `Mesh` is a couple of hundred bytes,
a loaded one is the whole welded mesh in RAM (measured 383 MB for 2.55M
triangles).  That is the reason loading is a separate call and not a property
that quietly happens on first access — a worker's memory budget is decided
before it starts, and an implicit load would blow it from inside an attribute
lookup.

One writer, deliberately.  `write` takes a `Mesh`, so a caller never assembles
a header itself, and there is exactly one place that knows an STL facet is 50
bytes with a real normal in the first 12.
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
class Geometry:
    """A welded mesh: shared vertices, and faces indexing into them.

    This is the expensive thing.  A binary STL on disk stores no vertex
    sharing — each triangle carries its own three coordinate triples, so a
    vertex touched by six faces appears six times (measured: exactly 6.0x on
    real models).  Recovering that sharing is what `load` does, and it is a
    precondition of edge-based algorithms rather than an optimisation: quadric
    edge collapse works on edges, so it must know which faces meet where.
    """

    verts: np.ndarray                 # (n_verts, 3) float32
    faces: np.ndarray                 # (n_faces, 3) int64, indices into verts


@dataclass(frozen=True)
class Mesh:
    """What a file says about itself, and optionally the mesh itself.

    path        the file this mesh came from
    destination where its repaired result belongs.  **Required**, because the
                marker names the indicator scan looks for are derived from it
                (`<base>.failed.stl` and friends) — two derivations would mean
                two spellings, and a rerun would silently reprocess everything.
                One field, one source of truth.
    kind        binary STL, ASCII STL, OBJ, or unknown
    triangles   count from the header, or **None when it cannot be known
                without parsing** — ASCII STL and OBJ always report None
    is_valid    False when the file cannot be read as what it claims to be
    problem     why, when is_valid is False; None otherwise
    geometry    the welded mesh, once `load` has been called; None before that

    Frozen, and every operation returns a new one.  `load` does not fill in
    geometry on the mesh you hand it — it gives you back a second mesh that has
    it.  That is what keeps the cheap object cheap: a `Mesh` sitting in a queue
    stays a couple of hundred bytes no matter what a worker does with its own
    copy elsewhere.
    """

    path: str
    destination: str
    kind: Kind
    triangles: int | None
    is_valid: bool
    problem: str | None = None
    geometry: Geometry | None = None

    @property
    def needs_conversion(self) -> bool:
        """True when this must become a binary STL before it can be measured."""
        return self.kind in (Kind.ASCII_STL, Kind.OBJ)

    @property
    def is_loaded(self) -> bool:
        """True when the geometry is in memory and this object is large."""
        return self.geometry is not None

    def with_geometry(self, geometry: Geometry) -> Mesh:
        """A copy carrying `geometry`, with `triangles` re-derived from it.

        This is how a step that changes the mesh — decimation, repair — reports
        its result: it returns a new `Mesh` rather than writing a file, so the
        caller decides whether the result is worth keeping.  The triangle count
        is taken from the faces rather than carried over, because the whole
        point of those steps is that it changed.
        """
        return Mesh(self.path, self.destination, self.kind,
                    len(geometry.faces), self.is_valid, self.problem, geometry)


    def with_destination(self, destination: str) -> Mesh:
        """A copy bound for a different output.

        `with_geometry` is for a step that *transforms* a mesh — same file,
        changed geometry — so it carries the destination across unchanged.
        A split is the other shape: one input becomes several outputs, and each
        needs its own destination or their markers collide.  This is how a
        part gets one.
        """
        return Mesh(self.path, destination, self.kind, self.triangles,
                    self.is_valid, self.problem, self.geometry)


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


def probe(path: str, destination: str) -> Mesh:
    """Everything a queue needs to know about a file, cheaply.

    `destination` is where the repaired result belongs.  It is required rather
    than derived here because deriving it is the caller's policy — the output
    tree layout, the OBJ-becomes-STL rule — and `indicators` must look for
    markers at exactly the same spelling this mesh will be written to.

    Reads at most a few hundred bytes.  Never parses geometry, so the cost is
    the same for a 5 MB file and a 700 MB one — which is what makes it usable
    on a whole collection before any work begins.
    """
    file_kind = kind(path)

    if file_kind is Kind.UNKNOWN:
        return Mesh(path, destination, file_kind, None, False,
                    "unreadable or empty")

    if file_kind is Kind.OBJ:
        try:
            empty = os.path.getsize(path) == 0
        except OSError as exc:
            return Mesh(path, destination, file_kind, None, False, str(exc))
        if empty:
            return Mesh(path, destination, file_kind, None, False,
                        "OBJ file is empty")
        # No count without parsing; Blender validates at import.
        return Mesh(path, destination, file_kind, None, True)

    if file_kind is Kind.ASCII_STL:
        # Counting would mean scanning the whole file for `facet` lines, which
        # is the expense this module exists to avoid.  It is converted before
        # it is queued, and the conversion's output is what gets measured.
        return Mesh(path, destination, file_kind, None, True)

    count, problem = triangle_count(path)
    if problem is not None:
        return Mesh(path, destination, file_kind, None, False, problem)
    return Mesh(path, destination, file_kind, count, True)


def load(mesh: Mesh) -> Mesh:
    """Weld `mesh` into memory and return a **new** Mesh carrying it.

    Only binary STL can be loaded; anything else must be converted first, and
    asking is a programming error rather than a data problem, so it raises.

    The sort is done on the raw coordinate *bits* viewed as three uint32
    columns rather than on float rows.  Identical float32 values have identical
    bit patterns, so `np.lexsort` over the integer columns is exact and about
    4x faster than `np.unique(axis=0)` while allocating less (measured
    6.9s/461 MB -> 1.75s/383 MB on a 2.55M-triangle mesh).

    Negative zero is normalised first: -0.0 and 0.0 compare equal as floats but
    have different bits, so without this a shared vertex would split in two and
    leave a crack that quadric edge collapse cannot close.
    """
    if mesh.kind is not Kind.BINARY_STL:
        raise ValueError(
            f"only a binary STL can be loaded, not {mesh.kind.value} "
            f"({mesh.path}) — convert it first")

    count, problem = triangle_count(mesh.path)
    if problem is not None:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    problem)

    # Each intermediate is released the moment it is no longer needed.  On a
    # 7M-triangle mesh holding them all to the end peaks at 1000 MB against
    # 667 MB when freed eagerly — and that mesh was OOM-killed at 2.5 GB once
    # the decimator's own structures were added on top.
    try:
        with open(mesh.path, 'rb') as f:
            f.seek(HEADER_BYTES)
            raw = np.frombuffer(f.read(count * BYTES_PER_TRIANGLE),
                                dtype=np.uint8)
    except OSError as exc:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    f"{type(exc).__name__}: {exc}")
    if len(raw) < count * BYTES_PER_TRIANGLE:
        return Mesh(mesh.path, mesh.destination, mesh.kind, None, False,
                    f"short read: expected {count * BYTES_PER_TRIANGLE:,} "
                    f"bytes, got {len(raw):,}")
    raw = raw.reshape(count, BYTES_PER_TRIANGLE)

    # Bytes 12:48 of each record are the three vertices (9 float32).
    #
    # `.copy()`, not `ascontiguousarray`: the buffer from `frombuffer` is
    # read-only, and when the slice is already contiguous — which it is for a
    # single-triangle mesh — `ascontiguousarray` returns that read-only view
    # unchanged, and the -0.0 fold below then fails on it.  A mesh small enough
    # to hit that is a degenerate-face test case, not a model, so the bug would
    # have surfaced only on the strangest input.
    coords = raw[:, 12:48].copy().reshape(-1, 12)
    del raw
    fview = coords.view(np.float32).reshape(-1, 3)
    # Fold -0.0 to 0.0 in place; adding 0.0 leaves every other value untouched.
    np.add(fview, np.float32(0.0), out=fview)
    del fview

    bits = coords.view(np.uint32).reshape(-1, 3)
    order = np.lexsort((bits[:, 2], bits[:, 1], bits[:, 0]))
    srt = bits[order]
    new = np.empty(len(srt), dtype=bool)
    new[0] = True
    np.any(srt[1:] != srt[:-1], axis=1, out=new[1:])
    ids = np.cumsum(new) - 1
    inv = np.empty(len(srt), dtype=np.int64)
    inv[order] = ids
    del order, ids
    verts = srt[new].view(np.float32).reshape(-1, 3).copy()
    del srt, new, bits, coords

    return mesh.with_geometry(Geometry(verts, inv.reshape(count, 3)))


def write(mesh: Mesh) -> None:
    """Write a loaded `mesh` to its own destination, as a binary STL.

    The one writer.  Every step that produces geometry ends here, so there is a
    single place that knows the byte layout.

    There is no `path` argument: the mesh carries its destination, so a caller
    cannot write it somewhere the indicator scan will not look for its markers.

    The normals used to be left zeroed, on the reasoning that slicers recompute
    them from the winding.  Slicers do, but viewers do not all agree: given a
    zero normal some fall back to the winding and some to a guess, so the same
    file could render inside-out in one program and correctly in another.  That
    made a genuine comparison between two outputs impossible — one file with
    normals and one without are not being drawn the same way.  Computing them
    is a cross product over the face array, negligible beside the repair that
    produced the mesh.
    """
    if mesh.geometry is None:
        raise ValueError(f"{mesh.path} has no geometry to write — load it first")
    path = mesh.destination

    verts, faces = mesh.geometry.verts, mesh.geometry.faces
    n = len(faces)
    tv = verts[faces].astype(np.float32)               # (n, 3, 3)
    nrm = np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    # Degenerate faces have no normal to speak of; leave those zeroed rather
    # than dividing by zero and writing NaNs into the file.
    ok = ln > 1e-20
    nrm[ok] /= ln[ok][:, None]
    nrm[~ok] = 0.0

    buf = np.zeros((n, BYTES_PER_TRIANGLE), dtype=np.uint8)
    buf[:, 0:12] = nrm.astype(np.float32).view(np.uint8)
    buf[:, 12:48] = tv.reshape(n, 9).view(np.uint8)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)
        f.write(struct.pack('<I', n))
        f.write(buf.tobytes())


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
