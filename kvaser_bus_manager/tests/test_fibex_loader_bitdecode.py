import os
import sys
from math import ceil

import pytest

# Ensure backend is on the path (same pattern as other tests)
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))

from fibex_loader import FibexLoader
from bus_manager import BusManager


FIBEX_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "databases",
        "fibex",
        "MLBevo_Gen2_Fx_Cluster_KMatrix_V8.24.00F_20220602_SEn.xml",
    )
)


def _set_little_endian_bits(buf: bytearray, start_bit: int, bit_length: int, value: int) -> None:
    for i in range(bit_length):
        bit_index = start_bit + i
        byte_index = bit_index >> 3
        bit_in_byte = bit_index & 7
        bit = (int(value) >> i) & 1
        if bit:
            buf[byte_index] |= (1 << bit_in_byte)
        else:
            buf[byte_index] &= ~(1 << bit_in_byte)


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


def test_decode_skips_pdu_signals_when_update_bit_is_clear():
    loader = FibexLoader()
    loader.frames[7] = "slot7"
    loader._variants[7] = [
        {
            "name": "variant slot7",
            "base_cycle": 3,
            "cycle_repetition": 64,
            "signal_defs": [
                {
                    "name": "sig_a",
                    "start_bit": 0,
                    "bit_length": 8,
                    "encoding": "UNSIGNED",
                    "text_table": {},
                    "is_high_low_byte_order": False,
                    "pdu_update_bit_position": 15,
                },
                {
                    "name": "sig_b",
                    "start_bit": 16,
                    "bit_length": 8,
                    "encoding": "UNSIGNED",
                    "text_table": {},
                    "is_high_low_byte_order": False,
                    "pdu_update_bit_position": 31,
                },
            ],
        }
    ]
    loader._signal_defs[7] = list(loader._variants[7][0]["signal_defs"])

    payload = bytearray(4)
    _set_little_endian_bits(payload, 0, 8, 42)
    _set_little_endian_bits(payload, 16, 8, 77)

    out = loader.decode(7, payload, cycle=3)
    assert out is not None
    assert out["name"] == "slot7"
    assert "sig_a" not in out["signals"]
    assert "sig_b" not in out["signals"]

    _set_little_endian_bits(payload, 15, 1, 1)
    out = loader.decode(7, payload, cycle=3)
    assert out is not None
    assert out["signals"]["sig_a"] == 42.0
    assert "sig_b" not in out["signals"]

    _set_little_endian_bits(payload, 31, 1, 1)
    out = loader.decode(7, payload, cycle=3)
    assert out is not None
    assert out["signals"]["sig_a"] == 42.0
    assert out["signals"]["sig_b"] == 77.0


def test_decode_with_unmatched_cycle_falls_back_to_merged_defs():
    # When a cycle counter from the mirror does not match any FIBEX variant, the
    # decoder must fall back to the merged _signal_defs rather than returning an
    # empty signal set.  Returning nothing silently drops the frame from the
    # decoded MF4 and creates artificial gaps in the logged FlexRay signals.
    loader = FibexLoader()
    loader.frames[8] = "slot8"
    loader._variants[8] = [
        {
            "name": "variant 0/8",
            "base_cycle": 0,
            "cycle_repetition": 8,
            "signal_defs": [
                {
                    "name": "ZAS_Kl_15",
                    "start_bit": 17,
                    "bit_length": 1,
                    "encoding": "UNSIGNED",
                    "text_table": {0: "aus", 1: "ein"},
                    "is_high_low_byte_order": False,
                    "pdu_update_bit_position": 271,
                }
            ],
        }
    ]
    loader._signal_defs[8] = list(loader._variants[8][0]["signal_defs"])

    payload = bytearray(34)
    _set_little_endian_bits(payload, 17, 1, 1)
    _set_little_endian_bits(payload, 271, 1, 1)

    # Cycle 0 matches variant 0/8 exactly.
    out = loader.decode(8, payload, cycle=0)
    assert out is not None
    assert out["signals"]["ZAS_Kl_15"] == 1.0

    # Cycle 7 does NOT match variant 0/8 (7 % 8 != 0), so the decoder falls back
    # to the merged _signal_defs.  The signal must still be present to avoid gaps.
    out = loader.decode(8, payload, cycle=7)
    assert out is not None
    assert "ZAS_Kl_15" in out["signals"], (
        "Expected ZAS_Kl_15 to be decoded via merged-defs fallback when cycle "
        "does not match any variant (was silently dropped before this fix)"
    )
    assert out["signals"]["ZAS_Kl_15"] == 1.0


def test_bus_manager_masks_flexray_cycle_to_6_bits():
    class _Logger:
        def log(self, *_args, **_kwargs):
            return None

    manager = BusManager(socketio=None, logger=_Logger())
    manager.fibex.frames[8] = "slot8"
    manager.fibex._variants[8] = [
        {
            "name": "variant 0/8",
            "base_cycle": 0,
            "cycle_repetition": 8,
            "signal_defs": [
                {
                    "name": "ZAS_Kl_15",
                    "start_bit": 17,
                    "bit_length": 1,
                    "encoding": "UNSIGNED",
                    "text_table": {0: "aus", 1: "ein"},
                    "is_high_low_byte_order": False,
                    "pdu_update_bit_position": 271,
                }
            ],
        }
    ]
    manager.fibex._signal_defs[8] = list(manager.fibex._variants[8][0]["signal_defs"])

    seen = []
    manager.add_listener(lambda frame: seen.append(frame))

    payload = bytearray(34)
    _set_little_endian_bits(payload, 17, 1, 1)
    _set_little_endian_bits(payload, 271, 1, 1)

    manager.inject_frame(channel_id=200, arb_id=8, data=payload, flags=0x40, frame_type="FlexRay")

    assert seen
    decoded = seen[-1].get("decoded")
    assert isinstance(decoded, dict)
    assert decoded["signals"]["ZAS_Kl_15"] == 1.0


@pytest.fixture(scope="module")
def real_fibex_loader():
    if not os.path.exists(FIBEX_PATH):
        pytest.skip(f"FIBEX file not found: {FIBEX_PATH}")
    loader = FibexLoader()
    assert loader.load(FIBEX_PATH)
    return loader


def test_real_fibex_multiplexed_variants_honor_update_bits(real_fibex_loader):
    checked = 0

    for slot_id, variants in sorted(real_fibex_loader._variants.items()):
        if len(variants) < 2:
            continue

        for variant in variants:
            grouped = {}
            for signal_def in variant.get("signal_defs", []):
                update_bit = signal_def.get("pdu_update_bit_position")
                if update_bit is None:
                    continue
                grouped.setdefault(int(update_bit), []).append(signal_def)

            if not grouped:
                continue

            update_bit, signal_defs = sorted(grouped.items(), key=lambda item: item[0])[0]
            probe = next(
                (
                    signal_def
                    for signal_def in signal_defs
                    if not bool(signal_def.get("is_high_low_byte_order", False))
                ),
                None,
            )
            if probe is None:
                continue

            max_bit = update_bit + 1
            for signal_def in signal_defs:
                end_bit = int(signal_def.get("start_bit") or 0) + int(signal_def.get("bit_length") or 0)
                max_bit = max(max_bit, end_bit)
            payload = bytearray(ceil(max_bit / 8))

            out = real_fibex_loader.decode(slot_id, payload, cycle=int(variant.get("base_cycle") or 0))
            assert out is not None
            # decode() now returns the slot-stable name (self.frames[slot_id])
            # rather than the cycle-variant-specific name.
            assert out["name"] == real_fibex_loader.frames[slot_id]
            assert probe["name"] not in out["signals"]

            _set_little_endian_bits(payload, update_bit, 1, 1)
            out = real_fibex_loader.decode(slot_id, payload, cycle=int(variant.get("base_cycle") or 0))
            assert out is not None
            assert out["name"] == real_fibex_loader.frames[slot_id]
            assert probe["name"] in out["signals"]

            checked += 1
            if checked >= 8:
                break

        if checked >= 8:
            break

    assert checked >= 8
