#!/usr/bin/env python3
"""Generate a (MDF4) log from replayed SOME/IP mirror packets.

The user asked for an "lf4" trace. In practice, most toolchains use ASAM MDF4
container files with extension `.mf4`. Some vendors/flows rename or wrap them.

This script:
  1) Replays a SOME/IP mirror UDP payload N times to a local UDP socket.
  2) Parses it using the same logic as backend/ethernet_capture.py (imported).
  3) Writes an MDF4 file with one row per extracted mirror CAN frame.

Outputs:
  - <out>.mf4 (always)
  - If --also-lf4 is set: copies to <out>.lf4 as well.

This does NOT claim an official ASAM CAN logging schema; it's a valid MDF4
containing the mirror frames as signals.

Usage:
  python3 scripts/generate_lf4_from_udp_replay.py \
    --sample tests/data/vag_mirror_single_payload.bin \
    --out logs/mirror_full

To capture a real vehicle stream instead, see --listen mode (future extension).
"""

from __future__ import annotations

import argparse
import os
import shutil
import time

import numpy as np
from asammdf import MDF, Signal

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from ethernet_capture import EthernetCapture  # noqa: E402


def parse_payload_to_frames(payload: bytes):
    frames = []
    cap = EthernetCapture.__new__(EthernetCapture)
    cap._mirror_rx_count = 0
    cap._mirror_count = 0
    cap._mirror_errors = 0

    def cb(channel_id, arb_id, data, flags=0, frame_type='CAN'):
        # store seconds relative later
        frames.append((time.time(), int(channel_id), int(arb_id), bytes(data), str(frame_type)))

    cap.mirror_callback = cb
    cap._unpack_mirror_payload(payload)
    return frames


def write_mdf(frames, out_mf4: str):
    if not frames:
        raise SystemExit('No frames parsed; output would be empty')

    frames.sort(key=lambda r: r[0])
    t0 = frames[0][0]
    times = np.array([r[0] - t0 for r in frames], dtype=float)
    channel = np.array([r[1] for r in frames], dtype=np.uint16)
    can_id = np.array([r[2] for r in frames], dtype=np.uint32)
    dlc = np.array([len(r[3]) for r in frames], dtype=np.uint16)

    data_fixed = []
    for _, _, _, d, _ in frames:
        data_fixed.append(d[:64].ljust(64, b'\x00'))
    data_fixed = np.array(data_fixed, dtype='S64')

    mdf = MDF()
    sigs = [
        Signal(can_id, times, name='Mirror_CAN_ID'),
        Signal(channel, times, name='Mirror_Channel'),
        Signal(dlc, times, name='Mirror_CAN_DLC'),
        Signal(data_fixed, times, name='Mirror_CAN_Data', encoding='latin-1'),
    ]
    mdf.append(sigs)
    mdf.save(out_mf4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', required=True, help='Path to UDP payload bytes (incl. SOME/IP header)')
    ap.add_argument('--repeat', type=int, default=500, help='How many times to parse the sample payload')
    ap.add_argument('--out', required=True, help='Output base path without extension')
    ap.add_argument('--also-lf4', action='store_true', help='Also copy the MF4 to .lf4')
    args = ap.parse_args()

    payload = open(args.sample, 'rb').read()

    all_frames = []
    for _ in range(max(args.repeat, 1)):
        all_frames.extend(parse_payload_to_frames(payload))

    out_base = args.out
    out_mf4 = out_base + '.mf4'
    write_mdf(all_frames, out_mf4)

    if args.also_lf4:
        out_lf4 = out_base + '.lf4'
        shutil.copyfile(out_mf4, out_lf4)
        print(out_lf4)
    print(out_mf4)


if __name__ == '__main__':
    main()
