# Volume-loss rejection (`DESTROYED`)

## Symptom

The repair finishes, but the judge rejects it because too little volume was
kept (`repair destroyed geometry: N% of volume kept`). The source is copied
byte for byte to a `.destroyed.stl` marker (`processor.py`), and no repaired
output is written. The marker retires the job: to retry after a fix, delete
it first.

Both volumes are `scanner.component_volume` (sum of per-shell magnitudes):
`scan_volume_in` is taken on the whole model before the split, and
`scan_volume_out` after the merge.

There are two different causes.

## Cause A: support struts dropped as debris (verified on one file)

Presupported `_SUP` models consist of the figure plus hundreds or
thousands of small support shells. `split_shells` drops every shell under
`min_shell_faces = 100` as debris. The support struts are 80-face shells, so
they're all dropped, and their volume counts as lost.

Measured on `boot_left_SUP_STL.stl` (read-only probe over `scanner.shells`):

| | Value |
|---|---|
| Shells in input | 1,441 |
| Shells < 100 faces | 1,165 (49,740 faces) |
| Volume in those shells | 14,982 |
| `scan_volume_in` | 31,358 |
| `scan_volume_out` | 16,375 |
| in − out | 14,983 |

The dropped small shells account for the whole loss. All 276 surviving
parts passed the clean gate with no repair step run.

Files, all under `CA3D/Demon Queen/1-6 scale pres supports Demon Queen CA3D/STL/`:

| File | Volume kept | Faces in → out | Parts kept |
|---|---|---|---|
| Queen/boot_left_SUP_STL.stl | 52.2% | 787,966 → 738,226 | 276 |
| Queen/boot_right_SUP_STL.stl | 54.2% | 778,768 → 733,042 | |
| Queen/head_girl_SUP.stl | 53.4% | 878,906 → 770,058 | |
| Queen/arm_right_SUP_STL.stl | 59.0% | 819,492 → 800,900 | |
| Queen/arm_left_SUP_STL.stl | 60.9% | 733,590 → 714,472 | |
| Queen/hand_left_SUP.stl | 62.5% | 723,212 → 708,892 | |
| Dragon/dragon_head_1_SUP_STL.stl | 53.2% | 617,406 → 505,160 | |
| Dragon/dragon_head_2_SUP_STL.stl | 46.8% | 672,014 → 520,416 | 845 |
| Dragon/dragon_horn_right_SUP_STL.stl | 58.6% | 900,000 → 858,090 | |
| Dragon/wing_botton_left_SUP.stl | 55.0% | 680,216 → 477,396 | |

Found after the error snapshot: `Dragon/wing_broken_SUP.stl`, 61.6%,
690,872 → 659,208 (not measured).

Only `boot_left` was measured. The other nine are assumed to be the same
cause because they're the same kind of model with the same pattern. That is
**not individually verified**.

Open question: should a presupported model keep its supports? Keeping them
means the debris threshold is wrong for this input. Dropping them on purpose
means the judge should not count them as loss. The other option is to treat
presupported files as out of scope.

## Cause B: input volume is meaningless for open shells (verified 2026-10-04)

| File | Volume kept | Faces in → out | Parts |
|---|---|---|---|
| Exclusive - Cleopatra/arm2.stl | 61.1% | 191,918 → 185,852 | 4 |
| Exclusive - Cleopatra/fabric3.stl | 78.4% | 265,033 → 265,032 | 2 |

The rejection metric is broken here (below); whether the repaired shape
lost anything is unchecked. Not far-away debris. `arm2` has 45 shells: 4 real parts
plus 41 specks of 2–4 faces with < 6 units of |volume| in total. `fabric3`
has 2 shells. The real parts are **open** (hundreds to over a thousand open
edges each). For an open surface, the divergence-theorem volume that
`scan_volume_in` sums (`scanner.component_volume`) isn't a volume. It can
exceed the shell's bounding box, and it changes when the model is moved:

| Shell | Open edges | Bbox volume | "Volume" at origin | +100 mm in x | −100 mm in z |
|---|---|---|---|---|---|
| arm2 #1 | 1,090 | 14,300 | 1,589.0 | 2,742.4 | 3,687.9 |
| arm2 #2 | 556 | 8,870 | −2,075.3 | −2,545.8 | 138.6 |
| arm2 #3 | 677 | 507 | 1,919.0 (> bbox) | 1,736.0 | 793.1 |
| arm2 #4 | 1,623 | 590 | 4,887.6 (> bbox) | 4,377.6 | 1,740.8 |
| fabric3 #1 | 356 | 69,800 | 10,228.1 | 11,011.4 | 10,373.1 |
| fabric3 #2 | 574 | 351 | −1,235.6 (> bbox) | −2,018.9 | −1,380.6 |

Repair outputs closed solids, so `scan_volume_out` is real. The guard divides
a real output volume by an arbitrary input number and rejects the model as
`DESTROYED`. **Only the source copy is written, though the repair may be fine.** The
same arbitrariness can also *hide* a real loss on other open inputs (an
input number that happens to be small). Which way it goes depends on where
the model sits relative to the origin. This is the class of mistake modules.md
already warns about ("Never infer shape preservation from … summed signed
volumes").

Both tables in this section come from
[tools/experiments/shell_report.py](../../tools/experiments/shell_report.py)
(per-shell volumes, origin shift, boundary coincidence).

### The shells are pieces of one surface (verified 2026-10-04)

Both models were split by `split_shells` (`arm2`: 4 parts kept, 41 specks
dropped; `fabric3`: 2 parts). The open shells' **boundaries coincide**:
vertices along the seams are thousandths of a millimetre apart but not
bit-identical. So the exact weld in `mesh_io.load` doesn't join them, and the
shell split treats them as separate objects.

Nearest boundary vertex of the other shell (KD-tree over open-edge vertices):

| Pair | Within 0.01 mm | Within 0.1 mm | Median gap |
|---|---|---|---|
| fabric3 #1 → #2 / #2 → #1 | 90% / 59% | 100% / 98% | 0.0002 / 0.005 mm |
| arm2 #3 → #4 | 66% | **100%** | 0.006 mm |
| arm2 #1 → #4 / #4 → #1 | 68% / 48% | 78% / 65% | 0.002 / 0.013 mm |
| arm2 #2 → any other | 0% | ≤ 1% | 14–56 mm |

This suggests `fabric3` is one surface cut into 2 shells, and that in `arm2`
shells 1, 3, 4 are one surface stitched along seams, with shell 2 a separate
open part. It doesn't prove it: close facing walls or intended gaps would
measure the same. Grouping would need matching seam curves and compatible
orientation.

Consequence: each piece goes to winding as its own part, so winding closes
each piece's open seam independently. The result is separate solids sealed
along the cut lines (possible caps or slabs at the seams) instead of one
solid. So the split may really change the shape, even though the volume
guard flagged it for a meaningless reason. Not yet checked on the actual
output (needs a run plus a surface-distance check or visual inspection).

Winding doesn't need connectivity (the winding number works on unconnected
triangles). Rebuilt together, the pieces might form one solid across the
seams. Untested: `winding` orients each connected patch separately
(`bfs_orient` / `orient_outward`), so open patches may not get compatible
signs.

Fix directions for the split (not implemented, not tested):

- Group shells whose open boundaries coincide (tolerance tied to the grid
  spacing h) into one part before winding.
- Or weld near-coincident boundary vertices before splitting. Caution:
  tolerance merging (`merge_close`) was measured harmful on Amidara base.

Fix directions for the guard (not implemented, not tested):

- Measure input volume with the **winding number** (inside = winding > 0.5,
  the same rule winding uses to build the solid), which is well defined for
  open input, instead of the divergence sum. Not an independent check: it
  shares winding's inside rule, so it can't see losses that rule causes, and
  it reads open sheets as empty. Keep the surface-distance check alongside.
- Apply the volume guard only to **closed** input shells. For open input,
  judge shape preservation by original-to-output surface distance
  (`tools/experiments/wnmc_band.py --check` computes one).
