# Winding-number reconstruction (experiment)

A candidate replacement for alpha-wrap: rebuild each model as a solid from a
signed-distance field whose sign comes from the generalized winding number,
then extract the surface with marching cubes on one global grid processed in
blocks. Not wired into the pipeline. Script:
[`tools/experiments/wnmc_band.py`](../../tools/experiments/wnmc_band.py).
Block size is an open parameter; nothing here fixes it.

## Why

Alpha-wrap's memory and time follow the surface it wraps
(≈ 1.39 · A / α² output faces, ≈ 1.6–2.0 KB · A / α² peak; see
[TODO](TODO.md#runner-and-concurrency)), so one large model can need many GB.
It cannot be split: wrapping spatial regions separately produced a hollow
skin (2 % of the volume), because each region holds only an open patch of the
surface and "which side is inside" is lost by cutting (experiment recorded in
the same TODO item). This method decides inside/outside from the WHOLE model
first, so blocks can be processed independently and still form a solid.

## Algorithm

1. **Grid.** Spacing `h` (mm) over the model's bounding box padded by `2h`.
   Split into `B × B × B` blocks along grid indices; neighbouring blocks
   share their boundary layer of grid points.
2. **Surface samples.** Every vertex plus area-proportional random points
   (≥ 4 per `h²`), converted to their nearest grid index — no surface patch is
   farther than about `h/2` from a marked cell.
3. **Band.** Per block: mark the sample cells (from samples within the block
   grown by `BAND`), dilate by `BAND = 5` cells.
4. **Field in the band.** Exact signed distance to the original surface,
   sign from the fast winding number (`igl.signed_distance` with
   `SIGNED_DISTANCE_TYPE_FAST_WINDING_NUMBER`). Inside is negative.
5. **Field elsewhere.** Only the sign matters: winding number on a coarse
   lattice (every `K = 4`-th grid point), threshold 0.5; each far point takes
   the sign of its nearest lattice point, value `±(BAND + 1) · h`. A far point
   is more than `BAND` cells from the surface and its lattice point at most
   `K/2 · √3 ≈ 3.5` cells away, so both are on the same side.
6. **Surface.** `igl.marching_cubes` at level 0 per block (grid points
   ordered x-fastest).
7. **Weld.** Concatenate blocks, merge vertices with equal coordinates
   (quantized to `h · 1e-4`), drop faces that collapsed.

Seams need no stitching: a vertex on a shared block face is interpolated from
the same two grid values in both blocks. Marching cubes reads only cells the
surface crosses, whose corners lie in the band, so the far-field shortcut
cannot change the output.

## Verified

- Thin-band output is **triangle-for-triangle identical** to computing the
  exact signed distance at every grid point, on Mirko at 0.5 mm and 0.15 mm.
- 1 block and 27 blocks give identical output (sphere; Mirko 0.5 mm).
- Output is closed: 0 open, 0 non-manifold edges in every run.
- Owner's visual review of Mirko at 0.15 mm (2026-10-03): no visible quality
  loss against the original.
- Decimating that output to the original 300,002 faces (one pass reached
  the target) left 4 non-manifold edges; MeshFix cleaned them (−82 faces,
  5 s). The pipeline's conditional post-decimation MeshFix covers this.
  Decimation adds a few such edges to any repaired surface — the owner saw
  the same with the online repair tool Bambu suggests.

## Measurements

Model `Mirko_BodySFW.stl`: 300,002 faces, already clean, area 23,721 mm²,
diagonal 155.5 mm, volume 137,077 mm³. Machine: 12 threads, 32 GB; libigl
uses all threads. Times are reconstruction only unless noted.

| Run | Grid points | Blocks | Time | Peak | Faces out | Original → new (rms / max) |
|---|---|---|---|---|---|---|
| Sphere r=20, full field, 0.5 mm | 0.6 M | 1 / 27 | 0.9 / 1.1 s | 140 / 87 MB | 60,152 | 0.012 / 0.018 mm |
| Mirko, full field, 0.5 mm | 5.0 M | 1 | 12 s | 770 MB | 278,384 | 0.041 / 1.09 mm |
| Mirko, full field, 0.5 mm | 5.0 M | 27 | 25 s | 275 MB | 278,384 | same |
| Mirko, full field, 0.15 mm | 171 M | 27 | 324 s incl. checks | 1,617 MB incl. checks | 3,110,482 | 0.010 / 0.41 mm |
| Mirko, thin band, 0.15 mm | 171 M | 27 | **66 s** | 1,872 MB | 3,110,482 | identical |
| Mirko, thin band, 0.06 mm | 2.6 G | 216 | 807 s | 7,512 MB | 19,467,152 | not measured |
| Alpha-wrap, α 0.15 / offset 0.06 | — | — | 175 s | 1,963 MB | 1,460,894 | — |

Volume kept: 99.98 % at 0.5 mm, 99.998 % at 0.15 mm. The 0.5 mm max error is
detail finer than the grid.

Cost of one million queries against Mirko: exact signed distance 6.6 s,
unsigned distance alone 7.4 s, winding number alone 1.3 s (sign agreement
100 %). That ratio is why only the band gets distances.

Per-phase time, thin band:

| Phase | 0.15 mm (27 blocks) | 0.06 mm (216 blocks) | Note |
|---|---|---|---|
| exact distance | 29 s | 169 s | the real geometric work; ~6× for 2.5× finer |
| band mask | 9 s | 250 s | every block scans all surface samples (26 M at 0.06) |
| far-point lattice | 9 s | 149 s | per-point arithmetic over all far points |
| winding number | 5 s | 47 s | |
| grid coordinates | 3 s | 49 s | |
| marching cubes | 3 s | 42 s | |
| other | ~8 s | ~101 s | |

An earlier version found far-point lattice indices with `np.unique` over all
far points: 195 of 255 s at 0.15 mm. Building each block's lattice directly
removed that with byte-identical output.

## Detailed, broken model: `join_complication.stl` (2026-10-03)

Several models joined into one STL by the owner: 3,799,673 faces, many small
details, 435 open / 28 non-manifold edges / 7 winding seams, area 18,383 mm²,
diagonal 123.7 mm, volume 27,406 mm³. Both methods at the alpha-wrap
settings this model gets (α 0.15, offset 0.06; grid `h` 0.15 mm, 27 blocks),
then the pipeline's post-steps: decimate to 900,000 (the config default),
decimate again if above, MeshFix if defects.

| | Winding number + MC | Alpha-wrap |
|---|---|---|
| Reconstruction time / peak | 290 s / 3,470 MB | 160 s / 3,585 MB |
| Faces out, defects | 1,991,844, clean | 1,019,524, clean |
| After decimation to 900k | 30 NM, 337 self-intersecting triangles | clean |
| MeshFix | −23,226 faces (2.6 %), 16 s → 876,774, clean | not needed |
| Volume | 27,416 (+0.04 %) | 28,471 (+3.9 %: offset × area) |
| Outer-skin vertices > 0.15 mm from final | 4.8 % (3.4 % before decimation) | 10.1 % |

**Owner's visual review:** the winding-number result looks slightly but
noticeably better; no visible damage in either.

Notes:

- New surface → original: rms 0.003, max 0.066 mm — nothing invented.
- "Outer skin" = original vertices with winding number < 0.75. The vertices
  both methods leave behind look like thin layers of faces (owner, viewing
  the exported point clouds): internal sheets of the joined, open shells, not
  visible detail. Only 69 of 1.84 M were kept by alpha-wrap but not by this
  method — it does not lose thin parts alpha-wrap keeps.
- Distance queries against a 3.8 M-face input dominate the time (199 of
  290 s), which makes this method slower than alpha-wrap here.
- Marching-cubes triangles decimate less cleanly than alpha-wrap's; MeshFix
  repairs it. Remeshing before decimation is untested.
- Alpha-wrap peaked at 3.6 GB against 1.5 GB predicted from area alone: the
  formula in TODO predicts output faces well (1.1 M vs 1.02 M) but needs a
  term for the input's own size.

## Block count (join_complication, 0.15 mm, 2026-10-03)

Same model and settings as above; only the block count changes. Post =
decimate to 900k, decimate again, MeshFix (separate process).

| Blocks | Reconstruction | Recon peak | Post | Post peak | NM after decimation | MeshFix removed | Final faces |
|---|---|---|---|---|---|---|---|
| 1 | **55.5 s** | 10,654 MB | 25.3 s | 1,311 MB | 39 | 23,312 | 876,688 |
| 8 | 127.5 s | 4,004 MB | 24.1 s | 1,286 MB | 34 | 23,260 | 876,740 |
| 27 | 290 s | 3,470 MB | — | — | 30 | 23,226 | 876,774 |
| 729 | 3,831 s (64 min) | 3,553 MB | 21.7 s | 1,301 MB | 30 | 23,228 | 876,772 |

- Reconstruction output: 1,991,844 faces for every block count.
- Time grows with block count because each block rebuilds the libraries'
  search structures over the whole 3.8 M-face input (distance 26.7 s at 1
  block, 1,994 s at 729; winding 2.7 s → 1,562 s), mostly single-threaded
  (~3.5 of 12 threads busy at 729).
- Memory falls from 10.7 GB (1 block) to ~3.5 GB and then stays: the output
  held until the final weld and the surface samples set the floor.
- Final face counts differ by up to 84 faces: block count changes triangle
  order, and decimation depends on order. Visual difference not yet checked.
- The post timings of 1 and 8 blocks ran while the 729-block run was using
  ~3.5 threads, so they are slightly inflated.

## Open

- **Block size** — not chosen. Fewer blocks are faster (less per-block
  overhead), more blocks bound per-block memory.
- **Memory does not yet fall with blocks.** Peak is dominated by holding all
  output pieces until the final weld plus all surface samples (7.5 GB at
  0.06 mm). Streaming each block's piece out and welding only shared seams
  would keep it near one block's size.
- **Band mask cost** — bucket the samples by block once instead of scanning
  them per block.
- **Grid spacing** — 0.15 mm matched alpha-wrap's quality on Mirko by eye;
  0.06 mm yields 65× more faces than the decimation target. Whether 0.06 mm
  is visible after decimation is untested.
- **Broken models** — one so far (`join_complication.stl`, above). More are
  needed: large holes (the 0.5 threshold) and features thinner than `h`.
- **Speed on large inputs** — distance queries scale with the input's face
  count; the band could be computed against a decimated copy of the input.
- **GPU** — the machine has an AMD RX 6700 XT; the ready-made GPU stacks
  (NVIDIA Warp, Kaolin) need CUDA. Taichi (Vulkan) or PyTorch-ROCm would
  mean implementing the fast winding number ourselves.
