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
- **Implementation:** converts project geometry (`float32`/`int64`) to PyMeshLab's `float64`/`int32` arrays, wraps float filter parameters as `PercentageValue`, and converts results back without a filesystem round trip. Each `step_*` function checks its own `pipeconfig.ENABLE_X` flag and catches what `apply_filters` raises — naming a specific filter and its parameters is PyMeshLab-specific mechanics, so it lives here; `repairer.WHOLE_MESH_STEPS`/`_repair_part` name the *order* these run in, which is `repairer`'s policy, not this module's.
- **Tried/rejected:** the `step_*` functions previously lived in `repairer`, mixing tool mechanics with repair policy — moved here (owner decision, 2026-09-21) so a module's own filter names/parameters stay in that module, and `repairer` reads as an ordered list of steps rather than containing PyMeshLab-specific calls inline.

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
- **Tried/rejected:** Blender decimation added no demonstrated coverage. STL exchange plus `remove_doubles(0.01)` lost small features. The old normal vote had no independent normals, and Blender T-junction repair damaged tested geometry. `repairer.blender_part` — a thin wrapper that only called `step_blender_repair` — was the only way to reach Blender in `repairer.repair` for one whole session — a wholesale `tool=` replacement for PyMeshFix, never wired into the default sequence — until this decision put `step_blender_repair` there directly. The wrapper was then removed as redundant: `step_blender_repair` already has the exact signature `tool=` requires, so `repair(mesh, tool=blender.step_blender_repair)` needs nothing `blender_part` added.

### `alphawrap`

- **For:** reconstruct a mesh as a watertight, manifold, self-intersection-free solid via CGAL's Alpha Wrapping (`pip install cgal`, not currently in `install.sh`).
- **Interface:** `wrap(mesh, alpha, offset) -> Mesh`, `is_available()`.
- **Implementation:** unlike every other tool module here, this one *reconstructs* the surface at a resolution `alpha` controls rather than editing the existing triangulation — the output shares no vertices, indices, or exact geometry with the input. What it guarantees unconditionally is topology (watertight, manifold), not fidelity, output triangle count, or printability. There is no known formula deriving `alpha`/`offset` from a mesh's own properties (checked against CGAL's own documentation) — two real models measured outside this module (`Amidara_Blustmorn_1-12_base.stl`, `mandy_part0_BEFORE.stl`; see [discovered-bugs.md](discovered-bugs.md#cgal-alpha-wrapping--spiked-not-adopted) and [mandy-volume-loss.md](mandy-volume-loss.md#cgal-alpha-wrapping-keeps-the-hair-2026-09-21)) needed bounding-box-diagonal ratios that disagreed by more than 2x from each other on both parameters, so callers must supply both explicitly. `alpha_wrap_3` itself does not raise on a non-positive `alpha`/`offset` — it silently returns an empty mesh (confirmed by direct probing) — so `wrap` validates both are finite and positive itself, and separately refuses an empty CGAL result rather than returning it as if it were a successful repair. **Not wired into `repairer`, not in `install.sh`, and has no uniform `step_*` entry point** — the one-argument `step_*(mesh)` contract every other module follows has no defensible parameter source yet (see above), so wiring it into the pipeline is deferred until a parameter policy exists.
- **Tried/rejected:** none yet — nothing has been tried and rejected inside this module itself.

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

- **For:** own operation order, `Step` classification, measurement recording, and split/merge orchestration — nothing here names a specific tool's filters or parameters, which is why `repair()` reads as an ordered list of steps rather than containing PyMeshLab/Blender/PyMeshFix calls inline.
- **Interface:** `repair(mesh, min_shell_faces, tool) -> Result`, `WHOLE_MESH_STEPS`, `PART_MESH_STEPS`.
- **Implementation:** `WHOLE_MESH_STEPS` is one tuple of `(name: str, step_fn)` pairs — weld, then the four CLEAN filters — and *is* the visible order: `repair()`'s first phase is a single loop over it, not a hand-written block per step. `Step` itself only classifies at the coarse level (`PREP` covers all five whole-mesh steps, plus `SPLIT`/`PART`/`MERGE`); which specific step produced one `StepResult` is carried in its `detail` string (`f"{name}: {detail}"`, via the shared `_named_detail` helper), not in `.step`. Each `step_fn` is a uniform `(mesh) -> (ok, mesh, detail)` function owned by whichever module knows that tool's mechanics (`welder.step_weld_close_tjunctions`, `meshlab.step_clean_*`) — `repairer` only sequences them. Then: split; each retained part is oriented, repaired in Blender, then repaired in PyMeshFix — `_repair_part` loops over `PART_MESH_STEPS` (also `(name, step_fn)` pairs: `meshlab.step_orient`, `blender.step_blender_repair`, `meshfix.step_meshfix_repair`), the same visible-tuple pattern as `WHOLE_MESH_STEPS`, though tests that substitute one of these must patch `repairer.PART_MESH_STEPS` itself rather than the origin module's attribute — the tuple captures the function objects at import time, so patching e.g. `blender.step_blender_repair` afterward does not reach an already-built tuple. `_run_step` is the one place a step gets called-and-recorded, used by the whole-mesh loop and once per part (with a `detail_prefix` for `"part {index}: "`, `name=None` since `_repair_part`'s own composite detail already names its three sub-steps) — `_repair_part` itself only accumulates detail text over `PART_MESH_STEPS`, it does not call `_record_step`, so a part still produces exactly one `Step.PART` entry, not one per sub-step. `_run_step` returns a `_StepOutcome` carrying the step's own advanced mesh even when recording it afterward raises, so a `scanner.scan` failure during recording does not lose the step's result — the caller assigns `mesh = outcome.mesh` before re-raising `outcome.record_error`, matching the pre-refactor ordering where the mesh reassignment happened on its own line before anything that could raise. SPLIT and MERGE call `_record_step` (the one shared "scan and append a `StepResult`" implementation) directly rather than through `_run_step`, since they change cardinality — one mesh becomes several, or several become one — and do not fit the "one step, one mesh in, one mesh out" shape. `Result.ok` means the sequence ran, not that it is acceptable. The closing measurements run **inside** the guarded block and the merged mesh is checked for finite coordinates first: tools return arrays this module did not build, and `_count_lost` feeds them to cKDTree, which raises on NaN — outside the guard that raise escaped `repair` entirely (A03).
- **Tried/rejected:** CLEAN after split cannot see coincident copies. Whole-mesh or guarded orientation missed local inversions and erased seam evidence; unconditional per-part orientation worked. A failure reason as a plain detail string was indistinguishable from a success note, which is how a failed PyMeshFix call reached `ok=True` (R02) — the original fix was a distinct `PartFailed` type; it was later replaced by the uniform `(ok, mesh, detail)` 3-tuple every step now returns, once every built-in step and the injected `tool` callable shared that one contract (owner decision, 2026-09-21) — the `ok` boolean carries what `PartFailed` used to. Splitting the four CLEAN filters into separate `apply_filters` calls costs one PyMeshLab float64↔float32 round trip each instead of one combined call; the owner chose this anyway, for one method per operation, accepting the extra rounding (measured not to change the outcome on the fixtures this project has). The PyMeshLab-filter step functions (`step_clean_*`/`step_orient`) previously lived here, duplicating "mark time, call step, record, stop on failure" three times by hand (WELD, CLEAN, PART) instead of once — moved to `meshlab` and collapsed into `_run_step`/`_record_step` (owner decision, 2026-09-21) after the owner found the duplication confusing. A stale `CLEAN_FILTERS` constant, kept from before the uniform-step refactor and no longer read by `repair()`, was deleted; the tests that needed a "one combined `apply_filters()` call" reference now keep a local tuple for that comparison rather than production code duplicating it. `Step`'s fine-grained WELD/CLEAN_* members were later collapsed into one `Step.PREP` (owner decision, 2026-09-21), trading structured `Step` filtering for string-named `WHOLE_MESH_STEPS`/`PART_MESH_STEPS` tuples that can move steps around without touching an enum — the step towards `pipeconfig`-driven step ordering tracked in open-issues.md. `DO_NOT_RETRY`, a dict of rejected-filter reasons enforced only by its own tests, was removed as a production constant; its content moved into the tests that need it (owner decision: important information that isn't behavior-driving doesn't need to be a constant). `repairer.is_available()` was removed — Blender/fast_simplification/PyMeshFix/PyMeshLab are all hard requirements now, so a startup check belongs in the main entry script once it exists, not in `repairer` or `processor`; tracked in open-issues.md.

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
