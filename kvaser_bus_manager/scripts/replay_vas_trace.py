#!/usr/bin/env python3
"""Replay VAS trace commands to the gateway ECU via DoIP.

Sends the exact UDS sequence captured from the VAS/ODIS trace to see
how the gateway reacts — especially the RoutineControl 0x0253 auth
and whether mirror streaming resumes.
"""
import socket
import struct
import time
import sys

GW_HOST = 'fe80::200:ff:fe00:0'
IFACE = 'eth0'
SA = 0x0E00  # tester
TA = 0x4010  # gateway

scope_id = socket.if_nametoindex(IFACE)


def connect():
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((GW_HOST, 13400, 0, scope_id))
    # Routing Activation
    ra = struct.pack('!BBHI', 0x02, 0xFD, 0x0005, 7) + struct.pack('!H', SA) + b'\x00' * 5
    s.sendall(ra)
    resp = s.recv(4096)
    print(f"[RA] response: {resp.hex()}")
    time.sleep(0.3)
    return s


def uds_tx(s, uds_payload, *, timeout=2.0, label=""):
    diag = (struct.pack('!BBHI', 0x02, 0xFD, 0x8001, 4 + len(uds_payload))
            + struct.pack('!HH', SA, TA)
            + uds_payload)
    s.sendall(diag)

    # Collect responses (handle NRC 0x78 = requestCorrectlyReceivedResponsePending)
    all_data = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        s.settimeout(max(0.5, deadline - time.time()))
        try:
            chunk = s.recv(4096)
            all_data += chunk
            # Check if we got a final response (not 0x78 pending)
            uds_resp = _extract_uds(all_data)
            if uds_resp and not (len(uds_resp) >= 3 and uds_resp[0] == 0x7F and uds_resp[2] == 0x78):
                break
            if uds_resp and uds_resp[0] == 0x7F and uds_resp[2] == 0x78:
                print(f"  [{label}] NRC 0x78 (pending), waiting...")
                deadline = time.time() + 10  # extend for pending
                all_data = b''  # reset, wait for real response
        except socket.timeout:
            break

    uds_resp = _extract_uds(all_data)
    if uds_resp:
        print(f"  [{label}] TX: {uds_payload[:8].hex()}{'...' if len(uds_payload)>8 else ''}")
        print(f"  [{label}] RX: {uds_resp.hex()}")
    else:
        print(f"  [{label}] TX: {uds_payload.hex()}")
        print(f"  [{label}] RX raw: {all_data.hex()}")
    return uds_resp or all_data


def _extract_uds(data):
    """Extract UDS payload from DoIP diagnostic message."""
    offset = 0
    last_uds = None
    while offset + 8 <= len(data):
        ver, inv, ptype, plen = struct.unpack('!BBHI', data[offset:offset + 8])
        if ver != 0x02 or inv != 0xFD:
            offset += 1
            continue
        if offset + 8 + plen > len(data):
            break
        if ptype == 0x8001 and plen >= 5:
            last_uds = data[offset + 12:offset + 8 + plen]
        offset += 8 + plen
    return last_uds


def main():
    print("=" * 60)
    print("VAS Trace Replay — Gateway Mirror Activation")
    print("=" * 60)

    s = connect()

    # ── 1. TesterPresent (physical) ──
    print("\n── Step 1: TesterPresent ──")
    uds_tx(s, bytes([0x3E, 0x00]), label="TP")
    time.sleep(0.2)

    # ── 2. ReadDID F19E (ECU identification) ──
    print("\n── Step 2: ReadDID F19E (ECU name) ──")
    r = uds_tx(s, bytes([0x22, 0xF1, 0x9E]), label="F19E")
    time.sleep(0.2)

    # ── 3. ReadDID F1A2 (HW version) ──
    print("\n── Step 3: ReadDID F1A2 ──")
    uds_tx(s, bytes([0x22, 0xF1, 0xA2]), label="F1A2")
    time.sleep(0.2)

    # ── 4. Extended Session ──
    print("\n── Step 4: DiagnosticSessionControl Extended (0x03) ──")
    uds_tx(s, bytes([0x10, 0x03]), label="Session")
    time.sleep(0.3)

    # ── 5. WriteDID F198 (tester serial — as VAS did) ──
    print("\n── Step 5: WriteDID F198 (tester serial) ──")
    # From trace: 2E F198 03 86 C2 11 B2 07
    uds_tx(s, bytes([0x2E, 0xF1, 0x98, 0x03, 0x86, 0xC2, 0x11, 0xB2, 0x07]), label="F198")
    time.sleep(0.2)

    # ── 6. WriteDID F199 (programming date = today 26.02.12) ──
    print("\n── Step 6: WriteDID F199 (programming date) ──")
    uds_tx(s, bytes([0x2E, 0xF1, 0x99, 0x26, 0x02, 0x12]), label="F199")
    time.sleep(0.2)

    # ── 7. ReadDID F187 (part number) ──
    print("\n── Step 7: ReadDID F187 ──")
    uds_tx(s, bytes([0x22, 0xF1, 0x87]), label="F187")
    time.sleep(0.2)

    # ── 8. RoutineControl 0x0253 — SFD Authentication (challenge) ──
    print("\n── Step 8: RoutineControl 0x0253 — Request Challenge ──")
    # From trace: 31 01 02 53 01 01
    r = uds_tx(s, bytes([0x31, 0x01, 0x02, 0x53, 0x01, 0x01]), label="RC0253-challenge", timeout=5)
    time.sleep(0.5)

    # Extract challenge from response (if positive: 71 01 02 53 ...)
    challenge = None
    if r and len(r) > 5 and r[0] == 0x71:
        challenge = r[5:]
        print(f"  Challenge ({len(challenge)} bytes): {challenge.hex()}")

    # ── 9. RoutineControl 0x0253 — Send Response ──
    # We don't have the SFD keys, so we send the challenge data from VAS trace
    # to see what the gateway does (it will likely reject it)
    print("\n── Step 9: RoutineControl 0x0253 — Send Auth Response ──")
    # From VAS trace response bytes (we can't compute the real key)
    # Just try sending 31 01 02 53 00 01 with dummy data to see NRC
    r = uds_tx(s, bytes([0x31, 0x01, 0x02, 0x53, 0x00, 0x01]), label="RC0253-auth", timeout=12)
    time.sleep(0.5)

    # ── 10. ReadDID 0250 (adaptation channel map) ──
    print("\n── Step 10: ReadDID 0250 (adaptation channels) ──")
    uds_tx(s, bytes([0x22, 0x02, 0x50]), label="0250", timeout=3)
    time.sleep(0.2)

    # ── 11. RoutineControl 0x06A9 (adaptation list) ──
    print("\n── Step 11: RoutineControl 0x06A9 ──")
    uds_tx(s, bytes([0x31, 0x01, 0x06, 0xA9, 0x00]), label="RC06A9", timeout=5)
    time.sleep(0.3)

    # ── 12. ReadDID 0x096F (Mirror Mode) ──
    print("\n── Step 12: ReadDID 0x096F (Mirror Mode) ──")
    r = uds_tx(s, bytes([0x22, 0x09, 0x6F]), label="096F")
    if r and len(r) >= 24 and r[0] == 0x62:
        tb = r[3]
        ip_b = r[6:22]
        port = struct.unpack('!H', r[22:24])[0]
        import ipaddress
        if ip_b[:12] == bytes(10) + bytes([0xFF, 0xFF]):
            ip = str(ipaddress.IPv4Address(ip_b[12:16]))
        else:
            ip = str(ipaddress.IPv6Address(ip_b))
        print(f"  Mirror: target_bus={tb} dest={ip}:{port} {'ACTIVE' if tb else 'STOPPED'}")

    # ── 13. Keep session alive with TesterPresent ──
    print("\n── Step 13: TesterPresent loop (10s) ──")
    for i in range(5):
        uds_tx(s, bytes([0x3E, 0x80]), label=f"TP-{i+1}")
        time.sleep(2)

    s.close()
    print("\n" + "=" * 60)
    print("Replay complete. Check tcpdump for mirror traffic.")
    print("=" * 60)


if __name__ == '__main__':
    main()
