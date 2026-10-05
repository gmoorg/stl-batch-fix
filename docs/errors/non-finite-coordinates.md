# STL with non-finite coordinates

## Symptom

The loader rejects the file before any processing (`load_failure`):

```
not finite: the file contains NaN or infinite coordinates
```

No output or marker is written.

## Where the error comes from

Our own check, not PyMeshLab: `mesh_io.load` tests the raw float32
coordinates with `np.isfinite` right after reading them, before welding. The
prepare pass sets `load_failure` just before calling `mesh_io.load`, so
decimation and PyMeshLab never see these files.

## Files

Both under `CA3D/Demon Queen/1-6 scale pres supports Demon Queen CA3D/STL/Queen/`:

- current_SUP_STL.stl
- torso_girl_SUP.stl

## Measured: torso_girl_SUP.stl (2026-10-04)

Raw file (numpy over the STL records):

| | Value |
|---|---|
| Triangles in file | 889,454 |
| Faces with a non-finite coordinate | 224 |
| NaN values | 2,016 (= 224 × 9: every coordinate of those faces) |
| Infinite values | 0 |

PyMeshLab `MeshSet.load_new_mesh(path)`:

| | Value |
|---|---|
| Result | loads **without error or warning** |
| Faces loaded | 889,230 (889,454 − **224**) |
| Vertices | 449,416 |
| Non-finite vertices in loaded mesh | 0 |
| Load time / peak RSS | 2.8 s / 0.48 GB |

The importer **silently drops** as many faces as the file has NaN faces. The
counts match exactly, which strongly suggests it drops the NaN faces, but
that hasn't been confirmed face by face. Decimation to 900k didn't exercise
anything (889,230 is already under the target).

Rerun with a 500k target, so decimation runs on the loaded mesh: it finished
normally, 889,230 → 500,000 faces (254,783 vertices) in 6.9 s, peak RSS
0.61 GB, still **0 non-finite vertices**. Once the importer has dropped the
NaN faces, decimation behaves like on any other mesh.

`current_SUP_STL.stl` hasn't been measured. This case had whole NaN
triangles only; partial NaNs and infinities weren't tested.

## Observations

- The rejection is a deliberate decision in our loader. STL stores every
  triangle independently, so the bad faces are identifiable. Here they are
  whole triangles of NaN, not single bad coordinates.
- If the initial decimation moves to `load_new_mesh(source)`, this check no
  longer runs first. PyMeshLab would then accept these files silently, minus
  the NaN faces, instead of rejecting them. Whether that's acceptable is a
  decision to make deliberately, not a side effect of the switch.
- Both are presupported `_SUP` exports from the same source as the
  [volume-loss](volume-loss-rejected.md) files, so the exporter may be at fault.
