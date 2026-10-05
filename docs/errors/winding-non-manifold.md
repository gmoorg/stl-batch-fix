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

Implemented 2026-10-05 (see Fix below). Two changes, both wanted:

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
   - Bound the risk: given thousands of NM edges, MeshFix was still running
     when the 1 h `per_file_timeout` killed the job (3,604 s, both cases) ([post-wrap-meshfix-timeout.md](post-wrap-meshfix-timeout.md)).
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

## Fix (implemented 2026-10-05)

**Weld by grid edge.** `igl.marching_cubes` places every vertex on one grid
edge and reports which (`E2V`, the edge's two corner indices; it was
discarded before). `winding._edge_ids` turns that into a global edge id per
vertex (block origin added), and `_weld` merges vertices with the same id.
Seam copies from neighbouring blocks (equal up to ≤ 4e-15) merge exactly;
vertices on different edges never merge, however close, so a near-node
cluster can't be merged partially. No coordinate is rounded and there is no
tolerance. A map that isn't one unit grid edge per vertex is a
`RuntimeError`.

**Guard.** `_check` no longer fails on NM edges: they pass on, and the step
detail says `nm=N passed on`. Empty output, non-finite coordinates, open
edges and degenerate faces still fail (open edges would mean a seam or
marching-cubes defect; none were seen). No NM cap: winding's defect was 1–3
edges; the MeshFix timeouts came from thousands made by decimation. A direct
`reconstruct` / `repairer.repair` caller may receive NM edges;
`processor`'s judge flags any left in a final output (`UNREPAIRED`).

All six failing models
([tools/experiments/winding_edge_weld_check.py](../../tools/experiments/winding_edge_weld_check.py),
batch h and 10 GB plan, one run each, both welds on the same
marching-cubes output):

| File | h | Blocks | Rounding weld faces / NM | Grid-edge weld faces / NM / open | Volume change |
|---|---|---|---|---|---|
| torso_girl | 0.10885 | 1 | 1,635,616 / 1 | 1,635,672 / 0 / 0 | 0 |
| Hip | 0.15 | 2 | 2,559,212 / 3 | 2,559,324 / 0 / 0 | 0 |
| Sword_and_head1 (part 1) | 0.15 | 1 | 801,736 / 1 | 801,764 / 0 / 0 | 0 |
| Sword_sheath | 0.15 | 2 | 1,069,044 / 1 | 1,069,072 / 0 / 0 | 0 |
| left_sword (part 1) | 0.15 | 2 | 1,590,828 / 1 | 1,590,892 / 0 / 0 | 1e-6 |
| Base_Part_01 | 0.15 | 2 | 3,749,762 / 1 | 3,749,912 / 0 / 0 | 1e-6 |

Volume is `scanner.component_volume`, change in model units (absolute). The
grid-edge weld keeps the faces the rounding weld collapsed (28–150 more).
Not run through the full batch yet.

Tests: `tests/tests/test_winding.py` — the weld merges seam copies and keeps
close neighbours apart (fails with the old weld), edge-map contract, closed
and manifold with equal face counts for 1–3 blocks far from the origin and
at small and large scale, NM passed on with the step detail, residual NM
flagged by the judge, and the remaining hard failures.

**Not addressed here:** the near-node sliver clusters themselves remain
(manifold, tiny triangles). They were suspected of triggering the vertices
post-wrap decimation throws off the surface; a causal test refuted that
([post-wrap-meshfix-timeout.md](post-wrap-meshfix-timeout.md)). Float32 `Geometry` can still round two
near-node vertices to the same point (distinct indices, zero area); that is
not degenerate in `scanner`'s sense and not NM.

## Earlier weld directions (superseded)

The grid-edge weld replaced these candidates:

- **Raise the field floor** (`h·1e-6` → ~1e-3·h) so vertices land outside
  the weld tolerance. Its surface shift was not bounded: when both values
  on an edge are below the floor, interpolation moves toward the middle.
- **Weld only seam vertices.** Clusters at grid nodes on seam planes could
  still merge partially.
- **Merge whole clusters** by connected components within a tolerance.
  Harder to guarantee manifold output.

An alpha-wrap fallback isn't needed for these failures. A grid shift would
only move the coincidence elsewhere.
