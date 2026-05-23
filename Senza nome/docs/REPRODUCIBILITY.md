# Reproducibility (system-as-is)

This repository now contains the runtime assets needed to reproduce the current system behavior:
- Current runtime config: `kvaser_bus_manager/config/app_config.json`
- Vehicle databases: `databases/dbc/` and `kvaser_bus_manager/databases/{dbc,fibex}/`
- Project assets: `kvaser_bus_manager/projects/`
- Pinned model weights: `kvaser_bus_manager/yolov8n.pt`
- Dependency lockfiles: `kvaser_bus_manager/requirements-lock.txt` (and `requirements-lock-root.txt` if applicable)
- OS/package snapshot: `repro_state/`

## Recreate the Python environment

From repo root:

```bash
./scripts/reproduce_system.sh
```

By default this creates/uses `kvaser_bus_manager/.venv`.

## Full snapshot archive (optional)

GitHub has practical limits for very large binary artifacts (e.g. big MF4/MP4 captures and huge logs). Those remain ignored by default.

To create a local archive containing the current runtime assets + reproducibility metadata:

```bash
./scripts/make_full_snapshot.sh
```

The archive is written to `./snapshots/` and can be uploaded to external storage (or GitHub Releases) when needed.

## Setup SSD

As default in this repo we consider to have a ssd connected to the device, to save data in the local folder projcet go to this [line](https://git.lamborghini.com/lamborghini/lambo-trc-onboard/blob/773e009a92399f57b581711cefc9296a1ff61d1b/kvaser_bus_manager/backend/app.py#L949) and change it accordingly to previous commented line.

### Step 1

Check for device name.

```bash
lsblk
```

### Step 2

Erase ssd

```bash
mkfs.ext4 /dev/DEVICE_NAME
```

### Step 3

Mount, create dir and change access

```bash
mount /dev/DEVICE_NAME /mnt/ssd
mkdir /mnt/ssd/logs
chmod 777 /mnt/ssd
chmod 777 /mnt/ssd/logs
```

### Step 4

Mount at booting time the ssd.

Get device ID.

```bash
blkid
```

Edit fstab file as follows

```bash
vim /etc/fstab
```

```bash
UUID=DEVICE_ID /mnt/ssd etx4 defualts,noatime 0 2
```
