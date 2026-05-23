"""Tests for the /api/scantools/run endpoint DoIP bypass.

The frontend can invoke `doip_clear_dtcs` and `doip_mode06` even when no CAN
channel is active (DoIP runs over Automotive Ethernet, TCP/13400). Before the
fix, the backend rejected those actions with HTTP 409
"channel not active; start Bus System first ..." because the channel-active
check ran before the DoIP-specific branches.

These tests verify the bypass and that request params reach the scanner
service unchanged.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _load_backend_app_module():
    path = os.path.join(BACKEND_DIR, 'app.py')
    spec = importlib.util.spec_from_file_location('kbsm_backend_app_doip_clear_tests', path)
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
    """Replace the bus manager with one that has NO active channels."""
    fake_manager = types.SimpleNamespace(
        running=False,
        handlers={},               # No active channel
        bitrate_by_channel={},
        lock=_NullLock(),
        simulate_ecu=False,
        can_driver_is_mock=lambda: False,
    )
    monkeypatch.setattr(backend_app, 'manager', fake_manager)


@pytest.mark.parametrize('action', ['doip_clear_dtcs', 'doip_mode06'])
def test_scantools_doip_action_bypasses_channel_check(monkeypatch, action):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    _install_inactive_bus(monkeypatch, backend_app)

    cfg = {
        'eth_settings': {
            'target_ip': '169.254.1.5',
            'interface': 'eth0',
            'doip_auto_discover': True,
            'doip_tester_logical_address': 0x0E00,
        },
        'system_mode': 'real',
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
        'action': action,
    })

    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {'status': 'started'}

    # No CAN channel was active, but the action was dispatched anyway.
    assert captured['action'] == action
    assert captured['channel_id'] == 0
    assert captured['params']['gateway_ip'] == '169.254.1.5'
    assert captured['params']['gateway_iface'] == 'eth0'
    assert captured['params']['tester_logical_address'] == 0x0E00
    assert captured['params']['auto_discover'] is True


def test_scantools_doip_clear_request_overrides_take_precedence(monkeypatch):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    _install_inactive_bus(monkeypatch, backend_app)

    cfg = {
        'eth_settings': {
            'target_ip': '169.254.1.5',
            'interface': 'eth0',
            'doip_tester_logical_address': 0x0E00,
        },
    }
    monkeypatch.setattr(backend_app, 'config_store', _MemoryConfigStore(cfg))

    captured = {}

    class _FakeScannerService:
        def start_action(self, channel_id, act, params=None):
            captured['params'] = dict(params or {})
            return True

    monkeypatch.setattr(backend_app, 'scanner_service', _FakeScannerService())

    response = client.post('/api/scantools/run', json={
        'channel_id': 0,
        'action': 'doip_clear_dtcs',
        'gateway_ip': '10.0.0.7',
        'gateway_iface': 'eth1',
        'tester_logical_address': 0x0F00,
    })

    assert response.status_code == 200
    assert captured['params']['gateway_ip'] == '10.0.0.7'
    assert captured['params']['gateway_iface'] == 'eth1'
    assert captured['params']['tester_logical_address'] == 0x0F00


def test_scantools_can_action_still_requires_active_channel(monkeypatch):
    """Sanity check: non-DoIP actions still get the channel-active guard."""
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    _install_inactive_bus(monkeypatch, backend_app)
    monkeypatch.setattr(backend_app, 'config_store', _MemoryConfigStore({}))

    class _FakeScannerService:
        def start_action(self, *a, **kw):
            raise AssertionError('must not be called when channel is inactive')

    monkeypatch.setattr(backend_app, 'scanner_service', _FakeScannerService())

    response = client.post('/api/scantools/run', json={
        'channel_id': 0,
        'action': 'scan_obd',
    })

    assert response.status_code == 409
    body = response.get_json()
    assert 'channel not active' in (body.get('error') or '')
