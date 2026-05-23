#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { echo "[validate] $*"; }
fail() { echo "[validate] ERROR: $*" >&2; exit 1; }
warn() { echo "[validate] WARNING: $*" >&2; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }

say "Repo: $ROOT_DIR"
cd "$ROOT_DIR"

need_cmd python3
need_cmd curl

PY="$ROOT_DIR/.venv/bin/python"
PIP="$ROOT_DIR/.venv/bin/pip"
if [[ ! -x "$PY" ]]; then
  fail "venv missing: $PY (run: ./install/install.sh)"
fi

say "Python: $($PY -V 2>/dev/null || true)"

say "Checking key Python imports…"
$PY - <<'PY'
import flask, socketio, cantools
print('ok')
PY

if [[ -f /usr/lib/libcanlib.so ]] || [[ -f /usr/lib/aarch64-linux-gnu/libcanlib.so ]] || [[ -f /usr/local/lib/libcanlib.so ]]; then
  say "Kvaser libcanlib found. Checking canlib import…"
  $PY - <<'PY'
import canlib.canlib
print('canlib import ok')
PY
else
  warn "Kvaser libcanlib not found. If you need real hardware, run: sudo ./install/install_kvaser_drivers.sh"
fi

say "Checking systemd service (if available)…"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files | grep -q '^kvbm\.service'; then
    systemctl is-enabled kvbm.service >/dev/null 2>&1 || warn "kvbm.service not enabled"
    systemctl is-active kvbm.service >/dev/null 2>&1 || warn "kvbm.service not active"
  else
    warn "kvbm.service not installed (run: sudo ./install/install_autostart_systemd.sh)"
  fi
else
  warn "systemctl not available; skipping systemd checks"
fi

say "Checking HTTP endpoints on :5000…"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:5000/api/config >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [[ "$i" -eq 30 ]]; then
    fail "app not reachable on http://127.0.0.1:5000 (start kvbm.service or run ./install/run_kvaser_bus_manager.sh)"
  fi
done

curl -fsS --max-time 3 http://127.0.0.1:5000/dbc_catalog >/dev/null || fail "dbc_catalog page not reachable"
curl -fsS --max-time 3 http://127.0.0.1:5000/copilot >/dev/null || warn "copilot page not reachable (non-fatal)"

say "Checking DBC catalog DB…"
if [[ -f "$ROOT_DIR/logs/monitor/dbc_catalog.db" ]]; then
  say "dbc_catalog.db present"
else
  warn "dbc_catalog.db missing (run: $PY scripts/import_dbc_catalog_db.py)"
fi

say "Checking Ollama/Copilot (optional)…"
if command -v ollama >/dev/null 2>&1; then
  if curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    say "ollama HTTP ok"
    curl -fsS --max-time 5 http://127.0.0.1:5000/api/copilot/status >/dev/null || warn "copilot status API failed"
  else
    warn "ollama installed but HTTP not reachable on :11434"
  fi
else
  warn "ollama not installed (run: ./install/setup_ollama_pi5.sh)"
fi

say "OK: validation finished"
