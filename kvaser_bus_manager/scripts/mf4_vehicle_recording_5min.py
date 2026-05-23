#!/usr/bin/env python3
"""MF4 recording validation: 5 minutes of vehicle-like CAN traffic.

What this test does
- Uses the saved per-channel DBC assignments from /api/config.
- Starts the bus (async /api/start) like the installed system.
- Starts MF4 logging via /api/log/start.
- Generates DBC-based CAN frames and injects them via /api/can/inject_batch_fast.
- Adapts rate until every CAN channel reaches at least a target load (default 25%).
- Holds for the remainder of the 5 minutes.
- Stops logging and verifies that an MF4 artifact exists in /api/logs.

Notes
- This validates the recording pipeline (MF4 write path) as installed.
- It does not guarantee *physical* bus saturation (injection bypasses HW TX).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

try:
    import cantools
except Exception as e:  # pragma: no cover
    cantools = None
    _cantools_import_error = e


def _sanitize_dbc_text_for_cantools(text: str) -> str:
    # Drop BA_* lines (cantools sometimes chokes on Vector attributes)
    kept = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith('BA_') or s.startswith('BA_DEF_') or s.startswith('BA_DEF_DEF_'):
            continue
        kept.append(line)
    text = "\n".join(kept) + "\n"

    # Mark extended IDs by setting bit31 for BO_ and common references
    mapping: Dict[int, int] = {}
    import re

    for m in re.finditer(r'^\s*BO_\s+(\d+)\s+\S+\s*:\s*(\d+)\s+\S+\s*$', text, flags=re.M):
        try:
            mid = int(m.group(1))
        except Exception:
            continue
        if mid & 0x80000000:
            continue
        if mid > 0x7FF:
            mapping[mid] = (mid | 0x80000000)

    if not mapping:
        return text

    def _sub(pat: str, s: str) -> str:
        def repl(mm):
            prefix, id_s, suffix = mm.group(1), mm.group(2), mm.group(3)
            try:
                val = int(id_s)
            except Exception:
                return mm.group(0)
            new_val = mapping.get(val)
            if new_val is None:
                return mm.group(0)
            return f"{prefix}{new_val}{suffix}"

        return re.sub(pat, repl, s, flags=re.M)

    text = _sub(r'^(\s*BO_\s+)(\d+)(\s+)', text)
    text = _sub(r'^(\s*CM_\s+BO_\s+)(\d+)(\s+)', text)
    text = _sub(r'^(\s*CM_\s+SG_\s+)(\d+)(\s+)', text)
    text = _sub(r'^(\s*VAL_\s+)(\d+)(\s+)', text)
    text = _sub(r'^(\s*BO_TX_BU_\s+)(\d+)(\s*:\s*)', text)
    text = _sub(r'^(\s*SIG_GROUP_\s+)(\d+)(\s+)', text)
    text = _sub(r'^(\s*SIG_VALTYPE_\s+)(\d+)(\s+)', text)
    return text


def _http_json(method: str, url: str, payload: dict | list | None = None, timeout_s: float = 5.0) -> Any:
    method = method.upper()
    r = requests.request(method, url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json() if r.content else None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_logger_channels(api_base: str) -> List[Dict[str, Any]]:
    j = _http_json('GET', f'{api_base}/api/config', timeout_s=5.0) or {}
    cfg = j.get('config') if isinstance(j, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    chans = cfg.get('logger_channels')
    return chans if isinstance(chans, list) else []


def _resolve_dbc_path(dbc_name: str) -> Path:
    root = _project_root()
    # Runtime backend resolves dbc_name to UPLOAD_FOLDER_DBC; for the generator we load from local databases folder.
    p = root / 'databases' / 'dbc' / os.path.basename(dbc_name)
    return p


def _load_cantools_db(dbc_path: Path) -> Any:
    if cantools is None:
        raise RuntimeError(f'cantools import failed: {_cantools_import_error}')
    try:
        return cantools.database.load_file(str(dbc_path), strict=False)
    except Exception:
        text = dbc_path.read_text(encoding='utf-8', errors='replace')
        sanitized = _sanitize_dbc_text_for_cantools(text)
        import tempfile

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile('w', suffix='.sanitized.dbc', delete=False, encoding='utf-8') as tf:
                tf.write(sanitized)
                tmp_path = tf.name
            return cantools.database.load_file(tmp_path, strict=False)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


def _pick_values_for_message(msg: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sigs = getattr(msg, 'signals', None) or []
    for s in sigs:
        name = getattr(s, 'name', None)
        if not name:
            continue
        choices = getattr(s, 'choices', None)
        if isinstance(choices, dict) and choices:
            out[name] = random.choice(list(choices.keys()))
            continue
        mn = getattr(s, 'minimum', None)
        mx = getattr(s, 'maximum', None)
        if mn is not None and mx is not None:
            try:
                mn = float(mn)
                mx = float(mx)
                if mx < mn:
                    mn, mx = mx, mn
                out[name] = int(round(mn + (mx - mn) * random.random()))
                continue
            except Exception:
                pass
        out[name] = 0
    return out


@dataclass(frozen=True)
class ChannelPlan:
    channel: int
    dbc_path: Path
    messages: List[Any]


def _build_plans(chans: List[Dict[str, Any]], max_messages: int, prefer_dlc8: bool) -> List[ChannelPlan]:
    plans: List[ChannelPlan] = []
    for c in chans:
        if not isinstance(c, dict):
            continue
        try:
            ch_id = int(c['id'])
        except Exception:
            continue
        dbc_name = str(c.get('dbc_name') or '').strip()
        if not dbc_name:
            continue
        dbc_path = _resolve_dbc_path(dbc_name)
        if not dbc_path.exists():
            raise FileNotFoundError(f'DBC not found for ch{ch_id}: {dbc_path}')
        db = _load_cantools_db(dbc_path)
        msgs = [m for m in (getattr(db, 'messages', []) or []) if int(getattr(m, 'length', 8) or 8) <= 8]
        msgs_with_signals = [m for m in msgs if getattr(m, 'signals', None)]
        if msgs_with_signals:
            msgs = msgs_with_signals
        if prefer_dlc8:
            dlc8 = [m for m in msgs if int(getattr(m, 'length', 8) or 8) == 8]
            if dlc8:
                msgs = dlc8
        try:
            msgs.sort(key=lambda m: (-(int(getattr(m, 'length', 8) or 8)), int(getattr(m, 'frame_id', 0) or 0)))
        except Exception:
            pass
        if max_messages > 0:
            msgs = msgs[:max_messages]
        if not msgs:
            raise RuntimeError(f'No usable messages in {dbc_path} for ch{ch_id}')
        plans.append(ChannelPlan(channel=ch_id, dbc_path=dbc_path, messages=msgs))
    if not plans:
        raise RuntimeError('No configured CAN channels with DBCs found in /api/config logger_channels')
    return plans


def _get_bus_load_by_channel(api_base: str) -> Dict[int, float]:
    stats = _http_json('GET', f'{api_base}/api/bus/stats', timeout_s=3.0) or {}
    by_ch = stats.get('bus_load_by_channel') if isinstance(stats, dict) else None
    by_ch = by_ch if isinstance(by_ch, dict) else {}
    out: Dict[int, float] = {}
    for k, v in by_ch.items():
        try:
            out[int(k)] = float(v)
        except Exception:
            continue
    return out


def _wait_bus_started(api_base: str, timeout_s: float = 45.0) -> None:
    t0 = time.monotonic()
    while True:
        st = _http_json('GET', f'{api_base}/api/runtime/status', timeout_s=3.0) or {}
        bus = st.get('bus') if isinstance(st, dict) else {}
        running = bool((bus or {}).get('running'))
        starting = bool((bus or {}).get('starting'))
        if running and not starting:
            return
        if time.monotonic() - t0 > timeout_s:
            raise TimeoutError('Bus did not reach running state in time')
        time.sleep(1.0)


def _estimate_load_percent(fps: float, dlc: int, bitrate_bps: int = 500000) -> float:
    # Match backend Diagnostics approximation: bits = (bytes*8) + (frames*50)
    bits_per_frame = (max(0, min(8, int(dlc))) * 8) + 50
    if bitrate_bps <= 0:
        bitrate_bps = 500000
    return max(0.0, min(100.0, (float(fps) * float(bits_per_frame)) / float(bitrate_bps) * 100.0))


def _list_logs(api_base: str) -> List[dict]:
    j = _http_json('GET', f'{api_base}/api/logs', timeout_s=5.0) or {}
    logs = j.get('files') if isinstance(j, dict) else None
    return logs if isinstance(logs, list) else []


def _start_logging_mf4(api_base: str) -> None:
    _http_json('POST', f'{api_base}/api/log/start', payload={'formats': ['mf4']}, timeout_s=10.0)


def _stop_logging(api_base: str) -> None:
    _http_json('POST', f'{api_base}/api/log/stop', payload={}, timeout_s=10.0)


def _start_bus_from_config(api_base: str) -> None:
    chans = _load_logger_channels(api_base)
    payload = {'channels': []}
    for c in chans:
        if not isinstance(c, dict):
            continue
        # default type CAN
        payload['channels'].append({
            'id': int(c.get('id', 0)),
            'type': 'CAN',
            'bitrate': int(c.get('bitrate', -2)),
            'dbc_name': str(c.get('dbc_name') or '').strip(),
        })
    _http_json('POST', f'{api_base}/api/start', payload=payload, timeout_s=10.0)


def _inject_batch_fast(
    api_base: str,
    frames: List[dict],
    *,
    log: bool,
    listeners: bool,
) -> Tuple[int, int]:
    payload = {
        'frames': frames,
        'options': {
            'decode': False,
            'emit': False,
            'diag': True,
            'log': bool(log),
            'listeners': bool(listeners),
        },
    }
    r = requests.post(f'{api_base}/api/can/inject_batch_fast', json=payload, timeout=10.0)
    if r.status_code != 200:
        return 0, len(frames)
    j = r.json() if r.content else {}
    injected = int(j.get('injected', 0) or 0)
    errc = int(j.get('error_count', 0) or 0)
    return injected, errc


def main() -> int:
    ap = argparse.ArgumentParser(description='5-min MF4 recording test with vehicle-like CAN traffic (>=25% per CAN)')
    ap.add_argument('--api', default='http://127.0.0.1:5000', help='Backend base URL')
    ap.add_argument('--minutes', type=float, default=5.0, help='Total duration (minutes)')
    ap.add_argument('--target-load', type=float, default=25.0, help='Min load % per CAN channel')
    ap.add_argument('--max-messages-per-dbc', type=int, default=200)
    ap.add_argument('--batch-size', type=int, default=600)
    ap.add_argument('--prefer-dlc8', action='store_true', default=True)
    ap.add_argument('--no-prefer-dlc8', dest='prefer_dlc8', action='store_false')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--no-listeners', dest='listeners', action='store_false', default=False, help='Disable backend listeners for injected frames (recommended)')
    ap.add_argument('--log', dest='log', action='store_true', default=True, help='Keep MF4 logging on injected frames (must be on)')
    ap.add_argument('--start-bus', action='store_true', default=True)
    ap.add_argument('--no-start-bus', dest='start_bus', action='store_false')
    ap.add_argument('--require-bus', action='store_true', help='Fail if bus does not reach running state (default: continue without it)')
    args = ap.parse_args()

    if args.seed:
        random.seed(int(args.seed))

    api_base = str(args.api).rstrip('/')

    chans = _load_logger_channels(api_base)
    can_chans = [c for c in chans if isinstance(c, dict) and 'id' in c]
    wanted = sorted({int(c['id']) for c in can_chans})
    if not wanted:
        print('No CAN channels configured in /api/config logger_channels', file=sys.stderr)
        return 2

    plans = _build_plans(can_chans, int(args.max_messages_per_dbc), bool(args.prefer_dlc8))
    print('Configured CAN channels:', wanted)

    # Capture logs before
    before = _list_logs(api_base)
    before_names = {str(x.get('name')) for x in before if isinstance(x, dict) and x.get('name')}

    if args.start_bus:
        _start_bus_from_config(api_base)
        try:
            _wait_bus_started(api_base, timeout_s=60.0)
        except TimeoutError as e:
            if args.require_bus:
                raise
            print(f'WARN: {e}. Continuing without bus running (injection+MF4 still valid).', file=sys.stderr)

    _start_logging_mf4(api_base)

    t0 = time.monotonic()
    t_end = t0 + float(args.minutes) * 60.0

    # Start low and adapt: frames/sec per channel.
    target_fps_by_ch: Dict[int, float] = {p.channel: 700.0 for p in plans}
    last_adjust = 0.0

    sent = 0
    errors = 0

    try:
        while time.monotonic() < t_end:
            now = time.monotonic()
            # Adjust every 3s
            if (now - last_adjust) >= 3.0:
                last_adjust = now
                loads = _get_bus_load_by_channel(api_base)
                missing = [ch for ch in wanted if ch not in loads]
                if missing:
                    # If bus isn't running, stats may not exist. Use open-loop estimate.
                    est = {ch: round(_estimate_load_percent(float(target_fps_by_ch.get(ch, 0.0)), 8), 2) for ch in wanted}
                    below = [ch for ch in wanted if est.get(ch, 0.0) < float(args.target_load)]
                    print('EstLoads:', est, 'below:', below)
                    for ch in wanted:
                        cur = float(target_fps_by_ch.get(ch, 700.0))
                        lv = float(est.get(ch, 0.0))
                        if lv < float(args.target_load):
                            target_fps_by_ch[ch] = min(6000.0, cur * 1.12)
                        else:
                            target_fps_by_ch[ch] = max(500.0, cur * 0.997)
                else:
                    below = [ch for ch in wanted if loads.get(ch, 0.0) < float(args.target_load)]
                    print('Loads:', {ch: round(loads.get(ch, 0.0), 2) for ch in wanted}, 'below:', below)
                    for ch in wanted:
                        cur = float(target_fps_by_ch.get(ch, 700.0))
                        lv = float(loads.get(ch, 0.0))
                        if lv < float(args.target_load):
                            target_fps_by_ch[ch] = min(6000.0, cur * 1.15)
                        else:
                            # keep stable, small decay to avoid runaway
                            target_fps_by_ch[ch] = max(500.0, cur * 0.995)

            # Produce one batch per channel per loop.
            for p in plans:
                fps = float(target_fps_by_ch.get(p.channel, 700.0))
                if fps <= 0:
                    continue
                # Loop period ~0.25s
                period_s = 0.25
                n = int(max(1, min(int(args.batch_size), fps * period_s)))
                frames: List[dict] = []
                msgs = p.messages
                for i in range(n):
                    msg = msgs[(sent + i) % len(msgs)]
                    arb_id = int(getattr(msg, 'frame_id', 0) or 0)
                    dlc = int(getattr(msg, 'length', 8) or 8)
                    data: List[int]
                    try:
                        vals = _pick_values_for_message(msg)
                        raw = msg.encode(vals)
                        data = list(raw)
                    except Exception:
                        data = [random.randint(0, 255) for _ in range(max(0, min(8, dlc)))]
                    frames.append({'channel_id': int(p.channel), 'id': int(arb_id), 'data': data})

                inj, errc = _inject_batch_fast(api_base, frames, log=bool(args.log), listeners=bool(args.listeners))
                sent += int(inj)
                errors += int(errc)

            time.sleep(0.02)
    finally:
        _stop_logging(api_base)

    after = _list_logs(api_base)
    after_names = {str(x.get('name')) for x in after if isinstance(x, dict) and x.get('name')}
    new_names = sorted(after_names - before_names)

    print(json.dumps({
        'ok': True,
        'duration_min': float(args.minutes),
        'target_load': float(args.target_load),
        'sent': int(sent),
        'errors': int(errors),
        'new_logs': new_names,
    }, indent=2))

    mf4_new = [n for n in new_names if n.lower().endswith('.mf4')]
    if not mf4_new:
        print('ERROR: No new MF4 file detected in /api/logs', file=sys.stderr)
        return 3

    print('MF4 created:', mf4_new[-1])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
