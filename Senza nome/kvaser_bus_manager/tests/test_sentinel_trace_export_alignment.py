import os
import sys
import pytest

from kvaser_bus_manager.backend.experimental_assistant import ExperimentalAssistantService, TraceFrame

try:
    import asammdf  # noqa: F401
except Exception:
    asammdf = None


class _FakeConfigStore:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_config_only(self):
        return self._cfg


class _FakeBusManager:
    class _NullLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def __init__(self, *, with_live_dbcs=True, decode_map=None):
        self.dbcs = {0: [object()]} if with_live_dbcs else {}
        self.arxml_decoder = None
        self.fibex = None
        self.lock = self._NullLock()
        self._decode_map = dict(decode_map or {})

    def add_listener(self, _listener):
        return None

    def decode_frame(self, channel_id, arb_id, data, flags=0, frame_type='CAN'):
        key = (
            str(frame_type or '').strip().upper(),
            int(channel_id),
            int(arb_id),
        )
        out = self._decode_map.get(key)
        if out is None:
            return None
        return {
            'name': str(out.get('name') or ''),
            'signals': dict(out.get('signals') or {}),
        }


def _make_assistant(tmp_path, *, exp_cfg=None, with_live_dbcs=True, decode_map=None):
    cfg = {'experimental_assistant': exp_cfg or {}}
    return ExperimentalAssistantService(
        bus_manager=_FakeBusManager(with_live_dbcs=with_live_dbcs, decode_map=decode_map),
        config_store=_FakeConfigStore(cfg),
        scanner_service=None,
        log_dir_resolver=lambda: str(tmp_path),
        scan_report_dir_resolver=lambda: str(tmp_path),
        ethernet_manager=None,
    )


@pytest.mark.skipif(asammdf is None, reason='asammdf not installed')
def test_export_trace_mf4_uses_full_raw_schema_and_preserves_flags(tmp_path):
    assistant = _make_assistant(tmp_path)

    frames = [
        TraceFrame(
            ts_ms=1_000,
            channel=101,
            frame_id=0x123,
            data=list(range(12)),
            flags=0xAB,
            frame_type='CAN',
            decoded_name='',
            decoded_signals=None,
        ),
        TraceFrame(
            ts_ms=1_040,
            channel=201,
            frame_id=0x33,
            data=list(range(20)),
            flags=0x44,
            frame_type='FLEXRAY',
            decoded_name='',
            decoded_signals=None,
        ),
    ]

    out_name = assistant._export_trace_mf4(frames, prefix='incident_mil_on', trigger_ts_ms=1_000)
    assert out_name

    out_path = os.path.join(str(tmp_path), out_name)
    assert os.path.isfile(out_path)

    mdf = asammdf.MDF(out_path)
    try:
        ch_names = set(str(x) for x in (getattr(mdf, 'channels_db', {}) or {}).keys())
    finally:
        mdf.close()

    assert 'CAN_ID' in ch_names
    assert 'DLC' in ch_names
    assert 'PayloadLength' in ch_names
    assert 'Channel' in ch_names
    assert 'BusType' in ch_names
    assert 'Flags' in ch_names
    assert 'DataByte63' in ch_names


def test_export_trace_decoded_mf4_uses_live_decoder_without_channel_filter(tmp_path, monkeypatch):
    assistant = _make_assistant(tmp_path, with_live_dbcs=True)

    raw_name = 'incident_mil_on_20260101_000000_trace.mf4'
    raw_path = os.path.join(str(tmp_path), raw_name)
    with open(raw_path, 'wb') as fh:
        fh.write(b'raw-mf4')

    calls = {}

    class _FakeDecoder:
        def __init__(self, mf4_path, dbc_paths, *, arxml_catalog=None, arxml_decoder=None, bus_manager=None):
            calls['init'] = {
                'mf4_path': mf4_path,
                'dbc_paths': list(dbc_paths),
                'bus_manager': bus_manager,
                'arxml_decoder': arxml_decoder,
                'arxml_catalog': arxml_catalog,
            }

        def export(self, out_path, *, signals=None, channel=None, start_s=None, end_s=None):
            calls['export'] = {
                'out_path': out_path,
                'signals': signals,
                'channel': channel,
                'start_s': start_s,
                'end_s': end_s,
            }
            with open(out_path, 'wb') as fh:
                fh.write(b'decoded-mf4')

    import kvaser_bus_manager.backend.mf4_decoded_export as mf4_decoded_export
    sys.modules.setdefault('mf4_decoded_export', mf4_decoded_export)

    monkeypatch.setattr(mf4_decoded_export, 'MF4Decoder', _FakeDecoder)

    out_name = assistant._export_trace_decoded_mf4(
        raw_mf4_filename=raw_name,
        trace_frames=[],
        prefix='incident_mil_on',
        trigger_ts_ms=1_000,
    )

    assert out_name
    assert calls['init']['mf4_path'] == raw_path
    assert calls['init']['dbc_paths'] == []
    assert calls['init']['bus_manager'] is assistant.bus_manager

    assert calls['export']['signals'] is None
    assert calls['export']['channel'] is None
    assert calls['export']['start_s'] is None
    assert calls['export']['end_s'] is None


def test_incident_trace_channel_scope_defaults_to_all(tmp_path):
    assistant = _make_assistant(tmp_path, exp_cfg={})
    assert assistant._incident_trace_channel(7) is None


def test_incident_trace_channel_scope_legacy_trigger_channel(tmp_path):
    assistant = _make_assistant(tmp_path, exp_cfg={'trace_channel_scope': 'trigger_channel'})
    assert assistant._incident_trace_channel(7) == 7
    assert assistant._incident_trace_channel(None) is None


@pytest.mark.skipif(asammdf is None, reason='asammdf not installed')
def test_end_to_end_trace_raw_to_decoded_includes_can_and_flexray(tmp_path):
    decode_map = {
        ('CAN', 101, 0x123): {
            'name': 'CanMsg',
            'signals': {'Engine_RPM': 2500.0},
        },
        ('FLEXRAY', 201, 0x33): {
            'name': 'FrMsg',
            'signals': {'Torque': 123.0},
        },
    }
    assistant = _make_assistant(tmp_path, with_live_dbcs=True, decode_map=decode_map)

    frames = [
        TraceFrame(
            ts_ms=10_000,
            channel=101,
            frame_id=0x123,
            data=[0x10, 0x27, 0, 0, 0, 0, 0, 0],
            flags=0,
            frame_type='CAN',
            decoded_name='',
            decoded_signals=None,
        ),
        TraceFrame(
            ts_ms=10_010,
            channel=201,
            frame_id=0x33,
            data=[9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
            flags=7,
            frame_type='FLEXRAY',
            decoded_name='',
            decoded_signals=None,
        ),
    ]

    raw_name = assistant._export_trace_mf4(frames, prefix='incident_mil_on', trigger_ts_ms=10_000)
    assert raw_name

    dec_name = assistant._export_trace_decoded_mf4(
        raw_mf4_filename=raw_name,
        trace_frames=frames,
        prefix='incident_mil_on',
        trigger_ts_ms=10_000,
    )
    assert dec_name

    dec_path = os.path.join(str(tmp_path), dec_name)
    assert os.path.isfile(dec_path)

    mdf = asammdf.MDF(dec_path)
    try:
        ch_names = {
            str(name) for name in (getattr(mdf, 'channels_db', {}) or {}).keys()
            if str(name) and str(name) not in {'CAN_ID', 'DLC', 'PayloadLength', 'Channel', 'BusType', 'Flags'}
            and not str(name).startswith('DataByte')
        }
    finally:
        mdf.close()

    assert any('Engine_RPM' in n for n in ch_names), ch_names
    assert any('Torque' in n for n in ch_names), ch_names
