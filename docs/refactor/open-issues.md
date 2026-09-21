# Open issues

This is the compact work list. Detailed reproductions and owner comment slots remain in [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md), whose audit-status table records which findings are confirmed against the current tree and which were withdrawn or misdescribed.

Fix order agreed 2026-09-20, cheapest and most certain first: R03 zero-face input, R02 PyMeshFix failure propagation, R01/R09 error propagation, A04 atomic commit, A03 non-finite rejection, A02 component preservation. Each step adds its missing regression before the fix.

No final face-budget gate is required; `max_faces` is a decimation target. See the T16 owner decision.

## Prevent false success and model loss

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
