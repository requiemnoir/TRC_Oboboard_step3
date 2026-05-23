#!/usr/bin/env bash
# Avvia stack TRC Onboard su macOS via Docker Desktop.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE="${1:-mirror}"
ENV_FILE="${ROOT_DIR}/docker/mac.env"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.mac.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker non trovato. Installa Docker Desktop per Mac."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker non è in esecuzione. Avvia Docker Desktop e riprova."
  exit 1
fi

# Config runtime mirror (merge user.json se assente)
mkdir -p "${ROOT_DIR}/mirror_logger/config"
if [[ ! -f "${ROOT_DIR}/mirror_logger/config/user.json" ]]; then
  cp "${ROOT_DIR}/mirror_logger/config/default.json" "${ROOT_DIR}/mirror_logger/config/user.json"
fi
# Profilo Mac: logging simulato, senza DoIP verso gateway reale
python3 <<'PY'
import json
from pathlib import Path
p = Path("mirror_logger/config/user.json")
data = json.loads(p.read_text(encoding="utf-8"))
data["auto_start_capture"] = True
data["auto_activate_mirror"] = False
data["pcap_enabled"] = False
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

echo "[*] Build e avvio (profilo: ${PROFILE})..."
case "${PROFILE}" in
  mirror)
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up --build -d mirror-logger
    ;;
  full)
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" --profile full up --build -d
    ;;
  *)
    echo "Uso: $0 [mirror|full]"
    exit 2
    ;;
esac

echo
echo "=== Servizi ==="
docker compose -f "${COMPOSE_FILE}" ps

echo
echo "=== URL ==="
echo "  mirror_logger: http://127.0.0.1:5050"
if [[ "${PROFILE}" == "full" ]]; then
  KVBM_PORT="${KVBM_HOST_PORT:-5001}"
  echo "  kvbm:          http://127.0.0.1:${KVBM_PORT}"
fi
echo
echo "Health:"
echo "  curl -s http://127.0.0.1:5050/api/health | python3 -m json.tool"
echo
echo "Log: docker compose -f docker-compose.mac.yml logs -f mirror-logger"
