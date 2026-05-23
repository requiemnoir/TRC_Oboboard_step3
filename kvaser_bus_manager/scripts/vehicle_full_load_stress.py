#!/usr/bin/env python3
"""Vehicle-like full-load CAN stress test.

Goal
- Simulate real-vehicle traffic using the configured per-channel DBCs.
- Ramp up a global time-compression scale until each port reaches ~100% load.
- Keep full load for a hold period, while logging and monitoring CPU temperature.

How it works
- Reads logger_channels from /api/config (dbc_name + bitrate per channel).
- For each channel DBC:
  - Loads messages (<=8 bytes, prefer those with signals).
  - Parses GenMsgCycleTime/CycleTime from the original DBC text (Vector style BA_ lines).
  - Schedules messages at their nominal cycle time; applies a scale factor so
    effective period = cycle_time / scale.
- Injects frames via /api/can/inject_batch.
- Uses /api/bus/stats (bus_load_by_channel) to decide when each port is saturated.

Notes
- This stresses the software pipeline (decode/log/emit) and the host CPU.
- It does not guarantee physical CAN bus arbitration-level saturation when using
  injection endpoints (which bypass the physical CAN controller).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
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


def _http_json(method: str, url: str, payload: dict | list | None = None, timeout_s: float = 3.0) -> Any:
    method = method.upper()
    try:
        if method == 'GET':
            r = requests.get(url, timeout=timeout_s)
        else:
            r = requests.request(method, url, json=payload, timeout=timeout_s)
        r.raise_for_status()
        return r.json() if r.content else None
    except Exception:
        return None


def _read_temp_c() -> float | None:
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            raw = Path(p).read_text().strip()
            if raw.isdigit():
                return int(raw) / 1000.0
        except Exception:
            pass
    return None


def _list_logs(api_base: str) -> List[Dict[str, Any]]:
    j = _http_json('GET', f"{api_base}/api/logs", timeout_s=3.0)
    return j if isinstance(j, list) else []


def _start_logging(api_base: str, formats: List[str]) -> bool:
    try:
        j = _http_json('POST', f"{api_base}/api/log/start", {'formats': formats}, timeout_s=4.0)
        return isinstance(j, dict) and (j.get('status') == 'logging_started')
    except Exception:
        return False


def _stop_logging(api_base: str) -> bool:
    try:
        j = _http_json('POST', f"{api_base}/api/log/stop", {}, timeout_s=6.0)
        return isinstance(j, dict) and (j.get('status') == 'logging_stopped')
    except Exception:
        return False


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_logger_channels(api_base: str) -> List[Dict[str, Any]]:
    j = _http_json("GET", f"{api_base}/api/config", timeout_s=3.0) or {}
    cfg = j.get("config") if isinstance(j, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    chans = cfg.get("logger_channels")
    return chans if isinstance(chans, list) else []


def _parse_cycle_times_ms(dbc_text: str) -> Dict[int, int]:
    """Parse Vector-style message cycle time attributes.

    Common formats:
      BA_ "GenMsgCycleTime" BO_ 123 10;
      BA_ "CycleTime" BO_ 123 10;

    Returns mapping raw_id -> cycle_ms.
    """
    mapping: Dict[int, int] = {}
    # Consider both common attribute names.
    pat = re.compile(r"^\s*BA_\s+\"(?:GenMsgCycleTime|CycleTime)\"\s+BO_\s+(\d+)\s+(\d+)\s*;\s*$")
    for line in dbc_text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        try:
            mid = int(m.group(1))
            ms = int(m.group(2))
        except Exception:
            continue
        if ms <= 0:
            continue
        mapping[mid] = ms
    return mapping


def _sanitize_dbc_text_for_cantools(text: str) -> str:
    """Keep cantools compatibility for Vector DBCs, but don't destroy original text."""
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


def _load_cantools_db(dbc_path: Path) -> Any:
    if cantools is None:
        raise RuntimeError(f"cantools import failed: {_cantools_import_error}")

    try:
        return cantools.database.load_file(str(dbc_path), strict=False)
    except Exception:
        # fallback: sanitize content
        text = dbc_path.read_text(encoding='utf-8', errors='replace')
        sanitized = _sanitize_dbc_text_for_cantools(text)
        # load via temp file to keep cantools interface
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


def _pick_signal_value(sig, state: Dict[str, Any], state_key: str) -> Any:
    # Prefer enum/choices
    try:
        choices = getattr(sig, 'choices', None)
        if isinstance(choices, dict) and choices:
            if state_key in state and random.random() < 0.85:
                return state[state_key]
            return random.choice(list(choices.keys()))
    except Exception:
        pass

    mn = getattr(sig, 'minimum', None)
    mx = getattr(sig, 'maximum', None)
    is_float = bool(getattr(sig, 'is_float', False))

    if mn is not None and mx is not None:
        try:
            mn_f = float(mn)
            mx_f = float(mx)
            if mx_f < mn_f:
                mn_f, mx_f = mx_f, mn_f
            if mn_f == mx_f:
                state[state_key] = mn_f if is_float else int(mn_f)
                return state[state_key]

            cur = state.get(state_key)
            cur = (mn_f + mx_f) / 2.0 if cur is None else float(cur)
            span = max(1e-9, (mx_f - mn_f))
            step = span * 0.02
            cur = cur + (random.random() * 2.0 - 1.0) * step
            cur = max(mn_f, min(mx_f, cur))
            out = float(cur) if is_float else int(round(cur))
            state[state_key] = out
            return out
        except Exception:
            state[state_key] = 0
            return 0

    if state_key not in state:
        state[state_key] = 0
    return state[state_key]


@dataclass(frozen=True)
class MsgPlan:
    channel: int
    msg: Any
    period_s: float


@dataclass(frozen=True)
class ChannelPlan:
    channel: int
    dbc_name: str
    messages: List[Any]
    nominal_fps: float


def _build_channel_plan(
    ch: int,
    dbc_name: str,
    dbc_path: Path,
    max_messages: int,
    default_cycle_ms: int,
    prefer_dlc8: bool,
) -> ChannelPlan:
    text = dbc_path.read_text(encoding='utf-8', errors='replace')
    cycle_raw = _parse_cycle_times_ms(text)

    db = _load_cantools_db(dbc_path)
    messages = [m for m in (getattr(db, 'messages', []) or []) if int(getattr(m, 'length', 8) or 8) <= 8]

    msgs_with_signals = [m for m in messages if getattr(m, 'signals', None)]
    if msgs_with_signals:
        messages = msgs_with_signals

    # Prefer DLC=8 when available (common on real vehicles and improves achievable load).
    if prefer_dlc8:
        try:
            dlc8 = [m for m in messages if int(getattr(m, 'length', 8) or 8) == 8]
            if dlc8 and len(dlc8) >= 20:
                messages = dlc8
        except Exception:
            pass

    # Stable ordering: larger payload first, then by ID.
    try:
        messages.sort(key=lambda m: (-(int(getattr(m, 'length', 8) or 8)), int(getattr(m, 'frame_id', 0) or 0)))
    except Exception:
        pass

    if max_messages > 0:
        messages = messages[:max_messages]

    plans: List[MsgPlan] = []
    for m in messages:
        try:
            fid = int(getattr(m, 'frame_id', 0))
        except Exception:
            continue

        # Try: BA_ parsed cycle time by raw id or extended-flagged id
        raw_id = fid & 0x1FFFFFFF
        ms = cycle_raw.get(raw_id) or cycle_raw.get(fid)  # accept either

        # Try: cantools message.cycle_time (if present)
        if not ms:
            try:
                ct = getattr(m, 'cycle_time', None)
                if ct:
                    ms = int(ct)
            except Exception:
                ms = None

        if not ms:
            ms = int(default_cycle_ms)

        period_s = max(0.001, float(ms) / 1000.0)
        plans.append(MsgPlan(channel=ch, msg=m, period_s=period_s))

    # Baseline "vehicle" fps: sum(1/period) for all included messages.
    nominal_fps = 0.0
    for p in plans:
        try:
            nominal_fps += 1.0 / max(float(p.period_s), 0.001)
        except Exception:
            continue

    # Keep just the message objects for fast round-robin generation.
    msg_list = [p.msg for p in plans]
    return ChannelPlan(channel=ch, dbc_name=dbc_name, messages=msg_list, nominal_fps=float(nominal_fps))


def _encode_message(msg, state: Dict[str, Any], prefix: str) -> Tuple[int, List[int]]:
    values: Dict[str, Any] = {}
    for sig in getattr(msg, 'signals', []) or []:
        try:
            name = str(sig.name)
            key = f"{prefix}:{getattr(msg, 'frame_id', 0)}:{name}"
            values[name] = _pick_signal_value(sig, state, key)
        except Exception:
            continue

    payload = msg.encode(values, scaling=True, strict=False)
    data = list(payload)

    try:
        length = int(getattr(msg, 'length', len(data)))
        if length > 0:
            data = (data + [0] * length)[:length]
    except Exception:
        pass

    return int(getattr(msg, 'frame_id', 0)), data


def main() -> int:
    ap = argparse.ArgumentParser(description="Vehicle-like full-load CAN stress test (all ports)")
    ap.add_argument('--api', default='http://127.0.0.1:5000', help='API base URL')
    ap.add_argument('--duration-s', type=float, default=60.0, help='Max test duration')
    ap.add_argument('--hold-s', type=float, default=15.0, help='How long to hold full load once reached')
    ap.add_argument('--soak-s', type=float, default=None, help='If set with --target-load, holds for this many seconds after reaching target (e.g. 300 for 5 min)')
    ap.add_argument('--target-load', type=float, default=None, help='Optional target bus load per channel (%). If omitted, ramps until each port plateaus (max sustainable load).')
    ap.add_argument('--ramp-interval-s', type=float, default=3.0, help='Seconds between rate adjustments')
    ap.add_argument('--ramp-gain', type=float, default=1.35, help='Rate multiplier while below target')
    ap.add_argument('--max-multiplier', type=float, default=50.0, help='Maximum multiplier applied to nominal vehicle fps')
    ap.add_argument('--plateau-eps', type=float, default=1.0, help='Plateau detection epsilon (percentage points)')
    ap.add_argument('--plateau-windows', type=int, default=4, help='How many ramp samples to consider for plateau')
    ap.add_argument('--workers-per-channel', type=int, default=2, help='Parallel inject workers per channel')
    ap.add_argument('--batch-size', type=int, default=250, help='Frames per inject_batch request')
    ap.add_argument('--max-messages-per-dbc', type=int, default=200, help='Max messages per DBC')
    ap.add_argument('--default-cycle-ms', type=int, default=100, help='Cycle time for messages missing attributes')
    ap.add_argument('--prefer-dlc8', dest='prefer_dlc8', action='store_true', default=True, help='Prefer DLC=8 messages when available (default: on)')
    ap.add_argument('--no-prefer-dlc8', dest='prefer_dlc8', action='store_false', help='Do not prefer DLC=8 messages')
    ap.add_argument('--seed', type=int, default=0, help='Random seed (0 disables)')
    ap.add_argument('--log', dest='log', action='store_true', default=True, help='Enable backend logging for injected frames (default: on)')
    ap.add_argument('--no-log', dest='log', action='store_false', help='Disable backend logging for injected frames (higher throughput)')
    ap.add_argument('--listeners', dest='listeners', action='store_true', default=True, help='Notify backend listeners for injected frames (default: on)')
    ap.add_argument('--no-listeners', dest='listeners', action='store_false', help='Disable backend listeners for injected frames (higher throughput)')
    ap.add_argument('--stop-temp', type=float, default=80.0, help='Abort if CPU temp reaches this (°C)')
    ap.add_argument('--start-bus', action='store_true', help='Call /api/start using saved logger_channels before running')
    ap.add_argument('--record-mf4', action='store_true', help='Start MF4 logging via API, run test, stop logging, and verify an MF4 file was created')
    args = ap.parse_args()

    if args.seed:
        random.seed(args.seed)

    api_base = args.api.rstrip('/')

    # If doing a soak run (e.g., 5 min recording), make sure duration allows ramp + soak.
    if args.soak_s is not None and args.target_load is not None:
        try:
            soak_s = float(args.soak_s)
        except Exception:
            soak_s = None
        if soak_s is not None and soak_s > 0:
            # Reuse hold logic: hold starts when target reached.
            args.hold_s = soak_s
            # Ensure the overall max duration can cover ramp + soak.
            args.duration_s = max(float(args.duration_s), float(soak_s) + 180.0)

    # Ensure API reachable
    try:
        _http_json('GET', f"{api_base}/api/runtime/status", timeout_s=2.0)
    except Exception as e:
        print(f"ERROR: API not reachable: {e}", file=sys.stderr)
        return 2

    logger_channels = _load_logger_channels(api_base)
    selected = []
    for ch in logger_channels:
        if not isinstance(ch, dict):
            continue
        try:
            cid = int(ch.get('id'))
        except Exception:
            continue
        dbc_name = str(ch.get('dbc_name') or '').strip()
        try:
            bitrate = int(ch.get('bitrate') or -2)
        except Exception:
            bitrate = -2
        if not dbc_name:
            continue
        selected.append({'id': cid, 'type': 'CAN', 'bitrate': bitrate, 'dbc_name': dbc_name})

    if not selected:
        print('ERROR: no logger_channels with dbc_name configured (set DBCs per channel in UI).', file=sys.stderr)
        return 3

    # Optional: start MF4 logging and snapshot existing logs for later verification.
    pre_logs: Dict[str, int] = {}
    if args.record_mf4:
        try:
            for it in _list_logs(api_base):
                if isinstance(it, dict) and isinstance(it.get('name'), str):
                    pre_logs[it['name']] = int(it.get('size', 0) or 0)
        except Exception:
            pre_logs = {}

        ok = _start_logging(api_base, ['mf4'])
        if not ok:
            print('ERROR: failed to start MF4 logging via /api/log/start', file=sys.stderr)
            return 8

    if args.start_bus:
        try:
            _http_json('POST', f"{api_base}/api/start", {'channels': selected}, timeout_s=2.0)
        except Exception:
            pass

    # Wait until bus is running (autostart is async)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 90.0:
        st = _http_json('GET', f"{api_base}/api/runtime/status", timeout_s=2.0) or {}
        bus = st.get('bus') if isinstance(st, dict) else None
        if isinstance(bus, dict) and bus.get('running'):
            break
        time.sleep(1.0)

    print('Configured channels:', [c['id'] for c in selected])

    root = _project_root()
    dbc_dir = root / 'databases' / 'dbc'

    # Build per-channel message lists + nominal fps derived from cycle times
    channel_plans: List[ChannelPlan] = []
    for ch in selected:
        dbc_name = str(ch['dbc_name'])
        dbc_path = dbc_dir / os.path.basename(dbc_name)
        if not dbc_path.exists():
            print(f"WARN: missing DBC file for channel {ch['id']}: {dbc_path}")
            continue
        try:
            cp = _build_channel_plan(
                int(ch['id']),
                os.path.basename(dbc_name),
                dbc_path,
                int(args.max_messages_per_dbc),
                int(args.default_cycle_ms),
                bool(args.prefer_dlc8),
            )
            if cp.messages:
                channel_plans.append(cp)
                print(f"ch{cp.channel}: dbc={cp.dbc_name} messages={len(cp.messages)} nominal_fps~{cp.nominal_fps:.1f}")
        except Exception as e:
            print(f"WARN: failed loading DBC for channel {ch['id']}: {e}")
            continue

    if not channel_plans:
        print('ERROR: no channel plans built (DBC load failed or empty).', file=sys.stderr)
        return 4

    # Per-channel rate multiplier over nominal vehicle fps
    multiplier = 1.0

    # Shared mutable targets for workers
    rate_lock = threading.Lock()
    target_fps_by_ch: Dict[int, float] = {cp.channel: max(50.0, cp.nominal_fps) for cp in channel_plans}

    # Per-worker encoding state
    state_by_worker: Dict[Tuple[int, int], Dict[str, Any]] = {}
    stop_evt = threading.Event()

    results: Dict[str, Any] = {
        'target_load': None if args.target_load is None else float(args.target_load),
        'multiplier_final': None,
        'rate_history': [],
        'bus_load_by_channel_max': {},
        'inject': {'sent': 0, 'errors': 0},
        'inject_by_channel': {},
        'temp_max_c': None,
        'reached_full_load': False,
        'time_to_full_load_s': None,
    }

    # Temp monitor
    def temp_monitor():
        mx = None
        while not stop_evt.is_set():
            t = _read_temp_c()
            if t is not None:
                mx = t if mx is None else max(mx, t)
                results['temp_max_c'] = mx
                if t >= float(args.stop_temp):
                    print(f"STOP: CPU temp {t:.1f}°C >= {float(args.stop_temp):.1f}°C")
                    stop_evt.set()
                    break
            time.sleep(1.0)

    threading.Thread(target=temp_monitor, daemon=True).start()

    session = requests.Session()

    def inject_batch(frames: List[dict]) -> Tuple[int, int]:
        if not frames:
            return 0, 0
        try:
            # Prefer fast endpoint (no Socket.IO emit, no decode) for maximum throughput.
            payload = {
                'frames': frames,
                'options': {
                    'decode': False,
                    'emit': False,
                    'log': bool(args.log),
                    'diag': True,
                    'listeners': bool(args.listeners),
                },
            }

            r = session.post(f"{api_base}/api/can/inject_batch_fast", json=payload, timeout=6.0)
            if r.status_code == 404:
                r = session.post(f"{api_base}/api/can/inject_batch", json={'frames': frames}, timeout=6.0)
            r.raise_for_status()
            j = r.json() if r.content else {}
            inj = int(j.get('injected', 0) or 0)
            errc = int(j.get('error_count', 0) or 0)
            return inj, errc
        except Exception:
            return 0, len(frames)

    def worker_loop(ch_plan: ChannelPlan, worker_idx: int) -> None:
        key = (int(ch_plan.channel), int(worker_idx))
        st = state_by_worker.setdefault(key, {})
        msgs = ch_plan.messages
        if not msgs:
            return

        msg_i = 0
        batch_size = int(max(20, args.batch_size))

        while not stop_evt.is_set():
            with rate_lock:
                ch_fps = float(target_fps_by_ch.get(int(ch_plan.channel), 100.0))
            workers = max(int(args.workers_per_channel), 1)
            fps = max(10.0, ch_fps / float(workers))
            period_s = float(batch_size) / fps

            t0 = time.monotonic()
            frames: List[dict] = []
            for _ in range(batch_size):
                msg = msgs[msg_i % len(msgs)]
                msg_i += 1
                try:
                    arb_id, data = _encode_message(msg, st, prefix=f"ch{ch_plan.channel}:w{worker_idx}")
                except Exception:
                    # Fallback: still stress the pipeline with correct ID + random payload.
                    try:
                        arb_id = int(getattr(msg, 'frame_id', 0))
                    except Exception:
                        arb_id = random.randint(0x100, 0x7FF)
                    try:
                        dlc = int(getattr(msg, 'length', 8) or 8)
                    except Exception:
                        dlc = 8
                    data = [random.randint(0, 255) for _ in range(max(0, min(8, dlc)))]
                frames.append({'channel_id': int(ch_plan.channel), 'id': int(arb_id), 'data': data})

            inj, errc = inject_batch(frames)
            results['inject']['sent'] += inj
            results['inject']['errors'] += errc
            try:
                results['inject_by_channel'][str(int(ch_plan.channel))] = int(results['inject_by_channel'].get(str(int(ch_plan.channel)), 0)) + int(inj)
            except Exception:
                pass

            dt = time.monotonic() - t0
            sleep_s = period_s - dt
            if sleep_s > 0:
                time.sleep(min(0.05, sleep_s))

    t_start = time.monotonic()
    t_next_ramp = t_start + float(args.ramp_interval_s)
    t_full_load = None

    # Plateau tracking: per-channel recent max loads
    recent_max: Dict[int, List[float]] = {}

    # Start workers
    workers: List[threading.Thread] = []
    for cp in channel_plans:
        for wi in range(max(int(args.workers_per_channel), 1)):
            t = threading.Thread(target=worker_loop, args=(cp, wi), daemon=True)
            t.start()
            workers.append(t)

    while not stop_evt.is_set():
        now = time.monotonic()
        if now - t_start >= float(args.duration_s):
            break

        if now >= t_next_ramp:
            t_next_ramp = now + float(args.ramp_interval_s)

            stats = _http_json('GET', f"{api_base}/api/bus/stats", timeout_s=2.0) or {}
            by_ch = stats.get('bus_load_by_channel') if isinstance(stats, dict) else None
            by_ch = by_ch if isinstance(by_ch, dict) else {}

            wanted = {int(c['id']) for c in selected}
            loads = {int(k): float(v) for k, v in by_ch.items() if int(k) in wanted} if by_ch else {}

            for k, v in loads.items():
                prev = results['bus_load_by_channel_max'].get(str(k))
                results['bus_load_by_channel_max'][str(k)] = v if prev is None else max(float(prev), v)

            # Update plateau windows
            try:
                window = max(2, int(args.plateau_windows))
            except Exception:
                window = 4
            for ch, lv in loads.items():
                arr = recent_max.get(int(ch))
                if arr is None:
                    arr = []
                arr.append(float(lv))
                if len(arr) > window:
                    arr = arr[-window:]
                recent_max[int(ch)] = arr

            target = None if args.target_load is None else float(args.target_load)
            below = [ch for ch, lv in loads.items() if (target is not None and lv < target)]

            with rate_lock:
                snap_rates = {str(ch): round(float(v), 1) for ch, v in sorted(target_fps_by_ch.items())}

            results['rate_history'].append({
                't': round(now - t_start, 2),
                'multiplier': round(multiplier, 3),
                'loads': {str(ch): round(lv, 2) for ch, lv in sorted(loads.items())},
                'target_fps': snap_rates,
            })

            # Determine completion condition:
            # - If target provided: all wanted channels >= target.
            # - Else: each wanted channel has plateaued within eps over the window.
            all_present = wanted.issubset(set(loads.keys())) if loads else False
            if target is not None:
                complete = bool(loads) and all_present and (len(below) == 0)
            else:
                try:
                    eps = float(args.plateau_eps)
                except Exception:
                    eps = 1.0
                complete = bool(loads) and all_present
                if complete:
                    for ch in sorted(wanted):
                        arr = recent_max.get(int(ch)) or []
                        if len(arr) < window:
                            complete = False
                            break
                        if (max(arr) - min(arr)) > eps:
                            complete = False
                            break

            if complete:
                if t_full_load is None:
                    t_full_load = now
                    results['reached_full_load'] = True
                    results['time_to_full_load_s'] = round(t_full_load - t_start, 3)
                    if target is None:
                        print(f"Reached max sustainable load (plateau) on all ports at t={results['time_to_full_load_s']}s (mult={multiplier:.2f})")
                    else:
                        print(f"Reached target load on all ports at t={results['time_to_full_load_s']}s (mult={multiplier:.2f})")
                elif (now - t_full_load) >= float(args.hold_s):
                    break
            else:
                multiplier = min(float(args.max_multiplier), multiplier * float(args.ramp_gain))
                with rate_lock:
                    for cp in channel_plans:
                        base = max(50.0, float(cp.nominal_fps))
                        target_fps_by_ch[int(cp.channel)] = base * float(multiplier)

        time.sleep(0.2)

    stop_evt.set()

    for t in workers:
        t.join(timeout=1.0)

    # Stop MF4 logging and verify an MF4 file was produced.
    if args.record_mf4:
        _stop_logging(api_base)

        produced = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            logs = _list_logs(api_base)
            # Prefer a CAN session MF4 (avoid tiny Ethernet-only *.eth.mf4 when both are produced).
            candidates = []
            for it in logs:
                if not isinstance(it, dict):
                    continue
                name = it.get('name')
                if not isinstance(name, str) or not name.lower().endswith('.mf4'):
                    continue
                if name in pre_logs:
                    continue
                try:
                    size = int(it.get('size', 0) or 0)
                except Exception:
                    size = 0
                if size <= 0:
                    continue
                candidates.append({'name': name, 'size': size})

            if candidates:
                # Rank: non-eth first, then larger size.
                def _rank(x: dict) -> tuple:
                    n = str(x.get('name') or '').lower()
                    is_eth = ('.eth.' in n) or n.endswith('.eth.mf4')
                    return (1 if is_eth else 0, -int(x.get('size') or 0))

                candidates.sort(key=_rank)
                produced = candidates[0]
                break
            time.sleep(1.0)

        if produced:
            results['mf4'] = produced
            print(f"MF4 created: {produced['name']} ({produced['size']} bytes)")
        else:
            results['mf4'] = None
            print('ERROR: MF4 recording requested but no new .mf4 file appeared in /api/logs', file=sys.stderr)
            print(json.dumps(results, indent=2, sort_keys=True))
            return 9

    results['multiplier_final'] = round(multiplier, 3)
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
