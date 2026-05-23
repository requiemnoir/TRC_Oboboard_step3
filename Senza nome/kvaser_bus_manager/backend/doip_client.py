"""Minimal DoIP client — DEPRECATED.

For full diagnostic scan functionality (DTC read, extended data, snapshot records),
use DoIPGatewayScanner from vag_scanner.py instead. This module is kept only for
backward compatibility with ethernet_manager.py's basic DoIP transport needs.
"""
import os
import socket
import time
import threading
import struct

class DoIPClient:
    def __init__(self, target_ip, logger):
        self.target_ip = target_ip
        self.port = 13400
        self.logger = logger
        self.sock = None
        self.running = False
        self.connected = False
        self.logical_address = 0x0E00 # Tester
        # Target logical address (ECU). 0x0000 means "unknown".
        self.target_address = 0x0000

    def start(self):
        self.running = True
        threading.Thread(target=self._run).start()

    def stop(self):
        self.running = False
        if self.sock:
            self.sock.close()

    def _run(self):
        # 1. Vehicle Discovery (UDP)
        print("DoIP: Sending Vehicle Discovery...")
        # Simplified: Assume we know IP, skip UDP broadcast for now or implement if needed.
        # Requirement says "Vehicle discovery".
        self._discover()

        # 2. TCP Connection (dual-stack)
        try:
            self.sock = socket.create_connection((self.target_ip, self.port), timeout=3.0)
            self.connected = True
            print(f"DoIP: Connected to {self.target_ip}")
            
            # 3. Routing Activation
            self._routing_activation()
            
        except Exception as e:
            print(f"DoIP Error: {e}")
            self.connected = False

    def _discover(self):
        # Send UDP Vehicle Identification Request
        # Header: Ver(1) + InvVer(1) + Type(2) + Len(4)
        # Type 0x0001 = Vehicle ID Request
        msg = struct.pack("!BBHL", 0x02, 0xFD, 0x0001, 0x0000)
        # Best-effort discovery:
        # - IPv6 multicast first (common on automotive gateways)
        # - IPv4 broadcast fallback
        msg = msg
        try:
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            try:
                s6.settimeout(0.3)
                try:
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
                except Exception:
                    pass
                mcast = str(os.getenv('KBSM_DOIP_DISCOVERY_IPV6_MCAST') or 'ff02::1').strip() or 'ff02::1'
                # No iface selection here; this client is minimal.
                s6.sendto(msg, (mcast, 13400, 0, 0))
            finally:
                try:
                    s6.close()
                except Exception:
                    pass
        except Exception:
            pass

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.sendto(msg, ('255.255.255.255', 13400))
            # In a real app we would listen for response to get IP and Logical Addr
        except:
            pass
        s.close()

    def _routing_activation(self):
        # Type 0x0005 = Routing Activation Request
        # Payload: SA(2) + ActivationType(1) + Reserved(1) + ISOReserved(4) + OEMReserved(4)
        payload = struct.pack("!HBBII", int(self.logical_address) & 0xFFFF, 0x00, 0x00, 0x00000000, 0x00000000)
        header = struct.pack("!BBHL", 0x02, 0xFD, 0x0005, len(payload))
        self.sock.send(header + payload)
        # Read response... (Skipped for brevity, assume success)
        print("DoIP: Routing Activation Sent")

    def send_uds(self, sid, did=0, data=b'', *, target_address=None):
        if not self.connected:
            return

        if target_address is not None:
            try:
                self.target_address = int(target_address) & 0xFFFF
            except Exception:
                pass
        
        # UDS Message: Type 0x8001
        # SA(2) + TA(2) + UDS_Data
        uds_payload = struct.pack("!B", sid)
        if did:
            uds_payload += struct.pack("!H", did)
        uds_payload += data
        
        doip_payload = struct.pack("!HH", self.logical_address, self.target_address) + uds_payload
        header = struct.pack("!BBHL", 0x02, 0xFD, 0x8001, len(doip_payload))
        
        self.sock.send(header + doip_payload)
        
        # Log Request
        try:
            self.logger.log_doip(time.time(), int(self.target_address), int(sid), int(did), 0)
        except Exception:
            pass
        
        # Receive Response (Simplified blocking read)
        # In real implementation, use a reader thread.
