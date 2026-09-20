### Proposed pipeline — `_process_file_impl` restructured (superseded)

**Obsolete as a plan, kept for four facts about the frozen code.** It was a
263-line restructuring of `_process_file_impl`, drafted 2026-09-14 before the
module rewrite. `splitter` and D23 removed the thing it was organising: the
`is_part=True` re-entry is gone, and with it the flag that threaded through
nineteen call sites. The full draft is in git history.

What survives, because it describes the **frozen script's actual behaviour**
and the prose elsewhere describes its *source order* instead:

- **Skip checks run only for whole files**, never for parts — `is_part` short
  circuits the entire phase, so a part is never compared against `dst` or the
  marker files.
- **Decimation happens before the scan**, not after, which is why defect counts
  in the logs are post-decimation. (D13 later fixed this ordering as
  deliberate; see also the measurement that decimation *creates* most of the
  defects.)
- **Split has one live call site, not two.** Step B tests `_split_deferred` and
  skips itself; step B2 does the work. B and B2 are the same step written
  twice, which is why the prose and the execution disagree.
- **A partial merge is not `open`.** The branch returns status `open` when some
  parts failed, which conflates "repaired with open edges remaining" with
  "some parts are missing entirely" — two different things for a reader
  deciding whether to print the file.

Also noted there and still true: **both volume constants are hardcoded**
(`_VOLUME_LOSS_LIMIT`, `_VOLUME_MIN_MEANINGFUL`), and the PyMeshFix
availability branch is dead code now that it is a hard requirement.
