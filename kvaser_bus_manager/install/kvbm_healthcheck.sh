#!/usr/bin/env bash
set -euo pipefail

# App-level watchdog: API health + disk space + optional log retention.
# Intended to be run by systemd timer (kvbm-healthcheck.timer).

SERVICE_NAME="kvbm.service"
API_URL="${KVBM_HEALTHCHECK_URL:-http://127.0.0.1:5000/api/health}"
RETENTION_URL="${KVBM_RETENTION_URL:-http://127.0.0.1:5000/api/maintenance/enforce_retention}"
CONNECT_TIMEOUT_S="${KVBM_HEALTHCHECK_CONNECT_TIMEOUT_S:-2}"
MAX_TIME_S="${KVBM_HEALTHCHECK_MAX_TIME_S:-8}"
# Grace period: don't restart the service if it was started recently.
STARTUP_GRACE_S="${KVBM_HEALTHCHECK_STARTUP_GRACE_S:-120}"
# Trigger retention when free space drops below this threshold (MB).
MIN_FREE_DISK_MB="${KVBM_MIN_FREE_DISK_MB:-512}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found; cannot healthcheck"
  exit 0
fi

# --- Startup grace: skip restart if the service was started recently ---
if command -v systemctl >/dev/null 2>&1; then
  active_enter=$(systemctl show -p ActiveEnterTimestampMonotonic --value "${SERVICE_NAME}" 2>/dev/null || echo "")
  now_mono=$(cat /proc/uptime 2>/dev/null | awk '{printf "%d", $1 * 1000000}')
  if [[ -n "$active_enter" && "$active_enter" != "0" && -n "$now_mono" ]]; then
    elapsed_us=$(( now_mono - active_enter ))
    grace_us=$(( STARTUP_GRACE_S * 1000000 ))
    if (( elapsed_us < grace_us )); then
      echo "kvbm healthcheck: service started ${elapsed_us}us ago, within ${STARTUP_GRACE_S}s grace – skipping restart"
      exit 0
    fi
  fi
fi

health_json="$(curl -fsS --connect-timeout "${CONNECT_TIMEOUT_S}" --max-time "${MAX_TIME_S}" "${API_URL}" 2>/dev/null || true)"

if [[ -z "$health_json" ]]; then
  echo "kvbm healthcheck failed (no response); restarting ${SERVICE_NAME}"
  /usr/bin/systemctl restart "${SERVICE_NAME}" || true
  exit 0
fi

# Disk low: try retention before restart (restart does not free space).
if command -v python3 >/dev/null 2>&1; then
  export MIN_FREE_DISK_MB
  low="$(printf '%s' "$health_json" | python3 -c "
import json,sys,os
try:
    h=json.load(sys.stdin)
except Exception:
    print('0'); raise SystemExit(0)
d=h.get('disk') or {}
free=float(d.get('free_mb') or 0)
min_mb=float(os.environ.get('MIN_FREE_DISK_MB','512') or 512)
print('1' if free>0 and free<min_mb else '0')
" 2>/dev/null || echo 0)"
  if [[ "$low" == "1" ]]; then
    echo "kvbm healthcheck: disk low (<${MIN_FREE_DISK_MB}MB free) — enforcing log retention"
    curl -fsS --connect-timeout "${CONNECT_TIMEOUT_S}" --max-time "${MAX_TIME_S}" \
      -X POST "${RETENTION_URL}" >/dev/null 2>&1 || true
  fi
fi

# API ok flag
if printf '%s' "$health_json" | python3 -c "import json,sys; h=json.load(sys.stdin); sys.exit(0 if h.get('ok') else 1)" 2>/dev/null; then
  exit 0
fi

echo "kvbm healthcheck reported not ok; restarting ${SERVICE_NAME}"
/usr/bin/systemctl restart "${SERVICE_NAME}" || true
