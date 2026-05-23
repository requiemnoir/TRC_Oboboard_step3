#!/usr/bin/env bash
set -euo pipefail

log() { echo ">>> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  die "Run as root (sudo)."
fi

USER_NAME="${1:-}"
if [[ -z "$USER_NAME" ]]; then
  USER_NAME="$(systemctl show -p User kvbm.service 2>/dev/null | sed 's/^User=//' | tr -d ' ' || true)"
  if [[ -z "$USER_NAME" ]]; then
    USER_NAME="${SUDO_USER:-}"
  fi
fi
[[ -n "$USER_NAME" ]] || die "Could not determine target user. Pass it as: sudo $0 <user>"

RULE_FILE="/etc/polkit-1/rules.d/49-kvbm-power.rules"
log "Installing polkit rule: $RULE_FILE (user=$USER_NAME)"

cat > "$RULE_FILE" <<EOF
// Managed by Kvaser Bus Manager installer
// Allow kvbm UI power controls without interactive authentication.

polkit.addRule(function(action, subject) {
  if (!subject || !subject.user) return polkit.Result.NOT_HANDLED;

  if (subject.user === "${USER_NAME}") {
    var a = action.id || "";
    if (a === "org.freedesktop.login1.power-off" ||
        a === "org.freedesktop.login1.power-off-multiple-sessions" ||
        a === "org.freedesktop.login1.power-off-ignore-inhibit" ||
        a === "org.freedesktop.login1.reboot" ||
        a === "org.freedesktop.login1.reboot-multiple-sessions" ||
        a === "org.freedesktop.login1.reboot-ignore-inhibit" ||
        a === "org.freedesktop.login1.halt" ||
        a === "org.freedesktop.login1.halt-multiple-sessions" ||
        a === "org.freedesktop.login1.halt-ignore-inhibit") {
      return polkit.Result.YES;
    }
  }

  return polkit.Result.NOT_HANDLED;
});
EOF

chmod 0644 "$RULE_FILE"

# Best-effort reload
systemctl restart polkit 2>/dev/null || true

log "OK: polkit power rule installed"
