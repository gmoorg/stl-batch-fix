# Worker pool

Read [context](overview.md) when needed. Entries retain their original chronology; later corrections can supersede earlier proposals.

**Current lookup:** [D4's implemented interface](d4.md) describes `libs.pool`. The `get_next`/`admit` design in D4's draft and [D5](d5.md) was replaced. [D7](d7.md) explains format preparation, but its companion-copy isolation is not implemented. D8–D11 describe the intended batch runner, which is still unfinished; see [current state](../current-state.md) and the [status map](../history-status.md).

- [D1 — The runner is not exempt](d1.md)
- [D2 — Worker-pull, not parent-push](d2.md)
- [D3 — Threads, not a process pool](d3.md)
- [D4 — The Pool interface](d4.md)
- [D5 — Admission is a condition variable and a caller-supplied callback](d5.md)
- [D6 — No retry. If it failed, it failed](d6.md)
- [D7 — A preparation stage that normalises everything to binary STL](d7.md)
- [D8 — The watchdog does not survive](d8.md)
- [D9 — Volume is measured at open, like any other fact](d9.md)
- [D10 — The budget arithmetic stays in `--one-file`](d10.md)
- [D11 — Status is a plain dict under a lock, written by the owning thread](d11.md)
- [Open — worker pool](open-worker-pool.md)
