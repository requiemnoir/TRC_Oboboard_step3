"""Smoke test per DoIPActivator: build payload + file lock contention."""
from __future__ import annotations
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from doip_activator import (   # noqa: E402
    _build_mirror_did_payload,
    _doip_header,
    _gateway_doip_lock,
    _HAS_FCNTL,
)


def test_build_payload_can_only():
    pl = _build_mirror_did_payload(
        dest_ip='192.168.0.100', dest_port=30490,
        can_networks=[1, 3], flexray_channels=[], lin_networks=[],
        target_bus=2,
    )
    # byte 0: target_bus
    assert pl[0] == 2
    # byte 1: CAN mask con bit0 e bit2 set (net_id 1 e 3)
    assert pl[1] == 0b00000101
    # byte 2: FR/LIN bitmask = 0
    assert pl[2] == 0
    # byte 3-18 = IPv6 (16 byte)
    assert len(pl) == 3 + 16 + 2   # = 21
    # ultimi 2 byte: port 30490 BE
    assert pl[-2:] == (30490).to_bytes(2, 'big')


def test_build_payload_flexray_and_lin():
    pl = _build_mirror_did_payload(
        dest_ip='10.0.0.5', dest_port=12345,
        can_networks=[], flexray_channels=['A', 'B'], lin_networks=[1, 3],
        target_bus=2,
    )
    assert pl[1] == 0   # no CAN
    # byte 2 = FR_A(0x01) | FR_B(0x02) | LIN1(0x10) | LIN3(0x40) = 0x53
    assert pl[2] == 0x53


def test_doip_header_format():
    hdr = _doip_header(0x8001, 16)
    assert len(hdr) == 8
    assert hdr[0] == 0x02 and hdr[1] == 0xFD
    assert hdr[2:4] == bytes.fromhex('8001')
    assert int.from_bytes(hdr[4:8], 'big') == 16


def test_gateway_lock_contention():
    """Lock cross-process serializza: 2 thread, secondo deve essere bloccato."""
    if not _HAS_FCNTL:
        print('  skip (no fcntl, e.g. Windows)')
        return
    res = []
    def worker(name, delay=0.0):
        if delay:
            time.sleep(delay)
        try:
            with _gateway_doip_lock(timeout_s=0.0):
                res.append((name, 'acquired'))
                time.sleep(0.3)
        except BlockingIOError:
            res.append((name, 'blocked'))
    t1 = threading.Thread(target=worker, args=('A',))
    t1.start(); time.sleep(0.05)
    t2 = threading.Thread(target=worker, args=('B',))
    t2.start()
    t1.join(); t2.join()
    assert any(r[1] == 'blocked' for r in res), f'contention non rilevata: {res}'
    assert any(r[1] == 'acquired' for r in res)


if __name__ == '__main__':
    test_build_payload_can_only()
    test_build_payload_flexray_and_lin()
    test_doip_header_format()
    test_gateway_lock_contention()
    print('OK — 4 test PASS')
