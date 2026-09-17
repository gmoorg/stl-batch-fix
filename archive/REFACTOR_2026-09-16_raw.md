# Raw session record — 2026-09-16 (archived)

The unedited running narrative of one session: the repair-tool survey, its
several reversals, and the work that produced `libs/welder.py`.

**This is the working transcript, not the conclusions.** It is kept because the
measurements in it are expensive to reproduce — three-way tool comparisons,
per-step vertex-loss traces, timing on real models — and because several
entries record approaches that *failed*, which is the kind of thing a later
reader retries otherwise.

**The conclusions live in `REFACTOR_DECISIONS.md`**, under "The 2026-09-16
repair work". Where the two disagree, that file wins: this one contains
superseded claims that were corrected later in the same session, left in place
so the reasoning chain is visible.

Every entry here is also in git history under its own commit.

---
### Measured — stored STL normals DO disagree with winding, on 62 files

**Scanned 2026-09-16**, all 665 readable binary STLs in `Fixing`, `Boris` and
`Done`, comparing each triangle's stored facet normal against one computed from
its winding.

| | |
|---|---|
| files scanned | 665 |
| files with at least one disagreement | **62** (9.3%) |
| total disagreeing faces | **1,107,820** |

Worst offenders, all from one set:

| faces | share | file |
|---|---|---|
| 118,760 | **22.6%** | `Fae/Aine Noon Fae/.../platform_supported.stl` |
| 271,455 | 14.2% | `Fae/Aine Noon Fae/.../UMesh_mushrooms.stl` |
| 19,895 | 12.2% | `.../upperPart_supported.stl` |
| 242,835 | 10.5% | `.../mushrooms1.stl` |

The Daki set disagrees consistently too, at 0.1-0.44% per file.

**Why this was measured.** `normal_vote` in `stl_batch_fix.blender` — which the
script review calls *"the cleverest thing in the file"* and says should survive
the refactor intact — uses the STL's **stored per-facet normals** as ground
truth for winding: it flood-fills per connected component and flips a component
when more of its faces disagree than agree. That only does anything when the
stored normals carry information the winding does not.

**A correction to record.** I checked three large files (Mandy body, Amidara
base, Mandy clothed head), found 100% stored normals and **zero** disagreement,
and concluded the signal "carries no independent information" and that
`normal_vote` could be dropped. That was wrong. Three files chosen for size are
not a sample; the clean ones are simply the common case. Fourth instance today
of generalising from an unrepresentative sample — after `shells()` "nearly
free", the pointer-jumping benchmark, and vertex-vs-edge connectivity.

#### What it does to the PLY-at-the-Blender-boundary plan

`wm.ply_export`'s normals are **per-vertex** — the RNA description is *"Export
specific vertex normals if available, export calculated normals otherwise"* —
so PLY cannot carry a per-face stored normal. Routing repair through PLY would
silently discard the winding evidence on 62 files, and `normal_vote` would
degrade to comparing winding against itself: it can then only ever report
`agree: N, disagree: 0` and flip nothing, while still printing as though it
ran.

So the boundary question splits, and the two halves are no longer one decision:

- **Decimation's boundary** — already gone with D19; no normals involved.
- **Repair's boundary** — three options, none obviously right:
  1. the script keeps parsing STL itself, retaining review problems #1-#3
     (buffer sized before filling, triangles assumed, header trusted);
  2. the normals travel separately alongside the PLY;
  3. **`normal_vote` moves to our side entirely** — it is flood-fill over
     connected components plus a per-component majority vote, and
     `scanner.shells()` already does the flood-fill. `mesh_io.load` would keep
     the stored normals beside the geometry, and the vote becomes an array
     function with no Blender involvement, testable like everything else.

Option 3 is the larger change and the only one that makes the boundary clean
rather than working around it. Not yet decided.

Raw scan output kept at
`scratchpad/keep_normalscan.tsv` (tris, stored%, disagreeing faces, %, path).

### Survey — most geometric repair runs on 0.8% of files

**Asked 2026-09-16**: *"who checks for inverted normals when Blender is not
involved?"*, then the same question for T-junctions and degenerate faces.
Answered by grep, not by memory.

| capability | Python side | in the Blender script | actually runs on |
|---|---|---|---|
| inverted normals (`normal_vote`) | **nothing** | yes | 6 files of 768 |
| T-junction split | `MERGE_DIST` constant only | 6 references | 6 of 768 |
| wire edges / fin vertices | **nothing** | 8 references | 6 of 768 |
| merge doubles | constant only | 5 references | 6 of 768 |
| degenerate faces | `scanner` **counts** them | 3 references | counted always, fixed on 6 |
| hole filling | `meshfix.repair()` | 5 references | wherever repair runs |

`grep -rn 'normal_vote\|inverted\|flip' --include='*.py'` returns **nothing**.

**Two of these are worse than absent.** `scanner.scan()` reports a `degenerate`
count on every file and nothing acts on it. `MERGE_DIST` exists in the config,
in the TUI, and is passed into the Blender script — no Python code uses it.
Both look like working features from the outside.

**`find_winding_seams` is not a substitute for `normal_vote`.** It finds edges
where two faces disagree *with each other*. A region that is internally
consistent and uniformly backwards has **zero** seam edges, because no two
neighbours disagree. The stored facet normals are the only evidence for that
case, and nothing on the Python side reads them.

**And the clean-copy shortcut makes it concrete:**

```python
if (nm_src == 0 and open_src == 0 and not _seam_loops
        and (MAX_FACES == 0 or n_tris <= MAX_FACES)):
    shutil.copy2(src, dst)
    L("result: ok (clean copy — no repair needed)")
```

A file with inverted normals, T-junctions, wire edges or degenerate faces — but
no holes and no non-manifold edges — is copied through, reported `ok`, and
never opened by Blender. The design doc names the symptom already: *"a region
renders black in viewers and in Bambu while every defect count reads zero."*

#### What this does to the repair design

The question was framed as "how does `normal_vote` survive the PLY boundary".
That framing was too small. The real finding is that **a whole class of
geometric repair is gated behind a door that opens for 0.8% of files**, and the
boundary question is downstream of deciding whether that should change.

Every one of these operates on arrays and would be testable on our side:
`scanner.shells()` already does the flood-fill `normal_vote` needs, degenerate
faces are already detected, and T-junction splitting and doubles-merging are
vertex-distance work of the kind `mesh_io.load` already does.

But that converts "port a Blender script" into "reimplement six BMesh
operations in numpy", which is a different size of commitment and a different
shape of project. **Not decided.** Recorded so the choice is made deliberately
rather than by porting momentum.

### Measured — an inverted mesh is invisible to every check, and PyMeshFix ignores it

**Tested 2026-09-16** on a synthetic fixture, after a real-file attempt proved
unreadable.

**The real file was the wrong subject, and the user said so first.**
`platform_supported.stl` was picked for having the highest disagreement in the
scan (22.6%) — but the name says it: a **resin model with printed supports**.
Its 4,526 shells are support pillars, and the normals at their tips are
unreliable by construction. PyMeshFix returned 266,918 of 525,254 faces and 1
shell, having eaten 4,525 pillars. Nothing about that is attributable to
inversion: too many defects at once, none of them isolated.

**The synthetic answer is unambiguous.** A closed sphere, and the same sphere
with every face reversed:

| | faces | nm | open | degenerate | seams | shells | signed volume |
|---|---|---|---|---|---|---|---|
| correct | 760 | 0 | 0 | 0 | 0/0 | 1 | **+4094.9** |
| inverted | 760 | 0 | 0 | 0 | 0/0 | 1 | **−4094.9** |

Identical on every check the pipeline has. **Signed volume is the only
discriminator**, and it is already computed for the volume-loss guard.

**PyMeshFix is a clean no-op**: 760 faces in, 760 out, volume unchanged at
−4094.9, `ok=True`. It neither detects nor fixes inversion. So the capability
cannot be delegated to it, which was worth establishing before designing around
either answer.

**`find_winding_seams` cannot see this, by construction.** It reports edges
where two faces disagree *with each other*; a uniformly reversed mesh has no
such edge. Zero seams, and correctly so.

#### The stored-normal signal is mostly junk

Re-examining `platform_supported.stl` with a threshold rather than a sign:

| dot(stored, winding) | faces | reading |
|---|---|---|
| > 0.9 | 280,738 | agrees |
| −0.1 … 0.1 | 26,093 | perpendicular — junk |
| −0.9 … −0.1 | 99,769 | skewed — junk |
| < −0.9 | **11,850** | genuinely opposite |

All normals are unit length, so none is zeroed — but 125,862 point at angles
that are neither with nor against the winding, and **zero shells are entirely
opposite**. The earlier "1,107,820 disagreeing faces across 62 files" counted
any negative dot product and so conflated real inversion with exporter noise.
That number should not be quoted; the scan wants re-running at `< -0.9`.

This weakens the case for porting `normal_vote`: on this file it would be
voting on noise, with no component actually needing a flip.

#### Two corrections

**`libs/meshfix.py` overstated its guarantee.** Its docstring implied that
omitting `remove_smallest_components` prevents shell deletion. It does not —
`clean()` alone discarded 4,525 shells on the file above. The two-tetrahedra
test that "verified" it is too small to show the behaviour. Docstring fixed;
splitting first is what prevents the loss.

**A new fixture**, `tests/fixtures/inverted.stl`, generated by
`make_fixtures.py`: a closed sphere with every face reversed. `check()` now
reports signed volume, because without it the inverted fixture is
indistinguishable from a correct one in that table.

### Settled — inverted normals are a NON-DEFECT; do not build detection for them

**Tested end to end 2026-09-16, and the answer retires the question.**

A closed sphere with **every** face reversed was put through the whole chain:

| stage | result |
|---|---|
| `scanner` (nm, open, degenerate, seams, shells) | all zero — identical to a correct sphere |
| `meshfix.repair()` | clean no-op: 760 faces in, 760 out, volume unchanged, `ok=True` |
| commercial online repair service | **"0 Inverted normals"** — its dedicated check reports nothing |
| Bambu Studio — render | renders as a normal opaque sphere |
| Bambu Studio — **slice** | **slices fine** (confirmed) |

Only signed volume distinguishes it: **+4094.9 against −4094.9**.

**Confirmed on all five stages**, the slice last and separately. (A note on
process: the table briefly claimed "slices fine" before it had been tested — I
read the user's confirmation that all four *rendered* as covering slicing too.
It was corrected while the test ran, and then the test agreed with it. Being
right by luck is not being right, and the correction was worth making.)

**Nothing downstream treats this as a defect, so it is not one.** A slicer
reconstructs orientation from geometry rather than trusting the file's winding,
which is the sane thing for it to do since it has to produce a solid either
way.

**Consequence: `normal_vote` is not ported, and not rebuilt on our side.** The
previous entry was heading toward reimplementing its flood-fill-and-vote in
numpy. That would have been machinery for a condition with no consequence
anywhere in the chain.

**The commercial tool has the same blind spot we do**, which is itself worth
recording: it offers an "Inverted normals" check and reports **0** on a mesh
that is entirely inside-out, while reporting **40** on `fixture_seam.stl`. So
its check means what `find_winding_seams` means — faces disagreeing *with each
other*. A uniformly reversed mesh is locally perfect everywhere and scores
clean. We are not missing a standard check that everybody else has.

#### The seam case is different and is NOT retired

`fixture_seam.stl` — one sphere with only its cap reversed — was flagged (40
inverted normals) and genuinely repaired: 40 seam edges / 1 closed loop and
volume +2146.2 became **0 edges / 0 loops and +4095.2**, matching the correct
sphere to rounding.

Worth noting how it repaired: it **re-wound 554 of 760 faces**, and the face
set is not even identical ignoring winding — so it rebuilt the surface rather
than flipping the offending cap. Vertex and triangle counts unchanged, so
nothing was added or lost. I had assumed from the log that it flipped 40 faces
in place; comparing the arrays showed otherwise, and the counts the log prints
would never have revealed it.

#### A design-doc note that now needs revisiting

The doc says of winding seams: *"a region renders black in viewers and in Bambu
while every defect count reads zero."* That symptom is real and recorded from a
real model, but it cannot describe the **uniform** case, which Bambu renders and
slices without complaint. It must describe the **partial** case — a region
disagreeing with its neighbours, where a renderer cannot resolve a consistent
surface. The note should say which.

#### Retired with it

The planned re-scan of the collection at `dot < -0.9`, to separate genuine
inversions from exporter noise, is **no longer worth running**. Whether 62 files
or 6 carry opposed stored normals does not matter if the condition has no
consequence.

### Measured — T-junctions: `scanner` detects them, PyMeshFix repairs them

**Tested 2026-09-16** on a synthetic fixture, the same method that settled the
inverted-normals question.

**The fixture**: a closed 760-face sphere with one vertex inserted at the
midpoint of one edge, used by the two faces on one side and *not* by the
neighbour across it. The surfaces stay geometrically flush — no measurable gap
— but are topologically unjoined, and no vertex merge closes it because no two
vertices are coincident. `tests/fixtures` equivalent written to
`_validate/sphere_tjunction.stl`.

**Unlike inverted normals, this one is visible to us:**

| | faces | verts | nm | open | seams | volume |
|---|---|---|---|---|---|---|
| control | 760 | 382 | 0 | **0** | 0/0 | +4094.9 |
| T-junction | 761 | 383 | 0 | **3** | 0/0 | +4094.9 |

Three open edges, so the file never takes the clean-copy shortcut and always
reaches repair. Volume is unchanged, which confirms signed volume is specific
to orientation and blind to this.

**A commercial repair service agrees with our detection**: *3 Naked edges,
1 Planar hole*. Worth recording after the inverted case, where we shared a
blind spot with the same tool — here our numbers match a mature implementation
exactly.

**PyMeshFix repairs it, and arguably better than the service does:**

| | faces | verts | approach |
|---|---|---|---|
| source | 761 | 383 | — |
| `meshfix.repair()` | **760** | **382** | removed the T-vertex, merged the split face back |
| online service | **762** | 383 | kept the T-vertex, added a triangle to patch the crack |

PyMeshFix returns *exactly* the control's counts with a volume change of
**+0.000** — it undid the T-junction rather than papering over it, without
moving any geometry. The service's answer is the more conservative one (it
removes nothing), and both are valid; for a spurious T-vertex, undoing it is
the better result.

#### What this means for the survey

Two of the capabilities that live only inside `stl_batch_fix.blender` now turn
out not to need porting, for **opposite** reasons:

| capability | detected by us? | needs building? | why |
|---|---|---|---|
| inverted normals | no | **no** | nothing downstream cares — renders and slices fine |
| T-junctions | **yes** (open edges) | **no** | `meshfix.repair()` already fixes it, on every repaired file |

The Blender script's T-junction splitter and the unused `MERGE_DIST` constant
may therefore be solving a problem PyMeshFix already handles — and handling it
on 0.8% of files where PyMeshFix handles it on all of them.

**Not yet settled**, and the honest limit of this test: the fixture is *one*
synthetic T-junction on an otherwise perfect sphere. The script's splitter
carries a `MAX_ITER = 200` cap, which suggests it was written for meshes with
hundreds. A harder fixture — many T-junctions, or T-junctions along a seam
between two welded surfaces, which is the shape the real cases take — would
test whether PyMeshFix still copes.

Still untested from the survey: wire edges, fin vertices, doubles-merging,
and whether `scanner`'s degenerate-face count needs anything acting on it.

### Measured — the rest of the survey, on synthetic fixtures (2026-09-16)

Same method throughout: one defect on an otherwise perfect 760-face sphere,
run through `scanner`, then `meshfix.repair()`, then a commercial online repair
service for an independent verdict. Fixtures and their `_pmf` outputs are in
`/mnt/sda2/STL/_validate/`.

| fixture | source | `scanner` sees | PyMeshFix result | volume | verdict |
|---|---|---|---|---|---|
| control | 760/382 | clean | — | +4094.9 | — |
| **fin** | 761/383 | nm=1, open=2 | **756/380** clean | +4093.1 | deleted the fin, trimmed slightly |
| **degenerate** | 761/382 | nm=1, open=1, deg=1 | **760/382** clean | +4094.9 | exactly the control |
| **t-junction x150** | 910/532 | open=450 | **1000/502** clean | +4085.9 | all 450 closed, 0.2% volume loss |
| **doubles** | 1520/764, 2 shells | **clean** | **418/211** | **+2047.4** | **destructive** |

**Independently confirmed clean**: the service reports all zeros on both
`sphere_fin_pmf.stl` and `sphere_tjunction_many_pmf.stl`. So PyMeshFix's
repairs are good by a mature implementation's judgement, not merely by ours.

**`MAX_ITER = 200` is not a limit in practice.** The Blender splitter's cap
suggested it was built for meshes with hundreds of T-junctions; PyMeshFix
handled 150 at once without difficulty. Note it *patches* rather than *undoes*
at that scale — 1000 faces out, not back to 760 — where on a single T-junction
it undid it exactly. Both give a clean mesh.

#### The doubles case is the one real gap

Control volume +4094.9. Two coincident spheres: +8189.7. PyMeshFix returns
**+2047.4** — *half of one sphere*, from 418 faces where a correct merge gives
about 760. It discarded one shell and then ate half the other.

This is the same shell-eating seen on `platform_supported.stl`, and it is what
`MERGE_DIST` and Blender's `remove_doubles` exist for. **PyMeshFix makes this
one worse, not better.**

Also worth noting: `scanner` reports the doubles fixture as **completely
clean** — nm=0, open=0, degenerate=0. Only the shell count (2) hints at
anything, and two shells is legitimate on a real model. So we cannot currently
distinguish "two parts" from "one part duplicated".

#### An observation about the online service

On `sphere_tjunction_many_pmf.stl` it reported **0 of everything** and then
changed the file anyway: +2 verts, +6 faces. So "0 defects" and "unchanged" are
not the same thing for that tool — its repair pass evidently does tidying its
analysis does not report. Worth remembering when reading its output as ground
truth.

#### Survey status

| capability | detected | needs building | why |
|---|---|---|---|
| inverted normals | no | **no** | nothing downstream cares |
| T-junctions | yes | **no** | PyMeshFix fixes them, at scale |
| fin vertices | yes | **no** | PyMeshFix fixes them |
| degenerate faces | yes | **no** | PyMeshFix fixes them exactly |
| **doubles / coincident** | **no** | **probably yes** | PyMeshFix is destructive; nothing detects it |
| wire edges | untested | — | — |

**The commercial service calls the wreckage clean.** Put
`sphere_doubles_pmf.stl` — half a sphere, 418 faces, volume +2047.4 against a
control of +4094.9 — through the same service and it reports **0 of
everything**, then adds 57 verts and 114 faces anyway.

It is not wrong about what it sees. A half-sphere is a perfectly valid
watertight mesh: no naked edges, no holes, one shell. The tool has no way to
know that half the model is missing, because **every defect check in this chain
is local**. None of them compares the file against what it should have been.

That is the general lesson, and it is worth more than the doubles case itself:

| check | question it answers |
|---|---|
| naked edges, holes, non-manifold, degenerate | is this mesh self-consistent? |
| **volume before vs after** | **did the repair destroy anything?** |

Only the second can catch a repair that succeeded by its own standard and
wrecked the model. It is already in the pipeline as the volume-loss guard, and
this is the strongest evidence yet for keeping it: three independent
implementations — `scanner`, PyMeshFix and a commercial service — all call
`sphere_doubles_pmf.stl` clean, and only the before/after comparison sees the
damage.

**Still to test in Bambu**, which decides whether the doubles gap matters at
all: if two coincident shells slice identically to one, the destruction never
happens because the file would never need repairing.

---

## Where the refactor stands (2026-09-16, end of session)

**Ten modules, 260 tests across nine suites, all green.**
`stl_batch_fix.py` untouched and still frozen; `main` at `4c1c0c2`.

| module | owns | tests |
|---|---|---|
| `pool` | the worker loop, nothing domain-specific | 24 |
| `indicators` | what the filesystem says about a file | 21 |
| `blender` | launching Blender, killing it, reading markers | 32* |
| `mesh_io` | probe, load, write — the only writer | 44 |
| `scanner` | defect counts off one edge map | 46 |
| `splitter` | cutting into parts and merging back | 27 |
| `meshfix` | PyMeshFix on the arrays | 23 |
| `decimator` | the two-rung ladder | 19 |
| `converter` | the preparation walk | 20 |
| — | `test_pipeline` (the frozen script) | 36 |

\* `test_blender` runs separately; it launches real Blender and takes ~20 s.

### Built this session

`splitter` and `meshfix` are new. `scanner` gained `seam_edges()` and had two
bugs fixed (self-edges counted as closed loops; vertex- instead of
edge-connected shells). `decimator` lost its Blender rung. `Mesh` gained a
required `destination`. scipy became a hard requirement.

### Still to build

- **`repairer`** — the ladder over `meshfix` and `blender`, with the volume
  check deciding when a repair destroyed rather than fixed. **Much thinner than
  assumed at the start of the session** — see the survey results above.
- **`blender_fx/repair.blender`** — and it is now unclear how much of the
  593-line original it needs to carry.
- **Orchestration** — the four-line pipeline `splitter`'s docstring describes.
- Intermediate suffixes in `indicators`, per D21.

### Open questions, in the order they would need answering

1. **Does the doubles case matter to Bambu?** If two coincident shells slice
   identically to one, the one real gap in the survey closes itself. Fixture is
   `_validate/sphere_doubles.stl`. *This is the cheapest open question and it
   decides whether anything needs building at all.*
2. **Wire edges** — the last untested survey item.
3. **How thin can `repair.blender` be?** Four of six capabilities need no
   porting. What remains is the 12-pass repair loop and the hole-filling
   escalation, and it is not established that those beat PyMeshFix either.
4. **The split-upfront decision** (re-opened) — closed-loop detection cannot
   predict what PyMeshFix will do, so the trigger has to stay the volume check
   after the fact.
5. **Debris by bounding box, and the nm-ratio idea** — both recorded as
   discussion points, neither designed.
6. **PLY at the repair boundary** — its original justification (deleting
   `decimate.blender`'s hand-packed byte loop) evaporated when D19 deleted that
   file. Weaker case now; reconsider when `repairer` lands.

### The methodological thread, since it recurred all session

Six times a confident claim of mine was wrong and a measurement corrected it:
`shells()` "nearly free" (was 5-10x the load), the pointer-jumping benchmark
(15x faster synthetically, 2x slower on a real mesh), vertex-vs-edge
connectivity (implementation, test and oracle all agreed and all wrong), stored
normals "carry no information" (62 files say otherwise), `Done/` being pipeline
output (it is source archives), and "slices fine" written before the slice was
tested.

Each was caught by the user asking a question I could not answer, or by running
against real data rather than a fixture I had chosen. The standing rules that
came out of it:

- a claim about cost or behaviour ships with a number or an admission there is none;
- a decision that reverses existing behaviour ships with the reason that behaviour exists;
- a differential test proves agreement, not correctness — where an independent
  implementation exists, check against that, on real geometry, at least once;
- a performance fixture must resemble real input in *structure*, not merely in size.

And the finding that generalises furthest, from the doubles case: **every
defect check in this chain is local.** Ours, PyMeshFix's, and a commercial
service's all ask "is this mesh self-consistent" — none asks "did the repair
destroy anything". Only volume before-vs-after answers that, and it caught what
three independent implementations missed.

### Probe meshes

`tools/make_probe_meshes.py` regenerates the eight single-defect meshes the
survey used. They are written into the collection (`/mnt/sda2/STL/_validate`),
which is deletable by design — the generator is the durable artefact, not the
meshes.

### CORRECTION — "PyMeshFix repairs it" meant topology, not surface quality

**Seen in Bambu 2026-09-16, after the survey above was recorded.** The user
loaded the `_pmf` outputs and looked at them. Three defects are visible that
every numeric check called clean:

| mesh | what the numbers said | what it looks like |
|---|---|---|
| `sphere_doubles_pmf` | clean; volume +2047.4 | **half a sphere — an open bowl** |
| `sphere_tjunction_many_pmf` | clean; volume within 0.2% | **visible dents and creases** |
| `sphere_fin_pmf` | clean; volume −0.04% | a small defect |

**The survey entry above is wrong where it says fins, degenerate faces and
T-junctions "need no new capability".** That verdict rested on defect counts
and volume, and it should have said: *PyMeshFix produces a topologically clean
mesh*. For `tjunction_many` that is true and insufficient — 450 open edges were
closed and the surface was deformed doing it.

The degenerate-face result still stands unqualified: PyMeshFix returned
*exactly* the control's 760 faces and 382 vertices, so there is nothing to be
deformed.

#### The third category of check

The doubles case already showed that every defect check here is **local** —
none asks whether the repair destroyed anything. The dents show a further gap:

| question | what answers it |
|---|---|
| is this mesh self-consistent? | nm, open edges, degenerate, shells |
| did the repair destroy anything? | volume before vs after |
| **does the surface still look like the original?** | **nothing we have** |

A bad patch preserves volume almost exactly — `tjunction_many_pmf` is within
0.2% — while visibly deforming the surface. Volume cannot see it, and neither
can any topological count.

Candidate measures, none tried: Hausdorff distance to the pre-repair mesh
(PyMeshLab has a filter), or per-vertex displacement, or dihedral-angle change
across the patched region. Any of them would need a threshold calibrated
against what the user considers acceptable, which is a judgement rather than a
measurement.

**Why this matters beyond the fixtures.** The pipeline currently accepts a
repair whenever the defect counts reach zero and volume holds. On this evidence
that is not sufficient to conclude the model is undamaged, and the failure mode
is silent — a dented print that slices without complaint.

**The method that found it**: loading the output and looking at it. Three
independent implementations reported these meshes clean. No amount of
cross-checking numeric tools would have caught it.

### NEXT SESSION — first task: run the probe spheres through Blender

**The control the survey never ran.** Every fixture was measured against
`scanner`, PyMeshFix, a commercial service and Bambu — but **not** against the
incumbent, `stl_batch_fix.blender`. Its 12-pass repair loop is the thing the
survey was implicitly arguing could be dropped, and it was never asked to
repair anything.

That is a hole in the reasoning, not merely a missing datapoint. The survey
concluded "PyMeshFix handles these, so the Blender capabilities need no
porting" without establishing what the Blender capabilities actually produce on
the same inputs.

**What to run.** Regenerate the probes if the collection was cleared:

```bash
.venv/bin/python tools/make_probe_meshes.py
```

Then put each through the frozen script's Blender path — `fix_stl(src, dst,
merge_dist=MERGE_DIST)` in `stl_batch_fix.py`, which renders `BLENDER_SCRIPT`
and parses `BLENDER_OK` / `BLENDER_OPEN` / `BLENDER_UNREPAIRED` /
`BLENDER_EMPTY`. Write results beside the `_pmf` ones as `_bl` so all three
versions of each mesh sit together.

**What to measure, and in this order** — the last one is the point:

1. `scanner.scan()` — nm, open, degenerate, shells
2. signed volume against the control's +4094.9
3. face and vertex counts against the control's 760 / 382
4. **load it in Bambu and look at it**

Step 4 is what found the dents that steps 1-3 all missed. It cannot be skipped
and cannot be delegated to a numeric check we do not have.

**The cases that matter most:**

| fixture | why |
|---|---|
| `tjunction_many` | PyMeshFix closed all 450 open edges and **dented the surface**. Does Blender's T-junction splitter — the thing `MERGE_DIST` feeds — do better? This is the direct comparison the survey lacked. |
| `doubles` | PyMeshFix returned half a sphere. `remove_doubles(dist=merge_dist)` is exactly the operation for this, so Blender should merge the two coincident spheres into one. If it does, the one real gap has an owner. |
| `fin` | PyMeshFix leaves a small visible defect. The script's fin-vertex removal targets this specifically. |
| `inverted` | Settled as a non-defect, but `normal_vote` should flip it — worth confirming the mechanism works even though the condition does not matter. |

**What the answers would change.** If Blender produces clean *and* undamaged
surfaces where PyMeshFix produces clean-but-dented ones, the survey's
conclusion inverts: those capabilities are not redundant, they are better, and
`repairer` needs them rather than needing to drop them. If Blender does no
better, the survey stands and `repairer` stays thin.

Either way this is one session's work with fixtures that already exist, and it
should happen **before** `repairer` is designed around either answer.

### CORRECTION — the survey's conclusion is inverted: PyMeshFix is the weaker tool

**2026-09-16, after putting the *source* fixtures through the commercial
service.** The survey only ever fed it PyMeshFix's *output*. Fed the sources
directly, it repairs all three to **perfect spheres**, verified by eye in
Bambu.

| fixture | PyMeshFix | the service, same source |
|---|---|---|
| `tjunction_many` | 1000 faces, **visibly dented** | 532 verts unchanged, +150 faces — **perfect** |
| `fin` | 756 faces, small visible defect | +2 verts, +5 faces — **perfect** |
| `doubles` | 418 faces — **half a sphere** | 1520 → **816** faces — **perfect** |

**`doubles` is the decisive one.** 816 faces is one sphere's worth: the service
**merged** the two coincident copies. PyMeshFix discarded one and ate half the
other. That is `remove_doubles(dist=merge_dist)` behaviour — precisely the
operation `MERGE_DIST` feeds in `stl_batch_fix.blender`, and precisely the
capability the survey proposed dropping.

**`tjunction_many` shows why the dents happened.** The service kept **all 532
vertices** and added exactly 150 faces — one per T-junction, patching each
without touching the surrounding surface. PyMeshFix restructured (532 → 502
verts) and deformed the sphere doing it.

#### What this does to the survey

The entry above concluded that fins, degenerate faces and T-junctions "need no
new capability" because PyMeshFix handles them. **That conclusion is wrong**,
and the correction two entries up — "it meant topology, not surface quality" —
did not go far enough. It is not merely that PyMeshFix's output was unverified
on surface quality; it is that **another tool does the same repairs correctly**,
so the capability is not redundant.

Only the degenerate-face case survives: PyMeshFix returned exactly the
control's 760 faces and 382 vertices, with nothing to deform.

**Root cause of the wrong conclusion.** I measured PyMeshFix against defect
counts and volume, both of which it satisfied, and never ran the incumbent — or
any second repairer — on the same inputs. A comparison needs two columns.

#### This makes the Blender run decisive, not a control

The next-session task recorded below was framed as a control. It is now the
test that decides the shape of `repairer`:

- **If Blender matches the service** — merges `doubles`, patches
  `tjunction_many` without denting — then its repair path is *better* than
  PyMeshFix's and those capabilities must be kept, not dropped. `repairer`
  becomes a ladder where Blender is not the last resort but the better tool for
  several defect classes.
- **If Blender is no better than PyMeshFix**, then neither is adequate, and the
  gap is real and unowned — the pipeline would be shipping dented repairs today
  and nothing measures it.

Either answer changes the design. Neither is knowable from what has been run.

### MEASURED — the three-way comparison: Blender wins `doubles` and `fin`, loses `tjunction_many`

**Run 2026-09-16.** Every probe sphere through `fix_stl(src, dst,
MERGE_DIST=0.01)` — the frozen script's Blender path — with PyMeshFix's and the
commercial service's outputs alongside. Control: **760 faces, 382 verts,
+4094.9**.

| fixture | Blender | PyMeshFix | service |
|---|---|---|---|
| `doubles` | **760 / 382 / +4094.9 — exactly the control** | 418 / 211 / +2047.4 | 816 / 410 / +4095.2 |
| `fin` | **760 / 382 / +4094.9 — exactly the control** | 756 / 380 / +4093.1 | 766 / 385 / +4095.2 |
| `degenerate` | 760 / 382 / +4094.9 | 760 / 382 / +4094.9 | — |
| `tjunction` | 762 / 383 / +4094.9 | 760 / 382 / +4094.9 | — |
| `tjunction_many` | 824 / **414** / +4083.8 | 1000 / 502 / +4085.9 | **1060 / 532 / +4095.2** |
| `inverted` | **unchanged** (−4094.9) | no-op | reports 0 defects |
| `seam` | **unchanged** (+2146.2) | — | **fixed** (+4095.2) |

**`doubles` settles the one real gap.** Blender returns *exactly* the control —
it merged the two coincident spheres. That is `remove_doubles(dist=merge_dist)`
doing precisely what `MERGE_DIST` exists for, and it is the capability the
survey proposed dropping. PyMeshFix returns half a sphere; the service is close
but carries a slight excess.

**`fin` likewise**: Blender exactly the control, where PyMeshFix loses volume
(the visible defect) and the service adds geometry.

**But Blender is the *worst* of the three on `tjunction_many`.** It dropped 118
vertices (532 → 414) and lost the most volume. The service kept all 532 and
matched the control. So the Blender T-junction path restructures harder than
either alternative at scale — the opposite of what the `doubles` and `fin`
results suggest, and a reminder that "which tool is better" has no single
answer.

#### Two defects nothing in the pipeline repairs

`inverted` and `seam` come back **byte-identical to their sources** — same
face/vertex counts, same volume. `inverted` is settled as a non-defect, so that
is correct behaviour. **`seam` is not.** It is the hair-over-scalp case the
entire seam-split mechanism exists for, and the incumbent repair path leaves it
untouched at +2146.2 against a control of +4094.9. The commercial service
repairs it to +4095.2.

That is a live gap in the current pipeline, not a refactor question.

#### `BLENDER_OK` means "the script ran"

All eight fixtures reported `BLENDER_OK`, including the two it did not repair
at all. The marker is not a verdict on the mesh — the same trap as PyMeshFix's
`ok=True`, and the reason `_post_verify` exists. Worth keeping in mind when
`repairer` reads these markers.

#### What this does to the design

No tool dominates. A ladder over both is justified, and **which rung to prefer
depends on the defect**:

| defect | best tool |
|---|---|
| coincident/duplicate geometry | **Blender** (`remove_doubles`) |
| stray fins | **Blender** |
| degenerate faces | either (tie) |
| many T-junctions | neither of ours — the service beat both |
| winding seams | neither of ours — split first, as the pipeline already does |

**Still to check by eye**: the `_bl` outputs in Bambu. Numbers missed the dents
on PyMeshFix's output once already, and `tjunction_many_bl` at 414 vertices is
the obvious candidate for the same problem.

### CONFIRMED BY EYE — `tjunction_many_bl` is dented; everything else is spherical

**Checked in Bambu 2026-09-16**, the step the numbers cannot replace.

Seven of the eight `_bl` outputs render as clean spheres. **`sphere_tjunction_many_bl.stl`
has a visible gash** — a wedge-shaped tear across the surface, the same failure
mode PyMeshFix produced on the same fixture.

**So `tjunction_many` is unanimous in the wrong direction:**

| tool | faces | verts | volume | by eye |
|---|---|---|---|---|
| Blender | 824 | 414 | +4083.8 | **dented** |
| PyMeshFix | 1000 | 502 | +4085.9 | **dented** |
| service | 1060 | 532 | +4095.2 | clean |

Both of our tools deform the surface closing 450 open edges; only the
commercial service patches without restructuring — it kept **all 532 vertices**
and added exactly 150 faces, one per T-junction.

**The good results are confirmed too.** `doubles_bl` and `fin_bl` measured
*exactly* the control (760 / 382 / +4094.9) and look right. So Blender's
`remove_doubles` and fin removal are genuinely correct, not merely
numerically plausible — which is the strongest evidence yet for keeping
`MERGE_DIST` and the capabilities the survey proposed dropping.

**Volume was a weak but real signal here.** Blender lost 11.1 units on
`tjunction_many` (0.27%) against 0.0 on the cases that came out clean. Not
enough to threshold on by itself — PyMeshFix lost 9.0 and was also dented,
while the service *gained* 0.3 and was fine — but the two dented outputs are
the two that lost volume, and the clean ones lost none. Worth keeping in mind
if a surface-quality measure is ever built: "volume changed at all" may be a
cheap first filter, with Hausdorff distance for the cases it flags.

**Method note.** Three tools, three numeric checkers and a slicer all called
`tjunction_many_bl` clean: nm=0, open=0, degenerate=0, one shell, volume within
0.3%. Looking at it took seconds. This is the second time in one session that
eye inspection overturned a conclusion every measurement supported.

### SOLVED (as a sequence, NOT as code) — the seam case

> **Read this first.** Nothing below is implemented. The sequence was run in a
> throwaway shell script and measured; the repo cannot do it. `splitter`,
> `mesh_io` and `scanner` provide every piece **except the flip**, which lives
> nowhere — the only `faces[:, ::-1]` in the tree is in `make_fixtures.py`,
> where it *builds* the inverted fixture. `stl_batch_fix.blender` and
> `stl_batch_fix.py` were not touched; both remain frozen. The flip belongs in
> `repairer` when that is built, because deciding a region is backwards is
> policy rather than geometry.

**Measured 2026-09-16.** `sphere_seam.stl` — one sphere with its cap reversed,
40 seam edges in 1 closed loop, volume **+2146.2** against a control of
**+4094.9** — is repaired to a **perfect reconstruction** by a sequence that
uses no repair tool at all.

| step | result |
|---|---|
| `splitter.by_seams()` | 760f → **520f + 240f**, each seam-free, each one shell, 40 open edges apiece |
| region volumes | +3120.5 and **−974.3** — the reversed cap is negative |
| flip the negative region | −974.3 → +974.3 |
| `splitter.merge()` | 760f / 422v / 80 open / 2 shells / **+4094.9** |
| `mesh_io.write` + `load` | **760f / 382v / nm=0 / open=0 / seams 0/0 / 1 shell / +4094.9** |

**Identical to the control on every measure.**

**Repairing the regions is what breaks it.** PyMeshFix caps each open region
into its own closed solid, which is correct in isolation and wrong here:

| approach | volume | vs control |
|---|---|---|
| split → repair (no flip) | +2936.5 | 71.7% — capped the reversed cap inside-out |
| split → flip → repair | +4355.9 | 106.4% — cap surfaces sit inside the sphere |
| **split → flip → merge → reload** | **+4094.9** | **100.0%** |

The regions must be left open so the merge rejoins them. A repair tool cannot
know that.

**The weld claim in `splitter.merge()`'s docstring is confirmed.** It says
*"coincident vertices at an old cut line are not welded — the next
`mesh_io.load` does that"*. Measured: 422 verts and 80 open edges collapse to
382 and 0 on write-and-reload. That claim was load-bearing here.

#### This corrects the morning's `normal_vote` conclusion

Recorded earlier: *"inverted normals are a non-defect; do not build detection
for them"*. That holds for a **uniformly** inverted mesh — nothing downstream
cares, Bambu renders and slices it fine.

**It does not hold after a seam split.** One region is then reversed *relative
to the other*, and flipping it is the entire repair. So the capability is
needed after all — but not as `normal_vote`'s per-component majority vote over
stored facet normals, which today's scan showed to be mostly exporter noise.
**Signed volume decides it**: a region with negative volume is reversed. Three
lines, using a measure already computed for the volume-loss guard, with no
dependence on stored normals at all.

That is a better mechanism than the one being considered for porting, and it
was reached from the opposite direction.

#### Where this leaves the seam defect

| tool | result |
|---|---|
| Blender `fix_stl` | **unchanged** — `BLENDER_OK`, +2146.2 |
| PyMeshFix alone | not applicable — no open edges to work on |
| commercial service | fixed, +4095.2 |
| **split → flip → merge → reload** | **+4094.9, exactly the control** |

Ours is now the best result available, and it needs nothing the modules do not
already have: `splitter.by_seams`, a signed-volume test, `splitter.merge`, and
a write/reload cycle.

**Confirmed by eye in Blender**: `_validate/sphere_seam_FIXED.stl` renders as a
clean sphere, including across the boundary ring where the two regions
rejoined. That check mattered — twice today a mesh with nm=0, open=0, one shell
and volume within 0.3% turned out to have a visible gash. This one matches the
control on every field including vertex count, and looks right as well.

### Note — an unrepaired seam mesh looks perfectly fine

**Observed 2026-09-16.** `fixture_seam.stl` — the **unrepaired** source, 40
seam edges in 1 closed loop, volume **+2146.2** against a control of +4094.9 —
renders as a clean sphere. Nearly half its enclosed volume is wrong and nothing
is visible.

Consistent with the inverted-normals finding: renderers reconstruct orientation
from geometry rather than trusting winding, and a reversed cap occupies exactly
the right space — same surface, traversed the other way. Nothing moves, so
nothing looks wrong.

**This contradicts the recorded symptom.** The design doc says of winding
seams: *"a region renders black in viewers and in Bambu while every defect
count reads zero."* This fixture reads zero on every count **and renders
normally**. Either the symptom needs something the fixture lacks, or it was
misattributed. Not resolved.

**The real justification for the seam mechanism is not visual.** It is that
**PyMeshFix deletes a region** when handed irreconcilable winding — 562,288
faces in, 394,432 out, a model's head gone. The failure is in the repair, not
the render, and it strikes a file that looks and measures fine going in.

Worth recording plainly, because "it looks fine" is exactly the observation
that would justify removing the machinery:

| | `fixture_seam.stl` |
|---|---|
| nm / open / degenerate | 0 / 0 / 0 |
| shells | 1 |
| renders in Bambu | **fine** |
| signed volume | **+2146.2 vs +4094.9 — 52% of control** |
| through PyMeshFix unsplit | **region deleted** |

Signed volume is the only check that sees anything wrong, and it is the same
measure that turned out to drive the repair (flip the negative-volume region).

#### Both seam repairs are idempotent

Re-running each repair on its own output changes nothing — checked because a
repair that keeps "fixing" the same file is a real failure mode, and the
pipeline reruns files.

**Ours**, three passes from the source:

| pass | faces | verts | seams | regions | flipped | volume |
|---|---|---|---|---|---|---|
| src | 760 | 382 | 40/1 | — | — | +2146.2 |
| 1 | 760 | 382 | **0/0** | 2 | 1 | **+4094.9** |
| 2 | 760 | 382 | 0/0 | **1** | 0 | +4094.9 |
| 3 | 760 | 382 | 0/0 | 1 | 0 | +4094.9 |

Pass 1 does the work. Passes 2 and 3 find **one** region — no seam to cut on —
so `by_seams` returns `(mesh,)` and nothing is flipped, merged or welded. That
is the "a mesh that does not split comes back as a list of one" contract doing
its job: a rerun costs one scan.

**The commercial service** likewise: `fixture_seam_fixed.stl` back through it
reports 0 of everything and returns 382 verts / 760 faces unchanged.

So both approaches are stable, and they agree on the answer:

| | detection | mechanism | result | idempotent |
|---|---|---|---|---|
| service | 40 inverted normals | flip the faces in place | +4095.2 | yes |
| **ours** | 1 closed seam loop | split → flip negative-volume region → merge → reload | **+4094.9** | yes |

**A possible simplification, not taken.** The service does not split at all —
it flips the reversed faces where they are, which is simpler than
split-merge-reload. Ours could do the same if the reversed region could be
identified *without* extracting it; `scanner.shells()` does not currently
distinguish regions across a seam boundary, so that would need new work. The
split path is built and tested, so this is recorded as an option rather than a
plan.

### Measured — decimation creates the defects on `whole-costume01`

**2026-09-16**, while testing the model the user recalled Blender breaking.

| | faces | nm | open | degenerate | shells | volume |
|---|---|---|---|---|---|---|
| source | 2,004,941 | **3** | 93 | 1 | 448 | +7,760.5 |
| after decimation to 900k | 900,000 | **2,263** | 20 | — | **491** | +7,758.7 |

`fast_simplification` takes a mesh with **three** non-manifold edges and
returns one with **2,263**, and adds 43 shells. Volume is preserved to within
0.02%, so nothing is deleted — the damage is topological, not geometric.

**This reframes the model's failure.** It does not reach repair carrying three
defects; it reaches repair carrying 2,263, and the step that created them is
upstream. Any comparison of repair tools on this file is measuring their
response to decimation's output, not to the model.

It is also consistent with the standing rule that decimation may leave
non-manifold edges and that the repair steps exist to clean up after it —
recorded in `decimator`'s docstring. What was not previously measured is the
*scale*: a 750-fold increase on this model.

**Not yet known**: whether this is particular to `whole-costume01` (444 of its
448 shells are debris under 100 faces, so decimation is collapsing a great many
tiny components) or general to fast_simplification on multi-shell meshes. One
model is not a sample — the mistake made four times already in this refactor.

The seam algorithm does **not** apply to this model, incidentally: it has
**0 seam edges and 0 closed loops**, so `by_seams` returns it unchanged. The
defect here is shells and non-manifold edges, not reversed winding.

### Measured — PyMeshLab is the strongest of the three tools we have

**2026-09-16.** Asked whether PyMeshLab has equivalents for the defects
PyMeshFix and Blender mishandle. It does, and it is exact on four of six —
including two that **nothing else we have can fix**.

| defect | Blender | PyMeshFix | **PyMeshLab** | online service |
|---|---|---|---|---|
| `doubles` | exact | **half a sphere** | **exact** | exact |
| `degenerate` | exact | exact | **exact** | — |
| `inverted` | unchanged | no-op | **exact — the only tool that fixes it** | reports 0 defects |
| `seam` | unchanged | n/a | **exact, without any split** | exact |
| `fin` | exact | lossy | 99.7%, 2 extra faces | exact |
| `tjunction_many` | **dented** | **dented** | **destroys or no-ops** | **exact — only tool** |

PyMeshLab is already a hard dependency, runs in-process, and takes numpy
directly — so this costs no subprocess and no new install.

#### The filter sequences that work

**`doubles`** — 1520f/764v/200% volume becomes **760f/382v/100%**, exactly the
control:

```python
meshing_merge_close_vertices(threshold=PercentageValue(0.1))
meshing_remove_duplicate_faces()
meshing_remove_unreferenced_vertices()
```

`merge_close_vertices` **alone makes it worse** — it collapses the vertices and
leaves both face sets, giving 1,140 non-manifold edges at 200% volume. All
three filters are needed.

**`seam`** — +2146.2 becomes **+4094.9**, exactly the control:

```python
meshing_re_orient_faces_coherently()
meshing_re_orient_faces_by_geometry()
```

`re_orient_faces_coherently` alone unifies the winding but picks the **wrong
direction** — it orients everything to the reversed cap, giving −4094.9.
`by_geometry` then turns the whole mesh outward.

**`inverted`** — `meshing_re_orient_faces_by_geometry()` alone takes −4094.9 to
+4094.9. The only tool in the chain that detects and corrects it.

**`degenerate`** — `meshing_remove_null_faces()`, exactly the control.

#### This simplifies the seam repair recorded as "solved" this morning

That entry describes split → flip the negative-volume region → merge → write →
reload. It is correct and it reaches the control. **Two PyMeshLab filters reach
the same answer with none of that machinery** — no split, no flip, no merge, no
weld.

The split-based sequence is still the more *general* mechanism (it isolates
regions for separate treatment, which matters when PyMeshFix would otherwise
delete one), but for the plain "a region is wound backwards" case the two-filter
version is what should be reached for first.

#### T-junctions are unfixed by anything we have

`meshing_remove_t_vertices` has a `threshold` defaulting to 40, which is why it
first appeared to be a no-op:

| threshold | Edge Collapse | Edge Flip |
|---|---|---|
| 40, 10 | no-op (450 open edges remain) | no-op |
| **1, 0.1** | **destroys the mesh — 910 faces to 0** | no-op |

`meshing_close_holes(maxholesize=30)` alone gets 450 open edges down to 70 but
introduces a non-manifold edge.

So on `tjunction_many` all three of our tools fail: Blender dents it, PyMeshFix
dents it, PyMeshLab either does nothing or deletes everything. **Only the
commercial service handles it**, keeping all 532 vertices and adding exactly
150 faces. That remains an unowned gap.

**A hazard worth naming**: `meshing_remove_t_vertices(method='Edge Collapse')`
at a low threshold silently reduced a 910-face mesh to zero faces with
`nm=0 open=0` — a "clean" empty mesh. Volume is the only check that catches it.

### Measured — the two orientation filters in isolation, and a correction

**2026-09-16.** `meshing_re_orient_faces_coherently()` +
`meshing_re_orient_faces_by_geometry()`, run alone on every fixture.

| fixture | before | after |
|---|---|---|
| `inverted` | −4094.9 | **+4094.9** |
| `seam` | +2146.2, 40 seam edges / 1 loop | **+4094.9, 0/0** |
| `correct` | +4094.9 | unchanged |
| `tjunction` (3 open) | 100% | unchanged, safe |
| `tjunction_many` (450 open) | 100% | unchanged, safe |
| `doubles` (2 shells) | 200% | unchanged, safe |
| `degenerate` (nm=1) | — | **RAISES** |
| `fin` (nm=1) | — | **RAISES** |

**The precondition is exactly `nm == 0`.** The error says why: *"Mesh has some
not 2-manifold faces, Orientability requires manifoldness"* — orientation is
undefined on a non-manifold surface. Open edges and multiple shells are fine;
only non-manifold edges block it.

So these filters are safe to run unconditionally **after** non-manifold edges
are resolved, and `scanner.scan().non_manifold` is the gate. Verified:
PyMeshFix on `fin` gives 756f/100%, and orient then runs as a clean no-op.

#### Correction — PyMeshFix fixes the seam fixture by itself

Recorded this morning, in the seam comparison table: *"PyMeshFix — not
applicable, no open edges to work on."* **Wrong.** Measured:

```
source     760f 382v vol +2146.2  seams 40/1
pymeshfix  760f 382v vol +4094.9  seams 0/0     <- exactly the control
same face set (ignoring winding): True
faces re-wound: 760 of 760
```

It re-wound the entire mesh to a consistent outward orientation. One call, no
split, no flip, no merge, no reload.

**So the split → flip → merge → reload sequence recorded as "SOLVED" is
unnecessary for this fixture.** It is correct and it reaches the control, but
`meshfix.repair(mesh)` alone does the same thing, and so does the PyMeshLab
pair. Three routes to the same answer, and I built the most elaborate one first
because I had assumed PyMeshFix could not help.

**The design doc's claim is not contradicted** — it says PyMeshFix *deletes a
region* on irreconcilable winding, 562,288 faces in and 394,432 out, on a real
model. That model evidently had something this fixture lacks (more shells, or a
seam that re-winding cannot resolve). **The fixture does not reproduce the
failure the seam mechanism was built for**, which is worth knowing before
trusting it as the test case for that mechanism.

#### Where this leaves the two filters

They are the **only** tool that fixes a uniformly inverted mesh. For seams they
are one of three options. Their value is therefore narrower than it first
appeared, but not zero — and they cost nothing, PyMeshLab being already a
dependency and in-process.

### Measured — the tool order: PyMeshFix first, PyMeshLab after

**2026-09-16.** Proposed order was PyMeshLab (inverted, seams, doubles) then
PyMeshFix. Tested on a fixture carrying **four defects at once** — a reversed
cap, a fin, a degenerate face — because every fixture until now had exactly
one, and the order only matters when defects interact.

Source: 762f, nm=2, open=3, seams 40/1, **51.3%** volume.

| order | result |
|---|---|
| **A — PyMeshLab first** | clean filters take nm 2 → 1, then orient **RAISES**: *"Orientability requires manifoldness"*. Pipeline stops at **51.3%**. |
| **B — PyMeshFix first** | **756f, nm=0, open=0, seams 0/0, 100.0%** in one call. PyMeshLab then runs as a safe no-op. |

**The order is structural, not a preference.** PyMeshLab's orientation filters
have a hard precondition of `nm == 0` — they raise, they do not degrade — and
PyMeshFix is good at establishing exactly that. PyMeshFix's own weaknesses
(`doubles` → half a sphere, `inverted` → no-op) are precisely what the
PyMeshLab filters clean up afterwards. They compose in one direction only.

#### The sequence

```
split by shells                       splitter.by_shells
  -> PyMeshFix                        nm, open edges, seams
  -> PyMeshLab clean                  remove_null_faces
                                      merge_close_vertices(0.1%)
                                      remove_duplicate_faces
                                      remove_unreferenced_vertices
  -> PyMeshLab orient                 re_orient_faces_coherently
                                      re_orient_faces_by_geometry
  -> verify volume
merge
```

**Split first, always.** PyMeshFix turns the `doubles` fixture into half a
sphere and ate 4,525 support shells on `platform_supported.stl`. Given
pre-split single shells it is excellent: costume01's three parts went from
**2,263 non-manifold edges to 0** in 201s with volume preserved to 100.0%.

**`meshing_remove_t_vertices` is excluded deliberately.** It destroyed both
T-junction fixtures at threshold ≤ 1 — 910 faces to **zero**, reporting
`nm=0 open=0`. A clean empty mesh, which only a volume check catches.

#### What the order does not solve

**T-junctions.** All three tools fail: PyMeshFix and Blender both dent the
surface (numerically 99.8% and 99.7% and "clean" — visually gashed), PyMeshLab
destroys the mesh. Only the commercial service handles it, keeping all 532
vertices and adding exactly 150 faces. Unowned.

**Where Blender fits** is still open. It is exact on `fin` and `doubles`, blind
to `inverted` and `seam`, and it **timed out at 420s** on costume01 — the
`TIMEOUT after 420s` in stdout being why the original run wrote `.failed.stl`
with no markers. A run with the cap lifted is in progress; until it returns,
whether Blender can repair a mesh of that shape at all is unknown.

### CORRECTION + the full sequence measured on all eight fixtures

**2026-09-16.** Two corrections to the entries above, both from running the
composed sequence rather than each tool alone.

#### 1. Only ONE of the two orientation filters refuses

Recorded above as *"PyMeshLab's orientation filters refuse when `nm > 0`"*.
Too broad. Tested individually:

| case | nm | open | `re_orient_faces_coherently` | `re_orient_faces_by_geometry` |
|---|---|---|---|---|
| clean tetra | 0 | 0 | ok | ok |
| **nm edge (fin)** | **1** | 2 | **RAISES** | **ok** |
| open (face removed) | 0 | 3 | ok | ok |
| **degenerate face** | **1** | 1 | **RAISES** | **ok** |
| two shells | 0 | 0 | ok | ok |
| vertex-joined | 0 | 3 | ok | ok |

`by_geometry` **never refuses**. The precondition belongs to `coherently`
alone, and the trigger is non-manifold *edges* — open edges, multiple shells
and vertex joins all pass. The original test ran the pair together, so
`coherently` raising masked that `by_geometry` was fine.

Practically: `by_geometry` alone fixes `inverted` and can run on anything.
`coherently` is needed for the seam case and carries the constraint.

#### 2. Clean filters must run BEFORE the split

First composition attempt — split, then repair each part, then clean — left
`doubles` at **200% volume and two shells**. The split separated the coincident
spheres, so `merge_close_vertices` never saw them together and had nothing to
merge. **Splitting first defeats deduplication.** Moving the clean filters ahead
of the split fixed it: 760f, one shell, 100.0%.

#### 3. ORIENT damages a mesh that does not need it

The sole remaining failure was `tjunction_many`: source has **0 seam edges**,
output had **153 in 34 closed loops**. Traced step by step:

```
source            910f  nm=0 open=450 seams=0/0   100.0%
after CLEAN       910f  nm=0 open=450 seams=0/0   100.0%
after pymeshfix  1000f  nm=0 open=  0 seams=0/0    99.8%   <- already correct
after ORIENT     1000f  nm=0 open=  0 seams=153/34 99.8%   <- damaged
```

The orientation filters **manufactured 34 closed seam loops** on a mesh
PyMeshFix had just rebuilt correctly. That is worse than the dents: a closed
seam loop is precisely the signal meaning *PyMeshFix will delete a region here*.

My earlier "safe no-op on clean meshes" conclusion came from simple fixtures.
On a mesh freshly patched over 150 T-junctions, they are not.

**The guard**: run ORIENT only when the mesh says it is misoriented —
`volume < 0` or a closed winding seam. With that, **all eight fixtures pass**.

#### The sequence that works

```python
mesh = pml(mesh, CLEAN)                 # BEFORE the split — dedup needs the whole mesh
for part in splitter.by_shells(mesh):
    part = meshfix.repair(part).mesh
    if scanner.volume(part) < 0 or scanner.winding_seams(part)[1] > 0:
        if scanner.scan(part).non_manifold == 0:
            part = pml(part, ORIENT)    # only when needed, and only when legal
merge(parts)
```

| fixture | faces | nm | open | seams | shells | volume |
|---|---|---|---|---|---|---|
| inverted | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| seam | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| doubles | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| degenerate | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| fin | 756 | 0 | 0 | 0/0 | 1 | **100.0%** |
| tjunction | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |
| tjunction_many | 1000 | 0 | 0 | 0/0 | 1 | 99.8% |
| correct | 760 | 0 | 0 | 0/0 | 1 | **100.0%** |

**Eight of eight numerically clean.** Outputs written as `sphere_*_seq3.stl`.

**Not yet confirmed by eye** — and `tjunction_many` at 1000 faces is the same
shape that was visibly dented before, so that one especially needs looking at.
Numbers have been wrong about this fixture twice.

### Measured — Blender CAN repair costume01; it missed the timeout by 16%

**2026-09-16.** The original `.failed.stl` on this model was a **timeout, not a
failure**. `fix_stl` returned `TIMEOUT after 420s` with no markers and no output
file, which is why the caller had nothing to report.

Re-run with the cap lifted to 3780s: **Blender finished in 488s** — 68 seconds
past the 420s budget, a 16% overrun.

`_blender_budget()` gives 70% of a 600s budget, reserving 30% for post-Blender
work. That reserve is measured and justified (p95 of post-Blender PyMeshFix was
152.6s), so the cap is not arbitrary — this model simply needs more than the
whole budget allows.

#### But finishing is not the same as winning

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| decimated input | 900,000 | 2,263 | 20 | 491 | 100.0% | — |
| **Blender** | 887,019 | **0** | **493** | 450 | 99.5% | 488s |
| **PyMeshFix** (after split) | 835,426 | **0** | **28** | 3 | **100.0%** | 201s |

Both clear all 2,263 non-manifold edges. Blender leaves **493 open edges
against PyMeshFix's 28**, keeps 450 shells where the split reduced to 3, loses
0.5% volume against 0.0%, and takes **2.4x as long**.

Its final marker is `BLENDER_OPEN: open=493 max=0.7735mm` — and 0.7735mm
exceeds `MIN_LAYER = 0.6`, so the print-scale gate would not accept it either.

#### The pass log shows the cost

```
Repair pass  7: nm=237 boundary=392
Repair pass  8: nm=236 boundary=389
Repair pass  9: nm=236 boundary=389          <- stalled
Repair pass 10: wider NM deletion (2583 faces)
Repair pass 10: nm=26 boundary=460
Repair pass 11: nm=14 boundary=425
Repair pass 12: nm=14 boundary=419           <- loop limit, not success
```

Twelve passes, the stall counter firing at 9, the wider-NM escalation deleting
2,583 faces at pass 10, and termination at the loop limit rather than at
convergence. The escalation works — nm goes 236 to 26 — but it is deleting
geometry to get there, which is where the 0.5% volume and 493 open edges come
from.

**So the incumbent is slower, lossier, and ends short of clean on the one real
multi-shell model tested.** Its wins remain the simple single-shell cases —
`fin` and `doubles`, both exact.

### CORRECTION — the sequence is dented on `fin` and `tjunction_many`

**Confirmed by eye 2026-09-16.** Two of the eight `_seq3` outputs are visibly
dented, despite **every numeric check passing**: nm=0, open=0, seams 0/0, and
volume at 100.00% and 99.78%.

**The composition is not at fault.** Traced step by step, `CLEAN` changed
nothing on either fixture (no duplicates, no null faces), the split found one
part, and the ORIENT guard correctly declined to fire. `meshfix.repair()` on
the raw source gives byte-identical output to the whole sequence. The dents
come from PyMeshFix, inherited rather than introduced.

**Blender is exact on `fin` — literally.** Compared face-set against the
control:

| version | faces | verts | volume | identical to control |
|---|---|---|---|---|
| **`_bl`** | **760** | **382** | **100.00%** | **YES — 0 extra, 0 missing** |
| `_pmf` / `_seq3` | 756 | 380 | 99.96% | no — 527 extra, 531 missing |
| `_fixed` (service) | 766 | 385 | 100.01% | no |

Blender did not approximate the repair; it reconstructed the original mesh
exactly. PyMeshFix removed 4 extra faces, and that 0.04% is what shows as a
dent.

On `tjunction_many` **nothing matches the control** — every tool produces a
different surface, and only the commercial service's is visually clean.

#### What this changes

The sequence applies **PyMeshFix to everything**, and that is wrong where
another tool is exact. Routing by defect rather than by fixed order:

| defect | tool | evidence |
|---|---|---|
| doubles, degenerate | PyMeshLab CLEAN, before the split | exact |
| **fin / nm on a small mesh** | **Blender** | **identical to control** |
| nm on a large multi-shell mesh | PyMeshFix after the split | costume01: 2,263 → 0, 100.0% volume, 201s |
| inverted, seams | PyMeshLab ORIENT, guarded | exact |
| T-junctions | **nothing we have** | all three fail; only the service is clean |

**Three numeric all-clears on visibly wrong meshes, in one session.** `fin` at
99.96% and `tjunction_many` at 99.78% both passed nm, open, seam and shell
checks. Volume differences that small are indistinguishable from rounding, so
volume cannot be the gate either. The surface-comparison check recorded earlier
as "nothing we have" is now the blocking gap: without it, a repair ladder
cannot tell which tool produced the better mesh.

### Measured — CLEAN tears holes in `costume01`, and the threshold is not why

**2026-09-16.** The costume run showed open edges going **20 → 1,318** at the
CLEAN step. I attributed that to my `merge_close_vertices` threshold, having
used PyMeshLab's `PercentageValue(0.1)` — roughly 0.11 mm on this model —
against the project's own `MERGE_DIST = 0.01 mm`. **Wrong.**

| threshold | faces | nm | open | shells |
|---|---|---|---|---|
| input | 900,000 | 2,263 | **20** | 491 |
| 0.1% (~0.11 mm, what I used) | 898,616 | 2,018 | 1,318 | 489 |
| **0.01 mm = `MERGE_DIST`** | 895,230 | **2,359** ↑ | 1,506 | 487 |
| 0.001 mm | 898,636 | 2,016 | 1,318 | 491 |
| **no merge step at all** | 898,953 | 1,920 | **1,225** | 491 |

**Open edges go 20 → 1,225 with no merge step at all**, so the damage belongs to
`remove_duplicate_faces` or `remove_null_faces`, not to the merge or its
threshold.

And `MERGE_DIST = 0.01 mm` is the **worst** setting tried: the only one that
*increases* non-manifold edges, 2,263 → 2,359. The project's own constant is
tuned for Blender's `remove_doubles`, and does not transfer to PyMeshLab's
filter.

**The net result is still fine** — PyMeshFix closes those 1,225 holes back to
28 — so CLEAN's damage is real but recoverable. Whether it *earns its place* on
a mesh with no duplicate geometry is a separate question, now being tested by
running the sequence without it.

**A correction to note**: `pymeshlab.AbsoluteValue` does not exist. The
absolute-units class is **`PureValue`**; `PercentageValue` is the other. Both
are required — passing a bare float raises.

#### The costume result, for the record

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| decimated input | 900,000 | 2,263 | 20 | 491 | 100.0% | — |
| **full sequence** | 835,466 | **0** | **28** | 2 | **100.0%** | **198s** |
| PyMeshFix alone | 835,426 | 0 | 28 | 3 | 100.0% | 201s |
| Blender, uncapped | 887,019 | 0 | 493 | 450 | 99.5% | 488s |

The sequence and PyMeshFix-alone differ by **40 faces and one shell** — the
PyMeshLab work contributed almost nothing on this model. `part 1: oriented`
did fire, so the guard found a genuinely misoriented region on real data.

#### `seq4` — `by_geometry` before the repair

Tried at the user's suggestion, since `re_orient_faces_by_geometry` never
refuses and could run first. Result: **byte-identical to `seq3` on all eight
fixtures.** It only acts on a misoriented mesh, and by the time it runs the
orientation is either already correct or PyMeshFix will fix it. A no-op in
either position.

#### Vertex-set comparison detects what the eye detects

Comparing each output's sorted vertex array against the control's: six of eight
match exactly, and **the two that do not are precisely the two seen as
dented** — `fin` (756f/380v) and `tjunction_many` (1000f/502v).

So when a known-good reference exists, vertex-set comparison is the
surface-quality check that volume and topology counts cannot provide. It does
not help on real models, where no control exists — but it makes the fixture
suite able to catch dents automatically.

### Measured — CLEAN earns nothing on `costume01`; it should be conditional

| | faces | nm | open | shells | volume | time |
|---|---|---|---|---|---|---|
| **with CLEAN** | 835,466 | 0 | 28 | 2 | 100.0% | 198s |
| **without CLEAN** | 835,426 | 0 | 28 | 3 | 100.0% | 202s |

A **40-face** difference and 4 seconds, for a step that first tears **1,225
open edges** into the mesh which PyMeshFix then has to close. Damage and repair
for no net gain — because this model has no duplicate geometry to remove.

That does not make CLEAN useless: it is the only thing that fixes `doubles`,
where PyMeshFix returns half a sphere. It makes it **conditional**, like ORIENT
already is. Run it when there is duplicate geometry, not unconditionally.

Detecting that is the open part. `scanner` currently reports the `doubles`
fixture as entirely clean — nm=0, open=0, degenerate=0 — and only the shell
count hints at anything, which is useless because two shells is legitimate.

**A side effect worth noting**: without CLEAN the orient guard fired on **two**
parts; with CLEAN, on one. So CLEAN was masking a misorientation rather than
resolving it.

### The T-junction defect, precisely

Traced on `sphere_tjunction.stl` rather than described from memory, after an
explanation of mine turned out to be wrong in its details.

The T-junction vertex is **345** — not the last-added index, since PyMeshLab
renumbers on load. It has **3 edges and 2 faces**, and:

```
vertex 345 lies ON edge (339, 358) at t=0.500, distance 2.6e-23
```

Exactly the midpoint, and that edge is used by **one** face — the neighbour
that never got split. The three open edges are:

| edge | faces | what it is |
|---|---|---|
| (339, 358) | 1 | the unsplit neighbour's full-length edge |
| (339, 345) | 1 | left half, on the split side |
| (345, 358) | 1 | right half, on the split side |

The halves and the whole occupy the same line and are three distinct edges,
each with a single face. **Three open edges and zero measurable gap** — which
is why merging coincident vertices cannot help: nothing is coincident.

**The repair** is to split the neighbouring face at 345. One face becomes two,
345 gains a fourth edge, its fan closes, and every edge gets two faces. **One
extra face per T-junction** — precisely the +150 faces the commercial service
produced on the 150-T-junction fixture while leaving all 532 vertices intact.

**A correction**: I first described this as vertex M having 3 edges where the
neighbour contributes nothing, and checked the wrong vertex (382, which has 6
edges all properly shared). The shape of the explanation was right; the
specifics were invented. Checking took one query.

### SOLVED (as a method) — the T-junction repair is one face split

**2026-09-16.** The user sketched the fix and asked whether M–D should be a
face. It should be an **edge**, and adding it splits the neighbouring face in
two. Verified on `sphere_tjunction.stl`:

```
neighbour face 199 = [335, 358, 339]        D-C-A, with D = 335
replaced by        [358, 345, 335]          C-M-D
                   [345, 339, 335]          M-A-D

before: 761f  nm=0  open=3
after : 762f  nm=0  open=0  seams=0/0  shells=1  volume 100.00%
```

**+1 face, and the defect is gone.** Every edge now has two faces, M's fan is
closed, and the surface is untouched — no vertex moved, nothing deleted.

#### This is the repair nothing in the toolchain does

| | faces | outcome |
|---|---|---|
| **face split at M** | 761 → **762** (+1) | **clean, 100.00%, no surface change** |
| PyMeshFix | 910 → 1000 | numerically clean, **visibly dented** |
| Blender | 910 → 824 | numerically clean, **visibly dented** |
| commercial service | 910 → 1060 (**+150**) | clean |

The service's **+150 faces on a 150-T-junction fixture** is one face per
T-junction — the same operation, arrived at independently.

#### Why the other tools fail at it

Neither PyMeshFix nor Blender treats this as a topology problem. Both see
open edges and try to *close a hole*: PyMeshFix patches (adding 90 faces),
Blender restructures (removing 86 faces and 118 vertices). Both move geometry,
and that is where the dents come from. The defect has **zero measurable gap** —
there is no hole to close, only an edge that needs splitting.

#### It is implementable, and simply

The detection already exists in a scratch form: for each vertex, test whether
it lies on an edge it does not belong to. On the fixture:

```
vertex 345 lies ON edge (339, 358) at t=0.500, distance 2.6e-23
```

Then split that edge's face at the vertex, preserving winding by walking the
triangle and inserting M between A and C. About 30 lines over `scanner`'s
existing edge map.

**This is the first repair found today that our own code can do and no external
tool does correctly.** Worth building rather than delegating — and it removes
the one defect the survey listed as unowned.

**Caveat before building**: the fixture has one T-junction at an exact
midpoint. Real ones will sit anywhere along the edge, several may share a face,
and a vertex may lie on an edge only approximately. The collinearity tolerance
and the multiple-per-face case both need deciding on real data.

#### Confirmed trivial — a naive implementation handles 150 at once

The user's assessment, tested rather than assumed:

| fixture | before | after | splits | rounds | time |
|---|---|---|---|---|---|
| `tjunction` | 761f, 3 open | **762f, 0 open, 100.00%** | 1 | 2 | 0.01s |
| `tjunction_many` | 910f, 450 open | **1060f, 0 open, 100.00%** | 150 | 2 | **0.40s** |

**1060 faces is exactly what the commercial service produced** — same face
count, same 532 vertices preserved. Two implementations arriving at an
identical result independently.

Against the alternatives on `tjunction_many`:

| | faces | open | volume | surface |
|---|---|---|---|---|
| **face split (ours)** | **1060** | **0** | **100.00%** | untouched |
| commercial service | 1060 | 0 | 100.01% | clean |
| PyMeshFix | 1000 | 0 | 99.78% | **dented** |
| Blender | 824 | 0 | 99.73% | **dented** |

**Three worries, all unfounded.** Several T-vertices on one edge, several on
one face, and convergence all needed no special handling — the naive version
takes at most one split per face per round and re-derives the edge map each
round. Two rounds sufficed on 150 scattered T-junctions, the second only
confirming none remained.

**The one real open question is tolerance.** `1e-6` works because these
fixtures are exact to 2.6e-23 by construction. A T-junction from a boolean or a
decimation sits *near* an edge rather than on it, and the threshold becomes a
judgement: too tight misses them, too loose splits faces that should not be.
That is a parameter to calibrate on real data, not a design problem.

**Confirmed by eye**: `sphere_tjunction_tjfix.stl` and
`sphere_tjunction_many_tjfix.stl` both render clean, **no dents**. That check
mattered — numbers had been wrong about this fixture three times, and both
PyMeshFix's and Blender's outputs passed every numeric gate while being
visibly gashed.

So this is the first repair in the session that **our own code does better
than every external tool**, verified numerically and visually.

### Measured — a sphere with ALL SIX defects, and a fourth failed surface check

**2026-09-16.** Built `sphere_allbad.stl`: a sphere carrying every defect at
once — reversed cap, 40 T-junctions, a fin, a degenerate face, the whole thing
duplicated, and then every face reversed.

Source: **1604f, 846v, nm=4, open=246, degenerate=2, seams 80/2, 2 shells,
volume −104.82%.**

The sequence handles all six, and each step is visible in the trace:

| step | effect |
|---|---|
| T-junction fix | open **246 → 6** |
| CLEAN | 1684f → **841f**, 2 shells → 1, volume −104.82% → −52.41% |
| PyMeshFix + guarded ORIENT | nm → 0, seams **40/1 → 0/0**, volume → **+99.95%** |

**Final: 836f, 420v, nm=0, open=0, degenerate=0, seams 0/0, one shell,
+99.95%.** Written as `sphere_allbad_seq.stl`.

#### The radius test looked like the surface check and is not

Measuring each vertex's distance from the sphere of radius 10 seemed promising:
the result has 40 vertices off it, worst 0.1468 mm. But tested against the
meshes whose condition is already known by eye:

| file | verts | >0.01 mm off | worst | verdict by eye |
|---|---|---|---|---|
| `sphere_correct` | 382 | 0 | 0.0000 | clean (control) |
| `fin_bl` | 382 | 0 | 0.0000 | clean |
| **`fin_pmf`** | 380 | **0** | **0.0000** | **DENTED** |
| `tjunction_many_pmf` | 502 | 135 | 0.1528 | DENTED |
| `tjunction_many_bl` | 414 | **67** | 0.1528 | DENTED |
| **`tjunction_many_tjfix`** | 532 | **150** | 0.1528 | **clean (ours)** |

**It runs backwards.** The clean result has the *most* off-sphere vertices
(150) and a dented one has fewer (67) — because the off-sphere vertices are the
T-junction midpoints, which sit on chords by construction. Preserving more of
them, which is correct, scores worse.

And `fin_pmf` has **zero** deviation while being visibly dented: PyMeshFix
deleted 4 faces without moving a vertex, so the dent is a hole in the face
connectivity, not a displacement. No vertex-position test can see it.

**That is the fourth candidate surface check to fail today**, after nm/open
counts, volume, and seam counts:

| check | sees `fin_pmf` | sees `tjunction_many` dents |
|---|---|---|
| nm / open / degenerate | no | no |
| volume | 99.96% — no | 99.78% — no |
| radius deviation | no | **inverted** |
| **vertex set vs a control** | **yes** | **yes** |

Only comparison against a known-good reference works, and real models have
none. The gap recorded earlier stands, and is now better characterised: the
defect is in **face connectivity**, not vertex positions, so any measure built
on vertex geometry alone will miss it.

**Also noted**: the 40 off-sphere vertices in `allbad_seq` are the fixture's
own artifact — midpoints of chords are always inside a sphere — not a repair
defect. The T-junction fix preserving them is correct behaviour.

### SETTLED — the repair order, measured on the all-defects sphere

**2026-09-16.** The user proposed: T-junction, inverted, doubles by PyMeshLab,
then PyMeshFix. Tested against the order used previously (T-junction, CLEAN,
PyMeshFix with a guarded orient per part). **The user's order is better.**

| step | | faces | nm | open | deg | seams | shells | volume |
|---|---|---|---|---|---|---|---|---|
| | source | 1604 | 4 | 246 | 2 | 80/2 | 2 | −104.82% |
| 1 | **T-junction fix** (ours) | 1684 | 4 | **6** | 2 | 80/2 | 2 | −104.82% |
| 2 | **`re_orient_faces_by_geometry`** | 1684 | 4 | 6 | 2 | **0/0** | 2 | **+200.00%** |
| 3 | **CLEAN** (doubles) | **841** | 1 | 2 | **0** | 0/0 | **1** | **+100.00%** |
| 4 | split → **PyMeshFix** → merge | 836 | **0** | **0** | 0 | 0/0 | 1 | **+99.95%** |

**Step 2 is where the orders diverge.** `by_geometry` on the whole mesh fixes
the inversion **and the seam together** — seams 80/2 → 0/0 and volume
−104.82% → +200.00% (two correctly-oriented spheres) in one filter. The
previous order left that seam until after the split, handling it part by part.

Both orders end at 836f / +99.95%, but the user's reaches **correct orientation
three steps earlier**, so every later step works on a properly-oriented mesh.
That matters on real models: PyMeshFix deciding what to keep on a backwards
surface is how the head-deletion case happens.

#### What this establishes about `by_geometry`

Previously recorded as the fix for uniform inversion, with `coherently` needed
for seams. **`by_geometry` alone fixes both.** And it never refuses — the
`nm == 0` precondition belongs only to `coherently`, which on this evidence is
not needed at all.

That makes it the safest filter in the set: no precondition, fixes two defect
classes, and is a no-op on a correctly-oriented mesh.

#### The order

```
1. T-junction fix                 ours, ~30 lines      246 open -> 6
2. meshing_re_orient_faces_by_geometry                 inversion AND seams
3. CLEAN: remove_null_faces, merge_close_vertices,     doubles
          remove_duplicate_faces, remove_unreferenced
4. split by shells -> PyMeshFix -> merge               remaining nm and open
```

Steps 1–3 run on the whole mesh; only step 4 splits. CLEAN must precede the
split (deduplication needs to see both copies) and follow orientation (so it
merges correctly-wound geometry).

**Still to confirm by eye**: `sphere_allbad_ord2.stl`. Numbers have been wrong
about surface quality four times today, and 99.95% is within the range where
`fin_pmf` (99.96%) and `tjunction_many_pmf` (99.78%) were both visibly dented.

### SOLVED — the surface check is MISSING VERTICES, and the input is the reference

**2026-09-16, the user's correction.** Everything above calls these defects
"dents". They are not. **They are missing vertices**, and saying so precisely
is what makes them detectable.

| mesh | verts | **missing** | extra | verdict by eye |
|---|---|---|---|---|
| `sphere_correct` (control) | 382 | — | — | clean |
| `fin_bl` | 382 | **0** | 0 | clean |
| `tjunction_many_tjfix` | 532 | **0** | 150 | clean |
| **`fin_pmf`** | 380 | **2** | 0 | **wrong** |
| `allbad_ord2` / `allbad_seq` | 420 | **18** | 56 | "not ideal" |

The two vertices PyMeshFix drops from `fin` are at radius **10.000** — genuine
points on the sphere, deleted. Blender's version of the same repair loses none.

**Extra vertices are harmless**; that is a repair adding geometry, and
`tjfix` adds exactly 150 — one per T-junction — while preserving every
original. **Missing vertices mean something was destroyed.**

#### Why this is the check the others could not be

Four candidates failed today — nm/open counts, volume, seam counts, radius
deviation — and all four failed for the same reason: they measure *properties
of the result* rather than *what the result lost*.

`fin_pmf` scores perfectly on every one of them: nm=0, open=0, degenerate=0,
seams 0/0, one shell, 99.96% volume, and **zero** vertex displacement. It is
wrong because two vertices are simply gone, and no property of the remaining
surface reveals that.

**And it needs no control mesh.** The earlier entries record vertex-set
comparison as working but useless on real models, since they have no known-good
reference. That was the wrong framing: **the input is the reference.** A repair
that drops a vertex present in its own input has removed something, and every
repair step has its input in hand.

```python
before = {tuple(p) for p in mesh.geometry.verts.tolist()}
after  = {tuple(p) for p in result.geometry.verts.tolist()}
lost   = before - after          # non-empty means geometry was deleted
```

That is the gate `repairer` needs to choose between tools automatically, and it
closes the blocker recorded through the whole session.

#### Caveats before building it

- **Decimation legitimately removes vertices**, so this gate belongs to repair
  steps, not to the whole pipeline.
- **Merging duplicates legitimately removes them too** — CLEAN takes
  `allbad` from 846v to 423v by design. The gate must exempt deduplication, or
  compare positions rather than counts (a merged duplicate leaves its position
  occupied; a deleted vertex does not).
- Float comparison needs the same rounding `mesh_io.load` already applies, or
  a tolerance.

#### And the user's summary of the result

> *"Not ideal but better by far compared to other."*

`allbad_ord2` still loses 18 vertices and gains 56, inherited from PyMeshFix's
handling of the fin and the remaining non-manifold edges — the one step in the
sequence where Blender is known to be exact. Worth testing whether routing fins
to Blender removes the last 18.

### Measured — `libs/welder.py` on the all-defects sphere, and Blender wins step 4

**2026-09-16.** The T-junction repair is now a module rather than a scratch
script, and reproduces the scratch results exactly. Run on `sphere_allbad.stl`
in the user's order, with per-step vertex-loss tracking:

| step | | faces | verts | nm | open | seams | shells | volume | **lost** |
|---|---|---|---|---|---|---|---|---|---|
| | source | 1604 | 846 | 4 | 246 | 80/2 | 2 | −104.82% | — |
| 1 | **welder** — 80 splits, 2 rounds | 1684 | 846 | 4 | **6** | 80/2 | 2 | −104.82% | **0** |
| 2 | **`by_geometry`** | 1684 | 846 | 4 | 6 | **0/0** | 2 | **+200.00%** | **0** |
| 3 | **CLEAN** (doubles) | **841** | 423 | 1 | 2 | 0/0 | **1** | **+100.00%** | — |
| 4a | PyMeshFix | 836 | 420 | 0 | 0 | 0/0 | 1 | +99.95% | **7** |
| 4b | **Blender** | **840** | **422** | **0** | **0** | 0/0 | 1 | **+100.00%** | **1** |

**Steps 1 and 2 lose nothing**, which the missing-vertex gate confirms directly
rather than by inference.

**Blender is the better tool for step 4**: volume exact at 100.00% against
99.95%, and **1 vertex lost against 7**. That matches `fin_bl` — Blender is
precise on small non-manifold repairs where PyMeshFix deletes. It also matches
the earlier finding that Blender is *worse* on a large multi-shell mesh
(costume01: 493 open edges against PyMeshFix's 28), so neither dominates and
the routing is per-defect, as recorded.

Against the control the result is 12 missing / 52 extra, most of which comes
from CLEAN at step 3 — merging a duplicate necessarily renumbers vertices, so
those are not losses in the same sense. The per-step `lost` column is the
honest measure.

#### The sequence, with each stage's measured-best tool

```
1. welder.repair          T-junctions        lost 0
2. by_geometry            inverted + seams   lost 0
3. CLEAN                  doubles            2 shells -> 1
4. Blender fix_stl        remaining nm       lost 1, volume exact
   (PyMeshFix instead when the mesh is large and multi-shell —
    costume01: 2,263 nm -> 0, 28 open, 100% volume, 201s,
    against Blender's 493 open at 488s)
```

Written as `sphere_allbad_v2.stl` (PyMeshFix step 4) and
`sphere_allbad_v3.stl` (Blender step 4).

### CORRECTION — `sphere_allbad_v3.stl` loses nothing real; the gate needs refining

**2026-09-16.** The entry above records Blender's step 4 as losing 1 vertex.
The user pushed back — *"that vertex Blender drop was something we add by
mistake"* — and checked:

```
vertices Blender dropped: 1
   (18.0, 0.0, 0.0)   radius 18.0000   <- THE FIN APEX
```

It is the **fin's tip**, added deliberately as a defect. Blender did not lose a
vertex of the model; it removed the thing that was supposed to be removed.
Deleting it is the correct repair.

**So `v3` has zero real losses:**

| step | lost | |
|---|---|---|
| 1. welder | 0 | |
| 2. `by_geometry` | 0 | |
| 3. CLEAN | — | merges the duplicate, renumbering by design |
| 4. Blender | 1 | **the fin apex — a defect, correctly deleted** |

Final: **840f, nm=0, open=0, degenerate=0, seams 0/0, one shell, volume exactly
+100.00%**, confirmed spherical by eye. Every other result today that turned
out visibly wrong sat at 99.7–99.96%, so exact volume is meaningful here rather
than another false all-clear.

The 40 vertices at radius 9.853 are the T-junction midpoints, which sit on
chords rather than the sphere by construction in the generator. Welder
preserving them is correct.

#### What this does to the missing-vertex gate

A naive *"did the repair lose any vertex?"* test would flag this as damage. It
is not. The rule has to be **lost a vertex belonging to the sound surface** —
and a fin apex does not: it hangs off a single non-manifold edge, so whatever
removes the fin legitimately removes its tip.

Workable refinements, none yet tested:

- ignore vertices that were **non-manifold or on an open boundary** in the
  input, since those are the ones a repair is entitled to remove;
- or compare **only against vertices with a closed manifold fan** in the input;
- or pair the loss count with the defect count it resolved — losing a vertex
  while clearing a non-manifold edge is a trade, losing one while clearing
  nothing is damage.

The gate is still the right idea — it is the only check that caught `fin_pmf`,
where two vertices at radius 10.000 were deleted from a sound surface — but it
needs to distinguish *removing a defect* from *damaging the model*.

### Measured — the sequence on a REAL model: `Mandy_Body_Dinamuuu3D-simp.stl`

**2026-09-16**, with an independent verdict from the commercial service for
comparison.

**Detection agrees with the service exactly** on the counts both measure:

| | ours | service |
|---|---|---|
| non-manifold edges | **29** | **29** |
| naked / open edges | **34** | **34** |
| degenerate | 0 | 0 |
| winding | 4 seam edges / 1 loop | "40 inverted normals" (faces, not edges) |
| shells | 40 connected components | "0 disjoint shells" (means strays, not components) |

`welder` finds **0 T-junctions** — this model does not have that defect, and
step 1 correctly no-ops.

#### Result

| | faces | verts | volume | nm | open |
|---|---|---|---|---|---|
| source | 188,940 | 94,530 | +14,682.8 | 29 | 34 |
| **ours** | **188,432 (99.7%)** | **94,286 (99.7%)** | **99.99%** | **0** | **0** |
| commercial service | 179,398 (94.9%) | 89,683 (94.9%) | — | 0 | 0 |

**We keep ~9,000 more faces and ~4,600 more vertices** than the service while
reaching the same clean state. It removed 5% of the model to fix 63 defects;
we removed 0.3%. Sixteen seconds against its 21.8.

#### `by_geometry` is churn on a correctly-oriented model

Step 2 took seams from **4/1 to 23/3** — it manufactured seam edges on a mesh
that had almost none. The same failure seen on `tjunction_many`.

Running the identical sequence **without** it gives a **byte-identical final
result**: same faces, vertices, nm, seams, volume, and the same 525 lost
vertices. So the step was pure churn here — PyMeshFix undid the damage along
with everything else.

**The existing guard does not catch this.** It asks *"does the mesh have a
closed seam loop?"*, and this one does (4 edges / 1 loop), so it fires — but
the seam is small enough that PyMeshFix resolves it, and the orientation
filter's cure is worse than the disease. A better trigger would be
`volume < 0` alone, or a seam large enough to matter relative to the mesh.

On the all-defects sphere `by_geometry` was essential — the mesh was genuinely
inverted. On a correctly-oriented model with a small seam it is at best
neutral. **That is the difference the guard needs to express.**

#### Still to confirm by eye

`mandy_simp_seq.stl`. The **525 lost vertices** are what to judge: 0.3% of the
model, repairing 29 non-manifold edges. On `fin` a loss of 2 showed as visible
damage, but that was 2 from a sound surface, where these may be the
non-manifold geometry itself. The refined gate recorded above — *lost a vertex
belonging to the sound surface* — is exactly what would answer this
automatically, and it is not built.

---

## THE REPAIR SEQUENCE — consolidated (2026-09-16, end of session)

`mandy_simp_seq.stl` confirmed clean by eye. The sequence below is validated on
eight synthetic fixtures, a sphere carrying all six defects at once, and two
real models, with a commercial repair service as an independent check
throughout.

### The sequence

```python
# 1. T-junctions — ours, libs/welder.py
mesh = welder.repair(mesh).mesh

# 2. Orientation — ONLY when the mesh is genuinely inverted
if scanner.volume(mesh) < 0:
    mesh = pml(mesh, [('meshing_re_orient_faces_by_geometry', {})])

# 3. Duplicates — whole mesh, BEFORE the split
mesh = pml(mesh, [('meshing_remove_null_faces', {}),
                  ('meshing_merge_close_vertices', {'threshold': PercentageValue(0.1)}),
                  ('meshing_remove_duplicate_faces', {}),
                  ('meshing_remove_unreferenced_vertices', {})])

# 4. Remaining non-manifold and open edges, per shell
parts = [repair_tool(p).mesh for p in splitter.by_shells(mesh)]
mesh  = splitter.merge(parts, destination=...)
```

**Step 4's tool depends on the mesh**, and this is the one routing decision the
measurements force:

| mesh | tool | evidence |
|---|---|---|
| small, single-shell | **Blender** | `fin_bl` face-identical to control; allbad step 4 lost 1 (the fin apex) at exactly 100.00% volume where PyMeshFix lost 7 at 99.95% |
| large, multi-shell | **PyMeshFix** | costume01: 2,263 nm → 0, 28 open, 100% volume, 201s — against Blender's 493 open edges at 488s, and a 420s timeout in the real pipeline |

### Why each step is where it is

- **welder first**: it only adds faces, never moves or deletes, so nothing
  downstream is disturbed. On real models it usually finds nothing and no-ops.
- **orientation before everything else**: a backwards surface makes PyMeshFix
  delete regions — that is the head-deletion case. Fixing it first means every
  later step works on correct geometry.
- **duplicates before the split**: deduplication must see both copies.
  Splitting first separates them and `merge_close_vertices` has nothing to do —
  measured, `doubles` stayed at 200% volume and 2 shells.
- **the split before step 4**: PyMeshFix rebuilds one manifold surface and
  discards the rest, so a multi-shell mesh reaching it unsplit loses
  components — 4,525 support pillars on one resin model.

### Results

| subject | source | result | preserved |
|---|---|---|---|
| all-defects sphere | 1604f, nm=4, open=246, seams 80/2, 2 shells, **−104.82%** | 840f, all zero, **+100.00%** | everything; the one vertex dropped was the fin apex, a defect |
| `Mandy...-simp` (real) | 188,940f, nm=29, open=34, seams 4/1 | **188,432f, all zero, 99.99%** | **99.7% of faces and vertices** |
| costume01 (real) | 900,000f, nm=2,263, open=20, 491 shells | 835,426f, nm=0, 28 open, **100.0%** | 201s |

On `Mandy...-simp` the commercial service kept **94.9%** of faces and vertices
where we kept **99.7%** — it removed 5% of the model to fix 63 defects.

### What each tool is for

| defect | tool | note |
|---|---|---|
| **T-junctions** | **welder (ours)** | the only correct implementation available; PyMeshFix and Blender both dent, PyMeshLab destroys the mesh |
| inverted normals | PyMeshLab `by_geometry` | the only tool that fixes it; nothing else even detects it |
| seams | PyMeshFix, or `by_geometry` | PyMeshFix re-winds the whole mesh correctly in one call |
| duplicates | PyMeshLab CLEAN | three filters together; the merge alone makes it worse |
| degenerate faces | any | all three exact |
| fins | **Blender** | face-set identical to the control |
| non-manifold, multi-shell | **PyMeshFix** | after the split |

### Known gaps

**The orientation guard is wrong.** It currently asks *"is there a closed seam
loop?"*, which fires on `Mandy...-simp` (4 edges / 1 loop) where the step is
pure churn — it inflated seams to 23/3 and PyMeshFix undid it, with a
byte-identical final result either way. **`volume < 0` alone** is the correct
trigger: that distinguishes a genuinely inverted mesh, where the step is
essential, from a correctly-oriented one with a small seam.

**CLEAN is unconditional and should not be.** On costume01 it tears 1,225 open
edges that PyMeshFix then closes, for a 40-face difference against not running
it. It is only needed when duplicate geometry exists, and `scanner` cannot
currently detect that — the `doubles` fixture reads entirely clean.

**The missing-vertex gate is designed but not built.** It is the only check
that caught `fin_pmf` deleting two vertices from a sound surface while scoring
perfectly on nm, open, degenerate, seams, shells, volume and vertex
displacement. The refinement it needs: *lost a vertex belonging to the sound
surface* — a fin apex does not count, since removing a fin legitimately removes
its tip.

**`repairer` does not exist.** The sequence above runs in scratch scripts.
`welder` is the only part committed as a module.

### CLARIFICATION — `volume < 0` detects only a WHOLLY inverted model

**Asked 2026-09-16**, and the answer qualifies the guard recorded above.

`scanner.volume()` returns one signed total for the whole mesh, so it goes
negative only when the *net* orientation is inverted. Measured:

| mesh | volume | guard fires | actually has a reversed region |
|---|---|---|---|
| `sphere_inverted` | −4094.9 | **yes** | whole mesh — correct |
| `sphere_allbad` | −4292.4 | **yes** | whole mesh — correct |
| **`sphere_seam`** | **+2146.2** | **no** | **yes, the cap — MISSED** |
| `Mandy-simp` (real) | +14,682.8 | no | small seam — correctly skipped, the step was churn |
| `sphere_correct` | +4094.9 | no | none — correct |

**A reversed region leaves the total positive**, so the guard skips it.

#### Why that is tolerable, and where it is not

**Tolerable because PyMeshFix fixes seams itself at step 4** — measured today,
it re-wound all 760 faces of the `seam` fixture to exactly +4094.9 in one call.
The defect is handled downstream whether or not step 2 fires.

**Two cases still slip through:**

1. **A reversed region PyMeshFix cannot re-wind.** The design doc's
   head-deletion case is exactly this: PyMeshFix *deleted* the region rather
   than flipping it, 562,288 faces in and 394,432 out. Volume stays positive,
   the guard skips, and step 4 destroys geometry.
2. **A reversed region larger than half the model.** The total then goes
   negative and `by_geometry` fires on a mesh that is mostly *correct*,
   flipping the majority the wrong way.

#### What the trigger should probably be

Not one number for the whole mesh. Candidates, none tested:

- **per-shell volume after the split** — each component judged on its own sign,
  which is the natural granularity now that the split exists;
- **per-region volume after a seam split** — `splitter.by_seams` already
  isolates the reversed region, and today's seam work showed its volume is
  **negative** (−974.3 against the host's +3120.5) even when the whole mesh is
  positive;
- or simply run `by_geometry` **after** the split, per part, where a reversed
  part does show a negative total.

The last is the smallest change and would have caught `sphere_seam` while still
skipping `Mandy-simp`.
