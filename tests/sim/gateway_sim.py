"""Gateway veicolo simulato — invia stream AUTOSAR Bus Mirror UDP + risponde a DoIP UDS DID 0xF1A0.

Modes:
  --mode flood   : invia mirror UDP a rate target, senza aspettare DoIP activate
  --mode reactive: avvia DoIP TCP server (porta 13400) e inizia a mirror solo
                   dopo aver ricevuto una WriteDataByIdentifier(0xF1A0, payload)
  --mode both    : sia DoIP listener che mirror attivo (default)

Esempi:
  python gateway_sim.py --target 127.0.0.1:30490 --rate 5000 --duration 30
  python gateway_sim.py --target 127.0.0.1:30490 --rate 10000 --duration 60 --mode flood
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from typing import Tuple


# ----------------------------------------------------------------- AUTOSAR pkt

def autosar_frame(net_type: int, net_id: int, frame_id: int, payload: bytes) -> bytes:
    """One mirror entry: net_type(1) + net_id(1) + frame_id(4 BE) + len(2 BE) + payload."""
    return struct.pack("!BBIH", net_type, net_id, frame_id & 0xFFFFFFFF, len(payload)) + payload


def autosar_packet(seq: int, ts_us: int, entries: list) -> bytes:
    """AUTOSAR Bus Mirror packet: 7-byte header + concatenated entries."""
    body = struct.pack("!BIH", 0x00, ts_us & 0xFFFFFFFF, seq & 0xFFFF)
    for e in entries:
        body += e
    return body


def build_realistic_packet(seq: int) -> bytes:
    """One realistic Lambo-class mirror packet: 2× FlexRay full + 8× CAN + 3× LIN."""
    entries = []
    # 2 FlexRay channels (dense)
    entries.append(autosar_frame(0x04, 0, 0x100 + (seq % 32), b"\x55" * 24))
    entries.append(autosar_frame(0x04, 1, 0x200 + (seq % 16), b"\xAA" * 24))
    # 8 CAN classic networks
    for net_id in range(1, 9):
        entries.append(autosar_frame(0x01, net_id, 0x300 + (seq % 64),
                                     b"\x10\x20\x30\x40\x50\x60\x70\x80"))
    # 1 CAN-FD
    entries.append(autosar_frame(0x02, 5, 0x400 + (seq % 16), b"\x55" * 32))
    # 3 LIN networks
    for net_id in (1, 2, 3):
        entries.append(autosar_frame(0x03, net_id, 0x011 + (seq % 8),
                                     b"\x01\x02\x03\x04\x05\x06\x07\x08"))
    return autosar_packet(seq & 0xFFFF, seq * 100, entries)


# ----------------------------------------------------------------- DoIP server

DOIP_PORT = 13400
DID_MIRROR = 0xF1A0


def _doip_header(payload_type: int, payload_len: int) -> bytes:
    return struct.pack("!BBHI", 0x02, 0xFD, payload_type, payload_len)


def doip_server(stop: threading.Event, on_did_write):
    """Minimal DoIP TCP server: accept routing activation + WDBI(0xF1A0)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", DOIP_PORT))
    srv.listen(1)
    srv.settimeout(0.5)
    print(f"[doip] listening on TCP :{DOIP_PORT}")
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        print(f"[doip] client connected from {addr}")
        try:
            conn.settimeout(2.0)
            while not stop.is_set():
                hdr = conn.recv(8)
                if not hdr or len(hdr) < 8:
                    break
                ver, inv, ptype, plen = struct.unpack("!BBHI", hdr)
                payload = b""
                while len(payload) < plen:
                    chunk = conn.recv(plen - len(payload))
                    if not chunk:
                        break
                    payload += chunk
                print(f"[doip] rx payload_type=0x{ptype:04X} len={plen}")

                if ptype == 0x0005:  # Routing activation request
                    # respond: routing activation response (0x0006)
                    resp_pl = struct.pack("!HHB", 0x0E00, 0x4010, 0x10) + b"\x00\x00\x00\x00"
                    conn.sendall(_doip_header(0x0006, len(resp_pl)) + resp_pl)
                    print("[doip] sent routing activation response (OK)")
                elif ptype == 0x8001:  # Diagnostic message
                    # parse UDS: source_addr(2), target_addr(2), SID, data
                    if len(payload) >= 5:
                        sid = payload[4]
                        if sid == 0x2E and len(payload) >= 7:  # WDBI
                            did = struct.unpack("!H", payload[5:7])[0]
                            data = payload[7:]
                            print(f"[doip] WDBI DID=0x{did:04X} data_len={len(data)}")
                            if did == DID_MIRROR:
                                on_did_write(data)
                                # positive response 0x6E + DID
                                pos = struct.pack("!HH", 0x4010, 0x0E00) + bytes([0x6E]) + struct.pack("!H", did)
                                conn.sendall(_doip_header(0x8001, len(pos)) + pos)
                                print("[doip] sent WDBI positive response → mirror ARMED")
                            else:
                                print(f"[doip] unknown DID 0x{did:04X}, NRC")
        except Exception as exc:
            print(f"[doip] error: {exc!r}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    srv.close()
    print("[doip] server stopped")


# ----------------------------------------------------------------- UDP sender

def udp_sender(target_ip: str, target_port: int, rate_pps: float,
               duration_s: float, start_event: threading.Event,
               stats: dict):
    """Send realistic AUTOSAR mirror packets at target rate for duration."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    print(f"[udp] sender ready → {target_ip}:{target_port} rate={rate_pps}pps dur={duration_s}s")

    start_event.wait()
    print("[udp] start_event fired, beginning transmission")

    t0 = time.time()
    interval = 1.0 / rate_pps if rate_pps > 0 else 0
    sent = 0
    bytes_sent = 0
    deadline = t0 + duration_s
    next_t = t0
    while time.time() < deadline:
        pkt = build_realistic_packet(sent)
        try:
            sock.sendto(pkt, (target_ip, target_port))
            sent += 1
            bytes_sent += len(pkt)
        except OSError as exc:
            print(f"[udp] send error: {exc}")
            time.sleep(0.01)
            continue
        next_t += interval
        sleep_for = next_t - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
    elapsed = time.time() - t0
    stats["sent_packets"] = sent
    stats["sent_bytes"] = bytes_sent
    stats["elapsed_s"] = elapsed
    stats["effective_pps"] = sent / elapsed if elapsed > 0 else 0
    stats["effective_mbps"] = (bytes_sent * 8 / 1e6) / elapsed if elapsed > 0 else 0
    print(f"[udp] DONE: {sent} packets, {bytes_sent} bytes, {elapsed:.2f}s, "
          f"{stats['effective_pps']:.0f} pps, {stats['effective_mbps']:.1f} Mbps")
    sock.close()


# ----------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="127.0.0.1:30490",
                    help="ip:port destinazione (default 127.0.0.1:30490)")
    ap.add_argument("--rate", type=float, default=2000,
                    help="packets/s (1 pkt = ~14 frames AUTOSAR mix)")
    ap.add_argument("--duration", type=float, default=30,
                    help="durata invio in secondi")
    ap.add_argument("--mode", choices=["flood", "reactive", "both"], default="both")
    args = ap.parse_args()

    target_ip, target_port = args.target.split(":")
    target_port = int(target_port)

    stop = threading.Event()
    start_sender = threading.Event()
    stats = {}

    if args.mode == "flood":
        start_sender.set()

    # DoIP server in background (modes: reactive, both)
    if args.mode in ("reactive", "both"):
        def on_did(payload):
            print(f"[doip] mirror activated by client! payload bytes: {payload[:6].hex()}…")
            start_sender.set()

        t_doip = threading.Thread(
            target=doip_server, args=(stop, on_did), daemon=True
        )
        t_doip.start()
        if args.mode == "both":
            # also arm sender after 2s if no DoIP request comes
            def arm_after():
                if not start_sender.wait(timeout=2.0):
                    print("[doip] no DoIP req in 2s, auto-arming sender (mode=both)")
                    start_sender.set()
            threading.Thread(target=arm_after, daemon=True).start()

    # UDP sender (main thread)
    try:
        udp_sender(target_ip, target_port, args.rate, args.duration, start_sender, stats)
    except KeyboardInterrupt:
        print("\n[main] interrupted")
    finally:
        stop.set()

    print("\n=== GATEWAY SIM REPORT ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
