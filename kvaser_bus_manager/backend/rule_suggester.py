from __future__ import annotations

import csv
import json
import os
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

from monitor_types import now_ms, new_id


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return float(int(x))
        return float(x)
    except Exception:
        return None


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if p <= 0:
        return float(xs[0])
    if p >= 100:
        return float(xs[-1])
    k = (len(xs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return float(xs[f])
    d = k - f
    return float(xs[f] * (1.0 - d) + xs[c] * d)


def suggest_rules_from_session_csv(
    csv_path: str,
    *,
    channel_to_source_id: Callable[[int], Optional[str]],
    min_count: int = 200,
    max_samples_per_signal: int = 5000,
    margin_fraction: float = 0.05,
    severity: str = 'warning',
) -> Dict[str, Any]:
    """Build min/max comparison rule suggestions from a session CSV.

    Requires the session CSV to have Decoded column containing JSON payloads
    like {"name": "Msg", "signals": {"Sig": 1.23, ...}}.
    """

    csv_path = os.path.abspath(str(csv_path))
    if not os.path.isfile(csv_path):
        raise FileNotFoundError('csv not found')

    if severity not in {'info', 'warning', 'critical'}:
        severity = 'warning'

    # Reservoir samples per key.
    # key -> {'n': int, 'samples': [float], 'min': float, 'max': float}
    stats: Dict[str, Dict[str, Any]] = {}

    decoded_rows = 0
    total_rows = 0

    with open(csv_path, 'r', encoding='utf-8', errors='ignore', newline='') as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            return {'ok': False, 'error': 'empty csv'}

        # Locate columns
        col = {name.strip(): idx for idx, name in enumerate(header)}
        # Support legacy header with exact labels
        ch_i = col.get('Channel')
        dec_i = col.get('Decoded')

        if ch_i is None or dec_i is None:
            return {'ok': False, 'error': 'CSV missing required columns Channel/Decoded'}

        for row in r:
            total_rows += 1
            try:
                if not row or len(row) <= max(ch_i, dec_i):
                    continue
                dec_cell = str(row[dec_i] or '').strip()
                if not dec_cell:
                    continue

                # Channel -> source_id mapping
                try:
                    ch = int(str(row[ch_i]).strip())
                except Exception:
                    ch = 0
                source_id = channel_to_source_id(int(ch) or 0) or f"CAN{int(ch) or 0}"

                try:
                    dec = json.loads(dec_cell)
                except Exception:
                    continue
                if not isinstance(dec, dict):
                    continue
                msg = str(dec.get('name') or '').strip()
                sigs = dec.get('signals')
                if not msg or not isinstance(sigs, dict):
                    continue

                decoded_rows += 1
                for sig, val in sigs.items():
                    fv = _safe_float(val)
                    if fv is None:
                        continue
                    k = f"{source_id}:{msg}:{str(sig)}"
                    st = stats.get(k)
                    if st is None:
                        st = {'n': 0, 'samples': [], 'min': float(fv), 'max': float(fv)}
                        stats[k] = st
                    st['n'] = int(st.get('n') or 0) + 1
                    if float(fv) < float(st.get('min')):
                        st['min'] = float(fv)
                    if float(fv) > float(st.get('max')):
                        st['max'] = float(fv)

                    samples = st['samples']
                    if len(samples) < int(max_samples_per_signal):
                        samples.append(float(fv))
                    else:
                        # Reservoir sampling
                        n = int(st['n'])
                        j = random.randint(1, n)
                        if j <= int(max_samples_per_signal):
                            samples[j - 1] = float(fv)
            except Exception:
                continue

    if decoded_rows == 0:
        return {
            'ok': False,
            'error': 'no decoded payloads found in CSV; enable LOG_DECODED_MODE=full (CSV) or use MF4 decoded channels',
            'total_rows': total_rows,
        }

    suggestions: List[Dict[str, Any]] = []
    rejected = 0

    for key, st in stats.items():
        try:
            n = int(st.get('n') or 0)
            if n < int(min_count):
                rejected += 1
                continue
            samples = list(st.get('samples') or [])
            if len(samples) < 20:
                rejected += 1
                continue

            p01 = _percentile(samples, 1.0)
            p99 = _percentile(samples, 99.0)
            vmin = float(st.get('min'))
            vmax = float(st.get('max'))

            # Pick an operating range based on robust percentiles.
            lo = float(p01)
            hi = float(p99)
            span = float(hi - lo)
            if span <= 1e-9:
                # Constant-ish signal; skip unless it occasionally spikes.
                if abs(float(vmax - vmin)) <= 1e-9:
                    rejected += 1
                    continue
                span = float(max(abs(vmax - vmin), 1.0))

            margin = float(max(span * float(margin_fraction), 1e-6))

            # Parse key
            source_id, msg, sig = key.split(':', 2)

            base_name = f"AI range: {msg}.{sig}"

            # Too low
            suggestions.append(
                {
                    'id': new_id('rule'),
                    'name': f"{base_name} < {lo:.3g}",
                    'enabled': True,
                    'severity': severity,
                    'a': {'source_id': source_id, 'message': msg, 'signal': sig, 'unit': None},
                    'op': 'lt',
                    'b_kind': 'const',
                    'b': None,
                    'b_const': float(lo),
                    'threshold': float(margin),
                    'debounce_s': 1.0,
                    'missing_timeout_s': 0.5,
                    'conditions': [],
                    'conditions_mode': 'and',
                    'actions': [{'kind': 'log_csv', 'params': {}}, {'kind': 'emit_ws', 'params': {}}],
                    'created_at_ms': now_ms(),
                    'updated_at_ms': now_ms(),
                    '_ai_reason': {
                        'kind': 'range_low',
                        'count': n,
                        'p01': lo,
                        'p99': hi,
                        'min': vmin,
                        'max': vmax,
                        'margin': margin,
                    },
                }
            )

            # Too high
            suggestions.append(
                {
                    'id': new_id('rule'),
                    'name': f"{base_name} > {hi:.3g}",
                    'enabled': True,
                    'severity': severity,
                    'a': {'source_id': source_id, 'message': msg, 'signal': sig, 'unit': None},
                    'op': 'gt',
                    'b_kind': 'const',
                    'b': None,
                    'b_const': float(hi),
                    'threshold': float(margin),
                    'debounce_s': 1.0,
                    'missing_timeout_s': 0.5,
                    'conditions': [],
                    'conditions_mode': 'and',
                    'actions': [{'kind': 'log_csv', 'params': {}}, {'kind': 'emit_ws', 'params': {}}],
                    'created_at_ms': now_ms(),
                    'updated_at_ms': now_ms(),
                    '_ai_reason': {
                        'kind': 'range_high',
                        'count': n,
                        'p01': lo,
                        'p99': hi,
                        'min': vmin,
                        'max': vmax,
                        'margin': margin,
                    },
                }
            )
        except Exception:
            continue

    return {
        'ok': True,
        'csv_path': csv_path,
        'total_rows': total_rows,
        'decoded_rows': decoded_rows,
        'signals_total': len(stats),
        'signals_rejected': rejected,
        'suggestions': suggestions,
    }
