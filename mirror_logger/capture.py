"""
capture.py — Cattura mirror via AF_PACKET + BPF (Linux only).

Performance:
  - filtro BPF in JIT kernel-side: scarta tutto il traffico non-IP/UDP/TCP
    prima di toccare lo userspace (-95% wake-up sul Pi)
  - SO_RCVBUF a 16 MB: nessun drop kernel a regime
  - SO_TIMESTAMPNS: timestamp ns dal driver, più accurato di time.time()
  - parsing manuale Ethernet/IP/UDP/TCP: ~3 µs per pacchetto (no oggetti)

Stack:
  AF_PACKET (raw)           kernel
       │
       ▼  BPF: solo IPv4 UDP/TCP  ─► tutto il resto droppato in kernel
       ▼
  recvmsg() → bytes ───► parse Eth/IP/UDP/TCP ───► MirrorParser

Reassembly TCP per stream DoIP (port 13400) gestito qui.
PCAP streaming opzionale (formato pcap classic, no scapy).

FakeCapture: backend di sviluppo per macOS (env MIRROR_FAKE=1).
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from mirror_parser import MirrorParser, RawFrame


_IS_LINUX = sys.platform.startswith('linux')

# AF_PACKET costanti — Linux only
ETH_P_ALL          = 0x0003
ETH_P_IP           = 0x0800
ETH_P_8021Q        = 0x8100
SO_ATTACH_FILTER   = 26
SO_TIMESTAMPNS     = 35


# ---------------------------------------------------------------------------
# BPF program — accetta IPv4 UDP/TCP, droppa il resto (7 istruzioni)
# ---------------------------------------------------------------------------

class _SockFilter(ctypes.Structure):
    _fields_ = [
        ('code', ctypes.c_ushort),
        ('jt',   ctypes.c_ubyte),
        ('jf',   ctypes.c_ubyte),
        ('k',    ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ('len',    ctypes.c_ushort),
        ('filter', ctypes.POINTER(_SockFilter)),
    ]


def _build_bpf_filter() -> tuple[bytes, ctypes.Array]:
    """Costruisce il filtro BPF: accetta solo IPv4 con proto UDP o TCP."""
    instr = [
        # ldh [12]   — ethertype
        (0x28, 0, 0, 12),
        # jeq #0x0800 ? continua : drop
        (0x15, 0, 4, ETH_P_IP),
        # ldb [23]   — IP protocol
        (0x30, 0, 0, 23),
        # jeq #17 (UDP) ? accept : continua
        (0x15, 2, 0, 17),
        # jeq #6 (TCP) ? accept : drop
        (0x15, 1, 0, 6),
        # drop
        (0x06, 0, 0, 0),
        # accept (full packet)
        (0x06, 0, 0, 0xFFFF),
    ]
    arr = (_SockFilter * len(instr))(*[_SockFilter(c, jt, jf, k) for c, jt, jf, k in instr])
    fprog = _SockFprog(len(instr), arr)
    return bytes(fprog), arr   # ritorna anche arr per evitare GC


# ---------------------------------------------------------------------------
# DoIP TCP reassembler (identico al precedente)
# ---------------------------------------------------------------------------

class _DoIPReassembler:
    _MAX_BUF_PER_FLOW = 256 * 1024

    def __init__(self) -> None:
        self._buffers: dict[tuple, bytearray] = {}
        self._lock = threading.Lock()

    def feed(self, key: tuple, segment: bytes) -> bytes:
        if not segment:
            return b''
        with self._lock:
            buf = self._buffers.get(key)
            if buf is None:
                buf = bytearray()
                self._buffers[key] = buf
            buf.extend(segment)
            if len(buf) > self._MAX_BUF_PER_FLOW:
                del buf[:-65536]

            out = bytearray()
            while len(buf) >= 8:
                ver = buf[0]; inv = buf[1]
                if ver not in (0x02, 0x03) or (ver ^ inv) != 0xFF:
                    del buf[0]
                    continue
                plen = int.from_bytes(buf[4:8], 'big')
                if plen > 65535:
                    del buf[0]
                    continue
                total = 8 + plen
                if len(buf) < total:
                    break
                out.extend(buf[:total])
                del buf[:total]
            return bytes(out)

    def drop(self, key: tuple) -> None:
        with self._lock:
            self._buffers.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                'flows':       len(self._buffers),
                'buffered_kb': round(sum(len(b) for b in self._buffers.values()) / 1024, 1),
            }


# ---------------------------------------------------------------------------
# PCAP writer minimale (no scapy)
# ---------------------------------------------------------------------------

class _SimplePcapWriter:
    """Scrive un file pcap classic (LINKTYPE_ETHERNET).  Append + sync opzionale."""

    _MAGIC          = 0xA1B2C3D4
    _LINKTYPE_ETHER = 1

    def __init__(self, path: str, snaplen: int = 65535):
        self._fh = open(path, 'wb', buffering=1024 * 1024)
        # pcap global header (24 byte)
        self._fh.write(struct.pack(
            '<IHHiIII',
            self._MAGIC, 2, 4, 0, 0, snaplen, self._LINKTYPE_ETHER,
        ))
        self._lock = threading.Lock()

    def write(self, ts_pkt: float, pkt: bytes) -> None:
        sec = int(ts_pkt)
        usec = int((ts_pkt - sec) * 1_000_000)
        n = len(pkt)
        hdr = struct.pack('<IIII', sec, usec, n, n)
        with self._lock:
            self._fh.write(hdr)
            self._fh.write(pkt)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# MirrorCapture — AF_PACKET (Linux)
# ---------------------------------------------------------------------------

class MirrorCapture:
    """Cattura mirror via AF_PACKET + BPF filter kernel-side.

    Compatibile solo Linux.  Su macOS/Win usare FakeCapture per sviluppo UI.
    """

    _RCVBUF_BYTES = 16 * 1024 * 1024   # 16 MB
    _RECV_BUF     = bytearray(2048)    # MTU + safety

    def __init__(
        self,
        interface: str,
        mirror_port: int,
        on_frame: Callable[[RawFrame], None],
        *,
        pcap_path: Optional[str] = None,
    ):
        if not _IS_LINUX:
            raise RuntimeError(
                'MirrorCapture richiede Linux (AF_PACKET).  '
                'Per sviluppo su macOS/Win: usa FakeCapture (env MIRROR_FAKE=1).'
            )

        self.interface   = interface
        self.mirror_port = int(mirror_port)
        self._pcap_path  = pcap_path

        # UDP: no dedup (i frame ripetuti ad alta frequenza sono legittimi).
        self._parser = MirrorParser(callback=on_frame, dedupe_window_s=0.0)

        self._doip_re = _DoIPReassembler()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._bpf_arr_keepalive = None
        self._pcap: Optional[_SimplePcapWriter] = None

        self._lock = threading.Lock()
        self._pkt_count   = 0
        self._frame_count = 0
        self._byte_count  = 0
        self._error_count = 0
        self._stats_t     = time.monotonic()
        self._pps  = 0.0
        self._kbps = 0.0

    # ------------------------------------------------------------------

    def start(self) -> None:
        # Apri AF_PACKET socket
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._RCVBUF_BYTES)
        except OSError:
            pass
        # Timestamp ns dal kernel
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        except OSError:
            pass
        # Filtro BPF (solo IPv4 UDP/TCP)
        bpf_bytes, bpf_arr = _build_bpf_filter()
        s.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, bpf_bytes)
        self._bpf_arr_keepalive = bpf_arr   # evita GC

        # Bind interfaccia
        s.bind((self.interface, ETH_P_ALL))
        self._sock = s

        # PCAP opzionale
        if self._pcap_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self._pcap_path)) or '.', exist_ok=True)
                self._pcap = _SimplePcapWriter(self._pcap_path)
            except Exception as e:
                print(f'[Capture] PCAP open fail: {e}', flush=True)
                self._pcap = None

        self._running = True
        self._thread = threading.Thread(
            target=self._recv_loop, name='mirror-capture', daemon=True,
        )
        self._thread.start()
        print(f'[Capture] AF_PACKET {self.interface}:{self.mirror_port} avviato', flush=True)

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RD)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._pcap:
            self._pcap.close()
            print(f'[Capture] PCAP chiuso → {self._pcap_path}', flush=True)
            self._pcap = None

    def stats(self) -> dict:
        with self._lock:
            base = {
                'pps':    round(self._pps, 1),
                'kbps':   round(self._kbps, 1),
                'frames': self._frame_count,
                'errors': self._error_count,
            }
        base.update({f'doip_{k}': v for k, v in self._doip_re.stats().items()})
        return base

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    def _recv_loop(self) -> None:
        sock = self._sock
        cmsg_space = socket.CMSG_SPACE(16)
        parser_parse  = self._parser.parse
        parser_doip   = self._parser.parse_doip_tcp
        doip_feed     = self._doip_re.feed
        doip_drop     = self._doip_re.drop
        mirror_port   = self.mirror_port
        pcap_write    = self._pcap.write if self._pcap else None

        while self._running:
            try:
                pkt, ancdata, _flags, _addr = sock.recvmsg(2048, cmsg_space)
            except (OSError, ValueError):
                if self._running:
                    with self._lock:
                        self._error_count += 1
                continue

            if not pkt:
                continue

            # Timestamp dal kernel (SO_TIMESTAMPNS) o fallback time.time()
            ts_pkt = 0.0
            for cmsg_level, cmsg_type, cmsg_data in ancdata:
                if cmsg_level == socket.SOL_SOCKET and cmsg_type == SO_TIMESTAMPNS:
                    if len(cmsg_data) >= 16:
                        secs, nsecs = struct.unpack('qq', cmsg_data[:16])
                        ts_pkt = secs + nsecs / 1e9
                    break
            if ts_pkt == 0.0:
                ts_pkt = time.time()

            n_frames = 0
            try:
                n_frames = self._dispatch(
                    pkt, ts_pkt, mirror_port,
                    parser_parse, parser_doip, doip_feed, doip_drop,
                )
            except Exception as e:
                with self._lock:
                    self._error_count += 1
                # Solo prima volta per non spammare
                if self._error_count < 5:
                    print(f'[Capture] dispatch err: {e}', flush=True)

            if pcap_write is not None:
                try:
                    pcap_write(ts_pkt, pkt)
                except Exception:
                    pass

            # Stats
            pkt_len = len(pkt)
            with self._lock:
                self._pkt_count   += 1
                self._frame_count += n_frames
                self._byte_count  += pkt_len
                now = time.monotonic()
                el = now - self._stats_t
                if el >= 2.0:
                    self._pps  = self._pkt_count / el
                    self._kbps = (self._byte_count * 8) / (el * 1000.0)
                    self._pkt_count  = 0
                    self._byte_count = 0
                    self._stats_t    = now

    @staticmethod
    def _dispatch(
        pkt: bytes,
        ts_pkt: float,
        mirror_port: int,
        parser_parse,
        parser_doip,
        doip_feed,
        doip_drop,
    ) -> int:
        """Parse Eth/IP/UDP/TCP manuale e instrada al parser/reassembler."""
        n = len(pkt)
        if n < 34:                # min: 14 Eth + 20 IP
            return 0

        # Ethernet header
        ethertype = (pkt[12] << 8) | pkt[13]
        ip_off = 14
        if ethertype == ETH_P_8021Q:
            if n < 38:
                return 0
            ethertype = (pkt[16] << 8) | pkt[17]
            ip_off = 18
        if ethertype != ETH_P_IP:
            return 0

        # IPv4 header
        ver_ihl = pkt[ip_off]
        if (ver_ihl >> 4) != 4:
            return 0
        ihl = (ver_ihl & 0x0F) * 4
        if ihl < 20 or ip_off + ihl > n:
            return 0

        # Frammentazione
        frag = ((pkt[ip_off + 6] << 8) | pkt[ip_off + 7]) & 0x1FFF
        if frag != 0:
            return 0

        proto    = pkt[ip_off + 9]
        src_ip   = pkt[ip_off + 12: ip_off + 16]
        dst_ip   = pkt[ip_off + 16: ip_off + 20]
        l4_off   = ip_off + ihl

        if proto == 17:          # UDP
            if l4_off + 8 > n:
                return 0
            sport = (pkt[l4_off]     << 8) | pkt[l4_off + 1]
            dport = (pkt[l4_off + 2] << 8) | pkt[l4_off + 3]
            ulen  = (pkt[l4_off + 4] << 8) | pkt[l4_off + 5]
            if ulen < 8:
                return 0
            payload_end = min(l4_off + ulen, n)
            payload = pkt[l4_off + 8: payload_end]
            if dport == mirror_port or sport == mirror_port:
                return parser_parse(payload, ts_pkt=ts_pkt)
            return 0

        if proto == 6:           # TCP
            if l4_off + 20 > n:
                return 0
            sport = (pkt[l4_off]     << 8) | pkt[l4_off + 1]
            dport = (pkt[l4_off + 2] << 8) | pkt[l4_off + 3]
            data_off = (pkt[l4_off + 12] >> 4) * 4
            if data_off < 20 or l4_off + data_off > n:
                return 0
            tcp_flags = pkt[l4_off + 13]
            payload = pkt[l4_off + data_off: n]

            if dport == mirror_port or sport == mirror_port:
                if not payload:
                    return 0
                return parser_parse(payload, ts_pkt=ts_pkt)

            if dport == 13400 or sport == 13400:
                key = (bytes(src_ip), sport, bytes(dst_ip), dport)
                if tcp_flags & 0x05:    # FIN o RST
                    doip_drop(key)
                if not payload:
                    return 0
                full = doip_feed(key, payload)
                if full:
                    return parser_doip(full, ts_pkt=ts_pkt)
            return 0

        return 0


# ---------------------------------------------------------------------------
# FakeCapture — backend di sviluppo (no rete reale)
# ---------------------------------------------------------------------------

class FakeCapture:
    """Genera frame fittizi per testare UI e logger su macOS/Win.

    Attivazione: `MIRROR_FAKE=1` in env, oppure istanziarla direttamente.
    """

    def __init__(
        self,
        interface: str,
        mirror_port: int,
        on_frame: Callable[[RawFrame], None],
        *,
        rate_pps: int = 200,
        pcap_path: Optional[str] = None,   # ignorato
    ):
        self.interface   = interface
        self.mirror_port = mirror_port
        self._on_frame   = on_frame
        self._rate_pps   = max(1, int(rate_pps))
        self._running    = False
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._frame_count = 0
        self._frames_window = 0
        self._stats_t     = time.monotonic()
        self._pps_real    = 0.0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._gen_loop, name='mirror-fake-capture', daemon=True,
        )
        self._thread.start()
        print(f'[FakeCapture] generatore @ {self._rate_pps} fps', flush=True)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def stats(self) -> dict:
        with self._lock:
            return {
                'pps':    round(self._pps_real, 1),
                'kbps':   0.0,
                'frames': self._frame_count,
                'errors': 0,
                'doip_flows': 0,
                'doip_buffered_kb': 0.0,
                'fake':   True,
            }

    def _gen_loop(self) -> None:
        period = 1.0 / self._rate_pps
        # Set di ID CAN simulati con payload incrementale
        ids = [0x0FD, 0x0A8, 0x0116, 0x0086, 0x0030, 0x0103]
        ch_for = {0x0FD: 100, 0x0A8: 100, 0x0116: 101, 0x0086: 101, 0x0030: 102, 0x0103: 102}
        counter = 0
        next_t = time.monotonic()

        while self._running:
            arb = ids[counter % len(ids)]
            data = bytes([
                counter & 0xFF,
                (counter >> 8) & 0xFF,
                0x55, 0xAA,
                0x00, 0x00, 0x00, 0x00,
            ])
            f = RawFrame(
                ts_ns      = time.time_ns(),
                ts_pkt     = time.time(),
                frame_type = 'CAN',
                channel_id = ch_for[arb],
                arb_id     = arb,
                flags      = 0,
                dlc        = 8,
                data       = data,
            )
            try:
                self._on_frame(f)
            except Exception:
                pass
            counter += 1

            with self._lock:
                self._frame_count += 1
                self._frames_window += 1
                now = time.monotonic()
                el = now - self._stats_t
                if el >= 2.0:
                    self._pps_real = self._frames_window / el
                    self._frames_window = 0
                    self._stats_t = now

            next_t += period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()


# ---------------------------------------------------------------------------
# Factory: sceglie automaticamente il backend
# ---------------------------------------------------------------------------

def make_capture(
    interface: str,
    mirror_port: int,
    on_frame: Callable[[RawFrame], None],
    *,
    pcap_path: Optional[str] = None,
):
    """Ritorna MirrorCapture su Linux, FakeCapture altrove o se MIRROR_FAKE=1."""
    if os.environ.get('MIRROR_FAKE', '').strip() in ('1', 'true', 'yes'):
        return FakeCapture(interface, mirror_port, on_frame, pcap_path=pcap_path)
    if not _IS_LINUX:
        print(
            '[Capture] non-Linux rilevato → uso FakeCapture (sviluppo).  '
            'Imposta MIRROR_FAKE=0 e gira su Linux per cattura reale.',
            flush=True,
        )
        return FakeCapture(interface, mirror_port, on_frame, pcap_path=pcap_path)
    return MirrorCapture(interface, mirror_port, on_frame, pcap_path=pcap_path)
