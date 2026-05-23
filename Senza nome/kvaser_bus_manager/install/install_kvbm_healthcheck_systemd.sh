#!/usr/bin/env bash
set -euo pipefail

# Installs a lightweight watchdog timer that restarts kvbm.service if the HTTP API stops responding.
# Run with sudo.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_SERVICE="/etc/systemd/system/kvbm-healthcheck.service"
TARGET_TIMER="/etc/systemd/system/kvbm-healthcheck.timer"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root (sudo)."
  exit 1
fi

chmod +x "${ROOT_DIR}/install/kvbm_healthcheck.sh"

sed -e "s|{{WORKDIR}}|${ROOT_DIR}|g" "${ROOT_DIR}/install/kvbm-healthcheck.service.template" > "${TARGET_SERVICE}"
sed -e "s|{{WORKDIR}}|${ROOT_DIR}|g" "${ROOT_DIR}/install/kvbm-healthcheck.timer.template" > "${TARGET_TIMER}"

chmod 0644 "${TARGET_SERVICE}" "${TARGET_TIMER}"

systemctl daemon-reload
systemctl enable --now kvbm-healthcheck.timer

echo "Installed kvbm-healthcheck.timer"
echo "Status: systemctl status kvbm-healthcheck.timer --no-pager"
echo "Logs: journalctl -u kvbm-healthcheck.service -f"
