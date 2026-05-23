#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# XCP-on-CAN Update Installer for TRC OnBoard Sentinel
# Date:    2026-03-03
# Version: 1.0
# ═══════════════════════════════════════════════════════════════════════════════
#
# WHAT THIS UPDATE INCLUDES:
#   ✓ XCP-over-CAN client (ASAM XCP Part 2 + Part 5)
#   ✓ A2L / GLC / LAB / MAP / SYM / SKB parsers
#   ✓ 7-tab XCP UI (Connection, Files, Signal Browser, Active Signals,
#     Acquisition, Live Data, Log)
#   ✓ 242 real production signals (DIM_5201C621 gearbox ECU)
#   ✓ DAQ / Polling mode with per-signal raster & prescaler editing
#   ✓ Project save/load with auto-restore on restart
#   ✓ Seed & Key authentication support
#   ✓ Auto-persistence: all config survives service restart
#   ✓ Production project: DIM_5201C621_PRODUCTION (pre-configured)
#
# USAGE:
#   chmod +x install_xcp_update.sh
#   sudo ./install_xcp_update.sh [TARGET_DIR]
#
#   TARGET_DIR defaults to /home/boss/TRC_OnBOard_Sentiel
#
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${CYAN}[XCP-UPDATE]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"
TARGET="${1:-/home/boss/TRC_OnBOard_Sentiel}"
BACKUP_DIR="$TARGET/backups/pre_xcp_update_$(date +%Y%m%d_%H%M%S)"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  TRC OnBoard Sentinel — XCP-on-CAN Update Installer"
echo "  Date: 2026-03-03"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ ! -d "$FILES_DIR" ]]; then
    err "Files directory not found: $FILES_DIR"
    err "Run this script from the update package folder."
    exit 1
fi

if [[ ! -d "$TARGET/kvaser_bus_manager" ]]; then
    err "Target directory not valid: $TARGET"
    err "Expected to find $TARGET/kvaser_bus_manager/"
    exit 1
fi

log "Target:  $TARGET"
log "Source:  $FILES_DIR"
log "Backup:  $BACKUP_DIR"
echo ""

# ── Check if running as root or with sudo ────────────────────────────────────
if [[ $EUID -ne 0 ]] && systemctl is-active --quiet kvbm.service 2>/dev/null; then
    warn "Service kvbm.service is running. You may need sudo to restart it."
fi

# ── Stop service ──────────────────────────────────────────────────────────────
log "Stopping kvbm.service..."
if systemctl is-active --quiet kvbm.service 2>/dev/null; then
    sudo systemctl stop kvbm.service 2>/dev/null || warn "Could not stop service (may need sudo)"
    ok "Service stopped"
else
    warn "Service was not running"
fi

# ── Create backup ─────────────────────────────────────────────────────────────
log "Creating backup of existing files..."
mkdir -p "$BACKUP_DIR"

# List of files we'll overwrite — back them up
MODIFIED_FILES=(
    "kvaser_bus_manager/backend/app.py"
    "kvaser_bus_manager/backend/ethernet_capture.py"
    "kvaser_bus_manager/backend/fibex_loader.py"
    "kvaser_bus_manager/frontend/static/js/app.js"
    "kvaser_bus_manager/frontend/templates/_navbar.html"
    "kvaser_bus_manager/scripts/generate_decoded_mf4.py"
)

backed_up=0
for f in "${MODIFIED_FILES[@]}"; do
    src="$TARGET/$f"
    if [[ -f "$src" ]]; then
        dst_dir="$BACKUP_DIR/$(dirname "$f")"
        mkdir -p "$dst_dir"
        cp "$src" "$dst_dir/"
        ((backed_up++))
    fi
done
ok "Backed up $backed_up existing files → $BACKUP_DIR"

# ── Install files ─────────────────────────────────────────────────────────────
log "Installing update files..."

installed=0
skipped=0

# Copy all files from staging, preserving directory structure
while IFS= read -r -d '' src_file; do
    rel_path="${src_file#$FILES_DIR/}"
    dst_file="$TARGET/$rel_path"
    dst_dir="$(dirname "$dst_file")"

    mkdir -p "$dst_dir"
    cp "$src_file" "$dst_file"
    ((installed++))
done < <(find "$FILES_DIR" -type f -print0)

ok "Installed $installed files"

# ── Ensure correct ownership ──────────────────────────────────────────────────
log "Setting ownership (boss:boss)..."
chown -R boss:boss "$TARGET/kvaser_bus_manager/" 2>/dev/null || warn "Could not chown (run with sudo)"
if [[ -d "$TARGET/xcp" ]]; then
    chown -R boss:boss "$TARGET/xcp/" 2>/dev/null || true
fi
ok "Ownership set"

# ── Ensure log directories exist ──────────────────────────────────────────────
log "Creating required directories..."
mkdir -p "$TARGET/kvaser_bus_manager/logs/xcp_can_projects" 2>/dev/null || true
mkdir -p "$TARGET/kvaser_bus_manager/databases/a2l" 2>/dev/null || true
mkdir -p "$TARGET/kvaser_bus_manager/databases/skb" 2>/dev/null || true
chown -R boss:boss "$TARGET/kvaser_bus_manager/logs" 2>/dev/null || true
chown -R boss:boss "$TARGET/kvaser_bus_manager/databases" 2>/dev/null || true
ok "Directories ready"

# ── Clear Python bytecode cache ───────────────────────────────────────────────
log "Clearing Python cache..."
find "$TARGET/kvaser_bus_manager/backend/__pycache__" -name "*.pyc" -delete 2>/dev/null || true
find "$TARGET/kvaser_bus_manager/backend/__pycache__" -name "*.pyo" -delete 2>/dev/null || true
ok "Cache cleared"

# ── Restart service ───────────────────────────────────────────────────────────
log "Restarting kvbm.service..."
if sudo systemctl restart kvbm.service 2>/dev/null; then
    sleep 3
    if systemctl is-active --quiet kvbm.service 2>/dev/null; then
        ok "Service restarted and running"
    else
        err "Service started but may have failed — check: sudo journalctl -u kvbm.service -n 30"
    fi
else
    warn "Could not restart service (run: sudo systemctl restart kvbm.service)"
fi

# ── Verify installation ──────────────────────────────────────────────────────
log "Verifying installation..."
echo ""

VERIFY_FILES=(
    "kvaser_bus_manager/backend/app.py"
    "kvaser_bus_manager/backend/a2l_xcp_parser.py"
    "kvaser_bus_manager/backend/xcp_can_client.py"
    "kvaser_bus_manager/frontend/templates/xcp_can.html"
    "kvaser_bus_manager/frontend/templates/_navbar.html"
    "kvaser_bus_manager/frontend/static/js/app.js"
    "kvaser_bus_manager/backend/ethernet_capture.py"
    "kvaser_bus_manager/backend/fibex_loader.py"
    "kvaser_bus_manager/scripts/generate_decoded_mf4.py"
    "kvaser_bus_manager/tests/test_fibex_loader_bitdecode.py"
    "kvaser_bus_manager/databases/a2l/5201C621.a2l"
    "kvaser_bus_manager/databases/skb/LBTCU_SeedKey_XCP_0001_v3_2.skb"
    "kvaser_bus_manager/logs/xcp_can_projects/DIM_5201C621_PRODUCTION.json"
)

all_ok=true
for f in "${VERIFY_FILES[@]}"; do
    if [[ -f "$TARGET/$f" ]]; then
        # Verify it matches the update file
        if [[ -f "$FILES_DIR/$f" ]]; then
            if cmp -s "$TARGET/$f" "$FILES_DIR/$f"; then
                ok "$f"
            else
                err "$f (content mismatch!)"
                all_ok=false
            fi
        else
            ok "$f (exists, no source to compare)"
        fi
    else
        err "$f (MISSING!)"
        all_ok=false
    fi
done

echo ""

# ── Post-install API check ───────────────────────────────────────────────────
log "Testing XCP API endpoints..."
sleep 5

api_ok=true
# Test config endpoint
cfg_resp=$(curl -s -m 5 http://localhost:5000/api/xcp/can/config 2>/dev/null || echo '{}')
if echo "$cfg_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok')" 2>/dev/null; then
    ok "GET /api/xcp/can/config → ok"
else
    warn "GET /api/xcp/can/config → service may still be starting (A2L parse takes ~15s)"
    api_ok=false
fi

# Test signals endpoint
sig_resp=$(curl -s -m 5 http://localhost:5000/api/xcp/can/signals 2>/dev/null || echo '{}')
sig_count=$(echo "$sig_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('signals',[])))" 2>/dev/null || echo "0")
if [[ "$sig_count" -gt 0 ]]; then
    ok "GET /api/xcp/can/signals → $sig_count signals auto-restored"
else
    warn "GET /api/xcp/can/signals → 0 signals (auto-restore may still be in progress)"
fi

# Test signal_acq endpoint
acq_resp=$(curl -s -m 5 http://localhost:5000/api/xcp/can/signal_acq 2>/dev/null || echo '{}')
acq_count=$(echo "$acq_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('signal_acq',{})))" 2>/dev/null || echo "0")
if [[ "$acq_count" -gt 0 ]]; then
    ok "GET /api/xcp/can/signal_acq → $acq_count acq configs auto-restored"
else
    warn "GET /api/xcp/can/signal_acq → 0 configs (auto-restore may still be in progress)"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
if $all_ok; then
    echo -e "  ${GREEN}UPDATE INSTALLED SUCCESSFULLY${NC}"
else
    echo -e "  ${YELLOW}UPDATE INSTALLED WITH WARNINGS${NC}"
    echo "  Check the errors above and verify manually."
fi
echo ""
echo "  Files installed:  $installed"
echo "  Files backed up:  $backed_up"
echo "  Backup location:  $BACKUP_DIR"
echo ""
echo "  XCP UI:  http://<device-ip>:5000/xcp_can"
echo ""
echo "  To rollback:  cp -r $BACKUP_DIR/* $TARGET/"
echo "                sudo systemctl restart kvbm.service"
echo "═══════════════════════════════════════════════════════════════"
echo ""
