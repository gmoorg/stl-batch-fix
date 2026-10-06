# Out-of-memory kill with nothing logged

## Symptom

The per-model `.log` holds only the run header and `---- start decimate [-]`.
`batch.log` holds only `start decimate ... 39620499 faces in`. The job record
says `crashed: parent wrote fallback marker ...` with no cause. The user sees
the failure as having no explanation.

## File

| File | Size | Faces | Crash after |
|---|---|---|---|
| CA3D/Azula/1-9scale uncut Azula CA3D/Azula.stl | 1.98 GB | 39,620,499 | 545 s |

## Cause (verified)

The kernel OOM killer:

```
Oct 04 15:56:03 kernel: Out of memory: Killed process 3133388 (python)
  total-vm:20353920kB, anon-rss:18661520kB ... oom_score_adj:200
```

The job started at 15:47:01 (log header), and 545 s later is 15:56:06, which
matches the kill. The child reached 18,661,520 kB anon RSS (19.1 GB,
17.8 GiB) during whole-model PyMeshLab decimation of 39.6M faces, on a
31 GiB machine. It was the only batch job running: `batch.log` has no other
entries between Azula's `start decimate` (15:47:29) and the kill.

## Why nothing is logged

SIGKILL can't be caught, so faulthandler never runs and the child writes
nothing. The parent saw an abnormal exit and recorded only "crashed",
without the signal (SIGKILL / -9). Since 2026-10-05 it reports the signal
(item 1 below); it still doesn't point at the OOM killer.

## Two separate problems

1. **Diagnostics (done 2026-10-05):** the parent now reports the child's
   signal or exit status in the job reason and the model log, e.g.
   `crashed (killed by SIGKILL)`; see
   [orchestration.md](../refactor/orchestration.md) step 5. The owner chose
   the signal only: an OOM kill is still read from the kernel log by hand
   (`journalctl -k`, "Out of memory: Killed process <pid>"). SIGKILL
   alone doesn't mean out of memory: the parent's own timeout and
   cancellation also kill children. Say "out of memory" only with kernel or
   cgroup evidence. Keep the existing rule that a validated child result is
   trusted even after an abnormal exit (`batch_repair.py`).
2. **Memory:** the initial decimation of very large inputs isn't
   bounded by `reconstruct_memory_budget_gb` (that budget sizes winding only).
   The scheduler does reserve for it: `jobmemory.prepare_bytes` =
   0.4 GB + 700 B/face, about 28 GB for Azula. That exceeds the budget
   (0.7 × `MemAvailable` read at run start, so at most 0.7 × 31 GiB ≈
   23.3 GB; the run didn't log the value), so the job is admitted *alone* by
   design. Running
   alone can't help when the job needs more than free RAM. BambuStudio was
   also open and was the process that triggered the global OOM.

Follow-up measurement: loaded straight from file, Azula loads in 97 s
(18.1 GB peak, 12.7 GB settled) and is then killed during decimation past
23.3 GB, still climbing. See [decimation-memory-path.md](decimation-memory-path.md).
