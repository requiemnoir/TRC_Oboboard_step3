#!/usr/bin/env bash
set -euo pipefail

echo ">>> SETUP CAN & FLEXRAY INTERFACES"

echo ">>> Detecting Kvaser channels..."
if command -v listChannels >/dev/null 2>&1; then
    listChannels || true
elif [[ -x /usr/doc/canlib/examples/listChannels ]]; then
    /usr/doc/canlib/examples/listChannels || true
else
    echo "listChannels command not found. Are drivers installed?" >&2
fi

# This script is a placeholder for system-level configuration if needed.
# Kvaser 'canlib' handles interface activation programmatically in the application.
# However, we can set virtual channels for testing if no hardware is present.

if [[ "${1:-}" == "--virtual" ]]; then
    echo ">>> Setting up Virtual CAN for testing..."
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        sudo modprobe vcan
        sudo ip link add dev vcan0 type vcan || true
        sudo ip link set up vcan0
    else
        modprobe vcan
        ip link add dev vcan0 type vcan || true
        ip link set up vcan0
    fi
    echo ">>> vcan0 started."
fi

echo ">>> Interface setup complete. The Python application will manage bus states."
