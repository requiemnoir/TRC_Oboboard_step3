# Kvaser Linux Drivers (vendored)

This directory contains a vendored snapshot of the Kvaser CAN drivers
(`linuxcan`) used to build and load the Kvaser kernel modules required by
the TRC OnBoard system on the target machine.

Two forms are provided:

- `linuxcan/`  — extracted source tree (use this to build/install).
- `linuxcan.tar.gz` — original tarball as obtained from Kvaser, kept for
  byte-exact reproducibility.

## Build & install

```bash
cd linuxcan
make
sudo make install
```

Reload modules:

```bash
sudo modprobe -r mhydra leaf usbcanII pcican pcican2 pciefd virtualcan 2>/dev/null || true
sudo modprobe mhydra
```

Verify with `lsusb` / `dmesg | tail` and `canlib/list_channels` (built under
`canlib/examples/`).

## License

See `linuxcan/COPYING`, `COPYING.GPL`, `COPYING.BSD` inside the source tree.
The drivers are redistributed under their original license terms.
