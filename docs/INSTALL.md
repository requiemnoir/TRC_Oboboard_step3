# Installazione su Raspberry Pi (fresh)

## Prerequisiti

- Raspberry Pi OS (Debian-based) consigliato.
- Accesso SSH.
- (Opzionale) Interfaccia Kvaser collegata via USB.
- Git LFS (necessario per scaricare alcuni database DBC/FIBEX inclusi nel repo).

## Installazione (consigliata)

```bash
sudo apt-get update
sudo apt-get install -y git git-lfs

git clone https://github.com/requiemnoir/TRC_Onboarda_Setinel_V1.git
cd TRC_Onboarda_Setinel_V1
git lfs install
./install.sh
```

### Installazione 100% riproducibile (consigliata su Pi 5)

Per ottenere un sistema identico (service systemd + import catalogo DBC + Copilot locale):

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
./install/install.sh -y --full --import-dbc-catalog --with-copilot
```

Verifica automatica (post-install):

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
./install/validate_reproducible_install.sh
```

### Opzioni

- Senza driver Kvaser:

```bash
./install.sh --no-drivers
```

- Non interattivo:

```bash
./install.sh -y
```

- Watchdog hardware (richiede reboot):

```bash
./install.sh --hw-watchdog
sudo reboot
```

## Verifica

1) Servizio:

```bash
systemctl status kvbm.service --no-pager
```

2) Log:

```bash
journalctl -u kvbm.service -f
```

3) UI:

Aprire `http://<ip_raspberry>:5000`

Display status page: `http://<ip_raspberry>:5000/display`

Su Raspberry Pi con desktop grafico, l'installer configura anche un autostart che:
- rileva quando un monitor e' collegato
- apre la pagina `/display` in kiosk fullscreen sui piccoli display da veicolo
- usa una finestra normale sui monitor piu' grandi, cosi' puoi uscire dal fullscreen sulla workstation

Timeline Viewer (pagina dedicata): `http://<ip_raspberry>:5000/timeline`

Nota: se aggiorni il repo (git pull) e una pagina nuova risulta “Not Found”, riavvia il servizio:

```bash
sudo systemctl restart kvbm.service
```

## Catalogo DBC (messaggi + descrizioni)

La UI include una pagina dedicata per esplorare i DBC e le descrizioni (commenti):

- Pagina: `http://<ip_raspberry>:5000/dbc_catalog`

Questa pagina può importare i `.dbc` in un DB SQLite persistente per ricerche veloci.
Il DB viene salvato in `logs/monitor/dbc_catalog.db`.

Se aggiungi nuovi file `.dbc` in `kvaser_bus_manager/databases/dbc/`, usa il pulsante **Importa/aggiorna** dalla UI oppure via API:

```bash
curl -sS -H 'Content-Type: application/json' \
	-d '{"import_all":true,"include_signals":true}' \
	http://127.0.0.1:5000/api/dbc/import
```

## Copilot (LLM locale con Ollama)

Se vuoi la guida virtuale offline (modello locale), su Raspberry Pi 5 puoi installare Ollama e il modello consigliato con:

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
chmod +x install/setup_ollama_pi5.sh
./install/setup_ollama_pi5.sh

```

## Sentinel (MIL/EPC incidenti): robustezza per uso “lungo viaggio”

Le pipeline incidenti fanno scan + (opzionale) LLM + report HTML. Per evitare blocchi e saturazioni durante guida prolungata:

- `scan_rate_limit_s`: rate limit globale scans (evita “tempeste”).
- `sentinel_llm_breaker_*`: circuit breaker LLM (se provider è giù/busy, salta analisi per un periodo di cooldown).
- `logs_retention_*`: cleanup automatico per evitare disco pieno.

Questi stati sono visibili in `GET /api/experimental/status` sotto `status.scan`, `status.sentinel_breaker`, `status.logs_retention`.

## Test (pytest)

Il repo include una suite minima in `kvaser_bus_manager/tests/`.

Esecuzione da root progetto:

```bash
cd TRC_Onboarda_Setinel_V1
kvaser_bus_manager/.venv/bin/python -m pytest -q kvaser_bus_manager/tests
```

Oppure (grazie a `tests/conftest.py`) puoi lanciare anche da dentro `kvaser_bus_manager/`:

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
./.venv/bin/python -m pytest -q
```
```

Questo script:
- installa/abilita il servizio `ollama`
- scarica il modello `llama3.2:3b`
- configura `kvbm.service` (drop-in systemd) con le variabili Copilot

UI Copilot: `http://<ip_raspberry>:5000/copilot`

## Note su asset non inclusi nel repo

Per rendere il repository condivisibile, alcuni file tipicamente **proprietari** o **molto grandi** non sono versionati:

- Database veicolo: `**/databases/dbc/*` e `**/databases/fibex/*` (vedi i rispettivi README nelle cartelle).
- Progetti diagnostici PDX: `kvaser_bus_manager/projects/pdx/*`.
- Log/capture: `*.mf4`, `*.pcap`, ecc.
- Pesi YOLO: `*.pt` (se usi il trigger YOLO, `ultralytics` può scaricare i pesi al primo avvio se c'è connettività; in alternativa metti `yolov8n.pt` in `kvaser_bus_manager/backend/` o imposta `CAM_YOLO_MODEL`).

Il sistema parte anche senza questi asset, ma alcune funzioni (decodifica DBC/FIBEX, PDX, YOLO offline) richiedono che tu li aggiunga localmente.
