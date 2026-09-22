# One-file geometry pipeline

## Adopted default order

```text
load finite, non-empty source
  → decimate whole file to max_faces
  → compute the already-decimated whole mesh's bounding-box diagonal once
  → split into edge-connected shells
  → alpha-wrap each retained part (alpha=diag/800, offset=diag/2000)
  → merge accepted parts
  → apply existing repair failure, volume-loss, topology, and solid checks
  → decimate again with the same max_faces
  → enforce final face budget and validate finite geometry, volume, and topology
  → write atomically, or write a full-mesh failure marker
```

Owner decision, 2026-09-22: post-repair decimation is now adopted default policy,
superseding the earlier rejection of re-decimation after repair. Alpha wrapping
can produce many more triangles. The second pass must meet the face budget and
its actual output must pass validation; it cannot conceal an already-rejected
repair. `max_faces <= 0` disables both decimations. Only the final pass has the
new hard face-budget postcondition; the first pass's existing behavior is unchanged.

The whole-mesh step tuple is empty. The authoritative per-part tuple contains
only `alphawrap.step_alpha_wrap`. Each repair call binds its own pre-split
diagonal with `functools.partial`; explicit non-None `tool=` overrides bypass
both calculation and binding. Weld, CLEAN filters, orientation, Blender, and
PyMeshFix remain intact as tools but no longer run by default.

## Why this order

- Decimate before split: the face budget belongs to the whole file.
- Wrap parts independently using one whole-mesh scale: the measured recipe is
  `diag/800`, `diag/2000`, visually confirmed on three real models; see
  [the recipe evidence](discovered-bugs.md#settled-diagonal-ratio-recipe-alphadiag800-offsetdiag2000).
- Judge repair before final decimation: later operations must not hide destruction.
- Validate final output: re-decimation can recreate defects or destroy geometry.
  Final volume retention uses the existing repair input volume baseline.

`Outcome` preserves first decimation, repair, and final decimation separately.
Final decimation failure or excess faces produces `UNDECIMATED` with a source
marker. Unmeasurable final geometry and destructive loss also use the source.
Remaining final topology defects use the final mesh for the repaired marker.

## Remaining limitations

Original-to-first-decimation preservation and meaningful-component retention
remain open. Shell face-count filtering does not establish whether a small part
is debris. Topology and volume alone do not prove visual fidelity. Automatic
seam splitting remains disabled by default. The earlier weld/CLEAN/orient/
Blender/PyMeshFix experiments remain historical evidence, not the current route.
