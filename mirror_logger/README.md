# mirror_logger

Logger autonomo per rete veicolo via AUTOSAR Bus Mirror.  
Nessuna decodifica online, nessuna AI, nessuna dipendenza Kvaser.

## Architettura

```
Gateway (ETH) ──UDP/30490──► MirrorCapture (AF_PACKET + BPF kernel)
                                     │
                              MirrorParser
                          (AUTOSAR / VAG / DoIP)
                                     │
                               RawLogger
                          (queue → MF4 chunked)
                                     │
                              logs/*.mf4
```

| Componente | File | Responsabilità |
|---|---|---|
| Parser payload | `mirror_parser.py` | decodifica 4 formati mirror, zero DBC |
| Logger MF4 | `raw_logger.py` | queue async, recarray numpy, chunk 30s |
| Cattura | `capture.py` | **AF_PACKET + BPF kernel** (Linux), FakeCapture (Mac/Win) |
| Attivatore DoIP | `doip_activator.py` | routing activation + UDS 0x2E DID 0x096F |
| Config | `config.py` | JSON atomico, debounce 500ms |
| Web UI | `app.py` + `frontend/` | Flask slim, polling 1s, no AI |

### Backend di cattura

- **Linux (Pi, prod)** → `MirrorCapture`: socket `AF_PACKET` con filtro BPF kernel-side (accetta solo IPv4 UDP/TCP), `SO_RCVBUF=16MB`, `SO_TIMESTAMPNS` per timestamp ns dal driver, parsing manuale Ethernet/IP/UDP/TCP. Throughput target: >50.000 frame/s.
- **macOS/Windows (dev)** → `FakeCapture`: generatore di frame fittizi a 200 fps configurabili, per testare UI e logger senza rete reale. Selezionato automaticamente se non Linux, oppure con env `MIRROR_FAKE=1`.

## Formato MF4

Ogni file `session_YYYYMMDD_HHMMSS_pNNNN.mf4` contiene:

| Canale | Tipo | Nota |
|---|---|---|
| `t` | float64 | epoch seconds (asse MDF, da ts_ns) |
| `ts_pkt` | float64 | Scapy packet.time (alta risoluzione kernel) |
| `ts_ns` | uint64 | **timestamp principale** ns epoch |
| `ch` | uint16 | 100+net=CAN, 200+net=FlexRay, 150+net=LIN |
| `bus_type` | uint8 | 1=CAN 2=CAN-FD 3=FlexRay 4=LIN |
| `arb_id` | uint32 | CAN Arb-ID / FlexRay Slot-ID |
| `flags` | uint32 | CAN flags / ciclo FlexRay |
| `dlc` | uint8 | lunghezza dati effettiva |
| `payload` | uint8[64] | payload raw zero-padded (1 sola colonna 2D) |

> Nota: il payload è un singolo canale con shape `(N, 64)` invece di 64
> colonne separate `dbN`. Drasticamente più veloce in scrittura
> (~30× su Pi 4) e perfettamente leggibile da `asammdf`, MDA, Vector CANape.

## Installazione

```bash
pip install -r requirements.txt   # solo flask + asammdf + numpy
```

`AF_PACKET` richiede `CAP_NET_RAW` (root o capability):

```bash
sudo python app.py
# oppure capability mirata (no root):
sudo setcap cap_net_raw,cap_net_admin=eip $(readlink -f $(which python3))
python app.py
```

Su macOS/Windows parte automaticamente in modalità Fake (UI funzionante, dati simulati).

## Sicurezza opzionale (token)

Se la variabile `MIRROR_LOGGER_TOKEN` è valorizzata, tutte le route `/api/*`
richiedono header `X-Auth-Token: <token>` (eccetto `/api/health`):

```bash
sudo MIRROR_LOGGER_TOKEN='supersegreto' python app.py
```

L'UI HTML rimane raggiungibile senza token; le chiamate JS devono
includere il token nei fetch via header (non in query string: i token
in URL finiscono nei log).

### Accesso API (token)

Genera un token locale (non committarlo):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > config/.token
chmod 600 config/.token
export MIRROR_LOGGER_TOKEN="$(cat config/.token)"
```

In UI/API usa **solo** header `X-Auth-Token`.

## Configurazione

Editare `config/default.json` o sovrascrivere tramite UI/API:

```json
{
  "interface":        "eth0",
  "gateway_ip":       "192.168.0.140",
  "mirror_dest_ip":   "192.168.0.100",
  "mirror_dest_port": 30490,
  "can_networks":     [1, 2, 3],
  "flexray_channels": ["A", "B"],
  "log_dir":          "logs",
  "chunk_interval_s": 30
}
```

## API REST

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/api/start` | Avvia sessione logging |
| `POST` | `/api/stop`  | Ferma sessione, flush finale |
| `GET`  | `/api/status`| Stats live (logger + capture + mirror) |
| `GET`  | `/api/sessions` | Lista file MF4 |
| `GET`  | `/api/sessions/<file>` | Download MF4 |
| `POST` | `/api/mirror/activate` | Attiva DoIP mirror |
| `POST` | `/api/mirror/deactivate` | Disattiva mirror |
| `GET`  | `/api/config` | Leggi config corrente |
| `POST` | `/api/config` | Aggiorna config (partial JSON) |

## Performance attesa (Pi 4, AF_PACKET)

| Metrica | Target |
|---|---|
| Latenza primo frame | ~150 ms |
| Throughput sostenuto | >50.000 frame/s |
| Overhead BPF kernel | ~0 µs (filtro JIT) |
| Overhead parsing userspace | ~3 µs/pkt |
| Overhead append MF4 | ~1 µs/frame |
| Drop rate normale | <0.001% |
| Chunk flush (30 s) | <80 ms |
