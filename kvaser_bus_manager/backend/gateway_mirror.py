import ipaddress
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union


MIRROR_MODE_DID_DEFAULT = 0x096F
MIRROR_MODE_BUS_MAPPING_DID_DEFAULT = 0x189A


@dataclass(frozen=True)
class MirrorModeRequest:
    did: int
    payload: bytes


def _mask_from_indices(indices: Sequence[int], *, min_index: int, max_index: int) -> int:
    mask = 0
    for n in indices:
        try:
            i = int(n)
        except Exception:
            continue
        if i < min_index or i > max_index:
            continue
        mask |= 1 << (i - min_index)
    return mask


def _encode_ip_16(dest_ip: str) -> bytes:
    ip_s = str(dest_ip or '').strip()
    if not ip_s:
        raise ValueError('missing dest_ip')

    ip = ipaddress.ip_address(ip_s)
    if isinstance(ip, ipaddress.IPv4Address):
        # Use IPv4-mapped IPv6: ::ffff:a.b.c.d (16 bytes)
        mapped = ipaddress.IPv6Address('::ffff:' + ip_s)
        return mapped.packed
    return ip.packed


def build_mirror_mode_payload(
    *,
    target_bus: Union[int, str] = 2,
    can: Optional[Sequence[int]] = None,
    flexray: Optional[Sequence[str]] = None,
    lin: Optional[Sequence[int]] = None,
    dest_ip: str,
    dest_port: int,
) -> bytes:
    """Build payload for Mirror_mode DID (0x096F for this PDX).

    Layout (from EV_Gatew31xUNECE_005032_d.odx):
    - byte 0: target bus (0=not_active, 1=data_bus_can_diagnostic, 2=data_bus_ethernet)
    - byte 1: CAN1..CAN8 enable bits (bit0..bit7)
    - byte 2: FlexRay/LIN enable bits
        - bit0: FR channel A
        - bit1: FR channel B
        - bit4: LIN1
        - bit5: LIN2
        - bit6: LIN3
    - bytes 3..18: ip_address (16 bytes)
    - bytes 19..20: port (uint16, big-endian)
    """

    target_bus_val: int
    if isinstance(target_bus, str):
        tb = target_bus.strip().lower()
        if tb in {'0', 'off', 'not_active', 'disabled', 'disable'}:
            target_bus_val = 0
        elif tb in {'1', 'can_diagnostic', 'data_bus_can_diagnostic'}:
            target_bus_val = 1
        elif tb in {'2', 'ethernet', 'data_bus_ethernet'}:
            target_bus_val = 2
        else:
            raise ValueError(f"unsupported target_bus: {target_bus}")
    else:
        target_bus_val = int(target_bus) & 0xFF

    can_list = list(can or [])
    can_mask = _mask_from_indices(can_list, min_index=1, max_index=8) & 0xFF

    fr_mask = 0
    for ch in (flexray or []):
        c = str(ch or '').strip().upper()
        if c == 'A':
            fr_mask |= 1 << 0
        elif c == 'B':
            fr_mask |= 1 << 1

    lin_list = list(lin or [])
    lin_mask = 0
    for n in lin_list:
        try:
            i = int(n)
        except Exception:
            continue
        if i == 1:
            lin_mask |= 1 << 4
        elif i == 2:
            lin_mask |= 1 << 5
        elif i == 3:
            lin_mask |= 1 << 6

    b2 = (fr_mask | lin_mask) & 0xFF

    ip16 = _encode_ip_16(dest_ip)
    port = int(dest_port) & 0xFFFF
    port_be = bytes([(port >> 8) & 0xFF, port & 0xFF])

    return bytes([target_bus_val, can_mask, b2]) + ip16 + port_be


def build_mirror_mode_write_request(
    *,
    did: int = MIRROR_MODE_DID_DEFAULT,
    target_bus: Union[int, str] = 2,
    can: Optional[Sequence[int]] = None,
    flexray: Optional[Sequence[str]] = None,
    lin: Optional[Sequence[int]] = None,
    dest_ip: str,
    dest_port: int,
) -> MirrorModeRequest:
    payload = build_mirror_mode_payload(
        target_bus=target_bus,
        can=can,
        flexray=flexray,
        lin=lin,
        dest_ip=dest_ip,
        dest_port=dest_port,
    )
    return MirrorModeRequest(did=int(did) & 0xFFFF, payload=payload)


def default_mirror_definition() -> Dict[str, object]:
    return {
        'did': f"0x{MIRROR_MODE_DID_DEFAULT:04X}",
        'did_bus_mapping': f"0x{MIRROR_MODE_BUS_MAPPING_DID_DEFAULT:04X}",
        'target_bus': {
            '0': 'not_active',
            '1': 'data_bus_can_diagnostic',
            '2': 'data_bus_ethernet',
        },
        'bits': {
            'byte1': {
                'bit0': 'CAN1',
                'bit1': 'CAN2',
                'bit2': 'CAN3',
                'bit3': 'CAN4',
                'bit4': 'CAN5',
                'bit5': 'CAN6',
                'bit6': 'CAN7',
                'bit7': 'CAN8',
            },
            'byte2': {
                'bit0': 'FR_CHANNEL_A',
                'bit1': 'FR_CHANNEL_B',
                'bit4': 'LIN1',
                'bit5': 'LIN2',
                'bit6': 'LIN3',
            },
        },
        'fields': {
            'ip_address': {'byte_offset': 3, 'length': 16, 'encoding': 'ipv6/ipv4-mapped'},
            'port': {'byte_offset': 19, 'length': 2, 'endian': 'big'},
        },
        'notes': [
            'Definition derived from EV_Gatew31xUNECE_005032_d.odx inside the active PDX.',
            'Write via UDS 0x2E (WriteDataByIdentifier).',
        ],
    }
