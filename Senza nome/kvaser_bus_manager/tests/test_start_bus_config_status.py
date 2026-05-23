import importlib.util
import os
import sys
import types


BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _load_backend_app_module():
    path = os.path.join(BACKEND_DIR, 'app.py')
    spec = importlib.util.spec_from_file_location('kbsm_backend_app_start_config_tests', path)
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

    def update(self, patch):
        self.cfg.update(dict(patch or {}))
        return {'config': dict(self.cfg)}

    def load(self):
        return {'config': dict(self.cfg)}


def test_start_bus_persists_logger_channels_for_status(monkeypatch):
    backend_app = _load_backend_app_module()
    client = backend_app.app.test_client()

    queued = {}

    def fake_kickoff(payload):
        queued['payload'] = payload
        return True

    fake_manager = types.SimpleNamespace(
        running=True,
        handlers={0: object()},
        lock=_NullLock(),
    )
    fake_capture = types.SimpleNamespace(running=True)
    fake_eth_manager = types.SimpleNamespace(capture=fake_capture, config={'interface': 'eth0'})
    fake_config_store = _MemoryConfigStore()

    monkeypatch.setattr(backend_app, '_kickoff_bus_start_async', fake_kickoff)
    monkeypatch.setattr(backend_app, 'config_store', fake_config_store)
    monkeypatch.setattr(backend_app, 'manager', fake_manager)
    monkeypatch.setattr(backend_app, 'eth_manager', fake_eth_manager)
    monkeypatch.setattr(backend_app, '_mirror_channel_map', {101: 0, 201: 0})

    response = client.post('/api/start', json={
        'channels': [{
            'id': 0,
            'bitrate': 500000,
            'dbc_names': ['simulation.dbc', '../ignored.dbc'],
        }]
    })

    assert response.status_code == 200
    assert response.get_json()['status'] == 'started'
    assert queued['payload']['channels'][0]['id'] == 0
    assert queued['payload']['channels'][0]['bitrate'] == 500000
    assert queued['payload']['channels'][0]['dbcs'] == [
        os.path.join(backend_app.UPLOAD_FOLDER_DBC, 'simulation.dbc')
    ]

    assert fake_config_store.get_config_only()['logger_channels'] == [{
        'id': 0,
        'bitrate': 500000,
        'dbc_names': ['simulation.dbc'],
        'dbc_name': 'simulation.dbc',
    }]

    status = client.get('/api/log/status')
    assert status.status_code == 200
    payload = status.get_json()
    assert payload['inputs']['bus_running'] is True
    assert payload['inputs']['bus_channels'] == [0]
    assert payload['inputs']['logger_channels_config'] == [0]
