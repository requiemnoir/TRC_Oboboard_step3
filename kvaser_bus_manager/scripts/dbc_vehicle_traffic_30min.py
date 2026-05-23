#!/usr/bin/env python3
"""Generate DBC-based CAN traffic on all ports for 30 minutes.

What it does
- Reads configured CAN channels + dbc_name from /api/config (logger_channels).
- For each channel, fetches message list via /api/dbc/describe?dbc_name=...
- Injects random payload frames using only message IDs present in the DBC.

Intended workflow
- You start MF4 recording manually (UI / API), like in-vehicle.
- Run this script; it waits a warmup period, injects for the requested duration,
  then exits.

Notes
- Uses /api/can/inject_batch_fast when available.
- Defaults are chosen to be "vehicle-ish" without saturating the machine.
- This is injection-based (software pipeline stress), not hardware TX arbitration.
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import requests


@dataclass(frozen=True)
class MsgDef:
    frame_id: int
    dlc: int


def _http_json(session: requests.Session, method: str, url: str, payload: dict | list | None = None, timeout_s: float = 5.0) -> Any:
    method = method.upper()
    if method == 'GET':
        r = session.get(url, timeout=timeout_s)
    else:
        r = session.request(method, url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json() if r.content else None


def _load_logger_channels(session: requests.Session, api_base: str) -> List[Dict[str, Any]]:
    j = _http_json(session, 'GET', f"{api_base}/api/config", timeout_s=3.0) or {}
    cfg = j.get('config') if isinstance(j, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    chans = cfg.get('logger_channels')
    return chans if isinstance(chans, list) else []


def _describe_dbc(session: requests.Session, api_base: str, dbc_name: str) -> List[MsgDef]:
    # Cache because multiple channels can share the same DBC.
    cache = getattr(_describe_dbc, '_cache', None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(_describe_dbc, '_cache', cache)
    cached = cache.get(dbc_name)
    if isinstance(cached, list) and cached:
        return cached

    # Note: large Vector DBCs can take time to parse server-side.
    out: dict | None = None
    last_err: Exception | None = None
    for timeout_s in (30.0, 60.0):
        try:
            r = session.get(f"{api_base}/api/dbc/describe", params={'dbc_name': dbc_name}, timeout=timeout_s)
            r.raise_for_status()
            j = r.json() or {}
            out = j if isinstance(j, dict) else None
            last_err = None
            break
        except Exception as e:
            last_err = e
            continue

    if out is None:
        raise RuntimeError(f"dbc describe failed for {dbc_name}: {last_err}")
    if not (isinstance(out, dict) and out.get('ok') is True):
        raise RuntimeError(out.get('error') if isinstance(out, dict) else 'describe failed')

    msgs = out.get('messages')
    if not isinstance(msgs, list):
        return []

    defs: List[MsgDef] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        try:
            fid = int(m.get('frame_id') or 0)
            dlc = int(m.get('length') or 8)
        except Exception:
            continue
        if dlc < 0:
            dlc = 0
        if dlc > 8:
            dlc = 8
        if fid == 0:
            continue
        defs.append(MsgDef(frame_id=fid, dlc=dlc))

    # Keep a stable, bounded list
    defs.sort(key=lambda x: (x.frame_id, x.dlc))
    cache[dbc_name] = defs
    return defs


def _endpoint_exists(session: requests.Session, api_base: str, path: str) -> bool:
    try:
        r = session.options(f"{api_base}{path}", timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


def _inject_batch_fast(
    session: requests.Session,
    api_base: str,
    frames: List[dict],
    log_enabled: bool,
    emit_enabled: bool,
    decode_enabled: bool,
) -> Tuple[int, int]:
    payload = {
        'frames': frames,
        'options': {
            'decode': bool(decode_enabled),
            'emit': bool(emit_enabled),
            'listeners': False,
            'diag': True,
            'log': bool(log_enabled),
        },
    }
    r = session.post(f"{api_base}/api/can/inject_batch_fast", json=payload, timeout=10)
    r.raise_for_status()
    j = r.json() or {}
    if isinstance(j, dict) and j.get('ok') is True:
        return int(j.get('injected') or 0), int(j.get('error_count') or 0)
    return 0, 1


def _inject_batch_compat(session: requests.Session, api_base: str, frames: List[dict]) -> Tuple[int, int]:
    payload = {'frames': frames}
    r = session.post(f"{api_base}/api/can/inject_batch", json=payload, timeout=15)
    r.raise_for_status()
    j = r.json() or {}
    if isinstance(j, dict) and j.get('ok') is True:
        return int(j.get('injected') or 0), int(j.get('error_count') or 0)
    return 0, 1


def main() -> int:
    ap = argparse.ArgumentParser(description='DBC-based CAN traffic generator (30 min default)')
    ap.add_argument('--api', default='http://127.0.0.1:5000', help='API base URL')
    ap.add_argument('--duration-min', type=float, default=30.0, help='Duration to generate traffic')
    ap.add_argument('--warmup-s', type=float, default=10.0, help='Wait before traffic (time to start MF4 recording)')
    ap.add_argument('--fps-per-channel', type=float, default=1100.0, help='Approx frames/sec per channel')
    ap.add_argument('--batch-size', type=int, default=250, help='Frames per HTTP request per channel')
    ap.add_argument('--max-messages-per-dbc', type=int, default=300, help='Limit message pool size per DBC')
    ap.add_argument('--seed', type=int, default=0, help='RNG seed (0 disables)')
    ap.add_argument('--emit', action='store_true', help='Emit frames to Live Traffic (heavier)')
    ap.add_argument('--decode', action='store_true', help='Decode frames server-side so Live Traffic shows message names (heavier)')
    ap.add_argument('--no-log', action='store_true', help='Disable backend log() calls (NOT recommended if you want MF4 recording)')
    args = ap.parse_args()

    api_base = str(args.api).rstrip('/')
    if int(args.seed) != 0:
        random.seed(int(args.seed))

    duration_s = max(1.0, float(args.duration_min) * 60.0)
    warmup_s = max(0.0, float(args.warmup_s))
    fps = max(1.0, float(args.fps_per_channel))
    batch_size = max(1, int(args.batch_size))
    emit_enabled = bool(args.emit)
    decode_enabled = bool(args.decode)
    log_enabled = not bool(args.no_log)
    max_msgs = max(1, int(args.max_messages_per_dbc))

    with requests.Session() as s:
        chans = _load_logger_channels(s, api_base)
        selected: List[Tuple[int, str]] = []
        for c in chans:
            if not isinstance(c, dict):
                continue
            try:
                ch_id = int(c.get('id'))
            except Exception:
                continue
            dbc_name = str(c.get('dbc_name') or '').strip()
            if dbc_name:
                selected.append((ch_id, dbc_name))

        if not selected:
            print('No CAN channels with dbc_name configured in /api/config logger_channels')
            return 2

        use_fast = _endpoint_exists(s, api_base, '/api/can/inject_batch_fast')
        print(f"Using injector: {'fast' if use_fast else 'compat'}")
        if use_fast:
            print(f"Fast options: emit={emit_enabled} decode={decode_enabled} log={log_enabled}")
        else:
            if emit_enabled or decode_enabled:
                print('Note: --emit/--decode only affect the fast injector; compat injector always emits+decodes via manager.inject_frame().')

        pools: Dict[int, List[MsgDef]] = {}
        for ch_id, dbc_name in selected:
            defs = _describe_dbc(s, api_base, dbc_name)
            # Prefer DLC<=8 already; cap size
            if len(defs) > max_msgs:
                defs = defs[:max_msgs]
            if not defs:
                print(f"WARN: ch{ch_id} dbc={dbc_name} has no messages")
                continue
            pools[int(ch_id)] = defs
            print(f"ch{ch_id}: dbc={dbc_name} messages={len(defs)}")

        if not pools:
            print('No usable DBC message pools')
            return 3

        if warmup_s > 0:
            print(f"Warmup {warmup_s:.1f}s: start MF4 recording now...")
            t_end = time.monotonic() + warmup_s
            while True:
                left = t_end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.25, left))

        period_s = batch_size / fps
        t0 = time.monotonic()
        t_stop = t0 + duration_s
        t_next_log = t0 + 10.0

        sent = 0
        errs = 0

        while True:
            now = time.monotonic()
            if now >= t_stop:
                break

            for ch_id, defs in pools.items():
                frames = []
                for _ in range(batch_size):
                    m = random.choice(defs)
                    data = [random.randint(0, 255) for _ in range(m.dlc)]
                    frames.append({'channel_id': int(ch_id), 'id': int(m.frame_id), 'data': data})

                if use_fast:
                    inj, err = _inject_batch_fast(
                        s,
                        api_base,
                        frames,
                        log_enabled=log_enabled,
                        emit_enabled=emit_enabled,
                        decode_enabled=decode_enabled,
                    )
                else:
                    inj, err = _inject_batch_compat(s, api_base, frames)
                sent += int(inj)
                errs += int(err)

            # pacing
            dt = time.monotonic() - now
            sleep_s = period_s - dt
            if sleep_s > 0:
                time.sleep(min(0.05, sleep_s))

            if now >= t_next_log:
                t_next_log = now + 10.0
                elapsed = now - t0
                print(f"t={elapsed:.0f}s sent={sent} errors={errs}")

        print(f"Done. duration_s={duration_s:.1f} sent={sent} errors={errs}")
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
