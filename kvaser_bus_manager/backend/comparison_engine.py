from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import threading
import time

from config_store import ConfigStore
from monitor_types import (
    ComparisonRule,
    RuleAction,
    SignalRef,
    new_id,
    now_ms,
)
from violation_logger import ViolationLogger, make_violation


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return float(int(x))
        value = getattr(x, 'value', None)
        if value is not None:
            if isinstance(value, bool):
                return float(int(value))
            return float(value)
        return float(x)
    except Exception:
        return None


def _float_or_default(value: Any, default: float) -> float:
    v = _safe_float(value)
    return float(v) if v is not None else float(default)


class ComparisonEngine:
    """Evaluates comparison rules against decoded signal streams.

    Persistence: stored under ConfigStore key 'comparison_rules'.

    Integration point: call on_frame(frame_dict) from BusManager listener.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        violation_logger: ViolationLogger,
        *,
        socketio=None,
    ):
        self._store = config_store
        self._vlog = violation_logger
        self._socketio = socketio
        self._lock = threading.Lock()
        self._rules: Dict[str, ComparisonRule] = {}
        self._last_value: Dict[str, Tuple[float, int]] = {}
        self._last_violation_ts: Dict[str, float] = {}
        self.reload()

    def reload(self) -> None:
        cfg = self._store.get_config_only() or {}
        raw = cfg.get('comparison_rules')
        rules: Dict[str, ComparisonRule] = {}
        if isinstance(raw, list):
            for obj in raw:
                if not isinstance(obj, dict):
                    continue
                try:
                    rid = str(obj.get('id') or '').strip() or new_id('rule')
                    name = str(obj.get('name') or rid).strip()[:64]
                    enabled = bool(obj.get('enabled', True))
                    severity = str(obj.get('severity') or 'warning').strip().lower()
                    if severity not in {'info', 'warning', 'critical'}:
                        severity = 'warning'

                    a0 = obj.get('a') if isinstance(obj.get('a'), dict) else {}
                    a = SignalRef(
                        source_id=str(a0.get('source_id') or ''),
                        message=str(a0.get('message') or ''),
                        signal=str(a0.get('signal') or ''),
                        unit=a0.get('unit'),
                    )

                    op = str(obj.get('op') or 'delta_abs').strip().lower()
                    if op not in {'lt', 'gt', 'eq', 'le', 'ge', 'ne', 'delta_abs', 'delta_pct', 'missing'}:
                        op = 'delta_abs'

                    b_kind = str(obj.get('b_kind') or 'const').strip().lower()
                    if b_kind not in {'signal', 'const'}:
                        b_kind = 'const'

                    b = None
                    if b_kind == 'signal' and isinstance(obj.get('b'), dict):
                        b0 = obj.get('b') or {}
                        b = SignalRef(
                            source_id=str(b0.get('source_id') or ''),
                            message=str(b0.get('message') or ''),
                            signal=str(b0.get('signal') or ''),
                            unit=b0.get('unit'),
                        )

                    b_const = _safe_float(obj.get('b_const'))
                    threshold = float(_safe_float(obj.get('threshold')) or 0.0)
                    debounce_s = _float_or_default(obj.get('debounce_s'), 2.0)
                    missing_timeout_s = _float_or_default(obj.get('missing_timeout_s'), 0.5)

                    actions: List[RuleAction] = []
                    raw_actions = obj.get('actions') if isinstance(obj.get('actions'), list) else []
                    for act in raw_actions:
                        if not isinstance(act, dict):
                            continue
                        kind = str(act.get('kind') or '').strip()
                        if kind not in {'log_csv', 'emit_ws', 'beep'}:
                            continue
                        params = act.get('params') if isinstance(act.get('params'), dict) else {}
                        actions.append(RuleAction(kind=kind, params=dict(params)))

                    if not actions:
                        actions = [RuleAction(kind='log_csv'), RuleAction(kind='emit_ws')]

                    rules[rid] = ComparisonRule(
                        id=rid,
                        name=name,
                        enabled=enabled,
                        severity=severity,  # type: ignore[arg-type]
                        a=a,
                        op=op,  # type: ignore[arg-type]
                        b_kind=b_kind,  # type: ignore[arg-type]
                        b=b,
                        b_const=b_const,
                        threshold=threshold,
                        debounce_s=debounce_s,
                        missing_timeout_s=missing_timeout_s,
                        conditions=obj.get('conditions') if isinstance(obj.get('conditions'), list) else [],
                        conditions_mode=str(obj.get('conditions_mode') or 'and') if str(obj.get('conditions_mode') or 'and') in {'and', 'or'} else 'and',
                        actions=actions,
                        created_at_ms=int(obj.get('created_at_ms') or now_ms()),
                        updated_at_ms=int(obj.get('updated_at_ms') or now_ms()),
                    )
                except Exception:
                    continue

        with self._lock:
            self._rules = rules

    def _save(self) -> None:
        with self._lock:
            payload = [r.to_dict() for r in self._rules.values()]
        self._store.update({'comparison_rules': payload})

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in sorted(self._rules.values(), key=lambda x: x.name)]

    def upsert_rule(self, obj: Dict[str, Any]) -> ComparisonRule:
        if not isinstance(obj, dict):
            raise ValueError('rule must be an object')

        rid = str(obj.get('id') or '').strip() or new_id('rule')
        name = str(obj.get('name') or rid).strip()[:64]
        enabled = bool(obj.get('enabled', True))
        severity = str(obj.get('severity') or 'warning').strip().lower()
        if severity not in {'info', 'warning', 'critical'}:
            severity = 'warning'

        a0 = obj.get('a') if isinstance(obj.get('a'), dict) else {}
        a = SignalRef(
            source_id=str(a0.get('source_id') or '').strip(),
            message=str(a0.get('message') or '').strip(),
            signal=str(a0.get('signal') or '').strip(),
            unit=a0.get('unit'),
        )
        if not a.source_id or not a.message or not a.signal:
            raise ValueError('Signal A is incomplete')

        op = str(obj.get('op') or 'delta_abs').strip().lower()
        if op not in {'lt', 'gt', 'eq', 'le', 'ge', 'ne', 'delta_abs', 'delta_pct', 'missing'}:
            raise ValueError('invalid operator')

        b_kind = str(obj.get('b_kind') or 'const').strip().lower()
        if b_kind not in {'signal', 'const'}:
            raise ValueError('invalid compare target')

        b = None
        b_const = None
        if b_kind == 'signal':
            b0 = obj.get('b') if isinstance(obj.get('b'), dict) else {}
            b = SignalRef(
                source_id=str(b0.get('source_id') or '').strip(),
                message=str(b0.get('message') or '').strip(),
                signal=str(b0.get('signal') or '').strip(),
                unit=b0.get('unit'),
            )
            if not b.source_id or not b.message or not b.signal:
                raise ValueError('Signal B is incomplete')
        else:
            b_const = _safe_float(obj.get('b_const'))
            if b_const is None:
                raise ValueError('Constant value required')

        threshold = float(_safe_float(obj.get('threshold')) or 0.0)
        debounce_s = _float_or_default(obj.get('debounce_s'), 2.0)
        missing_timeout_s = _float_or_default(obj.get('missing_timeout_s'), 0.5)

        actions: List[RuleAction] = []
        raw_actions = obj.get('actions') if isinstance(obj.get('actions'), list) else []
        for act in raw_actions:
            if not isinstance(act, dict):
                continue
            kind = str(act.get('kind') or '').strip()
            if kind not in {'log_csv', 'emit_ws', 'beep'}:
                continue
            params = act.get('params') if isinstance(act.get('params'), dict) else {}
            actions.append(RuleAction(kind=kind, params=dict(params)))
        if not actions:
            actions = [RuleAction(kind='log_csv'), RuleAction(kind='emit_ws')]

        now = now_ms()
        with self._lock:
            prev = self._rules.get(rid)
            created = prev.created_at_ms if prev else now
            rule = ComparisonRule(
                id=rid,
                name=name,
                enabled=enabled,
                severity=severity,  # type: ignore[arg-type]
                a=a,
                op=op,  # type: ignore[arg-type]
                b_kind=b_kind,  # type: ignore[arg-type]
                b=b,
                b_const=b_const,
                threshold=threshold,
                debounce_s=debounce_s,
                missing_timeout_s=missing_timeout_s,
                conditions=obj.get('conditions') if isinstance(obj.get('conditions'), list) else [],
                conditions_mode=str(obj.get('conditions_mode') or 'and') if str(obj.get('conditions_mode') or 'and') in {'and', 'or'} else 'and',
                actions=actions,
                created_at_ms=created,
                updated_at_ms=now,
            )
            self._rules[rid] = rule

        self._save()
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        rid = str(rule_id)
        with self._lock:
            existed = rid in self._rules
            if existed:
                self._rules.pop(rid, None)
        if existed:
            self._save()
        return existed

    def _conditions_match(self, r: ComparisonRule, ts_ms: int) -> bool:
        conds = r.conditions or []
        if not conds:
            return True
        mode = r.conditions_mode if r.conditions_mode in {'and', 'or'} else 'and'
        results: List[bool] = []
        for c in conds:
            results.append(self._eval_condition(r, c, ts_ms))
        return all(results) if mode == 'and' else any(results)

    def _eval_condition(self, r: ComparisonRule, cond: Any, ts_ms: int) -> bool:
        if not isinstance(cond, dict):
            return False

        # Condition may either be flat {source_id,message,signal,...} or nested under 'a'.
        a0 = cond.get('a') if isinstance(cond.get('a'), dict) else cond
        source_id = str(a0.get('source_id') or r.a.source_id or '').strip()
        message = str(a0.get('message') or '').strip()
        signal = str(a0.get('signal') or '').strip()
        if not source_id or not message or not signal:
            return False

        op = str(cond.get('op') or 'eq').strip().lower()
        if op not in {'lt', 'gt', 'eq', 'le', 'ge', 'ne', 'delta_abs', 'delta_pct', 'missing'}:
            return False

        threshold = float(_safe_float(cond.get('threshold')) or 0.0)

        max_age_s = _safe_float(cond.get('max_age_s'))
        max_age_ms = int(float(max_age_s) * 1000.0) if (max_age_s is not None and float(max_age_s) > 0.0) else None

        missing_timeout_s = _float_or_default(
            cond.get('missing_timeout_s'),
            _float_or_default(cond.get('timeout_s'), _float_or_default(r.missing_timeout_s, 0.5)),
        )

        a_key = f"{source_id}:{message}:{signal}"
        with self._lock:
            a_val_ts = self._last_value.get(a_key)

        last_ts = a_val_ts[1] if a_val_ts else None
        age_ms = (ts_ms - int(last_ts)) if last_ts is not None else 10**12

        if op == 'missing':
            return age_ms >= int(missing_timeout_s * 1000.0)

        if not a_val_ts:
            return False
        if max_age_ms is not None and age_ms > max_age_ms:
            return False

        a_val = float(a_val_ts[0])

        b_kind = str(cond.get('b_kind') or 'const').strip().lower()
        if b_kind not in {'const', 'signal'}:
            b_kind = 'const'

        if b_kind == 'const':
            b_const = _safe_float(cond.get('b_const'))
            if b_const is None:
                # Also accept 'value' as alias
                b_const = _safe_float(cond.get('value'))
            if b_const is None:
                return False
            ok, _ = self._eval(op, a_val, float(b_const), threshold)
            return bool(ok)

        b0 = cond.get('b') if isinstance(cond.get('b'), dict) else {}
        b_source_id = str(b0.get('source_id') or source_id).strip()
        b_message = str(b0.get('message') or '').strip()
        b_signal = str(b0.get('signal') or '').strip()
        if not b_source_id or not b_message or not b_signal:
            return False

        b_key = f"{b_source_id}:{b_message}:{b_signal}"
        with self._lock:
            b_val_ts = self._last_value.get(b_key)
        if not b_val_ts:
            return False

        b_last_ts = int(b_val_ts[1] or 0)
        b_age_ms = ts_ms - b_last_ts
        b_max_age_s = _safe_float(cond.get('b_max_age_s'))
        b_max_age_ms = (
            int(float(b_max_age_s) * 1000.0)
            if (b_max_age_s is not None and float(b_max_age_s) > 0.0)
            else max_age_ms
        )
        if b_max_age_ms is not None and b_age_ms > b_max_age_ms:
            return False

        ok, _ = self._eval(op, a_val, float(b_val_ts[0]), threshold)
        return bool(ok)

    def on_frame(self, frame: Dict[str, Any]) -> None:
        """Evaluate rules for a decoded frame."""
        try:
            decoded = frame.get('decoded') if isinstance(frame, dict) else None
            if not isinstance(decoded, dict):
                return
            msg_name = str(decoded.get('name') or '').strip()
            sigs = decoded.get('signals')
            if not msg_name or not isinstance(sigs, dict):
                return

            # Resolve source_id for bus/channel.
            source_id = None
            try:
                # Preferred: producer already attached a concrete source_id
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

            ts_ms = int(frame.get('timestamp') or int(time.time() * 1000))

            # Update cache for all decoded signals
            for k, v in sigs.items():
                fv = _safe_float(v)
                if fv is None:
                    continue
                cache_key = f"{source_id}:{msg_name}:{k}"
                with self._lock:
                    self._last_value[cache_key] = (float(fv), ts_ms)

            # Snapshot current rules list (avoid holding lock during evaluation)
            with self._lock:
                rules = list(self._rules.values())

            for r in rules:
                if not r.enabled:
                    continue
                if r.a.source_id != source_id:
                    continue
                if r.a.message != msg_name:
                    continue

                # Optional multi-signal gating.
                if not self._conditions_match(r, ts_ms):
                    continue

                a_key = f"{r.a.source_id}:{r.a.message}:{r.a.signal}"
                with self._lock:
                    a_val_ts = self._last_value.get(a_key)

                if r.op == 'missing':
                    last_ts = a_val_ts[1] if a_val_ts else None
                    age_ms = (ts_ms - int(last_ts)) if last_ts is not None else 10**12
                    if age_ms < int(float(r.missing_timeout_s) * 1000.0):
                        continue
                    diff = float(age_ms) / 1000.0
                    self._emit_violation(
                        r,
                        ts_ms,
                        a={'message': r.a.message, 'signal': r.a.signal, 'value': None},
                        b={'kind': 'missing', 'timeout_s': r.missing_timeout_s},
                        diff=diff,
                        threshold=r.missing_timeout_s,
                        description=f"Missing {r.a.message}.{r.a.signal} for {diff:.3f}s (timeout {r.missing_timeout_s:.3f}s)",
                    )
                    continue

                if not a_val_ts:
                    continue
                a_val = float(a_val_ts[0])

                b_val = None
                b_desc: Dict[str, Any] = {}
                if r.b_kind == 'const':
                    b_val = float(r.b_const or 0.0)
                    b_desc = {'kind': 'const', 'value': b_val}
                else:
                    if not r.b:
                        continue
                    b_key = f"{r.b.source_id}:{r.b.message}:{r.b.signal}"
                    with self._lock:
                        b_val_ts = self._last_value.get(b_key)
                    if not b_val_ts:
                        continue
                    b_val = float(b_val_ts[0])
                    b_desc = {'kind': 'signal', 'message': r.b.message, 'signal': r.b.signal, 'value': b_val}

                if b_val is None:
                    continue

                ok, diff = self._eval(r.op, a_val, b_val, r.threshold)
                if not ok:
                    continue

                # Debounce per rule
                now_s = time.time()
                last_s = self._last_violation_ts.get(r.id) or 0.0
                if (now_s - last_s) < float(r.debounce_s or 0.0):
                    continue
                self._last_violation_ts[r.id] = now_s

                self._emit_violation(
                    r,
                    ts_ms,
                    a={'message': r.a.message, 'signal': r.a.signal, 'value': a_val},
                    b=b_desc,
                    diff=diff,
                    threshold=r.threshold,
                    description=self._describe(r, a_val, b_val, diff),
                )

        except Exception:
            return

    def _eval(self, op: str, a: float, b: float, threshold: float) -> Tuple[bool, Optional[float]]:
        try:
            if op == 'lt':
                return (a < b - threshold), (b - a)
            if op == 'gt':
                return (a > b + threshold), (a - b)
            if op == 'le':
                return (a <= b - threshold), (b - a)
            if op == 'ge':
                return (a >= b + threshold), (a - b)
            if op == 'eq':
                return (abs(a - b) <= threshold), abs(a - b)
            if op == 'ne':
                return (abs(a - b) > threshold), abs(a - b)
            if op == 'delta_abs':
                return (abs(a - b) > threshold), abs(a - b)
            if op == 'delta_pct':
                denom = abs(b) if abs(b) > 1e-9 else 1.0
                pct = abs(a - b) / denom * 100.0
                return (pct > threshold), pct
        except Exception:
            return (False, None)
        return (False, None)

    def _describe(self, r: ComparisonRule, a: float, b: float, diff: Optional[float]) -> str:
        base = f"{r.name}: {r.a.message}.{r.a.signal}={a:.3f}"
        if r.b_kind == 'const':
            base += f" vs const {b:.3f}"
        else:
            if r.b:
                base += f" vs {r.b.message}.{r.b.signal}={b:.3f}"
        if r.op in {'delta_abs', 'delta_pct', 'ne'}:
            if diff is not None:
                unit = '%' if r.op == 'delta_pct' else ''
                base += f" diff={diff:.3f}{unit} (thr {r.threshold:.3f}{unit})"
        return base

    def _emit_violation(
        self,
        r: ComparisonRule,
        ts_ms: int,
        *,
        a: Dict[str, Any],
        b: Dict[str, Any],
        diff: Optional[float],
        threshold: Optional[float],
        description: str,
    ) -> None:
        v = make_violation(
            ts_ms=ts_ms,
            rule_id=r.id,
            rule_name=r.name,
            severity=r.severity,
            description=description,
            a=a,
            b=b,
            diff=diff,
            threshold=threshold,
        )
        # Always persist
        try:
            self._vlog.log(v)
        except Exception:
            pass
        # Emit WS if configured
        try:
            if self._socketio is not None:
                self._socketio.emit('violation', v.to_dict())
        except Exception:
            pass
