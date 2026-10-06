# TODO

Open tasks only. Implemented behavior: [modules](modules.md) and
[pipeline](orchestration.md). Historical review evidence and test results are
[archived](../../../stl-batch-fix.old/archive/README.md). Remove completed tasks; update the owning reference.

## Priority (owner, 2026-10-05)

Work in this order; details in
[decimation-memory-path.md](../errors/decimation-memory-path.md). Done (2026-10-05): the
narrow PLY dialects; the PLY decimation cache beside the input folder
(decimate-from-file was measured and not adopted: PyMeshLab's STL reader
needs more memory than our loader); the chunked STL read with
NaN/inf and coincident-corner triangles dropped and counted (`mesh_io.load`,
1024-triangle chunks). The incremental weld proposed with it was dropped
(owner): the load is not the job peak, and only the whole-file buffer was
an allocation with no purpose.

Also done (2026-10-05): float64 `Geometry.verts`, job memory re-measured
(constants kept); the crash signal: a child with no trusted result is
reported with its signal or exit status, and the model log gets the
parent's diagnosis ([orchestration.md](orchestration.md) step 5).

1. [ ] **Test the `is_already_clean` gate** (`skip_clean = true`) on real
   models (owner, 2026-10-05: wanted, on in the owner's config). Amidara
   base fails it (confirmed 2026-10-06, below). The sample's outputs pass
   Bambu's recommended repair tool (2026-10-06, below). Confirm that gated
   output slices and prints. Keep it off by default until
   then. The NM-only fast path (Reconstruction below) widens the same
   gate and is tested with it.
   - [x] Run the gated sample outputs through an online repair/analysis
     tool and note whether it finds anything the scan cannot see
     (self-intersections, overlapping or inverted shells). Done (owner,
     2026-10-06): every file in `out/` below, Amidara included, is clean
     according to the repair tool Bambu recommends — it found nothing the
     scan missed. Slicing and printing are still unconfirmed. Sample re-run
     on HEAD 2026-10-05 (`skip_clean = true`, `max_faces = 900000`),
     outputs under `/mnt/sda2/STL/GateSample/out/` (sources in `in/`):
     - whole model skipped: `zoro/NomNom Zoro/Zoro_STL/178mm_split/r_blade.stl`,
       `MonHun_duo/Hinoa_Minoto_Bikini/Minoto_Bikini_Right_leg.stl`,
       `Fae/Aine  Noon Fae/Aine__Noon_Fae_-_STL/wingR.stl`,
       `CA3D/1-6Scale Fantasy Dragon/Fantasy/Dragon_1_Part_5.stl`,
       `Peach Figure - Scooby-Doo - Velma Dinkley/VELMA_NSFW_PEACHFIGURE/Velma nsfw/Legs_nsfw_v1.stl`
       (decimated to 900k, still clean);
     - clean parts merged unrepaired: `Mandy Pinup Figurine/Mandy NSFW Dinamuu3D/Mandy NSFW Version A/MandyNakedA_Arms.stl`
       (4/14 parts), `nutshell-atelier-belly-dancer-nsfw/3rd-02.stl` (1/2),
       `Kuton Figurines - Hebe/Unsupported_STL/cloth.stl` (6/7),
       `CA3D/Cleopatra + NSFW/1-9 Scale Uncut Cleopatra_NSFW/model.stl` (6/7);
     - control, fully repaired (not gated): `Shadaloo Studios - Madelyne Pryor nsfw/Madelyne_NM_Body.stl`
       — looks excellent on visual inspection (owner, 2026-10-06);
     - Amidara base, confirmed failing the gate (2026-10-06, HEAD `bcceb5f`,
       same settings, child run directly): no NM or open edges but 922
       winding seams, so both gates said "not clean"; repaired by winding
       (1,848,928 faces, 42 s) → decimate (315,482, 1 NM edge) → MeshFix
       (315,070) → PROCESS, 100.00% volume, 0 seams; 1 min 20 s, 2.1 GB
       peak. Output: `Amidara/Amidara_Blustmorn_1-12_base.stl` (source
       `/mnt/sda2/Amidara_Blustmorn_1-12_base.stl`). Still to check: it
       slices and prints.
2. Everything else: volume guard on open shells
   ([volume-loss-rejected.md](../errors/volume-loss-rejected.md)), MeshFix
   time/NM guard ([post-wrap-meshfix-timeout.md](../errors/post-wrap-meshfix-timeout.md),
   "Not yet done"). Idea only, not a requirement (owner, 2026-10-05): when
   the decimated result has too many NM edges, alpha-wrap it before
   PyMeshFix instead of handing MeshFix the NM edges. The wrap rebuilds the
   surface with many faces, so it needs the post decimation again after it
   (wrap → decimate → MeshFix).

## Intake conversion without Blender

Done 2026-10-06: `textmesh` converts OBJ and ASCII STL in-process; the
decisions (source coordinates, triangles and quads only, FAILED marker for
malformed input, bowtie quads split anyway) and the Blender agreement
measurement are in [modules](modules.md) (`textmesh` row). Blender itself
was then retired (owner, 2026-10-06): module, script and tests archived
outside the repository, startup check and `install.sh` section removed
(modules.md, `blender` note).

## Reconstruction

- [ ] Winding-number reconstruction (`libs/winding.py`, default part step
  since 2026-10-03) follow-ups, details in [reconstruction](reconstruction.md):
  more broken models (large holes); lower the memory floor — measured
  2026-10-06: the final `scanner.scan` is the peak for large outputs (fix
  it first: int64 edge keys, 6× less memory, ~45× faster), then the weld /
  streaming each block's output; avoid the per-block winding-number octree
  rebuild (`igl.FastWindingNumberBVH`, cached). Post-reconstruction decimation is bounded by no budget (memory and
  time follow ~3·A/h² rebuilt faces; sphere r 132: 29 M faces, 353 s,
  13.9 GB) — admission reserves for it, reducing it needs an owner decision
  (e.g. per-part spacing from area, or decimating blocks before the weld).
- [ ] NM-only fast path: revisit after the next long run (owner,
  2026-10-05). Implemented under `skip_clean` (orchestration step 4a): a
  part whose only scanned defect is NM edges gets MeshFix alone, accepted
  when clean with component volume in 98–102%, else the part sequence runs
  on the original part. Open:
  - `NM_FAST_PATH_MAX_PERCENT = 0.05` (NM edges per 100 part faces; owner
    asked for a percentage, 2026-10-06) and `NM_FAST_PATH_VOLUME_BAND` are
    provisional; set them from the long run's `nm_fast_path` log lines (NM
    count and percentage, MeshFix step time, volume, kept/fallback). The
    limit is not a time bound: MeshFix runs in-process, and a ratio does
    not cap the NM count on a very large part (`max_faces = 0`). The
    Aloy/Laura MeshFix timeouts (534 and 6,968 NM on 900k faces) were
    damaged post-reconstruction decimations, not fast-path input.
  - Open edges next (owner chose NM only first). Needs a shape check that
    works on open input: its volume is no reliable reference (priority 2,
    open-shell volume guard).
  - Seams stay excluded: MeshFix can delete whole surfaces on
    irreconcilable winding. "Only NM" is only what the scan sees:
    self-intersections, overlapping shells and a whole inverted shell are
    invisible, and winding across the NM edges is checked only on the
    result.
  - Measured on fixtures before the implementation (2026-10-05, custom
    `part_steps`). `nm_only` / `nm_seam` / `nm_hole`
    (tests.md; 760-face sphere, 20 closed fins, no decimation): MeshFix
    alone 0.01 s, clean, all 20 fins gone, every output vertex on the
    sphere, but 800 → 662 faces and volume −1.05% (faces deleted around
    each NM edge, refilled flat). Default sequence: ~54 s, clean, fins
    gone, volume −0.07% (`nm_hole` −0.18%), output vertex radius
    9.876–10.030 (`nm_hole` 9.739–10.059; rebuilt surface, r 10 truth).
    MeshFix also cleared the seam and hole.
    On an 80-segment sphere (12,640 faces, scratch run under load):
    1/20/200 fins, MeshFix 0.5–2 s and volume −0.0/−0.0/−0.05%; default
    56–78 s. Limits: a closed fin on a perfect sphere is an easy NM case.
    It does not set an NM-count limit, does not stand in for the Aloy/Laura
    thousands, and its overlapping fin faces are not a self-intersection
    test.

## Print-risk check and rib supports

`check_3mf.py` / `support_3mf.py`, see [modules](modules.md#print-risk-check-separate-from-repair).
The first rib output (`/mnt/sda2/ank.supported.3mf`, 2026-10-04) is not yet
confirmed by a test print: Bambu loading, ribs in the sliced preview and
removal are all unverified.

- [ ] Triangular ribs for easier removal: taper each rib's cross-section to
  a narrow edge at the model instead of a full-width rectangular wall, so it
  touches along a thinner line. In the 0.1–0.44 mm band a rib is only 1–2
  layers tall, so a taper there may not survive slicing — decide together
  with the lift idea below, which makes ribs tall enough to taper.
- [ ] Tilt search: try orientations within ±5° and pick the one with the
  most plate contact / least unsupportable area. A first measurement on the
  ank body (2026-10-04) gave 0.7 → 14.4 mm² of contact at a 3.3° tilt, at
  the cost of more unsupportable area (6.5 → 12.5 mm²); the trade-off rule is
  open.
- [ ] Lift the whole model so the ribs can be joined: with the model raised,
  ribs become tall enough to stand on a shared connecting base, so they come
  off as one piece instead of single-line ribs each fused to the plate where
  they may stay stuck. Needs a lift height (and how it interacts with Bambu
  dropping objects to the bed) and the base's shape.
- [ ] Look online for existing tools that already do this (generated or
  custom breakaway supports for FDM, near-plate undersides, 3MF
  post-processing) before extending ours.

## Tests

Target: end-to-end coverage — real `batch_repair.py` runs over a few fixtures
that each combine many defects — replacing per-tool unit tests of geometry.

- [ ] Extend the end-to-end set. `test_end_to_end` runs four defect
  composites (`defect_spheres.composites()`) through the real two-pass batch
  and checks shape, volume, debris, the rod tip and log attribution. Still
  missing: a meaningful small part that must survive, self-intersection, and
  real broken models (large holes).
- [ ] Per-step outcome tests (shape kept by each step on defect spheres):
  done for the decimator (`test_decimator.TestShapeIsKept`), not yet for
  winding, MeshFix or the split.
- Keep (not a task): the runner robustness tests with fake child scripts
  (crash, timeout, Ctrl+C, half-written output, recovery, exactly-once
  reporting, both passes) and the config validation tests — real models
  cannot fail on demand, and config mistakes must stop a run before any write.
- [ ] Then remove unit tests the end-to-end set covers, and all tests of tools
  absent from the default pipeline (welder, seam split, MeshLab filters,
  `open_loops_are_printable`).
  Justify each removal by the remaining coverage; never bless known geometry
  loss to make a test pass.
- [ ] Split the suite into fast tests (robustness, config) and the slow
  end-to-end set so the fast part can run on every change (the full suite
  takes ~8 min); update the test guide.

## Deferred (not critical now)

- [ ] Investigate chunked STL writes (owner, 2026-10-05). `mesh_io.write`
  builds the whole file in memory: from code reading (not measured), per
  face a float64 `verts[faces]` gather (72 B), its float32 copy (36 B), the
  edge differences, cross product and normals (~40 B), the 50 B record
  buffer, and `buf.tobytes()`, a second 50 B copy of that buffer, made only
  to pass to `f.write` (which accepts the array itself). Writing in chunks,
  like `load` reads, would bound all of it by the chunk size. Measure first
  whether the write is ever near a job's peak (the merged model is written
  after every part is done); the `tobytes` copy is waste either way.

- [ ] Low priority: check that initial decimation keeps meaningful detail.
  Detail too small to survive decimation is usually too small to print, so
  this matters only for thin but long features — antennae, sword blades,
  fingers, cables — which can be printable yet lose their tips or break into
  pieces when the face budget is tight. Total retained volume cannot show
  this: a lost antenna is a tiny share of the volume.
- [ ] How often MeshFix is needed after decimation: read it from `batch.log`
  after a run over the full collection (the `meshfix` step records whether it
  ran).
- [ ] Move step tuning values into `pipeconfig` (MeshFix clean/fill parameters,
  lost-vertex tolerance, retained-volume threshold); decide
  then whether any belong in `batch_repair.toml` (the shell floor already is:
  `min_shell_faces`).
- [ ] Job memory calibration has no MeshFix-heavy multi-part model yet
  (orchestration.md "Job memory"); add one when such a model turns up.
- [ ] Consider moving step ordering into `pipeconfig`, per-step IDs, and explicit
  split/merge entries. Do not restore removed ENABLE switches.
