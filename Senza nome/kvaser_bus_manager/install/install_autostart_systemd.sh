#!/usr/bin/env bash
set -euo pipefail

# Installs a systemd service so the app starts on Raspberry Pi boot.
# Run with sudo.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="kvbm"
TARGET_UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root (sudo)."
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/install/kvbm.service.template" ]]; then
  echo "ERROR: missing template: ${ROOT_DIR}/install/kvbm.service.template"
  exit 1
fi

APP_USER="${SUDO_USER:-root}"
WORKDIR="$ROOT_DIR"

# Render unit file
sed \
  -e "s|{{USER}}|${APP_USER}|g" \
  -e "s|{{WORKDIR}}|${WORKDIR}|g" \
  "${ROOT_DIR}/install/kvbm.service.template" > "$TARGET_UNIT"

chmod 0644 "$TARGET_UNIT"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

# If something is already listening on the default port, systemd will crash-loop.
# Only terminate listeners that are clearly this app (backend/app.py under our WORKDIR).
if command -v ss >/dev/null 2>&1; then
  LISTENER_PID="$(ss -lptn 'sport = :5000' 2>/dev/null | sed -nE 's/.*pid=([0-9]+).*/\1/p' | head -n 1 || true)"
  if [[ -n "${LISTENER_PID}" ]]; then
    LISTENER_CMD="$(ps -p "${LISTENER_PID}" -o cmd= 2>/dev/null || true)"
    if [[ "${LISTENER_CMD}" == *"${WORKDIR}/.venv/bin/python"*"backend/app.py"* ]] || [[ "${LISTENER_CMD}" == *"backend/app.py"*"${WORKDIR}"* ]]; then
      echo "Port 5000 is already in use by an existing kvbm instance (pid=${LISTENER_PID}); stopping it."
      kill "${LISTENER_PID}" || true
      sleep 1
    else
      echo "ERROR: Port 5000 is already in use by another process (pid=${LISTENER_PID})."
      echo "Command: ${LISTENER_CMD}"
      echo "Stop it or change the app port, then re-run this installer."
      exit 1
    fi
  fi
fi

systemctl restart "${SERVICE_NAME}.service"

echo "Installed and started ${SERVICE_NAME}.service"
echo "Check status: systemctl status ${SERVICE_NAME}.service --no-pager"
echo "Logs: journalctl -u ${SERVICE_NAME}.service -f"
