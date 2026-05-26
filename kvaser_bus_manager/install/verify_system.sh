#!/usr/bin/env bash
# verify_system.sh — controllo end-to-end del sistema TRC Onboard sulla Pi.
# Lancia DOPO il boot per validare ogni componente. Idempotente, read-only.
#
# Output a colori; exit code 0 = tutto OK, !=0 = almeno un check critico fallito.
#
# Usage:
#   bash install/verify_system.sh
#   bash install/verify_system.sh --json    # output JSON per parsing

set -uo pipefail

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

declare -A R   # results: [check_name]="ok|warn|fail|msg"
errors=0
warns=0

ok()   { R["$1"]="ok|$2"; }
warn() { R["$1"]="warn|$2"; ((warns++)); }
fail() { R["$1"]="fail|$2"; ((errors++)); }

# ─── 1. Backend service ────────────────────────────────────────────
if systemctl is-active --quiet trc-native.service; then
  ok service "trc-native.service active ($(systemctl show -p ActiveEnterTimestamp --value trc-native.service))"
else
  fail service "trc-native.service NOT active: $(systemctl is-active trc-native.service)"
fi

# ─── 2. /api/live (HTTP) ───────────────────────────────────────────
port=$(awk -F= '/^[[:space:]]*KBSM_PORT[[:space:]]*=/{gsub(/[[:space:]"'"'"']/, "", $2); print $2}' \
         /etc/default/trc-native /etc/default/trc-usb 2>/dev/null | tail -n1)
port="${port:-5000}"
if curl -sk --max-time 3 "http://127.0.0.1:${port}/api/live" >/dev/null 2>&1; then
  ok api_live "http://127.0.0.1:${port}/api/live responding"
elif curl -sk --max-time 3 "https://127.0.0.1:${port}/api/live" >/dev/null 2>&1; then
  ok api_live "https://127.0.0.1:${port}/api/live responding (TLS mode)"
else
  fail api_live "backend non risponde su porta $port (HTTP/HTTPS entrambi falliti)"
fi

# ─── 3. Display autostart entry ────────────────────────────────────
if [[ -f /home/boss/.config/autostart/trc-display.desktop ]] \
   || [[ -f /etc/xdg/autostart/trc-display.desktop ]]; then
  loc=""
  [[ -f /home/boss/.config/autostart/trc-display.desktop ]] && loc="user-scope"
  [[ -f /etc/xdg/autostart/trc-display.desktop ]] && loc="${loc:+$loc + }system-scope"
  ok autostart "trc-display.desktop installato ($loc)"
else
  fail autostart "trc-display.desktop NON installato"
fi

# ─── 4. Chromium browser ───────────────────────────────────────────
if command -v chromium-browser >/dev/null 2>&1; then
  ok chromium "chromium-browser ($(chromium-browser --version 2>&1 | head -1))"
elif command -v chromium >/dev/null 2>&1; then
  ok chromium "chromium ($(chromium --version 2>&1 | head -1))"
else
  fail chromium "nessun chromium installato: sudo apt install chromium-browser"
fi

# ─── 5. Chromium kiosk actually running? ───────────────────────────
if pgrep -af "chromium.*--kiosk" >/dev/null 2>&1; then
  ok kiosk "kiosk Chromium attivo (PID $(pgrep -f 'chromium.*--kiosk' | head -1))"
else
  warn kiosk "kiosk non attivo (boot recente? reboot + verifica)"
fi

# ─── 6. Mirror UDP listener ────────────────────────────────────────
if ss -uln 2>/dev/null | grep -q ":30490\b"; then
  ok mirror_udp "listener UDP :30490 attivo"
else
  warn mirror_udp ":30490 non in LISTEN (mirror disabled? KBSM_MIRROR_LISTEN_ENABLED=1)"
fi

# ─── 7. Kvaser kernel modules ──────────────────────────────────────
if lsmod 2>/dev/null | grep -qE '^(mhydra|leaf|kvpcicanII|usbcanII|kvpciefd|kvvirtualcan)\b'; then
  loaded=$(lsmod | grep -E '^(mhydra|leaf|kvpci|usbcanII|kvvirtualcan)' | awk '{print $1}' | tr '\n' ',' | sed 's/,$//')
  ok kvaser "moduli kernel caricati: $loaded"
else
  warn kvaser "nessun modulo Kvaser caricato: bash base/install/kvaser_drivers_src/linuxcan/installscript.sh"
fi

# ─── 8. Voice / Piper ──────────────────────────────────────────────
if [[ -x /usr/local/bin/piper && -f /opt/piper/models/it_IT-paola-medium.onnx ]]; then
  ok piper "piper + modello it_IT-paola"
else
  warn piper "piper o modello mancante"
fi

# ─── 9. Voice / Whisper ────────────────────────────────────────────
if [[ -x /usr/local/bin/whisper-cli && -f /opt/whisper.cpp/models/ggml-small-q5_1.bin ]]; then
  ok whisper "whisper-cli + modello ggml-small-q5_1"
else
  warn whisper "whisper-cli o modello mancante"
fi

# ─── 10. Ollama / LLM ──────────────────────────────────────────────
if command -v ollama >/dev/null 2>&1; then
  # Ollama default port is 11434 (was 11435 in earlier setup); fall back across both.
  models_out=""
  for host in 127.0.0.1:11434 127.0.0.1:11435; do
    if out=$(OLLAMA_HOST=$host ollama list 2>/dev/null) && [[ -n "$out" ]]; then
      models_out="$out"; break
    fi
  done
  found=$(grep -iEo 'gemma[0-9]+:[A-Za-z0-9._-]+' <<<"$models_out" | head -1)
  if [[ -n "$found" ]]; then
    ok llm "ollama installato + $found disponibile"
  else
    warn llm "ollama installato ma nessun gemma pulled: ollama pull gemma3:270m"
  fi
else
  warn llm "ollama NON installato: install_models.sh ollama"
fi

# ─── 11. YOLO ──────────────────────────────────────────────────────
REPO_DIR="/home/boss/Documents/lambo-trc-onboard-ehra"
if "${REPO_DIR}/.venv/bin/pip" show ultralytics >/dev/null 2>&1; then
  ok yolo "ultralytics installato in venv"
else
  warn yolo "ultralytics NON installato: install_models.sh yolo"
fi

# ─── 12. DBC/ARXML/FIBEX ───────────────────────────────────────────
# Use `-L` so symlinked DB roots (e.g. config/db → mf4_standalone_decoder/databases)
# are followed. Also scan the kvaser_bus_manager databases dir used by the backend
# (UPLOAD_FOLDER_DBC) and the mf4 decoder databases dir directly, in case config/db
# isn't wired up yet.
db_count=0
for d in \
  "${REPO_DIR}/config/db" \
  "${REPO_DIR}/config" \
  "${REPO_DIR}/base/kvaser_bus_manager/config/db" \
  "${REPO_DIR}/base/kvaser_bus_manager/databases" \
  "${REPO_DIR}/mf4_standalone_decoder/databases"; do
  if [[ -d "$d" || -L "$d" ]]; then
    db_count=$((db_count + $(find -L "$d" -maxdepth 3 \( -name "*.dbc" -o -name "*.arxml" -o -name "*.xml" \) 2>/dev/null | wc -l)))
  fi
done
if (( db_count > 0 )); then
  ok db "$db_count file DBC/ARXML/XML trovati"
else
  warn db "nessun DBC/ARXML/FIBEX in config/db — decoding signal-level disabilitato"
fi

# ─── 13. Logger directory writable ─────────────────────────────────
if [[ -w "${REPO_DIR}/logs" ]]; then
  free_mb=$(df -m "${REPO_DIR}/logs" | tail -1 | awk '{print $4}')
  ok logs "${REPO_DIR}/logs scrivibile, ${free_mb} MB liberi"
else
  fail logs "${REPO_DIR}/logs non scrivibile"
fi

# ─── 14. Heartbeat (opzionale) ─────────────────────────────────────
if systemctl is-enabled --quiet trc-heartbeat.service 2>/dev/null; then
  ok heartbeat "trc-heartbeat enabled"
else
  warn heartbeat "trc-heartbeat NON enabled (opzionale)"
fi

# ─── Output ────────────────────────────────────────────────────────
if (( JSON == 1 )); then
  python3 -c "
import json
r = {}
$(for k in "${!R[@]}"; do
    v="${R[$k]}"
    status="${v%%|*}"
    msg="${v#*|}"
    printf 'r[%q] = {%q: %q, %q: %q}\n' "$k" "status" "$status" "message" "$msg"
  done)
print(json.dumps(r, indent=2, ensure_ascii=False))
"
else
  echo ""
  echo "==== TRC Onboard verify_system ===="
  for k in service api_live autostart chromium kiosk mirror_udp kvaser piper whisper llm yolo db logs heartbeat; do
    v="${R[$k]:-fail|not checked}"
    status="${v%%|*}"
    msg="${v#*|}"
    case "$status" in
      ok)   col="\033[32m✓\033[0m" ;;
      warn) col="\033[33m!\033[0m" ;;
      fail) col="\033[31m✗\033[0m" ;;
      *)    col="?" ;;
    esac
    printf "  %b  %-12s  %s\n" "$col" "$k" "$msg"
  done
  echo ""
  echo "Sommario: $errors fail, $warns warn, $(( ${#R[@]} - errors - warns )) ok"
fi

(( errors > 0 )) && exit 2
(( warns > 0 )) && exit 1
exit 0
