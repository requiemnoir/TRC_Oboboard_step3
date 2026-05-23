#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Kvaser Bus Manager - One-command installer

Usage:
  ./install/install.sh [options]

Options:
  --full              Install everything (default)
  --with-copilot       Install/configure local Copilot via Ollama (Pi5 recommended)
  --no-copilot         Do not install/configure Ollama/Copilot
  --copilot-model M    Ollama model to pull (default: llama3.2:3b)
  --copilot-base-url U Ollama base URL (default: http://127.0.0.1:11434)
  --import-dbc-catalog Pre-import DBC catalog into SQLite (default)
  --no-import-dbc-catalog
                      Skip DBC catalog pre-import
  --no-drivers        Skip Kvaser driver install
  --no-systemd        Skip systemd service install
  --no-healthcheck    Skip kvbm healthcheck timer install
  --hw-watchdog       Enable Raspberry HW watchdog (requires reboot)
  --dev               Install dev/test deps (requirements-dev.txt, e.g. pytest)
  -y, --yes           Non-interactive (assume yes where safe)
  -h, --help          Show help

Notes:
  - Driver install may require a reboot.
  - This script creates a venv at: <repo>/.venv
  - After install, UI is at: http://<raspberry-ip>:5000
EOF
}

YES=0
DO_DRIVERS=1
DO_SYSTEMD=1
DO_HEALTHCHECK=1
DO_HW_WATCHDOG=0
COPILOT_MODE="auto"  # auto|on|off
DO_COPILOT=0
COPILOT_MODEL="llama3.2:3b"
COPILOT_BASE_URL="http://127.0.0.1:11434"
DO_IMPORT_DBC_CATALOG=1
DO_DEV=0
FAIL=0

if [[ $# -eq 0 ]]; then
  :
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      ;;
    --with-copilot)
      COPILOT_MODE="on"
      DO_COPILOT=1
      ;;
    --no-copilot)
      COPILOT_MODE="off"
      DO_COPILOT=0
      ;;
    --copilot-model)
      shift
      COPILOT_MODEL="${1:-}"
      ;;
    --copilot-base-url)
      shift
      COPILOT_BASE_URL="${1:-}"
      ;;
    --import-dbc-catalog)
      DO_IMPORT_DBC_CATALOG=1
      ;;
    --no-import-dbc-catalog)
      DO_IMPORT_DBC_CATALOG=0
      ;;
    --no-drivers)
      DO_DRIVERS=0
      ;;
    --no-systemd)
      DO_SYSTEMD=0
      ;;
    --no-healthcheck)
      DO_HEALTHCHECK=0
      ;;
    --hw-watchdog)
      DO_HW_WATCHDOG=1
      ;;
    --dev)
      DO_DEV=1
      ;;
    -y|--yes)
      YES=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

is_rpi() {
  [[ -f /proc/device-tree/model ]] && grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null
}

is_arm64() {
  [[ "$(uname -m 2>/dev/null || true)" == "aarch64" || "$(uname -m 2>/dev/null || true)" == "arm64" ]]
}

if [[ "$COPILOT_MODE" == "auto" ]]; then
  # Default behavior: enable Copilot/Ollama automatically on Raspberry Pi / ARM64.
  if is_arm64 || is_rpi; then
    DO_COPILOT=1
  else
    DO_COPILOT=0
  fi
fi

if [[ $DO_COPILOT -eq 0 && "$COPILOT_MODE" == "auto" ]]; then
  # If interactive, ask once; keep default off on non-Pi.
  if [[ -t 0 ]]; then
    if confirm "Install Copilot (Ollama + model download)?"; then
      DO_COPILOT=1
    fi
  fi
fi

if [[ $DO_COPILOT -eq 1 ]]; then
  if [[ -z "${COPILOT_MODEL}" ]]; then
    echo "ERROR: --copilot-model requires a value" >&2
    exit 2
  fi
  if [[ -z "${COPILOT_BASE_URL}" ]]; then
    echo "ERROR: --copilot-base-url requires a value" >&2
    exit 2
  fi
fi

cd "$ROOT_DIR"

if [[ ! -f "$ROOT_DIR/requirements.txt" ]]; then
  echo "ERROR: requirements.txt not found at $ROOT_DIR" >&2
  exit 1
fi

if [[ ! -d "$ROOT_DIR/backend" ]]; then
  echo "ERROR: backend/ not found at $ROOT_DIR" >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_root() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

run_root_bash() {
  local script="$1"; shift
  if [[ ! -f "$script" ]]; then
    return 2
  fi
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    bash "$script" "$@"
  else
    sudo bash "$script" "$@"
  fi
}

is_debian_like() {
  [[ -f /etc/debian_version ]] || [[ -f /etc/os-release && "$(. /etc/os-release && echo "${ID_LIKE:-}")" == *debian* ]]
}

confirm() {
  local prompt="$1"
  if [[ $YES -eq 1 ]]; then
    return 0
  fi
  read -r -p "${prompt} [y/N]: " ans
  [[ "${ans,,}" == "y" || "${ans,,}" == "yes" ]]
}

echo "==> Kvaser Bus Manager installer"
echo "    Repo: $ROOT_DIR"

if ! is_debian_like; then
  echo "WARNING: This installer currently targets Debian/Raspberry Pi OS." >&2
  echo "         You can still try, but you may need to install deps manually." >&2
fi

if ! need_cmd python3; then
  echo "ERROR: python3 not found. Install Python 3 first." >&2
  exit 1
fi

echo "==> Installing OS dependencies (apt)"
if is_debian_like && need_cmd apt-get; then
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  sudo apt-get update
  sudo apt-get install -y \
    git \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential \
    libcap2-bin \
    ca-certificates \
    curl \
    tar
else
  echo "Skipping apt-get (not available)." >&2
fi

echo "==> Creating/updating virtualenv (.venv)"
if [[ ! -d "$ROOT_DIR/.venv" ]]; then
  python3 -m venv "$ROOT_DIR/.venv"
fi

"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements.txt"

if [[ $DO_DEV -eq 1 ]]; then
  if [[ -f "$ROOT_DIR/requirements-dev.txt" ]]; then
    echo "==> Installing dev/test dependencies (requirements-dev.txt)"
    "$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements-dev.txt"
  else
    echo "WARNING: requirements-dev.txt not found; skipping --dev" >&2
  fi
fi

if [[ $DO_DRIVERS -eq 1 ]]; then
  echo "==> Installing Kvaser drivers (may require reboot)"
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  if run_root_bash "$ROOT_DIR/install/install_kvaser_drivers.sh"; then
    :
  else
    echo "ERROR: Driver install failed. Retry: sudo ./install/install_kvaser_drivers.sh" >&2
    FAIL=1
  fi

  echo "==> Setting up CAN/FlexRay (best-effort)"
  run_root_bash "$ROOT_DIR/install/setup_can_flexray.sh" || true
else
  echo "==> Skipping Kvaser drivers (--no-drivers)"
fi

echo "==> Validating Python runtime (best-effort)"
"$ROOT_DIR/.venv/bin/python" -c "import flask" >/dev/null 2>&1 || {
  echo "ERROR: Python deps not installed correctly (flask import failed)." >&2
  exit 1
}

if [[ $DO_DRIVERS -eq 1 ]]; then
  # canlib's loader historically called exit(1) when libcanlib.so is missing.
  # Treat failure as fatal for a "full" install.
  if ! "$ROOT_DIR/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import canlib.canlib as _
PY
  then
    echo "ERROR: Kvaser python canlib import failed. Drivers/SDK likely not installed correctly." >&2
    echo "       Check: ls -la /usr/lib/libcanlib.so* and try: sudo ./install/install_kvaser_drivers.sh" >&2
    FAIL=1
  fi
fi

if [[ $DO_IMPORT_DBC_CATALOG -eq 1 ]]; then
  echo "==> Pre-importing DBC catalog into SQLite (logs/monitor/dbc_catalog.db)"
  mkdir -p "$ROOT_DIR/logs/monitor" || true
  if "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/import_dbc_catalog_db.py" \
      --dbc-dir "$ROOT_DIR/databases/dbc" \
      --out-dir "$ROOT_DIR/logs/monitor" \
      --include-signals 1; then
    :
  else
    echo "WARNING: DBC catalog pre-import failed (non-fatal). You can import later from UI /dbc_catalog." >&2
  fi
else
  echo "==> Skipping DBC catalog pre-import (--no-import-dbc-catalog)"
fi

if [[ $DO_SYSTEMD -eq 1 ]]; then
  echo "==> Installing systemd service (kvbm.service)"
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  if ! run_root_bash "$ROOT_DIR/install/install_autostart_systemd.sh"; then
    echo "ERROR: systemd service install failed." >&2
    FAIL=1
  fi

  echo "==> Enabling power controls (reboot/shutdown)"
  if [[ -x "$ROOT_DIR/install/setup_power_controls_polkit.sh" ]]; then
    run_root_bash "$ROOT_DIR/install/setup_power_controls_polkit.sh" "${SUDO_USER:-$(whoami)}" || {
      echo "WARNING: power controls polkit setup failed (buttons may not work)." >&2
    }
  fi

  # Legacy fallback (may not work under strict CapabilityBoundingSet).
  if [[ -x "$ROOT_DIR/install/setup_power_controls_sudoers.sh" ]]; then
    run_root_bash "$ROOT_DIR/install/setup_power_controls_sudoers.sh" "${SUDO_USER:-$(whoami)}" || true
  fi

  echo "==> Fixing permissions for user uploads/saves"
  if [[ -x "$ROOT_DIR/install/fix_data_permissions.sh" ]]; then
    run_root_bash "$ROOT_DIR/install/fix_data_permissions.sh" "${SUDO_USER:-$(whoami)}" || true
  fi

  echo "==> Installing display autostart"
  if [[ -x "$ROOT_DIR/install/setup_display_autostart.sh" ]]; then
    run_root_bash "$ROOT_DIR/install/setup_display_autostart.sh" || {
      echo "WARNING: display autostart setup failed (browser kiosk will not start automatically)." >&2
    }
  fi

  echo "==> Installing sysctl drop-in for eth0 IPv6 link-local"
  echo 'net.ipv6.conf.eth0.addr_gen_mode = 0' | run_root_bash tee /etc/sysctl.d/99-eth0-ipv6-ll.conf >/dev/null || true
else
  echo "==> Skipping systemd service (--no-systemd)"
fi

if [[ $DO_COPILOT -eq 1 ]]; then
  echo "==> Setting up local Copilot (Ollama)"
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  if bash "$ROOT_DIR/install/setup_ollama_pi5.sh" "$COPILOT_MODEL" "$COPILOT_BASE_URL"; then
    :
  else
    echo "ERROR: Copilot/Ollama setup failed. Retry: ./install/setup_ollama_pi5.sh '${COPILOT_MODEL}' '${COPILOT_BASE_URL}'" >&2
    FAIL=1
  fi
else
  echo "==> Copilot/Ollama not requested (use --with-copilot)"
fi

if [[ $DO_HEALTHCHECK -eq 1 ]]; then
  echo "==> Installing kvbm healthcheck timer"
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  if run_root_bash "$ROOT_DIR/install/install_kvbm_healthcheck_systemd.sh"; then
    :
  else
    echo "ERROR: healthcheck timer install failed." >&2
    FAIL=1
  fi
else
  echo "==> Skipping healthcheck (--no-healthcheck)"
fi

if [[ $DO_HW_WATCHDOG -eq 1 ]]; then
  echo "==> Enabling Raspberry HW watchdog (requires reboot)"
  if [[ $EUID -ne 0 ]]; then
    sudo -v
  fi
  if [[ -x "$ROOT_DIR/install/enable_hw_watchdog_rpi.sh" ]]; then
    sudo "$ROOT_DIR/install/enable_hw_watchdog_rpi.sh"
    echo "HW watchdog enabled. Reboot recommended: sudo reboot"
  else
    echo "WARNING: enable_hw_watchdog_rpi.sh missing." >&2
  fi
fi

echo
echo "==> Done"
echo "- Start manually (without systemd): ./install/run_kvaser_bus_manager.sh"
echo "- If systemd installed: systemctl status kvbm.service --no-pager"
echo "- Logs: journalctl -u kvbm.service -f"
echo "- Display page: http://<raspberry-ip>:5000/display"
echo "- UI: http://<raspberry-ip>:5000"

if [[ $DO_DRIVERS -eq 1 ]]; then
  echo
  echo "Note: If you just installed drivers, a reboot may be required." 
fi

if [[ $DO_SYSTEMD -eq 1 ]]; then
  if ! systemctl is-enabled kvbm.service >/dev/null 2>&1; then
    echo "ERROR: kvbm.service is not enabled." >&2
    FAIL=1
  fi
  if ! systemctl is-active kvbm.service >/dev/null 2>&1; then
    echo "ERROR: kvbm.service is not active." >&2
    FAIL=1
  fi
fi

if [[ $DO_HEALTHCHECK -eq 1 ]]; then
  if ! systemctl is-enabled kvbm-healthcheck.timer >/dev/null 2>&1; then
    echo "ERROR: kvbm-healthcheck.timer is not enabled." >&2
    FAIL=1
  fi
fi

if [[ $FAIL -ne 0 ]]; then
  echo
  echo "==> Install completed with ERRORS (see messages above)." >&2
  exit 1
fi
