#!/usr/bin/env bash
# run.sh — launch the STL Batch Fix TUI
#
# Environment overrides (set before calling this script):
#   PYTHON=python3               – Python interpreter (default: python3)
#   BLENDER_BIN=/path/to/blender – Blender executable (default: blender)
#
# Settings (input folder, workers, etc.) are read from a .fixcfg file
# next to this script.  If none exists, one is created from defaults.
#
# To run the underlying script directly without the TUI:
#   python stl_batch_fix.py --input /path/to/stls --workers 4

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the venv Python that has pymeshlab/pymeshfix installed.
# Fall back to PYTHON env var, then python3 on PATH.
VENV_PYTHON="$SCRIPT_DIR/../.venv/bin/python"
if [ -z "${PYTHON:-}" ] && [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="${PYTHON:-python3}"
fi

# ---------------------------------------------------------------------------
# Cgroup resource limits — keep workers from freezing the desktop.
#
# systemd-run wraps the whole process tree (Python workers + Blender) in a
# transient user scope with hard limits:
#   CPU_QUOTA  — max CPU across all cores, e.g. 200% = 2 full cores
#   MEM_MAX    — OOM-kill the group before it touches the desktop
#   IO_WEIGHT  — lower I/O scheduling priority (default 100, lower = less)
#
# CPU_QUOTA defaults to one full core per configured worker, so the two settings
# can't silently disagree — 3 workers sharing a 200% quota would each run at
# two-thirds speed for no stated reason.  WORKERS is read from the active
# .fixcfg; override any of these from the environment:
#   CPU_QUOTA=300% MEM_MAX=12G bash run.sh
#
if [ -z "${CPU_QUOTA:-}" ]; then
    # Read WORKERS from the config the TUI will load (first *.fixcfg found).
    _cfg=$(ls -1 "$SCRIPT_DIR"/*.fixcfg 2>/dev/null | head -n1 || true)
    _workers=""
    if [ -n "$_cfg" ]; then
        _workers=$(sed -n 's/^[[:space:]]*WORKERS[[:space:]]*=[[:space:]]*\([0-9]\+\).*/\1/p' \
                   "$_cfg" | head -n1)
    fi
    [ -n "$_workers" ] || _workers=3
    # Never let the quota exceed the machine's actual core count.
    _cores=$(nproc 2>/dev/null || echo 4)
    [ "$_workers" -le "$_cores" ] || _workers="$_cores"
    CPU_QUOTA="$((_workers * 100))%"
fi
MEM_MAX="${MEM_MAX:-8G}"
IO_WEIGHT="${IO_WEIGHT:-50}"

# Per-worker RLIMIT_AS is derived from MEM_MAX by the TUI.  Export it in bytes so
# a worker that balloons hits MemoryError and fails one file, rather than the
# cgroup OOM-killer picking a victim — possibly the parent, orphaning the rest.
_mem_bytes=$(printf '%s' "$MEM_MAX" | awk '
    /[0-9]$/            { printf "%d", $0; exit }
    /[Kk]$/             { printf "%d", substr($0,1,length($0)-1) * 1024; exit }
    /[Mm]$/             { printf "%d", substr($0,1,length($0)-1) * 1024 * 1024; exit }
    /[Gg]$/             { printf "%d", substr($0,1,length($0)-1) * 1024 * 1024 * 1024; exit }
    { print "" }')
[ -n "$_mem_bytes" ] && export WORKER_MEM_MAX="$_mem_bytes"

# Probe for a usable systemd user manager without actually starting a scope —
# `systemd-run … true` would create and tear down a real transient unit just to
# answer the question, and fails spuriously once the transient-unit limit is hit.
if command -v systemd-run &>/dev/null \
   && systemctl --user show-environment &>/dev/null; then
    echo "run.sh: CPUQuota=${CPU_QUOTA}  MemoryMax=${MEM_MAX}  IOWeight=${IO_WEIGHT}" >&2
    exec systemd-run --user --scope --quiet \
        --property="CPUQuota=${CPU_QUOTA}" \
        --property="MemoryMax=${MEM_MAX}" \
        --property="MemorySwapMax=0" \
        --property="IOWeight=${IO_WEIGHT}" \
        -- "$PYTHON" "$SCRIPT_DIR/stl_batch_fix_tui.py" "$@"
else
    # No systemd user manager (e.g. plain SSH session, container).  The per-worker
    # RLIMIT_AS above is then the only memory guard — there is no cgroup backstop,
    # so the TUI's own SIGKILL sweep is what stops workers outliving the run.
    echo "run.sh: no systemd user manager — running without cgroup limits" >&2
    exec "$PYTHON" "$SCRIPT_DIR/stl_batch_fix_tui.py" "$@"
fi
