# `libs` module reference

Public interfaces omit some optional arguments. Read the code before changing a contract.

## Data and tools

### `mesh_io`

- **For:** classify, probe, load, and write meshes; preserve source/destination identity.
- **Interface:** `kind`, `triangle_count`, `probe`, `load`, `write`, bounds helpers; `Mesh`, `Geometry`; `write_ply`/`read_ply` for Blender.
- **Implementation:** a `Mesh` holds metadata and optional welded array geometry. Operations return new values. Deliverables are binary STL; Blender exchange is binary PLY.
- **Tried/rejected:** path-only values caused repeated reads. STL exchange destroyed the vertex table and re-welded by an absolute distance, measurably deleting small detail; PLY avoids that reconstruction.

### `scanner`

- **For:** measure topology and geometry without repairing it.
- **Interface:** `scan`, `open_loops`, `open_loops_are_printable`, `winding_seams`, `volume`, `shells`, `shell_count`, `diagonal`.
- **Implementation:** derives indexed edges from loaded arrays; shells are edge-connected and use SciPy connectivity.
- **Tried/rejected:** pure NumPy shells were too slow. Edge counts alone were rejected as a success oracle: closed, zero-area, non-finite, or incomplete models can score clean.

### `meshfix`

- **For:** one PyMeshFix invocation on one loaded part.
- **Interface:** `repair(mesh, fill_holes=True) -> Result`, `is_available`.
- **Implementation:** captures native output and returns geometry plus tool status; tool success is not a printability verdict.
- **Tried/rejected:** multiple disconnected shells in one call can lose surfaces. Process-wide output capture is unsafe across concurrent calls and needs isolation or locking.

### `blender`

- **For:** launch headless Blender for conversion or repair with a timeout.
- **Interface:** `convert`, `repair`, `is_available`, `Runner`, `Result`.
- **Implementation:** repair uses `blender_fx/repair.blender` through PLY; it removes bad non-manifold faces and fins, then fills holes.
- **Tried/rejected:** Blender decimation added no demonstrated coverage. STL exchange plus `remove_doubles(0.01)` lost small features. The old normal vote had no independent normals, and Blender T-junction repair damaged tested geometry.

## Geometry operations

### `decimator`

- **For:** reduce one loaded mesh to a face budget.
- **Interface:** `decimate(mesh, max_faces) -> Result`, `available_rungs`, `is_available`.
- **Implementation:** tries `fast_simplification`, then PyMeshLab; reports the rung and measurements.
- **Tried/rejected:** split-first gives every part a budget and can exceed the final limit. Blender as a third rung was unnecessary. Decimation can erase or fuse geometry, so compare it with the original.

### `welder`

- **For:** repair a T-junction by splitting the face whose edge skips vertices on a neighboring open-edge path.
- **Interface:** `find(mesh)`, `repair(mesh) -> Result`.
- **Implementation:** follows a topological boundary path and adjacent face strip; adds faces without moving/deleting vertices.
- **Tried/rejected:** fixed and relative distance tests missed real bent junctions or failed with scale. PyMeshLab `remove_t_vertices` destroyed a measured mesh; PyMeshFix and Blender dented the fixture. Reversed path chains remain unresolved.

### `splitter`

- **For:** create independent repair parts and concatenate accepted results.
- **Interface:** `by_shells`, `by_seams`, `merge`.
- **Implementation:** shell parts are edge-connected and get independent identities; merge concatenates arrays, not a boolean union. Seam splitting requires closed winding loops and an explicit call.
- **Tried/rejected:** vertex-connected splitting joins solids touching at one point. Unsplit PyMeshFix loses shells. Seam count did not predict damage. The current `<100 faces` debris rule can delete meaningful parts.

### `repairer`

- **For:** own the ordered repair sequence and measurements without writing.
- **Interface:** `repair(mesh, min_shell_faces, tool) -> Result`, `blender_part`, `is_available`.
- **Implementation:** weld → CLEAN → split; each retained part is oriented and currently sent to PyMeshFix; parts merge afterward. Blender through PLY exists, but defect routing is unfinished. `Result.ok` means the sequence ran, not that it is acceptable.
- **Tried/rejected:** CLEAN after split cannot see coincident copies. Whole-mesh or guarded orientation missed local inversions and erased seam evidence; unconditional per-part orientation worked. Blender cannot be a wholesale replacement for PyMeshFix because they repair different defects.

### `processor`

- **For:** process, judge, and write one mesh outcome.
- **Interface:** `process(mesh, max_faces, tool) -> Outcome`, `write(outcome, source_path, output_file)`.
- **Implementation:** decimates, repairs, scans, then chooses an `Indicator`; loss must be checked before topology because a partial model can be closed.
- **Tried/rejected:** judging only post-decimation input misses decimation loss. Signed volume can cancel between opposite shells. Finite geometry, component retention, and final-budget gates are missing.

## Files and execution

### `indicators`

- **For:** classify outputs, markers, conversions, and ignored files.
- **Interface:** `check`, `export_path`; `Indicator`, `Finding`.
- **Implementation:** filenames encode durable outcomes and restart behavior.
- **Tried/rejected:** direct final-path writes allow partial files to look complete; outputs and markers need atomic replacement.

### `converter`

- **For:** walk sources, copy companions, convert OBJ/ASCII STL, and emit probed binary-STL jobs.
- **Interface:** `prepare(source_root, output_root, emit, ...) -> Summary`.
- **Implementation:** preserves relative paths and caches conversions under `stl-exported/`.
- **Tried/rejected:** inline copy errors can abort the walk, worker conversion exceptions can disappear, and same-stem OBJ/STL inputs collide.

### `pool`

- **For:** reusable thread workers that pull caller-selected work.
- **Interface:** `Pool(...).start()`; see code for callbacks.
- **Implementation:** selection is serialized under a lock; handlers run concurrently and report completion.
- **Tried/rejected:** parent-push/process-pool scheduling made resource admission harder. Python watchdogs cannot safely interrupt native PyMeshFix; final execution needs disposable per-file children. Selector exceptions currently disappear.
