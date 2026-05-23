# TRC_Onboarda_Setinel_V1

Sistema onboard per Raspberry Pi dedicato a:
- acquisizione bus automotive (CAN/FlexRay/Ethernet),
- logging (CSV/TXT/JSON + MF4),
- Web UI (Logger / ScanTools / MF4 Viewer / Impostazioni) con branding **EV-Q Onboard Manager**,
- ScanTools VAG (CAN + DoIP) con report HTML + export CSV/XML.

Include anche **The Sentinel** (ex “Experimental”): monitor live spie/incidenti con export **TRACE & CTX** (ZIP con MF4 + report scan).

## Installazione (Raspberry nuovo)

```bash
sudo apt-get update
sudo apt-get install -y git git-lfs

git clone https://github.com/requiemnoir/TRC_Onboarda_Setinel_V1.git
cd TRC_Onboarda_Setinel_V1
git lfs install
./install.sh
```

UI: `http://<raspberry-ip>:5000`

Display status page: `http://<raspberry-ip>:5000/display`

On Raspberry Pi desktop installs, the browser launcher now detects when a monitor is connected.
Small displays open the recording status page in kiosk fullscreen, while larger displays stay windowed so you can exit fullscreen and work normally.

Timeline Viewer (pagina dedicata): `http://<raspberry-ip>:5000/timeline`

## Reproducibility

See [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) for pinned dependencies, included runtime assets (DBC/FIBEX/config/model), and how to create an optional full snapshot archive.
## Documentazione

- Install: [docs/INSTALL.md](docs/INSTALL.md)
- Manuale: [docs/MANUAL.md](docs/MANUAL.md)
- Dettagli applicazione: [kvaser_bus_manager/README.md](kvaser_bus_manager/README.md)
