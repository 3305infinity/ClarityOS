#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Moonwalk — Post-install Python Environment Setup
# ═══════════════════════════════════════════════════════════════
#
#  This script creates a Python virtual environment and installs
#  the required packages for the Moonwalk backend.
#
#  Called automatically on first launch by the Electron app,
#  or can be run manually:
#    chmod +x setup.sh && ./setup.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="${SCRIPT_DIR}/backend"
# MOONWALK_VENV_DIR can be set by Electron to redirect the venv to a writable location
VENV_DIR="${MOONWALK_VENV_DIR:-${SCRIPT_DIR}/venv}"
REQUIREMENTS="${BACKEND_DIR}/requirements.txt"
PYTHON_BIN=""

# ── Colors ──
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[Moonwalk]${NC} $1"; }
warn()    { echo -e "${YELLOW}[Moonwalk]${NC} $1"; }
fail()    { echo -e "${RED}[Moonwalk]${NC} $1"; exit 1; }

# ── Find Python 3.10+ (str | None union syntax requires 3.10+) ──
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            local major="${ver%%.*}"
            local minor="${ver#*.}"
            if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
                PYTHON_BIN="$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# ── Main ──
info "Setting up Moonwalk Python environment..."

if [[ -d "$VENV_DIR" && -f "$VENV_DIR/bin/python3" ]]; then
    info "Virtual environment already exists at $VENV_DIR"
    info "Upgrading packages..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade -r "$REQUIREMENTS" 2>/dev/null || true
    info "Setup complete!"
    exit 0
fi

if ! find_python; then
    fail "Python 3.10+ is required but was not found. Install via: brew install python@3.13"
fi

info "Using $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

info "Creating virtual environment..."
"$PYTHON_BIN" -m venv "$VENV_DIR"

info "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
if [[ -f "$REQUIREMENTS" ]]; then
    "$VENV_DIR/bin/pip" install --quiet -r "$REQUIREMENTS"
fi

info "Setup complete!"
echo ""
echo "To start Moonwalk:"
echo "  npm start"
echo ""
