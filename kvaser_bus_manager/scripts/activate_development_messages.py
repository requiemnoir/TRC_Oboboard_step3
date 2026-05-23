#!/usr/bin/env python3
"""
Activate Development Messages (DID 0x0902).
Hypothesis: This master switch was disabled by the Stop command and prevents
the mirror from sending packets even if 0x096F says active.
"""
import socket
import struct
import time
import sys
import unittest.mock

GW_HOST = 'fe80::200:ff:fe00:0'
IFACE = 'eth0'
SA = 0x0E00  # tester
TA = 0x4010  # gateway

try:
    scope_id = socket.if_nametoindex(IFACE)
except OSError:
    print(f"Interface {IFACE} not found. Using scope_id=0 (may fail).")
    scope_id = 0

def connect():
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((GW_HOST, 13400, 0, scope_id))
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
        
    # Routing Activation
    # source address 0E00
    ra = struct.pack('!BBHI', 0x02, 0xFD, 0x0005, 7) + struct.pack('!H', SA) + b'\x00' * 5
    s.sendall(ra)
    resp = s.recv(4096)
    if resp:
        print(f"[RA] response: {resp.hex()}")
        if resp[0] == 0x03 and resp[4] == 0x10:
            print("[RA] Routing Activation Success")
        else:
            print("[RA] Routing Activation may have failed")
    else:
        print("[RA] No response")
    
    time.sleep(0.3)
    return s

def _extract_uds(payload):
    # payload is DoIP message. header is 8 bytes for 0x8001
    # 02 FD 80 01 <Len> <SA> <TA> <UDS...>
    # Header 4 bytes, len 4 bytes. 
    # But DoIP header is: Version(1), INV(1), Type(2), Len(4).
    if len(payload) < 8:
        return None
    type_ = (payload[2] << 8) | payload[3]
    length = (payload[4] << 24) | (payload[5] << 16) | (payload[6] << 8) | payload[7]
    
    if type_ == 0x8001:
        # payload[8:] is source(2)+target(2)+uds_data
        if len(payload) >= 8 + length:
            return payload[8+4 : 8+length]
    return None

def uds_tx(s, uds_payload, *, timeout=2.0, label=""):
    print(f"[{label}] -> {uds_payload.hex()}")
    # DoIP Header: Ver(02), Inv(FD), Type(8001), Len(4+len(uds))
    length_field = 4 + len(uds_payload)
    diag = (struct.pack('!BBHI', 0x02, 0xFD, 0x8001, length_field)
            + struct.pack('!HH', SA, TA)
            + uds_payload)
    s.sendall(diag)

    deadline = time.time() + timeout
    while time.time() < deadline:
        s.settimeout(max(0.5, deadline - time.time()))
        try:
            chunk = s.recv(4096)
            if not chunk: continue
            
            # Simple parsing of response
            uds_resp = _extract_uds(chunk)
            if uds_resp:
                print(f"[{label}] <- {uds_resp.hex()}")
                # Check for RCRRP (7F xx 78)
                if len(uds_resp) >= 3 and uds_resp[0] == 0x7F and uds_resp[2] == 0x78:
                    print("  ... pending ...")
                    continue
                return uds_resp
        except socket.timeout:
            continue
    return None

def main():
    s = connect()
    
    # 1. Extended Session
    uds_tx(s, bytes([0x10, 0x03]), label="Session 03")
    time.sleep(0.2)
    
    # 2. Read DID 0902
    resp = uds_tx(s, bytes([0x22, 0x09, 0x02]), label="Read 0902")
    current_val = None
    if resp and resp[0] == 0x62 and len(resp) >= 4:
        # 62 09 02 <val>
        current_val = resp[3]
        print(f"Current Value of 0902: 0x{current_val:02X}")
    else:
        print("Failed to read 0902")

    # 3. Try to write various values
    vals_to_try = [0x09, 0x08, 0x04, 0x02] # Group1+XCP, XCP, Group3, Group2
    
    for v in vals_to_try:
        print(f"Attempting to write 0x0902 = 0x{v:02X}")
        resp = uds_tx(s, bytes([0x2E, 0x09, 0x02, v]), label=f"Write {v:02X}")
        
        if resp:
            if resp[0] == 0x6E:
                print(f"SUCCESS! Written 0902 = 0x{v:02X}.")
                break
            elif resp[0] == 0x7F:
                error_code = resp[2]
                print(f"Error writing 0x{v:02X}: NRC={hex(error_code)}")
        time.sleep(0.5)

    # 4. Read back
    uds_tx(s, bytes([0x22, 0x09, 0x02]), label="Read 0902 post-write")

    s.close()

if __name__ == '__main__':
    main()
