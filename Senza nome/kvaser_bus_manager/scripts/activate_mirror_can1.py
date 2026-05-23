#!/usr/bin/env python3
"""
Activate Mirror for CAN1 over Ethernet.
Target IP: 192.168.200.1
Target Port: 30490
"""
import socket
import struct
import time
import sys
import ipaddress

GW_HOST = 'fe80::200:ff:fe00:0'
IFACE = 'eth0'
SA = 0x0E00  # tester
TA = 0x4010  # gateway

try:
    scope_id = socket.if_nametoindex(IFACE)
except OSError:
    print(f"Interface {IFACE} not found. Using scope_id=0.")
    scope_id = 0

def connect():
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((GW_HOST, 13400, 0, scope_id))
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)
        
    ra = struct.pack('!BBHI', 0x02, 0xFD, 0x0005, 7) + struct.pack('!H', SA) + b'\x00' * 5
    s.sendall(ra)
    resp = s.recv(4096)
    time.sleep(0.3)
    return s

def _extract_uds(payload):
    # DoIP Header = 8 bytes
    if len(payload) < 8: return None
    type_ = (payload[2] << 8) | payload[3]
    length = (payload[4] << 24) | (payload[5] << 16) | (payload[6] << 8) | payload[7]
    
    # Payload[8:] contains SA(2)+TA(2)+UDS_DATA(len-4)
    if type_ == 0x8001 and len(payload) >= 8 + length:
        return payload[8+4 : 8+length]
    return None

def uds_tx(s, uds_payload, label=""):
    print(f"[{label}] -> {uds_payload.hex()}")
    length_field = 4 + len(uds_payload)
    diag = (struct.pack('!BBHI', 0x02, 0xFD, 0x8001, length_field)
            + struct.pack('!HH', SA, TA)
            + uds_payload)
    s.sendall(diag)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        s.settimeout(max(0.5, deadline - time.time()))
        try:
            chunk = s.recv(4096)
            if not chunk: continue
            
            # Simple check for multiple messages in one packet or just raw bytes
            # For simplicity, assume one response
            uds_resp = _extract_uds(chunk)
            if uds_resp:
                print(f"[{label}] <- {uds_resp.hex()}")
                # Pending check
                if len(uds_resp) >= 3 and uds_resp[0] == 0x7F and uds_resp[2] == 0x78:
                    continue
                return uds_resp
        except socket.timeout:
            continue
    return None

def main():
    s = connect()
    
    # Session 0x03
    uds_tx(s, bytes([0x10, 0x03]), label="Session 03")
    time.sleep(0.2)
    
    # 1. READ 0x0902 first to check status
    resp = uds_tx(s, bytes([0x22, 0x09, 0x02]), label="Read 0902")
    if resp and resp[0] == 0x62:
        val = resp[3]
        if val != 0x01:
            print(f"Enabling Dev Messages (Current: {val}, Setting to 0x01)...")
            uds_tx(s, bytes([0x2E, 0x09, 0x02, 0x01]), label="Write 0902")
        else:
            print("Dev Messages already active (0x01).")

    # 2. READ 0x096F
    # This might fail if length is huge, but let's see.
    # Actually, we just want to write.
    
    # 3. Construct Mirror Payload (0x096F)
    # Byte 0: Target Bus (2 = Ethernet)
    # Byte 1: CAN Mask (0x01 = CAN1)
    # Byte 2: FlexRay/LIN (0x00)
    # Byte 3-18: IP (16 bytes)
    # Byte 19-20: Port (30490)
    
    target_bus = 0x02
    can_mask = 0x01
    fr_lin_mask = 0x00
    
    # ::ffff:192.168.200.1 (IPv4 Mapped)
    # ip_str = "192.168.200.1"
    # ipv6 = ipaddress.IPv6Address(f"::ffff:{ip_str}")
    
    # Try IPv6 Link-Local of the host (eth0)
    # fe80::4b1d:47c6:5f44:cbcd
    ip_str = "fe80::4b1d:47c6:5f44:cbcd"
    ipv6 = ipaddress.IPv6Address(ip_str)
    
    ip_bytes = ipv6.packed # 16 bytes
    
    print(f"Target IP: {ip_str} ({ip_bytes.hex()})")
    
    port = 30490
    port_bytes = struct.pack('!H', port)
    
    payload = bytes([target_bus, can_mask, fr_lin_mask]) + ip_bytes + port_bytes
    
    # Write DID 096F
    print(f"Writing Mirror Config (Length {len(payload)})...")
    uds_data = bytes([0x2E, 0x09, 0x6F]) + payload
    resp = uds_tx(s, uds_data, label="Write 096F")
    
    if resp and resp[0] == 0x6E:
        print(f"SUCCESS: Mirror Configured for CAN1 -> Ethernet ({ip_str}:30490)")
    else:
        print("FAILED to write Mirror Config")

    s.close()

if __name__ == '__main__':
    main()
