import os
import sys


BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


import ethernet_manager


class _FakeCapture:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def test_ethernet_manager_focuses_capture_filter_on_mirror_when_only_mirror_active(monkeypatch):
    created = []

    def _factory(**kwargs):
        cap = _FakeCapture(**kwargs)
        created.append(cap)
        return cap

    monkeypatch.setattr(ethernet_manager, 'EthernetCapture', _factory)

    manager = ethernet_manager.EthernetManager(socketio=None, main_logger=None)
    manager.start({
        'interface': 'eth0',
        'mirror_port': 30490,
        'pcap_enabled': False,
        'someip_enabled': False,
        'doip_enabled': False,
        'xcp_enabled': False,
    })

    assert created
    assert created[0].started is True
    assert created[0].kwargs['bpf_filter'] == 'udp port 30490 or tcp port 13400'


def test_ethernet_manager_capture_filter_respects_env_override(monkeypatch):
    created = []

    def _factory(**kwargs):
        cap = _FakeCapture(**kwargs)
        created.append(cap)
        return cap

    monkeypatch.setattr(ethernet_manager, 'EthernetCapture', _factory)
    monkeypatch.setenv('KBSM_ETH_BPF_FILTER', 'udp port 12345')

    manager = ethernet_manager.EthernetManager(socketio=None, main_logger=None)
    manager.start({
        'interface': 'eth0',
        'mirror_port': 30490,
        'pcap_enabled': False,
        'someip_enabled': False,
        'doip_enabled': False,
        'xcp_enabled': False,
    })

    assert created
    assert created[0].kwargs['bpf_filter'] == 'udp port 12345'