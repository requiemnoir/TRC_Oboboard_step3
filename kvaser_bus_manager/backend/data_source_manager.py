from __future__ import annotations

from typing import Any, Dict, List, Optional
import os
import threading

from config_store import ConfigStore
from monitor_types import DataSource, BusType, new_id, now_ms


class DataSourceManager:
    """Manages user-configurable data sources.

    MVP: fully supports CAN sources. Other bus types are kept for forward compatibility.

    Persistence: stored under ConfigStore key 'data_sources'.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        *,
        dbc_dir: str,
        fibex_dir: str | None = None,
    ):
        self._store = config_store
        self._dbc_dir = os.path.abspath(dbc_dir)
        self._fibex_dir = os.path.abspath(fibex_dir) if fibex_dir else None
        self._lock = threading.Lock()
        self._sources: Dict[str, DataSource] = {}
        self.reload()

    def reload(self) -> None:
        cfg = self._store.get_config_only() or {}
        raw = cfg.get('data_sources')
        sources: Dict[str, DataSource] = {}
        if isinstance(raw, list):
            for obj in raw:
                if not isinstance(obj, dict):
                    continue
                try:
                    sid = str(obj.get('id') or '').strip() or new_id('src')
                    name = str(obj.get('name') or sid)
                    typ = str(obj.get('type') or 'CAN')
                    if typ not in {'CAN', 'FlexRay', 'EthDoIP', 'EthSOMEIP', 'EthXCP'}:
                        typ = 'CAN'
                    enabled = bool(obj.get('enabled', True))
                    config = obj.get('config') if isinstance(obj.get('config'), dict) else {}
                    dbc_name = str(obj.get('dbc_name') or '').strip()
                    fibex_name = str(obj.get('fibex_name') or '').strip()
                    sources[sid] = DataSource(
                        id=sid,
                        name=name,
                        type=typ,  # type: ignore[arg-type]
                        enabled=enabled,
                        config=dict(config),
                        dbc_name=dbc_name,
                        fibex_name=fibex_name,
                        created_at_ms=int(obj.get('created_at_ms') or now_ms()),
                        updated_at_ms=int(obj.get('updated_at_ms') or now_ms()),
                    )
                except Exception:
                    continue
        with self._lock:
            self._sources = sources

    def _save(self) -> None:
        with self._lock:
            payload = [s.to_dict() for s in self._sources.values()]
        self._store.update({'data_sources': payload})

    def list_sources(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in sorted(self._sources.values(), key=lambda x: (x.type, x.name))]

    def get_source(self, source_id: str) -> Optional[DataSource]:
        with self._lock:
            return self._sources.get(str(source_id))

    def upsert_source(self, obj: Dict[str, Any]) -> DataSource:
        if not isinstance(obj, dict):
            raise ValueError('source must be an object')

        sid = str(obj.get('id') or '').strip()
        if not sid:
            sid = new_id('src')

        name = str(obj.get('name') or sid).strip()[:64]
        typ: BusType = str(obj.get('type') or 'CAN')  # type: ignore[assignment]
        if typ not in {'CAN', 'FlexRay', 'EthDoIP', 'EthSOMEIP', 'EthXCP'}:
            typ = 'CAN'

        enabled = bool(obj.get('enabled', True))
        config = obj.get('config') if isinstance(obj.get('config'), dict) else {}

        dbc_name = str(obj.get('dbc_name') or '').strip()
        fibex_name = str(obj.get('fibex_name') or '').strip()

        # Validate associated database per bus type.
        if typ != 'CAN':
            dbc_name = ''
        if typ != 'FlexRay':
            fibex_name = ''

        if dbc_name:
            if os.path.basename(dbc_name) != dbc_name:
                raise ValueError('invalid dbc_name')
            path = os.path.join(self._dbc_dir, dbc_name)
            if not os.path.isfile(path):
                raise ValueError('dbc not found')

        if fibex_name:
            if os.path.basename(fibex_name) != fibex_name:
                raise ValueError('invalid fibex_name')
            if not self._fibex_dir:
                raise ValueError('fibex_dir not configured')
            path = os.path.join(self._fibex_dir, fibex_name)
            if not os.path.isfile(path):
                raise ValueError('fibex not found')

        now = now_ms()

        with self._lock:
            prev = self._sources.get(sid)
            created = prev.created_at_ms if prev else now
            src = DataSource(
                id=sid,
                name=name,
                type=typ,
                enabled=enabled,
                config=dict(config),
                dbc_name=dbc_name,
                fibex_name=fibex_name,
                created_at_ms=created,
                updated_at_ms=now,
            )
            self._sources[sid] = src

        self._save()
        return src

    def delete_source(self, source_id: str) -> bool:
        sid = str(source_id)
        with self._lock:
            existed = sid in self._sources
            if existed:
                self._sources.pop(sid, None)
        if existed:
            self._save()
        return existed

    def list_dbcs(self) -> List[str]:
        try:
            out = []
            for f in os.listdir(self._dbc_dir):
                if f.lower().endswith('.dbc') and os.path.basename(f) == f:
                    out.append(f)
            return sorted(out)
        except Exception:
            return []

    def ensure_default_can_sources(self) -> None:
        """Create 4 default CAN sources if none exist."""
        with self._lock:
            if self._sources:
                return
        for ch in range(4):
            self.upsert_source({
                'name': f'CAN{ch}',
                'type': 'CAN',
                'enabled': True,
                'config': {'channel_id': ch, 'bitrate': 500000, 'can_fd': False},
            })

    def find_can_source_by_channel(self, channel_id: int) -> Optional[str]:
        try:
            cid = int(channel_id)
        except Exception:
            return None
        with self._lock:
            for s in self._sources.values():
                if s.type != 'CAN':
                    continue
                cfg = s.config or {}
                try:
                    if int(cfg.get('channel_id')) == cid:
                        return s.id
                except Exception:
                    continue
        return None

    def find_flexray_source_by_channel(self, channel_id: int) -> Optional[str]:
        try:
            cid = int(channel_id)
        except Exception:
            return None
        with self._lock:
            for s in self._sources.values():
                if s.type != 'FlexRay':
                    continue
                cfg = s.config or {}
                try:
                    if int(cfg.get('channel_id')) == cid:
                        return s.id
                except Exception:
                    continue
        return None
