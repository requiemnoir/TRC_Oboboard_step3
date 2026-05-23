from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import csv
import os
import sqlite3
import threading
import time

from monitor_types import Violation, new_id


class ViolationLogger:
    """Persists violations to SQLite and optionally to daily CSV.

    SQLite is used for fast filtering/pagination for the dashboard.
    """

    def __init__(self, *, base_dir: str, enable_csv: bool = True):
        self._base_dir = os.path.abspath(base_dir)
        os.makedirs(self._base_dir, exist_ok=True)
        self._db_path = os.path.join(self._base_dir, 'violations.db')
        self._enable_csv = bool(enable_csv)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS violations (
                    id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    description TEXT NOT NULL,
                    a_json TEXT NOT NULL,
                    b_json TEXT NOT NULL,
                    diff REAL,
                    threshold REAL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_viol_ts ON violations(ts_ms)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_viol_rule ON violations(rule_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_viol_sev ON violations(severity)")

    def _csv_path_for_day(self, ts_ms: int) -> str:
        day = time.strftime('%Y%m%d', time.localtime(ts_ms / 1000.0))
        return os.path.join(self._base_dir, f'violations_{day}.csv')

    def log(self, v: Violation) -> None:
        import json
        row = v.to_dict()
        with self._lock:
            with self._connect() as con:
                con.execute(
                    """INSERT OR REPLACE INTO violations
                    (id, ts_ms, rule_id, rule_name, severity, description, a_json, b_json, diff, threshold)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row['id'],
                        int(row['ts_ms']),
                        row['rule_id'],
                        row['rule_name'],
                        row['severity'],
                        row['description'],
                        json.dumps(row.get('a') or {}, ensure_ascii=False),
                        json.dumps(row.get('b') or {}, ensure_ascii=False),
                        row.get('diff'),
                        row.get('threshold'),
                    ),
                )

            if self._enable_csv:
                path = self._csv_path_for_day(int(row['ts_ms']))
                is_new = not os.path.isfile(path)
                with open(path, 'a', encoding='utf-8', newline='') as f:
                    w = csv.writer(f)
                    if is_new:
                        w.writerow(['ts_ms', 'severity', 'rule_id', 'rule_name', 'description', 'diff', 'threshold'])
                    w.writerow([
                        int(row['ts_ms']),
                        row['severity'],
                        row['rule_id'],
                        row['rule_name'],
                        row['description'],
                        row.get('diff'),
                        row.get('threshold'),
                    ])

    def query(
        self,
        *,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        severity: Optional[str] = None,
        rule_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        desc: bool = True,
    ) -> Dict[str, Any]:
        import json

        limit = max(1, min(int(limit or 200), 2000))
        offset = max(0, int(offset or 0))
        where = []
        args: List[Any] = []

        if start_ms is not None:
            where.append('ts_ms >= ?')
            args.append(int(start_ms))
        if end_ms is not None:
            where.append('ts_ms <= ?')
            args.append(int(end_ms))
        if severity:
            where.append('severity = ?')
            args.append(str(severity))
        if rule_id:
            where.append('rule_id = ?')
            args.append(str(rule_id))

        wsql = (' WHERE ' + ' AND '.join(where)) if where else ''
        order = 'DESC' if desc else 'ASC'

        with self._lock:
            with self._connect() as con:
                total = con.execute(f'SELECT COUNT(*) AS c FROM violations{wsql}', tuple(args)).fetchone()['c']
                rows = con.execute(
                    f"SELECT * FROM violations{wsql} ORDER BY ts_ms {order} LIMIT ? OFFSET ?",
                    tuple(args + [limit, offset]),
                ).fetchall()

        out = []
        for r in rows:
            out.append({
                'id': r['id'],
                'ts_ms': int(r['ts_ms']),
                'rule_id': r['rule_id'],
                'rule_name': r['rule_name'],
                'severity': r['severity'],
                'description': r['description'],
                'a': json.loads(r['a_json'] or '{}'),
                'b': json.loads(r['b_json'] or '{}'),
                'diff': r['diff'],
                'threshold': r['threshold'],
            })

        return {'ok': True, 'total': int(total), 'items': out, 'limit': limit, 'offset': offset}

    def stats_last_24h(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3600 * 1000
        with self._lock:
            with self._connect() as con:
                rows = con.execute(
                    """
                    SELECT severity, COUNT(*) AS c
                    FROM violations
                    WHERE ts_ms >= ?
                    GROUP BY severity
                    """,
                    (start_ms,),
                ).fetchall()
                by_sev = {r['severity']: int(r['c']) for r in rows}
                total = int(sum(by_sev.values()))

        return {'ok': True, 'window': '24h', 'total': total, 'by_severity': by_sev}

    def clear(self, *, delete_csv: bool = False) -> Dict[str, Any]:
        """Delete all stored violations.

        This clears the SQLite history used by the dashboard.
        If delete_csv=True, also deletes violations_YYYYMMDD.csv files in base_dir.
        """
        deleted = 0
        with self._lock:
            with self._connect() as con:
                try:
                    deleted = int(con.execute('SELECT COUNT(*) AS c FROM violations').fetchone()['c'])
                except Exception:
                    deleted = 0
                try:
                    con.execute('DELETE FROM violations')
                    con.commit()
                except Exception:
                    pass
                # Best-effort vacuum to keep db small.
                try:
                    con.execute('VACUUM')
                except Exception:
                    pass

        csv_deleted = 0
        if bool(delete_csv):
            try:
                for name in os.listdir(self._base_dir):
                    if not isinstance(name, str):
                        continue
                    low = name.lower()
                    if not (low.startswith('violations_') and low.endswith('.csv')):
                        continue
                    try:
                        os.remove(os.path.join(self._base_dir, name))
                        csv_deleted += 1
                    except Exception:
                        continue
            except Exception:
                pass

        return {'ok': True, 'deleted': int(deleted), 'csv_deleted': int(csv_deleted)}


def make_violation(
    *,
    ts_ms: int,
    rule_id: str,
    rule_name: str,
    severity: str,
    description: str,
    a: Dict[str, Any],
    b: Dict[str, Any],
    diff: Optional[float],
    threshold: Optional[float],
) -> Violation:
    return Violation(
        id=new_id('viol'),
        ts_ms=int(ts_ms),
        rule_id=str(rule_id),
        rule_name=str(rule_name),
        severity=severity,  # type: ignore[arg-type]
        description=str(description),
        a=dict(a or {}),
        b=dict(b or {}),
        diff=diff,
        threshold=threshold,
    )
