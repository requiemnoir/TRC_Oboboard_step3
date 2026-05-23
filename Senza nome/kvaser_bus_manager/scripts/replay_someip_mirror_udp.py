#!/usr/bin/env python3
"""Replay a captured SOME/IP mirror UDP payload toward localhost.

Purpose: allow demoing the UI end-to-end (EthernetCapture -> parser -> BusManager -> UI)
without having the vehicle connected.

It sends the exact UDP payload bytes (including SOME/IP header) from a golden sample
file at a configurable rate.

Example:
  python3 scripts/replay_someip_mirror_udp.py \
    --sample tests/data/vag_mirror_single_payload.bin \
    --host 127.0.0.1 --port 30490 --pps 50
"""

from __future__ import annotations

import argparse
import socket
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', required=True, help='Path to UDP payload bytes (including SOME/IP header)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=30490)
    ap.add_argument('--pps', type=float, default=20.0, help='Packets per second')
    ap.add_argument('--count', type=int, default=0, help='0 = infinite')
    args = ap.parse_args()

    payload = open(args.sample, 'rb').read()
    if not payload:
        raise SystemExit('Sample payload is empty')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    interval = 1.0 / max(args.pps, 0.1)
    sent = 0
    t_next = time.time()

    try:
        while True:
            sock.sendto(payload, (args.host, args.port))
            sent += 1
            if args.count and sent >= args.count:
                break
            t_next += interval
            sleep = t_next - time.time()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # we're lagging; resync
                t_next = time.time()
    finally:
        sock.close()

    print(f"sent={sent} host={args.host} port={args.port} pps={args.pps}")


if __name__ == '__main__':
    main()
