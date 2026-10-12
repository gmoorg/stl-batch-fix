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
Use the archives only for evidence a task actually needs:
[docs/archive](docs/archive/README.md) (investigations behind current
decisions) and [the older archive](../stl-batch-fix.old/archive/README.md)
(outside the repository).
Current code establishes implemented behavior; TODO records future decisions.

## Run and verify

`install.sh` checks and installs dependencies into the project environment.
Use `tools/project_python.sh` for every project Python command (including probes).
It selects `/mnt/sda2/python/.venv/bin/python` and sets the repository directory.
Every option lives in `batch_repair.toml` beside the script (git-ignored)
except one command-line flag, `--alpha`. Copy the documented example once,
edit it, then run:

```bash
cp batch_repair.example.toml batch_repair.toml   # first time only; then edit it
./fix.sh                                         # = tools/project_python.sh batch_repair.py
./fix.sh --alpha                                 # alpha-wrap instead of winding reconstruction
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
(`libs/winding.py`); `reconstruct_memory_budget_gb` (default 10) sizes it
per part and worker. `--alpha` rebuilds every part with CGAL alpha-wrap
instead (CGAL then required); it wraps both sides of a hole wider than
alpha, so such a model is refused (DESTROYED) where winding repairs it.
The run's first line, also in `batch.log`, names the reconstruction used.
`skip_clean = true` (opt-in; sample outputs passed Bambu's recommended
repair tool, slicing and printing unconfirmed) skips repair for an
already-clean decimated model, or otherwise for its individual clean parts;
a part whose only defect is non-manifold edges tries MeshFix alone first.
`--alpha` turns `skip_clean` off with a warning: the check does not see
self-intersections, which are what alpha-wrap is used to remove.

To fix individual files where they are, select them in Nemo (Cinnamon's
file manager), right-click and choose **STL fix** or **STL alpha fix**
(`--alpha`). Install the two entries once with
`tools/install_nemo_actions.sh`; the same works from a shell:

```bash
./fix_files.sh [--alpha] -- "my models/foot.stl" "my models/leg.obj"
```

Each `foo.stl`/`foo.obj` gets `foo_fixed.stl` beside it, or a marker such as
`foo_fixed.failed.stl`, replacing earlier results of that file; the model
log is appended to `foo_fixed.log` and the run logs to `batch.log` and
`progress.log` in that folder. All other options come from
`batch_repair.toml`. The work happens in a hidden `.stl-fix-*` folder there,
which is removed at the end (or by the next run, after a hard kill).

A separate read-only checker predicts base-layer print risks in a Bambu
Studio 3MF (`tools/project_python.sh check_3mf.py plate.3mf [--png DIR]`);
`support_3mf.py plate.3mf` writes `plate.supported.3mf` with thin breakaway
ribs under undersides the slicer cannot support; see
[modules](docs/refactor/modules.md#print-risk-check-separate-from-repair).

The batch runner exists and has real smoke coverage. Open review items and
structural changes remain in TODO. A passing topology scan does not prove
shape preservation or printability; limitations are in the module reference.
