# TRC_Onboarda_Setinel_V1

Sistema onboard per Raspberry Pi dedicato a:
- acquisizione bus automotive (CAN/FlexRay/Ethernet),
- logging (CSV/TXT/JSON + MF4),
- Web UI (Logger / ScanTools / MF4 Viewer / Impostazioni) con branding **EV-Q Onboard Manager**,
- ScanTools VAG (CAN + DoIP) con report HTML + export CSV/XML.

Include anche **The Sentinel** (ex “Experimental”): monitor live spie/incidenti con export **TRACE & CTX** (ZIP con MF4 + report scan).

## Installazione (Raspberry nuovo)

### 1. Bootstrap base
```bash
sudo apt-get update
sudo apt-get install -y git git-lfs

git clone https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
git lfs install
git lfs pull                  # scarica ARXML/FIBEX/XML proprietari (~293 MB)
```

### 2. Installa l'applicazione TRC + driver Kvaser
```bash
./install.sh                  # delega a kvaser_bus_manager/install/install.sh
```
Configura: venv Python + dipendenze + driver Kvaser kernel + systemd service.

### 3. (Opzionale) Voce e LLM locale
```bash
# Piper TTS (italiano) + Whisper.cpp (STT)
sudo bash kvaser_bus_manager/install/install_voice.sh

# Ollama + Gemma per copilot LLM (richiede Internet)
sudo bash kvaser_bus_manager/install/setup_ollama_pi5.sh
```

### 4. Verifica finale
```bash
bash kvaser_bus_manager/install/verify_system.sh
```
Stampa stato di service, backend, autostart display, kiosk, mirror UDP, Kvaser kernel modules, Piper/Whisper, Ollama+Gemma, DBC/ARXML/FIBEX caricati, log directory.

### Endpoint utente
- **UI principale:** `http://<raspberry-ip>:5000`
- **Display kiosk:** `http://<raspberry-ip>:5000/display` (auto-launch su monitor connesso)
- **Timeline Viewer:** `http://<raspberry-ip>:5000/timeline`
- **Metrics Prometheus:** `http://<raspberry-ip>:5000/metrics`
- **Health aggregate:** `http://<raspberry-ip>:5000/api/health/aggregate`

## Cosa serve avere dopo `git lfs pull`

Per un sistema completamente funzionante (DBC decoding + FlexRay AUTOSAR):

| Categoria | Path | Provenienza |
|-----------|------|-------------|
| DBC CCAN/HCAN/DiagCAN | `kvaser_bus_manager/databases/dbc/` | nel repo, ~12 MB |
| FIBEX FlexRay XML | `kvaser_bus_manager/databases/fibex/` | LFS, ~70 MB |
| ARXML AUTOSAR | `kvaser_bus_manager/databases/arxml/` | LFS, ~222 MB |
| App config con gateway_mirror | `kvaser_bus_manager/config/app_config.example.json` | nel repo, da copiare a `app_config.json` |
| Driver Kvaser linuxcan | `install/kvaser_drivers_src/` | nel repo, source build |
| Modelli voce (Piper IT + Whisper) | `/opt/piper/`, `/opt/whisper.cpp/` | install_voice.sh li scarica |
| Modello LLM (Gemma) | `/opt/ollama/models/` | setup_ollama_pi5.sh li scarica |

## Configurazione gateway DoIP mirror

Per attivare il mirror della gateway veicolo (DID 0xF1A0), modifica
`kvaser_bus_manager/config/app_config.json` sezione `gateway_mirror`:

```json
{
  "gateway_mirror": {
    "enabled": true,
    "autostart": true,
    "dest_ip": "192.168.200.1",
    "dest_port": 30490,
    "gateway_ip": "fe80::200:ff:fe00:0%eth0",
    "target_addr": "0x4010",
    "tester_logical_address": "0x0E00",
    "target_bus": "ethernet",
    "can": [],
    "flexray": ["A"],
    "lin": []
  }
}
```

⚠️ **Importante**: lascia `"can": []` (vuoto) e abilita SOLO le reti effettivamente
popolate sul gateway target. Mettere `can:[1,2,3,4,5,6]` su un gateway che ne ha
meno fa fallire la DID write e il mirror non parte.

## Reproducibility

See [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) for pinned dependencies, included runtime assets (DBC/FIBEX/config/model), and how to create an optional full snapshot archive.
## Documentazione

- Install: [docs/INSTALL.md](docs/INSTALL.md)
- Manuale: [docs/MANUAL.md](docs/MANUAL.md)
- Dettagli applicazione: [kvaser_bus_manager/README.md](kvaser_bus_manager/README.md)
