#!/usr/bin/env bash
set -euo pipefail

# Simple app-level watchdog: if the API is unresponsive, restart kvbm.service.
# Intended to be run by systemd timer.

SERVICE_NAME="kvbm.service"
API_URL="${KVBM_HEALTHCHECK_URL:-http://127.0.0.1:5000/api/config}"
CONNECT_TIMEOUT_S="${KVBM_HEALTHCHECK_CONNECT_TIMEOUT_S:-2}"
MAX_TIME_S="${KVBM_HEALTHCHECK_MAX_TIME_S:-3}"
# Grace period: don't restart the service if it was started less than N seconds ago.
# The backend needs ~55-60s to load all DBC files + mirror preload before Flask starts listening.
STARTUP_GRACE_S="${KVBM_HEALTHCHECK_STARTUP_GRACE_S:-120}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found; cannot healthcheck"
  exit 0
fi

# --- Startup grace: skip check if the service was started recently ---
if command -v systemctl >/dev/null 2>&1; then
  active_enter=$(systemctl show -p ActiveEnterTimestampMonotonic --value "${SERVICE_NAME}" 2>/dev/null || echo "")
  now_mono=$(cat /proc/uptime 2>/dev/null | awk '{printf "%d", $1 * 1000000}')
  if [[ -n "$active_enter" && "$active_enter" != "0" && -n "$now_mono" ]]; then
    elapsed_us=$(( now_mono - active_enter ))
    grace_us=$(( STARTUP_GRACE_S * 1000000 ))
    if (( elapsed_us < grace_us )); then
      echo "kvbm healthcheck: service started ${elapsed_us}us ago, within ${STARTUP_GRACE_S}s grace – skipping"
      exit 0
    fi
  fi
fi

if curl -fsS --connect-timeout "${CONNECT_TIMEOUT_S}" --max-time "${MAX_TIME_S}" "${API_URL}" >/dev/null; then
  exit 0
fi

echo "kvbm healthcheck failed; restarting ${SERVICE_NAME}"
/usr/bin/systemctl restart "${SERVICE_NAME}" || true
