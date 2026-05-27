# TRC Onboard — Slave Node (capture only)

> **Branch `slave`** — sottoinsieme dedicato alla cattura AUTOSAR Bus Mirror.
> Per il nodo controller (UI + AI + Sentinel) vedi il **branch `master`**.

Questo Raspberry Pi 5 è dedicato **esclusivamente** a:

1. Attivare il mirror DoIP sul gateway veicolo (DID 0xF1A0)
2. Ricevere su UDP :30490 lo stream AUTOSAR Bus Mirror
3. Scrivere MF4 raw su NVMe locale con guarantee 0% drop
4. Esporre stato + comandi remoti via HTTP/Socket.IO sul `/api/*` del daemon
5. (opzionale) Console USB-C di emergenza per debug se l'Ethernet è giù

Niente UI Flask completa, niente Copilot LLM, niente camera, niente Whisper —
quelle vivono sul **master Pi**, che si collega a questo nodo via LAN privata.

---

## Bring-up rapido (Pi 5 4 GB)

```bash
sudo apt install -y git git-lfs
git clone -b slave https://github.com/requiemnoir/TRC_Oboboard_step3.git
cd TRC_Oboboard_step3
git lfs install && git lfs pull

# installer one-shot: token, netplan, venv, systemd unit, start daemon
sudo bash slave_node/install/install_slave.sh
```

Output atteso:
- `/etc/trc-node-token` — bearer token statico (copialo sul master)
- `eth0` configurato statico **192.168.50.20/24**
- `trc-slave.service` enabled + started
- UI locale: `http://192.168.50.20:8001/`

## (Opzionale) Console USB-C di emergenza

```bash
sudo bash install/enable_usb_serial_gadget.sh && sudo reboot
```

Dopo il reboot, collegando un cavo USB-C al laptop:
- **macOS:** `screen /dev/cu.usbmodem* 115200`
- **Linux:** `minicom -D /dev/ttyACM0 -b 115200`

Login: `boss` / password sistema.

## Endpoint slave

| Path | Cosa serve |
|------|-----------|
| `http://<slave-ip>:8001/` | UI status locale (debug) |
| `http://<slave-ip>:8001/api/health` | health JSON |
| `http://<slave-ip>:8001/api/capture/status` | live stats |
| `http://<slave-ip>:8001/api/capture/{start,stop,snapshot}` | capture control |
| `http://<slave-ip>:8001/api/logs?lines=N` | log ring buffer |
| `http://<slave-ip>:8001/api/cmd/exec` | comando in allow-list (debug) |
| `http://<slave-ip>:8001/api/mf4/list` | MF4 sul disco locale |
| `http://<slave-ip>:8001/api/mf4/<file>` | download MF4 |
| `http://<slave-ip>:8001/metrics` | Prometheus-format |
| ws `/slave` namespace | push log/frame/snapshot events |

Tutte le chiamate richiedono `Authorization: Bearer <token-da-etc-trc-node-token>`
quando il token file esiste.

---

## Setup completo (master + slave)

Vedi [`SETUP_DUAL_NODE.md`](SETUP_DUAL_NODE.md) per:

- Topologia hardware (Pi 5 16 GB master + Pi 5 4 GB slave + switch TP-Link)
- Procedura bring-up step-by-step
- Tabella protocollo wire (REST + SocketIO)
- Failure modes e recovery
- Comandi quotidiani
- Test eseguiti in dual-VM

## Branch GitHub

- **`main`** — codice base mono-nodo legacy
- **`master`** — Pi master con UI/Sentinel/Voice/Copilot + pannello /slave-node/
- **`slave`** — **questo branch**, Pi slave dedicato capture

## Test eseguiti

- ✅ Smoke test locale Mac (`SlaveClient` ↔ `slave_daemon` su 127.0.0.1)
- ✅ Dual-VM Lima Ubuntu 24.04 ARM64: master VM esegue comandi reali
  sul slave VM via `host.lima.internal:18001` (hostname risposta confermata)
- ✅ Unit tests `mirror_logger` (4/4) — parser AUTOSAR/CAN-FD/FlexRay/LIN
- ⚠️ Validazione finale in vettura su Pi 5 reale (DoIP gateway Lambo)
