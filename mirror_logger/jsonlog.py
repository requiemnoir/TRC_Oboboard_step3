"""
jsonlog.py — handler/formatter logging.Formatter che emette JSON line-per-line.

Compatibile con `journalctl -o json`, Loki, Filebeat, Datadog. Più
facile da parsare di print stdout.

Attivazione:
    import jsonlog
    jsonlog.install_root(level='INFO', extra={'service': 'mirror_logger'})

Poi nei moduli usa logging standard:
    import logging
    log = logging.getLogger(__name__)
    log.info('sessione avviata', extra={'session_id': '...'})

Per backward compat, `print(...)` esistente NON viene intercettato: questo
modulo aggiunge una pipeline alternativa che convive col print.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    """Emette un record per riga in formato JSON, compatibile journalctl/Loki."""

    _RESERVED = frozenset((
        'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
        'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
        'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
        'processName', 'process', 'message',
    ))

    def __init__(self, extra: Optional[Dict[str, Any]] = None):
        super().__init__()
        self._base_extra = dict(extra or {})

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            'ts':     int(record.created * 1000),    # ms epoch
            'iso':    time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))
                      + f'.{int(record.msecs):03d}Z',
            'level':  record.levelname,
            'logger': record.name,
            'msg':    record.getMessage(),
            'mod':    record.module,
            'line':   record.lineno,
            'thread': record.threadName,
        }
        if record.exc_info:
            try:
                payload['exc'] = self.formatException(record.exc_info)
            except Exception:
                pass
        # Campi extra passati dall'utente
        for k, v in self._base_extra.items():
            payload.setdefault(k, v)
        # Eventuali extra dell'invocazione (log.info('...', extra={...}))
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith('_'):
                continue
            try:
                json.dumps(v)   # check serializable
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return json.dumps({'ts': payload['ts'], 'level': payload['level'],
                               'msg': str(payload.get('msg', ''))})


def install_root(level: str = 'INFO',
                 extra: Optional[Dict[str, Any]] = None,
                 stream=None) -> None:
    """Installa JsonFormatter sul root logger se MIRROR_LOGGER_JSON_LOG=1.

    No-op se la env var è 0/false (mantiene print stdout esistenti).
    """
    flag = str(os.environ.get('MIRROR_LOGGER_JSON_LOG', '0')).strip().lower()
    if flag not in {'1', 'true', 'yes', 'on'}:
        return
    root = logging.getLogger()
    # Rimuovi handler esistenti per evitare duplicati
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(extra=extra))
    root.addHandler(handler)
    try:
        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    except Exception:
        root.setLevel(logging.INFO)
