# Winding reconstruction output is non-manifold

## Symptom

The `winding` step raises:

```text
RuntimeError: reconstruction is not a closed manifold: open=0, non_manifold=1, degenerate=0
```

The output is closed (no open edges) but has 1–3 non-manifold edges. The part
fails, so the model fails and gets a `.failed.stl` marker. No fallback runs.

## Files

| File | Faces into winding | Parts | Failing part | NM | Winding time |
|---|---|---|---|---|---|
| 3dsqulpts - Thief/Hip.stl | 900,000 | 1 | 1/1 | 3 | 98 s |
| LionRealm Studio - One Piece - Zoro/Sword_sheath.stl | 899,997 | 1 | 1/1 | 1 | 93 s |
| LionRealm Studio - One Piece - Zoro/left_sword.stl | 898,781 | 2 | 1/2 | 1 | 83 s |
| zoro/Roronoa Zoro v2/stl files/Sword_and_head1.stl | 398,043 | 4 | 1/4 | 1 | 21 s |
| CA3D/1-6Scale Fantasy Dragon/Base/Base_Part_01.stl | 900,000 | 1 | 1/1 | 1 | 87 s |
| CA3D/Demon Queen/1-12 scale Demon Queen CA3D/Queen/torso_girl.stl | 699,869 | 1 | 1/1 | 1 | 30 s |

The two LionRealm models also fail under the duplicate
`zoro/LionRealm Studio - One Piece - Zoro/` folder, for 8 failures in total.

Hip, Sword_sheath, left_sword and Base_Part_01 came from the decimation cache
(`stl-decimated/...900000.stl`; Base_Part_01 at `batch.log:91`).
Sword_and_head1 (399,762 faces) and torso_girl (699,869) were under
`max_faces`, so they weren't decimated.

## Cause (verified on 3 of 6, 2026-10-04): `winding._weld` partially merges vertex clusters

Marching cubes produced output with no NM edges on the tested parts, and our
`_weld` introduces them. Only edge incidence was checked (`scanner.scan`):
bow-tie vertices, self-intersections and orientation were not.

Measured with
[tools/experiments/winding_nm_probe.py](../../tools/experiments/winding_nm_probe.py),
which uses the batch's grid spacing and 10 GB plan. It captures the
marching-cubes output before `_weld` and skips `_check`:

| File | Blocks | Raw MC output (exact merge only) | After `_weld` | NM edge location |
|---|---|---|---|---|
| torso_girl | 1 | 817,896 v, nm **0**, open 0 | 817,868 v, nm **1** | on grid node [339, 205, 442], edge 0.0001·h long |
| Sword_and_head1 (part 1/4) | 1 | 400,950 v, nm **0**, open 0 | 400,936 v, nm **1** | on grid node [669, 170, 88], 0.0001·h |
| Hip (decimation cache) | 2 (8 blocks) | nm **0**, open 112 (block seams unjoined) | nm **3**, open 0 | beside grid nodes (215.999→216, 225.088) |

Mechanism:

1. Where the field is ~0 **at a grid node**, marching cubes places the vertex
   of every edge meeting that node almost on the node. That gives a cluster of
   nearly coincident but distinct vertices, joined by sliver triangles. Still
   manifold.
2. `_weld` exists to join vertices that neighbouring blocks computed on their
   shared faces. It keys vertices by `rint(V / (h·1e-4))`. A cluster that
   straddles a rounding boundary is merged **partially**: some members join
   (e.g. 3 into one), one stays separate, and the faces between them
   collapse and are dropped.
3. The partial merge pinches the surface into a non-manifold edge.

Single-block parts don't need the weld at all: their raw output was already
clean. Multi-block parts need it for the seams (Hip: 112 open edges
without it), and the same partial merges happen there near grid nodes.

Not yet probed: Sword_sheath, left_sword, Base_Part_01 (expected to be the
same; same error signature).

## What the post-reconstruction steps do with it (measured, torso_girl)

The broken winding output (with `_check` disabled) passed through the default
post steps (`decimate` → conditional `meshfix`), using
[tools/experiments/winding_post_steps.py](../../tools/experiments/winding_post_steps.py)
(`--meshfix-only` for the direct row):

| Path | Faces | NM | Open | Volume | Time |
|---|---|---|---|---|---|
| input part | 699,869 | 15 | 364 | 22,930.492 | |
| winding (weld defect) | 1,635,616 | **1** | 0 | 22,929.940 | |
| → decimate to 699,869 (pipeline target) | 699,868 | **0** | 0 | 22,929.888 | MeshFix then skipped: no defects |
| → decimate to 900k (first probe) | 900,000 | **0** | 0 | 22,929.928 | MeshFix then skipped: no defects |
| → MeshFix directly (no decimation) | 1,635,038 | **0** | 0 | 22,929.939 | 47.0 s; 578 faces removed, no self-intersections |

The pipeline's target is the part's own size (699,869; `repairer.py` sets
`faceCount` before the part steps run). The first probe used 900k; the
rerun at 699,869 (2026-10-05) gives the same result.

Decimation removed the defect at both targets: quadric collapse takes the 0.0001·h
sliver edges first. MeshFix alone also fixes it, with volume unchanged to
five digits. The model fails only because `winding._check` rejects the
output before those steps run. Only torso_girl was run through the post
steps. Hip (NM 3, 8 blocks) has not been.

## Decision (owner, 2026-10-04): loosen the guard *and* fix the weld

Not implemented. Two changes, both wanted:

1. **Loosen `winding._check`.** Failing a part because NM edges exist
   defeats the purpose of having MeshFix as the last step to fix NM. NM
   edges in winding output should pass on to decimation and MeshFix.
   To settle while planning:
   - **Keep `degenerate` as a hard failure (or remove such faces).** The
     post-reconstruction decimation receives arrays, and degenerate faces
     segfault PyMeshLab's quadric decimation on that path (see
     [decimation-segfault.md](decimation-segfault.md)). Winding's `_weld`
     already drops them, so this guard costs nothing.
   - Keep empty output and non-finite coordinates as hard failures.
   - Open edges: decide whether they also pass to MeshFix (none were
     observed in these failures).
   - Bound the risk: MeshFix has run past the 1 h timeout when given
     thousands of NM edges ([post-wrap-meshfix-timeout.md](post-wrap-meshfix-timeout.md)).
     Winding's defect is 1–3 edges, but a loosened guard has no upper limit.
   - `_check` is winding's documented success contract (closed, manifold;
     see the winding entry in [modules.md](../refactor/modules.md)).
     MeshFix is conditional, and custom `part_steps` replace the default
     sequence, so a later fix isn't guaranteed for every caller. Cover
     direct and custom callers and shape preservation, not only torso_girl's
     edge counts.
2. **Fix the weld so winding doesn't produce NM** (directions below). The
   guard then rarely matters, and the output stays clean for the designs that
   rely on it.

## Fix directions for the weld (not implemented, not tested)

- **Keep the field off zero at grid nodes:** raise the existing floor
  (`winding.py` already clamps `|field|` to `h·1e-6`) to order 1e-3·h, so a
  vertex lands further from the node than the weld tolerance. The surface
  shift is **not bounded** by the floor: when both values on an edge are
  below it, interpolation moves toward the middle of the edge. The field is
  also not an exact distance near open or overlapping geometry. Needs a
  measured bound on displacement, plus thin-feature and block-seam tests.
- **Weld only seam vertices** (those on block boundary planes), and skip the
  weld entirely for one block.
- **Merge whole clusters consistently** (connected components within a
  tolerance instead of rounded keys). Harder to guarantee manifold output.

Of the earlier candidates, letting NM through to MeshFix is now adopted as
the guard change above, as a safety net alongside the weld fix. An
alpha-wrap fallback isn't needed for these failures. A grid shift would only
move the coincidence elsewhere.
