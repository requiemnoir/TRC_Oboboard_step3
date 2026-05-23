#!/usr/bin/env python3
"""Mock DoIP gateway + a couple of ECUs (local test).

Purpose
  - Provide a deterministic ISO 13400-ish endpoint so `vag_doip_scan_report`
    can be tested without a vehicle.
  - Enough behavior to let `DoIPGatewayScanner` discover ECUs and read DTCs.

What it implements (minimal)
  - TCP server on ::1:13400
  - Routing Activation Request (0x0005) -> Routing Activation Response (0x0006)
  - Diagnostic Messages (0x8001):
      * UDS 0x3E 00 (TesterPresent) -> 0x7E 00
      * UDS 0x19 02 0xFF (ReadDTCInformation) -> 0x59 02 <statusAvailMask> + records
      * UDS 0x22 F1 87/8A/89/8C (ident) -> 0x62 ... + ASCII

Notes
  - This is NOT a full DoIP implementation. It's only meant for local smoke tests.
  - The server accepts a single client at a time.

Usage
  1) Run server in one terminal:
       python3 kvaser_bus_manager/scripts/doip_mock_gateway.py
  2) Run scan in another terminal:
       python3 -c "from kvaser_bus_manager.backend.vag_scanner import DoIPGatewayScanner; s=DoIPGatewayScanner('::1', emit_log=print); print(s.run_scan_report()); s.close()"
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _doip_send(conn: socket.socket, ptype: int, payload: bytes) -> None:
    hdr = struct.pack("!BBHL", 0x02, 0xFD, int(ptype) & 0xFFFF, len(payload))
    conn.sendall(hdr + (payload or b""))


@dataclass
class MockEcu:
    la: int
    ident: str
    # list of tuples (uds_dtc_3bytes_int, status_byte)
    dtcs: list[tuple[int, int]]


def _uds_positive_tester_present() -> bytes:
    return bytes([0x7E, 0x00])


def _uds_read_dtcs_response(dtcs: list[tuple[int, int]]) -> bytes:
    # 0x59 0x02 <statusAvailabilityMask> + (DTC(3) + status(1))*
    status_mask = 0xFF
    payload = bytearray([0x59, 0x02, status_mask])
    for dtc_val, st in dtcs:
        v = int(dtc_val) & 0xFFFFFF
        payload += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, int(st) & 0xFF])
    return bytes(payload)


def _uds_read_did_response(did: int, text: str) -> bytes:
    did = int(did) & 0xFFFF
    data = (text or "").encode("ascii", errors="ignore")[:64]
    return bytes([0x62, (did >> 8) & 0xFF, did & 0xFF]) + data


def _handle_diag(ecus: dict[int, MockEcu], sa: int, ta: int, uds: bytes) -> bytes | None:
    ecu = ecus.get(int(ta) & 0xFFFF)
    if ecu is None or not uds:
        return None

    # Tester Present
    if uds[:2] == bytes([0x3E, 0x00]):
        return _uds_positive_tester_present()

    # DiagnosticSessionControl (0x10) - accept any session
    if len(uds) >= 2 and uds[0] == 0x10:
        sess = uds[1]
        # Positive response: 50 <session> + P2/P2star timing
        return bytes([0x50, sess, 0x00, 0x19, 0x01, 0xF4])

    # Read DTCs: 19 02 FF
    if len(uds) >= 3 and uds[0] == 0x19 and uds[1] == 0x02 and uds[2] == 0xFF:
        return _uds_read_dtcs_response(ecu.dtcs)

    # ControlDTCSetting (0x85) - accept
    if len(uds) >= 2 and uds[0] == 0x85:
        return bytes([0xC5, uds[1]])

    # Identification DIDs
    if len(uds) >= 3 and uds[0] == 0x22:
        did = (uds[1] << 8) | uds[2]
        if did in (0xF187, 0xF18A, 0xF189, 0xF18C):
            return _uds_read_did_response(did, ecu.ident)
        # Mirror-mode DID read — return current (or default) 21-byte payload
        if did in (0x096F, 0x2A3C, 0x2A20):
            # Return a default "not_active" mirror status: 21 zero bytes
            return bytes([0x62, (did >> 8) & 0xFF, did & 0xFF]) + bytes(21)

    # Mirror-mode DID write (gateway feature). Accept it to allow scanner smoke tests.
    # UDS: 2E <DID_HI> <DID_LO> <DATA...>
    if len(uds) >= 3 and uds[0] == 0x2E:
        did = (uds[1] << 8) | uds[2]
        if did in (0x096F, 0x2A3C, 0x2A20):
            # Positive response: 6E <DID_HI> <DID_LO>
            return bytes([0x6E, (did >> 8) & 0xFF, did & 0xFF])

    # Negative response: service not supported
    # 7F <sid> 11
    return bytes([0x7F, uds[0], 0x11])


def serve(*, host: str = "::1", port: int = 13400, stop_evt: threading.Event | None = None) -> None:
    stop_evt = stop_evt or threading.Event()

    # Two mock ECUs at common low LAs.
    # NOTE: When `DoIPGatewayScanner` uses the PDX comm index, it may probe a list
    # of many addresses. For smoke tests, we typically pass `ecu_addresses=[...]`
    # so discovery stays fast and deterministic.
    ecus = {
        0x0001: MockEcu(la=0x0001, ident="MOCK-ECU-ENGINE", dtcs=[(0x123456, 0x0B), (0x654321, 0x08)]),
        0x0003: MockEcu(la=0x0003, ident="MOCK-ECU-ABS", dtcs=[(0x00ABCD, 0x04)]),
    }

    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, int(port)))
    srv.listen(1)
    print(f"Mock DoIP gateway listening on [{host}]:{port}")

    try:
        while not stop_evt.is_set():
            srv.settimeout(0.5)
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                continue

            print(f"Client connected: {addr}")
            with conn:
                conn.settimeout(1.0)
                try:
                    while not stop_evt.is_set():
                        hdr = _recv_exact(conn, 8)
                        ver, inv, ptype, length = struct.unpack("!BBHL", hdr)
                        if ver != 0x02 or inv != 0xFD:
                            break
                        payload = _recv_exact(conn, int(length)) if int(length) else b""

                        # Routing activation
                        if int(ptype) == 0x0005:
                            _doip_send(conn, 0x0006, b"")
                            continue

                        # Diagnostic message
                        if int(ptype) == 0x8001 and len(payload) >= 4:
                            sa, ta = struct.unpack("!HH", payload[:4])
                            uds = payload[4:]

                            # Optional: send a diagnostic ACK (0x8002). Some stacks may log it.
                            # We keep it minimal: same SA/TA and no UDS bytes.
                            try:
                                ack_payload = struct.pack("!HH", int(ta) & 0xFFFF, int(sa) & 0xFFFF)
                                _doip_send(conn, 0x8002, ack_payload)
                            except Exception:
                                pass

                            resp_uds = _handle_diag(ecus, sa, ta, uds)
                            if resp_uds is None:
                                continue
                            # swap SA/TA so response is ECU -> tester
                            resp_payload = struct.pack("!HH", int(ta) & 0xFFFF, int(sa) & 0xFFFF) + resp_uds
                            _doip_send(conn, 0x8001, resp_payload)
                            continue

                        # Ignore everything else
                except ConnectionError:
                    pass
                except Exception as e:
                    print(f"Client error: {e}")
            print("Client disconnected")
    finally:
        try:
            srv.close()
        except Exception:
            pass


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
