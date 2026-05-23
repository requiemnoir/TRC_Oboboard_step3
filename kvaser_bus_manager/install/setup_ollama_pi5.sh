#!/usr/bin/env bash
set -euo pipefail

# Setup Ollama + llama3.2 on Raspberry Pi 5 (ARM64) and wire kvbm.service env.
# Idempotent: safe to run multiple times.

MODEL_DEFAULT="llama3.2:3b"
BASE_URL_DEFAULT="http://127.0.0.1:11434"

MODEL="${1:-$MODEL_DEFAULT}"
BASE_URL="${2:-$BASE_URL_DEFAULT}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing command: $1" >&2
    exit 1
  }
}

say() { echo "[ollama-setup] $*"; }

need_sudo() {
  command -v sudo >/dev/null 2>&1 || {
    echo "ERROR: sudo not found (required for service install/config)." >&2
    exit 1
  }
}

run_root_sh() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    sh
  else
    need_sudo
    sudo sh
  fi
}

say "Model: $MODEL"
say "Base URL: $BASE_URL"

# 1) Install ollama if missing
if ! command -v ollama >/dev/null 2>&1; then
  say "Ollama not found. Installing…"
  require_cmd curl
  # The official installer typically needs root to place binaries + service.
  curl -fsSL https://ollama.com/install.sh | run_root_sh
else
  say "Ollama already installed: $(ollama --version 2>/dev/null || true)"
fi

# 2) Enable + start service
if command -v systemctl >/dev/null 2>&1; then
  say "Enabling ollama.service…"
  need_sudo
  sudo systemctl enable --now ollama.service
  sudo systemctl is-active --quiet ollama.service && say "ollama.service is active" || {
    say "ollama.service not active; showing status";
    sudo systemctl status ollama.service --no-pager || true
    exit 1
  }
else
  say "systemctl not found; cannot manage ollama.service automatically"
fi

# 3) Wait for HTTP readiness
say "Waiting for Ollama HTTP…"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "$BASE_URL/api/version" >/dev/null 2>&1; then
    say "Ollama HTTP ready"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    say "ERROR: Ollama HTTP not reachable at $BASE_URL"
    exit 1
  fi
done

# 4) Pull model (can take a while)
say "Pulling model: $MODEL (this can take minutes)"
ollama pull "$MODEL"

# 5) Wire kvbm.service override env vars
OVR_DIR="/etc/systemd/system/kvbm.service.d"
OVR_FILE="$OVR_DIR/override.conf"

kvbm_unit_exists() {
  [[ -f /etc/systemd/system/kvbm.service ]] || [[ -f /lib/systemd/system/kvbm.service ]] || [[ -f /usr/lib/systemd/system/kvbm.service ]]
}

if command -v systemctl >/dev/null 2>&1 && kvbm_unit_exists; then
  need_sudo
  say "Writing systemd override: $OVR_FILE"
  sudo mkdir -p "$OVR_DIR"

  # Minimal override: only Copilot env; keep everything else in main unit.
  sudo tee "$OVR_FILE" >/dev/null <<EOF
[Service]
Environment=COPILOT_PROVIDER=ollama
Environment=COPILOT_BASE_URL=$BASE_URL
Environment=COPILOT_MODEL=$MODEL
Environment=COPILOT_TIMEOUT_S=180
Environment=COPILOT_MAX_CONTEXT_CHARS=4000
Environment=COPILOT_LLM_NUM_PREDICT=256
EOF

  say "Reloading systemd + restarting kvbm.service"
  sudo systemctl daemon-reload
  sudo systemctl restart kvbm.service || {
    say "WARNING: could not restart kvbm.service (non-fatal)"
    sudo systemctl status kvbm.service --no-pager || true
  }
else
  say "kvbm.service not found (or systemd missing); skipping service override wiring"
  say "You can export env vars manually: COPILOT_PROVIDER=ollama COPILOT_BASE_URL=$BASE_URL COPILOT_MODEL=$MODEL"
fi

# 6) Quick local API checks (best-effort)
say "Smoke-check Copilot endpoints (best-effort)"
if curl -fsS --max-time 2 http://127.0.0.1:5000/api/config >/dev/null 2>&1; then
  curl -sS --max-time 5 http://127.0.0.1:5000/api/copilot/status | python3 -m json.tool | head -n 80 || true
  curl -sS --max-time 120 -H 'Content-Type: application/json' -d '{"message":"Ciao! Riassumi lo stato attuale in 5 bullet (frasi corte)."}' http://127.0.0.1:5000/api/copilot/chat | python3 -m json.tool | head -n 140 || true
else
  say "WARNING: app not reachable on http://127.0.0.1:5000 (skipping copilot endpoint checks)"
fi

say "Done"
