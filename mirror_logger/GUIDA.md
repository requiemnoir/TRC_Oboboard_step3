# Guida operativa - mirror_logger

Sistema standalone di logging per **AUTOSAR Bus Mirror** ricevuto da un gateway
ECU. Cattura il flusso UDP/TCP via `AF_PACKET + BPF`, decodifica i payload
nei 4 formati mirror principali (AUTOSAR ISO 23150, VAG SOME/IP, IronBird,
RawCAN) e scrive su file MF4 strutturati per asammdf.

Nessuna interfaccia Kvaser, nessun DBC: solo mirror perfetto con timestamp
nanosecondi dal kernel.

---

## 1. Requisiti

| Componente | Versione | Note |
|---|---|---|
| Python    | >= 3.10  | testato con 3.12 |
| OS prod   | Linux (kernel >= 5.x) | Pi 4/5, Debian, Ubuntu |
| OS dev    | macOS / Windows | parte in modalita' FakeCapture |
| RAM       | >= 1 GB libera | buffer di cattura 16 MB + queue MF4 |
| Privilegi | `CAP_NET_RAW` o root | per AF_PACKET su Linux |

Pacchetti Python: solo `flask`, `asammdf`, `numpy`. Niente Scapy, niente
libpcap.

---

## 2. Installazione

### 2.1 Installazione automatica (consigliata)

```bash
cd mirror_logger
chmod +x install.sh
./install.sh                     # installa solo il software
./install.sh --systemd           # installa anche il service systemd
./install.sh --no-cap            # salta setcap (userai sudo a runtime)
./install.sh --port 8080         # cambia porta UI
```

Lo script:

1. verifica Python >= 3.10
2. crea `.venv/` locale
3. installa i requirements
4. su Linux esegue `setcap cap_net_raw,cap_net_admin=eip` sul python del venv
   (cosi' `app.py` parte senza sudo)
5. genera un token random in `config/.token` (chmod 600)
6. opzionalmente registra il service systemd

### 2.2 Installazione manuale

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Linux: capability per non usare sudo
sudo setcap cap_net_raw,cap_net_admin=eip $(readlink -f .venv/bin/python)

# token API
python -c "import secrets; print(secrets.token_urlsafe(32))" > config/.token
chmod 600 config/.token
```

---

## 2.3 Accesso rapido a questa istanza

Leggi il token locale direttamente dal file generato in installazione:

```bash
cat config/.token
```

L'interfaccia web resta accessibile su `http://127.0.0.1:5050`, ma tutte le
API richiedono il token nel campo UI dedicato oppure nell'header HTTP
`X-Auth-Token`. Non usare token in query string o URL condivisi.

---

## 3. Configurazione

File: `config/default.json` (caricato all'avvio, salvato in `config/user.json`).

| Chiave | Default | Descrizione |
|---|---|---|
| `interface`        | `eth0`  | NIC su cui ascoltare il mirror |
| `mirror_dest_port` | `30490` | porta UDP destinazione del Bus Mirror |
| `gateway_ip`       | `192.168.0.140` | IP del gateway (per attivazione DoIP) |
| `doip_port`        | `13400` | porta TCP DoIP standard |
| `target_address`   | `0x1234` | logical address ECU |
| `source_address`   | `0x0E00` | tester address |
| `pcap_enabled`     | `false` | salva anche un .pcap raw accanto all'MF4 |
| `chunk_seconds`    | `30`    | rotazione chunk MF4 |

Modifica via UI -> tab "Config" oppure editando direttamente `config/user.json`.

---

## 4. Avvio

### 4.1 Manuale

```bash
source .venv/bin/activate
export MIRROR_LOGGER_TOKEN="$(cat config/.token)"
python app.py
```

UI: <http://localhost:5050>

Header da inviare alle API: `X-Auth-Token: <token>`

### 4.2 Systemd (Linux prod)

```bash
sudo systemctl start mirror-logger
sudo systemctl status mirror-logger
sudo journalctl -u mirror-logger -f
```

### 4.3 Modalita' Fake (sviluppo macOS/Windows)

Si attiva automaticamente fuori da Linux, oppure:

```bash
MIRROR_FAKE=1 python app.py
```

Genera ~200 frame/s su 6 ID rotanti per testare UI e logger senza HW.

---

## 5. Workflow tipico in vettura

1. Collega il Pi al gateway (cavo automotive ethernet o switch).
2. Verifica IP statico Pi e gateway (`ip addr`, `ping 192.168.0.140`).
3. Apri UI nel browser del laptop.
4. Premi **"Attiva Mirror"** -> esegue routing activation DoIP + UDS WriteDID
   `0x096F` per abilitare il Bus Mirror lato gateway.
5. Premi **"Start Logging"** -> apre socket AF_PACKET, BPF kernel-side,
   inizia a scrivere `logs/<session_id>/part_*.mf4`.
6. A fine missione: **"Stop"** -> chiude e flusha l'ultimo chunk.
7. Scarica le sessioni dal tab "Sessions".

---

## 6. Output

Struttura file:

```
logs/
  20260513_142301_abc12/        # session_id = ts + suffix random
    part_000.mf4
    part_001.mf4
    ...
    cap_20260513_142301_abc12.pcap   # solo se pcap_enabled
```

Schema MF4 (asammdf 7+):

| Signal | Tipo | Note |
|---|---|---|
| `timestamp_ns` | u8 | timestamp kernel SO_TIMESTAMPNS |
| `timestamp_pkt` | u8 | timestamp dal payload mirror (se presente) |
| `frame_type`   | u1 | 0=CAN, 1=CANFD, 2=LIN, 3=FlexRay, 4=Eth |
| `channel_id`   | u2 | 100+net=CAN, 200+net=FR, 150+net=LIN |
| `arb_id`       | u4 | CAN ID / FR slot |
| `flags`        | u1 | bit0=ext, bit1=rtr, bit2=brs, bit3=esi |
| `dlc`          | u1 | data length |
| `payload`      | u1 [N x 64] | **un solo signal 2D, non 64 colonne** |

Per estrarre con asammdf:

```python
from asammdf import MDF
mdf = MDF('part_000.mf4')
ts  = mdf.get('timestamp_ns').samples
ids = mdf.get('arb_id').samples
pl  = mdf.get('payload').samples   # shape (N, 64)
dlc = mdf.get('dlc').samples
# Esempio: tutti i frame di ID 0x0FD
mask = ids == 0x0FD
for t, d, n in zip(ts[mask], pl[mask], dlc[mask]):
    print(t, d[:n].tobytes().hex())
```

---

## 7. Troubleshooting

| Sintomo | Causa | Fix |
|---|---|---|
| `PermissionError: AF_PACKET` | mancano capability | `sudo setcap cap_net_raw,cap_net_admin=eip $(readlink -f .venv/bin/python)` |
| `OSError: [Errno 19] No such device` | NIC sbagliata | `ip link` e correggi `interface` in config |
| 0 frame/s | mirror non attivo o filtro VLAN | controlla "Attiva Mirror" e che il gateway risponda 0x6E (UDS positive) |
| Drop rate > 1% | buffer pieno / CPU satura | aumenta `SO_RCVBUF` in `capture.py`, oppure abbassa `chunk_seconds` |
| `401 Unauthorized` su API | token mancante | header `X-Auth-Token` o env `MIRROR_LOGGER_TOKEN` |
| FakeCapture all'avvio anche su Pi | non Linux *o* `MIRROR_FAKE=1` | `unset MIRROR_FAKE`, verifica `uname -s` |

Log applicativi: stdout (con systemd vai con `journalctl -u mirror-logger -f`).

---

## 8. API REST

Tutte sotto `/api/*`, header `X-Auth-Token` obbligatorio.

| Metodo | Endpoint | Descrizione |
|---|---|---|
| GET    | `/api/status`            | stato cattura, pps, chunk corrente |
| GET    | `/api/config`            | leggi config attiva |
| POST   | `/api/config`            | aggiorna (debounce 500ms su disco) |
| POST   | `/api/mirror/activate`   | DoIP routing + UDS WriteDID 0x096F |
| POST   | `/api/mirror/deactivate` | spegne mirror lato gateway |
| POST   | `/api/start`             | apre socket e inizia logging |
| POST   | `/api/stop`              | chiude e flusha |
| GET    | `/api/sessions`          | lista sessioni su disco |
| GET    | `/api/sessions/<id>/<file>` | download MF4 / pcap |

---

## 8.bis Integrazione live-UI col `kvaser_bus_manager`

Per far apparire i frame del bus mirror anche nella **UI Live Traffic del
KBM** (con badge "mirror" nel canale):

Sul KBM (lato `kvaser_bus_manager`) attiva il listener UDP nuovo:

```ini
# /etc/systemd/system/kvbm.service (override)
Environment=KBSM_MIRROR_LISTEN_ENABLED=1
Environment=KBSM_LIVE_TRAFFIC_ENABLE=1
```

Il KBM apre un socket UDP su :30490 in **parallelo** alla cattura AF_PACKET del
`mirror_logger`. Su Linux i due socket ricevono entrambi i pacchetti senza
conflitto. Il KBM decodifica con DBC/ARXML/FIBEX caricati e fa emit
`socketio('bus_data_batch')` al frontend.

**Risultato**: stesso stream del `mirror_logger` MF4 visibile in tempo reale
nella UI del KBM. Per il Sentinel del KBM significa anche che i frame del bus
mirror passano attraverso `_update_lamps_from_frame` → può rilevare MIL/spie
su bus mirror-only (FlexRay, CAN gateway), non solo sui bus canlib diretti.

Verifica:

```bash
curl -fsS http://localhost:5000/api/bus/mirror_listener_stats | jq
# atteso: {ok:true, enabled:true, stats:{pkts_received, frames_emitted, ...}}
```

---

## 9. Performance attesa (Pi 4, AF_PACKET)

| Metrica | Valore |
|---|---|
| Latenza primo frame | ~150 ms |
| Throughput sostenuto | > 50.000 frame/s |
| Overhead BPF kernel | ~0 us (filtro JIT) |
| Overhead parsing user | ~3 us/pkt |
| Overhead append MF4  | ~1 us/frame |
| Drop rate normale    | < 0.001% |
| Flush chunk 30 s     | < 80 ms |

---

## 10. Disinstallazione

```bash
sudo systemctl disable --now mirror-logger 2>/dev/null
sudo rm -f /etc/systemd/system/mirror-logger.service
sudo systemctl daemon-reload
rm -rf .venv logs/*
```

I dati in `logs/` non vengono mai cancellati automaticamente.
