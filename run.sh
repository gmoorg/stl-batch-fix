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
    _cores=$(nproc 2>/dev/null || echo 4)
    # WORKERS=0 means the TUI picks the count itself from RAM and cores, so the
    # quota cannot be derived from the config here — grant all cores and let the
    # TUI's own ceiling do the limiting.  A literal 0 would otherwise become
    # CPUQuota=0%, which stalls the run completely.
    if [ -z "$_workers" ] || [ "$_workers" -le 0 ]; then
        _workers="$_cores"
    fi
    # Never let the quota exceed the machine's actual core count.
    [ "$_workers" -le "$_cores" ] || _workers="$_cores"
    CPU_QUOTA="$((_workers * 100))%"
fi
# Memory ceiling for the whole process tree.  Derived from installed RAM rather
# than hardcoded: the previous fixed 8G was set before we knew where the memory
# was going, and on a 31 GiB machine it OOM-killed a worker that had reached
# 5.8 GB while 18 GiB sat free.  A single worker can legitimately need ~6 GB on
# a 7M-triangle mesh (decimation alone peaks at 2.5 GB, and NM repair runs on
# top of that), so the budget has to accommodate WORKERS x that.
#
# Reserve is what stays for the desktop: whichever is larger of 8 GiB or a
# quarter of RAM, so the same rule works on a 16 GiB laptop and a 64 GiB box.
if [ -z "${MEM_MAX:-}" ]; then
    _ram_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 8388608)
    _ram_g=$(( _ram_kb / 1024 / 1024 ))
    _reserve_g=$(( _ram_g / 4 ))
    [ "$_reserve_g" -ge 8 ] || _reserve_g=8
    _budget_g=$(( _ram_g - _reserve_g ))

    # Also cap by what is actually free right now.  Installed RAM is not
    # available RAM: a browser and an IDE can easily hold 12 GB, and a budget
    # derived only from MemTotal would let the run claim memory the machine
    # does not have, pushing the desktop into reclaim.  MemAvailable is the
    # right figure — MemFree excludes page cache, which is reclaimable and
    # would make the run look far more constrained than it is.
    #
    # This is a start-up decision only.  Free memory is deliberately NOT
    # consulted when deciding to start each file: workers that have already
    # begun have not yet reached their peak, so a spawn gated on current free
    # memory reads the past to predict the future and overcommits — which is
    # how the earlier OOM happened.  Per-file admission uses the predicted cost
    # from the mesh's triangle count instead.
    _avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "${_avail_kb:-0}" -gt 0 ]; then
        _avail_g=$(( _avail_kb / 1024 / 1024 ))
        _avail_budget_g=$(( _avail_g * 85 / 100 ))   # leave headroom for the desktop
        [ "$_budget_g" -le "$_avail_budget_g" ] || _budget_g="$_avail_budget_g"
    fi

    [ "$_budget_g" -ge 4 ] || _budget_g=4      # floor for small machines
    MEM_MAX="${_budget_g}G"
fi
IO_WEIGHT="${IO_WEIGHT:-50}"

# Exported for reference only — the TUI no longer turns this into a per-worker
# RLIMIT_AS.  That cap limited *virtual* address space rather than resident
# memory, and the C++ mesh libraries reserve far more VA than they reside: three
# workers under a 2 GiB cap each failed every decimation with std::bad_alloc
# while using ~1 GB RSS apiece.  MemoryMax below is the real limit, and it
# already covers the whole tree (workers and Blender alike).
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
# MemoryHigh sits below MemoryMax and is the throttle rather than the axe: once
# the group crosses it the kernel applies heavy reclaim pressure and stalls the
# allocating task, which slows the run instead of killing a worker.  This is the
# graceful degradation a Windows pagefile provides, achieved without swap — the
# machine has none by choice, and an OOM kill was previously the only outcome
# available once the limit was reached.  MemoryMax remains the hard backstop.
MEM_HIGH="${MEM_HIGH:-$(awk -v m="$MEM_MAX" 'BEGIN{
    g = m; sub(/[Gg]$/, "", g);
    if (g+0 > 0) { h = int(g * 0.85); if (h < 1) h = 1; printf "%dG", h }
}')}"

if command -v systemd-run &>/dev/null \
   && systemctl --user show-environment &>/dev/null; then
    echo "run.sh: CPUQuota=${CPU_QUOTA}  MemoryHigh=${MEM_HIGH}  MemoryMax=${MEM_MAX}  IOWeight=${IO_WEIGHT}" >&2
    exec systemd-run --user --scope --quiet \
        --property="CPUQuota=${CPU_QUOTA}" \
        ${MEM_HIGH:+--property="MemoryHigh=${MEM_HIGH}"} \
        --property="MemoryMax=${MEM_MAX}" \
        --property="MemorySwapMax=0" \
        --property="IOWeight=${IO_WEIGHT}" \
        -- "$PYTHON" "$SCRIPT_DIR/stl_batch_fix_tui.py" "$@"
else
    # No systemd user manager (e.g. plain SSH session, container).  Nothing then
    # bounds memory at all, and the TUI's own SIGKILL sweep is the only thing
    # stopping workers from outliving the run.
    echo "run.sh: no systemd user manager — running without cgroup limits" >&2
    exec "$PYTHON" "$SCRIPT_DIR/stl_batch_fix_tui.py" "$@"
fi
