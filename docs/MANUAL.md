# TRC Onboard (Kvaser Bus Manager) — Manuale di funzionamento

Questo progetto fornisce un sistema “onboard” per Raspberry Pi che:
- acquisisce traffico automotive (CAN / FlexRay / Ethernet DoIP),
- decodifica con database (DBC / FIBEX),
- registra in log (CSV/TXT/JSON e MF4),
- offre una Web UI per Logger / ScanTools / MF4 Viewer / Impostazioni,
- supporta ScanTools VAG (CAN + DoIP) con report HTML ed export CSV/XML.

## 1) Modalità e cosa fanno

### Logger
- Visualizza traffico live.
- Avvia/ferma acquisizione manuale (Start/Stop Acquisition) senza trigger.
- Può essere usato insieme a trigger (YOLO/Motion/Custom/CAN/Ethernet) quando armati.

### ScanTools
- Esegue azioni diagnostiche (OBD/UDS) e genera report.
- Include VAG scan su CAN e scan DoIP quando disponibili.

### MF4 Viewer
- Visualizza file `.mf4` presenti nella cartella log.
- Permette selezione segnali e plot.

### Timeline Viewer
- Pagina dedicata: `http://<IP_RASPBERRY>:5000/timeline`
- Allinea MP4 (video sessione) e MF4 (segnali) per review e plotting sincronizzato.

### Impostazioni
- Configurazione generale (log dir, formati, trigger, health, progetti PDX, ecc.).

## 2) Avvio rapido

1. Collegare Raspberry a rete (e opzionalmente Kvaser via USB).
2. Avviare il servizio:
   - se installato con systemd: `systemctl status kvbm.service --no-pager`
   - in alternativa: `./install/run_kvaser_bus_manager.sh`
3. Aprire la UI: `http://<IP_RASPBERRY>:5000`

Se dopo un aggiornamento (git pull) la pagina Timeline risulta 404, riavvia `kvbm.service`:

`sudo systemctl restart kvbm.service`

## 3) Acquisizione (logging)

### Start/Stop manuale
- In modalità Logger, premere **Start Acquisition**.
- Per chiudere la sessione e finalizzare file/log, premere **Stop Acquisition**.

### Formati
I formati dipendono dalla configurazione e dalle dipendenze installate:
- CSV/TXT/JSON: disponibili in modo standard.
- MF4: richiede `numpy` + `asammdf`.

### MF4 “spezzettati”
Per tolleranza a power-loss, il sistema può scrivere MF4 a chunk (part). Alla chiusura della sessione, viene creato un MF4 consolidato unico e (se configurato) vengono eliminati i chunk.

## 4) Trigger (opzionali)

Il sistema può avviare la registrazione anche da trigger (quando armati) ad esempio:
- Motion / YOLO / Custom object (camera),
- Trigger CAN basato su DBC (condizione su segnale),
- Trigger Ethernet (primo pacchetto osservato).

Nota: lo Start/Stop manuale non dipende dai trigger.

## 5) ScanTools e report

### VAG Scan su CAN
- Esegue richieste UDS su CAN.
- Il sistema evita “falsi positivi” quando gira in modalità simulazione/mock (a meno di override esplicito lato env).

### VAG Scan DoIP
- Connessione su TCP/13400.
- Supporto discovery (IPv6 preferito, fallback IPv4) e handling di link-local con zone-id.

### Output
- Report HTML.
- Export automatico CSV/XML generato assieme all’HTML.

## 6) Cartelle e dati

- Log runtime: `kvaser_bus_manager/logs/` (o percorso configurato).
- Database:
  - CAN: DBC in `kvaser_bus_manager/databases/dbc/`
  - FlexRay: FIBEX in `kvaser_bus_manager/databases/fibex/`
- Progetti PDX: **non versionati**; vedi `kvaser_bus_manager/projects/pdx/README.md`.

## 7) Servizi (systemd)

Quando installato, sono previsti:
- `kvbm.service`: avvia l’app (Flask + Socket.IO).
- `kvbm-healthcheck.timer` (+ service): watchdog software che riavvia se la API non risponde.

Comandi utili:
- Stato: `systemctl status kvbm.service --no-pager`
- Log: `journalctl -u kvbm.service -f`
- Restart: `sudo systemctl restart kvbm.service`

## 8) Troubleshooting rapido

- UI non raggiungibile:
  - verificare servizio: `systemctl status kvbm.service --no-pager`
  - verificare porta 5000: `ss -lptn | grep 5000`
- MF4 non disponibile:
  - verificare dipendenze: `python -c "import numpy, asammdf"`
- CAN non invia:
  - assicurarsi driver Kvaser installati e device visibile.
