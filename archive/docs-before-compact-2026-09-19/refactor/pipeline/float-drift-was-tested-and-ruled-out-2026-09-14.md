### Float drift was tested and ruled out (2026-09-14)

**Do not re-propose quantised vertex welding.** Reading
`_weld_binary_stl`'s bit-sort comments naturally suggests it — sort on
coordinates rounded to 4 decimals and cast to int64, rather than on raw float
bits. It would appear to fix float drift, fold `-0.0` for free, save memory via
a packed key, and flatten coplanar faces. Measured on `Millenium_Falcon.stl`
(393 k tris) and `whole-costume01.stl` (2.0 M tris), all four claims fail:

```text
Falcon           exact 48,745 verts, 0 degenerate
                 4/5/6 decimals: +0 merges, +0 degenerate

whole-costume01  exact 999,976 verts, 1 degenerate
                 4 decimals: +29 merges, +37 degenerate
                 5 decimals:  +1 merge,   +2 degenerate
                 6 decimals:  +0 merges,  +0 degenerate
```

- **Drift merging**: 29 extra merges in 6 M vertex slots — 0.0029% — on the
  messiest model in the collection, and none at all on the Falcon. The 24.2x
  duplication in the Falcon is the STL format writing identical bits
  repeatedly, not drift.
- **Flatness**: degenerate faces went *up*, not down. Geometry never moved in
  this test (sort-key-only variant); collapsing two vertices 0.1 µm apart onto
  one index turns a sliver into a zero-area triangle.
- **Memory**: the packed-int64 win needs all three axes in 63 bits. The Falcon
  spans 155 mm and needs 22 bits/axis — 66 > 63, so it does not pack, leaving
  three int64 columns at 2x the current key size.
- **`-0.0`**: genuinely free, but already handled in one line.

The probe is `design/quantweld.py` (read-only, self-contained; run it against
any mesh to re-check). Float drift is not a problem this collection has.

