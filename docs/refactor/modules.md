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

- **For:** own the PyMeshLab dependency boundary for in-memory mesh operations, plus the uniform pipeline steps built from a single PyMeshLab filter — the four CLEAN filters and orient.
- **Interface:** `is_available`, `to_mesh`, `from_mesh`, `apply_filters`; `step_clean_null_faces`, `step_clean_merge_close`, `step_clean_duplicate_faces`, `step_clean_unreferenced`, `step_orient`, each `(mesh) -> (ok, mesh, detail)`.
- **Implementation:** converts project geometry (`float32`/`int64`) to PyMeshLab's `float64`/`int32` arrays, wraps float filter parameters as `PercentageValue`, and converts results back without a filesystem round trip. Each `step_*` function checks its own `pipeconfig.ENABLE_X` flag and catches what `apply_filters` raises — naming a specific filter and its parameters is PyMeshLab-specific mechanics, so it lives here; these filters are now unwired from the default repair sequence; explicit callers choose their order, which is `repairer`'s policy, not this module's.
- **Tried/rejected:** the `step_*` functions previously lived in `repairer`, mixing tool mechanics with repair policy — moved here (owner decision, 2026-09-21) so a module's own filter names/parameters stay in that module, and `repairer` reads as an ordered list of steps rather than containing PyMeshLab-specific calls inline.

### `pipeconfig`

- **For:** the single place every pipeline step's on/off switch lives.
- **Interface:** `ENABLE_ALPHA_WRAP`, `ENABLE_WELD`, `ENABLE_CLEAN_NULL_FACES`, `ENABLE_CLEAN_MERGE_CLOSE`, `ENABLE_CLEAN_DUPLICATE_FACES`, `ENABLE_CLEAN_UNREFERENCED`, `ENABLE_SPLIT_SHELLS`, `ENABLE_SPLIT_SEAMS`, `ENABLE_ORIENT`, `ENABLE_PART_TOOL`, `ENABLE_FILL_BOUNDARIES`, `ENABLE_CLEAN`.
- **Implementation:** module-level mutable booleans, all default `True` except `ENABLE_SPLIT_SEAMS`. `meshfix` and `repairer` read them as `pipeconfig.ENABLE_X` at the point of use rather than importing the names, so a switch flipped between runs in one session takes effect; they describe an experiment on the whole run, not a property of one mesh, so they are set before calling `repair`, not toggled per part.
- **Tried/rejected:** previously duplicated as separate module-level flags in `meshfix` and `repairer`, which put the same kind of switch in two places and let one of `repairer`'s own comments (`ENABLE_SPLIT_SEAMS`) go stale about whether the step it gates was ever called at all.

### `meshfix`

- **For:** one PyMeshFix invocation on one loaded part.
- **Interface:** `repair(mesh, fill_holes=True) -> Result`, `step_meshfix_repair(mesh) -> (ok, mesh, detail)`, `is_available`.
- **Implementation:** captures native output and returns geometry plus tool status; tool success is not a printability verdict. `step_meshfix_repair` remains an explicit-use uniform entry point, unwired from the default: checks `pipeconfig.ENABLE_PART_TOOL`, wraps `repair`'s richer `Result` into the `(ok, mesh, detail)` shape every orchestration step shares, and catches what `repair` raises (e.g. unloaded geometry) so it reaches the uniform contract instead of escaping.
- **Tried/rejected:** multiple disconnected shells in one call can lose surfaces. **Process-wide output capture is unsafe across concurrent calls and needs isolation or locking** — `_Capture` redirects file descriptors 1 and 2 for the whole process while open, so two worker threads calling `repair`/`step_meshfix_repair` at the same time can each read the other's captured output. This predates and is unrelated to the `step_meshfix_repair` wrapper added alongside Blender's own per-part step; it remains open, tracked in [open issues](open-issues.md).

### `blender`

- **For:** launch headless Blender for conversion or repair with a timeout.
- **Interface:** `convert`, `repair`, `step_blender_repair(mesh) -> (ok, mesh, detail)`, `is_available`, `Runner`, `Result`.
- **Implementation:** repair uses `blender_fx/repair.blender` through PLY; it removes bad non-manifold faces and fins, then fills holes. `step_blender_repair` is an explicit-use uniform entry point (`pipeconfig.ENABLE_BLENDER_PART`, default on), now unwired from the default sequence: writes a temp PLY, calls `repair`, reads the result back via `mesh_io.read_ply` to preserve the part's identity, and converts a launch exception, timeout, or accepted-exit-code-with-missing-output into `ok=False` rather than raising. The script's own exit 2 (`BLENDER_UNREPAIRED`, non-manifold edges remain) now writes its output before exiting, so an incomplete Blender repair is still handed to PyMeshFix instead of failing the part outright.
- **Tried/rejected:** Blender decimation added no demonstrated coverage. STL exchange plus `remove_doubles(0.01)` lost small features. The old normal vote had no independent normals, and Blender T-junction repair damaged tested geometry. `repairer.blender_part` — a thin wrapper that only called `step_blender_repair` — was the only way to reach Blender in `repairer.repair` for one whole session — a wholesale `tool=` replacement for PyMeshFix, never wired into the default sequence — until this decision put `step_blender_repair` there directly. The wrapper was then removed as redundant: `step_blender_repair` already has the exact signature `tool=` requires, so `repair(mesh, tool=blender.step_blender_repair)` needs nothing `blender_part` added.

### `alphawrap`

- **For:** reconstruct a watertight, manifold solid with CGAL Alpha Wrapping, a required dependency installed and import-checked by `install.sh`.
- **Interface:** `wrap(mesh, alpha, offset) -> Mesh`, `step_alpha_wrap(mesh, *, whole_diagonal=None) -> (ok, mesh, detail)`, `is_available()`.
- **Implementation:** the default repair step checks `ENABLE_ALPHA_WRAP` first, then requires a finite positive whole-mesh diagonal. It computes `alpha=diag/800`, `offset=diag/2000` in float64 arithmetic and catches wrapping failures into the uniform step contract. Missing diagonal never falls back to a part's bounds. The recipe was measured and visually confirmed on three real models; see [discovered-bugs.md](discovered-bugs.md#settled-diagonal-ratio-recipe-alphadiag800-offsetdiag2000). `wrap` remains the explicit-parameter low-level API and rejects invalid parameters and empty CGAL output. Reconstruction does not guarantee fidelity or triangle count.


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

- **For:** sequence operations, split/merge parts, and record repair measurements.
- **Interface:** `repair(mesh, min_shell_faces, tool=None) -> Result`, `WHOLE_MESH_STEPS`, `PART_MESH_STEPS`, `_repair_part(part, steps=None)`.
- **Implementation:** authoritative `WHOLE_MESH_STEPS = ()` and `PART_MESH_STEPS = (('step_alpha_wrap', alphawrap.step_alpha_wrap),)`. For the default path, `repair` computes the input whole mesh's diagonal once before splitting, and creates a local tuple using `functools.partial` to bind only the alpha-wrap function. Each part runs that bound tuple. No context is shared between calls. Explicit non-None tools bypass diagonal calculation and binding. `_repair_part` reads the module tuple at call time when no steps are passed. Tests replacing functions patch the tuple itself, since it stores function objects at import time.
- **Evidence:** one `Step.PART` entry per part, with named step detail; split and merge are recorded separately. Failed steps stop the run. Closing measurements and finite-coordinate validation remain guarded. `Result.ok` describes execution, not printability. NumPy/SciPy splitting, merging, and scanning need no PyMeshLab availability gate.
- **History:** weld, four CLEAN filters, orientation, Blender, and PyMeshFix were the previous default. Their modules and explicit tool interfaces remain intact; they are now unwired.


### `processor`

- **For:** process, judge, and write one mesh outcome.
- **Interface:** `process(mesh, max_faces, tool) -> Outcome`, `write(outcome, source_path, output_file)`, `_decide(..., final_decimation=None)`.
- **Implementation:** decimate, repair, apply every existing rejection check, then decimate again with the same budget. `Outcome.decimation`, `.repair`, and `.final_decimation` preserve independent evidence; repair measurements are never rewritten to describe decimation. Final excess faces or failed decimation produce `UNDECIMATED` with a source marker. Final geometry must have finite coordinates, measurable enclosed volume, acceptable retention against `repaired.volume_in`, and no non-manifold/open edges. Success counts and both successful and repaired-marker writes use final geometry.
- **Limitations:** original-to-first-decimation loss and meaningful-component retention remain open. Post-repair decimation was formerly rejected policy; the owner adopted it on 2026-09-22 with final geometry validation and a hard final face-budget check. No new budget check is added to the first pass.


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
