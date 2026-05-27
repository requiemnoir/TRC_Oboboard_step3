#!/usr/bin/env bash
# enable_usb_serial_gadget.sh — abilita USB serial console (gadget mode) sul Pi 5.
#
# Effetto: collegando un cavo USB-C dal Pi al laptop, su /dev/ttyACMx (Linux/Mac) o
# COMx (Windows) si apre una shell di emergenza per debug quando l'Ethernet è giù.
#
# Modifiche permanenti (al primo run):
#   1. /boot/firmware/config.txt : aggiunge "dtoverlay=dwc2,dr_mode=peripheral"
#   2. /boot/firmware/cmdline.txt: aggiunge "modules-load=dwc2"
#   3. abilita trc-usb-console.service
#
# Idempotente. Richiede reboot la prima volta.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then echo "ERROR: sudo richiesto" >&2; exit 1; fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOT_CFG="/boot/firmware/config.txt"
BOOT_CMDL="/boot/firmware/cmdline.txt"

# 1. config.txt: dtoverlay=dwc2
if ! grep -qE '^dtoverlay=dwc2,dr_mode=peripheral' "${BOOT_CFG}" 2>/dev/null; then
  cp -p "${BOOT_CFG}" "${BOOT_CFG}.bak.$(date +%s)"
  echo "" >> "${BOOT_CFG}"
  echo "# TRC USB serial gadget (added by enable_usb_serial_gadget.sh)" >> "${BOOT_CFG}"
  echo "dtoverlay=dwc2,dr_mode=peripheral" >> "${BOOT_CFG}"
  echo "[usb_serial] config.txt patched"
else
  echo "[usb_serial] config.txt already patched"
fi

# 2. cmdline.txt: modules-load=dwc2 (NB single-line file)
if ! grep -qE 'modules-load=dwc2' "${BOOT_CMDL}" 2>/dev/null; then
  cp -p "${BOOT_CMDL}" "${BOOT_CMDL}.bak.$(date +%s)"
  # cmdline.txt deve restare 1 riga
  sed -i 's| *$| modules-load=dwc2|' "${BOOT_CMDL}"
  echo "[usb_serial] cmdline.txt patched"
else
  echo "[usb_serial] cmdline.txt already patched"
fi

# 3. systemd unit
install -m 0644 "${REPO_DIR}/install/systemd/trc-usb-console.service" \
                /etc/systemd/system/trc-usb-console.service
systemctl daemon-reload
systemctl enable trc-usb-console.service
echo "[usb_serial] trc-usb-console.service enabled"

cat <<EOF

[usb_serial] DONE. Riavvia il Pi per attivare il dwc2 gadget.

Dopo reboot, collega un cavo USB-C (data, non solo power!) tra Pi e laptop:

  macOS:   ls /dev/cu.usbmodem*    → screen /dev/cu.usbmodemXXXX 115200
  Linux:   ls /dev/ttyACM*         → minicom -D /dev/ttyACM0 -b 115200
  Windows: Device Manager → COMx  → PuTTY COMx 115200 8N1

Login: boss / (password sistema)

EOF
