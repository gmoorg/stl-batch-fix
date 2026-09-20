### Pending verification — two models the user will restore

`Mandy_Body_Dinamuuu3D.stl` and `whole-costume01.stl` are to be restored from
source and used as known-behaviour test cases, rather than trusting numbers
from meshes nobody has inspected. Expectations to check against, recorded now
so they are not reconstructed from memory:

**Mandy** — 39 real shells, smallest 750 faces after decimation (the
measurement `_MIN_SHELL_FACES = 100` was calibrated against). On seams the doc
records **5 edges / 0 loops straight from decimation, 40 edges / 7 loops after
Blender**; this scan found **7 edges / 1 loop undecimated**. Three states,
three answers.

**The question that matters**, and it may undercut a decision already taken:
the split-upfront design assumes detection fires *before* repair. If Mandy's
closed loops only appear once Blender has rebuilt the surface — which is what
the doc's 0-loops-after-decimation figure suggests — then upfront seam
detection will not catch the very case the seam split exists for, and the
volume-loss backstop is doing the real work. Worth settling on the restored
file before `repairer` is built around the assumption.

**whole-costume01** — 444 shells, 443 of them under 100 faces. From the
surviving `~parts` debris: 892,445 faces after decimation, 2,055 nm, 9 open,
and two split parts that repair reduced to 1 and 16 faces.

