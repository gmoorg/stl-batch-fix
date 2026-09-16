# stl_batch_fix.py — Design and Implementation Reference

> **Keep this in sync with the code.** This file carries the *reasoning* —
> parameter values that took measurement to establish, why the steps run in the
> order they do, and what was tried and failed. None of that is recoverable from
> the source. When a change lands in `stl_batch_fix.py` or
> `stl_batch_fix_tui.py`, update this document in the same commit.
>
> It has drifted before: on 2026-09-13 it was 13 code commits behind and still
> documented `TIMEOUT` at 1200 s as a "per-file limit", a debris rule that had
> been replaced, and a pipeline order that no longer matched. Stale rationale is
> worse than none, because it is trusted.

## Purpose

Non-destructive batch repair of STL files for 3D printing. Goal: every output
file passes Bambu Studio's mesh check with zero errors, without remeshing or
changing geometry beyond what is strictly required to make it watertight.

Bambu Studio mesh errors the script targets:

- **Naked edges** — edges with 1 connected face (open hole in mesh)
- **Planar / Non-Planar holes** — boundary loops left unfilled
- **Non-Manifold edges** — edges with 3+ connected faces (slicing errors)
- **Inverted normals** — faces wound inward (slicer can't tell inside from outside)
- **Duplicate faces** — same vertices used twice
- **Degenerate faces** — 2 or 3 coincident vertices

Decimation is a **deliverable, not an optimisation**. Smaller files print
faster, can be uploaded to online repair tools when the script cannot handle
them, and — the reason it is non-negotiable — a too-large repaired file
decimated later in Bambu Studio immediately regains non-manifold edges and
needs repairing all over again.

---

## External dependencies

| Library | Role | Required |
| --- | --- | --- |
| **fast_simplification** | Primary decimator (numpy arrays, no mesh database) | Strongly recommended |
| **pymeshfix** | Repairs non-manifold AND open edges | **Required** |
| **pymeshlab** | Shell splitting and merging, decimation fallback | **Required** |
| **numpy** | Mesh arrays throughout | Required |
| **Blender 4.x** | Last-resort repair when the Python passes cannot finish | Optional but useful |

`BLENDER_BIN` in the environment overrides the executable name.

---

## Configuration

| Constant        | Default                    | CLI override                     | Meaning                                  |
|-----------------|----------------------------|----------------------------------|------------------------------------------|
| `INPUT_FOLDER`  | `/mnt/sda2/STL/Fixing/`    | `--input`                        | Source folder                            |
| `OUTPUT_SUFFIX` | `""` (empty)               | `--suffix`                       | Appended to output filename stem         |
| `MERGE_DIST`    | `0.01`                     | `--merge-dist`                   | Vertex merge radius, mm (Blender only)   |
| `BLENDER`       | `blender`                  | `BLENDER_BIN` env                | Blender executable name / path           |
| `RECURSIVE`     | `True`                     | `--recursive` / `--no-recursive` | Walk subdirectories                      |
| `WORKERS`       | `0`                        | `--workers`                      | Parallel workers; **0 = auto**           |
| `MIN_LAYER`     | `0.6` mm                   | `--min-layer`                    | Finest layer you print at; open boundaries smaller than this are accepted (0 = require zero) |
| `TIMEOUT_PART`  | `3600`                     | `--timeout-part`                 | Budget for **one mesh** — an unsplit model, or a single shell part |
| `TIMEOUT`       | `3600`                     | `--timeout`                      | Whole-file ceiling for a **split** model; `0` = no practical ceiling (24 h) |
| `BLENDER_RESERVE_PCT` | `30` %               | `--blender-reserve-pct`          | Percent of `TIMEOUT_PART` withheld from Blender for the steps after it |
| `MAX_FACES`     | `900 000`                  | `--max-faces`                    | Decimate above this — Bambu's complexity warning, not a memory limit |

`WORKERS = 0` derives the count from RAM and cores via `auto_worker_count()`,
capped at `AUTO_WORKERS_CAP` (6) and never exceeding the number of files.

### Timeouts

Two caps with different jobs. They are **not** a shared pool: a part finishing
in 1 s donates nothing to the next one.

**`TIMEOUT_PART` is set for throughput, not for a 100 % repair rate.** 100 %
success is not the goal: a file that exceeds the budget gets a `.timeout.stl`
marker and goes to another repair tool, which costs minutes of attention. An
hour spent on one pathological mesh is an hour of a worker not spent on the
other 760 files, to produce an output obtainable elsewhere — so an
over-generous cap costs *more* than a tight one.

Measured, after decimation to ≤ `MAX_FACES`:

```text
 2,055 defects ->   289s   succeeded
 2,263 defects ->   292s   succeeded
33,353 defects -> 3,080s   succeeded   <- lone outlier, 10x everything else
```

600 s catches 757 of 761 files with room to spare and diverts the outlier.
Raising it to 3600 was a mistake, corrected the same day: it optimised for
"never lose a repairable file" without asking what losing one costs.

**Face count is deliberately not an input.** Decimation caps every mesh reaching
PyMeshFix at `MAX_FACES`, so size cannot explain a 10× runtime spread between
two 900 k-face meshes; defect count can. Decimation itself is cheap and linear —
**4.6 µs per input triangle** (median over 187 events), 1,457 s for the entire
collection against 3,080 s for that one PyMeshFix pass. Decimation is ~2 % of a
large file's runtime and should not feature in a budget.

**`TIMEOUT` is a curation rule, not a measurement.** A split model that cannot
be repaired within an hour is one worth not keeping. Set it to taste. Because
`TIMEOUT_PART` is 3600, this ceiling binds as soon as a file splits: a 2-part
file wants 7,200 s and gets the hour.

`_part_cap(n) = min(_effective_timeout(), TIMEOUT_PART × n)`.

**`TIMEOUT = 0` means no practical ceiling** and resolves to `_NO_CEILING`
(86,400 s) rather than infinity, so a genuine runaway still stops. Always read
it through `_effective_timeout()` — reading the global directly treats `0` as
*no time at all* and kills every split file instantly. Three readers had that
bug when the sentinel was introduced, including the TUI watchdog guard
`if _fix.TIMEOUT <= 0: return`, which would have silently disabled the only
backstop against a worker wedged inside C++.

Within one mesh, Blender gets `TIMEOUT_PART − elapsed − reserve` rather than a
fresh budget, and is skipped entirely below `_BLENDER_MIN_RUN` (30 s). A flat
budget did not cap: a mesh 450 s into a 600 s budget handed Blender another
600 s and reached 1,050 s.

**Arming tightens only.** `_arm_mesh_alarm()` replaces a pending alarm only with
a *shorter* one, and `_cancel_mesh_alarm()` clears it when the file ends. Both
were bugs: arming used to overwrite unconditionally, so a split *widened* the
cap it was meant to enforce (`t=0.0s ARM 20s` → `t=3.5s ARM 100s`), and nothing
cleared it, so in the bare-script path an alarm armed for file N fired during
file N+1 and marked an innocent file `.timeout.stl`.

Logs are written to the output tree, derived from `INPUT_FOLDER` at run start
by `retarget_logs()`, so a run on a different folder keeps its diagnostics
beside its results.

Single file, for debugging:

```bash
python stl_batch_fix.py --one-file "Zelda NSFW/Chair_foot1.stl"
```

---

## File classification

Files are collected by walking `INPUT_FOLDER`. Excluded from collection:

- anything whose name contains `.part.` (`_PART_MARKER`) or `.seam.`
  (`_SEAM_MARKER`) — split parts and seam regions live in the output tree and
  are processed inline, never collected
- every `_SIGNAL_SUFFIXES` name: `.failed.stl`, `.timeout.stl`, `.broken.stl`,
  `.unrepaired.stl`, `.open.stl`, `.original.stl`, and the pipeline temporaries
  `.decimate.stl`, `.repairnm.stl`, `.pymeshfix.stl`, `.merge.stl`, `.partial`

Signal files are full copies of real meshes, so collecting one would repair it,
re-mark it, and repair it again.

`partition_already_done()` runs before any worker starts and skips files that
already have an output or a marker — on a mostly-repaired collection that saves
hundreds of pointless process dispatches.

---

## Output path

```python
input_folder  = os.path.abspath(INPUT_FOLDER)
out_root      = os.path.join(os.path.dirname(input_folder), 'Fixed')
dst           = os.path.join(out_root, rel_path_stem + OUTPUT_SUFFIX + ext)
```

Output root = **parent of the input folder** + `/Fixed/`.
Example: `INPUT_FOLDER=/mnt/sda2/STL/Fixing/` → output in `/mnt/sda2/STL/Fixed/`.

---

## Companion files

Non-STL files with extensions `.png .jpg .jpeg .gif .bmp .webp .tiff .tif .svg
.pdf .txt .md .readme` found in the source folder are copied to the output
folder preserving relative paths. Existing copies are skipped.

---

## Processing pipeline (per file)

```text
scan errors  (triangle count from the header, NM and open edge counts)
  ↓
Step C — decimate if > MAX_FACES
          fast_simplification → pymeshlab → blender, first that works
          (the two fallback rungs have never executed in any run —
           D12 in REFACTOR_DECISIONS.md proposes dropping them)
  ↓
Step B — split multi-shell  (PyMeshLab connected components)
          parts written to <output>/~parts/<mesh>.<MAX_FACES>/~<name>.part.N.stl
          each part is repaired INLINE, in the same worker
  ↓
Step E — repair with PyMeshFix  (runs when nm > 0 OR open > 0 OR a seam exists)
  ↓
Step E0 — volume-loss recovery, only when E deleted geometry:
          run Blender first (its repair makes the seam boundary explicit),
          then split at closed winding-seam loops, repair each region,
          merge back
          NOTE: a pre-emptive seam split before step E also exists in the
          source but is disabled (`if False and …`) — see Winding seams
  ↓
print-scale gate — open boundaries smaller than MIN_LAYER are accepted
                   as-is rather than sent to Blender
  ↓
Step F — Blender fallback  (only if defects remain, or PyMeshFix failed)
  ↓
post-verify — independent edge rescan before the output is accepted
```

There is no step D. PyMeshLab's non-manifold repair used to sit between C and
E; it is gone. See **Why step D was removed**.

**Pipeline order rationale:**

- **Split before repair**: PyMeshFix rebuilds one manifold surface and discards
  every other component, so a multi-shell mesh reaching it unsplit comes back as
  its largest shell alone.
- **Decimate before splitting, always**: the whole mesh is brought under
  `MAX_FACES` first, and the parts are slices of an already-decimated mesh. The
  face budget is therefore spent once, before anything is divided, and the merge
  lands under the limit — measured: every merged output in the logs comes in just
  under 900k, `Mandy_Clothed_SeamlessHipLegs` exactly on it.
  Splitting first would give each part its own `MAX_FACES` budget, so N parts
  could claim N × 900k: a figurine whose fingernail is a separate shell would
  decimate that fingernail to the same budget as the torso, and the merge would
  break the ceiling decimation exists to enforce. See D13 in
  `REFACTOR_DECISIONS.md`.
- **Never skipped, only ordered**: a mesh over `_LARGE_MESH_TRI_LIMIT` cannot be
  scanned or split at full size, but after decimation it can. Skipping the split
  outright is what deleted a model's head — see **Why the split comes after
  decimation**.
- **Blender before the seam split, not after**: straight from decimation the
  Mandy mesh has 5 seam edges in 0 closed loops and cannot be separated; after
  Blender's repair it has 40 in 7 loops and splits cleanly. Blender rebuilds the
  surface where the regions meet, turning an ambiguous join into an explicit
  boundary. Established in `2c524d8` — it, not `MAX_FACES`, is what fixed that
  model.
- **Blender is not only the last step.** It also runs inside the E0 recovery
  route and as a decimation fallback. Assuming otherwise is what made half of
  all Blender invocations invisible in the log until the timing and counting
  moved inside `_run_blender_script()`, where every route funnels through.
- **The print-scale gate sits before step F**: an open boundary smaller than one
  layer produces no toolpath, so sending it to Blender costs a subprocess and
  risks making things worse. Measured on `1st-body.stl`: Blender turned 4
  coincident 0.04 mm open edges into 28 holes of 0.02–0.31 mm, and the file went
  from 44.96 % to 99.99 % of its volume once the gate accepted them instead.

---

## Why step D was removed

PyMeshLab's `meshing_repair_non_manifold_edges` resolves a non-manifold edge by
**deleting the offending faces**, which turns a topology defect into boundary
loops. Measured on `Demon Queen/body.stl.stl`:

```text
source                2,061,994 tris  nm=3    open=0    z=[0.00, 108.23]
decimated               900,000 tris  nm=3    open=0    z=[0.00, 108.23]
+ step D                899,994 tris  nm=0    open=12   z=[0.00, 108.23]
+ pymeshfix             748,856 tris  nm=0    open=0    z=[4.90, 108.23]   ← 17% deleted
pymeshfix alone         899,976 tris  nm=0    open=0    z=[0.00, 108.23]   ← 24 faces
```

Step D fixed 3 non-manifold edges by tearing 12 open ones, and PyMeshFix's
reconstruction around those holes then deleted 151,144 faces and 4.9 mm off the
bottom of the model — the feet. PyMeshFix repairs non-manifold edges directly,
so step D was manufacturing the damage it then had to repair.

Step E's gate widened from `open > 0` to `nm > 0 or open > 0` to take over the
work. Previous behaviour is tagged `v1.0-pre-stepD-removal`.

Also ruled out while investigating:

- `meshing_repair_non_manifold_edges(method=1)` (split vertices instead of
  deleting faces) does not repair these edges at all — nm=3 in, nm=3 out — and
  OOMs past 12 GB on a mesh with 9,167 of them.
- Decimation itself is not the cause: visually identical to source, bounding box
  unchanged, and it introduces no new non-manifold edges on that model.
- MeshLib deletes offending triangles exactly as PyMeshLab does; ManifoldPlus
  voxel-rebuilds the surface and loses fine detail.

---

## Why the split comes after decimation

`_LARGE_MESH_TRI_LIMIT` (2,000,000) is the point above which the in-process
Python work — edge scan, PyMeshLab split, decimation — costs too much memory to
run with several workers. Step B used to be **skipped** above it.

`Mandy_Body_Dinamuuu3D.stl` is 2,061,994 triangles: 3% over the threshold, with
39 genuine shells and no debris. It reached PyMeshFix unsplit and came back as
one shell of 394,432 faces with the head — a 121,537-face component — deleted,
reported `ok`.

The information was not unavailable, only early: after decimation the mesh is
900k, well under the limit. So the split simply runs after decimation, for every
file. Measured: decimation preserves components (444 shells in, 445 out,
smallest still 3 vertices), and splitting 900k costs less than splitting the 2M
original would have.

*Implementation note:* the source reaches that order by a detour — step B tests
`_split_deferred` and skips itself, and step B2 runs the split later. B and B2
are the same operation reached two ways, an artifact of the order having been
changed rather than designed. D13 collapses them into one call site; the
behaviour is already what the single order describes.

`split_shells()` drops shells under `_MIN_SHELL_FACES` (100) faces, so a mesh
that is one real body plus hundreds of specks still returns no parts and takes
the normal path — 443 of `whole-costume01`'s 444 shells are under that floor,
and PyMeshFix discarding those is not a loss.

The floor is **flat, not a fraction of the largest shell**. The old rule,
`max(100, largest // 1000)`, scaled with the biggest shell and so discarded more
as the model grew: on a 2 M-face figure it set the floor at 1,315 faces, and 562
after decimation — large enough to silently drop a magnet peg or a locating pin,
which are parts, not debris.

*Known inconsistency:* the 2026-09-12 Falcon split kept parts of 38 and 36 faces
despite the floor of 100. Not yet traced — see `TODO.md` item 4.

---

## Winding seams (step E0)

PyMeshFix rebuilds one coherent surface. Handed a mesh containing two regions
that disagree about which way is *out* — hair over a scalp, cloth over a body,
a separately-sculpted part fused to its host — it keeps one and deletes the
other, and reports success. Measured on `Mandy_Body_Dinamuuu3D.stl` part 0:

```text
joined    562,288 faces -> 394,432   volume 13,730 -> 11,676   head GONE
split     394,148 + 167,321 faces, each internally consistent
each piece through PyMeshFix: 100.0% and 100.2% of volume preserved
merged    561,368 faces  nm=0 open=0 seams=0   volume 13,730
```

**Nothing else in the pipeline can see this.** The deleted region sits inside
the model's own bounding box, so the bbox check stays quiet, and
`scan_mesh_errors` counts non-manifold and open edges only — the file reports
`nm=0 open=0` and takes the clean-copy shortcut past every repair stage.

### The pre-emptive seam split is disabled — read this before re-enabling it

There was once a step E0 that split at closed seam loops *before* step E. It is
still in the source, behind `if False and _seam_loops > 0 …`, kept for the
measurements in its comments. **It never runs.**

Seam loops alone do not justify splitting. On a sphere with its cap reversed —
40 seam edges in 1 closed loop — PyMeshFix re-winds it correctly and returns the
same 760 faces, where splitting first gives 880 faces and introduces 2
non-manifold edges. The Mandy mesh that *needed* the split had **exactly 40 seam
edges too**. Nothing measurable before step E distinguishes "PyMeshFix will fix
this" from "PyMeshFix will delete this".

So the decision moved to *after* step E, where the damage is a measured fact
rather than a guess: the volume check compares enclosed volume before and after,
and only then calls `_repair_by_seam_split()`. That is the live path, and it is
what the pipeline diagram calls step E0 — recovery, not a pre-pass.

Re-enabling the old branch would split meshes PyMeshFix would have repaired
correctly, at a cost of extra faces and new non-manifold edges.

The two regions meet along **seam edges**, where both faces traverse the shared
edge the same way instead of in opposite directions. Only **closed loops** of
them trigger a split: a loop encircles something, whereas a few seam edges with
loose ends are local noise. The same model before repair had 5 seam edges in 0
loops and correctly split nothing; after Blender's repair it had 40 in 7 loops.

Symptom to recognise: a region renders **black** in viewers and in Bambu while
every defect count reads zero.

**Re-winding cannot fix this**, both approaches tried and measured:

- flipping faces by local majority leaves the seam (67 → 22 inverted)
- flipping globally from a BFS seed reaches 0 inverted but *inverts one of the
  surfaces* — head volume +1,503 → −2,615, total 13,730 → 9,623

The two surfaces cannot both be edge-consistent while joined, which is why an
online repair service remeshes the junction (+3,432 faces) instead of
re-winding it. Splitting sidesteps the question: each piece is already
consistent on its own.

Cost is one edge walk, ~3.5 s on a 560k mesh, against PyMeshFix's 16 s.

The seam split runs on shell parts too, not only whole files. The two splits
nest rather than compete — the shell split separates components that do not
touch, this one separates connected regions that disagree about orientation —
and a shell part is exactly where the second kind lives.

---

## Shell splitting (`split_shells`)

Uses `generate_splitting_by_connected_components` from PyMeshLab.

**Key implementation detail**: after the filter runs, PyMeshLab adds the
component meshes to the MeshSet *after* the original, which is still present at
its own index. Only iterate the new range:

```python
count_before = len(ms)
ms.generate_splitting_by_connected_components()
count_after = len(ms)
# Only range(count_before, count_after) — the rest is the original
```

**Debris filter**: shells with fewer than `_MIN_SHELL_FACES` (100) faces are
discarded as printing artifacts or zero-thickness surfaces.

**Parts** are written to `<output>/~parts/<mesh>.<MAX_FACES>/`, repaired inline
by the same worker via `_repair_part()`, then merged back by `_merge_parts()`
and deleted. The `~parts` folder is never walked by the collector, and nesting
the per-mesh directory inside it means the existing `d != PARTS_DIRNAME` walk
guard still excludes everything beneath.

**A part's filename is its state.** `split_shells()` writes each shell as
`~<name>`, and a successful repair renames it to `<name>`. Bare means finished,
`~` means pending or abandoned, so a rerun repairs only what is still pending.
Previously the state lived in sibling signal files, which outlived the part they
described: a stale `.failed.stl` made a part report `skip`, every merge site
counts `skip` as success, and the merge stitched in unrepaired geometry and
called it done. Parts therefore also ignore the four signal-file checks, which
exist for real inputs.

**The directory is keyed by `MAX_FACES`** because part indices are assigned by
face-count rank *after* decimation, so the same index is a different shell at a
different decimation target. Directories for this mesh at other settings are
deleted before a split, which makes a stale part unreachable rather than merely
detectable. `MAX_FACES` is not the only input to that ranking —
`_MIN_SHELL_FACES` and which decimator ran also shift face counts — so the `~`
protocol, not the directory name, is what guarantees correctness.

**Partial merges.** If some parts repair and others do not, what succeeded is
merged into `<name>.open.stl` (status `open`) and the source is still copied to
`.failed.stl`. All-or-nothing discarded a great deal: `Millenium_Falcon` splits
into a 90,712-tri body — nm=0 open=0 after repair, carrying the model's entire
117 × 36 × 155 mm bbox — plus four specks of 36–160 tris. One 160-tri fin failed
and the whole file was written off, losing 99.2 % of a repaired model. The
failed part is left **out** rather than pulled in from its `.original.stl`: a
non-manifold shell can make a slicer misbehave over the whole object, so a clean
model missing a small fin beats a complete one that may not slice.

**Anti-recursion**: the whole split block is guarded by `not is_part`, and every
recursive call passes `is_part=True`, so recursion is depth-1 by construction.

---

## Parallelism

`ProcessPoolExecutor` runs `WORKERS` processes. A pool worker does not do the
mesh work itself — it spawns `stl_batch_fix.py --one-file <path>` and waits:

```text
TUI / batch main loop
  └── pool worker  (survives everything)
        └── repair process  ← this is what gets killed on timeout or OOM
```

**Both entry points now submit `process_file_subprocess`.** They did not always:
the bare script called `process_file_safe` directly, so the protections you got
depended on how you launched the run — the TUI had child isolation, a
`communicate(timeout=)` kill and the SIGALRM cap, and the bare script had none
of them. A 3,106 s run that went uncapped was traced to exactly that, and the
dead-looking timeout machinery was in fact live on the path being used daily.

The asymmetry also left the bare path exposed to the hazard the child isolation
exists for: a dying pool worker fails **every** pending future, taking the
innocent files running beside it down too.

`_cancel_mesh_alarm()` still runs at the end of every file. With a child per
file an alarm cannot outlive its file, but the cancellation is the guarantee
rather than a side effect of the process model.

`_terminate_broken` in CPython fails **every** pending work item when one worker
dies, unconditionally. So killing a worker on timeout destroyed every file
running beside it: one run lost 635 s of work on an innocent 7M-triangle mesh,
another took down three files at once. With the repair in a child process the
pool never breaks, and siblings keep running.

The worker also knows *what* killed its child. A negative return code is the
signal number — `-9` SIGKILL (the OOM killer, or its own timeout), `-11` a
segfault in a mesh library — which is attribution the parent could never make
from a summary row stuck at `started`. That guesswork is why a set of timeouts
was once reported as "likely out of memory".

The timeout is enforced by the worker, not the parent: the worker is already
doing nothing but waiting, whereas the parent has to be scheduled to notice, and
under memory pressure that ran ~500 s late. The parent keeps a watchdog at 3× the
limit as a backstop for a worker that has stopped reporting entirely.

Split parts are **not** resubmitted to the pool. They are repaired inline inside
the worker that split them, so the main loop never sees them.

### Process-lifecycle hazards, learned the hard way

- `worker_status` is a `Manager().dict()` proxy. Every read is a blocking socket
  round-trip with **no timeout**, and it is read every 0.25 s by both the
  watchdog and the panel redraw. A worker SIGKILLed while holding that
  connection's lock blocks the parent forever — observed as `futex_do_wait`, 0 %
  CPU, no children, while the UI kept redrawing stale rows. `_status_snapshot()`
  does the read on a daemon thread it never joins.
- `shutdown(cancel_futures=True)` does not wake a worker already blocked reading
  the call queue; the executor's own management thread then joins it forever.
  `_abandon_pool()` SIGKILLs the pool's processes first and reaps them.
- `os.kill(pid, 0)` succeeds for a **zombie**, so a killed worker kept its row in
  the panel with the clock still counting up. `_pid_is_live()` reads
  `/proc/<pid>/stat` and treats state `Z` as dead.
- Falling off `main()` waits for multiprocessing's atexit child join, which a
  blocked worker never satisfies — a run printed its entire report and then sat
  there. `main()` flushes and calls `os._exit(0)`.
- `max_tasks_per_child` forces the `spawn` start method, which is incompatible
  with this fork-based design. Do not add it.

---

## Memory control

`_BYTES_PER_TRIANGLE` (~890 bytes) is measured end to end on a 7,000,034-triangle
mesh that peaked at 5.8 GB. From it:

- `estimate_peak_bytes(n_tris)` — projected peak for one file
- `mesh_is_too_large()` — a file whose own peak exceeds 80 % of the whole budget
  cannot be made to fit by reducing worker count
- `auto_worker_count()` — budget ÷ the **median** file's cost, capped by cores
  and `AUTO_WORKERS_CAP`. A high percentile was tried and is wrong: it lets one
  outlier set the ceiling for the entire run.
- `plan_worker_count()` — lowers concurrency as an individual large file comes up

`run.sh` wraps the run in a systemd scope with `MemoryMax` derived from
`MemTotal` minus a reserve, capped by `MemAvailable`, and `MemoryHigh` at 85 % so
the kernel throttles before it kills.

**Do not reintroduce a per-worker `RLIMIT_AS`.** It caps *virtual* address space,
and the C++ mesh libraries reserve far more VA than they reside: three workers
under a 2 GiB cap failed every decimation with `std::bad_alloc` at ~1 GB RSS
each. It is also inherited across fork/exec, so it killed Blender children too.

---

## State files

All state files contain real STL geometry — never empty placeholders. They are
named `<name>.<signal>.stl` so they open in any mesh viewer, and a marker is a
**fallback print**: a source the pipeline could not repair is often not a bad
mesh, just a large one, and printing it slowly beats not printing it.

| File                    | Contents          | Meaning                                        | Retry           |
|-------------------------|-------------------|------------------------------------------------|-----------------|
| `<stem>.broken.stl`     | copy of source    | Permanently bad mesh (corrupt or 0 faces)      | Never           |
| `<stem>.failed.stl`     | copy of source    | Transient error (crash, or the repair died)    | Delete to retry |
| `<stem>.timeout.stl`    | copy of source    | Exceeded `TIMEOUT`; written by the parent, since a killed worker never reaches its own cleanup | Delete to retry |
| `<stem>.unrepaired.stl` | copy of source    | Repair ran, non-manifold edges remain          | Delete to retry |
| `<stem>.undecimated.stl` | copy of source   | Every decimator failed; the mesh is still over `MAX_FACES`. A complete, printable model that simply was not reduced — run it through Bambu Studio's own simplify | Delete to retry |
| `<stem>.open.stl`       | repaired output   | nm=0 but open edges remain (slicers cope), **or** a partial split merge — what repaired, with the failed parts left out | Delete to retry |
| `<stem>.original.stl`   | copy of source    | Kept beside a real output whose repair moved the bounding box | Not a retry marker |

`.original.stl` is deliberately **not** a retry marker — the real output next to
it is a genuine result, and the pre-filter ignores this file.

The TUI's `d` menu deletes markers by category, which is the supported way to
requeue work.

---

## Bounding-box reporting

A repair adds or adjusts triangles inside an existing boundary, so it should not
move the model's extents. `stl_bounds()` streams the vertex block (no welding,
no memory spike) before and after, and `compare_bounds()` records any change in
the log, a `bbox_drift` summary column, a `BBOX` count in the panel, and an
end-of-run section.

It is an **inspection list, not a guard** — nothing is rejected. Magnitude alone
cannot say whether a change is damage or cleanup. Calibrated against inspected
results from a 141-file run:

| Change | File | Verdict |
|---|---|---|
| 0.033 mm | `Goblin/Poni1` | no visible difference |
| 1.077 mm | `Zelda NSFW/Chair_foot1` | end caps destroyed |
| 89.190 mm | `Transhuman_Girl/Leg1` | stray artifact removed — a correct repair |

`BBOX_TOLERANCE_PCT` is **0.7 % of the model's bbox diagonal**, floored at
`_BBOX_TOLERANCE_FLOOR` (0.1 mm). Relative rather than absolute because an mm
threshold assumes the file prints at 1:1 — rescaling a 40 mm part to 120 mm
turns a 0.3 mm drift into 0.9 mm — and a proportion is invariant under that.
The floor exists because a shell part can have a sub-millimetre diagonal
(`whole-costume01`'s parts measure 1.3 mm and 0.2 mm), where a pure proportion
would flag floating-point noise.

Measured against real diagonals, 0.7 % silences 4 of 20 flagged files —
`imp_stand_42mm` (0.181 mm on 35.9 mm), `imp_stand_70mm` (0.236 mm on 59.8 mm)
and two others — while `Stool_Base` (7.555 mm) and everything larger still
flags.

The previous `_BBOX_TOLERANCE` was 0.1 mm: in the empty gap between the noise and the real
changes, and below one layer height.

A face-count check would not substitute — `Chair_foot1` lost 0.14 % of its faces
while losing its end caps.

---

## Logging and crash safety

Three files in the output root, rotated `.1`–`.5` at run start rather than
truncated, so restarting after a bad run does not destroy the log explaining it:

- `repair_log.tsv` — per-step trace, written under `flock` by all workers
- `repair_summary.tsv` — one row per file, machine-readable
- `repair_steps.tsv` — one row per **step**, with before/after numbers
- `review_decimated.tsv` — files decimated `REVIEW_RATIO` (2×) or more

`log_summary_start()` writes a provisional `started` row before any work begins.
A worker killed mid-file never reaches the code that writes the real row, so
without it the one file that killed the run is the single file missing from the
summary. Its row is built from `_SUMMARY_COLUMNS` so adding a column cannot
silently misalign it.

### `repair_steps.tsv` — one row per step

The summary's `steps_ran`/`steps_failed` answer *did it work*. They cannot
answer *what did it fix*, because packing before/after counts into one cell
means parsing a cell to count anything — the same mistake the step log made in
prose. So each step also writes its own row:

```text
file    mesh    step    outcome  reason  secs  nm_in nm_out  open_in open_out
        tris_in tris_out  detail
```

Every question becomes a group-by rather than a regex:

```bash
awk -F'\t' '$3=="pymeshfix2"' repair_steps.tsv   # does PyMeshFix ever fix Blender's output?
```

`mesh` is what the step ran on, `file` the parent it belongs to, so a split
file's parts group with their parent. A part cannot name its own parent — it is
repaired by a separate `process_file()` call receiving only the part path — so
the parent stamps `_CURRENT_FILE` before repairing parts and parts read it from
there. Parts write their own rows; `_absorb_part_steps()` mirrors them into the
parent's summary columns with `_emit=False`, so nothing is counted twice.

**`pymeshfix2` carries Blender's numbers as its baseline.** The question this
row exists for — *does running PyMeshFix on Blender's output ever improve it* —
cannot be answered from the return value. `None` means "did not settle the
file", which conflates *ran and left nm>0* with *threw*; `ok` says it worked
without saying what it fixed. So `_try_pymeshfix_after_blender` takes a
`measured` dict the caller owns and fills it on every path, and `nm_in`/
`open_in` come from Blender's own post-verify rather than the file's original
counts — the comparison has to start where Blender finished. The `unrepaired`
route re-runs PyMeshFix on the *source* instead, so its rows are tagged
`from-source` in `detail`.

The archived 13-for-13 success rate is survivorship: the failures wrote nothing.

**Steps that decline are recorded too.** The print-scale gate used to log only
when it fired, so a gate that never ran and a gate that ran and rejected the
mesh were indistinguishable — and whether `MIN_LAYER` is set right is exactly
the open question. A rejection now records `printscale fail above-print-scale`
with `2loops/largest11.3137mm/layer0.6`, which says why.

### Step outcomes — why the summary counts them and the step log cannot

`steps_ran` and `steps_failed` record each step as `name:outcome`, with skips
carrying a reason. `--stats` aggregates them into per-step success rates.

They exist because the step log states what happened in sentences, and sentences
do not count. Measured while trying to answer "how often does the second
PyMeshFix rescue Blender's output": 26 grep matches for 13 actual events,
because one event prints two lines. Worse, `result: open` and
`result: unrepaired` appeared **zero times in 14,843 log lines** next to a
summary row recording `status='open'` — so a branch that never ran and a branch
that ran without logging were indistinguishable after the fact.

`ok`/`fail` mean the step ran; `skip` means it did not, and always carries why.
"PyMeshFix never ran because it is unavailable" and "PyMeshFix ran and failed"
are different facts that the old logging conflated.

**Parts report through their parent.** A shell part is repaired by its own
`process_file()` call with its own `stats`, discarded on return, and
`log_summary()` only runs for `not is_part` — so every step inside a split file
was invisible. Measured on the `foot1` fixture: its part 1 ran PyMeshFix (failed
on an empty mesh) then a Blender fallback that succeeded, and the parent row
recorded `path=none` with no steps at all. `process_file()` now carries `steps`
out on the result, exactly as it already did for `bbox_drift`, and the parent
folds them in via `_absorb_part_steps()` under a `part/` or `region/` prefix —
prefixed so a 39-shell file does not report 39 PyMeshFix runs as though the
whole mesh had been repaired 39 times.

### Density, not size — `tris_per_mm` and `min_feature`

A triangle count says nothing on its own: 5M triangles at 200 mm and 5M at
5 mm are the same number and completely different meshes. The Princess Leia
set makes the point — `Branches.stl` carries 5,576,353 triangles inside a
**5.6 mm** box, roughly a million per millimetre of extent, and that is what
predicts both a slow open in the slicer and a very heavy decimation.

- **`tris_per_mm`** — triangles ÷ largest extent. Separates "big model" from
  "absurdly dense model". Fixture range: `falcon` 1, `foot2` 76; Leia's
  `Branches` ≈ 995,000.
- **`min_feature`** — bbox diagonal ÷ √triangles, a rough average edge length.
  Below `MIN_LAYER` the mesh holds detail no layer can render, so decimation is
  discarding nothing real. This is what makes *was this decimation lossy?* an
  answerable question rather than a guess.
- **`merge_dist_pct`** — recorded in the Blender step's `detail` as
  `merge0.078%`, because it is a property of the operation, not the file. The
  same `MERGE_DIST = 0.01 mm` is 0.005% of a 200 mm body and 0.4% of a 2.5 mm
  head: a rounding error in one case, a weld that closes real detail in the
  other. If repair quality turns out to correlate with scale, this is the
  column that shows it.

`fmt` and `dims_mm` record the source format and model extents. Both were
already computed and thrown away: `is_ascii`/`is_obj` rode on the result dict,
and `stl_bounds()` is called twice per file for drift detection. Without them,
"do ASCII sources fail more often" and "is the print-scale gate firing on small
models" cannot be asked.

`_post_verify()` returns `(nm, open, verified)`. The third value exists because
the first two cannot express *unknown*: a scan that could not run — ASCII input,
a scan error, a mesh too large — used to return a bare `(0, 0)`, which callers
read as verified-clean and wrote out as `ok` having checked nothing. Results
carrying `verified=False` display as `OK UNVERIFIED`.

---

## `libs/` — domain-free modules

Modules that know nothing about this project. The rule is deliberately strict:
nothing in `libs/` may import the pipeline, mention meshes, or assume what the
work items are. A module earns its place there by being usable in an unrelated
program without edits.

`libs/pool.py` — N threads, each pulling work from a caller-supplied function.
Implements D4 in `REFACTOR_DECISIONS.md`. The pool owns **a lock and a shutdown
flag**, and nothing else: not the items, not the ordering, not the admission
rule, not even which item each thread holds — the worker passes its finished
item back, because the worker is the only thing that knows.

```python
Pool(n_workers, item_selector, item_handler).start()

item_selector(done, error) -> T | None   # next item, or None to shut this
                                         # worker down
item_handler(item)                       # do the work
```

**The pool owns the loop.** The caller supplies work, not control flow —
there is no `get_next` and nothing to write a `while` around.

**One call, one place, one outcome.** The selector learns what finished
(`done`), whether it succeeded (`error` is the exception or `None`), and
decides what runs next. `error` carries the exception rather than a flag, so
the diagnostic detail survives.

`item_selector` runs **with the lock held**, so the caller's queue and
accounting need no locks of their own; serialising those calls is the pool's
entire contribution. `item_handler` runs **without** it, concurrently: that is
where a subprocess is spawned and a mesh repaired, and holding the lock across
it would serialise every worker and make the pool pointless.

**One call does two jobs**: it reports the item a worker just finished (`done`,
`None` on the first call) and returns the next one. That is what makes resource
accounting possible — a budget policy releases what the finished item reserved
before deciding what fits next. Every item is reported, including each worker's
last, so a running total returns to zero.

**Returning `None` shrinks the pool.** With a cheapest-first queue an item that
does not fit now never will — everything after it is larger — so shutting the
worker down frees its share for the workers still running. `n_workers` is a
ceiling, not a target: surplus threads ask once, are told `None`, and exit,
which costs microseconds and is why the pool need not know the queue's length.

**A failed item is still reported.** The loop assigns `done` after the
try/except, so an item whose handler raised comes back to the selector exactly
like one that succeeded — whether a failure should release its resources is the
policy's business, and it sees the item either way.

**There is deliberately no separate error callback.** An earlier version had
one, and it created a trap: the failed item reached both the error handler and
the selector, so a caller that released resources in both freed them twice, and
a budget policy drifted upward until it admitted work there was no memory for.
The fix was not to document the hazard but to remove it — collapsing the two
into `item_selector(done, error)` leaves one place that can release, so double
release is not expressible. `test_a_failed_item_is_released_exactly_once` pins
the behaviour.

Two earlier shapes were tried and dropped:

- **The pool kept `dict[thread name → item]`** so it could work out `done`
  itself. That duplicated a value the worker already held in a local, and its
  only observable use reported progress keyed by `"w3"`, which means nothing to
  any display. Since workers write their own logs, nothing wanted it.
- **The caller wrote the loop** around a public `get_next(done)`. That is
  correct only if the caller remembers a `try` — forget it and an exception
  leaves the loop with the in-flight item, so the policy is never told and a
  budget silently loses that capacity. Moving the loop inside removed the
  possibility rather than documenting the hazard.

---

`libs/indicators.py` — what the filesystem already says about a source file.
Detection only: it looks for the markers a previous run left, reports the last
one found, and decides nothing. Whether a finding means skip, convert or
process is the caller's judgement.

```python
check(source, input_folder, output_file) -> Finding(source, indicator, path)
```

Two trees are involved, which is why this is a module rather than a few
`os.path.exists` calls at a call site:

```text
COMPANION     not a mesh at all              copy it, or it is already copied
SOURCE tree   <input>/stl-exported/<rel>.stl   already converted — use this path
OUTPUT tree   <output>/<rel>.stl               already fixed
              <rel>.broken .failed .unrepaired .open .timeout   how it ended
```

**Companions are checked first.** Pass `copy_extensions` and a matching file
reports `COPY_AS_IS` or `ALREADY_COPIED` without touching anything else: none
of the mesh markers can exist beside a `.png`, so testing for them is
meaningless work. The set is injected rather than hardcoded, because which
extensions count as companions is the caller's policy and not a fact about the
filesystem.

`ALREADY_COPIED` is deliberately distinct from `ALREADY_FIXED`. Same existence
test, different claim — collapsing them would make any count of *repaired*
files wrong, the same conflation that let `status='open'` mean two unrelated
things.

Checks run in that order and the **last** match is reported, so anything in the
output tree outranks an export — converting a file that will not be processed
is wasted work. Among the output markers the choice is cosmetic: every one of
them means the file has been dealt with, so which is named affects the message
and not the outcome.

`.original.stl` is deliberately *not* an indicator. It sits beside a
**successful** output as evidence that the repair moved the bounding box, so
treating it as one would skip files that actually worked.

`libs/converter.py` — the preparation walk. Decides what each file *is*, copies
the things that are not meshes, converts the ones that cannot be measured as
they stand, and hands the rest to a consumer.

```python
prepare(source_root, output_root, emit,
        copy_extensions=..., convert=..., workers=4) -> Summary
```

Two phases, because a converted file's triangle count does not exist until
Blender has written it:

```text
walk       classify every file        copy companions, emit binary STLs,
                                      set ASCII/OBJ aside
convert    drain the set-aside files  pool → probe the result → emit
```

**Order is not promised.** Binary files arrive during the walk and converted
ones afterwards. That costs nothing: the repair queue must be sorted by
triangle count for memory admission (D13) regardless, and only the consumer
knows that — so the converter is a *source* of items, not the thing that orders
them.

**Failures are emitted, not dropped.** A file that cannot be converted comes
through as a `Mesh` with `is_valid=False` and a `problem`. Silence would make
this the one place in the pipeline where a file vanishes without appearing in
any count; the consumer can ignore it, but it cannot report what it never sees.
That also keeps logging a later decision rather than one baked in here — `emit`
is the gateway, and it can filter.

**Copies run inline during the walk**, not in the pool. They are fast enough
not to matter and keeping them inline is less machinery.

**`emit` is called one at a time**, in both phases. `prepare` serialises it, so
a consumer needs no locking of its own even though the conversion phase runs
several workers. The lock is uncontended during the walk and costs microseconds
across a collection — a better trade than a contract reading "serial here,
concurrent there, lock accordingly", since a caller cannot forget a lock that
is not theirs to take.

Returning a `queue.Queue` instead was considered: thread-safe for free, and a
consumer could start before the walk finished. Rejected because nothing *can*
start early — the queue has to be complete before it can be sorted — and
`Pool`'s selector sheds a worker rather than waiting, so a gradually-filling
queue would end the run. The overlap a queue buys is overlap this pipeline
cannot use, against a sentinel protocol and a thread for the caller to manage.

> The counters needed a plain `threading.Lock`, and two cleverer arrangements
> were wrong first. Counting inside the pool's selector looks free, since the
> pool serialises that call — but the selector is told only *that* an item
> finished, not which result it produced, so pairing a completion with its
> outcome meant popping a shared list, and four workers finishing out of order
> attributed the wrong ones. A lock needs no reasoning about which call the
> pool happens to serialise.

`libs/scanner.py` — count a mesh's topological defects, from the geometry in
memory. Four questions off one edge map:

```python
scan(mesh)                      -> Scan(open_edges, non_manifold, faces, degenerate)
open_loops(mesh)                -> the open boundaries, largest first, measured
open_loops_are_printable(mesh, min_layer)
winding_seams(mesh)             -> (seam_edge_count, closed_loops)
seam_edges(mesh)                -> the edges themselves, (n, 2) int64
shells(mesh) / shell_count(mesh, min_faces)
```

**Why it is one module.** A defect count is the pipeline's decision variable,
not a report: it decides whether a repair is needed, whether one worked, and
whether a file may be written out as finished. The old pipeline asked after
decimation, after Blender, and twice around PyMeshFix — eight call sites, each
re-reading the file and rebuilding the edge map. Building that map is the work;
all four questions come off it.

**The counting rule**, stated once so it cannot drift: an edge used by exactly
one face is **open**, by exactly two is **sound**, by three or more is
**non-manifold**.

**No triangle ceiling.** `scan_mesh_errors` returned `(-1, -1)` above
`_LARGE_MESH_TRI_LIMIT = 2_000_000` and `_post_verify` turned that into
"UNVERIFIED" — on exactly the meshes that are hardest to scan *and* most likely
to be broken. With welded faces the indices are the identity, so the old
192-bit packed keys, the `-0.0` folding and the ceiling all go away together.

**An unloaded mesh raises rather than reporting clean.** This is the same
hazard `_post_verify`'s three-valued return was added to kill: an unscannable
mesh returning `(0, 0)` reads as verified-clean at every call site, and files
were written out as finished having been checked by nothing. Here the case
cannot be represented.

**`is_clean` stays mathematical; printability is a separate question.** No open
edges and no non-manifold edges is the standard; whether a mesh that fails it
is nevertheless printable is `open_loops_are_printable`, which measures each
hole's *span* against the layer height. Keeping those apart is what stopped the
pipeline destroying a model to close pinholes no printer could express.

`libs/decimator.py` — reduce a mesh to a face budget, by whichever library is
available. One entry point, two implementations behind it:

```python
is_available()                  # startup: can anything decimate?
available_rungs()               # startup report: what is missing
decimate(mesh, max_faces) -> Result(mesh, rung, faces_in, faces_out, attempts)
```

Both rungs run the same Garland-Heckbert quadric edge collapse and differ only
in how much machinery sits around it. Measured, 2.55M triangles → 900k:

| rung | time | peak | works on |
|---|---|---|---|
| fast_simplification | ~5.6s | ~915 MB | numpy arrays |
| pymeshlab | 42.0s | 1557 MB | numpy arrays |

**Size does not select the decimator.** fast_simplification handles a 2.55M
mesh in less memory than PyMeshLab needs for 1.2M, so there is no band where a
mesh is too big for one and must go to the other. PyMeshLab is for when
fast_simplification *fails*, not for when the mesh is large.

**Nothing here touches the disk.** Both rungs work on the arrays directly —
PyMeshLab takes numpy in (`Mesh(vertex_matrix=, face_matrix=)`) and gives it
back, so `load_new_mesh`/`save_current_mesh` would be a filesystem round trip
inside our own process for no reason. Two tests assert that decimation writes
no files at all; that is the claim the in-memory design rests on.

**There was a third rung, Blender, and D19 removed it.** It never ran: 104 of
104 decimations in the step log took fast_simplification, with no failure and
no fallback. But that record is only the meshes fast_simplification happened to
handle, and is not what justified the removal. What justified it is that a
manual fallback exists — Bambu Studio's own simplify — so a mesh defeating both
rungs is a file to mark and handle by hand, not a lost model. The Blender rung
bought one manual step on a case that has not occurred, against a script, a
subprocess, temp files and a format boundary. `blender_fx/decimate.blender` is
deleted with it.

**Failure returns the input unchanged**, with every attempt recorded, and the
caller writes `.undecimated.stl`. Decimation is a deliverable, so a caller must
be able to tell *not decimated* from *decimated badly* and mark the file rather
than ship it; `attempts` exists because a silent fallback is indistinguishable
from a first-choice success at the call site.

`libs/mesh_io.py` — what a mesh file says about itself, and the mesh itself
when a step actually needs it. The two are deliberately separated: everything
in the first group answers from the header or by streaming, because those
answers decide whether a file is queued at all and in what order, so they must
be cheap enough to ask about every file in a collection before any work starts.

```python
probe(path) -> Mesh(path, kind, triangles, is_valid, problem, geometry=None)
bounds(path) / dimensions(path) / diagonal(path)

load(mesh)  -> Mesh          # a NEW mesh, .geometry attached
write(mesh, path)            # the one writer
mesh.with_geometry(geometry) # how a step reports a changed mesh
```

**Loading is an explicit call, never a property.** A probed `Mesh` is a couple
of hundred bytes; a loaded one is the welded mesh in RAM — measured 174 MB peak
for 1M triangles, 383 MB for 2.55M. A worker's memory budget is decided before
it starts, so a load that happened implicitly on first attribute access could
blow that budget from inside what looks like a field read.

**`Mesh` is frozen and every operation returns a new one.** `load` does not
fill geometry in on the mesh it is given; it hands back a second mesh carrying
it. That is what keeps the queue cheap — a `Mesh` waiting to be processed
cannot have quietly become a 400 MB object while it sat there. See D15.

**One writer.** `write` takes a `Mesh`, so no caller assembles a header itself
and exactly one place knows an STL facet is 50 bytes with a real normal in the
first 12. The normals are computed, not zeroed: viewers disagree about a zero
normal — some fall back to the winding, some guess — which made honest
comparison between two outputs impossible.

**An unknown count is `None`, never `0`** — the one deliberate behaviour change
from the code it replaces. `check_stl_integrity` returned `0` triangles for an
ASCII STL, and `measure_files` then sorted those *first*, as the cheapest work
in the queue, when they may be the most expensive things in it. An OBJ got
`size // 60` as a guess. Both are numbers the queue believed and neither was
measured. `None` forces the caller to decide, which under D7 means converting
first and measuring the conversion.

**The ASCII tiebreaker is load-bearing.** Some exporters (SolidWorks, older
Slic3r) write a `solid <name>` text header onto a *binary* file, so "starts
with solid" is not enough: when the binary count field agrees with the file
size, the file is binary despite the header. Only the first 256 bytes are
sniffed, so an ASCII STL whose solid name runs past ~240 characters before the
first `facet normal` is misread as binary — one-directional, because it is then
parsed as binary, the count field is garbage, and the size cross-check rejects
it as invalid rather than repairing wrong bytes. A false "invalid" on a file
nobody produces, against a larger read on every file in a collection.

**Size cross-check, both directions.** A file too short for the triangles it
claims is invalid. A file *longer* is fine — some exporters append colour data
after the last triangle, non-standard but read correctly by every slicer.

`bounds` streams in 200k-triangle passes and skips bytes 0:12 of each record,
which are the face normal: including them would widen every box by whatever
the exporter happened to write there.

`libs/blender.py` — run a script in headless Blender, with a deadline. It knows
how to launch Blender, wait for it, kill it if it overruns and clean up after
itself; it knows nothing about meshes, about what the script does, or about
what its output means.

```python
runner = Runner()                       # or Runner('/path/to/blender')
result = runner.run(script_text, timeout=420)

result.exit_code        # None if it was killed
result.stdout_capture   # the caller's protocol lives in here
result.is_timed_out
result.second_elapsed   # filled on the kill path too
```

**No budget arithmetic.** `timeout` is seconds, supplied by the caller. How
much of a mesh's remaining time Blender may have, and what to reserve for the
steps after it, is pipeline policy that changes with the pipeline.

**No `/proc` walking**, and that was measured rather than assumed — see D14.
Within one process Blender is a **direct** child with no children of its own,
so `Popen.kill()` reaches it. The grandchild problem is real but belongs to
whoever kills an intermediate process: killing a `--one-file` child alone
leaves its Blender alive and reparented to init, holding its memory.

**`Runner` is a class rather than a function** because a signal handler needs
to reach a Blender that is already in flight, so the handle has to live
somewhere. Holding it on an instance means two runners cannot fight over it and
the caller decides what is shared. `kill_current()` is safe from a handler and
from another thread.

`second_elapsed` is filled on the timeout path as well as the success path: a
killed run still spent its time and still cost a launch, so a caller
accumulating cost should read it rather than timing the call itself.

**`convert()`** turns an OBJ or an ASCII STL into a binary STL:

```python
ok, path = blender.convert(source, destination, timeout=600)
```

It lives here rather than in the pipeline because a format conversion is
generic Blender work — *load this, save that as binary STL* — with no
`MERGE_DIST`, no markers of ours, no protocol. The repair and decimation
scripts fail that test and stay outside. This is what makes D7 buildable:
`indicators.check()` could already report `EXPORT_READY`, and until now nothing
could produce one.

It returns `(ok, path)` rather than a `Result`: a caller almost always wants to
know whether the file is there now, and anyone needing stdout can render
`CONVERT_SCRIPT` and call `Runner.run` directly.

The script writes `<destination>.partial` and renames on success, so an
interrupted conversion cannot leave a file that a later run mistakes for a
finished one — existence is what decides whether a conversion is reused.

**Scripts live in `libs/blender_fx/`** as `<name>.blender`, read at import and
rendered with `str.format`. They are data, not modules. Kept as files rather
than string literals because a Blender script is Python that an editor should
be able to read, and burying it in a quoted block makes it unreadable and
unlintable. `fix` and `decimate` will join `convert` there when the pipeline's
own scripts move.

**`_kill` kills but does not reap**, and that separation is load-bearing.
Reaping means `communicate()`, which closes the pipes — and a kill arriving
from a signal handler or another thread lands while the thread inside `run` is
mid-`os.read` on exactly those descriptors. Measured: it raises
`OSError: [Errno 9] Bad file descriptor`, that propagates out of `run`, and no
`Result` is ever built. So reaping belongs to whoever is already waiting; the
timeout path calls `communicate()` itself immediately after killing because it
*is* the waiter. Consolidating the two kill sites into one helper is right —
consolidating the reap with it is not.

---

## Tests

`test_blender.py` — 32 tests, ~20 s. Most never launch Blender: the module's
job is process handling, and that is exercised against a stand-in executable in
milliseconds, deterministically, on a machine with no Blender installed. A
stand-in is a real subprocess, so `Popen`, `communicate`, the timeout and the
kill are all genuinely tested — only the program on the other end differs. The
few tests that need the real thing are guarded by `is_available()`.

> **A stand-in must `exec`.** A shell script running `sleep 30` is *two*
> processes: `kill()` reaches the shell, but `sleep` survives holding the
> stdout pipe open, so `communicate()` waits the full 30 s. That modelled the
> wrong shape and produced three failures against correct code — the suite took
> 183 s instead of 15 s. Blender is a single process, so `exec sleep 30` models
> it and a bare `sleep 30` models something else entirely.

`test_converter.py` — 19 tests for `libs/converter.py`, ~0.05 s. `convert` is
injected, so a fake one records what it was asked to do and writes whatever the
test needs — no Blender. Covers the walk's exclusions (`stl-exported/`,
AppleDouble sidecars), companions copied but never emitted, an existing export
reused without reconverting, failures emitted rather than dropped, and exact
counts with 8 workers on 40 files.

`test_scanner.py` — 31 tests for `libs/scanner.py`, ~6 s. Fixtures are built by
hand from index arrays rather than read from files, because every expected
count has to be derivable on paper: a tetrahedron has 4 faces and 6 edges each
used twice; remove one face and exactly 3 edges become open. A test whose
expected number cannot be justified without running the code is testing
nothing. Most of the runtime is one deliberate case — a 2.1M-triangle scan,
just past the limit where the old file-based scanner gave up.

`winding_seams` additionally carries a **differential test against the original
dict-based implementation**, kept verbatim in the test file as the oracle: 400
random meshes, including non-manifold and degenerate ones, must agree exactly.
That check earned its place — the first vectorised rewrite compared
`forward[:-1] == forward[1:]` (length n-1) against a mask of length n and
raised on every mesh with a seam candidate, 200 of 200 trials. It was caught
because the differential check ran *before* any unit tests were written; tests
written first would have encoded the broken behaviour.

`test_decimator.py` — 19 tests for `libs/decimator.py`, ~0.4 s. Both rungs are
third-party quadric edge collapse, so what is tested is the *ladder*, not the
algorithms: which rung runs, that a failure falls through to the next, and that
a mesh already within budget is returned untouched. Rungs are forced by
patching the module's availability flags, so a test can reach the second
without needing the first to genuinely break. Two tests assert that decimation
writes **no files at all** — the claim the in-memory design rests on — and one
pins that no Blender rung exists (D19).

`test_mesh_io.py` — 44 tests for `libs/mesh_io.py`, ~0.7 s. Every file is
generated by the test that needs it, because the interesting cases are the ones
no ordinary fixture contains: a binary STL wearing a text header, a file
truncated mid-triangle, a file with bytes appended after the last one, and a
240k-triangle mesh that crosses the streaming reader's chunk boundary.

The `load`/`write` cases assert the properties that are easy to lose silently:
that welding actually dedups (12 corner slots become 4 vertices), that `-0.0`
welds with `0.0` rather than splitting a shared vertex, that a written mesh
reloads identically, that normals are unit-length and a degenerate face gets a
zero normal rather than a NaN, and that `load` leaves the mesh it was given
untouched. Bad data is a result and only a bad request raises: loading a
truncated file returns an invalid `Mesh`, while loading an ASCII STL or writing
an unloaded mesh raises `ValueError`.

`test_indicators.py` — 21 tests for `libs/indicators.py`, ~0.01 s. Every file
is an empty touch, since the module tests for existence and never opens
anything.

`test_pool.py` — 24 tests for `libs/pool.py`, ~0.9 s. No meshes, no
subprocesses: fake work is a short sleep and policies are plain closures over
each test's own list, so it is fast and deterministic. It covers the half of
the system `test_pipeline.py` cannot reach — every item handled exactly once
under 16-way contention, `select` never called concurrently, every item
reported back including each worker's last, a budget policy holding a capacity
ceiling end to end and returning to zero, shedding by returning `None`, and
`stop()`.

`test_pipeline.py` — 36 end-to-end tests, ~3.8 s:

```bash
.venv/bin/python test_pipeline.py            # all of it
.venv/bin/python test_pipeline.py body arms  # named fixtures
```

Each test runs the real `process_file()` and compares the **output mesh against
the input mesh**: component count, bounding box, an independent defect rescan,
and face count against the decimation target.

Fixtures are small generated meshes in `tests/fixtures/` (~516 KB, built by
`make_fixtures.py`), not models from the collection — the collection is meant to
be deletable, and 208 MB of fixtures does not belong in a repository. `MAX_FACES`
and `_LARGE_MESH_TRI_LIMIT` are configuration, so the tests lower both and a
3,216-triangle mesh takes the same branches a 2M-triangle one does.

What the tests do not cover: a bug that only a real multi-million-triangle sculpt
triggers. For that, run the collection and read the bbox flags.

---

## Known limitations

- **PyMeshFix closes intentional boundaries.** Given a mesh whose only defect is
  open edges, it pulls the boundary shut rather than patching it — this removed
  ~1.1 mm of end cap from `Zelda NSFW/Chair_foot1`. Not fixed: Bambu Studio does
  ask for those holes to be filled, so skipping the repair is not obviously
  right. The `foot1` test pins the current behaviour so a change to it is
  visible.
- **Decimation produces non-manifold edges** and this is unavoidable. Quadric
  edge collapse ranks collapses by geometric error; `fast_simplification`'s only
  collapse guard is a normal-flip check, with no manifold test at all, and
  PyMeshLab and Bambu leak the same way. Lower `agg` values are *worse*
  (7.0 → 9,167 NM edges; 3.0 → 11,276).
- **Sub-mm irreducible gaps**: boundary loops under ~0.01 mm that cannot be
  safely merged or filled. Reported as residual open edges; slicers ignore them.
- **Debris shells** under `_MIN_SHELL_FACES` (100) faces are silently dropped
  from split output. The floor does not always hold: the 2026-09-12 Falcon split
  kept parts of 38 and 36 faces. Untraced — `TODO.md` item 4.
- **`is_ascii_stl` reads only 256 bytes**, so an ASCII STL whose solid name runs
  past ~240 characters is misread as binary. It then fails the header size
  cross-check and is reported corrupt rather than silently mangled.

---

## What NOT to do (lessons learned)

### Pipeline

- **Do not** run PyMeshFix on an unsplit multi-shell mesh — it rebuilds one
  manifold surface and discards the rest. This deleted a model's head.
- **Do not** hand PyMeshFix a mesh with closed winding-seam loops. Same failure
  for a different reason: the regions are connected, but disagree about which
  way is out, and it keeps one and deletes the other. Split at the seam first.
- **Do not** try to fix a winding seam by re-winding faces. Local flipping
  leaves the seam; global flipping inverts one of the two surfaces. They cannot
  both be edge-consistent while joined.
- **Do not** gate the seam check on `not is_part`. A shell part is exactly where
  nested surfaces live.
- **Do not** skip the shell split for oversized meshes. Defer it until after
  decimation instead.
- **Do not** reintroduce PyMeshLab's non-manifold repair between decimation and
  PyMeshFix — it converts NM edges into open edges by deleting faces, and
  PyMeshFix's reconstruction around those holes is what destroys geometry.
- **Do not** use Blender's DECIMATE modifier — it does not preserve topology.
- **Do not** run `select_more()` for NM face deletion in Blender — it expands
  into adjacent good geometry, leaving craters patched with flat triangles.
- **Do not** scope `seen_junctions` across Blender repair passes — vertex
  indices are renumbered after deletions and stale indices match wrong vertices.
- **Do not** use `bpy.ops.export_mesh.stl` — it introduces a spurious NM edge.
- **Do not** remove slivers on Blender repair passes > 1 — it re-opens boundary
  loops and the counts oscillate instead of converging.
- **Do not** use a global normal vote in Blender — it flips correct shells
  outnumbered by an inverted one. Use per-component BFS.
- **Do not** use a union-find / snap-grid approach for shell detection — a grid
  aggressive enough to close micro-gaps also merges legitimately separate shells,
  such as two arms of a figure that are close but not touching.

### Files and state

- **Do not** collect `.part.N.stl` files. Parts live in `<output>/~parts/` and
  are repaired inline by the worker that split them; collecting them would
  process them twice. *(This reverses earlier guidance from when parts were
  resubmitted to the pool.)*
- **Do not** make any marker an empty file, a hardlink, or a log line. They are
  full copies because they are fallback prints.
- **Do not** treat `.original.stl` as a retry marker.
- **Do not** include the original mesh in split output — PyMeshLab keeps it in
  the MeshSet after `generate_splitting_by_connected_components`.

### Processes

- **Do not** kill a pool worker to enforce a timeout — CPython fails every
  pending future when a worker dies. Kill its child repair process instead.
- **Do not** call `shutdown()` on a pool whose worker was SIGKILLed without
  killing the processes first.
- **Do not** trust `os.kill(pid, 0)` as a liveness check — it succeeds for
  zombies.
- **Do not** read a `Manager` proxy on the thread that must stay responsive.
- **Do not** set a per-worker `RLIMIT_AS`.
- **Do not** add `max_tasks_per_child` — it forces the `spawn` start method.
