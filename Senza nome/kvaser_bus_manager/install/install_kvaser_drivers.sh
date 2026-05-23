#!/usr/bin/env bash
set -euo pipefail

log() { echo ">>> $*"; }
warn() { echo "WARNING: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

log "KVASER DRIVER INSTALLATION STARTED"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "Run as root (sudo)."
fi

FORCE_REINSTALL="${FORCE_REINSTALL:-0}"
KEEP_TEMP="${KEEP_TEMP:-0}"

# If already installed, skip rebuild unless forced.
if [[ "$FORCE_REINSTALL" != "1" ]] && [[ -e /usr/lib/libcanlib.so ]]; then
    log "libcanlib already present at /usr/lib/libcanlib.so (skipping rebuild)."
else
    :
fi

# 1. Update System
log "Updating system..."
need_cmd apt-get
apt-get update

# Kernel headers: Debian-like usually provides linux-headers-$(uname -r), but Raspberry Pi kernels
# often require raspberrypi-kernel-headers.
headers_pkg="linux-headers-$(uname -r)"
log "Installing kernel headers ($headers_pkg)..."
if ! apt-get install -y "$headers_pkg"; then
    warn "Could not install $headers_pkg"
    warn "Trying raspberrypi-kernel-headers instead..."
    apt-get install -y raspberrypi-kernel-headers
fi

apt-get install -y \
    build-essential \
    git \
    curl \
    unzip \
    ca-certificates \
    tar \
    xz-utils \
    usbutils

need_cmd curl
need_cmd tar
need_cmd make
need_cmd cc
need_cmd modprobe
need_cmd ldconfig

# 2. Download Kvaser Linux Drivers (linuxcan)
# NOTE: The legacy https://www.kvaser.com/downloads-kvaser/linuxcan.tar.gz URL is no longer valid.
SINGLE_DOWNLOAD_URL="https://kvaser.com/single-download/?download_id=47147"
FALLBACK_URL="https://pim.kvaser.com/var/assets/Product_Resources/linuxcan-5.50.312.tar.gz"

# Optional offline/override sources:
# - KVASER_LINUXCAN_TARBALL=/path/to/linuxcan-*.tar.gz
# - KVASER_LINUXCAN_SRC_DIR=/path/to/extracted/linuxcan (folder containing Makefile)
KVASER_LINUXCAN_TARBALL="${KVASER_LINUXCAN_TARBALL:-}"
KVASER_LINUXCAN_SRC_DIR="${KVASER_LINUXCAN_SRC_DIR:-}"

KVASER_URL="$(curl -fsSL "$SINGLE_DOWNLOAD_URL" \
    | grep -Eo 'https://pim\.kvaser\.com/var/assets/Product_Resources/linuxcan-[0-9.]+\.tar\.gz' \
    | head -n 1 || true)"

if [[ -z "${KVASER_URL}" ]]; then
    warn "Could not resolve latest linuxcan tarball from: $SINGLE_DOWNLOAD_URL"
    warn "Falling back to: $FALLBACK_URL"
    KVASER_URL="$FALLBACK_URL"
fi

TMP_DIR="$(mktemp -d -t kvaser_install.XXXXXX)"
cleanup() {
    if [[ "$KEEP_TEMP" == "1" ]]; then
        warn "Keeping temp dir: $TMP_DIR"
    else
        rm -rf "$TMP_DIR" || true
    fi
}
trap cleanup EXIT

cd "$TMP_DIR"

if [[ -n "$KVASER_LINUXCAN_SRC_DIR" ]] && [[ -d "$KVASER_LINUXCAN_SRC_DIR" ]] && [[ -f "$KVASER_LINUXCAN_SRC_DIR/Makefile" ]]; then
    log "Using KVASER_LINUXCAN_SRC_DIR=$KVASER_LINUXCAN_SRC_DIR"
    DRIVER_DIR="$KVASER_LINUXCAN_SRC_DIR"
elif [[ -n "$KVASER_LINUXCAN_TARBALL" ]] && [[ -f "$KVASER_LINUXCAN_TARBALL" ]]; then
    log "Using KVASER_LINUXCAN_TARBALL=$KVASER_LINUXCAN_TARBALL"
    cp -f "$KVASER_LINUXCAN_TARBALL" ./linuxcan.tar.gz
    tar -xzf linuxcan.tar.gz
    DRIVER_DIR="$(find . -maxdepth 2 -type d -name "linuxcan*" | head -n 1 || true)"
    [[ -n "$DRIVER_DIR" ]] || die "Could not find extracted linuxcan directory"
else
    log "Downloading drivers from $KVASER_URL..."
    curl -fL --retry 5 --retry-delay 2 --retry-connrefused -o linuxcan.tar.gz "$KVASER_URL"

    # Validate archive before extracting (avoid HTML error pages)
    if ! tar -tzf linuxcan.tar.gz >/dev/null 2>&1; then
        warn "Downloaded file is not a valid .tar.gz: $KVASER_URL"
        warn "First lines of downloaded file (likely HTML error):"
        head -n 20 linuxcan.tar.gz || true
        die "Driver download/format validation failed"
    fi

    tar -xzf linuxcan.tar.gz

    DRIVER_DIR="$(find . -maxdepth 2 -type d -name "linuxcan*" | head -n 1 || true)"
    [[ -n "$DRIVER_DIR" ]] || die "Could not find extracted linuxcan directory"
fi

cd "$DRIVER_DIR"

# 3. Build and Install
if [[ "$FORCE_REINSTALL" == "1" ]] || [[ ! -e /usr/lib/libcanlib.so ]]; then
    log "Building drivers..."
    make
else
    log "Skipping build (libcanlib already installed)."
fi

log "Installing drivers..."
make install

# Make sure dynamic linker cache is updated
ldconfig || true

# 4. Load Kernel Modules
log "Loading kernel modules..."
modprobe mhydra || true
modprobe leaf || true
modprobe usbcanII || true

# 5. Setup Udev Rules (for non-root access)
log "Configuring udev rules..."
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bfd", MODE="0666"' > /etc/udev/rules.d/99-kvaser.rules
udevadm control --reload-rules
udevadm trigger

# Optional: put listChannels on PATH (linuxcan installs examples into /usr/doc/canlib/examples)
if [[ -x /usr/doc/canlib/examples/listChannels ]] && [[ ! -e /usr/local/bin/listChannels ]]; then
    install -m 0755 /usr/doc/canlib/examples/listChannels /usr/local/bin/listChannels
fi

# 6. Verify Installation
log "Verifying installation..."

[[ -e /usr/lib/libcanlib.so ]] || die "libcanlib.so not found after install (expected /usr/lib/libcanlib.so)"

if lsmod | grep -Eq '^(mhydra|leaf|usbcanII)\b'; then
    log "SUCCESS: Kvaser drivers installed and modules are loaded."
else
    warn "Driver modules are not currently loaded (may load when device is detected)."
fi

if command -v listChannels >/dev/null 2>&1; then
    log "listChannels is available: $(command -v listChannels)"
else
    warn "listChannels not on PATH (non-fatal)."
fi

log "INSTALLATION COMPLETE. PLEASE REBOOT."
