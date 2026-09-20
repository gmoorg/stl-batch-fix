### Measured — `scanner.shells()` is too slow, and that is my bug

Timed on surviving collection meshes, load versus shell count:

| triangles | load | shell count | count as share of load |
|---|---|---|---|
| 309,555 | 0.20s | 2.03s | 1015% |
| 2,061,994 | 1.91s | 12.51s | 657% |
| 5,081,319 | 6.42s | 33.86s | 527% |

**Counting shells costs 5-10x the entire load.** I had claimed in discussion
that counting was "nearly free" now that it no longer goes through PyMeshLab's
file-writing split — reasoning from "it is just union-find" without measuring.
Wrong by an order of magnitude.

The synthetic curve shows it is linear, about 2x `scan()`, so it is not
pathological — just a Python-level `find()` loop over every face, which is the
same class of thing `_build_edge_counts` was criticised for and which `scan()`
avoids by being vectorised. **`shells()` needs a vectorised rewrite before any
ordering decision is argued from its numbers**; a proper implementation should
be near `scan()`, order 1s on a 2M mesh rather than 12.5s.

One real finding survives regardless: `Mandy_Body_Dinamuuu3D.stl` has **39
shells, all 39 above the 100-face floor** — matching the design doc's note
exactly, so the floor behaves as recorded on real data.

