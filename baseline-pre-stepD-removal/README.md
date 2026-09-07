# Baseline: last run WITH step D (pymeshlab NM repair)

Captured 2026-09-07, immediately before removing step D and re-running the
whole collection.  Corresponds to git tag `v1.0-pre-stepD-removal`.

Kept outside the `.1`–`.5` log rotation so five later runs cannot age it out.

## Why this run matters

It contains the `Demon Queen/body.stl.stl` trace that motivated the change —
a model whose source was almost clean (3 non-manifold edges, no open edges)
and which the pipeline damaged badly:

    src                   1,789,954 tris  nm=3  open=0   z=[0.00,108.23]
    decimated               900,000 tris  nm=3  open=0   z=[0.00,108.23]
    + step D                899,994 tris  nm=0  open=12  z=[0.00,108.23]
    + pymeshfix             748,856 tris  nm=0  open=0   z=[4.90,108.23]

Step D resolved 3 non-manifold edges by deleting 6 faces, which tore 12 open
edges.  PyMeshFix then reconstructed around those holes and deleted 151,144
faces — 17% of the mesh — taking 4.9 mm off the bottom of the model, i.e. the
feet.  Both stages reported success.

## What replaced it

With step D removed, PyMeshFix repairs the non-manifold edges directly:

    decimated -> pymeshfix  899,976 tris  nm=0  open=0   z=[0.00,108.23]  53s

24 faces lost instead of 151,144, full model height preserved, and faster
(53s vs 86s) because nothing had to be reconstructed around artificial holes.

## Also ruled out during the investigation

- `pymeshlab method=1` (vertex split): does not repair these edges at all
  (nm=3 in, nm=3 out) and OOMs past 12 GB on a mesh with 9,167 NM edges.
- Decimation itself: exonerated.  Visually identical to source, bounding box
  unchanged, introduces no new NM edges on this model.
- MeshLib: deletes offending triangles, same as pymeshlab.
- ManifoldPlus: voxel-rebuilds the surface and loses fine detail.

## Not verified

Behaviour on meshes with thousands of non-manifold edges, where step D may
have been doing useful work.  The test file for that case (`Head Smoke Full`,
9,167 NM edges) was deleted before it could be checked.  If such a file
regresses, restore with `git checkout v1.0-pre-stepD-removal`.
