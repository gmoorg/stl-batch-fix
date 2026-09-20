### Discussion point — measured part data, and what it says about the drop rule

**Recorded 2026-09-16 for a later conversation. Nothing decided.**

The nm-ratio idea above was recorded as untestable because dropping is silent
and the logs hold nothing. Three orphaned `~parts` folders survived interrupted
runs, so a small amount of real per-part data does exist after all. Scanned
with `libs/scanner`:

| part | faces | nm% | bbox mm | what happened |
|---|---|---|---|---|
| Falcon part.0 | 90,712 | 0.0% | 117 x 36 x 155 | committed |
| Falcon part.1 | 156 | 0.0% | 5.7 x 5.8 x 3.2 | committed |
| Falcon part.3 | 38 | 0.0% | 0.72 x 0.52 x 3.08 | committed |
| Falcon part.4 | 36 | 0.0% | 9.3 x 1.0 x 4.2 | committed |
| Falcon part.2 | 160 | **12.5%** | 7.3 x 1.0 x 6.5 | **broken** |
| costume part.1 (original) | 250 | **9.7%** | 1.5 x 0.8 x 1.0 | repaired to **1 face** |
| costume part.2 (original) | 136 | 1.2% | 4.8 x 1.8 x 0.9 | repaired to 16 faces |

nm% is against total edge slots (3 x faces).

**What the numbers support.** nm% predicts *repair failure* cleanly on this
sample: the three parts with meaningful nm (12.5%, 9.7%, 1.2%) are exactly the
three that broke or were annihilated, and every 0.0% part committed. That is a
real signal.

**What they do not support — and this is the point to revisit.** nm% is not a
*debris* test:

- **Falcon parts 3 and 4 are 36 and 38 faces with 0.0% nm.** Both are below the
  100-face floor and would be dropped as debris today, yet both are
  topologically perfect and both committed. The nm test would correctly defend
  them.
- **But part 3's bbox is 0.72 x 0.52 x 3.08 mm** — a 3 mm sliver half a
  millimetre thick. Locating pin, or splinter? Clean topology cannot say. It is
  clean either way.
- **costume part.1 is 250 faces — above the floor — at 9.7% nm in a 1.5 mm
  box.** Debris by any reading, and the face floor would have *kept* it.

So the face floor and the nm ratio disagree in **both** directions on this
sample, which is more informative than agreement would have been.

**The shape this suggests**, to be argued later rather than assumed now:

- **nm% ~ "will repair survive this part?"** — possibly better used to *route* a
  part (repair it differently, or not at all) than to discard it.
- **bounding box ~ "is this worth printing?"** — the actual debris question, and
  the one the face count was always standing in for.

Falcon part 3 is the case that decides the design: clean topology does not make
a 0.5 mm-thick sliver worth printing, and a face count does not know its size.

**Caveats, stated so the table is not over-read.** Seven distinct parts from two
models — this suggests, it does not settle. And the sample is biased: these
parts survived *because* their runs failed. Parts that merged cleanly were
deleted by `_cleanup_parts`, so the data is drawn from the trouble cases.

**Related, unrelated to the drop rule**: those three `~parts` folders are todo
M4 sitting on disk — pipeline intermediates left behind when a run is killed
mid-file, 89 MB in the belly-dancer folder alone. `_SIGNAL_SUFFIXES` already
names them as such. The startup-sweep idea recorded for Blender scratch would
cover these too.

