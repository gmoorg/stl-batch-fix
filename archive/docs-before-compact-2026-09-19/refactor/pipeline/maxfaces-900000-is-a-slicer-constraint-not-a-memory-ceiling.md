### `MAX_FACES = 900_000` is a slicer constraint, not a memory ceiling

Recorded because the source does not say so and the wrong reading is easy to
reach: Bambu Studio warns that a model is too complex above roughly this
triangle count, **regardless of the model's physical size**. That is the entire
reason for the number.

Two consequences follow, and both kill an otherwise attractive idea:

- **A resolution-derived target does not work.** Deriving the decimation target
  from `min_feature` vs `MIN_LAYER` — decimate only until features reach
  printable size — was proposed and rejected. The user scales models to
  printable size *after* repair, so any target computed at authoring scale is
  wrong by the scale factor. The constraint is on triangle count as such, which
  scaling does not change.
- **The fused seam is therefore a trade-off, not a bug.** Reaching 900 k from
  5.1 M means something must go, and QEC gives up creases first because a crease
  between two touching surfaces barely affects the silhouette. The only lever
  that does not require knowing the final scale is weighting creases more
  heavily *within the same budget* — a decimator quality setting, not a
  different target. Whether `fast_simplification` exposes such a weighting is
  unknown and worth checking before assuming it.

