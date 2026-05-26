#!/usr/bin/env python3
"""
replay_mf4.py — replay di un file MF4 (prodotto da RawLogger) come stream
UDP AUTOSAR ISO 23150 verso una destinazione.

Strumento ufficiale per testing/lab. Sostituisce gli script ad-hoc
(host_sender.py) e supporta:
  - lettura diretta dei file MF4 raw del mirror_logger
  - timing realtime (rispetta i ts_ns originali) o accelerato/rallentato
  - loop continuo
  - bucketing in pacchetti AUTOSAR (raggruppa frame nella stessa finestra
    di tempo per ridurre overhead UDP)
  - splitting in chunk < MTU 1500 byte
  - statistiche periodiche

Uso CLI:
  python -m replay_mf4 --mf4 logs/session_*.mf4 --host 192.168.0.100 --port 30490
  python -m replay_mf4 --mf4 ./traccia.mf4 --speed 2.0 --loop
  python -m replay_mf4 --mf4 ./logs --host 127.0.0.1   # tutta una sessione (sorted glob)
"""
from __future__ import annotations
import argparse
import glob
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np
from asammdf import MDF

# Bus type codes inversi: bus_type nel MF4 → AUTOSAR net_type
_MF4_BUS_TO_AUTOSAR = {
    1: 0x01,   # CAN     → net_type 0x01
    2: 0x02,   # CAN-FD  → net_type 0x02
    3: 0x04,   # FlexRay → net_type 0x04
    4: 0x03,   # LIN     → net_type 0x03
}


def _channel_to_net_id(channel_id: int) -> int:
    """Inversa della convenzione mirror_parser: ch 100+net_id_CAN, 200+net_id_FR,
    150+net_id_LIN → ritorna net_id originale."""
    if channel_id >= 200:
        return channel_id - 200
    if channel_id >= 150:
        return channel_id - 150
    if channel_id >= 100:
        return channel_id - 100
    return channel_id & 0xFF


def _build_autosar_packet(seq: int, ts_us: int, entries: List[Tuple]) -> bytes:
    """Header AUTOSAR ISO 23150 + N frame entries."""
    buf = bytearray(struct.pack('!BIH', 0x00, ts_us & 0xFFFFFFFF, seq & 0xFFFF))
    for nt, ni, fid, payload in entries:
        buf += struct.pack('!BBIH', nt & 0xFF, ni & 0xFF, fid & 0xFFFFFFFF, len(payload))
        buf += payload
    return bytes(buf)


def _split_by_mtu(entries: List[Tuple], max_payload: int = 1400) -> Iterator[List[Tuple]]:
    """Yield chunk di entries che stanno entro `max_payload` byte (UDP < MTU 1500)."""
    cur: List[Tuple] = []
    cur_size = 0
    for e in entries:
        size = 8 + len(e[3])
        if cur and cur_size + size > max_payload:
            yield cur
            cur, cur_size = [], 0
        cur.append(e)
        cur_size += size
    if cur:
        yield cur


def _load_mf4_files(paths: List[Path]) -> Tuple[np.ndarray, np.ndarray, np.ndarray,
                                                np.ndarray, np.ndarray, np.ndarray]:
    """Carica più part MF4 e li concatena ordinati per ts_ns.

    Ritorna: (ts_ns, ch, bus, arb_id, dlc, payload[N,64])
    """
    ts_all, ch_all, bus_all, arb_all, dlc_all, pl_all = [], [], [], [], [], []
    for p in paths:
        mdf = MDF(str(p))
        try:
            ts_all.append(np.asarray(mdf.get('ts_ns').samples, dtype=np.int64))
            ch_all.append(np.asarray(mdf.get('ch').samples, dtype=np.int32))
            bus_all.append(np.asarray(mdf.get('bus_type').samples, dtype=np.int32))
            arb_all.append(np.asarray(mdf.get('arb_id').samples, dtype=np.int64))
            dlc_all.append(np.asarray(mdf.get('dlc').samples, dtype=np.int32))
            pl_all.append(np.asarray(mdf.get('payload').samples, dtype=np.uint8))
        finally:
            mdf.close()
    ts = np.concatenate(ts_all)
    ch = np.concatenate(ch_all)
    bus = np.concatenate(bus_all)
    arb = np.concatenate(arb_all)
    dlc = np.concatenate(dlc_all)
    pl = np.concatenate(pl_all)
    order = np.argsort(ts, kind='stable')
    return ts[order], ch[order], bus[order], arb[order], dlc[order], pl[order]


def replay(
    *,
    mf4_paths: List[Path],
    host: str,
    port: int,
    speed: float = 1.0,
    bucket_ms: float = 10.0,
    loop: bool = False,
    verbose: bool = True,
) -> None:
    """Esegue il replay verso host:port."""
    if not mf4_paths:
        raise ValueError('nessun MF4 da replayare')
    print(f'[replay] carico {len(mf4_paths)} part MF4…', flush=True)
    ts_ns, ch, bus, arb, dlc, pl = _load_mf4_files(mf4_paths)
    n = ts_ns.size
    if n == 0:
        print('[replay] MF4 vuoti, esco', flush=True)
        return
    t0_ns = int(ts_ns[0])
    duration_s = (ts_ns[-1] - t0_ns) / 1e9
    print(f'[replay] {n:,} frame, durata {duration_s:.1f}s, speed={speed}×, '
          f'bucket={bucket_ms}ms, loop={loop}', flush=True)
    print(f'[replay] → {host}:{port}', flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    addr = (host, int(port))
    bucket_ns = int(bucket_ms * 1e6)

    iter_no = 0
    try:
        while True:
            iter_no += 1
            run_start = time.perf_counter()
            seq = 0
            pkts_sent = 0
            frames_sent = 0
            bytes_sent = 0
            last_log = run_start

            # Pre-bucketing
            rel_ns = ts_ns - t0_ns
            buckets = rel_ns // bucket_ns
            n_buckets = int(buckets.max()) + 1
            starts = np.searchsorted(buckets, np.arange(n_buckets + 1))

            for b in range(n_buckets):
                i0, i1 = int(starts[b]), int(starts[b + 1])
                if i0 == i1:
                    continue
                # Pacing
                target_s = (b * bucket_ns / 1e9) / speed
                while True:
                    elapsed = time.perf_counter() - run_start
                    if elapsed >= target_s:
                        break
                    wait = target_s - elapsed
                    if wait > 0.001:
                        time.sleep(min(wait, 0.001))

                # Costruisci frame entries per il bucket
                entries: List[Tuple] = []
                for j in range(i0, i1):
                    nt = _MF4_BUS_TO_AUTOSAR.get(int(bus[j]), 0x01)
                    net_id = _channel_to_net_id(int(ch[j]))
                    n_bytes = int(dlc[j])
                    payload = pl[j, :n_bytes].tobytes() if n_bytes > 0 else b''
                    entries.append((nt, net_id, int(arb[j]), payload))

                # Split per MTU e invia
                ts_us = int(target_s * 1e6)
                for chunk in _split_by_mtu(entries):
                    pkt = _build_autosar_packet(seq, ts_us, chunk)
                    seq = (seq + 1) & 0xFFFF
                    try:
                        sock.sendto(pkt, addr)
                        pkts_sent += 1
                        bytes_sent += len(pkt)
                        frames_sent += len(chunk)
                    except OSError as e:
                        print(f'[replay] sendto err: {e}', flush=True)

                if verbose:
                    now = time.perf_counter()
                    if now - last_log >= 5.0:
                        rate = frames_sent / (now - run_start)
                        print(f'  iter={iter_no} t={now-run_start:6.1f}s '
                              f'pkts={pkts_sent} frames={frames_sent:,} '
                              f'avg_fps={rate:.0f}', flush=True)
                        last_log = now

            dur = time.perf_counter() - run_start
            print(f'[replay] iter {iter_no} fine: {frames_sent:,} frame, '
                  f'{bytes_sent/1e6:.1f} MB in {dur:.2f}s '
                  f'({dur/(duration_s/speed if speed > 0 else 1):.3f}× rt)',
                  flush=True)
            if not loop:
                break
    except KeyboardInterrupt:
        print('\n[replay] interrotto da utente', flush=True)
    finally:
        sock.close()


def main() -> int:
    p = argparse.ArgumentParser(description='Replay MF4 RawLogger → UDP AUTOSAR ISO 23150')
    p.add_argument('--mf4', required=True,
                   help='file MF4, directory contenente session_*.mf4, o glob pattern')
    p.add_argument('--host', default='127.0.0.1', help='destination host (default: 127.0.0.1)')
    p.add_argument('--port', type=int, default=30490, help='destination UDP port (default: 30490)')
    p.add_argument('--speed', type=float, default=1.0,
                   help='speed factor (1.0=realtime, 2.0=2× più veloce, 0.5=metà; default 1.0)')
    p.add_argument('--bucket-ms', type=float, default=10.0,
                   help='bucket di aggregazione in pacchetti UDP (default 10ms come AUTOSAR)')
    p.add_argument('--loop', action='store_true', help='ripeti continuamente')
    p.add_argument('--quiet', action='store_true', help='no log progress')
    args = p.parse_args()

    src = Path(args.mf4)
    if src.is_dir():
        files = sorted(src.glob('session_*.mf4'))
    elif '*' in args.mf4 or '?' in args.mf4:
        files = sorted(Path(x) for x in glob.glob(args.mf4))
    else:
        files = [src]
    files = [f for f in files if f.is_file() and f.suffix.lower() == '.mf4']
    if not files:
        print(f'[replay] nessun MF4 trovato in {args.mf4}', file=sys.stderr)
        return 2

    replay(
        mf4_paths=files,
        host=args.host,
        port=args.port,
        speed=args.speed,
        bucket_ms=args.bucket_ms,
        loop=args.loop,
        verbose=not args.quiet,
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
