# Final batch orchestration

## Module order

```text
CLI or TUI → RunConfig
  → converter.prepare
       indicators.check
       blender.convert only for input normalization
  → sort/admit Jobs with pool.Pool
  → isolated child per file
       mesh_io.load
       processor.process
         decimator → repairer → scanner decision
       atomic processor.write
  → FileResult over JSON
  → one reporting/event component
  → CLI/TUI display + durable summary
```

## Required behavior

1. Validate configuration and dependencies once.
2. Classify every source once; copy companions and normalize OBJ/ASCII STL to probed binary STL.
3. Preserve identity and relative output paths; reject destination collisions.
4. Admit jobs by worker and memory limits, preferably smallest measurable first.
5. Run each file in a child so timeout, native crash, OOM, or Blender descendants cannot kill the pool.
6. Return exactly one structured result per source. The parent creates timeout/crash markers when needed.
7. Commit outputs and markers atomically.
8. Emit one event stream for CLI/TUI and return nonzero when actionable failures remain.

## Why other structures failed

- Separate TUI/script pools duplicate scheduling, status, and cancellation.
- A process pool does not preserve pending work when a worker dies; stable threads should own disposable children.
- Mutable globals let CLI, tests, and children disagree; pass immutable values.
- Logs written by several layers lose exactly-once accounting; one runner owns final results.
- A killed child cannot write its timeout result; the surviving parent must.
- Direct final-path writes make interrupted files look finished.

## Build order

Define `RunConfig`, `Job`, `FileResult`, `RunEvent`, and `RunSummary`; build a serial runner; add child isolation; add scheduling/admission; add reporting; then point CLI and TUI at the shared runner. Do not port legacy `_process_file_impl` repair logic.
