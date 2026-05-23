#!/usr/bin/env python3
"""Full Gateway Mirror smoke test against mock DoIP gateway.

1. Starts the mock DoIP gateway on ::1:13400
2. Starts a Flask test client (no real HTTP server needed)
3. Tests all mirror API endpoints
4. Verifies UI-backend field coherence
"""
import json
import os
import sys
import threading
import time

# Ensure backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))


def import_mock():
    """Import mock gateway serve from scripts dir."""
    import importlib.util
    mod_path = os.path.join(os.path.dirname(__file__), "doip_mock_gateway.py")
    spec = importlib.util.spec_from_file_location("doip_mock_gateway", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doip_mock_gateway"] = mod   # register so @dataclass works
    spec.loader.exec_module(mod)
    return mod

def main():
    mock_mod = import_mock()
    mock_stop = threading.Event()
    mock_thread = threading.Thread(
        target=mock_mod.serve,
        kwargs={'host': '::1', 'port': 13400, 'stop_evt': mock_stop},
        daemon=True,
    )
    mock_thread.start()
    time.sleep(0.5)  # let mock bind

    # Import Flask app
    os.chdir(os.path.join(os.path.dirname(__file__), '..', 'backend'))
    sys.path.insert(0, os.getcwd())
    
    # Suppress noisy output during import
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        from app import app, config_store
    finally:
        sys.stdout = old_stdout

    app.testing = True
    client = app.test_client()

    passed = 0
    failed = 0
    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            print(f"  ✅ {name}")
            passed += 1
        else:
            print(f"  ❌ {name} — {detail}")
            failed += 1

    print("\n=== TEST 1: GET /api/gateway/mirror/config ===")
    r = client.get('/api/gateway/mirror/config')
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("ok=true", d.get('ok') is True, str(d))
    check("config is dict", isinstance(d.get('config'), dict), str(d))

    print("\n=== TEST 2: POST /api/gateway/mirror/config (save) ===")
    save_cfg = {
        "config": {
            "gateway_ip": "::1",
            "target_addr": "0x0001",
            "tester_logical_address": "0x0E00",
            "target_bus": "ethernet",
            "dest_ip": "::1",
            "dest_port": 34999,
            "auto_discover_ip": False,
            "can": [1],
            "flexray": [],
            "lin": [],
        }
    }
    r = client.post('/api/gateway/mirror/config', json=save_cfg)
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("ok=true", d.get('ok') is True, str(d))
    check("target_addr=0x0001", d.get('config', {}).get('target_addr') == '0x0001', str(d.get('config')))
    check("dest_port=34999", d.get('config', {}).get('dest_port') == 34999, str(d.get('config')))

    print("\n=== TEST 3: POST /api/gateway/mirror/config (messy target_addr) ===")
    messy_cfg = dict(save_cfg)
    messy_cfg['config'] = dict(messy_cfg['config'])
    messy_cfg['config']['target_addr'] = '0x0001 (mock ECU)'
    r = client.post('/api/gateway/mirror/config', json=messy_cfg)
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("parsed to 0x0001", d.get('config', {}).get('target_addr') == '0x0001', str(d.get('config', {}).get('target_addr')))

    print("\n=== TEST 4: POST /api/gateway/mirror/start ===")
    # Ensure config is saved first
    client.post('/api/gateway/mirror/config', json=save_cfg)
    r = client.post('/api/gateway/mirror/start')
    d = r.get_json() or {}
    check("status 200", r.status_code == 200, f"got {r.status_code}: {d}")
    check("ok=true", d.get('ok') is True, f"detail: {json.dumps(d)}")
    check("response_hex starts with 6e", d.get('response_hex', '').startswith('6e'), d.get('response_hex'))
    check("target_addr=0x0001", d.get('target_addr') == '0x0001', d.get('target_addr'))
    check("gateway_ip=::1", d.get('gateway_ip') == '::1', d.get('gateway_ip'))

    print("\n=== TEST 5: POST /api/gateway/mirror/stop ===")
    r = client.post('/api/gateway/mirror/stop')
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("ok=true", d.get('ok') is True, f"detail: {json.dumps(d)}")
    check("response_hex starts with 6e", d.get('response_hex', '').startswith('6e'), d.get('response_hex'))

    print("\n=== TEST 6: POST /api/gateway/mirror/discover_target_addr ===")
    # Clear target_addr from config to test discovery
    clear_cfg = dict(save_cfg)
    clear_cfg['config'] = dict(clear_cfg['config'])
    clear_cfg['config']['target_addr'] = ''
    client.post('/api/gateway/mirror/config', json=clear_cfg)
    
    r = client.post('/api/gateway/mirror/discover_target_addr', json={
        "gateway_ip": "::1",
        "auto_discover_ip": False,
        "tester_logical_address": "0x0E00",
    })
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("ok=true", d.get('ok') is True, f"detail: {json.dumps(d)}")
    check("target_addr found", bool(d.get('target_addr')), f"target_addr={d.get('target_addr')}")
    # ECU 0x0001 should be discovered since it responds to TesterPresent + ReadDID
    check("discovered 0x0001", d.get('target_addr') in ('0x0001', '0x0003'), d.get('target_addr'))

    print("\n=== TEST 7: Start with empty target_addr (auto-discovery) ===")
    clear_cfg['config']['target_addr'] = ''
    client.post('/api/gateway/mirror/config', json=clear_cfg)
    r = client.post('/api/gateway/mirror/start')
    d = r.get_json()
    check("status 200", r.status_code == 200, f"got {r.status_code}")
    check("ok=true (auto-discovered)", d.get('ok') is True, f"detail: {json.dumps(d)}")
    check("response_hex starts with 6e", d.get('response_hex', '').startswith('6e'), d.get('response_hex'))
    check("target_addr auto-filled", bool(d.get('target_addr')), d.get('target_addr'))

    print("\n=== TEST 8: Config round-trip (save → load → verify) ===")
    full_cfg = {
        "config": {
            "enabled": True,
            "autostart": False,
            "auto_discover_ip": True,
            "gateway_ip": "::1",
            "target_addr": "0x0003",
            "tester_logical_address": "0x0E00",
            "target_bus": "ethernet",
            "dest_ip": "192.168.1.100",
            "dest_port": 35000,
            "can": [1, 3, 5],
            "flexray": ["A"],
            "lin": [2],
        }
    }
    client.post('/api/gateway/mirror/config', json=full_cfg)
    r = client.get('/api/gateway/mirror/config')
    d = r.get_json()
    cfg = d.get('config', {})
    check("enabled=true", cfg.get('enabled') is True)
    check("target_addr=0x0003", cfg.get('target_addr') == '0x0003', cfg.get('target_addr'))
    check("dest_ip=192.168.1.100", cfg.get('dest_ip') == '192.168.1.100', cfg.get('dest_ip'))
    check("dest_port=35000", cfg.get('dest_port') == 35000, cfg.get('dest_port'))
    check("can=[1,3,5]", cfg.get('can') == [1, 3, 5], cfg.get('can'))
    check("flexray=['A']", cfg.get('flexray') == ['A'], cfg.get('flexray'))
    check("lin=[2]", cfg.get('lin') == [2], cfg.get('lin'))
    check("target_bus=ethernet", cfg.get('target_bus') == 'ethernet', cfg.get('target_bus'))

    print("\n=== TEST 9: UI field names match backend ===")
    # These are the fields the UI sends (from gmUiToConfig in app.js)
    ui_fields = ['enabled', 'autostart', 'auto_discover_ip', 'gateway_ip',
                 'target_addr', 'tester_logical_address', 'target_bus',
                 'dest_ip', 'dest_port', 'can', 'flexray', 'lin']
    for f in ui_fields:
        check(f"field '{f}' in config response", f in cfg, f"missing from {list(cfg.keys())}")

    # Check that response from start/stop includes fields UI expects
    r = client.post('/api/gateway/mirror/config', json=save_cfg)  # restore
    r = client.post('/api/gateway/mirror/start')
    d = r.get_json()
    start_fields = ['ok', 'gateway_ip', 'target_addr', 'tester_logical_address', 'did', 'payload_hex', 'response_hex']
    for f in start_fields:
        check(f"start response has '{f}'", f in d, f"missing from {list(d.keys())}")

    print(f"\n{'='*50}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print(f"{'='*50}\n")

    mock_stop.set()
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
