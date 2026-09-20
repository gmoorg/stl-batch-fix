### The fused seam on Leia is decimation, not repair

The user noticed the Leia output looked *"slightly distorted — two touching
parts have a fused line between them, like hip and leg stitched together rather
than being clear body parts."* Traced on `Lower_Body.stl` (12.5 x 6.6 x 7.7 mm):

```text
decimate       5,136,808 -> 899,999   (5.7x, flagged in review_decimated.tsv)
fill holes       899,999 ->  899,859   -140
drop fragments   899,859 ->  899,648   -211
clean            899,648 ->  899,374   -274
```

**PyMeshFix is not the cause**, which was the first hypothesis and it was
wrong: every sub-step *removed* faces. A stitched web between two surfaces
would show the count going up. It closed 96 open edges by deleting boundary
geometry, not by spanning a gap. Blender never ran on any Leia part at all, so
`MERGE_DIST` was never applied either.

**Decimation is what produces the fused seam.** Quadric edge collapse optimises
silhouette error, and a crease where a hip and a leg touch barely changes the
silhouette — so it is cheap to collapse across, and QEC does. At 5.7x on a part
whose features are already ~85x below the layer height, that is the visible
result.

**The same limit lands very differently by scale.** `MAX_FACES = 900_000`
applies identically to a 12.5 mm hip and a 200 mm Falcon. The Falcon, at 393 k
triangles, was never decimated at all; Leia's `Lower_Body` lost 82% of its
geometry to reach the same ceiling.

That reads like an argument for a resolution-derived target — decimate only
until features reach printable size — and it is wrong. See the next section:
`MAX_FACES` is Bambu's complexity threshold, and models are scaled to printable
size *after* repair, so a target computed from `min_feature` at authoring scale
is wrong by the scale factor. **The fused seam is a trade-off of reaching 900 k
at all, not a badly chosen number.**

**Also exposed by this trace:** `Lower_Body.stl` scanned as `nm=-1 open=-1` —
over the 2 M limit, so never measured. The `nm 134 -> 0, open 96 -> 0` recorded
against it are **post-decimation** counts. Those 134 non-manifold edges may be
decimation artifacts rather than source defects, and nothing in the current
pipeline can tell the difference.

