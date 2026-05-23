"""Regression: ScanTools DoIP actions must pause Sentinel MIL DoIP polling.

The Sentinel MIL background polling and ScanTools both use tester logical
address 0x0E00. If both run concurrently against the gateway, the older
TCP connection's routing entry is evicted and the next send fails with
EPIPE — manifesting as a post-RoutingActivation Broken-pipe loop and the
ScanTools console reporting "Discovery complete. ECUs found: 0".

To prevent this we pause Sentinel MIL polling for the duration of any
DoIP-based ScanTools action and resume it afterwards.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List

THIS = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(THIS, '..'))
BACKEND = os.path.join(ROOT, 'backend')
for p in (ROOT, BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

import vag_scanner  # noqa: E402


class _FakeSentinel:
    def __init__(self) -> None:
        self.events: List[str] = []
        self._paused = False

    def pause_doip_mil(self) -> None:
        self.events.append('pause')
        self._paused = True

    def resume_doip_mil(self) -> None:
        self.events.append('resume')
        self._paused = False


class _FakeDoIP:
    def __init__(self, gateway_ip: str, *, emit_log=None, tester_logical_address: int = 0x0E00) -> None:
        self.gateway_ip = gateway_ip
        self.emit_log = emit_log
        self.tester_logical_address = tester_logical_address

    def clear_dtcs_doip(self) -> None:
        return None

    def scan_mode06_doip(self) -> None:
        return None

    def run_scan_report(self, ecu_addresses=None):
        return ('fake_report.html', [])

    def close(self) -> None:
        return None


def _make_service_with_fake_doip(monkeypatch):
    monkeypatch.setattr(vag_scanner, 'DoIPGatewayScanner', _FakeDoIP)
    # Avoid real UDP discovery
    monkeypatch.setattr(vag_scanner, 'discover_doip_gateway_ip', lambda **_: '127.0.0.1')
    svc = vag_scanner.VAGScannerService(bus_manager=None, socketio=None)
    sentinel = _FakeSentinel()
    svc._sentinel = sentinel
    return svc, sentinel


def _wait_until_idle(svc, timeout_s=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not svc.running:
            return True
        time.sleep(0.02)
    return False


def test_doip_clear_dtcs_pauses_and_resumes_sentinel(monkeypatch):
    svc, sentinel = _make_service_with_fake_doip(monkeypatch)
    started = svc.start_action(
        channel_id=0,
        action='doip_clear_dtcs',
        params={'gateway_ip': '127.0.0.1', 'tester_logical_address': 0x0E00},
    )
    assert started
    assert _wait_until_idle(svc), 'action did not finish in time'
    # Sentinel must have been paused at the start and resumed in the finally.
    assert sentinel.events == ['pause', 'resume'], sentinel.events


def test_doip_mode06_pauses_and_resumes_sentinel(monkeypatch):
    svc, sentinel = _make_service_with_fake_doip(monkeypatch)
    assert svc.start_action(
        channel_id=0,
        action='doip_mode06',
        params={'gateway_ip': '127.0.0.1'},
    )
    assert _wait_until_idle(svc)
    assert sentinel.events == ['pause', 'resume']


def test_vag_doip_scan_report_pauses_and_resumes_sentinel(monkeypatch):
    svc, sentinel = _make_service_with_fake_doip(monkeypatch)
    assert svc.start_action(
        channel_id=0,
        action='vag_doip_scan_report',
        params={'gateway_ip': '127.0.0.1', 'ecu_addresses': [], 'tester_logical_address': 0x0E00},
    )
    assert _wait_until_idle(svc)
    assert sentinel.events == ['pause', 'resume']


def test_can_only_action_does_not_pause_sentinel(monkeypatch):
    svc, sentinel = _make_service_with_fake_doip(monkeypatch)
    # 'self_test' is not a DoIP action; pause must not be invoked.
    # Inject a stub for _run_self_test to avoid touching real CAN.
    monkeypatch.setattr(svc, '_run_self_test', lambda *a, **kw: None)
    assert svc.start_action(channel_id=0, action='self_test', params={})
    assert _wait_until_idle(svc)
    assert sentinel.events == []
