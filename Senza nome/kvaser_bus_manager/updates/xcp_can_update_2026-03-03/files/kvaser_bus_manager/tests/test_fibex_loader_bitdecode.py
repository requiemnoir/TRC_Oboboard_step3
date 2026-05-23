import os
import sys

# Ensure backend is on the path (same pattern as other tests)
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))

from fibex_loader import FibexLoader


def test_extract_bits_little_vs_highlow_16bit():
    data = bytes([0x12, 0x34])
    assert FibexLoader._extract_bits(data, 0, 16, False) == 0x3412
    assert FibexLoader._extract_bits(data, 0, 16, True) == 0x1234


def test_decode_supports_over_64bit_signal():
    loader = FibexLoader()
    loader.frames[1] = "slot1"
    loader._signal_defs[1] = [
        {
            "name": "wide",
            "start_bit": 0,
            "bit_length": 72,
            "encoding": "UNSIGNED",
            "text_table": {},
            "is_high_low_byte_order": False,
        }
    ]
    out = loader.decode(1, b"\x01" + b"\x00" * 8)
    assert out is not None
    assert out["signals"]["wide"] == 1.0


def test_decode_signed_8bit():
    loader = FibexLoader()
    loader.frames[2] = "slot2"
    loader._signal_defs[2] = [
        {
            "name": "s8",
            "start_bit": 0,
            "bit_length": 8,
            "encoding": "SIGNED",
            "text_table": {},
            "is_high_low_byte_order": False,
        }
    ]
    out = loader.decode(2, b"\xFF")
    assert out is not None
    assert out["signals"]["s8"] == -1.0
