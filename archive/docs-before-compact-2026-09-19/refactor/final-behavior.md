# Final interface and runner plan

Derived from the observable interface of `stl_batch_fix.py` and `stl_batch_fix_tui.py` on 2026-09-18. The runner treats `libs.processor` as the one-mesh operation; this plan records the required pipeline without copying repair code from the legacy script. Current refactor decisions override legacy implementation choices.

## Single goal

Everything in this refactor serves one result: an STL that actually fixes the intended model for FDM printing. Architecture, clean code, logs, tests, and professional-looking output are useful only when they help prove that result. A run is unsuccessful when it silently loses a meaningful part, emits invalid or impractical geometry, or labels an unproven result as repaired. When the pipeline cannot establish a safe result, it must preserve the complete available model in an explicit non-success outcome rather than present a partial mesh as fixed.

## Interface to preserve

### Entry modes

| Mode | Legacy interface | Refactor target |
|---|---|---|
| Batch CLI | Default invocation scans and processes a folder | One CLI adapter calls the shared runner. |
| TUI | `run.sh` launches `stl_batch_fix_tui.py` | The TUI observes the same runner through callbacks; it does not own another pool. |
| Statistics | `--stats` reads the existing run summary and exits | Reporting can be read without loading repair dependencies. |
| One file | `--one-file`, `--is-part`, and `--result-fd` provide an isolated child/debug entry | Keep an internal child entry and a supported single-file diagnostic command. |

User settings to carry into a configuration value: input folder, output suffix, recursive walk, worker count, face budget, per-mesh timeout, split-file ceiling, Blender reserve, minimum printable layer, merge distance, and bounding-box tolerance. Internal child flags must not become pipeline globals.

### Filesystem behavior

- Read STL and OBJ sources and configured companion files from the source tree.
- Preserve relative paths in a sibling `Fixed/` output tree.
- Cache OBJ and ASCII-STL conversions as binary STL under source-tree `stl-exported/`.
- Ignore `stl-exported/`, `~parts/`, `__MACOSX`, AppleDouble files, generated outputs, markers, and intermediates during collection.
- Skip completed outputs and markers on a later run. Deleting a retryable marker requests another attempt.
- Keep markers as usable mesh copies: broken, failed, timeout, unrepaired, open edges, destroyed, and undecimated.
- Commit outputs and markers atomically so an interrupted write cannot look complete.

### Observable run behavior

- Report each source exactly once as success, skipped, broken, failed, timed out, unrepaired, open, destroyed, or undecimated.
- A conversion, copy, or worker failure affects that item and does not silently disappear or abort unrelated work.
- Show live active-file status and final counts to both CLI and TUI adapters.
- Preserve diagnostic summaries: per-file reason, duration, triangle counts, path/steps, tool time, and geometry warnings. Compatibility with current TSV names and columns is useful during transition.
- Return a nonzero batch exit code when actionable failures remain. An empty input tree and statistics-only mode exit successfully.
- Ctrl+C stops admission, terminates active child/tool processes, and leaves completed results restartable.

## Shared contracts

Use plain values at the interface boundaries:

```text
RunConfig    paths, limits, worker policy, repair settings
Job          probed Mesh plus its source identity
FileResult   source, indicator, output/marker, reason, measurements, timing
RunEvent     prepared / started / progress / completed
RunSummary   preparation counts plus outcome counts
```

`FileResult` needs a JSON representation for the isolated child boundary. CLI and TUI formatting belongs outside these values.

## Pipeline steps

The pipeline has two levels. The batch level owns files, scheduling, isolation, and reporting. The mesh level owns geometry and returns one `Outcome`.

### Batch level

| Step | Owner | Result |
|---|---|---|
| 1. Parse and validate configuration | CLI/TUI adapter | One immutable `RunConfig`; dependencies and paths checked once. |
| 2. Walk and classify the source tree | `converter.prepare` + `indicators` | Companions copied, completed items skipped, mesh sources emitted once. |
| 3. Normalize inputs | `converter.prepare` + `blender.convert` | Every admitted mesh is a probed binary STL with a real face count. |
| 4. Order and admit jobs | runner selector + `pool.Pool` | Smallest measurable jobs first, bounded by workers and memory. |
| 5. Isolate one job | isolation wrapper | A child receives explicit job/config data and returns JSON `FileResult`. |
| 6. Execute the mesh pipeline | `mesh_io` + `processor` | One clean output candidate or one explicit non-success `Outcome`. |
| 7. Commit the outcome | atomic writer | Verified STL or full-mesh marker appears at its final path. |
| 8. Record completion | reporting + event callback | CLI/TUI progress, durable summary row, and exactly one final count. |

Preparation errors follow steps 7–8 directly: they produce a marker/result without entering geometry processing. A timeout or child crash is converted into a result by the surviving parent.

### One-mesh level

This is the intended order using the modules already present:

```text
probed binary Mesh
  → mesh_io.load
  → validate finite, non-empty source and record original evidence
  → decimator.decimate when over max_faces
  → compare decimated geometry with the original source
  → repairer.repair
       1. welder.repair T-junctions
       2. PyMeshLab CLEAN filters
       3. splitter.by_shells → independent part meshes
       4. for each retained part separately
            a. orient that part
            b. scan the part and select its repair route
            c. run PyMeshFix on that part for the defects it handles
            d. run Blender repair through PLY for holes/fins and defined fallback cases
            e. rescan after each tool; continue only while the route can improve it
            f. retain that part's result and failure evidence
       5. require an acceptable result for every retained part
       6. splitter.merge all accepted parts
  → processor decision gates, in order
       1. operation/tool failure
       2. non-finite or invalid geometry
       3. destructive volume/component/vertex loss
       4. non-manifold edges
       5. significant open edges
       6. final face budget
  → atomic output or marker write
```

Splitting defines the repair units. Orientation, PyMeshFix, and Blender operate on one part mesh at a time; no repair-tool call receives the combined set of shells. Blender uses the existing PLY boundary so the vertex table survives the round trip. Parts are merged only after every retained part has completed its own repair and acceptance handling. “Separately” describes geometry isolation inside the file-level job; it does not require one operating-system process per part.

Blender is a required capability of the final repair path, not an optional feature to omit. Measurements assign different work to the tools: Blender performed best on holes and fins, while PyMeshFix handled non-manifold geometry and winding seams better. The current `_repair_part` always runs PyMeshFix, while `blender_part` can only replace it through the injectable `tool` argument. The remaining design work is to define a defect-based route, including when one tool follows the other, its remaining timeout, and the acceptance check between them.

The preservation comparison must start from the original loaded mesh, before decimation. A clean topology result cannot override missing components, invalid measurements, a failed part, or destructive decimation. The final scan and face count apply to the merged result actually being written.

### Why CLEAN precedes SPLIT

This order was rechecked against the decisions, implementation, and tests. Keep `WELD → CLEAN → SPLIT` for the current filters:

- Vertex merging makes coincident duplicate faces detectable.
- Duplicate copies can initially appear as separate edge-connected shells.
- Splitting first prevents one cleanup pass from seeing both copies.
- The `doubles` regression left two shells and about 200% volume when split first; cleaning before the split reduces the duplicate geometry to the intended control surface.

`meshing_merge_close_vertices` can also join intended shells that happen to be very near each other. That risk needs a focused fixture and preservation check, but it is not a reason to move the whole CLEAN stage after splitting. If the risk is reproduced, separate the filters by responsibility: perform only cleanup that must see the combined geometry before splitting, then apply safe local cleanup to each part.

The current code implements the central path from decimation through merge and part of the decision stage. The validation, original-to-decimated preservation check, component-retention rule, and final face-budget gate remain incomplete and are tracked in [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md).

### Repair routing decisions still required

These branches exist as capabilities but are not fully routed in the current default mesh sequence:

- `repairer.blender_part`: make Blender part of the route; decide the measured defect trigger, whether it runs before or after PyMeshFix for mixed defects, its remaining timeout, and how the intermediate geometry is re-judged.
- `splitter.by_seams`: use only as recovery after measured destructive repair; a seam loop alone is not a reason to split.
- `scanner.open_loops_are_printable`: decide whether known model scale permits small closed boundary loops to pass before declaring `OPEN_EDGES`.

Each accepted fallback returns to the same decision gates. It does not bypass preservation, topology, or final-budget checks.

## Composition with current modules

```text
CLI or TUI
    │ RunConfig + event callback
    ▼
runner.run
    ├─ converter.prepare
    │    ├─ indicators.check
    │    └─ blender.convert for OBJ/ASCII STL
    ├─ scheduler using pool.Pool
    │    └─ isolated one-file child
    │         ├─ mesh_io.load
    │         ├─ processor.process
    │         └─ processor.write
    └─ reporting + RunSummary
```

Existing modules already cover preparation, markers, mesh I/O, one-mesh processing, Blender execution, and the generic thread loop. Missing work is the runner, isolation wrapper, reporting adapter, and entry-point wiring.

## Implementation plan

### 1. Freeze interface contracts

- Add immutable `RunConfig`, `FileResult`, `RunEvent`, and `RunSummary` values.
- Map every `indicators.Indicator` to one result and marker policy.
- Define exit-code rules and the event order.
- Add contract tests before adding concurrency.

### 2. Build a serial runner

- Call `converter.prepare` and collect every emitted `Mesh`.
- Sort measurable mesh jobs by face count.
- For each job: report start, handle invalid preparation results, load it, call `processor.process`, commit with `processor.write`, then report completion.
- Continue after one item fails.
- Test temporary source/output trees, companion copying, conversion cache reuse, markers, reruns, and destination collisions.

This phase supplies the first end-to-end entry through `libs/` and replaces the current legacy-only pipeline test gap.

### 3. Add durable isolated execution

- Move one job into a child process invoked with explicit configuration and a JSON result pipe.
- Keep the owning worker alive when the child times out, crashes, aborts, or is OOM-killed.
- Let the parent write timeout/crash markers because a killed child cannot do so.
- Kill the child's Blender descendants on timeout or interrupt.
- Test normal result, Python exception, malformed/no result, timeout, signal death, interrupted write, and retry.

### 4. Add scheduling and admission

- Feed jobs to `pool.Pool` through a selector that owns ordering, completion, and error policy.
- Admit work by configured memory budget and face-count estimate; reduce active workers when needed.
- Surface selector and handler exceptions in `FileResult` and `RunSummary`.
- Stop releasing jobs after interrupt while immediately terminating active isolation children.
- Test exactly-once handling, completion out of order, worker shedding, and interrupt cleanup.

### 5. Add reporting once

- Make the runner emit structured events and write logs/summaries from one reporting component.
- Adapt the plain CLI and TUI to the same event stream.
- Keep only compact results in memory; detailed tool output goes to logs.
- Implement statistics mode on stored summaries without importing mesh tools.

### 6. Switch entry points

- Point the CLI entry at the new runner while retaining compatible user flags.
- Change the TUI from importing legacy globals and creating its own pool to constructing `RunConfig` and observing events.
- Update `run.sh` only after both adapters pass the same runner tests.
- Keep the legacy script available as a comparison until collection verification finishes.

### 7. Prove replacement

- Run focused module tests and the full commands in [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md).
- Run end-to-end tests through the new serial and concurrent runner paths.
- Verify skip/resume, markers, timeout, crash, Ctrl+C, output layout, summaries, and exit codes.
- Run a representative collection and compare source accounting and durable outcomes with the legacy interface. Geometry acceptance is judged by `processor` tests, not by interface parity.

## Explicit non-goals

- Do not port `_process_file_impl` or its step implementation into the runner.
- Do not preserve the legacy `ProcessPoolExecutor`; the selected owner pool uses threads plus isolated children.
- Do not duplicate orchestration in the TUI.
- Do not make mutable module globals the configuration API.
- Do not treat identical log wording as compatibility; preserve statuses, reasons, files, and restart behavior.
