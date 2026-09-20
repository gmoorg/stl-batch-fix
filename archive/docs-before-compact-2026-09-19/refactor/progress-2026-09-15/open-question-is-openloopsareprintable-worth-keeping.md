### Open question — is `open_loops_are_printable` worth keeping?

Raised and **not settled**. Recorded mid-discussion so it can be resumed
without re-reading the pipeline.

**What the check does.** Accepts a mesh with open edges, without repairing it,
when every open boundary is smaller than one printed layer.

**The case for it** — the 1st-body cascade, the reason it was written:

```text
after pymeshfix   879,332 faces  volume  99.99%  open=4    0.01mm, all at one point
  -> blender called to clear them (180s)
after blender     874,236 faces  volume 100.04%  open=142   28 NEW holes, 0.023-0.162mm
  -> pymeshfix called again to clear those
final             356,392 faces  volume  44.94%  open=0     head and torso gone
```

The check fires at the first line. It does not save a redundant call — it
declines to *start* a cascade that creates defects and ends by destroying the
model while reporting `open=0`, which is the pipeline succeeding by its own
standard.

**The case against it**, and the reason it is open: `MIN_LAYER = 0.6` is an
absolute constant. On the Princess Leia part, 5.6 mm across, a 0.6 mm hole is
**11% of the model** — the check would wave through a hole that is plainly
real. The constant is also already doing a second, unrelated job at
`stl_batch_fix.py:2853`, where a mean edge length below `MIN_LAYER` is read as
evidence the mesh is over-detailed.

**The recovery ladder as it actually is** (worth stating, because the
discussion assumed one rung fewer): a repair that loses volume does not simply
get dropped. `_repair_by_seam_split` cuts the mesh at its winding seams and
repairs each region separately — measured 100.0% and 100.2% volume on the two
regions of a mesh that lost 15% while joined. So it is
*repair -> volume check -> seam-split retry -> fall back to the pre-repair
mesh*, and the printability check sits ahead of all of it.

**The candidate fix, not yet agreed**: make the test scale-aware rather than
absolute — a hole is unprintable only if it is below the layer height *and*
below some fraction of the model's bounding-box diagonal. `mesh_io.diagonal`
already exists. That removes the tiny-model failure while keeping the cascade
guard. The alternative is to drop the check and always take the second repair
pass, accepting the cascade risk.

**Also agreed but not yet built**: `scanner` should own `volume(mesh)`.
`_mesh_volume` is already an `einsum` over `verts[faces]`; today it pays a full
weld from a path every time it is asked, twice per volume comparison.

---

