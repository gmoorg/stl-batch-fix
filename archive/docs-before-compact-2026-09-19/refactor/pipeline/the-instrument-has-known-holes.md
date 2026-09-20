### The instrument has known holes

Stated so the tables above are not over-trusted:

- **Three steps carry no timing at all** — `decimate`, `split`, `seamsplit`.
  "How long does decimation take" is still unanswerable, and `seamsplit` cannot
  be priced against the one save it produced.
- **`pymeshfix2` still logged no baseline.** `nm_in`/`open_in` came out empty
  on both rows, because they read `_pv_nm`, which is only assigned when Blender
  *reports success* — and the two rows that fired are precisely the case where
  it did not (`BLENDER_OPEN`). The numbers were recoverable from the step log
  (`open=12 -> 0`) but the column added for this question stayed blank.
- **The archived 13-for-13 for `pymeshfix2` was survivorship.** Before
  2026-09-13 the failure path wrote nothing, so "never helps" and "never ran"
  were the same observation.
- **`bbox_drift` is last-writer-wins and does not cross the part boundary.**
  28 `bbox changed` events in the run produced 9 summary rows. Both Falcons
  show empty drift because part 2 drifted under PyMeshFix and back under
  Blender, to three decimals — the excursion exists only in the step log.

