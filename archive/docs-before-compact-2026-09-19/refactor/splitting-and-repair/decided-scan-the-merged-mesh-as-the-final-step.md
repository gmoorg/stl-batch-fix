### Decided — scan the merged mesh as the final step

**Raised by the user 2026-09-16.** After parts are repaired and merged, run
`scanner.scan()` on the result before writing it out.

This is not re-checking work already checked. Repairing parts independently and
reassembling can introduce defects **no individual part had**: two parts
sharing a boundary can be re-wound differently from each other, and the merge
itself can leave the seam between them open. The merged mesh is geometry that
nothing has yet inspected.

It extends the principle `_post_verify` already encodes — *"PyMeshFix (and
Blender) self-report unreliably, so nothing is written out as 'ok' on a
library's word alone"* — to the one step that currently has no verification at
all.

