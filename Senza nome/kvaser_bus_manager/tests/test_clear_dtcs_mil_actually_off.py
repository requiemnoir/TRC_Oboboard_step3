"""End-to-end style test for the "Clear DTCs — OBD Mode 04" button flow.

Scenario: user opens the ScanTools panel with NO CAN channel selected
(Channel Configuration empty) and clicks "Clear DTCs — OBD Mode 04".

Expected behaviour after the fix:
    1. The frontend sends action=`clear_dtcs` with channel_id=0 (the
       new fallback path -- previously the frontend blocked the click
       with an alert).
    2. The backend's `/api/scantools/run` notices the CAN channel is not
       active and re-dispatches the action as `doip_clear_dtcs` (the
       built-in DoIP fallback).
    3. `DoIPGatewayScanner.clear_dtcs_doip()` issues OBD Mode $04 on the
       emissions ECU -- this is the ONLY request that actually turns the
       MIL OFF -- and the final Mode 01 PID $01 verification confirms
       MIL=OFF.

The whole flow is exercised against in-process stubs (no real network).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types
from typing import List, Optional, Tuple

import pytest


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _load_backend_app_module():
    path = os.path.join(BACKEND_DIR, 'app.py')
    spec = importlib.util.spec_from_file_location('kbsm_backend_app_clear_mil_tests', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _MemoryConfigStore:
    def __init__(self, initial=None):
        self.cfg = dict(initial or {})

    def get_config_only(self):
        return dict(self.cfg)


def _install_inactive_bus(monkeypatch, backend_app):
    fake_manager = types.SimpleNamespace(
        running=False,
        handlers={},
        bitrate_by_channel={},
        lock=_NullLock(),
        simulate_ecu=False,
        can_driver_is_mock=lambda: False,
    )
    monkeypatch.setattr(backend_app, 'manager', fake_manager)


# ---------------------------------------------------------------------------
# (1) Backend route: clear_dtcs without active CAN channel -> doip_clear_dtcs.
# ---------------------------------------------------------------------------
def test_clear_dtcs_without_can_channel_dispatches_doip_clear(monkeypatch):
    """The new frontend sends `clear_dtcs` with channel_id=0 even when no
    CAN row is configured.  The backend must transparently re-dispatch it
    as `doip_clear_dtcs` (status: 'started', mode: 'doip_fallback')."""
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    _install_inactive_bus(monkeypatch, backend_app)

    cfg = {
        'eth_settings': {
            'target_ip': '169.254.42.10',
            'interface': 'eth0',
            'doip_tester_logical_address': 0x0E00,
        },
    }
    monkeypatch.setattr(backend_app, 'config_store', _MemoryConfigStore(cfg))

    captured = {}

    class _FakeScannerService:
        def start_action(self, channel_id, act, params=None):
            captured['channel_id'] = channel_id
            captured['action'] = act
            captured['params'] = dict(params or {})
            return True

    monkeypatch.setattr(backend_app, 'scanner_service', _FakeScannerService())

    response = client.post('/api/scantools/run', json={
        'channel_id': 0,
        'action': 'clear_dtcs',
    })

    body = response.get_json()
    assert response.status_code == 200, body
    assert body == {'status': 'started', 'mode': 'doip_fallback'}

    # The backend rewrote `clear_dtcs` -> `doip_clear_dtcs` and forwarded
    # the gateway settings from app config.
    assert captured['action'] == 'doip_clear_dtcs'
    assert captured['channel_id'] == 0
    assert captured['params']['gateway_ip'] == '169.254.42.10'
    assert captured['params']['gateway_iface'] == 'eth0'


# ---------------------------------------------------------------------------
# (2) Real MIL-clear path: clear_dtcs_doip() must turn the MIL OFF.
# ---------------------------------------------------------------------------
def _make_doip_scanner(monkeypatch, *, discovered):
    """Build a DoIPGatewayScanner with networking stubbed out, the same
    way the existing test_clear_dtcs_doip.py suite does."""
    from kvaser_bus_manager.backend import vag_scanner as vs

    logs: List[str] = []
    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: logs.append(str(m)))

    monkeypatch.setattr(sc, '_connect_with_recovery', lambda: None)
    monkeypatch.setattr(sc, '_routing_activation', lambda: None)
    monkeypatch.setattr(sc, '_discover_ecus', lambda scan_list: list(discovered))
    monkeypatch.setattr(vs, '_load_active_pdx_comm_index', lambda: {})
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {})

    return sc, logs, vs


def test_clear_dtcs_doip_actually_turns_mil_off(monkeypatch):
    """Simulate an emissions ECU (engine, address 0x0010) with the MIL ON
    and 1 stored DTC.  The clear procedure must:

      * try UDS 0x14 (and accept whatever the ECU answers),
      * unconditionally fall through to OBD Mode $04 (MIL reset path),
      * verify with Mode 01 PID $01 that MIL is now OFF.

    This test fails if any of those three steps regresses, which is the
    only way the system can claim "MIL really cleared".
    """
    sc, logs, vs = _make_doip_scanner(monkeypatch, discovered=[0x0010])

    # ECU model: starts with MIL ON + 1 DTC.  After Mode $04 the ECU
    # clears its emissions store and turns the MIL OFF.
    state = {
        'mil_on': True,
        'dtc_count': 1,
        'mode04_seen': False,
    }
    seen_requests: List[Tuple[int, bytes]] = []

    def fake_uds(target_addr: int, uds_req: bytes, timeout_s: float = 1.2):
        ta = int(target_addr) & 0xFFFF
        req = bytes(uds_req)
        seen_requests.append((ta, req))

        # DiagnosticSessionControl: always positive.
        if req[:1] == b'\x10' and len(req) >= 2:
            sub = req[1]
            return bytes([0x50, sub, 0x00, 0x32, 0x01, 0xF4])

        # OBD-II powertrain ECU rejects UDS 0x14 (legislated emissions
        # store can ONLY be cleared via OBD Mode $04 per ISO 14229 Annex).
        if req[:1] == b'\x14':
            return b'\x7F\x14\x22'  # ConditionsNotCorrect

        # OBD Mode $04 — this is the request that REALLY clears the MIL.
        if req == b'\x04':
            state['mode04_seen'] = True
            state['mil_on'] = False
            state['dtc_count'] = 0
            return b'\x44'

        # ReadDTCInformation 0x19 02 0x08 (confirmed mask): no DTCs.
        if req == b'\x19\x02\x08':
            # 59 02 <availability_mask>  with empty body == no confirmed DTCs.
            return b'\x59\x02\xFF'

        # OBD Mode 01 PID $01 (Monitor status since DTCs cleared) —
        # used by the final MIL verification step.
        if req == b'\x01\x01':
            a = (0x80 if state['mil_on'] else 0x00) | (state['dtc_count'] & 0x7F)
            return bytes([0x41, 0x01, a, 0x00, 0x00, 0x00])

        return None

    monkeypatch.setattr(sc, '_uds_transact', fake_uds)

    sc.clear_dtcs_doip()

    # ── Assertions on the wire trace ───────────────────────────────
    requests_to_engine = [r for ta, r in seen_requests if ta == 0x0010]

    # Both the UDS clear ladder AND the OBD Mode $04 fallback must have run.
    assert any(r[:1] == b'\x14' for r in requests_to_engine), \
        "UDS 0x14 ClearDiagnosticInformation was never attempted"
    assert state['mode04_seen'], \
        "OBD Mode $04 (the only path that turns the MIL OFF) was not attempted"

    # Final MIL verification probe must have been issued.
    assert any(r == b'\x01\x01' for r in requests_to_engine), \
        "Final MIL verification (Mode 01 PID $01) was not issued"

    # ── Assertions on what the user sees in the ScanTools console ──
    joined = '\n'.join(logs)
    assert 'OBD Mode $04' in joined, "log does not mention the Mode $04 fallback"
    # The log line is: "DoIP: ✓ MIL is OFF (verified on ECU 0x0010, 0 stored DTC(s))."
    assert 'MIL is OFF' in joined, f"MIL was not verified OFF in logs:\n{joined}"
    assert 'MIL still ON' not in joined, f"MIL still reported ON:\n{joined}"

    # And the simulated ECU model agrees.
    assert state['mil_on'] is False
    assert state['dtc_count'] == 0


def test_clear_dtcs_doip_reports_mil_still_on_when_mode04_rejected(monkeypatch):
    """Negative control: if the ECU rejects Mode $04 too, the procedure
    must NOT lie -- it must report 'MIL still ON' so the operator knows
    the clear didn't actually work."""
    sc, logs, vs = _make_doip_scanner(monkeypatch, discovered=[0x0010])

    def fake_uds(target_addr: int, uds_req: bytes, timeout_s: float = 1.2):
        req = bytes(uds_req)
        if req[:1] == b'\x10':
            sub = req[1]
            return bytes([0x50, sub, 0x00, 0x32, 0x01, 0xF4])
        if req[:1] == b'\x14':
            return b'\x7F\x14\x22'
        if req == b'\x04':
            return b'\x7F\x04\x22'  # rejected
        if req == b'\x19\x02\x08':
            return b'\x59\x02\xFF'
        if req == b'\x01\x01':
            # MIL still ON, 1 DTC stored.
            return bytes([0x41, 0x01, 0x81, 0x00, 0x00, 0x00])
        return None

    monkeypatch.setattr(sc, '_uds_transact', fake_uds)

    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert 'MIL still ON' in joined, \
        "Procedure must explicitly report MIL still ON when Mode $04 fails"
    assert 'MIL is OFF' not in joined, \
        f"Procedure must not falsely claim MIL OFF:\n{joined}"


# ---------------------------------------------------------------------------
# (3) Full integration: HTTP request -> scanner_service -> Mode $04 -> MIL OFF.
# ---------------------------------------------------------------------------
def test_full_flow_http_clear_dtcs_clears_mil(monkeypatch):
    """Hit the real /api/scantools/run endpoint with action=clear_dtcs and
    no active CAN channel; let the real VAGScannerService run; and verify
    that the (stubbed) emissions ECU ends up with MIL OFF.

    This is the closest we can get to a real vehicle test in-process.
    """
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    _install_inactive_bus(monkeypatch, backend_app)

    cfg = {
        'eth_settings': {
            'target_ip': '169.254.42.10',
            'interface': 'eth0',
            'doip_tester_logical_address': 0x0E00,
        },
    }
    monkeypatch.setattr(backend_app, 'config_store', _MemoryConfigStore(cfg))

    # Simulated emissions ECU model.  Same model as the unit test above.
    state = {'mil_on': True, 'dtc_count': 2, 'mode04_seen': False}

    def fake_uds(self, target_addr, uds_req, timeout_s=1.2):
        req = bytes(uds_req)
        if req[:1] == b'\x10':
            return bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4])
        if req[:1] == b'\x14':
            return b'\x7F\x14\x22'
        if req == b'\x04':
            state['mode04_seen'] = True
            state['mil_on'] = False
            state['dtc_count'] = 0
            return b'\x44'
        if req == b'\x19\x02\x08':
            return b'\x59\x02\xFF'
        if req == b'\x01\x01':
            a = (0x80 if state['mil_on'] else 0x00) | (state['dtc_count'] & 0x7F)
            return bytes([0x41, 0x01, a, 0x00, 0x00, 0x00])
        return None

    from kvaser_bus_manager.backend import vag_scanner as vs

    # Stub the network primitives on the class, so any DoIPGatewayScanner
    # the service creates is fully isolated from the network.
    monkeypatch.setattr(vs.DoIPGatewayScanner, '_connect_with_recovery',
                        lambda self: None)
    monkeypatch.setattr(vs.DoIPGatewayScanner, '_routing_activation',
                        lambda self: None)
    monkeypatch.setattr(vs.DoIPGatewayScanner, 'close',
                        lambda self: None)
    monkeypatch.setattr(vs.DoIPGatewayScanner, '_discover_ecus',
                        lambda self, scan_list=None: [0x0010])
    monkeypatch.setattr(vs.DoIPGatewayScanner, '_uds_transact', fake_uds)
    monkeypatch.setattr(vs, '_load_active_pdx_comm_index', lambda: {})
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {})
    # discover_doip_gateway_ip would otherwise try a UDP broadcast.
    monkeypatch.setattr(vs, 'discover_doip_gateway_ip',
                        lambda iface=None, timeout_s=1.2: '169.254.42.10')

    # Use the real service so we exercise the real start_action thread.
    service = vs.VAGScannerService(backend_app.manager, socketio=None)
    monkeypatch.setattr(backend_app, 'scanner_service', service)

    response = client.post('/api/scantools/run', json={
        'channel_id': 0,
        'action': 'clear_dtcs',
    })
    body = response.get_json()
    assert response.status_code == 200, body
    assert body.get('status') == 'started'
    assert body.get('mode') == 'doip_fallback'

    # Wait (with timeout) for the worker thread to finish.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not service.running:
            break
        time.sleep(0.05)
    assert not service.running, "scanner_service did not finish in time"

    # Ground truth: the simulated ECU has MIL OFF and 0 DTCs.
    assert state['mode04_seen'], "OBD Mode $04 was never sent end-to-end"
    assert state['mil_on'] is False
    assert state['dtc_count'] == 0
