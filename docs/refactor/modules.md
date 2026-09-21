# `libs` module reference

Public interfaces omit some optional arguments. Read the code before changing a contract.

The modules are **independent of the legacy batch script**, not independent of
the mesh-processing domain. They expose reusable mesh and tool boundaries
without importing `stl_batch_fix.py`, depending on its globals, or assuming
its CLI, TUI, worker lifecycle, or filesystem layout. The legacy script remains
reference material until the refactor is complete.

## Data and tools

### `mesh_io`

- **For:** classify, probe, load, and write meshes; preserve source/destination identity.
- **Interface:** `kind`, `triangle_count`, `probe`, `load`, `write`, `staged_write`, bounds helpers; `Mesh`, `Geometry`; `write_ply`/`read_ply` for Blender.
- **Implementation:** a `Mesh` holds metadata and optional welded array geometry. Operations return new values. Deliverables are binary STL; Blender exchange is binary PLY. `triangle_count` rejects a zero-triangle header, so an 84-byte file is invalid at `probe` rather than an `IndexError` inside `load` (R03). Both entrances reject non-finite coordinates — `load` returns an invalid `Mesh`, `read_ply` raises — because NaN scans as `open=0, nm=0` and defeats every `<` comparison downstream (A03). `staged_write` is the one place a file gets published: it stages a short-named `.part` sibling, restores the umask mode `mkstemp` would otherwise narrow to 0600, and `os.replace`s it into position, so a name a rerun trusts appears only once its bytes are complete (A04). Every writer whose output `indicators.check` reads back goes through it — the STL, the markers, and the companion copy.
- **Tried/rejected:** path-only values caused repeated reads. STL exchange destroyed the vertex table and re-welded by an absolute distance, measurably deleting small detail; PLY avoids that reconstruction.

### `scanner`

- **For:** measure topology and geometry without repairing it.
- **Interface:** `scan`, `open_loops`, `open_loops_are_printable`, `winding_seams`, `volume`, `component_volume`, `shells`, `shell_count`, `diagonal`.
- **Implementation:** derives indexed edges from loaded arrays; shells are edge-connected and use SciPy connectivity. `volume` is signed, which is what makes an inside-out mesh visible; `component_volume` sums each shell's magnitude, so it is the one to divide when asking whether geometry survived. Faces are grouped by `shells`, which returns indices, so no submesh is built.
- **Tried/rejected:** pure NumPy shells were too slow. Edge counts alone were rejected as a success oracle: closed, zero-area, non-finite, or incomplete models can score clean. Signed volume as a *size* was rejected for multi-shell meshes: two oppositely wound shells cancelled to -0.00006, and dropping one then scored 7,049,393,791% kept (A02).

### `meshlab`

- **For:** own the PyMeshLab dependency boundary for in-memory mesh operations.
- **Interface:** `is_available`, `to_mesh`, `from_mesh`, `apply_filters`.
- **Implementation:** converts project geometry (`float32`/`int64`) to PyMeshLab's `float64`/`int32` arrays, wraps float filter parameters as `PercentageValue`, and converts results back without a filesystem round trip.
- **Tried/rejected:** keeping these methods in `repairer` mixed tool mechanics with repair policy. The filter list and order remain in `repairer`.

### `pipeconfig`

- **For:** the single place every pipeline step's on/off switch lives.
- **Interface:** `ENABLE_WELD`, `ENABLE_CLEAN_NULL_FACES`, `ENABLE_CLEAN_MERGE_CLOSE`, `ENABLE_CLEAN_DUPLICATE_FACES`, `ENABLE_CLEAN_UNREFERENCED`, `ENABLE_SPLIT_SHELLS`, `ENABLE_SPLIT_SEAMS`, `ENABLE_ORIENT`, `ENABLE_PART_TOOL`, `ENABLE_FILL_BOUNDARIES`, `ENABLE_CLEAN`.
- **Implementation:** module-level mutable booleans, all default `True` except `ENABLE_SPLIT_SEAMS`. `meshfix` and `repairer` read them as `pipeconfig.ENABLE_X` at the point of use rather than importing the names, so a switch flipped between runs in one session takes effect; they describe an experiment on the whole run, not a property of one mesh, so they are set before calling `repair`, not toggled per part.
- **Tried/rejected:** previously duplicated as separate module-level flags in `meshfix` and `repairer`, which put the same kind of switch in two places and let one of `repairer`'s own comments (`ENABLE_SPLIT_SEAMS`) go stale about whether the step it gates was ever called at all.

### `meshfix`

- **For:** one PyMeshFix invocation on one loaded part.
- **Interface:** `repair(mesh, fill_holes=True) -> Result`, `step_meshfix_repair(mesh) -> (ok, mesh, detail)`, `is_available`.
- **Implementation:** captures native output and returns geometry plus tool status; tool success is not a printability verdict. `step_meshfix_repair` is the uniform pipeline entry point `repairer` composes: checks `pipeconfig.ENABLE_PART_TOOL`, wraps `repair`'s richer `Result` into the `(ok, mesh, detail)` shape every orchestration step shares, and catches what `repair` raises (e.g. unloaded geometry) so it reaches the uniform contract instead of escaping.
- **Tried/rejected:** multiple disconnected shells in one call can lose surfaces. **Process-wide output capture is unsafe across concurrent calls and needs isolation or locking** — `_Capture` redirects file descriptors 1 and 2 for the whole process while open, so two worker threads calling `repair`/`step_meshfix_repair` at the same time can each read the other's captured output. This predates and is unrelated to the `step_meshfix_repair` wrapper added alongside Blender's own per-part step; it remains open, tracked in [open issues](open-issues.md).

### `blender`

- **For:** launch headless Blender for conversion or repair with a timeout.
- **Interface:** `convert`, `repair`, `step_blender_repair(mesh) -> (ok, mesh, detail)`, `is_available`, `Runner`, `Result`.
- **Implementation:** repair uses `blender_fx/repair.blender` through PLY; it removes bad non-manifold faces and fins, then fills holes. `step_blender_repair` is the uniform pipeline entry point `repairer` composes into the default per-part sequence (before PyMeshFix, `pipeconfig.ENABLE_BLENDER_PART`, default on): writes a temp PLY, calls `repair`, reads the result back via `mesh_io.read_ply` to preserve the part's identity, and converts a launch exception, timeout, or accepted-exit-code-with-missing-output into `ok=False` rather than raising. The script's own exit 2 (`BLENDER_UNREPAIRED`, non-manifold edges remain) now writes its output before exiting, so an incomplete Blender repair is still handed to PyMeshFix instead of failing the part outright.
- **Tried/rejected:** Blender decimation added no demonstrated coverage. STL exchange plus `remove_doubles(0.01)` lost small features. The old normal vote had no independent normals, and Blender T-junction repair damaged tested geometry. `blender_part` (now a thin call into `step_blender_repair`) was the only way to reach Blender in `repairer.repair` for one whole session — a wholesale `tool=` replacement for PyMeshFix, never wired into the default sequence — until this decision put `step_blender_repair` there directly.

## Geometry operations

### `decimator`

- **For:** reduce one loaded mesh to a face budget.
- **Interface:** `decimate(mesh, max_faces) -> Result`, `available_rungs`, `is_available`.
- **Implementation:** requires `fast_simplification`; reports its measurements or returns the unchanged mesh with `Rung.FAILED` so the caller writes an undecimated marker.
- **Tried/rejected:** PyMeshLab and Blender decimation were rejected as fallback rungs. `fast_simplification` is a required deliverable; when it fails, the caller writes an undecimated marker for manual handling. Split-first gives every part a budget and can exceed the final limit. Decimation can erase or fuse geometry, so compare it with the original.

### `welder`

- **For:** repair a T-junction by splitting the face whose edge skips vertices on a neighboring open-edge path.
- **Interface:** `find(mesh)`, `repair(mesh) -> Result`, `step_weld_close_tjunctions(mesh) -> (ok, mesh, detail)`.
- **Implementation:** follows a topological boundary path and adjacent face strip; adds faces without moving/deleting vertices. A chain whose vertices do not progress along the spanning edge is refused, because `_split` fans faces in chain order and a reordered chain builds edges the path does not have (A01). `step_weld_close_tjunctions` is the uniform pipeline entry point: checks `pipeconfig.ENABLE_WELD`, wraps `repair`'s `Result` into `(ok, mesh, detail)`, and catches what `repair` raises.
- **Tried/rejected:** fixed and relative distance tests missed real bent junctions or failed with scale; no distance bound is applied, and the owner accepts that (R04). PyMeshLab `remove_t_vertices` destroyed a measured mesh; PyMeshFix and Blender dented the fixture. Sorting a backward chain into order produced two new faces and left all four open edges — the junction is refused instead, and PyMeshFix closes those holes properly.

### `splitter`

- **For:** create independent repair parts and concatenate accepted results.
- **Interface:** `by_shells`, `by_seams`, `merge`.
- **Implementation:** shell parts are edge-connected and get independent identities; merge concatenates arrays, not a boolean union. Seam splitting requires closed winding loops and an explicit call. `by_seams` has **no face floor** — it returns every region it cuts, because separating contradictory winding and judging whether a region is worth saving are different questions. `by_shells` keeps its debris floor.
- **Tried/rejected:** vertex-connected splitting joins solids touching at one point. Unsplit PyMeshFix loses shells. Seam count did not predict damage. The current `<100 faces` debris rule can delete meaningful parts.

### `repairer`

- **For:** own the ordered repair sequence and measurements without writing.
- **Interface:** `repair(mesh, min_shell_faces, tool) -> Result`, `blender_part`, `is_available`; per-step wrappers `step_clean_null_faces`, `step_clean_merge_close`, `step_clean_duplicate_faces`, `step_clean_unreferenced`, `step_orient`, each `(mesh) -> (ok, mesh, detail)`.
- **Implementation:** weld → CLEAN (four separate uniform steps, in order, each with its own flag and its own `StepResult`) → split; each retained part is oriented, repaired in Blender, then repaired in PyMeshFix — three uniform `(mesh) -> (ok, mesh, detail)` calls composed in `_repair_part`, stopping at the first `ok=False`; parts merge afterward. CLEAN's filter policy and the orient step live here, while PyMeshLab execution belongs to `meshlab`; Blender's and PyMeshFix's own uniform steps live in their own modules (`blender.step_blender_repair`, `meshfix.step_meshfix_repair`). `Result.ok` means the sequence ran, not that it is acceptable. `is_available()` follows which steps are actually enabled — Blender and PyMeshFix are required only when their own `pipeconfig` flag is on, PyMeshLab unconditionally (it runs CLEAN and orient). The closing measurements run **inside** the guarded block and the merged mesh is checked for finite coordinates first: tools return arrays this module did not build, and `_count_lost` feeds them to cKDTree, which raises on NaN — outside the guard that raise escaped `repair` entirely (A03).
- **Tried/rejected:** CLEAN after split cannot see coincident copies. Whole-mesh or guarded orientation missed local inversions and erased seam evidence; unconditional per-part orientation worked. A failure reason as a plain detail string was indistinguishable from a success note, which is how a failed PyMeshFix call reached `ok=True` (R02) — the original fix was a distinct `PartFailed` type; it was later replaced by the uniform `(ok, mesh, detail)` 3-tuple every step now returns, once every built-in step and the injected `tool` callable shared that one contract (owner decision, 2026-09-21) — the `ok` boolean carries what `PartFailed` used to. Splitting the four CLEAN filters into separate `meshlab.apply_filters` calls costs one PyMeshLab float64↔float32 round trip each instead of one combined call; the owner chose this anyway, for one method per operation, accepting the extra rounding (measured not to change the outcome on the fixtures this project has).

### `processor`

- **For:** process, judge, and write one mesh outcome.
- **Interface:** `process(mesh, max_faces, tool) -> Outcome`, `write(outcome, source_path, output_file)`.
- **Implementation:** decimates, repairs, scans, then chooses an `Indicator`; loss must be checked before topology because a partial model can be closed. After topology passes, the approved mesh is measured: enclosing no volume is `BROKEN`, since closed edges do not make a solid (A07).
- **Tried/rejected:** judging only post-decimation input misses decimation loss (R06, accepted by the owner). Signed volume cancelled between opposite shells; `scanner.component_volume` replaced it (A02). Trusting `volume_out` instead of measuring the mesh let a collinear sheet pass as printable. Component retention is still an open acceptance concern (R05). The decimation target is applied before repair; blind re-decimation afterward can recreate repaired defects.

## Files and execution

### `indicators`

- **For:** classify outputs, markers, conversions, and ignored files.
- **Interface:** `check`, `export_path`; `Indicator`, `Finding`.
- **Implementation:** filenames encode durable outcomes and restart behavior.
- **Tried/rejected:** direct final-path writes allow partial files to look complete; outputs and markers need atomic replacement.

### `converter`

- **For:** walk sources, copy companions, convert OBJ/ASCII STL, and emit probed binary-STL jobs.
- **Interface:** `prepare(source_root, output_root, emit, ...) -> Summary`.
- **Implementation:** preserves relative paths and caches conversions under `stl-exported/`. A converter that raises is emitted and counted exactly like one returning `False`, caught beside the call rather than through the pool's `error` argument, which reaches only the selector (R01). A companion copy that fails counts one `copy_failed` and the walk continues (A06).
- **Tried/rejected:** relying on the pool's `error` argument to notice a failed conversion did not work: the selector is told that an item finished, not which result it produced. Letting a copy error propagate cost every source after it, because the walk is a single pass. Same-stem OBJ/STL inputs still collide (A05), accepted by the owner for this deployment.

### `pool`

- **For:** reusable thread workers that pull caller-selected work.
- **Interface:** `Pool(...).start()`; see code for callbacks.
- **Implementation:** selection is serialized under a lock; handlers run concurrently and report completion. A handler exception is reported to the selector through `error`; a *selector* exception stops the pool and is re-raised from `start()` after the join, because the selector is itself the channel a handler failure would be reported through (R09).
- **Tried/rejected:** parent-push/process-pool scheduling made resource admission harder. Python watchdogs cannot safely interrupt native PyMeshFix; final execution needs disposable per-file children. Letting a selector exception kill its worker silently made an empty run indistinguishable from a complete one.
