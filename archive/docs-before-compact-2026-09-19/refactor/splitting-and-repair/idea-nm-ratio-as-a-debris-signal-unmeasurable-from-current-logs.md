### Idea — nm ratio as a debris signal (unmeasurable from current logs)

**Raised 2026-09-16. A hypothesis with a clear test, not yet testable.**

**The idea**: distinguish debris from small-but-real parts by the *proportion*
of defective edges, not by face count. A legitimate part is a closed solid —
few non-manifold or open edges relative to its size. A fragment torn off a
sculpt is mostly boundary: a scrap of surface rather than an object, so its
defect ratio is high whatever its face count.

**Why it is worth pursuing.** It addresses the failure the existing floor is
known to risk. The comment at `stl_batch_fix.py:~895` records the worry
directly: a floor set too high "would silently drop a magnet peg, a locating
pin or a small accessory, which are parts, not debris." Those are small *and*
topologically clean. A speck is small *and* malformed. Face count cannot tell
them apart; a defect ratio can.

**It composes with the bbox idea rather than competing with it.** Bounding box
answers *too small to print*; nm ratio answers *not a solid object*. A magnet
peg is small with clean topology, a speck is small with bad topology — two
signals for two different failure modes, and a part should probably have to
fail both before being dropped.

**Why it cannot be checked against the collection logs.** Asked for, and the
data does not exist. Three independent reasons:

1. **Dropping is silent.** Both sites discard without logging — a bare
   `continue` in `split_at_seams` (line 1316) and a list filter in
   `split_shells` (~line 902). Neither takes `L`, neither writes a step row. A
   dropped part leaves no trace: not its face count, not its extent, not a
   defect count.
2. **Per-part defects are never measured before the drop.** The floor is
   applied to a boolean *face mask* over the parent mesh. Nothing scans it at
   that point. Scanning happens later, on parts that survived and were written
   to disk — so a dropped part's nm count was never computed by anything.
3. **The surviving sample is one part.** `~foot1.part.1.stl` is the only
   per-part mesh in the step log, across 78 rows of the same repeating cycle.

**This is the second reason for the "record what is dropped" note above**,
which was written for the volume guard's benefit. Recording a drop is not only
about stopping the volume check misreading a deliberate discard — it is the
only way the drop rule itself can ever be evaluated. A rule that discards
without recording cannot be checked against outcomes, only trusted.

**The test, whenever the split stage is built**: scan each part *before*
applying any floor, and record for every part — kept or dropped — its face
count, bounding box, nm count and open-edge count. One run over the collection
then answers whether the defect ratio separates specks from small real parts,
and where the threshold sits. Until that data exists this stays an idea.

