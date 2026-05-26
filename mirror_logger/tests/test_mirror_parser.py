"""Smoke test per MirrorParser: verifica i 4 formati supportati."""
from __future__ import annotations
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mirror_parser import MirrorParser, RawFrame   # noqa: E402


def _autosar_frame(net_type: int, net_id: int, frame_id: int, payload: bytes) -> bytes:
    """Frame entry AUTOSAR ISO 23150."""
    return struct.pack('!BBIH', net_type, net_id, frame_id & 0xFFFFFFFF, len(payload)) + payload


def _autosar_packet(seq: int, ts_us: int, entries: list) -> bytes:
    """Pacchetto completo: header 7B + entries."""
    body = struct.pack('!BIH', 0x00, ts_us & 0xFFFFFFFF, seq & 0xFFFF)
    for e in entries:
        body += e
    return bytes(body)


def test_parser_autosar_can_can_fd_flexray_lin():
    """AUTOSAR ISO 23150 multi-bus mix."""
    frames = []
    def collect(f: RawFrame): frames.append(f)
    p = MirrorParser(callback=collect)

    pkt = _autosar_packet(1, 12345, [
        _autosar_frame(0x01, 1, 0x0FD, b'\x11\x22\x33\x44\x55\x66\x77\x88'),   # CAN
        _autosar_frame(0x02, 1, 0x0A8, b'\xAA' * 16),                          # CAN-FD
        _autosar_frame(0x04, 0, 0x040, b'\x00' * 12),                          # FlexRay
        _autosar_frame(0x03, 1, 0x011, b'\x01\x02\x03\x04\x05\x06\x07\x08'),   # LIN
    ])
    n = p.parse(pkt, ts_pkt=1.0)
    assert n == 4, f'expected 4 frames, got {n}'
    assert len(frames) == 4

    types = [f.frame_type for f in frames]
    assert 'CAN' in types
    assert 'CAN-FD' in types
    assert 'FlexRay' in types
    assert 'LIN' in types

    # Channel mapping convention:
    # CAN: 100+net_id, FR: 200+net_id, LIN: 150+net_id
    for f in frames:
        if f.frame_type in ('CAN', 'CAN-FD'):
            assert 100 <= f.channel_id <= 110, f'unexpected ch {f.channel_id}'
        elif f.frame_type == 'FlexRay':
            assert 200 <= f.channel_id <= 210
        elif f.frame_type == 'LIN':
            assert 150 <= f.channel_id <= 160


def test_parser_raw_can_fallback():
    """Raw CAN-in-UDP: [arb_id:4 BE][dlc:1][data:N] ripetuto."""
    frames = []
    p = MirrorParser(callback=lambda f: frames.append(f))
    # Costruisco 3 frame CAN raw (arb_id alti, sopra le soglie autosar parser)
    body = b''
    for arb_id in (0x12345678, 0x09876543, 0x0ABCDEF0):
        body += struct.pack('!IB', arb_id, 8) + b'\x10\x20\x30\x40\x50\x60\x70\x80'

    n = p.parse(body, ts_pkt=2.0)
    assert n >= 1, f'expected at least 1 raw frame, got {n}'
    for f in frames:
        assert f.frame_type == 'CAN'
        assert f.channel_id == 99       # Iron Bird / Raw catch-all


def test_parser_invalid_too_short():
    """Pacchetto sotto la lunghezza minima → 0 frame."""
    p = MirrorParser(callback=lambda f: None)
    assert p.parse(b'', ts_pkt=0) == 0
    assert p.parse(b'\x00\x01', ts_pkt=0) == 0


def test_parser_dedupe_window():
    """Dedupe: stesso frame entro window viene scartato."""
    frames = []
    p = MirrorParser(callback=lambda f: frames.append(f), dedupe_window_s=0.5)
    entry = _autosar_frame(0x01, 1, 0x0FD, b'\x01' * 8)
    pkt = _autosar_packet(1, 12345, [entry])
    p.parse(pkt, ts_pkt=1.0)
    p.parse(pkt, ts_pkt=1.0)   # immediato → dedupe → scarta
    assert len(frames) == 1, f'dedup failed: {len(frames)} frame'


if __name__ == '__main__':
    test_parser_autosar_can_can_fd_flexray_lin()
    test_parser_raw_can_fallback()
    test_parser_invalid_too_short()
    test_parser_dedupe_window()
    print('OK — 4 test PASS')
