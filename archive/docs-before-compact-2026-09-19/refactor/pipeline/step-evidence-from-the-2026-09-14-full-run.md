### Step evidence from the 2026-09-14 full run

902 files, 897 ok, 2 open, 2 failed, zero exceptions. 916 step rows.

```text
step                ran   ok  fail  skip   rate   med     p95     max
pymeshfix           570  554    16    23    97%  23.4s  148.9s  950.1s
decimate            237  237     0     0   100%     -       -       -
split                34   32     2     0    94%     -       -       -
blender              21   14     7     0    67%  12.8s  105.1s  105.1s
seamsplit            20    2    18     0    10%     -       -       -
printscale            9    6     3     0    67%     -       -       -
pymeshfix2            2    2     0     0   100%   9.2s    9.5s    9.5s
```

**`decimate` is 237-for-237**, including Leia's ~1 M-triangles-per-mm parts.
D12's premise holds on fresh data; the fallback rungs have still never run.

**`seamsplit` is the weakest step in the pipeline, 2-for-20.** Both successes
are `Mandy_Body_Dinamuuu3D.stl` part 0 — the model the route was built for —
appearing twice because it is duplicated in the collection. So: one real save
for 20 Blender invocations. Every attempt found seam edges but no closed loop
to cut on, which is why the Blender-first fallback runs at all; it produced a
usable loop twice, left 15 still unseparable, and returned no mesh 3 times.

The trigger fires on the wrong population. Of 20 volume-loss triggers, only
~6 were real destruction (27–34% volume retained); twelve retained 85–94%,
which is what filling holes and dropping fragments legitimately costs. But the
95% threshold cannot simply be tightened — **Mandy itself sits at 85%**, near
the bottom of the mild cluster. Several triggers were on meshes of a few mm³
(`66 -> 62`, `183 -> 165`), which `_VOLUME_MIN_MEANINGFUL` is meant to filter
and evidently does not.

**`blender` mixes two jobs.** Of 21 runs: 19 repair-fallback (12 ok, 7 fail =
63%) and 2 ASCII conversion (2 ok). Conversion cannot meaningfully "fail to
repair", so one success rate over both muddies each. D7's preparation stage
already proposes moving conversion out of the repair path, which would separate
them.

