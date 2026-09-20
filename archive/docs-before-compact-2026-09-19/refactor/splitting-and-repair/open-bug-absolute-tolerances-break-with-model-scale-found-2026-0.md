### OPEN BUG — absolute tolerances break with model scale (found 2026-09-17)

**Not fixed. Found at the end of the session, recorded to resume from.**

Two tolerances written this session are in **absolute model units**, and at
least one of them already fails on ordinary print sizes.

| constant | value | unit | status |
|---|---|---|---|
| `welder.DEFAULT_TOLERANCE` | `1e-6` | **absolute** | **BROKEN — misses junctions at r=50-200** |
| `repairer.LOST_VERTEX_TOLERANCE` | `1e-4` | **absolute** | untested at scale, same risk |
| CLEAN `threshold` | `0.1` | `PercentageValue` of bbox diagonal | **correct** |
| `splitter.MIN_SHELL_FACES` | `100` | face count | fine, not a distance |
| `welder.MAX_ROUNDS` | `10` | iterations | fine, not a distance |
| `blender.repair(merge_dist=)` | `0.01` mm | **absolute** | **third instance — see below** |

**A third instance, found the same day**: `blender_fx/repair.blender` welds its
input with `bmesh.ops.remove_doubles(dist=merge_dist)` at a default of
**0.01 mm absolute**. Same failure shape as the two above, and worse than
either, because it is the *only* thing reconstructing a vertex table that STL
destroyed on the way in — see the re-opened PLY entry. Switching that boundary
to PLY removes the call rather than fixing its tolerance, which is the better
outcome: a guess deleted beats a guess calibrated.

#### The measured failure

One T-junction injected into a sphere, the same fixture at seven radii. The
junction's distance from the edge **grows with the model**, because float32
carries ~7 significant digits and a coordinate near 100 rounds at ~1e-5:

| radius | junction distance | found at `1e-6`? |
|---|---|---|
| 1 | 1.9e-08 | yes |
| 10 | 2.6e-23 | yes |
| **50** | **1.2e-06** | **NO** |
| **100** | **2.5e-06** | **NO** |
| **200** | **5.0e-06** | **NO** |
| 500 | 0.0 | yes |
| 1000 | 0.0 | yes |

A 100-200 mm print is an entirely ordinary size, so this is a live gap rather
than an edge case. A tolerance of `radius * 1e-4` finds the junction at **all
seven** radii.

The non-monotonic pattern (500 and 1000 pass again) is not understood and is
worth a look: it is presumably where the midpoint happens to land on an exactly
representable float. Do not assume the safe radii are safe for other geometry.

#### Why this is embarrassing rather than surprising

**The project already learned this**, in D-note form: `MERGE_DIST = 0.01mm` was
measured as the *worst* PyMeshLab threshold tried — the only setting that
*increased* non-manifold edges (2,263 -> 2,359) — and CLEAN was deliberately
given a `PercentageValue` instead. Then two new absolute tolerances were
written without applying that lesson.

`welder`'s own docstring flags the constant as "**not calibrated on real
data**". What was not clear is that the problem is the **unit**, not the value:
no single absolute number can be right across scales.

#### The fix, when resumed

Scale both by the bounding-box diagonal, as CLEAN already does. Sketch:

```python
diagonal = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0)))
tolerance = relative * diagonal        # relative ~1e-7 gives 1e-5 on a 100mm model
```

Points to settle:

- **Keep an absolute override.** A caller repairing a junction from a boolean
  may know the real gap; the relative default must not be the only option.
- **`mesh_io.bounds()` already exists** and computes this from a path; a
  geometry-level helper may belong in `scanner` instead, since `welder` and
  `repairer` both need it and neither should read a file.
- **Test across scales**, which nothing currently does: every probe is r=10.
  A `for radius in (1, 10, 100, 1000)` loop over the T-junction fixture is what
  would have caught this.
- **`LOST_VERTEX_TOLERANCE` needs the same treatment**, and its own measurement:
  the float-noise floor it was calibrated against (1.0e-05 on a r=10 sphere)
  will also scale.

---

