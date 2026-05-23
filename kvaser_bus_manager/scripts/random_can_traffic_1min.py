#!/usr/bin/env python3
"""Generate random CAN traffic on all configured channels for a fixed duration.

Use case
- You (manually) start MF4 recording from the UI/system as in-vehicle.
- Then run this script: it injects random CAN frames for ~60 seconds and stops.

Notes
- Uses HTTP injection endpoints (stresses logging/recording pipeline, not physical bus TX).
- Prefers /api/can/inject_batch_fast when available.
"""

from __future__ import annotations

import argparse
import os
import random
import time
from typing import Any, Dict, List, Tuple

import requests


def _http_json(session: requests.Session, method: str, url: str, payload: dict | list | None = None, timeout_s: float = 3.0) -> Any:
    method = method.upper()
    if method == "GET":
        r = session.get(url, timeout=timeout_s)
    else:
        r = session.request(method, url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json() if r.content else None


def _load_channels(session: requests.Session, api_base: str) -> List[int]:
    cfg = _http_json(session, "GET", f"{api_base}/api/config", timeout_s=3.0) or {}
    c = cfg.get("config") if isinstance(cfg, dict) else None
    c = c if isinstance(c, dict) else {}
    chans = c.get("logger_channels")
    if not isinstance(chans, list):
        return []

    out: List[int] = []
    for item in chans:
        if not isinstance(item, dict):
            continue
        try:
            out.append(int(item.get("id")))
        except Exception:
            continue
    return sorted(set(out))


def _endpoint_exists(session: requests.Session, api_base: str, path: str) -> bool:
    # Use OPTIONS to avoid side-effects.
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
) -> Tuple[int, int]:
    payload = {
        "frames": frames,
        "options": {
            "decode": False,
            "emit": bool(emit_enabled),
            "listeners": False,
            "diag": True,
            "log": bool(log_enabled),
        },
    }
    j = _http_json(session, "POST", f"{api_base}/api/can/inject_batch_fast", payload=payload, timeout_s=5.0) or {}
    if isinstance(j, dict) and j.get("ok") is True:
        return int(j.get("injected") or 0), int(j.get("error_count") or 0)
    return 0, 1


def _inject_batch_compat(session: requests.Session, api_base: str, frames: List[dict]) -> Tuple[int, int]:
    payload = {"frames": frames}
    j = _http_json(session, "POST", f"{api_base}/api/can/inject_batch", payload=payload, timeout_s=8.0) or {}
    if isinstance(j, dict) and j.get("ok") is True:
        return int(j.get("injected") or 0), int(j.get("error_count") or 0)
    return 0, 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate random CAN traffic for 60s on all ports")
    ap.add_argument("--api", default="http://127.0.0.1:5000", help="API base URL")
    ap.add_argument("--duration-s", type=float, default=60.0, help="Traffic duration")
    ap.add_argument("--warmup-s", type=float, default=5.0, help="Wait before starting traffic (time to start MF4 recording)")
    ap.add_argument("--fps", type=float, default=900.0, help="Approx frames/sec per channel")
    ap.add_argument("--batch-size", type=int, default=300, help="Frames per HTTP request per channel")
    ap.add_argument("--dlc", type=int, default=8, help="Payload length (0..8)")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (0 disables)")
    ap.add_argument("--emit", action="store_true", help="Emit frames to Live Traffic (Socket.IO bus_data). Increases load.")
    ap.add_argument("--no-log", action="store_true", help="Disable backend log() calls (useful for pure load testing; not for MF4 recording)")
    args = ap.parse_args()

    api_base = str(args.api).rstrip("/")
    if int(args.seed) != 0:
        random.seed(int(args.seed))

    dlc = max(0, min(8, int(args.dlc)))
    batch_size = max(1, int(args.batch_size))
    fps = max(1.0, float(args.fps))
    warmup_s = max(0.0, float(args.warmup_s))
    duration_s = max(0.1, float(args.duration_s))
    log_enabled = not bool(args.no_log)
    emit_enabled = bool(args.emit)

    with requests.Session() as s:
        channels = _load_channels(s, api_base)
        if not channels:
            print("No channels found in /api/config (config.logger_channels)")
            return 2

        use_fast = _endpoint_exists(s, api_base, "/api/can/inject_batch_fast")
        injector = _inject_batch_fast if use_fast else _inject_batch_compat

        print(f"Channels: {channels} | injector={'fast' if use_fast else 'compat'} | fps/ch~{fps} | batch={batch_size} | dlc={dlc} | log={'on' if log_enabled else 'off'} | emit={'on' if emit_enabled else 'off'}")
        if warmup_s > 0:
            print(f"Warmup {warmup_s:.1f}s: start MF4 recording now...")
            t_end = time.monotonic() + warmup_s
            while True:
                left = t_end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.25, left))

        t0 = time.monotonic()
        t_stop = t0 + duration_s

        # Per-channel pacing
        period_s = batch_size / fps
        sent_total = 0
        err_total = 0

        while True:
            now = time.monotonic()
            if now >= t_stop:
                break

            for ch in channels:
                frames = []
                for _ in range(batch_size):
                    arb_id = random.randint(0x000, 0x7FF)
                    data = [random.randint(0, 255) for _ in range(dlc)]
                    frames.append({"channel_id": int(ch), "id": int(arb_id), "data": data})

                inj, err = (
                    injector(s, api_base, frames, log_enabled, emit_enabled)
                    if use_fast
                    else injector(s, api_base, frames)
                )
                sent_total += int(inj)
                err_total += int(err)

            # Sleep a bit to approximate fps target
            dt = time.monotonic() - now
            sleep_s = period_s - dt
            if sleep_s > 0:
                time.sleep(min(0.05, sleep_s))

        print(f"Done. sent={sent_total} errors={err_total} elapsed_s={time.monotonic() - t0:.2f}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
