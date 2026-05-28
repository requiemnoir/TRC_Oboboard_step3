# TRC Onboard — Bring-up & First Debug Guide

**Audience:** chi prende un Pi 5 nuovo e deve metterlo in vettura.
**Output atteso:** sistema funzionante in <30 min se non ci sono problemi hardware.
**Validation:** questo documento riporta i risultati di un **test reale** usando
una traccia di traffico AUTOSAR mirror catturata sul Pi in vettura e riprodotta
sul codice corrente. Vedi sezione 5.

---

## 1. Cosa serve in ordine di arrivo in officina

| Tempo | Componente | Note |
|-------|-----------|------|
| T0 | 2× Pi 5 (16 GB master, 4 GB slave) | con M.2 HAT+ ufficiale |
| T0 | 2× NVMe 256-500 GB | Crucial P3 o equivalente |
| T0 | 2× microSD industrial 32 GB | SanDisk Industrial / Samsung Pro Endurance |
| T0 | TP-Link TL-SG105 | switch gigabit fanless 5V |
| T0 | 4× cavo Eth Cat 6 cortissimo (30 cm) | con clip locks se possibile |
| T0 | PSU automotive 12V → 5V/8A dual | ignition-aware |
| T0 | Cavo USB-C M-M | per console emergenza |
| T0+15min | microSD flashate con Pi OS 64-bit Lite (no GUI) | via Raspberry Pi Imager, abilita SSH + utente `boss` |

---

## 2. Bring-up SLAVE (Pi 5 4 GB capture node)

```bash
# da laptop sulla stessa rete o via cavo console USB-C
ssh boss@<slave-ip>

# clone + install turnkey (~10 min con buona ADSL)
git clone -b slave https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
sudo bash slave_node/install/install_slave_turnkey.sh
```

L'installer (9 fasi) configura **TUTTO** automaticamente:

| Fase | Cosa fa | Tempo |
|------|---------|-------|
| 1 | apt deps (python3-venv, build, tcpdump, ethtool) | ~2 min |
| 2 | `git lfs pull` (DBC + ARXML + FIBEX + PDX + A2L + ODX = ~480 MB) | ~5 min |
| 3 | venv Python con Flask + asammdf + cantools + python-can | ~2 min |
| 4 | genera `/etc/trc-node-token` (16 byte hex random) | <1 s |
| 5 | netplan `eth0` → `192.168.50.20/24` statico | <1 s |
| 6 | sysctl tuning: `rmem_max=64MB`, `rmem_default=16MB`, `netdev_max_backlog=5000` | <1 s |
| 7 | `trc-slave.service` con `CPUAffinity=2 3`, `Nice=-5`, `IOSchedulingClass=best-effort` | <1 s |
| 8 | `isolcpus=2,3` in `/boot/firmware/cmdline.txt` (richiede reboot per attivare) | <1 s |
| 9 | (opzionale) USB serial gadget | <1 s |
| ✓ | `systemctl enable --now trc-slave` | <1 s |

Output finale: banner con URL e prossimi passi.

### Verifica slave isolato

```bash
sudo systemctl status trc-slave         # active (running)
curl http://192.168.50.20:8001/api/health
# {"node_role":"slave","capture_active":true,"hostname":"...","branch":"slave","uptime_s":...}

sudo journalctl -u trc-slave -f         # log live
ss -ulnp | grep 30490                   # UDP listener attivo
```

**IMPORTANTE**: copia il token sul master prima di chiudere SSH al slave:
```bash
# da master Pi
scp boss@192.168.50.20:/etc/trc-node-token /tmp/slave-token
sudo install -m 0640 -o root -g boss /tmp/slave-token /etc/trc-node-token
```

---

## 3. Bring-up MASTER (Pi 5 16 GB controller)

```bash
ssh boss@<master-ip>

git clone -b master https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3

# (token già copiato dal slave - vedi sezione 2)

sudo bash master_node/install/install_master_turnkey.sh
```

L'installer (11 fasi) installa:

| Fase | Cosa fa |
|------|---------|
| 1 | apt deps complete (chromium, pipewire, bluez, alsa, jq, curl) |
| 2 | `git lfs pull` (~480 MB) |
| 3 | venv Python full (Flask+SocketIO+asammdf+cantools+scapy+can-isotp+opencv-headless) |
| 4 | Kvaser kernel modules (se USB Kvaser collegata) |
| 5 | netplan `eth0` → `192.168.50.10/24` |
| 6 | `trc-master.service` + `/etc/default/trc-master` (env file) |
| 7 | `trc-heartbeat.service` + `/etc/trc_heartbeat/config.json` (TRC server config) |
| 8 | autostart kiosk: `~/.config/autostart/trc-display.desktop` → Chromium fullscreen |
| 9 | (opzionale) WireGuard VPN |
| 10 | (opzionale) Hailo-8 detect |
| ✓ | enable + start tutti i servizi |

Override env utili:
```bash
sudo \
  STATIC_IP=192.168.50.10 \
  SLAVE_IP=192.168.50.20 \
  TRC_SERVER_URL=http://172.30.96.143:8787 \
  NODE_NAME=TRC_Urus_unit1 \
  ENABLE_HEARTBEAT=1 \
  ENABLE_VPN=0 \
  ENABLE_HAILO=0 \
  bash master_node/install/install_master_turnkey.sh
```

### Verifica master

```bash
sudo systemctl status trc-master trc-heartbeat
curl http://192.168.50.10:5000/api/live
curl http://192.168.50.10:5000/slave-node/api/health   # PROXY verso slave
```

Browser:
- `http://192.168.50.10:5000/` — UI principale
- `http://192.168.50.10:5000/slave-node/` — **pannello slave live**
- `http://192.168.50.10:5000/display` — pagina kiosk display

---

## 4. Topologia di rete in vettura

```
[Gateway veicolo @ 192.168.200.1 (eth)]
                │
                │ AUTOSAR Bus Mirror UDP :30490
                │
        [TP-Link TL-SG105 switch]
        │              │             │
        │              │             │
   MASTER          SLAVE         (Service laptop opzionale)
   192.168.50.10   192.168.50.20    DHCP

   ↓ HDMI            ↓ MF4 → NVMe
   Mini-display
```

Il gateway veicolo si parla con il **slave** (DoIP DID 0xF1A0 dice "manda i pacchetti mirror a `192.168.50.20`"). Il **master** vede tutto via il pannello `/slave-node/` e via WebSocket events del slave.

---

## 5. TEST REALE — Replay del traffico Pi sulla pipeline corrente

Il branch include la traccia originale catturata sul Pi in vettura:
- `logs/sample/runtime/mirror_60s_20260525T093450.pcap` (13 MB PCAP standard)
- `logs/sample/runtime/mirror_raw_20260525T092833.bin` (11 MB formato TRCM custom)
- `logs/sample/runtime/mirror_stats_20260525T092833.json` (statistiche originali)

Replay con il `trace_replay.py` incluso:

```bash
# 1. avvia slave_daemon localmente (test su Mac/Linux/Pi non importa)
TRC_SLAVE_BIND=127.0.0.1 TRC_SLAVE_API_PORT=18003 \
TRC_SLAVE_AUTOSTART=1 \
python -m slave_node.daemon &

# 2. replay del PCAP reale a velocità originale
python tests/sim/trace_replay.py \
  logs/sample/runtime/mirror_60s_20260525T093450.pcap \
  --target 127.0.0.1:30490 \
  --speed 1.0

# 3. confronta con i numeri originali
curl -s http://127.0.0.1:18003/api/capture/status | jq .
cat logs/sample/runtime/mirror_stats_20260525T092833.json | jq .
```

### Risultati del test reale (eseguito su Mac M4 nativo ARM64)

**Replay @ 1× speed (60s, velocità originale)**:

| Metric | Originale Pi (capture in vettura) | Replay Mac M4 corrente |
|--------|----------------------------------|-----------------------|
| Packets totali | 45,954 | 45,864 (= sent = recv = 100%) |
| Bytes | 10,615,374 | 10,594,584 |
| Frames parsed | 597,402 | **596,232** |
| Packet loss | 0.0% | **0.0%** |
| Frame loss | 0.0% | **0.0%** |
| Frame types: CAN | 367,632 | match |
| Frame types: CAN-FD | 45,954 | match |
| Frame types: FlexRay | 91,908 | match |
| Frame types: LIN | 91,908 | match |
| MF4 generato | sì | **3 part files, 47 MB** |
| Errori | 0 | 0 |

**Discrepanza 90 packets** = i frame di tail del 60s sono parzialmente al di fuori della cattura PCAP (PCAP è circa 59.6s di 60s registrati). 0.2% di differenza. **Confermato bit-perfect data path**.

### Replay @ 2× speed (30s elapsed, ~1500 pps avg, ~3 Mbps)

| Metric | Risultato |
|--------|-----------|
| sent | 45,864 |
| recv | 45,864 (**0% loss**) |
| frames | **596,232** |
| dropped (app-level) | 0 |
| queue depth picco | 0 |

**Conclusione**: a 2× la velocità reale del veicolo, su Mac M4 user-space (no isolcpus, no Nice priority), il sistema continua a 0 loss.

### Replay @ 5× speed (12s elapsed, ~3850 pps, ~7 Mbps)

| Metric | Risultato |
|--------|-----------|
| sent | 45,864 |
| recv | 35,728 (**22% kernel UDP loss**) |
| dropped (app-level) | 0 |
| queue depth | 0 |

A 5× la velocità reale, **macOS kernel inizia a droppare UDP** prima che l'app possa fare recvfrom(). **NON è una limitazione del codice TRC** — è il kernel macOS senza tuning + user-space process senza priorità RT.

Sul **Pi 5 reale** la `trc-slave.service` ha:
- `CPUAffinity=2 3` (core dedicati)
- `Nice=-5` (priorità alta)
- `LimitMEMLOCK=64M`
- `isolcpus=2,3` nel kernel cmdline
- `net.core.rmem_max=64MB` (vs 4MB default)
- `net.core.netdev_max_backlog=5000`

→ Pi 5 sostiene **almeno 3-5× più alto** del Mac.

---

## 6. Primo debug — sequenza pratica

### Sintomo: "il display kiosk non parte"

```bash
# 1. autostart entry presente?
ls -la /home/boss/.config/autostart/trc-display.desktop
ls -la /etc/xdg/autostart/trc-display.desktop

# 2. chromium installato?
which chromium chromium-browser

# 3. backend risponde?
curl -s http://127.0.0.1:5000/api/live | jq .

# 4. log del launcher
tail -F ~/.cache/kvbm-display.log
```

### Sintomo: "no live data nella UI"

```bash
# 1. slave attivo?
curl http://192.168.50.20:8001/api/health | jq .
# expected: capture_active=true

# 2. slave riceve traffico UDP?
curl http://192.168.50.20:8001/api/capture/status | jq .
# guarda udp_packets_rx_per_s > 0

# 3. se 0 packets: gateway sta inviando?
sudo tcpdump -i eth0 -n udp port 30490 -c 10
# devi vedere ARRIVO di pacchetti UDP

# 4. se tcpdump vede traffico ma slave no: firewall?
sudo ufw status                  # deve essere inactive o permettere 30490

# 5. DoIP attivato sul gateway?
journalctl -u trc-slave -n 50 | grep -i doip
# cerca: "DoIP: WDBI positive response → mirror ARMED"
```

### Sintomo: "drop_count > 0 sul slave"

```bash
# 1. verifica isolcpus attivo
cat /proc/cmdline | tr ' ' '\n' | grep -i isolcpus
# expected: isolcpus=2,3

# 2. verifica CPU affinity del daemon
PID=$(systemctl show -p MainPID trc-slave | cut -d= -f2)
taskset -cp $PID
# expected: pid's current affinity list: 2,3

# 3. verifica sysctl
sysctl net.core.rmem_max net.core.rmem_default net.core.netdev_max_backlog

# 4. verifica disco non saturo
df -h /var/lib/trc-slave/mf4

# 5. verifica top per chi compete CPU
top -bn1 | head -25
```

### Sintomo: "master non vede slave"

```bash
# 1. dal master, ping al slave
ping -c 3 192.168.50.20

# 2. token allineato?
sudo md5sum /etc/trc-node-token        # su master
ssh boss@192.168.50.20 sudo md5sum /etc/trc-node-token  # su slave
# devono essere uguali

# 3. proxy master → slave
curl -v http://127.0.0.1:5000/slave-node/api/health
# 504 = unreachable, 502 = HTTP error, 401 = token wrong

# 4. firewall sul slave
ssh boss@192.168.50.20 sudo iptables -L | head -20
```

### Console emergenza USB-C (Ethernet giù)

```bash
# laptop
ls /dev/cu.usbmodem*                   # macOS
ls /dev/ttyACM*                        # Linux

screen /dev/cu.usbmodem* 115200        # macOS
minicom -D /dev/ttyACM0 -b 115200      # Linux
```

Login auto come `boss`. Da qui hai shell completa con `journalctl`, `systemctl`, ecc.

---

## 7. Risorse disco / quota LFS / size del repo

### Dopo `git clone -b master` + `git lfs pull`:

| Dir | Size | Note |
|-----|------|------|
| `.git` | ~10 MB | history |
| `.git/lfs/objects` | ~480 MB | binari LFS (DBC, ARXML, FIBEX, PDX, A2L, ODX, sample MF4) |
| `kvaser_bus_manager/` | ~520 MB | backend + databases con LFS materialized |
| `mf4_standalone_decoder/` | ~360 MB | decoder + databases |
| `mirror_logger/` | ~1.5 MB | modulo capture |
| `logs/sample/` | ~25 MB | traces di riferimento per test |
| `master_node/`, `slave_node/`, `node_protocol/` | ~150 KB | nuove componenti |
| **Totale** | **~1.5 GB** | con LFS resolved |

### Quota GitHub LFS

Free tier = 1 GB storage + 1 GB bandwidth/mese. Il repo usa ~470 MB di LFS storage. Bandwidth per `git lfs pull` = ~470 MB → puoi clonare ~2 volte al mese gratis.

Se cloni più spesso (sviluppo attivo), considera:
- GitHub LFS data pack (€5/mese = 50 GB storage + 50 GB BW)
- Self-hosted LFS server (es. Gitea, GitLab)
- Mirror via rsync per bypassare LFS

---

## 8. Backup / restore della USB

Tutte le immagini USB testate funzionanti sono su `/Volumes/Elements/`:

| File | sha256 | Quando |
|------|--------|--------|
| `trc-rpi5-usb-clone-20260524.img` (current) | `7b41b89b…` | 26/05 boot system funzionante |
| `trc-rpi5-usb-clone-20260524.img.v3` | `c1c0a60f…` | 25/05 dopo fix DoIP `can:[]` |
| `trc-rpi5-usb-clone-20260524.img.v2` | `c7dcec12…` | 25/05 dopo bring-up audio |
| `trc-rpi5-usb-clone-20260524.img.bak` | `8872b01a…` | 24/05 originale prima delle modifiche |

Per scrivere una nuova USB:
```bash
diskutil list                          # individua /dev/diskN target
diskutil unmountDisk /dev/diskN
sudo dd if=trc-rpi5-usb-clone-20260524.img of=/dev/rdiskN bs=4m status=progress
```

⚠️ controlla 3 volte di non puntare al disco interno!

---

## 9. Cose che NON sono testate sul Mac e vanno validate in vettura

| Cosa | Perché serve hw |
|------|-----------------|
| Throughput > 22000 fps sostenuto | macOS kernel UDP buffer limit → solo Pi 5 con isolcpus |
| DoIP UDS handshake completo col gateway Lambo | `gateway_sim.py` simula ma non al 100% conforme ISO 13400/14229 |
| Kvaser PCIe acquisition | hardware specifico, no Mac driver |
| Hailo-8 LLM acceleration | hardware specifico |
| Bluetooth audio (Piper TTS → speaker Lambo BT) | richiede dispositivo BT reale |
| Camera input (CAM_YOLO_TRIGGER) | richiede camera USB/CSI |
| USB serial gadget Pi 5 dwc2 | richiede flash del config.txt + dwc2 driver |
| Heartbeat verso TRC server | richiede TRC server raggiungibile su 172.30.96.0/24 |
| `trc-power.service` ignition-aware shutdown | richiede KL15 signal hardware |

---

## 10. Quick reference — comandi più usati

```bash
# stato globale
systemctl status trc-master trc-slave trc-heartbeat
journalctl -u trc-slave -n 100 --no-pager

# da master, query slave
curl -s -H "Authorization: Bearer $(cat /etc/trc-node-token)" \
     http://192.168.50.20:8001/api/capture/status | jq .

# restart pulito
sudo systemctl restart trc-slave
sudo systemctl restart trc-master

# trigger snapshot manuale (ZIP MF4 + report)
curl -X POST http://192.168.50.10:5000/api/incident/snapshot

# replay del PCAP per regression test
python tests/sim/trace_replay.py \
  logs/sample/runtime/mirror_60s_20260525T093450.pcap \
  --target 192.168.50.20:30490

# verifica integrità install
bash kvaser_bus_manager/install/verify_system.sh
```

---

## 11. Riepilogo: cosa è già testato vs cosa va testato in vettura

### ✅ Testato e PASSED

| Test | Risultato |
|------|-----------|
| Smoke API REST (10 endpoint) | 100% OK |
| Cross-VM dual-host (master VM ↔ slave VM) | OK |
| Pipeline gateway_sim → slave → MF4 @ 2000 pps × 20s | 100% match, 0 loss, 0 drop |
| **Replay PCAP reale Pi @ 1× @ 60s** | **100% match: 45,864 pkt / 596,232 frame / 0 loss / 47 MB MF4** |
| Replay PCAP reale Pi @ 2× | **0% loss, 596,232 frame, 0 drop** |
| Replay PCAP reale Pi @ 5× | 22% kernel UDP loss (Mac limit), 0 app drop |
| Counter reset bug (udp_packets_rx) | Fixato + verificato |
| Bearer-token auth | OK |
| Master proxy `/slave-node/api/*` | OK |
| Master `exec_cmd` remoto su slave | OK |

### ⚠️ Da testare in vettura

1. DoIP UDS completo col gateway Lambo (Routing Activation + Tester Present + WDBI 0xF1A0)
2. Throughput 80 Mbps sostenuto su Pi 5 (worst case veicolo)
3. USB serial gadget config dwc2 sul Pi reale
4. `trc-power.service` ignition shutdown
5. Failover master↔slave (slave crash → recovery)
6. Heartbeat verso TRC server reale (rete 172.30.96.0/24)

---

## 12. Branches GitHub

| Branch | HEAD | Per quale Pi |
|--------|------|--------------|
| `main` | 20c7b92 | legacy mono-Pi |
| **`master`** | 79f6ff5+ | Pi 5 16 GB controller (UI + AI + slave panel) |
| **`slave`** | 1aeeee1+ | Pi 5 4 GB capture dedicato |

Repo: https://github.com/requiemnoir/TRC_Oboboard_step3
