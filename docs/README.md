# Documentation routing

Read only the reference relevant to the task:

- [Modules](refactor/modules.md): responsibilities, contracts, limitations.
- [Pipeline](refactor/orchestration.md): current calls, conditions, failures.
- [TODO](refactor/TODO.md): open work and agreed future design.
- [Tests](refactor/tests.md): commands, suite boundaries, regression evidence.

[Archive](archive/README.md) (in the repository, since 2026-10-10): the
investigations behind current decisions — winding reconstruction evidence,
the 2026-10-04 full-run errors. The older
[outside archive](../../stl-batch-fix.old/archive/README.md) keeps dated
reviews and history from before. Neither is a specification.
All current actions belong in TODO. Avoid recursive documentation reads.

## Keep one current reference per topic

Update the owning reference when behavior changes; link to it rather than
copying its explanation into another document. Keep implemented behavior in
modules/pipeline, future work in TODO, and investigation evidence in archives.
On completion, remove the task's stale TODO description instead of appending
a contradictory "done" entry. Handoffs and reviews are dated evidence, not
additional specifications. Verify claims against code before carrying them
into an active reference.
