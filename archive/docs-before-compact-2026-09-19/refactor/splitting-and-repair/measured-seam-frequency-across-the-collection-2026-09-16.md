### Measured — seam frequency across the collection (2026-09-16)

575 meshes scanned in 18 minutes (11 skipped as over 2.5M triangles, a test
bound only — the real pipeline decimates first, so it never sees a mesh above
`MAX_FACES`).

| | count | share |
|---|---|---|
| no seam edges at all | 513 | 89% |
| genuine seam edges | ~29 | 5% |
| phantom (self-edge bug, D18) | ~33 | 6% |
| **genuine, with a closed loop** | **~10** | **under 2%** |

**This answers the question left open above** — whether closed loops affect 6
files or 200, which was recorded as the thing that would decide whether the
splitting stage needs a different design. It is about 10 of 575. Splitting
stays a narrow path, and upfront detection is cheap insurance rather than a
major cost centre.

The genuine hits, ranked:

| edges / loops | mesh |
|---|---|
| 922 / 129 | `Amidara_Blustmorn_1-12_base.stl` — an order of magnitude beyond anything else |
| 117 / 18 | `Amidara_Blustmorn_1-12_hands_2.stl` |
| 83 / 27 | `Princess_Leia .../Neck_Cuff.stl` — a cuff over a neck, the hair-over-scalp shape exactly |
| 15 / 1 | `belly-dancer/3rd-01.stl` |
| 7 / 1 | `Mandy_Body_Dinamuuu3D.stl` |
| 3 / 2 | `Skeletor - STL/Base.stl` |

**Caveat, stated so the numbers are not over-quoted**: this scan ran *pre-fix*
code. The `edges == loops` rows are provably phantom (a loop needs at least
three edges), but a genuine row may also carry some self-edges inflating its
count — Amidara's 922 could be 900 after filtering. The ranking almost
certainly holds; the exact figures do not. A re-run settles it and costs 18
minutes.

Incidentally: the whole `Boris` tree — 185 assembly parts, cut from larger
assemblies — returned **zero** seams. The cutting was done cleanly.

