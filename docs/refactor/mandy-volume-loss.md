# Mandy: what the volume guard is catching

Measured 2026-09-21 against the current tree, on
`Mandy Dinamuuu3D/Complete model (Thingiverse version)/Mandy_Body_Dinamuuu3D.stl`
(2,061,994 faces, 40 shells, 84 open / 266 non-manifold edges).

This file records findings only. Nothing here has been implemented, and the
pipeline's steps and their order are unchanged.

## The verdict

| run | volume kept | verdict |
|---|---|---|
| `mandy_ours_decimated.stl`, the 2026-09-17 input | **46.27%** | DESTROYED |
| full pipeline from source, decimated to 900k | **85.82%** | DESTROYED |

The 46.27% figure reproduces to two decimal places against the measurement
recorded in the archive, so nothing since has changed that path.

## It is real loss, confirmed by eye

The owner inspected `mandy_pipeline_inspect.stl` and identified missing
geometry. The model is bent double: legs straight, torso folded forward, head
upside-down near mid-height, hair hanging toward the floor. An earlier reading
of the damaged Z band as "legs and hips" assumed an upright pose and was wrong.

`libs.meshfix` removes **171,064 faces**, of which **170,610 are one connected
object** — 99.7%. It sits at 13% of model height, spans 32% of it, and is
confined to one side in Y. That is the hanging hair.

In the damaged band the outermost geometry is what goes: maximum radius from
the model axis falls from **24.68 to 15.85**, p90 from 16.23 to 13.50. An
interior wall being removed would raise the median and leave the maximum alone;
this is outer surface.

PyMeshFix says why on stderr:

    WARNING- forceNormalConsistence: Basic_TMesh was not orientable.
             Cut performed.

## What separates the hair from the body

Nothing in `archive/` describes this. The 2026-09-16 note concludes the
opposite — that seam detection is not what helps Mandy, and names
`Amidara_Blustmorn_1-12_base.stl` and `Princess_Leia .../Neck_Cuff.stl` as the
hair-over-scalp cases instead.

Measured on the removed object itself:

- It touches the body at **277 vertices of 85,617 — 0.32%**.
- That count is **identical at every tolerance from 1e-6 to 1e-3**, so those
  vertices are exactly shared rather than merely close. The other 99.68% of the
  hair is geometrically separate.
- The hair arrives already broken: **open=312, nm=67, 30 open boundary loops**,
  48 seam edges and **0 closed seam loops**.

So a boundary exists and is measurable. It is a hairline attachment, not a
winding seam, which is why no current mechanism finds it.

## Mechanisms ruled out by measurement

| hypothesis | result |
|---|---|
| shell splitting fixed it | No. 46.27% unchanged on the same input. |
| seam splitting would fix it | No. Cutting part 0 on all 52 seam edges yields 562,246 faces plus three single triangles. |
| decimation destroyed the seam loop | No. The raw source has the loop (7 edges / 1 loop; 5/1 on the largest shell) and `by_seams` still returns one piece of 941,571 faces. |
| it is the hair, so lower `MIN_SHELL_FACES` | No. The seam isolates three single triangles, so no *second* region clears a floor — but see below: the floor also discards the 562,246-face region that the cut did produce. |

Previously recorded as tried and insufficient: `fill_holes=False` (42.4%),
`by_geometry` first (42.5%), `re_orient_faces_coherently` (raises),
`meshing_repair_non_manifold_edges` (84.8%, then plateaus).

## Why this matters to the volume guard

The guard is correct here. It is the only check that notices: the repaired
result scores `open=0, nm=0` with all 39 components present, so every
topological test calls a model missing its hair perfectly clean. This is the
case D27 built it for.

The owner's separate concern is also real, and is a different event. Three of
sixteen probes are watertight, correctly repaired results that the guard
rejects:

| fixture | kept | where the volume goes |
|---|---|---|
| `sphere_doubles` | 50.00% | **CLEAN** — a coincident duplicate is merged |
| `sphere_allbad` | 49.98% | **CLEAN** |
| `opposite_volume_shells` | 50.00% | **SPLIT** — a component is dropped (A02) |
| Mandy | 85.82% | **inside PyMeshFix** |

Across every probe, no part loses volume inside the part tool: all measure
99.94–100%. Mandy's part 0 is the only one that does, at 84.83%.

So *where* a repair loses volume separates a legitimate duplicate removal from
real destruction, using measurements `Result.steps` already records. That is a
change to what `_decide` reads, not to the pipeline's stages — but it is
unimplemented and undecided.

D27 knew about the duplicate case and accepted it: "the safe direction to be
wrong in, and no real model in the collection has the defect."

## Still open

- Mandy needs a repair, not a better verdict. The hair is non-orientable and
  welded to the body at 277 vertices; nothing in the current sequence addresses
  that. **The owner has ruled out branching to Blender instead of PyMeshFix**,
  so a fix has to work within the existing steps.
- Whether the 0.90 threshold is right is untested at scale. The measured spread
  is wide — sound repairs at 95–100%, Mandy at 85.82%, duplicates at ~50% — so
  no single threshold separates Mandy from the duplicate cases.

## Codex independent review, 2026-09-21

### Scope and method

This is an independent investigation, with no pipeline implementation change.
I loaded the original `Mandy_Body_Dinamuuu3D.stl` from
`/mnt/sda2/STL/Fixing/Mandy/Mandy Dinamuuu3D/Complete model (Thingiverse version)/`
and the saved `mandy_part0_BEFORE.stl`, `mandy_part0_AFTER.stl`, and
`mandy_part0_REMOVED.stl` from `/mnt/sda2/STL/_validate/`. I did not regenerate
these files, run the full pipeline or test suite, start Blender, or inspect the
legacy repair script. I did not load `mandy_pipeline_inspect.stl`; claims about
the full repaired model's volume and topology were not independently remeasured.

All Python commands used `PYTHONDONTWRITEBYTECODE=1 tools/project_python.sh`
with scripts supplied on stdin. The interpreter reported
`/mnt/sda2/python/.venv/bin/python`. Experiments stayed in memory. This appended
section is the only file written by this investigation. The existing account's
SHA-256 before appending was
`22698e67a8de2e55140c941ff11e4bb55c6ce7956707226df5ba5829c8795d53`.

“Hair” below means the supplied REMOVED face set, identified as hair by the
owner, rather than an anatomical classification inferred from height. I did not
independently perform a visual anatomy assessment. “Body” means BEFORE minus
that face set, not the already repaired AFTER mesh.

For reproducibility: load with `mesh_io.load(mesh_io.probe(path, path))`;
map REMOVED vertices into BEFORE with `scipy.spatial.cKDTree`; compare sorted
triples of mapped vertex indices, ignoring winding, to select removed faces.
Every removed vertex matched at distance zero, and all 171,064 removed faces
matched. The complement has 391,185 faces. Edge components were computed with
`splitter._face_regions_after_cut`, avoiding the slow `scanner.shells` path.
Topology and winding-seam figures below use the repository scanner.

### Findings: there is a useful boundary, but it is not a winding seam

The previously measured seam results reproduce:

| Mesh | Faces | Open edges | Non-manifold edges | Winding-seam edges / closed components |
|---|---:|---:|---:|---:|
| Original source | 2,061,994 | 84 | 266 | 7 / 1 |
| BEFORE, part 0 | 562,249 | 31 | 67 | 52 / 1 |
| REMOVED | 171,064 | 312 | 67 | 48 / 0 |
| Body complement | 391,185 | 281 | 0 | 3 / 1 |
| AFTER | 391,442 | 0 | 0 | 0 / 0 |

The raw source has 40 edge-connected components. Cutting every winding-seam
edge still gives 40 components with the same sorted face counts; the largest
has 941,571 faces. On that largest component, seam counts are 5 / 1 and the
actual `by_seams` call returns the unchanged 941,571-face mesh. On BEFORE,
cutting all 52 seam edges gives 562,246 faces plus three single triangles;
the actual `by_seams` call returns 562,249 faces unchanged. Thus there is no
useful hair separation from the current winding-seam test. On a disconnected
raw input, “cuts nothing” means no additional separation: `by_seams` can also
return already separate shells when its closed-loop gate opens.

REMOVED has 85,617 vertices. Exactly 277 are shared with the unrepaired body
complement, and that count stays 277 at tolerances 1e-6, 1e-5, 1e-4 and 1e-3.
Against AFTER, however, only **129** match at distance exactly zero; 277 match
at each of those nonzero tolerances. Equal tolerance counts alone do not prove
exact equality to the repaired output. Exact attachment to the input body is
independently confirmed.

The exact removed/body interface consists of **281 edges on 277 vertices**.
Each is an ordinary edge incident to exactly two BEFORE faces, one removed and
one retained. Only **one** of these edges is a winding seam. Its edge graph has
8 components that are closed degree-two loops under the scanner's definition;
that is not a claim that the entire interface consists of eight simple loops.
Cutting these 281 edges reproduces 15 regions: body regions of 391,181, 3 and
1 faces, and the 12 REMOVED components. The largest removed component has
170,610 faces, confirming the earlier figure. REMOVED also reproduces the
reported 30 open boundary components from `scanner.open_loops`.

That exact interface is a **post-repair diagnostic**: finding it this way needs
the removed-face labels. It is not yet an independent anatomical hairline
detector. The fact that most vertices are not shared also does not establish
that triangles are spatially disjoint; I did not measure self-intersections or
near contacts.

There is nevertheless a separate, entirely mesh-derived candidate boundary:
**block face adjacency across every edge incident to more than two faces**.
On BEFORE those are the 67 non-manifold edges. This needs neither anatomy,
height, removed-face labels nor a closed winding loop. It yields 32 regions:

- 394,616 faces, including 3,431 faces from REMOVED;
- **167,595 faces, all from REMOVED**;
- 30 regions containing 38 faces in total, all from REMOVED.

The two large regions account for 562,211 of the 562,249 faces. This refutes
the broad statement that there is “no second region above any floor”: that is
true for winding-seam cuts, but false for non-manifold-edge cuts. It separates
most of the deleted hair, not the exact whole hair/body interface. Blocking
both non-manifold edges and winding seams instead produces large regions of
394,577 and 167,594 faces plus 58 small regions; it does not identify the
complete hair either. Neither candidate was measured on the raw source.

### Why orientation fails, and whether the body is orientable

I independently tested face-orientation consistency using a parity union-find
on edges with exactly two incident faces. The constraint between adjacent
faces is: flip exactly one face if both traverse the shared edge in the same
direction; otherwise flip both or neither. An inconsistent cycle proves that
no assignment of face flips satisfies those adjacencies. Edges with more than
two faces are excluded, so this does not mistake non-manifold incidence alone
for a proof of non-orientability.

BEFORE has 843,245 two-face edges and the deterministic traversal found two
contradictory constraints. REMOVED alone has 256,327 two-face edges and also
two contradictions. These counts depend on traversal and are not a count of
independent defects or a prescription to cut two particular edges. AFTER has
587,163 two-face edges and zero contradictions. Loading BEFORE directly into
`PyTMesh`, before filling or cleaning, reproduced
`forceNormalConsistence: Basic_TMesh was not orientable. Cut performed.`
It retained all 562,249 faces at that loading stage, increasing the vertex
count from 281,063 to 281,190. Thus the warning proves an orientation cut during
loading; it does **not** itself identify when or why all the hair is deleted.

**The unrepaired body without REMOVED is orientable.** It has no non-manifold
edges. Applying `meshing_re_orient_faces_coherently` to it succeeds and reduces
its winding seams from 3 to zero without needing the repaired AFTER surface.
It is still open, with 281 boundary edges. REMOVED contains all 67 non-manifold
edges and is independently orientation-inconsistent. This supports the claim
that the deleted region carries the obstruction; attachment to the body is
not necessary for that obstruction to exist. I did not identify the original
modeling operation that created it or localize a minimal corrective cut.

### Preservation experiments within the existing kinds of steps

Source inspection, not a new full-pipeline measurement: `repairer.repair`
currently runs WELD, duplicate CLEAN, shell SPLIT, per-part geometric orientation
and PyMeshFix, then MERGE. `splitter.by_shells` connects faces through
non-manifold edges too; `by_seams` is not invoked. The current CLEAN filters do
not resolve the parity obstruction. Merely flipping normals cannot resolve an
inconsistent orientation cycle.

I ran focused PyMeshFix experiments on the already saved part, using
`PyTMesh.load_array`, `fill_small_boundaries(0, True)` and `clean()` with default
cleaning iterations. No full pipeline or new STL output was involved.

First, separating the known REMOVED set from its body complement and repairing
each directly, without another geometric-orientation pass, gives:

| Input | Output faces | Original vertices retained within 1e-4 | Signed volume before / after |
|---|---:|---:|---:|
| Body complement | 391,822 | 195,708 / 195,723 | 11,869.927571 / 11,642.901374 |
| REMOVED | 165,322 | 82,663 / 85,617 | 1,860.133466 / 2,058.060662 |

Both outputs scan with zero open and non-manifold edges. This shows that
PyMeshFix can retain substantial hair geometry when it is isolated; it does
not prove exact shape preservation or provide a mesh-only segmentation rule.
Signed volumes of defective open inputs are diagnostics, not trustworthy solid
volumes or substitutes for the pipeline's component-volume guard.

Second, I took the two large regions from the 67-edge non-manifold cut,
ran the existing `meshing_re_orient_faces_by_geometry` step on each, then the
same PyMeshFix sequence:

| Input region | Output faces | Signed volume before / after |
|---|---:|---:|
| 394,616-face remainder | 202,086 | 11,676.318730 / 5,792.226449 |
| 167,595-face hair region | 167,572 | 2,053.659655 / 2,053.661830 |

Both outputs again have zero open and non-manifold edges. Together they retain
**85,431 / 85,617 hair vertices** within 1e-4, but the remainder suffers severe
loss. The extracted regions still have 10 and 1 non-manifold edges respectively:
blocking adjacency across such edges does not necessarily eliminate their
incidence within a region connected by other paths.

As a CLEAN-stage candidate, I also applied
`meshing_repair_non_manifold_edges` with `method=1` to BEFORE, then split
edge-connected regions, retained those with at least 100 faces, and ran the
existing geometric orientation and PyMeshFix sequence. It gave the same 32
region sizes and the same two repaired outputs above. Before extraction, the
filter retained all 562,249 faces but reported 108 open / 11 non-manifold edges
and allocated **8,796,665 vertex rows**, including unreferenced rows. It did
not make the part manifold. The two repaired outputs together retained only
184,798 of BEFORE's 281,063 vertices within 1e-4; their summed absolute signed
volume was 7,845.888280 versus BEFORE's signed 13,730.061036. This is not a
successful model-preserving remedy. I did not run the final merge/judge for
these experiments or establish the cause of this new remainder loss.

### Answer and next action

**With the present pipeline implementation, no successful hair-preserving
repair is demonstrated.** Winding-seam splitting does not help this part.
However, “nothing within these stages could preserve the hair” would be too
strong: the non-manifold-edge boundary is detectable from the mesh alone, and
repairing its large hair region retains nearly all of that region's volume.
Both tested ways of using that boundary damage the remainder, so neither is
ready to adopt. Keeping hair at the cost of the body is not a fix.

The strongest correction to the earlier account is therefore a new boundary
and a qualified path for further investigation, not a proposed production
change. A next bounded experiment should isolate why the 394,616-face remainder
collapses during orientation/repair, checking geometry after each operation,
and test local non-manifold connectivity repair within CLEAN before the
existing split. Any candidate must preserve both body and hair and pass the
merged-result judge; face counts, vertex retention and clean topology alone
are insufficient. Do not implement the blanket edge cut on this evidence.

I independently confirmed the key seam counts, exact input attachment,
170,610-face dominant removed component, and hair-associated orientation
obstruction. I additionally established that the body alone is orientable and
that a non-manifold-edge cut isolates a large hair region. I did **not** verify
the historical 46.27% / 85.82% full-pipeline figures, radial/Z measurements,
probe results, alternative settings listed earlier, printability, geometric
self-intersections, or preservation across the full model. Those remain claims
of the earlier account or unmeasured questions, rather than new findings here.

## Owner's note

### History

I want to be sure what we run seam detection before CLEAN step not after.
I recall the conversation what probably was not recorder properly what CLEAN should be a first step after splits completed.

## Investigation of the owner's note, 2026-09-21

Claude and Codex, collaborating. **No pipeline change was made, and none is
proposed here.** The note raises two separate questions; the second was
investigated in depth and is answered below.

### Seam detection is absent from the pipeline, but it is not inert

`splitter.by_seams` exists and is tested, but `repairer.repair` never calls it.
`repairer.py:5` and `splitter.py:4` both say so. So there is no before-or-after
CLEAN in the shipping pipeline to verify.

It was nevertheless run both ways, because the note asked specifically about its
position. **Measured on Mandy, seam detection's position relative to CLEAN makes
no difference**, and CLEAN does not destroy seam evidence:

| mesh | before CLEAN | after CLEAN |
|---|---|---|
| part 0 | 52 seam edges, 1 loop → 562,246 + 1 + 1 + 1 faces | identical |
| raw source | 7 seam edges, 1 loop → **40 parts** | **18** seam edges, 1 loop → 40 parts |

On the raw source CLEAN *increases* detected seam edges from 7 to 18 — it
exposes seams rather than erasing them — while the region split stays at 40
either way. Running seam detection before CLEAN gains nothing measurable here.

**A caveat worth restating, since it is easy to misread.** Calling `by_seams` on
the raw source returns 40 parts (941,571 / 374,415 / 267,531 / 141,423 /
137,294 faces and more), which looks like a successful seam split and is not
one: the raw source *already has* 40 edge-connected components, so the seam cut
produced no additional separation. This is exactly the trap the Codex section
above flags — on a disconnected input, "cuts nothing" means no *additional*
separation, not an unchanged return value. The summary line in the ruled-out
table, "`by_seams` still returns one piece of 941,571 faces", describes the
largest component rather than the call's return.

On part 0 it returns unchanged for a specific reason worth recording: not
because no seam is found, but because the cut it makes is too small to keep. The
seam test finds its closed loop and cuts into 562,246 + 1 + 1 + 1 faces; the
three single triangles fall under `MIN_SHELL_FACES=100`, so the `kept <= 1` gate
returns the mesh whole.

#### The floor is deciding whether to split, not what is worth saving

That gate is a defect, raised by the owner and confirmed here. `MIN_SHELL_FACES`
is documented as a worth-saving judgement about a component — it "retained
observed real Mandy parts (smallest 750 faces) while rejecting costume01 specks"
(archived implementation evidence). In `by_seams` it additionally decides
whether a split happens at all, and discards the regions that *did* clear the
floor along with the debris:

Measured against the code **as it was**; `by_seams` no longer takes `min_faces`
at all, so these calls are historical:

| call | result |
|---|---|
| `by_seams(part0, min_faces=0)` | **4 parts**: 562,246 + 1 + 1 + 1 |
| `by_seams(part0, min_faces=2)` | **1 part**: 562,249 — the cut is thrown away |
| `by_seams(part0, min_faces=100)` | **1 part**: 562,249 |
| `by_shells(part0, min_faces=0)` | 1 part: 562,249 |
| `by_shells(part0, min_faces=100)` | 1 part: 562,249 — unchanged by the floor |

`by_shells` returns identical geometry at either floor, so its threshold only
filters. `by_seams` returns structurally different geometry depending on the
floor, and a floor of just **2** is already enough to suppress the split.

The asymmetry is a missing branch. `by_shells` keeps the single real component
when the rest is debris (`len(kept) == 1` → `_extract`). `by_seams` has no such
branch: `len(kept) <= 1` returns the uncut mesh, so a legitimate 562,246-face
region is lost because its companions were three single triangles. The comment
there — "the seam did not actually separate anything" — misreports a cut that
did occur.

Consequence: **the `by_seams` cutting path is unreachable at production settings
whenever the seam isolates debris**, which is the common case. Every test that
exercises the cutting path passes `min_faces=0`; the only test at the production
floor was an Amidara-inspired case that, on inspection, has **no closed seam
loop at all** — it exits at the `loops == 0` guard and never reaches the floor,
so it was described as covering a non-separating closed seam while covering
something else. Nothing tested part 0's situation, where the seam does separate
a large region. "No region worth keeping" and "fewer than two regions worth
keeping" were being treated as the same condition.

This is a `by_seams` defect and does not by itself rescue Mandy's hair — the
562,246-face region still contains both hair and body (below). It is recorded
because the earlier conclusion "there is no second region above any floor" reads
as though the seam split had nothing to offer, when the actual situation is that
it produced a region and the floor discarded it.

**Fixed 2026-09-21.** `min_faces` is **removed** from `by_seams`. Every region
is returned and the caller decides what is worth saving, because it needs the
regions in order to decide. A `len(regions) <= 1` guard returns the original
object when the cut separates nothing, rather than an extracted copy stripped of
unreferenced vertices, and `name` is keyword-only so an old positional floor
raises immediately instead of binding to the naming callback. Mandy part 0 now
returns 4 parts — 562,246 + 1 + 1 + 1, all 562,249 faces preserved — and the
large region has 0 closed seam loops.

The parameter was first kept as an opt-in defaulting to 0; the owner asked why
it existed at all. It had no production caller, and its legacy path returned the
*whole mesh* when fewer than two regions qualified — so a caller asking for
regions of at least 97 faces got back a 192-face mesh containing the region it
had just excluded. That is the same defect in smaller form, so the parameter
went.

Two things this fix does **not** do. It does not change any repair outcome:
extracting the region gives the same 391,442 faces, 0.32% hair and 84.83% volume
as the uncut mesh. That dropping three triangles changes nothing shows the
removal is insufficient to affect the result; it does not identify what causes
the hair loss. And it does not establish that every `by_seams` output has
consistent winding: a mesh with no closed loop, or whose cut separates nothing,
still comes back whole with its seam edges intact.

**Seam splitting cannot isolate the hair.** The 562,246-face region holds
**30.46% hair vertices** — hair and body share one seam region. The winding seam
does not run along the hair/body boundary, so no placement of `by_seams` reaches
it. That remains consistent with the non-manifold-edge cut being the mechanism
that does separate the hair.

### The duplicate-shell defect has no known real-world instance

CLEAN runs before SPLIT so that coincident duplicate shells are deduplicated
while both copies are still in one mesh. That rationale rests entirely on the
synthetic `doubles` fixture, whose own construction note says "two shells is
legitimate on a real model" (`tools/make_probe_meshes.py`).

Measured: a scale-free duplicate-shell detector over **1,450 real models
(5.1 GB)** from `/mnt/sda2/STL/{Fixing,Done,Fixed}`. Per model, `by_shells` at
the production floor, then all-pairs comparison; a pair counts as duplicate when
face counts match exactly, bounding boxes agree within `diag*1e-4`, and
bidirectional nearest-neighbour vertex distance is within that tolerance.
Validated against the probes: it flags exactly `sphere_doubles` and
`sphere_allbad`, and does not flag `sphere_two_shells` or
`opposite_volume_shells`.

**Result over all 1,450 models, 0 read failures: zero pairs meeting this
detector's criteria.** Codex separately found no
archived note or production log recording the defect on a real model; every
"split-first gives 200% volume" claim — `pipeline.md`, the archived repair
study, `test_repairer.py` — traces back to the one synthetic fixture. The
"Merge doubles removed" messages in the logs are Blender's per-part vertex
welding, not duplicate shells.

This bounds the observed frequency; it is not proof of absence. The detector
requires equal face counts and uses a tolerance ten times tighter than CLEAN's,
so it does not exclude every geometry whole-mesh CLEAN could alter. Components
discarded below `MIN_SHELL_FACES` are outside the comparison entirely.

### SPLIT-first has no demonstrated benefit either

`costume01_decimated.stl` (900,000 faces, 113.70 mm diagonal) shows a real
ordering effect: whole-mesh CLEAN shrinks a 136-face component to 88 faces,
crossing `MIN_SHELL_FACES=100`, so the split discards it. SPLIT-first retains it.

That component does **not** survive the rest of the SPLIT-first path. Traced
through per-part CLEAN, geometric orientation and the PyMeshFix sequence:

| stage | faces | open / nm |
|---|---:|---:|
| extracted component | 136 | 0 / 5 |
| per-part close merge | 128 | 0 / 7 |
| per-part duplicate removal | 125 | 5 / 7 |
| PyMeshFix load, after orientation | 125 | 9 / 0 |
| PyMeshFix clean | **14** | 0 / 0 |

It ends as a 9-vertex remnant of about 1.01 x 0.54 x 0.20 mm, against an
original 4.77 x 1.83 x 0.92 mm; PyMeshFix reported 87 intersecting triangles.
Surviving the split is worth nothing when the part tool destroys it a step
later.

What the component *is* remains unresolved. Its thickness perpendicular to the
sheet is substantially less than its 0.92 mm axis-aligned Z extent, and its 65
vertices have nearest neighbours in the original main surface — 55 matching
exactly, median distance zero, maximum 0.1512 mm — consistent with a fragment
detached during decimation. But defective topology and a near-zero signed volume
cannot distinguish genuinely thin geometry from cancellation between misoriented
surfaces, so neither its intended significance nor its disposability is settled.

### Three claims that measurement refuted

Recorded because each is the kind of reading that looks convincing and is wrong:

- **"The 5 mm component is meaningful geometry."** Unproven. Size alone does not
  distinguish an intended part from a thin decimation sliver. A near-zero signed
  volume on a closed shell can equally mean a flat sheet or cancellation between
  misoriented surfaces.
- **"CLEAN damages the model: `open` 9 -> 961."** Wrong as stated. Isolating the
  filters shows duplicate-face removal alone drives 9 -> 888 **without moving any
  vertex**, measured on the isolated 892,445-face main component of the decimated
  mesh. (On the complete 900,000-face mesh the same filter gives 20 -> 1,226;
  the two are different experiments and should not be conflated.) Those are
  boundaries previously masked by coincident faces, not new holes. 952
  additional open edges are not 952 holes.
- **"The loss is silent."** Overstated. `repairer.py` records input and retained
  face totals, so the aggregate is recoverable. The accurate concern is narrower:
  nothing identifies *which* components were discarded or whether they mattered,
  and a face percentage cannot establish a volume percentage.

Also corrected: a 491 -> 489 shell-count drop does not by itself prove
cross-shell joining, since components can vanish during vertex collapse.

### Tested directly: SPLIT-first does not fix Mandy

The hypothesis was that CLEAN's `merge_close_vertices`, running at 0.1% of the
whole-model bbox diagonal, fuses the hair to the body before the split can
separate them — so splitting first would let the hair be repaired as its own
part instead of being deleted inside PyMeshFix.

Measured on `mandy_part0_BEFORE.stl` (562,249 faces; bbox diagonal 108.46 mm,
so the whole-mesh merge tolerance is 0.10846 mm), with
`mandy_part0_REMOVED.stl` as ground truth for the hair:

| order | stage | parts | faces | hair kept | volume |
|---|---|---:|---:|---:|---:|
| CLEAN-first | after whole-mesh CLEAN | 1 | 562,249 | 100.00% | 100.00% |
| CLEAN-first | after SPLIT | 1 | 562,249 | 100.00% | 100.00% |
| CLEAN-first | after per-part repair | 1 | 391,442 | **0.32%** | 84.83% |
| SPLIT-first | after SPLIT | 1 | 562,249 | 100.00% | 100.00% |
| SPLIT-first | after per-part CLEAN | 1 | 562,249 | 100.00% | 100.00% |
| SPLIT-first | after per-part repair | 1 | 391,442 | **0.32%** | 84.83% |

**The two orders give identical results.** Two reasons, both upstream of the
ordering question:

1. **The split never separates the hair.** Both orders reach repair with
   `parts=1`; hair and body are one edge-connected component, so `by_shells`
   returns them fused whenever it runs. There is no second part for the hair to
   be repaired as.
2. **CLEAN does nothing on this mesh.** 562,249 faces in, 562,249 out, 100%
   hair and volume retained, under both orders. The 0.10846 mm tolerance is not
   fusing hair to body — the 277 shared vertices are genuinely shared indices in
   the source, not near-coincident vertices that CLEAN welded.

The destruction happens inside PyMeshFix on a single fused part. Why it takes
the hair specifically is not established here: the earlier loading-stage
measurement retained all 562,249 faces, so the orientation-cut warning marks a
cut during loading without identifying when or why the hair is deleted. What is
established is that CLEAN ordering does not reach it. The mechanism that *does*
isolate the hair is
cutting the 67 non-manifold edges, which yields a 167,595-face hair region — a
change to splitting, not to CLEAN's position.

### Where this leaves the ordering decision

**Neither order is supported by real-model evidence.** CLEAN-before-SPLIT is
defended only by a synthetic fixture with no observed real instance;
SPLIT-first's one apparent win disappears inside PyMeshFix. The honest finding
is that this ordering was never grounded in real-model measurement in either
direction.

Real, undisputed consequences of the order, none yet tied to a benefit:

- **Merge tolerance changes with the bounding box.** `merge_close_vertices` is
  relative to the bbox diagonal, so whole-mesh CLEAN used 0.1137 mm on costume01
  while per-part CLEAN on that component used about 0.00519 mm — a 20x
  difference in what counts as "close", from the same parameter.
- **Floor eligibility changes.** Cleaning before the split moves components
  across `MIN_SHELL_FACES`.
- **There is no second floor check after the part tool** (`repairer.py`), so a
  part reduced to a remnant by repair is merged back regardless of its size.

### Evidence citations were broken — fixed in this update

Four modules — `repairer.py:44`, `processor.py:22`, `welder.py:20`,
`splitter.py:22` — cited `docs/refactor/implementation-evidence.md`, which does
not exist. It was left in `archive/docs-before-compact-2026-09-19/refactor/`
during the 2026-09-19 compaction. The rationale behind `CLEAN_FILTERS`,
`MIN_SHELL_FACES`, `LOST_VERTEX_TOLERANCE` and `MIN_VOLUME_KEPT` was therefore
unreachable from the code that depends on it. This is how an unsourced comment
came to be the only visible justification for the CLEAN/SPLIT order.

**Resolved here:** all four citations now point at the archived path, which the
refactor index permits for historical measurements. `pipeline.md` also stated
"Split-first left `doubles` as two shells at about 200% volume" without
recording that `doubles` is synthetic; that bullet now says so and links here.