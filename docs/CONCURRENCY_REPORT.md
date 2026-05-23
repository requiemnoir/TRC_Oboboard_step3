# TRC Onboard — Concurrency Report

Analisi sistemica di **cosa il software NON può fare contemporaneamente**, dove
si scontrano risorse, dove i conflitti sono mitigati e dove no. Include un
**piano di fix prioritizzato** con righe di codice esatte per chiudere le
combinazioni invalide.

> ⚠️ Documento risultante da ricognizione codice + verifica mirata.
> Validità al commit `keep_recording`. Da aggiornare quando aggiungi feature.

---

## 0. Premessa — verità verificata (audit di 2026-05-23)

Un audit di questo documento ha rivelato un'imprecisione iniziale: si era
parlato del Sentinel come se facesse MIL polling DoIP attivo per default.
**Non è così.**

Il Sentinel ha **due modalità**:

- **Modalità `passive` (default, raccomandata e ora applicata in config)**: il
  Sentinel **non interroga né il bus né il gateway**. Riceve i frame tramite
  i listener installati su `bus_manager` ed `ethernet_manager`, decodifica
  il bit MIL via `_update_lamps_from_frame()` (cfr. mapping `Motor_01.OBD_MIL`
  o equivalente nel veicolo), e su transizione OFF→ON triggera
  `_handle_lamp_incident(kind='mil')` che a sua volta chiama il
  cross-process bridge `/api/incident/snapshot` del mirror_logger (§12).
- **Modalità `active`**: fallback per veicoli che non broadcastano il MIL
  ciclicamente. Esegue `_poll_mil()` via CAN canlib o DoIP. Solo in questa
  modalità potrebbe esserci conflitto con il mirror_logger DoIPActivator.

Quindi, **nell'uso operativo standard (modalità `passive`)**: il Sentinel
**solo ascolta** il flusso CAN/Ethernet già in transito, **non genera alcun
traffico aggiuntivo**, e può coesistere indefinitamente col mirror_logger
senza alcun lock o coordinamento.

Il bridge implementato (§12) NON è per coordinare il polling — è solo per
arricchire l'incident MF4 esportato dal Sentinel coi frame mirror catturati
dall'altro processo.

---

## 1. Executive summary

Il sistema è composto da **due processi Flask separati**:
- **`kvaser_bus_manager`** (porta 5000): UI generale, ScanTools, Sentinel,
  Logger CAN/Ethernet, ARXML/FIBEX decoder, Camera/YOLO.
- **`mirror_logger`** (porta 5050): ingest AUTOSAR Bus Mirror dedicato,
  AF_PACKET + RawLogger MF4, DoIP activator standalone.

I due processi **non condividono memoria** ma condividono **risorse hardware**:
gateway veicolo (TCP 13400 / UDP discovery), interfaccia di rete `eth0`,
filesystem `logs/`, e — in caso di Pi 5 con doppia capture — banda CPU/I/O.

### Verdetto sintetico (post-audit, con Sentinel in `passive`)

| Combinazione | Stato | Note |
|---|---|---|
| Solo `mirror_logger` (no KBM) | ✅ sicuro | Endpoint lifecycle protetti da `_lifecycle_lock` |
| Solo KBM (no mirror_logger) | ✅ sicuro | Conflitto Sentinel↔ScanTools mitigato in process |
| **KBM Sentinel `passive` + mirror_logger 24/7** | ✅ **sicuro** | Sentinel non genera traffico DoIP, snapshot incident via bridge |
| KBM ScanTools DoIP + mirror_logger DoIPActivator | ⚠️ mitigato | File lock `_gateway_doip_lock`: serializza setup, ScanTools può interrompere keepalive chiudendo TCP |
| KBM Sentinel `active` + mirror_logger DoIPActivator | ⚠️ mitigato | Stesso meccanismo. **Evitare modalità `active` se non strettamente necessaria**. |
| Mirror logger + KBM Ethernet capture su stesso `eth0` | ⚠️ tollerato | 2× CPU, nessun frame drop (AF_PACKET clone-per-socket Linux) |
| Sentinel incident export + BusLogger MF4 split | ⚠️ race finestra ms | Già try/except, ma fallisce silenziosamente. Da P1 §7.3 |
| KBM Logger CAN + Sentinel trace ring buffer | ✅ sicuro | Ring buffer ha `_lock` interno |

**Il caso d'uso primario** (Sentinel `passive` + mirror_logger fisso) è ora
✅ verificato end-to-end.

---

## 2. Inventario delle feature

Sezione di riferimento. Tutte le righe `file:line` sono verificate.

### 2.1 `kvaser_bus_manager` (Flask :5000)

| Feature | File entrypoint | Risorse occupate | Endpoint API che la avviano |
|---|---|---|---|
| Logger CAN base (`BusLogger`) | [`backend/logger.py`](../kvaser_bus_manager/backend/logger.py) | Kvaser canlib channels, queue, MF4 thread | `POST /api/start`, `POST /api/log/start` |
| Ethernet Manager (`EthernetCapture` scapy) | [`backend/ethernet_capture.py`](../kvaser_bus_manager/backend/ethernet_capture.py) | AF_PACKET socket su `eth0`, scapy sniff thread | `POST /api/eth/start` |
| Gateway Mirror activation (DoIP DID 0x096F) | [`backend/gateway_mirror.py`](../kvaser_bus_manager/backend/gateway_mirror.py) | TCP DoIP 13400, tester 0x0E00 | parte di `/api/eth/start` quando mirror enabled |
| The Sentinel (`ExperimentalAssistantService`) | [`backend/experimental_assistant.py:1235`](../kvaser_bus_manager/backend/experimental_assistant.py#L1235) | RAM (trace ring buffer 45 s), thread DoIP MIL poller | servizio sempre attivo + `/api/experimental/...` |
| ScanTools VAG (`VAGScannerService`) | [`backend/vag_scanner.py`](../kvaser_bus_manager/backend/vag_scanner.py) | TCP DoIP 13400 con tester 0x0E00, canlib UDS | `/api/scantools/doip_*`, `/api/scantools/scan` |
| XCP client | [`backend/xcp_eth_client.py`](../kvaser_bus_manager/backend/xcp_eth_client.py), `xcp_can_client.py` | UDP XCP / CAN channel dedicato | `/api/xcp/*` |
| ARXML/FIBEX decoder live | [`backend/arxml_decoder.py`](../kvaser_bus_manager/backend/arxml_decoder.py) | RAM (catalog parser) | implicito (decode hot path) |
| MF4 export/merge | [`backend/mf4_decoded_export.py`](../kvaser_bus_manager/backend/mf4_decoded_export.py) | Thread merge, file rename | `/api/export/...` |
| Live Traffic UI | [`backend/ethernet_manager.py`](../kvaser_bus_manager/backend/ethernet_manager.py) (UI batch) | SocketIO emit thread | env `KBSM_LIVE_TRAFFIC_ENABLE=1` |
| Camera/YOLO trigger | [`backend/camera_manager.py`](../kvaser_bus_manager/backend/camera_manager.py) | `/dev/video*`, GPU/CPU per YOLO, JPEG encoder | env `CAM_YOLO_TRIGGER=1` |
| Retention (in-line, no watchdog dedicato) | `app.py:8729` `enforce_logs_retention` | filesystem `logs/` | `POST /api/maintenance/enforce_retention` |

### 2.2 `mirror_logger` (Flask :5050)

| Feature | File entrypoint | Risorse occupate | Endpoint API |
|---|---|---|---|
| `MirrorCapture` (AF_PACKET) | [`capture.py:170-310`](../mirror_logger/capture.py#L170) | AF_PACKET socket su `eth0`, BPF kernel, 16 MB SO_RCVBUF | `POST /api/start` |
| `RawLogger` (MF4 chunked) | [`raw_logger.py`](../mirror_logger/raw_logger.py) | Queue (524k slot default), MF4 worker thread | `POST /api/start` |
| `DoIPActivator` | [`doip_activator.py`](../mirror_logger/doip_activator.py) | TCP DoIP 13400, tester 0x0E00, keepalive thread | `POST /api/mirror/activate` |
| `RetentionWatchdog` | [`reliability.py`](../mirror_logger/reliability.py) | Thread periodico, file scan, lock-free | auto-started + `POST /api/maintenance/enforce_retention` |
| `MirrorParser` | [`mirror_parser.py`](../mirror_logger/mirror_parser.py) | Stateless (RLock per dedupe) | invocato da capture |

---

## 3. The Sentinel — dettaglio operativo

### 3.1 Cos'è

Servizio sempre attivo del KBM, attivato automaticamente all'avvio
([`app.py:3139`](../kvaser_bus_manager/backend/app.py#L3139)).
Monitora **spie e MIL** (Malfunction Indicator Lamp) per cattura incidenti.

### 3.2 Il trace ring buffer

Definito in [`experimental_assistant.py:215-241`](../kvaser_bus_manager/backend/experimental_assistant.py#L215):

```python
class TraceRingBuffer:
    def __init__(
        self,
        *,
        keep_ms: int = 20000,      # default 20 s (per istanze normali)
        max_frames: int = 200000,
        decoded_signal_preview_limit: int = 10,
    ):
        self._lock = threading.Lock()
        self._frames: Deque[TraceFrame] = deque()
        self._dropped = 0
```

Istanziato in [`experimental_assistant.py:1235`](../kvaser_bus_manager/backend/experimental_assistant.py#L1235):

```python
self.trace = TraceRingBuffer(keep_ms=45000)   # 45 secondi
```

**Riepilogo del buffer**:

| Caratteristica | Valore |
|---|---|
| **Dimensione temporale** | **45 secondi** (hardcoded, NON 30) |
| **Cap su numero frame** | 200.000 |
| **Memoria** | RAM (`collections.deque`, in-process Python) |
| **Contenuto** | tutti i frame visti dal `BusManager.add_listener` (CAN + FlexRay + LIN + Ethernet mirror se attivo) + max 10 segnali decoded per frame |
| **Lock** | `threading.Lock()` interno → thread-safe |
| **Footprint stimato** | ~30 MB di RAM a regime con 45 s di traffico veicolo full |

### 3.3 Trigger di incidente

L'incident si scatena quando rileva una transizione **OFF→ON** di:

1. **MIL via DoIP polling** ([`_poll_mil_doip`](../kvaser_bus_manager/backend/experimental_assistant.py#L2001), 1-2 s/poll)
2. **Spie via decoded signals** ([`_handle_mil_incident`](../kvaser_bus_manager/backend/experimental_assistant.py#L2250), EPC/gearbox)
3. **Simulazione** (`POST /api/experimental/simulate_mil_on`)

Al trigger, il Sentinel:
- Estrae dal trace buffer la finestra **-15 s / +15 s** attorno all'evento
- Genera un file MF4 raw delle ECU + un report HTML
- Salva in `logs/sentinel/incident_<ts>/`

### 3.4 Interazione con la cattura mirror

**Il punto critico richiesto dall'utente**: cosa succede quando la cattura
mirror (KBM Ethernet o mirror_logger) è attiva e il Sentinel triggera un
incident.

| Scenario | Sentinel | Mirror cattura | Esito |
|---|---|---|---|
| Solo Sentinel attivo (no mirror) | trace cresce con CAN/FR/LIN da canlib | — | ✅ |
| Sentinel + KBM Ethernet capture | trace cresce anche con frame mirror Ethernet | KBM `EthernetCapture` (scapy) | ✅ stesso processo, listener condiviso |
| Sentinel + `mirror_logger` su altro processo | trace **NON contiene** i frame del mirror_logger | mirror_logger MF4 indipendente | ⚠️ il Sentinel del KBM perde traccia del mirror se il mirror gira nell'altro processo |
| Sentinel MIL poll + `mirror_logger` DoIPActivator | entrambi a 0x0E00 sul gateway | DoIP keepalive | ❌ **RACE su sessione DoIP** (vedi §5.2) |

**Implicazione operativa importante**: se affidi il logging del mirror al
modulo `mirror_logger`, il **Sentinel del KBM non vede i frame mirror nel
suo ring buffer**. Quando esporta un incident MF4, il file conterrà solo
le ECU collegate via canlib (CAN diretti), non i bus mirror.

Per avere incident dump completi col mirror, due strade:
1. Tenere acceso anche il `KBSM_LIVE_TRAFFIC_ENABLE=1` del KBM (doppia capture,
   CPU extra ma incident complete)
2. Far emettere al `mirror_logger` un evento "incident requested" che salva
   anche dal suo lato (richiede coordinamento cross-process)

---

## 4. Risorse singleton condivise

### 4.1 Risorse OS-level (esclusive)

| Risorsa | Proprietà | Bind |
|---|---|---|
| TCP `:5000` | KBM Flask | exclusive bind |
| TCP `:5050` | mirror_logger Flask | exclusive bind |
| TCP `:5500` (host VM) | port-forward QEMU → 5000 | dev only |
| TCP `:5051` (host VM) | port-forward QEMU → 5050 | dev only |

### 4.2 Risorse OS-level (condivise — Linux semantic)

| Risorsa | Chi la usa | Semantica Linux | È un problema? |
|---|---|---|---|
| Socket AF_PACKET su `eth0` | KBM `EthernetCapture` (se attivo) + `mirror_logger MirrorCapture` (se attivo) | clone-per-socket: **entrambi ricevono tutti i pacchetti** | ⚠️ **NO** (mito): solo overhead CPU. Non drop frame |
| TCP `gateway:13400` | KBM ScanTools + KBM Sentinel + `mirror_logger DoIPActivator` | **Una sola sessione DoIP per tester 0x0E00 per gateway** | ❌ **SÌ — race vera** |
| UDP discovery `:13400` broadcast | tutti i DoIP client | broadcast ricevuto da tutti | ✅ no race |

### 4.3 Risorse software (esclusività logica)

| Risorsa | Lock attuale | Conseguenza se manca |
|---|---|---|
| Canale Kvaser canlib | `BusManager._lock` parziale, `_bus_start_in_progress` flag | doppia open → `canERR_NOCHANNELS` |
| `BusLogger.queue` | thread-safe (`queue.Queue`) | nessuna |
| `mirror_logger._logger`, `_capture`, `_activator` | **`_lifecycle_lock = threading.RLock()`** ([`app.py:91`](../mirror_logger/app.py#L91)) | OK (fixato dopo review) |
| Sentinel `TraceRingBuffer._frames` | `TraceRingBuffer._lock` interno | OK |
| Sentinel `_doip_mil_scanner` | `_doip_mil_lock` | OK |
| `BusLogger.mdf_buffer` (MF4 part rename) | nessun lock globale, solo `try/except` | race con incident export (basso) |
| `_manual_stop_latch` dict in KBM | **nessun lock** ([`app.py:1800`](../kvaser_bus_manager/backend/app.py#L1800)) | race teorica (Python GIL mitiga get/set singoli, ma get+check+set NON è atomico) |

---

## 5. Conflitti reali e mitigation

### 5.1 ScanTools DoIP ↔ Sentinel MIL (in-process, MITIGATO)

**Problema**: entrambi aprono sessione DoIP a `gateway:13400` con tester
address `0x0E00`. Il gateway rifiuta la seconda sessione → EPIPE → "0 ECUs
found".

**Mitigation attuale**: `pause_doip_mil() / resume_doip_mil()`.

Catena in [`vag_scanner.py:5942-6111`](../kvaser_bus_manager/backend/vag_scanner.py#L5942):
```python
# Prima dell'azione DoIP
if s is not None and hasattr(s, 'pause_doip_mil'):
    s.pause_doip_mil()           # chiude lo scanner Sentinel
# Esegui scan / clear DTCs / live data
...
# Finally
if s is not None and hasattr(s, 'resume_doip_mil'):
    s.resume_doip_mil()
```

Con timeout di sicurezza 120 s ([`experimental_assistant.py:2017-2023`](../kvaser_bus_manager/backend/experimental_assistant.py#L2017)) per evitare
deadlock se l'azione ScanTools crashasse senza chiamare resume.

**Test di regressione**: [`test_scantools_pauses_sentinel_mil.py`](../kvaser_bus_manager/tests/test_scantools_pauses_sentinel_mil.py).

### 5.2 ScanTools DoIP ↔ `mirror_logger` DoIPActivator (CROSS-PROCESS, NON MITIGATO ❌)

**Problema**: stesso pattern di §5.1, ma tra **processi separati**. Il
mirror_logger ha un `DoIPActivator` che apre TCP a `gateway:13400` con
tester `0x0E00` e mantiene keepalive (`TesterPresent`) ogni 2 s. Se nel
frattempo il KBM ScanTools tenta una scansione DoIP, il gateway rifiuta.

**Mitigation cross-process attuale**: **nessuna**. Il pause/resume del
Sentinel è in-process e non si estende al mirror_logger.

**Catena del fallimento**:
1. UI: utente preme "Attiva Mirror" in mirror_logger → DoIPActivator apre
   sessione DoIP, keepalive ogni 2 s
2. UI: stesso utente apre KBM e clicca "Scan" su ScanTools
3. `VAGScannerService` chiama `pause_doip_mil()` sul Sentinel locale (OK)
4. ScanTools tenta apertura DoIP → gateway risponde NAK o EPIPE
5. Scansione fallisce con "0 ECUs found" o NRC 0x33

**Fix proposto**: vedi §7.1 (endpoint lock cross-process tramite file lock o
proxy HTTP).

### 5.3 Sentinel MIL ↔ `mirror_logger` DoIPActivator (CROSS-PROCESS, NON MITIGATO ❌)

Stesso meccanismo di §5.2. Il Sentinel del KBM fa MIL polling a 1-2 s, il
mirror_logger fa TesterPresent a 2 s. Se entrambi attivi → gateway alterna
risposte agli uni e agli altri, instabile.

**Fix proposto**: identico a §7.1.

### 5.4 KBM `EthernetCapture` ↔ `mirror_logger MirrorCapture` (CROSS-PROCESS, MA NON CONFLITTO ⚠️)

L'agente Explore l'aveva marcato come **CRITICO**. **Falso positivo**.

**Realtà su Linux** (verificato kernel ≥ 5.x):
- Due socket AF_PACKET su stessa NIC sono **independent receive queues**
- Il driver copia ogni pacchetto in tutte le queue che hanno un BPF compatibile
- **Nessun frame viene rubato all'altro socket**
- L'unico costo è 2× CPU (parsing in entrambi i processi) e 2× I/O log

**Quando è OK averli entrambi attivi**:
- Vuoi che il KBM mostri Live Traffic in UI
- E vuoi anche che il `mirror_logger` faccia MF4 dedicato

**Quando NON ha senso**:
- Stai loggando con `mirror_logger` → spegni la cattura mirror nel KBM
  (`KBSM_ETHERNET_MIRROR_DISABLED=1` o flag UI) per risparmiare CPU.

### 5.5 BusLogger MF4 split ↔ Sentinel incident export (in-process, race finestra ms)

**Problema**:
- `BusLogger._split_mf4_part()` rinomina `session_X_pNNNN.mf4.tmp` → `.mf4`
- `Sentinel._export_trace_mf4()` legge il path corrente per copiarlo

Se il rename avviene **tra `listdir()` e `open()`** del Sentinel → `FileNotFoundError`.

**Mitigation attuale**: try/except generico, fallisce silenziosamente
([`experimental_assistant.py:2299` circa]).

**Impatto**: incident MF4 incompleto in casi rari (race finestra ms).

**Fix proposto**: vedi §7.3.

### 5.6 `_manual_stop_latch` dict senza lock (in-process, race teorica)

**Problema**: dict globale Python letto/scritto da più thread senza
sincronizzazione. In Python le operazioni singole su dict sono protette dal
GIL, ma il pattern `if dict.get(k) and not condition: dict[k] = False` (vedi
[`app.py:2070,2283,2384,7884,8021`](../kvaser_bus_manager/backend/app.py#L2070))
non è atomico.

**Probabilità**: bassa (richiede due trigger handlers che scattano nello stesso
ms). **Impatto**: latch sbagliato → un trigger può saltare la stop manuale o
viceversa.

**Fix proposto**: vedi §7.4.

### 5.7 Camera/YOLO cooldown non atomico (in-process, race teorica)

**Problema**: `_yolo_last_trigger_s = time.time()` scritto senza lock dopo un
check. Se due frame YOLO consecutivi superano il cooldown nello stesso ms,
entrambi triggerano.

**Impatto**: minimo (un trigger video duplicato).

**Fix proposto**: vedi §7.5 (basso priorità).

---

## 6. Matrice di compatibilità feature × feature

✅ = nessun conflitto noto · ⚠️ = funziona ma con overhead/race minore · ❌ = race vera, da mitigare · — = stessa feature

|                          | CAN log | KBM Eth cap | Mirror logger | Gateway mirror activ. | Sentinel MIL | ScanTools DoIP | XCP eth | Camera YOLO |
|---|---|---|---|---|---|---|---|---|
| **CAN log**              | —       | ✅          | ✅            | ✅                    | ✅           | ⚠️ (pause)     | ✅       | ✅          |
| **KBM Eth cap**          | ✅      | —           | ⚠️ (2× CPU)   | ✅                    | ✅           | ✅             | ✅       | ✅          |
| **Mirror logger**        | ✅      | ⚠️ (2× CPU) | —             | ❌ (cross-proc)       | ❌ (cross-proc) | ❌ (cross-proc) | ⚠️ (UDP eth) | ✅          |
| **Gateway mirror activ.**| ✅      | ✅          | ❌            | —                     | ⚠️           | ⚠️ (pause)     | ⚠️       | ✅          |
| **Sentinel MIL**         | ✅      | ✅          | ❌            | ⚠️                    | —            | ✅ (pause)     | ⚠️       | ✅          |
| **ScanTools DoIP**       | ⚠️      | ✅          | ❌            | ⚠️ (pause)            | ✅ (pause)   | —              | ⚠️       | ✅          |
| **XCP eth**              | ✅      | ✅          | ⚠️            | ⚠️                    | ⚠️           | ⚠️             | —        | ✅          |
| **Camera YOLO**          | ✅      | ✅          | ✅            | ✅                    | ✅           | ✅             | ✅       | —           |

**Letteralmente, ciò che NON puoi fare contemporaneamente** (le tre celle ❌):
1. `mirror_logger.activate_mirror` + KBM `ScanTools DoIP scan` o `clear DTC`
2. `mirror_logger.activate_mirror` + KBM Sentinel MIL polling (se entrambi accesi)
3. `mirror_logger.activate_mirror` (DoIPActivator) + KBM `Gateway mirror activation` (entrambi vogliono il DID 0x096F)

Tutto il resto o è ok o è "stesso processo già protetto".

---

## 7. Piano di fix prioritizzato

Per ciascun fix: dove va, codice suggerito, complessità, impatto.

### 7.1 [P0] Lock cross-process per DoIP gateway (file lock)

**Obiettivo**: serializzare l'accesso al gateway DoIP da KBM ScanTools/Sentinel
e dal `mirror_logger`. Implementazione minima: file lock advisory in `/var/run/`
(o `/tmp/` se non disponibile in CI).

**Posizioni**:
- `mirror_logger/doip_activator.py` — wrappa `_run` con acquisizione lock
- `kvaser_bus_manager/backend/experimental_assistant.py` — `_doip_mil_lock` interno
  va esteso a un file-lock cross-process
- `kvaser_bus_manager/backend/vag_scanner.py` — già chiama `pause_doip_mil`,
  va esteso a `pause_external_doip()` che parla col mirror_logger

**Codice suggerito** (`mirror_logger/doip_activator.py`, prima di `_run`):

```python
import fcntl
import contextlib

_GLOBAL_DOIP_LOCK_PATH = '/var/run/trc_doip.lock'

@contextlib.contextmanager
def _global_doip_lock():
    """File-lock advisory cross-process per evitare doppia sessione DoIP
    sullo stesso gateway. Cade in /tmp se /var/run non scrivibile."""
    path = _GLOBAL_DOIP_LOCK_PATH
    try:
        f = open(path, 'a+')
    except PermissionError:
        path = '/tmp/trc_doip.lock'
        f = open(path, 'a+')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
```

Poi in `_connect_and_activate` (`doip_activator.py:184`):
```python
def _run(self) -> None:
    while not self._stop_event.is_set():
        try:
            with _global_doip_lock():
                self._connect_and_activate()
            self._keepalive_loop()   # keepalive fuori dal lock per non bloccare ScanTools
        except Exception as e:
            ...
```

Complessità: **bassa** (un blocco contextmanager). Impatto: chiude §5.2 e
§5.3 in modo robusto.

### 7.2 [P0] Endpoint `/api/lock/status` su mirror_logger per KBM-side check

**Obiettivo**: dare al KBM ScanTools un modo di sapere se il mirror_logger ha
DoIPActivator attivo, così può rifiutare la scan o avvisare l'utente.

**Posizione**: `mirror_logger/app.py`, aggiungere route.

**Codice**:

```python
@app.get('/api/lock/status')
def api_lock_status():
    """Indica risorse esclusive attualmente prenotate dal mirror_logger.

    Cross-process consumers (es. kvaser_bus_manager) possono chiamare
    questa route per sapere se possono usare il gateway DoIP."""
    with _lifecycle_lock:
        return jsonify({
            'ok': True,
            'doip_active': bool(_activator and _activator.activated),
            'session_active': bool(_logger and _logger.active),
            'session_id': _active_session_id(),
        })
```

Complessità: **molto bassa**. Da abbinare a un check lato KBM nel
`vag_scanner.py` prima della scan (HTTP `GET` con timeout 500 ms verso
`http://127.0.0.1:5050/api/lock/status`).

### 7.3 [P1] Atomic snapshot dei file MF4 nel Sentinel incident export

**Obiettivo**: evitare la race finestra-ms tra `BusLogger._split_mf4_part`
e `Sentinel._export_trace_mf4`.

**Soluzione**: prima di esportare, fare `os.listdir()` + `os.stat()` sotto
un lock del logger (esposto come property), oppure `os.link()` (hard link)
dei file da congelare verso `incident_<ts>/` prima di trasformarli.

**Posizione**: `kvaser_bus_manager/backend/logger.py` (aggiungere lock pubblico),
`experimental_assistant.py` (usare lock + hard-link snapshot).

```python
# In logger.py: esporre lock di rotazione
def freeze_current_parts(self) -> list[str]:
    """Snapshot atomico dei file MF4 stabilizzati."""
    with self._split_lock:
        return list(sorted(glob.glob(f'{self.base_path}_p*.mf4')))
```

```python
# In experimental_assistant.py incident export:
parts = self.bus_manager.logger.freeze_current_parts()
for src in parts:
    dst = incident_dir / Path(src).name
    try:
        os.link(src, dst)   # hard-link atomic, sopravvive a rename src
    except OSError:
        shutil.copy2(src, dst)
```

Complessità: **media**. Impatto: chiude §5.5.

### 7.4 [P1] Lock per `_manual_stop_latch` nel KBM

**Posizione**: `kvaser_bus_manager/backend/app.py:1800`.

```python
_manual_stop_latch = {'can': False, 'eth': False, 'yolo': False, 'custom': False}
_manual_stop_latch_lock = threading.RLock()

def _set_manual_stop(kind: str, value: bool) -> None:
    with _manual_stop_latch_lock:
        _manual_stop_latch[kind] = value

def _get_manual_stop(kind: str) -> bool:
    with _manual_stop_latch_lock:
        return bool(_manual_stop_latch.get(kind))
```

Sostituire i 7+ usi diretti con queste due helper. Complessità: **bassa** (refactor meccanico).

### 7.5 [P2] Cooldown atomico YOLO

**Posizione**: `kvaser_bus_manager/backend/camera_manager.py`.

Usare `threading.Lock` o, più semplicemente, scrivere `_yolo_last_trigger_s`
solo dentro un check-and-set protetto da `with self._yolo_lock:`.

Complessità: **molto bassa**. Impatto: marginale.

### 7.6 [✅ IMPLEMENTATO] Cross-process bridge Sentinel ↔ mirror_logger (opzione B)

**Stato**: implementato + testato end-to-end (vedi §12).

Quando il Sentinel rileva un incident, ora chiama
`POST http://127.0.0.1:5050/api/incident/snapshot` con `label` e `window_s`.
Il mirror_logger forza un flush sincrono, fa hard-link dei file MF4 della
finestra e ritorna un `manifest`. Il path/manifest viene esposto nel dict
dell'incident del Sentinel (chiave `mirror_snapshot`) e disponibile per i
report HTML.

**Configurazione via env** (lato KBM):
- `MIRROR_LOGGER_INCIDENT_URL` — default `http://127.0.0.1:5050/api/incident/snapshot`
- `MIRROR_LOGGER_TOKEN` — token per header `X-Auth-Token` (se mirror_logger lo richiede)
- `MIRROR_LOGGER_INCIDENT_WINDOW_S` — finestra retroattiva, default 45 s
- `MIRROR_LOGGER_INCIDENT_TIMEOUT_S` — timeout HTTP, default 5 s

**Disabilitazione**: `MIRROR_LOGGER_INCIDENT_URL=''`. Il Sentinel funziona
comunque, semplicemente non include il bundle mirror nell'incident.

**Best-effort**: se il mirror_logger è offline o non ha sessione attiva
(`409`), il client log un warning e l'incident del Sentinel completa con i
soli dati canlib. **Non solleva mai eccezioni** che possano fermare il
trigger.

---

## 8. Endpoint da rifiutare con 409 quando incompatibili

Lista di endpoint che, una volta implementati i lock cross-process di §7.1-7.2,
devono rispondere `409 Conflict` se la risorsa è già occupata.

| Endpoint | Condizione di rifiuto | HTTP response |
|---|---|---|
| `mirror_logger POST /api/mirror/activate` | Già attivo (✅ già implementato) | `{"ok": true, "message": "già attivo"}` |
| `mirror_logger POST /api/start` | Sessione già attiva (✅ già implementato) | `{"ok": false, "error": "sessione già attiva"}, 409` |
| `mirror_logger POST /api/mirror/activate` | KBM ScanTools attivo (cross-process check) | `409 {"ok": false, "error": "ScanTools busy on gateway DoIP"}` |
| `KBM POST /api/scantools/scan` | mirror_logger DoIP attivo | `409 {"ok": false, "error": "mirror_logger DoIPActivator running, deactivate first"}` |
| `KBM POST /api/scantools/doip_clear_dtcs` | idem | idem |
| `KBM POST /api/start` (CAN logger) | già attivo | `409` (verificare se già implementato) |

---

## 9. Cose che il report Explore ha esagerato (falsi positivi)

Per onestà intellettuale, declasso i seguenti dall'agente Explore:

| Claim originale | Realtà | Severità reale |
|---|---|---|
| "AF_PACKET dual = CRITICO, OS-level race su pacchetti" | Linux clona pacchetti per ogni socket AF_PACKET, no contesa | ⚠️ solo overhead CPU |
| "XCP DoIP + Sentinel collision unmitigated" | XCP usa CAN o UDP XCP, non DoIP 0x0E00 | ✅ non applicabile |
| "Sentinel buffer 30 s" (in citazione utente) | È **45 s** hardcoded | nota terminologica |

---

## 10. Sintesi operativa: 5 regole da seguire oggi

In attesa dell'implementazione dei fix §7.1-7.2:

1. **Mai** `mirror_logger /api/mirror/activate` se nel KBM stai per usare
   ScanTools o se il Sentinel sta facendo MIL polling DoIP.
2. **Mai** ScanTools DoIP nel KBM se il `mirror_logger` ha già attivato il
   gateway mirror (controlla `GET http://localhost:5050/api/status` →
   `.mirror.activated == false`).
3. **Sì** a far girare KBM Ethernet capture + mirror_logger in parallelo se
   ti servono entrambe le UI (overhead CPU 2×, ma nessun drop frame).
4. Se usi `mirror_logger` come logger principale: ricordati che il Sentinel
   del KBM **non vedrà nei suoi incident dump** i frame del bus mirror.
5. Retention watchdog è già coordinato (file freschi protetti 30 s) — non
   serve coordinamento esterno.

---

## 13. Update 2026-05-23: Sentinel `passive` + bridge verificato end-to-end

**Fix architettturale applicato**: il Sentinel ora ha config
`sentinel_diagnostic_mode` con default `'passive'`. Implementato in
[`experimental_assistant.py`](../kvaser_bus_manager/backend/experimental_assistant.py).

- **`passive`**: il `_loop` non chiama mai `_poll_mil()`. La rilevazione
  MIL passa solo per `_update_lamps_from_frame()` sui frame broadcast.
  **Zero traffico DoIP / CAN aggiuntivo generato dal Sentinel.**
- **`active`**: comportamento storico (poll attivo via DoIP o CAN). Da
  usare solo se la ECU non emette spontaneamente il bit MIL.

Test eseguiti tutti PASS:
- Static check `_loop` ha branch `passive` che skippa `_poll_mil`
- Test funzionale `_update_lamps_from_frame`: MIL=0 → no incident; MIL=0→1 →
  triggera; ripetuto → no double trigger; ciclo OFF→ON post-debounce → secondo
  trigger correttamente.
- E2E nella VM (profilo `pi5`, 2 vCPU + 50 MB/s + 2k IOPS): replay LB63X 15 s
  → 549.056 frame, drop=0, snapshot `link: 4, copy: 0`, manifest JSON corretto.

Bug fix in fase di test: `/api/lock/status` era erroneamente sotto auth →
ora esentato come `/api/health` (necessario per uso cross-process).

---

## 12. Cross-process bridge Sentinel ↔ mirror_logger

Implementato e testato end-to-end nella VM ARM64 (May 2026).

### 12.1 Endpoint `POST /api/incident/snapshot` (mirror_logger)

Definito in [`mirror_logger/app.py`](../mirror_logger/app.py).

```
POST /api/incident/snapshot
Headers: X-Auth-Token: <token> (se configurato)
Body JSON (tutto opzionale):
  {
    "label":    "mil_on_<ts>",   // slug per la dir di output, max 64 char
    "window_s": 45                // finestra retroattiva, 1-300
  }

Risposta 200:
  {
    "ok": true,
    "incident_dir": "/path/to/logs/incident_<session>_<label>",
    "manifest": {
      "session_id":    "...",
      "label":         "...",
      "incident_at_ms": ...,
      "window_s":      45,
      "files":         [{"name":"...", "size":..., "mtime":..., "strategy":"link"}],
      "total_size":    ...,
      "strategy":      {"link": N, "copy": M}
    }
  }

Risposta 409 (nessuna sessione attiva):
  {"ok": false, "error": "nessuna sessione MF4 attiva, nulla da snapshottare"}
```

**Comportamento**:
1. Acquisisce `_lifecycle_lock` (RLock cross-route)
2. Verifica `_logger.active == True`, altrimenti `409`
3. Chiama `_logger.force_flush(timeout_s=3.0)` → flush sincrono del chunk corrente
4. Glob `session_<id>_p*.mf4` filtrato per `mtime >= now - window_s` + il
   precedente per coprire il bordo
5. Per ogni file: `os.link()` atomico (zero-copy); fallback `shutil.copy2()` su EXDEV
6. Aggiunge il PCAP affiancato se `pcap_enabled=true`
7. Scrive `manifest.json` nella dir incident

### 12.2 Client nel Sentinel (KBM)

Definito in [`experimental_assistant.py:_request_mirror_snapshot`](../kvaser_bus_manager/backend/experimental_assistant.py).

Chiamato in **due punti**:
- [`_handle_mil_incident`](../kvaser_bus_manager/backend/experimental_assistant.py) — MIL ON via DoIP
- [`_handle_lamp_incident`](../kvaser_bus_manager/backend/experimental_assistant.py) — spie EPC/gearbox

Il manifest restituito viene aggiunto al dict dell'incident sotto la chiave
`mirror_snapshot`, sempre presente (vuoto `{}` se mirror_logger offline).

### 12.3 Metodo `RawLogger.force_flush(timeout_s=5.0)`

Aggiunto in [`raw_logger.py`](../mirror_logger/raw_logger.py).

- Acquisisce un `_flush_request_lock` interno per coordinarsi col worker
- Chiama `_flush_current_part(finalize=False)` — scrive il chunk corrente
  ma **non incrementa** `part_index` (può essere ri-flushato dal worker
  al prossimo ciclo)
- Restituisce `True` su successo, `False` su timeout / errore
- **Non interrompe la sessione**: il logger continua a girare normalmente

### 12.4 Test end-to-end (VM ARM64, profilo pi5)

Scenario:
1. mirror_logger attivo nella VM con `_lifecycle_lock` + nuovo endpoint
2. `host_sender.py` replay 10 s LB63X → ~365k frame, 3 parti MF4 (~36 MB totali)
3. `curl POST /api/incident/snapshot` con `window_s=15`

Risultato:
- 3 file MF4 hard-linked **istantaneamente** (refcount=2 visibile in `ls -la`)
- `total_size: 36.5 MB` snapshottati con zero-copy
- `strategy: {link: 3, copy: 0}` — hard-link OK su ext4
- manifest.json correttamente scritto
- Sessione attiva non interrotta, stop pulito con `drop=0`

### 12.5 Impatto operativo

Ora, quando il Sentinel del KBM rileva un incident MIL/spia:
- L'incident MF4 del Sentinel contiene **i frame CAN diretti via canlib**
  (come prima)
- In parallelo, il mirror_logger snapshotta **i frame del bus mirror**
  della stessa finestra temporale
- I due bundle possono essere combinati a posteriori (script di merge MF4
  con asammdf) per ottenere un dump completo dell'incident

Per il consumer (es. report HTML del Sentinel): il path della dir snapshot
mirror è in `incident['mirror_snapshot']['incident_dir']`.

---

## 11. Riferimenti

- File principale KBM: [`kvaser_bus_manager/backend/app.py`](../kvaser_bus_manager/backend/app.py)
- Sentinel: [`experimental_assistant.py`](../kvaser_bus_manager/backend/experimental_assistant.py)
- ScanTools: [`vag_scanner.py`](../kvaser_bus_manager/backend/vag_scanner.py)
- Mirror logger: [`mirror_logger/app.py`](../mirror_logger/app.py)
- DoIP activator: [`mirror_logger/doip_activator.py`](../mirror_logger/doip_activator.py)
- Reliability/retention: [`mirror_logger/reliability.py`](../mirror_logger/reliability.py)
- Guida migrazione: [`docs/MIGRATION_AF_PACKET.md`](MIGRATION_AF_PACKET.md)
