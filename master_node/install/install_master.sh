#!/usr/bin/env bash
# install_master.sh — provisiona un Pi 5 16 GB come MASTER.
#
# Esegue:
#   1. apt deps (python venv, network)
#   2. crea venv .venv per kvaser_bus_manager + node_protocol deps
#   3. configura netplan eth0 statico 192.168.50.10/24
#   4. installa systemd unit trc-master.service (lancia il backend KBM esistente)
#   5. attende che il token sia presente in /etc/trc-node-token
#      (genera placeholder se non esiste; va sincronizzato col slave!)
#
# Idempotente.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: esegui con sudo" >&2; exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="${TRC_USER:-${SUDO_USER:-boss}}"
STATIC_IP="${STATIC_IP:-192.168.50.10}"
SLAVE_IP="${SLAVE_IP:-192.168.50.20}"
ETH_IFACE="${ETH_IFACE:-eth0}"
NETPLAN_FILE="${NETPLAN_FILE:-/etc/netplan/90-trc-master.yaml}"
TOKEN_FILE="${TOKEN_FILE:-/etc/trc-node-token}"
UNIT_DST="/etc/systemd/system/trc-master.service"

echo "[install_master] repo=${REPO_DIR} user=${RUN_USER} static_ip=${STATIC_IP} slave_ip=${SLAVE_IP}"

# 1. apt
echo "[install_master] apt deps..."
DEBIAN_FRONTEND=noninteractive apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3-venv python3-pip python3-dev build-essential \
  iproute2 net-tools curl ca-certificates jq

# 2. venv
echo "[install_master] creating venv → ${REPO_DIR}/.venv"
sudo -u "${RUN_USER}" python3 -m venv "${REPO_DIR}/.venv" 2>/dev/null || true
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "${RUN_USER}" "${REPO_DIR}/.venv/bin/pip" install --quiet \
  Flask==3.0.0 'Flask-SocketIO>=5.3,<6' simple-websocket \
  gunicorn 'gevent>=23,<25' gevent-websocket \
  'numpy>=2,<3' asammdf cantools python-can \
  'opencv-python-headless<5' scapy \
  'python-socketio[client]>=5.10' 'websocket-client'

# 3. netplan
if [[ -d /etc/netplan ]]; then
  cat > "${NETPLAN_FILE}" <<EOF
# Managed by install_master.sh — TRC Onboard MASTER on private LAN.
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
  chmod 0600 "${NETPLAN_FILE}"
  echo "[install_master] wrote ${NETPLAN_FILE}"
fi

# 4. token check
if [[ ! -f "${TOKEN_FILE}" ]]; then
  echo "[install_master] WARNING: ${TOKEN_FILE} not found."
  echo "                 Copia il token dal slave: scp boss@${SLAVE_IP}:${TOKEN_FILE} ${TOKEN_FILE}"
  echo "                 Per ora genero un placeholder NON valido (le chiamate al slave"
  echo "                 falliranno con 401 finché non lo sostituisci)."
  python3 -c "import secrets; print(secrets.token_hex(16))" > "${TOKEN_FILE}.placeholder"
  chmod 0640 "${TOKEN_FILE}.placeholder"
fi

# /etc/default for slave IP override
cat > /etc/default/trc-master <<EOF
# Overrides per trc-master.service
TRC_SLAVE_IP=${SLAVE_IP}
TRC_SLAVE_API_PORT=8001
TRC_SLAVE_TOKEN_FILE=${TOKEN_FILE}
EOF
chmod 0644 /etc/default/trc-master

# 5. systemd unit
TMP_UNIT=$(mktemp)
sed -e "s|__REPO_DIR__|${REPO_DIR//|/\\|}|g" \
    -e "s|__RUN_USER__|${RUN_USER}|g" \
    "${REPO_DIR}/master_node/install/trc-master.service" > "${TMP_UNIT}"
install -m 0644 "${TMP_UNIT}" "${UNIT_DST}"
rm -f "${TMP_UNIT}"
echo "[install_master] systemd unit installed → ${UNIT_DST}"

systemctl daemon-reload
systemctl enable trc-master.service
systemctl restart trc-master.service
sleep 1
systemctl --no-pager --full status trc-master.service || true

cat <<EOF

[install_master] DONE.

  service:   ${UNIT_DST}
  token:     ${TOKEN_FILE}        # sincronizza dal slave se non esiste
  ui:        http://${STATIC_IP}:5000/
  slave panel: http://${STATIC_IP}:5000/slave-node/

Per sincronizzare il token col slave:
  scp boss@${SLAVE_IP}:${TOKEN_FILE} ${TOKEN_FILE}.tmp
  sudo install -m 0640 -o root -g ${RUN_USER} ${TOKEN_FILE}.tmp ${TOKEN_FILE}
  sudo systemctl restart trc-master.service

Healthcheck slave:
  curl -H "Authorization: Bearer \$(cat ${TOKEN_FILE})" http://${SLAVE_IP}:8001/api/health | jq .

EOF
