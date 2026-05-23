#!/usr/bin/env python3
"""Generate a minimal, real MF4 trace from a captured SOME/IP mirror packet.

This is an offline utility used to create a reproducible MF4 artifact even when the
vehicle isn't connected.

Input:
  - PCAP containing SOME/IP mirror UDP payloads (Service 0x02FD, Method 0xF302)

Output:
  - An MF4 file with a few useful signals:
      * Mirror_CAN_ID (uint32)
      * Mirror_CAN_Bus (uint8)
      * Mirror_CAN_DLC (uint16)
      * Mirror_CAN_Data (bytes)  (stored as fixed-length S64)

Notes:
  - This does *not* try to be an ASAM CAN logging MF4 with full CAN bus semantics.
    It's a simple, valid MF4 file that preserves the payload content/time ordering.
  - Parser logic mimics the robust resync scanner from backend/ethernet_capture.py.
"""

from __future__ import annotations

import argparse
import os
import struct
import time

import numpy as np
from asammdf import MDF, Signal
from scapy.all import rdpcap, UDP


KNOWN_IDS = {
    0x0FD, 0x0A8, 0x0A7, 0x0116, 0x0086, 0x007C, 0x0108, 0x00B5, 0x0040,
    0x030B, 0x0030, 0x023C, 0x03C0, 0x0103, 0x00AD, 0x00B3, 0x0121, 0x03D5,
}


def iter_someip_mirror_payloads(pcap_path: str):
    pkts = rdpcap(pcap_path)
    for p in pkts:
        if UDP not in p:
            continue
        raw = bytes(p[UDP].payload)
        if len(raw) < 16:
            continue
        srv = struct.unpack('!H', raw[0:2])[0]
        met = struct.unpack('!H', raw[2:4])[0]
        if srv != 0x02FD or met != 0xF302:
            continue
        yield float(p.time), raw


def scan_inner(inner: bytes):
    """Return list of (bus_ch, can_id, data) from inner payload."""

    def try_at(payload: bytes, off: int):
        if off + 10 > len(payload):
            return None
        bus_ch = payload[off + 2]
        ntype = payload[off + 3]
        can_id = struct.unpack('!H', payload[off + 6:off + 8])[0]
        dlc = struct.unpack('!H', payload[off + 8:off + 10])[0]
        if bus_ch > 7:
            return None
        if ntype != 1:
            return None
        if dlc > 64:
            return None
        end = off + 10 + dlc
        if end > len(payload):
            return None
        return (10 + dlc, bus_ch, can_id, payload[off + 10:end])

    start_offsets = [4, 3, 2, 0]
    best_frames = []
    best_score = -1

    for start in start_offsets:
        frames = []
        score = 0
        i = start
        while i + 10 <= len(inner):
            hit = try_at(inner, i)
            if hit is None:
                i += 1
                continue
            consumed, bus, can_id, data = hit
            frames.append((bus, can_id, data))
            score += 3 if can_id in KNOWN_IDS else 1
            i += consumed
        if frames and score > best_score:
            best_frames = frames
            best_score = score

    return best_frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pcap', required=True, help='Input pcap (must contain SOME/IP 0x02FD/0xF302)')
    ap.add_argument('--out', default=None, help='Output mf4 path (default: ./mirror_trace_<ts>.mf4)')
    args = ap.parse_args()

    all_records = []
    for ts, raw in iter_someip_mirror_payloads(args.pcap):
        inner = raw[16:]
        frames = scan_inner(inner)
        for bus, can_id, data in frames:
            all_records.append((ts, bus, can_id, data))

    if not all_records:
        raise SystemExit('No SOME/IP mirror frames found in pcap')

    # Sort by capture timestamp
    all_records.sort(key=lambda r: r[0])

    t0 = all_records[0][0]
    times = np.array([r[0] - t0 for r in all_records], dtype=float)
    bus = np.array([r[1] for r in all_records], dtype=np.uint8)
    can_id = np.array([r[2] for r in all_records], dtype=np.uint32)
    dlc = np.array([len(r[3]) for r in all_records], dtype=np.uint16)

    # Fixed-length bytes (S64) to stay simple/portable
    data_fixed = []
    for _, _, _, d in all_records:
        if len(d) > 64:
            d = d[:64]
        data_fixed.append(d.ljust(64, b'\x00'))
    data_fixed = np.array(data_fixed, dtype='S64')

    mdf = MDF()
    sigs = [
        Signal(can_id, times, name='Mirror_CAN_ID'),
        Signal(bus, times, name='Mirror_CAN_Bus'),
        Signal(dlc, times, name='Mirror_CAN_DLC'),
        Signal(data_fixed, times, name='Mirror_CAN_Data', encoding='latin-1'),
    ]
    mdf.append(sigs)

    out = args.out
    if not out:
        out = os.path.abspath(f"mirror_trace_{int(time.time())}.mf4")

    mdf.save(out)
    print(out)


if __name__ == '__main__':
    main()
