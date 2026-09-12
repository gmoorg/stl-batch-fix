# STL Batch Fix

Non-destructive batch repair of STL files for 3D printing.  
Every output file targets zero mesh errors in Bambu Studio (and compatible slicers): no naked edges, no non-manifold edges, no inverted normals.

Geometry is preserved as closely as possible — flat surfaces stay flat, detail is not smoothed away.

---

## What it fixes

| Error | How |
|---|---|
| Non-manifold edges (3+ faces on one edge) | PyMeshLab NM repair, then Blender fallback |
| Open / naked edges (holes) | PyMeshFix watertight fill, then Blender fallback |
| Multi-shell files (separate bodies in one STL) | Auto-split into `<name>.part.N.stl` files, each repaired independently |
| Oversized meshes (> `MAX_FACES` triangles) | PyMeshLab Quadric Edge Collapse decimation |
| Duplicate / degenerate faces | Pre-processing via PyMeshLab before any repair |
| Inverted normals | Blender per-shell normal vote |

---

## Pipeline (per file)

```text
scan errors  (nm + open edge counts)
  ↓
split multi-shell → <name>.part.N.stl  (each part continues below)
  ↓   …but deferred past decimation whenever decimation will run
decimate if > MAX_FACES  (fast_simplification, PyMeshLab, or Blender)
  ↓
split multi-shell, retried now that the mesh is small enough
  ↓
PyMeshFix  (repairs non-manifold AND open edges in one pass)
  ↓
volume check — did it repair the mesh, or delete part of it?
  ↓   if volume was lost: split at the winding seams and retry per region,
  ↓   running Blender first when there is no closed loop to cut on
print-scale gate — open boundaries under MIN_LAYER are accepted as-is
  ↓
Blender fallback  (only if nm edges, or over-MIN_LAYER open edges, remain)
```

**Split ordering is a trade-off, not a rule.** Splitting first lets PyMeshFix
work per-shell, which matters because `MAX_FACES` is a *per-file* budget — a
240-face speck would otherwise survive untouched while a 1.3 M-face body
absorbed the whole reduction. But the shell scan needs the mesh in memory, so
above `_LARGE_MESH_TRI_LIMIT` (2 M triangles) the split is deferred until after
decimation.

That deferral has a cost. Decimating before the split can fuse surfaces that
were separate components, and PyMeshFix then has to reconstruct one watertight
boundary from both and discards one. A 2.06 M-triangle figure — 3 % over the
limit — lost 15 % of its volume and its head that way. The volume check above
is what catches it.

There is no step D. PyMeshLab's non-manifold repair used to run between
decimation and PyMeshFix; it turned 3 non-manifold edges into 12 open edges and
deleted 151 144 faces on one model. PyMeshFix repairs non-manifold edges
directly, so the step was creating the damage it then had to repair.

---

## Output layout

Source and output folders are siblings:

```
/path/to/STL/
  Fixing/          ← INPUT_FOLDER  (source files, never modified)
  Fixed/           ← output        (repaired copies, same relative structure)
    repair_log.tsv
```

Split parts go directly to the output folder — the source file is never modified:

```
Fixing/
  Arms.stl            ← never touched

Fixed/
  ~parts/
    Arms.part.0.stl   ← repaired shell 0
    Arms.part.1.stl   ← repaired shell 1
  Arms.stl            ← merged result (if all parts succeeded)
  Arms.failed.stl     ← copy of source (if any part failed)
```

---

## Dependencies

| Library | Role | Required |
|---|---|---|
| **pymeshlab** | Decimation, NM repair, degenerate face removal, shell splitting | Strongly recommended |
| **pymeshfix** | Open-edge fill (fast, robust watertight repair) | Recommended |
| **numpy** | Required by pymeshfix | Recommended |
| **Blender 4.x** | Fallback repair for meshes that Python passes cannot fully fix | Optional but useful |

Run `bash install.sh` to check and install Python libraries automatically.

---

## Quick start

```bash
# 1. Install Python dependencies
bash install.sh

# 2. Run on default folder (edit INPUT_FOLDER in stl_batch_fix.py or pass via env)
INPUT_FOLDER=/path/to/STL/Fixing  bash run.sh

# 3. Or pass CLI flags
bash run.sh --input /path/to/STL/Fixing --workers 4 --max-faces 500000
```

---

## Configuration

All settings have defaults in `stl_batch_fix.py` and can be overridden via `--flag` or environment variable.

| Setting | Default | CLI flag | Env var | Meaning |
|---|---|---|---|---|
| `INPUT_FOLDER` | `/mnt/sda2/STL/Fixing/` | `--input` | `INPUT_FOLDER` | Source folder |
| `OUTPUT_SUFFIX` | `""` | `--suffix` | — | Appended to output filename stem |
| `MERGE_DIST` | `0.01` mm | `--merge-dist` | — | Vertex merge radius |
| `MIN_LAYER` | `0.6` mm | `--min-layer` | — | Finest layer you print at; open boundaries smaller than this are accepted rather than repaired (0 = require zero open edges) |
| `WORKERS` | `0` (auto) | `--workers` | — | Parallel worker processes; 0 derives from cores and memory |
| `TIMEOUT_PART` | `600` s | `--timeout-part` | — | Budget for **one mesh** — a whole unsplit model, or a single shell part. Almost every file is judged by this |
| `TIMEOUT` | `3600` s | `--timeout` | — | Ceiling for **split files only**; a split file's cap is `min(TIMEOUT, TIMEOUT_PART × n_parts)` |
| `BLENDER_RESERVE_PCT` | `30` % | `--blender-reserve-pct` | — | Percent of `TIMEOUT_PART` held back from Blender for the steps after it |
| `MAX_FACES` | `900 000` | `--max-faces` | — | Decimate threshold (0 = disabled) |
| `RECURSIVE` | `True` | `--recursive` / `--no-recursive` | — | Walk subdirectories |

### Timeouts

Two independent caps, not a shared pool. A part that finishes in 1 s donates
nothing to the next one — each part gets `TIMEOUT_PART`, and the file as a
whole gets `min(TIMEOUT, TIMEOUT_PART × n_parts)`, whichever binds first.

Within one mesh, Blender is given `TIMEOUT_PART − elapsed − reserve` rather
than a fresh budget, and is skipped entirely when under 30 s remain. A flat
budget did not cap: a mesh that had spent 450 s of 600 s handed Blender another
600 s and reached 1050 s. Worse, an over-running Blender is SIGKILLed with the
whole process tree instead of timing out cleanly, so the pipeline never gets
the `TIMEOUT after N s` it knows how to handle.

The split loops check the clock **between** parts. A part already blocked
inside a library call is not interrupted there, and does not need to be — a
part that overruns dooms the file anyway, and the worker's kill of the child
process is the backstop.

---

## State files

The script never silently overwrites or deletes source files.

| File | Meaning | Retry |
|---|---|---|
| `<stem>.broken.stl` | Permanently unreadable (corrupt or 0 faces) | Never |
| `<stem>.failed.stl` | Transient error (crash / timeout) or split partially failed | Delete to retry |
| `<stem>.unrepaired.stl` | Script ran but NM errors remain | Delete to retry |
| `<stem>.open.stl` | Repaired but open edges remain (slicer handles these) | Delete to retry |

---

## Parallelism

`ProcessPoolExecutor` runs `WORKERS` Python processes in parallel.  
Each process handles one file end-to-end: split detection, per-part repair, and merge all happen inside the same worker. No dynamic task submission — the total file count is fixed at startup.

### Killing Blender

A worker's Blender subprocess is found by reading the kernel's own child list
(`/proc/<pid>/task/<tid>/children`) rather than by tracking a reported PID.
That is deliberate: any reporting scheme has a window between `Popen()`
returning and the PID being recorded, and a kill arriving in that window finds
nothing. `/proc` has no such window. `_blender_proc` is still tracked for a
fast direct `kill()`, but every handler sweeps `/proc` afterwards anyway.

**This assumes Blender is a *direct* child of the worker.** `_child_pids_of()`
walks one level only. Nothing currently spawns a process that spawns Blender —
parts are repaired inside the worker rather than in grandchild processes — but
if that ever changes, the sweep will miss it and Blender will reparent to init
and keep its memory until it finishes on its own.

### Ctrl+C

Depends on how the run was started:

| launched via | on Ctrl+C |
|---|---|
| `stl_batch_fix_tui.py` | Workers get SIGTERM, each kills its Blender child and sweeps `/proc`; after 5 s anything still alive gets SIGKILL. A second Ctrl+C exits immediately. |
| `stl_batch_fix.py` directly | **Blender is not killed.** No SIGINT handler is installed, so workers die of unhandled `KeyboardInterrupt` without running the SIGTERM path, and their Blender children reparent to init and keep running. |

Use the TUI for interactive runs. After interrupting a bare-script run, check
for orphans with `pgrep -a blender` and kill them by hand.

---

## Known limitations

- **Sub-layer boundary loops**: open boundaries smaller than `MIN_LAYER` (the finest layer you print at) are accepted as-is rather than repaired — they produce no toolpath, so closing them changes nothing that reaches the plate. Chasing them is not free: on one model a 0.04 mm pinhole sent the mesh to Blender, which punched 28 new sub-0.31 mm holes, which sent it back to PyMeshFix, which truncated the model at the waist and cost 55% of its volume.
- **Heavy decimation NM**: very complex meshes can exit Quadric Edge Collapse with new NM edges; the Blender fallback handles these.
- **Debris shells**: shells with fewer than `_MIN_SHELL_FACES` (100) faces are discarded after splitting (treated as printing artifacts). The floor is flat rather than proportional — a proportional one scaled with model size and threw away a 384-face chair-foot cap on a 681k-face mesh.
- **Nested surfaces in one shell**: a model whose head is an outer surface with a second surface nested inside it is a single connected component, so shell splitting cannot separate them. PyMeshFix must reconstruct one watertight boundary from both and discards one. Detected by the enclosed-volume check, recovered by splitting at the winding seams after Blender makes the boundary explicit.

---

## Files in this folder

| File | Purpose |
|---|---|
| `stl_batch_fix.py` | Main script (worker logic, repair pipeline) |
| `stl_batch_fix_tui.py` | Terminal UI — config screen + live progress display |
| `stl_batch_fix.blender` | Blender repair script template (loaded at startup; not a runnable script) |
| `stl_batch_fix.decimate.blender` | Blender decimation script template — used for meshes too large for PyMeshLab |
| `run.sh` | Launcher — calls the TUI; pass `--help` for direct script flags |
| `install.sh` | Dependency checker / installer |
| `default.fixcfg` | Settings file (created on first run if absent) |
| `README.md` | This file |
