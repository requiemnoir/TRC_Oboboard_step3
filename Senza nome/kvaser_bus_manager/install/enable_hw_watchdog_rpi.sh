#!/usr/bin/env bash
set -euo pipefail

# Enables Raspberry Pi hardware watchdog.
# If the system hangs (kernel lockup / scheduler stuck), the SoC watchdog will reboot the Pi.
# Run with sudo.

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root (sudo)."
  exit 1
fi

CFG=""
if [[ -f /boot/firmware/config.txt ]]; then
  CFG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
  CFG="/boot/config.txt"
else
  echo "ERROR: could not find /boot/config.txt or /boot/firmware/config.txt"
  exit 1
fi

# Enable dtparam watchdog
if ! grep -qE '^dtparam=watchdog=on\b' "${CFG}"; then
  echo "Enabling dtparam=watchdog=on in ${CFG}"
  echo "" >> "${CFG}"
  echo "# Enable hardware watchdog" >> "${CFG}"
  echo "dtparam=watchdog=on" >> "${CFG}"
else
  echo "dtparam=watchdog=on already present in ${CFG}"
fi

# Ensure module loads at boot
mkdir -p /etc/modules-load.d
cat >/etc/modules-load.d/bcm2835_wdt.conf <<'EOF'
bcm2835_wdt
EOF

# Install watchdog userspace daemon
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y watchdog
else
  echo "WARN: apt-get not found; install 'watchdog' package manually"
fi

# Minimal watchdog.conf (avoid aggressive load checks to prevent false reboots)
if [[ -f /etc/watchdog.conf ]]; then
  cp -a /etc/watchdog.conf "/etc/watchdog.conf.bak.$(date +%Y%m%d_%H%M%S)"
fi

cat >/etc/watchdog.conf <<'EOF'
# Auto-generated for KVBM in-vehicle use
watchdog-device = /dev/watchdog
interval = 10
realtime = yes
priority = 1
EOF

systemctl enable watchdog
systemctl restart watchdog || true

echo "Hardware watchdog enabled."
echo "IMPORTANT: reboot the Raspberry Pi to activate the dtparam watchdog."