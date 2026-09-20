## Target — the module design

Worked out in discussion before the numbered decisions began; they refine it.
A direction, not a specification — deviate where the code argues back.

**`Stl` — a plain-data DTO.** Facts and paths: format, triangle count, defect
counts, bounds, volume. **No geometry.** A 900k-face mesh is ~45 MB of
triangles, and memory is already the binding constraint (`_BYTES_PER_TRIANGLE`,
`auto_worker_count`, the OOM killer taking workers); an immutable value carrying
arrays would double peak memory at every handoff. Vertex arrays are loaded and
discarded inside each operation, as they are today.

> **Amended by D15.** The type became `mesh_io.Mesh` and it *can* carry
> geometry, in an optional field filled only by an explicit `load()`. The
> constraint above still holds and is what D15 is built to satisfy — it is met
> by "every operation returns a new mesh" rather than by "the type cannot hold
> arrays". The handoff this paragraph feared does not happen: what sits in the
> queue is always the probed mesh, and the loaded one is a different value that
> a worker creates and drops.

Values are overwritten as newer data arrives. Where a step genuinely needs the
prior value, the DTO simply holds both — `volume` and `volume_before`, `bounds`
and `bounds_before`. Two decisions need that: volume loss after repair (the
Mandy seam recovery) and bbox drift. No append-only history mechanism; the cases
are few and known. See D9.

Keep it serialisable. The `--one-file` child reports its result to the thread
that spawned it as a plain dict over a JSON pipe, so either the DTO is plain
data by construction or it gains an explicit `to_dict()` at that boundary.

**That is the only boundary.** Under D3 the worker is a thread in the parent
process, not a separate process, so nothing has to be serialised between a
worker and the parent — only between the child and the thread that owns it.

**Tool modules** — `blender_handler`, `pymeshfix_handler`, `pymeshlab_handler`.
One tool each, no policy. This is where the invisible-Blender bug came from:
four call routes, timing recorded at one of them.

**Operation modules** — `decimator`, `repairer`, `splitter`, `scanner`. Each
owns its fallback ladder *and* its `isRequired…` predicate, so `Stl` never
learns `MAX_FACES` or which tool does what. `decimator` owns
fast_simplification → pymeshlab → blender; `repairer` owns pymeshfix → blender
plus seam recovery.

- **A predicate must be cheap and side-effect-free.** If it is not, it is a
  process and gets named as one: `scanner.scan(stl)` returns an `Stl` carrying
  defect counts, after which `repairer.isRequiredRepair(stl)` is free because it
  reads facts already held. Two current functions are processes wearing
  predicate clothing — `_open_loops_are_printable` re-scans, `_will_decimate`
  recomputes a condition decided elsewhere.
- **Inject capabilities, not control flow.** Where a module needs a fact it
  cannot cheaply obtain, pass the processor in (`isRequiredRepair(stl,
  scan=scanner.scan)`) rather than duplicating the logic or re-scanning. Give
  the parameters defaults so the common path stays prose. Injected callables
  answer questions or perform named operations; they never make decisions the
  module owns. Applied to admission in D5 and to budgets in D10.

**Pipeline** — reads as prose, orders the steps, and documents why the order is
load-bearing. The steps look independent and are not: the split runs after
decimation (D13), Blender runs before the seam split, the print-scale gate
precedes the Blender fallback. State those constraints in the module docstrings
or someone will tidy the sequence and silently regress it.

**The runner.** An earlier version of this section said the runner was *exempt*
— 1,455 lines of pool management that should stay explicit imperative code.
**That was wrong; see D1.** It is not one hard thing, it is the same concern
implemented twice in two files and already drifted apart, and D2–D11 specify
what replaces it.

---

