### Decided — drop the Blender decimation rung; mark the failure instead

**Decided 2026-09-15.** The decimation ladder loses its Blender rung. Two rungs
remain — fast_simplification, then PyMeshLab — and a mesh that defeats both is
written out with a marker rather than handed to a third decimator.

**What the evidence actually says.** The collection run shows **88 of 88
decimations taking fast_simplification**, zero falling through. That is 88
meshes in one run, not a proof that failure is impossible, and it was nearly
over-read here as "fast_simplification always works" — the same shape as two
other claims made tonight without measurement. To be rechecked against the logs
before the change lands.

**What actually settles it is not that number.** There is a manual fallback:
Bambu Studio's own simplify. So a mesh that defeats both Python decimators is
not a lost model — it is a file that gets marked and handled by hand with a
tool already in use. That reduces the Blender rung from insurance against
losing a model to saving one manual step on a case that has not yet occurred,
against the cost of a script, a subprocess, temp files and a format boundary.

**PyMeshLab stays.** It is array-native and in-process, so it costs nothing to
keep and makes a fast_simplification failure a non-event rather than manual
work. It is Blender specifically that is expensive.

**The nice consequence**: with Blender gone from decimation, *both* remaining
rungs work on arrays, so **decimation never touches the disk at all**.
`decimate.blender` can be deleted, and the PLY work scoped above becomes
`repairer`-only.

#### The marker: `.undecimated.stl`

A new result indicator beside the existing ones, and
`Indicator.UNDECIMATED` alongside them in `libs/indicators.py`.

**Why a marker rather than a log line.** Decimation is a deliverable. A mesh
that silently ships undecimated gets re-decimated by the printer, which
reintroduces the non-manifold edges this tool exists to remove — so "not
decimated" must be a state the *filesystem* records, not a line in a log nobody
reads. It is also trivially checkable: the indicator scan already tests for
sibling markers, so one more suffix costs a single `os.path.exists`, and an
existing marker means the file is not retried.

**It is a full copy of the source, like every other signal file** — never an
empty marker, never a hardlink. These are fallback prints. An undecimated mesh
is a particularly good one: it is a complete, printable model that simply was
not reduced, so it can go to the plate or through Bambu Studio's simplify as
it stands.

**What must survive from the current code**: `decimate()` returns
`Rung.FAILED` with the input mesh unchanged and every attempt recorded in
`attempts`. The caller turns that into the marker. The distinction between
"not decimated" and "decimated badly" is the whole reason the input is returned
untouched rather than a partial result.

