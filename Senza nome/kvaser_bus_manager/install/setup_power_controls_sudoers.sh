#!/usr/bin/env bash
set -euo pipefail

log() { echo ">>> $*"; }
warn() { echo "WARNING: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  die "Run as root (sudo)."
fi

USER_NAME="${1:-}"
if [[ -z "$USER_NAME" ]]; then
  # Prefer the user configured in the service, else fall back to the sudo caller.
  USER_NAME="$(systemctl show -p User kvbm.service 2>/dev/null | sed 's/^User=//' | tr -d ' ' || true)"
  if [[ -z "$USER_NAME" ]]; then
    USER_NAME="${SUDO_USER:-}"
  fi
fi
[[ -n "$USER_NAME" ]] || die "Could not determine target user. Pass it as: sudo $0 <user>"

SYSTEMCTL_BIN="$(command -v systemctl || true)"
[[ -n "$SYSTEMCTL_BIN" ]] || SYSTEMCTL_BIN="/usr/bin/systemctl"

OUT_FILE="/etc/sudoers.d/kvbm-power"
log "Installing sudoers rule: $OUT_FILE (user=$USER_NAME)"

# Allow only the minimal commands needed by the UI power buttons.
cat > "$OUT_FILE" <<EOF
# Managed by Kvaser Bus Manager installer
# Allow kvbm UI to reboot/shutdown without interactive auth.

Defaults:${USER_NAME} !requiretty

${USER_NAME} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} reboot
${USER_NAME} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} poweroff
EOF

chmod 0440 "$OUT_FILE"

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$OUT_FILE" >/dev/null
fi

log "OK: power controls sudoers installed"
