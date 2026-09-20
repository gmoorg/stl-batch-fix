# Open issues

This is the compact work list. Detailed reproductions and owner comment slots remain in [`CODE_REVIEW.md`](../../libs/review/CODE_REVIEW.md).

## Prevent false success and model loss

- Reject zero-face, non-finite, zero-area, or invalid geometry.
- Compare decimation with the original; current destruction checks begin too late.
- Preserve meaningful components. The `<100 faces` rule and signed-volume cancellation can approve missing parts.
- Propagate PyMeshFix failure; it can currently become `repairer.ok=True`.
- Add a final face-budget gate after repair/merge.
- Commit output and markers atomically.
- Define per-part routing between PyMeshFix and Blender, including mixed defects and intermediate acceptance.

## Algorithm questions

- Handle reversed/non-monotone T-junction paths.
- Distinguish a far bent junction from a hole; no defensible distance bound exists.
- Test whether CLEAN joins intended nearby shells; if so, divide global and per-part filters.
- Replace face-count debris deletion with a preservation rule.
- Decide when seam recovery and `open_loops_are_printable` are safe.
- Replace remaining absolute geometry tolerances where scale tests require it.

## Runner and concurrency

- Build serial runner, per-file isolation, scheduling, reporting, and shared adapters.
- Surface converter, copy, selector, child, and reporting failures exactly once.
- Resolve OBJ/STL same-stem collisions.
- Serialize PyMeshFix output capture or isolate it.
- Track concurrent Blender processes and kill descendants on timeout/interruption.

## Missing regression coverage

- Zero-face/non-finite input and failed PyMeshFix.
- Decimation-lost appendage and small valid shell.
- Opposite-volume shells and zero-area closed topology.
- Reversed T-junction and far bent path.
- Selector/converter/copy exceptions and source collisions.
- Interrupted write and final face budget.
- Full runner markers, retry, crash, timeout, Ctrl+C, layout, summary, and exit code.

Do not turn an unresolved geometry judgment into a passing expectation merely to make the suite green.
