# One-file geometry pipeline

## Required order

```text
load and validate finite, non-empty source
  → record original components, vertices, bounds, volume, and face count
  → decimate whole file if over budget
  → compare decimated mesh with the original
  → weld T-junctions
  → CLEAN: null faces → merge close vertices → duplicate faces → unused vertices
  → split into edge-connected shell parts
  → for each retained part separately
       orient by geometry
       scan and select repair route
       PyMeshFix for its supported defects
       Blender through PLY for holes/fins and defined fallback cases
       rescan and preserve failure evidence after each tool
  → require every meaningful part to remain acceptable
  → merge accepted parts
  → judge merged geometry and topology
  → write atomically, or write a full-mesh non-success marker
```

Current code runs the central path but always chooses PyMeshFix for a part. Blender routing, original-to-decimated preservation, meaningful-component retention, and finite validation remain open. The face target applies before repair; repair is not followed by blind re-decimation.

## Why this order

- **Decimate before split:** `max_faces` is a file budget. Split-first can produce `parts × max_faces`. Compare with the original immediately because later repair cannot prove what decimation removed.
- **Weld before hole repair:** welding preserves vertices and supplies missing face subdivision. General hole tools dented the fixture.
- **CLEAN before split:** merging vertices exposes duplicate faces while coincident copies are visible. Split-first left `doubles` as two shells at about 200% volume.
- **Split before PyMeshFix:** PyMeshFix can rebuild one surface and discard other disconnected shells.
- **Orient after split:** a whole-mesh volume guard misses local inversions; whole-mesh orientation erased seam evidence. Unconditional per-part orientation fixed tested cases.
- **Repair parts independently:** no repair call receives all shells. One bad part must prevent whole-file success.
- **Use both tools:** Blender measured better on holes/fins; PyMeshFix measured better on non-manifold geometry and winding seams. The selector and mixed-defect order need evidence.
- **Merge before final judgment:** merged counts are authoritative. Test destruction before cleanliness because a missing half can still be watertight.

## Tried and rejected

- Topology counts as the sole success test: accepts partial, zero-area, or non-finite geometry.
- Face-count change as a repair oracle: tiny input changes can cause large valid PyMeshFix count changes.
- Automatic seam splitting: a seam loop did not reliably predict damage; keep it as measured recovery.
- Dropping every small shell: face count does not establish debris versus required detail.
- Fixed authoring-unit tolerances: users rescale models, and absolute thresholds failed at different scales.
- Re-decimating after repair without another repair cycle: it can recreate repaired defects. The pre-repair decimation target deliberately leaves headroom under the approximate slicer limit.
