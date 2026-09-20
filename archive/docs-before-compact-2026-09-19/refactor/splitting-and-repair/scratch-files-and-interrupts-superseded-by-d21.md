### Scratch files and interrupts (superseded by D21)

**Recorded 2026-09-15, resolved the same day.** The entry asked where Blender's
temporary files should live so an interrupted run does not leak them, and
proposed a known scratch root swept at startup.

**D21 replaced the premise**: intermediates live in the destination folder and
are deleted when the next step succeeds *or is not needed*. There is no scratch
root to sweep, no ownership question between concurrent instances, and a retry
finds a kept input by construction because its path derives from the output
path.

Two things from the original worth keeping:

**The Ctrl+C gap is real.** D14 has the pool deliberately not catching
`KeyboardInterrupt`. A `finally` normally runs on interrupt, but if it arrives
inside `Runner.run` or during the `finally` itself, the file survives — and
that is the likeliest moment, since a run is interrupted *because* something is
wrong. Same exposure for an OOM kill or power loss.

**The input file is not garbage.** The original framed every temp as a leak.
The file handed *to* Blender is a valid mesh that cost a weld and a write, it
is what a retry continues from, and it is the reproducer for whatever killed
Blender. Only Blender's *output* is worthless on failure — truncated, and
dangerous because something later could mistake it for a result. A blind sweep
would have deleted exactly what should be kept.
