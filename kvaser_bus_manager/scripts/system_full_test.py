#!/usr/bin/env python3
"""End-to-end cross-test + stress harness for Kvaser Bus Manager.

What it tests (no hardware required)
- Starts the bus (using saved logger_channels + simulate_ecu)
- Starts logging (CSV/MF4 by default)
- Generates sustained CAN load via /api/can/inject_batch_fast
- Generates Ethernet load via UDP broadcast flood (best effort)
- Runs ScanTools actions (OBD scan + clear DTCs) to validate ECU simulation
- Monitors:
  - API health (/api/runtime/status)
  - bus_stats (load/errors)
  - CPU temperature (/sys/class/thermal/thermal_zone0/temp when available)
  - Host memory pressure (via /proc/meminfo)

Exit codes
- 0: success
- 2: API not reachable
- 3: could not start bus/logging
- 4: aborted due to temperature or repeated API failures
- 5: missing expected artifacts

Usage
  ./.venv/bin/python scripts/system_full_test.py --api http://127.0.0.1:5000 --duration-s 60
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

try:
    import cantools
except Exception:  # pragma: no cover
    cantools = None


def _read_temp_c() -> float | None:
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            raw = Path(p).read_text().strip()
            if raw.isdigit():
                return int(raw) / 1000.0
        except Exception:
            pass
    return None


def _read_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            v = v.strip().split()[0]
            try:
                out[k.strip()] = int(v)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _http_json(session: requests.Session, method: str, url: str, payload: dict | list | None = None, timeout_s: float = 3.0) -> Any:
    method = method.upper()
    if method == "GET":
        r = session.get(url, timeout=timeout_s)
    else:
        r = session.request(method, url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json() if r.content else None


def _list_logs(session: requests.Session, api_base: str) -> List[Dict[str, Any]]:
    j = _http_json(session, "GET", f"{api_base}/api/logs", timeout_s=5.0)
    return j if isinstance(j, list) else []


def _start_bus_from_config(session: requests.Session, api_base: str) -> bool:
    cfg = _http_json(session, "GET", f"{api_base}/api/config", timeout_s=5.0) or {}
    c = cfg.get("config") if isinstance(cfg, dict) else None
    c = c if isinstance(c, dict) else {}
    chans = c.get("logger_channels")
    chans = chans if isinstance(chans, list) else []

    selected = []
    for ch in chans:
        if not isinstance(ch, dict):
            continue
        try:
            cid = int(ch.get("id"))
        except Exception:
            continue
        dbc_name = str(ch.get("dbc_name") or "").strip()
        if not dbc_name:
            continue
        try:
            bitrate = int(ch.get("bitrate") or -2)
        except Exception:
            bitrate = -2
        selected.append({"id": cid, "type": "CAN", "bitrate": bitrate, "dbc_name": dbc_name})

    if not selected:
        # Still try to start the bus with default channels, so ScanTools can run.
        selected = [{"id": 0, "type": "CAN", "bitrate": -2, "dbc_name": ""}]

    # Start bus (async). Force ECU simulation for ScanTools.
    _http_json(session, "POST", f"{api_base}/api/start", {"channels": selected, "simulate_ecu": True}, timeout_s=3.0)

    # Wait for running.
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30.0:
        st = _http_json(session, "GET", f"{api_base}/api/runtime/status", timeout_s=2.0) or {}
        bus = st.get("bus") if isinstance(st, dict) else None
        if isinstance(bus, dict) and bus.get("running"):
            return True
        time.sleep(0.5)
    return False


def _start_logging(session: requests.Session, api_base: str, formats: List[str]) -> bool:
    j = _http_json(session, "POST", f"{api_base}/api/log/start", {"formats": formats}, timeout_s=6.0)
    return isinstance(j, dict) and j.get("status") == "logging_started"


def _stop_logging(session: requests.Session, api_base: str) -> None:
    try:
        _http_json(session, "POST", f"{api_base}/api/log/stop", {}, timeout_s=10.0)
    except Exception:
        pass


def _log_status(session: requests.Session, api_base: str) -> Dict[str, Any]:
    try:
        j = _http_json(session, "GET", f"{api_base}/api/log/status", timeout_s=3.0)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _get_can_trigger_cfg(session: requests.Session, api_base: str) -> Dict[str, Any]:
    try:
        j = _http_json(session, "GET", f"{api_base}/api/trigger/can", timeout_s=3.0)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _set_can_trigger_cfg(session: requests.Session, api_base: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    j = _http_json(session, "POST", f"{api_base}/api/trigger/can", payload=cfg, timeout_s=5.0)
    return j if isinstance(j, dict) else {}


def _encode_trigger_frames_from_dbc(dbc_path: Path) -> Tuple[str, str, int, List[int], List[int]]:
    """Return (msg_name, sig_name, frame_id, start_data, stop_data, start_value, stop_value)."""
    if cantools is None:
        raise RuntimeError("cantools not available")

    def _sanitize_for_cantools(text: str) -> str:
        # Keep cantools compatibility for Vector DBCs.
        kept = []
        for line in text.splitlines():
            s = line.lstrip()
            if s.startswith('BA_') or s.startswith('BA_DEF_') or s.startswith('BA_DEF_DEF_'):
                continue
            kept.append(line)
        text2 = "\n".join(kept) + "\n"

        # Mark extended IDs by setting bit31 for BO_ and common references.
        import re

        mapping: Dict[int, int] = {}
        for m in re.finditer(r'^\s*BO_\s+(\d+)\s+\S+\s*:\s*(\d+)\s+\S+\s*$', text2, flags=re.M):
            try:
                mid = int(m.group(1))
            except Exception:
                continue
            if mid & 0x80000000:
                continue
            if mid > 0x7FF:
                mapping[mid] = (mid | 0x80000000)

        if not mapping:
            return text2

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

        text2 = _sub(r'^(\s*BO_\s+)(\d+)(\s+)', text2)
        text2 = _sub(r'^(\s*CM_\s+BO_\s+)(\d+)(\s+)', text2)
        text2 = _sub(r'^(\s*CM_\s+SG_\s+)(\d+)(\s+)', text2)
        text2 = _sub(r'^(\s*VAL_\s+)(\d+)(\s+)', text2)
        text2 = _sub(r'^(\s*BO_TX_BU_\s+)(\d+)(\s*:\s*)', text2)
        text2 = _sub(r'^(\s*SIG_GROUP_\s+)(\d+)(\s+)', text2)
        text2 = _sub(r'^(\s*SIG_VALTYPE_\s+)(\d+)(\s+)', text2)
        return text2

    try:
        db = cantools.database.load_file(str(dbc_path), strict=False)
    except Exception:
        import tempfile

        raw = dbc_path.read_text(encoding='utf-8', errors='replace')
        sanitized = _sanitize_for_cantools(raw)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile('w', suffix='.sanitized.dbc', delete=False, encoding='utf-8') as tf:
                tf.write(sanitized)
                tmp_path = tf.name
            db = cantools.database.load_file(tmp_path, strict=False)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    messages = list(getattr(db, "messages", []) or [])
    if not messages:
        raise RuntimeError("DBC has no messages")

    def _score(msg) -> int:
        sigs = list(getattr(msg, "signals", []) or [])
        if not sigs:
            return -1
        # Prefer messages with at least one bounded integer-like signal.
        best = -1
        try:
            fid = int(getattr(msg, 'frame_id', 0) or 0)
        except Exception:
            fid = 0
        # Prefer standard IDs when possible (easier to inject into existing backend decoders).
        if (fid & 0x1FFFFFFF) <= 0x7FF:
            best = max(best, 3)
        for s in sigs:
            try:
                if bool(getattr(s, "is_float", False)):
                    continue
                mn = getattr(s, "minimum", None)
                mx = getattr(s, "maximum", None)
                if mn is not None and mx is not None:
                    best = max(best, 4)
                else:
                    best = max(best, 3)
            except Exception:
                continue
        return best

    messages.sort(key=_score, reverse=True)

    chosen_msg = None
    chosen_sig = None
    for msg in messages:
        sigs = list(getattr(msg, "signals", []) or [])
        if not sigs:
            continue
        for s in sigs:
            try:
                if bool(getattr(s, "is_float", False)):
                    continue
                chosen_msg = msg
                chosen_sig = s
                break
            except Exception:
                continue
        if chosen_msg is not None:
            break

    if chosen_msg is None or chosen_sig is None:
        raise RuntimeError("No suitable message/signal found for trigger test")

    msg_name = str(getattr(chosen_msg, "name", ""))
    sig_name = str(getattr(chosen_sig, "name", ""))
    frame_id = int(getattr(chosen_msg, "frame_id", 0) or 0)
    dlc = int(getattr(chosen_msg, "length", 8) or 8)
    dlc = max(0, min(8, dlc))

    # Build full signal dict with safe defaults.
    base: Dict[str, Any] = {}
    for s in (getattr(chosen_msg, "signals", []) or []):
        name = str(getattr(s, "name", ""))
        if not name:
            continue
        mn = getattr(s, "minimum", None)
        mx = getattr(s, "maximum", None)
        if mn is not None:
            try:
                base[name] = int(mn) if not bool(getattr(s, "is_float", False)) else float(mn)
                continue
            except Exception:
                pass
        if mx is not None:
            try:
                base[name] = 0 if float(mx) >= 0 else int(mx)
                continue
            except Exception:
                pass
        base[name] = 0

    # Choose start/stop values that likely fit bounds.
    mn = getattr(chosen_sig, "minimum", None)
    mx = getattr(chosen_sig, "maximum", None)
    start_v: Any = 1
    stop_v: Any = 0
    try:
        if mn is not None and mx is not None:
            mn_f = float(mn)
            mx_f = float(mx)
            if 1 < mn_f:
                start_v = int(mn_f)
            if 0 < mn_f:
                stop_v = int(mn_f)
            if 0 > mx_f:
                stop_v = int(mx_f)
    except Exception:
        pass

    start_map = dict(base)
    stop_map = dict(base)
    start_map[sig_name] = start_v
    stop_map[sig_name] = stop_v

    start_bytes = list(chosen_msg.encode(start_map, scaling=True, strict=False))
    stop_bytes = list(chosen_msg.encode(stop_map, scaling=True, strict=False))
    start_bytes = (start_bytes + [0] * dlc)[:dlc]
    stop_bytes = (stop_bytes + [0] * dlc)[:dlc]

    return msg_name, sig_name, frame_id, start_bytes, stop_bytes, start_v, stop_v


def _test_can_trigger(session: requests.Session, api_base: str) -> Dict[str, Any]:
    """Configure CAN trigger and validate it starts/stops logging."""
    res: Dict[str, Any] = {"ok": False, "error": None}

    # Identify DBC for channel 0 from config.
    cfg = _http_json(session, "GET", f"{api_base}/api/config", timeout_s=4.0) or {}
    c = cfg.get("config") if isinstance(cfg, dict) else None
    c = c if isinstance(c, dict) else {}
    chans = c.get("logger_channels") if isinstance(c.get("logger_channels"), list) else []
    dbc_name = None
    channel_id = 0
    for ch in chans:
        if not isinstance(ch, dict):
            continue
        try:
            cid = int(ch.get("id"))
        except Exception:
            continue
        dn = str(ch.get("dbc_name") or "").strip()
        if cid == 0 and dn:
            dbc_name = dn
            channel_id = cid
            break
    if not dbc_name:
        res["error"] = "no dbc_name configured for channel 0"
        return res

    project_root = Path(__file__).resolve().parents[1]
    dbc_path = project_root / "databases" / "dbc" / Path(dbc_name).name
    if not dbc_path.exists():
        res["error"] = f"dbc not found on disk: {dbc_path}"
        return res

    try:
        msg_name, sig_name, frame_id, start_data, stop_data, start_v, stop_v = _encode_trigger_frames_from_dbc(dbc_path)
    except Exception as e:
        res["error"] = f"failed to build trigger frames: {e}"
        return res

    res.update({
        "channel_id": channel_id,
        "dbc": str(dbc_path.name),
        "message": msg_name,
        "signal": sig_name,
        "frame_id": frame_id,
        "start_value": start_v,
        "stop_value": stop_v,
    })

    prev = _get_can_trigger_cfg(session, api_base)
    res["prev_cfg"] = prev

    try:
        _stop_logging(session, api_base)
    except Exception:
        pass

    try:
        _set_can_trigger_cfg(
            session,
            api_base,
            {
                "armed": True,
                "channel_id": int(channel_id),
                "dbc_name": str(dbc_path.name),
                "message": msg_name,
                "signal": sig_name,
                "start_op": "eq",
                "start_value": start_v,
                "stop_op": "eq",
                "stop_value": stop_v,
                "formats": ["csv", "mf4"],
            },
        )
    except Exception as e:
        res["error"] = f"failed to set can trigger config: {e}"
        return res

    # Inject start frame (decoded path must be active).
    try:
        _http_json(
            session,
            "POST",
            f"{api_base}/api/can/inject",
            {"channel_id": int(channel_id), "id": int(frame_id), "data": list(start_data)},
            timeout_s=4.0,
        )
    except Exception as e:
        res["error"] = f"failed to inject start frame: {e}"
        return res

    # Wait until logging becomes active and is trigger-started.
    t0 = time.monotonic()
    started = False
    while time.monotonic() - t0 < 10.0:
        st = _log_status(session, api_base)
        if bool(st.get("active")):
            started = True
            break
        time.sleep(0.25)
    res["started"] = started
    if not started:
        res["error"] = "trigger did not start logging"
        # Restore previous cfg best-effort
        try:
            if prev:
                _set_can_trigger_cfg(session, api_base, prev)
        except Exception:
            pass
        return res

    # Inject stop frame.
    try:
        _http_json(
            session,
            "POST",
            f"{api_base}/api/can/inject",
            {"channel_id": int(channel_id), "id": int(frame_id), "data": list(stop_data)},
            timeout_s=4.0,
        )
    except Exception as e:
        res["error"] = f"failed to inject stop frame: {e}"
        return res

    t1 = time.monotonic()
    stopped = False
    while time.monotonic() - t1 < 15.0:
        st = _log_status(session, api_base)
        if not bool(st.get("active")):
            stopped = True
            break
        time.sleep(0.25)
    res["stopped"] = stopped
    if not stopped:
        res["error"] = "trigger did not stop logging"

    # Restore previous cfg best-effort.
    try:
        if prev:
            _set_can_trigger_cfg(session, api_base, prev)
        else:
            _set_can_trigger_cfg(session, api_base, {"armed": False})
    except Exception:
        pass

    res["ok"] = bool(started and stopped)
    return res


def _udp_flood(stop_evt: threading.Event, rate_hz: float, payload_len: int = 512) -> Dict[str, int]:
    sent = 0
    errors = 0

    payload = os.urandom(payload_len)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.2)

        targets = [("255.255.255.255", 5001), ("192.168.0.255", 5001), ("192.168.1.255", 5001)]
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


def _can_inject_loop(
    stop_evt: threading.Event,
    api_base: str,
    channel_id: int,
    fps: float,
    batch_size: int,
    log_enabled: bool,
    emit_enabled: bool,
) -> Dict[str, int]:
    sent = 0
    errors = 0

    period_s = max(0.001, float(batch_size) / max(float(fps), 1.0))

    with requests.Session() as s:
        while not stop_evt.is_set():
            t0 = time.monotonic()
            frames = []
            for _ in range(batch_size):
                arb_id = random.randint(0x000, 0x7FF)
                data = [random.randint(0, 255) for _ in range(8)]
                frames.append({"channel_id": int(channel_id), "id": int(arb_id), "data": data})

            payload = {
                "frames": frames,
                "options": {
                    "decode": False,
                    "emit": bool(emit_enabled),
                    "log": bool(log_enabled),
                    "diag": True,
                    "listeners": False,
                },
            }

            try:
                r = s.post(f"{api_base}/api/can/inject_batch_fast", json=payload, timeout=6.0)
                r.raise_for_status()
                j = r.json() if r.content else {}
                if isinstance(j, dict) and j.get("ok") is True:
                    sent += int(j.get("injected") or 0)
                    errors += int(j.get("error_count") or 0)
                else:
                    errors += 1
            except Exception:
                errors += 1

            dt = time.monotonic() - t0
            sleep_s = period_s - dt
            if sleep_s > 0:
                time.sleep(min(0.05, sleep_s))

    return {"sent": sent, "errors": errors}


def _run_scantools(session: requests.Session, api_base: str, channel_id: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"started": [], "errors": []}
    for action in ["scan_obd", "clear_dtcs"]:
        started = False
        last_error: str | None = None
        for attempt in range(1, 7):
            try:
                j = _http_json(
                    session,
                    "POST",
                    f"{api_base}/api/scantools/run",
                    {"channel_id": channel_id, "action": action},
                    timeout_s=3.0,
                )
                if isinstance(j, dict) and j.get("status") == "started":
                    out["started"].append(action)
                    started = True
                    break
                # If the service reports busy, give it time and retry.
                if isinstance(j, dict) and j.get("status") in {"busy", "error"}:
                    last_error = str(j)
                else:
                    last_error = str(j)
            except requests.HTTPError as e:
                # 409 CONFLICT is expected when another scan is still running.
                if getattr(getattr(e, "response", None), "status_code", None) == 409:
                    last_error = str(e)
                else:
                    out["errors"].append({"action": action, "error": str(e)})
                    break
            except Exception as e:
                out["errors"].append({"action": action, "error": str(e)})
                break

            time.sleep(min(2.0, 0.5 * attempt))

        if not started and last_error is not None:
            out["errors"].append({"action": action, "error": last_error})

        # Give the scanner some time to run before the next action.
        time.sleep(1.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Full system cross-test + stress harness")
    ap.add_argument("--api", default="http://127.0.0.1:5000", help="API base URL")
    ap.add_argument("--duration-s", type=float, default=60.0, help="Total test duration")
    ap.add_argument("--formats", nargs="+", default=["csv", "mf4"], help="Logging formats")
    ap.add_argument("--stop-temp", type=float, default=80.0, help="Abort if CPU temp reaches this (°C)")
    ap.add_argument("--can-fps", type=float, default=1200.0, help="Approx CAN frames/sec per channel")
    ap.add_argument("--can-batch", type=int, default=250, help="Frames per inject_batch_fast request")
    ap.add_argument("--eth-pps", type=float, default=800.0, help="UDP packets/sec")
    ap.add_argument("--emit", action="store_true", help="Emit bus_data events (heavier)")
    ap.add_argument("--no-log", action="store_true", help="Disable backend log() for injected frames (pure load)")
    ap.add_argument("--test-can-trigger", action="store_true", help="Also test CAN trigger start/stop via /api/trigger/can")
    args = ap.parse_args()

    api_base = str(args.api).rstrip("/")
    duration_s = max(5.0, float(args.duration_s))

    results: Dict[str, Any] = {
        "api": api_base,
        "duration_s": duration_s,
        "aborted": False,
        "failure_reason": None,
        "api_failures": 0,
        "temp_max_c": None,
        "meminfo_min": {},
        "bus_load_max_by_channel": {},
        "can": {},
        "eth": {},
        "scantools": {},
        "artifacts": {},
    }

    with requests.Session() as sess:
        # Reachability
        try:
            _http_json(sess, "GET", f"{api_base}/api/runtime/status", timeout_s=2.0)
        except Exception as e:
            print(f"ERROR: API not reachable: {e}")
            return 2

        pre_logs = {it.get("name"): int(it.get("size", 0) or 0) for it in _list_logs(sess, api_base) if isinstance(it, dict) and isinstance(it.get("name"), str)}

        if not _start_bus_from_config(sess, api_base):
            print("ERROR: failed to start bus")
            return 3

        if not _start_logging(sess, api_base, list(args.formats)):
            print("ERROR: failed to start logging")
            return 3

        rt = _http_json(sess, "GET", f"{api_base}/api/runtime/status", timeout_s=3.0) or {}
        channels = (rt.get("bus") or {}).get("channels") if isinstance(rt, dict) else None
        try:
            channels = [int(c) for c in (channels or [])]
        except Exception:
            channels = [0]
        if not channels:
            channels = [0]

        stop_evt = threading.Event()

        # Workers
        can_threads: List[threading.Thread] = []
        can_results: Dict[int, Dict[str, int]] = {}
        can_lock = threading.Lock()

        def _can_runner(ch: int):
            r = _can_inject_loop(
                stop_evt,
                api_base,
                ch,
                fps=float(args.can_fps),
                batch_size=int(args.can_batch),
                log_enabled=(not bool(args.no_log)),
                emit_enabled=bool(args.emit),
            )
            with can_lock:
                can_results[int(ch)] = r

        for ch in channels:
            t = threading.Thread(target=_can_runner, args=(int(ch),), daemon=True)
            can_threads.append(t)
            t.start()

        eth_result: Dict[str, int] = {}

        def _eth_runner():
            nonlocal eth_result
            eth_result = _udp_flood(stop_evt, rate_hz=float(args.eth_pps))

        eth_thread = threading.Thread(target=_eth_runner, daemon=True)
        eth_thread.start()

        # Cross-test ScanTools early while traffic is running.
        results["scantools"] = _run_scantools(sess, api_base, channel_id=int(channels[0]))

        if bool(args.test_can_trigger):
            # Run this early so it is not masked by long-running manual sessions.
            results["can_trigger"] = _test_can_trigger(sess, api_base)

        t_end = time.monotonic() + duration_s
        mem_min: Dict[str, int] = {}

        while time.monotonic() < t_end:
            # Temp
            tc = _read_temp_c()
            if tc is not None:
                results["temp_max_c"] = tc if results["temp_max_c"] is None else max(float(results["temp_max_c"]), float(tc))
                if tc >= float(args.stop_temp):
                    results["aborted"] = True
                    results["failure_reason"] = f"temp_reached_{tc:.1f}C"
                    break

            # Memory pressure snapshot (kB) - store minima across run
            mi = _read_meminfo()
            for k in ("MemAvailable", "MemFree", "SwapFree"):
                v = mi.get(k)
                if v is None:
                    continue
                if k not in mem_min:
                    mem_min[k] = int(v)
                else:
                    mem_min[k] = min(int(mem_min[k]), int(v))

            # API health + stats
            try:
                st = _http_json(sess, "GET", f"{api_base}/api/runtime/status", timeout_s=2.0) or {}
                if not isinstance(st, dict):
                    raise ValueError("runtime_status not a dict")
                stats = _http_json(sess, "GET", f"{api_base}/api/bus/stats", timeout_s=2.0) or {}
                bl = stats.get("bus_load_by_channel") if isinstance(stats, dict) else None
                if isinstance(bl, dict):
                    for k, v in bl.items():
                        try:
                            kk = int(k)
                            vv = float(v)
                        except Exception:
                            continue
                        prev = results["bus_load_max_by_channel"].get(str(kk))
                        if prev is None:
                            results["bus_load_max_by_channel"][str(kk)] = vv
                        else:
                            results["bus_load_max_by_channel"][str(kk)] = max(float(prev), vv)
            except Exception:
                results["api_failures"] = int(results.get("api_failures") or 0) + 1
                if results["api_failures"] >= 10:
                    results["aborted"] = True
                    results["failure_reason"] = "api_unstable"
                    break

            time.sleep(0.5)

        stop_evt.set()
        for t in can_threads:
            t.join(timeout=2.0)
        eth_thread.join(timeout=2.0)

        _stop_logging(sess, api_base)

        results["meminfo_min"] = mem_min
        results["can"] = {str(k): v for k, v in can_results.items()}
        results["eth"] = eth_result

        # Artifact validation
        post_logs = _list_logs(sess, api_base)
        new_logs = []
        for it in post_logs:
            if not isinstance(it, dict):
                continue
            name = it.get("name")
            if not isinstance(name, str):
                continue
            try:
                size = int(it.get("size", 0) or 0)
            except Exception:
                size = 0
            prev_size = pre_logs.get(name)
            # Treat as "new" if:
            # - name didn't exist before, OR
            # - size increased relative to snapshot (handles same-second session name collisions).
            if prev_size is not None and int(size) <= int(prev_size):
                continue
            new_logs.append({"name": name, "size": size})

        def _pick(pattern: str) -> Dict[str, Any] | None:
            # pattern is a suffix or substring matcher; keep it simple.
            cand = [x for x in new_logs if pattern in x["name"]]
            if not cand:
                return None
            cand.sort(key=lambda x: int(x.get("size") or 0), reverse=True)
            return cand[0]

        results["artifacts"]["csv"] = _pick(".csv")
        # Prefer main session MF4 over eth
        mf4s = [x for x in new_logs if x["name"].lower().endswith(".mf4")]
        mf4s.sort(
            key=lambda x: (
                1 if (".eth." in x["name"].lower() or x["name"].lower().endswith(".eth.mf4")) else 0,
                -int(x.get("size") or 0),
            )
        )
        results["artifacts"]["mf4"] = mf4s[0] if mf4s else None
        results["artifacts"]["vag_scan"] = _pick("vag_scan_")

        ok = True
        if results["aborted"]:
            ok = False
        if not results["artifacts"]["csv"] or int(results["artifacts"]["csv"].get("size") or 0) <= 0:
            ok = False
        if not results["artifacts"]["mf4"] or int(results["artifacts"]["mf4"].get("size") or 0) <= 0:
            ok = False
        if not results["artifacts"]["vag_scan"]:
            ok = False

        print(json.dumps(results, indent=2, sort_keys=True))

        if not ok:
            return 5 if not results["aborted"] else 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
