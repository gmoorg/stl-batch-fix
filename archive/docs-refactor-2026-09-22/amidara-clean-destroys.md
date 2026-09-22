# Owner's notes

I did not printed Amidare base before but I did print hands without issues

# Amidara: the pipeline destroys a printable model

Measured 2026-09-21 against the current tree, on
`Done/HardWitch-Games/Amidara Blustmorn/1-12mm/Amidara_Blustmorn_1-12_base.stl`
and its sibling `..._hands_2.stl`.

This file records findings only. Nothing here has been implemented, and the
pipeline's steps and their order are unchanged. The owner has deferred all
module changes until this investigation is reviewed.

> **Keep [discovered bugs](discovered-bugs.md) in step with this file.** It
> carries the short status of this investigation and Mandy's, and is the page to
> read — and to update — when either one moves.

## The verdict

| model | source | after the pipeline | guard says | owner's eye says |
|---|---|---|---|---|
| `base` | 315,482f, watertight | 279,168f, **96.93%** | PASS | **broken** |
| `hands_2` | 150,656f, watertight | 145,290f, **100.00%** | PASS | **artifact added** |

Both sources are pristine — `open=0, nm=0, degenerate=0`, one shell each — and
the owner validated both visually. The pipeline deletes **36,314 faces** from
`base` and the result is unusable. Every automated check passes it.

`hands_2` is the sharper case: 100.00% volume kept, topologically perfect, and
the owner found geometry in the output that is not in the input.

## Why it happens

The destruction is PyMeshFix's, and it is not a regression in how we call it.

**Our own steps are not the cause.** With CLEAN and orientation both disabled,
PyMeshFix still returns 97.26% from the pristine mesh. `merge_close_vertices`
changes the final result by nothing at all, and orientation accounts for 0.33
of a 3-point loss.

**The call did not change.** Reading the legacy `stl_batch_fix.run_pymeshfix`
suggested three differences from `libs/meshfix.repair`; all three are refuted:

| variant | faces | volume |
|---|---:|---:|
| refactor: `load_array` + `fill_small_boundaries(0,True)` + `clean()` | 279,140 | 97.26% |
| + `remove_smallest_components` (legacy has it, we do not) | 279,140 | 97.26% |
| legacy: `MeshFix(path).repair()`, reading the file | 279,140 | 97.26% |
| legacy sequence from our arrays | 279,140 | 97.26% |

The legacy script — the one that worked before — destroys this model today too.
File-vs-arrays makes no difference, and legacy's paired-open-vertex snap cannot
apply because it returns early with no open edges.

**The cause is self-intersection.** `PyTMesh.select_intersecting_triangles`
reports triangles whose surfaces pass through each other, and `clean()` deletes
them. The default count overstates it — coincident edges and vertices are
counted as intersections unless `justproper=True`, and on a closed manifold
every triangle shares edges with its neighbours:

| model | default | `justproper=True` |
|---|---:|---:|
| `base` | 22,707 (7.20%) | **11,365 (3.60%)** |
| `hands_2` | 5,333 (3.54%) | **1,289 (0.86%)** |

So the self-intersections are real, at half the first-reported scale.

## But they are not a printing defect

Every one of the twelve Amidara parts is watertight — `open=0, nm=0,
degenerate=0` — and every one but the tail self-intersects. The owner printed
ten of them, including the head **as-is with no decimation**:

| part | faces | self-intersections | printed |
|---|---:|---:|---|
| **`head`** | 573,394 | **67,166 (11.71%)** | **yes, undecimated** |
| `base` | 315,482 | 11,365 (3.60%) | **no** |
| `body` | 1,208,930 | 11,079 (0.92%) | yes |
| `legs_right_main` | 523,868 | 9,384 (1.79%) | yes |
| `base_chair` | 302,912 | 1,597 (0.53%) | no |
| `hands_2` | 150,656 | 1,289 (0.86%) | yes |
| `legs_left_main` | 517,432 | 782 (0.15%) | yes |
| `base_chair_alter` | 314,046 | 312 (0.10%) | yes |
| `legs_right_alter` | 507,002 | 264 (0.05%) | yes |
| `legs_left_alter` | 498,518 | 149 (0.03%) | yes |
| `hands_1` | 149,574 | 69 (0.05%) | yes |
| `tail` | 199,996 | 0 | yes |

Counts are `select_intersecting_triangles(justproper=True)`, which excludes the
coincident edges and vertices that a closed manifold necessarily has.

**The head settles it.** It printed without issue at **11.71%
self-intersecting**, more than three times `base`'s rate and nearly six times
its count, straight from source with no decimation. `base` is *less*
self-intersecting than a part that demonstrably prints.

A slicer reduces each layer to 2D polygons and resolves overlaps with a fill
rule, so self-intersecting geometry inside a solid region is absorbed into the
union and the toolpath is unchanged. Self-intersection breaks boolean
operations, offsetting and some mesh algorithms; FDM slicing of a watertight
manifold is not among them. Ten real prints across this set say so, at rates
from 0% to 11.71%.

`base` itself has been sliced in Bambu Studio — no complaint, correct layer
simulation — but not printed. It no longer carries the argument alone.

**So the pipeline destroys a printable model to remove a defect that does not
affect printing.** PyMeshFix is not malfunctioning: `clean()` enforces
*geometric validity*, which is a stricter standard than *printability*, and this
project needs only the latter. Where the two conflict, the slicer is the
authority.

## An independent tool disagrees about the remedy

The owner ran `base` through an online repair service. Its analysis:

    0 Naked edges          0 Non-Manifold edges    0 Duplicate faces
    0 Planar holes         0 Degenerate faces      0 Disjoint shells
    0 Non-Planar holes     1506 Inverted normals

It agrees with our scanner on every count we measure — and reports a defect we
do not name: **1,506 inverted normals**. Its repair **added** geometry:
155,710 → 165,132 vertices (+9,422) and 315,482 → 340,960 triangles (+25,478).

Against PyMeshFix on the same input, the two tools move in opposite directions:

| tool | vertices | triangles | what it does |
|---|---:|---:|---|
| online repair | +9,422 | **+25,478** | re-meshes around inverted patches |
| PyMeshFix `clean()` | −16,138 | **−36,342** | deletes self-intersecting triangles |

**We do measure this defect, under another name.** `scanner.winding_seams`
reports **922 seam edges in 129 closed loops** on `base` — a seam edge is where
two adjacent faces disagree about which side is out, so it is the boundary of
an inverted patch. 1,506 inverted faces bounded by 922 shared edges is the same
finding in different units.

Two reasons it never surfaces:

- `Scan.is_clean` is `open_edges == 0 and non_manifold == 0`. Winding seams are
  not in it, so `base` reads as perfectly clean.
- Signed volume equals component volume (50,629.38 both ways), so the inversions
  are **local patches**, not a flipped shell, and no volume check can see them.

**The inversions do not explain the self-intersections.** Applying either
orientation filter leaves the self-intersection count at exactly 11,365, and
both make the winding worse:

| mesh | seam edges | closed loops | signed volume | self-int |
|---|---:|---:|---:|---:|
| source | 922 | 129 | 50,629.38 | 11,365 |
| `re_orient_faces_coherently` | 1,190 | 29 | **35,286.33** | 11,365 |
| `re_orient_faces_by_geometry` | **7,659** | **562** | 50,627.82 | 11,365 |

`coherently` loses 30% of the signed volume — it flipped large regions the wrong
way, which is the failure already recorded in
`tests/tests/test_repairer.py::test_re_orient_faces_coherently_is_not_used`
(formerly `repairer.DO_NOT_RETRY`, removed as a production constant since it
was only ever read by its own tests — see modules.md).
`by_geometry`, which the pipeline runs on every part, multiplies the seam edges
by more than eight. Neither is a fix, and the self-intersections are independent
geometry rather than a symptom of the winding.

### Can PyMeshLab reconstruct the way the online tool does?

**Not with a boolean, and the reason is worth stating precisely: a boolean union
is not a repair function.** It is a modeling operation that combines two solids,
and it *requires* valid solids as input rather than producing them. Its refusal
below is a precondition check, not a diagnosis of a defect it exists to fix. An
earlier version of this section called it "PyMeshLab's equivalent of the online
tool"; that was wrong.

PyMeshLab's candidates, with that correction in mind:

| filter | result on `base` |
|---|---|
| `generate_boolean_union` (self-union) | **refused** — see below |
| `compute_selection_by_self_intersections_per_face` | selects **9,193 faces** (report only) |
| `meshing_remove_folded_faces` | 0 faces removed; self-int 11,365 → 11,323 |
| `meshing_isotropic_explicit_remeshing` | would rewrite every triangle; not tried |
| `meshing_close_holes`, `meshing_repair_non_manifold_*` | nothing to act on |

**The boolean refused, and its reason is the finding:**

    Mesh inputs must induce a piecewise constant winding number field.
    Make sure that both the input mesh are watertight (closed).

`base` *is* watertight. What it lacks is consistent **orientation** — a
piecewise-constant winding number field needs that, and 1,506 inverted normals
break it. So the reconstruction path is blocked by the winding defect, not by
the self-intersections it would resolve.

That creates a dependency the pipeline cannot currently satisfy: reconstruction
needs correct winding first, and both PyMeshLab orientation filters make the
winding worse on this mesh (922 seam edges → 1,190 or 7,659).

**PyMeshLab has no repair for this defect at all.** Its full `meshing_*` list
offers exactly three tools touching orientation — the two re-orient filters and
`meshing_invert_face_orientation`, which flips every face unconditionally. There
is no `repair_self_intersections` and no `fix_inverted_normals`. It repairs
non-manifold edges and vertices, which `base` does not have, and nothing for
what `base` does have.

So the gap is not that we are calling the wrong PyMeshLab filter. PyMeshLab
cannot fix 1,506 inverted normals, and the online tool evidently can.

### PyMeshFix *can* fix the winding — by cutting the mesh open

`PyTMesh.fix_connectivity()` has never been called by this project, and it does
exactly what the orientation filters could not:

| step | seam edges | open | self-int | volume |
|---|---:|---:|---:|---:|
| source | 922 | 0 | 11,365 | 100.00% |
| `fix_connectivity()` | **0** | **2,048** | 11,365 | 95.78% |
| `strong_degeneracy_removal(3)` | **0** | **2,048** | 11,365 | 95.78% |

Both take the winding seams to zero — the defect the online tool reports as
1,506 inverted normals — and both do it by **tearing the mesh open at the
inverted patches** rather than flipping them. Inconsistency is resolved by
removing the inconsistent connections, which costs watertightness and 4.2% of
the volume. (The two produce identical figures, suggesting degeneracy removal
calls the same connectivity pass.)

Neither touches the self-intersections, which stay at 11,365 throughout. That
independently confirms the two defects are unrelated.

The boolean was offered the source, the `fix_connectivity` result and the
degeneracy-removal result, and **refused all three**: it needs watertight *and*
consistently wound, and each of those steps alone trades one for the other.

### A sequence that repairs `base` without destroying it

Filling the holes `fix_connectivity` makes closes the trade:

    PyTMesh.fix_connectivity()            # winding seams 922 -> 0, opens 2,048 edges
    PyTMesh.fill_small_boundaries(0, True)  # closes them again

| | source | current pipeline | `fix_connectivity` + fill |
|---|---:|---:|---:|
| faces | 315,482 | 279,140 (**−36,342**) | 320,152 (**+4,670**) |
| open edges | 0 | 0 | **0** |
| non-manifold | 0 | 0 | **0** |
| degenerate | 0 | 0 | **0** |
| winding seams | **922** | 0 | **0** |
| volume | 100% | 97.26% | **99.81%** |

Every defect the online tool reported is resolved, the mesh stays watertight,
and **geometry is added rather than deleted** — the same direction the online
tool moved (+4,670 triangles against its +25,478).

**Confirmed at the pipeline level, not just the bare `PyTMesh` calls above.**
Running `repairer.repair()` on `base` with `ENABLE_WELD`,
`ENABLE_CLEAN_MERGE_CLOSE`, `ENABLE_ORIENT` and `ENABLE_CLEAN` all disabled
(`ENABLE_PART_TOOL` and `ENABLE_FILL_BOUNDARIES` left on) reaches the identical
320,152 faces / 99.81% / `lost_vertices=0`, through `step_meshfix_repair`'s own
`fill_small_boundaries` call rather than a standalone `fix_connectivity()`. So
`ENABLE_CLEAN` alone isolates `clean()`'s contribution: with it off, the part
survives with zero face loss. The output still carries the unrepaired
self-intersections (this document's "not a printing defect" section) —
disabling `ENABLE_CLEAN` trades `clean()`'s over-aggressive deletion for
leaving self-intersections in place, not a genuine repair of either defect.
Not yet adopted as a config change.

### The owner's inspection refutes the table above

Every conclusion in this section was drawn from counters, and the counters are
wrong. On inspection the owner reports: `base_INTONLY.stl` is broken;
`base_DEGONLY.stl` and both `FIXCONN` outputs look broadly correct but
**the repaired faces are inverted** in all three.

`FIXCONN_FILL` measures `seam=0`, so the pipeline's winding test calls it
perfect. A direct check of the 4,592 new faces against the old geometry they
touch says otherwise:

| output | new faces | new/old edges wound the same way (flipped) | `winding_seams` |
|---|---:|---:|---:|
| `base_FIXCONN_FILL.stl` | 4,592 | **762** | **0** |
| `base_INTONLY.stl` | 743 | 0 | 0 |

**`scanner.winding_seams` cannot see this.** Those 762 flipped pairs sit on
edges where the same directed edge appears more than twice — a non-manifold
configuration — and the seam test only examines edges with exactly two incident
faces. `fill_small_boundaries` produced overlapping geometry, and the
inconsistency hides precisely where the seam test does not look.

So the sequence does **not** repair `base`. It exchanges 922 visible winding
seams for ~762 invisible ones, and the earlier claim here that it "resolves
every defect the online tool reported" was false. The boolean accepting the
mesh was likewise not the independent verification it was presented as: the
boolean's own output carried 353 non-manifold edges, which should have been
read as a warning rather than a footnote.

Also real, and unchanged by the correction: self-intersections rise from 11,365
to 15,153 because the fill creates new ones. PyMeshLab's `meshing_close_holes`
is not a substitute for `fill_small_boundaries` — 38 faces added, 1,994 edges
still open.

**Nothing here is a working repair.** `INTONLY` is broken outright; the other
three are inverted where they were patched. The sequence is one measured
experiment on one mesh, refuted by the only test that has been reliable all
day, which is the owner opening the file.

### `fix_connectivity` + fill, then step 15 on its output — also refuted

Tested 2026-09-21, the "both, in order" row [open issues](open-issues.md) left
blank: run `libs/meshfix.repair()` — `fill_small_boundaries(0, True)` then
`clean(10, 3)`, the pipeline's actual step 15, not a bare `clean()` call — on
the `FIXCONN_FILL` output above, rather than on the raw source.

| sequence | faces | open | nm | winding seams | self-int (`justproper`) | volume |
|---|---:|---:|---:|---:|---:|---:|
| source | 315,482 | 0 | 0 | 922 (129 loops) | 11,365 | 100.00% |
| `fix_connectivity()` + fill | 320,152 | 0 | 0 | 0 | 15,153 | 99.81% |
| **+ step 15 (fill + `clean(10,3)`)** | 279,202 | 0 | 0 | 0 | **0** | 97.49% |

Every counter this project has clears: watertight, no non-manifold edges, no
winding seams, and `select_intersecting_triangles(justproper=True)` — the same
measure that found 11,365 self-intersections on the source — finds **zero**.
Output: `/mnt/sda2/STL/_validate/base_FIXCONN_FILL_CLEAN.stl`.

**The owner inspected it. `clean()` deleted original faces — geometry present
in the source `base.stl`, not just the patch geometry `fix_connectivity` +
fill added.** Confirmed by the owner: the missing material is original
geometry, the same failure mode `clean()` has on the raw source, not a defect
confined to the newly-added patches. The hypothesis this experiment was built
to test — that `clean()` would delete only the inverted patches surgically,
because the winding is already consistent by the time it runs, rather than
cascading into the wholesale loss it causes on the raw source — is refuted.
`clean()`'s self-intersection removal cascades through retriangulation
regardless of what feeds it; it is not selective between patch and original
geometry. Every counter measured this as the cleanest result in this entire
document, and it deleted real geometry anyway.

This is the strongest instance yet of the measurement gap this document keeps
finding: not just topology (`open=0, nm=0`) or volume (`hands_2`'s 100.00%)
but every counter available, including self-intersection, calling a broken
mesh clean.

A same-direction winding-incidence check was attempted here, in the same
spirit as the 762-flipped-pairs finding on `FIXCONN_FILL` alone (above): for
each face present in an output but absent from the source, whether it shares
a directed edge with an old face walked the *same* way (the defect signature,
since correctly-oriented adjacent faces walk a shared edge in opposite
directions). **This did not reproduce a comparable count and should not be
read as corroborating or contradicting the 762 figure.** The script that
produced 762 is not available to this session; a new implementation was
written for this experiment, matching vertices across PyMeshFix's
independently-renumbered output arrays by nearest-neighbour position
(tolerance `1e-4`, one-to-one within each mesh — PyMeshFix does not preserve
vertex indices or exact coordinates even for geometry it leaves alone,
confirmed by hashing `FIXCONN_FILL` regenerated from source: byte-identical
to the original, `sha256:73da68...724e`). Under one-to-one matching it found
0 same-direction incidences on `FIXCONN_FILL` alone — where the owner's own
inspection and the 762 count both say the defect is present — so this
particular check is not sensitive enough to trust, on either output. Treat
"0 incidences detected" here as a statement about this ad hoc method's
detection floor, not about the mesh.

### Blender cannot see the defect either — same blind spot as the scanner

Owner's proposal, 2026-09-21: `fix_connectivity()` + `fill_small_boundaries()`
genuinely produces the 762-flipped-pair defect, and Blender's repair loop
exists specifically to find and fix non-manifold geometry — so run Blender
*on the intermediate*, where it has a real defect to act on, rather than on
the pristine source (`open=0, nm=0`), where Blender's own early-exit check
("mesh already clean — writing without repair", `repair.blender`'s first
check before the main loop) fires immediately and it does nothing.

Measured: `blender.step_blender_repair(FIXCONN_FILL)` — 320,152 faces in,
**320,152 faces out, byte-identical**. `ok=True`, `BLENDER_OK`. No change at
all.

**Why: `repair.blender`'s `count_defects`/`nm_faces_of` have the identical
blind spot as `scanner.winding_seams`.** Both ask *how many* faces share an
edge (`not edge.is_manifold` in Blender's BMesh terms; `counts > 2` in
`scanner`'s), never *which direction* each face walks it. The 762 flipped
pairs sit on edges genuinely shared by exactly 2 faces — wound the same way,
which is the defect — so `is_manifold` reads them as fine. Confirmed from the
script directly (`libs/blender_fx/repair.blender`'s `count_defects`,
`nm_faces_of`): no code path in the repair loop inspects edge winding
direction at all, only edge-to-face cardinality. Forcing the loop to run
anyway (bypassing the "already clean" early exit) would not help — the loop
that would run afterward is built entirely on the same defect-free reading,
so it would still see nothing to repair.

**This sequence is refuted for the same underlying reason the scanner gap is
open (see below): neither this project's own topology counters nor Blender's
repair loop test winding consistency on non-manifold-cardinality edges.**
Fixing that requires a genuinely different check — not a different tool run
on the same numbers.

`scanner.winding_seams` misses winding inconsistency on edges shared by more
than two faces. `Scan.is_clean` does not consider winding at all. Between them,
a mesh can carry hundreds of inverted patches and read as flawless — which is
how this document came to record a destroyed mesh as repaired.

Worth noting for cross-checking: PyMeshLab independently counts 9,193
self-intersecting faces where PyMeshFix counts 11,365 at `justproper=True`.
Different algorithms, same order of magnitude — the defect is real and not an
artifact of one library.

## The arguments we could try instead

`libs/meshfix.py` calls `clean(CLEAN_MAX_ITERS, CLEAN_INNER_LOOPS)` =
`clean(10, 3)` explicitly (`libs/meshfix.py:116-117,148`) — not bare, and this
matches the library default by stating it, not by omitting it. An earlier
version of this section said the call was bare; that was wrong. The
parameters change the outcome substantially, and none of the alternatives
below are what the pipeline supplies:

| variant | faces | volume | self-intersections left |
|---|---:|---:|---:|
| `clean(1, 1)` | 292,444 | **99.73%** | **6,187** |
| `clean(1, 3)` | 279,282 | 97.09% | — |
| `clean(3, 3)` | 279,174 | 97.01% | — |
| `clean()` = `(10, 3)`, current | 279,174 | 97.01% | — |
| `fill(0,True)` + `clean()`, shipping path | 279,140 | 97.26% | **0** |

Everything from `inner_loops=3` upward converges, so the deletion happens in the
first pass and later iterations add nothing. `clean(1,1)` keeps 13,300 more
faces and is faster, but leaves 6,187 self-intersections in the output — whether
that is better is a question for a slicer, not a face count.
`base_GENTLE.stl` and `base_CURRENT_pmf.stl` are written for that comparison.

Full signatures:

- `clean(max_iters=10, inner_loops=3)` — called explicitly with these values.
- `fill_small_boundaries(nbe=0, refine=True)` — we pass `(0, True)`. `nbe=0`
  means fill **all** boundaries regardless of size, and `refine=True` produced
  `WARNING- Fill holes: Refinement stage failed to converge. Breaking.` on a
  mesh whose input had no boundaries at all.
- `select_intersecting_triangles(tris_per_cell=50, justproper=False)` — never
  called. Reports without deleting; it produced the numbers above.
- `remove_smallest_components()` — no arguments, keeps **only the largest
  component**. Legacy calls it; we do not. A no-op on single-shell `base`, but
  restoring it would silently discard every secondary shell, which is the
  failure the shell split exists to prevent.

## Why this went unnoticed

Both models are `is_clean` and under `MAX_FACES=900000`, so under the gate the
owner has specified — skip repair for a part that is already clean and within
the decimation budget — **neither would ever reach PyMeshFix**:

| model | faces | `is_clean` | under budget | would skip repair |
|---|---:|---|---|---|
| `base` | 315,482 | yes | yes | **yes** |
| `hands_2` | 150,656 | yes | yes | **yes** |

The defect is latent rather than new. Models like these were not being sent to
repair, so nothing surfaced it.

That gate is recorded in [open issues](open-issues.md) and is **blocked on this
investigation**: adding it now would skip repair for models that genuinely need
fixing, trading one silent failure for another. It is defence in depth, not the
fix.

## Open questions

**Does a self-intersecting watertight manifold always slice correctly, or only
usually?** Ten printed parts here say it does at rates up to 11.71%, which is
strong but still one model family in one slicer. Are there configurations — a
surface passing through itself so the inside/outside test flips, a shell
penetrating a shell — where the fill rule yields the wrong solid rather than
the union? If such cases exist, the rule is about the *kind* of
self-intersection rather than the count, and no measurement here distinguishes
kinds.

**How common is harmless self-intersection beyond the Amidara family?** Eleven
of twelve parts here — all from one model — carry it and ten printed. If that
holds across `/mnt/sda2/STL/{Done,Fixing}`, `clean()` has been damaging the
corpus broadly and nobody has looked. A sample of 20–30 unrelated models with
`justproper=True` would settle the scale.

### Answered: the deletion cascades, and there is no surgical alternative

`PyTMesh` exposes the two halves of `clean()` separately, as
`strong_intersection_removal(max_iter)` and `strong_degeneracy_removal(max_iter)`.
Run alone on `base`:

| call | faces deleted | open | volume | self-int left (loose/strict) |
|---|---:|---:|---:|---|
| `strong_intersection_removal(3)` | **36,200** | 0 | 97.09% | 4 / 4 |
| `strong_degeneracy_removal(3)` | **0** | **2,048** | 95.78% | 22,707 / 11,365 |
| `fill(0,True)` + `clean()`, current | 36,342 | 0 | 97.26% | 0 / 0 |

**The deletion is self-intersection removal, and it cascades.** Intersection
removal alone accounts for 36,200 of the 36,342 faces — 99.6% of the loss.
11,365 offending triangles cost 36,200 faces, about 3.2x, so removal propagates
through retriangulation rather than deleting the offenders alone.

**It removes on the strict definition.** It clears the 11,365 proper
intersections, leaving 4, without needing to touch the ~11,000 coincident
edge and vertex cases the loose count includes. So `clean()` is not deleting
ordinary neighbouring triangles; that worse possibility is ruled out.

**There is no surgical alternative here.** Degeneracy removal was the hoped-for
answer — degenerate faces do break slicers, self-intersections demonstrably do
not — but `base` has **zero degenerate faces**, so the call deletes nothing, and
it *opens 2,048 edges* in a watertight mesh while doing so. It is not a safe
substitute for `clean()`; it is a second way to damage the model.

`justproper=True` appears in this document only on
`select_intersecting_triangles`, which reports and deletes nothing. No result
above was produced with it applied to a mesh.

**What should the pipeline's standard be?** `scanner` measures open edges,
non-manifold edges and degenerate faces, and nothing measures self-intersection
— on this evidence perhaps nothing should. But Mandy does need repair, and its
hair loss is also PyMeshFix's orientability cut, so "never run PyMeshFix" is not
the answer either. There is a precedent in the repository for exactly this
distinction: `scanner.open_loops` and `open_loops_are_printable` measure holes
against layer height rather than demanding zero. Self-intersection may want the
same treatment.

## What was measured, and what was not

Measured: both sources' topology and self-intersection counts; the full stepwise
pipeline on `base`; every PyMeshFix call variant above; the legacy call paths;
`clean()` and `fill_small_boundaries` parameter sweeps; remaining
self-intersections after each.

Not measured: whether `clean(1,1)`'s output slices or prints correctly; what the
36,342 deleted faces are geometrically; any model beyond these two; whether
`hands_2`'s fabricated artifact has the same cause as `base`'s loss; the corpus
frequency of self-intersection.

Environment: pymeshfix 0.18.1 (installed 2026-08-24), pymeshlab 2025.7.post1
(2026-08-28), numpy 2.5.2, Python 3.12.3. No Amidara run appears in
`logs-preserved-*` or `_logbackup`, so the earlier success the owner recalls is
undated — a different file, a different stage, or a different library version
all remain possible.

## A caution about this account

Five claims in the session that produced this file were stated confidently and
then refuted by measurement: that no evidence justified CLEAN-before-SPLIT; that
CLEAN damaged the model; that a costume01 fragment was meaningful geometry; that
rejoining before repair could not help; and that the legacy PyMeshFix call
differed from ours. Each was plausible and fitted the data at hand.

The slicer claim above was the most likely sixth, and it survived — but only
after two corrections from the owner. An early draft said both measured models
printed; in fact only `hands_2` had. The owner then supplied the full printing
record, which is what makes the claim hold: ten parts printed, the worst of
them at 11.71% self-intersecting and undecimated.

Note the shape of that: the claim was written before the evidence that supports
it existed, and the evidence arrived from the owner rather than from
measurement. It happens to be right. The next one may not be.
