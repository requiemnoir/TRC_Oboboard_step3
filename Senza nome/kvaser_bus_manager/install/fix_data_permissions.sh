#!/usr/bin/env bash
set -euo pipefail

log() { echo ">>> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  die "Run as root (sudo)."
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET_USER="${1:-${SUDO_USER:-}}"
[[ -n "$TARGET_USER" ]] || die "Could not determine target user. Pass it as: sudo $0 <user>"

TARGET_GROUP="$TARGET_USER"
if getent group "$TARGET_GROUP" >/dev/null 2>&1; then
  :
else
  # Fall back to user's primary group
  TARGET_GROUP="$(id -gn "$TARGET_USER")"
fi

log "Fixing data permissions for user=$TARGET_USER group=$TARGET_GROUP"

# Directories that receive user uploads / persisted data.
DATA_DIRS=(
  "$ROOT_DIR/logs"
  "$ROOT_DIR/logs/monitor"
  "$ROOT_DIR/logs/uploads"
  "$ROOT_DIR/databases"
  "$ROOT_DIR/databases/dbc"
  "$ROOT_DIR/databases/fibex"
  "$ROOT_DIR/projects"
  "$ROOT_DIR/projects/pdx"
  "$ROOT_DIR/config"
  "$ROOT_DIR/custom_objects"
)

for d in "${DATA_DIRS[@]}"; do
  install -d -m 0775 -o "$TARGET_USER" -g "$TARGET_GROUP" "$d"
done

# Ensure everything under these dirs is writable by the service user.
for d in "$ROOT_DIR/logs" "$ROOT_DIR/databases" "$ROOT_DIR/projects" "$ROOT_DIR/config" "$ROOT_DIR/custom_objects"; do
  if [[ -d "$d" ]]; then
    chown -R "$TARGET_USER":"$TARGET_GROUP" "$d"
    chmod -R u+rwX,g+rwX "$d"
  fi
done

log "OK: data directories are writable"
