#!/usr/bin/env python3

import argparse
import os
import random
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import cantools
except Exception as e:  # pragma: no cover
    cantools = None
    _cantools_import_error = e


def _project_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, '..'))


def _list_dbc_files(dbc_dir: str) -> List[str]:
    try:
        names = [n for n in os.listdir(dbc_dir) if n.lower().endswith('.dbc')]
    except Exception:
        names = []
    return sorted(names)


def _load_logger_channels(base_url: str, timeout_s: float) -> List[Dict[str, Any]]:
    res = requests.get(f"{base_url.rstrip('/')}/api/config", timeout=timeout_s)
    res.raise_for_status()
    data = res.json() or {}
    cfg = data.get('config') if isinstance(data, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    chans = cfg.get('logger_channels')
    return chans if isinstance(chans, list) else []


def _pick_signal_value(sig, state: Dict[str, Any], state_key: str) -> Any:
    """Pick a 'coherent' value: enums when available, otherwise bounded random-walk."""

    # Prefer enum/choices if present
    try:
        choices = getattr(sig, 'choices', None)
        if isinstance(choices, dict) and choices:
            # Keep last choice most of the time to look stable.
            if state_key in state and random.random() < 0.85:
                return state[state_key]
            return random.choice(list(choices.keys()))
    except Exception:
        pass

    mn = getattr(sig, 'minimum', None)
    mx = getattr(sig, 'maximum', None)
    is_float = bool(getattr(sig, 'is_float', False))

    # If we have bounds, do a small random-walk inside them
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
            if cur is None:
                cur = (mn_f + mx_f) / 2.0
            else:
                cur = float(cur)

            span = max(1e-9, (mx_f - mn_f))
            step = span * 0.02  # 2% of range per tick
            cur = cur + (random.random() * 2.0 - 1.0) * step
            if cur < mn_f:
                cur = mn_f
            if cur > mx_f:
                cur = mx_f

            out: Any
            if is_float:
                out = float(cur)
            else:
                out = int(round(cur))
            state[state_key] = out
            return out
        except Exception:
            state[state_key] = 0
            return 0

    # No bounds: keep stable default
    if state_key not in state:
        state[state_key] = 0
    return state[state_key]


def _load_dbc_messages(dbc_path: str, max_messages: int) -> List[Any]:
    if cantools is None:
        raise RuntimeError(f"cantools import failed: {_cantools_import_error}")

    def _load(path: str):
        return cantools.database.load_file(path, strict=False)

    try:
        db = _load(dbc_path)
    except Exception:
        # Fallback: strip attribute sections and mark extended IDs in cantools-compatible form.
        try:
            with open(dbc_path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception:
            raise

        # Drop attribute sections (often unused for decode/encode but can break parsing).
        try:
            kept = []
            for line in text.splitlines():
                s = line.lstrip()
                if (
                    s.startswith('BA_')
                    or s.startswith('BA_DEF_')
                    or s.startswith('BA_DEF_DEF_')
                    or s.startswith('BA_DEF_REL_')
                    or s.startswith('BA_DEF_DEF_REL_')
                ):
                    continue
                kept.append(line)
            text = '\n'.join(kept) + '\n'
        except Exception:
            pass

        # Rewrite message IDs: cantools marks extended frames by setting bit 31 in the DBC ID.
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

        sanitized = text
        if mapping:
            sanitized = _sub(r'^(\s*BO_\s+)(\d+)(\s+)', sanitized)
            sanitized = _sub(r'^(\s*CM_\s+BO_\s+)(\d+)(\s+)', sanitized)
            sanitized = _sub(r'^(\s*CM_\s+SG_\s+)(\d+)(\s+)', sanitized)
            sanitized = _sub(r'^(\s*VAL_\s+)(\d+)(\s+)', sanitized)
            sanitized = _sub(r'^(\s*BO_TX_BU_\s+)(\d+)(\s*:\s*)', sanitized)
            sanitized = _sub(r'^(\s*SIG_GROUP_\s+)(\d+)(\s+)', sanitized)
            sanitized = _sub(r'^(\s*SIG_VALTYPE_\s+)(\d+)(\s+)', sanitized)

        with tempfile.NamedTemporaryFile('w', suffix='.sanitized.dbc', delete=False, encoding='utf-8') as tf:
            tf.write(sanitized)
            tmp_path = tf.name

        db = _load(tmp_path)

    messages = [m for m in (db.messages or []) if getattr(m, 'length', 8) <= 8]

    # Prefer messages with at least one signal
    msgs_with_signals = [m for m in messages if getattr(m, 'signals', None)]
    if msgs_with_signals:
        messages = msgs_with_signals

    if max_messages > 0:
        messages = messages[:max_messages]
    return messages


def _encode_message(msg) -> Tuple[int, List[int]]:
    raise NotImplementedError("use _encode_message_with_state")


def _encode_message_with_state(msg, state: Dict[str, Any], prefix: str) -> Tuple[int, List[int]]:
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

    # Ensure DLC matches message length when possible
    try:
        length = int(getattr(msg, 'length', len(data)))
        if length > 0:
            data = (data + [0] * length)[:length]
    except Exception:
        pass

    return int(getattr(msg, 'frame_id', 0)), data


def _validate_all_dbcs(dbc_dir: str, max_messages: int) -> int:
    names = _list_dbc_files(dbc_dir)
    if not names:
        print(f"No DBC files found in {dbc_dir}")
        return 2

    ok = 0
    bad = 0
    for name in names:
        path = os.path.join(dbc_dir, name)
        try:
            msgs = _load_dbc_messages(path, max_messages)
            print(f"OK  {name}  messages_used={len(msgs)}")
            ok += 1
        except Exception as e:
            print(f"BAD {name}  error={e}")
            bad += 1

    print(f"summary: ok={ok} bad={bad} total={ok+bad}")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description='Simulate CAN traffic based on configured per-channel DBCs')
    ap.add_argument('--base-url', default='http://127.0.0.1:5000', help='Kvaser Bus Manager base URL')
    ap.add_argument('--duration-s', type=float, default=30.0, help='How long to run')
    ap.add_argument('--rate-hz', type=float, default=50.0, help='Frames/sec per configured channel')
    ap.add_argument('--batch-size', type=int, default=200, help='Frames per /api/can/inject_batch call')
    ap.add_argument('--messages-per-dbc', type=int, default=25, help='Max messages to use per DBC (filters to <= 8 bytes)')
    ap.add_argument('--channels', default='', help='Optional comma-separated channel ids to include (e.g. "0,1,2")')
    ap.add_argument('--timeout-s', type=float, default=2.0, help='HTTP timeout')
    ap.add_argument('--seed', type=int, default=0, help='Random seed (0 disables)')
    ap.add_argument('--validate-all-dbcs', action='store_true', help='Just load/validate all DBC files in databases/dbc and report failures')
    ap.add_argument('--all-dbcs', action='store_true', help='Ignore logger_channels and simulate traffic from all DBCs into --default-channel')
    ap.add_argument('--default-channel', type=int, default=0, help='Channel id to use with --all-dbcs')
    args = ap.parse_args()

    if args.seed:
        random.seed(args.seed)

    root = _project_root()
    dbc_dir = os.path.join(root, 'databases', 'dbc')

    if args.validate_all_dbcs:
        return _validate_all_dbcs(dbc_dir, args.messages_per_dbc)

    allow_ids: Optional[set[int]] = None
    if str(args.channels).strip():
        allow_ids = set(int(x.strip()) for x in str(args.channels).split(',') if x.strip() != '')

    channel_messages: Dict[int, List[Any]] = {}

    if args.all_dbcs:
        ch_id = int(args.default_channel)
        if allow_ids is not None and ch_id not in allow_ids:
            raise SystemExit(f"default channel {ch_id} not in --channels")

        msgs_all: List[Any] = []
        for name in _list_dbc_files(dbc_dir):
            dbc_path = os.path.join(dbc_dir, name)
            try:
                msgs = _load_dbc_messages(dbc_path, args.messages_per_dbc)
                msgs_all.extend(msgs)
            except Exception as e:
                print(f"WARN: Failed to load DBC {name}: {e}")
                continue
        if msgs_all:
            channel_messages[ch_id] = msgs_all
    else:
        chans = _load_logger_channels(args.base_url, args.timeout_s)
        if not chans:
            raise SystemExit('No logger_channels found in /api/config. Configure channels+DBCs in the UI first.')

        for ch in chans:
            try:
                ch_id = int(ch.get('id'))
            except Exception:
                continue

            if allow_ids is not None and ch_id not in allow_ids:
                continue

            dbc_name = str(ch.get('dbc_name') or '').strip()
            if not dbc_name:
                continue

            dbc_path = os.path.join(dbc_dir, os.path.basename(dbc_name))
            if not os.path.isfile(dbc_path):
                print(f"WARN: DBC not found for channel {ch_id}: {dbc_path}")
                continue

            try:
                msgs = _load_dbc_messages(dbc_path, args.messages_per_dbc)
            except Exception as e:
                print(f"WARN: Failed to load DBC for channel {ch_id} ({dbc_name}): {e}")
                continue

            if msgs:
                channel_messages[ch_id] = msgs

    if not channel_messages:
        raise SystemExit('No channels with valid dbc_name found. Assign a DBC to at least one channel.')

    session = requests.Session()
    end_t = time.time() + float(args.duration_s)
    period_s = 1.0 / max(float(args.rate_hz), 0.1)
    next_t: Dict[int, float] = {cid: time.time() for cid in channel_messages.keys()}

    signal_state: Dict[str, Any] = {}

    frames: List[Dict[str, Any]] = []
    sent = 0
    errors = 0

    while time.time() < end_t:
        now = time.time()
        any_due = False

        for cid, msgs in channel_messages.items():
            if now < next_t[cid]:
                continue
            any_due = True
            next_t[cid] = now + period_s

            msg = random.choice(msgs)
            try:
                arb_id, data = _encode_message_with_state(msg, signal_state, prefix=str(cid))
                frames.append({
                    'channel_id': cid,
                    'id': int(arb_id),
                    'data': data,
                })
            except Exception:
                errors += 1

        if len(frames) >= int(args.batch_size):
            try:
                res = session.post(
                    f"{args.base_url.rstrip('/')}/api/can/inject_batch",
                    json={'frames': frames},
                    timeout=args.timeout_s,
                )
                if res.ok:
                    j = res.json() or {}
                    sent += int(j.get('injected') or 0)
                else:
                    errors += 1
                frames = []
            except Exception:
                errors += 1
                frames = []

        if not any_due:
            time.sleep(min(0.01, max(0.0, min(next_t.values()) - now)))

    # Flush
    if frames:
        try:
            res = session.post(
                f"{args.base_url.rstrip('/')}/api/can/inject_batch",
                json={'frames': frames},
                timeout=args.timeout_s,
            )
            if res.ok:
                j = res.json() or {}
                sent += int(j.get('injected') or 0)
            else:
                errors += 1
        except Exception:
            errors += 1

    print(f"done: sent={sent} errors={errors} channels={sorted(channel_messages.keys())}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
