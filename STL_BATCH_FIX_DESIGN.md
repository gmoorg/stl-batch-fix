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

---

## External dependencies

| Library | Role | Required |
| --- | --- | --- |
| **pymeshlab** | Shell splitting, decimation, NM repair, degenerate face removal | Strongly recommended |
| **pymeshfix** | Open-edge fill (fast watertight repair) | Recommended |
| **numpy** | Required by pymeshfix | Recommended |
| **Blender 4.x** | Fallback repair when Python passes cannot fully fix a mesh | Optional but useful |

---

## Configuration

| Constant        | Default                    | CLI override                     | Env var        | Meaning                             |
|-----------------|----------------------------|----------------------------------|----------------|-------------------------------------|
| `INPUT_FOLDER`  | `/mnt/sda2/STL/Fixing/`    | `--input`                        | `INPUT_FOLDER` | Source folder                       |
| `OUTPUT_SUFFIX` | `""` (empty)               | `--suffix`                       | —              | Appended to output filename stem    |
| `MERGE_DIST`    | `0.01`                     | `--merge-dist`                   | —              | Starting vertex merge radius (mm)   |
| `BLENDER`       | `blender`                  | —                                | —              | Blender executable name / path      |
| `RECURSIVE`     | `True`                     | `--recursive` / `--no-recursive` | —              | Walk subdirectories                 |
| `WORKERS`       | `3`                        | `--workers`                      | —              | Parallel worker processes           |
| `TIMEOUT`       | `1200`                     | `--timeout`                      | —              | Seconds before killing Blender      |
| `MAX_FACES`     | `900 000`                  | `--max-faces`                    | —              | Decimate if face count exceeds this |

`INPUT_FOLDER` is also read from the `INPUT_FOLDER` environment variable before
CLI parsing. CLI `--input` takes precedence over env.

---

## File classification

Files are collected by walking `INPUT_FOLDER`. A file is skipped from collection
if its stem (lowercase) already ends with `OUTPUT_SUFFIX`, preventing re-processing
of already-fixed files.

`.part.N.stl` split parts **are** collected — they are processed normally. The
`_is_part` flag inside `process_file` prevents re-splitting parts.

Per-file checks (in order) before launching the pipeline:

1. `.broken` marker next to **source** file → SKIP permanently (bad mesh data)
2. `.failed` marker next to **destination** file → SKIP until manually deleted
3. Destination file already exists → SKIP silently (no log line)
4. Binary size check: `file_size < 80 + 4 + n_tris × 50` → mark `.broken`, report CORRUPT
5. ASCII STL → skip binary check; imported normally

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
scan errors  (face count, NM edge count, open edge count)
  ↓
Step B — split multi-shell  (PyMeshLab connected components)
          original renamed to <name>.stl.splitted
          parts written as <name>.part.N.stl
          each part re-enters the pipeline from the top
  ↓
Step C — decimate if > MAX_FACES  (PyMeshLab Quadric Edge Collapse)
  ↓
Step D — repair NM edges/vertices  (PyMeshLab meshing_repair_non_manifold_*)
  ↓
Step E — fill open edges  (PyMeshFix)
  ↓
Step F — Blender fallback  (only if NM or open edges remain after D + E)
```

**Pipeline order rationale:**

- Split before repair: PyMeshFix on a combined multi-shell mesh merges the shells
  into one watertight body, destroying the separate-part boundary.
- Decimate after split: Quadric Edge Collapse operates per-shell, giving it the
  correct face budget for each part independently.
- PyMeshLab NM repair before PyMeshFix: NM repair opens edges (deletes faces);
  PyMeshFix then closes those openings cleanly.
- Blender is last resort only — avoids the overhead of launching a subprocess
  for files that Python passes handle completely.

---

## Shell splitting (`split_shells`)

Uses `generate_splitting_by_connected_components` from PyMeshLab.

**Key implementation detail**: after calling this filter, PyMeshLab adds the
component meshes to the MeshSet AFTER the original mesh. The original is still
present at its original index. To avoid duplicating it in the output:

```python
count_before = len(ms)
ms.generate_splitting_by_connected_components()
count_after = len(ms)
# Only iterate range(count_before, count_after) — skip the original
```

**Debris filter**: shells with fewer than `max(100, largest_shell // 1000)`
faces are discarded (treated as printing artifacts / zero-thickness surfaces).

**File naming**:

- `Arms.stl` → renamed to `Arms.stl.splitted` (original geometry preserved)
- Parts written as `Arms.part.0.stl`, `Arms.part.1.stl`, …

**Anti-recursion guard (`_is_part`)**:  
Checks whether `<name>.stl.splitted` exists next to the file. If it does, the
file is a split part and will not be re-split. This is safer than a regex test
on the filename because it survives user files already named `.part.N.stl`.

---

## Parallelism

`ProcessPoolExecutor` spawns `WORKERS` OS processes.  
The main loop uses `concurrent.futures.wait(FIRST_COMPLETED)` with a mutable
`pending` set — not `as_completed()` — so that dynamically submitted split-part
futures are properly processed:

```python
pending = set(future_to_src)
while pending:
    done, pending = concurrent.futures.wait(
        pending, return_when=concurrent.futures.FIRST_COMPLETED)
    for future in done:
        ...
        if result['status'] == 'split':
            for part in parts:
                total += 1
                new_future = pool.submit(process_file, part)
                future_to_src[new_future] = (total, part)
                pending.add(new_future)
```

`as_completed()` snapshots the dict at call time and misses futures added later.

---

## Logging

Log file: `LOG_FILE` (default `/mnt/sda2/STL/Fixed/repair_log.tsv`).

| Outcome    | Log line                                                        |
|------------|-----------------------------------------------------------------|
| OK         | `[n/N] rel/path ... OK -> filename.stl (X bytes)`               |
| SPLIT      | `[n/N] rel/path ... SPLIT -> K shells`                          |
| CLEAN COPY | `[n/N] rel/path ... CLEAN COPY -> filename.stl`                 |
| SKIP       | Silent if already fixed; printed with reason otherwise          |
| UNREPAIRED | `[n/N] rel/path ... UNREPAIRED [mesh defects remain]`           |
| FAILED     | `[n/N] rel/path ... FAILED [broken / transient]`                |
| CORRUPT    | `[n/N] rel/path ... CORRUPT - reason`                           |

UNREPAIRED = pipeline ran to completion but NM or open edges remain.  
No output STL is written for UNREPAIRED. The `.unrepaired` marker prevents
repeated retries until deleted.

---

## State files

All state files contain the original STL geometry — no empty placeholders.

| File                    | Location            | Meaning                                              | Retry           |
|-------------------------|---------------------|------------------------------------------------------|-----------------|
| `<stem>.broken.stl`     | Next to destination | Permanently bad mesh (corrupt or 0 faces)            | Never           |
| `<stem>.failed.stl`     | Next to destination | Transient error (crash or timeout)                   | Delete to retry |
| `<stem>.unrepaired.stl` | Next to destination | Repair ran but holes/NM edges remain                 | Delete to retry |
| `<name>.stl.splitted`   | Next to source      | Original file after shell split                      | —               |

---

## Known limitations

- **Sub-mm irreducible gaps**: tiny boundary loops (< 0.01 mm) that can't be
  safely merged or filled. Reported as residual open edges. Slicers ignore them.
- **Heavy decimation NM**: very complex meshes can exit Quadric Edge Collapse
  with new NM edges; the Blender fallback handles these.
- **Debris shells**: shells smaller than `max(100, largest // 1000)` faces are
  silently discarded from split output.

---

## What NOT to do (lessons learned)

- **Do not** use `as_completed()` for the main pool loop — it snapshots the
  future dict at call time; dynamically added split-part futures are never seen.
  Use `wait(FIRST_COMPLETED)` with a mutable `pending` set instead.
- **Do not** exclude `.part.N.stl` from `collect_stl_files` — parts must be
  visible to the collector so they are processed on re-runs. Use the
  `_is_part` / `.stl.splitted` sibling check inside `process_file` to prevent
  re-splitting.
- **Do not** run PyMeshFix before splitting multi-shell files — PyMeshFix merges
  all shells into one watertight body, losing the separate-part boundary.
- **Do not** include the original mesh in split output — PyMeshLab keeps the
  original in the MeshSet after `generate_splitting_by_connected_components`;
  only iterate `range(count_before, count_after)` for the components.
- **Do not** use a union-find / snap-grid approach for shell detection — a snap
  grid aggressive enough to close micro-gaps also merges legitimately separate
  shells (e.g. two arms of a figure that are close but not touching).
- **Do not** use Blender's DECIMATE modifier — does not preserve topology,
  introduces NM edges. Use PyMeshLab Quadric Edge Collapse with
  `preserve_topology=True`.
- **Do not** run `select_more()` for NM face deletion in Blender — expands into
  adjacent good geometry, creating craters patched with flat triangles (visible
  faceting on curved surfaces).
- **Do not** scope `seen_junctions` across Blender repair passes — vertex
  indices are renumbered after deletions; stale indices match wrong vertices.
- **Do not** use `bpy.ops.export_mesh.stl` — introduces a spurious NM edge.
- **Do not** remove slivers on Blender repair passes > 1 — re-opens boundary
  loops causing oscillation (nm/boundary counts increase instead of decrease).
- **Do not** use a global normal vote in Blender — flips correct shells
  outnumbered by an inverted shell. Use per-component BFS.
