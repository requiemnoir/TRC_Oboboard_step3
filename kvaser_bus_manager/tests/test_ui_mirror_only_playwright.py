#!/usr/bin/env python3
"""Browser smoke tests for mirror-only acquisition UI."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BACKEND_APP = os.path.join(PROJECT_DIR, 'backend', 'app.py')
PYTHON_BIN = os.path.abspath(os.path.join(PROJECT_DIR, '..', '.venv', 'bin', 'python'))

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - handled by skip
    sync_playwright = None
    PlaywrightError = Exception


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_for_backend(url: str, timeout_s: float = 90.0) -> None:
    deadline = time.time() + float(timeout_s)
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'{url}/api/runtime/status', timeout=2.0) as resp:
                if int(getattr(resp, 'status', 0) or 0) == 200:
                    return
        except Exception as exc:  # pragma: no cover - only used during startup
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f'backend did not become ready at {url}: {last_error}')


@pytest.fixture(scope='session')
def ui_base_url():
    external = str(os.getenv('KVBM_UI_BASE_URL') or '').strip()
    if external:
        _wait_for_backend(external, timeout_s=30.0)
        yield external.rstrip('/')
        return

    port = _pick_free_port()
    base_url = f'http://127.0.0.1:{port}'
    env = dict(os.environ)
    env['KBSM_HOST'] = '127.0.0.1'
    env['KBSM_PORT'] = str(port)
    env.setdefault('KBSM_DEBUG', '0')
    env.setdefault('PYTHONUNBUFFERED', '1')

    proc = subprocess.Popen(
        [PYTHON_BIN, BACKEND_APP],
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_backend(base_url)
        yield base_url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


@pytest.fixture()
def browser_page():
    if sync_playwright is None:
        pytest.skip('playwright package is not available')

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            yield page, context
            context.close()
            browser.close()
    except PlaywrightError as exc:
        pytest.skip(f'playwright browser is not available: {exc}')


def _socket_stub_script() -> str:
    return (
        "window.io = function(){"
        "return {on(){},off(){},emit(){},connect(){},disconnect(){}};"
        "};"
    )


def _install_ui_routes(context, base_url: str, state: dict, calls: dict) -> None:
    config_payload = {
        'config': {
            'logger_channels': [],
            'formats_default': ['mf4'],
            'gateway_mirror': {
                'enabled': True,
                'can': [1, 2, 3, 4],
            },
            'eth_settings': {
                'interface': 'eth0',
            },
        }
    }

    def handle(route):
        request = route.request
        url = request.url
        method = request.method.upper()

        if 'cdnjs.cloudflare.com/ajax/libs/socket.io/' in url:
            route.fulfill(
                status=200,
                content_type='application/javascript',
                body=_socket_stub_script(),
            )
            return

        if url.startswith(f'{base_url}/api/log/status'):
            route.fulfill(status=200, content_type='application/json', body=json.dumps(state))
            return

        if url.startswith(f'{base_url}/api/start') and method == 'POST':
            calls['bus_start'] = calls.get('bus_start', 0) + 1
            payload = {}
            try:
                payload = json.loads(request.post_data or '{}')
            except Exception:
                payload = {}

            started_ids = []
            logger_rows = []
            for ch in payload.get('channels') or []:
                if not isinstance(ch, dict):
                    continue
                try:
                    channel_id = int(ch.get('id'))
                except Exception:
                    continue
                if channel_id in started_ids:
                    continue
                started_ids.append(channel_id)
                row = {'id': channel_id}
                try:
                    bitrate = int(ch.get('bitrate') or 0)
                except Exception:
                    bitrate = 0
                if bitrate:
                    row['bitrate'] = bitrate
                logger_rows.append(row)

            started_ids.sort()
            state['inputs']['bus_running'] = bool(started_ids)
            state['inputs']['bus_channels'] = started_ids
            state['inputs']['logger_channels_config'] = started_ids
            config_payload['config']['logger_channels'] = logger_rows
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'status': 'started', 'async': True}))
            return

        if url.startswith(f'{base_url}/api/acq/start') and method == 'POST':
            calls['start'] += 1
            state['active'] = True
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'status': 'logging_started'}))
            return

        if url.startswith(f'{base_url}/api/acq/stop') and method == 'POST':
            calls['stop'] += 1
            state['active'] = False
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'status': 'stopping'}))
            return

        if url.startswith(f'{base_url}/api/config'):
            if method == 'GET':
                route.fulfill(status=200, content_type='application/json', body=json.dumps(config_payload))
            else:
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'ok': True, 'config': config_payload['config']}))
            return

        if url.startswith(f'{base_url}/api/interfaces'):
            route.fulfill(
                status=200,
                content_type='application/json',
                body=json.dumps([
                    {'id': 0, 'name': 'CAN0', 'upc': 'Kvaser USBcan Pro 4xHS'},
                    {'id': 1, 'name': 'CAN1', 'upc': 'Kvaser USBcan Pro 4xHS'},
                ]),
            )
            return

        if url.startswith(f'{base_url}/api/dbcs'):
            route.fulfill(status=200, content_type='application/json', body=json.dumps(['simulation.dbc']))
            return

        if url.startswith(f'{base_url}/api/logs'):
            route.fulfill(status=200, content_type='application/json', body=json.dumps([]))
            return

        if url.startswith(f'{base_url}/api/system/stats'):
            route.fulfill(
                status=200,
                content_type='application/json',
                body=json.dumps({'cpu_temp_c': None, 'cpu_percent': None, 'ram_percent': None}),
            )
            return

        route.continue_()

    context.route('**/*', handle)


def _base_log_status_state() -> dict:
    return {
        'active': False,
        'stopping': False,
        'kl15': {
            'enabled': False,
            'detected': False,
            'recording': False,
            'last_value': None,
        },
        'inputs': {
            'bus_running': False,
            'bus_channels': [],
            'logger_channels_config': [],
            'eth_running': True,
            'eth_interface': 'eth0',
            'gateway_mirror_enabled': True,
            'gateway_mirror_can': [1, 2, 3, 4],
            'gateway_mirror_virtual_channels': [99, 101, 102, 103, 104, 201],
            'gateway_mirror_map': {'99': 0, '101': 0, '102': 1, '103': 2, '104': 3, '201': 0},
        },
    }


def test_mirror_only_ui_smoke(ui_base_url, browser_page):
    page, context = browser_page
    state = _base_log_status_state()
    calls = {'start': 0, 'stop': 0, 'bus_start': 0}
    _install_ui_routes(context, ui_base_url, state, calls)

    page.goto(f'{ui_base_url}/', wait_until='domcontentloaded')
    page.wait_for_function(
        "() => {"
        "const el = document.getElementById('acq-inputs-status');"
        "return !!el && !el.textContent.includes('Loading');"
        "}"
    )

    inputs_text = page.locator('#acq-inputs-status').text_content() or ''
    config_text = page.locator('#acq-config-status').text_content() or ''
    live_hint = page.locator('#live-traffic-source-hint').text_content() or ''
    header_text = page.locator('table thead th').nth(1).text_content() or ''

    assert 'Local CAN inactive' in inputs_text
    assert 'Mirror active on eth0: net 1, net 2, net 3, net 4' in inputs_text
    assert 'UI logger channels: none' in config_text
    assert 'Logical source mapping remains separate' in config_text
    assert 'virtual mirror channel IDs' in live_hint
    assert header_text.strip() == 'Input'

    page.locator('#btn-log-start').click()
    page.wait_for_function("() => !document.getElementById('btn-log-stop').disabled")

    assert calls['start'] == 1
    assert calls['bus_start'] == 0
    assert page.locator('#btn-log-start').is_disabled()
    assert page.locator('#btn-log-stop').is_enabled()

    page.evaluate(
        "(frame) => handleBusFrames([frame])",
        {
            'timestamp': 1712750000000,
            'channel': 101,
            'id': 0xFD,
            'dlc': 8,
            'data': [0, 100, 0, 0, 0, 0, 0, 0],
            'type': 'CAN',
            'capture_origin': 'mirror',
            'decoded': {'name': 'ESP_21'},
        },
    )

    page.wait_for_selector('#log-table-body tr')
    input_cell_text = page.locator('#log-table-body tr').first.locator('td').nth(1).text_content() or ''
    name_cell_text = page.locator('#log-table-body tr').first.locator('td').nth(3).text_content() or ''

    assert input_cell_text.strip() == 'CAN 101 mirror'
    assert name_cell_text.strip() == 'ESP_21'

    page.locator('#btn-log-stop').click()
    page.wait_for_function("() => !document.getElementById('btn-log-start').disabled")

    assert calls['stop'] == 1
    assert page.locator('#btn-log-start').is_enabled()
    assert page.locator('#btn-log-stop').is_disabled()


def test_explicit_bus_start_updates_ui_input_status(ui_base_url, browser_page):
    page, context = browser_page
    state = _base_log_status_state()
    calls = {'start': 0, 'stop': 0, 'bus_start': 0}
    _install_ui_routes(context, ui_base_url, state, calls)

    page.goto(f'{ui_base_url}/', wait_until='domcontentloaded')
    page.wait_for_function(
        "() => {"
        "const el = document.getElementById('acq-inputs-status');"
        "return !!el && !el.textContent.includes('Loading');"
        "}"
    )

    page.wait_for_function("() => !!document.querySelector('.interface-select')")
    page.evaluate(
        """() => {
            const select = document.querySelector('.interface-select');
            if (!select) throw new Error('missing interface select');
            select.value = '0';
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )

    before_text = page.locator('#acq-config-status').text_content() or ''
    assert 'UI logger channels: none' in before_text

    page.evaluate("() => document.getElementById('btn-start')?.click()")
    page.wait_for_function("() => document.getElementById('btn-start').disabled")
    page.evaluate("() => refreshLoggingStatus()")
    page.wait_for_function(
        "() => {"
        "const inputs = document.getElementById('acq-inputs-status')?.textContent || '';"
        "const cfg = document.getElementById('acq-config-status')?.textContent || '';"
        "return inputs.includes('Local CAN active: CAN0') && cfg.includes('UI logger channels: CAN0');"
        "}"
    )

    inputs_text = page.locator('#acq-inputs-status').text_content() or ''
    config_text = page.locator('#acq-config-status').text_content() or ''

    assert calls['bus_start'] == 1
    assert 'Local CAN active: CAN0' in inputs_text
    assert 'Mirror active on eth0: net 1, net 2, net 3, net 4' in inputs_text
    assert 'UI logger channels: CAN0' in config_text
    assert page.locator('#btn-start').is_disabled()
    assert page.locator('#btn-stop').is_enabled()