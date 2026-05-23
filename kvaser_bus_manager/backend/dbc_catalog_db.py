from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os
import sqlite3
import threading
import time


class DbcCatalogDb:
    """Persists a DBC catalog (messages/signals + comments) to SQLite.

    This is useful to serve a stable, queryable list even when the DBC parsing
    is expensive or when you want a persistent snapshot.
    """

    def __init__(self, *, base_dir: str):
        self._base_dir = os.path.abspath(base_dir)
        os.makedirs(self._base_dir, exist_ok=True)
        self._db_path = os.path.join(self._base_dir, 'dbc_catalog.db')
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dbcs (
                    dbc_name TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    mtime REAL,
                    size INTEGER,
                    imported_ts_ms INTEGER NOT NULL,
                    messages_count INTEGER NOT NULL,
                    signals_count INTEGER NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    dbc_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    frame_id INTEGER NOT NULL,
                    length INTEGER NOT NULL,
                    comment TEXT,
                    PRIMARY KEY (dbc_name, name)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_msg_dbc ON messages(dbc_name)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_msg_frame ON messages(dbc_name, frame_id)")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    dbc_name TEXT NOT NULL,
                    msg_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    unit TEXT,
                    comment TEXT,
                    PRIMARY KEY (dbc_name, msg_name, name)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_sig_dbc ON signals(dbc_name)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sig_msg ON signals(dbc_name, msg_name)")

    def get_dbc_meta(self, dbc_name: str) -> Optional[Dict[str, Any]]:
        dbc_name = str(dbc_name or '').strip()
        if not dbc_name:
            return None
        with self._lock:
            with self._connect() as con:
                row = con.execute(
                    "SELECT * FROM dbcs WHERE dbc_name = ?",
                    (dbc_name,),
                ).fetchone()
        if not row:
            return None
        return {
            'dbc_name': row['dbc_name'],
            'path': row['path'],
            'mtime': row['mtime'],
            'size': row['size'],
            'imported_ts_ms': int(row['imported_ts_ms']),
            'messages_count': int(row['messages_count']),
            'signals_count': int(row['signals_count']),
        }

    def list_dbcs(self) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT * FROM dbcs ORDER BY imported_ts_ms DESC"
                ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                'dbc_name': r['dbc_name'],
                'path': r['path'],
                'mtime': r['mtime'],
                'size': r['size'],
                'imported_ts_ms': int(r['imported_ts_ms']),
                'messages_count': int(r['messages_count']),
                'signals_count': int(r['signals_count']),
            })
        return out

    def import_dbc_file(
        self,
        *,
        dbc_name: str,
        path: str,
        include_signals: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        dbc_name = str(dbc_name or '').strip()
        path = os.path.abspath(str(path or '').strip())
        if not dbc_name:
            return {'ok': False, 'error': 'missing dbc_name'}
        if not os.path.isfile(path):
            return {'ok': False, 'error': 'dbc not found'}

        try:
            st = os.stat(path)
            mtime = float(st.st_mtime)
            size = int(st.st_size)
        except Exception:
            mtime = None
            size = None

        if not force:
            prev = self.get_dbc_meta(dbc_name)
            if prev and prev.get('mtime') == mtime and prev.get('size') == size:
                return {
                    'ok': True,
                    'dbc_name': dbc_name,
                    'imported': False,
                    'skipped': True,
                    'reason': 'unchanged',
                    'messages_count': int(prev.get('messages_count') or 0),
                    'signals_count': int(prev.get('signals_count') or 0),
                }

        from dbc_loader import load_dbc_database

        db = load_dbc_database(path)
        messages = list(getattr(db, 'messages', None) or [])

        msgs_rows: List[Tuple[Any, ...]] = []
        sigs_rows: List[Tuple[Any, ...]] = []

        for m in messages:
            msg_name = getattr(m, 'name', None)
            if not msg_name:
                continue
            msgs_rows.append(
                (
                    dbc_name,
                    str(msg_name),
                    int(getattr(m, 'frame_id', 0) or 0),
                    int(getattr(m, 'length', 0) or 0),
                    getattr(m, 'comment', None),
                )
            )
            if include_signals:
                for s in (getattr(m, 'signals', None) or []):
                    sig_name = getattr(s, 'name', None)
                    if not sig_name:
                        continue
                    sigs_rows.append(
                        (
                            dbc_name,
                            str(msg_name),
                            str(sig_name),
                            getattr(s, 'unit', None),
                            getattr(s, 'comment', None),
                        )
                    )

        imported_ts_ms = int(time.time() * 1000)
        with self._lock:
            with self._connect() as con:
                con.execute('BEGIN')
                con.execute('DELETE FROM signals WHERE dbc_name = ?', (dbc_name,))
                con.execute('DELETE FROM messages WHERE dbc_name = ?', (dbc_name,))
                con.execute('DELETE FROM dbcs WHERE dbc_name = ?', (dbc_name,))

                if msgs_rows:
                    con.executemany(
                        'INSERT INTO messages (dbc_name, name, frame_id, length, comment) VALUES (?, ?, ?, ?, ?)',
                        msgs_rows,
                    )
                if sigs_rows:
                    con.executemany(
                        'INSERT INTO signals (dbc_name, msg_name, name, unit, comment) VALUES (?, ?, ?, ?, ?)',
                        sigs_rows,
                    )

                con.execute(
                    'INSERT INTO dbcs (dbc_name, path, mtime, size, imported_ts_ms, messages_count, signals_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        dbc_name,
                        path,
                        mtime,
                        size,
                        imported_ts_ms,
                        int(len(msgs_rows)),
                        int(len(sigs_rows)),
                    ),
                )
                con.commit()

        return {
            'ok': True,
            'dbc_name': dbc_name,
            'imported': True,
            'skipped': False,
            'messages_count': int(len(msgs_rows)),
            'signals_count': int(len(sigs_rows)),
        }

    def get_catalog(
        self,
        *,
        dbc_name: str,
        include_signals: bool = False,
        max_messages: int = 500,
        max_signals_per_msg: int = 200,
    ) -> Dict[str, Any]:
        dbc_name = str(dbc_name or '').strip()
        if not dbc_name:
            return {'ok': False, 'error': 'missing dbc_name'}

        max_messages = max(1, min(int(max_messages or 500), 5000))
        max_signals_per_msg = max(1, min(int(max_signals_per_msg or 200), 5000))

        with self._lock:
            with self._connect() as con:
                msgs = con.execute(
                    """
                    SELECT name, frame_id, length, comment
                    FROM messages
                    WHERE dbc_name = ?
                    ORDER BY frame_id ASC
                    LIMIT ?
                    """,
                    (dbc_name, max_messages),
                ).fetchall()

                out_msgs: List[Dict[str, Any]] = []
                if include_signals:
                    for m in msgs:
                        sigs = con.execute(
                            """
                            SELECT name, unit, comment
                            FROM signals
                            WHERE dbc_name = ? AND msg_name = ?
                            ORDER BY name ASC
                            LIMIT ?
                            """,
                            (dbc_name, m['name'], max_signals_per_msg),
                        ).fetchall()
                        out_msgs.append({
                            'name': m['name'],
                            'frame_id': int(m['frame_id']),
                            'length': int(m['length']),
                            'comment': m['comment'],
                            'signals': [
                                {'name': s['name'], 'unit': s['unit'], 'comment': s['comment']}
                                for s in sigs
                            ],
                        })
                else:
                    out_msgs = [
                        {
                            'name': m['name'],
                            'frame_id': int(m['frame_id']),
                            'length': int(m['length']),
                            'comment': m['comment'],
                        }
                        for m in msgs
                    ]

        return {
            'ok': True,
            'dbc_name': dbc_name,
            'count': int(len(out_msgs)),
            'messages': out_msgs,
        }

    def get_signals_for_message(
        self,
        *,
        dbc_name: str,
        msg_name: str,
        limit: int = 2000,
        offset: int = 0,
    ) -> Dict[str, Any]:
        dbc_name = str(dbc_name or '').strip()
        msg_name = str(msg_name or '').strip()
        if not dbc_name:
            return {'ok': False, 'error': 'missing dbc_name'}
        if not msg_name:
            return {'ok': False, 'error': 'missing message'}

        limit = max(1, min(int(limit or 2000), 5000))
        offset = max(0, int(offset or 0))

        with self._lock:
            with self._connect() as con:
                total = con.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM signals
                    WHERE dbc_name = ? AND msg_name = ?
                    """,
                    (dbc_name, msg_name),
                ).fetchone()['c']
                rows = con.execute(
                    """
                    SELECT name, unit, comment
                    FROM signals
                    WHERE dbc_name = ? AND msg_name = ?
                    ORDER BY name ASC
                    LIMIT ? OFFSET ?
                    """,
                    (dbc_name, msg_name, limit, offset),
                ).fetchall()

        items = [{'name': r['name'], 'unit': r['unit'], 'comment': r['comment']} for r in rows]
        return {
            'ok': True,
            'dbc_name': dbc_name,
            'message': msg_name,
            'total': int(total),
            'limit': int(limit),
            'offset': int(offset),
            'signals': items,
        }

    def search_signals(
        self,
        *,
        query: str,
        dbc_name: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Search signals by keyword across name/comments (and message name/comments).

        This is intentionally deterministic (no LLM) to support reliable UX and Copilot grounding.
        """
        q = str(query or '').strip()
        if not q:
            return {'ok': False, 'error': 'missing query'}

        dbc = str(dbc_name or '').strip() or None
        if dbc and not dbc:
            dbc = None

        # Tokenize lightly; keep order, drop empties.
        terms = [t.strip().lower() for t in q.replace('\t', ' ').split(' ') if t.strip()]
        # Avoid pathological queries.
        terms = terms[:8]
        if not terms:
            return {'ok': False, 'error': 'missing query'}

        limit = max(1, min(int(limit or 50), 200))

        where = []
        params: List[Any] = []
        if dbc:
            where.append('s.dbc_name = ?')
            params.append(dbc)

        # Match ANY term (OR). This is much more usable for natural-language queries
        # where we may add synonyms (e.g., marcia/gear/gangposition/prnd).
        term_clauses = []
        for t in terms:
            like = f"%{t}%"
            term_clauses.append(
                "(lower(s.name) LIKE ? OR lower(coalesce(s.comment,'')) LIKE ? OR "
                "lower(s.msg_name) LIKE ? OR lower(coalesce(m.comment,'')) LIKE ?)"
            )
            params.extend([like, like, like, like])

        if term_clauses:
            where.append('(' + ' OR '.join(term_clauses) + ')')

        where_sql = ' AND '.join(where) if where else '1=1'

        # Relevance scoring: prefer matches in signal name, then signal comment,
        # then message name/comment. (Deterministic + fast enough for SQLite.)
        score_parts = []
        score_params: List[Any] = []
        for t in terms:
            like = f"%{t}%"
            score_parts.append("CASE WHEN lower(s.name) LIKE ? THEN 10 ELSE 0 END")
            score_parts.append("CASE WHEN lower(coalesce(s.comment,'')) LIKE ? THEN 6 ELSE 0 END")
            score_parts.append("CASE WHEN lower(s.msg_name) LIKE ? THEN 2 ELSE 0 END")
            score_parts.append("CASE WHEN lower(coalesce(m.comment,'')) LIKE ? THEN 1 ELSE 0 END")
            score_params.extend([like, like, like, like])
        score_sql = ' + '.join(score_parts) if score_parts else '0'

        sql = f"""
            SELECT
                ({score_sql}) AS score,
                s.dbc_name AS dbc_name,
                s.msg_name AS msg_name,
                m.frame_id AS frame_id,
                m.length AS length,
                m.comment AS msg_comment,
                s.name AS signal,
                s.unit AS unit,
                s.comment AS sig_comment
            FROM signals s
            LEFT JOIN messages m
              ON m.dbc_name = s.dbc_name AND m.name = s.msg_name
            WHERE {where_sql}
            ORDER BY score DESC, s.msg_name ASC, s.name ASC
            LIMIT ?
        """
        exec_params: List[Any] = []
        exec_params.extend(score_params)
        exec_params.extend(params)
        exec_params.append(limit)

        with self._lock:
            with self._connect() as con:
                rows = con.execute(sql, tuple(exec_params)).fetchall()

        items = []
        for r in rows:
            items.append(
                {
                    'score': int(r['score'] or 0),
                    'dbc_name': r['dbc_name'],
                    'message': r['msg_name'],
                    'frame_id': int(r['frame_id'] or 0),
                    'length': int(r['length'] or 0),
                    'message_comment': r['msg_comment'],
                    'signal': r['signal'],
                    'unit': r['unit'],
                    'signal_comment': r['sig_comment'],
                }
            )

        return {
            'ok': True,
            'dbc_name': dbc,
            'query': q,
            'terms': terms,
            'terms_mode': 'or',
            'count': int(len(items)),
            'items': items,
        }
