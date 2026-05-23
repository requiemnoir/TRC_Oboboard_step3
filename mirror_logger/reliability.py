"""
reliability.py — watchdog disco, retention log e guardie per uso in vettura.

Obiettivi:
- non riempire il disco (retention periodica)
- segnalare spazio libero insufficiente prima di perdere dati
- operazioni best-effort (non devono mai crashare il processo)
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


def disk_snapshot(path: str) -> Dict[str, Any]:
    """Ritorna uso disco per `path` (o vuoto se non disponibile)."""
    try:
        p = str(path or '').strip()
        if not p:
            return {}
        if not os.path.isdir(p):
            p = os.path.dirname(p) or p
        usage = shutil.disk_usage(p)
        free_mb = float(usage.free) / (1024.0 * 1024.0)
        total_mb = float(usage.total) / (1024.0 * 1024.0)
        used_pct = round((float(usage.used) / float(usage.total)) * 100.0, 2) if usage.total else 0.0
        return {
            'path': p,
            'total_bytes': int(usage.total),
            'used_bytes': int(usage.used),
            'free_bytes': int(usage.free),
            'free_mb': round(free_mb, 1),
            'total_mb': round(total_mb, 1),
            'used_percent': used_pct,
        }
    except Exception as e:
        return {'path': str(path or ''), 'error': str(e)}


def is_disk_low(path: str, *, min_free_mb: float) -> bool:
    snap = disk_snapshot(path)
    if 'error' in snap or 'free_mb' not in snap:
        return False
    try:
        free_mb = float(snap.get('free_mb', 0) or 0)
    except Exception:
        return False
    return free_mb < float(max(1.0, min_free_mb))


def enforce_logs_retention(
    log_dir: str,
    *,
    enabled: bool = True,
    max_age_days: float = 14.0,
    max_total_mb: float = 4096.0,
    grace_s: float = 30.0,
    keep_names: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Elimina log vecchi / in eccesso per evitare disco pieno."""
    if not enabled:
        return {'enabled': False, 'ok': True}
    log_dir = str(log_dir or '').strip()
    if not log_dir or not os.path.isdir(log_dir):
        return {'enabled': True, 'ok': False, 'error': 'missing log dir'}

    keep = set(keep_names or set())
    heavy_exts = {'.mf4', '.zip', '.pcap', '.pcapng', '.merge_error.txt'}

    try:
        max_age_days = float(max(0.0, min(max_age_days, 365.0)))
    except Exception:
        max_age_days = 14.0
    cutoff_s = time.time() - (max_age_days * 86400.0) if max_age_days > 0 else None

    try:
        max_total_bytes = int(max(50.0, min(float(max_total_mb), 1024.0 * 1024.0)) * 1024.0 * 1024.0)
    except Exception:
        max_total_bytes = int(4096 * 1024 * 1024)

    entries: List[Tuple[str, float, int, bool]] = []
    total_bytes = 0

    def _entry_size(path: str, *, is_dir: bool) -> int:
        if not is_dir:
            try:
                return max(0, int(os.lstat(path).st_size))
            except Exception:
                return 0
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += max(0, int(os.lstat(os.path.join(root, name)).st_size))
                    except Exception:
                        continue
        except Exception:
            return 0
        return int(total)

    try:
        for de in os.scandir(log_dir):
            try:
                is_file = de.is_file(follow_symlinks=False)
                is_dir = de.is_dir(follow_symlinks=False) and de.name.startswith('incident_')
                if not is_file and not is_dir:
                    continue
                if de.name in keep:
                    continue
                st = de.stat(follow_symlinks=False)
                sz = _entry_size(de.path, is_dir=is_dir)
                total_bytes += max(0, int(sz))
                entries.append((de.path, float(st.st_mtime), int(sz), bool(is_dir)))
            except Exception:
                continue
    except Exception:
        entries = []

    deleted_files = 0
    deleted_dirs = 0
    deleted_bytes = 0

    def _try_unlink(p: str, sz: int, is_dir: bool = False) -> bool:
        nonlocal deleted_files, deleted_dirs, deleted_bytes, total_bytes
        try:
            st = os.stat(p)
            if (time.time() - float(st.st_mtime)) < grace_s:
                return False
        except Exception:
            pass
        try:
            if is_dir:
                shutil.rmtree(p)
                deleted_dirs += 1
            else:
                os.unlink(p)
                deleted_files += 1
            deleted_bytes += max(0, sz)
            total_bytes -= max(0, sz)
            return True
        except Exception:
            return False

    if cutoff_s is not None:
        for p, m, sz, is_dir in sorted(entries, key=lambda x: x[1]):
            if m < cutoff_s:
                _try_unlink(p, sz, is_dir)

    if total_bytes > max_total_bytes:

        def _prio(path: str, is_dir: bool) -> int:
            if is_dir:
                return 0
            ext = os.path.splitext(path)[1].lower()
            if ext in heavy_exts:
                return 0
            if ext in {'.html', '.json'}:
                return 2
            return 1

        for p, _m, sz, is_dir in sorted(entries, key=lambda x: (_prio(x[0], x[3]), x[1])):
            if total_bytes <= max_total_bytes:
                break
            if not os.path.exists(p):
                continue
            _try_unlink(p, sz, is_dir)

    return {
        'enabled': True,
        'ok': True,
        'deleted_files': deleted_files,
        'deleted_dirs': deleted_dirs,
        'deleted_bytes': deleted_bytes,
        'remaining_bytes': max(0, total_bytes),
        'max_age_days': max_age_days,
        'max_total_mb': max_total_mb,
    }


class RetentionWatchdog:
    """Thread periodico per retention + avvisi disco."""

    def __init__(
        self,
        *,
        log_dir_resolver: Callable[[], str],
        config_resolver: Callable[[], Dict[str, Any]],
        keep_names_resolver: Optional[Callable[[], set[str]]] = None,
        interval_s: float = 300.0,
    ):
        self._log_dir_resolver = log_dir_resolver
        self._config_resolver = config_resolver
        self._keep_names_resolver = keep_names_resolver
        self._interval_s = max(30.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='mirror-retention', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Dict[str, Any]:
        cfg = self._config_resolver() or {}
        log_dir = ''
        keep_names: set[str] = set()
        try:
            log_dir = str(self._log_dir_resolver() or '').strip()
        except Exception:
            log_dir = ''
        if self._keep_names_resolver is not None:
            try:
                keep_names = set(self._keep_names_resolver() or set())
            except Exception:
                keep_names = set()
        out = enforce_logs_retention(
            log_dir,
            enabled=bool(cfg.get('logs_retention_enabled', True)),
            max_age_days=float(cfg.get('logs_retention_max_age_days', 14) or 14),
            max_total_mb=float(cfg.get('logs_retention_max_total_mb', 4096) or 4096),
            keep_names=keep_names,
        )
        snap = disk_snapshot(log_dir)
        out['disk'] = snap
        min_free = float(cfg.get('min_free_disk_mb', 256) or 256)
        out['disk_low'] = is_disk_low(log_dir, min_free_mb=min_free)
        out['keep_names'] = sorted(keep_names)
        self.last_result = dict(out)
        if out.get('disk_low'):
            print(
                f'[Reliability] ATTENZIONE: spazio disco basso '
                f'({snap.get("free_mb", "?")} MB liberi su {snap.get("path", log_dir)})',
                flush=True,
            )
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                print(f'[Reliability] retention err: {e}', flush=True)
            self._stop.wait(self._interval_s)
