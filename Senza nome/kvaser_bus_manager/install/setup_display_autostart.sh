#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTOSTART_DIR="/etc/xdg/autostart"
AUTOSTART_FILE="${AUTOSTART_DIR}/kvbm-display.desktop"

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root (sudo)." >&2
  exit 1
fi

chmod +x "${ROOT_DIR}/install/run_kvbm_display.sh"
mkdir -p "${AUTOSTART_DIR}"

cat >"${AUTOSTART_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=KVBM Display
Comment=Launch the EV-Q recording status display when a monitor is attached
Exec=${ROOT_DIR}/install/run_kvbm_display.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

chmod 0644 "${AUTOSTART_FILE}"

echo "Installed display autostart: ${AUTOSTART_FILE}"
echo "The browser launcher will switch to kiosk mode only on small displays."