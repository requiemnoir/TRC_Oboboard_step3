#!/usr/bin/env bash
# =============================================================================
# mirror_logger - installer
# =============================================================================
# Crea venv, installa requirements, configura capability AF_PACKET su Linux,
# genera systemd unit opzionale.
#
# Uso:
#   ./install.sh                # installa in ./.venv
#   ./install.sh --systemd      # installa anche service systemd
#   ./install.sh --no-cap       # salta setcap (verra' usato sudo a runtime)
#   ./install.sh --help
# =============================================================================

set -euo pipefail

# ---------- colori ----------
if [[ -t 1 ]]; then
    C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
    C_BLUE=$'\033[1;34m';  C_RESET=$'\033[0m'
else
    C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_RESET=""
fi
log()   { echo "${C_BLUE}[*]${C_RESET} $*"; }
ok()    { echo "${C_GREEN}[OK]${C_RESET} $*"; }
warn()  { echo "${C_YELLOW}[!]${C_RESET} $*"; }
err()   { echo "${C_RED}[ERR]${C_RESET} $*" >&2; }

# ---------- parametri ----------
INSTALL_SYSTEMD=0
DO_SETCAP=1
PORT="${PORT:-5050}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --systemd)  INSTALL_SYSTEMD=1; shift ;;
        --no-cap)   DO_SETCAP=0; shift ;;
        --port)     PORT="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,15p' "$0"
            exit 0 ;;
        *) err "Opzione sconosciuta: $1"; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

# ---------- detect OS ----------
OS_NAME="$(uname -s)"
log "OS rilevato: ${OS_NAME}"
IS_LINUX=0
[[ "$OS_NAME" == "Linux" ]] && IS_LINUX=1

# ---------- python ----------
PY_BIN="${PYTHON:-python3}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
    err "Python3 non trovato. Installa python3.10+ e ripeti."
    exit 1
fi
PY_VER="$($PY_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "Python: $PY_BIN (v${PY_VER})"

PY_OK=$($PY_BIN -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')
if [[ "$PY_OK" != "1" ]]; then
    err "Richiesto Python >= 3.10 (trovato $PY_VER)"
    exit 1
fi

# ---------- venv ----------
VENV_DIR="${SCRIPT_DIR}/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    log "Creazione virtualenv in $VENV_DIR"
    "$PY_BIN" -m venv "$VENV_DIR"
    ok "venv creato"
else
    ok "venv gia' presente"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------- pip install ----------
log "Aggiornamento pip"
python -m pip install --upgrade pip wheel >/dev/null

log "Installazione requirements"
pip install -r requirements.txt
ok "Dipendenze installate"

# ---------- directory di runtime ----------
mkdir -p logs config
ok "Directory logs/ e config/ pronte"

# ---------- capability AF_PACKET (solo Linux) ----------
if [[ "$IS_LINUX" -eq 1 && "$DO_SETCAP" -eq 1 ]]; then
    PY_REAL="$(readlink -f "$VENV_DIR/bin/python")"
    if command -v setcap >/dev/null 2>&1; then
        log "Imposto cap_net_raw,cap_net_admin su: $PY_REAL"
        if sudo setcap cap_net_raw,cap_net_admin=eip "$PY_REAL"; then
            ok "Capability impostate (avvio senza sudo)"
        else
            warn "setcap fallito - usa 'sudo python app.py' a runtime"
        fi
    else
        warn "setcap non disponibile (apt install libcap2-bin) - usa sudo a runtime"
    fi
elif [[ "$IS_LINUX" -eq 0 ]]; then
    warn "Non sei su Linux: AF_PACKET non disponibile."
    warn "Il sistema partira' in modalita' FakeCapture (dati simulati)."
fi

# ---------- token auth ----------
TOKEN_FILE="$SCRIPT_DIR/config/.token"
if [[ ! -f "$TOKEN_FILE" ]]; then
    TOKEN="$(python -c "import secrets; print(secrets.token_urlsafe(32))")"
    echo "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    ok "Token API generato in config/.token"
else
    ok "Token API gia' presente"
fi

# ---------- systemd unit (opzionale) ----------
if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
    if [[ "$IS_LINUX" -ne 1 ]]; then
        warn "--systemd ignorato (non Linux)"
    else
        UNIT_PATH="/etc/systemd/system/mirror-logger.service"
        log "Creazione unit systemd: $UNIT_PATH"
        TOKEN="$(cat "$TOKEN_FILE")"
        sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=TRC Mirror Logger (AUTOSAR Bus Mirror -> MF4)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
Environment=MIRROR_LOGGER_TOKEN=$TOKEN
Environment=PORT=$PORT
Environment=MIRROR_LOGGER_WATCHDOG=1
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/app.py
ExecStop=/usr/bin/curl -fsS -m 20 -X POST -H "X-Auth-Token: $TOKEN" http://127.0.0.1:$PORT/api/stop || true
TimeoutStopSec=60
Restart=always
RestartSec=3
# capability gia' impostate via setcap, niente root
AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF
        sudo systemctl daemon-reload
        sudo systemctl enable mirror-logger.service
        ok "Service installato. Avvio: sudo systemctl start mirror-logger"
    fi
fi

# ---------- riepilogo ----------
echo
echo "=========================================================="
ok   "Installazione completata"
echo "=========================================================="
echo "  venv         : $VENV_DIR"
echo "  token API    : $(cat "$TOKEN_FILE")"
echo "  porta UI     : $PORT"
echo
echo "Avvio manuale:"
echo "  source .venv/bin/activate"
echo "  export MIRROR_LOGGER_TOKEN=\"\$(cat config/.token)\""
echo "  python app.py"
echo
echo "UI: http://localhost:$PORT"
echo "Header API: X-Auth-Token: <token>"
if [[ "$IS_LINUX" -eq 0 ]]; then
    echo
    warn "macOS/Windows -> modalita' FakeCapture attiva (dati simulati)."
    warn "Per dati reali serve un Raspberry Pi / host Linux."
fi
echo "=========================================================="
