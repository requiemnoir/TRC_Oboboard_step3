# TRC Onboard — Bring-up Turn-key (2× Pi 5)

**Source of truth:** branch `master` (Pi 5 16 GB controller) e `slave` (Pi 5 4 GB capture) su https://github.com/requiemnoir/TRC_Oboboard_step3

**ISO di riferimento confermato funzionante in vettura:**
- `trc-rpi5-usb-clone-20260524.img` (sha256 `7b41b89b…783c`, 62.2 GB)
- Backup `.v3` (`c1c0a60f…b49e`), `.v2` (`c7dcec12…8d95`), `.bak` (v1 originale `8872b01a…43cfcc`)

---

## 1. Cosa importi clonando da GitHub

### Codice
- `kvaser_bus_manager/` — backend Flask + UI completo
- `mirror_logger/` — capture AUTOSAR Bus Mirror (raw + reliability + metrics)
- `node_protocol/` — wire schemas master↔slave (REST + WS auth)
- `master_node/` — slave_client + UI panel (mountato a `/slave-node/`)
- `slave_node/` — daemon + UI dedicata + install scripts
- `mf4_standalone_decoder/` — decoder offline
- `xcp/` — XCP master per ECU calibration
- `tests/sim/gateway_sim.py` — simulatore gateway per test

### Database (importati dalla ISO funzionante, via Git LFS ~480 MB)
| Categoria | Files | Size totale LFS |
|-----------|-------|-----------------|
| **DBC** (CAN matrici) | 9 file: CCAN, HCAN, DiagCAN, DCAN, ECAN, K2CAN, HCAN_modified_Vector, _5201C531_CAN7, simulation | ~16 MB (plain git, non LFS) |
| **FIBEX** FlexRay XML | 2 versioni: V8.21.01F + V8.24.00F | ~140 MB LFS (dedupe ×2 paths) |
| **ARXML** AUTOSAR | MLBevo_Gen1_Autosar V8.21.05F | ~222 MB LFS (dedupe ×4 paths) |
| **PDX** ECU diagnostic | LB634_2025-12-04_2_56_P_d.pdx | ~33 MB LFS (dedupe ×2 paths) |
| **A2L** ECU calibration | 5201C535_2, 5201C621 | ~53 MB LFS |
| **ODX** Gateway diagnostic | EV_Gatew.odx | ~14 MB LFS |
| **SKB** XCP Seed/Key | LBTCU_SeedKey_XCP_0001_v3_2 | 354 B (plain git) |
| **DTC history** sample | logs/sample/dtc_history.db | 1.8 MB LFS |
| **Sample MF4 traces** | session_20260525, incident_mil_on… | ~7 MB LFS |

### Configurazione DoIP (in `kvaser_bus_manager/config/app_config.example.json`)
```json
"gateway_mirror": {
  "auto_discover_ip": true,
  "autostart": true,
  "can": [],                                ← FIX importato dalla ISO
  "dest_ip": "192.168.200.1",
  "dest_port": 30490,
  "enabled": true,
  "flexray": ["A"],
  "gateway_ip": "fe80::200:ff:fe00:0%eth0",
  "lin": [],
  "target_addr": "0x4010",
  "target_bus": "ethernet",
  "tester_logical_address": "0x0E00"
}
```

### Templates config
- `deploy/trc_heartbeat.config.json.example` — TRC server URL, node_name, auth_token, intervalli (importato letterale dal Pi)
- `deploy/wg-vehicle.conf.example` — template WireGuard VPN (vettura non lo usa di default)

---

## 2. Bring-up sequence (operatore)

### Hardware preparato
- 2× Raspberry Pi 5 (master 16 GB + slave 4 GB)
- 2× microSD industrial 32 GB (SanDisk Industrial)
- 2× NVMe 256 GB su M.2 HAT+ ufficiale
- TP-Link TL-SG105 switch gigabit 5 porte (€15)
- Cavi Cat 6 30 cm × 3 (vettura ↔ switch ↔ Pi×2)
- PSU automotive 12 V → 5 V/8 A dual output

### Step 0 — flash microSD con Pi OS 64-bit Lite
- Usa Raspberry Pi Imager
- Account: `boss`, password forte, SSH abilitato
- Wi-Fi opzionale per primo bring-up

### Step 1 — bring-up SLAVE (~10 min)
```bash
# da laptop, sopra Wi-Fi o cavo USB-C console (vedi sezione 6)
ssh boss@<slave-ip-iniziale>

git clone -b slave https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
sudo bash slave_node/install/install_slave_turnkey.sh
```

L'installer fa **tutto**:
- apt deps (git, python venv, build essentials, tcpdump)
- `git lfs pull` (~480 MB database, ~5 min su buona ADSL)
- venv Python con asammdf, cantools, scapy, ecc.
- Genera `/etc/trc-node-token` (copialo sul master)
- netplan eth0 → 192.168.50.20/24 statico
- sysctl tuning UDP rcvbuf 64 MB
- `trc-slave.service` systemd con CPUAffinity=2,3 + Nice=-5
- `isolcpus=2,3` in `/boot/firmware/cmdline.txt` (reboot per attivare)
- Avvia il daemon

Verifica:
```bash
sudo systemctl status trc-slave
curl http://192.168.50.20:8001/api/health | jq .
```

### Step 2 — bring-up MASTER (~12 min)
```bash
ssh boss@<master-ip-iniziale>

git clone -b master https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3

# sincronizza il token dal slave (fondamentale!)
scp boss@192.168.50.20:/etc/trc-node-token /tmp/slave-token
sudo install -m 0640 -o root -g boss /tmp/slave-token /etc/trc-node-token

sudo bash master_node/install/install_master_turnkey.sh
```

L'installer fa:
- apt deps complete (chromium, pipewire, bluez, alsa, ecc.)
- `git lfs pull` (~480 MB)
- venv Python full
- Kvaser kernel modules (se USB Kvaser collegata)
- netplan eth0 → 192.168.50.10/24
- `trc-master.service` con `TRC_NODE_ROLE=master` → blueprint `/slave-node/` attivo
- `/etc/default/trc-master` con env
- `trc-heartbeat.service` se ENABLE_HEARTBEAT=1 (default sì) → invia ping a TRC server
- Autostart kiosk via `~/.config/autostart/trc-display.desktop` → Chromium kiosk su `/display`
- (Opzionale) WireGuard se ENABLE_VPN=1
- (Opzionale) Hailo-8 detect

Verifica:
```bash
sudo systemctl status trc-master trc-heartbeat
curl http://192.168.50.10:5000/api/live
curl http://192.168.50.10:5000/slave-node/api/health
```

### Step 3 — collega allo switch + gateway veicolo
```
[Vehicle Gateway] ─┬─ TP-Link TL-SG105 ─┬─ MASTER Pi 5  (eth0 = 192.168.50.10)
                   │                     ├─ SLAVE Pi 5   (eth0 = 192.168.50.20)
                   │                     └─ (opzionale)  Service laptop
                   │
                   └─ TRC server LAN     (172.30.96.143:8787)
```

I 2 Pi devono essere sulla **stessa LAN della gateway**, con IP statici sul subnet privato `192.168.50.0/24`. Il switch porta anche il segnale dal gateway veicolo via porta dedicata.

### Step 4 — accendi vettura, KL15 ON
- Backend autostart entro 25 s sul master
- Display kiosk entro 35 s
- Slave daemon attivo entro 8 s (boot rapido)
- DoIP DID 0xF1A0 inviato al gateway → mirror UDP comincia a fluire

### Step 5 — apri pannello
Da laptop sulla stessa LAN o dal mini-display HDMI:
```
http://192.168.50.10:5000/                   UI principale
http://192.168.50.10:5000/slave-node/        Pannello slave (live)
http://192.168.50.10:5000/display            Display kiosk
```

---

## 3. Configurazione TRC server (heartbeat)

Il file `/etc/trc_heartbeat/config.json` (installato dal turnkey installer):

```json
{
  "trc_server_url": "http://172.30.96.143:8787",
  "node_name": "TRC_Urus_master",
  "auth_token": "trc-heartbeat-2026",
  "heartbeat_interval_s": 30,
  "vpn_gateway": "172.30.96.143",
  "ping_interval_s": 30
}
```

Override pre-install:
```bash
sudo TRC_SERVER_URL=http://10.0.0.5:8787 NODE_NAME=TRC_Urus_demo \
  bash master_node/install/install_master_turnkey.sh
```

Override post-install: edita `/etc/trc_heartbeat/config.json` + `sudo systemctl restart trc-heartbeat`

Il payload del heartbeat include:
- hostname, IP, MAC eth0
- versione TRC (git sha + branch)
- uptime
- stato del slave (via SlaveClient)
- numero MF4 sul slave + free disk

Lo script `trc_heartbeat_sender.py` (in `deploy/`) gestisce SIGHUP per re-leggere la config a caldo.

---

## 4. Configurazione VPN (opzionale)

**Default vettura:** NO VPN client. La Pi è collegata fisicamente alla LAN vettura (`172.30.96.0/24`) dove vive anche il TRC server. Niente tunnel.

**Setup VPN solo se:**
- Pi accede al TRC server da rete diversa (sviluppo da casa, demo cliente, ecc.)
- Vuoi crittografia E2E del heartbeat traffic
- Multi-sito (tante Pi in vetture diverse, tutte verso un server centrale)

Per WireGuard:
```bash
sudo ENABLE_VPN=1 bash master_node/install/install_master_turnkey.sh
sudo nano /etc/wireguard/wg-vehicle.conf      # personalizza chiavi/peer
sudo systemctl enable --now wg-quick@wg-vehicle
```

Genera le chiavi:
```bash
wg genkey | tee /tmp/private.key | wg pubkey > /tmp/public.key
cat /tmp/private.key    # in [Interface] PrivateKey =
cat /tmp/public.key     # da inviare al server
```

Template completo già in `deploy/wg-vehicle.conf.example`.

Alternative supportate (installa a mano):
- **Tailscale** — più semplice, NAT-friendly: `curl -fsSL https://tailscale.com/install.sh | sh` + `tailscale up`
- **ZeroTier** — peer-to-peer mesh
- **OpenVPN** — legacy, evita se possibile

---

## 5. Database autoload

Il backend KBM ha un autoloader che scansiona:
- `kvaser_bus_manager/databases/dbc/` → DBC
- `kvaser_bus_manager/databases/arxml/` + `fibex/` → AUTOSAR/FIBEX
- `kvaser_bus_manager/databases/pdx/` → PDX (nuova location canonica)
- `kvaser_bus_manager/databases/a2l/` → XCP A2L
- `kvaser_bus_manager/databases/skb/` → Seed/Key XCP

Tutti i file della ISO sono **già pre-caricati** dopo `git lfs pull`. Niente upload manuale via UI.

Per forzare reload a runtime: `curl -X POST http://localhost:5000/api/db/reload`

---

## 6. USB serial console (debug emergenza)

Quando Ethernet è giù in vettura:
```bash
# sul Pi, una sola volta:
sudo bash install/enable_usb_serial_gadget.sh && sudo reboot
```

Poi collega cavo USB-C dal Pi al laptop:
- **macOS:** `screen /dev/cu.usbmodem* 115200`
- **Linux:** `minicom -D /dev/ttyACM0 -b 115200`
- **Windows:** PuTTY COMx 115200 8N1

Login automatico come `boss`.

---

## 7. Verifica finale

```bash
# sul master Pi
bash kvaser_bus_manager/install/verify_system.sh
```

Lo script verifica:
- ✓ `trc-master.service` active
- ✓ `trc-slave.service` reachable (dal master via slave_client)
- ✓ Backend risponde `/api/live`
- ✓ Display autostart entry presente
- ✓ Chromium installato
- ✓ Kvaser kernel modules caricati
- ✓ Piper/Whisper voice models
- ✓ Ollama + gemma model
- ✓ Mirror UDP listener `ss -uln`
- ✓ DBC/ARXML/FIBEX/PDX/A2L count
- ✓ trc-heartbeat ping ricevuto

---

## 8. Cosa NON è importato (intenzionalmente)

- ❌ **Modelli LLM Ollama** (Gemma, ~2 GB) — installa con `sudo bash kvaser_bus_manager/install/setup_ollama_pi5.sh`
- ❌ **Modelli voce Piper/Whisper** (~250 MB) — installa con `sudo bash kvaser_bus_manager/install/install_voice.sh`
- ❌ **Custom YOLO model `yolov8n.pt`** (~6 MB) — auto-scarica al primo uso se `CAM_YOLO_TRIGGER=1`
- ❌ **Modelli Hailo HEF** (specifici per Hailo-8) — installa via Hailo SDK separatamente
- ❌ **Tutti i log/trace di runtime** (sono in `.gitignore`) — solo `logs/sample/` ha le fixture
- ❌ **Token TRC server reale** — il template ha `trc-heartbeat-2026` placeholder, sostituiscilo

---

## 9. Roadmap di affidabilità

- [ ] **Failover automatico**: se slave crasha, master rileva entro 5s (panel mostra warning)
- [ ] **mDNS discovery**: `trc-slave.local` invece di IP fisso → setup zero
- [ ] **Hailo-8L M.2** sul master → LLM Gemma3:4B latency <100ms
- [ ] **systemd-firstboot** per propagare token automaticamente al primo boot del master
- [ ] **WireGuard mesh** opzionale con TRC server per multi-vettura

---

## 10. Branch e commit

| Branch | HEAD | Cosa contiene |
|--------|------|---------------|
| `main` | 20c7b92 | Codice base mono-Pi (legacy) |
| `master` | 39a0a5c | Tutto + master_node + slave_node + DB + traces + installer turnkey |
| `slave` | a60da6d | Stesso codice, README focused su slave role, install_slave_turnkey |

LFS usage GitHub: ~470 MB (su quota free 1 GB).

---

## 11. Riepilogo: cosa è importato dalla ISO funzionante

✅ **Importato verbatim:**
1. Tutti i database DBC (9), FIBEX (2 versioni), ARXML (1), PDX (1), A2L (2), ODX (1), SKB (1)
2. Configurazione DoIP `gateway_mirror` con il fix `"can": []`
3. Template `trc_heartbeat.config.json` (URL server, token, intervalli)
4. Sample traces (session MF4 + incident MIL + DTC history + VAG scan)
5. Script `run_kvbm_display.sh` per autostart kiosk
6. Tutti gli install scripts `kvaser_bus_manager/install/` (Kvaser, voice, ollama, autostart, healthcheck)
7. `verify_system.sh` con detect Ollama port 11434+11435, gemma3:270m suggestion, DB scan ricorsivo

✅ **Aggiunto sopra (non era sull'ISO):**
1. Architettura master/slave (split su 2 Pi)
2. `node_protocol/` REST + SocketIO + bearer auth
3. `slave_node/` daemon + UI dedicata
4. `master_node/` slave_client + panel UI proxy
5. Install scripts turn-key che orchestrano TUTTO
6. USB serial gadget fallback per debug emergenza
7. Gateway simulator per test E2E
8. WireGuard template (opzionale)
9. Documentazione (questo file + SETUP_DUAL_NODE.md + TEST_E2E_REPORT.md)

❌ **Non importato (volutamente o per limite):**
1. Modelli LLM/Voice (multi-GB, installati al primo boot via install_models.sh)
2. Token TRC server reale (placeholder nel template)
3. Runtime logs MB-grandi (mirror_capture jsonl 103 MB, trc-native.log)
4. macOS resource forks (`._*` files dal extract via exFAT)
5. Modifiche al kernel/firmware Pi 5 (rispetto Pi OS upstream)
