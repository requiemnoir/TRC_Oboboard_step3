#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/snapshots}"
TS="$(date -Iseconds | tr ':' '-')"
ARCHIVE="$OUT_DIR/TRC_OnBoard_full_${TS}.tar.gz"

mkdir -p "$OUT_DIR"

# NOTE: This creates a local archive of the *current machine state* artifacts.
# It is intentionally NOT auto-committed to git (too large for GitHub).
# You can upload the resulting archive to external storage or GitHub Releases.

tar \
  --exclude-vcs \
  --exclude='./.venv' \
  --exclude='./kvaser_bus_manager/.venv' \
  -czf "$ARCHIVE" \
  -C "$ROOT_DIR" \
  kvaser_bus_manager/config/app_config.json \
  kvaser_bus_manager/databases \
  kvaser_bus_manager/projects \
  kvaser_bus_manager/yolov8n.pt \
  repro_state \
  README.md docs install.sh VEHICLE_TEST_RUNBOOK.md \
  kvaser_bus_manager/frontend kvaser_bus_manager/backend kvaser_bus_manager/requirements*.txt \
  requirements-lock-root.txt \
  .gitignore

echo "Created: $ARCHIVE"
