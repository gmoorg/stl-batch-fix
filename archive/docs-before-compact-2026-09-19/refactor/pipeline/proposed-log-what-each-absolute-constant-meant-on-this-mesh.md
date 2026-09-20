### Proposed — log what each absolute constant meant on this mesh

Not a change to any constant. The values are right; what is missing is any
record of what they implied for a given model, which is what made the Leia
distortion take a full trace to diagnose.

Each step that consults an absolute constant should record its relative value:

```text
printscale     applied at 14.6% of model extent  (MIN_LAYER 0.6mm, diag 4.1mm)
merge_dist     0.24% of diagonal
volume check   SKIPPED — 12mm³ below the 50mm³ floor
```

The third line matters most. A guard that silently does not run is the same
failure mode as every logging gap found on 2026-09-13/14: *never ran* and
*ran and found nothing* must not look identical afterwards.

