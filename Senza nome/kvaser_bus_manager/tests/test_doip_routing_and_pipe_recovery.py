"""Tests for DoIP routing-activation response-code validation and EPIPE
recovery in DoIPGatewayScanner._doip_send.

These reproduce the failure observed in the field:

    DoIP: Routing activation request (activation_type=0x00, tester=0x0E00)
    DoIP: Routing activation request (activation_type=0x01, tester=0x0E00)
    DoIP: Routing activation response received
    ...
    ScanTools failed: [Errno 32] Broken pipe

Two regressions are covered:
1. Routing Activation Response payloads with response_code != 0x10 must NOT
   be treated as success (and the socket must be reconnected before the next
   attempt).
2. A BrokenPipeError on _doip_send for a non-RoutingActivation frame must
   trigger one transparent reconnect + re-activation + resend.
"""
from __future__ import annotations

import socket
import struct
from typing import List, Optional, Tuple

import pytest

from kvaser_bus_manager.backend import vag_scanner as vs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ra_response(tester_la: int, entity_la: int, code: int) -> bytes:
    """Build an ISO 13400-2 Routing Activation Response payload."""
    # SA(2) + TA(2) + RoutingActivationResponseCode(1) + Reserved(4)
    return struct.pack('!HHB', tester_la & 0xFFFF, entity_la & 0xFFFF, code & 0xFF) + b'\x00\x00\x00\x00'


# ---------------------------------------------------------------------------
# 1) Routing activation response code must be checked.
# ---------------------------------------------------------------------------
def test_routing_activation_rejects_non_0x10_response_code(monkeypatch):
    """A response with code != 0x10 must NOT be considered success."""
    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: None)

    sends: List[Tuple[int, bytes]] = []
    reconnects = {'n': 0}

    def fake_send(ptype, payload):
        sends.append((int(ptype) & 0xFFFF, bytes(payload)))

    def fake_recv(timeout_s=1.0):
        # Always reply with refusal code 0x00 ("Routing activation denied:
        # unknown source address").
        return 0x0006, _ra_response(0x0E00, 0x1000, 0x00)

    def fake_connect_with_recovery():
        reconnects['n'] += 1
        # Pretend we have a socket so subsequent code paths don't bail.
        sc.sock = object()  # type: ignore[assignment]

    monkeypatch.setattr(sc, '_doip_send', fake_send)
    monkeypatch.setattr(sc, '_doip_recv', fake_recv)
    monkeypatch.setattr(sc, '_connect_with_recovery', fake_connect_with_recovery)
    monkeypatch.setattr(sc, 'close', lambda: None)

    with pytest.raises(RuntimeError):
        sc._routing_activation()

    # All combinations should have been attempted (act_types x testers).
    assert len(sends) >= 2
    # And the gateway socket should have been reconnected after each refusal
    # (defensive: prevents broken-pipe on the next _doip_send).
    assert reconnects['n'] >= 1


def test_routing_activation_accepts_0x10_response_code(monkeypatch):
    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: None)

    sends: List[Tuple[int, bytes]] = []

    def fake_send(ptype, payload):
        sends.append((int(ptype) & 0xFFFF, bytes(payload)))

    def fake_recv(timeout_s=1.0):
        return 0x0006, _ra_response(0x0E00, 0x1000, 0x10)  # success

    monkeypatch.setattr(sc, '_doip_send', fake_send)
    monkeypatch.setattr(sc, '_doip_recv', fake_recv)
    monkeypatch.setattr(sc, '_connect_with_recovery', lambda: None)
    monkeypatch.setattr(sc, 'close', lambda: None)

    sc._routing_activation()  # Must NOT raise.
    assert len(sends) == 1   # First attempt accepted.


# ---------------------------------------------------------------------------
# 2) _doip_send must transparently recover from BrokenPipeError on non-RA
#    frames (i.e. UDS diagnostic message 0x8001) by reconnecting and
#    re-activating routing.
# ---------------------------------------------------------------------------
class _PipeBreakSocket:
    """Fake socket that raises BrokenPipeError on the first sendall(),
    then succeeds on subsequent sendall() calls."""

    def __init__(self):
        self.calls = 0
        self.sent: List[bytes] = []

    def sendall(self, data):
        self.calls += 1
        if self.calls == 1:
            raise BrokenPipeError(32, 'Broken pipe')
        self.sent.append(bytes(data))

    def close(self):
        pass

    def settimeout(self, *_):
        pass


def test_doip_send_recovers_from_broken_pipe_on_uds_frame(monkeypatch):
    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: None)
    sc.sock = _PipeBreakSocket()

    state = {'reconnect': 0, 'reactivate': 0}

    def fake_close():
        # Don't kill our fake socket — we want to inspect it after the retry.
        pass

    def fake_connect_with_recovery():
        state['reconnect'] += 1
        # After "reconnect", the original fake socket succeeds on the next sendall.

    def fake_routing_activation():
        state['reactivate'] += 1

    monkeypatch.setattr(sc, 'close', fake_close)
    monkeypatch.setattr(sc, '_connect_with_recovery', fake_connect_with_recovery)
    monkeypatch.setattr(sc, '_routing_activation', fake_routing_activation)

    # Send a diagnostic message frame (0x8001) and verify the retry path.
    sc._doip_send(0x8001, b'\x0E\x00\x10\x00\x14\xFF\xFF\xFF')

    assert state['reconnect'] == 1
    assert state['reactivate'] == 1
    assert isinstance(sc.sock, _PipeBreakSocket)
    assert len(sc.sock.sent) == 1


def test_doip_send_does_not_recurse_on_routing_activation_frame(monkeypatch):
    """If the first frame (Routing Activation 0x0005) hits EPIPE, we must
    raise instead of recursing into routing activation."""
    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: None)
    sc.sock = _PipeBreakSocket()

    monkeypatch.setattr(sc, 'close', lambda: None)
    # If recovery is wrongly attempted, this would be called — fail loudly.
    monkeypatch.setattr(sc, '_routing_activation', lambda: pytest.fail('must not recurse'))
    monkeypatch.setattr(sc, '_connect_with_recovery', lambda: pytest.fail('must not reconnect'))

    with pytest.raises(BrokenPipeError):
        sc._doip_send(0x0005, b'\x00' * 11)
