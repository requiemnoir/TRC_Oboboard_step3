"""Structured parser for automotive calibration/measurement description files.

Supported formats
-----------------
A2L  (ASAM MCD-2 MC)  — ECU description used by Vector CANape / INCA
LAB  (Vector CANape)   — signal list / measurement group file (.lab)
MAP  (GCC/LLVM linker) — symbol address map (.map)
SYM  (PEAK Symbol)     — text symbol table (.sym)

The parser is intentionally lenient: it extracts the data it can without
raising exceptions so that partial or non-standard files still work.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class A2lMeasurement:
    """MEASUREMENT or CHARACTERISTIC object extracted from the A2L."""
    name:         str
    object_type:  str          = 'MEASUREMENT'   # MEASUREMENT | CHARACTERISTIC
    long_id:      str          = ''
    data_type:    str          = 'FLOAT32_IEEE'
    address:      int          = 0
    addr_ext:     int          = 0
    factor:       float        = 1.0
    offset:       float        = 0.0
    unit:         str          = ''
    min_value:    Optional[float] = None
    max_value:    Optional[float] = None
    byte_order:   str          = 'little'
    bit_mask:     Optional[int] = None
    array_size:   int          = 1
    description:  str          = ''
    group:        str          = ''
    # computed
    size_bytes:   int          = field(default=0, init=False, repr=False)

    _DTYPE_SIZE: Dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _map = {
            'UBYTE': 1, 'SBYTE': 1,
            'UWORD': 2, 'SWORD': 2,
            'ULONG': 4, 'SLONG': 4,
            'A_UINT64': 8, 'A_INT64': 8,
            'FLOAT32_IEEE': 4,
            'FLOAT64_IEEE': 8,
        }
        self.size_bytes = _map.get(self.data_type.upper(), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name':        self.name,
            'type':        self.object_type,
            'long_id':     self.long_id,
            'data_type':   self.data_type,
            'address':     f'0x{self.address:08X}',
            'addr_ext':    self.addr_ext,
            'factor':      self.factor,
            'offset':      self.offset,
            'unit':        self.unit,
            'min':         self.min_value,
            'max':         self.max_value,
            'byte_order':  self.byte_order,
            'array_size':  self.array_size,
            'size_bytes':  self.size_bytes,
            'description': self.description,
            'group':       self.group,
        }


@dataclass
class A2lEvent:
    """TIME_CORRELATION or ECU event channel (for DAQ list assignment)."""
    channel:    int
    name:       str
    short_name: str          = ''
    cycle_ms:   float        = 0.0   # 0 = unknown / triggered
    priority:   int          = 0
    max_daq:    int          = 0
    description: str         = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'channel':    self.channel,
            'name':       self.name,
            'short_name': self.short_name,
            'cycle_ms':   self.cycle_ms,
            'priority':   self.priority,
            'max_daq':    self.max_daq,
            'description': self.description,
        }


@dataclass
class A2lXcpConfig:
    """XCP transport-layer configuration extracted from A2L IF_DATA sections.

    ASAM MCD-2 MC stores the XCP connection parameters inside:
      /begin IF_DATA XCP ... /end IF_DATA
    or the older:
      /begin IF_DATA ASAP1B_CCP ... /end IF_DATA

    Fields that could not be determined remain at their default (None or 0).
    """
    # CAN IDs
    cmd_id:         Optional[int]  = None   # Master → Slave  (CRO / CMD)
    res_id:         Optional[int]  = None   # Slave → Master  (DTO / RES)
    daq_id:         Optional[int]  = None   # DAQ DTO (if different from res_id)
    broadcast_id:   Optional[int]  = None   # Broadcast ID (optional)
    is_extended_id: Optional[bool] = None   # 29-bit CAN IDs
    is_canfd:       Optional[bool] = None   # CAN FD
    baudrate:       Optional[int]  = None   # CAN baudrate in bps
    # Protocol
    byte_order:     Optional[str]  = None   # 'little' | 'big'
    max_cto:        Optional[int]  = None   # Max CTO (Command Transfer Object)
    max_dto:        Optional[int]  = None   # Max DTO (Data Transfer Object)
    max_bs:         Optional[int]  = None   # Block Size (block mode)
    min_st:         Optional[int]  = None   # Min Separation Time (block mode)
    # DAQ
    max_daq:        Optional[int]  = None   # Max DAQ lists
    max_event_channel: Optional[int] = None
    daq_config_type:   Optional[str] = None   # 'STATIC' | 'DYNAMIC'
    timestamp_mode:    Optional[str] = None   # 'NO_TIMESTAMP' | 'SIZE_...'
    # Security
    seed_key_dll:   Optional[str]  = None   # Path to .skb / .dll
    # Protocol version
    protocol_version: Optional[str] = None
    transport_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return only non-None fields as a dict."""
        return {k: v for k, v in {
            'cmd_id':         f'0x{self.cmd_id:X}' if self.cmd_id is not None else None,
            'res_id':         f'0x{self.res_id:X}' if self.res_id is not None else None,
            'daq_id':         f'0x{self.daq_id:X}' if self.daq_id is not None else None,
            'broadcast_id':   f'0x{self.broadcast_id:X}' if self.broadcast_id is not None else None,
            'is_extended_id': self.is_extended_id,
            'is_canfd':       self.is_canfd,
            'baudrate':       self.baudrate,
            'byte_order':     self.byte_order,
            'max_cto':        self.max_cto,
            'max_dto':        self.max_dto,
            'max_bs':         self.max_bs,
            'min_st':         self.min_st,
            'max_daq':        self.max_daq,
            'max_event_channel': self.max_event_channel,
            'daq_config_type': self.daq_config_type,
            'timestamp_mode': self.timestamp_mode,
            'seed_key_dll':   self.seed_key_dll,
            'protocol_version': self.protocol_version,
            'transport_version': self.transport_version,
        }.items() if v is not None}

    def to_app_config(self) -> Dict[str, Any]:
        """Convert to the app_config.json 'xcp_can' format used by XcpCanClient.

        Only includes fields that were actually detected.
        """
        cfg: Dict[str, Any] = {}
        if self.cmd_id is not None:
            cfg['cmd_id'] = f'0x{self.cmd_id:X}'
        if self.res_id is not None:
            cfg['res_id'] = f'0x{self.res_id:X}'
        if self.is_extended_id is not None:
            cfg['is_extended_id'] = self.is_extended_id
        if self.is_canfd is not None:
            cfg['is_canfd'] = self.is_canfd
        if self.byte_order is not None:
            cfg['byte_order'] = self.byte_order
        if self.max_cto is not None:
            cfg['max_cto'] = self.max_cto
        if self.max_dto is not None:
            cfg['max_dto'] = self.max_dto
        return cfg


@dataclass
class A2lParseResult:
    """Complete parsed A2L descriptor."""
    measurements:    List[A2lMeasurement] = field(default_factory=list)
    characteristics: List[A2lMeasurement] = field(default_factory=list)
    events:          List[A2lEvent]       = field(default_factory=list)
    groups:          Dict[str, List[str]] = field(default_factory=dict)   # group→[names]
    xcp_config:      A2lXcpConfig         = field(default_factory=A2lXcpConfig)
    ecu_name:        str                  = ''
    ecu_version:     str                  = ''
    a2l_version:     str                  = ''
    errors:          List[str]            = field(default_factory=list)

    def all_signals(self) -> List[A2lMeasurement]:
        return self.measurements + self.characteristics

    def to_summary(self) -> Dict[str, Any]:
        return {
            'ecu_name':           self.ecu_name,
            'ecu_version':        self.ecu_version,
            'a2l_version':        self.a2l_version,
            'measurements_count': len(self.measurements),
            'characteristics_count': len(self.characteristics),
            'events_count':       len(self.events),
            'groups':             {g: len(v) for g, v in self.groups.items()},
            'xcp_config':         self.xcp_config.to_dict(),
            'errors':             self.errors[:20],
        }


# ─────────────────────────────────────────────────────────────────────────────
# A2L parser
# ─────────────────────────────────────────────────────────────────────────────

# Regexes compiled once at module load
_RE_FLOAT  = r'[\d.eE+\-]+'
_RE_HEX_INT = r'(?:0[xX][0-9A-Fa-f]+|\d+)'

_re_meas_block = re.compile(
    r'/begin\s+(MEASUREMENT|CHARACTERISTIC)\s+([\w.\-]+)'
    r'\s+.*?/end\s+(?:MEASUREMENT|CHARACTERISTIC)',
    re.DOTALL | re.IGNORECASE,
)
_re_event_block = re.compile(
    r'/begin\s+EVENT\s+([\w.\-]+)\s+([\w.\-]+)'   # short_name  long_name
    r'\s+(\d+)'                                    # channel_index
    r'\s.*?/end\s+EVENT',
    re.DOTALL | re.IGNORECASE,
)
_re_group_block = re.compile(
    r'/begin\s+GROUP\s+([\w.\-]+)(?:\s+"[^"]*")?\s+'
    r'.*?/begin\s+REF_MEASUREMENT\s+(.*?)/end\s+REF_MEASUREMENT'
    r'.*?/end\s+GROUP',
    re.DOTALL | re.IGNORECASE,
)
_re_ecu_name    = re.compile(r'/begin\s+MODULE\s+([\w.\-]+)', re.IGNORECASE)
_re_a2l_ver     = re.compile(r'ASAP2_VERSION\s+(\d+)\s+(\d+)', re.IGNORECASE)


def _extract_kv(block: str, key: str, n_vals: int = 1) -> Optional[Tuple]:
    """Extract `key val1 [val2 ...]` from an A2L block. Returns tuple or None."""
    pat = re.compile(
        r'\b' + re.escape(key) + r'\s+' +
        r'\s+'.join([r'(' + _RE_FLOAT + r'|' + _RE_HEX_INT + r'|"[^"]*"|\w+)'] * n_vals),
        re.IGNORECASE,
    )
    m = pat.search(block)
    if not m:
        return None
    vals = []
    for g in m.groups():
        s = g.strip('"')
        vals.append(s)
    return tuple(vals)


def _safe_int(s: Any) -> int:
    try:
        return int(str(s), 0)
    except Exception:
        return 0


def _safe_float(s: Any) -> float:
    try:
        return float(str(s))
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# XCP transport-layer config extraction from IF_DATA blocks
# ─────────────────────────────────────────────────────────────────────────────

def _extract_xcp_config(text: str) -> A2lXcpConfig:
    """Extract XCP transport-layer parameters from A2L IF_DATA sections.

    A2L files contain XCP configuration in multiple possible forms:
    1. /begin IF_DATA XCP ... /end IF_DATA   (ASAM MCD-2 MC standard)
    2. /begin IF_DATA ASAP1B_CCP ... /end IF_DATA  (older CCP)
    3. /begin IF_DATA CANAPE_EXT ... /end IF_DATA   (Vector CANape extension)

    Inside IF_DATA XCP we look for:
    • /begin XCP_ON_CAN ... /end XCP_ON_CAN
    • /begin PROTOCOL_LAYER ... /end PROTOCOL_LAYER
    • /begin DAQ ... /end DAQ
    • /begin SEGMENT ... /end SEGMENT
    • SEED_AND_KEY / CHECKSUM references

    Also parses standalone MOD_PAR / MOD_COMMON for BYTE_ORDER.
    """
    cfg = A2lXcpConfig()

    # ── Global BYTE_ORDER from MOD_COMMON ──────────────────────────────
    m_bo = re.search(r'/begin\s+MOD_COMMON\s+.*?BYTE_ORDER\s+(MSB_FIRST|LSB_FIRST|MSB_LAST|LSB_LAST)',
                     text, re.DOTALL | re.IGNORECASE)
    if m_bo:
        val = m_bo.group(1).upper()
        cfg.byte_order = 'big' if val in ('MSB_FIRST', 'LSB_LAST') else 'little'

    # ── Collect all IF_DATA blocks ─────────────────────────────────────
    if_data_blocks: List[str] = []
    for m in re.finditer(r'/begin\s+IF_DATA\s+(\w+)(.*?)/end\s+IF_DATA',
                         text, re.DOTALL | re.IGNORECASE):
        if_data_blocks.append(m.group(0))

    if not if_data_blocks:
        return cfg

    full_if = '\n'.join(if_data_blocks)

    # ── XCP_ON_CAN section ─────────────────────────────────────────────
    # Typical layout:
    #   /begin XCP_ON_CAN
    #     CAN_ID_BROADCAST     0x100
    #     CAN_ID_MASTER        0x601
    #     CAN_ID_SLAVE         0x602
    #     BAUDRATE             500000
    #     SAMPLE_POINT         80
    #     MAX_DLC_REQUIRED
    #     CAN_FD { ... }
    #   /end XCP_ON_CAN
    m_xoc = re.search(
        r'/begin\s+XCP_ON_CAN\b(.*?)/end\s+XCP_ON_CAN',
        full_if, re.DOTALL | re.IGNORECASE,
    )
    if m_xoc:
        xoc = m_xoc.group(1)
        _parse_xcp_on_can(xoc, cfg)
    else:
        # Some A2L files put CAN IDs directly under IF_DATA XCP
        # or use CAN_ID_xx at top level — try from the entire IF_DATA blob
        _parse_xcp_on_can(full_if, cfg)

    # ── PROTOCOL_LAYER section ─────────────────────────────────────────
    m_pl = re.search(
        r'/begin\s+PROTOCOL_LAYER\b(.*?)/end\s+PROTOCOL_LAYER',
        full_if, re.DOTALL | re.IGNORECASE,
    )
    if m_pl:
        _parse_protocol_layer(m_pl.group(1), cfg)
    else:
        # Try inline tokens
        _parse_protocol_layer(full_if, cfg)

    # ── DAQ section ────────────────────────────────────────────────────
    m_daq = re.search(
        r'/begin\s+DAQ\b(.*?)/end\s+DAQ',
        full_if, re.DOTALL | re.IGNORECASE,
    )
    if m_daq:
        _parse_daq_config(m_daq.group(1), cfg)

    # ── Seed & Key ─────────────────────────────────────────────────────
    m_sk = re.search(
        r'SEED_AND_KEY\s+"([^"]*)"',
        full_if, re.IGNORECASE,
    )
    if m_sk:
        cfg.seed_key_dll = m_sk.group(1)

    return cfg


def _parse_xcp_on_can(block: str, cfg: A2lXcpConfig) -> None:
    """Extract CAN-specific XCP parameters from a text block."""

    # CAN IDs — many naming variants exist across tool vendors
    # Master → Slave (CMD): CAN_ID_MASTER / CAN_ID_CMD / CMD_ID / CRO_ID
    for pat in (
        r'CAN_ID_MASTER\s+(' + _RE_HEX_INT + r')',
        r'CAN_ID_CMD\s+(' + _RE_HEX_INT + r')',
        r'CMD_ID\s+(' + _RE_HEX_INT + r')',
        r'CRO_ID\s+(' + _RE_HEX_INT + r')',
    ):
        m = re.search(pat, block, re.IGNORECASE)
        if m:
            cfg.cmd_id = _safe_int(m.group(1))
            break

    # Slave → Master (RES): CAN_ID_SLAVE / CAN_ID_RES / RES_ID / DTO_ID
    for pat in (
        r'CAN_ID_SLAVE\s+(' + _RE_HEX_INT + r')',
        r'CAN_ID_RES\s+(' + _RE_HEX_INT + r')',
        r'RES_ID\s+(' + _RE_HEX_INT + r')',
        r'DTO_ID\s+(' + _RE_HEX_INT + r')',
    ):
        m = re.search(pat, block, re.IGNORECASE)
        if m:
            cfg.res_id = _safe_int(m.group(1))
            break

    # DAQ DTO ID (sometimes separate from RES)
    m = re.search(r'CAN_ID_DAQ\s+(' + _RE_HEX_INT + r')', block, re.IGNORECASE)
    if m:
        cfg.daq_id = _safe_int(m.group(1))

    # Broadcast ID
    m = re.search(r'CAN_ID_BROADCAST\s+(' + _RE_HEX_INT + r')', block, re.IGNORECASE)
    if m:
        cfg.broadcast_id = _safe_int(m.group(1))

    # Baudrate
    m = re.search(r'BAUDRATE\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.baudrate = int(m.group(1))

    # Extended IDs (29-bit)
    # Check explicitly for EXTENDED keyword on CAN_ID lines (not in comments)
    if re.search(r'\bCAN_ID_MASTER\s+0x[0-9A-Fa-f]+\s+EXTENDED', block, re.IGNORECASE):
        cfg.is_extended_id = True
    elif re.search(r'\bCAN_ID_SLAVE\s+0x[0-9A-Fa-f]+\s+EXTENDED', block, re.IGNORECASE):
        cfg.is_extended_id = True
    elif re.search(r'\bCAN_ID_BROADCAST\s+0x[0-9A-Fa-f]+\s+EXTENDED', block, re.IGNORECASE):
        cfg.is_extended_id = True
    elif re.search(r'(?<![_\w])\bXTD\b(?![_\w])', block):
        # XTD keyword standalone (not in comments/descriptions)
        cfg.is_extended_id = True
    elif cfg.cmd_id is not None and cfg.cmd_id > 0x7FF:
        cfg.is_extended_id = True
    elif cfg.is_extended_id is None:
        cfg.is_extended_id = False

    # CAN FD
    if re.search(r'\bCAN_FD\b|\bCANFD\b', block, re.IGNORECASE):
        cfg.is_canfd = True
    elif re.search(r'\bMAX_DLC\s+64\b|\bMAX_DLC_REQUIRED\b', block, re.IGNORECASE):
        cfg.is_canfd = True
    elif cfg.is_canfd is None:
        cfg.is_canfd = False

    # MAX_CTO / MAX_DTO can appear here too
    m = re.search(r'MAX_CTO\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.max_cto = int(m.group(1))
    m = re.search(r'MAX_DTO\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.max_dto = int(m.group(1))


def _parse_protocol_layer(block: str, cfg: A2lXcpConfig) -> None:
    """Extract XCP protocol-layer parameters."""

    # PROTOCOL_LAYER typically has positional params:
    #   T1  T2  T3  T4  T5  T6  T7  MAX_CTO  MAX_DTO  BYTE_ORDER  ...
    # But many A2L files use keyword=value notation.

    # MAX_CTO / MAX_DTO (keyword form)
    if cfg.max_cto is None:
        m = re.search(r'MAX_CTO\s+(\d+)', block, re.IGNORECASE)
        if m:
            cfg.max_cto = int(m.group(1))
    if cfg.max_dto is None:
        m = re.search(r'MAX_DTO\s+(\d+)', block, re.IGNORECASE)
        if m:
            cfg.max_dto = int(m.group(1))

    # BYTE_ORDER in protocol layer
    m = re.search(r'BYTE_ORDER\s+(MSB_FIRST|LSB_FIRST|BYTE_ORDER_MSB_FIRST|BYTE_ORDER_MSB_LAST)',
                  block, re.IGNORECASE)
    if m:
        val = m.group(1).upper()
        cfg.byte_order = 'big' if 'MSB_FIRST' in val else 'little'

    # Block mode: MAX_BS / MIN_ST
    m = re.search(r'MAX_BS\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.max_bs = int(m.group(1))
    m = re.search(r'MIN_ST\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.min_st = int(m.group(1))

    # Protocol / Transport version
    m = re.search(r'PROTOCOL_LAYER_VERSION\s+(\S+)', block, re.IGNORECASE)
    if not m:
        m = re.search(r'XCP_PROTOCOL_LAYER_VERSION\s+(\S+)', block, re.IGNORECASE)
    if m:
        cfg.protocol_version = m.group(1)

    m = re.search(r'TRANSPORT_LAYER_VERSION\s+(\S+)', block, re.IGNORECASE)
    if m:
        cfg.transport_version = m.group(1)

    # Positional parse fallback: /begin PROTOCOL_LAYER  T1 T2 T3 T4 T5 T6 T7 MaxCTO MaxDTO  ByteOrder ...
    # If we still don't have max_cto/max_dto, try positional
    if cfg.max_cto is None or cfg.max_dto is None:
        tokens = re.split(r'\s+', block.strip())
        # Find decimal numbers > 4 that could be CTO/DTO
        nums = []
        for t in tokens:
            try:
                v = int(t, 0)
                if 4 <= v <= 64:
                    nums.append(v)
            except Exception:
                pass
        # Heuristic: the last 2 numbers in [4..64] range are likely CTO, DTO
        if len(nums) >= 2:
            if cfg.max_cto is None:
                cfg.max_cto = nums[-2]
            if cfg.max_dto is None:
                cfg.max_dto = nums[-1]


def _parse_daq_config(block: str, cfg: A2lXcpConfig) -> None:
    """Extract DAQ configuration parameters."""

    # DAQ configuration type
    if re.search(r'\bDYNAMIC\b', block, re.IGNORECASE):
        cfg.daq_config_type = 'DYNAMIC'
    elif re.search(r'\bSTATIC\b', block, re.IGNORECASE):
        cfg.daq_config_type = 'STATIC'

    # MAX_DAQ
    m = re.search(r'MAX_DAQ\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.max_daq = int(m.group(1))

    # MAX_EVENT_CHANNEL
    m = re.search(r'MAX_EVENT_CHANNEL\s+(\d+)', block, re.IGNORECASE)
    if m:
        cfg.max_event_channel = int(m.group(1))

    # Timestamp
    m = re.search(r'TIMESTAMP_SUPPORTED\b|TIMESTAMP_MODE\s+(\w+)', block, re.IGNORECASE)
    if m:
        cfg.timestamp_mode = m.group(1) if m.group(1) else 'SUPPORTED'


# ── COMPU_METHOD pre-parser ──────────────────────────────────────────────────
_re_compu_block = re.compile(
    r'/begin\s+COMPU_METHOD\s+([\w.\-]+)\s+'
    r'.*?/end\s+COMPU_METHOD',
    re.DOTALL | re.IGNORECASE,
)

@dataclass
class _CompuMethod:
    """Parsed COMPU_METHOD — unit + linear conversion."""
    name:   str
    unit:   str   = ''
    factor: float = 1.0
    offset: float = 0.0
    method: str   = 'IDENTICAL'   # RAT_FUNC | TAB_VERB | IDENTICAL | ...

def _parse_compu_methods(text: str) -> Dict[str, '_CompuMethod']:
    """Build name → _CompuMethod dict from all /begin COMPU_METHOD blocks."""
    out: Dict[str, _CompuMethod] = {}
    _RE_COEFF6 = re.compile(
        r'COEFFS\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')'
        r'\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')',
        re.IGNORECASE,
    )
    _RE_COEFF2 = re.compile(
        r'COEFFS_LINEAR\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')', re.IGNORECASE
    )

    for m in _re_compu_block.finditer(text):
        name = m.group(1)
        blk  = m.group(0)
        cm   = _CompuMethod(name=name)

        # Method type: 2nd positional token after "long_id"
        mt = re.search(r'(RAT_FUNC|TAB_VERB|IDENTICAL|LINEAR|TAB_INTP|TAB_NOINTP|FORM)',
                       blk, re.IGNORECASE)
        if mt:
            cm.method = mt.group(1).upper()

        # Unit: 5th positional field – comes as "unit" after format string
        # Layout:  Name "LongId" MethodType "Format" "Unit"
        u = re.findall(r'"([^"]*)"', blk)
        # u[0] = LongId, u[1] = format, u[2] = unit  (if present)
        if len(u) >= 3 and u[2] and u[2].lower() not in ('', 'nounit'):
            cm.unit = u[2]
        elif len(u) >= 2 and u[1] and u[1].lower() not in ('', 'nounit', '%10.5'):
            # Some A2Ls have only 2 quoted strings
            pass

        # REF_UNIT keyword
        ru = re.search(r'REF_UNIT\s+([\w.\-]+)', blk, re.IGNORECASE)
        if ru and not cm.unit:
            unit_name = ru.group(1)
            # Strip common wrapper prefixes (e.g. RB_RBA_Common_CentralElements_Units_NoUnit)
            cleaned = re.sub(r'^.*_Units?_', '', unit_name)
            if cleaned.lower() not in ('nounit', 'no_unit', '', '-'):
                cm.unit = cleaned

        # COEFFS (6-param rational): phys = (c0 + c1*x - c2) / (c3 + c4*x - c5)
        # Simplified linear: factor = c1/c4 if c0=0,c2=0,c3=0,c5=1
        mc6 = _RE_COEFF6.search(blk)
        if mc6:
            c = [float(mc6.group(i)) for i in range(1, 7)]
            # Standard form: phys = (c0 + c1*raw - c2) / (c3 + c4*raw - c5)
            # For linear: c0=0, c2=0, c3=0, c5=1 → phys = c1*raw / (c4*raw - 1)
            # Actually ASAM: phys = (c2 - c0) / (c3 - c1) * raw + ...
            # Typical: COEFFS 0 N 0 0 0 D → factor = N/D
            if c[4] != 0:
                cm.factor = c[1] / c[4]
                cm.offset = (c[0] - c[3] * cm.factor)
            elif c[1] != 0:
                cm.factor = c[1]
                cm.offset = c[0]
        else:
            mc2 = _RE_COEFF2.search(blk)
            if mc2:
                cm.factor = float(mc2.group(1))
                cm.offset = float(mc2.group(2))

        out[name] = cm
    return out


def parse_a2l(text: str) -> A2lParseResult:
    """Parse an A2L file and return a structured result.

    Extracts:
    • MEASUREMENT  objects → result.measurements
    • CHARACTERISTIC objects → result.characteristics
    • EVENT objects → result.events
    • GROUP / REF_MEASUREMENT → result.groups
    """
    result = A2lParseResult()

    # ── Version / ECU identification ──────────────────────────────────
    m_ver = _re_a2l_ver.search(text)
    if m_ver:
        result.a2l_version = f'{m_ver.group(1)}.{m_ver.group(2)}'

    m_mod = _re_ecu_name.search(text)
    if m_mod:
        result.ecu_name = m_mod.group(1)

    # Read PROJECT description if present
    m_proj = re.search(r'/begin\s+PROJECT\s+\S+\s+"([^"]+)"', text, re.IGNORECASE)
    if m_proj:
        result.ecu_version = m_proj.group(1)[:120]

    # ── XCP transport-layer config from IF_DATA ──────────────────────
    result.xcp_config = _extract_xcp_config(text)

    # ── Pre-parse COMPU_METHOD blocks for unit/factor lookup ─────────
    compu_methods = _parse_compu_methods(text)

    # ── Events ────────────────────────────────────────────────────────
    # Try /begin EVENT parsing first
    for em in _re_event_block.finditer(text):
        short = em.group(1)
        long  = em.group(2)
        chan_idx = _safe_int(em.group(3))
        blk = em.group(0)

        # CYCLE_OFFSET  unit  period  (some variants RATE instead)
        cycle_ms = 0.0
        m_rate = re.search(r'(?:CYCLE_OFFSET|RATE)\s+(\d+)\s+(\d+)\s+(\d+)', blk, re.IGNORECASE)
        if m_rate:
            # field 3 = cycle time in µs/10µs/100µs depending on unit field
            # field 2 = time unit (5=ms, 6=10ms …), simplified
            try:
                cycle_ms = float(m_rate.group(3)) * 10 / 1000  # assume 10µs unit
            except Exception:
                pass

        result.events.append(A2lEvent(
            channel    = chan_idx,
            name       = long or short,
            short_name = short,
            cycle_ms   = cycle_ms,
        ))

    # Fallback: synthesise event 0 if none found
    if not result.events:
        result.events.append(A2lEvent(channel=0, name='Default', short_name='EVT_0', cycle_ms=0))

    # ── MEASUREMENT & CHARACTERISTIC blocks ───────────────────────────
    _RE_ADDR   = re.compile(r'ECU_ADDRESS\s+(' + _RE_HEX_INT + r')', re.IGNORECASE)
    _RE_EXT    = re.compile(r'ECU_ADDRESS_EXTENSION\s+(' + _RE_HEX_INT + r')', re.IGNORECASE)
    _RE_DTYPE  = re.compile(r'(?<!\w)(?:DATATYPE|DATA_TYPE)\s+([\w]+)', re.IGNORECASE)
    _RE_UNIT   = re.compile(r'PHYS_UNIT\s+"([^"]*)"', re.IGNORECASE)
    _RE_COEFF  = re.compile(
        r'COEFFS_LINEAR\s+(' + _RE_FLOAT + r')\s+(' + _RE_FLOAT + r')', re.IGNORECASE
    )
    _RE_COEFFS = re.compile(
        r'COEFFS\s+\S+\s+\S+\s+(' + _RE_FLOAT + r')\s+\S+\s+\S+\s+(' + _RE_FLOAT + r')',
        re.IGNORECASE,
    )
    _RE_LOWER  = re.compile(r'LOWER_LIMIT\s+(' + _RE_FLOAT + r')', re.IGNORECASE)
    _RE_UPPER  = re.compile(r'UPPER_LIMIT\s+(' + _RE_FLOAT + r')', re.IGNORECASE)
    _RE_BO     = re.compile(r'BYTE_ORDER\s+(MSB_FIRST|LSB_FIRST)', re.IGNORECASE)
    _RE_DESC   = re.compile(r'LONG_IDENTIFIER\s+"([^"]*)"', re.IGNORECASE)
    _RE_ANNOT  = re.compile(r'\bANNOTATION_ORIGIN\s+"([^"]*)"', re.IGNORECASE)
    _RE_ARRAY  = re.compile(r'\bARRAY_SIZE\s+(\d+)', re.IGNORECASE)
    _RE_BIT    = re.compile(r'\bBIT_MASK\s+(' + _RE_HEX_INT + r')', re.IGNORECASE)

    for m in _re_meas_block.finditer(text):
        obj_type = m.group(1).upper()
        name     = m.group(2)
        blk      = m.group(0)

        try:
            # Address ─────────────────────────────────────────────────
            ma = _RE_ADDR.search(blk)
            if not ma:
                # For CHARACTERISTIC, address is sometimes the 5th token after the type
                # /begin CHARACTERISTIC  NAME  "LongID"  TYPE  ADDR  ...
                toks = blk.split()
                addr = 0
                for i, t in enumerate(toks):
                    if t.upper() in ('MEASUREMENT', 'CHARACTERISTIC') and i == 0:
                        # after: NAME  "long"  dtype  addr
                        for j in range(i + 1, min(i + 10, len(toks))):
                            try:
                                v = int(toks[j], 0)
                                if v > 0x1000:
                                    addr = v
                                    break
                            except Exception:
                                pass
            else:
                addr = _safe_int(ma.group(1))

            # For CHARACTERISTIC the address field position is different
            if addr == 0 and obj_type == 'CHARACTERISTIC':
                # Layout: /begin CHARACTERISTIC name "long" type ADDR ...
                toks = re.split(r'\s+', blk.strip())
                # Find position after object type keyword
                for j, t in enumerate(toks):
                    if t.upper() == 'CHARACTERISTIC':
                        # skip name, long_id (quoted), type, then address
                        k = j + 1
                        while k < len(toks) and k < j + 12:
                            try:
                                v = int(toks[k], 0)
                                if v > 0x1000:
                                    addr = v
                                    break
                            except Exception:
                                pass
                            k += 1
                        break

            ext  = _safe_int(_RE_EXT.search(blk).group(1)) if _RE_EXT.search(blk) else 0

            # Data type ────────────────────────────────────────────────
            md  = _RE_DTYPE.search(blk)
            dtype = md.group(1).upper() if md else 'FLOAT32_IEEE'
            # Positional fallback for MEASUREMENT:
            #   /begin MEASUREMENT Name "LongId" DataType CompuMethod ...
            # DataType is 3rd positional token (after Name and "LongId")
            _KNOWN_DTYPES = {
                'UBYTE', 'SBYTE', 'UWORD', 'SWORD', 'ULONG', 'SLONG',
                'A_UINT64', 'A_INT64', 'FLOAT16_IEEE', 'FLOAT32_IEEE', 'FLOAT64_IEEE',
            }
            if not md and obj_type == 'MEASUREMENT':
                _dm = re.match(
                    r'/begin\s+MEASUREMENT\s+\S+\s+"[^"]*"\s+(\w+)',
                    blk, re.IGNORECASE,
                )
                if _dm and _dm.group(1).upper() in _KNOWN_DTYPES:
                    dtype = _dm.group(1).upper()
            # If no explicit DATATYPE, the token after the type keyword is the dtype
            if not md and obj_type == 'CHARACTERISTIC':
                toks = re.split(r'\s+', blk.strip())
                for j, t in enumerate(toks):
                    if t.upper() == 'CHARACTERISTIC' and j + 3 < len(toks):
                        dtype = toks[j + 3].upper()
                        break

            # Calibration coefficients ─────────────────────────────────
            # 1) Try inline COEFFS/COEFFS_LINEAR inside the block
            factor, offset = 1.0, 0.0
            mc = _RE_COEFF.search(blk)
            if mc:
                factor = _safe_float(mc.group(1))
                offset = _safe_float(mc.group(2))
            else:
                mc2 = _RE_COEFFS.search(blk)
                if mc2:
                    factor = _safe_float(mc2.group(1))
                    offset = _safe_float(mc2.group(2))

            # 2) Extract COMPU_METHOD reference name from positional tokens
            #    MEASUREMENT: Name "LongId" DataType CompuMethodRef Res Acc Lower Upper
            #    CHARACTERISTIC: Name "LongId" Type Addr RecordLayout MaxDiff CompuMethodRef Lower Upper
            compu_ref = ''
            _cm_match = re.match(
                r'/begin\s+MEASUREMENT\s+\S+\s+"[^"]*"\s+\w+\s+([\w.\-]+)',
                blk, re.IGNORECASE,
            )
            if _cm_match:
                compu_ref = _cm_match.group(1)
            elif obj_type == 'CHARACTERISTIC':
                # For CHARACTERISTIC, CompuMethod is further in the line
                _cm_char = re.match(
                    r'/begin\s+CHARACTERISTIC\s+\S+\s+"[^"]*"\s+\w+\s+\S+\s+\S+\s+\S+\s+([\w.\-]+)',
                    blk, re.IGNORECASE,
                )
                if _cm_char:
                    compu_ref = _cm_char.group(1)

            # 3) Look up COMPU_METHOD for unit / factor / offset
            cm_unit = ''
            if compu_ref and compu_ref in compu_methods:
                cm = compu_methods[compu_ref]
                cm_unit = cm.unit
                # Only override factor/offset if inline COEFFS were not found
                if not mc and not _RE_COEFFS.search(blk):
                    if cm.factor != 1.0 or cm.offset != 0.0:
                        factor = cm.factor
                        offset = cm.offset

            # Limits and meta ──────────────────────────────────────────
            ml = _RE_LOWER.search(blk)
            mu = _RE_UPPER.search(blk)
            mb = _RE_BO.search(blk)
            munit = _RE_UNIT.search(blk)
            mdesc = _RE_DESC.search(blk)
            marr  = _RE_ARRAY.search(blk)
            mbit  = _RE_BIT.search(blk)
            mlong = re.match(
                r'/begin\s+(?:MEASUREMENT|CHARACTERISTIC)\s+\S+\s+"([^"]*)"',
                blk, re.IGNORECASE,
            )

            # Unit: PHYS_UNIT inline > COMPU_METHOD unit > empty
            unit_val = ''
            if munit and munit.group(1).strip():
                unit_val = munit.group(1)
            elif cm_unit:
                unit_val = cm_unit

            # Description: LONG_IDENTIFIER > ANNOTATION_TEXT > ANNOTATION_ORIGIN > long_id > empty
            desc_val = ''
            if mdesc:
                desc_val = mdesc.group(1)
            else:
                # Parse ANNOTATION_TEXT blocks — extract first non-empty text
                _at = re.search(
                    r'/begin\s+ANNOTATION_TEXT\s+"?([^"]*?)"?\s*/end\s+ANNOTATION_TEXT',
                    blk, re.IGNORECASE | re.DOTALL,
                )
                if _at and _at.group(1).strip():
                    desc_val = _at.group(1).strip().replace('\\r\\n', ' ').replace('\r\n', ' ').replace('\n', ' ').strip()
                else:
                    _ao = _RE_ANNOT.search(blk)
                    if _ao:
                        desc_val = _ao.group(1)

            sig = A2lMeasurement(
                name        = name,
                object_type = obj_type,
                long_id     = mlong.group(1) if mlong else (mdesc.group(1) if mdesc else ''),
                data_type   = dtype,
                address     = addr,
                addr_ext    = ext,
                factor      = factor,
                offset      = offset,
                unit        = unit_val,
                min_value   = _safe_float(ml.group(1)) if ml else None,
                max_value   = _safe_float(mu.group(1)) if mu else None,
                byte_order  = 'big' if (mb and mb.group(1).upper() == 'MSB_FIRST') else 'little',
                array_size  = _safe_int(marr.group(1)) if marr else 1,
                description = desc_val,
                bit_mask    = _safe_int(mbit.group(1)) if mbit else None,
            )

            if obj_type == 'MEASUREMENT':
                result.measurements.append(sig)
            else:
                result.characteristics.append(sig)

        except Exception as exc:
            result.errors.append(f'{name}: {exc}')

    # ── Groups ────────────────────────────────────────────────────────
    for gm in _re_group_block.finditer(text):
        gname = gm.group(1)
        refs  = re.findall(r'(\w[\w.\-]+)', gm.group(2))
        result.groups[gname] = refs

    # Auto-assign group from existing groups
    sig_to_group: Dict[str, str] = {}
    for grp, names in result.groups.items():
        for n in names:
            sig_to_group[n] = grp
    for s in result.all_signals():
        if s.name in sig_to_group:
            s.group = sig_to_group[s.name]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# LAB file parser  (Vector CANape .lab)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LabSignalConfig:
    """Per-signal acquisition configuration derived from LAB group names."""
    name:          str
    group:         str   = '__default__'
    raster_ms:     float = 0.0        # 0 = unknown / event-driven
    mode:          str   = 'daq'      # 'daq' | 'polling'
    event_channel: int   = 0          # resolved after matching vs A2L events
    prescaler:     int   = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name':          self.name,
            'group':         self.group,
            'raster_ms':     self.raster_ms,
            'mode':          self.mode,
            'event_channel': self.event_channel,
            'prescaler':     self.prescaler,
        }


@dataclass
class LabParseResult:
    """Parsed content of a Vector CANape .lab file."""
    groups:         Dict[str, List[str]]            = field(default_factory=dict)
    signals:        List[str]                       = field(default_factory=list)
    signal_configs: Dict[str, LabSignalConfig]      = field(default_factory=dict)
    errors:         List[str]                       = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'groups':         self.groups,
            'signals':        self.signals,
            'signal_configs': {k: v.to_dict() for k, v in self.signal_configs.items()},
            'errors':         self.errors,
        }


# Regex to extract a numeric raster from a group name, e.g. "100ms", "10 ms", "1000us"
_LAB_RASTER_RE = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*'               # numeric value
    r'(ms|us|µs|s|sec|millisec|hz)\b',     # unit
    re.IGNORECASE,
)
_LAB_POLL_RE = re.compile(r'\bpoll(?:ing)?\b', re.IGNORECASE)


def _parse_group_raster(group_name: str) -> Tuple[float, str]:
    """Extract raster period (ms) and mode from a LAB group name.

    Returns (raster_ms, mode) where mode is 'daq' or 'polling'.
    """
    if _LAB_POLL_RE.search(group_name):
        return (0.0, 'polling')

    m = _LAB_RASTER_RE.search(group_name)
    if m:
        val = float(m.group(1).replace(',', '.'))
        unit = m.group(2).lower()
        if unit in ('us', 'µs'):
            return (val / 1000.0, 'daq')
        elif unit in ('s', 'sec'):
            return (val * 1000.0, 'daq')
        elif unit in ('hz',):
            return ((1000.0 / val) if val > 0 else 0.0, 'daq')
        else:  # ms, millisec
            return (val, 'daq')

    return (0.0, 'daq')


def resolve_lab_events(
    lab: 'LabParseResult',
    events: List[A2lEvent],
) -> None:
    """Resolve LAB group rasters to the best-matching A2L event channels.

    Modifies ``lab.signal_configs`` in-place, setting ``event_channel`` and
    ``prescaler`` for each signal.  When a group raster cannot be matched
    exactly it picks the event whose ``cycle_ms`` makes the best prescaler.
    """
    if not events:
        return

    # Build lookup: cycle_ms → event (prefer lowest channel index)
    by_cycle: Dict[float, A2lEvent] = {}
    for ev in sorted(events, key=lambda e: e.channel):
        if ev.cycle_ms > 0 and ev.cycle_ms not in by_cycle:
            by_cycle[ev.cycle_ms] = ev

    base_event = events[0]  # fallback: first event

    for cfg in lab.signal_configs.values():
        if cfg.mode == 'polling':
            continue  # polling signals don't need event assignment

        if cfg.raster_ms <= 0:
            cfg.event_channel = base_event.channel
            cfg.prescaler = 1
            continue

        # Exact match
        if cfg.raster_ms in by_cycle:
            cfg.event_channel = by_cycle[cfg.raster_ms].channel
            cfg.prescaler = 1
            continue

        # Best match: find event whose cycle_ms divides raster_ms evenly
        best_ev = base_event
        best_pre = max(1, round(cfg.raster_ms / base_event.cycle_ms)) if base_event.cycle_ms > 0 else 1
        for cycle, ev in by_cycle.items():
            if cycle <= 0:
                continue
            pre = cfg.raster_ms / cycle
            if pre >= 1 and abs(pre - round(pre)) < 0.01:
                ipre = int(round(pre))
                if 1 <= ipre <= 255:
                    best_ev = ev
                    best_pre = ipre
                    break
        cfg.event_channel = best_ev.channel
        cfg.prescaler = max(1, min(255, best_pre))


def parse_lab(text: str) -> LabParseResult:
    """Parse a Vector CANape .lab file.

    LAB file grammar (simplified):
      [GROUP_NAME]
      SignalName1
      SignalName2
      ...
      [NEXT_GROUP]
      ...

    Signals not under any group header go into group '__default__'.
    Group names encode raster / timing info (e.g. ``100ms``, ``10ms``,
    ``Polling``) which is extracted into ``signal_configs``.
    """
    result  = LabParseResult()
    current = '__default__'
    all_sigs: List[str] = []
    group_rasters: Dict[str, Tuple[float, str]] = {}  # group → (raster_ms, mode)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(';') or line.startswith('//'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1].strip()
            if current not in result.groups:
                result.groups[current] = []
            if current not in group_rasters:
                group_rasters[current] = _parse_group_raster(current)
        else:
            # Signal name — may have a comment after whitespace
            sig_name = line.split(maxsplit=1)[0].rstrip(';')
            if sig_name:
                result.groups.setdefault(current, []).append(sig_name)
                all_sigs.append(sig_name)
                if sig_name not in result.signal_configs:
                    raster, mode = group_rasters.get(current, (0.0, 'daq'))
                    result.signal_configs[sig_name] = LabSignalConfig(
                        name=sig_name,
                        group=current,
                        raster_ms=raster,
                        mode=mode,
                    )

    # Deduplicate preserving order
    seen: set = set()
    for s in all_sigs:
        if s not in seen:
            result.signals.append(s)
            seen.add(s)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# GLC file parser  (Vector VLConfig XML — CcpXcpSignal blocks)
# ─────────────────────────────────────────────────────────────────────────────

_GLC_BLOCK_RE = re.compile(r'<CcpXcpSignal>(.*?)</CcpXcpSignal>', re.DOTALL)


def _glc_val(block: str, tag: str) -> str:
    """Extract ``<Tag><Value>…</Value></Tag>`` from a CcpXcpSignal block."""
    m = re.search(
        rf'<{re.escape(tag)}>\s*<Value>(.*?)</Value>\s*</{re.escape(tag)}>',
        block, re.DOTALL,
    )
    return m.group(1).strip() if m else ''


def parse_glc(text: str) -> LabParseResult:
    """Parse a Vector VLConfig *.glc XML file.

    Extracts ``<CcpXcpSignal>`` blocks and returns a :class:`LabParseResult`
    compatible with the existing LAB import pipeline.  Each signal carries:

    * **Name** – measurement name (must exist in A2L)
    * **MeasurementMode** – ``DAQ`` or ``Polling``
    * **DaqEventId** – maps to ``event_channel``
    * **PollingTime** – raster in ms (used as ``raster_ms``)
    * **EcuName** – stored in group field for informational purposes
    * **IsActive** – inactive signals are skipped
    """
    result = LabParseResult()
    blocks = _GLC_BLOCK_RE.findall(text)
    if not blocks:
        result.errors.append('No <CcpXcpSignal> blocks found in GLC file')
        return result

    seen: set = set()
    for blk in blocks:
        name = _glc_val(blk, 'Name')
        if not name:
            continue

        active = _glc_val(blk, 'IsActive').lower()
        if active == 'false':
            continue

        mode_raw = _glc_val(blk, 'MeasurementMode').lower()
        mode = 'polling' if mode_raw == 'polling' else 'daq'

        try:
            event_ch = int(_glc_val(blk, 'DaqEventId') or '0')
        except ValueError:
            event_ch = 0

        try:
            raster_ms = float(_glc_val(blk, 'PollingTime') or '0')
        except ValueError:
            raster_ms = 0.0

        ecu = _glc_val(blk, 'EcuName') or 'unknown'
        group = f'{ecu}_evt{event_ch}'

        result.groups.setdefault(group, []).append(name)

        if name not in seen:
            result.signals.append(name)
            seen.add(name)

        if name not in result.signal_configs:
            result.signal_configs[name] = LabSignalConfig(
                name=name,
                group=group,
                raster_ms=raster_ms,
                mode=mode,
                event_channel=event_ch,
                prescaler=1,
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Symbol / MAP file parser (linker map → ECU addresses)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolEntry:
    name:    str
    address: int
    size:    int    = 0
    section: str   = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name':    self.name,
            'address': f'0x{self.address:08X}',
            'size':    self.size,
            'section': self.section,
        }


@dataclass
class SymbolMapResult:
    symbols: List[SymbolEntry] = field(default_factory=list)
    errors:  List[str]        = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'count':   len(self.symbols),
            'symbols': [s.to_dict() for s in self.symbols],
            'errors':  self.errors[:20],
        }


def parse_map_file(text: str) -> SymbolMapResult:
    """Parse a linker .map file (GCC / LLVM / ARM) to extract symbol addresses.

    Three common formats are handled:
    1. GCC ld .map  — '0x00000000deadbeef  symbol_name'
    2. LLVM lld     — 'section  0xADDR  size  file.o:(symbol)'
    3. IAR/Keil     — '   0x20000000   g   0x0004  symbol_name'
    """
    result = SymbolMapResult()
    seen: set = set()

    # Format 1: GNU ld — hex_addr  name
    _re_gnu = re.compile(
        r'^\s*(0x[0-9A-Fa-f]{4,16})\s+([\w$@.]+)\s*$', re.MULTILINE
    )
    # Format 2: IAR/Keil — spaces + addr + spaces + class + spaces + size + spaces + name
    _re_keil = re.compile(
        r'^\s+(0x[0-9A-Fa-f]+)\s+[GlgSs]\s+(0x[0-9A-Fa-f]+)\s+([\w$.]+)', re.MULTILINE
    )
    # Format 3: ARM .map section table — name  0xaddr  size
    _re_arm = re.compile(
        r'^([\w$.]+)\s+(0x[0-9A-Fa-f]+)\s+(\d+)', re.MULTILINE
    )

    def _add(name: str, address: int, size: int = 0, section: str = '') -> None:
        if name not in seen and address >= 0x1000:
            seen.add(name)
            result.symbols.append(SymbolEntry(name=name, address=address, size=size, section=section))

    # Try all patterns; GNU ld is tried last (most permissive) to avoid noise
    for m in _re_keil.finditer(text):
        _add(m.group(3), _safe_int(m.group(1)), _safe_int(m.group(2)))

    if not result.symbols:
        for m in _re_arm.finditer(text):
            name = m.group(1)
            if re.match(r'^[A-Za-z_]', name):
                _add(name, _safe_int(m.group(2)), int(m.group(3)))

    if not result.symbols:
        for m in _re_gnu.finditer(text):
            name = m.group(2)
            if re.match(r'^[A-Za-z_$]', name):
                _add(name, _safe_int(m.group(1)))

    return result


def parse_sym_file(text: str) -> SymbolMapResult:
    """Parse a PEAK/Vector .sym file (text symbol table format).

    Common format:
      {ENUMS}
      {SEND}
      [Label]  0xABCD  Extended/Standard  29bit  500,500
      {RECEIVE}
      ...

    For XCP we only care about the flat Symbol sections, formatted as:
      name  0xADDR  [attr ...]
    """
    result = SymbolMapResult()
    seen: set = set()

    _re_sym = re.compile(
        r'^([\w$.]+)\s+(0x[0-9A-Fa-f]+)(?:\s+(\d+))?', re.MULTILINE
    )
    for m in _re_sym.finditer(text):
        name = m.group(1)
        addr = _safe_int(m.group(2))
        size = int(m.group(3)) if m.group(3) else 0
        if addr >= 0x100 and name not in seen:
            seen.add(name)
            result.symbols.append(SymbolEntry(name=name, address=addr, size=size))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SKB file — Vector Seed & Key Binary (XCP/CCP Security Access)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkbParseResult:
    """Parsed metadata from a Vector Seed & Key Binary (.skb) file.

    The .skb file contains a proprietary bytecode algorithm used to compute
    the *key* from a *seed* for XCP (GET_SEED → UNLOCK) security access.
    Full computation requires the Vector ``SeedNKeyXcp.dll`` or a compatible
    implementation.  This parser extracts whatever metadata can be determined
    from the binary header and stores the raw bytes for later use.
    """
    file_size:        int        = 0
    header_signature: str        = ''    # first 2 bytes as hex
    security_levels:  List[int]  = field(default_factory=list)
    raw_bytes:        bytes      = b''
    is_valid:         bool       = False
    errors:           List[str]  = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'file_size':        self.file_size,
            'header_signature': self.header_signature,
            'security_levels':  self.security_levels,
            'is_valid':         self.is_valid,
            'errors':           self.errors,
        }


def parse_skb_file(raw_bytes: bytes) -> SkbParseResult:
    """Parse a Vector .skb (Seed & Key Binary) file.

    The .skb format is proprietary (Vector Informatik).  The file typically:
    • Starts with a 2-byte signature (common: 0x01 0x02)
    • Contains bytecode for computing key = f(seed, security_level)
    • Is small (< 10 KB) since it only holds the algorithm, not data

    This parser validates the file and stores it for use in the UNLOCK flow.
    The actual key computation requires either:
    1. The Vector ``SeedNKeyXcp.dll`` (Windows only)
    2. A Python re-implementation of the specific algorithm
    3. A custom seed-key callback configured per ECU

    Returns SkbParseResult with raw_bytes stored for later use.
    """
    result = SkbParseResult(file_size=len(raw_bytes))

    if len(raw_bytes) < 4:
        result.errors.append('File too small to be a valid .skb')
        return result

    # Store raw bytes for use during UNLOCK
    result.raw_bytes = raw_bytes

    # Header signature (common skb files start with 01 02)
    result.header_signature = f'0x{raw_bytes[0]:02X}{raw_bytes[1]:02X}'

    # Heuristic: try to identify supported security levels.
    # In many .skb variants, specific byte offsets indicate which XCP
    # resource protection levels are covered.
    # Common resources (ASAM XCP Part 2 §6.5.6):
    #   0x01 = CAL/PAG, 0x04 = DAQ, 0x08 = STIM, 0x10 = PGM
    # We don't try to execute the algorithm — just flag the file as loaded.
    possible_levels = set()
    for lvl in (0x01, 0x04, 0x08, 0x10):
        if lvl in raw_bytes:
            possible_levels.add(lvl)
    result.security_levels = sorted(possible_levels)
    result.is_valid = True

    return result


# ─────────────────────────────────────────────────────────────────────────────
# High-level helper: merge A2L + LAB → filtered signal list
# ─────────────────────────────────────────────────────────────────────────────

def filter_measurements_by_lab(
    a2l: A2lParseResult, lab: LabParseResult
) -> List[A2lMeasurement]:
    """Return only the A2L signals whose name appears in the LAB file."""
    names = set(lab.signals)
    return [s for s in a2l.all_signals() if s.name in names]


def build_daq_lists_from_selection(
    selected: List[Dict[str, Any]],
    events:   List[A2lEvent],
    max_dto:  int = 8,
) -> List[Dict[str, Any]]:
    """Auto-generate DAQ list configuration from a user selection.

    Each item in `selected`:
      {
        'name':          str,
        'address':       str (hex) | int,
        'dtype':         str,
        'byte_order':    str,
        'factor':        float,
        'offset':        float,
        'unit':          str,
        'event_channel': int,    # which ECU event to sample on
        'prescaler':     int,
      }

    Signals with the same event_channel are grouped into one DAQ list.
    Each ODT is limited to (max_dto - 1) bytes (PID byte occupies byte 0).

    Returns a list ready for POST /api/xcp/can/daq/setup.
    """
    from collections import defaultdict

    # Group by (event_channel, prescaler)
    groups: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for s in selected:
        ev  = int(s.get('event_channel', 0))
        pre = int(s.get('prescaler', 1))
        groups[(ev, pre)].append(s)

    _DTYPE_SIZE: Dict[str, int] = {
        'UBYTE': 1, 'SBYTE': 1,
        'UWORD': 2, 'SWORD': 2,
        'ULONG': 4, 'SLONG': 4,
        'A_UINT64': 8, 'A_INT64': 8,
        'FLOAT32_IEEE': 4, 'FLOAT64_IEEE': 8,
    }

    daq_lists: List[Dict[str, Any]] = []
    for (ev, pre), sigs in groups.items():
        # Split into ODTs respecting max_dto
        payload_budget = max_dto - 1  # subtract PID byte
        current_odt: List[Dict[str, Any]] = []
        current_sz  = 0

        for sig in sigs:
            sz = _DTYPE_SIZE.get(str(sig.get('dtype', 'FLOAT32_IEEE')).upper(), 4)
            if current_sz + sz > payload_budget and current_odt:
                # Start new DAQ list for this event (simplest approach)
                daq_lists.append({
                    'event_channel': ev,
                    'prescaler':     pre,
                    'mode':          0x10,  # timestamp on
                    'signals': [
                        {
                            'name':       s['name'],
                            'address':    s['address'],
                            'addr_ext':   s.get('addr_ext', 0),
                            'dtype':      s.get('dtype', 'FLOAT32_IEEE'),
                            'byte_order': s.get('byte_order', 'little'),
                            'factor':     s.get('factor', 1.0),
                            'offset':     s.get('offset', 0.0),
                            'unit':       s.get('unit', ''),
                        }
                        for s in current_odt
                    ],
                })
                current_odt = []
                current_sz  = 0
            current_odt.append(sig)
            current_sz += sz

        if current_odt:
            daq_lists.append({
                'event_channel': ev,
                'prescaler':     pre,
                'mode':          0x10,
                'signals': [
                    {
                        'name':       s['name'],
                        'address':    s['address'],
                        'addr_ext':   s.get('addr_ext', 0),
                        'dtype':      s.get('dtype', 'FLOAT32_IEEE'),
                        'byte_order': s.get('byte_order', 'little'),
                        'factor':     s.get('factor', 1.0),
                        'offset':     s.get('offset', 0.0),
                        'unit':       s.get('unit', ''),
                    }
                    for s in current_odt
                ],
            })

    return daq_lists
