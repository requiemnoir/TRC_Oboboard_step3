#!/usr/bin/env bash
# install_slave_turnkey.sh — provisiona un Pi 5 4 GB come SLAVE COMPLETO.
#
# Eseguito UNA VOLTA su un Pi nuovo dopo `git clone -b slave ... && cd repo`,
# porta il sistema da zero a stato funzionante.
#
# Fasi:
#   1. dipendenze apt
#   2. git lfs pull (DBC/ARXML/FIBEX/PDX → per consistency, anche se slave non
#      decoda nessuno; servono solo se vuoi runtime decode opzionale)
#   3. venv Python + deps minimali (mirror_logger + slave_daemon)
#   4. Genera /etc/trc-node-token (16 byte hex) se assente
#   5. netplan eth0 statico (default 192.168.50.20/24)
#   6. trc-slave.service installato (CPUAffinity=2,3, Nice=-5)
#   7. /boot/firmware/cmdline.txt: isolcpus=2,3 (pin del daemon)
#   8. (opzionale) USB serial gadget per console di emergenza
#   9. avvia il daemon
#
# Uso:
#   sudo bash slave_node/install/install_slave_turnkey.sh
#
# Override:
#   STATIC_IP=192.168.50.20
#   ENABLE_ISOLCPUS=1                   # richiede reboot
#   ENABLE_USB_CONSOLE=1                # richiede reboot

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then echo "ERROR: usa sudo" >&2; exit 1; fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="${TRC_USER:-${SUDO_USER:-boss}}"
STATIC_IP="${STATIC_IP:-192.168.50.20}"
ETH_IFACE="${ETH_IFACE:-eth0}"
ENABLE_ISOLCPUS="${ENABLE_ISOLCPUS:-1}"
ENABLE_USB_CONSOLE="${ENABLE_USB_CONSOLE:-0}"

log() { printf "\n=== [%s] %s ===\n" "$(date +%H:%M:%S)" "$*"; }

# ───────────────────────────────────────── 1. apt
log "Phase 1: apt dependencies (minimal — slave = capture only)"
DEBIAN_FRONTEND=noninteractive apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  git git-lfs python3-venv python3-pip python3-dev build-essential \
  iproute2 net-tools tcpdump curl ca-certificates \
  ethtool

# ───────────────────────────────────────── 2. lfs pull
log "Phase 2: git lfs pull (databases — anche slave li sincronizza per consistency)"
sudo -u "${RUN_USER}" git -C "${REPO_DIR}" lfs install
sudo -u "${RUN_USER}" git -C "${REPO_DIR}" lfs pull

# ───────────────────────────────────────── 3. venv
log "Phase 3: Python venv + slave-only deps"
sudo -u "${RUN_USER}" python3 -m venv "${REPO_DIR}/.venv"
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet \
  Flask==3.0.0 'Flask-SocketIO>=5.3,<6' simple-websocket \
  'numpy>=2,<3' asammdf cantools python-can

# ───────────────────────────────────────── 4. token
log "Phase 4: bearer token at /etc/trc-node-token"
if [[ ! -f /etc/trc-node-token ]]; then
  python3 -c "import secrets; print(secrets.token_hex(16))" > /etc/trc-node-token
  chmod 0640 /etc/trc-node-token
  chown root:"${RUN_USER}" /etc/trc-node-token
  echo "  new token generated"
else
  echo "  token already exists — left untouched"
fi

# ───────────────────────────────────────── 5. netplan
log "Phase 5: netplan static IP ${STATIC_IP}/${ETH_IFACE}"
if [[ -d /etc/netplan ]]; then
  cat > /etc/netplan/90-trc-slave.yaml <<EOF
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    ${ETH_IFACE}:
      addresses:
        - ${STATIC_IP}/24
      dhcp4: false
      dhcp6: false
      optional: true
EOF
  chmod 0600 /etc/netplan/90-trc-slave.yaml
fi

# Tune SO_RCVBUF for kernel UDP at high rate (no app drops in vehicle worst case)
cat > /etc/sysctl.d/90-trc-slave.conf <<EOF
# UDP rcvbuf for mirror traffic (target ~80 Mbps sustained)
net.core.rmem_max = 67108864
net.core.rmem_default = 16777216
net.core.netdev_max_backlog = 5000
EOF
sysctl -p /etc/sysctl.d/90-trc-slave.conf 2>&1 | head

# ───────────────────────────────────────── 6. systemd
log "Phase 6: trc-slave.service"
TMP_UNIT=$(mktemp)
sed -e "s|__REPO_DIR__|${REPO_DIR}|g" "${REPO_DIR}/slave_node/install/trc-slave.service" > "${TMP_UNIT}"
sed -i "s|^User=.*|User=${RUN_USER}|" "${TMP_UNIT}"
sed -i "s|^Group=.*|Group=${RUN_USER}|" "${TMP_UNIT}"
sed -i "s|-o boss -g boss|-o ${RUN_USER} -g ${RUN_USER}|g" "${TMP_UNIT}"
install -m 0644 "${TMP_UNIT}" /etc/systemd/system/trc-slave.service
rm -f "${TMP_UNIT}"

cat > /etc/default/trc-slave <<EOF
# overrides per trc-slave.service
# TRC_SLAVE_MF4_DIR=/mnt/nvme/mf4
# TRC_SLAVE_BIND=0.0.0.0
# TRC_SLAVE_API_PORT=8001
EOF
chmod 0644 /etc/default/trc-slave

# ───────────────────────────────────────── 7. isolcpus
if [[ "${ENABLE_ISOLCPUS}" == "1" ]]; then
  log "Phase 7: isolcpus=2,3 in /boot/firmware/cmdline.txt"
  CMDL=/boot/firmware/cmdline.txt
  if [[ -f "${CMDL}" ]] && ! grep -q "isolcpus" "${CMDL}"; then
    cp -p "${CMDL}" "${CMDL}.bak.$(date +%s)"
    sed -i 's| *$| isolcpus=2,3|' "${CMDL}"
    echo "  cmdline.txt patched — reboot required to activate isolcpus"
  else
    echo "  isolcpus already in cmdline.txt or cmdline file missing"
  fi
fi

# ───────────────────────────────────────── 8. USB serial console (opzionale)
if [[ "${ENABLE_USB_CONSOLE}" == "1" ]]; then
  log "Phase 8: USB serial gadget (debug fallback)"
  bash "${REPO_DIR}/install/enable_usb_serial_gadget.sh" || true
fi

# ───────────────────────────────────────── 9. start
log "Phase 9: enable + start trc-slave"
systemctl daemon-reload
systemctl enable trc-slave.service
systemctl restart trc-slave.service
sleep 2
systemctl --no-pager --full status trc-slave.service | head -15 || true

cat <<EOF

╔════════════════════════════════════════════════════════════════════╗
║  TRC SLAVE turn-key install COMPLETE                               ║
╠════════════════════════════════════════════════════════════════════╣
║  API:        http://${STATIC_IP}:8001/                             ║
║  Token:      /etc/trc-node-token (copia sul master!)               ║
║  MF4 dir:    /var/lib/trc-slave/mf4/                               ║
║  isolcpus:   ${ENABLE_ISOLCPUS} → reboot per attivare              ║
║                                                                    ║
║  Sul master Pi:                                                    ║
║    1. scp boss@${STATIC_IP}:/etc/trc-node-token /etc/trc-node-token
║    2. sudo systemctl restart trc-master                            ║
║                                                                    ║
║  Logs:                                                             ║
║    journalctl -u trc-slave -f                                      ║
║                                                                    ║
║  Verifica da master:                                               ║
║    curl http://${STATIC_IP}:8001/api/health                        ║
║    curl http://master:5000/slave-node/                             ║
╚════════════════════════════════════════════════════════════════════╝
EOF
