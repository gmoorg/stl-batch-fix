### Known gaps

**The orientation guard is wrong.** `scanner.volume()` is one signed total, so
`volume < 0` fires only on a **wholly** inverted model. A reversed *region*
leaves the total positive — `sphere_seam` is +2146.2 with its cap backwards and
the guard skips it. Tolerable because PyMeshFix re-winds seams itself at step
4, but two cases slip through: a region PyMeshFix *deletes* rather than flips
(the head-deletion case), and a region larger than half the model, where the
sign flips and `by_geometry` inverts the correct majority.

**The fix is positional**: run `by_geometry` *after* the split, per part. An
isolated reversed region does show a negative volume — measured at −974.3
against its host's +3120.5. That would catch `sphere_seam` while still skipping
`Mandy…-simp`, where the step was pure churn (it inflated seams 4/1 → 23/3 and
the final result was byte-identical without it).

**CLEAN is unconditional and should not be.** On costume01 it tears 1,225 open
edges that PyMeshFix then closes, for a 40-face difference against not running
it at all. It is only needed when duplicate geometry exists, and `scanner`
cannot detect that — the `doubles` fixture reads entirely clean, and only the
shell count hints, which is useless since two shells is legitimate.

> **Attributed 2026-09-17 by scanning after each filter** rather than blaming
> CLEAN collectively. Two earlier guesses at the culprit were wrong; this is
> measured. On costume01, after `welder`:
>
> | filter | faces | nm | open | orphan | volume |
> |---|---|---|---|---|---|
> | (loaded) | 900,000 | 2,263 | 20 | 0 | +7758.7 |
> | `remove_null_faces` | 900,000 | **2,262** | 20 | 0 | +7758.7 |
> | `merge_close_vertices` | 899,787 | **2,452** ↑ | 17 | **10** | +7758.7 |
> | **`remove_duplicate_faces`** | 898,617 | 2,018 | **1,319** ↑ | 10 | **+7766.5** ↑ |
> | `remove_unreferenced_vertices` | 898,617 | 2,018 | 1,319 | **0** | +7766.5 |
>
> **The culprit is `remove_duplicate_faces`, and the cause is winding.** Of the
> 1,170 duplicate faces on that mesh, **1,166 have OPPOSITE winding** — they
> are zero-thickness sheets, not redundant copies.
>
> Two faces sharing all three vertices with opposite winding **read as
> perfectly clean**: each pairs the other's three edges, so `open=0, nm=0`.
> Delete one and all three edges lose their partner. Demonstrated on a two-face
> mesh: `open=0` becomes `open=3`. The arithmetic matches costume01 — 1,166
> sheets against a rise of 1,302 open edges.
>
> Three corrections this produced:
>
> - **`remove_null_faces` repairs rather than tears** — nm 2,263 → 2,262, and
>   the degenerate face gone. Confirmed separately on a fixture: a mesh with one
>   degenerate face returns to its control exactly.
> - **`merge_close_vertices` makes non-manifold edges worse on real data** —
>   2,262 → 2,452, the documented failure mode now measured outside a fixture.
> - **`remove_unreferenced_vertices` has a real job after all.** It was argued
>   twice in discussion to be protecting against nothing; `merge_close_vertices`
>   creates **10 orphans** on costume01 and this removes exactly those. They are
>   harmless — no edges, no faces, invisible to every check, and `mesh_io.write`
>   drops them because it walks faces — but they do occur.
>
> **Corrected immediately, and the whole framing above is wrong.** "The filter
> tears 1,299 open edges" reads the symptom as the cause. A zero-thickness
> sheet **is a defect**: a closed surface cannot carry two coincident faces of
> opposite winding, because that flap encloses no volume. It reads as clean
> only because the two faces alibi each other's edges.
>
> So `remove_duplicate_faces` is not damaging a sound model. It **removes the
> alibi**, and the open edges it "creates" were always there — masked.
>
> Demonstrated on a tetrahedron, both directions:
>
> | case | before the filter | after |
> |---|---|---|
> | sound tetra + an opposite-winding flap | `nm=3`, vol +0.1667 | **4f, nm=0, open=0**, vol unchanged |
> | tetra *missing* a face, sheet in the gap | `open=0, nm=3` — reads clean | **4f, nm=0, open=0** |
>
> Neither tears anything. In the second case the filter **repairs outright**:
> one face of the sheet becomes the missing surface and the redundant one goes.
>
> On costume01 the 1,166 sheets mask genuine defects in a model that is
> **already broken** — which is consistent with everything else known about
> that file: decimation took it from 3 non-manifold edges to 2,263.
>
> **Deleting both faces of a sheet is explicitly rejected.** The sheet may be
> large, and both faces may be the only surface in that region; removing both
> would cut a real hole in the model rather than expose one.
>
> **What follows for CLEAN**: the "should be conditional" claim above is
> undermined. The 40-face cost is not damage, it is the price of surfacing 1,166
> hidden defects so that step 3 can repair them. Skipping CLEAN would ship a
> model that *reads* clean while carrying them.

**Decimation creates the defects.** `fast_simplification` took costume01 from
**3 non-manifold edges to 2,263** and 448 shells to 491, preserving volume to
0.02%. Repair tools are mostly cleaning up after decimation rather than after
the modeller. Not known whether this is particular to that model or general.

**`repairer` now exists** — see D24. The scratch scripts it was built from had
already been deleted, so it was rebuilt from this record and re-validated
against the saved fixture outputs.

