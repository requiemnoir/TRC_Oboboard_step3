#!/usr/bin/env python3
"""Self-test for DoIP IPv6 connectivity (local mock).

This does not implement full ISO 13400; it only validates that our client-side
socket creation can connect to an IPv6 endpoint and exchange basic DoIP frames.

It starts a TCP server on ::1:13400, accepts one connection, expects a Routing
Activation Request (0x0005) and replies with a minimal Routing Activation
Response (0x0006).

Usage:
  ./.venv/bin/python scripts/doip_ipv6_self_test.py
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import sys
from pathlib import Path


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _server(stop_evt: threading.Event, ready_evt: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("::1", 13400))
    srv.listen(1)
    ready_evt.set()
    try:
        conn, _addr = srv.accept()
        with conn:
            conn.settimeout(2.0)
            hdr = _recv_exact(conn, 8)
            ver, inv, ptype, length = struct.unpack("!BBHL", hdr)
            payload = _recv_exact(conn, int(length)) if int(length) else b""

            # Expect routing activation request
            if ver != 0x02 or inv != 0xFD or ptype != 0x0005:
                return

            # Minimal routing activation response (ptype 0x0006) with empty payload.
            resp_hdr = struct.pack("!BBHL", 0x02, 0xFD, 0x0006, 0)
            conn.sendall(resp_hdr)
    finally:
        try:
            srv.close()
        except Exception:
            pass
        stop_evt.set()


def main() -> int:
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend_dir))
    import vag_scanner  # type: ignore
    DoIPGatewayScanner = vag_scanner.DoIPGatewayScanner

    stop_evt = threading.Event()
    ready_evt = threading.Event()

    t = threading.Thread(target=_server, args=(stop_evt, ready_evt), daemon=True)
    t.start()
    ready_evt.wait(timeout=1.0)

    doip = DoIPGatewayScanner("::1", emit_log=None, tester_logical_address=0x0E00)
    try:
        doip._connect()
        doip._routing_activation()
    finally:
        doip.close()

    stop_evt.wait(timeout=1.5)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
