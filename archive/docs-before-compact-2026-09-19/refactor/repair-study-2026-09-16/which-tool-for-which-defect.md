### Which tool for which defect

| defect | tool | note |
|---|---|---|
| **T-junctions** | **welder (ours)** | PyMeshFix and Blender both dent; PyMeshLab's `remove_t_vertices` **destroys the mesh** |
| inverted normals | PyMeshLab `by_geometry` | the only tool that fixes it — nothing else even detects it |
| seams | PyMeshFix, or `by_geometry` | PyMeshFix re-wound all 760 faces of the fixture correctly in one call |
| duplicates | PyMeshLab CLEAN | all three filters; `merge_close_vertices` **alone makes it worse** (1,140 nm at 200% volume) |
| degenerate faces | any | all three exact |
| fins | **Blender** | face-set identical to the control |
| non-manifold, multi-shell | **PyMeshFix** | after the split |

