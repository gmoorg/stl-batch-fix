### Settled — the split moves from recovery to planning

**Decided in discussion.** Detection runs upfront on every file; the split
happens when detection calls for it, not after a repair has already failed.

**The reasoning that changed my objection.** I argued for split-on-failure on
runtime: 6 files of 768 ever needed it, and paying detection on all 768 looked
like doubling the run to help six. Two counters, both better than the argument
they answered:

- **Closed seam loops make the split unavoidable.** PyMeshFix deletes a region
  when regions disagree about winding, and that was learned the hard way. So
  split-on-failure does not avoid the split — it pays for a doomed PyMeshFix
  pass first, then splits anyway. Upfront removes the wasted pass, not work.
- **Detection makes the work orderable.** The pool already sorts by triangle
  count for memory admission. A file that needs splitting currently looks
  identical to one that does not until it fails; known upfront, it is
  schedulable rather than discovered.

And the cost is partly refunded: splitting lets debris shells be dropped rather
than repaired. On `whole-costume01` that is 443 of 444 shells under 100 faces —
work that was being spent on fragments nobody would print.

**The shape**: detect (`scan` + `winding_seams`, both cheap) -> split when
seams or multiple shells say so -> drop debris, recorded -> repair each part ->
merge. Volume-loss-then-seam-split stays as the backstop for what detection
misses.

