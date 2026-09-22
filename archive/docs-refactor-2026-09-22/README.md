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
| See where the known bugs stand | [Discovered bugs](discovered-bugs.md) — Mandy, Amidara, and why the checks miss both |
| Diagnose remaining work | [Open issues](open-issues.md), then detailed [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md) |
| Judge a volume-loss verdict | [Mandy volume loss](mandy-volume-loss.md) — what the guard catches, and what it wrongly rejects |
| Ask why a clean model came back broken | [Amidara](amidara-clean-destroys.md) — the repair destroys a printable mesh, and every check passes it |

The compact files are the current design record. Historical D1–D27 notes remain under `archive/docs-before-compact-2026-09-19/refactor/` only when exact measurements or chronology are needed.

## Resuming an investigation in a clean session

Both agents read the same four files, in this order. Stop after step 3 unless
the task needs the measurements.

1. **[Discovered bugs](discovered-bugs.md)** — where Mandy and Amidara stand, what is blocked, and the next experiment. One page; start nowhere else.
2. **[Open issues](open-issues.md)** — the work list. The top of "Prevent false success and model loss" holds the items those two bugs produced.
3. **[Final orchestration](orchestration.md)** — the 25 numbered steps a model passes through, each with its goal, its condition, and its switch. Needed before changing or disabling anything.
4. **The investigation itself**, only when the task turns on a measurement: [Mandy volume loss](mandy-volume-loss.md) or [Amidara](amidara-clean-destroys.md). Both are long and hold the raw numbers, including refuted claims kept on purpose.

Two standing cautions for whoever picks this up:

- **The pipeline's checks do not detect the damage it causes.** A mesh can lose a limb, gain invented geometry, or carry hundreds of inverted faces and still score `open=0, nm=0` at 100% volume. Verify a repair by opening the file, not by reading the counters.
- **Every real defect found on 2026-09-21 was found that way.** Nine conclusions drawn from measurements alone were later refuted. Prefer "the loss localises to X, mechanism unidentified" over naming a cause that was never isolated.
