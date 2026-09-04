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

```
scan errors
  ↓
split multi-shell → <name>.part.N.stl  (each part continues below)
  ↓
decimate if > MAX_FACES  (PyMeshLab Quadric Edge Collapse)
  ↓
repair non-manifold edges/vertices  (PyMeshLab)
  ↓
fill open edges  (PyMeshFix)
  ↓
Blender fallback  (only if NM or open edges remain after Python passes)
```

Split is done first so that PyMeshFix and PyMeshLab work per-shell — combining shells before repair causes PyMeshFix to merge them into a single watertight body, losing the separate-part boundary.

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
| `WORKERS` | `3` | `--workers` | — | Parallel worker processes |
| `TIMEOUT` | `1200` s | `--timeout` | — | Per-file timeout (kills hung Blender) |
| `MAX_FACES` | `900 000` | `--max-faces` | — | Decimate threshold (0 = disabled) |
| `RECURSIVE` | `True` | `--recursive` / `--no-recursive` | — | Walk subdirectories |

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

---

## Known limitations

- **Irreducible tiny gaps** (< 0.01 mm boundary loops): reported as residual open edges. Slicers ignore gaps this small.
- **Heavy decimation NM**: very complex meshes can exit Quadric Edge Collapse with new NM edges; the Blender fallback handles these.
- **Debris shells**: shells with fewer than `max(100, largest_shell // 1000)` faces are discarded after splitting (treated as printing artifacts).

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
