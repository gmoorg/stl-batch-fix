### Method

Six conclusions in that session were wrong and were caught by measurement, not
review. The pattern in every case: one tool measured against defect counts,
no second tool on the same input, and no look at the output.

- **A numeric all-clear is not a result.** Three meshes passed nm, open,
  degenerate, seam, shell *and* volume checks while being visibly damaged.
- **A comparison needs two columns.** "PyMeshFix handles this" was recorded
  before anything else had been run on the same input.
- **Load the file and look at it.** That found what four numeric checks missed,
  twice.
