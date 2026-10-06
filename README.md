# STL Batch Fix

Repair STL batches for FDM printing while preserving meaningful geometry.
Implementation: `libs/` and `batch_repair.py`. The pre-refactor script
(`stl_batch_fix.py` and its TUI) lives only on the `old-script` branch.

## Read only what the task needs

- Module behavior: the relevant entry in [modules](docs/refactor/modules.md),
  then that module's code and focused tests.
- Execution order, conditions, or failures: [pipeline](docs/refactor/orchestration.md).
- Planned changes and unresolved issues: [TODO](docs/refactor/TODO.md).
- Test selection and fixtures: [tests](docs/refactor/tests.md).
- Agent roles and independent review: [COLLABORATION.md](COLLABORATION.md).

Do not read all references at startup. Historical designs, completed handoffs,
and investigation narratives are excluded from the normal reading path.
Use [the archive index](../stl-batch-fix.old/archive/README.md) (kept outside the repository) only for evidence a task actually needs.
Current code establishes implemented behavior; TODO records future decisions.

## Run and verify

`install.sh` checks and installs dependencies into the project environment.
Use `tools/project_python.sh` for every project Python command (including probes).
It selects `/mnt/sda2/python/.venv/bin/python` and sets the repository directory.
The batch runner takes no command-line arguments. Every option lives in
`batch_repair.toml` beside the script (git-ignored). Copy the documented
example once, edit it, then run:

```bash
cp batch_repair.example.toml batch_repair.toml   # first time only; then edit it
./fix.sh                                         # = tools/project_python.sh batch_repair.py
tools/project_python.sh -m unittest tests.tests.test_processor
tools/project_python.sh -m unittest discover -s tests/tests -t . -q -p 'test_*.py'
```

[batch_repair.example.toml](batch_repair.example.toml) documents every option.
A bad or unknown key stops the run before anything is written. Input/output
directories must not overlap. `max_faces = 0` disables only the initial
decimation; post-wrap per-part reduction still runs. Face targets are
best-effort. Every model also gets a raw log beside its output
(`foo.stl` → `foo.log`, appended per run with a dated header) holding
every tool's own output, step separators and any crash message.
Steps append to `<output>/batch.log` by default (`log_file`
overrides it). Run summary/progress also goes to the terminal.
Each part is rebuilt as a solid by winding-number reconstruction
(`libs/winding.py`; alpha-wrap stays available but is no longer the default);
`reconstruct_memory_budget_gb` (default 10) sizes it per part and worker.
`skip_clean = true` (opt-in, not yet validated) skips repair for an
already-clean decimated model, or otherwise for its individual clean parts;
a part whose only defect is non-manifold edges tries MeshFix alone first.

A separate read-only checker predicts base-layer print risks in a Bambu
Studio 3MF (`tools/project_python.sh check_3mf.py plate.3mf [--png DIR]`);
`support_3mf.py plate.3mf` writes `plate.supported.3mf` with thin breakaway
ribs under undersides the slicer cannot support; see
[modules](docs/refactor/modules.md#print-risk-check-separate-from-repair).

The batch runner exists and has real smoke coverage. Open review items and
structural changes remain in TODO. A passing topology scan does not prove
shape preservation or printability; limitations are in the module reference.
