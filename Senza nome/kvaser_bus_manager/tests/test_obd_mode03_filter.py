import pytest

from kvaser_bus_manager.backend.vag_scanner import VAGScanner


def _make_scanner():
    # Avoid VAGScanner.__init__ because it requires a BusManager with listeners.
    # We only need the pure parsing helper.
    return VAGScanner.__new__(VAGScanner)


def test_parse_mode03_filters_padding_and_zero():
    s = _make_scanner()

    # 43 + pairs: 00 00 (none), AA AA (padding), FF FF (padding), 01 0C (P010C)
    payload = bytes([0x43, 0x00, 0x00, 0xAA, 0xAA, 0xFF, 0xFF, 0x01, 0x0C])
    dtcs = s._parse_obd_mode03(payload)

    assert [d.code for d in dtcs] == ["P010C"]


def test_parse_mode03_filters_placeholder_like_p00aa_pattern():
    s = _make_scanner()

    # This would decode to P00AA (commonly observed as filler)
    payload = bytes([0x43, 0x00, 0xAA])
    dtcs = s._parse_obd_mode03(payload)

    assert dtcs == []
