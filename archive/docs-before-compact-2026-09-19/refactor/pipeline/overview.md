## Mesh pipeline

The work is extracting the steps of `_process_file_impl` (826 lines, nesting
depth 8, 91 if-statements, 23 return points) into the operation modules the
Target section describes. `TODO.md` item 1 holds the approach — how small to
make each step, and what the 36 tests do not catch.

