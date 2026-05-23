import time
import struct
import socket
import threading
from scapy.all import send, IP, UDP, TCP, Ether

def send_someip():
    print("Starting SOME/IP Simulation...")
    while True:
        # SOME/IP Header Construction
        # Service ID: 0x1234, Method ID: 0x0001
        msg_id = (0x1234 << 16) | 0x0001
        length = 8 + 4 # 8 bytes remaining header + 4 bytes payload
        # Client ID: 0x0001, Session ID: 0x0001
        req_id = (0x0001 << 16) | 0x0001
        proto_ver = 0x01
        iface_ver = 0x01
        msg_type = 0x01 # Request
        ret_code = 0x00
        
        header = struct.pack('!IIIBBBB', msg_id, length, req_id, proto_ver, iface_ver, msg_type, ret_code)
        payload = b'\xDE\xAD\xBE\xEF'
        
        pkt = IP(dst="127.0.0.1")/UDP(dport=30490)/header/payload
        send(pkt, verbose=False)
        time.sleep(0.5)

def send_doip_announcement():
    print("Starting DoIP Simulation...")
    while True:
        # Vehicle Announcement Message (UDP)
        # Header: Ver(2) + InvVer(0xFD) + Type(0x0004) + Len(32)
        # Payload: VIN(17) + LogicalAddr(2) + EID(6) + GID(6) + FurtherAction(1) + SyncStatus(1)
        
        header = struct.pack("!BBHL", 0x02, 0xFD, 0x0004, 33)
        vin = b'WAUZZZQ8XKA000001'
        logical_addr = struct.pack("!H", 0x1000)
        eid = b'\x00'*6
        gid = b'\x00'*6
        
        payload = vin + logical_addr + eid + gid + b'\x00' # + SyncStatus (missing in len calc above, fixed to 32+1=33)
        
        pkt = IP(dst="127.0.0.1")/UDP(dport=13400)/header/payload
        send(pkt, verbose=False)
        time.sleep(2)

if __name__ == "__main__":
    print("Simulating Automotive Ethernet Traffic on Loopback...")
    print("NOTE: Ensure you select 'lo' interface and '127.0.0.1' target IP in the Web UI.")
    
    t1 = threading.Thread(target=send_someip, daemon=True)
    t2 = threading.Thread(target=send_doip_announcement, daemon=True)
    
    t1.start()
    t2.start()

    # t1.join() and t2.join() removed to allow KeyboardInterrupt
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping Simulation...")
