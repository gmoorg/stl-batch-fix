### The sequence

```python
mesh = welder.repair(mesh).mesh                    # 1. T-junctions
if scanner.volume(mesh) < 0:                       # 2. orientation
    mesh = pml(mesh, [('meshing_re_orient_faces_by_geometry', {})])
mesh = pml(mesh, CLEAN)                            # 3. duplicates
parts = [tool(p).mesh for p in splitter.by_shells(mesh)]   # 4. per shell
mesh  = splitter.merge(parts, destination=...)
```

```python
CLEAN = [('meshing_remove_null_faces', {}),
         ('meshing_merge_close_vertices', {'threshold': PercentageValue(0.1)}),
         ('meshing_remove_duplicate_faces', {}),
         ('meshing_remove_unreferenced_vertices', {})]
```

**Step 4's tool depends on the mesh** — the one routing decision the
measurements force:

| mesh | tool | evidence |
|---|---|---|
| small, single-shell | **Blender** | `fin` output face-identical to the control; on the all-defects sphere it lost 1 vertex (the fin apex, a defect) at exactly 100.00% volume where PyMeshFix lost 7 at 99.95% |
| large, multi-shell | **PyMeshFix** | costume01: 2,263 nm → 0, 28 open, 100% volume, 201s — against Blender's 493 open edges at 488s and a 420s timeout in the real pipeline |

**Why the order:**

- **welder first** — it only adds faces, never moves or deletes, so nothing
  downstream is disturbed.
- **orientation early** — a backwards surface makes PyMeshFix delete regions,
  which is the head-deletion case. Fix it before anything acts on the geometry.
- **duplicates before the split** — deduplication must see both copies.
  Measured: splitting first left `doubles` at 200% volume and 2 shells.
- **split before step 4** — PyMeshFix rebuilds *one* manifold surface and
  discards the rest; unsplit, it ate 4,525 support pillars on a resin model.

