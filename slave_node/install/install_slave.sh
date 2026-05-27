#!/usr/bin/env bash
# install_slave.sh — provisiona un Pi 5 come SLAVE (dedicated capture node).
#
# Esegue:
#   1. apt deps (python venv, network tools, fcntl-friendly libs)
#   2. crea venv .venv con tutte le deps di mirror_logger + Flask + SocketIO
#   3. genera /etc/trc-node-token (16 byte hex)
#   4. configura netplan: eth0 statico 192.168.50.20/24 (override via env STATIC_IP)
#   5. installa systemd unit trc-slave.service
#   6. avvia il servizio
#
# Idempotente. Rilancialo per riconfigurazione.
#
# Uso:
#   sudo bash slave_node/install/install_slave.sh
#   sudo STATIC_IP=192.168.50.30 bash slave_node/install/install_slave.sh
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: esegui con sudo" >&2; exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="${TRC_USER:-${SUDO_USER:-boss}}"
STATIC_IP="${STATIC_IP:-192.168.50.20}"
SUBNET="${SUBNET:-192.168.50.0/24}"
NETPLAN_FILE="${NETPLAN_FILE:-/etc/netplan/90-trc-slave.yaml}"
TOKEN_FILE="${TOKEN_FILE:-/etc/trc-node-token}"
UNIT_DST="/etc/systemd/system/trc-slave.service"

echo "[install_slave] repo=${REPO_DIR} user=${RUN_USER} static_ip=${STATIC_IP}"

# 1. apt
echo "[install_slave] apt deps..."
DEBIAN_FRONTEND=noninteractive apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3-venv python3-pip python3-dev build-essential \
  iproute2 net-tools tcpdump curl ca-certificates

# 2. venv
echo "[install_slave] creating venv → ${REPO_DIR}/.venv"
sudo -u "${RUN_USER}" python3 -m venv "${REPO_DIR}/.venv"
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet \
  Flask==3.0.0 'Flask-SocketIO>=5.3,<6' simple-websocket \
  'numpy>=2,<3' asammdf cantools python-can \
  'python-socketio[client]>=5.10' 'websocket-client'

# 3. token
if [[ ! -f "${TOKEN_FILE}" ]]; then
  python3 -c "import secrets; print(secrets.token_hex(16))" > "${TOKEN_FILE}"
  chmod 0640 "${TOKEN_FILE}"
  chown root:"${RUN_USER}" "${TOKEN_FILE}"
  echo "[install_slave] new token written → ${TOKEN_FILE}"
else
  echo "[install_slave] token already exists at ${TOKEN_FILE}"
fi

# 4. netplan — eth0 statico
ETH_IFACE="${ETH_IFACE:-eth0}"
if [[ -d /etc/netplan ]]; then
  cat > "${NETPLAN_FILE}" <<EOF
# Managed by install_slave.sh — TRC Onboard SLAVE static IP on private LAN.
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
      # Mirror traffic dest_ip must point at ${STATIC_IP}
      # Master expected at 192.168.50.10
EOF
  chmod 0600 "${NETPLAN_FILE}"
  echo "[install_slave] wrote ${NETPLAN_FILE} (apply manually after boot test: sudo netplan apply)"
fi

# 5. systemd unit
TMP_UNIT=$(mktemp)
sed -e "s|__REPO_DIR__|${REPO_DIR//|/\\|}|g" \
    "${REPO_DIR}/slave_node/install/trc-slave.service" > "${TMP_UNIT}"
install -m 0644 "${TMP_UNIT}" "${UNIT_DST}"
rm -f "${TMP_UNIT}"
echo "[install_slave] systemd unit installed → ${UNIT_DST}"

# default env file (optional overrides)
if [[ ! -f /etc/default/trc-slave ]]; then
  cat > /etc/default/trc-slave <<EOF
# Override env per trc-slave.service (es. mover MF4 dir su NVMe esterno).
# TRC_SLAVE_MF4_DIR=/mnt/nvme/mf4
# TRC_SLAVE_BIND=0.0.0.0
# TRC_SLAVE_API_PORT=8001
EOF
  chmod 0644 /etc/default/trc-slave
fi

# 6. enable + start
systemctl daemon-reload
systemctl enable trc-slave.service
systemctl restart trc-slave.service
sleep 1
systemctl --no-pager --full status trc-slave.service || true

cat <<EOF

[install_slave] DONE.

  service:   ${UNIT_DST}
  token:     ${TOKEN_FILE}        # copia su master a stesso path
  api:       http://${STATIC_IP}:8001/
  logs:      journalctl -u trc-slave -f
  mf4 dir:   /var/lib/trc-slave/mf4/

Verifica da master Pi:
  curl -H "Authorization: Bearer \$(cat ${TOKEN_FILE})" \\
       http://${STATIC_IP}:8001/api/health | jq .

EOF
