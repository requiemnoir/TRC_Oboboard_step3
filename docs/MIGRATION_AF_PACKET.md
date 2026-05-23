# Migrazione del sistema di acquisizione TRC Onboard ad AF_PACKET

Questa guida descrive **come aggiornare un Raspberry Pi già in vettura** che gira
la versione "vecchia" di TRC Onboard (acquisizione mirror via Scapy
`sniff()` dentro `kvaser_bus_manager`) al nuovo stack di acquisizione basato su
`AF_PACKET` + BPF kernel-side, distribuito come modulo dedicato `mirror_logger/`.

> ℹ️ La migrazione **non sostituisce** `kvaser_bus_manager`. Lo affianca:
> il vecchio backend Ethernet continua a essere disponibile per UI generale,
> ScanTools, MF4 Viewer e tutto ciò che non è ingest mirror. Il nuovo
> `mirror_logger` è un servizio dedicato, isolato, che fa solo cattura +
> persistenza MF4 ad alta cadenza.

Sommario:

1. [Cosa cambia (perché AF_PACKET)](#1-cosa-cambia)
2. [Compatibilità e coesistenza](#2-compatibilità-e-coesistenza)
3. [Prerequisiti hardware e software](#3-prerequisiti)
4. [Pre-flight checklist](#4-pre-flight-checklist)
5. [Backup del sistema esistente](#5-backup)
6. [Aggiornamento del codice sul Pi](#6-aggiornamento-codice)
7. [Installazione `mirror_logger`](#7-installazione-mirror_logger)
8. [Configurazione interfaccia di rete e sysctl](#8-rete-e-sysctl)
9. [Capability `cap_net_raw` e systemd](#9-capability-systemd)
10. [Attivazione mirror lato gateway (DoIP DID 0x096F)](#10-mirror-gateway)
11. [Token API e firewall](#11-token-firewall)
12. [Smoke test post-install](#12-smoke-test)
13. [Stress test con tracce reali](#13-stress-test)
14. [Performance tuning](#14-performance-tuning)
15. [Convivenza con `kvaser_bus_manager`](#15-convivenza-kbm)
16. [Differenze API tra legacy e nuovo](#16-differenze-api)
17. [Rollback](#17-rollback)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. Cosa cambia

| Aspetto | Legacy (`kvaser_bus_manager/backend/ethernet_capture.py`) | Nuovo (`mirror_logger/capture.py`) |
|---|---|---|
| API socket | `scapy.sniff()` + libpcap | `socket.AF_PACKET` raw |
| Filtro BPF | stringa → compilata e applicata da libpcap | JIT kernel-side via `SO_ATTACH_FILTER` (7 istruzioni) |
| Parsing | oggetti scapy (`Ether`, `IP`, `UDP`) | parsing manuale byte-level, no allocazioni |
| Timestamp | `scapy_packet.time` (float software) | `SO_TIMESTAMPNS` (ns dal driver kernel) |
| Buffer kernel | default (~256 KB) | `SO_RCVBUF=16 MB` |
| Threading | `sniff()` bloccante con `timeout=1s` | thread dedicato + queue producer/consumer |
| Dipendenze | scapy, libpcap-dev, dpkt | nessuna (solo numpy + asammdf per persistenza) |
| Throughput target | <10 kfps stabili | **>50 kfps sostenuti su Pi 4/5** |
| Drop rate atteso | qualche % a 20 kfps | **<0.001 % a 36 kfps reali** |
| Latenza primo frame | ~1.5 s (caricamento scapy lazy) | ~150 ms |
| Reassembly DoIP TCP | non gestito | gestito (`_DoIPReassembler` interno) |
| PCAP capture | via scapy `wrpcap` (lento) | writer minimale interno (`_SimplePcapWriter`) |

Motivazioni concrete del cambio:
- **Drop kernel-side a regime alto**: con il sniff scapy, sopra ~15 kfps il
  socket di libpcap iniziava a buttare via pacchetti perché il buffer di
  ricezione non veniva svuotato abbastanza in fretta.
- **Costo del parsing oggetti**: ogni pacchetto allocava `Ether/IP/UDP` con
  GC sotto. A 30 kfps il garbage collector saturava la CPU del Pi 4.
- **Timestamp imprecisi**: `pkt.time` dal kernel scapy è prima del context-switch
  utente. Con AF_PACKET + `SO_TIMESTAMPNS` il timestamp viene letto direttamente
  dall'ISR del driver.
- **Manutenibilità**: ~600 righe `capture.py` vs >3000 righe nel legacy.

---

## 2. Compatibilità e coesistenza

Dopo la migrazione, sul Pi convivono **due servizi distinti**:

| Servizio | Porta | Funzione |
|---|---|---|
| `kvbm.service` (kvaser_bus_manager) | `5000` | UI EV-Q Onboard Manager, ScanTools, MF4 Viewer, diagnostica |
| `mirror-logger.service` (nuovo) | `5050` | Ingest mirror UDP/TCP DoIP → MF4 ad alta cadenza |

Possono girare contemporaneamente e non condividono stato. Il vecchio
`ethernet_capture.py` resta disponibile per il KBM (es. per la pagina
"Live Traffic" con `KBSM_LIVE_TRAFFIC_ENABLE=1`), ma **per il logging mirror
operativo si usa `mirror_logger`**.

**Consiglio**: dopo aver verificato che `mirror_logger` funziona, disattiva
la cattura mirror nel KBM per evitare doppio consumo di banda/CPU. Vedi
sezione [15](#15-convivenza-kbm).

---

## 3. Prerequisiti

### Hardware
- Raspberry Pi 4 (>=2 GB) o Pi 5 (consigliato per CAN-FD + FlexRay full).
- Cavo automotive Ethernet → gateway (con switch o link diretto).
- microSD A2 (>=64 GB) **oppure** SSD via USB 3 / NVMe HAT Pi5 (caldamente
  consigliato per lunghe sessioni di logging).
- Alimentazione stabile 5 V / 3-5 A (un Pi5 sotto carico mirror + KBM
  ne tira facilmente 2.5-3 A).

### Software (Pi)
- Raspberry Pi OS Bookworm (Debian 12) 64-bit, kernel **>= 5.15** (BPF JIT
  abilitato di default da `CONFIG_BPF_JIT=y` + `bpf_jit_enable=1`).
- Python >= 3.10 (Bookworm spedisce 3.11).
- Git, Git-LFS (se i database DBC/FIBEX sono LFS).
- `libcap2-bin` (per `setcap`).

### Dipendenze Python aggiunte da `mirror_logger`
Solo tre, già nel `requirements.txt` di [`mirror_logger/`](../mirror_logger/requirements.txt):

```
flask>=3.0
asammdf>=7.3
numpy>=1.24
```

Nessuna scapy. Nessuna libpcap. Il vecchio venv di KBM ha già queste dipendenze
(soddisfa il subset richiesto), ma per isolamento `mirror_logger` crea il
proprio `.venv`.

---

## 4. Pre-flight checklist

Esegui sul Pi prima di iniziare:

```bash
# Identità sistema
uname -m && uname -r              # aarch64 e kernel >= 5.15
cat /etc/os-release | grep VERSION # bookworm
python3 --version                 # >= 3.10

# Spazio disco libero (servono >=2 GB per venv + asammdf + buffer logs)
df -h /

# BPF JIT abilitato?
cat /proc/sys/net/core/bpf_jit_enable   # deve essere 1 (o 2)

# Capability disponibili
command -v setcap && getcap /usr/bin/python3 || echo "  (nessuna capability impostata)"

# Interfaccia mirror — verifica nome corretto
ip -br link show | grep -v lo

# Configurazione gateway veicolo (se nota)
ip addr show eth0 | grep inet
ping -c 2 192.168.0.140   # IP gateway tipico VAG / MLBevo

# Servizi attivi (devi sapere cosa stai per fermare/migrare)
systemctl --no-pager list-units --type=service --state=active | grep -iE "(kvbm|kvaser|mirror|trc)"
```

Annota i risultati. Se manca anche solo un prerequisito, **non procedere**.

---

## 5. Backup

**Sempre prima di toccare nulla.** Lavora sul Pi via SSH:

```bash
# 1. Snapshot del codice corrente
cd "${HOME}"   # tipicamente /home/pi
sudo tar -czf "trc_onboard_BACKUP_$(date +%Y%m%d_%H%M%S).tar.gz" \
    --exclude='*/.venv' --exclude='*/__pycache__' --exclude='*/logs' \
    TRC_Onboard*/

# 2. Snapshot di config/.token e config user.json del KBM
sudo cp -a TRC_Onboard*/kvaser_bus_manager/config/ ~/kbm_config_backup_$(date +%Y%m%d).tar
sudo cp -a TRC_Onboard*/kvaser_bus_manager/databases/ ~/kbm_databases_backup_$(date +%Y%m%d).tar 2>/dev/null || true

# 3. Snapshot delle unit systemd
sudo cp -a /etc/systemd/system/kvbm*.service ~/ 2>/dev/null || true

# 4. Mostra dove sono i backup
ls -lh ~/*.tar* ~/kbm_*backup* 2>/dev/null
```

Trasferisci il tarball su un disco esterno o una macchina di backup:

```bash
# Dal tuo laptop:
scp pi@<raspberry-ip>:~/trc_onboard_BACKUP_*.tar.gz ./
```

Solo a questo punto procedi.

---

## 6. Aggiornamento del codice

Due strade. Scegli in base alla tua workflow.

### 6a. Tramite `git pull` (se il Pi clona da un remoto)

```bash
cd ~/TRC_Onboard   # o il nome corretto
git fetch --all
git status         # verifica che non ci siano modifiche locali non committate
git checkout main  # o il branch di rilascio
git pull --ff-only

# Aggiorna eventuali sub-pacchetti / LFS
git lfs install --skip-smudge && git lfs pull
```

Se `git status` mostra modifiche locali (config personalizzate, ecc.), salvale
in un branch privato prima del pull:

```bash
git stash push -m "local-config-pre-af_packet"
```

### 6b. Tramite trasferimento manuale (offline / no remote)

Sul laptop di sviluppo (questo è il caso comune in vettura):

```bash
# Crea pacchetto slim della codebase aggiornata
tar -czf trc_onboard_v_af_packet.tar.gz \
    --exclude='*/.venv' --exclude='*/__pycache__' \
    --exclude='*/Tracciatest' --exclude='*/logs/*' \
    --exclude='*.cpython-*.so' \
    TRC_Onboard/

# Trasferisci
scp trc_onboard_v_af_packet.tar.gz pi@<raspberry-ip>:~/

# Sul Pi
cd ~ && tar -xzf trc_onboard_v_af_packet.tar.gz
# Ripristina eventuali config personalizzate dal backup
cp ~/kbm_config_backup_*/user.json TRC_Onboard/kvaser_bus_manager/config/ 2>/dev/null || true
```

A questo punto la directory `mirror_logger/` deve esistere accanto a
`kvaser_bus_manager/`:

```bash
ls TRC_Onboard/mirror_logger/   # capture.py, raw_logger.py, mirror_parser.py, app.py, install.sh
```

---

## 7. Installazione `mirror_logger`

Il nuovo modulo ha il suo installer dedicato.

```bash
cd ~/TRC_Onboard/mirror_logger
chmod +x install.sh

# Opzione A: install solo software (venv locale, setcap, token random)
./install.sh

# Opzione B: install + service systemd (consigliato in vettura)
./install.sh --systemd

# Opzione C: cambio porta (default 5050)
./install.sh --systemd --port 5060
```

Cosa fa lo script (vedi [`mirror_logger/install.sh`](../mirror_logger/install.sh)
per il dettaglio):

1. Verifica Python >= 3.10.
2. Crea `.venv/` locale.
3. Installa `flask`, `asammdf`, `numpy` (e nient'altro).
4. Su Linux: applica `setcap cap_net_raw,cap_net_admin=eip` al python del venv.
   Così il logger può aprire `AF_PACKET` senza `sudo` a runtime.
5. Genera un token random in `config/.token` (`chmod 600`).
6. Se `--systemd`: scrive l'unit `/etc/systemd/system/mirror-logger.service`
   con `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN`, `Restart=always`,
   `OOMScoreAdjust=-500`, healthcheck via `curl /api/health`.

Output atteso:

```
[*] OS rilevato: Linux
[*] Python: python3 (v3.11)
[OK] venv creato
[OK] Dipendenze installate
[OK] Directory logs/ e config/ pronte
[OK] Capability impostate (avvio senza sudo)
[OK] Token API generato in config/.token
[OK] Service installato. Avvio: sudo systemctl start mirror-logger
[OK] Installazione completata
  venv         : /home/pi/TRC_Onboard/mirror_logger/.venv
  token API    : <token-random-base64>
  porta UI     : 5050
```

> ⚠️ Annota il token: serve per ogni `POST /api/*`. È salvato in
> `mirror_logger/config/.token` con `chmod 600` (solo `pi` lo legge).

---

## 8. Rete e sysctl

### 8.1 Identifica l'interfaccia mirror

In vettura il Pi ha **due use case** di rete:

- `eth0` o `enp0s1` → connessa al gateway veicolo (riceve il mirror).
- `wlan0` o secondaria → per accesso SSH/UI dal laptop.

Verifica:

```bash
ip -br link show
ip addr show eth0
ethtool eth0   # (apt install ethtool)  → cerca "Link detected: yes"
```

Edita `mirror_logger/config/default.json` o `config/user.json`:

```json
{
  "interface": "eth0",
  "gateway_ip": "192.168.0.140",
  "mirror_dest_ip": "192.168.0.100",
  "mirror_dest_port": 30490,
  "can_networks": [1, 2, 3],
  "flexray_channels": ["A", "B"],
  "lin_networks": [],
  "target_bus": 2,
  "gateway_logical_addr": 0,
  "flask_host": "0.0.0.0",
  "flask_port": 5050
}
```

### 8.2 IP statico Pi (CRITICO)

Il gateway risponde solo a un IP noto. Su Bookworm con NetworkManager:

```bash
sudo nmcli con mod "Wired connection 1" \
    ipv4.method manual \
    ipv4.addresses 192.168.0.100/24 \
    ipv4.gateway 192.168.0.140
sudo nmcli con up "Wired connection 1"
ip addr show eth0
```

Se usi `dhcpcd` (Pi OS più vecchio), modifica `/etc/dhcpcd.conf`:

```conf
interface eth0
static ip_address=192.168.0.100/24
static routers=192.168.0.140
```

Riavvia il networking (`sudo systemctl restart dhcpcd` o reboot).

### 8.3 Sysctl per ricezione burst

Per non perdere pacchetti durante i picchi del Bus Mirror:

```bash
sudo tee /etc/sysctl.d/99-trc-mirror.conf <<'EOF'
# Buffer di ricezione massimo (default 256 KB → 32 MB)
net.core.rmem_max=33554432
net.core.rmem_default=16777216
# Coda interfaccia (txqueuelen non si tocca, ma netdev_max_backlog sì)
net.core.netdev_max_backlog=10000
# Disabilita reverse path filter su eth0 se hai più gateway
# net.ipv4.conf.eth0.rp_filter=0
EOF
sudo sysctl --system | grep -E "(rmem|backlog)"
```

`mirror_logger` chiede `SO_RCVBUF=16 MB` esplicitamente; se `rmem_max < 16 MB`
il kernel lo limita silenziosamente al massimo consentito.

### 8.4 MTU della NIC mirror

Se il gateway emette pacchetti UDP > 1500 byte, **devi**:

- alzare l'MTU della NIC su entrambi i lati (Pi e switch + gateway), **oppure**
- chiedere al fornitore del gateway di emettere pacchetti che stiano in MTU 1500.

`mirror_logger.MirrorCapture` **non riassembla i frammenti IP** (`capture.py:407-409`).
Se ricevi solo il primo frammento di un datagram frammentato, perdi tutto il
contenuto successivo del pacchetto.

Per impostare MTU 9000 (jumbo frames, dove supportato):

```bash
sudo ip link set eth0 mtu 9000
ip link show eth0   # verifica
```

Per renderlo persistente, aggiungilo a NetworkManager o dhcpcd.

### 8.5 Disabilita offload che ricombina pacchetti

Alcuni driver fanno GRO/LRO che **non** sono trasparenti per AF_PACKET:

```bash
sudo ethtool -K eth0 gro off lro off tso off gso off
# verifica
ethtool -k eth0 | grep -E "(gro|lro|tso|gso):"
```

Per persistenza: udev rule o `pre-up` script. Esempio NetworkManager dispatcher:

```bash
sudo tee /etc/NetworkManager/dispatcher.d/99-ethtool-off <<'EOF'
#!/usr/bin/env bash
IFACE="$1"; STATE="$2"
[[ "$IFACE" == "eth0" && "$STATE" == "up" ]] || exit 0
/sbin/ethtool -K eth0 gro off lro off tso off gso off 2>/dev/null || true
EOF
sudo chmod +x /etc/NetworkManager/dispatcher.d/99-ethtool-off
```

---

## 9. Capability e systemd

L'installer ha già fatto:

```bash
sudo setcap cap_net_raw,cap_net_admin=eip "$(readlink -f mirror_logger/.venv/bin/python)"
```

Verifica:

```bash
getcap "$(readlink -f ~/TRC_Onboard/mirror_logger/.venv/bin/python)"
# atteso: /usr/bin/python3.11 cap_net_admin,cap_net_raw=eip
```

Se hai installato `--systemd`, l'unit è già attiva:

```bash
sudo systemctl status mirror-logger.service
sudo systemctl enable mirror-logger.service   # avvio al boot
sudo systemctl start mirror-logger.service
sudo journalctl -u mirror-logger.service -f   # log in tempo reale
```

L'unit applica `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN` quindi NON serve
`sudo` a runtime; il setcap è un fallback se lanci `python app.py` manualmente.

---

## 10. Mirror gateway (DoIP DID 0x096F)

Il logger riceve solo se il gateway veicolo è stato istruito a emettere il mirror.
L'attivazione avviene via DoIP + UDS `WriteDataByIdentifier (0x2E)` sul DID
standard `0x096F`. Il codice in
[`mirror_logger/doip_activator.py`](../mirror_logger/doip_activator.py) implementa
questa sequenza:

1. UDP Vehicle Discovery (broadcast IPv4 + multicast IPv6 `ff02::1`).
2. TCP Routing Activation (logical tester address `0x0E00`).
3. UDS `0x2E 0x096F` con payload:

```
byte 0      : target_bus      (0=off, 1=CAN_diag, 2=Ethernet)
byte 1      : CAN bitmask     (bit0=CAN1 … bit7=CAN8)
byte 2      : FR/LIN bitmask  (bit0=FR_A, bit1=FR_B, bit4=LIN1, bit5=LIN2, bit6=LIN3)
byte 3-18   : IPv6 (o IPv4-mapped) del destination del mirror (= IP del Pi)
byte 19-20  : porta UDP destinazione (default 30490)
```

4. Keep-alive `TesterPresent (0x3E 0x80)` ogni `keepalive_interval_s` (default 2 s).

### Attivazione manuale via API

```bash
TOKEN="$(cat ~/TRC_Onboard/mirror_logger/config/.token)"
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/mirror/activate

# Verifica
curl -fsS -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/status | jq .mirror
# atteso:
# {
#   "activated": true,
#   "connected": true,
#   "gateway_ip": "192.168.0.140",
#   "last_error": ""
# }
```

### Attivazione automatica al boot

In `config/user.json`:

```json
{
  "auto_activate_mirror": true,
  "auto_start_capture": true
}
```

Al boot, dopo 2 s il logger lancia da solo `/api/mirror/activate` e dopo altri
2 s `/api/start` (vedi `app.py:716-745`).

---

## 11. Token e firewall

### Token API

L'install genera un token random in `config/.token`. Per usarlo:

```bash
# Via header (modo unico supportato)
curl -H "X-Auth-Token: $(cat config/.token)" http://localhost:5050/api/status
```

> ❌ Il fallback `?token=` come query string è stato rimosso (i token in URL
> finiscono nei log di accesso). Usa SOLO l'header `X-Auth-Token`.

### Firewall sul Pi

In vettura solitamente non c'è. Se hai `ufw`/`firewalld` attivo, apri 5050 da
LAN locale:

```bash
sudo ufw allow from 192.168.0.0/24 to any port 5050 proto tcp
```

Per il **bind**: di default `flask_host=0.0.0.0`. Se vuoi limitare al solo
loopback (e accedere via tunnel SSH), metti `"flask_host": "127.0.0.1"` in
`config/user.json`. Il logger emette un warning all'avvio se bind ≠ loopback
**e** nessun token è configurato (vedi `app.py:758-764`).

---

## 12. Smoke test post-install

Sequenza minima per verificare che tutto giri.

### 12.1 Servizio attivo

```bash
sudo systemctl is-active mirror-logger.service
# atteso: active

curl -fsS http://localhost:5050/api/health | jq .ok
# atteso: true
```

### 12.2 Health dettagliato

```bash
curl -fsS http://localhost:5050/api/health | jq
```

Devi vedere:
- `"ok": true`
- `"disk.writable": true`
- `"disk.low": false`
- `"logging_active": false` (non hai ancora avviato la sessione)

### 12.3 Attiva il mirror gateway

```bash
TOKEN="$(cat ~/TRC_Onboard/mirror_logger/config/.token)"
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/mirror/activate | jq

curl -fsS -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/status | jq .mirror
```

Atteso: `"activated": true, "connected": true, "last_error": ""`.

Se vedi `"NRC 0x33"` o simili: il gateway sta rifiutando la `WriteDID`. Cause
tipiche:
- Sessione diagnostica non attivata (alcuni gateway richiedono `0x10 0x03` prima).
- Logical address tester errato (`gateway_logical_addr` in config — prova `0`
  per auto-discovery, o il valore noto del progetto).
- Security access richiesto (raro per il DID mirror, ma possibile).

### 12.4 Avvia il logging e osserva

```bash
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/start | jq

# Verifica che il throughput aumenti
watch -n 1 "curl -fsS -H 'X-Auth-Token: $TOKEN' \
    http://localhost:5050/api/status | jq '{pps:.capture.pps,kbps:.capture.kbps,frames:.logger.frame_count,drop:.logger.dropped_count}'"
```

Su un veicolo MLBevo / LB63X con mirror full attivo:
- `pps` dovrebbe stabilizzarsi tra 5-15k pacchetti/s (pacchetti UDP, non frame).
- `frames` deve crescere di decine di migliaia al secondo.
- `drop` deve restare **0**.

Dopo 30 s ferma e verifica i file:

```bash
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/stop | jq

curl -fsS -H "X-Auth-Token: $TOKEN" \
    http://localhost:5050/api/sessions | jq '.sessions[0]'
```

Devi vedere `part_count >= 1` e `total_size > 0`. I file MF4 sono in
`mirror_logger/logs/session_<ts>_p0000.mf4` ecc.

### 12.5 Apri un MF4 e verifica i segnali

```bash
~/TRC_Onboard/mirror_logger/.venv/bin/python <<'PY'
from asammdf import MDF
import glob
f = sorted(glob.glob('/home/pi/TRC_Onboard/mirror_logger/logs/session_*.mf4'))[-1]
m = MDF(f)
print('signals:', [s.name for s in m.iter_channels()])
print('frame count:', len(m.get('ts_ns').samples))
m.close()
PY
```

Atteso:
```
signals: ['ts_pkt', 'ts_ns', 'ch', 'bus_type', 'arb_id', 'flags', 'dlc', 'payload']
frame count: <numero ragionevole, es. 1_000_000+>
```

---

## 13. Stress test con tracce reali

Per validare il sistema senza dover essere in vettura, puoi rispedire al Pi
una traccia MF4 reale come se arrivasse dal gateway.

Sul **laptop di sviluppo** (con la traccia disponibile localmente), usa lo
script di replay sviluppato per la VM ARM64:
[`/Volumes/Elements/qemu-arm64/seeds/host_sender.py`](../mirror_logger/). Lo
adatti cambiando `MIRROR_HOST = '<raspberry-ip>'` e lo lanci:

```bash
# Sul laptop
python3 host_sender.py
```

Il sender:
1. Apre la traccia MF4 sorgente.
2. Estrae il timing reale di ogni PDU (~36 kfps medi per LB63X).
3. Riconfeziona i frame in pacchetti AUTOSAR ISO 23150 (header 7 B + entries).
4. **Spezza i pacchetti per stare in MTU 1500** (importante! Altrimenti il Pi
   perde i frammenti — vedi sezione 8.4).
5. Invia a `<raspberry-ip>:30490` con pacing wall-clock (replay 1:1).

Sul Pi, mentre il sender gira, lascia il `mirror_logger` attivo (`/api/start`)
ma **NON attivare il mirror gateway reale** (`/api/mirror/deactivate` se è
acceso) — altrimenti ricevi anche il traffico reale e perdi il confronto con
la traccia.

A fine sender, fermi il logger e confronti:

| Numero | Atteso |
|---|---|
| `stats.dropped_count` | **0** |
| `logger.frame_count` finale | uguale ai frame inviati dal sender |
| `flush_errors` | **0** |

Risultati pratici già misurati su VM ARM64 + HVF (proxy del Pi 5):
**1.098.066 frame in 30 s, zero drop, realtime factor 1.000×**.

---

## 14. Performance tuning

Tutti i parametri sono in [`config/default.json`](../mirror_logger/config/default.json)
e sovrascrivibili in `config/user.json` o via `POST /api/config`.

| Chiave | Default | Quando aumentare | Quando ridurre |
|---|---|---|---|
| `queue_max` | 524288 | Burst > 500k frame/s sostenuti | Pi 2 GB con RAM scarsa |
| `put_timeout_ms` | 25 | Mai (è il backpressure consentito) | — |
| `chunk_interval_s` | 15 | Sessioni lunghe, file più grandi | Riduci a 5 per crash safety |
| `chunk_max_frames` | 2_000_000 | Carichi bassi (file più grossi) | Pi con poca RAM |
| `flush_interval_s` | 10 | Carichi alti (meno I/O) | Crash safety (riduci a 2-5) |
| `flush_interval_frames` | 5000 | Stessa logica | — |
| `pcap_enabled` | false | Debug / forensic | Sempre off in prod (raddoppia I/O) |

### Tuning ad alto carico (>50 kfps)

```json
{
  "queue_max": 1048576,
  "chunk_interval_s": 5,
  "chunk_max_frames": 5000000,
  "flush_interval_s": 2,
  "flush_interval_frames": 20000
}
```

Effetto: chunk più corti (=5 s) garantiscono che un crash perda al massimo 5 s
di dati; flush intermedio ogni 2 s mantiene la persistenza alta senza
riscrivere file enormi.

### Misurare drop e capacità reale

In esecuzione:

```bash
curl -fsS -H "X-Auth-Token: $TOKEN" http://localhost:5050/api/status | jq '.logger | {fps, drop_ratio, queue_size, queue_max}'
```

- Se `queue_size / queue_max > 0.5` per più di qualche secondo → producer più
  veloce del consumer; aumenta `queue_max` o riduci `chunk_interval_s` (chunk
  più piccoli = scritture più veloci).
- Se `drop_ratio > 0`: hai PERSO frame. Cause:
  - Disco saturo (`SD A1 vs A2`).
  - CPU saturata (controlla con `htop`).
  - `rmem_max` troppo basso (vedi 8.3).

---

## 15. Convivenza con `kvaser_bus_manager`

Dopo che `mirror-logger.service` è attivo e validato, **disattiva la cattura
mirror nel KBM** per evitare doppio consumo.

### Opzione A — Variabile d'ambiente (consigliata)

Nel file di systemd di `kvbm.service` (o nel `.env` letto dall'app):

```ini
Environment=KBSM_ETHERNET_MIRROR_DISABLED=1
Environment=KBSM_LIVE_TRAFFIC_ENABLE=0
```

Riavvia: `sudo systemctl daemon-reload && sudo systemctl restart kvbm.service`.

### Opzione B — Patch nella UI del KBM

Nella pagina di config di KBM, sezione **Ethernet**, deseleziona
"Abilita cattura mirror" e salva. Il setting resta in `config/user.json` di
KBM.

### Risorse condivise da verificare

| Risorsa | Conflitto | Soluzione |
|---|---|---|
| Porta UDP 30490 | mirror_logger ascolta solo; KBM ascoltava → libera | Usa solo mirror_logger |
| Interfaccia `eth0` AF_PACKET | due AF_PACKET su stessa NIC sono OK (filtri BPF indipendenti) | Nessuna azione |
| CPU | scapy del KBM saturava un core | Disabilita KBM eth capture |
| Disco | due processi che scrivono MF4 contemporaneamente | Punta i `log_dir` a path separati |

---

## 16. Differenze API

### Endpoint legacy KBM (sopravvivono per compatibilità)

| Path | Funzione |
|---|---|
| `POST /api/eth_capture/start` | Avviava sniff scapy |
| `POST /api/eth_capture/stop` | Fermava sniff |
| `GET  /api/eth_capture/status` | Stats live |

### Endpoint nuovi `mirror_logger`

| Metodo | Path | Note |
|---|---|---|
| `POST` | `/api/start` | Avvia sessione MF4 (logger + capture) |
| `POST` | `/api/stop` | Ferma + flush finale, ritorna stats |
| `GET`  | `/api/status` | `logger` + `capture` + `mirror` + `disk` + `reliability` |
| `GET`  | `/api/health` | Healthcheck pubblico (no auth, per systemd) |
| `POST` | `/api/mirror/activate` | DoIP routing + UDS WriteDID 0x096F |
| `POST` | `/api/mirror/deactivate` | Chiude DoIP + ferma keepalive |
| `GET`  | `/api/sessions` | Lista sessioni raggruppate |
| `GET`  | `/api/sessions/<id>/bundle.zip` | Download ZIP di tutti i part MF4 |
| `DELETE` | `/api/sessions/<id>` | Cancella sessione (non quella attiva) |
| `GET`  | `/api/logs/<filename>` | Download singolo file |
| `GET`  | `/api/config` | Config corrente |
| `POST` | `/api/config` | Update partial (debounce 500 ms) |
| `POST` | `/api/maintenance/enforce_retention` | Forza retention |

### Migrazione di integrazioni esterne

Se hai script o dashboard che parlano col KBM legacy, devi:

1. Cambiare endpoint base da `http://<pi>:5000/api/eth_capture/...` a
   `http://<pi>:5050/api/...`.
2. Aggiungere header `X-Auth-Token: <token>` (i vecchi non avevano auth o
   accettavano `?token=`).
3. Adattare il parsing della response: `status` ora include sotto-oggetti
   distinti (`logger`, `capture`, `mirror`, `disk`).

---

## 17. Rollback

Se per qualche motivo il nuovo stack non funziona in vettura e devi tornare al
legacy:

```bash
# 1. Ferma e disabilita il nuovo servizio
sudo systemctl stop mirror-logger.service
sudo systemctl disable mirror-logger.service

# 2. Riabilita la cattura nel KBM (se l'avevi disattivata)
sudo systemctl edit kvbm.service  # rimuovi KBSM_ETHERNET_MIRROR_DISABLED=1
sudo systemctl daemon-reload
sudo systemctl restart kvbm.service

# 3. (Opzionale) Ripristina il codice dal backup
cd ~ && tar -xzf trc_onboard_BACKUP_<timestamp>.tar.gz
sudo systemctl restart kvbm.service

# 4. Verifica
curl -fsS http://localhost:5000/api/health
```

I file MF4 già prodotti da `mirror_logger` restano in
`mirror_logger/logs/`. **Non cancellarli** se contengono dati utili.

---

## 18. Troubleshooting

| Sintomo | Causa probabile | Diagnosi | Fix |
|---|---|---|---|
| `PermissionError: [Errno 1] Operation not permitted` su `socket(AF_PACKET, ...)` | `setcap` non applicato (o python sbagliato) | `getcap $(readlink -f mirror_logger/.venv/bin/python)` | Riapplica `setcap cap_net_raw,cap_net_admin=eip` |
| `OSError: [Errno 19] No such device` | Nome interfaccia errato in config | `ip -br link show` | Aggiorna `interface` in `config/user.json` |
| 0 pps / 0 frame anche se mirror attivo | Frammentazione IP, BPF blocca i frammenti | `tcpdump -i eth0 -n 'ip[6:2] & 0x1fff != 0'` → se vedi pacchetti, è frammentazione | Riduci frame/pkt nel gateway, o alza MTU |
| Drop rate > 0 | rmem insufficiente o disco lento | `cat /proc/sys/net/core/rmem_max`, `iostat -x 1` | Vedi sezione 8.3 e 14 |
| `[DoIP] NRC 0x7F` su WriteDID | Sessione/security non aperta | `journalctl -u mirror-logger -f` | Verifica `gateway_logical_addr`, prova session 0x10 0x03 |
| `flush_errors > 0` | `asammdf` errore disco o RAM | log `journalctl` | Riduci `chunk_max_frames` o controlla `dmesg` per I/O error |
| Health `disk_low: true` | Spazio < `min_free_disk_mb` | `df -h logs/` | Esegui retention: `POST /api/maintenance/enforce_retention` |
| Pi si riavvia sotto carico | Alimentazione insufficiente | `dmesg | grep -i undervoltage` | Cavo USB-C ≥ 5 V / 5 A, alimentatore ufficiale Pi5 |
| Token errato → tutte le `/api/*` ritornano 401 | Token in env non sincronizzato | `echo $MIRROR_LOGGER_TOKEN`, `cat config/.token` | Allinea (uso `Environment=MIRROR_LOGGER_TOKEN=...` nel unit) |
| `auto_activate_mirror` non funziona al boot | Rete non pronta quando parte il timer | `journalctl -u mirror-logger` cerca "auto_activate" | Aggiungi `After=network-online.target` (già presente) e ritardo |

### Log utili

```bash
# Live tail del logger
sudo journalctl -u mirror-logger.service -f

# Storico (ultime 200 righe)
sudo journalctl -u mirror-logger.service -n 200 --no-pager

# Errori sistema (undervoltage, I/O, OOM)
dmesg --time-format iso | tail -50

# Status mirror in tempo reale
watch -n 1 "curl -fsS -H 'X-Auth-Token: $(cat ~/TRC_Onboard/mirror_logger/config/.token)' http://localhost:5050/api/status | jq '{m:.mirror,l:.logger,c:.capture,d:.disk}'"
```

### Quando aprire un ticket / chiedere aiuto

Includi sempre:
- Output di `uname -a` + `cat /etc/os-release`
- `getcap $(readlink -f mirror_logger/.venv/bin/python)`
- `ip -br link; ethtool -k eth0 | grep -E 'gro|lro|tso|gso'`
- `sysctl net.core.rmem_max net.core.netdev_max_backlog`
- `journalctl -u mirror-logger.service -n 200 --no-pager`
- Configurazione: `cat mirror_logger/config/{default,user}.json`
- Stato API: `curl -H "X-Auth-Token: $TOKEN" .../api/status`

---

## 21. Live-data bridge: il KBM mostra i frame del mirror_logger nella UI

**Cosa**: lo stesso flusso del bus mirror catturato dal `mirror_logger` su
`:30490` viene mostrato nella UI Live Traffic del KBM con badge `mirror`
nell'etichetta del frame.

### Come funziona

Il KBM (`kvaser_bus_manager`) ha un nuovo modulo
[`mirror_udp_listener.py`](../kvaser_bus_manager/backend/mirror_udp_listener.py)
che apre un socket **UDP** su `:30490` **in parallelo** al socket AF_PACKET
del `mirror_logger`. Su Linux i due tipi di socket ricevono entrambi tutti
i pacchetti dello stesso flusso senza interferirsi (AF_PACKET sniffa a
livello link, UDP riceve a livello transport).

Per ogni pacchetto UDP ricevuto:
1. Lo `MirrorParser` (riusato dal `mirror_logger` via import) estrae i frame
   AUTOSAR ISO 23150 / VAG SOME/IP / IronBird / RawCAN.
2. Per ogni `RawFrame`, viene chiamato
   `BusManager.inject_frame(channel_id, arb_id, data, frame_type,
   capture_origin='mirror')`.
3. `inject_frame` fa:
   - decode via DBC/ARXML/FIBEX
   - notify ai listener (inclusi quelli del Sentinel)
   - emit `socketio.emit('bus_data_batch', [...])` al frontend KBM
4. Il frontend [`app.js`](../kvaser_bus_manager/frontend/static/js/app.js)
   riceve via `socket.on('bus_data_batch')`, riconosce `capture_origin ==
   'mirror'` e mostra il badge "mirror" accanto al canale.

### Attivazione

```ini
# nel systemd unit kvbm.service (o variabili env del KBM)
Environment=KBSM_MIRROR_LISTEN_ENABLED=1     # abilita listener
Environment=KBSM_MIRROR_LISTEN_PORT=30490    # porta UDP (default 30490)
Environment=KBSM_MIRROR_LISTEN_HOST=0.0.0.0  # '127.0.0.1' per loopback only
Environment=KBSM_LIVE_TRAFFIC_ENABLE=1       # abilita emit live al frontend
```

Riavvia: `sudo systemctl daemon-reload && sudo systemctl restart kvbm.service`.

### Quando ha senso usarlo

- ✅ **Quando il `mirror_logger` è la sorgente unica del bus mirror** e
  vuoi che la UI del KBM mostri comunque i frame in tempo reale.
- ✅ **Quando il Sentinel deve "vedere" i frame del bus mirror** per
  rilevare il MIL su bus mirror-only (FlexRay, CAN gateway). I frame
  iniettati passano attraverso `_process_frame` → notify listener →
  Sentinel `_update_lamps_from_frame`.
- ⚠️ **Non serve** se il KBM ha già `EthernetCapture` (scapy) attivo,
  perché vedrebbe doppi frame.

### Validazione end-to-end (test eseguito 2026-05-23)

Setup VM ARM64 profilo `pi5` (2 vCPU + 50 MB/s + 2k IOPS), 30 minuti di
stress continuo (replay LB63X loop):

| Metrica KBM | Valore |
|---|---|
| Frame emessi al frontend (`bus_data_batch`) | **857.170** |
| Drop al frontend | **0** |
| Mix per tipo | CAN 758k / FlexRay 70k / LIN 28k |
| Memoria KBM dopo 15 min stress | stabile, no leak |
| Memoria mirror_logger dopo 15 min stress | stabile, no leak |

Verifica della doppia capture senza interferenza:
- mirror_logger AF_PACKET vede i pacchetti UDP → MF4 (731k frame, drop=0)
- MirrorUDPListener UDP socket vede gli stessi → KBM emit (857k frame, drop=0)
- Differenza nei numeri = artefatti del parser su pacchetti di confine
  (atteso, il MirrorParser è stateless per-pacchetto). **Nessun frame
  perso da nessuno dei due**.

### Statistiche del listener

Il listener espone:

```python
listener.stats() → {
    'running': bool,
    'port': int,
    'host': str,
    'pkts_received': int,
    'frames_emitted': int,
    'parse_errors': int,
    'inject_errors': int,
    'pps': float,
    'fps': float,
    'last_recv_ts': float,
}
```

(per consumare le stats da REST API serve aggiungere un endpoint dedicato —
non ancora implementato).

---

## 20. Configurazione raccomandata: Sentinel `passive` + mirror_logger 24/7

**Scenario in produzione**: vuoi il `mirror_logger` sempre attivo (logging
continuo del bus mirror), il Sentinel del KBM sempre acceso (cattura incidenti
MIL/spie), e nessun conflitto. Questa è la config validata:

### Lato KBM (`kvaser_bus_manager/config/user.json`)

```jsonc
{
  "sentinel_enabled": true,
  "sentinel_diagnostic_mode": "passive",   // ← niente poll attivo, ascolto puro
  "lamp_mappings": {
    "mil": {
      "message": "Motor_01",                 // adatta al tuo veicolo
      "signal":  "OBD_MIL_Status",
      "incident_kind": "mil",
      "debounce_ms": 500
    },
    "epc": { "message": "Motor_01", "signal": "EPC_Lamp", ... },
    ...
  }
}
```

Il Sentinel intercetta la transizione `OFF→ON` del bit MIL nei frame
broadcast (ciclici, ~10-100 ms a seconda del veicolo) e triggera un incident
**senza interrogare nessuno**.

### Lato mirror_logger (`mirror_logger/config/user.json`)

```jsonc
{
  "auto_start_capture": true,
  "auto_activate_mirror": true,           // ← se il gateway non è già configurato
  "interface":   "eth0",
  "gateway_ip":  "192.168.0.140",
  "mirror_dest_ip": "192.168.0.100"
}
```

### Cross-process bridge env (KBM systemd unit)

```ini
[Service]
Environment=MIRROR_LOGGER_INCIDENT_URL=http://127.0.0.1:5050/api/incident/snapshot
Environment=MIRROR_LOGGER_INCIDENT_WINDOW_S=45
Environment=MIRROR_LOGGER_INCIDENT_TIMEOUT_S=5
# MIRROR_LOGGER_TOKEN solo se il mirror_logger richiede auth diversa
```

### Cosa succede in vettura

1. Boot: il `mirror_logger` parte autonomamente, attiva il gateway via DoIP
   (WriteDID 0x096F), inizia a loggare l'MF4
2. KBM parte, il Sentinel installa i listener su `bus_manager` (canlib) e
   `ethernet_manager` (KBM `EthernetCapture`, se attivo)
3. Le ECU broadcastano i frame del bus motore (incluso bit MIL); il
   `BusManager` decodifica e passa al Sentinel via listener
4. Se il MIL passa OFF→ON → Sentinel triggera incident → chiama il bridge
   `POST /api/incident/snapshot` sul mirror_logger
5. `mirror_logger` forza flush + hard-link dei file MF4 della finestra in
   `logs/incident_<session>_mil_on_<ts>/` → restituisce manifest
6. Il KBM include `mirror_snapshot` nel dict dell'incident; il report HTML
   ha il link al bundle mirror

**Nessun lock contended, zero traffico DoIP generato dal Sentinel, mirror
logging continuo senza interruzioni.**

---

## 19. Cross-process bridge Sentinel ↔ mirror_logger

Quando il **Sentinel del KBM** rileva un incident (MIL ON, spia EPC/gearbox)
ed il `mirror_logger` sta loggando in parallelo come servizio separato, il
Sentinel chiama automaticamente l'endpoint
`POST http://127.0.0.1:5050/api/incident/snapshot` per congelare la
finestra di mirror corrispondente. I file MF4 vengono hard-linked
(atomici, zero-copy) in una directory dedicata `logs/incident_<session>_<label>/`
del mirror_logger.

### 19.1 Configurazione via systemd `kvbm.service`

Aggiungi nel unit (`/etc/systemd/system/kvbm.service`):

```ini
[Service]
Environment=MIRROR_LOGGER_INCIDENT_URL=http://127.0.0.1:5050/api/incident/snapshot
Environment=MIRROR_LOGGER_TOKEN=<token-del-mirror-logger>
Environment=MIRROR_LOGGER_INCIDENT_WINDOW_S=45
Environment=MIRROR_LOGGER_INCIDENT_TIMEOUT_S=5
```

Il token va recuperato dal mirror_logger:

```bash
cat ~/TRC_Onboard/mirror_logger/config/.token
```

Riavvia: `sudo systemctl daemon-reload && sudo systemctl restart kvbm.service`.

### 19.2 Disabilitare il bridge

Se non vuoi che il Sentinel chiami il mirror_logger (es. nei test):

```ini
Environment=MIRROR_LOGGER_INCIDENT_URL=
```

L'incident del Sentinel completa comunque, semplicemente senza i frame mirror.

### 19.3 Verifica funzionamento

```bash
# 1. Avvia entrambi i servizi
sudo systemctl start mirror-logger.service kvbm.service

# 2. Avvia una sessione mirror
TOKEN=$(cat ~/TRC_Onboard/mirror_logger/config/.token)
curl -X POST -H "X-Auth-Token: $TOKEN" http://localhost:5050/api/start

# 3. Simula un MIL incident
KBM_TOKEN=$(cat ~/TRC_Onboard/kvaser_bus_manager/config/.token 2>/dev/null)
curl -X POST -H "X-Auth-Token: $KBM_TOKEN" \
    http://localhost:5000/api/experimental/simulate_mil_on

# 4. Verifica che il mirror_logger abbia creato lo snapshot
ls -la ~/TRC_Onboard/mirror_logger/logs/incident_*/
cat ~/TRC_Onboard/mirror_logger/logs/incident_*/manifest.json

# 5. Apri il report dell'incident nel KBM: dovrebbe mostrare il path mirror
curl -H "X-Auth-Token: $KBM_TOKEN" \
    http://localhost:5000/api/experimental/last_incident | jq .mirror_snapshot
```

Atteso: `manifest.json` con `strategy: {link: N, copy: 0}` e
`mirror_snapshot` non vuoto nell'output del KBM.

### 19.4 Combinare i due bundle MF4 (post-incident)

Per ottenere un MF4 unico che combina i frame del Sentinel (canlib) e del
mirror_logger:

```python
from asammdf import MDF
sentinel_mf4 = MDF('~/TRC_Onboard/kvaser_bus_manager/logs/incident_<ts>.mf4')
mirror_parts = sorted(Path('~/TRC_Onboard/mirror_logger/logs/incident_<id>_<label>/').glob('*.mf4'))
mirror_mf4 = MDF.concatenate(mirror_parts) if len(mirror_parts) > 1 else MDF(str(mirror_parts[0]))
combined = MDF.concatenate([sentinel_mf4, mirror_mf4])  # se sono compatibili
combined.save('incident_full.mf4', overwrite=True)
```

### 19.5 Limiti noti

- Il bridge **richiede sessione mirror attiva**. Se il mirror_logger non sta
  loggando al momento dell'incident, il manifest è vuoto e l'incident del
  Sentinel ha solo i dati canlib.
- La finestra è **retroattiva**: `incident_at - window_s`. Non c'è
  pre-buffer separato nel mirror_logger; lo snapshot prende i chunk MF4
  già scritti su disco. Il `force_flush` materializza l'ultimo chunk al
  momento della chiamata.
- Cross-host: l'URL può puntare a un mirror_logger remoto (es. su un
  secondo Pi). In quel caso `MIRROR_LOGGER_INCIDENT_TIMEOUT_S` va alzato
  e i file restano sul Pi del mirror_logger (no trasferimento automatico).

---

## Riferimenti incrociati

- Codice: [`mirror_logger/capture.py`](../mirror_logger/capture.py), [`mirror_logger/raw_logger.py`](../mirror_logger/raw_logger.py), [`mirror_logger/mirror_parser.py`](../mirror_logger/mirror_parser.py), [`mirror_logger/doip_activator.py`](../mirror_logger/doip_activator.py)
- Guida operativa modulo: [`mirror_logger/GUIDA.md`](../mirror_logger/GUIDA.md)
- Reliability / retention: [`mirror_logger/reliability.py`](../mirror_logger/reliability.py)
- VM di sviluppo ARM64: `/Volumes/Elements/qemu-arm64/README_DEV.md`
- Installer modulo: [`mirror_logger/install.sh`](../mirror_logger/install.sh)
- Installer KBM (sopravvive): [`kvaser_bus_manager/install/install.sh`](../kvaser_bus_manager/install/install.sh)
