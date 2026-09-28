# Tests and fixtures

Run project Python commands through `tools/project_python.sh`; it selects the project environment and repository root regardless of the caller's current directory.

```bash
tools/project_python.sh -m unittest discover -s tests/tests -t . -q -p 'test_*.py'
tools/project_python.sh -m unittest tests.tests.test_welder
tools/project_python.sh tests/tests/make_fixtures.py --check
```

## Layout

- `tests/tests/test_<module>.py`: focused `libs` contracts and algorithms.
- `test_repair_pipeline.py`: refactor geometry sequence against known controls.
- `test_regression_fixtures.py`: guards source geometry of review probes.
- `test_pipeline.py`: legacy script coverage; not proof of the refactor runner.
- `make_fixtures.py`: generates/checks both fixture groups.
- `tests/fixtures/`: legacy fixtures; `tests/probes/`: refactor/model-loss probes.

## What tests must prove

- Assert intended geometry survives, not merely `open=0` and `nm=0`.
- Verify step order and that every split part reaches repair independently.
- Compare controlled defects with a known-correct surface.
- Separate wrapper tests from policy tests; native tool success is not pipeline success.
- Use real Blender/PyMeshFix only where a stub cannot prove the boundary.
- Cover scale, opposite winding, component retention, invalid input, timeout/crash, atomic writes, and exactly-once reporting.

High-priority probes are `small_valid_shell.stl`, `opposite_volume_shells.stl`, `decimation_lost_appendage.stl`, and `reversed_tjunction_chain.stl`. Their construction is tested; pipeline verdict tests remain missing.
