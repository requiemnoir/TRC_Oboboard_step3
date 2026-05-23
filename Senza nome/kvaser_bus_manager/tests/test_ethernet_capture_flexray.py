import os
import struct
import sys


BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


from ethernet_capture import EthernetCapture


class _NullLogger:
    def log(self, *_args, **_kwargs):
        return None


def test_vag_mirror_ignores_flexray_candidates_with_invalid_cycle():
    emitted = []

    def _mirror_callback(**kwargs):
        emitted.append(kwargs)

    capture = EthernetCapture("lo", _NullLogger(), mirror_callback=_mirror_callback)

    payload = bytearray(4)
    payload.extend(b"\x00\x00")
    payload.append(1)
    payload.append(0)
    payload.extend(b"\x00\x00")
    payload.extend(bytes([8, 200]))
    payload.extend(b"\x00\x08")
    payload.extend(b"\x00" * 8)

    assert capture._try_unpack_vag_mirror(bytes(payload)) is False
    assert emitted == []


def _build_vag_flexray_entry(*, bus_ch: int, slot_id: int, cycle: int, data: bytes) -> bytes:
    payload = bytes(data)
    return (
        struct.pack("!HBBH", 0, bus_ch, 0, 0)
        + bytes([slot_id & 0xFF, cycle & 0xFF])
        + struct.pack("!H", len(payload))
        + payload
    )


def test_vag_mirror_scans_past_large_unknown_block_for_flexray_records():
    emitted = []

    def _mirror_callback(**kwargs):
        emitted.append(kwargs)

    capture = EthernetCapture("lo", _NullLogger(), mirror_callback=_mirror_callback)

    payload = bytearray(4)
    payload.extend(
        _build_vag_flexray_entry(
            bus_ch=1,
            slot_id=29,
            cycle=3,
            data=bytes([0x11] * 34),
        )
    )
    payload.extend(bytes([0xFF] * 400))
    payload.extend(
        _build_vag_flexray_entry(
            bus_ch=1,
            slot_id=33,
            cycle=7,
            data=bytes([0x22] * 34),
        )
    )

    assert capture._try_unpack_vag_mirror(bytes(payload)) is True
    assert [(row["frame_type"], row["arb_id"], row["channel_id"], row["flags"]) for row in emitted] == [
        ("FlexRay", 29, 201, 3),
        ("FlexRay", 33, 201, 7),
    ]