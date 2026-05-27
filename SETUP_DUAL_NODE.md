# TRC Onboard — Dual-Node Setup (Master + Slave Pi 5)

**Stato:** branch `master` testato end-to-end in dual-VM Lima/Ubuntu 24.04 ARM64
**Topology:** un Pi 5 16 GB (master) + un Pi 5 4 GB (slave) collegati via switch 5-port gigabit
**Comunicazione:** REST HTTP :8001 + Socket.IO :8001/slave (stesso porto, namespace separato)
**Auth:** Bearer token statico in `/etc/trc-node-token` su entrambi i nodi

---

## 1. Architettura

```
[Gateway vettura] ─── Eth ─┬─ TP-Link TL-SG105 switch ─┬─ MASTER Pi 5 16GB (192.168.50.10:5000)
                          │                            │   ↳ UI / Sentinel / Voice / Copilot
                          │                            │   ↳ /slave-node/ panel proxy
                          │                            │
                          │                            └─ SLAVE Pi 5 4GB (192.168.50.20:8001)
                          │                                ↳ mirror_logger (UDP :30490)
                          │                                ↳ raw_logger → MF4
                          │                                ↳ doip_activator (gateway DID 0xF1A0)
                          │
                          └─ (optional: laptop service WiFi/Eth)

Communication:
  master  ──HTTP REST :8001──→  slave  (poll status, exec cmd, mf4 download, control)
  master  ←─Socket.IO /slave─── slave  (push: log lines, frame events, snapshot ack)
  master  ──ssh/scp/rsync :22─→  slave  (file sync, manual debug)
  master  ←──USB-C serial gadget /dev/ttyACMx─→  laptop  (emergency console)
```

Lo stesso layout funziona su **un singolo Pi 5** (modo legacy) lasciando `TRC_NODE_ROLE` non impostato — `master_node` non viene attivato e il backend funziona come prima.

---

## 2. Branch GitHub

- **`main`** — codice base, mono-nodo, legacy
- **`master`** — full repo + `master_node/` + `slave_node/` (entrambi presenti, ruolo deciso a runtime via `TRC_NODE_ROLE` env)
- **`slave`** — lean slave (sottoinsieme di `master`, ottimizzato per capture-only)

Per deploy:
- Pi master: `git checkout master`
- Pi slave: `git checkout slave`

Entrambi installano da `master_node/install/` o `slave_node/install/` rispettivamente.

---

## 3. Bring-up procedura

### Fase 0 — Hardware

| Componente | Quantità | Note |
|------------|---------|------|
| Raspberry Pi 5 16 GB | 1 (master) | UI/AI |
| Raspberry Pi 5 4 GB | 1 (slave) | capture only |
| microSD industrial 32+ GB | 2 | SanDisk Industrial o Samsung Pro Endurance |
| NVMe 256-500 GB | 2 | logging |
| Pi 5 M.2 HAT+ ufficiale | 2 | |
| Argon ONE V5 case | 2 | |
| TP-Link TL-SG105 | 1 | switch 5-port gigabit, alimentato 5V |
| Cavi Ethernet Cat 6 30cm | 4 | per evitare loop nel baule |
| PSU automotive 12 V → 5 V dual 5A | 1 | con ignition-aware shutdown |
| Cavo USB-C M-M | 1 | per console di emergenza |

### Fase 1 — Slave Pi (capture node)

```bash
# 1.1 boot Pi 5 (Pi OS 64-bit Lite, no GUI)
# 1.2 connetti via SSH dal laptop tramite switch
ssh boss@192.168.50.20      # se DHCP, scopri IP via switch

# 1.3 clone repo, slave branch
git clone -b slave https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
git lfs install && git lfs pull

# 1.4 install
sudo bash slave_node/install/install_slave.sh
# Output:
#   - crea /etc/trc-node-token
#   - configura netplan eth0 → 192.168.50.20/24
#   - installa trc-slave.service
#   - avvia il daemon
#   - mostra la URL: http://192.168.50.20:8001/

# 1.5 (opzionale) USB serial console di emergenza
sudo bash install/enable_usb_serial_gadget.sh && sudo reboot
```

Verifica:
```bash
sudo systemctl status trc-slave
curl http://localhost:8001/api/health     # JSON 200
```

### Fase 2 — Master Pi

```bash
# 2.1 boot Pi 5 16GB
ssh boss@192.168.50.10

# 2.2 clone master branch
git clone -b master https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
git lfs install && git lfs pull

# 2.3 sincronizza il token DAL slave (chiave condivisa per auth)
scp boss@192.168.50.20:/etc/trc-node-token /tmp/slave-token
sudo install -m 0640 -o root -g boss /tmp/slave-token /etc/trc-node-token

# 2.4 install
sudo bash master_node/install/install_master.sh
# Configura netplan, systemd unit, env file.
# Backend KBM avviato con TRC_NODE_ROLE=master → blueprint slave-panel attivo.

# 2.5 (opzionale) USB serial console
sudo bash install/enable_usb_serial_gadget.sh && sudo reboot
```

Verifica:
```bash
sudo systemctl status trc-master
curl http://localhost:5000/api/live                    # backend OK
curl http://localhost:5000/slave-node/api/health       # proxy to slave OK
```

### Fase 3 — Test end-to-end dal browser

Da PC sulla rete (o da master Pi stesso via HDMI):

| URL | Cosa fa |
|-----|---------|
| `http://192.168.50.10:5000/` | UI principale master |
| `http://192.168.50.10:5000/slave-node/` | **Pannello dedicato slave** (status + cmd remoti + log live + MF4 download) |
| `http://192.168.50.20:8001/` | UI locale del slave (solo per debug diretto) |

Il pannello `/slave-node/` mostra:
- 4 card: Health / Throughput / Storage / Last error
- 3 pulsanti capture: START / STOP / SNAPSHOT
- Log stream live (refresh ogni 3s, filtrabile per livello)
- Console comandi remoti (allow-list — `uname`, `journalctl`, `df`, `ss`, ecc.)
- Lista MF4 sul slave, scaricabili via proxy con click

---

## 4. Protocollo wire (per implementatori)

| Endpoint | Metodo | Direzione | Scopo |
|----------|--------|-----------|-------|
| `/api/health` | GET | master → slave | hostname, uptime, branch, git sha, capture state |
| `/api/capture/status` | GET | master → slave | fps, drop, queue, disk free, ecc. |
| `/api/capture/start` | POST | master → slave | avvia mirror_logger |
| `/api/capture/stop` | POST | master → slave | termina + ritorna stats finali |
| `/api/capture/snapshot` | POST | master → slave | force_flush MF4 corrente |
| `/api/logs?lines=N&level=X` | GET | master → slave | ring buffer logs |
| `/api/cmd/exec` | POST | master → slave | comando in allow-list (debug) |
| `/api/mf4/list` | GET | master → slave | inventario MF4 |
| `/api/mf4/<name>` | GET | master → slave | scarica un singolo MF4 |
| `/metrics` | GET | qualsiasi | Prometheus-format counters/gauges |
| WS `/slave` namespace event `frame` | slave → master | push | frame decoded (downsampled 1/32) |
| WS `/slave` event `log` | slave → master | push | ogni log line live |
| WS `/slave` event `snapshot` | slave → master | push | ack force_flush |

Tutte le chiamate REST richiedono `Authorization: Bearer <token>` se `/etc/trc-node-token` è presente (auto-generato dall'installer slave). In dev mode (token assente) l'API è aperta — bene solo per laboratorio.

---

## 5. Failure modes & recovery

| Scenario | Cosa succede | Azione operatore |
|----------|--------------|------------------|
| Master crasha / reboot | Slave continua a catturare. MF4 su NVMe slave salvi. | Reboot master, riconnessione automatica. |
| Slave crasha | Master mostra "slave offline" nel pannello. UI/Sentinel restano. Mirror UDP perso. | `sudo systemctl restart trc-slave` |
| Network down (switch off) | Sia master che slave continuano in autonomia. Master non sa più cosa fa slave. | Riparti switch, riconnessione auto. |
| Token disallineato | API risponde 401. | Re-sync: `scp boss@slave:/etc/trc-node-token /etc/...` |
| Gateway non risponde DID | Capture parte ma 0 fps. Vedi `udp_packets_rx_per_s` nel pannello = 0. | Riconfigura `gateway_mirror.can` in app_config.json del slave; verifica IP gateway. |
| Disco slave pieno | `raw_logger` segnala `last_error`. Master vede badge giallo. | Pulisci `/var/lib/trc-slave/mf4/` o monta NVMe esterno. |
| Ethernet rotto sul master | Slave non raggiungibile. | Console USB-C: `screen /dev/cu.usbmodem* 115200` per debug. |

---

## 6. Comandi quotidiani

```bash
# stato slave da remoto
curl -H "Authorization: Bearer $(cat /etc/trc-node-token)" \
     http://192.168.50.20:8001/api/capture/status | jq .

# riavvia slave da master
ssh boss@192.168.50.20 sudo systemctl restart trc-slave

# scarica gli ultimi MF4 sul master
rsync -avz boss@192.168.50.20:/var/lib/trc-slave/mf4/ /mnt/master-mf4/

# log live del slave da master
ssh boss@192.168.50.20 sudo journalctl -fu trc-slave

# console di emergenza (Eth giù)
# laptop: screen /dev/cu.usbmodemXXXX 115200   (macOS)
#         minicom -D /dev/ttyACM0 -b 115200    (Linux)
```

---

## 7. Test eseguiti in sviluppo

### 7.1 Smoke test locale (Mac M4 venv)
```
slave_daemon launched on 127.0.0.1:18001
SlaveClient → health/status/start/stop/exec_cmd/logs/mf4_list: ALL OK
```

### 7.2 Dual-VM Lima (Ubuntu 24.04 ARM64 × 2)
```
trc-master VM (192.168.5.15 + host.lima.internal bridge)
trc-slave  VM (192.168.5.15 + portforward 18001 → host)

master VM SlaveClient → host.lima.internal:18001 (= slave VM):
  ✓ health: hostname=lima-trc-slave   ← conferma: comando esegue sul slave
  ✓ start capture (session created)
  ✓ exec_cmd "uname -a" → "Linux lima-trc-slave ... aarch64"
  ✓ exec_cmd "hostname; uptime" → output dal slave
  ✓ list mf4
  ✓ stream logs
  ✓ stop capture
```

### 7.3 Mirror parser
Unit test del `mirror_parser.py` continuano a passare (4/4) — il protocollo wraps mirror_logger, non lo cambia.

---

## 8. File map (branch master)

```
TRC_Onboard/
├── node_protocol/                 # SHARED — entrambi i ruoli
│   ├── api.py                     #   wire schemas (dataclasses)
│   └── auth.py                    #   bearer token helpers
│
├── slave_node/                    # SOLO slave
│   ├── daemon.py                  #   Flask + SocketIO + mirror_logger wrapper
│   ├── templates/status.html      #   UI minimale locale http://slave:8001/
│   ├── static/css/slave.css       #   dark theme
│   ├── static/js/slave.js         #   vanilla JS
│   └── install/
│       ├── install_slave.sh       #   one-shot installer
│       └── trc-slave.service      #   systemd unit
│
├── master_node/                   # SOLO master
│   ├── slave_client.py            #   HTTP client (urllib stdlib)
│   ├── slave_subscriber.py        #   SocketIO subscriber per push events
│   ├── blueprint.py               #   Flask blueprint /slave-node/*
│   ├── templates/slave_panel.html #   UI panel (arancio = master)
│   ├── static/css/slave_panel.css
│   ├── static/js/slave_panel.js
│   └── install/
│       ├── install_master.sh
│       └── trc-master.service
│
├── install/
│   ├── enable_usb_serial_gadget.sh   # USB-C console fallback
│   └── systemd/trc-usb-console.service
│
├── kvaser_bus_manager/            # backend KBM esistente (master only)
├── mirror_logger/                 # usato dal slave_node.daemon
├── docs/, databases/, ...         # invariati
```

---

## 9. Roadmap

- [ ] systemd-firstboot trigger per auto-sync token al primo boot del master
- [ ] mDNS broadcast (avahi) `trc-slave.local` per scoprire IP senza DHCP fisso
- [ ] WireGuard tunnel master↔slave per ulteriore isolamento
- [ ] Hailo-8 integration sul master per acceleration LLM/Whisper
- [ ] CM5 industrial swap del slave per deploy permanente in vettura

---

## 10. Cosa ho testato io vs cosa va validato in vettura

**Testato sul Mac (dual-VM Lima Ubuntu ARM64):**
- ✅ Protocollo wire (tutti gli endpoint REST)
- ✅ SocketIO events
- ✅ Allow-list comandi remoti
- ✅ Master ↔ Slave handshake
- ✅ Capture lifecycle (start/stop/snapshot via API)
- ✅ Slave UI standalone + Master UI panel
- ✅ Code reuse mirror_logger (parser + raw_logger smoke 0% drop)

**Da validare in vettura sul Pi reale:**
- ⚠️ DoIP activation completa con gateway Lambo reale
- ⚠️ UDP throughput sostenuto a 80 Mbps con 2 FlexRay + 8 CAN
- ⚠️ NVMe write sostenuto (MF4 logging)
- ⚠️ EMC + range temperature (cofano estivo)
- ⚠️ USB serial gadget su Pi 5 (config.txt patch)
- ⚠️ trc-power.service ignition-aware shutdown
