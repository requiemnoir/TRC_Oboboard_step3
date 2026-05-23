"""
mirror_parser.py — Parser payload AUTOSAR Bus Mirroring / VAG MLBevo / Iron Bird / Raw CAN-in-UDP.

Nessuna decodifica DBC/ARXML: produce solo frame raw con timestamp nanosecondo.
Riusabile come libreria pura (nessuna dipendenza da Flask o Scapy).

Formati supportati:
  0  VAG SOME/IP-wrapped  (0x02FD / 0xF302)
  1  AUTOSAR ISO 23150 / SWS_BM
  2  Iron Bird legacy     (magic 0xD00D)
  3  Raw CAN-in-UDP       ([ArbID:4][DLC:1][Data:N])
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Frame raw — struttura dati minima, zero allocazioni superflue
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RawFrame:
    """Un frame bus senza decodifica.

    ts_ns    : timestamp assoluto (time.time_ns() al momento della ricezione)
    ts_pkt   : timestamp Scapy del pacchetto Ethernet (secondi float, alta risoluzione)
    frame_type: 'CAN' | 'CAN-FD' | 'FlexRay' | 'LIN'
    channel_id: canale virtuale (100+net_id per CAN mirror, 200+net_id per FlexRay, 150+net_id per LIN)
    arb_id   : CAN arb-ID / FlexRay slot-ID / LIN frame-ID
    flags    : cycle FlexRay (6-bit) o 0 per CAN
    dlc      : lunghezza dati effettiva
    data     : bytes (max 64 per CAN-FD)
    """
    ts_ns: int
    ts_pkt: float
    frame_type: str
    channel_id: int
    arb_id: int
    flags: int
    dlc: int
    data: bytes


# ---------------------------------------------------------------------------
# Costanti AUTOSAR network types
# ---------------------------------------------------------------------------

_NET_CAN    = 0x01
_NET_CANFD  = 0x02
_NET_LIN    = 0x03
_NET_FR     = 0x04
# 0x05 Ethernet — ignorato

# ---------------------------------------------------------------------------
# Known VAG MLBevo CAN IDs (usati solo per scoring, non per filtro)
# ---------------------------------------------------------------------------

_KNOWN_VAG_IDS = frozenset({
    0x0FD, 0x0A8, 0x0A7, 0x0116, 0x0086, 0x007C, 0x0108, 0x00B5, 0x0040,
    0x030B, 0x0030, 0x023C, 0x03C0, 0x0103, 0x00AD, 0x00B3, 0x0121, 0x03D5,
})

# ---------------------------------------------------------------------------
# MirrorParser
# ---------------------------------------------------------------------------

class MirrorParser:
    """Parser stateless (thread-safe) per payload UDP/TCP mirror.

    Uso:
        parser = MirrorParser(callback=on_frame)
        parser.parse(payload_bytes, ts_pkt=pkt.time)

    on_frame(frame: RawFrame) viene chiamata in modo sincrono per ogni frame
    estratto dal payload.  Il callback deve essere veloce (accoda e torna).
    """

    def __init__(
        self,
        callback: Callable[[RawFrame], None],
        *,
        dedupe_window_s: float = 0.0,
    ):
        self._cb = callback
        # Euristica start-offset per il formato VAG proprietario
        self._vag_start_hint: int = 4
        # Deduplicazione: disabilitata di default. Va attivata solo per
        # stream TCP/DoIP ri-trasmessi. Su UDP NON usare: scarta frame
        # legittimi ad alta frequenza con payload identico.
        self._dedupe_window_s: float = max(0.0, float(dedupe_window_s))
        self._dedupe_recent: dict = {}
        self._dedupe_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Entry point principale
    # ------------------------------------------------------------------

    def parse(self, payload: bytes, *, ts_pkt: float = 0.0) -> int:
        """Parsa il payload e chiama callback per ogni frame estratto.

        Ritorna il numero di frame emessi.
        """
        if not payload or len(payload) < 5:
            return 0

        ts_ns = time.time_ns()

        # --- Formato 0: VAG SOME/IP (0x02FD / 0xF302) ---
        if len(payload) >= 20:
            srv = struct.unpack_from('!H', payload, 0)[0]
            met = struct.unpack_from('!H', payload, 2)[0]
            if srv == 0x02FD and met == 0xF302:
                return self._parse_vag(payload[16:], ts_ns, ts_pkt)

        # --- Formato 2: Iron Bird (0xD00D) ---
        if len(payload) >= 15 and struct.unpack_from('!H', payload, 0)[0] == 0xD00D:
            return self._parse_iron_bird(payload, ts_ns, ts_pkt)

        # --- Formato 1: AUTOSAR ISO 23150 ---
        if len(payload) >= 15 and payload[0] <= 0x0F:
            n = self._parse_autosar(payload, ts_ns, ts_pkt)
            if n > 0:
                return n

        # --- Formato 3: Raw CAN-in-UDP ---
        if len(payload) >= 5:
            return self._parse_raw_can(payload, ts_ns, ts_pkt)

        return 0

    # ------------------------------------------------------------------
    # Formato 1 — AUTOSAR Bus Mirroring (ISO 23150 / SWS_BM)
    # ------------------------------------------------------------------

    def _parse_autosar(self, payload: bytes, ts_ns: int, ts_pkt: float) -> int:
        """
        Header (7 byte):
          [StatusByte:1][Timestamp_us:4][SeqCounter:2]
        Frame entries (ripetuti):
          [NetType:1][NetID:1][FrameID:4][PayloadLen:2][Payload:N]
        """
        if len(payload) < 7:
            return 0

        # Estrai timestamp AUTOSAR (µs) — usato per calcolare offset relativo
        autosar_ts_us = struct.unpack_from('!I', payload, 1)[0]
        autosar_ts_s  = autosar_ts_us / 1_000_000.0

        offset = 7
        count = 0
        while offset + 8 <= len(payload):
            net_type  = payload[offset]
            net_id    = payload[offset + 1]
            frame_id  = struct.unpack_from('!I', payload, offset + 2)[0]
            pld_len   = struct.unpack_from('!H', payload, offset + 6)[0]
            offset   += 8

            if pld_len > 4095 or offset + pld_len > len(payload):
                break

            data = payload[offset:offset + pld_len]
            offset += pld_len

            if net_type == _NET_CAN or net_type == _NET_CANFD:
                ftype = 'CAN' if net_type == _NET_CAN else 'CAN-FD'
                arb_id = frame_id & 0x1FFFFFFF
                ch = 100 + net_id
                self._emit(arb_id, data, ftype, ch, 0, ts_ns, ts_pkt, autosar_ts_s)
                count += 1
            elif net_type == _NET_FR:
                self._emit(frame_id, data, 'FlexRay', 200 + net_id, 0, ts_ns, ts_pkt, autosar_ts_s)
                count += 1
            elif net_type == _NET_LIN:
                self._emit(frame_id & 0xFF, data, 'LIN', 150 + net_id, 0, ts_ns, ts_pkt, autosar_ts_s)
                count += 1

        return count

    # ------------------------------------------------------------------
    # Formato 0 — VAG proprietario (dentro SOME/IP 0x02FD/0xF302)
    # ------------------------------------------------------------------

    def _parse_vag(self, payload: bytes, ts_ns: int, ts_pkt: float) -> int:
        """Decoder VAG MLBevo con byte-scanning e dual-layout."""
        if len(payload) < 10:
            return 0

        def _try_at(off: int):
            if off + 10 > len(payload):
                return None
            candidates: list = []

            # Layout A: ts:2, bus:1, ntype:1, reserved:2, field:2, len:2
            bus_a   = payload[off + 2]
            ntype_a = payload[off + 3]
            field_a = struct.unpack_from('!H', payload, off + 6)[0]
            len_a   = struct.unpack_from('!H', payload, off + 8)[0]
            if bus_a <= 15 and 0 < len_a <= 254 and off + 10 + len_a <= len(payload):
                data_a = payload[off + 10:off + 10 + len_a]
                if ntype_a == 1 and field_a <= 0x7FF and len_a <= 8:
                    candidates.append((10 + len_a, 'CAN', bus_a, field_a, data_a, 0))
                elif ntype_a == 0:
                    slot = (field_a >> 8) & 0xFF
                    cyc  = field_a & 0xFF
                    if slot and 0 <= cyc <= 63 and len_a >= 8:
                        candidates.append((10 + len_a, 'FlexRay', bus_a, slot, data_a, cyc))

            # Layout B (solo se A non ha prodotto nulla)
            if not candidates:
                bus_b   = struct.unpack_from('!H', payload, off + 2)[0] & 0xFF
                ntype_b = payload[off + 4]
                field_b = struct.unpack_from('!H', payload, off + 6)[0]
                len_b   = struct.unpack_from('!H', payload, off + 8)[0]
                if bus_b <= 15 and 0 < len_b <= 254 and off + 10 + len_b <= len(payload):
                    data_b = payload[off + 10:off + 10 + len_b]
                    if ntype_b == 1 and field_b <= 0x7FF and len_b <= 8:
                        candidates.append((10 + len_b, 'CAN', bus_b, field_b, data_b, 0))
                    elif ntype_b == 0:
                        slot = payload[off + 6]
                        cyc  = payload[off + 7]
                        if slot and 0 <= cyc <= 63 and len_b >= 8:
                            candidates.append((10 + len_b, 'FlexRay', bus_b, slot, data_b, cyc))

            if not candidates:
                return None

            def _score(c):
                _, ftype, _, fid, fdata, _ = c
                if ftype == 'FlexRay':
                    s = 1000
                    if len(fdata) == 34: s += 100
                    elif len(fdata) in {12,16,20,24,32}: s += 40
                    if 1 <= fid <= 255: s += 20
                    return s
                s = 500
                if fid in _KNOWN_VAG_IDS: s += 200
                return s

            candidates.sort(key=_score, reverse=True)
            return candidates[0]

        start_offsets = []
        for o in [self._vag_start_hint, 4, 3, 2, 0]:
            if o not in start_offsets:
                start_offsets.append(o)

        best_frames: list = []
        best_score  = -1
        best_start  = start_offsets[0]

        for start in start_offsets:
            frames: list = []
            score = 0
            i = start
            while i + 10 <= len(payload):
                hit = _try_at(i)
                if hit is None:
                    i += 1
                    continue
                consumed, ftype, bus_ch, fid, data, flags = hit
                if not data:
                    i += 1
                    continue
                frames.append((ftype, bus_ch, fid, data, flags))
                score += 3 if (ftype == 'CAN' and fid in _KNOWN_VAG_IDS) else (2 if ftype == 'FlexRay' else 1)
                i += consumed
            if frames and score > best_score:
                best_frames, best_score, best_start = frames, score, start

        if not best_frames:
            return 0

        self._vag_start_hint = best_start
        count = 0
        for ftype, bus_ch, fid, data, flags in best_frames:
            ch = (200 + bus_ch) if ftype == 'FlexRay' else (100 + bus_ch)
            self._emit(fid & (0xFFFFFF if ftype == 'FlexRay' else 0x1FFFFFFF),
                       data, ftype, ch, flags, ts_ns, ts_pkt)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Formato 2 — Iron Bird (0xD00D)
    # ------------------------------------------------------------------

    def _parse_iron_bird(self, payload: bytes, ts_ns: int, ts_pkt: float) -> int:
        BLOCK = 15
        offset = 0
        count = 0
        while offset + BLOCK <= len(payload):
            magic, arb_id, dlc = struct.unpack_from('!HIB', payload, offset)
            if magic != 0xD00D:
                break
            data = payload[offset + 7: offset + 7 + min(dlc, 8)]
            self._emit(arb_id, data, 'CAN', 99, 0, ts_ns, ts_pkt)
            offset += BLOCK
            count += 1
        return count

    # ------------------------------------------------------------------
    # Formato 3 — Raw CAN-in-UDP
    # ------------------------------------------------------------------

    def _parse_raw_can(self, payload: bytes, ts_ns: int, ts_pkt: float) -> int:
        offset = 0
        count = 0
        while offset + 5 <= len(payload):
            arb_id = struct.unpack_from('!I', payload, offset)[0]
            dlc    = payload[offset + 4]
            if dlc > 64 or arb_id > 0x1FFFFFFF:
                break
            actual = min(dlc, len(payload) - offset - 5)
            data   = payload[offset + 5: offset + 5 + actual]
            self._emit(arb_id, data, 'CAN', 99, 0, ts_ns, ts_pkt)
            offset += 5 + max(actual, dlc)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Parser mirror DoIP (frame incapsulati in DoIP diagnostico)
    # ------------------------------------------------------------------

    def parse_doip_tcp(self, tcp_payload: bytes, *, ts_pkt: float = 0.0) -> int:
        """Estrae frame mirror da stream TCP DoIP (port 13400)."""
        offset = 0
        count = 0
        ts_ns = time.time_ns()

        while offset + 8 <= len(tcp_payload):
            ver     = tcp_payload[offset]
            inv_ver = tcp_payload[offset + 1]
            if ver not in (0x02, 0x03) or (ver ^ inv_ver) != 0xFF:
                offset += 1
                continue
            ptype = struct.unpack_from('!H', tcp_payload, offset + 2)[0]
            plen  = struct.unpack_from('!I', tcp_payload, offset + 4)[0]
            if plen > 65535 or offset + 8 + plen > len(tcp_payload):
                break
            body   = tcp_payload[offset + 8: offset + 8 + plen]
            offset += 8 + plen

            if ptype != 0x8001 or len(body) < 5:
                continue
            src_addr = struct.unpack_from('!H', body, 0)[0]
            if (src_addr & 0xFF00) != 0x4000:
                continue
            uds = body[4:]
            if not uds or uds[0] in (0x7E, 0x50, 0x51, 0x67, 0x6E, 0x7F, 0x3E):
                continue
            if len(uds) >= 15 and uds[0] <= 0x0F:
                count += self._parse_autosar(uds, ts_ns, ts_pkt)
        return count

    # ------------------------------------------------------------------
    # Emit interno con deduplicazione
    # ------------------------------------------------------------------

    def _emit(self, arb_id: int, data: bytes, frame_type: str,
              channel_id: int, flags: int,
              ts_ns: int, ts_pkt: float,
              _autosar_ref: float = 0.0) -> None:
        data = bytes(data)
        if self._dedupe_window_s > 0.0:
            now = time.monotonic()
            key = (frame_type, channel_id, arb_id, flags & 0xFF, data)
            with self._dedupe_lock:
                # Cleanup proattivo prima di crescere oltre soglia
                if len(self._dedupe_recent) > 1024:
                    cutoff = now - self._dedupe_window_s * 4
                    self._dedupe_recent = {
                        k: v for k, v in self._dedupe_recent.items() if v > cutoff
                    }
                last = self._dedupe_recent.get(key)
                if last is not None and (now - last) <= self._dedupe_window_s:
                    return
                self._dedupe_recent[key] = now

        frame = RawFrame(
            ts_ns      = ts_ns,
            ts_pkt     = ts_pkt,
            frame_type = frame_type,
            channel_id = channel_id,
            arb_id     = arb_id & 0x1FFFFFFF,
            flags      = int(flags or 0),
            dlc        = len(data),
            data       = data,
        )
        self._cb(frame)
