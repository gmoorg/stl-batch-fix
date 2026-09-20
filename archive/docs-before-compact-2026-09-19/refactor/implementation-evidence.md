# Measurements behind current code comments

This page keeps measurements and rejected approaches that used to occupy long comment blocks in `libs/`. Check the linked code before treating an old measurement as a current result. The [current state](current-state.md) records what the modules do now.

## T-junction repair

- The old perpendicular-distance test used an absolute `1e-6` mm threshold. It found the synthetic junction at radii 1 and 10, then missed it at radii 50, 100, and 200 as float32 rounding grew. Making the threshold relative fixed that scale failure but still missed a real junction whose open path was far from the nominal edge line. The current `welder` follows the open-edge path and checks a connected face strip.
- On tested fixtures the path rule matched prior positive and negative counts across radii 0.5–5000 mm, and handled multiple vertices on one spanning edge. A 150-junction fixture converged in two passes. Observed chains had one or two interior vertices; `MAX_CHAIN=12` is a search guard.
- Interior path vertices must each have two open edges. A four-edge pinch on costume01 was ambiguous. Endpoints may have higher degree: requiring degree two there rejected 107 genuine junctions in a 150-junction fixture.
- On that 150-junction fixture, the face split produced 1,060 faces from 910 with no open edges and no meaningful volume loss. PyMeshFix returned 1,000 faces and lost two vertices; Blender returned 824 and lost 118. The operation adds faces without moving vertices.
- An earlier junction search reported 19 hits on Mandy and raised seam counts from 4 edges/1 loop to 16/5. It was crossing holes. Requiring the adjacent face strip cut the hits to two and left seam counts unchanged.

## Cleanup and result judging

- `CLEAN_FILTERS` keeps duplicate-face removal after vertex merging. On `doubles`, merging alone left 1,140 non-manifold edges and 200% volume. On costume01, 1,166 of 1,170 duplicate faces had opposite winding; removing one face exposes holes that the subsequent repair must handle.
- Exact vertex matching after PyMeshLab falsely reported 382 lost vertices on `sphere_doubles`; the largest harmless coordinate change was `1e-5`. At `1e-4`, that fixture reported zero lost, while genuinely damaged fixtures still reported 6 or 3. `LOST_VERTEX_TOLERANCE` is still absolute and remains an [open scale bug](splitting-and-repair/open-bug-absolute-tolerances-break-with-model-scale-found-2026-0.md).
- A real destructive PyMeshFix result retained 46.27% of Mandy's volume while passing topology checks. Sound repairs retained about 95–100%. `processor.MIN_VOLUME_KEPT=0.90` separates these observations; coincident duplicate shells can legitimately halve and are a known false positive.
- `splitter.MIN_SHELL_FACES=100` retained observed real Mandy parts (smallest 750 faces) while rejecting costume01 specks. Lowering the floor to 10 made costume01 split into 381 parts. This is a corpus observation, not a universal physical definition of debris.
- On a defect-dense costume01 mesh, adding a single weld face changed PyMeshFix's output by 884 faces while defect counts and retained volume were effectively the same. Face count alone is a poor regression oracle for that repair.

## Orientation and Blender boundary

- A signed-volume guard missed local inverted faces in real parts. Unconditional per-part orientation fixed 15 faces and broke one in the Mandy measurement. Running it before splitting erased seam evidence on `sphere_seam` (40 edges and one closed loop became zero); the current sequence runs it after the shell split.
- Blender's optional repair path now writes and reads PLY, preserving the vertex table. Older comments describing an STL repair round trip are stale. `blender.convert` still emits binary STL.
- The old absolute 0.01 mm Blender weld deleted detail on a 20 mm sphere: a 0.2 mm bead lost 22 of 389 vertices and a 0.1 mm bead lost 49. PLY carries vertex indices and avoids that reconstruction step.
