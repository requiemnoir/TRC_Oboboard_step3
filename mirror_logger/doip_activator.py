"""
doip_activator.py — Attivazione mirror gateway via DoIP / UDS 0x2E.

Flusso:
  1. UDP Vehicle Discovery (broadcast + multicast IPv6)
  2. TCP Routing Activation (logical address 0x0E00)
  3. UDS WriteDataByIdentifier 0x2E 0x096F (Mirror Mode DID)
  4. Keep-alive TesterPresent (0x3E) ogni 2s

Non fa nulla di più.  Nessuna diagnostica, nessun scan.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import struct
import threading
import time
from typing import Optional

try:
    import fcntl  # POSIX advisory lock
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

# DID standard Mirror Mode (VAG/AUTOSAR)
_DID_MIRROR_MODE     = 0x096F
_TESTER_ADDR         = 0x0E00   # logical address tester
_DOIP_VERSION        = 0x02
_DOIP_INV_VERSION    = 0xFD
_DOIP_PORT           = 13400

# File lock cross-process: serializza l'apertura della sessione DoIP al
# gateway tra mirror_logger.DoIPActivator e kvaser_bus_manager (ScanTools,
# Sentinel MIL polling). Senza questo, entrambi possono aprire una sessione
# con tester address 0x0E00 e il gateway risponde solo all'ultimo.
_GATEWAY_LOCK_CANDIDATES = ('/var/run/trc_doip_gateway.lock',
                            '/tmp/trc_doip_gateway.lock')


@contextlib.contextmanager
def _gateway_doip_lock(timeout_s: float = 0.0):
    """Advisory file lock per la sessione DoIP al gateway.

    timeout_s=0  : non-bloccante, solleva BlockingIOError se occupato.
    timeout_s>0  : bloccante con polling (intervallo 100 ms).
    Su sistemi senza fcntl (Windows) cade a no-op.
    """
    if not _HAS_FCNTL:
        yield
        return

    path = None
    fh = None
    for candidate in _GATEWAY_LOCK_CANDIDATES:
        try:
            fh = open(candidate, 'a+')
            path = candidate
            break
        except (PermissionError, FileNotFoundError):
            continue
    if fh is None:
        yield  # nessun path scrivibile → degradiamo a no-op
        return

    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    locked = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield path
    finally:
        if locked:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


def _doip_header(payload_type: int, payload_len: int) -> bytes:
    return struct.pack('!BBHL', _DOIP_VERSION, _DOIP_INV_VERSION, payload_type, payload_len)


def _build_mirror_did_payload(
    *,
    dest_ip: str,
    dest_port: int,
    can_networks: list[int],
    flexray_channels: list[str],
    lin_networks: list[int],
    target_bus: int = 2,
) -> bytes:
    """Costruisce il payload del DID 0x096F secondo la specifica AUTOSAR/VAG.

    Layout:
      byte 0  : target_bus  (0=off, 1=CAN_diag, 2=Ethernet)
      byte 1  : CAN bitmask (bit0=CAN1 … bit7=CAN8)
      byte 2  : FlexRay/LIN bitmask (bit0=FR_A, bit1=FR_B, bit4=LIN1, bit5=LIN2, bit6=LIN3)
      byte 3-18: IPv6 (16 byte, o IPv4-mapped)
      byte 19-20: port uint16 big-endian
    """
    can_mask = 0
    for n in can_networks:
        i = int(n)
        if 1 <= i <= 8:
            can_mask |= 1 << (i - 1)

    fr_mask = 0
    for ch in flexray_channels:
        c = str(ch or '').upper()
        if c == 'A': fr_mask |= 0x01
        if c == 'B': fr_mask |= 0x02

    lin_mask = 0
    for n in lin_networks:
        i = int(n)
        if i == 1: lin_mask |= 0x10
        if i == 2: lin_mask |= 0x20
        if i == 3: lin_mask |= 0x40

    ip = ipaddress.ip_address(dest_ip)
    if isinstance(ip, ipaddress.IPv4Address):
        ip16 = ipaddress.IPv6Address('::ffff:' + dest_ip).packed
    else:
        ip16 = ip.packed

    port_be = struct.pack('!H', int(dest_port) & 0xFFFF)

    return bytes([
        int(target_bus) & 0xFF,
        can_mask & 0xFF,
        (fr_mask | lin_mask) & 0xFF,
    ]) + ip16 + port_be


# ---------------------------------------------------------------------------
# DoIPActivator
# ---------------------------------------------------------------------------

class DoIPActivator:
    """Apre connessione DoIP, invia il DID mirror e mantiene keep-alive.

    Uso:
        act = DoIPActivator(
            gateway_ip='192.168.0.140',
            mirror_dest_ip='192.168.0.100',
            mirror_dest_port=30490,
            can_networks=[1, 2, 3],
            flexray_channels=['A', 'B'],
        )
        act.start()   # non bloccante
        ...
        act.stop()
    """

    def __init__(
        self,
        gateway_ip: str,
        mirror_dest_ip: str,
        mirror_dest_port: int,
        *,
        can_networks: list[int]      = (),
        flexray_channels: list[str]  = (),
        lin_networks: list[int]      = (),
        target_bus: int              = 2,
        gateway_logical_addr: int    = 0x0000,  # 0 = da scoprire
        mirror_did: int              = _DID_MIRROR_MODE,
        keepalive_interval_s: float  = 2.0,
        connect_timeout_s: float     = 5.0,
    ):
        self._gateway_ip          = gateway_ip
        self._mirror_dest_ip      = mirror_dest_ip
        self._mirror_dest_port    = int(mirror_dest_port)
        self._can_networks        = list(can_networks or [])
        self._flexray_channels    = list(flexray_channels or [])
        self._lin_networks        = list(lin_networks or [])
        self._target_bus          = int(target_bus)
        self._gateway_logical     = int(gateway_logical_addr)
        self._mirror_did          = int(mirror_did) & 0xFFFF
        self._keepalive_s         = max(0.5, float(keepalive_interval_s))
        self._connect_timeout_s   = max(1.0, float(connect_timeout_s))

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected  = False
        self._activated  = False
        self._last_error = ''

    # ------------------------------------------------------------------
    # Pubblico
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name='doip-activator',
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_sock()
        if self._thread:
            self._thread.join(timeout=5.0)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def activated(self) -> bool:
        return self._activated

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> dict:
        return {
            'connected':  self._connected,
            'activated':  self._activated,
            'gateway_ip': self._gateway_ip,
            'last_error': self._last_error,
        }

    # ------------------------------------------------------------------
    # Thread principale
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Acquisizione lock cross-process per la sola fase di setup
                # (routing activation + WriteDID). Il keepalive resta fuori
                # dal lock: ScanTools del KBM lo interromperà chiudendo la
                # connessione TCP, e questo thread la riaprirà al ciclo
                # successivo riacquisendo il lock.
                with _gateway_doip_lock(timeout_s=30.0):
                    self._connect_and_activate()
                self._keepalive_loop()
            except BlockingIOError:
                self._last_error = 'gateway DoIP busy (altro processo connesso)'
                self._connected  = False
                self._activated  = False
                print(f'[DoIP] {self._last_error} — retry in 5s', flush=True)
                self._stop_event.wait(5.0)
            except Exception as e:
                self._last_error = str(e)
                self._connected  = False
                self._activated  = False
                print(f'[DoIP] errore: {e} — riconnessione in 5s', flush=True)
                self._close_sock()
                self._stop_event.wait(5.0)

    def _connect_and_activate(self) -> None:
        # 1. Discovery UDP (best-effort, non bloccante)
        self._discover()

        # 2. Connessione TCP
        print(f'[DoIP] connessione a {self._gateway_ip}:{_DOIP_PORT}', flush=True)
        self._sock = socket.create_connection(
            (self._gateway_ip, _DOIP_PORT),
            timeout=self._connect_timeout_s,
        )
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(5.0)
        self._connected = True

        # 3. Routing Activation
        self._routing_activation()

        # 4. WriteDataByIdentifier 0x2E → DID mirror
        self._write_mirror_did()

    def _discover(self) -> None:
        """Vehicle Discovery UDP broadcast + IPv6 multicast (best-effort)."""
        msg = _doip_header(0x0001, 0)
        for af, addr in [
            (socket.AF_INET6, (str(os.getenv('MIRROR_DOIP_MCAST6', 'ff02::1')), _DOIP_PORT, 0, 0)),
            (socket.AF_INET,  ('255.255.255.255', _DOIP_PORT)),
        ]:
            try:
                s = socket.socket(af, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(0.2)
                s.sendto(msg, addr)
                s.close()
            except Exception:
                pass

    def _routing_activation(self) -> None:
        """DoIP Routing Activation Request (type 0x0005)."""
        # SA(2) + ActivationType(1) + Reserved(1) + ISOReserved(4) + OEMReserved(4)
        payload = struct.pack('!HBBII', _TESTER_ADDR & 0xFFFF, 0x00, 0x00, 0, 0)
        self._sock.sendall(_doip_header(0x0005, len(payload)) + payload)

        # Leggi risposta (type 0x0006)
        resp = self._recv_doip_response(expected_type=0x0006, timeout_s=3.0)
        if resp is None:
            print('[DoIP] routing activation: nessuna risposta (continuo comunque)', flush=True)
        else:
            rcode = resp[2] if len(resp) > 2 else 0xFF
            print(f'[DoIP] routing activation response code: 0x{rcode:02X}', flush=True)
            if self._gateway_logical == 0 and len(resp) >= 5:
                # resp[3:5] = Logical Address ECU
                self._gateway_logical = struct.unpack_from('!H', resp, 3)[0]
                print(f'[DoIP] gateway logical addr: 0x{self._gateway_logical:04X}', flush=True)

    def _write_mirror_did(self) -> None:
        """UDS 0x2E WriteDataByIdentifier per il DID mirror mode."""
        did_payload = _build_mirror_did_payload(
            dest_ip           = self._mirror_dest_ip,
            dest_port         = self._mirror_dest_port,
            can_networks      = self._can_networks,
            flexray_channels  = self._flexray_channels,
            lin_networks      = self._lin_networks,
            target_bus        = self._target_bus,
        )
        # UDS: SID=0x2E + DID(2) + payload
        uds_req = bytes([0x2E]) + struct.pack('!H', self._mirror_did) + did_payload

        # DoIP diagnostics request (type 0x8001)
        gw_addr = self._gateway_logical if self._gateway_logical else 0x0000
        diag_payload = struct.pack('!HH', _TESTER_ADDR, gw_addr) + uds_req
        self._sock.sendall(_doip_header(0x8001, len(diag_payload)) + diag_payload)

        resp = self._recv_doip_response(expected_type=0x8001, timeout_s=3.0)
        if resp is not None and len(resp) >= 5:
            uds_resp = resp[4:]
            if uds_resp and uds_resp[0] == 0x6E:
                self._activated = True
                self._last_error = ''
                print(f'[DoIP] mirror mode ATTIVATO (DID 0x{self._mirror_did:04X})', flush=True)
            elif uds_resp and uds_resp[0] == 0x7F:
                nrc = uds_resp[2] if len(uds_resp) > 2 else 0xFF
                self._activated = False
                self._last_error = f'UDS NRC 0x{nrc:02X} on WriteDID 0x{self._mirror_did:04X}'
                print(f'[DoIP] NRC 0x{nrc:02X} — mirror NON attivato', flush=True)
            else:
                self._activated = False
                hexed = (uds_resp or b'').hex()
                self._last_error = f'UDS resp inattesa: {hexed}'
                print(f'[DoIP] {self._last_error}', flush=True)
        else:
            self._activated = False
            self._last_error = 'nessuna risposta UDS WriteDID'
            print(f'[DoIP] {self._last_error}', flush=True)

    def _keepalive_loop(self) -> None:
        """Invia TesterPresent periodico per mantenere la sessione DoIP aperta."""
        # UDS TesterPresent (0x3E 0x80 = suppress positive response)
        uds_tp = bytes([0x3E, 0x80])
        gw_addr = self._gateway_logical if self._gateway_logical else 0x0000
        diag_payload = struct.pack('!HH', _TESTER_ADDR, gw_addr) + uds_tp

        print(f'[DoIP] keepalive avviato ogni {self._keepalive_s}s', flush=True)
        while not self._stop_event.is_set():
            self._stop_event.wait(self._keepalive_s)
            if self._stop_event.is_set():
                break
            try:
                self._sock.sendall(_doip_header(0x8001, len(diag_payload)) + diag_payload)
            except Exception as e:
                raise RuntimeError(f'keepalive fallito: {e}') from e

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recv_doip_response(
        self, *, expected_type: int, timeout_s: float = 3.0
    ) -> Optional[bytes]:
        """Riceve una risposta DoIP con header 8-byte + body."""
        deadline = time.monotonic() + timeout_s
        buf = b''
        try:
            self._sock.settimeout(max(0.1, timeout_s))
            while time.monotonic() < deadline:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 8:
                    ptype = struct.unpack_from('!H', buf, 2)[0]
                    plen  = struct.unpack_from('!I', buf, 4)[0]
                    if len(buf) < 8 + plen:
                        break
                    body = buf[8: 8 + plen]
                    buf  = buf[8 + plen:]
                    if ptype == expected_type:
                        return body
        except socket.timeout:
            pass
        except Exception:
            pass
        return None

    def _close_sock(self) -> None:
        self._connected = False
        self._activated = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
