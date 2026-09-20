### Do not retry these

- **`meshing_remove_t_vertices`** — a no-op at threshold ≥ 10, and at ≤ 1 it
  reduced a 910-face mesh to **zero faces** reporting `nm=0 open=0`. A clean
  empty mesh, which only a volume check catches.
- **`merge_close_vertices` alone** — collapses vertices while leaving both face
  sets: 1,140 non-manifold edges at 200% volume. The other three filters are
  not optional.
- **`re_orient_faces_coherently` alone** — unifies the winding but can pick the
  *wrong* direction, giving −4094.9 on the seam fixture. `by_geometry` after it
  turns the mesh outward.
- **PyMeshLab orientation on a mesh that does not need it** — on a correctly
  repaired mesh it manufactured **153 seam edges in 34 closed loops**, which is
  the signal meaning "PyMeshFix will delete a region here".
- **`MERGE_DIST = 0.01mm` as a PyMeshLab threshold** — tuned for Blender's
  `remove_doubles`; in PyMeshLab it was the only setting that *increased*
  non-manifold edges (2,263 → 2,359).

**API note**: PyMeshLab thresholds take `PercentageValue` or **`PureValue`**
(absolute units). There is no `AbsoluteValue` — passing a bare float raises.

