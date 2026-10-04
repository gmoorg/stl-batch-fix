# Documentation routing

Read only the reference relevant to the task:

- [Modules](refactor/modules.md): responsibilities, contracts, limitations.
- [Pipeline](refactor/orchestration.md): current calls, conditions, failures.
- [TODO](refactor/TODO.md): open work and agreed future design.
- [Tests](refactor/tests.md): commands, suite boundaries, regression evidence.
- [Reconstruction](refactor/reconstruction.md): winding-number + marching-cubes experiment (alpha-wrap alternative).

[Archives](../../stl-batch-fix.old/archive/README.md) (outside the repository) retain dated reviews and historical evidence.
All current actions belong in TODO. Avoid recursive documentation reads.

## Keep one current reference per topic

Update the owning reference when behavior changes; link to it rather than
copying its explanation into another document. Keep implemented behavior in
modules/pipeline, future work in TODO, and investigation evidence in archives.
On completion, remove the task's stale TODO description instead of appending
a contradictory "done" entry. Handoffs and reviews are dated evidence, not
additional specifications. Verify claims against code before carrying them
into an active reference.
