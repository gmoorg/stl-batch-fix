# Open issues

This is the compact work list. Detailed reproductions and owner comment slots remain in [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md), whose audit-status table records which findings are confirmed against the current tree and which were withdrawn or misdescribed.

Fix order agreed 2026-09-20, cheapest and most certain first: R03 zero-face input, R02 PyMeshFix failure propagation, R01/R09 error propagation, A04 atomic commit, A03 non-finite rejection, A02 component preservation. Each step adds its missing regression before the fix.

No final face-budget gate is required; `max_faces` is a decimation target. See the T16 owner decision.

## Prevent false success and model loss

- **Do not repair a part that is already clean and within the face budget.** Owner decision, 2026-09-21. A part that is `scanner.Scan.is_clean` (`open_edges == 0` and `non_manifold == 0`) and already under the decimation limit has nothing for the pipeline to do: decimation reports `not_needed`, and a hole-filling, self-intersection-removing repair has no defect to act on. Such a part should pass through untouched rather than being handed to the repair tool. The limit is the decimation budget, not a separate size guard — a part above it is decimated down first, so the test is applied to what the repair would actually receive.
  Running repair anyway is measurably destructive. Both Amidara models are clean and under `MAX_FACES=900000`, so **neither should ever have reached PyMeshFix**:

  | model | faces | verts | `is_clean` | under budget | current result |
  |---|---:|---:|---|---|---|
  | `..._base.stl` | 315,482 | 155,710 | yes | yes | 279,168f, 96.93%, **broken** |
  | `..._hands_2.stl` | 150,656 | 75,033 | yes | yes | 145,290f, 100.00%, **artifact** |

  This is probably why the damage went unnoticed: models like these were not being sent to repair, so the defect stayed latent rather than being new.

  **Sequencing: this gate must not be added until the PyMeshFix bug is resolved.** Gating on today's behaviour would skip repair for models that genuinely need fixing, trading one silent failure for another. Fix why a clean mesh is destroyed first; add the gate afterwards, as defence in depth — a clean mesh should survive repair *and* not be put through it.

- **`scanner.winding_seams` misses inconsistent winding on edges shared by more than two faces.** It only examines edges with exactly two incident faces, so an inversion inside a non-manifold region is invisible. Measured: a `fill_small_boundaries` output scored `seam=0` while 762 of its new faces were wound the opposite way to the old geometry they touch. Any repair that creates overlapping faces can hide its own damage there.
- **`Scan.is_clean` does not consider winding at all.** It is `open_edges == 0 and non_manifold == 0`, so a mesh with inverted normals reads as flawless — `Amidara_..._base.stl` has 1,506 of them and 922 winding-seam edges, and passes. Whatever the gate above ends up testing, `is_clean` alone is not sufficient to call a part undamaged. Both defects: [discovered bugs](discovered-bugs.md#the-measurement-gap-behind-both).
- **Nothing cleans up after the merge — but measurement says little is needed today.** Owner observation, 2026-09-21. `meshing_remove_duplicate_faces` (step 10) runs *before* the split, justified by its coupling to step 9: `merge_close_vertices` produces duplicate faces and step 10 removes them. That argument says nothing about whether the same cleanup is wanted after the merge, and `splitter.merge` explicitly does **not** weld coincident vertices at former cuts ("the caller must rescan the result").
  Measured: Mandy merges 40 parts into 2,060,200 faces with **zero duplicate faces**, and re-running `remove_duplicate_faces` afterwards changes nothing. A post-merge duplicate pass would be a no-op on the model most likely to need it. The reason is that `by_shells` splits along *existing* component boundaries, so parts share no cut surface — the unwelded-cut concern belongs to `by_seams`, which cuts *through* a surface and is not in the pipeline.
  What does survive: **36 coincident vertex pairs** in Mandy's merged output — separate vertices at identical coordinates where parts abut. Harmless there (`open=0, nm=0`), but it confirms the merge leaves unwelded geometry, and it is the same mechanism behind the `open=6` / `open=141` boundaries measured in the seam-splitting experiments. A post-merge weld is worth having **before** seam splitting is ever enabled, not now; and it must be narrower than CLEAN, since `merge_close_vertices` at bounding-box scale would weld across cut boundaries rather than along them.
- **Try `fix_connectivity` + fill, then the current step 15 on its output.** Owner's proposal, 2026-09-21. The hypothesis is that the two operations are complementary: the first repairs by *adding* geometry and leaves inverted patches behind, and `clean()` removes bad faces while keeping good ones — so running `clean()` afterwards might delete the inverted patches without the wholesale loss it causes on the raw source, since the winding is already consistent when it arrives.

  What each does alone on `Amidara_..._base.stl` (315,482f, watertight, 922 winding-seam edges, 11,365 self-intersections):

  | sequence | faces | open | nm | seam | volume |
  |---|---:|---:|---:|---:|---:|
  | step 15 `clean()` alone | 279,140 (**−36,342**) | 0 | 0 | 0 | 97.26% |
  | `fix_connectivity()` + `fill_small_boundaries(0, True)` | 320,152 (**+4,670**) | 0 | 0 | 0 | **99.81%** |
  | **both, in that order** | untested | | | | |

  `PyTMesh.fix_connectivity()` has never been called by this project. Alone it takes winding seams 922 → 0 and opens 2,048 edges; filling closes them again. It is the only operation found that moves in the same direction as the online repair tool, which fixed 1,506 inverted normals by adding 25,478 triangles.

  **It is not a working repair by itself.** The owner's inspection found the patched faces inverted, and a direct check confirms **762 of its 4,592 new faces are wound backwards** relative to the geometry they touch — while `scanner.winding_seams` reports 0, because those faces sit on edges shared by more than two faces, which the seam test skips. Self-intersections also rise 11,365 → 15,153. An earlier version of this record called it a full repair; that was wrong, and the volume and face counts are what made it look like one.

  Measure the combination against the owner's eye, not against the counters: every metric available today calls the intermediate result clean. Files in `/mnt/sda2/STL/_validate/`: `base_FIXCONN_FILL.stl`. Details in [Amidara](amidara-clean-destroys.md).
- Reject zero-face, non-finite, zero-area, or invalid geometry.
- Compare decimation with the original; current destruction checks begin too late.
- Preserve meaningful components. The `<100 faces` rule can still approve missing parts; signed-volume cancellation no longer can (A02, `scanner.component_volume`), but the component is still dropped — only the false success was fixed.
- Repair Mandy's hair rather than only detecting its loss. It is non-orientable and attached to the body at 277 shared vertices of 85,617; PyMeshFix cuts it and every topological check calls the result clean. Branching to Blender instead of PyMeshFix is ruled out by the owner, so a fix must work within the existing steps. Measurements in [Mandy volume loss](mandy-volume-loss.md).
- Stop rejecting correctly repaired duplicates. Three of sixteen probes are watertight results the volume guard marks DESTROYED; they lose volume at CLEAN, while real destruction happens inside the part tool. Same file.
- Distinguish a cavity from a second solid. `component_volume` discards the sign per component, so a repair that flips an inner shell outward scores 100%. Containment-aware volume would answer it.
- Propagate PyMeshFix failure; it can currently become `repairer.ok=True`.
- Commit output and markers atomically.
- Define per-part routing between PyMeshFix and Blender, including mixed defects and intermediate acceptance.

## Algorithm questions

- Handle reversed/non-monotone T-junction paths.
- Distinguish a far bent junction from a hole; no defensible distance bound exists.
- CLEAN/SPLIT order: measured 2026-09-21, neither order is supported by real-model evidence. CLEAN-before-SPLIT is defended only by the synthetic `doubles` fixture, and SPLIT-first showed no benefit — identical results on Mandy, and the costume01 component it preserves is destroyed by PyMeshFix anyway. Note the same parameter means a 20x different distance per-part vs whole-mesh. [Mandy volume loss](mandy-volume-loss.md#investigation-of-the-owners-note-2026-09-21).
- Replace face-count debris deletion with a preservation rule.
- ~~`by_seams` uses `MIN_SHELL_FACES` as a split gate~~ **Fixed 2026-09-21.** `min_faces` is **removed** from `by_seams`: it returns every region the seam produces, and worth-saving judgements belong to the caller, which needs the regions to make them. A `len(regions) <= 1` guard returns the original object when the cut separates nothing. `name` is keyword-only so an old positional floor raises immediately. Mandy part 0 now returns 4 parts (562,246 + 1 + 1 + 1, all 562,249 faces preserved) and the large region has 0 closed seam loops. This does **not** fix Mandy's hair: the repair outcome is unchanged. Measurements in [Mandy volume loss](mandy-volume-loss.md#the-floor-is-deciding-whether-to-split-not-what-is-worth-saving).
- A seam region is a valid split, not a repairable solid. A one-face region has three open edges, and `merge` does not weld cut boundaries. Seam routing, when built, must judge repaired regions rather than assume success.
- **Removing the `by_seams` floor exposes Amidara base to destructive repair.** Before that change the floor returned the mesh uncut there (only one region clears 100 faces); now the split proceeds, and repairing the regions independently takes a shell from 96.93% to 84.05% with a fabricated artifact on `hands_2`. `repairer` does not call `by_seams`, so nothing ships this today — but enabling seam splitting must not use independent per-region repair. Rejoining and welding *before* repair restores `open=0` and the original volume; whether the subsequent repair is sound is untested (117.39% in one variant). Measurements in [Mandy volume loss](mandy-volume-loss.md#unconditional-seam-splitting-with-independent-repair-breaks-real-models).
- No fixture covers a *closed* seam loop that separates nothing — the case `by_seams`' `len(regions) <= 1` guard exists for. On the tube fixture any closed loop separates by construction. The guard is reasoned, not measured.
- Assess per-part preservation **after** repair, not only at the split. There is no second floor check after the part tool, so a part reduced to a remnant is merged back regardless of size; but another face-count rule is not the answer, since a valid repaired part can legitimately have under 100 faces.
- Decide when seam recovery and `open_loops_are_printable` are safe.
- Replace remaining absolute geometry tolerances where scale tests require it.

## Runner and concurrency

- Build serial runner, per-file isolation, scheduling, reporting, and shared adapters.
- Surface converter, copy, selector, child, and reporting failures exactly once.
- Resolve OBJ/STL same-stem collisions.
- Serialize PyMeshFix output capture or isolate it.
- Track concurrent Blender processes and kill descendants on timeout/interruption.

## Missing regression coverage

- Zero-face/non-finite input and failed PyMeshFix.
- Decimation-lost appendage and small valid shell.
- Opposite-volume shells and zero-area closed topology.
- Reversed T-junction and far bent path.
- Selector/converter/copy exceptions and source collisions.
- Interrupted write.
- Full runner markers, retry, crash, timeout, Ctrl+C, layout, summary, and exit code.

Do not turn an unresolved geometry judgment into a passing expectation merely to make the suite green.
