#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Moonwalk — Build, Sign, Notarize & Upload to GCS
# ═══════════════════════════════════════════════════════════════
#
#  One-command release pipeline:
#    1. Package the Chrome extension
#    2. Build the signed & notarized DMG
#    3. Upload everything to GCS
#
#  Usage:
#    chmod +x scripts/release.sh
#    ./scripts/release.sh
#
#  For code signing + notarization, set these env vars:
#    export APPLE_ID="your@email.com"
#    export APPLE_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
#    export APPLE_TEAM_ID="XXXXXXXXXX"
#    export CSC_NAME="Developer ID Application: Your Name (TEAMID)"
#
#  Without these, the build will still produce a DMG — just unsigned.
#
#  For GCS upload:
#    export GCP_PROJECT="your-project-id"
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${GREEN}[Moonwalk]${NC} $1"; }
warn()    { echo -e "${YELLOW}[Moonwalk]${NC} $1"; }
fail()    { echo -e "${RED}[Moonwalk]${NC} $1"; exit 1; }
banner()  { echo -e "\n${CYAN}═══════════════════════════════════════════════${NC}\n${BOLD}  $1${NC}\n${CYAN}═══════════════════════════════════════════════${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

VERSION=$(node -e "console.log(require('./package.json').version)")

banner "Moonwalk Release v${VERSION}"

# ── Pre-flight Checks ──

info "Checking prerequisites..."

command -v node  &>/dev/null || fail "Node.js not found"
command -v npm   &>/dev/null || fail "npm not found"

# Check signing setup
if [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    info "✅ Apple notarization credentials found"
    WILL_NOTARIZE=true
else
    warn "⚠️  Apple notarization credentials not set — building unsigned DMG"
    warn "   Set APPLE_ID, APPLE_APP_PASSWORD, APPLE_TEAM_ID to enable"
    WILL_NOTARIZE=false
fi

if [[ -n "${CSC_NAME:-}" ]]; then
    info "✅ Code signing identity: $CSC_NAME"
else
    warn "⚠️  CSC_NAME not set — DMG will not be code-signed"
    warn "   Set CSC_NAME='Developer ID Application: Name (TEAMID)'"
fi

# ── Step 1: Package Chrome Extension ──

banner "Step 1: Package Chrome Extension"

info "Creating extension zip..."
node scripts/package-extension.mjs

# ── Step 2: Build DMG ──

banner "Step 2: Build macOS DMG"

info "Cleaning dist/..."
rm -rf dist/*.dmg dist/*.blockmap dist/*.yml 2>/dev/null || true

if [[ "$WILL_NOTARIZE" == true ]]; then
    info "Building signed + notarized universal DMG..."
    npx electron-builder --mac --universal
else
    info "Building unsigned universal DMG..."
    npx electron-builder --mac --universal
fi

# Find the built DMG
DMG=$(ls dist/*.dmg 2>/dev/null | head -1)
if [[ -z "$DMG" ]]; then
    fail "No DMG found in dist/ — build may have failed"
fi
info "✅ Built: $DMG"

# ── Step 3: Upload to GCS ──

banner "Step 3: Upload to GCS"

if command -v gsutil &>/dev/null; then
    node scripts/upload-gcs.mjs
else
    warn "gsutil not found — skipping GCS upload"
    warn "Install: brew install --cask google-cloud-sdk"
    echo ""
    info "Your release artifacts are in dist/:"
    ls -lh dist/*.dmg dist/*.zip 2>/dev/null || true
fi

banner "Release Complete! 🚀"
echo ""
info "Artifacts:"
ls -lh dist/*.dmg dist/*.zip 2>/dev/null | while read line; do
    echo "  $line"
done
echo ""
