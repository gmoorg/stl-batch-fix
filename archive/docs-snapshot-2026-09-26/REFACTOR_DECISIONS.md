# Refactor decisions

The live record was compacted into topic references. As of 2026-09-22, only
four stay live in `docs/refactor/` — the rest moved to
`archive/docs-refactor-2026-09-22/` once alpha wrapping replaced the tools
they were investigating (weld/CLEAN/orient/Blender/PyMeshFix):

- [Module responsibilities, interfaces, implementation notes, and rejected attempts](docs/refactor/modules.md) — live
- [Final module orchestration and runner plan](docs/refactor/orchestration.md) — live
- [Tests and fixtures](docs/refactor/tests.md) — live
- [Live task list](docs/refactor/TODO.md) — live
- [One-file pipeline order and rejected orders](archive/docs-refactor-2026-09-22/pipeline.md) — archived
- [CLI, TUI, and `.fixcfg`](archive/docs-refactor-2026-09-22/interfaces.md) — archived
- [Open issues (superseded — refreshed into docs/refactor/TODO.md)](archive/docs-refactor-2026-09-22/open-issues.md) — archived

The original D1–D27 notes and experiments are preserved under `archive/docs-before-compact-2026-09-19/refactor/`. Use them only for exact historical evidence; the compact references and current code take precedence.
