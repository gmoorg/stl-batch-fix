# Full-suite run errors

Errors collected from the full batch run `785dbe47` (started 2026-10-04,
input `/mnt/sda2/STL/Fixing`, output `/mnt/sda2/STL/Fixed`), investigated
the same day. The run was stopped by the owner after 556 of 1,067 jobs. The
rest of the library has not been checked.

The counts below are a snapshot taken while the run was still going; the
owner stopped and resumed it after the errors were gathered. The final record
(`progress.log`, 485 `PROCESS`) adds one later error,
`Dragon/wing_broken_SUP.stl` (`DESTROYED`, out of scope as `_SUP`); its 16
`FAILED` and 2 `TIMED_OUT` records are all covered by the kinds below.

Run config: `max_faces = 900000`, `skip_clean = true`,
`reconstruct_memory_budget_gb = 10`, `min_shell_faces = 100`, `workers = 0`
(auto), `per_file_timeout = 3600`, `memory_budget_fraction = 0.7`.
Machine: 31 GiB RAM, no swap, 12 cores.

Sources: `.failed.stl` / `.timeout.stl` markers, the per-model `.log` beside
each, `<output>/batch.log`, `<output>/progress.log` (job records), the kernel
journal, and the experiment scripts listed below.

Out of scope: load failures on files that are not STL or OBJ (`.lys`,
`Thumbs.db`), and presupported `_SUP` models, which got into the library by
mistake and will be removed from it (no filename filter). Azula (39.6M faces)
is also out of scope: too big to process on this machine.

| Kind | Files | Status | Doc |
|---|---|---|---|
| Segfault in PyMeshLab initial decimation | 7 | **Fixed** (2026-10-05): `meshlab.to_mesh` drops index-degenerate faces before PyMeshLab | [decimation-segfault.md](decimation-segfault.md) |
| Out-of-memory kill, nothing logged | 1 (Azula, 39.6M faces) | **Out of scope** (owner, 2026-10-05): too big for this machine, not pursued. Doc kept as reference | [oom-kill-silent.md](oom-kill-silent.md) |
| Winding output non-manifold | 8 (6 models) | **Cause verified** (3 of 6): `winding._weld` partially merges grid-node vertex clusters. Decisions: loosen `_check` *and* fix the weld | [winding-non-manifold.md](winding-non-manifold.md) |
| MeshFix timeout after post-wrap decimation | 2 | **Cause found** (Aloy): default decimation throws vertices up to 43 mm off the surface, likely triggered by winding's sliver clusters. Plan: fix winding first, re-test, then decide on decimation settings | [post-wrap-meshfix-timeout.md](post-wrap-meshfix-timeout.md) |
| Volume-loss rejection (`DESTROYED`) | 12 (10 `_SUP`, 2 real) | `_SUP`: struts dropped as debris (out of scope). `arm2`/`fabric3`: **guard measures open shells meaninglessly**, and the shells are pieces of one surface split at unwelded seams | [volume-loss-rejected.md](volume-loss-rejected.md) |
| STL with non-finite coordinates | 2 (`_SUP`) | Our loader rejects them; PyMeshLab's importer silently drops NaN faces (measured) | [non-finite-coordinates.md](non-finite-coordinates.md) |

Reference: [decimation-memory-path.md](decimation-memory-path.md) covers
what the initial decimation allocates, measured PyMeshLab memory per face,
and the owner's decisions: decimate from file, a float64 PLY cache (importer
and exporter widened), and a float64 internal vertex buffer.

## Side issues found during the investigation (not yet written up)

- **Crash reasons aren't recorded.** The job record says only "crashed".
  The child's exit signal (SIGSEGV vs SIGKILL/OOM) isn't reported, and the
  faulthandler traceback exists only in the per-model `.log`.
- **The memory budget is fixed at startup.** `memory_budget_fraction` is
  applied to `MemAvailable`, read once when the run starts
  (`runconfig.resolve`). Memory that other applications take later (such as
  BambuStudio during the Azula OOM) isn't seen.
- **Possible silent spikes:** default post-reconstruction decimation can
  move vertices far off the surface (see the timeout doc). Nothing checks for
  it in models that passed.

## Experiment scripts

All in `tools/experiments/`, read-only on inputs, not wired into the
pipeline. Usage in each docstring. Some modes write to a path you pass
(`segfault_probe.py` synth/crop, `meshlab_import_probe.py` OUTDIR,
`decimation_sliver_probe.py --save`); keep those outside the library.

| Script | Used for |
|---|---|
| `meshlab_file_decimate.py` | file-interface load + decimation memory/time; raw-STL NaN count |
| `meshlab_import_probe.py` | PyMeshLab import / PLY round trip on duplicate, degenerate, near-duplicate geometry |
| `segfault_probe.py` (+ `tests/probes/segv_min16.stl`) | degenerate-face segfault: count, array vs clean, synthetic cases, crop |
| `winding_nm_probe.py` | raw marching-cubes vs welded NM, NM edge locations |
| `winding_post_steps.py` | broken winding output through decimate / MeshFix |
| `decimation_sliver_probe.py` | slivers in winding output; vertices thrown off the surface by decimation |
| `placement_rod_check.py` | thin-feature accuracy with `optimalplacement` on/off |
| `placement_memory_probe.py` | PyMeshLab decimation time and peak memory with `optimalplacement` on/off |
| `shell_report.py` | per-shell volumes, origin dependence, seam/boundary coincidence |

Paths in the per-error docs are relative to the input root
`/mnt/sda2/STL/Fixing`. Output markers and logs sit at the same relative path
under `/mnt/sda2/STL/Fixed`. `zoro/LionRealm Studio - One Piece - Zoro/`
duplicates `LionRealm Studio - One Piece - Zoro/`, so those models fail twice.

