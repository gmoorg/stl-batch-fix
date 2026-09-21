# Refactor reference

## Goal

Produce an STL that preserves the intended model and is actually usable for FDM printing. Clean code, logs, and green tests support that goal; they do not compensate for lost parts, invalid geometry, an impractical face count, or a false success result.

`libs/` is an unfinished refactor of legacy `stl_batch_fix.py`. The modules implement most one-file operations, but the batch runner and several safety decisions remain unfinished.

## Fast reading order

| Need | Read |
|---|---|
| Change one module | [Modules](modules.md), then its code and focused test |
| Change geometry order | [One-file pipeline](pipeline.md), then [tests](tests.md) |
| Build the runner | [Final orchestration](orchestration.md) and [interfaces](interfaces.md) |
| Diagnose remaining work | [Open issues](open-issues.md), then detailed [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md) |
| Judge a volume-loss verdict | [Mandy volume loss](mandy-volume-loss.md) — what the guard catches, and what it wrongly rejects |

The compact files are the current design record. Historical D1–D27 notes remain under `archive/docs-before-compact-2026-09-19/refactor/` only when exact measurements or chronology are needed.
