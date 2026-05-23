#!/usr/bin/env python3
"""Offline smoke/stress test for Kvaser Bus Manager.

Runs without a vehicle:
- Starts logging (CSV + MF4 by default)
- Generates intense synthetic CAN traffic via /api/can/inject on all active channels
- Generates Ethernet traffic via UDP broadcast flood (best effort)
- Monitors Raspberry Pi CPU temperature and aborts safely if it exceeds a threshold
- Stops logging and validates expected artifacts

Usage:
  ./.venv/bin/python scripts/offline_smoke_test.py --duration 30 --can-rate 200 --eth-rate 1000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import socketio


def _http_json(method: str, url: str, payload: dict | None = None, timeout_s: float = 3.0) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def _read_temp_c() -> float | None:
    # Raspberry Pi typical path
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            raw = Path(p).read_text().strip()
            if raw.isdigit():
                return int(raw) / 1000.0
        except Exception:
            pass
    return None


def _ffprobe_summary(path: Path, timeout_s: float = 5.0) -> dict | None:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [
                ffprobe,
                '-v',
                'error',
                '-print_format',
                'json',
                '-show_format',
                '-show_streams',
                str(path),
            ],
            timeout=timeout_s,
        )
        j = json.loads(out.decode('utf-8', errors='replace'))
        fmt = j.get('format') or {}
        streams = j.get('streams') or []
        duration_s = None
        try:
            duration_s = float(fmt.get('duration'))
        except Exception:
            duration_s = None
        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        return {
            'duration_s': duration_s,
            'codec': (video_stream or {}).get('codec_name'),
            'width': (video_stream or {}).get('width'),
            'height': (video_stream or {}).get('height'),
        }
    except Exception:
        return None


def _udp_flood(stop_evt: threading.Event, rate_hz: float, payload_len: int = 512) -> dict:
    """Best-effort Ethernet traffic generator.

    Tries to send UDP broadcast packets; on systems without broadcast route it may fall back
    to a local destination (which might not be seen on eth0 capture).
    """
    sent = 0
    errors = 0

    payload = os.urandom(payload_len)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.2)

        targets = [("255.255.255.255", 5001)]
        # Also attempt limited broadcast / common private broadcasts.
        targets += [("192.168.0.255", 5001), ("192.168.1.255", 5001)]

        period = 1.0 / max(rate_hz, 1.0)
        next_t = time.monotonic()
        idx = 0
        while not stop_evt.is_set():
            now = time.monotonic()
            if now < next_t:
                time.sleep(min(0.01, next_t - now))
                continue

            try:
                s.sendto(payload, targets[idx % len(targets)])
                sent += 1
            except Exception:
                errors += 1
                # If broadcast is not permitted, try localhost as last resort.
                try:
                    s.sendto(payload, ("127.0.0.1", 5001))
                    sent += 1
                except Exception:
                    errors += 1

            idx += 1
            next_t += period
    finally:
        try:
            s.close()
        except Exception:
            pass

    return {"sent": sent, "errors": errors}


class _RateControl:
    def __init__(self, initial_rate_hz: float):
        self._lock = threading.Lock()
        self._rate_hz = float(initial_rate_hz)

    def get(self) -> float:
        with self._lock:
            return float(self._rate_hz)

    def set(self, rate_hz: float) -> None:
        with self._lock:
            self._rate_hz = float(max(rate_hz, 1.0))


def _can_inject_loop(stop_evt: threading.Event, api_base: str, channel: int, rate_ctl: _RateControl) -> dict:
    sent = 0
    errors = 0

    next_t = time.monotonic()

    session = urllib.request.build_opener()

    while not stop_evt.is_set():
        rate_hz = rate_ctl.get()
        period = 1.0 / max(rate_hz, 1.0)
        now = time.monotonic()
        if now < next_t:
            time.sleep(min(0.002, next_t - now))
            continue

        arb_id = random.randint(0x100, 0x7FF)
        payload = [random.randint(0, 255) for _ in range(8)]

        try:
            # Use opener for connection reuse.
            data = json.dumps({"channel_id": channel, "id": arb_id, "data": payload}).encode("utf-8")
            req = urllib.request.Request(
                url=f"{api_base}/api/can/inject",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with session.open(req, timeout=1.5) as resp:
                body = resp.read()
                if body:
                    j = json.loads(body.decode("utf-8"))
                    if not j or not j.get("ok", False):
                        errors += 1
                    else:
                        sent += 1
                else:
                    sent += 1
        except Exception:
            errors += 1

        next_t += period

    return {"sent": sent, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline smoke/stress test (CAN + Ethernet + temp monitor)")
    ap.add_argument("--api", default="http://127.0.0.1:5000", help="API base URL")
    ap.add_argument("--duration", type=float, default=30.0, help="Test duration in seconds (legacy; use --max-duration)")
    ap.add_argument("--max-duration", type=float, default=None, help="Max test duration in seconds")
    ap.add_argument("--can-rate", type=float, default=200.0, help="CAN inject rate per channel (frames/sec)")
    ap.add_argument("--can-workers", type=int, default=1, help="Injector workers per CAN channel")
    ap.add_argument("--target-busload", type=float, default=None, help="Target bus_load percentage to reach (requires Socket.IO)")
    ap.add_argument("--busload-timeout", type=float, default=120.0, help="Seconds to try reaching target bus load")
    ap.add_argument("--ramp-interval", type=float, default=2.0, help="Seconds between rate adjustments")
    ap.add_argument("--ramp-gain", type=float, default=1.25, help="Multiplier when bus_load is below target")
    ap.add_argument("--post-target-gain", type=float, default=1.10, help="Multiplier to keep increasing load after target")
    ap.add_argument("--fail-errors", type=int, default=50, help="Stop test when total CAN inject errors reach this")
    ap.add_argument("--eth-rate", type=float, default=800.0, help="UDP packets/sec")
    ap.add_argument("--formats", nargs="+", default=["csv", "mf4"], help="Logging formats")
    ap.add_argument("--warn-temp", type=float, default=75.0, help="Warning temperature (°C)")
    ap.add_argument("--crit-temp", type=float, default=80.0, help="Critical temperature (°C) -> abort (legacy; use --stop-temp)")
    ap.add_argument("--stop-temp", type=float, default=None, help="Stop test when CPU temp reaches this value (°C)")
    ap.add_argument("--check-video", action="store_true", help="Validate MP4 output existence and basic integrity")
    ap.add_argument("--video-min-bytes", type=int, default=200_000, help="Minimum MP4 size to consider video OK")
    ap.add_argument("--video-wait", type=float, default=15.0, help="Seconds to wait for MP4 to finalize")
    args = ap.parse_args()

    api_base = args.api.rstrip("/")

    max_duration = args.max_duration if args.max_duration is not None else args.duration
    max_duration = float(max(max_duration, 1.0))
    stop_temp = args.stop_temp if args.stop_temp is not None else args.crit_temp
    stop_temp = float(stop_temp)

    # Basic reachability
    try:
        rt = _http_json("GET", f"{api_base}/api/runtime/status", timeout_s=2.5)
    except urllib.error.URLError as e:
        print(f"ERROR: API not reachable: {e}", file=sys.stderr)
        return 2

    channels = rt.get("bus", {}).get("channels") or []
    try:
        channels = [int(c) for c in channels]
    except Exception:
        channels = []
    if not channels:
        # Fallback to common channels
        channels = [0, 1]

    print(f"API: {api_base}")
    print(f"Channels: {channels}")
    print(f"Duration: {max_duration:.1f}s")
    print(f"CAN rate: {args.can_rate:.1f} fps/channel")
    print(f"CAN workers: {args.can_workers}")
    print(f"ETH rate: {args.eth_rate:.1f} pps")
    print(f"Formats: {args.formats}")

    # Start logging
    print("Starting logging...")
    _http_json("POST", f"{api_base}/api/log/start", {"formats": args.formats}, timeout_s=5.0)

    # Capture base name for artifact validation
    time.sleep(0.3)
    st = _http_json("GET", f"{api_base}/api/log/status", timeout_s=2.5) or {}
    base_name = st.get("base_name")
    print(f"Session base_name: {base_name}")

    stop_evt = threading.Event()

    t_test_start = time.monotonic()
    results: dict[str, Any] = {
        "can": {},
        "eth": None,
        "temp_max_c": None,
        "temp_reached_c": None,
        "time_to_temp_s": None,
        "bus_load_max": None,
        "bus_load_target": args.target_busload,
        "time_to_busload_s": None,
        "busload_reached": False,
        "failure_reason": None,
        "aborted": False,
    }

    # Socket.IO bus_stats (bus_load feedback)
    sio = socketio.Client(reconnection=True, reconnection_attempts=5, reconnection_delay=1)
    bus_stats_lock = threading.Lock()
    bus_load = None
    bus_load_max = None
    bus_stats_last_t = None

    @sio.on('bus_stats')
    def _on_bus_stats(stats):
        nonlocal bus_load, bus_load_max, bus_stats_last_t
        try:
            v = float(stats.get('bus_load'))
        except Exception:
            return
        with bus_stats_lock:
            bus_load = v
            bus_load_max = v if bus_load_max is None else max(bus_load_max, v)
            bus_stats_last_t = time.monotonic()

    if args.target_busload is not None:
        try:
            sio.connect(api_base, transports=['polling', 'websocket'])
        except Exception as e:
            print(f"WARN: cannot connect Socket.IO to {api_base}: {e}")
            print("WARN: target bus_load control disabled")
            args.target_busload = None

    # Temperature monitor thread
    def temp_monitor() -> None:
        temp_max = None
        reached = False
        while not stop_evt.is_set():
            t = _read_temp_c()
            if t is not None:
                temp_max = t if temp_max is None else max(temp_max, t)
                results["temp_max_c"] = temp_max
                if (not reached) and t >= stop_temp:
                    reached = True
                    results["temp_reached_c"] = t
                    results["time_to_temp_s"] = round(time.monotonic() - t_test_start, 3)
                    print(f"STOP: CPU temp reached threshold: {t:.1f}°C >= {stop_temp:.1f}°C (t={results['time_to_temp_s']}s)")
                    results["aborted"] = True
                    stop_evt.set()
                    break
                if t >= args.warn_temp:
                    print(f"WARN: CPU temp high: {t:.1f}°C")
            time.sleep(1.0)

    tm = threading.Thread(target=temp_monitor, name="temp-monitor", daemon=True)
    tm.start()

    # Start CAN injector threads
    can_threads: list[threading.Thread] = []
    can_out: dict[tuple[int, int], dict] = {}
    # One shared control per channel (applies to all workers)
    can_rate_controls: dict[int, _RateControl] = {ch: _RateControl(args.can_rate) for ch in channels}

    def run_can(ch: int, worker_idx: int) -> None:
        can_out[(ch, worker_idx)] = _can_inject_loop(stop_evt, api_base, ch, can_rate_controls[ch])

    for ch in channels:
        for w in range(max(int(args.can_workers), 1)):
            t = threading.Thread(target=run_can, args=(ch, w), name=f"can-{ch}-{w}", daemon=True)
            t.start()
            can_threads.append(t)

    # Start Ethernet flood thread
    eth_out: dict[str, Any] = {}

    def run_eth() -> None:
        nonlocal eth_out
        eth_out = _udp_flood(stop_evt, args.eth_rate)

    et = threading.Thread(target=run_eth, name="eth-flood", daemon=True)
    et.start()

    # Adaptive bus_load ramp controller
    target_load = float(args.target_busload) if args.target_busload is not None else None
    t_target_reached = None
    t_busload_start = time.monotonic()
    t_last_adjust = 0.0

    # Run duration or until abort
    t_end = time.monotonic() + max_duration
    while not stop_evt.is_set() and time.monotonic() < t_end:
        # Stop on too many CAN errors
        total_errors = 0
        for v in list(can_out.values()):
            try:
                total_errors += int(v.get('errors', 0))
            except Exception:
                pass
        if total_errors >= int(args.fail_errors):
            results['failure_reason'] = f"can_inject_errors>={args.fail_errors}" 
            results['aborted'] = True
            stop_evt.set()
            break

        # Track bus_load max
        with bus_stats_lock:
            cur_load = bus_load
            cur_max = bus_load_max
            last_t = bus_stats_last_t
        if cur_max is not None:
            results['bus_load_max'] = cur_max

        # If controlling bus load, adjust rates periodically
        now = time.monotonic()
        if target_load is not None and (now - t_last_adjust) >= float(args.ramp_interval):
            t_last_adjust = now

            # If no bus_stats seen for a while, fail
            if last_t is None or (now - last_t) > 6.0:
                # Give initial grace period
                if (now - t_busload_start) > 10.0:
                    results['failure_reason'] = 'no_bus_stats'
                    results['aborted'] = True
                    stop_evt.set()
                    break

            if cur_load is not None:
                if (not results['busload_reached']) and cur_load >= target_load:
                    results['busload_reached'] = True
                    t_target_reached = now
                    results['time_to_busload_s'] = round(now - t_busload_start, 3)
                    print(f"Reached target bus_load {target_load:.1f}% at t={results['time_to_busload_s']}s")

                # If still below target and within timeout, ramp up aggressively
                if (not results['busload_reached']) and (now - t_busload_start) <= float(args.busload_timeout):
                    # Increase per-channel target rate by ramp_gain
                    for ch, ctl in can_rate_controls.items():
                        ctl.set(ctl.get() * float(args.ramp_gain))
                elif (not results['busload_reached']) and (now - t_busload_start) > float(args.busload_timeout):
                    results['failure_reason'] = f"busload_timeout<{target_load}%"
                    results['aborted'] = True
                    stop_evt.set()
                    break
                elif results['busload_reached']:
                    # After reaching target, keep increasing until something fails
                    for ch, ctl in can_rate_controls.items():
                        ctl.set(ctl.get() * float(args.post_target_gain))

        time.sleep(0.2)

    stop_evt.set()

    for t in can_threads:
        t.join(timeout=3.0)
    et.join(timeout=3.0)
    tm.join(timeout=2.0)

    # Aggregate CAN per channel
    agg: dict[str, dict[str, int]] = {}
    for (ch, _w), v in can_out.items():
        key = str(ch)
        if key not in agg:
            agg[key] = {'sent': 0, 'errors': 0}
        try:
            agg[key]['sent'] += int(v.get('sent', 0))
            agg[key]['errors'] += int(v.get('errors', 0))
        except Exception:
            pass
    results["can"] = {k: agg[k] for k in sorted(agg.keys(), key=lambda x: int(x))}
    results["eth"] = eth_out

    # Stop logging (even if aborted)
    print("Stopping logging...")
    try:
        _http_json("POST", f"{api_base}/api/log/stop", {}, timeout_s=10.0)
    except Exception as e:
        print(f"WARN: failed to stop logging via API: {e}")

    # Video validation (best effort)
    video_path: Path | None = None
    video_probe: dict | None = None
    video_ok = None
    if args.check_video:
        t0 = time.monotonic()
        last_size = None
        stable_count = 0
        while time.monotonic() - t0 < max(args.video_wait, 1.0):
            try:
                st2 = _http_json("GET", f"{api_base}/api/log/status", timeout_s=2.5) or {}
                v = st2.get('video') or {}
                vp = v.get('output_path')
                if vp:
                    video_path = Path(str(vp))
            except Exception:
                video_path = video_path

            if video_path and video_path.exists():
                try:
                    size = video_path.stat().st_size
                except Exception:
                    size = 0
                if last_size is not None and size == last_size and size > 0:
                    stable_count += 1
                else:
                    stable_count = 0
                last_size = size

                # Consider finalized once size stabilizes twice in a row.
                if stable_count >= 2:
                    break

            time.sleep(0.8)

        if video_path and video_path.exists():
            vsize = video_path.stat().st_size
            video_probe = _ffprobe_summary(video_path)
            # Accept either a decent size OR ffprobe showing a duration.
            has_duration = bool(video_probe and (video_probe.get('duration_s') or 0) > 0.5)
            video_ok = (vsize >= int(args.video_min_bytes)) or has_duration
        else:
            video_ok = False

    # Validate artifacts (best effort)
    repo_root = Path(__file__).resolve().parents[1]
    logs_dir = repo_root / "logs"

    expected: list[Path] = []
    if base_name:
        if "csv" in args.formats:
            expected.append(logs_dir / f"{base_name}.csv")
        if "mf4" in args.formats:
            expected.append(logs_dir / f"{base_name}.mf4")
            expected.append(logs_dir / f"{base_name}.eth.mf4")

    print("\n--- RESULTS ---")
    print(json.dumps(results, indent=2, sort_keys=True))

    if args.check_video:
        print("\n--- VIDEO ---")
        if video_path is None:
            print("video_path: (unknown)")
        else:
            exists = video_path.exists()
            size = video_path.stat().st_size if exists else 0
            print(f"video_path: {video_path}")
            print(f"exists: {exists} size={size} min_bytes={args.video_min_bytes}")
        if video_probe is not None:
            print("ffprobe:")
            print(json.dumps(video_probe, indent=2, sort_keys=True))
        print(f"video_ok: {video_ok}")

    if expected:
        print("\n--- ARTIFACTS ---")
        ok = True
        for p in expected:
            exists = p.exists()
            size = p.stat().st_size if exists else 0
            print(f"{p}: {'OK' if exists else 'MISSING'} size={size}")
            if not exists:
                ok = False
        if not ok:
            print("Artifact validation FAILED")
            return 1
        print("Artifact validation OK")

    if args.check_video and video_ok is False:
        print("Video validation FAILED")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
