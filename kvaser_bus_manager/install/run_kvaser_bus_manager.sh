#!/usr/bin/env bash
set -euo pipefail

# Runs the backend in a predictable way (standalone-friendly).
# Assumes you already installed dependencies into .venv.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: venv not found at $PY"
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Local Copilot/RAG defaults. These can still be overridden by systemd or shell env.
export COPILOT_PROVIDER="${COPILOT_PROVIDER:-ollama}"
export COPILOT_BASE_URL="${COPILOT_BASE_URL:-http://172.30.96.143:11434}"
export COPILOT_MODEL="${COPILOT_MODEL:-lb634-diag:latest}"

# Run with sudo to ensure permission for socket packet capture
# NOTE:
# - When running as a systemd service (User=...), sudo may fail (no TTY / no permission)
# - The systemd unit already grants CAP_NET_RAW/CAP_NET_ADMIN via AmbientCapabilities
#   so we can run without sudo.
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  exec "$PY" backend/app.py
fi

if command -v systemd-detect-virt >/dev/null 2>&1; then
  # In a service context, prefer no sudo.
  if [[ -n "${INVOCATION_ID:-}" ]]; then
    exec "$PY" backend/app.py
  fi
fi

# Interactive/manual runs: try sudo but fall back gracefully. If neither available
# we'll run the backend under a simple supervisor loop so the app auto-restarts
# when launched manually (mirrors systemd behavior for development).
run_once() {
  if sudo -n true >/dev/null 2>&1; then
    exec sudo "$PY" backend/app.py
  else
    echo "[warn] sudo not available; starting without sudo (packet capture may be limited)" >&2
    exec "$PY" backend/app.py
  fi
}

# If we're interactive (terminal attached), supervise restarts locally; otherwise
# just exec single-run (should be handled by systemd where INVOCATION_ID is set).
if [[ -t 1 ]]; then
  echo "[info] Running with local supervisor: will restart on crash (ctrl-c to stop)"
  while true; do
    "$PY" backend/app.py || true
    echo "[warn] backend exited unexpectedly; restarting in 2s..." >&2
    sleep 2
  done
else
  run_once
fi
