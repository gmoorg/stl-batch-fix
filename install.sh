#!/usr/bin/env bash
# install.sh — check and install dependencies for batch_repair.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer the project venv (the one tools/project_python.sh runs).
VENV_PYTHON="$SCRIPT_DIR/../.venv/bin/python"
if [ -z "${PYTHON:-}" ] && [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="${PYTHON:-python3}"
fi


RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  [OK]${NC} $*"; }
warn() { echo -e "${YELLOW}  [WARN]${NC} $*"; }
fail() { echo -e "${RED}  [FAIL]${NC} $*"; }   # prints only: the caller exits

# A REQUIRED package: report it when CHECK imports, else pip-install PIP_NAME
# and run CHECK again. Either failure stops the script — every required
# package goes through here, so none can fall through to "Done" (REVIEW.md
# #3: numpy's `|| fail` once did). VERSION, when not empty, is a Python
# expression printed after CHECK has run.
#   require_package NAME PIP_NAME CHECK VERSION WHY
require_package() {
    local name=$1 pip_name=$2 check=$3 version=$4 why=$5 ver
    if "$PYTHON" -c "$check" 2>/dev/null; then
        if [ -n "$version" ]; then
            ver=$("$PYTHON" -c "$check; print($version)" 2>/dev/null || echo "unknown")
            ok "$name $ver already installed"
        else
            ok "$name already installed"
        fi
        return 0
    fi
    warn "$name not found — installing..."
    if ! "$PYTHON" -m pip install "$pip_name"; then
        fail "$name install failed — it is REQUIRED ($why)"
        exit 1
    fi
    if ! "$PYTHON" -c "$check" 2>/dev/null; then
        fail "$name import failed after install — it is REQUIRED ($why)"
        exit 1
    fi
    ok "$name installed"
}

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
require_package pymeshlab pymeshlab "import pymeshlab" "pymeshlab.__version__" \
    "it is the decimator"

# ── pymeshfix ─────────────────────────────────────────────────────────────────
echo "→ pymeshfix"
require_package pymeshfix pymeshfix "import pymeshfix" "" "hole filling has no fallback"

# ── libigl ────────────────────────────────────────────────────────────────────
# Default part reconstruction (libs/winding.py): exact distance (AABB), fast
# winding number, marching cubes, patch orientation.
# The check names the symbols used, so an old libigl without them fails too.
echo "→ libigl (reconstruction)"
require_package libigl libigl \
    "import igl; igl.FastWindingNumberBVH; igl.marching_cubes; igl.AABB" "" \
    "default repair"

# ── CGAL Alpha Wrapping (optional) ────────────────────────────────────────────
# Alpha-wrap is no longer the default reconstruction (libs/winding.py replaced
# it); it stays available as an explicit step, so CGAL is optional.
echo "→ cgal (Alpha Wrapping, optional)"
if "$PYTHON" -c "from CGAL.CGAL_Alpha_wrap_3 import alpha_wrap_3" 2>/dev/null; then
    ok "cgal Alpha Wrapping already installed"
else
    warn "cgal Alpha Wrapping not found — installing (optional)..."
    if "$PYTHON" -m pip install cgal && "$PYTHON" -c "from CGAL.CGAL_Alpha_wrap_3 import alpha_wrap_3" 2>/dev/null; then
        ok "cgal Alpha Wrapping installed"
    else
        warn "cgal not available — only the optional explicit alpha-wrap step is affected"
    fi
fi

# ── scipy ─────────────────────────────────────────────────────────────────────
# Connected-component analysis (shell detection) via scipy.sparse.csgraph.
# Measured on Mandy_Body_Dinamuuu3D.stl against the per-face Python union-find
# it replaced: 12.75s -> 2.52s raw (2.06M faces), 5.72s -> 0.92s decimated.
# At 0.92s shell counting costs less than the edge scan itself (2.58s), so it
# can be asked on every file. A pure-numpy replacement was tried and was SLOWER on
# real geometry -- see D20 in ../stl-batch-fix.old/archive/docs-before-compact-2026-09-19/refactor/splitting-and-repair/d20.md before attempting one.
echo "→ scipy"
require_package scipy scipy "import scipy" "scipy.__version__" \
    "shell detection has no fallback"

# ── manifold3d (optional) ─────────────────────────────────────────────────────
# Only `support_3mf.py --layout grid` uses it (union of the support grid); the
# repair batch does not.
echo "→ manifold3d (support grid, optional)"
if "$PYTHON" -c "import manifold3d" 2>/dev/null; then
    ok "manifold3d already installed"
else
    warn "manifold3d not found — installing (optional)..."
    if "$PYTHON" -m pip install manifold3d && "$PYTHON" -c "import manifold3d" 2>/dev/null; then
        ok "manifold3d installed"
    else
        warn "manifold3d not available — only support_3mf.py --layout grid is affected"
    fi
fi

# ── matplotlib (optional) ─────────────────────────────────────────────────────
# Only `check_3mf.py --png` uses it (underside maps); it checks it at start.
echo "→ matplotlib (check_3mf.py --png, optional)"
if "$PYTHON" -c "import matplotlib" 2>/dev/null; then
    ok "matplotlib already installed"
else
    warn "matplotlib not found — installing (optional)..."
    if "$PYTHON" -m pip install matplotlib && "$PYTHON" -c "import matplotlib" 2>/dev/null; then
        ok "matplotlib installed"
    else
        warn "matplotlib not available — only check_3mf.py --png is affected"
    fi
fi

# ── numpy (required by pymeshfix) ─────────────────────────────────────────────
echo "→ numpy"
require_package numpy numpy "import numpy" "" "every mesh step uses it"

echo
echo "=== Done. First run: cp batch_repair.example.toml batch_repair.toml, edit it,"
echo "    then: tools/project_python.sh batch_repair.py ==="
