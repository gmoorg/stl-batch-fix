### Deferred — PLY for the Blender boundary (justification lapsed)

**Not done, and the original reason for doing it is gone.** The case was that
`decimate.blender` hand-packed STL bytes in a per-polygon Python loop — ~100
lines of byte assembly no unit test could reach — and `wm.ply_export` would
replace it with one C call. **D19 deleted that file entirely** when it removed
the Blender decimation rung, so those lines are gone rather than improved.

What remains is `convert.blender`, which already uses `bpy.ops.export_mesh.stl`
with no hand-packing — a much weaker case. Reconsider if `repairer` makes heavy
use of the Blender boundary.

> **RE-OPENED 2026-09-17 — the user's concern, and the trigger above has
> fired.** `repairer.blender_part` now exists: step 4 can route each part
> through `blender_fx/repair.blender`, which means **write STL, launch Blender,
> read STL back, per part**. On Mandy that is 38 round trips in one repair.
>
> **The user's position: switch to PLY for both input and output at the Blender
> boundary.** Recorded as raised, not yet argued through.
>
> **Claims below are labelled by provenance**, because the first version of
> this entry did not distinguish them and read as though all were freshly
> measured. Only the MEASURED ones were established on 2026-09-17.
>
> **MEASURED (2026-09-17, this session):**
>
> - `repairer.blender_part` produces **38 round trips on Mandy** — `parts=38`
>   in the run output.
> - The Blender script's `normal_vote` reported **`agree: 801, disagree: 0`**
>   on `sphere_allbad`, because `mesh_io.write` derives normals from the
>   winding ([mesh_io.py:362]). Confirmed byte-identical output with the vote
>   enabled and disabled across six fixtures.
>
> **MEASURED EARLIER, recorded in the code:**
>
> - **STL has no vertex table**, so every write splits the mesh into loose
>   triangles and every read re-welds it — exactly **6.0x duplication** of every
>   vertex ([mesh_io.py:69]).
> - `mesh_io.load` folds `-0.0` to `0.0` and welds by exact coordinate match
>   ([mesh_io.py:273]).
>
> **RE-VERIFIED 2026-09-17 (second session), previously inherited:**
>
> - Blender is **4.0.2**, as recorded.
> - `wm.ply_import`, `wm.ply_export`, `import_mesh.ply` and `export_mesh.ply`
>   **all exist**. Confirmed by running them.
> - **PLY survives the round trip welded.** A binary PLY written from our
>   `Geometry` (382 verts, 760 faces) imports as 382 verts, 760 faces, **0 open
>   edges, 0 non-manifold**, and `remove_doubles` then removes **0 vertices** —
>   the mesh is already welded on arrival.
>
> **The two paths measured side by side**, same fixture, in Blender:
>
> | | STL (current) | PLY |
> |---|---|---|
> | verts after load | **2,280** (v/f 3.000) | **382** (v/f 0.503) |
> | open edges after load | **2,280** — every triangle an island | **0** |
> | `remove_doubles` removes | **1,898 verts** | **0** |
> | final verts | 382 | 382 |
>
> Both reach the same answer *on this fixture*. One does it by reading the
> file; the other by destroying the vertex table and reconstructing 1,898
> vertices with a 0.01 mm proximity guess.
>
> **Still inherited, not re-checked**: Bambu Studio's import dialog lists no
> PLY. It only matters for the deliverable, which PLY was never proposed for.
>
> **The failing case, found 2026-09-17 (second session) at the user's
> suggestion to scale a sphere down.** The weld guess does not merely *risk*
> being wrong — it destroys geometry, and at sizes that occur in real models.
>
> A whole sphere, shrunk. PLY is unaffected at every size; the STL path is not:
>
> | radius | shortest edge | STL path | PLY path | |
> |---|---|---|---|---|
> | 1.0 mm | 0.049 mm | 382v / 760f | 382v / 760f | ok |
> | 0.5 mm | 0.024 mm | 382v / 760f | 382v / 760f | ok |
> | **0.2 mm** | **0.0098 mm** | **361v / 718f** | 382v / 760f | **DAMAGED** |
> | **0.1 mm** | 0.0049 mm | **333v / 662f** | 382v / 760f | **DAMAGED** |
> | **0.05 mm** | 0.0024 mm | **142v / 280f** | 382v / 760f | **63% of the model gone** |
>
> **A whole 0.2 mm sphere is not a real model — but a 0.2 mm feature is.** Same
> test on a normal 20 mm sphere carrying one small bead:
>
> | bead radius | STL path | PLY path | bead vertices kept |
> |---|---|---|---|
> | 0.30 mm | 764v / 1520f | 764v / 1520f | 389 / 389 |
> | **0.20 mm** | **742v / 1476f** | 764v / 1520f | **367 / 389** |
> | **0.15 mm** | **742v / 1476f** | 764v / 1520f | **367 / 389** |
> | **0.10 mm** | **715v / 1422f** | 764v / 1520f | **340 / 389** |
>
> The large sphere is untouched; the damage is confined to the small feature,
> which is exactly the geometry a repair pipeline must not eat. A 0.2 mm bead
> on a 20 mm figure is an ordinary level of detail — jewellery, buttons, eyes,
> lace — and the bounding-box diagonal (69 mm) gives no hint that anything is
> at risk.
>
> **This is a third absolute-tolerance failure**, alongside the two in the OPEN
> BUG entry, and it is the worst of them: `welder`'s tolerance being wrong
> means a defect goes *unrepaired*, while this one **deletes sound geometry**.
>
> So the case for PLY is no longer "it removes a reconstruction that cannot
> fail". It is: **the reconstruction has been measured failing on
> ordinary-sized detail, and PLY removes it.**
>
> **ARITHMETIC, not measurement:**
>
> - The normal occupies 12 of STL's 50 bytes per triangle, so **24% of the
>   file**. That is a file-size figure only — **no I/O time was measured**, and
>   the round-trip cost of the STL boundary is still unknown.
>
> **OVERSTATED IN THE FIRST DRAFT, corrected here:**
>
> - That entry called re-welding "a correctness-relevant transformation applied
>   38 times per model", implying observed harm. **There is none on record.**
>   The `-0.0` fold is real and its rationale is sound, but no measurement shows
>   it ever damaged a mesh across a Blender round trip — and
>   `test_a_written_mesh_reloads_identically` asserts the opposite for the
>   simple case. It is a theoretical risk, not a demonstrated one.
>
> **Scope is unchanged from the original entry**: the Blender scratch boundary
> only. Not an input format — there has never been a PLY in the collection — and
> never the deliverable, per the Bambu claim above (itself inherited).
>
> **The strongest argument, found after the above by reading the script
> (2026-09-17) — the user's point:** the format change cannot break much,
> because the script barely uses what STL carries.
>
> `blender_fx/repair.blender` does **not** use Blender's STL importer on the
> binary path. `load_binary_stl` is a hand-written parser that calls
> `bm.verts.new(...)` **three times per triangle** — a fresh vertex every time,
> never reused. A 103,729-face part therefore creates **311,187 loose
> vertices** in a Python loop, one `struct.unpack_from` per triangle.
>
> Then line 401 welds them back with
> `bmesh.ops.remove_doubles(dist=merge_dist)`, and **`merge_dist` is `0.01 mm`
> — an absolute distance**, the very value this project measured as the *worst*
> threshold when used in PyMeshLab (nm 2,263 -> 2,359).
>
> So the round trip is:
>
> | side | what happens to the vertex table |
> |---|---|
> | ours | `mesh_io.write` discards it, splitting into loose triangles |
> | Blender | a Python loop rebuilds 3 verts per triangle |
> | Blender | `remove_doubles` **reconstructs it by proximity guess** |
>
> **A vertex table is destroyed and then guessed back at an absolute
> tolerance.** With PLY the table is read directly, `remove_doubles` has
> nothing to do, and that guess leaves the pipeline. That is a correctness
> argument, not a performance one, and it is much stronger than the file-size
> figure above.
>
> **And the risk of changing format is low**, which is the user's observation:
> with `normal_vote` disabled the `orig_normal` layer is written by the loader
> and **never read** — confirmed by byte-identical output across six fixtures
> with the vote enabled and disabled. There is no normal logic left for a
> format change to break. What changes is the loader, and `wm.ply_import`
> replaces ~30 lines of hand-parsing.
>
> **The ordering recorded here was wrong, and the user caught it.** It said to
> settle "does the Blender rung survive?" first, on the reasoning that if
> Blender goes, the boundary goes and the format question is moot. Two things
> are wrong with that:
>
> - **The format question does not depend on it.** PLY is a strictly better way
>   to hand a mesh to Blender whenever Blender runs — including in the frozen
>   `stl_batch_fix.py`, which has been running batches with this exact loss for
>   as long as it has existed. Deferring a *measured data-loss bug* behind an
>   architectural question is backwards.
> - **"Alternative" was the wrong frame to begin with.** `blender_part` was
>   built as an either/or swap against PyMeshFix via `tool=`, and that framing
>   was challenged at the time and conceded — then kept leaking into the
>   reasoning anyway. The measurements say the two tools do **different jobs**:
>   Blender is better at holes and fins (`fin`: 760f/+4094.9, 1 vertex lost
>   against 756f/+4093.1 and 3 lost) and **never re-winds anything**, while
>   PyMeshFix re-winds correctly. A part with a hole wants one; a part with bad
>   winding wants the other. The boundary is therefore not
>   optional-if-we-choose-it.
>
> **The actual order:**
>
> 1. **Switch the Blender boundary to PLY.** It is a self-contained change to
>    `blender_fx/repair.blender` — `wm.ply_import` replaces ~30 lines of
>    hand-parsing, `wm.ply_export` replaces `write_stl_from_bm`, and
>    `remove_doubles(dist=merge_dist)` becomes unnecessary, deleting the third
>    absolute tolerance rather than recalibrating it. Justified on its own by
>    the measured loss above.
> 2. **Then** decide how step 4 chooses between the tools — by defect rather
>    than by size, which is what the measurements support and what
>    `_repair_part`'s docstring still frames as an unanswered threshold
>    question.
> 3. Measure the round-trip cost whenever convenient. It bears on performance
>    only; the correctness argument is already settled.

**Both preconditions were verified** (2026-09-15, Blender 4.0.2) and hold, so
this is a scope decision rather than a research one:

| | verts | faces | v/f |
|---|---|---|---|
| written by us | 2562 | 5120 | 0.500 |
| round trip, no modifier | 2562 | 5120 | 0.500 |
| after decimate to 30% | 770 | 1536 | **0.501** |

**The vertex table survives, decimation included** — v/f ≈ 0.5 is welded, 3.0
would be fully split. `wm.ply_export` writes binary little-endian, and the
header it emits is bare (`x, y, z` plus a face list), which is *why* nothing
splits: no per-face attributes to disagree about. It writes
`property list uchar uint vertex_indices` where our test wrote `int` — a reader
must handle what it finds.

**Scope, if it is ever done**: the Blender scratch boundary only. Supporting
PLY as an *input* format was proposed and dropped — there has never been a PLY
in the collection. No `indicators`, `converter` or `kind()` changes, and no
parsing of arbitrary PLY dialects.

**Not to be confused with the output format.** Bambu Studio's import dialog
lists no PLY, so it can never be the deliverable. 3MF is the candidate there if
that is ever revisited — vertex table, zip-compressed, carries units explicitly
(which the Leia model, authored in non-mm units, would have benefited from) —
weighed against STL being what every slicer and sharing site accepts.

**One correction worth keeping**: writing float32 does **not** drift. The bytes
held are the bytes written are the bytes read back, bit-identical — asserted by
`test_a_written_mesh_reloads_identically`. STL's weakness is the missing vertex
table, not precision. Drift enters only when another tool nudges a coordinate
between writes.
