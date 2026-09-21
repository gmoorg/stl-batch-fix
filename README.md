# STL Batch Fix

Repair STL models in batches for **FDM printing** while preserving the intended shape. Every refactor, test, and document serves one goal: produce an STL that actually fixes the model for printing. A polished pipeline that silently loses a meaningful part, emits invalid geometry, or reports success without proving the result has failed that goal. `stl_batch_fix.py` is the legacy script being refactored into `libs/`; do not inspect or change it until the refactor is complete. `STL_BATCH_FIX_DESIGN.md` is a potentially stale description of that legacy script. `libs/` is the unfinished refactor and does not yet have a complete batch runner.

## Start here in a new session

1. Open the compact [refactor index](docs/refactor/README.md) and follow only the topic needed: modules, pipeline, orchestration, interfaces, tests, or open issues.
2. Read [review findings](libs/review/CODE_REVIEW.md) only when detailed reproductions or the owner's comment slots are needed.
3. Check the relevant `libs/*.py` and `tests/tests/test_*.py` before editing.

For every non-trivial task, Claude Code leads the [Claude–Codex workflow](COLLABORATION.md): Codex independently validates intent before planning, challenges the plan, and reviews the completed change when Claude implements. Agreement advances automatically; user input is reserved for material ambiguity, consequential disagreement or preference, and actions requiring explicit approval.

The [docs index](docs/README.md) routes other questions. Detailed historical notes are archived outside `docs/`. The legacy [design](STL_BATCH_FIX_DESIGN.md) is retained for later and is outside the refactor reading path.

Collaboration keeps one Codex session for interpretation/planning and a separate
review session that resumes for fixes. Start a new task with
`tools/reset_codex.sh`. To start both AIs clean, save any needed handoff, run that
reset after active calls finish, then run `/clear` in Claude Code. See the
[session and reset instructions](COLLABORATION.md#starting-both-ais-clean).

## Current refactor in one pass

`converter.prepare` classifies and converts inputs; `pool.Pool` provides a worker primitive, but the batch walk is unfinished. For one mesh, `processor.process` decimates, then `repairer.repair` welds T-junctions, cleans geometry, and splits edge-connected shells. It processes every retained part separately: orient the part, repair it in Blender, then in PyMeshFix, verify the result, then merge all accepted parts. Blender's per-part repair (`pipeconfig.ENABLE_BLENDER_PART`, default on) now runs unconditionally before PyMeshFix on every part; defect-based routing — sending a part to only the tool it actually needs — remains unimplemented. `processor` checks the merged result for volume loss and topology before `processor.write` emits an STL or failure marker. Each stage has a limited purpose; preserve their order unless evidence supports a change.

The review records cases where that sequence can still lose geometry or misreport success. A successful repair must preserve meaningful components as well as satisfy practical slicer limits. Printability can also depend on scale, orientation, and supports, which the mesh alone may not settle.

## Work and verification

Run every Python test, fixture generator, or ad hoc script that imports this project through `tools/project_python.sh`, which always selects `/mnt/sda2/python/.venv/bin/python` and changes to the repository root. `run.sh` and `install.sh` select that environment automatically.

```bash
# Full suite
tools/project_python.sh -m unittest discover -s tests/tests -t . -q -p 'test_*.py'

# One test module
tools/project_python.sh -m unittest tests.tests.test_welder

# Fixture tools
tools/project_python.sh tests/tests/make_fixtures.py --check
tools/project_python.sh tools/make_probe_meshes.py tests/probes
```

Test modules live under `tests/tests/`; legacy pipeline fixtures remain in `tests/fixtures/`, while refactor regression models live in `tests/probes/`. `tests/tests/make_fixtures.py` regenerates and validates both sets. Direct test files, including `tests/tests/test_pipeline.py`, also require the environment interpreter. That pipeline test exercises the **legacy** script and is not refactor coverage. When changing a refactor step, update its focused test and the relevant compact reference or [open issue](docs/refactor/open-issues.md). Keep code comments on local contracts and invariants; keep experiments and rationale in docs.
