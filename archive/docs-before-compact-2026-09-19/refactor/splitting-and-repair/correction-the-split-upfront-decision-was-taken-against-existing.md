### CORRECTION — the split-upfront decision was taken against existing evidence

**Recorded 2026-09-16. The decision above ("the split moves from recovery to
planning") is re-opened, not settled.**

I argued for it and recorded it without reading the design doc passage that
already covers the question. The old code tried exactly that design, measured
it, and disabled it. The branch is still in the source behind
`if False and _seam_loops > 0`, kept for the measurements in its comments.

**Why it was disabled** (`STL_BATCH_FIX_DESIGN.md`, "The pre-emptive seam split
is disabled — read this before re-enabling it"):

> On a sphere with its cap reversed — 40 seam edges in 1 closed loop —
> PyMeshFix re-winds it correctly and returns the same 760 faces, where
> splitting first gives 880 faces and introduces 2 non-manifold edges. The
> Mandy mesh that *needed* the split had **exactly 40 seam edges too**. Nothing
> measurable before step E distinguishes "PyMeshFix will fix this" from
> "PyMeshFix will delete this".

Two meshes, identical seam counts, opposite correct actions. That is the whole
argument, and it is an argument from measurement rather than from reasoning.

**So closed-loop detection cannot be the trigger for splitting.** It can say
"this mesh has irreconcilable winding somewhere"; it cannot say "PyMeshFix will
destroy this". The volume check after the fact can, because by then the damage
is a measured fact rather than a prediction.

**A second error, in how the Mandy numbers were presented.** Three readings
were tabulated as though they were the same mesh at different times:

| source | mesh state | reading |
|---|---|---|
| design doc | after decimation | 5 edges / 0 loops |
| design doc | after decimation **and Blender repair** | 40 edges / 7 loops |
| our scan | **raw 2,061,994-triangle source** | 7 edges / 1 loop |

They are three different meshes. The doc's line is explicit: *"straight from
decimation the Mandy mesh has 5 seam edges in 0 closed loops and cannot be
separated; after Blender's repair it has 40 in 7 loops and splits cleanly."*
Blender rebuilding the surface is what creates the explicit boundary —
established in `2c524d8`, and the doc says it, not `MAX_FACES`, is what fixed
that model.

**What still stands from the earlier discussion**: detection upfront is cheap
(under 2% of meshes carry closed loops) and knowing a file's shape before
queuing it has value for ordering. What does not stand is using that detection
to *trigger* a split.

**To settle on the restored model, with fresh tools rather than from logs**:
does a closed loop exist in the raw mesh that our detector sees but the old
byte-level one missed, or does the boundary genuinely only appear after
Blender? The preserved logs cannot answer it — both runs record Mandy as
`skip`, so neither processed it.

**The process failure worth keeping**: this is the second time in two days that
a confident argument ran ahead of reading what was already recorded — after
`shells()` being called "nearly free" without measuring. Both times the
correction came from the user asking a question I could not answer. The
standing rule from that: a claim about cost or behaviour ships with a number or
an admission that there is none. Extend it — **a decision that reverses
existing behaviour ships with the reason that behaviour exists.**

