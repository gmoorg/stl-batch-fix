# Open items

Things worth investigating, with the evidence that prompted them. Nothing here
is a known bug — those get fixed. These are questions where the right answer is
not yet clear.

---

## The per-file timeout penalises multi-part files

`TIMEOUT` (1200 s) covers the whole split → repair → merge cycle, not each
repair. A file that splits into six shells therefore does six PyMeshFix passes
under one budget, and is killed for being *multi-part* rather than for being
slow. Both timeouts in the first full-collection run were this:

```text
Assembly.stl          1,620,826 tris, 6 shells, part.0 alone 200,980 faces
                      with nm=65.  Completed 1 part in 1200 s.

whole-costume01.stl   2,004,941 tris, 3 shells, part.0 alone 892,445 faces
                      with nm=2,055.  Completed 3 parts.
```

Neither was stuck — the log shows steady progress through the parts, just not
fast enough. Both have `.timeout.stl` markers, so they are skipped until the
marker is deleted.

**Options, none obviously right:**

- **Raise `TIMEOUT`.** Costs nothing on files that finish quickly, since the
  limit is only an upper bound. These two would probably need 2400–3600 s.
  Blunt, but the timeout exists to stop a *hang*, not to cap honest work.
- **Time each part separately.** Fairer — a six-shell file gets six budgets —
  but a genuinely stuck file then runs six times longer before anything
  notices, which is the failure the timeout was added to prevent.
- **Budget by size.** Scale the limit with triangle count, so a 2M-triangle
  mesh is allowed proportionally longer. Needs a measured cost-per-triangle
  figure, and the relationship is not linear: PyMeshFix's runtime tracks defect
  count more than face count.

**What would settle it:** run the two files with `--timeout 3600` and see
whether they complete, and how long they actually take. If it is 1500 s, raise
the limit. If it is 5000 s, the per-part option matters. Neither has been
measured.

---

## Millenium_Falcon fails where it used to succeed

Both copies (`Hanna and Chewie/` and `Hanna and Chewie/Alternative Version/`)
came back `failed` in the first full run of the current pipeline. This file
previously completed via the Blender fallback — PyMeshFix returns an empty mesh
on it, which is expected for a source with `nm=147,448` out of 393,198 faces,
and Blender recovered it.

Not yet investigated. The step log for that file is the place to start.
