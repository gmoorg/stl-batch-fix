### costume01 is a broken source, and should stop being the benchmark

**2026-09-17, the user's assessment, and the numbers support it.** As the
pipeline receives it:

| | |
|---|---|
| non-manifold edges | **2,263** |
| shells | **491**, of which **488 are under 100 faces** |
| those 488 shells' total volume | **0.0040 mm³** against the model's 7,758.7 |
| largest shell | **99.16%** of all faces |
| degenerate faces | 1 |

So it is one real model plus 488 collapsed bags — closed surfaces enclosing
essentially nothing, 0.16 mm to 4.77 mm across. The provenance is known and
recorded: `fast_simplification` took this file from **3 non-manifold edges to
2,263** and 448 shells to 491. The debris is decimation damage, not modelling.

**Consequences for how it is used here.** It has been the calibration target
for most of this session — the tolerance sweep, the T-junction counts, the
"5 candidates" figure. That was a poor choice: on a mesh this damaged almost
any threshold finds *something*, and one candidate traced by index turned out
to be a different defect entirely. Its value is as a **stress test** —
does the pipeline survive 2,263 non-manifold edges without destroying the
model — and it does, finishing nm=0 at 99.99% volume.

It is not evidence about what a normal model contains. Mandy is the better
reference for that, and a cleaner source for costume01 would be better still.

**One live consequence**: `MIN_SHELL_FACES = 100` keeps two zero-volume shells
of 250 and 136 faces because they are large enough, while dropping the other
486. A volume floor would reject all 488 outright, and `splitter`'s docstring
reasoned against a face-count *ratio* without considering volume at all. Not
changed — a real small part must be checked to have meaningful volume first.

---

