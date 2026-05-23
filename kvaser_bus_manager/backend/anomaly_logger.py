from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from monitor_types import new_id, now_ms


class AnomalyLogger:
    """Persists anomaly events to SQLite (+ optional daily CSV)."""

    def __init__(self, base_dir: str, *, enable_csv: bool = True):
        self.base_dir = os.path.abspath(str(base_dir))
        os.makedirs(self.base_dir, exist_ok=True)
        self.enable_csv = bool(enable_csv)
        self._lock = threading.Lock()
        self._db_path = os.path.join(self.base_dir, 'anomalies.db')
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
        con.execute('PRAGMA journal_mode=WAL;')
        con.execute('PRAGMA synchronous=NORMAL;')
        return con

    def _init_db(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS anomalies (
                        id TEXT PRIMARY KEY,
                        ts_ms INTEGER NOT NULL,
                        source_id TEXT NOT NULL,
                        score REAL NOT NULL,
                        threshold REAL NOT NULL,
                        top_json TEXT,
                        details_json TEXT
                    );
                    """
                )
                con.execute('CREATE INDEX IF NOT EXISTS idx_anom_ts ON anomalies(ts_ms);')
                con.execute('CREATE INDEX IF NOT EXISTS idx_anom_src ON anomalies(source_id);')
                con.commit()
            finally:
                con.close()

    def _csv_path_for_day(self, ts_ms: int) -> str:
        try:
            t = time.localtime((int(ts_ms) or now_ms()) / 1000.0)
            day = time.strftime('%Y%m%d', t)
        except Exception:
            day = time.strftime('%Y%m%d')
        return os.path.join(self.base_dir, f"anomalies_{day}.csv")

    def log(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Insert an event and return the stored object."""
        if not isinstance(event, dict):
            raise ValueError('event must be an object')

        ts_ms = int(event.get('ts_ms') or now_ms())
        out = {
            'id': str(event.get('id') or new_id('anom')),
            'ts_ms': ts_ms,
            'source_id': str(event.get('source_id') or ''),
            'score': float(event.get('score') or 0.0),
            'threshold': float(event.get('threshold') or 0.0),
            'top': event.get('top') if isinstance(event.get('top'), list) else [],
            'details': event.get('details') if isinstance(event.get('details'), dict) else {},
        }
        if not out['source_id']:
            out['source_id'] = 'unknown'

        top_json = json.dumps(out['top'], ensure_ascii=False)
        details_json = json.dumps(out['details'], ensure_ascii=False)

        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    'INSERT OR REPLACE INTO anomalies (id, ts_ms, source_id, score, threshold, top_json, details_json) VALUES (?,?,?,?,?,?,?)',
                    (
                        out['id'],
                        int(out['ts_ms'] or 0),
                        out['source_id'],
                        float(out['score'] or 0.0),
                        float(out['threshold'] or 0.0),
                        top_json,
                        details_json,
                    ),
                )
                con.commit()
            finally:
                con.close()

        if self.enable_csv:
            try:
                path = self._csv_path_for_day(ts_ms)
                is_new = not os.path.isfile(path)
                with open(path, 'a', newline='', encoding='utf-8') as f:
                    w = csv.writer(f)
                    if is_new:
                        w.writerow(['ts_ms', 'source_id', 'score', 'threshold', 'top_json'])
                    w.writerow([out['ts_ms'], out['source_id'], out['score'], out['threshold'], top_json])
            except Exception:
                pass

        return out

    def query(
        self,
        *,
        source_id: Optional[str] = None,
        since_ms: Optional[int] = None,
        until_ms: Optional[int] = None,
        limit: int = 200,
        desc: bool = True,
    ) -> Dict[str, Any]:
        limit = int(limit or 200)
        limit = max(1, min(limit, 5000))

        where = []
        args: List[Any] = []
        if source_id:
            where.append('source_id = ?')
            args.append(str(source_id))
        if since_ms is not None:
            where.append('ts_ms >= ?')
            args.append(int(since_ms))
        if until_ms is not None:
            where.append('ts_ms <= ?')
            args.append(int(until_ms))

        sql = 'SELECT id, ts_ms, source_id, score, threshold, top_json, details_json FROM anomalies'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY ts_ms ' + ('DESC' if desc else 'ASC')
        sql += ' LIMIT ?'
        args.append(limit)

        with self._lock:
            con = self._connect()
            try:
                cur = con.execute(sql, args)
                rows = cur.fetchall()
            finally:
                con.close()

        items = []
        for r in rows:
            try:
                top = json.loads(r[5]) if r[5] else []
            except Exception:
                top = []
            try:
                details = json.loads(r[6]) if r[6] else {}
            except Exception:
                details = {}
            items.append(
                {
                    'id': r[0],
                    'ts_ms': int(r[1] or 0),
                    'source_id': r[2],
                    'score': float(r[3] or 0.0),
                    'threshold': float(r[4] or 0.0),
                    'top': top,
                    'details': details,
                }
            )

        return {'ok': True, 'items': items, 'total': len(items)}
