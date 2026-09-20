### FIXED — `welder`'s tolerance is now relative to the edge length

**Fixed 2026-09-17.** Two changes to `_find_in`, and the second is the one that
matters:

1. **Candidates are nominated topologically**, not by scanning every
   open-boundary vertex against every open edge. A candidate M must sit on an
   open edge reaching one end of the spanning edge. (Not *both* ends: an edge
   subdivided twice has three pieces, so neither interior vertex connects
   straight to the far end. Requiring both was tried and missed that case
   entirely.)
2. **The distance test is a fraction of the edge's own length**, so one number
   works at every scale. `DEFAULT_TOLERANCE` is now `1e-5` of `|ac|` rather
   than `1e-6` mm.

**Verified across nine radii, 0.5 mm to 5000 mm** — all find their junction and
repair clean, including 50, 100 and 200 which previously found nothing. The
skipped test that recorded the bug now passes and its marker is gone.

**And calibrated on real data for the first time.** Sweeping the fraction on
costume01 (113.7 mm diagonal):

| fraction | found | worst distance / edge |
|---|---|---|
| 1e-7 | 4 | 5.2e-04 |
| **1e-6** | **5** | **1.5e-03** |
| **1e-5** | **5** | **1.5e-03** |
| 1e-4 | 10 | 6.1e-02 |
| 1e-3 | 13 | 5.4e-01 |

Five is stable across two decades, then it collapses: a candidate 6% of an
edge-length off the line is a hole with a fan across it, not a subdivided edge.
Real junctions at ~1.5e-03 against false ones at 6e-02 is a 40x gap, so the
value is not a fine judgement.

#### What looked like a regression was a mis-classification

On costume01 the new search finds **0** where the old found **1**, and my first
reading was that the topological filter was wrong for real data. It is not — and
reverting on that reading would have destroyed a verified fix. The user stopped
it; see `memory/prove-before-reverting.md`.

Traced with real indices. The vertex the old code found, M=246400, on the
spanning edge of face 6383 = (246440, 247679, 246330):

| vertex | faces using it |
|---|---|
| a = 246330 | 2352, 6383, 6681 |
| c = 246440 | 2351, 2353, 6383, 793245, 793246 |
| **M = 246400** | **4505, 4506, 6381, 6384, 6682** |

M's face set is **disjoint from both**. Neither edge (a,M) nor (M,c) exists in
the mesh. M belongs to a separate piece of surface that happens to pass through
almost the same point — the edge is **0.00878 mm** long and M sits **6.9e-07 mm**
off it, a `fast_simplification` collapse artefact on a sub-10-micron edge.

**So it is not a T-junction, and another tool already repairs it.** Tracked
through CLEAN filter by filter on the full mesh: `merge_close_vertices` takes
the a-c edge from **1 face to 2** — properly paired — with all three vertices
surviving (`a+ c+ M+`). Nothing is deleted; the two surfaces are stitched. That
is the correct tool, because the defect is *two surfaces that should be joined
and are not*, which is what a proximity merge is for.

The old code's hit was therefore a **false positive**: geometric proximity with
no topological basis. `welder` finding 0 is right, and the end-to-end result was
never affected — costume01 finishes nm=0 at 99.99% volume either way, because
CLEAN was always closing it.

**Still to check**: the "5 junctions" in the calibration table above may all be
this same class. That number should not be trusted as a T-junction count until
each is traced the way M=246400 was.

---

