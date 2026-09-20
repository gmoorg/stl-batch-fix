## Where the refactor stands (2026-09-15)

Seven modules built, 229 tests, all green. `stl_batch_fix.py` is untouched and
still frozen; `main` is at `4c1c0c2` and `refactor` is five commits ahead.

| module | what it owns | tests |
|---|---|---|
| `pool` | worker loop, nothing domain-specific | 24 |
| `indicators` | what the filesystem says about a file | 20 |
| `blender` | launching Blender, killing it, reading its markers | 32 |
| `mesh_io` | probe, load, write — the only writer | 44 |
| `scanner` | defect counts off one edge map | 31 |
| `decimator` | the three-rung ladder | 22 |
| `converter` | the preparation walk | 20 |

Still to build: **`repairer`** (PyMeshFix, seam split, NM repair, open-edge
fill) and the **orchestration** that threads a file through all of it.

> **Superseded 2026-09-17.** `repairer` is built — see D24. The shape it took
> is not the one sketched here: the seam split is not part of it (`by_shells`
> is what step 4 needs, and `by_seams` stayed unused), and open-edge fill is
> PyMeshFix's `fill_small_boundaries` rather than a step of its own. Only the
> orchestration remains.

