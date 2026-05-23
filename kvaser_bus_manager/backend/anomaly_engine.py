from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from config_store import ConfigStore
from monitor_types import now_ms, new_id
from violation_logger import make_violation


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return float(int(x))
        return float(x)
    except Exception:
        return None


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return float(xs[mid])
    return float((xs[mid - 1] + xs[mid]) / 2.0)


def _mad(values: List[float], med: float) -> float:
    if not values:
        return 0.0
    dev = [abs(float(v) - float(med)) for v in values]
    return _median(dev)


class AnomalyEngine:
    """Lightweight anomaly scoring on decoded DBC signals.

    - Maintains a cache of last decoded values per (source_id, message, signal).
    - Periodically samples a vector and computes a robust z-score based score.
    - Model is trained explicitly via train_live() and stored under ConfigStore key 'ai_anomaly'.
    """

    CFG_KEY = 'ai_anomaly'

    def __init__(self, config_store: ConfigStore, *, socketio=None, anomaly_logger=None, violation_logger=None):
        self._store = config_store
        self._socketio = socketio
        self._alog = anomaly_logger
        self._vlog = violation_logger
        self._lock = threading.Lock()

        self._cfg: Dict[str, Any] = {}
        self._model: Dict[str, Any] = {}
        self._last_value: Dict[str, Tuple[float, int]] = {}
        self._last_sample_ts: Dict[str, int] = {}
        self._last_score: Dict[str, float] = {}
        self._last_violation_ts: Dict[str, float] = {}
        self._train_thread = None
        self._train_status: Dict[str, Any] = {'state': 'idle'}

        self.reload()

    def reload(self) -> None:
        cfg = self._store.get_config_only() or {}
        raw = cfg.get(self.CFG_KEY)
        if not isinstance(raw, dict):
            raw = {}

        # Defaults
        enabled = bool(raw.get('enabled', False))
        mode = str(raw.get('mode') or 'vector').strip().lower()
        if mode not in {'vector', 'compare'}:
            mode = 'vector'
        threshold = float(_safe_float(raw.get('threshold')) or 6.0)
        sample_every_ms = int(_safe_float(raw.get('sample_every_ms')) or 20)
        sample_every_ms = max(5, min(sample_every_ms, 1000))
        min_complete_ratio = float(_safe_float(raw.get('min_complete_ratio')) or 0.9)
        min_complete_ratio = max(0.1, min(min_complete_ratio, 1.0))
        emit_ws = bool(raw.get('emit_ws', True))
        log_db = bool(raw.get('log_db', True))

        # Optional bridge: anomaly -> violation
        violation_enabled = bool(raw.get('violation_enabled', False))
        violation_emit_ws = bool(raw.get('violation_emit_ws', True))
        violation_rule_id = str(raw.get('violation_rule_id') or 'ai_anomaly').strip() or 'ai_anomaly'
        violation_rule_name = str(raw.get('violation_rule_name') or 'AI anomaly').strip() or 'AI anomaly'
        violation_severity = str(raw.get('violation_severity') or 'warning').strip().lower() or 'warning'
        if violation_severity not in {'info', 'warning', 'critical'}:
            violation_severity = 'warning'
        try:
            _vd = _safe_float(raw.get('violation_debounce_s'))
            violation_debounce_s = 2.0 if _vd is None else float(_vd)
        except Exception:
            violation_debounce_s = 2.0
        violation_debounce_s = max(0.0, min(violation_debounce_s, 60.0))

        signals = raw.get('signals') if isinstance(raw.get('signals'), list) else []
        cleaned = []
        for s in signals:
            if not isinstance(s, dict):
                continue
            sid = str(s.get('source_id') or '').strip()
            msg = str(s.get('message') or '').strip()
            sig = str(s.get('signal') or '').strip()
            if not sid or not msg or not sig:
                continue
            cleaned.append({'source_id': sid, 'message': msg, 'signal': sig, 'unit': s.get('unit')})

        # Compare mode signals
        def _clean_ref(obj) -> Dict[str, Any]:
            if not isinstance(obj, dict):
                return {'source_id': '', 'message': '', 'signal': '', 'unit': None}
            return {
                'source_id': str(obj.get('source_id') or '').strip(),
                'message': str(obj.get('message') or '').strip(),
                'signal': str(obj.get('signal') or '').strip(),
                'unit': obj.get('unit'),
            }

        compare_a = _clean_ref(raw.get('compare_a'))
        compare_b = _clean_ref(raw.get('compare_b'))
        compare_op = str(raw.get('compare_op') or 'delta').strip().lower()
        if compare_op not in {'delta', 'delta_pct'}:
            compare_op = 'delta'

        model = raw.get('model') if isinstance(raw.get('model'), dict) else {}

        with self._lock:
            self._cfg = {
                'enabled': enabled,
                'mode': mode,
                'threshold': threshold,
                'sample_every_ms': sample_every_ms,
                'min_complete_ratio': min_complete_ratio,
                'emit_ws': emit_ws,
                'log_db': log_db,
                'violation_enabled': violation_enabled,
                'violation_emit_ws': violation_emit_ws,
                'violation_rule_id': violation_rule_id,
                'violation_rule_name': violation_rule_name,
                'violation_severity': violation_severity,
                'violation_debounce_s': violation_debounce_s,
                'signals': cleaned,
                'compare_a': compare_a,
                'compare_b': compare_b,
                'compare_op': compare_op,
            }
            self._model = model

    def _post_evt(self, evt: Dict[str, Any], *, emit_ws: bool, log_db: bool) -> None:
        if log_db and self._alog is not None:
            try:
                self._alog.log(evt)
            except Exception:
                pass
        if emit_ws and self._socketio is not None:
            try:
                self._socketio.emit('anomaly', evt)
            except Exception:
                pass

        # Optional: create a violation for this anomaly.
        try:
            with self._lock:
                v_enabled = bool(self._cfg.get('violation_enabled'))
                v_emit_ws = bool(self._cfg.get('violation_emit_ws'))
                v_rule_id = str(self._cfg.get('violation_rule_id') or 'ai_anomaly')
                v_rule_name = str(self._cfg.get('violation_rule_name') or 'AI anomaly')
                v_sev = str(self._cfg.get('violation_severity') or 'warning')
                v_debounce = float(self._cfg.get('violation_debounce_s') or 0.0)

            if (not v_enabled) or (self._vlog is None):
                return

            ts_ms = int(evt.get('ts_ms') or now_ms())
            source_id = str(evt.get('source_id') or '')
            details = evt.get('details') if isinstance(evt.get('details'), dict) else {}
            kind = str(details.get('kind') or 'vector')

            # Debounce key per source + model kind (and compare pair).
            pair = ''
            if kind == 'compare':
                pair = str(details.get('a_key') or '') + '|' + str(details.get('b_key') or '')
            key = f"{source_id}:{kind}:{pair}" if pair else f"{source_id}:{kind}"
            now_s = time.time()
            with self._lock:
                last_s = float(self._last_violation_ts.get(key) or 0.0)
                if (now_s - last_s) < float(v_debounce or 0.0):
                    return
                self._last_violation_ts[key] = now_s

            score = _safe_float(evt.get('score'))
            thr = _safe_float(evt.get('threshold'))
            desc = f"AI anomaly ({kind}) score={float(score or 0.0):.3f} > thr={float(thr or 0.0):.3f}"
            v = make_violation(
                ts_ms=ts_ms,
                rule_id=str(v_rule_id or 'ai_anomaly'),
                rule_name=str(v_rule_name or 'AI anomaly'),
                severity=str(v_sev or 'warning'),
                description=desc,
                a={
                    'kind': 'ai_anomaly',
                    'source_id': source_id,
                    'anomaly_id': str(evt.get('id') or ''),
                    'details': dict(details or {}),
                    'top': list(evt.get('top') or []),
                },
                b={'threshold': float(thr) if thr is not None else None},
                diff=float(score) if score is not None else None,
                threshold=float(thr) if thr is not None else None,
            )
            try:
                self._vlog.log(v)
            except Exception:
                pass
            try:
                if v_emit_ws and self._socketio is not None:
                    self._socketio.emit('violation', v.to_dict())
            except Exception:
                pass
        except Exception:
            return

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'ok': True,
                'config': dict(self._cfg),
                'model': dict(self._model) if isinstance(self._model, dict) else {},
                'train': dict(self._train_status),
            }

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError('patch must be an object')
        cur = self._store.get_config_only() or {}
        obj = cur.get(self.CFG_KEY)
        if not isinstance(obj, dict):
            obj = {}
        obj.update(patch)
        self._store.update({self.CFG_KEY: obj})
        self.reload()
        return self.get_config()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            kind = str(self._model.get('kind') or '').strip().lower()
            if kind == 'compare':
                model_ok = (self._model.get('median') is not None) and (self._model.get('mad') is not None) and bool(self._model.get('a_key')) and bool(self._model.get('b_key'))
            else:
                model_ok = bool(self._model.get('median')) and bool(self._model.get('mad')) and bool(self._model.get('keys'))
            return {
                'ok': True,
                'enabled': bool(self._cfg.get('enabled')),
                'mode': str(self._cfg.get('mode') or 'vector'),
                'signals': list(self._cfg.get('signals') or []),
                'compare_a': dict(self._cfg.get('compare_a') or {}),
                'compare_b': dict(self._cfg.get('compare_b') or {}),
                'compare_op': str(self._cfg.get('compare_op') or 'delta'),
                'threshold': float(self._cfg.get('threshold') or 0.0),
                'sample_every_ms': int(self._cfg.get('sample_every_ms') or 0),
                'min_complete_ratio': float(self._cfg.get('min_complete_ratio') or 0.0),
                'model_trained': model_ok,
                'model_trained_at_ms': int(self._model.get('trained_at_ms') or 0),
                'train': dict(self._train_status),
                'last_scores': dict(self._last_score),
            }

    def _signal_key(self, source_id: str, message: str, signal: str) -> str:
        return f"{source_id}:{message}:{signal}"

    def on_frame(self, frame: Dict[str, Any]) -> None:
        try:
            decoded = frame.get('decoded') if isinstance(frame, dict) else None
            if not isinstance(decoded, dict):
                return
            msg_name = str(decoded.get('name') or '').strip()
            sigs = decoded.get('signals')
            if not msg_name or not isinstance(sigs, dict):
                return

            # Resolve source_id for bus/channel (CAN + FlexRay).
            source_id = None
            try:
                sid = frame.get('source_id')
                if isinstance(sid, str) and sid.strip():
                    source_id = sid.strip()

                ft = str(frame.get('type') or 'CAN').strip().upper()
                if ft in {'FLEXRAY', 'FLEX', 'FR'}:
                    bus_type = 'FLEXRAY'
                else:
                    bus_type = 'CAN'

                ch_raw = frame.get('channel')
                try:
                    channel = int(ch_raw)
                except Exception:
                    channel = 0

                if not source_id:
                    resolver = frame.get('_source_id_resolver')
                    if callable(resolver):
                        source_id = resolver(bus_type, channel)
            except Exception:
                source_id = None
            if not source_id:
                return

            ts_ms = int(frame.get('timestamp') or now_ms())

            # Update cache
            for k, v in sigs.items():
                fv = _safe_float(v)
                if fv is None:
                    continue
                cache_key = self._signal_key(str(source_id), msg_name, str(k))
                with self._lock:
                    self._last_value[cache_key] = (float(fv), ts_ms)

            # Snapshot config/model
            with self._lock:
                enabled = bool(self._cfg.get('enabled'))
                mode = str(self._cfg.get('mode') or 'vector')
                sample_every_ms = int(self._cfg.get('sample_every_ms') or 20)
                threshold = float(self._cfg.get('threshold') or 0.0)
                min_complete_ratio = float(self._cfg.get('min_complete_ratio') or 1.0)
                signals = list(self._cfg.get('signals') or [])
                compare_a = dict(self._cfg.get('compare_a') or {})
                compare_b = dict(self._cfg.get('compare_b') or {})
                compare_op = str(self._cfg.get('compare_op') or 'delta')
                model = dict(self._model) if isinstance(self._model, dict) else {}
                emit_ws = bool(self._cfg.get('emit_ws'))
                log_db = bool(self._cfg.get('log_db'))

            if not enabled:
                return

            kind = str(model.get('kind') or '').strip().lower() or 'vector'
            if mode == 'compare' or kind == 'compare':
                a_key = str(model.get('a_key') or '')
                b_key = str(model.get('b_key') or '')
                if not a_key or not b_key:
                    # Derive from config if model is legacy/missing
                    a = compare_a
                    b = compare_b
                    if not (a.get('source_id') and a.get('message') and a.get('signal')):
                        return
                    if not (b.get('source_id') and b.get('message') and b.get('signal')):
                        return
                    a_key = self._signal_key(str(a['source_id']), str(a['message']), str(a['signal']))
                    b_key = self._signal_key(str(b['source_id']), str(b['message']), str(b['signal']))

                med_d = _safe_float(model.get('median'))
                mad_d = _safe_float(model.get('mad'))
                if med_d is None or mad_d is None:
                    return

                # Throttle per pair
                pair_k = f"compare:{a_key}|{b_key}"
                with self._lock:
                    prev = int(self._last_sample_ts.get(pair_k) or 0)
                    if (ts_ms - prev) < int(sample_every_ms):
                        return
                    self._last_sample_ts[pair_k] = ts_ms

                with self._lock:
                    a_vt = self._last_value.get(a_key)
                    b_vt = self._last_value.get(b_key)
                if not a_vt or not b_vt:
                    return
                a_val = float(a_vt[0])
                b_val = float(b_vt[0])
                if compare_op == 'delta_pct':
                    denom = abs(b_val) if abs(b_val) > 1e-9 else 1.0
                    dval = (a_val - b_val) / denom * 100.0
                else:
                    dval = a_val - b_val

                denom = max(float(mad_d) * 1.4826, 1e-6)
                z = (float(dval) - float(med_d)) / denom
                score = abs(float(z))

                with self._lock:
                    self._last_score[pair_k] = float(score)

                if float(score) <= float(threshold):
                    return

                evt = {
                    'id': new_id('anom'),
                    'ts_ms': ts_ms,
                    'source_id': str(source_id),
                    'score': float(score),
                    'threshold': float(threshold),
                    'top': [
                        {
                            'key': 'delta',
                            'abs_z': float(score),
                            'value': float(dval),
                            'median': float(med_d),
                            'mad': float(mad_d),
                        }
                    ],
                    'details': {
                        'kind': 'compare',
                        'compare_op': compare_op,
                        'a_key': a_key,
                        'b_key': b_key,
                        'a_value': a_val,
                        'b_value': b_val,
                        'delta': float(dval),
                    },
                }
            else:
                keys = model.get('keys')
                med = model.get('median')
                mad = model.get('mad')
                if not (isinstance(keys, list) and isinstance(med, list) and isinstance(mad, list)):
                    return
                if len(keys) == 0 or len(keys) != len(med) or len(keys) != len(mad):
                    return

                # Throttle per source_id
                last_k = str(source_id)
                with self._lock:
                    prev = int(self._last_sample_ts.get(last_k) or 0)
                    if (ts_ms - prev) < int(sample_every_ms):
                        return
                    self._last_sample_ts[last_k] = ts_ms

                # Build vector
                vec: List[float] = []
                present = 0
                for kk in keys:
                    try:
                        with self._lock:
                            val_ts = self._last_value.get(str(kk))
                        if not val_ts:
                            vec.append(0.0)
                            continue
                        vec.append(float(val_ts[0]))
                        present += 1
                    except Exception:
                        vec.append(0.0)

                ratio = float(present) / float(len(keys) or 1)
                if ratio < float(min_complete_ratio):
                    return

                # Score: mean(|z|) where z is robust z-score using MAD.
                # scale MAD -> sigma with 1.4826.
                zs: List[Tuple[float, int]] = []
                score_sum = 0.0
                for i, x in enumerate(vec):
                    m = float(med[i])
                    d = float(mad[i])
                    denom = max(float(d) * 1.4826, 1e-6)
                    z = (float(x) - m) / denom
                    az = abs(z)
                    score_sum += az
                    zs.append((az, i))
                score = score_sum / float(len(vec) or 1)

                with self._lock:
                    self._last_score[str(source_id)] = float(score)

                if float(score) <= float(threshold):
                    return

                # Top contributors
                zs.sort(reverse=True, key=lambda t: t[0])
                top = []
                for az, idx in zs[:8]:
                    try:
                        sig_key = str(keys[idx])
                    except Exception:
                        sig_key = f"dim_{idx}"
                    top.append({'key': sig_key, 'abs_z': float(az), 'value': float(vec[idx]), 'median': float(med[idx]), 'mad': float(mad[idx])})

                evt = {
                    'id': new_id('anom'),
                    'ts_ms': ts_ms,
                    'source_id': str(source_id),
                    'score': float(score),
                    'threshold': float(threshold),
                    'top': top,
                    'details': {
                        'kind': 'vector',
                        'present_ratio': ratio,
                        'keys_n': int(len(keys)),
                    },
                }

            self._post_evt(evt, emit_ws=emit_ws, log_db=log_db)

        except Exception:
            return

    def train_live(self, *, duration_s: float = 120.0, max_samples: int = 2000) -> Dict[str, Any]:
        """Collect samples from current live cache and build a robust median/MAD model."""
        try:
            duration_s = float(duration_s)
        except Exception:
            duration_s = 120.0
        duration_s = max(1.0, min(duration_s, 600.0))
        max_samples = int(max_samples or 2000)
        max_samples = max(50, min(max_samples, 50000))

        with self._lock:
            if self._train_thread is not None and getattr(self._train_thread, 'is_alive', lambda: False)():
                return {'ok': True, 'train': dict(self._train_status), 'message': 'training already in progress'}

            mode = str(self._cfg.get('mode') or 'vector')
            signals = list(self._cfg.get('signals') or [])
            compare_a = dict(self._cfg.get('compare_a') or {})
            compare_b = dict(self._cfg.get('compare_b') or {})
            compare_op = str(self._cfg.get('compare_op') or 'delta')
            sample_every_ms = int(self._cfg.get('sample_every_ms') or 20)

        if mode == 'compare':
            if not (compare_a.get('source_id') and compare_a.get('message') and compare_a.get('signal')):
                raise ValueError('compare signal A is incomplete')
            if not (compare_b.get('source_id') and compare_b.get('message') and compare_b.get('signal')):
                raise ValueError('compare signal B is incomplete')
            a_key = self._signal_key(str(compare_a['source_id']), str(compare_a['message']), str(compare_a['signal']))
            b_key = self._signal_key(str(compare_b['source_id']), str(compare_b['message']), str(compare_b['signal']))
        else:
            if not signals:
                raise ValueError('no signals configured')
            keys = [self._signal_key(str(s['source_id']), str(s['message']), str(s['signal'])) for s in signals]

        def worker():
            with self._lock:
                self._train_status = {'state': 'running', 'started_at_ms': now_ms(), 'samples': 0, 'duration_s': duration_s}

            samples_vec: List[List[float]] = []
            samples_d: List[float] = []
            start = time.time()
            last_ms = 0
            while True:
                if (time.time() - start) >= duration_s:
                    break
                if (len(samples_d) + len(samples_vec)) >= max_samples:
                    break

                ts_ms = now_ms()
                if last_ms and (ts_ms - last_ms) < sample_every_ms:
                    time.sleep(0.002)
                    continue
                last_ms = ts_ms

                if mode == 'compare':
                    with self._lock:
                        a_vt = self._last_value.get(a_key)
                        b_vt = self._last_value.get(b_key)
                    if (a_vt is None) or (b_vt is None):
                        time.sleep(0.01)
                        continue
                    a_val = float(a_vt[0])
                    b_val = float(b_vt[0])
                    if compare_op == 'delta_pct':
                        denom = abs(b_val) if abs(b_val) > 1e-9 else 1.0
                        dval = (a_val - b_val) / denom * 100.0
                    else:
                        dval = a_val - b_val
                    samples_d.append(float(dval))
                    with self._lock:
                        self._train_status['samples'] = int(len(samples_d))
                else:
                    row: List[float] = []
                    present = 0
                    for kk in keys:
                        with self._lock:
                            vt = self._last_value.get(kk)
                        if vt is None:
                            row.append(0.0)
                            continue
                        row.append(float(vt[0]))
                        present += 1

                    # Require at least 70% present during training
                    if (float(present) / float(len(keys) or 1)) < 0.7:
                        time.sleep(0.01)
                        continue

                    samples_vec.append(row)
                    with self._lock:
                        self._train_status['samples'] = int(len(samples_vec))

            if (mode == 'compare' and not samples_d) or (mode != 'compare' and not samples_vec):
                with self._lock:
                    self._train_status = {'state': 'error', 'error': 'no samples collected', 'finished_at_ms': now_ms()}
                return

            if mode == 'compare':
                v = [float(x) for x in samples_d]
                m = _median(v)
                d = _mad(v, m)
                model = {
                    'kind': 'compare',
                    'a_key': a_key,
                    'b_key': b_key,
                    'compare_op': compare_op,
                    'median': float(m),
                    'mad': float(d),
                    'trained_at_ms': now_ms(),
                    'samples': int(len(samples_d)),
                }
            else:
                # Compute per-dimension median and MAD.
                cols = list(zip(*samples_vec))
                median = []
                mad = []
                for col in cols:
                    vv = [float(x) for x in col]
                    m = _median(vv)
                    d = _mad(vv, m)
                    median.append(float(m))
                    mad.append(float(d))
                model = {
                    'kind': 'vector',
                    'keys': keys,
                    'median': median,
                    'mad': mad,
                    'trained_at_ms': now_ms(),
                    'samples': int(len(samples_vec)),
                }

            # Persist into ConfigStore
            try:
                cur = self._store.get_config_only() or {}
                obj = cur.get(self.CFG_KEY)
                if not isinstance(obj, dict):
                    obj = {}
                obj['model'] = model
                self._store.update({self.CFG_KEY: obj})
            except Exception as e:
                with self._lock:
                    self._train_status = {'state': 'error', 'error': str(e), 'finished_at_ms': now_ms()}
                return

            self.reload()
            with self._lock:
                done_n = int(len(samples_d) if mode == 'compare' else len(samples_vec))
                self._train_status = {'state': 'done', 'finished_at_ms': now_ms(), 'samples': done_n}

        t = threading.Thread(target=worker, daemon=True)
        with self._lock:
            self._train_thread = t
        t.start()
        return {'ok': True, 'train': dict(self._train_status)}
