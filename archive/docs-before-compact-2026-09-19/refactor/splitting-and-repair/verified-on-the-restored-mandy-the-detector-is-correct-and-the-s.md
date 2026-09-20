### Verified on the restored Mandy — the detector is correct, and the seam is not the point

**Measured 2026-09-16** on the re-downloaded
`Mandy Dinamuuu3D/Complete model (Thingiverse version)/Mandy_Body_Dinamuuu3D.stl`,
with `libs/scanner` and `libs/decimator` rather than from logs.

**The new tools reproduce the recorded measurements exactly.** After decimation
to 900,000 faces via fast_simplification:

| | measured now | design doc |
|---|---|---|
| seam edges | 5 | 5 |
| closed loops | 0 | 0 |
| shells | 39 | 39 |
| smallest shell | 750 faces | 750 |

Every figure agrees, the smallest shell to the face. So the rewritten scanner
matches the old byte-level implementation on the mesh that motivated the whole
seam-split mechanism, and the D18 self-edge fix did not disturb a genuine
reading — Mandy carries only 10 degenerate faces, so it was never contaminated.

**Answering the question directly: the detection mechanism is not wrong.**

| mesh state | seam edges | closed loops |
|---|---|---|
| raw source (2,061,994 faces) | 7 | **1** |
| after decimation to 900k | 5 | **0** |

**Decimation destroys the loop.** Quadric edge collapse removes two of the
seven seam edges and the ring opens. The signal genuinely exists in the raw
mesh and is genuinely gone by the time the pipeline could act on it — so the
user's intuition that "the model should have a seam, otherwise it would not
lose the hair so cleanly" was right about the model, and the pipeline simply
never sees it. Under D13 decimation must come first (the face budget is spent
once, before anything is divided), so this is not a reordering that can be
undone.

Full raw-source figures, for reference: 266 non-manifold, 84 open, 10
degenerate, 39 shells, bbox 53.7 x 38.0 x 86.4 mm.

#### The finding that matters more: the head is already its own shell

Shell sizes after decimation begin: **562,288 · 121,537 · 63,073 · 60,932 ·
14,324 …**

The design doc records PyMeshFix returning *"one shell of 394,432 faces with
the head — a 121,537-face component — deleted"*. That second shell **is** the
head, and the measurement names it exactly.

**So the head was never fused to the body.** A plain connected-component split
— no seam logic whatsoever — separates it before PyMeshFix can see the whole
mesh. The doc's own conclusion agrees: what fixed that model was splitting
*after* decimation, not seam detection.

**This sharpens the re-opened split-upfront decision.** Seam detection is not
what saves Mandy; **shell splitting after decimation** is, and that is already
settled as D13. Seam detection earns its keep only where two regions are
genuinely fused into a single shell — hair over a scalp, a cuff over a neck.
On this collection that is `Amidara_Blustmorn_1-12_base.stl` (922 edges / 129
loops) and `Princess_Leia .../Neck_Cuff.stl` (83 / 27), not Mandy.

**Still open**: whether even those two need the seam path, or whether a shell
split handles them as well. Worth checking before `repairer` is built around a
mechanism that may have no remaining use case.


