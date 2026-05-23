"""XCP on CAN — Professional Vector-style Master Implementation.

Transport Layer:  ASAM XCP Part 5 — CAN Transport Layer
Protocol Layer:   ASAM XCP Part 2 — Protocol Layer Specification

Target use-case:  Gearbox ECU (Cambio) acquisition and calibration
                  via Kvaser hardware with Vector-compatible configuration.

Configuration (app_config.json key 'xcp_can'):
  can_channel       int    Kvaser channel index (0-based)              default: 0
  cmd_id            str    CAN ID for Master→Slave commands (hex/dec)  default: '0x7FF'
  res_id            str    CAN ID for Slave→Master responses (hex/dec) default: '0x7FE'
  is_extended_id    bool   29-bit CAN IDs                              default: false
  is_canfd          bool   CAN FD (max_cto / max_dto up to 64 bytes)   default: false
  byte_order        str    'little' | 'big'                            default: 'little'
  max_cto           int    Max Command Transfer Object size            default: 8
  max_dto           int    Max Data Transfer Object size               default: 8
  timeout_ms        int    Command timeout                             default: 200
  retry_count       int    Retries per command before giving up        default: 3

Vector XCP on CAN frame format (ASAM XCP Part 5, §4):
  • No transport-layer header — payload IS the XCP packet.
  • First byte  = PID (command or response PID).
  • Max payload = max_cto / max_dto bytes.
"""
from __future__ import annotations

import re
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# XCP Protocol Constants (ASAM XCP Part 2 §6.3)
# ─────────────────────────────────────────────────────────────────────────────

class XcpCmd(IntEnum):
    """XCP command PIDs (Master → Slave)."""
    CONNECT               = 0xFF
    DISCONNECT            = 0xFE
    GET_STATUS            = 0xFD
    SYNCH                 = 0xFC
    GET_COMM_MODE_INFO    = 0xFB
    GET_ID                = 0xFA
    SET_REQUEST           = 0xF9
    GET_SEED              = 0xF8
    UNLOCK                = 0xF7
    SET_MTA               = 0xF6
    UPLOAD                = 0xF5
    SHORT_UPLOAD          = 0xF4
    BUILD_CHECKSUM        = 0xF3
    TRANSPORT_LAYER_CMD   = 0xF2
    USER_CMD              = 0xF1
    DOWNLOAD              = 0xF0
    DOWNLOAD_NEXT         = 0xEF
    DOWNLOAD_MAX          = 0xEE
    SHORT_DOWNLOAD        = 0xED
    MODIFY_BITS           = 0xEC
    SET_CAL_PAGE          = 0xEB
    GET_CAL_PAGE          = 0xEA
    GET_PAG_PROCESSOR_INFO = 0xE9
    GET_SEGMENT_INFO      = 0xE8
    GET_PAGE_INFO         = 0xE7
    COPY_CAL_PAGE         = 0xE6
    CLEAR_DAQ_LIST        = 0xE3
    SET_DAQ_PTR           = 0xE2
    WRITE_DAQ             = 0xE1
    SET_DAQ_LIST_MODE     = 0xE0
    GET_DAQ_LIST_MODE     = 0xDF
    START_STOP_DAQ_LIST   = 0xDE
    START_STOP_SYNCH      = 0xDD
    GET_DAQ_CLOCK         = 0xDC
    READ_DAQ              = 0xDB
    GET_DAQ_PROCESSOR_INFO = 0xDA
    GET_DAQ_RESOLUTION_INFO = 0xD9
    GET_DAQ_LIST_INFO     = 0xD8
    GET_DAQ_EVENT_INFO    = 0xD7
    FREE_DAQ              = 0xD6
    ALLOC_DAQ             = 0xD5
    ALLOC_ODT             = 0xD4
    ALLOC_ODT_ENTRY       = 0xD3
    TIME_CORRELATION      = 0xC6


class XcpPid(IntEnum):
    """Special response/DAQ PIDs."""
    POSITIVE_RESPONSE = 0xFF
    NEGATIVE_RESPONSE = 0xFE
    # DAQ ODT PIDs start at 0x00 and go up to 0xFB.
    DAQ_MAX_PID       = 0xFB


class XcpErr(IntEnum):
    """XCP error codes (Negative response, byte 1)."""
    CMD_BUSY            = 0x10
    DAQ_ACTIVE          = 0x11
    PGM_ACTIVE          = 0x12
    CMD_UNKNOWN         = 0x20
    CMD_SYNTAX          = 0x21
    OUT_OF_RANGE        = 0x22
    WRITE_PROTECTED     = 0x23
    ACCESS_DENIED       = 0x24
    ACCESS_LOCKED       = 0x25
    PAGE_NOT_VALID      = 0x26
    PAGE_MODE_NOT_VALID = 0x27
    SEGMENT_NOT_VALID   = 0x28
    SEQUENCE            = 0x29
    DAQ_CONFIG          = 0x2A
    MEMORY_OVERFLOW     = 0x30
    GENERIC             = 0x31
    VERIFY              = 0x32

    @classmethod
    def name_of(cls, code: int) -> str:
        try:
            return cls(code).name
        except ValueError:
            return f'0x{int(code):02X}'


class XcpDType(IntEnum):
    """XCP / A2L data types."""
    UBYTE        = 1   # uint8
    SBYTE        = 2   # int8
    UWORD        = 3   # uint16
    SWORD        = 4   # int16
    ULONG        = 5   # uint32
    SLONG        = 6   # int32
    A_UINT64     = 7   # uint64
    A_INT64      = 8   # int64
    FLOAT32_IEEE = 9   # float32
    FLOAT64_IEEE = 10  # float64

    @classmethod
    def from_name(cls, name: str) -> 'XcpDType':
        n = str(name or '').upper().strip().replace(' ', '_').replace('-', '_')
        # Accept common A2L aliases
        _alias: Dict[str, str] = {
            'UINT8':   'UBYTE',  'INT8':    'SBYTE',
            'UINT16':  'UWORD',  'INT16':   'SWORD',
            'UINT32':  'ULONG',  'INT32':   'SLONG',
            'UINT64':  'A_UINT64', 'INT64': 'A_INT64',
            'FLOAT':   'FLOAT32_IEEE',
            'FLOAT32': 'FLOAT32_IEEE',
            'FLOAT64': 'FLOAT64_IEEE',
            'DOUBLE':  'FLOAT64_IEEE',
        }
        n = _alias.get(n, n)
        try:
            return cls[n]
        except KeyError:
            return cls.FLOAT32_IEEE


# dtype → (struct_format_char, byte_size)
_DTYPE_FMT: Dict[XcpDType, Tuple[str, int]] = {
    XcpDType.UBYTE:        ('B', 1),
    XcpDType.SBYTE:        ('b', 1),
    XcpDType.UWORD:        ('H', 2),
    XcpDType.SWORD:        ('h', 2),
    XcpDType.ULONG:        ('I', 4),
    XcpDType.SLONG:        ('i', 4),
    XcpDType.A_UINT64:     ('Q', 8),
    XcpDType.A_INT64:      ('q', 8),
    XcpDType.FLOAT32_IEEE: ('f', 4),
    XcpDType.FLOAT64_IEEE: ('d', 8),
}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class XcpSignal:
    """A single ECU measurement or calibration variable."""
    name:         str
    address:      int
    address_ext:  int            = 0
    dtype:        XcpDType       = XcpDType.FLOAT32_IEEE
    byte_order:   str            = 'little'   # 'little' | 'big'
    unit:         str            = ''
    factor:       float          = 1.0
    offset:       float          = 0.0
    min_value:    Optional[float] = None
    max_value:    Optional[float] = None
    comment:      str            = ''
    # Runtime state
    last_value:   Optional[float] = None
    last_ts_ms:   int            = 0
    # DAQ position: (daq_list_idx, odt_idx, odt_entry_idx), None if polled
    daq_ptr:      Optional[Tuple[int, int, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name':        self.name,
            'address':     f'0x{self.address:08X}',
            'addr_ext':    self.address_ext,
            'dtype':       self.dtype.name,
            'byte_order':  self.byte_order,
            'unit':        self.unit,
            'factor':      self.factor,
            'offset':      self.offset,
            'min':         self.min_value,
            'max':         self.max_value,
            'comment':     self.comment,
            'last_value':  self.last_value,
            'last_ts_ms':  self.last_ts_ms,
        }


@dataclass
class XcpDaqList:
    """One XCP DAQ list with a single ODT (Vector-default for calibration+DAQ)."""
    idx:           int
    signals:       List[XcpSignal] = field(default_factory=list)
    event_channel: int             = 0
    prescaler:     int             = 1
    priority:      int             = 0
    mode:          int             = 0x10   # 0x10 = TIMESTAMP enabled
    running:       bool            = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'idx':           self.idx,
            'event_channel': self.event_channel,
            'prescaler':     self.prescaler,
            'priority':      self.priority,
            'mode':          f'0x{self.mode:02X}',
            'running':       self.running,
            'signals':       [s.name for s in self.signals],
        }


@dataclass
class XcpSessionInfo:
    """Negotiated XCP session parameters (from CONNECT response)."""
    slave_block_mode:  bool  = False
    interleaved_mode:  bool  = False
    address_granularity: int = 1
    max_cto:           int   = 8
    max_dto:           int   = 8
    byte_order:        str   = 'little'
    protocol_layer_ver: int  = 1
    transport_layer_ver: int = 1
    # Runtime
    status:            int   = 0
    session_cfg_id:    int   = 0
    daq_count:         int   = 0
    event_count:       int   = 0
    min_daq:           int   = 0
    max_daq:           int   = 0
    ecu_id:            str   = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'max_cto':            self.max_cto,
            'max_dto':            self.max_dto,
            'byte_order':         self.byte_order,
            'addr_granularity':   self.address_granularity,
            'slave_block_mode':   self.slave_block_mode,
            'interleaved_mode':   self.interleaved_mode,
            'protocol_layer_ver': self.protocol_layer_ver,
            'transport_layer_ver': self.transport_layer_ver,
            'status':             self.status,
            'ecu_id':             self.ecu_id,
            'daq_count':          self.daq_count,
            'min_daq':            self.min_daq,
            'max_daq':            self.max_daq,
        }


def _parse_int_config(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    try:
        return int(str(value).strip(), 0)
    except Exception:
        return int(default)


def normalize_xcp_can_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a sanitized XCP-on-CAN config safe for runtime use and storage."""
    cfg = dict(default_xcp_can_config())
    if isinstance(config, dict):
        cfg.update(config)

    is_canfd = bool(cfg.get('is_canfd', False))
    frame_limit = 64 if is_canfd else 8
    cmd_id = _parse_int_config(cfg.get('cmd_id', '0x7FF'), 0x7FF)
    res_id = _parse_int_config(cfg.get('res_id', '0x7FE'), 0x7FE)

    cfg['can_channel'] = max(0, _parse_int_config(cfg.get('can_channel', 0), 0))
    cfg['baudrate'] = max(1000, _parse_int_config(cfg.get('baudrate', 1000000), 1000000))
    cfg['cmd_id'] = f'0x{cmd_id:X}'
    cfg['res_id'] = f'0x{res_id:X}'
    cfg['is_extended_id'] = bool((cmd_id > 0x7FF) or (res_id > 0x7FF))
    cfg['is_canfd'] = is_canfd
    cfg['byte_order'] = 'big' if str(cfg.get('byte_order', 'little')).lower().strip() in ('big', 'msb_first', 'motorola') else 'little'
    cfg['max_cto'] = max(2, min(_parse_int_config(cfg.get('max_cto', frame_limit), frame_limit), frame_limit))
    cfg['max_dto'] = max(2, min(_parse_int_config(cfg.get('max_dto', frame_limit), frame_limit), frame_limit))
    cfg['timeout_ms'] = max(50, _parse_int_config(cfg.get('timeout_ms', 200), 200))
    cfg['retry_count'] = max(1, _parse_int_config(cfg.get('retry_count', 3), 3))
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Vector SKB bytecode interpreter (Seed & Key Binary)
# ─────────────────────────────────────────────────────────────────────────────

def compute_key_from_skb(seed: bytes, security_level: int, skb_raw: bytes) -> bytes:
    """Compute the XCP UNLOCK key from a seed using a Vector .skb file.

    The .skb file contains a bytecode program that transforms seed → key.
    This implements the Vector SeedNKey bytecode VM v1.x format.

    Args:
        seed:            Seed bytes received from GET_SEED.
        security_level:  XCP resource being unlocked (0x01=CAL, 0x04=DAQ, etc.)
        skb_raw:         Raw .skb file contents.

    Returns:
        Computed key bytes.

    Raises:
        ValueError: If the SKB file cannot be interpreted.
    """
    if len(skb_raw) < 4:
        raise ValueError('SKB file too small')

    # Header: [version_lo, version_hi, protocol, variant_or_count]
    ver_lo = skb_raw[0]
    ver_hi = skb_raw[1]
    proto  = skb_raw[2]

    if proto != 0xFE:
        raise ValueError(f'Not an XCP SKB file (protocol byte 0x{proto:02X}, expected 0xFE)')

    # The SKB bytecode operates on a register-based VM:
    # - 16 general-purpose 32-bit registers (R0–R15)
    # - Seed bytes accessible by index
    # - Key bytes built by storing results
    regs = [0] * 16          # R0-R15, 32-bit unsigned
    key_bytes: List[int] = []
    seed_list = list(seed)

    # Byte 3: number of key-length entries (one per security level slot)
    n_levels = skb_raw[3]
    if 4 + n_levels > len(skb_raw):
        raise ValueError('SKB header truncated')

    # Key lengths per security level slot (indexed 0..n_levels-1)
    key_lengths = list(skb_raw[4: 4 + n_levels])

    # Find the bytecode start: after the key-length table
    code_start = 4 + n_levels

    # Security level → slot mapping.
    # Standard levels: 0x01=slot0, 0x02=slot1, 0x04=slot2, 0x08=slot3, 0x10=slot4.
    # Extended levels use higher slot indices.
    level_map = {0x01: 0, 0x02: 1, 0x04: 2, 0x08: 3, 0x10: 4,
                 0x20: 5, 0x40: 6, 0x80: 7}
    slot = level_map.get(security_level, 0)

    # Determine expected key length for this slot
    expected_key_len = key_lengths[slot] if slot < len(key_lengths) else 6

    # Execute the bytecode VM
    code = skb_raw[code_start:]
    pc = 0
    max_iterations = 50000  # safety limit

    def _u32(val: int) -> int:
        return val & 0xFFFFFFFF

    def _get_seed_byte(idx: int) -> int:
        return seed_list[idx] if idx < len(seed_list) else 0

    for _ in range(max_iterations):
        if pc >= len(code):
            break

        op = code[pc]
        pc += 1

        # 0x01: NOP
        if op == 0x01:
            pass

        # 0x02: RETURN / end of level
        elif op == 0x02:
            break

        # 0x03: DUP last register operation (typically R0 = R0)
        elif op == 0x03:
            pass  # often used as separator, no-op in this context

        # 0x04: LOAD reg <- immediate byte
        elif op == 0x04:
            if pc < len(code):
                regs[0] = code[pc]
                pc += 1

        # 0x05: PUSH 32-bit immediate → R0
        elif op == 0x05:
            if pc + 3 < len(code):
                val = struct.unpack_from('>I', code, pc)[0]
                regs[0] = _u32(val)
                pc += 4

        # 0x06: MOV Rn ← R0 (next byte = register index)
        elif op == 0x06:
            if pc < len(code):
                ri = code[pc] & 0x0F
                pc += 1
                regs[ri] = regs[0]

        # 0x07: MOV R0 ← Rn (next byte = register index)
        elif op == 0x07:
            if pc < len(code):
                ri = code[pc] & 0x0F
                pc += 1
                regs[0] = regs[ri]

        # 0x08: XOR R0 ^= Rn
        elif op == 0x08:
            regs[0] = _u32(regs[0] ^ regs[1])

        # 0x09: OR R0 |= R1
        elif op == 0x09:
            regs[0] = _u32(regs[0] | regs[1])

        # 0x0A: AND R0 &= R1
        elif op == 0x0A:
            regs[0] = _u32(regs[0] & regs[1])

        # 0x0B: NOT R0 = ~R0
        elif op == 0x0B:
            regs[0] = _u32(~regs[0])

        # 0x0C: SHL R0 <<= R1
        elif op == 0x0C:
            shift = regs[1] & 31
            regs[0] = _u32(regs[0] << shift)

        # 0x0D: SHR R0 >>= R1 (logical)
        elif op == 0x0D:
            shift = regs[1] & 31
            regs[0] = _u32(regs[0] >> shift)

        # 0x0E: ROR R0 = rotate_right(R0, R1)
        elif op == 0x0E:
            shift = regs[1] & 31
            if shift:
                regs[0] = _u32((regs[0] >> shift) | (regs[0] << (32 - shift)))

        # 0x0F: ROL R0 = rotate_left(R0, R1)
        elif op == 0x0F:
            shift = regs[1] & 31
            if shift:
                regs[0] = _u32((regs[0] << shift) | (regs[0] >> (32 - shift)))

        # 0x10: ADD R0 += R1
        elif op == 0x10:
            regs[0] = _u32(regs[0] + regs[1])

        # 0x11: SUB R0 -= R1
        elif op == 0x11:
            regs[0] = _u32(regs[0] - regs[1])

        # 0x12: MUL R0 *= R1
        elif op == 0x12:
            regs[0] = _u32(regs[0] * regs[1])

        # 0x13: MUL by immediate (2 bytes, big-endian)
        elif op == 0x13:
            if pc + 1 < len(code):
                imm = (code[pc] << 8) | code[pc + 1]
                pc += 2
                regs[0] = _u32(regs[0] * imm)

        # 0x1A: SWAP bytes in R0 (endian swap)
        elif op == 0x1A:
            v = regs[0]
            regs[0] = _u32(((v >> 24) & 0xFF) | ((v >> 8) & 0xFF00) |
                           ((v << 8) & 0xFF0000) | ((v << 24) & 0xFF000000))

        # 0x1B: Conditional branch: if R0 != 0 → skip next N bytes
        elif op == 0x1B:
            if pc < len(code):
                offset = code[pc]
                pc += 1
                if regs[0] != 0:
                    pc += offset

        # 0x1C: AND R0 &= Rn (next byte = register index)
        elif op == 0x1C:
            if pc < len(code):
                ri = code[pc] & 0x0F
                pc += 1
                regs[0] = _u32(regs[0] & regs[ri])
            else:
                # Standalone 0x1C = AND R0 &= R1
                regs[0] = _u32(regs[0] & regs[1])

        # 0x1D: OR R0 |= Rn
        elif op == 0x1D:
            if pc < len(code):
                ri = code[pc] & 0x0F
                pc += 1
                regs[0] = _u32(regs[0] | regs[ri])

        # 0x1E: XOR R0 ^= Rn
        elif op == 0x1E:
            if pc < len(code):
                ri = code[pc] & 0x0F
                pc += 1
                regs[0] = _u32(regs[0] ^ regs[ri])

        # 0x1F: Store key byte from R0 (low byte)
        elif op == 0x1F:
            if pc < len(code):
                key_idx = code[pc]
                pc += 1
                # Extend key_bytes as needed
                while len(key_bytes) <= key_idx:
                    key_bytes.append(0)
                key_bytes[key_idx] = regs[0] & 0xFF

        # 0x20: LOAD immediate byte → R0
        elif op == 0x20:
            if pc < len(code):
                regs[0] = code[pc]
                pc += 1

        # 0x21: END program
        elif op == 0x21:
            break

        # 0x80-0xBF: Load seed byte (index = op & 0x3F) → R0
        elif 0x80 <= op <= 0xBF:
            idx = op & 0x3F
            regs[0] = _get_seed_byte(idx)

        # 0xFD: marker/separator (skip 1 byte)
        elif op == 0xFD:
            if pc < len(code):
                pc += 1

        # 0xFE / 0xFF: section markers
        elif op in (0xFE, 0xFF):
            pass

        else:
            # Unknown opcode — skip
            pass

    # Build key from collected bytes, padded/truncated to expected length
    while len(key_bytes) < expected_key_len:
        key_bytes.append(0)
    return bytes(key_bytes[:expected_key_len])


# ─────────────────────────────────────────────────────────────────────────────
# XCPCanClient
# ─────────────────────────────────────────────────────────────────────────────

class XcpCanClient:
    """Professional XCP-on-CAN Master.

    Implements ASAM XCP Part 2 (protocol) + Part 5 (CAN transport).

    Communication path:
      BusManager.send_message()  →  Kvaser hardware  →  Gearbox ECU
      Kvaser hardware            →  BusManager listener  →  self._on_frame()

    Thread model:
      • Caller thread: executes commands synchronously (blocks on response).
      • BusManager listener thread: calls _on_frame() for every received frame.
      • Poll thread (optional): background SHORT_UPLOAD polling when DAQ unavailable.
    """

    # Maximum measurement history per signal (ring buffer)
    _MEAS_MAX = 10000

    def __init__(
        self,
        *,
        bus_manager: Any,
        config: Dict[str, Any],
        socketio: Any = None,
        mf4_logger_cb: Optional[Callable[[float, str, float, str], None]] = None,
    ) -> None:
        """
        Args:
            bus_manager:    BusManager instance.
            config:         Dict from app_config.json['xcp_can'].
            socketio:       Flask-SocketIO instance for live UI events.
            mf4_logger_cb:  Callback(timestamp_s, name, value, unit) for MF4 logging.
        """
        self._bm          = bus_manager
        self._socketio    = socketio
        self._mf4_cb      = mf4_logger_cb

        # ── Configuration ──────────────────────────────────────────────
        cfg = normalize_xcp_can_config(config)

        self._channel:      int  = int(cfg.get('can_channel', 0))
        self._baudrate:     int  = int(cfg.get('baudrate', 1000000))
        self._cmd_id:       int  = _parse_int_config(cfg.get('cmd_id',  '0x7FF'), 0x7FF)
        self._res_id:       int  = _parse_int_config(cfg.get('res_id',  '0x7FE'), 0x7FE)
        self._is_extended:  bool = bool(cfg.get('is_extended_id', False))
        self._is_canfd:     bool = bool(cfg.get('is_canfd', False))
        self._byte_order:   str  = str(cfg.get('byte_order', 'little')).lower().strip()
        _max_frame          = 64 if self._is_canfd else 8
        self._max_cto:      int  = min(int(cfg.get('max_cto', _max_frame)), _max_frame)
        self._max_dto:      int  = min(int(cfg.get('max_dto', _max_frame)), _max_frame)
        self._timeout_ms:   int  = max(50, int(cfg.get('timeout_ms', 200)))
        self._retry_count:  int  = max(1,  int(cfg.get('retry_count', 3)))

        # ── Synchronisation ────────────────────────────────────────────
        self._cmd_lock   = threading.Lock()   # serialise commands (one at a time)
        self._resp_event = threading.Event()   # signalled when response arrives
        self._state_lock = threading.Lock()    # protect _session, _signals, _daq_lists
        self._meas_lock  = threading.Lock()    # protect _measurements

        # Response mailbox (written by listener thread, read by command thread)
        self._last_response: Optional[bytes] = None

        # ── State ──────────────────────────────────────────────────────
        self._connected:  bool                        = False
        self._session:    Optional[XcpSessionInfo]    = None
        self._signals:    Dict[str, XcpSignal]        = {}
        self._daq_lists:  List[XcpDaqList]            = []
        self._daq_running: bool                       = False
        self._last_error: Optional[str]               = None

        # Background poll thread
        self._poll_thread:  Optional[threading.Thread] = None
        self._stop_poll:    threading.Event            = threading.Event()

        # Measurement ring buffers
        self._measurements: Dict[str, List[Dict[str, Any]]] = {}

        # Statistics
        self._stats: Dict[str, int] = {
            'cmd_sent':    0,
            'cmd_ok':      0,
            'cmd_err':     0,
            'cmd_timeout': 0,
            'daq_frames':  0,
            'meas_points': 0,
        }

        # Register as BusManager listener
        self._bm.add_listener(self._on_frame)

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────

    def _ensure_channel_open(self) -> bool:
        """Make sure the physical CAN channel is open in the BusManager.

        When the application runs in mirror-only mode (AUTOSAR Bus Mirroring
        via Ethernet) the physical Kvaser channels are never opened by the
        normal start_bus() flow.  XCP needs a *physical* channel to send
        commands, so we open it on-demand here.

        Returns True if the channel handler is (now) available.
        """
        with self._bm.lock:
            if self._channel in self._bm.handlers:
                return True

        # Channel not open — try to open it with the configured XCP baudrate.
        try:
            from can_handler import CANHandler

            # Map baudrate integer to Kvaser constant.
            _BR_MAP = {
                1000000: -1,   # canBITRATE_1M
                500000:  -2,   # canBITRATE_500K
                250000:  -3,   # canBITRATE_250K
                125000:  -4,   # canBITRATE_125K
                100000:  -5,   # canBITRATE_100K
                62000:   -6,   # canBITRATE_62K
                50000:   -7,   # canBITRATE_50K
                83000:   -8,   # canBITRATE_83K
                10000:   -9,   # canBITRATE_10K
            }
            br_raw = getattr(self, '_baudrate', 1000000)
            kvaser_br = _BR_MAP.get(int(br_raw), int(br_raw))

            handler = CANHandler(self._channel, kvaser_br)
            if handler.open():
                with self._bm.lock:
                    self._bm.handlers[self._channel] = handler
                    self._bm.bitrate_by_channel[self._channel] = kvaser_br
                # Ensure the reader thread is running so we can receive
                # responses on this channel.
                if not self._bm.running:
                    self._bm.running = True
                    if self._bm.thread is None or not self._bm.thread.is_alive():
                        self._bm.thread = threading.Thread(
                            target=self._bm._bus_loop, daemon=True
                        )
                        self._bm.thread.start()
                print(f'[XCP] Auto-opened physical CAN channel {self._channel} '
                      f'at {br_raw} baud for XCP communication.')
                return True
            else:
                print(f'[XCP] Failed to open CAN channel {self._channel}.')
                return False
        except Exception as exc:
            print(f'[XCP] Error opening CAN channel {self._channel}: {exc}')
            return False

    def _endian(self, byte_order: Optional[str] = None) -> str:
        """Return struct endian prefix '<' (little) or '>' (big)."""
        bo = str(byte_order or self._byte_order).lower().strip()
        return '>' if bo in ('big', 'msb_first', 'motorola') else '<'

    def _pad_cmd(self, payload: bytes) -> List[int]:
        """Pad payload to max_cto bytes with 0x00."""
        raw = list(payload[:self._max_cto])
        while len(raw) < min(8, self._max_cto):
            raw.append(0x00)
        return raw

    def _stat_inc(self, key: str) -> None:
        self._stats[key] = self._stats.get(key, 0) + 1

    # ─────────────────────────────────────────────────────────────────
    # BusManager listener (called from reader thread)
    # ─────────────────────────────────────────────────────────────────

    def _on_frame(self, frame: Dict[str, Any]) -> None:
        """Receive CAN frame from BusManager and dispatch to command/DAQ handler."""
        try:
            fid = int(frame.get('id', -1))
            if fid != self._res_id:
                return
            data = frame.get('data')
            if not data or len(data) < 1:
                return
            payload = bytes(data[:min(self._max_dto, len(data))])
            pid = payload[0]

            # ── Positive or Negative command response ──
            if pid in (XcpPid.POSITIVE_RESPONSE, XcpPid.NEGATIVE_RESPONSE):
                self._last_response = payload
                self._resp_event.set()
                return

            # ── DAQ packet (ODT PID 0x00–0xFB) ──
            if 0x00 <= pid <= int(XcpPid.DAQ_MAX_PID):
                ts_ms = int(frame.get('timestamp') or (time.time() * 1000))
                self._handle_daq_packet(int(pid), payload, ts_ms)

        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # DAQ packet decoder
    # ─────────────────────────────────────────────────────────────────

    def _handle_daq_packet(self, odt_pid: int, payload: bytes, ts_ms: int) -> None:
        """Decode an incoming DAQ frame.

        ODT PIDs are allocated starting from 0, one per DAQ list.
        Layout: [PID][data_byte_0][data_byte_1]…
        Signals are packed consecutively from byte 1 (PID_OFF not set).
        """
        try:
            with self._state_lock:
                if odt_pid >= len(self._daq_lists):
                    return
                daq_list = self._daq_lists[odt_pid]
                signals  = list(daq_list.signals)

            self._stat_inc('daq_frames')

            byte_off = 1  # byte 0 = PID
            for sig in signals:
                fmt_c, size = _DTYPE_FMT.get(sig.dtype, ('f', 4))
                if byte_off + size > len(payload):
                    break
                raw_bytes = payload[byte_off: byte_off + size]
                byte_off += size

                endian = self._endian(sig.byte_order)
                (raw_val,) = struct.unpack(endian + fmt_c, raw_bytes)
                phys = float(raw_val) * sig.factor + sig.offset

                sig.last_value  = phys
                sig.last_ts_ms  = ts_ms if ts_ms > 0 else int(time.time() * 1000)

                # Ring buffer
                with self._meas_lock:
                    buf = self._measurements.setdefault(sig.name, [])
                    buf.append({'ts_ms': sig.last_ts_ms, 'value': phys, 'unit': sig.unit})
                    if len(buf) > self._MEAS_MAX:
                        del buf[: self._MEAS_MAX // 10]

                self._stat_inc('meas_points')

                # Live SocketIO event
                try:
                    if self._socketio:
                        self._socketio.emit('xcp_daq', {
                            'signal':  sig.name,
                            'value':   phys,
                            'unit':    sig.unit,
                            'ts_ms':   sig.last_ts_ms,
                        })
                except Exception:
                    pass

                # MF4 log hook
                try:
                    if callable(self._mf4_cb):
                        self._mf4_cb(sig.last_ts_ms / 1000.0, sig.name, phys, sig.unit)
                except Exception:
                    pass

        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────
    # Command / Response engine
    # ─────────────────────────────────────────────────────────────────

    def _send_cmd(self, payload: bytes) -> Tuple[bool, Optional[bytes]]:
        """Send one XCP command and wait for the response.

        Thread-safe: serialised by _cmd_lock so only one command is in-flight
        at any time (XCP protocol requirement).

        Returns:
            (True,  response_bytes)   on positive response (0xFF).
            (False, error_bytes)      on negative response (0xFE).
            (False, None)             on timeout / CAN send failure.
        """
        if len(payload) < 1:
            return False, None

        pid = payload[0]
        raw = self._pad_cmd(payload)

        with self._cmd_lock:
            for attempt in range(max(1, self._retry_count)):
                self._resp_event.clear()
                self._last_response = None
                self._stat_inc('cmd_sent')

                ok = self._bm.send_message(
                    self._channel,
                    self._cmd_id,
                    raw,
                    is_extended=self._is_extended,
                )
                if not ok:
                    self._last_error = f'CAN send failed for CMD 0x{pid:02X}'
                    continue

                fired = self._resp_event.wait(timeout=self._timeout_ms / 1000.0)
                resp  = self._last_response

                if not fired or resp is None:
                    self._stat_inc('cmd_timeout')
                    self._last_error = f'Timeout CMD 0x{pid:02X} (attempt {attempt + 1}/{self._retry_count})'
                    continue

                if resp[0] == XcpPid.POSITIVE_RESPONSE:
                    self._stat_inc('cmd_ok')
                    return True, resp

                # Negative response
                err_code = resp[1] if len(resp) > 1 else 0xFF
                err_name = XcpErr.name_of(err_code)
                self._last_error = f'ERR_{err_name} (0x{err_code:02X}) for CMD 0x{pid:02X}'
                self._stat_inc('cmd_err')
                return False, resp

            return False, None

    # ─────────────────────────────────────────────────────────────────
    # Connection management
    # ─────────────────────────────────────────────────────────────────

    def connect(self, mode: int = 0x00) -> Dict[str, Any]:
        """CONNECT (0xFF).

        mode:
          0x00  Normal
          0x01  User-defined (Vector CANape extension)
        """
        if self._connected:
            return {'ok': True, 'message': 'already_connected', 'session': self._session_dict()}

        # Ensure the physical CAN channel is open in the BusManager.
        if not self._ensure_channel_open():
            return {'ok': False, 'error': f'Cannot open physical CAN channel {self._channel}'}

        cmd = bytes([int(XcpCmd.CONNECT), mode & 0xFF])
        ok, resp = self._send_cmd(cmd)
        if not ok:
            return {'ok': False, 'error': self._last_error}

        # CONNECT positive response layout (ASAM XCP Part 2 §6.3.1):
        # Byte 0: 0xFF
        # Byte 1: COMM_MODE_BASIC  (bits: bg=b0 interleaved=b5 block=b6 byte_order=b0)
        # Byte 2: MAX_CTO
        # Byte 3–4: MAX_DTO (little-endian)
        # Byte 5: PROTO_LAYER_VER
        # Byte 6: TRANSPORT_LAYER_VER
        try:
            comm = resp[1] if len(resp) > 1 else 0x00
            max_cto = resp[2] if len(resp) > 2 else self._max_cto
            max_dto = (
                struct.unpack_from('<H', resp, 3)[0] if len(resp) >= 5
                else self._max_dto
            )
            proto_ver  = resp[5] if len(resp) > 5 else 1
            transp_ver = resp[6] if len(resp) > 6 else 1

            slave_bo    = 'big' if (comm & 0x01) else 'little'
            slave_block = bool(comm & 0x40)
            interleaved = bool(comm & 0x20)

            self._session = XcpSessionInfo(
                slave_block_mode    = slave_block,
                interleaved_mode    = interleaved,
                address_granularity = 1,
                max_cto             = max(int(max_cto), 2),
                max_dto             = max(int(max_dto), 2),
                byte_order          = slave_bo,
                protocol_layer_ver  = int(proto_ver),
                transport_layer_ver = int(transp_ver),
            )
            # Adopt slave byte order
            self._byte_order = slave_bo
        except Exception:
            self._session = XcpSessionInfo()

        self._connected = True

        # Try to read ECU identification
        try:
            self.get_id(0x01)
        except Exception:
            pass

        self._emit('xcp_status', {'connected': True, 'session': self._session_dict()})
        return {'ok': True, 'session': self._session_dict()}

    def disconnect(self) -> Dict[str, Any]:
        """DISCONNECT (0xFE)."""
        if not self._connected:
            return {'ok': True, 'message': 'not_connected'}

        if self._daq_running:
            self.stop_daq()
        self.stop_polling()

        self._send_cmd(bytes([int(XcpCmd.DISCONNECT)]))
        self._connected = False
        self._session   = None

        self._emit('xcp_status', {'connected': False})
        return {'ok': True}

    # ─────────────────────────────────────────────────────────────────
    # Status & Info
    # ─────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """GET_STATUS (0xFD) — read current session state from slave."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        ok, resp = self._send_cmd(bytes([int(XcpCmd.GET_STATUS)]))
        if not ok:
            return {'ok': False, 'error': self._last_error}

        # Byte 1: SESSION_STATUS_BYTE
        # Byte 2: COMM_MODE_PROTECTION
        # Byte 4–5: SESSION_CONFIGURATION_ID
        try:
            sb   = resp[1] if len(resp) > 1 else 0
            prot = resp[2] if len(resp) > 2 else 0
            cfg  = struct.unpack_from('<H', resp, 4)[0] if len(resp) >= 6 else 0
            if self._session:
                self._session.status         = sb
                self._session.session_cfg_id = cfg
            return {
                'ok':             True,
                'status_byte':    sb,
                'daq_running':    bool(sb & 0x04),
                'cal_pag_active': bool(sb & 0x01),
                'pgm_running':    bool(sb & 0x10),
                'resume':         bool(sb & 0x40),
                'protection':     prot,
                'session_cfg_id': cfg,
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_comm_mode_info(self) -> Dict[str, Any]:
        """GET_COMM_MODE_INFO (0xFB)."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        ok, resp = self._send_cmd(bytes([int(XcpCmd.GET_COMM_MODE_INFO)]))
        if not ok:
            return {'ok': False, 'error': self._last_error}

        try:
            opt      = resp[2] if len(resp) > 2 else 0
            max_bs   = resp[3] if len(resp) > 3 else 0
            min_st   = resp[4] if len(resp) > 4 else 0
            queue_sz = resp[5] if len(resp) > 5 else 0
            drv_ver  = resp[6] if len(resp) > 6 else 0
            return {
                'ok':                True,
                'max_block_size':    int(max_bs),
                'min_sep_time_100us': int(min_st),
                'queue_size':        int(queue_sz),
                'drv_version':       int(drv_ver),
                'interleaved_mode':  bool(opt & 0x02),
                'block_mode':        bool(opt & 0x40),
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_id(self, req_type: int = 0x01) -> Dict[str, Any]:
        """GET_ID (0xFA): request ECU identification string.

        req_type:
          0x01  ASCII component identifier
          0x02  ASAM-MC2 filename
          0x04  WWW URL
        """
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        ok, resp = self._send_cmd(bytes([int(XcpCmd.GET_ID), req_type & 0xFF]))
        if not ok:
            return {'ok': False, 'error': self._last_error}

        try:
            req_mode = resp[1] if len(resp) > 1 else 0
            length   = struct.unpack_from('<I', resp, 4)[0] if len(resp) >= 8 else 0

            ecu_id = ''
            if length > 0:
                if not (req_mode & 0x01):
                    # Immediate delivery — data in bytes 8+
                    ecu_id = resp[8: 8 + length].decode('ascii', errors='replace').rstrip('\x00 ')
                else:
                    # Upload required
                    ul_ok, ul_raw = self._upload_raw(length)
                    if ul_ok and ul_raw:
                        ecu_id = ul_raw.decode('ascii', errors='replace').rstrip('\x00 ')

            if self._session and ecu_id:
                self._session.ecu_id = ecu_id

            return {'ok': True, 'ecu_id': ecu_id, 'length': int(length)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_daq_processor_info(self) -> Dict[str, Any]:
        """GET_DAQ_PROCESSOR_INFO (0xDA) — query DAQ capabilities."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        ok, resp = self._send_cmd(bytes([int(XcpCmd.GET_DAQ_PROCESSOR_INFO)]))
        if not ok:
            return {'ok': False, 'error': self._last_error}

        if len(resp) < 8:
            return {'ok': False, 'error': 'response too short'}

        try:
            props     = resp[1]
            max_daq   = struct.unpack_from('<H', resp, 2)[0]
            max_event = struct.unpack_from('<H', resp, 4)[0]
            min_daq   = resp[6]
            key_byte  = resp[7]

            if self._session:
                self._session.max_daq    = int(max_daq)
                self._session.min_daq    = int(min_daq)
                self._session.daq_count  = int(max_daq)
                self._session.event_count = int(max_event)

            return {
                'ok':                   True,
                'properties':           int(props),
                'max_daq':              int(max_daq),
                'max_event':            int(max_event),
                'min_daq':              int(min_daq),
                'dynamic_daq':          bool(props & 0x04),
                'prescaler_supported':  bool(props & 0x08),
                'resume_supported':     bool(props & 0x10),
                'pid_off_supported':    bool(props & 0x20),
                'timestamp_supported':  bool(props & 0x40),
                'bit_stim_supported':   bool(props & 0x80),
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_daq_event_info(self, event_channel: int) -> Dict[str, Any]:
        """GET_DAQ_EVENT_INFO (0xD7)."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        cmd = struct.pack('<BxH', int(XcpCmd.GET_DAQ_EVENT_INFO), event_channel & 0xFFFF)
        ok, resp = self._send_cmd(cmd)
        if not ok:
            return {'ok': False, 'error': self._last_error}

        if len(resp) < 7:
            return {'ok': False, 'error': 'response too short'}

        try:
            props       = resp[1]
            max_daq_list = resp[2]
            name_len    = resp[3]
            cycle       = resp[4]
            unit        = resp[5]
            prio        = resp[6]
            return {
                'ok':              True,
                'properties':      int(props),
                'max_daq_list':    int(max_daq_list),
                'name_length':     int(name_len),
                'cycle':           int(cycle),
                'time_unit':       int(unit),
                'priority':        int(prio),
            }
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ─────────────────────────────────────────────────────────────────
    # Security access (GET_SEED / UNLOCK)
    # ─────────────────────────────────────────────────────────────────

    def get_seed(self, resource: int = 0x01) -> Dict[str, Any]:
        """GET_SEED (0xF8) — request seed for a given resource.

        resource bitmask (ASAM XCP Part 2 §6.5.6):
          0x01  CAL/PAG
          0x04  DAQ
          0x08  STIM
          0x10  PGM

        Returns:
          {'ok': True, 'seed': bytes, 'seed_length': int, 'resource': int}
        """
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        # Mode 0 = first part (and only part for seeds ≤ max_cto-2)
        cmd = bytes([int(XcpCmd.GET_SEED), 0x00, resource & 0xFF])
        ok, resp = self._send_cmd(cmd)
        if not ok:
            return {'ok': False, 'error': self._last_error}

        try:
            seed_length = resp[1] if len(resp) > 1 else 0
            if seed_length == 0:
                # Resource is already unlocked
                return {'ok': True, 'seed': b'', 'seed_length': 0,
                        'resource': resource, 'already_unlocked': True}

            seed = bytes(resp[2: 2 + seed_length]) if len(resp) >= 2 + seed_length else bytes(resp[2:])

            # If seed is longer than what fits in one response, request remaining
            remaining = seed_length - len(seed)
            while remaining > 0:
                cmd2 = bytes([int(XcpCmd.GET_SEED), 0x01, resource & 0xFF])
                ok2, resp2 = self._send_cmd(cmd2)
                if not ok2 or resp2 is None:
                    return {'ok': False, 'error': f'GET_SEED continuation failed: {self._last_error}'}
                chunk_len = resp2[1] if len(resp2) > 1 else 0
                seed += bytes(resp2[2: 2 + chunk_len])
                remaining -= chunk_len
                if chunk_len == 0:
                    break

            return {'ok': True, 'seed': seed, 'seed_length': seed_length,
                    'resource': resource, 'already_unlocked': False}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def unlock(self, key: bytes) -> Dict[str, Any]:
        """UNLOCK (0xF7) — send computed key to unlock resource.

        key: the key bytes computed from the seed (via SKB or custom algorithm).

        Returns:
          {'ok': True, 'protection_status': int}
        """
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        key_len = len(key)
        max_payload = self._max_cto - 2  # 1 byte PID + 1 byte length

        # First frame
        chunk = key[:max_payload]
        cmd = bytes([int(XcpCmd.UNLOCK), key_len & 0xFF]) + chunk
        ok, resp = self._send_cmd(cmd)
        if not ok:
            return {'ok': False, 'error': self._last_error}

        # If key is longer than one frame, send remaining via UNLOCK mode=remaining
        sent = len(chunk)
        while sent < key_len:
            remaining_chunk = key[sent: sent + max_payload]
            cmd2 = bytes([int(XcpCmd.UNLOCK), len(remaining_chunk) & 0xFF]) + remaining_chunk
            ok2, resp2 = self._send_cmd(cmd2)
            if not ok2:
                return {'ok': False, 'error': self._last_error}
            sent += len(remaining_chunk)
            resp = resp2

        try:
            prot = resp[1] if len(resp) > 1 else 0
            return {'ok': True, 'protection_status': prot}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def security_access(self, resource: int, skb_raw: bytes,
                        key_callback: Optional[Callable[[bytes, int], bytes]] = None) -> Dict[str, Any]:
        """Full seed/key handshake: GET_SEED → compute key → UNLOCK.

        Args:
            resource:     XCP resource bitmask (0x01=CAL, 0x04=DAQ, etc.)
            skb_raw:      Raw bytes of the Vector .skb file.
            key_callback: Optional custom callback(seed, security_level) → key.
                          If None, the built-in SKB VM interpreter is used.

        Returns:
            {'ok': True/False, 'resource': int, 'seed_hex': str, 'key_hex': str, ...}
        """
        # 1. GET_SEED
        seed_result = self.get_seed(resource)
        if not seed_result.get('ok'):
            return seed_result
        if seed_result.get('already_unlocked'):
            return {'ok': True, 'resource': resource,
                    'message': 'already_unlocked', 'protection_status': 0}

        seed = seed_result['seed']
        seed_len = seed_result['seed_length']

        # 2. Compute key
        try:
            if key_callback:
                key = key_callback(seed, resource)
            else:
                key = compute_key_from_skb(seed, resource, skb_raw)
        except Exception as e:
            return {'ok': False, 'error': f'Key computation failed: {e}',
                    'seed_hex': seed.hex(), 'resource': resource}

        if not key:
            return {'ok': False, 'error': 'Key computation returned empty key',
                    'seed_hex': seed.hex(), 'resource': resource}

        # 3. UNLOCK
        unlock_result = self.unlock(key)
        unlock_result['resource'] = resource
        unlock_result['seed_hex'] = seed.hex()
        unlock_result['key_hex']  = key.hex()
        return unlock_result

    def unlock_all_resources(self, skb_raw: bytes,
                             key_callback: Optional[Callable[[bytes, int], bytes]] = None) -> Dict[str, Any]:
        """Attempt security access for all standard XCP resource types.

        Tries CAL/PAG (0x01), DAQ (0x04), STIM (0x08), PGM (0x10) in order.
        Returns summary of which resources were successfully unlocked.
        """
        resources = [
            (0x01, 'CAL_PAG'),
            (0x04, 'DAQ'),
            (0x08, 'STIM'),
            (0x10, 'PGM'),
        ]
        results = {}
        all_ok = True
        for res_id, res_name in resources:
            r = self.security_access(res_id, skb_raw, key_callback=key_callback)
            results[res_name] = r
            if not r.get('ok'):
                all_ok = False
        return {'ok': all_ok, 'resources': results}

    # ─────────────────────────────────────────────────────────────────
    # Memory access (Upload / Download)
    # ─────────────────────────────────────────────────────────────────

    def set_mta(self, address: int, addr_ext: int = 0) -> bool:
        """SET_MTA (0xF6) — set Memory Transfer Address pointer."""
        if not self._connected:
            return False
        cmd = struct.pack('<BBBBI',
                          int(XcpCmd.SET_MTA), 0x00, 0x00,
                          addr_ext & 0xFF,
                          address & 0xFFFFFFFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def _upload_raw(self, length: int, addr_ext: int = 0) -> Tuple[bool, Optional[bytes]]:
        """UPLOAD from current MTA, `length` bytes."""
        blocks: List[bytes] = []
        remaining = max(0, int(length))
        while remaining > 0:
            chunk = min(remaining, self._max_cto - 1)
            ok, resp = self._send_cmd(bytes([int(XcpCmd.UPLOAD), chunk & 0xFF]))
            if not ok:
                return False, None
            blocks.append(bytes(resp[1: 1 + chunk]))
            remaining -= chunk
        return True, b''.join(blocks)

    def short_upload(
        self, address: int, length: int, addr_ext: int = 0
    ) -> Tuple[bool, Optional[bytes]]:
        """SHORT_UPLOAD (0xF4) — read 1–6 bytes directly from ECU address.

        Returns (True, raw_bytes) or (False, None).
        """
        if not self._connected:
            return False, None
        n = max(1, min(int(length), self._max_cto - 2))
        cmd = struct.pack('<BBBBI',
                          int(XcpCmd.SHORT_UPLOAD),
                          n & 0xFF, 0x00,
                          addr_ext & 0xFF,
                          address & 0xFFFFFFFF)
        ok, resp = self._send_cmd(cmd)
        if not ok or resp is None:
            return False, None
        return True, bytes(resp[1: 1 + n])

    def upload(
        self, address: int, length: int, addr_ext: int = 0
    ) -> Tuple[bool, Optional[bytes]]:
        """Read `length` bytes from ECU via SET_MTA + UPLOAD."""
        if not self._connected:
            return False, None
        if not self.set_mta(address, addr_ext):
            return False, None
        return self._upload_raw(length)

    def short_download(
        self, address: int, data: bytes, addr_ext: int = 0
    ) -> bool:
        """SHORT_DOWNLOAD (0xED) — write 1–5 bytes directly to ECU address."""
        if not self._connected:
            return False
        n = len(data)
        if n < 1 or n > 5:
            return False
        header = struct.pack('<BBBBI',
                             int(XcpCmd.SHORT_DOWNLOAD),
                             n & 0xFF, 0x00,
                             addr_ext & 0xFF,
                             address & 0xFFFFFFFF)
        cmd = (header + data[:n])[: self._max_cto]
        ok, _ = self._send_cmd(cmd)
        return ok

    def download(
        self, address: int, data: bytes, addr_ext: int = 0
    ) -> bool:
        """Write arbitrary bytes to ECU via SET_MTA + DOWNLOAD."""
        if not self._connected:
            return False
        if not self.set_mta(address, addr_ext):
            return False
        remaining = data
        while remaining:
            chunk   = remaining[: self._max_cto - 2]
            remaining = remaining[len(chunk):]
            cmd = bytes([int(XcpCmd.DOWNLOAD), len(chunk) & 0xFF]) + chunk
            ok, _ = self._send_cmd(cmd)
            if not ok:
                return False
        return True

    def read_signal(self, sig: XcpSignal) -> Tuple[bool, Optional[float]]:
        """Read a single signal via SHORT_UPLOAD and apply calibration."""
        _, size = _DTYPE_FMT.get(sig.dtype, ('f', 4))
        ok, raw = self.short_upload(sig.address, size, sig.address_ext)
        if not ok or not raw or len(raw) < size:
            return False, None
        fmt_c, _ = _DTYPE_FMT.get(sig.dtype, ('f', 4))
        try:
            (raw_val,) = struct.unpack(self._endian(sig.byte_order) + fmt_c, raw[:size])
        except Exception:
            return False, None
        phys = float(raw_val) * sig.factor + sig.offset
        sig.last_value  = phys
        sig.last_ts_ms  = int(time.time() * 1000)
        return True, phys

    def write_signal(self, sig: XcpSignal, value: float) -> bool:
        """Write a calibration value via SHORT_DOWNLOAD."""
        fmt_c, size = _DTYPE_FMT.get(sig.dtype, ('f', 4))
        try:
            if sig.factor == 0.0:
                return False
            raw_val   = (value - sig.offset) / sig.factor
            raw_bytes = struct.pack(self._endian(sig.byte_order) + fmt_c, raw_val)
        except Exception:
            return False
        return self.short_download(sig.address, raw_bytes, sig.address_ext)

    # ─────────────────────────────────────────────────────────────────
    # DAQ system — low-level primitives
    # ─────────────────────────────────────────────────────────────────

    def free_daq(self) -> bool:
        """FREE_DAQ (0xD6) — clear all dynamic DAQ lists."""
        if not self._connected:
            return False
        ok, _ = self._send_cmd(bytes([int(XcpCmd.FREE_DAQ)]))
        if ok:
            with self._state_lock:
                self._daq_lists.clear()
        return ok

    def alloc_daq(self, count: int) -> bool:
        """ALLOC_DAQ (0xD5) — allocate `count` DAQ lists."""
        if not self._connected:
            return False
        cmd = struct.pack('<BxH', int(XcpCmd.ALLOC_DAQ), count & 0xFFFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def alloc_odt(self, daq_list_num: int, odt_count: int) -> bool:
        """ALLOC_ODT (0xD4) — allocate `odt_count` ODTs in a DAQ list."""
        if not self._connected:
            return False
        cmd = struct.pack('<BxHB', int(XcpCmd.ALLOC_ODT),
                          daq_list_num & 0xFFFF, odt_count & 0xFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def alloc_odt_entry(
        self, daq_list_num: int, odt_num: int, entry_count: int
    ) -> bool:
        """ALLOC_ODT_ENTRY (0xD3)."""
        if not self._connected:
            return False
        cmd = struct.pack('<BxHBB', int(XcpCmd.ALLOC_ODT_ENTRY),
                          daq_list_num & 0xFFFF,
                          odt_num & 0xFF, entry_count & 0xFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def set_daq_ptr(
        self, daq_list_num: int, odt_num: int, odt_entry_num: int
    ) -> bool:
        """SET_DAQ_PTR (0xE2)."""
        if not self._connected:
            return False
        cmd = struct.pack('<BxHBB', int(XcpCmd.SET_DAQ_PTR),
                          daq_list_num & 0xFFFF,
                          odt_num & 0xFF, odt_entry_num & 0xFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def write_daq(
        self, bit_offset: int, element_size: int, addr_ext: int, address: int
    ) -> bool:
        """WRITE_DAQ (0xE1) — define an ODT entry."""
        if not self._connected:
            return False
        cmd = struct.pack('<BBBbI',
                          int(XcpCmd.WRITE_DAQ),
                          bit_offset & 0xFF,
                          element_size & 0xFF,
                          addr_ext,
                          address & 0xFFFFFFFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def set_daq_list_mode(
        self,
        daq_list_num: int,
        mode: int,
        event_channel: int,
        prescaler:     int,
        priority:      int,
    ) -> bool:
        """SET_DAQ_LIST_MODE (0xE0)."""
        if not self._connected:
            return False
        cmd = struct.pack('<BBHHBB',
                          int(XcpCmd.SET_DAQ_LIST_MODE),
                          mode & 0xFF,
                          daq_list_num & 0xFFFF,
                          event_channel & 0xFFFF,
                          prescaler & 0xFF,
                          priority & 0xFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def start_stop_daq_list(self, mode: int, daq_list_num: int) -> bool:
        """START_STOP_DAQ_LIST (0xDE).

        mode:
          0x02  SELECT  (prepare for synchronised start)
          0x01  START
          0x00  STOP
        """
        if not self._connected:
            return False
        cmd = struct.pack('<BBH', int(XcpCmd.START_STOP_DAQ_LIST),
                          mode & 0xFF, daq_list_num & 0xFFFF)
        ok, _ = self._send_cmd(cmd)
        return ok

    def start_stop_synch(self, mode: int) -> bool:
        """START_STOP_SYNCH (0xDD).

        mode:
          0x01  Start all selected DAQ lists
          0x00  Stop  all DAQ lists
          0x02  Stop  all and prepare for restart
        """
        if not self._connected:
            return False
        cmd = bytes([int(XcpCmd.START_STOP_SYNCH), mode & 0xFF])
        ok, _ = self._send_cmd(cmd)
        return ok

    # ─────────────────────────────────────────────────────────────────
    # DAQ high-level API (Vector-style setup sequence)
    # ─────────────────────────────────────────────────────────────────

    def setup_daq(self, daq_lists_conf: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Configure DAQ from a high-level descriptor (Vector CANape style).

        Each entry in `daq_lists_conf`:
          {
            'event_channel': 0,       # ECU trigger event index
            'prescaler':     1,        # sampling prescaler (1 = every event)
            'priority':      0,
            'mode':          0x10,     # 0x10 = TIMESTAMP enabled
            'signals': [
              {
                'name':       'GearPosition',
                'address':    '0x20002000',   # hex string or int
                'addr_ext':   0,
                'dtype':      'UBYTE',         # ASAM XCP dtype name
                'byte_order': 'little',
                'factor':     1.0,
                'offset':     0.0,
                'unit':       '',
              }, …
            ]
          }

        Returns {'ok': bool, 'daq_lists': N, 'signals': [...names...]}
        """
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        if not daq_lists_conf:
            return {'ok': True, 'daq_lists': 0, 'signals': []}

        # 1. Optionally query processor capabilities
        try:
            self.get_daq_processor_info()
        except Exception:
            pass

        # 2. FREE_DAQ
        if not self.free_daq():
            return {'ok': False, 'error': 'FREE_DAQ failed'}

        n_lists = len(daq_lists_conf)

        # 3. ALLOC_DAQ
        if not self.alloc_daq(n_lists):
            return {'ok': False, 'error': 'ALLOC_DAQ failed'}

        new_lists: List[XcpDaqList] = []
        all_signal_names: List[str]  = []

        for list_idx, ld in enumerate(daq_lists_conf):
            sigs_conf = ld.get('signals') or []
            n_entries = len(sigs_conf)
            if n_entries == 0:
                continue

            # 4. ALLOC_ODT – one ODT per DAQ list (standard Vector approach)
            if not self.alloc_odt(list_idx, 1):
                return {'ok': False, 'error': f'ALLOC_ODT failed list={list_idx}'}

            # 5. ALLOC_ODT_ENTRY
            if not self.alloc_odt_entry(list_idx, 0, n_entries):
                return {
                    'ok': False,
                    'error': f'ALLOC_ODT_ENTRY failed list={list_idx} odt=0',
                }

            # 6. Write ODT entries
            signals: List[XcpSignal] = []
            for entry_idx, sd in enumerate(sigs_conf):
                if not self.set_daq_ptr(list_idx, 0, entry_idx):
                    return {
                        'ok': False,
                        'error': f'SET_DAQ_PTR failed ({list_idx},0,{entry_idx})',
                    }

                dtype = XcpDType.from_name(str(sd.get('dtype', 'FLOAT32_IEEE')))
                _, elem_size = _DTYPE_FMT.get(dtype, ('f', 4))

                addr_raw = sd.get('address', 0)
                if isinstance(addr_raw, str):
                    try:
                        addr = int(addr_raw, 0)
                    except Exception:
                        addr = 0
                else:
                    addr = int(addr_raw)

                addr_ext = int(sd.get('addr_ext', 0))

                if not self.write_daq(0xFF, elem_size, addr_ext, addr):
                    return {
                        'ok': False,
                        'error': f'WRITE_DAQ failed list={list_idx} entry={entry_idx}',
                    }

                sig = XcpSignal(
                    name        = str(sd.get('name', f'DAQ_{list_idx}_{entry_idx}')),
                    address     = addr,
                    address_ext = addr_ext,
                    dtype       = dtype,
                    byte_order  = str(sd.get('byte_order', self._byte_order)).lower(),
                    unit        = str(sd.get('unit', '')),
                    factor      = float(sd.get('factor', 1.0)),
                    offset      = float(sd.get('offset', 0.0)),
                    comment     = str(sd.get('comment', '')),
                    daq_ptr     = (list_idx, 0, entry_idx),
                )
                signals.append(sig)
                all_signal_names.append(sig.name)

            with self._state_lock:
                for s in signals:
                    self._signals[s.name] = s

            # 7. SET_DAQ_LIST_MODE
            mode       = int(ld.get('mode',          0x10))
            event_ch   = int(ld.get('event_channel', 0))
            prescaler  = int(ld.get('prescaler',     1))
            priority   = int(ld.get('priority',      0))

            if not self.set_daq_list_mode(list_idx, mode, event_ch, prescaler, priority):
                return {
                    'ok': False,
                    'error': f'SET_DAQ_LIST_MODE failed list={list_idx}',
                }

            # 8. SELECT for synchronised start
            if not self.start_stop_daq_list(0x02, list_idx):
                return {
                    'ok': False,
                    'error': f'START_STOP_DAQ_LIST(SELECT) failed list={list_idx}',
                }

            new_lists.append(XcpDaqList(
                idx           = list_idx,
                signals       = signals,
                event_channel = event_ch,
                prescaler     = prescaler,
                priority      = priority,
                mode          = mode,
            ))

        with self._state_lock:
            self._daq_lists = new_lists

        return {
            'ok':       True,
            'daq_lists': len(new_lists),
            'signals':   all_signal_names,
        }

    def start_daq(self) -> Dict[str, Any]:
        """START_STOP_SYNCH(0x01) — start all selected DAQ lists."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}
        if self._daq_running:
            return {'ok': True, 'message': 'already_running'}

        if not self.start_stop_synch(0x01):
            return {'ok': False, 'error': 'START_STOP_SYNCH start failed'}

        self._daq_running = True
        with self._state_lock:
            for dl in self._daq_lists:
                dl.running = True

        self._emit('xcp_status', {'daq_running': True})
        return {'ok': True}

    def stop_daq(self) -> Dict[str, Any]:
        """START_STOP_SYNCH(0x00) — stop all DAQ lists."""
        if not self._connected:
            return {'ok': False, 'error': 'not_connected'}

        self.start_stop_synch(0x00)
        self._daq_running = False
        with self._state_lock:
            for dl in self._daq_lists:
                dl.running = False

        self._emit('xcp_status', {'daq_running': False})
        return {'ok': True}

    # ─────────────────────────────────────────────────────────────────
    # Polling mode (fallback when DAQ not available on ECU)
    # ─────────────────────────────────────────────────────────────────

    def start_polling(
        self,
        signal_names: Optional[List[str]] = None,
        interval_ms:  int = 100,
    ) -> Dict[str, Any]:
        """Start background SHORT_UPLOAD polling for the listed signals.

        If `signal_names` is None, all registered signals are polled.
        Polling is the fallback mode when the slave does not support DAQ.
        """
        if self._daq_running:
            return {'ok': False, 'error': 'daq_running; stop DAQ before polling'}
        if self._poll_thread and self._poll_thread.is_alive():
            return {'ok': True, 'message': 'already_polling'}

        names = signal_names or list(self._signals.keys())
        interval_s = max(0.010, interval_ms / 1000.0)

        self._stop_poll.clear()

        def _loop() -> None:
            while not self._stop_poll.is_set() and self._connected:
                ts_ms = int(time.time() * 1000)
                for name in names:
                    with self._state_lock:
                        sig = self._signals.get(name)
                    if sig is None:
                        continue
                    ok, val = self.read_signal(sig)
                    if ok and val is not None:
                        with self._meas_lock:
                            buf = self._measurements.setdefault(sig.name, [])
                            buf.append({'ts_ms': ts_ms, 'value': val, 'unit': sig.unit})
                            if len(buf) > self._MEAS_MAX:
                                del buf[: self._MEAS_MAX // 10]
                        self._stat_inc('meas_points')
                        self._emit('xcp_daq', {
                            'signal': sig.name, 'value': val,
                            'unit': sig.unit, 'ts_ms': ts_ms,
                        })
                        try:
                            if callable(self._mf4_cb):
                                self._mf4_cb(ts_ms / 1000.0, sig.name, val, sig.unit)
                        except Exception:
                            pass
                self._stop_poll.wait(timeout=interval_s)

        self._poll_thread = threading.Thread(
            target=_loop, name='xcp-can-poll', daemon=True
        )
        self._poll_thread.start()
        return {'ok': True, 'signals': names, 'interval_ms': interval_ms}

    def stop_polling(self) -> None:
        """Stop the background polling thread."""
        self._stop_poll.set()
        if self._poll_thread and self._poll_thread.is_alive():
            try:
                self._poll_thread.join(timeout=2.0)
            except Exception:
                pass
        self._poll_thread = None

    # ─────────────────────────────────────────────────────────────────
    # Signal registry
    # ─────────────────────────────────────────────────────────────────

    def add_signal(self, sig_conf: Dict[str, Any]) -> XcpSignal:
        """Add a signal definition to the registry.

        sig_conf keys:  name, address (hex-str or int), addr_ext, dtype,
                        byte_order, unit, factor, offset, min, max, comment.
        """
        addr_raw = sig_conf.get('address', 0)
        if isinstance(addr_raw, str):
            try:
                addr = int(addr_raw, 0)
            except Exception:
                addr = 0
        else:
            addr = int(addr_raw)

        dtype = XcpDType.from_name(str(sig_conf.get('dtype', 'FLOAT32_IEEE')))

        sig = XcpSignal(
            name        = str(sig_conf.get('name', 'Signal')).strip(),
            address     = addr,
            address_ext = int(sig_conf.get('addr_ext', 0)),
            dtype       = dtype,
            byte_order  = str(sig_conf.get('byte_order', self._byte_order)).lower(),
            unit        = str(sig_conf.get('unit', '')),
            factor      = float(sig_conf.get('factor', 1.0)),
            offset      = float(sig_conf.get('offset', 0.0)),
            min_value   = float(sig_conf['min']) if sig_conf.get('min') is not None else None,
            max_value   = float(sig_conf['max']) if sig_conf.get('max') is not None else None,
            comment     = str(sig_conf.get('comment', '')),
        )
        with self._state_lock:
            self._signals[sig.name] = sig
        return sig

    def remove_signal(self, name: str) -> bool:
        with self._state_lock:
            return self._signals.pop(name, None) is not None

    def import_a2l_signals(self, a2l_text: str) -> Dict[str, Any]:
        """Parse a subset of A2L text and register MEASUREMENT/CHARACTERISTIC objects.

        Supports:
          /begin MEASUREMENT  name  … ECU_ADDRESS …  /end MEASUREMENT
          /begin CHARACTERISTIC name … ECU_ADDRESS … /end CHARACTERISTIC

        Returns {'ok': bool, 'imported': N, 'signals': [names], 'errors': [...]}
        """
        block_re   = re.compile(
            r'/begin\s+(?:MEASUREMENT|CHARACTERISTIC)\s+([\w.]+)'
            r'\s+.*?/end\s+(?:MEASUREMENT|CHARACTERISTIC)',
            re.DOTALL | re.IGNORECASE,
        )
        addr_re    = re.compile(r'ECU_ADDRESS\s+(0x[0-9A-Fa-f]+|\d+)', re.IGNORECASE)
        dtype_re   = re.compile(r'DATATYPE\s+(\w+)',                    re.IGNORECASE)
        unit_re    = re.compile(r'PHYS_UNIT\s+"([^"]*)"',               re.IGNORECASE)
        coeffs_re  = re.compile(
            r'COEFFS_LINEAR\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)',          re.IGNORECASE
        )
        lower_re   = re.compile(r'LOWER_LIMIT\s+([\d.eE+\-]+)',         re.IGNORECASE)
        upper_re   = re.compile(r'UPPER_LIMIT\s+([\d.eE+\-]+)',         re.IGNORECASE)
        bo_re      = re.compile(r'BYTE_ORDER\s+(MSB_FIRST|LSB_FIRST)',  re.IGNORECASE)
        ext_re     = re.compile(r'ECU_ADDRESS_EXTENSION\s+(0x[0-9A-Fa-f]+|\d+)', re.IGNORECASE)

        imported: List[str] = []
        errors:   List[str] = []

        for m in block_re.finditer(a2l_text):
            block = m.group(0)
            name  = m.group(1)

            am = addr_re.search(block)
            if not am:
                errors.append(f'No ECU_ADDRESS for {name}')
                continue
            try:
                address = int(am.group(1), 0)
            except Exception:
                errors.append(f'Bad ECU_ADDRESS for {name}')
                continue

            dm   = dtype_re.search(block)
            um   = unit_re.search(block)
            cm   = coeffs_re.search(block)
            lm   = lower_re.search(block)
            upm  = upper_re.search(block)
            bom  = bo_re.search(block)
            exm  = ext_re.search(block)

            factor = 1.0
            offset = 0.0
            if cm:
                try:
                    factor = float(cm.group(1))
                    offset = float(cm.group(2))
                except Exception:
                    pass

            min_val = None
            max_val = None
            try:
                min_val = float(lm.group(1)) if lm else None
            except Exception:
                pass
            try:
                max_val = float(upm.group(1)) if upm else None
            except Exception:
                pass

            byte_order = self._byte_order
            if bom:
                byte_order = 'big' if bom.group(1).upper() == 'MSB_FIRST' else 'little'

            addr_ext = 0
            if exm:
                try:
                    addr_ext = int(exm.group(1), 0)
                except Exception:
                    addr_ext = 0

            try:
                self.add_signal({
                    'name':       name,
                    'address':    address,
                    'addr_ext':   addr_ext,
                    'dtype':      dm.group(1) if dm else 'FLOAT32_IEEE',
                    'unit':       um.group(1) if um else '',
                    'factor':     factor,
                    'offset':     offset,
                    'min':        min_val,
                    'max':        max_val,
                    'byte_order': byte_order,
                })
                imported.append(name)
            except Exception as exc:
                errors.append(f'{name}: {exc}')

        return {
            'ok':       True,
            'imported': len(imported),
            'signals':  imported,
            'errors':   errors[:30],
        }

    # ─────────────────────────────────────────────────────────────────
    # Measurement history
    # ─────────────────────────────────────────────────────────────────

    def get_measurements(
        self,
        signal_name: str,
        limit:       int = 500,
    ) -> List[Dict[str, Any]]:
        """Return the last `limit` measurement samples for `signal_name`."""
        with self._meas_lock:
            buf = self._measurements.get(signal_name, [])
            return list(buf[-max(1, int(limit)):])

    def clear_measurements(self, signal_name: Optional[str] = None) -> None:
        """Clear measurement history (all signals or a single one)."""
        with self._meas_lock:
            if signal_name:
                self._measurements.pop(signal_name, None)
            else:
                self._measurements.clear()

    # ─────────────────────────────────────────────────────────────────
    # Status / introspection
    # ─────────────────────────────────────────────────────────────────

    def _session_dict(self) -> Dict[str, Any]:
        return self._session.to_dict() if self._session else {}

    def status(self) -> Dict[str, Any]:
        """Return a full status snapshot (JSON-safe)."""
        with self._state_lock:
            stats  = dict(self._stats)
            sigs   = {k: s.to_dict() for k, s in self._signals.items()}
            daq    = [dl.to_dict() for dl in self._daq_lists]
            conn   = bool(self._connected)
            daqrun = bool(self._daq_running)
            err    = self._last_error

        return {
            'ok':          True,
            'connected':   conn,
            'daq_running': daqrun,
            'channel':     self._channel,
            'cmd_id':      f'0x{self._cmd_id:03X}',
            'res_id':      f'0x{self._res_id:03X}',
            'is_extended': self._is_extended,
            'is_canfd':    self._is_canfd,
            'max_cto':     self._max_cto,
            'max_dto':     self._max_dto,
            'timeout_ms':  self._timeout_ms,
            'retry_count': self._retry_count,
            'session':     self._session_dict(),
            'last_error':  err,
            'stats':       stats,
            'signals':     sigs,
            'daq_lists':   daq,
        }

    def list_signals(self) -> List[Dict[str, Any]]:
        with self._state_lock:
            return [s.to_dict() for s in self._signals.values()]

    # ─────────────────────────────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: Any) -> None:
        """Emit a SocketIO event (best-effort)."""
        try:
            if self._socketio:
                self._socketio.emit(event, data)
        except Exception:
            pass

    def destroy(self) -> None:
        """Release all resources: stop DAQ/polling, disconnect, de-register listener."""
        try:
            if self._daq_running:
                self.stop_daq()
        except Exception:
            pass
        try:
            self.stop_polling()
        except Exception:
            pass
        try:
            if self._connected:
                self.disconnect()
        except Exception:
            pass
        try:
            self._bm.remove_listener(self._on_frame)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Real gearbox-ECU signal definitions (242 signals from GLC import)
# Source: IBN_Team_Shakedown_XCP_531_LB634___V2___LIGHT.glc
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_GEARBOX_SIGNALS: List[Dict[str, Any]] = [
    {
        'name':       'DST.DST_LodiParkInterface.sm',
        'address':    '0x6000FC79',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'DST.DST_ModeSelectGeneric.sm',
        'address':    '0x6000FD86',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'DST.DST_ShiftMode',
        'address':    '0x6000F49F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Variable to selectg shift map, 0 = Normal 1, 1 = Normal 2, 2 = Sport 1, 3 = Sport 2, 4 = Manual',
    },
    {
        'name':       'DST_DemandGear',
        'address':    '0x60020601',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Target gear from DST',
    },
    {
        'name':       'DST_DrivingMode',
        'address':    '0x70004B3D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current driving mode selected',
    },
    {
        'name':       'DST_HybridMode',
        'address':    '0x600205F5',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Indicates the current requested hybrid mode',
    },
    {
        'name':       'DST_PreselectGear',
        'address':    '0x600205E9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Demanded preselect gear for the transmission',
    },
    {
        'name':       'DWC.DWC_DCDemandK1',
        'address':    '0x6000FF9E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Device controller command for K1',
    },
    {
        'name':       'DWC.DWC_DCDemandK2',
        'address':    '0x6000FF9F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Device controller command for K2',
    },
    {
        'name':       'DWC.DWC_FlgCleanK1',
        'address':    '0x6000FFA1',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'K1 clean in progress',
    },
    {
        'name':       'DWC.DWC_FlgCleanK2',
        'address':    '0x6000FFA2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'K2 clean in progress',
    },
    {
        'name':       'DWC.DWC_FlgCleanVSS1',
        'address':    '0x6000FFA4',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'VSS1 clean active',
    },
    {
        'name':       'DWC.DWC_InputProcessing.DWC_K1Pressure',
        'address':    '0x6001003A',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Pressure on clutch - corrected for offsets',
    },
    {
        'name':       'DWC.DWC_InputProcessing.DWC_K1Slip',
        'address':    '0x6001003C',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current unfiltered slip of K1',
    },
    {
        'name':       'DWC.DWC_InputProcessing.DWC_K2Pressure',
        'address':    '0x60010042',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Pressure on clutch - corrected for offsets',
    },
    {
        'name':       'DWC.DWC_InputProcessing.DWC_K2Slip',
        'address':    '0x60010044',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current unfiltered slip of K2',
    },
    {
        'name':       'DWC.DWC_K1DeviceControl.DWC_CurrentDemand',
        'address':    '0x6001033A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current demand on pressure control valve',
    },
    {
        'name':       'DWC.DWC_K1DeviceControl.DWC_PressureDemand',
        'address':    '0x60010370',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Final pressure demand for clutch',
    },
    {
        'name':       'DWC.DWC_K1DeviceStatus',
        'address':    '0x6000FFBD',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of clutch control device',
    },
    {
        'name':       'DWC.DWC_K1StateMachine.sm',
        'address':    '0x600107E5',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'DWC.DWC_K1Torque',
        'address':    '0x6000FEFA',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'K1 torque capacity request',
    },
    {
        'name':       'DWC.DWC_K1TorqueFB',
        'address':    '0x6000FEFC',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'K1 torque capacity estimate',
    },
    {
        'name':       'DWC.DWC_K2DeviceControl.DWC_CurrentDemand',
        'address':    '0x6001081E',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current demand on pressure control valve',
    },
    {
        'name':       'DWC.DWC_K2DeviceControl.DWC_PressureDemand',
        'address':    '0x60010854',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Final pressure demand for clutch',
    },
    {
        'name':       'DWC.DWC_K2DeviceStatus',
        'address':    '0x6000FFCC',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of clutch control device',
    },
    {
        'name':       'DWC.DWC_K2StateMachine.sm',
        'address':    '0x60010CC9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'DWC.DWC_K2Torque',
        'address':    '0x6000FF1C',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'K2 torque capacity request',
    },
    {
        'name':       'DWC.DWC_K2TorqueFB',
        'address':    '0x6000FF1E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'K2 torque capacity estimate',
    },
    {
        'name':       'DWC.DWC_SlipTarget',
        'address':    '0x6000FF5E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current slip target for clutch',
    },
    {
        'name':       'DWC.DWC_SpeedPhaseType',
        'address':    '0x6000FFDF',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Description of speed  phase in progress',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_ControllerTorqueDemand',
        'address':    '0x60010F68',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Overall controller torque demand',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_ControllerType',
        'address':    '0x60011198',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Type of controller taking dominance',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_DominantState',
        'address':    '0x6001119F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'State of operation of the dominant controller',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_DominantTorque',
        'address':    '0x60010F72',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Torque demand for dominant clutch',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_FastFill',
        'address':    '0x600111A5',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fast fill flag',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_FFCTorqueDemand',
        'address':    '0x60010F84',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Torque demand from feedforward controller',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_ITerm',
        'address':    '0x60010E94',
        'dtype':      'SLONG',
        'unit':       '',
        'factor':     10000.0,
        'offset':     0.0,
        'comment':    'Summation of Integral term',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_PAClutchTargetTorque',
        'address':    '0x60010FF0',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Rate limited torque target for the clutch before engine torque limit',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_PassiveState',
        'address':    '0x600111EB',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'State of operation of the passive controller',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_PassiveTorque',
        'address':    '0x60011072',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Torque demand for passive clutch',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_PITerm',
        'address':    '0x60010F04',
        'dtype':      'SLONG',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Combined PI term',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_PTerm',
        'address':    '0x6001106C',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Proportional term of controller, after controller selection',
    },
    {
        'name':       'DWC.DWC_TorqueControl.DWC_SLCError',
        'address':    '0x60011082',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Slip Controller Error',
    },
    {
        'name':       'DWC.DWC_TorqueControl.IOP_DriverTorqueRaw',
        'address':    '0x60011138',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Driver torque signal - untouched',
    },
    {
        'name':       'DWC.DWC_TorqueControl.IOP_EngineTorqueNoGearboxRaw',
        'address':    '0x60011154',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Engine torque without gearbox intervention - untouched signal',
    },
    {
        'name':       'DWC.IOP_SystemStatus',
        'address':    '0x6000FE80',
        'dtype':      'ULONG',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current system status',
    },
    {
        'name':       'DWC_NVLIFETIMEB.DWC_K1DeviceControl.DWC_PressCurrent.DWC_CurrentOffsetKisspoint',
        'address':    '0x60034384',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Offset for valve characteristic at kisspoint pressure',
    },
    {
        'name':       'DWC_NVLIFETIMEB.DWC_K1DeviceControl.DWC_PressCurrent.DWC_CurrentOffsetMaximum',
        'address':    '0x60034386',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Offset for valve current demand at maximum pressure',
    },
    {
        'name':       'DWC_NVLIFETIMEB.DWC_K2DeviceControl.DWC_PressCurrent.DWC_CurrentOffsetKisspoint',
        'address':    '0x600343A0',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Offset for valve characteristic at kisspoint pressure',
    },
    {
        'name':       'DWC_NVLIFETIMEB.DWC_K2DeviceControl.DWC_PressCurrent.DWC_CurrentOffsetMaximum',
        'address':    '0x600343A2',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Offset for valve current demand at maximum pressure',
    },
    {
        'name':       'DWC_NVUSAGEB.DWC_Clutch1SeperatorPlateTempMax',
        'address':    '0x600355F0',
        'dtype':      'SLONG',
        'unit':       '',
        'factor':     10000.0,
        'offset':     0.0,
        'comment':    'Modelled Clutch 1 Seperator Plate Temperature',
    },
    {
        'name':       'DWC_NVUSAGEB.DWC_Clutch2SeperatorPlateTempMax',
        'address':    '0x600355F8',
        'dtype':      'SLONG',
        'unit':       '',
        'factor':     10000.0,
        'offset':     0.0,
        'comment':    'Modelled Clutch 1 Seperator Plate Temperature',
    },
    {
        'name':       'DWC_DownshiftType',
        'address':    '0x600205D1',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Type of downshift to be sequenced',
    },
    {
        'name':       'DWC_K1State',
        'address':    '0x600205C0',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Operating state of K1 clutch',
    },
    {
        'name':       'DWC_K1Status',
        'address':    '0x600205BF',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the K1 clutch',
    },
    {
        'name':       'DWC_K2State',
        'address':    '0x600205B4',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Operating state of K2 clutch',
    },
    {
        'name':       'DWC_K2Status',
        'address':    '0x600205B3',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the K2 clutch',
    },
    {
        'name':       'ENG_EngineInterface.ENG_EngineInterface.sm',
        'address':    '0x60011A98',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'ENG_TargetEngineTorque',
        'address':    '0x70004A90',
        'dtype':      'UWORD',
        'unit':       'Nm',
        'factor':     4.0,
        'offset':     0.0,
        'comment':    'Target engine torque',
    },
    {
        'name':       'ENG_TorqueReserveRequest',
        'address':    '0x6001B230',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     4.0,
        'offset':     0.0,
        'comment':    'Torque reserve request',
    },
    {
        'name':       'GAC.GAC_CmdGrac0',
        'address':    '0x60011C17',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to this gear actuator.',
    },
    {
        'name':       'GAC.GAC_CmdGrac1',
        'address':    '0x60011C18',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to this gear actuator.',
    },
    {
        'name':       'GAC.GAC_CmdGrac2',
        'address':    '0x60011C19',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to this gear actuator.',
    },
    {
        'name':       'GAC.GAC_CmdGrac3',
        'address':    '0x60011C1A',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to this gear actuator.',
    },
    {
        'name':       'GAC.GAC_CmdGrac4',
        'address':    '0x60011C1B',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to this gear actuator.',
    },
    {
        'name':       'GAC.GAC_CodeFailedGearTable',
        'address':    '0x60011C1E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to actuate to Neutral.',
    },
    {
        'name':       'GAC.GAC_CodeFailedHybridMode',
        'address':    '0x60011C1F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to actuate to Neutral.',
    },
    {
        'name':       'GAC.GAC_EOLPosnLearn.GAC_StatePosnGracA',
        'address':    '0x60011ECC',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Position state for this actuator.',
    },
    {
        'name':       'GAC.GAC_EOLPosnLearn.GAC_StatePosnGracB',
        'address':    '0x60011ECD',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Position state for this actuator.',
    },
    {
        'name':       'GAC.GAC_EOLPosnLearn.GAC_StatePosnGracC',
        'address':    '0x60011ECE',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Position state for this actuator.',
    },
    {
        'name':       'GAC.GAC_EOLPosnLearn.GAC_StatePosnGracD',
        'address':    '0x60011ECF',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Position state for this actuator.',
    },
    {
        'name':       'GAC.GAC_EOLPosnLearn.GAC_StatePosnGracF',
        'address':    '0x60011ED0',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Position state for this actuator.',
    },
    {
        'name':       'GAC.GAC_HybBankCtrl.sm',
        'address':    '0x60012E5F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'GAC.GAC_PosnGrac0_A',
        'address':    '0x60011B96',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Gear actuator position.',
    },
    {
        'name':       'GAC.GAC_PosnGrac1_B',
        'address':    '0x60011B9A',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Gear actuator position.',
    },
    {
        'name':       'GAC.GAC_PosnGrac2_C',
        'address':    '0x60011B9E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Gear actuator position.',
    },
    {
        'name':       'GAC.GAC_PosnGrac3_D',
        'address':    '0x60011BA2',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Gear actuator position.',
    },
    {
        'name':       'GAC.GAC_PosnGrac4_F',
        'address':    '0x60011BA6',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Gear actuator position.',
    },
    {
        'name':       'GAC.GAC_SupvrCtrl.sm',
        'address':    '0x60013553',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'GAC_CodeFailEngineOffCbr',
        'address':    '0x60026CEA',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to engage at clutch body ring with the engine off',
    },
    {
        'name':       'GAC_CodeFailGracEnga',
        'address':    '0x60026CE8',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to engage a gear.',
    },
    {
        'name':       'GAC_CodeFailGracToN',
        'address':    '0x60026E68',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to actuate to Neutral.',
    },
    {
        'name':       'GAC_CodeFailOutOfNInShift',
        'address':    '0x60026E67',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator moving out of Neutral, shift active.',
    },
    {
        'name':       'GAC_CodeFailOutOfNNoShift',
        'address':    '0x60026E66',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator moving out of Neutral, no shift active.',
    },
    {
        'name':       'GAC_CodeFailSpitInShift',
        'address':    '0x60026CE6',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator spitting out of engaged gear, shift active.',
    },
    {
        'name':       'GAC_CodeFailSpitNoShift',
        'address':    '0x60026CE4',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator spitting out of engaged gear, no shift active.',
    },
    {
        'name':       'GAC_CodeFailStuckN',
        'address':    '0x60026E65',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator stuck in Neutral or other general faults resulting in no gear available.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracA.GAC_InGearLearn.GAC_PosnAdpInGrNeg',
        'address':    '0x60034548',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged negative gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracA.GAC_InGearLearn.GAC_PosnAdpInGrPos',
        'address':    '0x6003454E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged positive gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracB.GAC_InGearLearn.GAC_PosnAdpInGrNeg',
        'address':    '0x6003455E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged negative gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracB.GAC_InGearLearn.GAC_PosnAdpInGrPos',
        'address':    '0x60034564',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged positive gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracC.GAC_InGearLearn.GAC_PosnAdpInGrNeg',
        'address':    '0x60034574',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged negative gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracC.GAC_InGearLearn.GAC_PosnAdpInGrPos',
        'address':    '0x6003457A',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged positive gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracD.GAC_InGearLearn.GAC_PosnAdpInGrNeg',
        'address':    '0x6003458A',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged negative gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracF.GAC_InGearLearn.GAC_PosnAdpInGrNeg',
        'address':    '0x600345A0',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged negative gear position.',
    },
    {
        'name':       'GAC_NVRAM.GAC_GracF.GAC_InGearLearn.GAC_PosnAdpInGrPos',
        'address':    '0x600345A6',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Adapted engaged positive gear position.',
    },
    {
        'name':       'GAC_CodeFailGracEngaMsg',
        'address':    '0x6001B22A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to engage a gear.',
    },
    {
        'name':       'GAC_CodeFailGracToNMsg',
        'address':    '0x60020591',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator to actuate to Neutral.',
    },
    {
        'name':       'GAC_CodeFailOutOfNInShiftMsg',
        'address':    '0x60020590',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator moving out of Neutral, shift active.',
    },
    {
        'name':       'GAC_CodeFailOutOfNNoShiftMsg',
        'address':    '0x6002058F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator moving out of Neutral, no shift active.',
    },
    {
        'name':       'GAC_CodeFailSpitInShiftMsg',
        'address':    '0x6001B228',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator spitting out of engaged gear, shift active.',
    },
    {
        'name':       'GAC_CodeFailSpitNoShiftMsg',
        'address':    '0x6001B226',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator spitting out of engaged gear, no shift active.',
    },
    {
        'name':       'GAC_CodeFailStuckNMsg',
        'address':    '0x6002058E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator stuck in Neutral or other general faults resulting in no gear available.',
    },
    {
        'name':       'GAC_CodeFailedGearTableMsg',
        'address':    '0x6002058D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator stuck in Neutral or other general faults resulting in no gear available.',
    },
    {
        'name':       'GAC_CodeFailedHybridModeTableMsg',
        'address':    '0x6002058C',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Code indicating failure of the gear actuator stuck in Neutral or other general faults resulting in no gear available.',
    },
    {
        'name':       'GAC_Input1Gear',
        'address':    '0x6002057D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Gear selected on the SEQ odd bank.',
    },
    {
        'name':       'GAC_Input2Gear',
        'address':    '0x6002057A',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Gear selected on the SEQ even bank.',
    },
    {
        'name':       'GAC_MdHybEnga',
        'address':    '0x60020576',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Enumeration for the engaged hybrid mode',
    },
    {
        'name':       'SWC_HAL_CommsTx.IOP_MaxEngineTorque',
        'address':    '0x60009F0E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Maximum engine torque',
    },
    {
        'name':       'HAL_VGPActualCurrent',
        'address':    '0x6001AFD0',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ0ActualCurrent',
        'address':    '0x6001AFCC',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ1ActualCurrent',
        'address':    '0x6001AFC8',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ2ActualCurrent',
        'address':    '0x6001AFC4',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ3ActualCurrent',
        'address':    '0x6001AFC0',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ4ActualCurrent',
        'address':    '0x6001AFBC',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VGQ5ActualCurrent',
        'address':    '0x6001AFB8',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VKP1ActualCurrent',
        'address':    '0x6001AFB4',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VKP2ActualCurrent',
        'address':    '0x6001AFB0',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VKUCCActualCurrent',
        'address':    '0x6001AFAC',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VKUGLActualCurrent',
        'address':    '0x6001AFA8',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VSS1ActualCurrent',
        'address':    '0x6001AFA4',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'HAL_VSS2ActualCurrent',
        'address':    '0x6001AFA0',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BSW measured current for names output drive',
    },
    {
        'name':       'IOP_BLDCPumpDerating',
        'address':    '0x6001AF0E',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Signal representing derating of the BLDC pump',
    },
    {
        'name':       'IOP_BLDCPumpFaults',
        'address':    '0x6001AF0C',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'BLDC reported fault signal',
    },
    {
        'name':       'IOP_BLDCPumpSpeedActual',
        'address':    '0x6001AF0A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Actual BLDC pump speed',
    },
    {
        'name':       'IOP_BLDCPumpTorque',
        'address':    '0x6001AF06',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Current BLDc pump torque',
    },
    {
        'name':       'IOP_BLDCPumpVoltage',
        'address':    '0x600204B9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Current BLDC pump voltage',
    },
    {
        'name':       'IOP_DriveModeTrans',
        'address':    '0x6002049E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Dec value of transmission drive mode',
    },
    {
        'name':       'IOP_EM1Torque',
        'address':    '0x6001AEDE',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Current EM1 torque',
    },
    {
        'name':       'IOP_EngineTemperature',
        'address':    '0x6001AED4',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Engine coolant temperature',
    },
    {
        'name':       'IOP_EngineTorqueActual',
        'address':    '0x6001AED2',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Current engine torque',
    },
    {
        'name':       'IOP_EngineTorqueMaximum',
        'address':    '0x6001AEC6',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Maximum engine torque signal',
    },
    {
        'name':       'IOP_EngineTorqueNoGearbox',
        'address':    '0x6001AEC0',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Engine torque if no gearbox intervention was active',
    },
    {
        'name':       'IOP_KL50EngStart',
        'address':    '0x6002047D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Engine start signal',
    },
    {
        'name':       'IOP_LateralAcceleration',
        'address':    '0x6002047B',
        'dtype':      'SBYTE',
        'unit':       '',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Lateral acceleration signal',
    },
    {
        'name':       'IOP_LongitudinalAcceleration',
        'address':    '0x6001AEAC',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     32.0,
        'offset':     0.0,
        'comment':    'Longitudinal acceleration signal',
    },
    {
        'name':       'IOP_ThrottlePedal',
        'address':    '0x60020462',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     2.5,
        'offset':     0.0,
        'comment':    'Throttle pedal signal',
    },
    {
        'name':       'IOP_TransMode',
        'address':    '0x60020460',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Enum value of transmission drive mode',
    },
    {
        'name':       'IOP_CommsTx.IOP_LimitTorqueGear',
        'address':    '0x6000BC90',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Gear used to limit the torque',
    },
    {
        'name':       'IOP_BLDCPumpSpeedRequest',
        'address':    '0x6001AE9A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed request to the BLDC pump',
    },
    {
        'name':       'IOP_DrivingMode',
        'address':    '0x70004B33',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current driving mode',
    },
    {
        'name':       'IOP_DrivingModeDisplay',
        'address':    '0x6002044D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Driving mode to display',
    },
    {
        'name':       'IOP_DrivingModeIst',
        'address':    '0x6002044C',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Driving mode display signal',
    },
    {
        'name':       'IOP_EngineSpeedDemanded',
        'address':    '0x70004A74',
        'dtype':      'UWORD',
        'unit':       'RPM',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Target engine speed to be transmitted on CAN',
    },
    {
        'name':       'IOP_GearboxMILStatus',
        'address':    '0x6002043A',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag used to send request to illuminate MIL lamp to driver display',
    },
    {
        'name':       'IOP_MaxEngineTorque',
        'address':    '0x6001AE90',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     10.0,
        'offset':     0.0,
        'comment':    'Maxium engine torque',
    },
    {
        'name':       'IOP_MaxEngineTorqueEnabled',
        'address':    '0x60020435',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag to indicate that the maximum engine torque is required',
    },
    {
        'name':       'IOP_P2P3Config',
        'address':    '0x60020433',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current P2P3 config engaged',
    },
    {
        'name':       'IOP_SpeedControlType',
        'address':    '0x6002042B',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed control type signal',
    },
    {
        'name':       'IOP_DiagnosticResetBitfield',
        'address':    '0x60020421',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Bitfield to indicate relevant safe state has been reached and diagnostic reset request is active',
    },
    {
        'name':       'IOP_BrakeSwitch',
        'address':    '0x600203CF',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating brake switch state.',
    },
    {
        'name':       'IOP_ClutchCoolingInletTemperature',
        'address':    '0x6001AE50',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Lube/cooling oil temperature.',
    },
    {
        'name':       'IOP_ClutchCoolingPressure',
        'address':    '0x6001AE4E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Low line (lube/cooling) oil pressure.',
    },
    {
        'name':       'IOP_EngineSpeed',
        'address':    '0x6001AE46',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed measured at engine speed sensor after fault handling',
    },
    {
        'name':       'IOP_Input1Speed',
        'address':    '0x6001AE40',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Shaft speed',
    },
    {
        'name':       'IOP_Input2Speed',
        'address':    '0x6001AE3A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Shaft speed',
    },
    {
        'name':       'IOP_InternalOutputSpeed',
        'address':    '0x6001B496',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Internally transmission mounted output speed sensor (not used for Lodi project)',
    },
    {
        'name':       'IOP_InternalOutputSpeedStatus',
        'address':    '0x600203B7',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_K1Pressure',
        'address':    '0x6001AE34',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Clutch 1 pressure.',
    },
    {
        'name':       'IOP_K2Pressure',
        'address':    '0x6001AE30',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Clutch 2 pressure.',
    },
    {
        'name':       'IOP_KL15',
        'address':    '0x600203B4',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the ignition switch KL15 state.Flag indicating the ignition switch KL15 state.',
    },
    {
        'name':       'IOP_KL30_1',
        'address':    '0x6001AE2A',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Voltage of KL30_1 feed',
    },
    {
        'name':       'IOP_KL30_1_Status',
        'address':    '0x600203B2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the KL30_1 supply',
    },
    {
        'name':       'IOP_KL30_2_Status',
        'address':    '0x600203B1',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the KL30_2 supply',
    },
    {
        'name':       'IOP_KL30_3_Status',
        'address':    '0x600203B0',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the KL87_3 supply',
    },
    {
        'name':       'IOP_LinePressure',
        'address':    '0x6001AE28',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     200.0,
        'offset':     0.0,
        'comment':    'High line (actuation) oil pressure.',
    },
    {
        'name':       'IOP_OutputSpeed',
        'address':    '0x6001AE1C',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Output speed',
    },
    {
        'name':       'IOP_ParkButton',
        'address':    '0x600203A9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating Park button pressed.',
    },
    {
        'name':       'IOP_ParkButtonStatus',
        'address':    '0x600203A8',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_ParkLockPos',
        'address':    '0x6001AE18',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Parklock position after diagnostic handling.',
    },
    {
        'name':       'IOP_ParkLockPosStatus',
        'address':    '0x600203A7',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_SS1_Status',
        'address':    '0x600203A2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SS2_Status',
        'address':    '0x600203A1',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SS3_Status',
        'address':    '0x600203A0',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SS4_Status',
        'address':    '0x6002039F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SS5_Status',
        'address':    '0x6002039E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SS6_Status',
        'address':    '0x6002039D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the SS1 supply',
    },
    {
        'name':       'IOP_SpeedSensorEven',
        'address':    '0x6001ADD2',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed measured at even speed sensor after fault handling',
    },
    {
        'name':       'IOP_SpeedSensorGear37',
        'address':    '0x6001ADD0',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed measured at reduction speed sensor after fault handling',
    },
    {
        'name':       'IOP_SpeedSensorOdd',
        'address':    '0x6001B480',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed measured at odd speed sensor after fault handling',
    },
    {
        'name':       'IOP_SpeedShaftReduction',
        'address':    '0x6001ADC6',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Speed of reduction shaft',
    },
    {
        'name':       'IOP_StatusPosnSynchroA',
        'address':    '0x60020393',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusPosnSynchroB',
        'address':    '0x60020392',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusPosnSynchroC',
        'address':    '0x60020391',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusPosnSynchroD',
        'address':    '0x60020390',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusPosnSynchroF',
        'address':    '0x6002038F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusSpeedSensorEven',
        'address':    '0x6002038E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusSpeedSensorGear37',
        'address':    '0x6002038D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusSpeedSensorOdd',
        'address':    '0x6002038C',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_StatusSumpOilTemperature',
        'address':    '0x60020389',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'IOP_SumpOilTemperature',
        'address':    '0x6001ADC0',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Sump oil temperature.',
    },
    {
        'name':       'IOP_TransmissionOilTemperature',
        'address':    '0x6001ADB8',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Transmission oil temperature',
    },
    {
        'name':       'IOP_TransmissionOilTemperatureStatus',
        'address':    '0x60020388',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Flag indicating the fail (valid) status for this signal.',
    },
    {
        'name':       'L2_Lodi.L2_ClutchStatusEven',
        'address':    '0x6000E08A',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the even clutch',
    },
    {
        'name':       'L2_Lodi.L2_ClutchStatusEvenQuality',
        'address':    '0x6000E08B',
        'dtype':      'SBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Quality of the even clutch status',
    },
    {
        'name':       'L2_Lodi.L2_ClutchStatusOdd',
        'address':    '0x6000E08C',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of the odd clutch',
    },
    {
        'name':       'L2_Lodi.L2_ClutchStatusOddQuality',
        'address':    '0x6000E08D',
        'dtype':      'SBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Quality of the odd clutch status',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_00_07',
        'address':    '0x6000E20C',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_08_15',
        'address':    '0x6000E20D',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_16_23',
        'address':    '0x6000E20E',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_24_31',
        'address':    '0x6000E20F',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_32_39',
        'address':    '0x6000E210',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_40_47',
        'address':    '0x6000E211',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_Debounce.L2_McFcBF_48_49',
        'address':    '0x6000E212',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Fault bit field',
    },
    {
        'name':       'L2_Lodi.L2_SpeedSecondary',
        'address':    '0x6000E07E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Calulcated secondary speed',
    },
    {
        'name':       'MOT_MotorState',
        'address':    '0x600201F5',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'State of operation of motor interface',
    },
    {
        'name':       'MOT_MotorStatus',
        'address':    '0x600201F4',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Status of motor interface',
    },
    {
        'name':       'PCL.PCL_Control.PCL_BitFieldFault',
        'address':    '0x60013A56',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Bit field of active faults',
    },
    {
        'name':       'PCL.PCL_Control.PCL_Controller.sm',
        'address':    '0x60013D3B',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'PCL.PCL_Control.PCL_CoolingValveDiags.PCL_FlowLPS',
        'address':    '0x60013D48',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Flow to LPS',
    },
    {
        'name':       'PCL.PCL_Control.PCL_CoolingValveDiags.PCL_PressureCooling',
        'address':    '0x60013D50',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Corrected clutch cooling pressure',
    },
    {
        'name':       'PCL.PCL_Control.PCL_CoolingValveDiags.PCL_PressureCoolingMax',
        'address':    '0x60013D56',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'max threshold Corrected clutch cooling pressure',
    },
    {
        'name':       'PCL.PCL_Control.PCL_CoolingValveDiags.PCL_PressureCoolingMin',
        'address':    '0x60013D5E',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'minimum threshold Corrected clutch cooling pressure',
    },
    {
        'name':       'PCL.PCL_Control.PCL_CoolingValveDiags.PCL_PressureCoolingNominal',
        'address':    '0x60013D66',
        'dtype':      'SWORD',
        'unit':       '',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    'Correct cooling pressure for current conditions',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlgDisablePumpLubrication',
        'address':    '0x60013BE8',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Disable e-pump lubrication',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlgDisablePumpRequest',
        'address':    '0x60013BE9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Disable the pump speed request, keep the enable pin live',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlgEnablePumpCharging',
        'address':    '0x60013BFB',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Enable e-pump to charge circuit',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlowCC',
        'address':    '0x60013AAC',
        'dtype':      'UWORD',
        'unit':       'litres/min',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Clutch cooling flow demand',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlowGearLubeDem',
        'address':    '0x60013ADA',
        'dtype':      'UWORD',
        'unit':       'litres/min',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Gear lubrication flow request - unlimited',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlowGL_Motor',
        'address':    '0x60013AD4',
        'dtype':      'UWORD',
        'unit':       'litres/min',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Gear lubrication flow that comes from the e pump',
    },
    {
        'name':       'PCL.PCL_Control.PCL_FlowGL_Valve',
        'address':    '0x60013AD8',
        'dtype':      'UWORD',
        'unit':       'litres/min',
        'factor':     100.0,
        'offset':     0.0,
        'comment':    'Gear lubrication flow that comes from the engine driven pump and through the VKU-GL valve',
    },
    {
        'name':       'PCL_PressureAvailable',
        'address':    '0x600201EC',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Line pressure adequate for actuation',
    },
    {
        'name':       'SEQ_ShiftSequencer.SEQ_ASGShiftSequencer.sm',
        'address':    '0x600143B3',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'SEQ_ShiftSequencer.SEQ_DCTShiftSequencer.sm',
        'address':    '0x60014422',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'SEQ_ShiftSequencer.SEQ_P2P3ShiftSequencer.sm',
        'address':    '0x600145D2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'SEQ_CurrentHybMode',
        'address':    '0x600201E3',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current hybrid mode',
    },
    {
        'name':       'SEQ_CurrentTorqueGear',
        'address':    '0x600201E2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Gear currently deemed to be transmitting the engine torque',
    },
    {
        'name':       'SEQ_EngineRequest',
        'address':    '0x600201E0',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to engine interface controller',
    },
    {
        'name':       'SEQ_HybMode',
        'address':    '0x600201DC',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Current hybrid mode',
    },
    {
        'name':       'SEQ_HybRequest',
        'address':    '0x600201DB',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Hybrid gear request',
    },
    {
        'name':       'SEQ_Input1Gear',
        'address':    '0x600201D7',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Target gear for selection on odd bank',
    },
    {
        'name':       'SEQ_Input1Request',
        'address':    '0x600201D6',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Request to the actuators on the odd side of the transmission',
    },
    {
        'name':       'SEQ_Input2Gear',
        'address':    '0x600201D5',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Target gear for selection on even bank',
    },
    {
        'name':       'SEQ_Input2Request',
        'address':    '0x600201D4',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Request to the actuators on the even side of the transmission',
    },
    {
        'name':       'SEQ_K1Request',
        'address':    '0x600201D3',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to K1 Clutch Controller',
    },
    {
        'name':       'SEQ_K2Request',
        'address':    '0x600201D2',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Command to K2 Clutch Controller',
    },
    {
        'name':       'SEQ_TargetGear',
        'address':    '0x600201C9',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Gear requested for targetting by the shift sequencers',
    },
    {
        'name':       'SEQ_TargetHybMode',
        'address':    '0x600201C8',
        'dtype':      'UBYTE',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    'Hybrid Mode requested for targetting by the shift sequencers',
    },
    {
        'name':       'DFES_numDFC',
        'address':    '0x60038CE0',
        'dtype':      'UWORD',
        'unit':       '-',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'DFES_stChk',
        'address':    '0x60038CC2',
        'dtype':      'UBYTE',
        'unit':       '-',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'rbg_BattSply_stQly',
        'address':    '0x600065B2',
        'dtype':      'UWORD',
        'unit':       '',
        'factor':     1.0,
        'offset':     0.0,
        'comment':    '',
    },
    {
        'name':       'rbg_BattSply_u',
        'address':    '0x600065B4',
        'dtype':      'UWORD',
        'unit':       'V',
        'factor':     1000.0,
        'offset':     0.0,
        'comment':    '',
    },
]



def default_xcp_can_config() -> Dict[str, Any]:
    """Return a safe default XCP-on-CAN configuration (Vector-style defaults)."""
    return {
        'can_channel':    0,
        'cmd_id':         '0x7FF',   # Master → Slave  (ASAM default)
        'res_id':         '0x7FE',   # Slave  → Master (ASAM default)
        'is_extended_id': False,
        'is_canfd':       False,
        'byte_order':     'little',
        'max_cto':        8,
        'max_dto':        8,
        'timeout_ms':     200,
        'retry_count':    3,
    }
