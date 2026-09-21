# Mandy: what the volume guard is catching

Measured 2026-09-21 against the current tree, on
`Mandy Dinamuuu3D/Complete model (Thingiverse version)/Mandy_Body_Dinamuuu3D.stl`
(2,061,994 faces, 40 shells, 84 open / 266 non-manifold edges).

This file records findings only. Nothing here has been implemented, and the
pipeline's steps and their order are unchanged.

## The verdict

| run | volume kept | verdict |
|---|---|---|
| `mandy_ours_decimated.stl`, the 2026-09-17 input | **46.27%** | DESTROYED |
| full pipeline from source, decimated to 900k | **85.82%** | DESTROYED |

The 46.27% figure reproduces to two decimal places against the measurement
recorded in the archive, so nothing since has changed that path.

## It is real loss, confirmed by eye

The owner inspected `mandy_pipeline_inspect.stl` and identified missing
geometry. The model is bent double: legs straight, torso folded forward, head
upside-down near mid-height, hair hanging toward the floor. An earlier reading
of the damaged Z band as "legs and hips" assumed an upright pose and was wrong.

`libs.meshfix` removes **171,064 faces**, of which **170,610 are one connected
object** — 99.7%. It sits at 13% of model height, spans 32% of it, and is
confined to one side in Y. That is the hanging hair.

In the damaged band the outermost geometry is what goes: maximum radius from
the model axis falls from **24.68 to 15.85**, p90 from 16.23 to 13.50. An
interior wall being removed would raise the median and leave the maximum alone;
this is outer surface.

PyMeshFix says why on stderr:

    WARNING- forceNormalConsistence: Basic_TMesh was not orientable.
             Cut performed.

## What separates the hair from the body

Nothing in `archive/` describes this. The 2026-09-16 note concludes the
opposite — that seam detection is not what helps Mandy, and names
`Amidara_Blustmorn_1-12_base.stl` and `Princess_Leia .../Neck_Cuff.stl` as the
hair-over-scalp cases instead.

Measured on the removed object itself:

- It touches the body at **277 vertices of 85,617 — 0.32%**.
- That count is **identical at every tolerance from 1e-6 to 1e-3**, so those
  vertices are exactly shared rather than merely close. The other 99.68% of the
  hair is geometrically separate.
- The hair arrives already broken: **open=312, nm=67, 30 open boundary loops**,
  48 seam edges and **0 closed seam loops**.

So a boundary exists and is measurable. It is a hairline attachment, not a
winding seam, which is why no current mechanism finds it.

## Mechanisms ruled out by measurement

| hypothesis | result |
|---|---|
| shell splitting fixed it | No. 46.27% unchanged on the same input. |
| seam splitting would fix it | No. Cutting part 0 on all 52 seam edges yields 562,246 faces plus three single triangles. |
| decimation destroyed the seam loop | No. The raw source has the loop (7 edges / 1 loop; 5/1 on the largest shell) and `by_seams` still returns one piece of 941,571 faces. |
| it is the hair, so lower `MIN_SHELL_FACES` | No. There is no second region above any floor; the seam isolates three faces. |

Previously recorded as tried and insufficient: `fill_holes=False` (42.4%),
`by_geometry` first (42.5%), `re_orient_faces_coherently` (raises),
`meshing_repair_non_manifold_edges` (84.8%, then plateaus).

## Why this matters to the volume guard

The guard is correct here. It is the only check that notices: the repaired
result scores `open=0, nm=0` with all 39 components present, so every
topological test calls a model missing its hair perfectly clean. This is the
case D27 built it for.

The owner's separate concern is also real, and is a different event. Three of
sixteen probes are watertight, correctly repaired results that the guard
rejects:

| fixture | kept | where the volume goes |
|---|---|---|
| `sphere_doubles` | 50.00% | **CLEAN** — a coincident duplicate is merged |
| `sphere_allbad` | 49.98% | **CLEAN** |
| `opposite_volume_shells` | 50.00% | **SPLIT** — a component is dropped (A02) |
| Mandy | 85.82% | **inside PyMeshFix** |

Across every probe, no part loses volume inside the part tool: all measure
99.94–100%. Mandy's part 0 is the only one that does, at 84.83%.

So *where* a repair loses volume separates a legitimate duplicate removal from
real destruction, using measurements `Result.steps` already records. That is a
change to what `_decide` reads, not to the pipeline's stages — but it is
unimplemented and undecided.

D27 knew about the duplicate case and accepted it: "the safe direction to be
wrong in, and no real model in the collection has the defect."

## Still open

- Mandy needs a repair, not a better verdict. The hair is non-orientable and
  welded to the body at 277 vertices; nothing in the current sequence addresses
  that. **The owner has ruled out branching to Blender instead of PyMeshFix**,
  so a fix has to work within the existing steps.
- Whether the 0.90 threshold is right is untested at scale. The measured spread
  is wide — sound repairs at 95–100%, Mandy at 85.82%, duplicates at ~50% — so
  no single threshold separates Mandy from the duplicate cases.
