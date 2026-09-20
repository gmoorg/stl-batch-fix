# Mesh pipeline

Read [context](overview.md) when needed. Entries retain their original chronology; later corrections can supersede earlier proposals.

**Current lookup:** [D13](d13.md) explains decimation before splitting and the shared face budget. [D12](d12.md) is the earlier one-rung proposal; current `decimator` keeps a PyMeshLab fallback, while [D19](../splitting-and-repair/d19.md) removed the Blender rung. The named measurements and proposed constants below concern the frozen script unless a later entry says otherwise. See the [status map](../history-status.md).

- [D12 — `decimator` is one implementation, required at startup](d12.md)
- [D13 — decimate first, then split: one order, no deferral](d13.md)
- [D14 — Ctrl+C kills; the pool does not catch it](d14.md)
- [Proposed pipeline — `_process_file_impl` restructured (superseded)](proposed-pipeline-processfileimpl-restructured-superseded.md)
- [Float drift was tested and ruled out (2026-09-14)](float-drift-was-tested-and-ruled-out-2026-09-14.md)
- [Step evidence from the 2026-09-14 full run](step-evidence-from-the-2026-09-14-full-run.md)
- [Absolute constants mean different things at different scales](absolute-constants-mean-different-things-at-different-scales.md)
- [The fused seam on Leia is decimation, not repair](the-fused-seam-on-leia-is-decimation-not-repair.md)
- [`MAX_FACES = 900_000` is a slicer constraint, not a memory ceiling](maxfaces-900000-is-a-slicer-constraint-not-a-memory-ceiling.md)
- [Proposed — log what each absolute constant meant on this mesh](proposed-log-what-each-absolute-constant-meant-on-this-mesh.md)
- [Proposed — derive `_BBOX_TOLERANCE_FLOOR` from the percentage](proposed-derive-bboxtolerancefloor-from-the-percentage.md)
- [The instrument has known holes](the-instrument-has-known-holes.md)
- [Review of `stl_batch_fix.blender` (2026-09-14)](review-of-stlbatchfix-blender-2026-09-14.md)
