"""Unit tests for DoIPGatewayScanner.clear_dtcs_doip().

These tests do NOT touch the network. They monkey-patch the helpers that talk
to the DoIP gateway (`_connect_with_recovery`, `_routing_activation`,
`_discover_ecus`, `_uds_transact`, `_load_active_pdx_comm_index`) so we can
verify the new state-machine: ExtendedSession (0x10 0x03) before
ClearDiagnosticInformation (0x14 FF FF FF), with retry on session-related NRCs
and proper NRC translation.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pytest

from kvaser_bus_manager.backend import vag_scanner as vs


def _make_scanner(monkeypatch, *, discovered: List[int]) -> Tuple[vs.DoIPGatewayScanner, List[str], List[Tuple[int, bytes]]]:
    """Build a DoIPGatewayScanner with all networking stubbed out.

    Returns the scanner, the list it logs into, and the list of UDS calls
    captured for later assertions.
    """
    logs: List[str] = []
    calls: List[Tuple[int, bytes]] = []

    sc = vs.DoIPGatewayScanner('127.0.0.1', emit_log=lambda m: logs.append(str(m)))

    # Stub network primitives.
    monkeypatch.setattr(sc, '_connect_with_recovery', lambda: None)
    monkeypatch.setattr(sc, '_routing_activation', lambda: None)
    monkeypatch.setattr(sc, '_discover_ecus', lambda scan_list: list(discovered))
    # Force PDX path empty so we don't depend on a loaded PDX.
    monkeypatch.setattr(vs, '_load_active_pdx_comm_index', lambda: {})
    # Empty per-ECU clear profile -> function falls back to the conservative
    # default ladder ([14 FF FF FF, 14 FF FF 33] in ExtendedSession).
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {})

    return sc, logs, calls


def _install_uds(monkeypatch, sc, responder):
    """Install a fake _uds_transact that records calls and delegates to ``responder``."""
    calls: List[Tuple[int, bytes]] = []

    def fake_uds(target_addr: int, uds_req: bytes, timeout_s: float = 1.2) -> Optional[bytes]:
        calls.append((int(target_addr) & 0xFFFF, bytes(uds_req)))
        return responder(int(target_addr) & 0xFFFF, bytes(uds_req))

    monkeypatch.setattr(sc, '_uds_transact', fake_uds)
    return calls


# ---------------------------------------------------------------------------
# Happy path: ExtendedSession OK + ClearDTC OK
# ---------------------------------------------------------------------------
def test_clear_happy_path_enters_extended_then_clears(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x0001, 0x0002])

    def responder(ta: int, req: bytes) -> Optional[bytes]:
        if req == b'\x10\x03':
            # Positive response to DiagnosticSessionControl(extended)
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\xFF':
            return b'\x54'
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    # Filter out the post-clear MIL verification probes (Mode 01 PID 01).
    clear_calls = [c for c in calls if c[1][:1] in (b'\x10', b'\x14', b'\x04')]
    # Expect 2 ECUs, each: 1x session control, 1x clear  -> 4 calls total.
    assert len(clear_calls) == 4
    assert clear_calls[0] == (0x0001, b'\x10\x03')
    assert clear_calls[1] == (0x0001, b'\x14\xFF\xFF\xFF')
    assert clear_calls[2] == (0x0002, b'\x10\x03')
    assert clear_calls[3] == (0x0002, b'\x14\xFF\xFF\xFF')

    joined = '\n'.join(logs)
    assert 'Clear OK' in joined
    assert 'Cleared 2/2' in joined


# ---------------------------------------------------------------------------
# Payload conformance: must be exactly 14 FF FF FF (per PDX
# Req_ClearDiagnInfor + DOP_TEXTTABLEGroupOfDTCs "All Groups")
# ---------------------------------------------------------------------------
def test_clear_request_payload_matches_pdx(monkeypatch):
    sc, _, _ = _make_scanner(monkeypatch, discovered=[0x0010])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        return b'\x54'

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    clear_calls = [r for _, r in calls if r[:1] == b'\x14']
    assert clear_calls == [b'\x14\xFF\xFF\xFF']


# ---------------------------------------------------------------------------
# Session-related NRC must trigger trying the next variant in the ladder.
# Scenario: ExtendedSession is "lost" the first time (no response), Clear
# gets NRC 0x7F. The function should move on to the next variant
# (14 FF FF 33 / WWH-OBD Emissions) which the ECU accepts.
# ---------------------------------------------------------------------------
def test_clear_falls_back_to_wwhobd_on_session_nrc(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x0042])

    state = {'session_attempt': 0}

    def responder(ta, req):
        if req == b'\x10\x03':
            state['session_attempt'] += 1
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\xFF':
            # 7F 14 7F = Service not supported in active session
            return b'\x7F\x14\x7F'
        if req == b'\x14\xFF\xFF\x33':
            # ECU accepts the WWH-OBD emissions group
            return b'\x54'
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    seq = [r for _, r in calls if r[:1] in (b'\x10', b'\x14', b'\x04')]
    # First variant: 10 03 + 14 FFFFFF (NRC). Second variant: 14 FFFF33 (OK,
    # session already current so no second 10 03).
    assert seq == [b'\x10\x03', b'\x14\xFF\xFF\xFF', b'\x14\xFF\xFF\x33']
    joined = '\n'.join(logs)
    assert 'Service not supported in active session' in joined
    assert 'WWH-OBD Clear Emissions' in joined
    assert 'Cleared 1/1' in joined


# ---------------------------------------------------------------------------
# NRC that is not session-related must NOT trigger a retry; must be logged
# with the human-readable name from the PDX NRC table.
# ---------------------------------------------------------------------------
def test_clear_non_session_nrc_is_not_retried(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x0007])

    state = {'session_attempt': 0, 'clear_attempt': 0}

    def responder(ta, req):
        if req == b'\x10\x03':
            state['session_attempt'] += 1
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\xFF':
            state['clear_attempt'] += 1
            # 0x33 = Security access denied (not in retry set)
            return b'\x7F\x14\x33'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    # Exactly one session control + one clear (no retry, no fallback variant
    # because NRC 0x33 is fatal — security access required).
    assert state['session_attempt'] == 1
    assert state['clear_attempt'] == 1
    joined = '\n'.join(logs)
    assert 'NRC=0x33' in joined
    assert 'Security access denied' in joined
    assert 'Cleared 0/1' in joined


# ---------------------------------------------------------------------------
# ECU that doesn't support extended session at all (NRC 0x12 to 0x10 0x03)
# must still attempt the clear in the current session.
# ---------------------------------------------------------------------------
def test_clear_when_extended_unsupported_falls_back(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x0033])

    def responder(ta, req):
        if req == b'\x10\x03':
            # 7F 10 12 = Sub-function not supported
            return b'\x7F\x10\x12'
        if req == b'\x14\xFF\xFF\xFF':
            return b'\x54'
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    # We must have tried session once, then clear once. No retry because the
    # clear succeeded.
    seq = [r for _, r in calls if r[:1] in (b'\x10', b'\x14', b'\x04')]
    assert seq == [b'\x10\x03', b'\x14\xFF\xFF\xFF']
    joined = '\n'.join(logs)
    # Session refused but we proceeded with the variant anyway.
    assert 'Clear OK' in joined
    assert 'Cleared 1/1' in joined


# ---------------------------------------------------------------------------
# No ECU discovered: function must return early without raising.
# ---------------------------------------------------------------------------
def test_clear_no_ecu_discovered(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[])
    called = {'n': 0}

    def fake_uds(*a, **kw):
        called['n'] += 1
        return None

    monkeypatch.setattr(sc, '_uds_transact', fake_uds)
    sc.clear_dtcs_doip()

    assert called['n'] == 0
    assert any('No ECUs discovered' in m for m in logs)


# ---------------------------------------------------------------------------
# Missing gateway IP must raise ValueError before any I/O.
# ---------------------------------------------------------------------------
def test_clear_missing_gateway_raises():
    sc = vs.DoIPGatewayScanner('')
    with pytest.raises(ValueError):
        sc.clear_dtcs_doip()


# ===========================================================================
# Per-ECU ClearDiagnosticInformation profile parser
# ===========================================================================


def test_derive_clear_variants_standard_uds_ecu():
    """ECU inherits FG_AllUDSSyste cleanly and FG_AllOBDSyste cleanly:
    must offer both UDS Clear All and WWH-OBD Clear Emissions, in that order.
    """
    parsed = {
        'parents': ['FG_AllUDSSyste', 'FG_AllOBDSyste'],
        'not_inherited': {'FG_AllUDSSyste': set(), 'FG_AllOBDSyste': set()},
    }
    variants = vs._derive_clear_variants(parsed)
    groups = [v['group'] for v in variants]
    assert groups[0] == b'\xFF\xFF\xFF'
    assert b'\xFF\xFF\x33' in groups


def test_derive_clear_variants_engine_ecu_excludes_uds_clear():
    """Engine ECU NOT-INHERITs DiagnServi_ClearDiagnInfor from FG_AllUDSSyste
    but inherits DiagnServi_ClearDiagnInforWWHOBD from FG_AllOBDSyste:
    WWH-OBD (FFFF33) must come first.
    """
    parsed = {
        'parents': ['FG_AllUDSSyste', 'FG_AllOBDSyste'],
        'not_inherited': {
            'FG_AllUDSSyste': {'DiagnServi_ClearDiagnInfor'},
            'FG_AllOBDSyste': set(),
        },
    }
    variants = vs._derive_clear_variants(parsed)
    assert variants[0]['group'] == b'\xFF\xFF\x33'
    # Fallback "FFFFFF" still appended
    assert any(v['group'] == b'\xFF\xFF\xFF' for v in variants)


def test_derive_clear_variants_no_parents_uses_default_session_fallback():
    """ECU without recognised parents (e.g. tiny stub ODX) must still receive
    both clear-group attempts as best-effort fallbacks (in ExtendedSession)."""
    parsed = {'parents': [], 'not_inherited': {}}
    variants = vs._derive_clear_variants(parsed)
    groups = sorted(v['group'] for v in variants)
    assert groups == [b'\xFF\xFF\x33', b'\xFF\xFF\xFF']
    # Sessions should be valid (currently 0x03 ExtendedSession for safety).
    assert all(v['session'] in (0x01, 0x03) for v in variants)


def test_parse_odx_clear_capabilities_extracts_not_inherited():
    """Smoke test of the ODX-text parser against a tiny synthetic snippet."""
    odx = """
    <ROOT>
      <PARENT-REF ID-REF="FG_AllUDSSyste" DOCREF="FG_AllUDSSyste" DOCTYPE="LAYER">
        <NOT-INHERITED-DIAG-COMMS>
          <NOT-INHERITED-DIAG-COMM>
            <DIAG-COMM-SNREF SHORT-NAME="DiagnServi_ClearDiagnInfor"/>
          </NOT-INHERITED-DIAG-COMM>
          <NOT-INHERITED-DIAG-COMM>
            <DIAG-COMM-SNREF SHORT-NAME="DiagnServi_ECUResetSoftReset"/>
          </NOT-INHERITED-DIAG-COMM>
        </NOT-INHERITED-DIAG-COMMS>
      </PARENT-REF>
      <PARENT-REF ID-REF="FG_AllOBDSyste" DOCREF="FG_AllOBDSyste" DOCTYPE="LAYER">
      </PARENT-REF>
    </ROOT>
    """
    parsed = vs._parse_odx_clear_capabilities(odx)
    assert parsed['parents'] == ['FG_AllUDSSyste', 'FG_AllOBDSyste']
    assert parsed['not_inherited']['FG_AllUDSSyste'] == {
        'DiagnServi_ClearDiagnInfor', 'DiagnServi_ECUResetSoftReset'
    }
    assert parsed['not_inherited']['FG_AllOBDSyste'] == set()


def test_mil_candidate_detection_ignores_generic_ffff33_fallback():
    profile = {
        'short_name': 'DLC_BV_TirePressMonit1UDS',
        'parents': ['FG_AllUDSSyste'],
        'variants': [
            {'sid': 0x14, 'group_hex': 'FFFFFF', 'label': 'UDS Clear All (FFFFFF)', 'session': 0x03},
            {'sid': 0x14, 'group_hex': 'FFFF33', 'label': 'WWH-OBD Clear Emissions (FFFF33, fallback)', 'session': 0x03},
        ],
    }
    assert not vs._pdx_profile_is_obd_mil_candidate(profile)


def test_mil_candidate_detection_uses_pdx_emissions_parent():
    profile = {
        'short_name': 'DLC_BV_EnginContrModul1UDS',
        'parents': ['FG_AllEmissRelatUDSSyste', 'FG_AllOBDSyste', 'FG_AllUDSSyste'],
        'variants': [],
    }
    assert vs._pdx_profile_is_obd_mil_candidate(profile)
    assert vs._pdx_profile_mil_priority(profile) == 0


# ---------------------------------------------------------------------------
# Profile-driven per-ECU clear: an engine ECU listed in the profile as
# WWH-OBD-only must skip 0x14 FF FF FF entirely.
# ---------------------------------------------------------------------------
def test_clear_uses_profile_variants_per_ecu(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4076])

    # Override the profile loader so 0x4076 maps to WWH-OBD-only.
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {
        0x4076: {
            'short_name': 'DLC_BV_EnginContrModul1UDS',
            'variants': [
                {'sid': 0x14, 'group_hex': 'FFFF33',
                 'label': 'WWH-OBD Clear Emissions (FFFF33)', 'session': 0x03},
            ],
        }
    })

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x10\x01':
            return b'\x50\x01\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\x33':
            return b'\x54'
        if req == b'\x04':
            return b'\x44'
        # Generic 14 FF FF FF would be NRC 0x11 on a real engine ECU; if our
        # implementation accidentally sends it, fail loudly.
        if req == b'\x14\xFF\xFF\xFF':
            raise AssertionError('Engine ECU must not be sent 14 FF FF FF')
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    seq = [r for _, r in calls if r[:1] in (b'\x10', b'\x14', b'\x04')]
    assert seq == [b'\x10\x03', b'\x14\xFF\xFF\x33', b'\x10\x01', b'\x04']
    joined = '\n'.join(logs)
    assert 'DLC_BV_EnginContrModul1UDS' in joined
    assert 'WWH-OBD Clear Emissions' in joined
    assert 'MIL reset OK (OBD Mode $04, PDX emissions/MIL ECU)' in joined
    assert 'Cleared 1/1' in joined


def test_obd_mode04_fallback_enters_default_session_first(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4076])
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {
        0x4076: {
            'short_name': 'DLC_BV_EnginContrModul1UDS',
            'parents': ['FG_AllEmissRelatUDSSyste', 'FG_AllOBDSyste', 'FG_AllUDSSyste'],
            'variants': [
                {'sid': 0x14, 'group_hex': 'FFFFFF',
                 'label': 'UDS Clear All (FFFFFF, fallback)', 'session': 0x03},
                {'sid': 0x14, 'group_hex': 'FFFF33',
                 'label': 'WWH-OBD Clear Emissions (FFFF33, fallback)', 'session': 0x03},
            ],
        }
    })

    state = {'default_entered': False}

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x10\x01':
            state['default_entered'] = True
            return b'\x50\x01\x00\x32\x01\xF4'
        if req[:1] == b'\x14':
            return b'\x7F\x14\x11'
        if req == b'\x04':
            return b'\x44' if state['default_entered'] else b'\x7F\x04\x22'
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    seq = [r for _, r in calls if r[:1] in (b'\x10', b'\x14', b'\x04')]
    assert seq[-2:] == [b'\x10\x01', b'\x04']
    joined = '\n'.join(logs)
    assert 'MIL reset OK (OBD Mode $04, final fallback)' in joined
    assert 'Cleared 1/1' in joined


def test_clear_profile_logs_short_name_tag(monkeypatch):
    """The per-ECU short_name from the profile must appear in the log lines."""
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x40B7])
    monkeypatch.setattr(vs, '_load_active_pdx_clear_profile', lambda: {
        0x40B7: {
            'short_name': 'DLC_BV_DCDCConveContrModulHVUDS',
            'variants': [
                {'sid': 0x14, 'group_hex': 'FFFFFF',
                 'label': 'UDS Clear All (FFFFFF, fallback)', 'session': 0x03},
            ],
        }
    })

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        return b'\x54'

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()
    joined = '\n'.join(logs)
    assert 'DLC_BV_DCDCConveContrModulHVUDS' in joined
    assert 'Cleared 1/1' in joined


# ---------------------------------------------------------------------------
# OBD Mode $04 fallback: when every UDS 0x14 variant fails (e.g. a powertrain
# ECU rejects Clear with NRC 0x33 or 0x11), the function MUST fall back to
# the OBD-II Service $04 single-byte request which is the only one that
# actually turns the MIL OFF on emissions ECUs.
# ---------------------------------------------------------------------------
def test_clear_falls_back_to_obd_mode04_when_uds_clear_rejected(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4076])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req[:1] == b'\x14':
            # Engine ECU: legislated emissions store cannot be cleared via UDS.
            # NRC 0x11 (Service Not Supported) — exhausts every variant.
            return b'\x7F\x14\x11'
        if req == b'\x04':
            # OBD-II Mode $04: positive response is 0x44 (no payload).
            return b'\x44'
        return None

    calls = _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    # Every UDS clear variant must have failed AND the OBD Mode $04 fallback
    # must have been issued exactly once for this ECU.
    assert any(req == b'\x04' for _, req in calls), \
        f"OBD Mode $04 fallback was never sent. Calls: {[r.hex() for _, r in calls]}"

    joined = '\n'.join(logs)
    assert 'OBD Mode $04' in joined
    assert 'Cleared 1/1' in joined


def test_obd_mode04_fallback_reports_nrc_when_ecu_is_not_obd(monkeypatch):
    """Non-OBD ECU must NOT count as cleared if both UDS and OBD Mode $04 fail."""
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4013])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req[:1] == b'\x14':
            return b'\x7F\x14\x22'  # ConditionsNotCorrect
        if req == b'\x04':
            return b'\x7F\x04\x11'  # Service not supported (no OBD on this ECU)
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert 'OBD Mode $04 → NRC=0x11' in joined
    assert 'Cleared 0/1' in joined


# ---------------------------------------------------------------------------
# MIL verification step: after clearing, the function must probe Mode 01
# PID $01 on OBD candidates and log whether the MIL is OFF or still ON.
# ---------------------------------------------------------------------------
def test_clear_logs_mil_off_when_pid01_reports_no_mil(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4010])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\xFF':
            return b'\x54'
        if req == b'\x14\xFF\xFF\x33':
            return b'\x54'
        if req == b'\x01\x01' and ta == 0x4010:
            # OBD Mode 01 PID 01 positive response: 41 01 <A> <B> <C> <D>
            # A bit 7 = MIL flag (0 = OFF). Lower 7 bits = stored DTC count.
            return b'\x41\x01\x00\x00\x00\x00'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert 'MIL is OFF' in joined


def test_clear_logs_mil_still_on_when_pid01_reports_mil_set(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4010])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req[:1] == b'\x14':
            return b'\x54'
        if req == b'\x01\x01' and ta == 0x4010:
            # MIL bit (0x80) set + 3 DTCs.
            return b'\x41\x01\x83\x00\x00\x00'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert 'MIL still ON' in joined
    assert '3 stored DTC' in joined


# ---------------------------------------------------------------------------
# Residual-DTC pass: after a positive group clear, query 0x19 02 0x08; if
# any confirmed DTC survives, attempt per-DTC clear with 0x14 <DTC>.
# ---------------------------------------------------------------------------
def test_residual_pass_clears_per_dtc_when_group_clear_was_no_op(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4010])

    # State machine: first 0x19 02 08 returns one confirmed DTC (U110400).
    # After per-DTC clear, second 0x19 02 08 returns zero confirmed DTCs.
    state = {'reads': 0, 'cleared_dtcs': set()}

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req == b'\x14\xFF\xFF\xFF':
            return b'\x54'
        if req == b'\x14\xFF\xFF\x33':
            return b'\x54'
        # Per-DTC clear: 14 11 04 00 (U110400)
        if len(req) == 4 and req[0] == 0x14 and req[1:] == b'\x11\x04\x00':
            state['cleared_dtcs'].add(0x110400)
            return b'\x54'
        if req == b'\x19\x02\x08':
            state['reads'] += 1
            # First read after group clear: U110400 still confirmed (status 0x09).
            # Second read after per-DTC clear: no confirmed DTCs.
            if state['reads'] == 1:
                return b'\x59\x02\xFF\x11\x04\x00\x09'
            return b'\x59\x02\xFF'
        if req == b'\x01\x01':
            return b'\x41\x01\x00\x00\x00\x00'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert '1 confirmed DTC(s) survived group clear' in joined
    assert 'all DTCs cleared after per-DTC pass' in joined
    assert 0x110400 in state['cleared_dtcs']


def test_residual_pass_reports_persistent_when_per_dtc_clear_also_fails(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x4010])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req[:1] == b'\x14' and len(req) == 4 and req[1:] == b'\xFF\xFF\xFF':
            return b'\x54'
        if req[:1] == b'\x14' and len(req) == 4 and req[1:] == b'\xFF\xFF\x33':
            return b'\x54'
        # Per-DTC clear of B184C00 (SFD-unlocked state): rejected with NRC 0x22.
        if len(req) == 4 and req[0] == 0x14 and req[1:] == b'\x98\x4C\x00':
            return b'\x7F\x14\x22'
        if req == b'\x19\x02\x08':
            # Always report B184C00 confirmed (firmware re-asserts).
            return b'\x59\x02\xFF\x98\x4C\x00\x09'
        if req == b'\x01\x01':
            return b'\x41\x01\x00\x00\x00\x00'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert '1 confirmed DTC(s) survived group clear' in joined
    assert 'per-DTC clear failed (NRC=0x22)' in joined
    assert '1 DTC(s) persist' in joined
    assert 'B184C00' in joined
    assert 'firmware-asserted DTC(s) persist across 1 ECU(s)' in joined


def test_residual_pass_skipped_when_19_02_not_supported(monkeypatch):
    sc, logs, _ = _make_scanner(monkeypatch, discovered=[0x0001])

    def responder(ta, req):
        if req == b'\x10\x03':
            return b'\x50\x03\x00\x32\x01\xF4'
        if req[:1] == b'\x14':
            return b'\x54'
        if req == b'\x19\x02\x08':
            # Service not supported.
            return b'\x7F\x19\x11'
        if req == b'\x01\x01':
            return b'\x41\x01\x00\x00\x00\x00'
        return None

    _install_uds(monkeypatch, sc, responder)
    sc.clear_dtcs_doip()

    joined = '\n'.join(logs)
    assert 'residual check skipped' in joined
