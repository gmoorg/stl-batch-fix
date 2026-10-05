# MeshFix timeout after post-wrap decimation

## Symptom

Winding succeeds with a clean result (`nm=0, open=0`). The post-wrap
decimation to 900k faces then introduces many non-manifold edges. The
following `meshfix` step is still running when the 3600 s
`per_file_timeout` kills the job (3,604 s in both cases), and the parent
writes a `.timeout.stl` marker. Whether MeshFix would ever finish is not
known.

## Files

| File | Winding out | Winding time | After decimate | NM after decimate | Last step started |
|---|---|---|---|---|---|
| Abe3D - Laura Kinney X-23/base.stl | 5,171,046 faces (blocks 2³) | 529 s | 899,998 | **6,968** | meshfix 19:01:55 |
| CA3D/Alloy/1-9 scale Aloy NSFW CA3D/1-9 scale Aloy NSFW CA3D/Base_part_1.stl | 1,402,300 faces | 42 s | 900,000 | 534 | meshfix 17:16:53 |

`batch.log` trail for `base.stl`:

```
winding   1/1  529.478  h=0.15, blocks=2^3, est=4.48 GB, 5171046 faces
decimate  1/1   95.124  meshlab, 899998 faces out
scan      1/1           nm=6968, open=0
meshfix   1/1           899998 faces in        <- no end line
```

## Observations

- Decimation adding a few NM edges is normal, and MeshFix normally clears
  them in seconds. Here the counts are hundreds to thousands, much higher.
- Both are heavy reductions: 5.2M to 0.9M (5.7×) for `base`, and 1.4M to 0.9M
  for `Base_part_1`. Whether reduction ratio predicts the NM count is
  **not measured**.
- Both models are bases. Large flat areas with sharp edges may decimate badly,
  but that is **unverified**.
- The whole job burns the full hour, blocking a worker.

## Where it times out (checked 2026-10-04)

Neither model was split into many parts: both had **1 shell part**. The time
goes into the single MeshFix call on that part: ~59 min for Aloy and ~49 min
for Laura, after winding (529 s for Laura) and decimation.

## Cause (Aloy reproduced 2026-10-04): decimation throws vertices off the surface

Rebuilt from the decimation cache with the batch's settings (h = 0.15, 1
block). Winding output is clean (1,402,300 faces, NM 0). The
post-reconstruction decimation to 900k then produces a broken mesh. A second
decimation with `optimalplacement=False` is a diagnostic only, not a
proposed setting:

| | Default decimation (batch) | `optimalplacement=False` |
|---|---|---|
| NM edges | **534** (474 shared by 4 faces, rest by 6–12) | 15 |
| Duplicate-face groups (same 3 vertices) | **286** (602 faces) | 5 |
| Vertices > 1·h from the winding surface | **292** | **0** |
| Furthest vertex from the surface | **285·h ≈ 43 mm** | 0 |
| Longest edge | 634·h ≈ 95 mm (model diagonal 121 mm) | 134·h ≈ 20 mm (on-surface, flat base) |

The distance rows are point-to-triangle distances to the winding surface
(`igl.point_mesh_squared_distance`, rechecked 2026-10-05). The first
measurement used nearest-vertex distance, an upper bound; the recheck gives
the same 292 vertices and the same 285.1·h maximum. Decimation ran
(`rung=meshlab`) at the pipeline's target, the part's own 900,000 faces.

417 of the 534 NM edges belong to duplicate faces. Optimal vertex placement
moves 292 vertices up to 43 mm off the surface, so spikes span the model,
with long slivers, duplicated faces and NM edges around them. MeshFix then
runs for an hour on it. With placement off, every vertex stays on the
surface and only 15 NM edges remain (the "a few NM after decimation is
normal" case).

**Suspected trigger, refuted (2026-10-05): the sliver clusters.** The
winding output holds a few hundred edges shorter than 0.001·h (284 on Aloy
before the weld fix, 306 after; 1,413 on Laura): the grid-node vertex
clusters behind [winding-non-manifold.md](winding-non-manifold.md). The
idea was that their near-singular quadrics send the "optimal" position
anywhere. Collapsing every such edge first (each group of short edges
merged to one vertex; output clean, NM 0) leaves the spikes as they were.

Re-test after the grid-edge weld, and the causal test
([tools/experiments/sliver_cause_probe.py](../../tools/experiments/sliver_cause_probe.py)):
same winding output per model, decimated to the pipeline's target four ways;
off-surface distance is point-to-triangle to the winding surface.

| Decimation | Aloy: NM / off > 1·h / furthest / time | Laura: NM / off > 1·h / furthest / time |
|---|---|---|
| defaults (batch) | 438 / 320 / 285·h / 9.0 s | 7,740 / 1,566 / 2,749·h (412 mm) / 75 s |
| defaults, short edges collapsed first | 428 / 314 / 285·h / 9.1 s | 7,780 / 1,519 / 2,747·h / 113 s |
| `planarquadric=True` | 0 / 0 / 0.1·h / 7.6 s | 1 / 0 / 0.1·h / 52 s |
| ~~`preservetopology=True`~~ (invalid, see below) | ~~0 / 0 / 0.1·h / 8.0 s~~ | ~~0 / 0 / 0.1·h / 57 s~~ |

One run each. So the slivers are not the trigger, and a field change to
remove them is not motivated. `planarquadric=True`, keeping optimal
placement, stops the spikes. What does trigger them is still not known.

**The `preservetopology` row is invalid: PyMeshLab keeps filter parameters
between calls.** All four decimations ran in one process, and a parameter
not passed keeps its value from the previous call of the same filter, even
on a new `MeshSet`. That row ran right after `planarquadric=True`, so it had
both flags. Checked on Aloy's winding output (2026-10-05): `preservetopology`
alone in a fresh process gives NM 1,068; after a `planarquadric` call, it
gives NM 0, and so does a following call with no flags at all. The other
rows are valid: nothing before them set a parameter.

This is also a pipeline risk: `decimator` passes only `targetfacenum`, so
it inherits whatever an earlier call in the same process set (another
step, a test, an experiment). Not yet fixed.

Time and memory per variant, each decimation in its own process from the
same post-fix winding output
([tools/experiments/placement_memory_probe.py](../../tools/experiments/placement_memory_probe.py)
`decimate IN.npz VARIANT OUT.npz`; peak = VmHWM):

| Model | Variant | Time | Peak | RSS after | NM | Off > 1·h |
|---|---|---|---|---|---|---|
| Aloy | defaults (batch) | 9.5 s | 0.68 GiB | 0.40 GiB | 438 | 320 |
| | `optimalplacement=False` | 13.5 s | 1.30 GiB | 0.92 GiB | 19 | 0 |
| | `planarquadric=True` | 8.0 s | 0.73 GiB | 0.45 GiB | 0 | 0 |
| | `preservetopology=True` | 10.0 s | 0.68 GiB | 0.40 GiB | 1,068 | not measured |
| Laura | defaults (batch) | 75.7 s | 2.51 GiB | 1.39 GiB | 7,740 | 1,566 |
| | `optimalplacement=False` | 75.8 s | 4.65 GiB | 3.05 GiB | 7 | 0 |
| | `planarquadric=True` | 54.3 s | 2.49 GiB | 1.38 GiB | 1 | 0 |
| | `preservetopology=True` | 190.0 s | 2.51 GiB | 1.40 GiB | 13,198 | not measured |

Off-surface counts come from the four-variant table above (valid rows
only). Not yet measured for `planarquadric`: accuracy on clean surfaces
(rod check) and other models.

**Possible silent damage:** a flung vertex is a spike in the printed part.
Models where MeshFix finished in time may carry such spikes. Nothing in the
pipeline checks for vertices leaving the surface (the volume guard wouldn't
notice a thin spike). How many processed outputs are affected is unknown.

Decimation settings are the owner's decision (PyMeshLab defaults for both
passes, see the decimator decision). `optimalplacement=False` was measured
only to identify the cause.

Thin-feature check of `optimalplacement=False` (2026-10-04, the
[reconstruction.md](../refactor/reconstruction.md) rod method;
[tools/experiments/placement_rod_check.py](../../tools/experiments/placement_rod_check.py)):
`sphere_with_rod` rebuilt at h = 0.092 (452,648 faces), decimated back to
840 faces.

| Setting | Rod tip | p99 | max | Vertices off rebuilt surface | NM / open |
|---|---|---|---|---|---|
| defaults (`optimalplacement=True`) | 0.004 | 0.014 | 0.101 | 0.044 | 0 / 0 |
| `optimalplacement=False` | 0.009 | 0.060 | 0.128 | **0.000** | 0 / 0 |

Both keep the rod (2·h limit = 0.184). With placement off, accuracy is
somewhat worse (p99 ×4, max ×1.25, in line with the existing "original
vertices kept" row: 0.004 / 0.055 / 0.174), but no vertex leaves the surface.
The icosphere check (20,480 → 1,000) is uninformative for placement off:
every kept vertex is an original one on the sphere, so the vertex-radius test
passes trivially (extent ratio 1.0031 vs 1.0022 with defaults).

Rerun after the grid-edge weld, with `planarquadric` (2026-10-05; each
variant in its own process, `placement_rod_check.py VARIANT`). The rebuilt
rod now has 452,652 faces, so the defaults' numbers moved slightly:

| Setting | Rod tip | p99 | max | Vertices off rebuilt surface | NM / open | Icosphere radius dev / extent ratio |
|---|---|---|---|---|---|---|
| defaults | 0.005 | 0.023 | 0.116 | 0.048 | 1 / 0 | 0.453% / 1.0022 |
| `optimalplacement=False` | 0.004 | 0.065 | 0.158 | 0.000 | 1 / 0 | 0.000% / 1.0031 |
| `planarquadric=True` | 0.004 | 0.022 | 0.094 | 0.075 | 0 / 0 | 0.489% / 1.0016 |

All keep the rod. `planarquadric` is as accurate as the defaults (same
p99, lower max) and leaves no NM edge; its 0.075 off the rebuilt surface is
ordinary optimal placement, within the 0.184 limit, not a thrown vertex.

Speed and memory of PyMeshLab decimation with the flag on and off
(2026-10-05,
[tools/experiments/placement_memory_probe.py](../../tools/experiments/placement_memory_probe.py)):
each model rebuilt once with winding, then decimated in a separate process
per setting from the same output, to the pipeline's target. Peak is the
process VmHWM (GiB), about 0.15–0.26 GiB of it baseline before decimation.

| Model (reduction) | `optimalplacement` | Time | Peak | RSS after | NM edges |
|---|---|---|---|---|---|
| Aloy (1.40M → 0.90M) | on (default) | 9.1–9.5 s | 0.68 | 0.40 | 534 |
| | off | 13.6–13.8 s | 1.29 | 0.91 | 15 |
| Laura (5.17M → 0.90M) | on (default) | 83 s | 2.50 | 1.39 | 6,968 |
| | off | 76 s | 4.65 | 3.05 | 8 |

Aloy ran twice per setting with identical results; Laura once. Placement off
needs about 1.9× the peak memory on both. Its speed is mixed (slower on Aloy,
slightly faster on Laura). Why it needs more memory is not known. "RSS after"
may include memory the allocator kept rather than live data; not checked.
Laura's rebuild matched the batch (5,171,046 faces, NM 0, 451 s), and its
default decimation reproduced the batch's 6,968 NM edges. Its off-surface
distances were measured later (table above: up to 412 mm).

Trade-off (before `planarquadric` / `preservetopology` were tested):
defaults are more accurate on clean surfaces but fling vertices; placement
off kept every vertex on the surface but is coarser and needs about twice
the decimation memory. Vertices on the
surface don't make it safe in general: collapses can still bridge cavities,
drop thin parts, flip faces or self-intersect. Shape checked on the rod
fixture and Aloy only; Laura only by NM count.

Reproduce with
[tools/experiments/decimation_sliver_probe.py](../../tools/experiments/decimation_sliver_probe.py)
(pass the `stl-decimated/...900000.stl` cache; `--save` keeps the decimated
arrays for a separate MeshFix run).

## Fix (implemented 2026-10-05): `planarquadric=True` in both passes

Owner decision: `decimator.QUADRIC_PARAMS` passes every parameter of the
quadric filter explicitly, PyMeshLab's defaults except `planarquadric=True`,
for the initial and the post-reconstruction decimation (one implementation
serves both). Passing every parameter also stops a call inheriting flags
from an earlier one in the process. The decimation cache name carries
`decimator.settings_tag()`, so caches made with the defaults are not reused.

Aloy and Laura through the real repair path (`processor.process`, batch
settings: skip_clean, min_shell_faces 100, 10 GB reconstruction budget),
from their decimation caches:

| Model | Result | Total | Winding | Decimation (NM after) | MeshFix |
|---|---|---|---|---|---|
| Aloy | PROCESS, 100.00 % volume | 70 s | 36 s | 10 s (0) | skipped: no defects |
| Laura | PROCESS, 100.00 % volume | 596 s | 489 s | 55 s (1) | 35 s, clean |

Both timed out at 3,604 s in the batch.

Initial pass on sources, defaults vs `planarquadric`, each in its own
process
([tools/experiments/initial_decimation_compare.py](../../tools/experiments/initial_decimation_compare.py)).
Criteria agreed before the run (diag = source diagonal): surface distance
both ways, p99 within max(1.25 × defaults', 1e-4·diag) and max within
max(1.25 × defaults', 1e-3·diag); component volume ratio within 0.005 of
defaults'; every source shell of ≥ 100 faces and both tips along the longest
axis within max(1.25 × defaults', 1e-3·diag). All pass on all three:

| Source | Faces | Time defaults / planar | Peak | src→dec max | dec→src max | NM defaults / planar |
|---|---|---|---|---|---|---|
| Base_Pillar_R | 1.00M → 0.90M | 4.5 / 4.6 s | 0.50 / 0.50 GiB | 0.0488 / 0.0488 | 0.0137 / 0.0137 | 5 / 5 |
| Bat Girl Merge | 4.83M → 0.90M | 66.6 / 67.6 s | 2.19 / 2.19 GiB | 0.211 / 0.211 | 0.0487 / 0.0217 | 42 / 45 |
| left_sword (24 shells) | 2.08M → 0.90M | 24.9 / 23.8 s | 0.98 / 0.97 GiB | 0.046 / 0.065 | 0.0173 / 0.0176 | 36 / 45 |

Scope: three source models; this does not prove the setting safe on every
source. The shell check is sampled coverage, not proof that components stay
topologically distinct. What triggers the defaults' thrown vertices is
still not known.

## Not yet done

- Reproduce MeshFix on the decimated output alone, to see whether it hangs or
  is just very slow.
- Check whether decimation with topology preservation, or a smaller reduction
  step, avoids the NM explosion.
- Consider a MeshFix time bound or NM-count guard, so the step fails quickly
  instead of consuming the per-file timeout.
