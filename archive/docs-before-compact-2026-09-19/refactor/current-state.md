# Current implementation and lookup

Checked against `libs/` on 2026-09-18. This is a code snapshot, not a claim that the full batch refactor or a collection run is complete. For why a choice was made, follow the linked decision. For remaining work, read [`TODO.md`](../../TODO.md) and the open entries below.

For the observable legacy interface and the plan to reproduce it with `libs/`, read [final interface and runner plan](final-behavior.md).

| Need to know | Current code | Rationale |
|---|---|---|
| Find and schedule files | `converter.prepare` classifies, copies companions, converts ASCII STL/OBJ, and emits meshes serially. `pool.Pool` supplies the worker loop and calls the selector under a lock. The batch walk connecting them to `processor` remains to be built. | [D4](pool/d4.md), [D7](pool/d7.md) |
| Represent and write a mesh | `mesh_io.Mesh` is frozen; probing is cheap, `load` adds geometry to a new value, and `write` writes the destination. Blender repair uses a PLY round trip. | [D15](mesh-data/d15.md), [D16](mesh-data/d16.md) |
| Reduce faces | `decimator.decimate` tries `fast_simplification`, then PyMeshLab. Blender is no longer a decimation rung. | [D12](pipeline/d12.md), [D19](splitting-and-repair/d19.md) |
| Measure defects and parts | `scanner` counts edge defects and finds edge-connected shells. `splitter.by_shells` uses those shells; `splitter.by_seams` exists but the current `repairer.repair` sequence does not call it. | [D17](mesh-data/d17.md), [D20](splitting-and-repair/d20.md), [D22](splitting-and-repair/d22.md) |
| Repair | `repairer.repair` welds T-junctions, cleans duplicate geometry, splits shells, orients each part, repairs it with PyMeshFix by default, then merges. `repairer.blender_part` repairs one part through a PLY round trip, but is currently only an injectable replacement; defect-based routing between Blender and PyMeshFix is unfinished. `welder` currently identifies junctions by an open-edge path, without an absolute distance threshold. | [D24](splitting-and-repair/d24.md), [D26](splitting-and-repair/d26.md), [Blender boundary](mesh-data/deferred-ply-for-the-blender-boundary-justification-lapsed.md), [T-junction findings](splitting-and-repair/the-t-junction-pattern-is-topological-but-topology-alone-is-not.md) |
| Judge and write | `processor.process` decimates, repairs, checks volume loss and rescans defects. `processor.write` writes a clean mesh or a full-mesh marker. `MIN_VOLUME_KEPT` is 0.90. | [D25](splitting-and-repair/d25.md), [D27](splitting-and-repair/d27.md) |

## Open risks worth checking first

- [Decimation can produce a mesh PyMeshFix will not repair](splitting-and-repair/open-our-decimation-makes-a-mesh-pymeshfix-will-not-repair.md).
- [Absolute `LOST_VERTEX_TOLERANCE` breaks at other model scales](splitting-and-repair/open-bug-absolute-tolerances-break-with-model-scale-found-2026-0.md).
- [Repair study gaps](repair-study-2026-09-16/known-gaps.md) and [evidence not gathered](unverified-evidence/overview.md) describe limits of existing measurements.

## Reading and updating

Open this page first, then the linked source or decision. The [numbered index](../../REFACTOR_DECISIONS.md) is for D1–D27; each topic index lists non-numbered experiments and corrections in original order. When code changes a statement here, update this page and the relevant decision or open issue. Historical measurements belong in the topic docs; code comments should state the current contract, invariant, or failure mode near the code they explain.

Several old entries still say “decided,” “fixed,” or “open” after a later change. The [history status map](history-status.md) lists the main overrides. It also distinguishes the proposed durable intermediate files in D21 from the current in-memory repair path.
