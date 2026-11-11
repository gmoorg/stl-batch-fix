# Script review

Reviewed: 2026-11-11. All nine findings resolved the same day (notes under each heading).

Scope: the current Python entry points and shell launchers, with supporting review of library boundaries, repair orchestration, configuration, publication, dependency checks, and relevant tests. This is a maintainability and correctness review, not an exhaustive audit of the geometry algorithms. Experimental probes were sampled only; generated meshes and archived implementations were excluded. No implementation changes were made. The existing README.md working-tree change was left intact.

Priorities: **P2** = correctness or operational reliability; **P3** = maintainability. Locations refer to the code at review time. Suggested function names are proposals, not existing APIs.

## Correctness and error handling

### 1. P2 — Handle support analysis failures at the CLI boundary

**Resolved 2026-11-11.** An analysis ValueError/MemoryError skips the instance (`analysis failed: ...`, exit 3). The post-write diagnostic (`_report_written_ribs`) reports a reread or analysis failure with the written path and keeps exit 0/3. Tests: `test_failed_analysis_skips_the_instance`, `test_failed_diagnostic_still_reports_the_written_file` (reread and analysis).

**Location:** `support_3mf.py:250–254, 300, 335–345`.

`basecheck.check()` is called without the `ValueError`/`MemoryError` handling used by `check_3mf.py:182–186`. An analysis failure therefore escapes as a traceback rather than the documented exit status. The diagnostic reread after writing ribs is also unguarded: a failure there can make an already-published output appear to have failed without explaining that the file exists.

**Recommendation:** extract `analyze_instance()` and a separate `report_written_ribs()` helper. Convert expected analysis failures into a per-instance skip or a documented error, according to the failure stage. Distinguish publication success from failure of the optional post-write diagnostic; report the output path explicitly in that case. Keep unexpected programming errors distinguishable from expected input/resource failures.

**Verification:** injecting `MemoryError('probe')` into `support_3mf.basecheck.check` with a valid temporary 3MF caused `main()` to raise `MemoryError`. Add focused coverage for analysis failure before publication and diagnostic failure after publication.

### 2. P2 — Reject non-finite support dimensions consistently

**Resolved 2026-11-11.** `--max-height` must be positive and finite; a non-finite project line width falls through to the next key, then 0.42. Pitch, width, gap and overlap were already checked by `RibSettings`/`GridSettings`. Tests cover nan, inf, -inf, 0, a valid value, and the line-width fallback.

**Location:** `support_3mf.py:120–153`.

`_check_layout_args()` checks `max_height > 0`, which accepts positive infinity. `_line_width()` also accepts positive infinity from project settings. This differs from the finite-number validation already used by `libs/printsettings.py:_number` and `libs/runconfig.py:_check`. Infinity is not a meaningful physical dimension and can bypass the intended support-height cutoff or fail later during geometry construction.

**Recommendation:** require `math.isfinite(value)` as well as the relevant range check. Validate layout arguments before reading the project; make invalid project line widths follow an explicit fallback or error policy. Reuse a small numeric validation helper where the semantics match, rather than adding a general validation framework.

**Verification:** `parse_args(['unused.3mf', '--max-height', 'inf'])` followed by `_check_layout_args()` succeeds and retains `inf`. Cover `nan`, `inf`, `-inf`, zero, and a valid finite value.

### 3. P2 — Make required dependency installation failures fail the script

**Resolved 2026-11-11.** Every required package goes through `require_package`, which exits 1 when install or re-import fails; libigl's symbol check is passed in as its check. Checked with `bash -n` and a stub interpreter whose pip fails. No installer test was added and no real install was run.

**Location:** `install.sh:136–147`.

The NumPy install command ends with `|| fail "numpy install failed"`. `fail()` only prints; it returns success when printing succeeds. Because this command is in an AND/OR list, `set -e` does not provide the intended failure behavior. The final success messages then run and the script can exit zero. This branch requires NumPy to be unavailable; it need not occur on a normal installation to be incorrect.

**Recommendation:** use the explicit `exit 1` failure block already used for the other required packages. Extract an `ensure_package()` helper for the repeated import/install/recheck pattern, with required versus optional failure behavior explicit. Keep specialized libigl symbol checks as callbacks or separate checks.

**Validation to add:** run the installer against a stub interpreter that makes only the NumPy import and install fail; assert a nonzero status and no successful completion message. Do not install or uninstall real packages for this test.

### 4. P2 — Declare and handle the optional PNG dependency

**Resolved 2026-11-11.** Matplotlib is optional in `libs.dependencies` and `install.sh`; `check_3mf --png` checks it before reading. Map write errors name the path, are counted in the summary, and leave the analysis exit status unchanged; the figure always closes. Test: `test_unwritable_map_keeps_the_analysis_status`. No missing-library test, by project rule.

**Location:** `check_3mf.py:86–114, 188–191`; `install.sh`; `libs/dependencies.py:LIBRARIES`.

`--png` imports Matplotlib on demand, but the installer and dependency registry do not include it. A repair installation can therefore pass its dependency checks while the advertised PNG option raises `ModuleNotFoundError`. PNG filesystem errors also escape from `save_png()`.

**Recommendation:** declare Matplotlib as an optional dependency for PNG export, check it when `--png` is requested, and turn expected export failures into a clear path-specific CLI error. Use `try/finally` around figure saving so figures close even if `savefig()` fails. Preserve the analysis result when explaining an export failure.

**Validation to add:** simulate a missing plotting dependency and an unwritable image destination; check the chosen exit status and error message.

## Function and module boundaries

### 5. P3 — Split support orchestration by responsibility

**Resolved 2026-11-11.** `_resolve_layout` (`_RibLayout`/`_GridLayout`), `_plan_instance`, `_plan_ribs`, `_plan_grid`, `_report_written_ribs`, with `_InstanceOutcome`. Output text and exit codes unchanged; existing ribs/grid/keel tests pass.

**Location:** `support_3mf.py:177–348` (`main`).

This function resolves layout defaults, reads the project, constructs layout settings, decides eligibility, analyzes instances, generates three support layouts, publishes, and performs post-write diagnostics. Layout branches depend on conditionally created locals such as `rib_settings`, `grid_settings`, and `foot`. Adding another layout would require editing several interleaved branches.

**Recommendation:** keep parsing and exit-code translation in `main()`. Extract `resolve_layout_settings()`, `plan_instance_support()`, and `report_written_ribs()`. Return a small structured instance outcome containing its status, optional `Addition`, and diagnostics instead of mutating several outer lists/counters. Keep ribs and grid/keel builders as ordinary functions; a plugin system or class hierarchy is unnecessary for three layouts.

**Preserve:** eligibility checks, non-printable skips, refusal to overwrite, partial-run status, and output naming. Use existing `test_support_3mf` cases as behavior checks before moving code; extend grid/keel coverage where their CLI branches are affected.

### 6. P3 — Separate batch execution from summary calculation and rendering

**Resolved 2026-11-11.** `_summarize_run` (one snapshot of the run state) → `_RunSummary`, rendered by `_print_run_summary` and `_final_record`; `reporter.finalize()` remains the single atomic final write. `collect` replaced by `emitted.append`.

**Location:** `batch_repair.py:1160–1344` (`_run_detailed`), especially `1260–1344`.

The function combines intake, two-pass dispatch, result aggregation, terminal output, progress-log serialization, and status calculation. Console and JSON reporting currently share computed values, which is valuable, but those responsibilities are maintained inside one long orchestration function.

**Recommendation:** first extract a pure `summarize_run()` that returns counters, diagnostics, incomplete state, and pending counts. Have `print_run_summary()` and a final-record serializer consume that same result. Only then consider extracting intake or pass dispatch if it makes the remaining control flow clearer. Replace the trivial local `collect()` wrapper at `1189–1190` with `emitted.append` unless it gains a separate responsibility.

**Preserve:** recovered outcomes count against a clean run, cancelled jobs are not terminal outcomes, prepared jobs are not double-counted, and `reporter.finalize()` remains the atomic final write. Validate through existing batch-run/progress/prepare-pass tests, including cancellation and unresolved workers.

### 7. P3 — Move per-file processing out of the executable batch module

**Resolved 2026-11-11.** New `libs/perfile.py`: `prepare_one_file`, `process_one_file` and the cache-layout functions. The child imports it instead of `batch_repair`; parent-death arming still runs first. Tests and `tools/experiments/malloc_child.py` updated.

**Location:** `batch_repair_child.py:60–99`; `batch_repair.py:_process_one_file` and `_prepare_one_file`.

The child imports the parent entry point and calls its private functions. That couples per-file pipeline work to parent orchestration and loads the parent's runner/progress dependencies into each child. The underscore names also describe a private interface that another executable depends on.

**Recommendation:** move the shared per-file operations into a focused library module with public `prepare_one_file()` and `process_one_file()` functions. Let both executables depend on that module. Keep subprocess creation, cancellation, reconciliation, and batch configuration in the parent. Retain the child's early parent-death arming before heavy imports.

**Validation:** exercise both child modes, validate their `ChildResult` files, and retain startup/parent-death checks. Update tests that patch parent-private helpers to patch the new owner.

### 8. P3 — Extract the per-part repair loop and correct its type contract

**Resolved 2026-11-11.** `_repair_part` returns `_PartOutcome`; `repair` keeps the face targets and the stop at the first failed part. `part_steps` is typed `tuple[Entry | tuple[str, MeshStep], ...]`.

**Location:** `libs/repairer.py:267–443`, especially `377–425`.

`repair()` handles whole-mesh preparation, gating, splitting, per-part configuration and repair, merging, and measurements. The per-part section is a useful independent responsibility: it must retain the split part's original face target, apply the clean/NM gates, and stop on the first failed part. Its `part_steps` annotation advertises tuples only, while the implementation accepts `Entry` objects through `execstep.as_entry()` and `part_steps_for()` returns `tuple[Entry, ...]`.

**Recommendation:** extract `_repair_part()` returning an explicit outcome; keep the loop's early-failure behavior visible to its caller. Update the accepted-entry type alias and documentation to match both supported forms. Reuse the executor's types rather than duplicating callable signatures.

**Validation:** preserve custom empty sequences, clean-gate precedence, NM fallback to the original part, original face targets, and first-failed-part behavior using focused repair pipeline tests.

### 9. P3 — Share the common print-settings argument definitions

**Resolved 2026-11-11.** `printsettings.add_settings_arguments(parser)`, used by both CLIs; each CLI has a test that parses all four overrides.

**Location:** `check_3mf.py:122–126`; `support_3mf.py:112–116`.

Both parsers independently define the same four settings overrides consumed by `resolve_settings()`. Changes to names, help, or defaults must be synchronized manually.

**Recommendation:** extract `add_print_settings_arguments(parser)` into a small CLI helper used by these two scripts. Keep layout-specific and analysis-only options local. Avoid merging the two complete parsers or their exit-code policies.

**Validation:** assert that both CLIs expose the four overrides and that the existing settings-precedence tests continue to pass.

## Suggested order

1. Address expected failures and invalid numeric inputs (findings 1–4).
2. Extract summary calculation and support planning without changing behavior (5–6).
3. Move per-file processing and extract per-part repair with focused regression checks (7–8).
4. Consolidate the small shared parser fragment (9).

Existing `libs/runconfig`, `libs/runstate`, `libs/proctree`, `libs/childresult`, and `libs/publication` already provide useful ownership boundaries. Preserve their process-lifecycle and publication guarantees. Do not split performance-sensitive geometry loops merely because they are long; require a concrete readability benefit and preservation evidence. Existing documented geometry limitations are not newly discovered defects in this review.

## Checks performed

- Read the entry points and shell launchers, inspected the relevant library implementations and focused tests, and used AST inspection to locate long functions.
- Reproduced the escaping analysis error and accepted infinite max-height with temporary data and mocks; no source models were changed.
- Focused test results are recorded below after completion. Full geometry/end-to-end and installation tests were not run; no package installation was attempted.
- Passed: `tools/project_python.sh -m unittest tests.tests.test_support_3mf tests.tests.test_check_3mf tests.tests.test_batch_repair_cli tests.tests.test_runconfig -q` — 118 tests, 41.189 seconds. Expected invalid-input diagnostics were printed by negative tests; Matplotlib also reported a temporary cache fallback because its default user cache directory was not writable.
