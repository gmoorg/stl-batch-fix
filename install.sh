#!/usr/bin/env bash
# install.sh — check and install dependencies for stl_batch_fix.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the venv Python (same logic as run.sh).
VENV_PYTHON="$SCRIPT_DIR/../.venv/bin/python"
if [ -z "${PYTHON:-}" ] && [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="${PYTHON:-python3}"
fi


RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  [OK]${NC} $*"; }
warn() { echo -e "${YELLOW}  [WARN]${NC} $*"; }
fail() { echo -e "${RED}  [FAIL]${NC} $*"; }

echo "=== STL Batch Fix — dependency check ==="
echo

# ── Python ────────────────────────────────────────────────────────────────────
echo "→ Python"
if command -v "$PYTHON" &>/dev/null; then
    ver=$("$PYTHON" --version 2>&1)
    ok "$ver"
else
    fail "Python 3 not found. Install it from https://python.org or via your package manager."
    exit 1
fi

# ── pip ───────────────────────────────────────────────────────────────────────
echo "→ pip"
if "$PYTHON" -m pip --version &>/dev/null; then
    ok "pip available"
else
    warn "pip not found — trying to install via ensurepip"
    "$PYTHON" -m ensurepip --upgrade || { fail "Could not install pip. Install manually."; exit 1; }
fi

# ── pymeshlab ─────────────────────────────────────────────────────────────────
echo "→ pymeshlab"
if "$PYTHON" -c "import pymeshlab" 2>/dev/null; then
    ver=$("$PYTHON" -c "import pymeshlab; print(pymeshlab.__version__)" 2>/dev/null || echo "unknown")
    ok "pymeshlab $ver already installed"
else
    warn "pymeshlab not found — installing..."
    "$PYTHON" -m pip install pymeshlab && ok "pymeshlab installed" || fail "pymeshlab install failed"
fi

# ── pymeshfix ─────────────────────────────────────────────────────────────────
echo "→ pymeshfix"
if "$PYTHON" -c "import pymeshfix" 2>/dev/null; then
    ok "pymeshfix already installed"
else
    warn "pymeshfix not found — installing..."
    "$PYTHON" -m pip install pymeshfix && ok "pymeshfix installed" || {
        warn "pymeshfix install failed — the script will still work but open-edge repair uses Blender only"
    }
fi

# ── fast-simplification ───────────────────────────────────────────────────────
# Primary decimator. Same quadric edge collapse as PyMeshLab/Blender, but
# operating on numpy arrays instead of a full mesh database: measured ~7.5s /
# 1.1 GB where PyMeshLab needs 42s / 1.6 GB and Blender OOMs, on a 2.55M
# triangle mesh. Without it the pipeline falls back to PyMeshLab, then Blender.
echo "→ fast-simplification"
if "$PYTHON" -c "import fast_simplification" 2>/dev/null; then
    ok "fast-simplification already installed"
else
    warn "fast-simplification not found — installing..."
    "$PYTHON" -m pip install fast-simplification && ok "fast-simplification installed" || {
        warn "fast-simplification install failed — decimation falls back to PyMeshLab/Blender (slower, more memory)"
    }
fi

# ── numpy (required by pymeshfix) ─────────────────────────────────────────────
echo "→ numpy"
if "$PYTHON" -c "import numpy" 2>/dev/null; then
    ok "numpy already installed"
else
    warn "numpy not found — installing..."
    "$PYTHON" -m pip install numpy && ok "numpy installed" || fail "numpy install failed"
fi

# ── rich (required by the TUI) ────────────────────────────────────────────────
echo "→ rich"
if "$PYTHON" -c "from rich.console import Console" 2>/dev/null; then
    ok "rich already installed"
else
    warn "rich not found — installing..."
    "$PYTHON" -m pip install rich && ok "rich installed" || fail "rich install failed"
fi

# ── Blender ───────────────────────────────────────────────────────────────────
echo "→ Blender"
BLENDER_BIN="${BLENDER_BIN:-blender}"
if command -v "$BLENDER_BIN" &>/dev/null; then
    ver=$("$BLENDER_BIN" --version 2>&1 | head -1 || echo "unknown")
    ok "$ver"
else
    warn "Blender not found on PATH."
    echo "   Blender is the fallback repair tool (used when pymeshlab/pymeshfix cannot fully fix a mesh)."
    echo "   Download: https://www.blender.org/download/"
    echo "   After installing, either:"
    echo "     • Add Blender to your PATH, or"
    echo "     • Set BLENDER_BIN=/path/to/blender before running run.sh"
fi

echo
echo "=== Done. Run: bash run.sh  (or  INPUT_FOLDER=/path/to/stls  bash run.sh) ==="
