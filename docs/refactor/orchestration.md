# Final batch orchestration

## Module order

```text
CLI or TUI → RunConfig
  → converter.prepare
       indicators.check
       blender.convert only for input normalization
  → sort/admit Jobs with pool.Pool
  → isolated child per file
       mesh_io.load
       processor.process
         decimator → repairer → scanner decision
       atomic processor.write
  → FileResult over JSON
  → one reporting/event component
  → CLI/TUI display + durable summary
```

## What happens to one model, step by step

Every call the pipeline makes to a single mesh, in order, with what each step
is for and when it runs. Steps marked **conditional** are skipped when their
condition is not met. Anything marked *(not implemented)* is designed but
absent from `repairer.repair` today.

### Intake

| # | step | goal | condition |
|---|---|---|---|
| 1 | `converter.classify` | decide whether the file is a usable mesh, a companion to copy, or needs conversion | always |
| 2 | `blender.convert` | normalize OBJ or ASCII STL to binary STL, because nothing downstream reads other formats | **conditional** — only when step 1 says the format is not binary STL |
| 3 | `mesh_io.probe` | read the header for a triangle count, so jobs can be admitted smallest-first without loading | always |
| 4 | `mesh_io.load` | read vertices and faces into arrays, welding exactly identical coordinates into one shared vertex table | always |

### Reduce

| # | step | goal | condition |
|---|---|---|---|
| 5 | `decimator.decimate` | bring the face count under the printing budget; an over-budget file gets reduced by the slicer instead, which reintroduces the defects this tool removes | **conditional** — `Rung.NOT_NEEDED` when already under `max_faces`; `max_faces <= 0` disables it |
| 6 | *decimation failure gate* | stop with `UNDECIMATED` rather than shipping an over-budget mesh | **conditional** — only when step 5 reports `FAILED` |

### Repair — `repairer.repair`

| # | step | goal | condition |
|---|---|---|---|
| 7 | `welder.repair` | split faces at T-junctions, where a vertex sits on another face's edge without being part of it. Adds faces, moves and deletes nothing | always |
| 8 | `meshing_remove_null_faces` | drop zero-area faces, which own edges without enclosing anything and corrupt every later edge count | always |
| 9 | `meshing_merge_close_vertices` | weld vertices within 0.1% of the bounding-box diagonal, so duplicate surfaces become one | always — **and measured harmful**: on `Amidara_..._base` it merges one vertex and creates two non-manifold edges. See [discovered bugs](discovered-bugs.md) |
| 10 | `meshing_remove_duplicate_faces` | remove faces repeated after the merge, which would otherwise read as non-manifold | always |
| 11 | `meshing_remove_unreferenced_vertices` | drop vertices no face uses any more | always |
| 12 | `splitter.by_shells` | separate edge-connected components, because PyMeshFix rebuilds one surface and discards the rest — an unsplit multi-shell mesh comes back as its largest shell alone | **conditional** — returns the mesh unchanged when there is one component; components under `MIN_SHELL_FACES=100` are **dropped** |
| 13 | `splitter.by_seams` | separate regions whose winding contradicts itself | *(not implemented)* — exists and is tested, never called by `repairer` |
| — | **per part, steps 14–15 run once each** | | |
| 14 | `meshing_re_orient_faces_by_geometry` | make every face point outward, since a signed-volume check misses locally inverted patches | always, per part — takes `base` from 922 winding-seam edges to 7,659 before step 15 sees it, but **not harmful to the result**: disabling it changes the output by 28 faces and +0.33pp |
| 15 | `meshfix.repair` → `fill_small_boundaries(0, True)` then `clean()` | close holes, then delete self-intersecting and degenerate geometry | always, per part — **and the destructive step**: removes 36,342 faces from a watertight `base`. See [Amidara](amidara-clean-destroys.md) |
| 16 | *part failure gate* | stop the whole file rather than merging back a part that could not be repaired | **conditional** — only when step 15 returns `PartFailed` |
| 17 | `splitter.merge` | concatenate the repaired parts into one mesh. Does **not** weld coincident vertices at former cuts | **conditional** — returns the single part unchanged when there is one |
| 18 | *finite check* | reject NaN or infinite coordinates before any measurement touches them | always |

### Judge — `processor._decide`, in this order

| # | step | goal | condition |
|---|---|---|---|
| 19 | measurability gate → `FAILED` | refuse an unmeasurable volume instead of comparing it, since every `<` test below is False against NaN and would wave it through | **conditional** — when `volume_in`, `volume_out` or their ratio is not finite |
| 20 | destruction gate → `DESTROYED` | catch a repair that kept less than `MIN_VOLUME_KEPT=0.90` of the volume. **Asked before the topology tests**, because a half-model is a valid closed surface and would otherwise pass them | **conditional** — when `volume_kept < 0.90` |
| 21 | `scanner.scan` → `UNREPAIRED` | reject remaining non-manifold edges | **conditional** — when `scan.non_manifold > 0` |
| 22 | → `OPEN_EDGES` | reject remaining holes | **conditional** — when `scan.open_edges > 0` |
| 23 | `scanner.component_volume` → `BROKEN` | reject a surface that encloses nothing. Four faces on a line own every edge twice and score `nm=0, open=0, is_clean=True` while being no solid at all | **conditional** — when the enclosed volume is not finite and positive |
| 24 | → `PROCESS` | accept | when every gate above passed |

### Commit

| # | step | goal | condition |
|---|---|---|---|
| 25 | `processor.write` | write the repaired STL, or the marker naming why not, atomically so an interrupted write cannot look finished | always |

## Turning steps off

Every repair step has a module-level switch, so what a step contributes can be
measured instead of argued about. All default to the shipping behaviour; a
disabled step still appears in `Result.steps` with `detail` saying it was
skipped, so a log never silently omits a stage.

| step | switch | default |
|---|---|---|
| 7 weld | `repairer.ENABLE_WELD` | True |
| 8 null faces | `repairer.ENABLE_CLEAN_NULL_FACES` | True |
| 9 merge close | `repairer.ENABLE_CLEAN_MERGE_CLOSE` | True |
| 10 duplicate faces | `repairer.ENABLE_CLEAN_DUPLICATE_FACES` | True |
| 11 unreferenced verts | `repairer.ENABLE_CLEAN_UNREFERENCED` | True |
| 12 shell split | `repairer.ENABLE_SPLIT_SHELLS` | True |
| 13 seam split | `repairer.ENABLE_SPLIT_SEAMS` | **False** |
| 14 orient | `repairer.ENABLE_ORIENT` | True |
| 15 part tool | `repairer.ENABLE_PART_TOOL` | True |
| 15a fill boundaries | `meshfix.ENABLE_FILL_BOUNDARIES` | True |
| 15b `clean()` | `meshfix.ENABLE_CLEAN` | True |
| 15b arguments | `meshfix.CLEAN_MAX_ITERS`, `CLEAN_INNER_LOOPS` | 10, 3 |

`ENABLE_SPLIT_SEAMS` is the one switch that is off by default and turns a step
*on*: `repairer` has never called `by_seams`. Enabling it repairs each seam
region independently, which is measured as destructive on real models.

```python
from libs import meshfix, repairer
repairer.ENABLE_CLEAN_MERGE_CLOSE = False   # step 9
meshfix.ENABLE_CLEAN = False                # step 15b
```

Measured on `Amidara_..._base.stl` (315,482 faces in):

| configuration | faces out | volume kept |
|---|---:|---:|
| default | 279,168 | 96.93% |
| steps 9 + 10 off | 279,168 | **96.93% — identical** |
| step 14 off | 279,140 | 97.26% |
| step 15b off | 318,968 | **99.81%** |
| steps 9, 14, 15b off | 320,152 | **99.81%** |

Two things that reads off directly: the CLEAN filters do no useful work on this
model, and `clean()` alone accounts for essentially the whole loss. Note that
99.81% is not a repair — the owner's inspection found the patched faces
inverted in those outputs. See [discovered bugs](discovered-bugs.md).

### What the judge cannot see

Steps 19–24 are the entire verdict, and they do not test winding. A mesh with
hundreds of inverted faces reaches step 24 and is written as a success — which
is how both known model-destroying bugs pass every check. See
[discovered bugs](discovered-bugs.md).

## Required behavior

1. Validate configuration and dependencies once.
  `fast_simplification` is required for startup; PyMeshLab remains required
  for the separate shell split/merge operations.
2. Classify every source once; copy companions and normalize OBJ/ASCII STL to probed binary STL.
3. Preserve identity and relative output paths; reject destination collisions.
4. Admit jobs by worker and memory limits, preferably smallest measurable first.
5. Run each file in a child so timeout, native crash, OOM, or Blender descendants cannot kill the pool.
6. Return exactly one structured result per source. The parent creates timeout/crash markers when needed.
7. Commit outputs and markers atomically.
8. Emit one event stream for CLI/TUI and return nonzero when actionable failures remain.

## Why other structures failed

- Separate TUI/script pools duplicate scheduling, status, and cancellation.
- A process pool does not preserve pending work when a worker dies; stable threads should own disposable children.
- Mutable globals let CLI, tests, and children disagree; pass immutable values.
- Logs written by several layers lose exactly-once accounting; one runner owns final results.
- A killed child cannot write its timeout result; the surviving parent must.
- Direct final-path writes make interrupted files look finished.

## Build order

Define `RunConfig`, `Job`, `FileResult`, `RunEvent`, and `RunSummary`; build a serial runner; add child isolation; add scheduling/admission; add reporting; then point CLI and TUI at the shared runner. Do not port legacy `_process_file_impl` repair logic.
