"""
metrics.py — export Prometheus text format degli stats del mirror_logger.

Implementazione manuale (no dipendenze esterne): l'output rispetta il
"Prometheus text exposition format v0.0.4" e può essere scrapato da
Prometheus, Grafana Agent, Datadog OpenMetrics, ecc.

Endpoint esposto: GET /metrics (no auth, conforme a convention Prometheus).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from raw_logger import RawLogger
    from capture import MirrorCapture
    from doip_activator import DoIPActivator
    from reliability import RetentionWatchdog


def _line(name: str, value: Any, *, help_text: str = '', mtype: str = 'gauge',
          labels: Optional[Dict[str, str]] = None) -> str:
    """Format singola metrica Prometheus."""
    out = []
    if help_text:
        out.append(f'# HELP {name} {help_text}')
        out.append(f'# TYPE {name} {mtype}')
    if labels:
        lbl = '{' + ','.join(f'{k}="{str(v)}"' for k, v in labels.items()) + '}'
    else:
        lbl = ''
    try:
        fv = float(value)
    except (TypeError, ValueError):
        fv = 0.0
    out.append(f'{name}{lbl} {fv}')
    return '\n'.join(out) + '\n'


def render_prometheus(
    *,
    logger: Optional['RawLogger'] = None,
    capture: Optional['MirrorCapture'] = None,
    activator: Optional['DoIPActivator'] = None,
    watchdog: Optional['RetentionWatchdog'] = None,
    disk_snap: Optional[Dict[str, Any]] = None,
) -> str:
    """Genera l'output Prometheus text format con tutte le metriche
    rilevanti per monitoring esterno."""
    out = []

    # Process info
    out.append(_line(
        'mirror_logger_info', 1,
        help_text='1 if mirror_logger is up',
        mtype='gauge',
    ))

    # Logger
    if logger is not None:
        try:
            ls = logger.stats() if logger.active else {}
        except Exception:
            ls = {}
        out.append(_line('mirror_logger_active',
                         1 if logger.active else 0,
                         help_text='1 if MF4 session is active',
                         mtype='gauge'))
        out.append(_line('mirror_logger_frames_total',
                         ls.get('frame_count', 0),
                         help_text='Total frames written to MF4 in current session',
                         mtype='counter'))
        out.append(_line('mirror_logger_dropped_total',
                         ls.get('dropped_count', 0),
                         help_text='Total frames dropped (queue full)',
                         mtype='counter'))
        out.append(_line('mirror_logger_flush_errors_total',
                         ls.get('flush_errors', 0),
                         help_text='Total MF4 flush errors',
                         mtype='counter'))
        out.append(_line('mirror_logger_queue_size',
                         ls.get('queue_size', 0),
                         help_text='Current queue size'))
        out.append(_line('mirror_logger_queue_max',
                         ls.get('queue_max', 0),
                         help_text='Max queue capacity'))
        out.append(_line('mirror_logger_fps',
                         ls.get('fps', 0),
                         help_text='Current frames per second sustained'))
        out.append(_line('mirror_logger_part_index',
                         ls.get('part_index', 0),
                         help_text='Current MF4 chunk index'))
        out.append(_line('mirror_logger_drop_ratio',
                         ls.get('drop_ratio', 0),
                         help_text='Ratio dropped/(dropped+received)'))
        out.append(_line('mirror_logger_elapsed_seconds',
                         ls.get('elapsed_s', 0),
                         help_text='Seconds since session started'))

    # Capture (AF_PACKET pps/kbps)
    if capture is not None:
        try:
            cs = capture.stats()
        except Exception:
            cs = {}
        out.append(_line('mirror_capture_pps',
                         cs.get('pps', 0),
                         help_text='Packets per second received from kernel'))
        out.append(_line('mirror_capture_kbps',
                         cs.get('kbps', 0),
                         help_text='Bandwidth received in kbps'))
        out.append(_line('mirror_capture_frames_total',
                         cs.get('frames', 0),
                         help_text='Total frames extracted from packets',
                         mtype='counter'))
        out.append(_line('mirror_capture_errors_total',
                         cs.get('errors', 0),
                         help_text='Total recv/parse errors',
                         mtype='counter'))

    # DoIP activator
    if activator is not None:
        try:
            st = activator.status()
        except Exception:
            st = {}
        out.append(_line('mirror_doip_connected',
                         1 if st.get('connected') else 0,
                         help_text='1 if DoIP TCP connection to gateway is up'))
        out.append(_line('mirror_doip_activated',
                         1 if st.get('activated') else 0,
                         help_text='1 if mirror gateway DID 0x096F was written'))

    # Disk
    if disk_snap:
        out.append(_line('mirror_logger_disk_free_mb',
                         disk_snap.get('free_mb', 0),
                         help_text='Free disk space on log directory (MB)'))
        out.append(_line('mirror_logger_disk_used_percent',
                         disk_snap.get('used_percent', 0),
                         help_text='Disk usage percentage'))
        out.append(_line('mirror_logger_disk_low',
                         1 if disk_snap.get('low') else 0,
                         help_text='1 if disk_free_mb < min_free_disk_mb'))

    # Retention watchdog
    if watchdog is not None:
        try:
            wd = getattr(watchdog, 'last_result', {}) or {}
        except Exception:
            wd = {}
        out.append(_line('mirror_retention_deleted_files',
                         wd.get('deleted_files', 0),
                         help_text='Files deleted in last retention run',
                         mtype='counter'))
        out.append(_line('mirror_retention_deleted_bytes',
                         wd.get('deleted_bytes', 0),
                         help_text='Bytes deleted in last retention run',
                         mtype='counter'))

    # Timestamp scrape
    out.append(_line('mirror_logger_scrape_timestamp_ms',
                     int(time.time() * 1000),
                     help_text='Server time at scrape (ms epoch)'))

    return ''.join(out)
