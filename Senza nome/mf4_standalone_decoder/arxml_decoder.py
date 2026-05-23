"""CAN frame decoder using ARXML catalogue definitions.

Provides the same decode interface as DBCLoader (returns
``{"name": "<msg>", "signals": {<sig>: <value>, ...}}`` )
but uses AUTOSAR ARXML definitions instead of DBC files.

Advantages over DBC:
  - Covers ALL buses in one file (CCAN, HCAN, ECAN, ICAN, KCAN, …)
  - Includes COMPU-METHOD (factor/offset, text tables)
  - Covers FlexRay frames too
  - Works even when DBC files fail to parse

Usage:
    from arxml_decoder import ArxmlDecoder
    dec = ArxmlDecoder()
    dec.load_from_catalog(catalog)      # ArxmlCatalog
    result = dec.decode(0x0FD, data)    # {"name":"ESP_21_…","signals":{…}}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


class ArxmlDecoder:
    """Decode raw CAN frames using an ArxmlCatalog.

    Builds an internal index  CAN-ID → [(frame_name, pdu_signals, bus_desc)]
    for O(1) lookup.  Multiple frames with the same ID (different buses) are
    supported – all matches are tried and the first that produces signals wins.
    """

    def __init__(self):
        self._catalog = None
        # CAN-ID → list of _FrameEntry
        self._can_index: Dict[int, List[_FrameEntry]] = {}
        # FlexRay slot-ID → list of _FrameEntry
        self._fr_index: Dict[int, List[_FrameEntry]] = {}
        # LIN frame-ID → list of _FrameEntry
        self._lin_index: Dict[int, List[_FrameEntry]] = {}
        self._loaded = False

    # ── public API ──────────────────────────────────────────────────────

    def load_from_catalog(self, catalog) -> int:
        """Build the decode index from an ArxmlCatalog.

        Returns the total number of frames indexed (CAN + FlexRay + LIN).
        """
        self._catalog = catalog
        self._can_index.clear()
        self._fr_index.clear()
        self._lin_index.clear()
        self._loaded = False

        if catalog is None:
            return 0

        count = 0
        for frame in catalog.frames.values():
            if frame.bus_type not in ('CAN', 'FLEXRAY', 'LIN') or not frame.frame_id:
                continue
            pdu = catalog.pdus.get(frame.pdu_name) if frame.pdu_name else None
            if pdu is None or not pdu.signals:
                continue  # no signals to decode

            entry = _FrameEntry(
                frame_name=frame.short_name,
                frame_id=frame.frame_id,
                frame_length=frame.frame_length,
                bus_desc=frame.desc or '',
                pdu_name=pdu.short_name,
                signals=[],
            )

            for sig in pdu.signals:
                cm = None
                if sig.compu_method and catalog.compu_methods:
                    cm = catalog.compu_methods.get(sig.compu_method)
                # Extract clean signal name (strip PDU/bus suffixes)
                sig_name = _clean_signal_name(sig.short_name)
                entry.signals.append(_SignalDef(
                    name=sig_name,
                    bit_position=sig.bit_position,
                    bit_size=sig.bit_size,
                    byte_order=sig.byte_order,
                    compu=cm,
                    unit=sig.unit or (cm.unit if cm else ''),
                ))

            if frame.bus_type == 'CAN':
                self._can_index.setdefault(frame.frame_id, []).append(entry)
            elif frame.bus_type == 'FLEXRAY':
                self._fr_index.setdefault(frame.frame_id, []).append(entry)
            else:
                self._lin_index.setdefault(frame.frame_id, []).append(entry)
            count += 1

        self._loaded = count > 0
        return count

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def frame_count(self) -> int:
        return sum(len(v) for v in self._can_index.values()) + \
               sum(len(v) for v in self._fr_index.values()) + \
               sum(len(v) for v in self._lin_index.values())

    @property
    def id_count(self) -> int:
        return len(self._can_index)

    @property
    def can_frame_count(self) -> int:
        return sum(len(v) for v in self._can_index.values())

    @property
    def fr_frame_count(self) -> int:
        return sum(len(v) for v in self._fr_index.values())

    @property
    def fr_slot_count(self) -> int:
        return len(self._fr_index)

    @property
    def lin_frame_count(self) -> int:
        return sum(len(v) for v in self._lin_index.values())

    @property
    def lin_id_count(self) -> int:
        return len(self._lin_index)

    def decode(self, frame_id: int, data) -> Optional[Dict[str, Any]]:
        """Decode a raw CAN frame.

        Args:
            frame_id: CAN arbitration ID (11-bit or 29-bit)
            data: raw payload bytes (list, bytes, or bytearray)

        Returns:
            ``{"name": "ESP_21", "signals": {"ESP_v_Signal": 123.45, ...}}``
            or ``None`` if the frame ID is unknown.
        """
        entries = self._can_index.get(frame_id)
        if not entries:
            return None

        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)

        # Try each matching frame (same ID may appear on different buses)
        for entry in entries:
            # Reject frames whose payload is shorter than the expected
            # frame length.  DLC=0 ghost/RTR frames would otherwise
            # decode every signal as 0 because _extract_bits returns 0
            # for out-of-range byte indices.
            if entry.frame_length and len(raw) < entry.frame_length:
                continue
            try:
                signals = self._decode_entry(entry, raw)
                if signals:
                    # Use a cleaner message name (strip bus suffix)
                    msg_name = _clean_frame_name(entry.frame_name)
                    return {
                        'name': msg_name,
                        'signals': signals,
                    }
            except Exception:
                continue

        # ID known but decode failed — return name-only
        return {
            'name': _clean_frame_name(entries[0].frame_name),
            'signals': {},
        }

    def decode_with_bus(self, frame_id: int, data, bus_hint: str = '') -> Optional[Dict[str, Any]]:
        """Decode a CAN frame, preferring entries matching *bus_hint*.

        bus_hint is e.g. 'CCAN', 'HCAN', 'ECAN', etc.
        """
        entries = self._can_index.get(frame_id)
        if not entries:
            return None

        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)

        # Sort entries so the bus_hint match comes first
        if bus_hint:
            bh = bus_hint.upper()
            entries = sorted(entries, key=lambda e: (0 if bh in e.bus_desc.upper() else 1))

        for entry in entries:
            if entry.frame_length and len(raw) < entry.frame_length:
                continue
            try:
                signals = self._decode_entry(entry, raw)
                if signals:
                    return {
                        'name': _clean_frame_name(entry.frame_name),
                        'signals': signals,
                    }
            except Exception:
                continue
        return None

    def known_ids(self) -> set:
        """Return the set of CAN IDs this decoder can handle."""
        return set(self._can_index.keys())

    def known_fr_slots(self) -> set:
        """Return the set of FlexRay slot IDs this decoder can handle."""
        return set(self._fr_index.keys())

    def known_lin_ids(self) -> set:
        """Return the set of LIN frame IDs this decoder can handle."""
        return set(self._lin_index.keys())

    def list_can_signals(self, only_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        """List all CAN message groups with their signals.

        Args:
            only_ids: if provided, only return messages whose CAN ID is in this set.

        Returns:
            ``[{"message": "ESP_21", "frame_id": 0x0FD,
                "signals": [{"key": "ESP_21.ESP_v_Signal", "unit": "km/h"}, ...]}, ...]``
        """
        seen_msgs: Dict[str, Dict[str, str]] = {}  # msg_name -> {sig_name: unit}
        msg_ids: Dict[str, int] = {}
        for fid, entries in self._can_index.items():
            if only_ids is not None and fid not in only_ids:
                continue
            for entry in entries:
                msg_name = _clean_frame_name(entry.frame_name)
                if msg_name not in seen_msgs:
                    seen_msgs[msg_name] = {}
                    msg_ids[msg_name] = fid
                for sig in entry.signals:
                    if sig.name and sig.name not in seen_msgs[msg_name]:
                        seen_msgs[msg_name][sig.name] = sig.unit or ''
        groups = []
        for msg_name in sorted(seen_msgs.keys()):
            sig_map = seen_msgs[msg_name]
            sigs = [{'key': f'{msg_name}.{sn}', 'unit': sig_map.get(sn, '')}
                    for sn in sorted(sig_map.keys())]
            if sigs:
                groups.append({'message': msg_name, 'frame_id': msg_ids.get(msg_name, 0),
                               'signals': sigs})
        return groups

    def list_fr_signals(self, only_slots: Optional[set] = None) -> List[Dict[str, Any]]:
        """List all FlexRay message groups with their signals.

        Args:
            only_slots: if provided, only return messages whose slot ID is in this set.

        Returns:
            ``[{"message": "Motor_10", "slot_id": 7,
                "signals": [{"key": "Motor_10.Motor_Moment", "unit": "Nm"}, ...]}, ...]``
        """
        seen_msgs: Dict[str, Dict[str, str]] = {}
        msg_slots: Dict[str, int] = {}
        for slot_id, entries in self._fr_index.items():
            if only_slots is not None and slot_id not in only_slots:
                continue
            for entry in entries:
                msg_name = _clean_frame_name(entry.frame_name)
                if msg_name not in seen_msgs:
                    seen_msgs[msg_name] = {}
                    msg_slots[msg_name] = slot_id
                for sig in entry.signals:
                    if sig.name and sig.name not in seen_msgs[msg_name]:
                        seen_msgs[msg_name][sig.name] = sig.unit or ''
        groups = []
        for msg_name in sorted(seen_msgs.keys()):
            sig_map = seen_msgs[msg_name]
            sigs = [{'key': f'{msg_name}.{sn}', 'unit': sig_map.get(sn, '')}
                    for sn in sorted(sig_map.keys())]
            if sigs:
                groups.append({'message': msg_name, 'slot_id': msg_slots.get(msg_name, 0),
                               'signals': sigs})
        return groups

    def list_lin_signals(self, only_ids: Optional[set] = None) -> List[Dict[str, Any]]:
        """List all LIN message groups with their signals."""
        seen_msgs: Dict[str, Dict[str, str]] = {}
        msg_ids: Dict[str, int] = {}
        for frame_id, entries in self._lin_index.items():
            if only_ids is not None and frame_id not in only_ids:
                continue
            for entry in entries:
                msg_name = _clean_frame_name(entry.frame_name)
                if msg_name not in seen_msgs:
                    seen_msgs[msg_name] = {}
                    msg_ids[msg_name] = frame_id
                for sig in entry.signals:
                    if sig.name and sig.name not in seen_msgs[msg_name]:
                        seen_msgs[msg_name][sig.name] = sig.unit or ''
        groups = []
        for msg_name in sorted(seen_msgs.keys()):
            sig_map = seen_msgs[msg_name]
            sigs = [{'key': f'{msg_name}.{sn}', 'unit': sig_map.get(sn, '')}
                    for sn in sorted(sig_map.keys())]
            if sigs:
                groups.append({'message': msg_name, 'frame_id': msg_ids.get(msg_name, 0),
                               'signals': sigs})
        return groups

    def decode_flexray(self, slot_id: int, data) -> Optional[Dict[str, Any]]:
        """Decode a raw FlexRay frame by slot ID.

        Args:
            slot_id: FlexRay slot ID (1..N)
            data: raw payload bytes

        Returns:
            ``{"name": "Motor_10", "signals": {"Motor_Moment": 12.5, ...}}``
            or ``None`` if the slot ID is unknown.
        """
        entries = self._fr_index.get(slot_id)
        if not entries:
            return None

        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)

        for entry in entries:
            if entry.frame_length and len(raw) < entry.frame_length:
                continue
            try:
                signals = self._decode_entry(entry, raw)
                if signals:
                    msg_name = _clean_frame_name(entry.frame_name)
                    return {
                        'name': msg_name,
                        'signals': signals,
                    }
            except Exception:
                continue

        # Slot known but decode failed
        return {
            'name': _clean_frame_name(entries[0].frame_name),
            'signals': {},
        }

    def decode_lin(self, frame_id: int, data) -> Optional[Dict[str, Any]]:
        """Decode a raw LIN frame by frame ID using ARXML catalogue data."""
        entries = self._lin_index.get(frame_id)
        if not entries:
            return None

        raw = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)

        for entry in entries:
            if entry.frame_length and len(raw) < entry.frame_length:
                continue
            try:
                signals = self._decode_entry(entry, raw)
                if signals:
                    return {
                        'name': _clean_frame_name(entry.frame_name),
                        'signals': signals,
                    }
            except Exception:
                continue

        return {
            'name': _clean_frame_name(entries[0].frame_name),
            'signals': {},
        }

    # ── internal ────────────────────────────────────────────────────────

    def _decode_entry(self, entry: '_FrameEntry', raw: bytes) -> Dict[str, Any]:
        """Decode all signals for a single frame entry."""
        signals = {}
        for sig_def in entry.signals:
            if sig_def.bit_size <= 0:
                continue
            try:
                raw_val = _extract_bits(raw, sig_def.bit_position,
                                        sig_def.bit_size, sig_def.byte_order)
            except Exception:
                continue

            # Apply COMPU-METHOD conversion
            if sig_def.compu is not None:
                phys = sig_def.compu.convert(raw_val)
            else:
                phys = raw_val

            signals[sig_def.name] = phys

        return signals


# ── Internal data structures ────────────────────────────────────────────────

class _SignalDef:
    __slots__ = ('name', 'bit_position', 'bit_size', 'byte_order', 'compu', 'unit')

    def __init__(self, *, name, bit_position, bit_size, byte_order, compu, unit):
        self.name = name
        self.bit_position = bit_position
        self.bit_size = bit_size
        self.byte_order = byte_order
        self.compu = compu
        self.unit = unit


class _FrameEntry:
    __slots__ = ('frame_name', 'frame_id', 'frame_length', 'bus_desc',
                 'pdu_name', 'signals')

    def __init__(self, *, frame_name, frame_id, frame_length, bus_desc,
                 pdu_name, signals):
        self.frame_name = frame_name
        self.frame_id = frame_id
        self.frame_length = frame_length
        self.bus_desc = bus_desc
        self.pdu_name = pdu_name
        self.signals = signals


# ── Bit-level extraction ───────────────────────────────────────────────────

def _extract_bits(data: bytes, start_bit: int, length: int,
                  byte_order: str) -> int:
    """Extract an unsigned integer from CAN payload.

    AUTOSAR / DBC bit numbering:
      - Little-endian (Intel): start_bit is the LSB position in the
        standard DBC bit numbering (byte0-bit0 = 0, byte0-bit7 = 7,
        byte1-bit0 = 8 …).
      - Big-endian (Motorola): start_bit is the MSB position in the
        DBC bit numbering, bits are laid out MSB-first across bytes.

    This matches the cantools / Vector convention used in VAG KMatrix DBC.
    """
    if byte_order == 'big-endian':
        return _extract_bits_big_endian(data, start_bit, length)
    else:
        return _extract_bits_little_endian(data, start_bit, length)


def _extract_bits_little_endian(data: bytes, start_bit: int, length: int) -> int:
    """Intel byte order: start_bit = LSB position."""
    value = 0
    for i in range(length):
        bit_pos = start_bit + i
        byte_idx = bit_pos // 8
        bit_in_byte = bit_pos % 8
        if byte_idx < len(data):
            if data[byte_idx] & (1 << bit_in_byte):
                value |= (1 << i)
    return value


def _extract_bits_big_endian(data: bytes, start_bit: int, length: int) -> int:
    """Motorola byte order: start_bit = MSB position (DBC convention).

    In DBC/AUTOSAR Motorola numbering, bits within a byte are numbered
    7..0 (MSB first), and bytes are numbered 0,1,2… The start_bit is
    the MSB of the signal.
    """
    value = 0
    bit_pos = start_bit
    for i in range(length):
        byte_idx = bit_pos // 8
        bit_in_byte = bit_pos % 8
        if byte_idx < len(data):
            if data[byte_idx] & (1 << bit_in_byte):
                value |= (1 << (length - 1 - i))
        # Move to next bit in Motorola order
        if bit_in_byte == 0:
            # Wrap to next byte, bit 7
            bit_pos += 15
        else:
            bit_pos -= 1
    return value


# ── Name cleanup helpers ───────────────────────────────────────────────────

def _clean_frame_name(name: str) -> str:
    """Strip bus-specific suffixes from ARXML frame names.

    e.g. "ESP_21_XIX_MLBevo_CCAN" → "ESP_21"
         "Motor_12_XIX_MLBevo_ECAN" → "Motor_12"
    """
    # Common pattern: <msg>_XIX_MLBevo_<BUS>
    for marker in ('_XIX_MLBevo_', '_XIX_MLBevo', '_XIX_'):
        idx = name.find(marker)
        if idx > 0:
            return name[:idx]
    return name


def _clean_signal_name(name: str) -> str:
    """Strip PDU/bus suffixes from signal mapping names.

    e.g. "ESP_v_Signal_XIX_ESP_21_XIX_MLBevo_CCAN" → "ESP_v_Signal"
    """
    for marker in ('_XIX_',):
        idx = name.find(marker)
        if idx > 0:
            return name[:idx]
    return name
