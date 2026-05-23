# Kvaser Bus Manager

A complete automotive bus management system for Raspberry Pi using Kvaser interfaces.

## Features
- **Protocols:** CAN (Classic/FD) & FlexRay
- **Hardware:** Kvaser USB/Ethernet interfaces
- **Decoding:** DBC (CAN) & FIBEX (FlexRay)
- **Logging:** CSV, JSON, TXT
- **UI:** Real-time Web Dashboard (Dark Mode)
- **DBC Catalog:** Browse DBC messages/signals + descriptions (comments)
- **Copilot (local LLM):** Virtual guide via Ollama (optional)
- **ScanTools:** OBD/Diagnostics actions + Live Data (RPM/Speed) from the Web UI

## Installation

### One-command install (recommended on a fresh Raspberry Pi)

On a new Raspberry Pi (Raspberry Pi OS / Debian-based):

```bash
sudo apt-get update
sudo apt-get install -y git git-lfs

git clone https://github.com/requiemnoir/TRC_Onboarda_Setinel_V1.git
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
git lfs install
chmod +x install/install.sh
./install/install.sh
```

This will:
- Install apt dependencies
- Create a Python virtualenv at `.venv` and install `requirements.txt`
- (By default) install Kvaser drivers + best-effort CAN/FlexRay setup
- Install and start `kvbm.service` + the optional healthcheck timer

After install, open: `http://<raspberry-pi-ip>:5000`

### Fully reproducible install (Pi 5 recommended)

This produces a predictable, repeatable setup (systemd service + DBC catalog pre-import + local Copilot via Ollama):

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
./install/install.sh -y --full --import-dbc-catalog --with-copilot
```

Post-install validation:

```bash
./install/validate_reproducible_install.sh
```

### DBC Catalog (messages + descriptions)

- UI page: `http://<raspberry-pi-ip>:5000/dbc_catalog`
- This can import `.dbc` files into a persistent SQLite db for fast lookup:
   - DB path: `logs/monitor/dbc_catalog.db`
   - API import: `POST /api/dbc/import` (supports `{"import_all": true}`)

### Copilot (local LLM, optional)

To run the onboard Copilot without internet (recommended on Raspberry Pi 5), install Ollama and pull the model:

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
chmod +x install/setup_ollama_pi5.sh
./install/setup_ollama_pi5.sh

```

## Sentinel (incidenti MIL/EPC) – note operative

Per operatività “lungo viaggio” (ore con spia accesa) sono disponibili:

- `scan_rate_limit_s` (throttle scans)
- `sentinel_llm_breaker_*` (circuit breaker LLM)
- `logs_retention_*` (quota/retention logs)

Stato live: `GET /api/experimental/status`.

## Test (pytest)

```bash
cd TRC_Onboarda_Setinel_V1
kvaser_bus_manager/.venv/bin/python -m pytest -q kvaser_bus_manager/tests
```

## Vehicle Mirror (MLBevo) — demo without a vehicle

The MLBevo gateway mirror stream arrives as **SOME/IP over UDP** (Service `0x02FD`, Method/Event `0xF302`).
To validate the UI + backend pipeline without having the car connected, you can replay a captured UDP payload.

### 1) Replay a captured SOME/IP mirror payload to localhost

The repo includes a golden sample payload used for regression tests:

- `kvaser_bus_manager/tests/data/vag_mirror_single_payload.bin`

Start the backend (systemd service or `backend/app.py`), then replay at a chosen rate:

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
python3 scripts/replay_someip_mirror_udp.py \
   --sample tests/data/vag_mirror_single_payload.bin \
   --host 127.0.0.1 --port 30490 --pps 50
```

This exercises the real parsing path:
`EthernetCapture -> SOME/IP detection -> VAG resync parser (classic CAN only) -> BusManager -> UI`.

### 2) Generate a real MF4 trace from a PCAP

If you have a PCAP containing SOME/IP mirror packets (UDP payloads starting with `02fd f302 ...`), you can generate
a minimal but valid MF4 artifact:

```bash
cd TRC_Onboarda_Setinel_V1/kvaser_bus_manager
python3 scripts/generate_mf4_from_pcap.py --pcap /path/to/mirror.pcap --out ./mirror_trace.mf4
```
```

Copilot UI: `http://<raspberry-pi-ip>:5000/copilot`

### Reproducibility / assets

- Runtime config is local: `config/app_config.json` is generated on first run from `config/app_config.example.json`.
- Vehicle databases (DBC/FIBEX) and PDX projects are intentionally not versioned by default (often proprietary/large).
- YOLO weights (`*.pt`) are not committed; if you enable YOLO and have internet access, `ultralytics` can download weights automatically, otherwise place the model file under `backend/` or set `CAM_YOLO_MODEL`.

Docs:
- Install: `../docs/INSTALL.md`
- Manual: `../docs/MANUAL.md`

If you want to skip driver installation (e.g., running without Kvaser hardware):

```bash
./install/install.sh --no-drivers
```

### Manual install (legacy)

1. **Install Drivers:**
   ```bash
   cd install
   chmod +x install_kvaser_drivers.sh
   ./install_kvaser_drivers.sh
   # REBOOT AFTER THIS STEP
   ```

2. **Install Python Dependencies:**
   ```bash
   pip3 install -r requirements.txt
   ```

3. **Run Application:**
   ```bash
   cd backend
   python3 app.py
   ```
   Access UI at: `http://<raspberry-pi-ip>:5000`

## Auto-Start (Systemd)

The recommended and supported way is via the installer, which renders and installs `kvbm.service`:

```bash
cd install
sudo ./install_autostart_systemd.sh
systemctl status kvbm.service --no-pager
```

## Watchdog (Recommended for in-vehicle use)

There are two complementary layers:

1) **Service watchdog (app-level)**: restarts `kvbm.service` if the HTTP API stops responding.
2) **Hardware watchdog (system-level)**: reboots the Raspberry Pi if the OS/kernel hangs.

### 1) Service watchdog (restart on app hang)

Installs a systemd timer that runs every ~15s and restarts `kvbm.service` if `/api/config` is not reachable.

```bash
cd install
chmod +x install_kvbm_healthcheck_systemd.sh
sudo ./install_kvbm_healthcheck_systemd.sh
```

Check:

```bash
systemctl status kvbm-healthcheck.timer --no-pager
journalctl -u kvbm-healthcheck.service -f
```

### 2) Hardware watchdog (reboot on system freeze)

Enables the Raspberry Pi SoC watchdog via `dtparam=watchdog=on` and the `watchdog` daemon.

```bash
cd install
chmod +x enable_hw_watchdog_rpi.sh
sudo ./enable_hw_watchdog_rpi.sh
sudo reboot

```

## MF4: Recommended workflow (raw capture + post-decode)

For best performance on Raspberry Pi (lowest CPU/RAM + smaller files), record MF4 as **raw frames only** and decode using DBC **after** acquisition.

- Two modes:
   - Raw-only MF4 (small, best for Raspberry Pi): `MF4_INCLUDE_DECODED=0`
   - MF4 with DBC-decoded channels (bigger files): `MF4_INCLUDE_DECODED=1`
- If you change the systemd drop-in (or add it after the service is already running), apply it with:
   - `sudo systemctl daemon-reload`
   - `sudo systemctl restart kvbm.service`
- Post-decode helper:
   - `scripts/decode_mf4_to_csv.py` decodes MF4 raw frames using one or more DBC files and writes a CSV.
   - Example:
      - `.venv/bin/python scripts/decode_mf4_to_csv.py --mf4 logs/session_YYYYmmdd_HHMMSS_part0000.mf4 --dbc databases/dbc/your.dbc --out decoded.csv`
```

## Usage

1. Connect Kvaser device via USB.
2. Open Web UI.
3. Select Interface and Protocol.
4. (Optional) Upload DBC or FIBEX file.
5. Click **Start Bus**.

### Live Traffic CPU Tuning

The live dashboard now keeps the same functionality while reducing unnecessary CPU load by batching UI updates and moving heavy camera work off the capture thread.

Environment variables:

- `KBSM_UI_BUS_EMIT_INTERVAL_S=0.10`
   - Batch CAN/FlexRay live frames for up to 100 ms before emitting to the browser.
- `KBSM_UI_BUS_EMIT_BATCH_MAX=64`
   - Emit sooner when this many bus frames are queued.
- `KBSM_UI_ETH_EMIT_INTERVAL_S=0.10`
   - Batch Ethernet live packets for up to 100 ms before emitting to the browser.
- `KBSM_UI_ETH_EMIT_BATCH_MAX=48`
   - Emit sooner when this many Ethernet packets are queued.
- `CAM_MOTION_FPS=4.0`
   - Limit motion detection evaluation frequency without disabling motion triggers.

Notes:

- Set either emit interval to `0` to disable batching and go back to per-message UI events.
- Single-event Socket.IO messages are still supported; the frontend now also accepts batched events.
- YOLO remains functionally unchanged, but inference runs on a dedicated worker thread so MJPEG capture stays responsive.

### DoIP (UDS over IP) and IPv6 gateways

ScanTools includes a DoIP action (`vag_doip_scan_report`) which connects to the vehicle/gateway on TCP/13400.

Defaults (recommended for VAG-like setups):
- Discovery prefers IPv6 multicast first, then falls back to IPv4 broadcast.

Notes for IPv6 link-local gateways:
- If the gateway IP is link-local (starts with `fe80::`), the connection usually needs a zone-id.
- You can pass `gateway_iface` (example: `eth0`) and discovery will return e.g. `fe80::1234%eth0`.

Environment variables:
- `KBSM_DOIP_DISCOVERY_PREFER_IPV6=1` (default) to prefer IPv6 discovery.
- `KBSM_DOIP_DISCOVERY_IPV6_MCAST=ff02::1` to override the IPv6 multicast address used for discovery.

### ECU Simulation (Dev/Test)

To test ScanTools/Live Data without a real vehicle/ECU, you can enable the built-in ECU simulator:

- Environment variable: `KBSM_SIM_ECU=1`
- Or send `"simulate_ecu": true` in the JSON body of `POST /api/start`
