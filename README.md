# STL Batch Fix

Repair STL batches for FDM printing while preserving meaningful geometry.
Current implementation: `libs/` and `tools/batch_repair.py`. The legacy
`stl_batch_fix.py` and its TUI are outside the refactor; do not use their
repair logic as authority or modify them until the refactor is complete.

## Read only what the task needs

- Module behavior: the relevant entry in [modules](docs/refactor/modules.md),
  then that module's code and focused tests.
- Execution order, conditions, or failures: [pipeline](docs/refactor/orchestration.md).
- Planned changes and unresolved issues: [TODO](docs/refactor/TODO.md).
- Test selection and fixtures: [tests](docs/refactor/tests.md).
- Agent roles and independent review: [COLLABORATION.md](COLLABORATION.md).

Do not read all references at startup. Historical designs, completed handoffs,
and investigation narratives are excluded from the normal reading path.
Use [the archive index](archive/README.md) only for evidence a task actually needs.
Current code establishes implemented behavior; TODO records future decisions.

## Run and verify

`install.sh` installs dependencies; it and `run.sh` select the project environment.
Use `tools/project_python.sh` for every project Python command (including probes).
It selects `/mnt/sda2/python/.venv/bin/python` and sets the repository directory.
The current batch runner is invoked explicitly:

```bash
tools/project_python.sh tools/batch_repair.py --input /path/to/input --output /path/to/output --max-faces 100000 --workers 4
tools/project_python.sh -m unittest tests.tests.test_processor
tools/project_python.sh -m unittest discover -s tests/tests -t . -q -p 'test_*.py'
```

Input/output directories must not overlap. `--max-faces 0` disables only the
initial decimation; post-wrap per-part reduction still runs. Face targets are
best-effort. Steps append to `<output>/batch.log` by default (`--log-file`
overrides it). Run summary/progress also goes to the terminal.

The batch runner exists and has real smoke coverage. Open review items and
structural changes remain in TODO. A passing topology scan does not prove
shape preservation or printability; limitations are in the module reference.
