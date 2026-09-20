### Four surface checks that do not work

`fin_pmf` scores perfectly on all of these and is visibly wrong:

| check | why it misses |
|---|---|
| nm / open / degenerate counts | all zero on the damaged mesh |
| volume | 99.96% — indistinguishable from rounding |
| seam counts | 0/0 |
| radius from the sphere | zero deviation, **and it runs backwards**: the clean `tjfix` output has the *most* off-sphere vertices (150, the T-junction midpoints on chords) |

**What does work: missing vertices, with the input as the reference.** A repair
that drops a vertex present in its own input removed something, and every
repair step has its input in hand — no control mesh needed.

```python
lost = {tuple(p) for p in before.geometry.verts.tolist()} - \
       {tuple(p) for p in after.geometry.verts.tolist()}
```

`fin_pmf` deletes 2 vertices at radius 10.000 — points on the sound surface.
That is the only check that catches it.

**It needs one refinement**: the rule is *lost a vertex belonging to the sound
surface*. A fin apex does not count — it hangs off a single non-manifold edge,
so removing the fin legitimately removes its tip. Candidate filters, untested:
ignore vertices that were non-manifold or on an open boundary in the input; or
compare only against vertices with a closed manifold fan.

