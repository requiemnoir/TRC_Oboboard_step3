"""trace_replay.py — riproduce un PCAP o .bin "TRCM" della mirror capture
inviando i pacchetti UDP allo slave_daemon, conservando il timing originale
(o accelerato/decelerato con --speed).

Uso:
    # Replay del .pcap reale a velocità originale
    python tests/sim/trace_replay.py \\
        logs/sample/runtime/mirror_60s_20260525T093450.pcap \\
        --target 127.0.0.1:30490

    # Replay accelerato 4×
    python tests/sim/trace_replay.py logs/sample/runtime/mirror_60s_*.pcap \\
        --target 127.0.0.1:30490 --speed 4.0

    # Replay del .bin nativo (formato TRCM custom)
    python tests/sim/trace_replay.py logs/sample/runtime/mirror_raw_*.bin --target ...

Output:
    Mostra packet/sec, byte/sec, total bytes sent, eventuali errori.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path


# ----------------------------------------------------------------- pcap reader

PCAP_GLOBAL_HEADER_LEN = 24
PCAP_RECORD_HEADER_LEN = 16
ETH_HEADER_LEN = 14
IPV4_MIN_HEADER_LEN = 20
IPV6_HEADER_LEN = 40
UDP_HEADER_LEN = 8


def read_pcap(path: str):
    """Yield (timestamp_s, udp_payload_bytes) for every UDP packet in a libpcap file."""
    with open(path, "rb") as f:
        gh = f.read(PCAP_GLOBAL_HEADER_LEN)
        if len(gh) < PCAP_GLOBAL_HEADER_LEN:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        # 0xa1b2c3d4 = native little-endian, micros
        # 0xa1b23c4d = nanoseconds
        if magic == 0xa1b2c3d4:
            ns_resolution = False
        elif magic == 0xa1b23c4d:
            ns_resolution = True
        else:
            print(f"WARN: unexpected PCAP magic 0x{magic:08x} — trying little-endian micros")
            ns_resolution = False
        linktype = struct.unpack("<I", gh[20:24])[0]
        if linktype != 1:  # 1 = LINKTYPE_ETHERNET
            print(f"WARN: linktype={linktype}, expected 1 (Ethernet) — may not parse")

        while True:
            rh = f.read(PCAP_RECORD_HEADER_LEN)
            if len(rh) < PCAP_RECORD_HEADER_LEN:
                return
            ts_sec, ts_frac, incl_len, orig_len = struct.unpack("<IIII", rh)
            data = f.read(incl_len)
            if len(data) < incl_len:
                return
            ts = ts_sec + (ts_frac * 1e-9 if ns_resolution else ts_frac * 1e-6)

            # Strip Ethernet header
            if len(data) < ETH_HEADER_LEN:
                continue
            eth_type = struct.unpack("!H", data[12:14])[0]
            ip_start = ETH_HEADER_LEN

            if eth_type == 0x0800:  # IPv4
                if len(data) < ip_start + IPV4_MIN_HEADER_LEN:
                    continue
                ihl = data[ip_start] & 0x0F
                proto = data[ip_start + 9]
                if proto != 17:  # not UDP
                    continue
                ip_hdr_len = ihl * 4
                udp_start = ip_start + ip_hdr_len
            elif eth_type == 0x86DD:  # IPv6
                if len(data) < ip_start + IPV6_HEADER_LEN:
                    continue
                next_hdr = data[ip_start + 6]
                if next_hdr != 17:
                    continue
                udp_start = ip_start + IPV6_HEADER_LEN
            else:
                continue

            if len(data) < udp_start + UDP_HEADER_LEN:
                continue
            udp_len = struct.unpack("!H", data[udp_start + 4:udp_start + 6])[0]
            payload_start = udp_start + UDP_HEADER_LEN
            payload_end = udp_start + udp_len
            payload = data[payload_start:payload_end]
            if payload:
                yield ts, payload


# ----------------------------------------------------------------- bin reader

# Format TRCM (custom mirror_logger):
#   "TRCM" (4) + version (uint16 BE) + ... → length-framed
# Simple heuristic: each frame is preceded by uint32 BE length.

def read_trcm_bin(path: str):
    """Yield (synthetic_ts, payload) from a TRCM .bin file. The exact framing
    is custom — we try a few heuristics. Falls back to streaming the whole file
    as one blob if framing detection fails."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"TRCM":
            print(f"WARN: file doesn't start with TRCM magic ({magic!r})")
            f.seek(0)
        version = f.read(2)
        # try framing: <uint32 BE length><payload>...
        # but the TRCM header probably has 4 more bytes of header before that
        f.seek(8)
        ts = time.time()
        n = 0
        while True:
            ln_bytes = f.read(4)
            if len(ln_bytes) < 4:
                return
            ln = struct.unpack(">I", ln_bytes)[0]
            if ln <= 0 or ln > 65535:
                # likely wrong framing — abort
                if n == 0:
                    print(f"WARN: TRCM framing not detected (first len={ln}); skipping .bin replay")
                return
            payload = f.read(ln)
            if len(payload) < ln:
                return
            yield ts + n * 0.001, payload  # synthetic 1 kHz
            n += 1


def read_packets(path: str):
    p = Path(path)
    if p.suffix.lower() == ".pcap":
        yield from read_pcap(str(p))
    elif p.suffix.lower() == ".bin":
        yield from read_trcm_bin(str(p))
    else:
        raise ValueError(f"unsupported extension: {p.suffix}")


# ----------------------------------------------------------------- replay

def replay(path: str, target_ip: str, target_port: int,
           speed: float = 1.0, max_packets: int = 0, preserve_timing: bool = True):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    addr = (target_ip, target_port)
    print(f"replay → {target_ip}:{target_port}  speed={speed}×  source={path}", flush=True)

    sent = 0
    bytes_sent = 0
    t_start = time.time()
    first_ts: float = None
    last_print = t_start
    errors = 0

    for ts, payload in read_packets(path):
        if first_ts is None:
            first_ts = ts
        if preserve_timing and speed > 0:
            wall_target = t_start + (ts - first_ts) / speed
            sleep_for = wall_target - time.time()
            if sleep_for > 0.002:
                time.sleep(sleep_for)
        try:
            sock.sendto(payload, addr)
            sent += 1
            bytes_sent += len(payload)
        except OSError as exc:
            errors += 1
            if errors <= 3:
                print(f"sendto error #{errors}: {exc}")
            time.sleep(0.001)

        now = time.time()
        if now - last_print >= 1.0:
            elapsed = now - t_start
            print(f"  [{elapsed:6.1f}s] sent {sent:,} pkt / {bytes_sent/1e6:.1f} MB "
                  f"({sent/elapsed:.0f} pps avg) errors={errors}")
            last_print = now

        if max_packets and sent >= max_packets:
            break

    elapsed = time.time() - t_start
    sock.close()
    print()
    print("=== REPLAY DONE ===")
    print(f"  packets sent : {sent:,}")
    print(f"  bytes sent   : {bytes_sent:,} ({bytes_sent/1e6:.2f} MB)")
    print(f"  duration     : {elapsed:.2f}s")
    print(f"  avg pps      : {sent/elapsed:.0f}")
    print(f"  avg Mbps     : {bytes_sent*8/elapsed/1e6:.2f}")
    print(f"  send errors  : {errors}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="path to .pcap or .bin trace")
    ap.add_argument("--target", default="127.0.0.1:30490",
                    help="UDP destination (default 127.0.0.1:30490)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="time scale (1.0=original, 2.0=2× faster, 0=no pacing/flood)")
    ap.add_argument("--max-packets", type=int, default=0,
                    help="cap on packets sent (0=all)")
    args = ap.parse_args()

    ip, port = args.target.split(":")
    preserve = args.speed > 0
    replay(args.source, ip, int(port), speed=args.speed if preserve else 1.0,
           max_packets=args.max_packets, preserve_timing=preserve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
