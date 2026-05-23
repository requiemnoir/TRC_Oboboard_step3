#!/bin/bash
set -e

echo ">>> KVASER DRIVER REPAIR (ATTEMPT 4 - DIRECT LINK)"

# 1. Install Dependencies
sudo apt-get update
sudo apt-get install -y build-essential linux-headers-$(uname -r) git wget unzip

# 2. Prepare Source Directory
SRC_DIR="$HOME/kvaser_drivers_src"
mkdir -p $SRC_DIR
cd $SRC_DIR

# 3. Download Drivers
echo ">>> Downloading drivers..."
rm -f linuxcan*.tar.gz
wget "https://pim.kvaser.com/var/assets/Product_Resources/linuxcan-5.50.312.tar.gz" -O linuxcan.tar.gz

# Verify file type
if ! file linuxcan.tar.gz | grep -q "gzip compressed data"; then
    echo ">>> ERROR: Downloaded file is not a valid gzip archive."
    exit 1
fi

tar -xzf linuxcan.tar.gz

# Find extracted dir
DRIVER_DIR=$(find . -maxdepth 1 -type d -name "linuxcan*" | head -n 1)
cd $DRIVER_DIR

echo ">>> Building in $PWD"

# 4. Clean previous builds
make clean || true

# 5. Build
echo ">>> Compiling..."
make

# 6. Install
echo ">>> Installing..."
sudo make install

# 7. Load Modules
echo ">>> Loading modules..."
sudo modprobe mhydra
sudo modprobe leaf
sudo modprobe usbcanII

# 8. Verify
echo ">>> Verifying..."
if lsmod | grep -q "mhydra"; then
    echo ">>> Module mhydra loaded."
else
    echo ">>> ERROR: mhydra module failed to load."
    exit 1
fi

echo ">>> REPAIR COMPLETE. Try running listChannels now."
if [ -f "/usr/sbin/listChannels" ]; then
    /usr/sbin/listChannels
elif [ -f "/usr/local/sbin/listChannels" ]; then
    /usr/local/sbin/listChannels
else
    echo ">>> WARNING: listChannels not found in standard paths."
fi
