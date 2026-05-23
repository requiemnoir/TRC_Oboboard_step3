import importlib.util
import os
import sys
import types

import numpy as np

try:
    import asammdf
except Exception:
    asammdf = None


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _load_backend_app_module():
    path = os.path.join(BACKEND_DIR, 'app.py')
    spec = importlib.util.spec_from_file_location('kbsm_backend_app_py_tests', path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


RAW_MF4_SIGNAL_NAMES = {
    'CAN_ID', 'DLC', 'PayloadLength', 'Channel', 'BusType', 'Flags',
    *{f'DataByte{i}' for i in range(64)},
}


def _decoded_channel_names(path):
    if asammdf is None:
        raise RuntimeError('asammdf not installed')
    mdf = asammdf.MDF(path)
    try:
        channels_db = getattr(mdf, 'channels_db', {}) or {}
        return {
            str(name) for name in channels_db.keys()
            if str(name) not in RAW_MF4_SIGNAL_NAMES and '.' in str(name)
        }
    finally:
        try:
            mdf.close()
        except Exception:
            pass


def _decoded_channel_sample_counts(path):
    if asammdf is None:
        raise RuntimeError('asammdf not installed')
    mdf = asammdf.MDF(path)
    try:
        channels_db = getattr(mdf, 'channels_db', {}) or {}
        counts = {}
        for name in channels_db.keys():
            key = str(name)
            if key in RAW_MF4_SIGNAL_NAMES or '.' not in key:
                continue
            sig = mdf.get(key)
            counts[key] = len(sig.samples)
        return counts
    finally:
        try:
            mdf.close()
        except Exception:
            pass


def test_export_coded_mf4_ignores_viewer_decode_inputs(monkeypatch, tmp_path):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    calls = {}

    class FakeDecoder:
        def __init__(self, mf4_path, dbc_paths, *, arxml_catalog=None, arxml_decoder=None, bus_manager=None):
            calls['init'] = {
                'mf4_path': mf4_path,
                'dbc_paths': list(dbc_paths),
                'arxml_catalog': arxml_catalog,
                'arxml_decoder': arxml_decoder,
                'bus_manager': bus_manager,
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
                fh.write(b'coded-mf4')

    fake_manager = types.SimpleNamespace(
        lock=_NullLock(),
        dbcs={1: [object()]},
        arxml_decoder=None,
    )

    monkeypatch.setattr(backend_app, 'LOG_FOLDER', str(tmp_path))
    monkeypatch.setattr(backend_app, '_find_log_file', lambda filename: str(raw_mf4))
    monkeypatch.setattr(backend_app, 'manager', fake_manager)

    import mf4_decoded_export
    monkeypatch.setattr(mf4_decoded_export, 'MF4Decoder', FakeDecoder)

    response = client.post('/api/mf4/export_decoded_mf4', json={
        'file': raw_mf4.name,
        'dbcs': ['ignored.dbc'],
        'auto': False,
        'channel': 7,
        'signals': ['Foo.Bar'],
        'start_s': 1.25,
        'end_s': 2.5,
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['file'].endswith('.mf4')
    assert '_coded_' in payload['file']
    assert os.path.isfile(payload['path'])

    assert calls['init']['mf4_path'] == str(raw_mf4)
    assert calls['init']['dbc_paths'] == []
    assert calls['init']['bus_manager'] is fake_manager

    assert calls['export']['signals'] is None
    assert calls['export']['channel'] is None
    assert calls['export']['start_s'] is None
    assert calls['export']['end_s'] is None


def test_export_coded_mf4_requires_live_decode_configuration(monkeypatch, tmp_path):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    fake_manager = types.SimpleNamespace(
        lock=_NullLock(),
        dbcs={},
        arxml_decoder=types.SimpleNamespace(loaded=False),
    )

    monkeypatch.setattr(backend_app, '_find_log_file', lambda filename: str(raw_mf4))
    monkeypatch.setattr(backend_app, 'manager', fake_manager)

    response = client.post('/api/mf4/export_decoded_mf4', json={'file': raw_mf4.name})

    assert response.status_code == 409
    payload = response.get_json()
    assert payload['ok'] is False
    assert 'live decoding is not configured' in payload['error']


def test_export_coded_mf4_can_respect_view_selection(monkeypatch, tmp_path):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    calls = {}

    class FakeDecoder:
        def __init__(self, mf4_path, dbc_paths, *, arxml_catalog=None, arxml_decoder=None, bus_manager=None):
            pass

        def export(self, out_path, *, signals=None, channel=None, start_s=None, end_s=None):
            calls['export'] = {
                'signals': signals,
                'channel': channel,
                'start_s': start_s,
                'end_s': end_s,
            }
            with open(out_path, 'wb') as fh:
                fh.write(b'coded-mf4')

    fake_manager = types.SimpleNamespace(
        lock=_NullLock(),
        dbcs={1: [object()]},
        arxml_decoder=None,
    )

    monkeypatch.setattr(backend_app, 'LOG_FOLDER', str(tmp_path))
    monkeypatch.setattr(backend_app, '_find_log_file', lambda filename: str(raw_mf4))
    monkeypatch.setattr(backend_app, 'manager', fake_manager)

    import mf4_decoded_export
    monkeypatch.setattr(mf4_decoded_export, 'MF4Decoder', FakeDecoder)

    response = client.post('/api/mf4/export_decoded_mf4', json={
        'file': raw_mf4.name,
        'respect_view_selection': True,
        'signals': ['Foo.Bar'],
        'channel': 7,
        'start_s': 1.25,
        'end_s': 2.5,
    })

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert calls['export']['signals'] == ['Foo.Bar']
    assert calls['export']['channel'] == 7
    assert calls['export']['start_s'] == 1.25
    assert calls['export']['end_s'] == 2.5


def test_export_coded_mf4_falls_back_to_ethernet_metrics(monkeypatch, tmp_path):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    eth_mf4 = tmp_path / 'raw_input.eth.mf4'
    eth_mf4.write_bytes(b'eth-mf4')

    calls = {'fallback': 0}

    class FakeDecoder:
        def __init__(self, mf4_path, dbc_paths, *, arxml_catalog=None, arxml_decoder=None, bus_manager=None):
            pass

        def export(self, out_path, *, signals=None, channel=None, start_s=None, end_s=None):
            raise ValueError('MF4 does not contain a raw frame table (CAN_ID / DLC / Channel / BusType / DataByte*)')

    def fake_export_eth(src_path, out_path):
        calls['fallback'] += 1
        with open(out_path, 'wb') as fh:
            fh.write(b'eth-coded-mf4')

    fake_manager = types.SimpleNamespace(
        lock=_NullLock(),
        dbcs={1: [object()]},
        arxml_decoder=None,
    )

    monkeypatch.setattr(backend_app, 'LOG_FOLDER', str(tmp_path))
    monkeypatch.setattr(backend_app, '_find_log_file', lambda filename: str(eth_mf4))
    monkeypatch.setattr(backend_app, 'manager', fake_manager)

    import mf4_decoded_export
    monkeypatch.setattr(mf4_decoded_export, 'MF4Decoder', FakeDecoder)
    monkeypatch.setattr(mf4_decoded_export, '_mf4_has_ethernet_metrics', lambda _: True)
    monkeypatch.setattr(mf4_decoded_export, 'export_ethernet_numeric_mf4', fake_export_eth)

    response = client.post('/api/mf4/export_decoded_mf4', json={'file': eth_mf4.name})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert os.path.isfile(payload['path'])
    assert calls['fallback'] == 1


def test_export_coded_mf4_merges_sibling_ethernet_metrics(monkeypatch, tmp_path):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    raw_mf4 = tmp_path / 'session_20260408_000000_part0000.mf4'
    raw_mf4.write_bytes(b'raw-mf4')
    eth_mf4 = tmp_path / 'session_20260408_000000.eth.mf4'
    eth_mf4.write_bytes(b'eth-mf4')

    calls = {'merge': 0}

    class FakeDecoder:
        def __init__(self, mf4_path, dbc_paths, *, arxml_catalog=None, arxml_decoder=None, bus_manager=None):
            pass

        def export(self, out_path, *, signals=None, channel=None, start_s=None, end_s=None):
            with open(out_path, 'wb') as fh:
                fh.write(b'coded-mf4')

    def fake_merge(out_path, eth_path, *, t0_epoch=None, start_s=None, end_s=None):
        calls['merge'] += 1
        assert os.path.basename(str(eth_path)) == eth_mf4.name
        return 4

    fake_manager = types.SimpleNamespace(
        lock=_NullLock(),
        dbcs={1: [object()]},
        arxml_decoder=None,
    )

    monkeypatch.setattr(backend_app, 'LOG_FOLDER', str(tmp_path))
    monkeypatch.setattr(backend_app, '_find_log_file', lambda filename: str(raw_mf4))
    monkeypatch.setattr(backend_app, 'manager', fake_manager)

    import mf4_decoded_export
    monkeypatch.setattr(mf4_decoded_export, 'MF4Decoder', FakeDecoder)
    monkeypatch.setattr(mf4_decoded_export, 'merge_ethernet_numeric_channels_into_mf4', fake_merge)

    response = client.post('/api/mf4/export_decoded_mf4', json={'file': raw_mf4.name})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert os.path.isfile(payload['path'])
    assert calls['merge'] == 1


def test_mf4decoder_live_mode_initializes_choice_reverse(tmp_path):
    import mf4_decoded_export

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    decoder = mf4_decoded_export.MF4Decoder(
        str(raw_mf4),
        [],
        bus_manager=object(),
    )

    assert decoder._choice_reverse == {}


def test_mf4decoder_live_mode_uses_bus_type_and_live_signal_names(tmp_path, monkeypatch):
    import mf4_decoded_export

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    decoder = mf4_decoded_export.MF4Decoder(
        str(raw_mf4),
        [],
        bus_manager=object(),
    )
    decoder._raw = (
        np.asarray([10.0, 11.0], dtype=np.float64),
        np.asarray([0x123, 0x45], dtype=np.uint32),
        np.asarray([8, 4], dtype=np.uint16),
        np.asarray([
            [1, 2, 3, 4, 5, 6, 7, 8],
            [9, 10, 11, 12, 0, 0, 0, 0],
        ], dtype=np.uint8),
        np.asarray([0, 150], dtype=np.uint16),
        np.asarray([1, 4], dtype=np.uint8),
        np.asarray([0, 0], dtype=np.uint32),
    )

    calls = []

    def fake_decode_frame(frame_id, data, *, channel=0, flags=0, frame_type='CAN'):
        calls.append((frame_id, channel, frame_type))
        if frame_type == 'CAN':
            return {'name': 'Motor_01', 'signals': {'Engine_RPM': 1234.0}}
        if frame_type == 'LIN':
            return {'name': 'LinMsg', 'signals': {'DoorState': 1.0}}
        return None

    monkeypatch.setattr(decoder, '_decode_frame', fake_decode_frame)

    buffers = decoder.decode()

    assert calls == [
        (0x123, 0, 'CAN'),
        (0x45, 150, 'LIN'),
    ]
    assert sorted(buffers.keys()) == ['CAN0.Engine_RPM', 'LIN.DoorState']


def test_mf4decoder_disambiguates_same_signal_name_across_messages(tmp_path, monkeypatch):
    import mf4_decoded_export

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    decoder = mf4_decoded_export.MF4Decoder(
        str(raw_mf4),
        [],
        bus_manager=object(),
    )
    decoder._raw = (
        np.asarray([10.0, 11.0], dtype=np.float64),
        np.asarray([0x120, 0x121], dtype=np.uint32),
        np.asarray([8, 8], dtype=np.uint16),
        np.asarray([
            [1, 2, 3, 4, 5, 6, 7, 8],
            [8, 7, 6, 5, 4, 3, 2, 1],
        ], dtype=np.uint8),
        np.asarray([0, 0], dtype=np.uint16),
        np.asarray([1, 1], dtype=np.uint8),
        np.asarray([0, 0], dtype=np.uint32),
    )

    def fake_decode_frame(frame_id, data, *, channel=0, flags=0, frame_type='CAN'):
        if frame_id == 0x120:
            return {'name': 'MsgA', 'signals': {'CRC': 11.0}}
        if frame_id == 0x121:
            return {'name': 'MsgB', 'signals': {'CRC': 22.0}}
        return None

    monkeypatch.setattr(decoder, '_decode_frame', fake_decode_frame)

    buffers = decoder.decode()

    assert 'CAN0.CRC' not in buffers
    assert sorted(buffers.keys()) == ['CAN0.MsgA.CRC', 'CAN0.MsgB.CRC']
    assert list(buffers['CAN0.MsgA.CRC']['y']) == [11.0]
    assert list(buffers['CAN0.MsgB.CRC']['y']) == [22.0]


def test_mf4decoder_can_decode_respects_recorded_dlc(tmp_path, monkeypatch):
    import mf4_decoded_export

    raw_mf4 = tmp_path / 'raw_input.mf4'
    raw_mf4.write_bytes(b'raw-mf4')

    decoder = mf4_decoded_export.MF4Decoder(
        str(raw_mf4),
        [],
        bus_manager=object(),
    )
    decoder._raw = (
        np.asarray([10.0], dtype=np.float64),
        np.asarray([0x123], dtype=np.uint32),
        np.asarray([3], dtype=np.uint16),
        np.asarray([[1, 2, 3, 0, 0, 0, 0, 0]], dtype=np.uint8),
        np.asarray([100], dtype=np.uint16),
        np.asarray([1], dtype=np.uint8),
        np.asarray([0], dtype=np.uint32),
    )

    calls = []

    def fake_decode_frame(frame_id, data, *, channel=0, flags=0, frame_type='CAN'):
        calls.append({
            'frame_id': frame_id,
            'data': tuple(data),
            'channel': channel,
            'flags': flags,
            'frame_type': frame_type,
        })
        return {'name': 'Motor_12', 'signals': {'MO_Drehzahl_01': 1234.0}}

    monkeypatch.setattr(decoder, '_decode_frame', fake_decode_frame)

    buffers = decoder.decode(signals=['Motor_12.MO_Drehzahl_01'])

    assert calls == [{
        'frame_id': 0x123,
        'data': (1, 2, 3),
        'channel': 100,
        'flags': 0,
        'frame_type': 'CAN',
    }]
    assert list(buffers['CAN100.MO_Drehzahl_01']['y']) == [1234.0]


def test_exported_mf4_contains_all_live_decoded_signals(tmp_path):
    if asammdf is None:
        return

    from logger import BusLogger, _raw_mf4_bus_type_code, RAW_MF4_PAYLOAD_BYTES
    import mf4_decoded_export

    live_path_base = os.path.join(tmp_path, 'live_decoded')
    raw_path_base = os.path.join(tmp_path, 'raw_source')
    export_path = os.path.join(tmp_path, 'afterward_coded.mf4')

    frames = [
        {
            'id': 0x100,
            'dlc': 8,
            'data': [0x10, 0x27, 0, 0, 0, 0, 0, 0],
            'flags': 0,
            'type': 'CAN',
            'channel': 0,
            'timestamp': 1000,
            'decoded': {
                'name': 'CanMsg',
                'signals': {'Engine_RPM': 2500.0, 'Throttle': 31.5},
            },
        },
        {
            'id': 0x22,
            'dlc': 4,
            'data': [1, 2, 3, 4],
            'flags': 0,
            'type': 'LIN',
            'channel': 150,
            'timestamp': 1010,
            'decoded': {
                'name': 'LinMsg',
                'signals': {'DoorState': 1.0},
            },
        },
        {
            'id': 0x33,
            'dlc': 6,
            'data': [9, 8, 7, 6, 5, 4],
            'flags': 5,
            'type': 'FLEXRAY',
            'channel': 200,
            'timestamp': 1020,
            'decoded': {
                'name': 'FrMsg',
                'signals': {'Torque': 123.0, 'State': 2.0},
            },
        },
    ]

    live_logger = BusLogger(log_dir=str(tmp_path))
    live_logger.set_mf4_include_decoded(True)
    live_logger.set_mf4_include_raw(True)
    live_logger.base_name = live_path_base
    live_logger.mdf_buffer = list(frames)
    live_logger._write_mf4()
    live_mf4_path = f'{live_path_base}.mf4'
    assert os.path.isfile(live_mf4_path)

    raw_chunk = {
        't': [], 'id': [], 'dlc': [], 'payload_len': [], 'ch': [], 'bus_type': [], 'flags': [],
        **{f'db{i}': [] for i in range(RAW_MF4_PAYLOAD_BYTES)},
    }
    for frame in frames:
        ts_f = float(frame['timestamp'])
        t_s = (ts_f / 1000.0) if ts_f > 1e11 else ts_f
        raw_chunk['t'].append(float(t_s))
        raw_chunk['id'].append(int(frame['id']))
        raw_chunk['dlc'].append(int(frame['dlc']))
        raw_chunk['payload_len'].append(int(frame['dlc']))
        raw_chunk['ch'].append(int(frame['channel']))
        raw_chunk['bus_type'].append(int(_raw_mf4_bus_type_code(frame.get('type', 'CAN'))))
        raw_chunk['flags'].append(int(frame.get('flags', 0)))
        payload = list(frame.get('data', []))
        padded = (payload + [0] * RAW_MF4_PAYLOAD_BYTES)[:RAW_MF4_PAYLOAD_BYTES]
        for idx, value in enumerate(padded):
            raw_chunk[f'db{idx}'].append(int(value) & 0xFF)

    raw_logger = BusLogger(log_dir=str(tmp_path))
    raw_logger.base_name = raw_path_base
    raw_logger._write_mf4_raw(raw_chunk)
    raw_mf4_path = f'{raw_path_base}.mf4'
    assert os.path.isfile(raw_mf4_path)

    decoded_lookup = {
        ('CAN', 0, 0x100): frames[0]['decoded'],
        ('LIN', 150, 0x22): frames[1]['decoded'],
        ('FLEXRAY', 200, 0x33): frames[2]['decoded'],
    }

    class FakeBusManager:
        def decode_frame(self, channel_id, arb_id, data, flags=0, frame_type='CAN'):
            key = (str(frame_type or '').strip().upper(), int(channel_id), int(arb_id))
            decoded = decoded_lookup.get(key)
            if decoded is None:
                return None
            return {
                'name': decoded['name'],
                'signals': dict(decoded['signals']),
            }

    decoder = mf4_decoded_export.MF4Decoder(
        raw_mf4_path,
        [],
        bus_manager=FakeBusManager(),
    )
    decoder.export(export_path)
    assert os.path.isfile(export_path)

    live_signals = _decoded_channel_names(live_mf4_path)
    export_signals = _decoded_channel_names(export_path)

    assert live_signals
    assert export_signals == live_signals


def test_exported_mf4_matches_live_decoded_sample_counts(tmp_path):
    if asammdf is None:
        return

    from logger import BusLogger, _raw_mf4_bus_type_code, RAW_MF4_PAYLOAD_BYTES
    import mf4_decoded_export

    live_path_base = os.path.join(tmp_path, 'live_counts')
    raw_path_base = os.path.join(tmp_path, 'raw_counts')
    export_path = os.path.join(tmp_path, 'afterward_counts_coded.mf4')

    frames = [
        {
            'id': 0x100,
            'dlc': 8,
            'data': [0x10, 0x27, 0, 0, 0, 0, 0, 0],
            'flags': 0,
            'type': 'CAN',
            'channel': 0,
            'timestamp': 1000,
            'decoded': {
                'name': 'CanMsg',
                'signals': {'Engine_RPM': 2500.0, 'Throttle': 31.5},
            },
        },
        {
            'id': 0x100,
            'dlc': 8,
            'data': [0x20, 0x27, 0, 0, 0, 0, 0, 0],
            'flags': 0,
            'type': 'CAN',
            'channel': 0,
            'timestamp': 1010,
            'decoded': {
                'name': 'CanMsg',
                'signals': {'Engine_RPM': 2600.0},
            },
        },
        {
            'id': 0x22,
            'dlc': 4,
            'data': [1, 2, 3, 4],
            'flags': 0,
            'type': 'LIN',
            'channel': 150,
            'timestamp': 1020,
            'decoded': {
                'name': 'LinMsg',
                'signals': {'DoorState': 1.0},
            },
        },
        {
            'id': 0x22,
            'dlc': 4,
            'data': [5, 6, 7, 8],
            'flags': 0,
            'type': 'LIN',
            'channel': 150,
            'timestamp': 1030,
            'decoded': {
                'name': 'LinMsg',
                'signals': {'DoorState': 0.0},
            },
        },
        {
            'id': 0x33,
            'dlc': 6,
            'data': [9, 8, 7, 6, 5, 4],
            'flags': 5,
            'type': 'FLEXRAY',
            'channel': 200,
            'timestamp': 1040,
            'decoded': {
                'name': 'FrMsg',
                'signals': {'Torque': 123.0, 'State': 2.0},
            },
        },
        {
            'id': 0x33,
            'dlc': 6,
            'data': [1, 1, 1, 1, 1, 1],
            'flags': 6,
            'type': 'FLEXRAY',
            'channel': 200,
            'timestamp': 1050,
            'decoded': {
                'name': 'FrMsg',
                'signals': {'Torque': 130.0},
            },
        },
    ]

    live_logger = BusLogger(log_dir=str(tmp_path))
    live_logger.set_mf4_include_decoded(True)
    live_logger.set_mf4_include_raw(True)
    live_logger.base_name = live_path_base
    live_logger.mdf_buffer = list(frames)
    live_logger._write_mf4()
    live_mf4_path = f'{live_path_base}.mf4'
    assert os.path.isfile(live_mf4_path)

    raw_chunk = {
        't': [], 'id': [], 'dlc': [], 'payload_len': [], 'ch': [], 'bus_type': [], 'flags': [],
        **{f'db{i}': [] for i in range(RAW_MF4_PAYLOAD_BYTES)},
    }
    for frame in frames:
        ts_f = float(frame['timestamp'])
        t_s = (ts_f / 1000.0) if ts_f > 1e11 else ts_f
        raw_chunk['t'].append(float(t_s))
        raw_chunk['id'].append(int(frame['id']))
        raw_chunk['dlc'].append(int(frame['dlc']))
        raw_chunk['payload_len'].append(int(frame['dlc']))
        raw_chunk['ch'].append(int(frame['channel']))
        raw_chunk['bus_type'].append(int(_raw_mf4_bus_type_code(frame.get('type', 'CAN'))))
        raw_chunk['flags'].append(int(frame.get('flags', 0)))
        payload = list(frame.get('data', []))
        padded = (payload + [0] * RAW_MF4_PAYLOAD_BYTES)[:RAW_MF4_PAYLOAD_BYTES]
        for idx, value in enumerate(padded):
            raw_chunk[f'db{idx}'].append(int(value) & 0xFF)

    raw_logger = BusLogger(log_dir=str(tmp_path))
    raw_logger.base_name = raw_path_base
    raw_logger._write_mf4_raw(raw_chunk)
    raw_mf4_path = f'{raw_path_base}.mf4'
    assert os.path.isfile(raw_mf4_path)

    decoded_lookup = {}
    for frame in frames:
        decoded_lookup[(
            str(frame.get('type', 'CAN')).strip().upper(),
            int(frame['channel']),
            int(frame['id']),
            tuple(int(x) for x in frame['data']),
            int(frame.get('flags', 0)),
        )] = frame['decoded']

    class FakeBusManager:
        def decode_frame(self, channel_id, arb_id, data, flags=0, frame_type='CAN'):
            key = (
                str(frame_type or '').strip().upper(),
                int(channel_id),
                int(arb_id),
                tuple(int(x) for x in data),
                int(flags),
            )
            decoded = decoded_lookup.get(key)
            if decoded is None:
                return None
            return {
                'name': decoded['name'],
                'signals': dict(decoded['signals']),
            }

    decoder = mf4_decoded_export.MF4Decoder(
        raw_mf4_path,
        [],
        bus_manager=FakeBusManager(),
    )
    decoder.export(export_path)
    assert os.path.isfile(export_path)

    live_counts = _decoded_channel_sample_counts(live_mf4_path)
    export_counts = _decoded_channel_sample_counts(export_path)

    assert live_counts
    assert export_counts == live_counts