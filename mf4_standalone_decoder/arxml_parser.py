"""AUTOSAR ARXML parser for Ethernet / Mirror PDU definitions.

Parses .arxml files to extract:
  - I-PDU definitions (names, lengths, signal mappings)
  - SOME/IP service definitions
  - Mirror data format definitions (Bus-Mirror-Channel, etc.)
  - CAN / FlexRay / LIN frame-to-PDU mappings
  - Ethernet cluster / VLAN / Socket connection info

The parsed catalogue is stored in memory and can be queried by the
mirror decoder or any other module that needs AUTOSAR-level metadata.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ── AUTOSAR XML namespace handling ──────────────────────────────────────────
_NS_PATTERN = re.compile(r'\{(.*?)\}')

def _detect_ns(root: ET.Element) -> str:
    """Return the AUTOSAR namespace URI from the root element tag."""
    m = _NS_PATTERN.match(root.tag)
    return m.group(1) if m else ''


def _ns(tag: str, ns: str) -> str:
    """Prepend namespace to an ARXML tag name."""
    return f'{{{ns}}}{tag}' if ns else tag


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class CompuScale:
    """A single scale range within a COMPU-METHOD."""
    lower: float = 0.0
    upper: float = 0.0
    factor: float = 1.0            # numerator[1] / denominator[0]
    offset: float = 0.0            # numerator[0] / denominator[0]
    text_value: str = ''           # COMPU-CONST/VT for TEXTTABLE entries
    desc: str = ''
    is_texttable: bool = False     # True when this scale is a named constant


@dataclass
class CompuMethod:
    """COMPU-METHOD definition (SCALE_LINEAR, TEXTTABLE, etc.)."""
    short_name: str = ''
    category: str = ''             # SCALE_LINEAR | TEXTTABLE | SCALE_LINEAR_AND_TEXTTABLE | IDENTICAL | ...
    unit: str = ''                 # resolved unit name
    scales: List['CompuScale'] = field(default_factory=list)

    # ── helpers ──
    def convert(self, raw_value: int) -> object:
        """Convert internal (raw) integer to physical value."""
        for sc in self.scales:
            if sc.lower <= raw_value <= sc.upper:
                if sc.is_texttable:
                    return sc.text_value
                return sc.offset + sc.factor * raw_value
        # No matching scale – return raw unchanged
        if self.category in ('IDENTICAL', 'FIXED_LENGTH', ''):
            return raw_value
        # Fallback: apply the first linear scale if any
        for sc in self.scales:
            if not sc.is_texttable:
                return sc.offset + sc.factor * raw_value
        return raw_value


@dataclass
class ArxmlSignal:
    """An I-PDU signal or I-SIGNAL extracted from ARXML."""
    short_name: str = ''
    bit_position: int = 0
    bit_size: int = 0
    byte_order: str = 'little-endian'   # 'big-endian' | 'little-endian'
    init_value: Optional[float] = None
    desc: str = ''
    compu_method: str = ''         # reference name/path to COMPU-METHOD
    system_signal_ref: str = ''    # SYSTEM-SIGNAL-REF resolved from I-SIGNAL
    unit: str = ''                 # resolved physical unit string


@dataclass
class ArxmlPdu:
    """An I-PDU definition."""
    short_name: str = ''
    length: int = 0            # PDU length in bytes
    pdu_type: str = ''         # GENERAL | SECURED | XCP | ...
    signals: List[ArxmlSignal] = field(default_factory=list)
    desc: str = ''
    path: str = ''             # full AUTOSAR path (/pkg/.../name)


@dataclass
class ArxmlFrame:
    """A CAN / FlexRay / Ethernet frame definition."""
    short_name: str = ''
    frame_id: int = 0          # CAN arb-id, FR slot-id, etc.
    frame_length: int = 0      # bytes
    pdu_name: str = ''         # mapped I-PDU short-name
    bus_type: str = ''         # CAN | FLEXRAY | LIN | ETHERNET
    desc: str = ''
    path: str = ''


@dataclass
class ArxmlMirrorChannel:
    """Bus Mirror Channel definition (AUTOSAR Bus Mirroring)."""
    short_name: str = ''
    source_bus: str = ''       # source network name
    source_bus_type: str = ''  # CAN | FLEXRAY | LIN
    dest_pdu: str = ''         # destination PDU short-name
    timestamp_support: bool = False
    network_id: int = 0
    desc: str = ''


@dataclass
class ArxmlSomeIpMethod:
    """SOME/IP service method / event definition."""
    short_name: str = ''
    method_id: int = 0
    service_id: int = 0
    message_type: str = ''     # REQUEST | RESPONSE | NOTIFICATION
    desc: str = ''


@dataclass
class ArxmlSocketConnection:
    """Ethernet socket connection info."""
    short_name: str = ''
    local_port: int = 0
    remote_port: int = 0
    protocol: str = ''         # TCP | UDP
    multicast_address: str = ''
    pdu_name: str = ''
    desc: str = ''


@dataclass
class ArxmlCatalog:
    """Complete parsed result from one or more ARXML files."""
    pdus: Dict[str, ArxmlPdu] = field(default_factory=dict)
    frames: Dict[str, ArxmlFrame] = field(default_factory=dict)
    mirror_channels: List[ArxmlMirrorChannel] = field(default_factory=list)
    someip_methods: List[ArxmlSomeIpMethod] = field(default_factory=list)
    socket_connections: List[ArxmlSocketConnection] = field(default_factory=list)
    compu_methods: Dict[str, CompuMethod] = field(default_factory=dict)
    # system_signal short_name → (compu_method_name, unit_name)
    system_signals: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    source_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # ----- helpers -----
    def pdu_by_name(self, name: str) -> Optional[ArxmlPdu]:
        return self.pdus.get(name)

    def frame_by_id(self, frame_id: int, bus_type: str = '') -> Optional[ArxmlFrame]:
        for f in self.frames.values():
            if f.frame_id == frame_id:
                if bus_type and f.bus_type.upper() != bus_type.upper():
                    continue
                return f
        return None

    def resolve_frame_signals(self, frame_id: int, bus_type: str = 'CAN') -> Tuple[Optional[ArxmlFrame], List[ArxmlSignal]]:
        """Find a frame by ID, then look up its PDU signals.

        Returns (frame, signals) — signals may be empty if the PDU
        is not an I-SIGNAL-I-PDU or has no signal mappings.
        """
        frame = self.frame_by_id(frame_id, bus_type)
        if frame is None:
            return None, []
        if frame.pdu_name:
            pdu = self.pdu_by_name(frame.pdu_name)
            if pdu:
                return frame, pdu.signals
        return frame, []

    def frames_by_bus(self, bus_name: str) -> List[ArxmlFrame]:
        """Return all frames whose desc or short_name contain *bus_name*."""
        bus_name_upper = bus_name.upper()
        result = []
        for f in self.frames.values():
            if bus_name_upper in f.short_name.upper() or bus_name_upper in (f.desc or '').upper():
                result.append(f)
        return result

    def summary(self) -> Dict[str, Any]:
        return {
            'source_files': self.source_files,
            'pdu_count': len(self.pdus),
            'frame_count': len(self.frames),
            'compu_method_count': len(self.compu_methods),
            'system_signal_count': len(self.system_signals),
            'mirror_channel_count': len(self.mirror_channels),
            'someip_method_count': len(self.someip_methods),
            'socket_connection_count': len(self.socket_connections),
            'errors': self.errors[:20],
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the entire catalogue to a plain dict (JSON-friendly)."""
        def _sig(s: ArxmlSignal) -> dict:
            return {
                'short_name': s.short_name,
                'bit_position': s.bit_position,
                'bit_size': s.bit_size,
                'byte_order': s.byte_order,
                'init_value': s.init_value,
                'desc': s.desc,
            }
        def _pdu(p: ArxmlPdu) -> dict:
            return {
                'short_name': p.short_name,
                'length': p.length,
                'pdu_type': p.pdu_type,
                'desc': p.desc,
                'path': p.path,
                'signals': [_sig(s) for s in p.signals],
            }
        def _frame(f: ArxmlFrame) -> dict:
            return {
                'short_name': f.short_name,
                'frame_id': f.frame_id,
                'frame_length': f.frame_length,
                'pdu_name': f.pdu_name,
                'bus_type': f.bus_type,
                'desc': f.desc,
            }
        def _mc(m: ArxmlMirrorChannel) -> dict:
            return {
                'short_name': m.short_name,
                'source_bus': m.source_bus,
                'source_bus_type': m.source_bus_type,
                'dest_pdu': m.dest_pdu,
                'timestamp_support': m.timestamp_support,
                'network_id': m.network_id,
                'desc': m.desc,
            }
        def _sc(sc: ArxmlSocketConnection) -> dict:
            return {
                'short_name': sc.short_name,
                'local_port': sc.local_port,
                'remote_port': sc.remote_port,
                'protocol': sc.protocol,
                'multicast_address': sc.multicast_address,
                'pdu_name': sc.pdu_name,
                'desc': sc.desc,
            }
        return {
            'source_files': self.source_files,
            'pdus': {k: _pdu(v) for k, v in self.pdus.items()},
            'frames': {k: _frame(v) for k, v in self.frames.items()},
            'mirror_channels': [_mc(m) for m in self.mirror_channels],
            'someip_methods': [{'short_name': m.short_name, 'method_id': m.method_id,
                                'service_id': m.service_id, 'message_type': m.message_type,
                                'desc': m.desc} for m in self.someip_methods],
            'socket_connections': [_sc(sc) for sc in self.socket_connections],
            'errors': self.errors[:50],
        }


# ── Parsing ─────────────────────────────────────────────────────────────────

def _text(el: Optional[ET.Element], tag: str, ns: str, default: str = '') -> str:
    """Safely extract text from a child element."""
    if el is None:
        return default
    child = el.find(_ns(tag, ns))
    if child is not None and child.text:
        return child.text.strip()
    return default


def _int_text(el: Optional[ET.Element], tag: str, ns: str, default: int = 0) -> int:
    t = _text(el, tag, ns)
    if not t:
        return default
    try:
        if t.startswith('0x') or t.startswith('0X'):
            return int(t, 16)
        return int(float(t))
    except Exception:
        return default


def _build_path(ancestors: List[str], name: str) -> str:
    return '/'.join(ancestors + [name])


def _parse_signals(pdu_el: ET.Element, ns: str,
                    isignal_lengths: Optional[Dict[str, int]] = None,
                    isignal_sysref: Optional[Dict[str, str]] = None) -> List[ArxmlSignal]:
    """Parse I-SIGNAL-TO-I-PDU-MAPPING children inside an I-SIGNAL-I-PDU.

    AUTOSAR R4.0 structure:
        I-SIGNAL-I-PDU
          └─ I-SIGNAL-TO-PDU-MAPPINGS          (container)
               └─ I-SIGNAL-TO-I-PDU-MAPPING    (one per signal)
                    ├─ SHORT-NAME
                    ├─ I-SIGNAL-REF  → /ISignal/<name>
                    ├─ PACKING-BYTE-ORDER  (MOST-SIGNIFICANT-BYTE-LAST = LE)
                    ├─ START-POSITION      (bit offset)
                    └─ TRANSFER-PROPERTY

    The bit-size (LENGTH) lives on the referenced I-SIGNAL element, so we
    look it up from *isignal_lengths* (pre-built dict: AUTOSAR-path → bits).
    *isignal_sysref* maps I-SIGNAL path → SYSTEM-SIGNAL short-name.
    """
    if isignal_lengths is None:
        isignal_lengths = {}
    if isignal_sysref is None:
        isignal_sysref = {}

    signals: List[ArxmlSignal] = []

    # ── Primary path: I-SIGNAL-TO-PDU-MAPPINGS container ────────────────
    for container_tag in ('I-SIGNAL-TO-PDU-MAPPINGS', 'I-SIGNAL-TO-I-PDU-MAPPINGS',
                          'SIGNAL-TO-PDU-MAPPINGS'):
        container = pdu_el.find(_ns(container_tag, ns))
        if container is None:
            continue
        for mapping in container:
            tag_local = mapping.tag.replace(f'{{{ns}}}', '') if ns else mapping.tag
            if 'SIGNAL' not in tag_local.upper():
                continue  # skip non-mapping children
            sig = ArxmlSignal()
            sig.short_name = _text(mapping, 'SHORT-NAME', ns)
            sig.bit_position = _int_text(mapping, 'START-POSITION', ns)

            # Byte order
            bo = _text(mapping, 'PACKING-BYTE-ORDER', ns)
            if 'FIRST' in bo.upper() or 'BIG' in bo.upper():
                sig.byte_order = 'big-endian'
            else:
                sig.byte_order = 'little-endian'     # MOST-SIGNIFICANT-BYTE-LAST

            # Resolve bit-size from I-SIGNAL-REF
            isig_ref_el = mapping.find(_ns('I-SIGNAL-REF', ns))
            ref_path = ''
            if isig_ref_el is not None and isig_ref_el.text:
                ref_path = isig_ref_el.text.strip()
                sig.bit_size = isignal_lengths.get(ref_path, 0)
                # Resolve SYSTEM-SIGNAL name via I-SIGNAL → SYSTEM-SIGNAL-REF
                sysref = isignal_sysref.get(ref_path) or isignal_sysref.get(ref_path.split('/')[-1])
                if sysref:
                    sig.system_signal_ref = sysref

            # Fallback: LENGTH child directly in mapping (some older formats)
            if sig.bit_size == 0:
                sig.bit_size = _int_text(mapping, 'LENGTH', ns)

            signals.append(sig)

    # ── Fallback: iterate all nested I-SIGNAL-TO-I-PDU-MAPPING directly ──
    if not signals:
        for mapping in pdu_el.iter(_ns('I-SIGNAL-TO-I-PDU-MAPPING', ns)):
            name = _text(mapping, 'SHORT-NAME', ns)
            if name and not any(s.short_name == name for s in signals):
                sig = ArxmlSignal(short_name=name)
                sig.bit_position = _int_text(mapping, 'START-POSITION', ns)
                bo = _text(mapping, 'PACKING-BYTE-ORDER', ns)
                if 'FIRST' in bo.upper() or 'BIG' in bo.upper():
                    sig.byte_order = 'big-endian'
                isig_ref_el = mapping.find(_ns('I-SIGNAL-REF', ns))
                if isig_ref_el is not None and isig_ref_el.text:
                    rp = isig_ref_el.text.strip()
                    sig.bit_size = isignal_lengths.get(rp, 0)
                    sr = isignal_sysref.get(rp) or isignal_sysref.get(rp.split('/')[-1])
                    if sr:
                        sig.system_signal_ref = sr
                if sig.bit_size == 0:
                    sig.bit_size = _int_text(mapping, 'LENGTH', ns)
                signals.append(sig)

    return signals


def _float_text(el: Optional[ET.Element], tag: str, ns: str, default: float = 0.0) -> float:
    t = _text(el, tag, ns)
    if not t:
        return default
    try:
        return float(t)
    except Exception:
        return default


def _parse_compu_method_element(cm_el: ET.Element, ns: str) -> 'CompuMethod':
    """Parse a single COMPU-METHOD XML element into a CompuMethod dataclass."""
    cm = CompuMethod()
    cm.short_name = _text(cm_el, 'SHORT-NAME', ns)
    cm.category = _text(cm_el, 'CATEGORY', ns)

    # Unit (may live directly on COMPU-METHOD)
    unit_ref_el = cm_el.find(_ns('UNIT-REF', ns))
    if unit_ref_el is not None and unit_ref_el.text:
        u = unit_ref_el.text.strip().split('/')[-1]
        if u.startswith('Unit_Unit_'):
            u = u[len('Unit_Unit_'):]
        elif u.startswith('Unit_'):
            u = u[len('Unit_'):]
        cm.unit = u

    # Parse COMPU-SCALES
    for scale_el in cm_el.iter(_ns('COMPU-SCALE', ns)):
        sc = CompuScale()
        # Limits
        lo_el = scale_el.find(_ns('LOWER-LIMIT', ns))
        if lo_el is not None and lo_el.text:
            try:
                sc.lower = float(lo_el.text.strip())
            except Exception:
                pass
        up_el = scale_el.find(_ns('UPPER-LIMIT', ns))
        if up_el is not None and up_el.text:
            try:
                sc.upper = float(up_el.text.strip())
            except Exception:
                pass

        # COMPU-RATIONAL-COEFFS → linear scale  phys = (num[0] + num[1]*raw) / den[0]
        coeffs_el = scale_el.find(_ns('COMPU-RATIONAL-COEFFS', ns))
        if coeffs_el is not None:
            num_el = coeffs_el.find(_ns('COMPU-NUMERATOR', ns))
            den_el = coeffs_el.find(_ns('COMPU-DENOMINATOR', ns))
            nums = []
            dens = []
            if num_el is not None:
                for v_el in num_el.iter(_ns('V', ns)):
                    try:
                        nums.append(float(v_el.text.strip()))
                    except Exception:
                        nums.append(0.0)
            if den_el is not None:
                for v_el in den_el.iter(_ns('V', ns)):
                    try:
                        dens.append(float(v_el.text.strip()))
                    except Exception:
                        dens.append(1.0)
            den_val = dens[0] if dens and dens[0] != 0 else 1.0
            sc.offset = (nums[0] / den_val) if len(nums) > 0 else 0.0
            sc.factor = (nums[1] / den_val) if len(nums) > 1 else 1.0
            sc.is_texttable = False
        else:
            # COMPU-CONST → text table entry
            const_el = scale_el.find(_ns('COMPU-CONST', ns))
            if const_el is not None:
                vt = _text(const_el, 'VT', ns)
                if vt:
                    sc.text_value = vt
                    sc.is_texttable = True
                    sc.factor = 0.0
                    sc.offset = 0.0

        # Description
        desc_el = scale_el.find(_ns('DESC', ns))
        if desc_el is not None:
            for l2 in desc_el.iter(_ns('L-2', ns)):
                if l2.text:
                    sc.desc = l2.text.strip()
                    break

        cm.scales.append(sc)

    return cm


def parse_arxml(filepath: str) -> ArxmlCatalog:
    """Parse a single ARXML file and return an ArxmlCatalog.

    Uses a two-pass approach for large files:
      Pass 1 – build I-SIGNAL path → LENGTH lookup (needed for signal bit-sizes)
      Pass 2 – full tree parse for PDUs, frames, mirror, SOME/IP, sockets
    """
    cat = ArxmlCatalog()
    cat.source_files.append(os.path.basename(filepath))

    # ── Pass 1: I-SIGNAL length + SYSTEM-SIGNAL-REF + COMPU-METHOD + SYSTEM-SIGNAL via iterparse ──
    isignal_lengths: Dict[str, int] = {}
    isignal_sysref: Dict[str, str] = {}   # I-SIGNAL path → SYSTEM-SIGNAL short_name
    compu_methods: Dict[str, CompuMethod] = {}
    # system_signal short_name → (compu_method_ref_name, unit_ref_name)
    system_signal_map: Dict[str, Tuple[str, str]] = {}
    ns = ''
    _pass1_keep = {'SHORT-NAME', 'LENGTH', 'I-SIGNAL-LENGTH',
                   'DATA-TYPE-POLICY', 'INIT-VALUE',
                   'NUMERICAL-VALUE-SPECIFICATION', 'SHORT-LABEL',
                   'VALUE', 'NETWORK-REPRESENTATION-PROPS',
                   'SW-DATA-DEF-PROPS-VARIANTS',
                   'SW-DATA-DEF-PROPS-CONDITIONAL',
                   'BASE-TYPE-REF', 'INVALID-VALUE',
                   'SYSTEM-SIGNAL-REF',
                   # COMPU-METHOD sub-elements
                   'CATEGORY', 'UNIT-REF',
                   'COMPU-INTERNAL-TO-PHYS', 'COMPU-SCALES', 'COMPU-SCALE',
                   'LOWER-LIMIT', 'UPPER-LIMIT',
                   'COMPU-RATIONAL-COEFFS', 'COMPU-NUMERATOR', 'COMPU-DENOMINATOR',
                   'V', 'COMPU-CONST', 'VT', 'DESC', 'L-2',
                   # SYSTEM-SIGNAL sub-elements
                   'PHYSICAL-PROPS', 'COMPU-METHOD-REF', 'DYNAMIC-LENGTH',
                   'DATA-CONSTR-REF',
                   }
    try:
        for _ev, elem in ET.iterparse(filepath, events=['end']):
            if not ns:
                m = _NS_PATTERN.match(elem.tag)
                if m:
                    ns = m.group(1)
            raw_tag = elem.tag.replace(f'{{{ns}}}', '') if ns else elem.tag

            if raw_tag == 'I-SIGNAL':
                sname = _text(elem, 'SHORT-NAME', ns)
                length = _int_text(elem, 'LENGTH', ns) or \
                         _int_text(elem, 'I-SIGNAL-LENGTH', ns)
                if sname and length:
                    isignal_lengths[f'/ISignal/{sname}'] = length
                    isignal_lengths[sname] = length
                # Resolve SYSTEM-SIGNAL-REF
                if sname:
                    ss_ref_el = elem.find(_ns('SYSTEM-SIGNAL-REF', ns))
                    if ss_ref_el is not None and ss_ref_el.text:
                        ss_name = ss_ref_el.text.strip().split('/')[-1]
                        isignal_sysref[f'/ISignal/{sname}'] = ss_name
                        isignal_sysref[sname] = ss_name
                elem.clear()

            elif raw_tag == 'COMPU-METHOD':
                cm = _parse_compu_method_element(elem, ns)
                if cm.short_name:
                    compu_methods[cm.short_name] = cm
                elem.clear()

            elif raw_tag == 'SYSTEM-SIGNAL':
                ss_name = _text(elem, 'SHORT-NAME', ns)
                if ss_name:
                    cm_ref = ''
                    unit_ref = ''
                    # Look for COMPU-METHOD-REF inside PHYSICAL-PROPS
                    for cm_ref_el in elem.iter(_ns('COMPU-METHOD-REF', ns)):
                        if cm_ref_el.text:
                            cm_ref = cm_ref_el.text.strip().split('/')[-1]
                            break
                    for unit_ref_el in elem.iter(_ns('UNIT-REF', ns)):
                        if unit_ref_el.text:
                            unit_ref = unit_ref_el.text.strip().split('/')[-1]
                            # Simplify common patterns like "Unit_Unit_KiloMeterPerHour"
                            if unit_ref.startswith('Unit_Unit_'):
                                unit_ref = unit_ref[len('Unit_Unit_'):]
                            elif unit_ref.startswith('Unit_'):
                                unit_ref = unit_ref[len('Unit_'):]
                            break
                    if cm_ref or unit_ref:
                        system_signal_map[ss_name] = (cm_ref, unit_ref)
                elem.clear()

            elif raw_tag not in _pass1_keep:
                elem.clear()
    except ET.ParseError as e:
        cat.errors.append(f'XML parse error (pass 1): {e}')
        return cat
    except Exception as e:
        cat.errors.append(f'File read error (pass 1): {e}')
        return cat

    cat.compu_methods.update(compu_methods)
    cat.system_signals.update(system_signal_map)

    # ── Pass 2: full parse ──────────────────────────────────────────────
    try:
        tree = ET.parse(filepath)
    except ET.ParseError as e:
        cat.errors.append(f'XML parse error: {e}')
        return cat
    except Exception as e:
        cat.errors.append(f'File read error: {e}')
        return cat

    root = tree.getroot()
    if not ns:
        ns = _detect_ns(root)

    # ── I-PDUs ──────────────────────────────────────────────────────────
    for pdu_el in root.iter(_ns('I-SIGNAL-I-PDU', ns)):
        pdu = ArxmlPdu()
        pdu.short_name = _text(pdu_el, 'SHORT-NAME', ns)
        pdu.length = _int_text(pdu_el, 'LENGTH', ns)
        pdu.desc = _text(pdu_el, 'DESC', ns) or _text(pdu_el, 'LONG-NAME', ns)
        pdu.signals = _parse_signals(pdu_el, ns, isignal_lengths, isignal_sysref)
        # Resolve compu_method + unit on each signal via system_signal_map
        for sig in pdu.signals:
            if sig.system_signal_ref and sig.system_signal_ref in system_signal_map:
                cm_name, unit_name = system_signal_map[sig.system_signal_ref]
                sig.compu_method = cm_name
                sig.unit = unit_name
        if pdu.short_name:
            cat.pdus[pdu.short_name] = pdu

    # Also parse GENERAL-PURPOSE-I-PDU, SECURED-I-PDU, etc.
    for tag in ('GENERAL-PURPOSE-I-PDU', 'SECURED-I-PDU',
                'MULTIPLEXED-I-PDU', 'USER-DEFINED-I-PDU',
                'DCM-I-PDU', 'NM-I-PDU', 'XCP-PDU'):
        for pdu_el in root.iter(_ns(tag, ns)):
            pdu = ArxmlPdu()
            pdu.short_name = _text(pdu_el, 'SHORT-NAME', ns)
            pdu.length = _int_text(pdu_el, 'LENGTH', ns)
            pdu.pdu_type = tag.replace('-', '_')
            pdu.desc = _text(pdu_el, 'DESC', ns) or _text(pdu_el, 'LONG-NAME', ns)
            if pdu.short_name and pdu.short_name not in cat.pdus:
                cat.pdus[pdu.short_name] = pdu

    # ── Frames (CAN / FlexRay / LIN) ───────────────────────────────────
    _frame_tags = {
        'CAN-FRAME': 'CAN',
        'FLEXRAY-FRAME': 'FLEXRAY',
        'LIN-FRAME': 'LIN',
        'ETHERNET-FRAME': 'ETHERNET',
    }
    for ftag, bus_type in _frame_tags.items():
        for f_el in root.iter(_ns(ftag, ns)):
            fr = ArxmlFrame()
            fr.short_name = _text(f_el, 'SHORT-NAME', ns)
            fr.frame_length = _int_text(f_el, 'FRAME-LENGTH', ns)
            fr.bus_type = bus_type
            fr.desc = _text(f_el, 'DESC', ns) or _text(f_el, 'LONG-NAME', ns)

            # CAN frame ID
            fr.frame_id = _int_text(f_el, 'IDENTIFIER', ns) or \
                          _int_text(f_el, 'CAN-ID', ns) or \
                          _int_text(f_el, 'SLOT-ID', ns)

            # PDU mapping
            for pm in f_el.iter(_ns('PDU-TO-FRAME-MAPPING', ns)):
                pdu_ref = pm.find(_ns('PDU-REF', ns))
                if pdu_ref is not None and pdu_ref.text:
                    fr.pdu_name = pdu_ref.text.strip().split('/')[-1]
                    break
            for pm in f_el.iter(_ns('I-PDU-PORT-REF', ns)):
                if pm.text:
                    fr.pdu_name = pm.text.strip().split('/')[-1]
                    break

            if fr.short_name:
                cat.frames[fr.short_name] = fr

    # Also look for FRAME-TRIGGERINGs to get CAN IDs and bus assignments
    for ft in root.iter(_ns('CAN-FRAME-TRIGGERING', ns)):
        name = _text(ft, 'SHORT-NAME', ns)
        fid = _int_text(ft, 'IDENTIFIER', ns)
        frame_ref_el = ft.find(_ns('FRAME-REF', ns))
        frame_ref_name = ''
        frame_ref_path = ''
        if frame_ref_el is not None and frame_ref_el.text:
            frame_ref_path = frame_ref_el.text.strip()
            frame_ref_name = frame_ref_path.split('/')[-1]

        # Determine CAN bus name from parent cluster context or frame name
        # e.g. FT name "FT_AGA_01" in cluster /Cluster/MLBevo_ECAN
        # or frame ref "/Frame/AGA_01_XIX_MLBevo_ECAN"
        bus_name = ''
        for suffix in ('_KCAN', '_ICAN', '_HCAN', '_ECAN', '_TCAN',
                        '_CCAN', '_SCCAN', '_SUBCAN', '_DiagCAN',
                        '_LIN', '_FlexRay'):
            if frame_ref_name.endswith(suffix) or frame_ref_name.endswith(suffix.replace('_', '')):
                bus_name = suffix.lstrip('_')
                break
            # Check in the middle: e.g. "_MLBevo_ECAN"
            if f'_MLBevo{suffix}' in frame_ref_name or f'_MLBevo_{suffix.lstrip("_")}' in frame_ref_name:
                bus_name = suffix.lstrip('_')
                break

        if frame_ref_name and frame_ref_name in cat.frames:
            if fid:
                cat.frames[frame_ref_name].frame_id = fid
            if bus_name and not cat.frames[frame_ref_name].desc:
                cat.frames[frame_ref_name].desc = bus_name
        elif frame_ref_name and fid:
            # Frame not found in CAN-FRAME pass — create from triggering
            fr = ArxmlFrame()
            fr.short_name = frame_ref_name
            fr.frame_id = fid
            fr.bus_type = 'CAN'
            fr.desc = bus_name
            cat.frames[frame_ref_name] = fr

    # Also handle FLEXRAY-FRAME-TRIGGERING
    # Structure: FLEXRAY-FRAME-TRIGGERING > ABSOLUTELY-SCHEDULED-TIMINGS >
    #   FLEXRAY-ABSOLUTELY-SCHEDULED-TIMING > SLOT-ID / COMMUNICATION-CYCLE
    for ft in root.iter(_ns('FLEXRAY-FRAME-TRIGGERING', ns)):
        name = _text(ft, 'SHORT-NAME', ns)
        # SLOT-ID is nested deeply — use iter() to search all descendants
        slot_id = 0
        for sid_el in ft.iter(_ns('SLOT-ID', ns)):
            if sid_el.text:
                try:
                    slot_id = int(sid_el.text.strip())
                except Exception:
                    pass
            break
        frame_ref_el = ft.find(_ns('FRAME-REF', ns))
        frame_ref_name = ''
        if frame_ref_el is not None and frame_ref_el.text:
            frame_ref_name = frame_ref_el.text.strip().split('/')[-1]
        if frame_ref_name and frame_ref_name in cat.frames:
            if slot_id:
                cat.frames[frame_ref_name].frame_id = slot_id

    # ── Bus Mirror definitions ──────────────────────────────────────────
    for mc_el in root.iter(_ns('BUS-MIRROR-CHANNEL-MAPPING-CAN', ns)):
        mc = ArxmlMirrorChannel()
        mc.short_name = _text(mc_el, 'SHORT-NAME', ns)
        mc.source_bus_type = 'CAN'
        mc.desc = _text(mc_el, 'DESC', ns)
        mc.network_id = _int_text(mc_el, 'NETWORK-ID', ns)

        net_ref = mc_el.find(_ns('SOURCE-NETWORK-REF', ns))
        if net_ref is not None and net_ref.text:
            mc.source_bus = net_ref.text.strip().split('/')[-1]

        pdu_ref = mc_el.find(_ns('DEST-I-PDU-REF', ns))
        if pdu_ref is not None and pdu_ref.text:
            mc.dest_pdu = pdu_ref.text.strip().split('/')[-1]

        ts_el = mc_el.find(_ns('TIMESTAMP-SUPPORT', ns))
        if ts_el is not None and (ts_el.text or '').strip().lower() in ('true', '1'):
            mc.timestamp_support = True

        cat.mirror_channels.append(mc)

    # Generic BUS-MIRROR-CHANNEL-MAPPING (FlexRay, LIN, etc.)
    for tag in ('BUS-MIRROR-CHANNEL-MAPPING-FLEXRAY', 'BUS-MIRROR-CHANNEL-MAPPING-LIN',
                'BUS-MIRROR-CHANNEL-MAPPING-IP', 'BUS-MIRROR-CHANNEL-MAPPING'):
        for mc_el in root.iter(_ns(tag, ns)):
            mc = ArxmlMirrorChannel()
            mc.short_name = _text(mc_el, 'SHORT-NAME', ns)
            mc.source_bus_type = tag.replace('BUS-MIRROR-CHANNEL-MAPPING-', '').replace('BUS-MIRROR-CHANNEL-MAPPING', 'GENERIC')
            mc.desc = _text(mc_el, 'DESC', ns)
            mc.network_id = _int_text(mc_el, 'NETWORK-ID', ns)

            net_ref = mc_el.find(_ns('SOURCE-NETWORK-REF', ns))
            if net_ref is not None and net_ref.text:
                mc.source_bus = net_ref.text.strip().split('/')[-1]

            pdu_ref = mc_el.find(_ns('DEST-I-PDU-REF', ns))
            if pdu_ref is not None and pdu_ref.text:
                mc.dest_pdu = pdu_ref.text.strip().split('/')[-1]

            cat.mirror_channels.append(mc)

    # ── SOME/IP service definitions ─────────────────────────────────────
    for svc in root.iter(_ns('SOMEIP-SERVICE-INTERFACE-DEPLOYMENT', ns)):
        svc_id = _int_text(svc, 'SERVICE-INTERFACE-ID', ns)
        for ev in svc.iter(_ns('SOMEIP-EVENT-DEPLOYMENT', ns)):
            m = ArxmlSomeIpMethod()
            m.short_name = _text(ev, 'SHORT-NAME', ns)
            m.method_id = _int_text(ev, 'EVENT-ID', ns) or _int_text(ev, 'METHOD-ID', ns)
            m.service_id = svc_id
            m.message_type = 'NOTIFICATION'
            cat.someip_methods.append(m)

        for meth in svc.iter(_ns('SOMEIP-METHOD-DEPLOYMENT', ns)):
            m = ArxmlSomeIpMethod()
            m.short_name = _text(meth, 'SHORT-NAME', ns)
            m.method_id = _int_text(meth, 'METHOD-ID', ns)
            m.service_id = svc_id
            m.message_type = 'REQUEST'
            cat.someip_methods.append(m)

    # ── Socket connections ──────────────────────────────────────────────
    for sc_el in root.iter(_ns('SOCKET-CONNECTION-BUNDLE', ns)):
        sc = ArxmlSocketConnection()
        sc.short_name = _text(sc_el, 'SHORT-NAME', ns)

        for sap in sc_el.iter(_ns('SOCKET-ADDRESS', ns)):
            port = _int_text(sap, 'PORT-NUMBER', ns)
            proto = _text(sap, 'TRANSPORT-PROTOCOL', ns)
            if port:
                if not sc.local_port:
                    sc.local_port = port
                else:
                    sc.remote_port = port
            if proto:
                sc.protocol = proto.upper()

        for pd in sc_el.iter(_ns('I-PDU-PORT-REF', ns)):
            if pd.text:
                sc.pdu_name = pd.text.strip().split('/')[-1]

        if sc.short_name:
            cat.socket_connections.append(sc)

    # Also look for TCP/UDP-TP config
    for tp_tag in ('TCP-TP', 'UDP-TP', 'SO-AD-CONFIG'):
        for tp_el in root.iter(_ns(tp_tag, ns)):
            port = _int_text(tp_el, 'PORT-NUMBER', ns)
            if port:
                sc = ArxmlSocketConnection()
                sc.short_name = _text(tp_el, 'SHORT-NAME', ns)
                sc.local_port = port
                sc.protocol = 'TCP' if 'TCP' in tp_tag else 'UDP'
                cat.socket_connections.append(sc)

    return cat


def parse_arxml_files(filepaths: Sequence[str]) -> ArxmlCatalog:
    """Parse multiple ARXML files and merge into a single catalogue."""
    merged = ArxmlCatalog()
    for fp in filepaths:
        cat = parse_arxml(fp)
        merged.source_files.extend(cat.source_files)
        merged.pdus.update(cat.pdus)
        merged.frames.update(cat.frames)
        merged.compu_methods.update(cat.compu_methods)
        merged.system_signals.update(cat.system_signals)
        merged.mirror_channels.extend(cat.mirror_channels)
        merged.someip_methods.extend(cat.someip_methods)
        merged.socket_connections.extend(cat.socket_connections)
        merged.errors.extend(cat.errors)
    return merged


def list_arxml_files(directory: str) -> List[str]:
    """List all .arxml files in a directory (non-recursive)."""
    if not os.path.isdir(directory):
        return []
    return sorted([
        f for f in os.listdir(directory)
        if f.lower().endswith('.arxml') and os.path.isfile(os.path.join(directory, f))
    ])


# ── Singleton / module-level catalogue ──────────────────────────────────────
_active_catalog: Optional[ArxmlCatalog] = None


def get_active_catalog() -> Optional[ArxmlCatalog]:
    return _active_catalog


def set_active_catalog(cat: ArxmlCatalog) -> None:
    global _active_catalog
    _active_catalog = cat


def load_catalog_from_directory(directory: str) -> ArxmlCatalog:
    """Parse all .arxml files in *directory* and set as active catalogue."""
    files = list_arxml_files(directory)
    paths = [os.path.join(directory, f) for f in files]
    cat = parse_arxml_files(paths)
    set_active_catalog(cat)
    return cat
