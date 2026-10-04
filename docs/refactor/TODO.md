# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../archive/README.md). Remove completed tasks; update the owning reference.

## Runner and concurrency

- [ ] Replace the admission memory estimate. Today it is source triangles ×
  890 B × an unvalidated factor 3 — but alpha-wrap's memory follows the
  surface it wraps, not the input face count. Measured 2026-10-03: a
  6,080-face sphere of radius 132 mm was estimated at 16 MB and peaked at
  15.3 GB (alpha-wrap 23 min). With 4 workers, several such models could be
  admitted together and exhaust RAM. The budget is also too small: it uses
  free pages (`SC_AVPHYS_PAGES`), not `MemAvailable` (4.5 GB vs 23.5 GB here).

  Measured prediction, A = surface area of the mesh being wrapped (mm²),
  α = the alpha actually used (after the 0.15 cap):

  - Output faces ≈ 1.39 · A / α² for a closed surface (open patches: ≈ 2.86,
    both sides of the sheet).
  - Peak memory of the wrapping process ≈ 1.6–2.0 KB · A / α², i.e. about
    1.3 KB per output face, plus ~100 MB process baseline that dominates
    small wraps.

  | Wrap | A (mm²) | α | Faces out | faces / (A/α²) | Peak | bytes / (A/α²) | Time |
  |---|---|---|---|---|---|---|---|
  | Sphere r=13.2 (1,000-facet input) | 2,195 | 0.0572 | 930,942 | 1.39 | — | — | 53 s |
  | Mirko_BodySFW, whole (300k faces) | 23,721 | 0.15 | 1,460,894 | 1.39 | 1,963 MB | 1,952 | 175 s |
  | Sphere r=132 (6,080 faces) | 219,473 | 0.15 | not logged | — | 15,277 MB | 1,642 | 1,368 s |
  | Mirko, 25 open region patches | 142–1,987 each | 0.15 | 18,786–250,564 | 2.80–2.97 | 119–337 MB | 4,002–9,608 | 1.5–19.6 s |

  An input-area estimate is conservative for meshes with internal or
  duplicate surfaces (only the outer envelope is wrapped). Two closed-model
  memory points so far; measure a few more before relying on the constant.
  Related option, not decided: choose α per part from its face budget
  (faces ≈ 1.39 · A / α²), which bounds memory and time directly — needs a
  visual check, since `diag/800` was chosen by inspection.
  Tried and rejected (2026-10-03): wrapping a model in 3×3×3 spatial regions
  and concatenating. Peak memory fell ~6× (337 MB vs 1,963 MB), but each
  region is an open surface patch, so the wrap is a thin two-sided sheet and
  the glued model was a hollow skin (2% of the original volume) with seams;
  it also took longer (259 s vs 175 s). Making regions solid needs capped cuts,
  which need the watertight input broken models lack. A solid tiled
  alternative would be winding-number inside/outside (libigl) + marching
  cubes on one global grid in blocks — a replacement for alpha-wrap, not a
  patch.

## Reconstruction

- [ ] Evaluate the winding-number + marching-cubes reconstruction as an
  alpha-wrap replacement — algorithm, measurements and open questions in
  [reconstruction](reconstruction.md). Next: broken models, memory streaming,
  grid spacing, block size.

## Tests

Target: end-to-end coverage — real `batch_repair.py` runs over a few fixtures
that each combine many defects — replacing per-tool unit tests of geometry.

- [ ] Build the end-to-end fixture set: a few models whose defects together
  cover open edges, non-manifold edges, inconsistent and globally inverted
  winding, oppositely wound shells, self-intersection, tiny debris shells, and
  a meaningful small part that must survive. Each test checks the published
  output and indicator, not only `open=0`/`nm=0`. Runtime: about 50 s per model
  through alpha-wrap regardless of shape — a 6,080-face sphere took 52 s, a
  1,232-face real foot 46 s (2026-10-02), because alpha scales with the model's
  own size and output is ~0.7–0.9M faces either way. Synthetic shapes are
  therefore not faster; keep the set small and run it with several workers.
- [ ] Keep the runner robustness tests, which use fake child scripts because
  real models cannot fail on demand: child crash, timeout, Ctrl+C (cleanup and
  second Ctrl+C), half-written output, recovery after a crash, and each file
  reported exactly once.
- [ ] Keep the config validation tests (`test_runconfig`, CLI config cases).
  They prove that a mistake in `batch_repair.toml` stops the run before any
  file is written, with a message naming the key: misspelled or unknown key,
  missing `input`/`output`/`max_faces`, wrong type (`workers = "4"`,
  `skip_clean = 1`), out-of-range value (negative, zero timeout, fraction
  above 1, `inf`), malformed TOML, unreadable file, NUL in a path, and that
  `batch_repair.example.toml` still lists every option with the code's
  default. They take milliseconds and need no fixtures.
- [ ] Then remove unit tests the end-to-end set covers, and all tests of tools
  absent from the default pipeline (welder, seam split, MeshLab filters,
  Blender repair, `open_loops_are_printable`, legacy `test_pipeline.py`).
  Justify each removal by the remaining coverage; never bless known geometry
  loss to make a test pass.
- [ ] Split the suite into fast tests (robustness, config) and the slow
  end-to-end set so the fast part can run on every change; update the test
  guide.

## Deferred (not critical now)

- [ ] Low priority: check that initial decimation keeps meaningful detail.
  Detail too small to survive decimation is usually too small to print, so
  this matters only for thin but long features — antennae, sword blades,
  fingers, cables — which can be printable yet lose their tips or break into
  pieces when the face budget is tight. Total retained volume cannot show
  this: a lost antenna is a tiny share of the volume.
- [ ] Evaluate the `is_already_clean` gate on real models (`skip_clean = true`):
  confirm Amidara base fails it and that gated output slices and prints. It
  may not be used at all; keep it off by default.
- [ ] How often MeshFix is needed after decimation: read it from `batch.log`
  after a run over the full collection (the `meshfix` step records whether it
  ran).
- [ ] Move step tuning values into `pipeconfig` (MeshFix clean/fill parameters,
  lost-vertex tolerance, shell floor, retained-volume threshold, Blender
  timeouts); decide then whether any belong in `batch_repair.toml`.
- [ ] Consider moving step ordering into `pipeconfig`, per-step IDs, and explicit
  split/merge entries. Do not restore removed ENABLE switches.
