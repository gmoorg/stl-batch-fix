# Refactor decisions

> **Documentation update (2026-09-18):** The comment policy below records the
> earlier approach. Module comments were subsequently compacted to current
> contracts and invariants; measurements moved to
> [implementation evidence](implementation-evidence.md). Start with the
> [current implementation](current-state.md) for the code as it stands.

A running record of what was decided and why, while the refactor is being
designed. Written because the reasoning is expensive to reconstruct and easy to
lose — the design doc went 13 commits stale in a day, and `TODO.md` went four
items behind.

**This file holds what was decided and why.** `TODO.md` holds what is left to do
and how to go about it — the guidelines, how small to make each step, what the
tests do not catch.

Decisions are grouped by topic. Numbers are global and stable across topics, so
a commit message citing D7 keeps meaning D7 when a new topic is added.

**Status (2026-09-17): thirteen modules in `libs/`, 427 tests, all green, no
skips.**
`stl_batch_fix.py` is untouched and still frozen. Built so far: `pool`,
`indicators`, `blender`, `mesh_io`, `scanner`, `splitter`, `meshfix`,
`decimator`, `converter`, `welder`, `repairer`, `processor`. Still to build:
**the batch walk** — the step that feeds files to `processor` through `pool`
and reports across a whole collection.

Raw session narratives are archived under `archive/` when they grow past
usefulness; this file keeps the conclusions.

> **Code comments are deliberately verbose while the refactor is in motion, and
> will be compacted to a few lines once it reaches a conclusion** (decided
> 2026-09-17). They carry measurements — which value was tried, what it did,
> why the obvious alternative was rejected — and re-deriving those costs a
> session. `welder` changed three times in one day, and compacting between
> changes would have meant losing the reasoning each time.
>
> **The cost is real and was demonstrated the same day**: asked to check
> `welder` for stale comments, five were found, one of them contradicting the
> code three lines below it. Both had been written minutes apart. The user's
> own practice is the opposite — small functions with names that cannot go
> stale — and that objection stands; the trade is accepted only while the
> design is still moving.
>
> **So: a comment is a claim, and every non-trivial edit has to re-check the
> claims around it.** Not optional, and not something to wait to be asked for.
> When the refactor settles, compact each block to its conclusion and leave the
> measurements here.

> **Renumbered 2026-09-13.** Entries were previously numbered in the order they
> were written, and each new one was inserted before D5 — so the file read
> D1–D4, D4b, D7, D8, D9, D10, D5, D6. They are now in decision order. Three
> contradictions were fixed at the same time: D2 and D3 still described `requeue`
> and a surviving watchdog, both of which later entries had abolished. Earlier
> commit messages refer to the old numbers.

---
