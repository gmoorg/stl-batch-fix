"""Compare the current bit-exact vertex weld against a quantised one.

Read-only.  Loads each mesh, welds it two ways, and reports what changes:

  - how many vertices each approach produces
  - how many EXTRA vertices the quantised weld merges (the float-drift pairs
    the exact weld cannot see)
  - the largest distance any vertex would move if the quantised coordinates
    were stored rather than used only as a sort key
  - degenerate triangles before and after, since flatness was the claim

Nothing is written.  The frozen script is not imported for its weld; the
exact weld is reimplemented here so the comparison is self-contained.
"""
import struct
import sys
import numpy as np


def read_raw(path):
    """Every vertex slot as float32 (N*3, 3), no welding."""
    with open(path, 'rb') as f:
        f.read(80)
        n = struct.unpack('<I', f.read(4))[0]
        raw = np.frombuffer(f.read(n * 50), dtype=np.uint8)
    if len(raw) < n * 50:
        raise RuntimeError(f"short read on {path}")
    raw = raw.reshape(n, 50)
    coords = np.ascontiguousarray(raw[:, 12:48]).reshape(-1, 12)
    return n, coords


def weld_exact(coords):
    """Current approach: sort on raw bits, after folding -0.0 to 0.0."""
    c = coords.copy()
    fview = c.view(np.float32).reshape(-1, 3)
    np.add(fview, np.float32(0.0), out=fview)          # -0.0 -> 0.0
    bits = c.view(np.uint32).reshape(-1, 3)
    order = np.lexsort((bits[:, 2], bits[:, 1], bits[:, 0]))
    srt = bits[order]
    new = np.empty(len(srt), dtype=bool)
    new[0] = True
    np.any(srt[1:] != srt[:-1], axis=1, out=new[1:])
    ids = np.cumsum(new) - 1
    inv = np.empty(len(srt), dtype=np.int64)
    inv[order] = ids
    verts = srt[new].view(np.float32).reshape(-1, 3).copy()
    return verts, inv


def weld_quantised(coords, decimals):
    """Quantise to `decimals` places, weld on the integer grid.

    Returns (verts_kept, inv, moved_max).  verts_kept are the ORIGINAL floats
    of the first occurrence in each bucket -- the sort-key-only variant, which
    does not move geometry.  moved_max reports how far vertices WOULD move if
    the quantised values were stored instead.
    """
    f = coords.view(np.float32).reshape(-1, 3).astype(np.float64)
    scale = 10.0 ** decimals
    q = np.rint(f * scale).astype(np.int64)            # -0.0 -> 0 for free
    order = np.lexsort((q[:, 2], q[:, 1], q[:, 0]))
    srt = q[order]
    new = np.empty(len(srt), dtype=bool)
    new[0] = True
    np.any(srt[1:] != srt[:-1], axis=1, out=new[1:])
    ids = np.cumsum(new) - 1
    inv = np.empty(len(srt), dtype=np.int64)
    inv[order] = ids
    keep = order[new]                                  # first slot per bucket
    verts = f[keep].astype(np.float32)
    moved = np.abs(f - (q / scale)).max()
    return verts, inv, moved, q


def degenerate_count(verts, faces):
    """Triangles with zero area after welding."""
    v = verts.astype(np.float64)
    a, b, c = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    cross = np.cross(b - a, c - a)
    area2 = np.linalg.norm(cross, axis=1)
    return int((area2 == 0.0).sum()), float(area2.mean())


def packs_in_int64(q):
    """Would all three axes fit in one packed int64 key?"""
    span = int(max(q.max(), -q.min()))
    bits_per_axis = int(span).bit_length() + 1          # +1 for sign
    return bits_per_axis, bits_per_axis * 3 <= 63


def report(path, label):
    print("=" * 78)
    print(label)
    print("=" * 78)
    n, coords = read_raw(path)
    slots = n * 3
    print(f"  triangles              {n:>12,}")
    print(f"  vertex slots           {slots:>12,}")

    ve, inve = weld_exact(coords)
    faces_e = inve.reshape(n, 3)
    deg_e, mean_area_e = degenerate_count(ve, faces_e)
    print(f"\n  exact weld (current)")
    print(f"    unique vertices      {len(ve):>12,}   "
          f"({slots/len(ve):.2f}x duplication)")
    print(f"    degenerate faces     {deg_e:>12,}")

    for dec in (4, 5, 6):
        vq, invq, moved, q = weld_quantised(coords, dec)
        faces_q = invq.reshape(n, 3)
        deg_q, _ = degenerate_count(vq, faces_q)
        extra = len(ve) - len(vq)
        bits, fits = packs_in_int64(q)
        print(f"\n  quantised weld, {dec} decimals (grid "
              f"{10.0**-dec:g} mm)")
        print(f"    unique vertices      {len(vq):>12,}   "
              f"({extra:+,} vs exact)")
        print(f"    extra merges         {extra:>12,}   "
              f"({100*extra/max(len(ve),1):.4f}% of exact verts)")
        print(f"    degenerate faces     {deg_q:>12,}   "
              f"({deg_q - deg_e:+,})")
        print(f"    max vertex movement  {moved:>12.8f} mm  "
              f"(if quantised values were stored)")
        print(f"    packed key           {bits:>12} bits/axis  "
              f"{'fits in one int64' if fits else 'DOES NOT FIT'}")
    print()


if __name__ == '__main__':
    targets = [
        ("/mnt/sda2/STL/Fixing/Hanna and Chewie/Millenium_Falcon.stl",
         "Millenium_Falcon.stl  (393k tris, 117x36x155mm)"),
        ("/mnt/sda2/STL/Fixing/nutshell-atelier-belly-dancer-nsfw/whole-costume01.stl",
         "whole-costume01.stl  (2.0M tris, over the scan limit)"),
    ]
    for path, label in targets:
        try:
            report(path, label)
        except Exception as exc:
            print(f"FAILED on {label}: {type(exc).__name__}: {exc}")
