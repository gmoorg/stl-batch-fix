### Review of `stl_batch_fix.blender` (2026-09-14)

Findings, not decisions — the script is frozen until the refactor, so none of
this was changed. Ordered by confidence. The file is 593 lines and runs in
six steps: load → merge doubles → T-junction split → normal vote → repair loop
→ final cleanup and validate.

**1. `write_stl_from_bm` assumes every face is a triangle.** It packs exactly
three vertices into each 50-byte record. Both paths that reach it triangulate
first (the early exit, and step 6a), so this is not live — but a quad would
have its fourth vertex silently dropped, producing a corrupt-but-parseable
STL. A `len(f.verts) != 3` assertion would make it impossible rather than
merely unlikely.

**2. The output buffer is sized before the loop that fills it.** `buf` is
allocated from `len(bm.faces)` captured up front. Nothing enforces that the
iteration yields the same count; a mismatch either raises inside `pack_into`
or leaves trailing zeros that parse as degenerate triangles at the origin.
Writing the count after the loop removes the coupling.

**3. `load_binary_stl` trusts the header's triangle count** — `f.read(n_tris *
50)` before any validation, so a corrupt header claiming 4 G triangles asks for
a 200 GB read. The per-triangle loop guards against a *short* read, but the
allocation happens first. `check_stl_integrity()` validates this on the Python
side, so it is defence-in-depth, not a live bug.

**4. Two thresholds answer the same question.** `_open_tol = 0.5` mm here
decides `BLENDER_OK` vs `BLENDER_OPEN`; `MIN_LAYER = 0.6` mm drives the Python
print-scale gate. A 0.55 mm gap is "fine" to one and "significant" to the
other. The Falcon's 0.7209 mm cleared both, so it has never bitten. These
should be one value, passed in the way `merge_dist` already is.

**5. `normal_layer` is re-fetched five times, defensively.** After
`remove_doubles`, after `split_t_junctions`, and in both import branches. The
repetition suggests uncertainty about when BMesh invalidates custom layers; it
is harmless, but it hides whether any one of those re-fetches is load-bearing.

**6. The boundary-loop walk picks `nexts[0]` with no geometric criterion.**
Where three or more boundary edges meet a vertex, it takes the first unvisited
one. A wrong branch yields a loop that is not the hole outline — the fill is
then rolled back by the NM check, so it fails safe, but it can silently fail to
close a hole that was fillable. The `seen_junctions` handling in step 4d
suggests the case is known.

**7. `except Exception: pass` around the face fill** makes a genuine error
indistinguishable from "this loop was not fillable" — the same shape as every
logging gap found on 2026-09-13/14.

**8. `MAX_ITER = 200` in the T-junction splitter is an undiagnosed cap.** One
split per iteration with a full re-scan; a mesh with 500 T-junctions silently
keeps 300. `total_splits == 200` is the signature and is never checked.

**Deliberately not flagged.** The 12-pass repair loop, the stall counter and
the wider-NM escalation all look sound, and this run supports them: 14 of 16
Blender invocations returned `BLENDER_OK`. Using the STL's own stored normals
as ground truth for winding (`normal_vote`, flood-filling per connected
component and flipping a component when more faces disagree than agree) is the
cleverest thing in the file and should survive the refactor intact.

**Priority if any of this is acted on:** #1 and #4. The first is a real
corruption path however narrow; the second is two files disagreeing about what
"too small to matter" means.

---

