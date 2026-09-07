# stl_batch_fix.py — Design and Implementation Reference

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
| **pymeshfix** | Repairs non-manifold AND open edges | Recommended |
| **pymeshlab** | Shell splitting and merging, decimation fallback | Recommended |
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
| `TIMEOUT`       | `1200`                     | `--timeout`                      | Per-file limit; the repair process is killed |
| `MAX_FACES`     | `900 000`                  | `--max-faces`                    | Decimate if face count exceeds this      |

`WORKERS = 0` derives the count from RAM and cores via `auto_worker_count()`,
capped at `AUTO_WORKERS_CAP` (6) and never exceeding the number of files.

`TIMEOUT` applies to the whole per-file repair, not only to Blender. It is
enforced by the worker against its own child process — see **Parallelism**.

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
Step B — split multi-shell  (PyMeshLab connected components)
          parts written to <output>/~parts/<name>.part.N.stl
          each part is repaired INLINE, in the same worker
          deferred to B2 when the mesh is over the scan limit
  ↓
Step C — decimate if > MAX_FACES
          fast_simplification → pymeshlab → blender, first that works
  ↓
Step B2 — the deferred split, now that decimation has brought the mesh
          under the scan limit
  ↓
Step E0 — split at winding seams  (closed seam loops only)
          regions repaired separately, merged back as one file
  ↓
Step E — repair with PyMeshFix  (runs when nm > 0 OR open > 0)
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
- **Split deferred, never skipped**: a mesh over `_LARGE_MESH_TRI_LIMIT` cannot
  be scanned or split at full size, but after decimation it can. Skipping the
  split outright is what deleted a model's head — see **Why the split is
  deferred**.
- **Decimate per shell**: quadric edge collapse gets the correct face budget for
  each part independently.
- **Blender last**: launching a subprocess costs more than the Python passes,
  and most files never need it.

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

## Why the split is deferred

`_LARGE_MESH_TRI_LIMIT` (2,000,000) is the point above which the in-process
Python work — edge scan, PyMeshLab split, decimation — costs too much memory to
run with several workers. Step B used to be **skipped** above it.

`Mandy_Body_Dinamuuu3D.stl` is 2,061,994 triangles: 3% over the threshold, with
39 genuine shells and no debris. It reached PyMeshFix unsplit and came back as
one shell of 394,432 faces with the head — a 121,537-face component — deleted,
reported `ok`.

The information was not unavailable, only early: after decimation the mesh is
900k, well under the limit. Step B now defers, and step B2 runs the same split
afterwards. Measured: decimation preserves components (444 shells in, 445 out,
smallest still 3 vertices), and splitting 900k costs less than splitting the 2M
original would have.

`split_shells()` drops shells under `max(100, largest // 1000)` faces, so a mesh
that is one real body plus hundreds of specks still returns no parts and takes
the normal path — 443 of `whole-costume01`'s 444 shells are 3-to-100 vertex
debris, and PyMeshFix discarding those is not a loss.

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
`nm=0 open=0` and takes the clean-copy shortcut past every repair stage. That
is why the seam check runs *before* that shortcut rather than inside step E.

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

**Debris filter**: shells with fewer than `max(100, largest_shell // 1000)`
faces are discarded as printing artifacts or zero-thickness surfaces.

**Parts** are written to `<output>/~parts/<name>.part.N.stl`, repaired inline by
the same worker via `process_file(part, is_part=True)`, then merged back by
`_merge_parts()` and deleted. The `~parts` folder is never walked by the
collector.

**Anti-recursion**: the whole split block is guarded by `not is_part`, and every
recursive call passes `is_part=True`, so recursion is depth-1 by construction.

---

## Parallelism

`ProcessPoolExecutor` runs `WORKERS` processes, but a pool worker does not do
the mesh work itself — it spawns `stl_batch_fix.py --one-file <path>` and waits.

```text
TUI / batch main loop
  └── pool worker  (survives everything)
        └── repair process  ← this is what gets killed on timeout or OOM
```

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
| `<stem>.open.stl`       | repaired output   | nm=0 but open edges remain (slicers cope)      | Delete to retry |
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

`_BBOX_TOLERANCE` is 0.1 mm: in the empty gap between the noise and the real
changes, and below one layer height.

A face-count check would not substitute — `Chair_foot1` lost 0.14 % of its faces
while losing its end caps.

---

## Logging and crash safety

Three files in the output root, rotated `.1`–`.5` at run start rather than
truncated, so restarting after a bad run does not destroy the log explaining it:

- `repair_log.tsv` — per-step trace, written under `flock` by all workers
- `repair_summary.tsv` — one row per file, machine-readable
- `review_decimated.tsv` — files decimated `REVIEW_RATIO` (2×) or more

`log_summary_start()` writes a provisional `started` row before any work begins.
A worker killed mid-file never reaches the code that writes the real row, so
without it the one file that killed the run is the single file missing from the
summary. Its row is built from `_SUMMARY_COLUMNS` so adding a column cannot
silently misalign it.

`_post_verify()` returns `(nm, open, verified)`. The third value exists because
the first two cannot express *unknown*: a scan that could not run — ASCII input,
a scan error, a mesh too large — used to return a bare `(0, 0)`, which callers
read as verified-clean and wrote out as `ok` having checked nothing. Results
carrying `verified=False` display as `OK UNVERIFIED`.

---

## Tests

`test_pipeline.py` — 34 end-to-end tests, ~0.7 s:

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
- **Debris shells** under `max(100, largest // 1000)` faces are silently dropped
  from split output.
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
