"""
app.py — Flask slim per mirror_logger.

Routes:
  GET  /                    → index.html
  POST /api/start           → avvia logging
  POST /api/stop            → ferma logging
  GET  /api/status          → statistiche live
    GET  /api/sessions        → lista file log
    GET  /api/sessions/<f>    → download log
    DELETE /api/sessions/<f>  → cancella log
  POST /api/mirror/activate → attiva DoIP mirror
  POST /api/mirror/deactivate → disattiva mirror
  GET  /api/config          → leggi config
  POST /api/config          → aggiorna config (partial)

Nessuna AI, nessuna decodifica, nessuna WebSocket obbligatoria.
Polling JS ogni 1s per stats.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

from flask import (
    Flask, jsonify, request, render_template,
    send_from_directory, send_file, after_this_request, abort
)

from config import config
from mirror_parser import MirrorParser
from raw_logger import RawLogger
from capture import make_capture
from doip_activator import DoIPActivator
from reliability import RetentionWatchdog, disk_snapshot, is_disk_low
from werkzeug.serving import WSGIRequestHandler

# ---------------------------------------------------------------------------
# App Flask
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / 'frontend' / 'templates'),
    static_folder=str(BASE_DIR / 'frontend' / 'static'),
)

# Auth token opzionale: se valorizzato, tutte le /api/* lo richiedono.
# Header: "X-Auth-Token: <token>" oppure query "?token=<token>"
_AUTH_TOKEN = os.environ.get('MIRROR_LOGGER_TOKEN', '').strip()


_PUBLIC_API_ROUTES = frozenset({
    '/api/health',
    '/api/lock/status',   # cross-process: consultato da KBM Sentinel/ScanTools
    '/metrics',           # Prometheus exposition format (convention: no auth)
})


@app.before_request
def _check_auth():
    if not _AUTH_TOKEN:
        return None
    if request.path in _PUBLIC_API_ROUTES:
        return None
    # Solo le rotte API richiedono auth (UI e static aperti per LAN)
    if not request.path.startswith('/api/'):
        return None
    sent = request.headers.get('X-Auth-Token', '') or request.args.get('token', '')
    if sent != _AUTH_TOKEN:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    return None

# ---------------------------------------------------------------------------
# Stato globale (singleton per processo)
# ---------------------------------------------------------------------------

_logger: Optional[RawLogger]   = None
_capture = None  # MirrorCapture | FakeCapture (set da make_capture)
_activator: Optional[DoIPActivator] = None
_session_start: float = 0.0
_watchdog: Optional[RetentionWatchdog] = None
_SESSION_MF4_RE = re.compile(r'^session_(\d{8}_\d{6})_p\d{4}\.mf4$')
_SESSION_PCAP_RE = re.compile(r'^cap_(\d{8}_\d{6})\.pcap$')
_POLL_CACHE_TTL_S = 5.0
_poll_cache: dict[str, tuple[float, dict]] = {}
_lifecycle_lock = threading.RLock()


class QuietPollingRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        if self.command == 'GET':
            path = self.path.split('?', 1)[0]
            if path in {'/api/status', '/api/sessions'}:
                return
        super().log_request(code, size)


def _poll_cache_get(key: str) -> dict | None:
    cached = _poll_cache.get(key)
    if not cached:
        return None
    stored_at, payload = cached
    if time.monotonic() - stored_at > _POLL_CACHE_TTL_S:
        _poll_cache.pop(key, None)
        return None
    return payload


def _poll_cache_set(key: str, payload: dict) -> dict:
    _poll_cache[key] = (time.monotonic(), payload)
    return payload


def _poll_cache_clear(*keys: str) -> None:
    if keys:
        for key in keys:
            _poll_cache.pop(key, None)
        return
    _poll_cache.clear()


def _cached_json(payload: dict):
    response = jsonify(payload)
    response.headers['Cache-Control'] = f'private, max-age={int(_POLL_CACHE_TTL_S)}, must-revalidate'
    return response


def _parse_int_maybe_hex(value, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return default
            return int(text, 0)
        return int(value)
    except Exception:
        return default


def _norm_int_list(values, *, lo: int, hi: int) -> list[int]:
    out: list[int] = []
    if not isinstance(values, list):
        return out
    for value in values:
        item = _parse_int_maybe_hex(value, default=-1)
        if lo <= item <= hi and item not in out:
            out.append(item)
    return out


def _norm_flexray_list(values) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    for value in values:
        item = str(value or '').strip().upper()
        if item in {'A', 'B'} and item not in out:
            out.append(item)
    return out


def _target_bus_code(value, default: int = 2) -> int:
    if isinstance(value, str):
        text = value.strip().lower()
        mapping = {
            'not_active': 0,
            'off': 0,
            'disabled': 0,
            'can': 1,
            'can_diag': 1,
            'diagnostic_can': 1,
            'ethernet': 2,
            'eth': 2,
        }
        if text in mapping:
            return mapping[text]
    return _parse_int_maybe_hex(value, default=default)


def _config_view() -> dict:
    data = config.all()
    eth_settings = data.get('eth_settings') if isinstance(data.get('eth_settings'), dict) else {}
    gateway_mirror = data.get('gateway_mirror') if isinstance(data.get('gateway_mirror'), dict) else {}

    runtime = dict(data)
    runtime.update({
        'interface': str(eth_settings.get('interface') or data.get('interface') or 'eth0').strip() or 'eth0',
        'gateway_ip': str(
            eth_settings.get('target_ip')
            or gateway_mirror.get('gateway_ip')
            or data.get('gateway_ip')
            or ''
        ).strip(),
        'mirror_dest_ip': str(gateway_mirror.get('dest_ip') or data.get('mirror_dest_ip') or '').strip(),
        'mirror_dest_port': _parse_int_maybe_hex(
            gateway_mirror.get('dest_port'),
            default=_parse_int_maybe_hex(data.get('mirror_dest_port'), default=30490),
        ) & 0xFFFF,
        'can_networks': _norm_int_list(
            gateway_mirror.get('can') if isinstance(gateway_mirror.get('can'), list) else data.get('can_networks', [1, 2, 3]),
            lo=1,
            hi=8,
        ),
        'flexray_channels': _norm_flexray_list(
            gateway_mirror.get('flexray') if isinstance(gateway_mirror.get('flexray'), list) else data.get('flexray_channels', []),
        ),
        'lin_networks': _norm_int_list(
            gateway_mirror.get('lin') if isinstance(gateway_mirror.get('lin'), list) else data.get('lin_networks', []),
            lo=1,
            hi=3,
        ),
        'target_bus': _target_bus_code(gateway_mirror.get('target_bus', data.get('target_bus', 2))),
        'gateway_logical_addr': _parse_int_maybe_hex(
            gateway_mirror.get('target_addr')
            or gateway_mirror.get('target_address')
            or data.get('gateway_logical_addr'),
            default=0,
        ) & 0xFFFF,
        'auto_activate_mirror': bool(gateway_mirror.get('autostart', data.get('auto_activate_mirror', False))),
    })
    return runtime


def _cfg(key: str, default=None):
    return _config_view().get(key, default)


def _log_dir() -> str:
    p = _cfg('log_dir', 'logs')
    if not os.path.isabs(p):
        p = str(BASE_DIR / p)
    os.makedirs(p, exist_ok=True)
    return p


def _safe_log_path(filename: str) -> str:
    safe = os.path.basename(filename)
    if safe != filename:
        abort(400)
    suffix = Path(safe).suffix.lower()
    if suffix not in {'.mf4', '.pcap'}:
        abort(400)
    path = os.path.join(_log_dir(), safe)
    if not os.path.isfile(path):
        abort(404)
    return path


def _dir_writable(path: str) -> bool:
    try:
        test_path = Path(path) / '.write_test'
        with open(test_path, 'wb'):
            pass
        test_path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _active_session_id() -> str | None:
    if _logger and _logger.active:
        session_id = getattr(_logger, 'session_id', '')
        if session_id:
            return str(session_id)
    return None


def _session_id_from_name(name: str) -> str | None:
    match = _SESSION_MF4_RE.match(name) or _SESSION_PCAP_RE.match(name)
    if not match:
        return None
    return match.group(1)


def _list_log_files() -> list[dict]:
    files = []
    for pattern in (os.path.join(_log_dir(), '*.mf4'), os.path.join(_log_dir(), '*.pcap')):
        for path in glob.glob(pattern):
            name = os.path.basename(path)
            session_id = _session_id_from_name(name)
            if not session_id:
                continue
            files.append({
                'name': name,
                'path': path,
                'size': os.path.getsize(path),
                'mtime': os.path.getmtime(path),
                'type': Path(name).suffix.lower().lstrip('.'),
                'session_id': session_id,
            })
    files.sort(key=lambda item: item['mtime'], reverse=True)
    return files


def _group_sessions() -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in _list_log_files():
        entry = grouped.setdefault(item['session_id'], {
            'session_id': item['session_id'],
            'mtime': item['mtime'],
            'total_size': 0,
            'part_count': 0,
            'has_pcap': False,
            'files': [],
        })
        entry['mtime'] = max(entry['mtime'], item['mtime'])
        entry['total_size'] += item['size']
        entry['part_count'] += 1 if item['type'] == 'mf4' else 0
        entry['has_pcap'] = entry['has_pcap'] or item['type'] == 'pcap'
        entry['files'].append({
            'name': item['name'],
            'size': item['size'],
            'mtime': item['mtime'],
            'type': item['type'],
        })
    sessions = list(grouped.values())
    sessions.sort(key=lambda item: item['mtime'], reverse=True)
    for session in sessions:
        session['files'].sort(key=lambda item: item['mtime'], reverse=True)
    return sessions


def _find_session_files(session_id: str) -> list[dict]:
    if not re.fullmatch(r'\d{8}_\d{6}', session_id):
        abort(400)
    files = [item for item in _list_log_files() if item['session_id'] == session_id]
    if not files:
        abort(404)
    return files


def _active_log_keep_names() -> set[str]:
    keep: set[str] = set()
    if _logger and _logger.active:
        session_id = getattr(_logger, 'session_id', '')
        if session_id:
            prefix = f'session_{session_id}_'
            pcap_name = f'cap_{session_id}.pcap'
            for item in _list_log_files():
                if item['name'].startswith(prefix) or item['name'] == pcap_name:
                    keep.add(item['name'])
    return keep


def _current_reliability_snapshot(force_refresh: bool = False) -> dict:
    global _watchdog
    if _watchdog is None:
        return {}
    if force_refresh:
        try:
            return _watchdog.run_once()
        except Exception as e:
            return {'ok': False, 'error': str(e)}
    return dict(_watchdog.last_result)


def _status_payload(force_refresh: bool = False) -> dict:
    log_stats = _logger.stats() if _logger else {'active': False}
    cap_stats = _capture.stats() if _capture else {'pps': 0, 'kbps': 0, 'frames': 0, 'errors': 0}
    act_status = _activator.status() if _activator else {'connected': False, 'activated': False, 'last_error': ''}

    log_dir = _log_dir()
    disk = disk_snapshot(log_dir)
    disk['writable'] = _dir_writable(log_dir)
    disk['low'] = is_disk_low(log_dir, min_free_mb=float(_cfg('min_free_disk_mb', 256) or 256))

    reliability = _current_reliability_snapshot(force_refresh=force_refresh)
    if reliability:
        reliability.setdefault('disk', dict(disk))
    else:
        reliability = {
            'enabled': bool(_cfg('logs_retention_enabled', True)),
            'disk': dict(disk),
            'disk_low': disk['low'],
        }

    return {
        'logger': log_stats,
        'capture': cap_stats,
        'mirror': act_status,
        'disk': disk,
        'reliability': reliability,
    }


def _health_payload(force_refresh: bool = False) -> dict:
    status = _status_payload(force_refresh=force_refresh)
    disk = status.get('disk', {})
    payload = {
        'ok': bool(disk.get('writable', False)) and not bool(disk.get('low', False)) and not bool(status.get('logger', {}).get('flush_errors', 0)),
        'logging_active': bool(status.get('logger', {}).get('active', False)),
        'mirror_connected': bool(status.get('mirror', {}).get('connected', False)),
        'disk': {
            'path': disk.get('path', _log_dir()),
            'writable': bool(disk.get('writable', False)),
            'low': bool(disk.get('low', False)),
            'free_mb': disk.get('free_mb'),
            'used_percent': disk.get('used_percent'),
        },
    }
    return payload


def _ensure_watchdog_started() -> None:
    global _watchdog
    if _watchdog is not None:
        return
    _watchdog = RetentionWatchdog(
        log_dir_resolver=_log_dir,
        config_resolver=_config_view,
        keep_names_resolver=_active_log_keep_names,
        interval_s=float(_cfg('logs_retention_min_interval_s', 300) or 300),
    )
    _watchdog.start()
    try:
        _watchdog.run_once()
    except Exception as e:
        print(f'[Reliability] avvio watchdog fallito: {e}', flush=True)


def _activate_mirror_core() -> dict:
    global _activator
    with _lifecycle_lock:
        if _activator and _activator.activated:
            return {'ok': True, 'message': 'già attivo', 'status': _activator.status()}

        _activator = DoIPActivator(
            gateway_ip=_cfg('gateway_ip', '192.168.0.140'),
            mirror_dest_ip=_cfg('mirror_dest_ip', ''),
            mirror_dest_port=_cfg('mirror_dest_port', 30490),
            can_networks=_cfg('can_networks', [1, 2]),
            flexray_channels=_cfg('flexray_channels', []),
            lin_networks=_cfg('lin_networks', []),
            target_bus=_cfg('target_bus', 2),
            gateway_logical_addr=_cfg('gateway_logical_addr', 0),
            keepalive_interval_s=_cfg('keepalive_interval_s', 2.0),
        )
        _activator.start()
        _poll_cache_clear('status')
        return {'ok': True, 'status': _activator.status()}


def _deactivate_mirror_core() -> dict:
    global _activator
    with _lifecycle_lock:
        if _activator:
            _activator.stop()
            _activator = None
        _poll_cache_clear('status')
        return {'ok': True}


def _start_session_core() -> dict:
    global _logger, _capture, _session_start
    with _lifecycle_lock:
        if _logger and _logger.active:
            return {'ok': False, 'error': 'sessione già attiva'}, 409

        _ensure_watchdog_started()

        # Logger MF4
        _logger = RawLogger(
            log_dir=_log_dir(),
            chunk_interval_s=_cfg('chunk_interval_s', 15),
            chunk_max_frames=_cfg('chunk_max_frames', 2_000_000),
            flush_interval_frames=_cfg('flush_interval_frames', 5_000),
            flush_interval_s=_cfg('flush_interval_s', 10),
            queue_max=_cfg('queue_max', 524_288),
            put_timeout_ms=_cfg('put_timeout_ms', 25),
        )
        _logger.start()
        _session_start = time.time()

        # Capture (AF_PACKET su Linux, FakeCapture altrove)
        pcap_path = None
        if _cfg('pcap_enabled', False):
            pcap_path = str(Path(_log_dir()) / f'cap_{_logger.session_id}.pcap')

        try:
            _capture = make_capture(
                interface=_cfg('interface', 'eth0'),
                mirror_port=_cfg('mirror_dest_port', 30490),
                on_frame=_logger.log,
                pcap_path=pcap_path,
            )
            _capture.start()
        except Exception:
            try:
                _logger.stop()
            finally:
                _logger = None
                _capture = None
            raise

        _poll_cache_clear('status', 'sessions')
        return {'ok': True, 'session_id': _logger.session_id}, 200


def _stop_session_core() -> tuple[dict, int]:
    global _logger, _capture
    with _lifecycle_lock:
        if not _logger or not _logger.active:
            return {'ok': False, 'error': 'nessuna sessione attiva'}, 409

        if _capture:
            _capture.stop()
            _capture = None

        stats = _logger.stop()
        _logger = None

        if _watchdog is not None:
            _watchdog.run_once()

        _poll_cache_clear('status', 'sessions')
        return {'ok': True, 'stats': stats}, 200


def _run_retention_once() -> tuple[dict, int]:
    _ensure_watchdog_started()
    if _watchdog is None:
        return {'ok': False, 'error': 'watchdog non disponibile'}, 500
    result = _watchdog.run_once()
    _poll_cache_clear('status', 'sessions')
    return {'ok': bool(result.get('ok', False)), 'result': result}, 200


def _shutdown_runtime() -> None:
    global _watchdog
    try:
        if _logger and _logger.active:
            _stop_session_core()
    except Exception as e:
        print(f'[mirror_logger] stop session err: {e}', flush=True)
    try:
        _deactivate_mirror_core()
    except Exception as e:
        print(f'[mirror_logger] deactivate mirror err: {e}', flush=True)
    try:
        config.flush()
    except Exception as e:
        print(f'[mirror_logger] config flush err: {e}', flush=True)
    if _watchdog is not None:
        try:
            _watchdog.stop()
        except Exception as e:
            print(f'[mirror_logger] watchdog stop err: {e}', flush=True)


def _register_shutdown_hooks() -> None:
    def _handle_signal(_signum, _frame):
        _shutdown_runtime()
        raise SystemExit(0)

    atexit.register(_shutdown_runtime)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/')
def index():
    return render_template('index.html')


@app.get('/api/health')
def api_health():
    return jsonify(_health_payload(force_refresh=True))


@app.get('/metrics')
def api_metrics_prometheus():
    """Prometheus text exposition format.

    Endpoint pubblico (convention Prometheus: niente auth, niente token).
    Espone metriche logger, capture, DoIP, disk, retention. Per uso con
    Prometheus/Grafana Agent/Datadog OpenMetrics.
    """
    from metrics import render_prometheus
    from reliability import disk_snapshot, is_disk_low
    try:
        log_dir = _log_dir()
        snap = disk_snapshot(log_dir)
        snap['low'] = is_disk_low(log_dir, min_free_mb=float(_cfg('min_free_disk_mb', 256) or 256))
    except Exception:
        snap = {}
    body = render_prometheus(
        logger=_logger,
        capture=_capture,
        activator=_activator,
        watchdog=_watchdog,
        disk_snap=snap,
    )
    return app.response_class(body, mimetype='text/plain; version=0.0.4; charset=utf-8')


@app.get('/api/lock/status')
def api_lock_status():
    """Risorse esclusive prenotate dal mirror_logger (cross-process).

    Endpoint pubblico (no auth) pensato per il KBM ScanTools / Sentinel: prima
    di aprire una sessione DoIP al gateway, chiamano questa route e — se
    `doip_active=true` — devono evitare la propria attivazione oppure
    chiedere all'utente di disattivare il mirror prima.

    Risposta:
      doip_active   : DoIPActivator attivo (keepalive in corso)
      session_active: RawLogger sta scrivendo MF4 (un'altra capture è in corso)
      session_id    : id sessione attiva, '' se nessuna
    """
    with _lifecycle_lock:
        doip = bool(_activator and _activator.activated)
        sess = bool(_logger and _logger.active)
        sid = _active_session_id() or ''
    return jsonify({
        'ok': True,
        'doip_active': doip,
        'session_active': sess,
        'session_id': sid,
    })


# ---------------------------------------------------------------------------
# Incident snapshot — integrazione cross-process Sentinel ↔ mirror_logger
# ---------------------------------------------------------------------------
# Quando il Sentinel del KBM rileva un incident (MIL on, spia EPC ecc.) chiama
# questo endpoint per "congelare" gli ultimi N secondi del mirror in una
# directory dedicata. I file vengono copiati in modo atomico perché i chunk
# MF4 attivi possono essere riscritti durante la sessione. L'incident bundle finale del KBM include sia i frame
# canlib del Sentinel sia i frame mirror estratti qui.

_SNAPSHOT_DEFAULT_WINDOW_S = 45
_SNAPSHOT_MAX_WINDOW_S     = 300
_SNAPSHOT_LABEL_RE         = re.compile(r'[^A-Za-z0-9_.\-]+')


def _copy_snapshot_file(src: str, dst: str) -> str:
    """Copia un file snapshot usando un temp + replace atomico."""
    tmp = f'{dst}.tmp-{os.getpid()}-{threading.get_ident()}'
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return 'copy'


@app.post('/api/incident/snapshot')
def api_incident_snapshot():
    """Cattura uno snapshot del mirror per un incident esterno (Sentinel).

    Body JSON opzionale:
      window_s : int     (default 45, max 300) — finestra retroattiva
      label    : str     (default 'incident_<ts>') — slug per dir output

    Comportamento:
      - Richiede sessione MF4 attiva (409 altrimenti)
      - Forza flush sincrono del chunk corrente del logger
            - Copia i file MF4 che cadono nella finestra in
        `logs/incident_<label>/`
      - Scrive `manifest.json` con metadati
      - Best-effort: il logger continua a girare normalmente

    Risposta:
      {ok: true, manifest: {...}, incident_dir: '...'}
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        window_s = int(data.get('window_s', _SNAPSHOT_DEFAULT_WINDOW_S))
    except (TypeError, ValueError):
        window_s = _SNAPSHOT_DEFAULT_WINDOW_S
    window_s = max(1, min(window_s, _SNAPSHOT_MAX_WINDOW_S))

    raw_label = str(data.get('label', '') or '').strip()
    if not raw_label:
        raw_label = f'incident_{int(time.time())}'
    label = _SNAPSHOT_LABEL_RE.sub('_', raw_label)[:64].strip('_') or f'incident_{int(time.time())}'

    with _lifecycle_lock:
        if not _logger or not _logger.active:
            return jsonify({
                'ok': False,
                'error': 'nessuna sessione MF4 attiva, nulla da snapshottare',
            }), 409

        session_id = _logger.session_id

        # Forza flush sincrono: drena la queue del RawLogger e serializza il
        # salvataggio MF4 prima di copiare i file snapshot.
        force_flush_ok = False
        try:
            force_flush_ok = bool(_logger.force_flush(timeout_s=3.0))
        except Exception as e:
            print(f'[incident] force_flush err: {e}', flush=True)

        now_s = time.time()
        cutoff_s = now_s - window_s

        log_dir = Path(_log_dir())
        incident_dir = log_dir / f'incident_{session_id}_{label}'
        incident_dir.mkdir(parents=True, exist_ok=True)

        # Trova tutti i chunk MF4 della sessione attiva
        pattern = f'session_{session_id}_p*.mf4'
        chunks = sorted(
            log_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
        )

        # Includi chunk con mtime >= cutoff_s; aggiungi anche il primo
        # precedente per coprire il bordo (potrebbe contenere frame dentro
        # la finestra che il prossimo flush sposterà solo dopo).
        in_window = [c for c in chunks if c.exists() and c.stat().st_mtime >= cutoff_s]
        before = [c for c in chunks if c.exists() and c.stat().st_mtime < cutoff_s]
        if before:
            in_window.insert(0, before[-1])

        # PCAP affiancato (se enabled)
        pcap_path = log_dir / f'cap_{session_id}.pcap'
        if pcap_path.exists():
            in_window.append(pcap_path)

        # Copia atomica: i chunk della sessione attiva possono essere
        # riscritti, quindi un hard-link non sarebbe uno snapshot immutabile.
        snapshot_files = []
        total_size = 0
        strategy_counts = {'copy': 0}
        for src in in_window:
            dst = incident_dir / src.name
            try:
                strategy = _copy_snapshot_file(str(src), str(dst))
            except OSError as e:
                print(f'[incident] copia {src.name} fallita: {e}', flush=True)
                continue
            sz = dst.stat().st_size
            snapshot_files.append({
                'name': src.name,
                'size': sz,
                'mtime': dst.stat().st_mtime,
                'strategy': strategy,
            })
            total_size += sz
            strategy_counts[strategy] += 1

        manifest = {
            'session_id':     session_id,
            'label':          label,
            'incident_at_ms': int(now_s * 1000),
            'window_s':       window_s,
            'force_flush_ok': force_flush_ok,
            'files':          snapshot_files,
            'total_size':     total_size,
            'strategy':       strategy_counts,
        }

        try:
            with open(incident_dir / 'manifest.json', 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
        except OSError as e:
            print(f'[incident] manifest write err: {e}', flush=True)

        _poll_cache_clear('sessions')

        return jsonify({
            'ok':           True,
            'incident_dir': str(incident_dir),
            'manifest':     manifest,
        })


# -- Sessione logging -------------------------------------------------------

@app.post('/api/start')
def api_start():
    try:
        payload, status = _start_session_core()
        return jsonify(payload), status

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/stop')
def api_stop():
    try:
        payload, status = _stop_session_core()
        return jsonify(payload), status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.get('/api/status')
def api_status():
    cached = _poll_cache_get('status')
    if cached:
        return _cached_json(cached)
    return _cached_json(_poll_cache_set('status', _status_payload()))


# -- Sessioni / download ----------------------------------------------------

@app.get('/api/sessions')
def api_sessions():
    cached = _poll_cache_get('sessions')
    if cached:
        return _cached_json(cached)
    return _cached_json(_poll_cache_set('sessions', {'sessions': _group_sessions()}))


@app.delete('/api/sessions')
def api_delete_all_sessions():
    active_id = _active_session_id()
    deleted = []
    for item in _list_log_files():
        if active_id and item['session_id'] == active_id:
            continue
        try:
            os.remove(item['path'])
            deleted.append(item['name'])
        except FileNotFoundError:
            continue
        except OSError as e:
            return jsonify({'ok': False, 'error': str(e), 'deleted_count': len(deleted)}), 500
    _poll_cache_clear('sessions')
    return jsonify({'ok': True, 'deleted_count': len(deleted), 'deleted': deleted})


@app.get('/api/sessions/<session_id>/bundle.zip')
def api_download_session_bundle(session_id: str):
    files = _find_session_files(session_id)
    tmp = tempfile.NamedTemporaryFile(prefix=f'mirror_{session_id}_', suffix='.zip', delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        with zipfile.ZipFile(tmp_path, mode='w', compression=zipfile.ZIP_STORED) as zf:
            for item in files:
                zf.write(item['path'], arcname=item['name'])
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    @after_this_request
    def _cleanup_zip(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name=f'session_{session_id}.zip')


@app.delete('/api/sessions/<session_id>')
def api_delete_grouped_session(session_id: str):
    files = _find_session_files(session_id)
    if _active_session_id() == session_id:
        return jsonify({'ok': False, 'error': 'sessione attiva'}), 409

    deleted = []
    for item in files:
        try:
            os.remove(item['path'])
            deleted.append(item['name'])
        except FileNotFoundError:
            continue
        except OSError as e:
            return jsonify({'ok': False, 'error': str(e), 'deleted': deleted}), 500

    _poll_cache_clear('sessions')
    return jsonify({'ok': True, 'session_id': session_id, 'deleted': deleted})


@app.get('/api/logs/<filename>')
def api_download_log(filename: str):
    path = _safe_log_path(filename)
    return send_from_directory(_log_dir(), os.path.basename(path), as_attachment=True)


@app.delete('/api/logs/<filename>')
def api_delete_log(filename: str):
    path = _safe_log_path(filename)
    active_id = _active_session_id()
    name = os.path.basename(path)
    session_id = _session_id_from_name(name)

    if active_id and session_id == active_id:
        return jsonify({'ok': False, 'error': 'file della sessione attiva'}), 409

    try:
        os.remove(path)
        _poll_cache_clear('sessions')
        return jsonify({'ok': True, 'deleted': name})
    except FileNotFoundError:
        abort(404)
    except OSError as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# -- Mirror DoIP ------------------------------------------------------------

@app.post('/api/mirror/activate')
def api_mirror_activate():
    try:
        return jsonify(_activate_mirror_core())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.post('/api/mirror/deactivate')
def api_mirror_deactivate():
    return jsonify(_deactivate_mirror_core())


# -- Config -----------------------------------------------------------------

@app.get('/api/config')
def api_config_get():
    return jsonify(_config_view())


@app.post('/api/config')
def api_config_set():
    data = request.get_json(force=True, silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'JSON non valido'}), 400
    # Non permettere campi pericolosi
    _READONLY = {'flask_host', 'flask_port', 'flask_debug'}
    clean = {k: v for k, v in data.items() if k not in _READONLY}
    config.update(clean)
    if _watchdog is not None:
        _watchdog.run_once()
    _poll_cache_clear()
    return jsonify({'ok': True})


@app.post('/api/maintenance/enforce_retention')
def api_enforce_retention():
    try:
        payload, status = _run_retention_once()
        return jsonify(payload), status
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    host  = _cfg('flask_host', '0.0.0.0')
    port  = _cfg('flask_port', 5050)
    debug = _cfg('flask_debug', False)
    _register_shutdown_hooks()
    _ensure_watchdog_started()

    # Avvio automatico opzionale
    def _autostart_worker():
        if _cfg('auto_activate_mirror', False):
            time.sleep(2)
            try:
                result = _activate_mirror_core()
                if not result.get('ok'):
                    print(f'[mirror_logger] auto_activate fallito: {result}', flush=True)
            except Exception as e:
                print(f'[mirror_logger] auto_activate err: {e}', flush=True)
        if _cfg('auto_start_capture', False):
            time.sleep(2)
            try:
                result, status = _start_session_core()
                if status >= 400:
                    print(f'[mirror_logger] auto_start fallito: {result}', flush=True)
            except Exception as e:
                print(f'[mirror_logger] auto_start err: {e}', flush=True)

    if _cfg('auto_activate_mirror', False) or _cfg('auto_start_capture', False):
        threading.Thread(target=_autostart_worker, name='mirror-autostart', daemon=True).start()

    print(f'[mirror_logger] avvio su http://{host}:{port}', flush=True)
    app.run(host=host, port=port, debug=debug, use_reloader=False, request_handler=QuietPollingRequestHandler)
