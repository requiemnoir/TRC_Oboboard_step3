import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple


def _now_ms() -> int:
    return int(time.time() * 1000)


def _raw_mf4_bus_type_code(frame_type: Any) -> int:
    try:
        ft = str(frame_type or '').strip().upper()
    except Exception:
        ft = ''
    if ft in {'CAN-FD', 'CANFD'}:
        return 2
    if ft in {'FLEXRAY', 'FLEX', 'FR'}:
        return 3
    if ft == 'LIN':
        return 4
    if ft in {'ETH', 'ETHERNET'}:
        return 5
    return 1


def _truthy(v) -> bool:
    return str(v or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _boolish(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, (int, float)):
        try:
            return float(v) != 0.0
        except Exception:
            return False
    # cantools NamedSignalValue / enum-like objects
    try:
        vv = getattr(v, 'value', None)
        if vv is not None and not isinstance(vv, bool):
            try:
                return float(vv) != 0.0
            except Exception:
                pass
    except Exception:
        pass
    try:
        nm = getattr(v, 'name', None)
        if nm is not None:
            # Recurse into string heuristics below
            return _boolish(str(nm))
    except Exception:
        pass
    # bytes/np.bytes_ -> decode as text
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode('utf-8', errors='ignore')
    except Exception:
        pass
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {'on', 'active', 'enabled', 'true', 'yes', '1'}:
            return True
        if s in {'off', 'inactive', 'disabled', 'false', 'no', '0', ''}:
            return False
        # Heuristics for enum-like lamp strings (common in logs/measurements)
        # e.g. 'EPCL_aus_kein_Text' (off), 'EPCL_gelb_Stoerung' (fault)
        try:
            if any(tok in s for tok in ['aus', 'off', 'inactive', 'disabled', 'kein', 'none', 'no_text']):
                return False
            if any(tok in s for tok in ['ein', 'on', 'active', 'enabled', 'stoerung', 'störung', 'fehler', 'fault', 'error', 'warn', 'warning', 'gelb', 'yellow', 'rot', 'red']):
                return True
        except Exception:
            pass
        return _truthy(s)
    try:
        return float(v) != 0.0
    except Exception:
        return False


def _json_safe_value(v: Any) -> Any:
    """Return a JSON-serializable representation for common decoded signal types.

    cantools may return enums/NamedSignalValue objects which are not JSON serializable.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    # cantools NamedSignalValue often exposes .value and .name
    try:
        name = getattr(v, 'name', None)
        value = getattr(v, 'value', None)
        if name is not None or value is not None:
            out = {}
            if name is not None:
                out['name'] = str(name)
            if value is not None:
                try:
                    out['value'] = int(value)
                except Exception:
                    try:
                        out['value'] = float(value)
                    except Exception:
                        out['value'] = str(value)
            return out
    except Exception:
        pass

    # Fallback: try numeric casts, else stringify.
    try:
        return int(v)
    except Exception:
        pass
    try:
        return float(v)
    except Exception:
        pass
    try:
        return str(v)
    except Exception:
        return None


def _json_sanitize(obj: Any) -> Any:
    """Recursively sanitize data structures to be JSON-serializable."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            out[str(k)] = _json_sanitize(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_json_sanitize(x) for x in obj]
    return _json_safe_value(obj)


def _to_number_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return float(v)
        except Exception:
            return None
    # cantools NamedSignalValue
    try:
        vv = getattr(v, 'value', None)
        if vv is not None:
            try:
                return float(vv)
            except Exception:
                pass
    except Exception:
        pass
    # string
    try:
        if isinstance(v, (bytes, bytearray)):
            v = v.decode('utf-8', errors='ignore')
        if isinstance(v, str):
            s = v.strip()
            if s == '':
                return None
            try:
                return float(s)
            except Exception:
                sl = s.lower()
                # enum-like strings: map to 0/1 for UI coloring
                if any(tok in sl for tok in ['aus', 'off', 'inactive', 'disabled', 'kein', 'none', 'no_text']):
                    return 0.0
                if any(tok in sl for tok in ['ein', 'on', 'active', 'enabled', 'stoerung', 'störung', 'fehler', 'fault', 'error', 'warn', 'warning', 'gelb', 'yellow', 'rot', 'red']):
                    return 1.0
                return None
    except Exception:
        pass
    # dict from _json_safe_value
    try:
        if isinstance(v, dict) and 'value' in v:
            return float(v.get('value'))
    except Exception:
        pass
    return None


@dataclass
class TraceFrame:
    ts_ms: int
    channel: int
    frame_id: int
    data: List[int]
    flags: int
    frame_type: str
    decoded_name: str = ''
    decoded_signals: Optional[Dict[str, Any]] = None


class TraceRingBuffer:
    """Time-based ring buffer for CAN trace excerpts.

    Keeps frames in RAM for a bounded time window; supports extracting a slice
    by timestamp.
    """

    def __init__(
        self,
        *,
        keep_ms: int = 20000,
        max_frames: int = 200000,
        decoded_signal_preview_limit: int = 10,
    ):
        self.keep_ms = int(max(1000, keep_ms))
        self.max_frames = int(max(1000, max_frames))
        self.decoded_signal_preview_limit = int(max(0, decoded_signal_preview_limit))
        self._lock = threading.Lock()
        self._frames: Deque[TraceFrame] = deque()
        self._dropped = 0
        self._watch_messages: set[str] = set()

    def set_watch_messages(self, names: List[str]) -> None:
        cleaned = set()
        for n in names or []:
            s = str(n or '').strip()
            if s:
                cleaned.add(s)
        with self._lock:
            self._watch_messages = cleaned

    def status(self) -> Dict[str, Any]:
        with self._lock:
            oldest = self._frames[0].ts_ms if self._frames else None
            newest = self._frames[-1].ts_ms if self._frames else None
            return {
                'keep_ms': self.keep_ms,
                'max_frames': self.max_frames,
                'size': len(self._frames),
                'dropped': int(self._dropped),
                'oldest_ts_ms': oldest,
                'newest_ts_ms': newest,
            }

    def add(self, frame: Dict[str, Any]) -> None:
        try:
            ts_ms = int(frame.get('timestamp') or 0)
        except Exception:
            ts_ms = 0
        if ts_ms <= 0:
            ts_ms = _now_ms()

        try:
            channel = int(frame.get('channel') or 0)
        except Exception:
            channel = 0
        try:
            frame_id = int(frame.get('id') or 0)
        except Exception:
            frame_id = 0
        try:
            flags = int(frame.get('flags') or 0)
        except Exception:
            flags = 0
        data = frame.get('data')
        if not isinstance(data, list):
            data = []
        try:
            data = [int(x) & 0xFF for x in data]
        except Exception:
            data = []
        frame_type = str(frame.get('type') or 'CAN')

        # Special handling for Ethernet payloads (passed as hex string or bytes)
        if frame_type == 'ETH':
            raw_payload = frame.get('payload_hex')
            if not raw_payload and isinstance(frame.get('data'), (bytes, str)):
                # Fallback if 'data' is already valid
                 pass
            elif raw_payload:
                try:
                    # Convert hex string to list of ints for consistency with TraceFrame
                    # For huge frames, we might want to truncate or store as bytes.
                    # Storing as bytes is compatible with _render_trace_table iteration.
                    data = bytes.fromhex(raw_payload)
                except Exception:
                    data = []

        decoded_name = ''
        decoded_signals = None
        decoded = frame.get('decoded')
        if isinstance(decoded, dict):
            decoded_name = str(decoded.get('name') or '').strip()
            sigs = decoded.get('signals')
            if isinstance(sigs, dict):
                keep_all = False
                with self._lock:
                    keep_all = bool(decoded_name and decoded_name in self._watch_messages)
                if keep_all:
                    decoded_signals = dict(sigs)
                elif self.decoded_signal_preview_limit != 0:
                    if self.decoded_signal_preview_limit > 0:
                        limited = {}
                        for i, (k, v) in enumerate(sigs.items()):
                            if i >= self.decoded_signal_preview_limit:
                                break
                            limited[str(k)] = v
                        decoded_signals = limited
                    else:
                        decoded_signals = dict(sigs)

        tf = TraceFrame(
            ts_ms=ts_ms,
            channel=channel,
            frame_id=frame_id,
            data=data,
            flags=flags,
            frame_type=frame_type,
            decoded_name=decoded_name,
            decoded_signals=decoded_signals,
        )

        with self._lock:
            self._frames.append(tf)
            self._trim_locked(now_ms=ts_ms)

    def _trim_locked(self, *, now_ms: int) -> None:
        cutoff = int(now_ms) - int(self.keep_ms)
        while self._frames and self._frames[0].ts_ms < cutoff:
            self._frames.popleft()
        while len(self._frames) > self.max_frames:
            self._frames.popleft()
            self._dropped += 1

    def slice(self, start_ms: int, end_ms: int, *, channel: Optional[int] = None) -> List[TraceFrame]:
        s = int(start_ms)
        e = int(end_ms)
        if e < s:
            s, e = e, s
        out: List[TraceFrame] = []
        with self._lock:
            for f in self._frames:
                if f.ts_ms < s:
                    continue
                if f.ts_ms > e:
                    break
                if channel is not None and int(f.channel) != int(channel):
                    continue
                out.append(f)
        return out


def parse_vag_scan_report_html(html_text: str) -> Dict[str, Any]:
    """Best-effort parser for existing scan report HTML.

    Returns a structure with dtc items that can be used for incident attribution.
    This parser intentionally avoids external deps.
    """
    text = str(html_text or '')
    items: List[Dict[str, Any]] = []

    # Newer reports group by ECU using <details id='ecu-N'><summary><strong>NAME</strong>...
    ecu_hdr_re = re.compile(
        r"<details\s+id=['\"](?P<ecu_id>ecu-\d+)['\"][^>]*>\s*<summary>\s*<strong>(?P<ecu_name>.*?)</strong>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    ecu_matches = list(ecu_hdr_re.finditer(text))

    # DTC rows in the main ECU table.
    # New 8-col format: code | state | status_byte | status_desc | timestamp | km | raw | desc
    dtc_row_re = re.compile(
        r"<tr>\s*<td[^>]*class=['\"]mono['\"][^>]*>(?P<code>.*?)</td>\s*"
        r"<td>\s*<span[^>]*state-(?P<state>active|passive)[^>]*>\s*(?P<state_txt>ACTIVE|PASSIVE)\s*</span>\s*</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<status_byte>.*?)</td>\s*"
        r"<td>(?P<status_desc>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<timestamp>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<km>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<raw>.*?)</td>\s*"
        r"<td[^>]*class=['\"]desc['\"][^>]*>(?P<desc_html>.*?)</td>\s*</tr>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Legacy 6-col format: code | state | status_byte | status_desc | raw | desc
    dtc_row_re_legacy = re.compile(
        r"<tr>\s*<td[^>]*class=['\"]mono['\"][^>]*>(?P<code>.*?)</td>\s*"
        r"<td>\s*<span[^>]*state-(?P<state>active|passive)[^>]*>\s*(?P<state_txt>ACTIVE|PASSIVE)\s*</span>\s*</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<status_byte>.*?)</td>\s*"
        r"<td>(?P<status_desc>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<raw>.*?)</td>\s*"
        r"<td[^>]*class=['\"]desc['\"][^>]*>(?P<desc_html>.*?)</td>\s*</tr>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _strip_tags(s: str) -> str:
        s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
        s = re.sub(r'<[^>]+>', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    def _extract_ctx(desc_html: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        # KM@fault: 1234 km
        m = re.search(r"KM@fault:\s*([^<]+)", desc_html, flags=re.IGNORECASE)
        if m:
            out['km_at_fault_text'] = m.group(1).strip()
            m2 = re.search(r"(\d+)", out['km_at_fault_text'])
            if m2:
                try:
                    out['odometer_km'] = int(m2.group(1))
                except Exception:
                    pass
        # Timestamp: 2026-02-06 05:45:53
        m = re.search(r"Timestamp:\s*([^<]+)", desc_html, flags=re.IGNORECASE)
        if m:
            ts = m.group(1).strip()
            out['timestamp_text'] = ts
            # Try parse to epoch ms
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
                try:
                    dt = datetime.strptime(ts, fmt)
                    out['timestamp_epoch_ms'] = int(dt.timestamp() * 1000)
                    break
                except Exception:
                    continue
        return out

    def _parse_row(rm, *, ecu_id: str = '', ecu_name: str = '', is_legacy: bool = False) -> Optional[Dict[str, Any]]:
        code = _strip_tags(str(rm.group('code') or ''))
        if not code:
            return None
        state = (rm.group('state') or '').strip().lower()
        status_byte = _strip_tags(str(rm.group('status_byte') or ''))
        status_desc = _strip_tags(str(rm.group('status_desc') or ''))
        raw = _strip_tags(str(rm.group('raw') or ''))
        desc_html = str(rm.group('desc_html') if not is_legacy else rm.group('desc_html') if 'desc_html' in rm.groupdict() else rm.group('desc') if 'desc' in rm.groupdict() else '')
        desc = _strip_tags(desc_html)
        ctx = _extract_ctx(desc_html)

        # New columns: timestamp, km (only in 8-col format)
        if not is_legacy:
            ts_raw = _strip_tags(str(rm.group('timestamp') or '')).strip()
            km_raw = _strip_tags(str(rm.group('km') or '')).strip()
            if ts_raw:
                ctx['timestamp_text'] = ts_raw
                # Parse ISO or standard timestamp
                for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                    clean_ts = ts_raw.replace(' (scan)', '').strip()
                    try:
                        dt = datetime.strptime(clean_ts, fmt)
                        ctx['timestamp_epoch_ms'] = int(dt.timestamp() * 1000)
                        break
                    except Exception:
                        continue
                if '(scan)' in ts_raw:
                    ctx['timestamp_is_scan'] = True
            if km_raw:
                ctx['km_at_fault_text'] = km_raw
                m2 = re.search(r'(\d+)', km_raw)
                if m2:
                    try:
                        ctx['odometer_km'] = int(m2.group(1))
                    except Exception:
                        pass

        return {
            'ecu_id': ecu_id,
            'ecu_name': ecu_name,
            'code': code,
            'active': True if state == 'active' else False,
            'status_byte': status_byte,
            'status_desc': status_desc,
            'raw': raw,
            'desc_report': desc,
            **ctx,
        }

    if ecu_matches:
        for i, m in enumerate(ecu_matches):
            ecu_id = str(m.group('ecu_id') or '').strip()
            ecu_name = _strip_tags(str(m.group('ecu_name') or ''))
            start = m.end()
            end = ecu_matches[i + 1].start() if (i + 1) < len(ecu_matches) else len(text)
            block = text[start:end]

            # Try new 8-col format first, fall back to legacy 6-col
            matches_8col = list(dtc_row_re.finditer(block))
            if matches_8col:
                for rm in matches_8col:
                    item = _parse_row(rm, ecu_id=ecu_id, ecu_name=ecu_name, is_legacy=False)
                    if item:
                        items.append(item)
            else:
                for rm in dtc_row_re_legacy.finditer(block):
                    item = _parse_row(rm, ecu_id=ecu_id, ecu_name=ecu_name, is_legacy=True)
                    if item:
                        items.append(item)

        return {
            'ok': True,
            'dtcs': items,
            'count': len(items),
            'source': 'ecu_blocks',
        }

    # Row fragments (non-ECU-blocks fallback).
    # Try 8-col first, then legacy 6-col.
    row_re_8 = re.compile(
        r"<tr>\s*<td[^>]*class=['\"]mono['\"][^>]*>(?P<code>[A-Z]\w+?)</td>\s*"
        r"<td>\s*<span[^>]*state-(?P<state>active|passive)[^>]*>\s*(?P<state_txt>ACTIVE|PASSIVE)\s*</span>\s*</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<status_byte>.*?)</td>\s*"
        r"<td>(?P<status_desc>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<timestamp>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<km>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<raw>.*?)</td>\s*"
        r"<td[^>]*class=['\"]desc['\"][^>]*>(?P<desc_html>.*?)</td>\s*</tr>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    row_re_6 = re.compile(
        r"<tr>\s*<td[^>]*class=['\"]mono['\"][^>]*>(?P<code>[A-Z]\w+?)</td>\s*"
        r"<td>\s*<span[^>]*state-(?P<state>active|passive)[^>]*>\s*(?P<state_txt>ACTIVE|PASSIVE)\s*</span>\s*</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<status_byte>.*?)</td>\s*"
        r"<td>(?P<status_desc>.*?)</td>\s*"
        r"<td[^>]*class=['\"]mono['\"][^>]*>(?P<raw>.*?)</td>\s*"
        r"<td[^>]*class=['\"]desc['\"][^>]*>(?P<desc_html>.*?)</td>\s*</tr>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    matches_8 = list(row_re_8.finditer(text))
    if matches_8:
        for rm in matches_8:
            item = _parse_row(rm, is_legacy=False)
            if item:
                items.append(item)
    else:
        for rm in row_re_6.finditer(text):
            item = _parse_row(rm, is_legacy=True)
            if item:
                items.append(item)

    return {
        'ok': True,
        'dtcs': items,
        'count': len(items),
    }


def _html_escape(s: Any) -> str:
    t = str(s if s is not None else '')
    return (
        t.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&#39;')
    )


def _fmt_ts_ms(ts_ms: int) -> str:
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts_ms) / 1000.0))
    except Exception:
        return str(ts_ms)


def _parse_slot_id(name: str) -> Optional[int]:
    try:
        s = str(name or '')
    except Exception:
        return None
    m = re.search(r'\bslot\s+(\d+)\b', s, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _messages_match(decoded_name: str, mapping_name: str) -> bool:
    """Heuristic match for decoded message name vs configured mapping.

    FlexRay/FIBEX: same slot-id can have multiple triggerings with different
    display names. Match by slot-id when present.
    """
    dn = str(decoded_name or '').strip()
    mn = str(mapping_name or '').strip()
    if not dn or not mn:
        return False
    if dn == mn:
        return True

    ds = _parse_slot_id(dn)
    ms = _parse_slot_id(mn)
    if ds is not None and ms is not None:
        return int(ds) == int(ms)

    # Safe fallback only for FlexRay-ish strings.
    try:
        if ('slot ' in dn.lower()) or ('slot ' in mn.lower()):
            a = dn.lower()
            b = mn.lower()
            if len(a) >= 8 and len(b) >= 8 and (a in b or b in a):
                return True
    except Exception:
        pass

    return False


def _render_trace_table(frames: List[TraceFrame], *, t0_ms: int) -> str:
    rows = []
    for f in frames:
        dt_ms = int(f.ts_ms) - int(t0_ms)
        
        # Format data: truncate if too long (e.g. Ethernet frames)
        raw_data = f.data or []
        if len(raw_data) > 32:
            displayed_data = raw_data[:32]
            suffix = f" ... ({len(raw_data)} bytes)"
        else:
            displayed_data = raw_data
            suffix = ""
            
        data_hex = ' '.join(f'{b:02X}' for b in displayed_data) + suffix
        
        sig_preview = ''
        if isinstance(f.decoded_signals, dict) and f.decoded_signals:
            parts = []
            for k, v in list(f.decoded_signals.items())[:10]:
                parts.append(f"{k}={v}")
            sig_preview = ', '.join(parts)
        
        # ID Column: Handle 'ETH' vs CAN ID
        if f.frame_type == 'ETH':
             id_col = 'ETH'
        else:
             id_col = f"0x{int(f.frame_id):X}"

        rows.append(
            '<tr>'
            f"<td class='mono'>{dt_ms:+d}</td>"
            f"<td class='mono'>{_html_escape(_fmt_ts_ms(f.ts_ms))}</td>"
            f"<td class='mono'>{int(f.channel)}</td>"
            f"<td class='mono'>{id_col}</td>"
            f"<td>{_html_escape(f.decoded_name)}</td>"
            f"<td class='small'>{_html_escape(sig_preview)}</td>"
            f"<td class='mono'>{_html_escape(data_hex)}</td>"
            '</tr>'
        )

    if not rows:
        return "<div class='muted'>No frames captured in window.</div>"

    return (
        "<div class='hint'>30s trace window: -15000ms .. +15000ms around trigger.</div>"
        "<div class='tblwrap'><table class='tbl'>"
        "<thead><tr>"
        "<th>Δt ms</th><th>timestamp</th><th>ch</th><th>id</th><th>message</th><th>signals (preview)</th><th>data</th>"
        "</tr></thead><tbody>"
        + ''.join(rows)
        + "</tbody></table></div>"
    )


def build_incident_html(
    *,
    incident_id: str,
    mil_on_ts_ms: int,
    scan_started_ts_ms: int,
    scan_finished_ts_ms: int,
    scan_report_filename: str,
    trace_mf4_filename: str = '',
    trace_raw_mf4_filename: str = '',
    primary: Optional[Dict[str, Any]],
    dtcs: List[Dict[str, Any]],
    trace_frames: List[TraceFrame],
    lamp_snapshot: Optional[Dict[str, Any]] = None,
    notify_placeholder: Optional[Dict[str, Any]] = None,
    sentinel_analyses: Optional[List[Dict[str, Any]]] = None,
) -> str:
    primary_code = (primary or {}).get('code') if isinstance(primary, dict) else None
    primary_desc = (primary or {}).get('desc') if isinstance(primary, dict) else None
    primary_conf = (primary or {}).get('confidence') if isinstance(primary, dict) else None
    sev = (primary or {}).get('severity') if isinstance(primary, dict) else 'warning'
    primary_ecu = (primary or {}).get('ecu_name') if isinstance(primary, dict) else None
    primary_status = (primary or {}).get('status_byte') if isinstance(primary, dict) else None
    primary_flags = (primary or {}).get('status_desc') if isinstance(primary, dict) else None
    primary_km = (primary or {}).get('odometer_km') if isinstance(primary, dict) else None
    primary_ts_txt = (primary or {}).get('timestamp_text') if isinstance(primary, dict) else None

    def _badge(text: str, kind: str) -> str:
        return f"<span class='badge badge-{kind}'>{_html_escape(text)}</span>"

    sev_kind = 'crit' if sev == 'critical' else ('warn' if sev == 'warning' else 'info')

    dtc_rows = []
    for d in dtcs[:400]:
        code = _html_escape(d.get('code'))
        active = bool(d.get('active'))
        state = 'ACTIVE' if active else 'PASSIVE'
        desc = _html_escape(d.get('desc') or d.get('desc_report') or '')
        ecu = _html_escape(d.get('ecu_name') or '')
        dtc_rows.append(
            "<tr>"
            f"<td>{ecu}</td>"
            f"<td class='mono'>{code}</td>"
            f"<td>{_badge(state, 'ok' if active else 'muted')}</td>"
            f"<td class='small'>{desc}</td>"
            "</tr>"
        )

    notify_html = ''
    if isinstance(notify_placeholder, dict) and notify_placeholder:
        notify_html = (
            "<h3>Notifications (placeholder)</h3>"
            "<div class='muted'>"
            + _html_escape(str(notify_placeholder))
            + "</div>"
        )

    lamp_html = ''
    if isinstance(lamp_snapshot, dict) and lamp_snapshot:
        lamp_html = (
            "<h3>Other warning lamps (CAN mappings)</h3>"
            "<div class='muted'>EPC and gearbox lamp are derived from CAN decoded signals (if configured). Missing mappings produce no result.</div>"
            "<pre class='mb-0'>" + _html_escape(json_dumps_safe(lamp_snapshot)) + "</pre>"
        )

    sentinel_html = ''
    if isinstance(sentinel_analyses, list) and sentinel_analyses:
        cards = []
        for a in sentinel_analyses[:20]:
            if not isinstance(a, dict):
                continue
            code = _html_escape(str(a.get('code') or ''))
            desc = _html_escape(str(a.get('desc') or a.get('pdx_description') or ''))
            content = str(a.get('analysis') or '').strip()
            err = str(a.get('error') or '').strip()
            cards.append(
                "<div class='card' style='margin-top:10px;'>"
                "<div class='bd'>"
                f"<div class='muted mono'>DTC: {code or 'unknown'}</div>"
                + (f"<div class='small'>{desc}</div>" if desc else "")
                + (f"<div class='muted' style='color:#ffb4b4'>LLM error: {_html_escape(err)}</div>" if err else "")
                + ("<pre class='small' style='margin-top:10px;'>" + _html_escape(content) + "</pre>" if content else "")
                + "</div></div>"
            )
        if cards:
            sentinel_html = (
                "<h3>Sentinel (LLM) analysis</h3>"
                "<div class='muted'>Generated after scan parsing. One DTC at a time; may be slow on CPU-only systems.</div>"
                + ''.join(cards)
            )

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width, initial-scale=1'/>
  <title>MIL Incident Report — { _html_escape(incident_id) }</title>
  <style>
    body {{ background:#0b0f14; color:#e6edf3; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; }}
    a {{ color:#f2c14e; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .wrap {{ max-width: 1200px; margin: 24px auto; padding: 0 16px; }}
    .card {{ border: 1px solid #2b3036; border-radius: 10px; background:#0f141b; margin-bottom: 14px; }}
    .hd {{ padding: 14px 16px; border-bottom: 1px solid #2b3036; display:flex; justify-content:space-between; gap: 12px; align-items:center; }}
    .bd {{ padding: 14px 16px; }}
    .title {{ font-size: 18px; font-weight: 700; }}
    .muted {{ color:#9aa4ad; font-size: 13px; white-space: pre-wrap; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    h3 {{ margin: 12px 0 8px; font-size: 14px; color:#f2c14e; }}
    .badge {{ display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border:1px solid #2b3036; }}
    .badge-crit {{ background:#3b0f0f; border-color:#5a1b1b; color:#ffb4b4; }}
    .badge-warn {{ background:#3a2a0f; border-color:#5a3b12; color:#ffd08a; }}
    .badge-info {{ background:#0f243a; border-color:#153a5a; color:#a9d5ff; }}
    .badge-ok {{ background:#0f3a1f; border-color:#1a5a2d; color:#b6ffd3; }}
    .badge-muted {{ background:#1a1f27; border-color:#2b3036; color:#b2bac2; }}
    .hint {{ color:#b2bac2; font-size: 12px; margin-bottom: 8px; }}
    .tblwrap {{ overflow:auto; max-height: 520px; border: 1px solid #2b3036; border-radius: 8px; }}
    table.tbl {{ width:100%; border-collapse: collapse; }}
    .tbl th, .tbl td {{ border-bottom: 1px solid #1b2027; padding: 8px; vertical-align: top; }}
    .tbl th {{ position: sticky; top: 0; background: #0f141b; text-align:left; font-size: 12px; color:#9aa4ad; }}
    .small {{ font-size: 12px; color:#c9d1d9; white-space: pre-wrap; }}
  </style>
</head>
<body>
<div class='wrap'>

  <div class='card'>
    <div class='hd'>
      <div>
        <div class='title'>MIL Incident Report</div>
        <div class='muted mono'>incident_id={_html_escape(incident_id)}</div>
      </div>
      <div>
        {_badge(sev, sev_kind)}
        {_badge(f"confidence={primary_conf or 'unknown'}", 'info')}
      </div>
    </div>
    <div class='bd'>
      <div class='grid'>
        <div>
          <h3>Timeline</h3>
          <div class='muted mono'>MIL ON: {_html_escape(_fmt_ts_ms(mil_on_ts_ms))} ({int(mil_on_ts_ms)})</div>
          <div class='muted mono'>Scan start: {_html_escape(_fmt_ts_ms(scan_started_ts_ms))}</div>
          <div class='muted mono'>Scan end: {_html_escape(_fmt_ts_ms(scan_finished_ts_ms))}</div>
        </div>
                <div>
                    <h3>Artifacts</h3>
                    <div class='muted'>Scan report: {(
                        "<a class='mono' href='/api/logs/" + _html_escape(scan_report_filename) + "'>" + _html_escape(scan_report_filename) + "</a>"
                    ) if scan_report_filename else "(none)"}</div>
                    <div class='muted'>Trace MF4 (decoded): {(
                        "<a class='mono' href='/api/logs/" + _html_escape(trace_mf4_filename) + "'>" + _html_escape(trace_mf4_filename) + "</a>"
                    ) if trace_mf4_filename else "(none)"}</div>
                    <div class='muted'>Trace MF4 (raw): {(
                        "<a class='mono' href='/api/logs/" + _html_escape(trace_raw_mf4_filename) + "'>" + _html_escape(trace_raw_mf4_filename) + "</a>"
                    ) if trace_raw_mf4_filename else "(none)"}</div>
                </div>
      </div>

      <h3>Primary attribution (best-effort)</h3>
      <div class='muted'>The system correlates MIL ON with active DTCs and available timestamp hints. If ECU does not provide timing fields, attribution falls back to heuristics.</div>
      <div class='card' style='margin-top:10px;'>
        <div class='bd'>
                    <div class='muted'>ECU: <span class='mono'>{ _html_escape(primary_ecu or 'unknown') }</span></div>
          <div class='muted mono'>DTC: { _html_escape(primary_code or 'unknown') }</div>
                    <div class='muted'>Status: <span class='mono'>{ _html_escape(primary_status or '') }</span> &nbsp; <span class='small'>{ _html_escape(primary_flags or '') }</span></div>
                    <div class='muted'>KM@fault: <span class='mono'>{ _html_escape(primary_km or '') }</span> &nbsp; Timestamp: <span class='mono'>{ _html_escape(primary_ts_txt or '') }</span></div>
          <div class='small'>{ _html_escape(primary_desc or '') }</div>
        </div>
      </div>

      <h3>All DTCs (parsed)</h3>
      <div class='tblwrap'><table class='tbl'>
                <thead><tr><th>ecu</th><th>code</th><th>state</th><th>description (PDX / report)</th></tr></thead>
        <tbody>
                    {''.join(dtc_rows) if dtc_rows else "<tr><td colspan='4' class='muted'>No DTCs parsed.</td></tr>"}
        </tbody>
      </table></div>

    <h3>Trace</h3>
    <div class='muted'>Trace is provided as downloadable MF4 (see Artifacts / TRACE &amp; CTX ZIP). The HTML no longer embeds a large frame table.</div>

    {sentinel_html}

    {lamp_html}

      {notify_html}
    </div>
  </div>

</div>
</body>
</html>"""


def build_final_report_html(
        *,
        incident_id: str,
        mil_on_ts_ms: int,
    event_kind: str = 'mil',
    event_ts_ms: Optional[int] = None,
        scan_action: str = '',
        scan_started_ts_ms: int,
        scan_finished_ts_ms: int,
        scan_report_filename: str,
        trace_mf4_filename: str = '',
        trace_raw_mf4_filename: str = '',
        bundle_filename: str = '',
        primary: Optional[Dict[str, Any]],
        dtcs: List[Dict[str, Any]],
        lamp_snapshot: Optional[Dict[str, Any]] = None,
        sentinel_analyses: Optional[List[Dict[str, Any]]] = None,
) -> str:
        if event_ts_ms is None:
            event_ts_ms = int(mil_on_ts_ms)

        k = str(event_kind or 'mil').strip().lower()
        if k == 'mil':
            evt_label = 'MIL ON'
        elif k == 'epc':
            evt_label = 'EPC LAMP ON'
        elif k in {'gearbox', 'cambio'}:
            evt_label = 'GEARBOX LAMP ON'
        else:
            evt_label = f"{k.upper()} EVENT"

        primary_code = (primary or {}).get('code') if isinstance(primary, dict) else None
        primary_desc = (primary or {}).get('desc') if isinstance(primary, dict) else None
        primary_ecu = (primary or {}).get('ecu_name') if isinstance(primary, dict) else None
        primary_conf = (primary or {}).get('confidence') if isinstance(primary, dict) else None
        primary_sev = (primary or {}).get('severity') if isinstance(primary, dict) else 'warning'

        analyses_by_code: Dict[str, Dict[str, Any]] = {}
        if isinstance(sentinel_analyses, list):
                for a in sentinel_analyses:
                        if not isinstance(a, dict):
                                continue
                        c = str(a.get('code') or '').strip()
                        if c:
                                analyses_by_code[c] = a

        def _link(label: str, filename: str) -> str:
                if not filename:
                        return "<span class='muted'>(none)</span>"
                fn = _html_escape(filename)
                return f"<a class='mono' href='/api/logs/{fn}'>{_html_escape(label)}: {fn}</a>"

        dtc_items_html = []
        for d in (dtcs or [])[:200]:
                if not isinstance(d, dict):
                        continue
                code = str(d.get('code') or '').strip()
                if not code:
                        continue
                ecu = str(d.get('ecu_name') or '').strip()
                active = bool(d.get('active'))
                desc = str(d.get('desc') or d.get('desc_pdx') or d.get('desc_report') or '').strip()
                a = analyses_by_code.get(code) or {}
                analysis = str(a.get('analysis') or '').strip()
                err = str(a.get('error') or '').strip()
                badge = "ACTIVE" if active else "PASSIVE"
                badge_cls = "ok" if active else "muted"
                has_llm = bool(analysis)
                llm_status = "ok" if has_llm else ("err" if err else "muted")
                llm_txt = "LLM" if has_llm else ("LLM error" if err else "LLM none")

                dtc_items_html.append(
                        "<details class='dtc' open>" if active else "<details class='dtc'>"
                )
                dtc_items_html.append(
                        "<summary>"
                        f"<span class='code mono'>{_html_escape(code)}</span>"
                        f"<span class='badge {badge_cls}'>{_html_escape(badge)}</span>"
                        f"<span class='badge {llm_status}'>{_html_escape(llm_txt)}</span>"
                        f"<span class='ecu muted'>{_html_escape(ecu)}</span>"
                        "</summary>"
                )
                if desc:
                        dtc_items_html.append(f"<div class='desc small'>{_html_escape(desc)}</div>")

                # Show timestamp / km / context pills
                d_ts = str(d.get('timestamp_text') or '').strip()
                d_km = d.get('odometer_km')
                d_km_txt = str(d.get('km_at_fault_text') or '').strip()
                ctx_pills = []
                if d_ts:
                    is_scan = d.get('timestamp_is_scan')
                    label = 'Scan time' if is_scan else 'Fault time'
                    ctx_pills.append(f"<span class='badge muted'>{_html_escape(label)}: {_html_escape(d_ts)}</span>")
                if isinstance(d_km, int):
                    ctx_pills.append(f"<span class='badge muted'>KM: {d_km}</span>")
                elif d_km_txt:
                    ctx_pills.append(f"<span class='badge muted'>KM: {_html_escape(d_km_txt)}</span>")
                d_status_desc = str(d.get('status_desc') or '').strip()
                if d_status_desc:
                    ctx_pills.append(f"<span class='badge muted'>{_html_escape(d_status_desc)}</span>")
                if ctx_pills:
                    dtc_items_html.append("<div class='desc small'>" + ' '.join(ctx_pills) + "</div>")

                if err:
                        dtc_items_html.append(f"<div class='err small'>LLM error: {_html_escape(err)}</div>")
                if analysis:
                        dtc_items_html.append("<pre class='analysis'>" + _html_escape(analysis) + "</pre>")
                dtc_items_html.append("</details>")

        lamps_html = ''
        if isinstance(lamp_snapshot, dict) and lamp_snapshot:
                lamps_html = "<pre class='analysis'>" + _html_escape(json_dumps_safe(lamp_snapshot)) + "</pre>"

        return f"""<!doctype html>
<html lang='en'>
<head>
    <meta charset='utf-8'/>
    <meta name='viewport' content='width=device-width, initial-scale=1'/>
    <title>Sentinel Final Report — {_html_escape(incident_id)}</title>
    <style>
        :root {{ --bg:#0b0f14; --panel:#0f141b; --line:#2b3036; --muted:#9aa4ad; --text:#e6edf3; --accent:#f2c14e; --ok:#1a5a2d; --warn:#5a3b12; --err:#5a1b1b; }}
        body {{ background:var(--bg); color:var(--text); font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin:0; }}
        a {{ color:var(--accent); text-decoration:none; }}
        a:hover {{ text-decoration:underline; }}
        .wrap {{ max-width: 1100px; margin: 22px auto; padding: 0 16px; }}
        .card {{ border:1px solid var(--line); border-radius: 12px; background:var(--panel); margin: 12px 0; }}
        .hd {{ padding: 14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap: 12px; align-items:center; }}
        .bd {{ padding: 14px 16px; }}
        .title {{ font-weight:800; font-size: 18px; }}
        .muted {{ color:var(--muted); font-size: 13px; }}
        .small {{ font-size: 13px; white-space: pre-wrap; }}
        .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
        .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
        .badge {{ display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border:1px solid var(--line); margin-left: 8px; }}
        .badge.ok {{ background:#0f3a1f; border-color:#1a5a2d; color:#b6ffd3; }}
        .badge.muted {{ background:#1a1f27; border-color:var(--line); color:#b2bac2; }}
        .badge.err {{ background:#3b0f0f; border-color:#5a1b1b; color:#ffb4b4; }}
        h3 {{ margin: 12px 0 8px; font-size: 14px; color: var(--accent); }}
        pre.analysis {{ white-space: pre-wrap; background:#0b0f14; border:1px solid #20262d; padding: 12px; border-radius: 10px; overflow:auto; }}
        details.dtc {{ border:1px solid #20262d; border-radius: 10px; margin: 10px 0; background:#0b0f14; }}
        details.dtc summary {{ cursor:pointer; padding: 10px 12px; display:flex; gap: 10px; align-items:center; }}
        details.dtc .desc {{ padding: 0 12px 10px; }}
        details.dtc pre {{ margin: 0 12px 12px; }}
        .code {{ min-width: 110px; }}
        .ecu {{ flex: 1; overflow:hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .err {{ color:#ffb4b4; }}
    </style>
</head>
<body>
<div class='wrap'>

    <div class='card'>
        <div class='hd'>
            <div>
                <div class='title'>Sentinel Final Report</div>
                <div class='muted mono'>incident_id={_html_escape(incident_id)}</div>
            </div>
            <div>
                <span class='badge muted'>{_html_escape(str(primary_sev or 'warning'))}</span>
                <span class='badge muted'>{_html_escape(f"confidence={primary_conf or 'unknown'}")}</span>
            </div>
        </div>
        <div class='bd'>
            <div class='grid'>
                <div>
                    <h3>Timeline</h3>
                    <div class='muted mono'>{_html_escape(evt_label)}: {_html_escape(_fmt_ts_ms(int(event_ts_ms or 0)))} ({int(event_ts_ms or 0)})</div>
                    <div class='muted mono'>Scan: {_html_escape(_fmt_ts_ms(scan_started_ts_ms))} → {_html_escape(_fmt_ts_ms(scan_finished_ts_ms))}</div>
                    <div class='muted mono'>Scan action: {_html_escape(str(scan_action or ''))}</div>
                </div>
                <div>
                    <h3>Artifacts</h3>
                    <div class='muted'>{_link('Scan report', scan_report_filename)}</div>
                    <div class='muted'>{_link('Trace MF4 (decoded)', trace_mf4_filename)}</div>
                    <div class='muted'>{_link('Trace MF4 (raw)', trace_raw_mf4_filename)}</div>
                    <div class='muted'>{_link('Bundle ZIP', bundle_filename)}</div>
                </div>
            </div>

            <h3>Primary attribution</h3>
            <div class='small'>ECU: <span class='mono'>{_html_escape(primary_ecu or 'unknown')}</span></div>
            <div class='small'>DTC: <span class='mono'>{_html_escape(primary_code or 'unknown')}</span></div>
            <div class='small'>{_html_escape(primary_desc or '')}</div>

            <h3>DTCs + Analysis</h3>
            <div class='muted'>Each DTC is a collapsible section. Sentinel analysis appears when enabled and available.</div>
            {''.join(dtc_items_html) if dtc_items_html else "<div class='muted'>No DTCs available.</div>"}

            <h3>Other lamps</h3>
            {lamps_html or "<div class='muted'>(none)</div>"}
        </div>
    </div>

</div>
</body>
</html>"""


def build_lamp_incident_html(
        *,
        incident_id: str,
        kind: str,
        lamp_on_ts_ms: int,
        mapping: Optional[Dict[str, Any]],
        trace_frames: List[TraceFrame],
    scan_report_filename: str = '',
    trace_mf4_filename: str = '',
    trace_raw_mf4_filename: str = '',
        lamp_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
        k = str(kind or '').strip().lower() or 'lamp'
        title = 'Lamp Incident Report'
        if k == 'epc':
                title = 'EPC Lamp Incident Report'
        if k in {'gearbox', 'cambio'}:
                title = 'Gearbox Lamp Incident Report'

        map_html = ''
        if isinstance(mapping, dict) and mapping:
                map_html = "<h3>Mapping</h3><pre class='mb-0'>" + _html_escape(json_dumps_safe(mapping)) + "</pre>"

        lamp_html = ''
        if isinstance(lamp_snapshot, dict) and lamp_snapshot:
                lamp_html = (
                        "<h3>Other warning lamps (CAN mappings)</h3>"
                        "<pre class='mb-0'>" + _html_escape(json_dumps_safe(lamp_snapshot)) + "</pre>"
                )

        return f"""<!doctype html>
<html lang='en'>
<head>
    <meta charset='utf-8'/>
    <meta name='viewport' content='width=device-width, initial-scale=1'/>
    <title>{_html_escape(title)} — {_html_escape(incident_id)}</title>
    <style>
        body {{ background:#0b0f14; color:#e6edf3; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; }}
        a {{ color:#f2c14e; text-decoration:none; }}
        a:hover {{ text-decoration:underline; }}
        .wrap {{ max-width: 1200px; margin: 24px auto; padding: 0 16px; }}
        .card {{ border: 1px solid #2b3036; border-radius: 10px; background:#0f141b; margin-bottom: 14px; }}
        .hd {{ padding: 14px 16px; border-bottom: 1px solid #2b3036; display:flex; justify-content:space-between; gap: 12px; align-items:center; }}
        .bd {{ padding: 14px 16px; }}
        .title {{ font-weight:700; font-size: 18px; }}
        .muted {{ color:#9aa4af; font-size: 13px; }}
        .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
        .tblwrap {{ overflow:auto; }}
        .tbl {{ width:100%; border-collapse: collapse; }}
        .tbl th, .tbl td {{ border-bottom: 1px solid #20262d; padding: 8px 10px; vertical-align: top; }}
        .tbl th {{ text-align:left; color:#cdd9e5; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }}
        .small {{ font-size: 12px; }}
        pre {{ background:#0b0f14; border:1px solid #20262d; padding:10px; border-radius:8px; overflow:auto; }}
        .hint {{ color:#9aa4af; font-size: 12px; margin: 8px 0 10px; }}
    </style>
</head>
<body>
    <div class='wrap'>
        <div class='card'>
            <div class='hd'>
                <div>
                    <div class='title'>{_html_escape(title)}</div>
                    <div class='muted mono'>Lamp ON: {_html_escape(_fmt_ts_ms(lamp_on_ts_ms))} ({int(lamp_on_ts_ms)})</div>
                </div>
                <div class='muted mono'>{_html_escape(incident_id)}</div>
            </div>
            <div class='bd'>
                <div class='muted'>Incident triggers on OFF→ON edge of the configured decoded signal.</div>
                <h3>Artifacts</h3>
                <div class='muted'>Scan report: {(
                    "<a class='mono' href='/api/logs/" + _html_escape(scan_report_filename) + "'>" + _html_escape(scan_report_filename) + "</a>"
                ) if scan_report_filename else "(none)"}</div>
                <div class='muted'>Trace MF4 (decoded): {(
                    "<a class='mono' href='/api/logs/" + _html_escape(trace_mf4_filename) + "'>" + _html_escape(trace_mf4_filename) + "</a>"
                ) if trace_mf4_filename else "(none)"}</div>
                <div class='muted'>Trace MF4 (raw): {(
                    "<a class='mono' href='/api/logs/" + _html_escape(trace_raw_mf4_filename) + "'>" + _html_escape(trace_raw_mf4_filename) + "</a>"
                ) if trace_raw_mf4_filename else "(none)"}</div>
                {map_html}
                {lamp_html}
            </div>
        </div>

        <div class='card'>
            <div class='hd'>
                <div class='title'>Trace</div>
                <div class='muted mono'>Download MF4 / ZIP</div>
            </div>
            <div class='bd'>
                <div class='muted'>Trace table is intentionally omitted from this HTML. Use the MF4 (or TRACE &amp; CTX ZIP) to inspect the trace.</div>
            </div>
        </div>
    </div>
</body>
</html>"""


def json_dumps_safe(obj: Any) -> str:
    try:
        import json
        return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return str(obj)


def choose_primary_dtc(dtcs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick a primary DTC from parsed scan DTCs.

    Currently: prefer ACTIVE DTCs, else first one.
    """
    if not dtcs:
        return None

    def _score(d: Dict[str, Any]) -> int:
        code = str(d.get('code') or '').strip()
        status_desc = str(d.get('status_desc') or '').lower()
        desc = str(d.get('desc') or d.get('desc_report') or '').strip()
        active = bool(d.get('active'))
        score = 0
        if active:
            score += 100
        if re.match(r'^[PCBU][0-9A-F]{4,6}$', code, flags=re.IGNORECASE):
            score += 60
        if code.upper() in {'UDS19', 'UDS', 'TIMEOUT'}:
            score -= 80
        if 'timeout' in status_desc or 'no response' in status_desc:
            score -= 120
        if desc:
            score += 10
        if isinstance(d.get('timestamp_epoch_ms'), int):
            score += 5
        if isinstance(d.get('odometer_km'), int):
            score += 5
        return score

    best = None
    best_s = -10**9
    for d in dtcs:
        if not isinstance(d, dict):
            continue
        s = _score(d)
        if s > best_s:
            best_s = s
            best = d
    return best


class ExperimentalAssistantService:
    """Monitors MIL (OBD) and generates incident reports on MIL ON events.

    Designed as an opt-in service controlled by config_store.
    """

    def __init__(
        self,
        *,
        bus_manager,
        config_store,
        scanner_service,
        log_dir_resolver,
        scan_report_dir_resolver=None,
        ethernet_manager=None,
    ):
        self.bus_manager = bus_manager
        self.config_store = config_store
        self.scanner_service = scanner_service
        self.log_dir_resolver = log_dir_resolver
        self.scan_report_dir_resolver = scan_report_dir_resolver or log_dir_resolver
        self.ethernet_manager = ethernet_manager
        self._eth_listener_installed = False

        # Need at least 30s retention for the required -15s/+15s excerpt.
        self.trace = TraceRingBuffer(keep_ms=45000)
        self._trace_listener_installed = False

        self._lock = threading.Lock()
        self._enabled = False
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._mil_prev: Optional[bool] = None
        self._last_mil_on_ts_ms: Optional[int] = None
        self._last_incident: Optional[Dict[str, Any]] = None
        self._incidents: List[Dict[str, Any]] = []

        # Pipeline/progress status for UI.
        # Updated by MIL/lamp incident handlers and (optionally) the LLM analysis loop.
        self._pipeline: Dict[str, Any] = {
            'phase': 'idle',
            'active': False,
            'updated_ts_ms': _now_ms(),
        }

        # Lamp sentinels derived from decoded CAN signals (EPC + gearbox/cambio).
        # Updated in the trace listener from live frames.
        self._lamp_prev: Dict[str, Optional[bool]] = {'epc': None, 'gearbox': None}
        self._lamp_last_val: Dict[str, Any] = {'epc': None, 'gearbox': None}
        self._lamp_last_num: Dict[str, Optional[float]] = {'epc': None, 'gearbox': None}
        self._lamp_last_ts_ms: Dict[str, Optional[int]] = {'epc': None, 'gearbox': None}
        self._lamp_last_channel: Dict[str, Optional[int]] = {'epc': None, 'gearbox': None}
        self._last_lamp_incident_ts_ms: Dict[str, int] = {'epc': 0, 'gearbox': 0}

        # Scan throttling + retention / breaker state (robust long-run behavior)
        self._last_scan_started_ts_ms: int = 0
        self._scan_rate_limited_count: int = 0

        self._sentinel_breaker_lock = threading.Lock()
        self._sentinel_breaker: Dict[str, Any] = {
            'failures': 0,
            'total_failures': 0,
            'cooldown_until_ts_ms': 0,
            'last_error': '',
            'last_error_ts_ms': 0,
            'last_success_ts_ms': 0,
        }

        self._logs_retention_lock = threading.Lock()
        self._logs_retention_last: Dict[str, Any] = {
            'ts_ms': 0,
            'deleted_files': 0,
            'deleted_bytes': 0,
            'remaining_bytes': None,
            'reason': '',
        }

        # Persistent DoIP connection for MIL polling (avoids reconnecting every cycle).
        self._doip_mil_scanner: Any = None  # DoIPGatewayScanner instance or None
        self._doip_mil_lock = threading.Lock()
        self._doip_mil_last_ok_ts_ms: int = 0
        self._doip_mil_consecutive_errors: int = 0
        self._doip_mil_paused: bool = False  # set True while Live Data DoIP holds the gateway
        self._doip_mil_paused_at: float = 0  # timestamp of pause; 0 = not paused

        # MIL polling state (exposed in /api/experimental/status for UI).
        self._mil_last_poll_ts_ms: int = 0
        self._mil_transport: str = ''  # 'can', 'doip', or ''

        self._load_persistence()

    def _sentinel_breaker_status(self) -> Dict[str, Any]:
        with self._sentinel_breaker_lock:
            return dict(self._sentinel_breaker or {})

    def _sentinel_breaker_should_skip(self, *, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        if not bool(cfg.get('sentinel_llm_breaker_enabled', True)):
            return (False, '')
        now = _now_ms()
        with self._sentinel_breaker_lock:
            st = dict(self._sentinel_breaker or {})

        try:
            decay_s = float(cfg.get('sentinel_llm_breaker_decay_s', 1800.0) or 1800.0)
        except Exception:
            decay_s = 1800.0
        decay_ms = int(max(0.0, min(decay_s, 24 * 3600.0)) * 1000.0)

        # Auto-decay failures after quiet time.
        try:
            last_err_ts = int(st.get('last_error_ts_ms') or 0)
        except Exception:
            last_err_ts = 0
        if decay_ms > 0 and last_err_ts and (now - last_err_ts) >= decay_ms:
            with self._sentinel_breaker_lock:
                self._sentinel_breaker['failures'] = 0

        with self._sentinel_breaker_lock:
            cd_until = int(self._sentinel_breaker.get('cooldown_until_ts_ms') or 0)
        if cd_until and now < cd_until:
            return (True, 'breaker_open')
        return (False, '')

    def _sentinel_breaker_note_success(self) -> None:
        now = _now_ms()
        with self._sentinel_breaker_lock:
            self._sentinel_breaker['failures'] = 0
            self._sentinel_breaker['cooldown_until_ts_ms'] = 0
            self._sentinel_breaker['last_success_ts_ms'] = int(now)

    def _sentinel_breaker_note_failure(self, *, cfg: Dict[str, Any], error: str) -> None:
        if not bool(cfg.get('sentinel_llm_breaker_enabled', True)):
            return
        now = _now_ms()
        try:
            threshold = int(cfg.get('sentinel_llm_breaker_failures', 3) or 3)
        except Exception:
            threshold = 3
        threshold = int(max(1, min(threshold, 20)))
        try:
            cooldown_s = float(cfg.get('sentinel_llm_breaker_cooldown_s', 900.0) or 900.0)
        except Exception:
            cooldown_s = 900.0
        cooldown_ms = int(max(1.0, min(cooldown_s, 24 * 3600.0)) * 1000.0)

        with self._sentinel_breaker_lock:
            self._sentinel_breaker['failures'] = int(self._sentinel_breaker.get('failures') or 0) + 1
            self._sentinel_breaker['total_failures'] = int(self._sentinel_breaker.get('total_failures') or 0) + 1
            self._sentinel_breaker['last_error'] = str(error or '')[:500]
            self._sentinel_breaker['last_error_ts_ms'] = int(now)
            failures = int(self._sentinel_breaker.get('failures') or 0)
            if failures >= threshold:
                self._sentinel_breaker['cooldown_until_ts_ms'] = int(now + cooldown_ms)

    def _logs_dir(self) -> str:
        try:
            d = str(self.log_dir_resolver() or '').strip()
        except Exception:
            d = ''
        return d if d and os.path.isdir(d) else ''

    def _enforce_logs_retention(self, *, reason: str = '') -> Dict[str, Any]:
        """Best-effort logs cleanup to avoid disk-full over long drives.

        Strategy:
        - Delete files older than max_age_days.
        - Enforce max_total_mb by deleting oldest files, preferring heavy artifacts first.
        """
        cfg = self._get_cfg()
        if not bool(cfg.get('logs_retention_enabled', True)):
            return {'enabled': False}

        try:
            min_interval_s = float(cfg.get('logs_retention_min_interval_s', 120.0) or 120.0)
        except Exception:
            min_interval_s = 120.0
        min_interval_ms = int(max(0.0, min(min_interval_s, 3600.0)) * 1000.0)

        now = _now_ms()
        with self._logs_retention_lock:
            last_ts = int(self._logs_retention_last.get('ts_ms') or 0)
            if last_ts and min_interval_ms and (now - last_ts) < min_interval_ms:
                return dict(self._logs_retention_last or {})

        log_dir = self._logs_dir()
        if not log_dir:
            return {'enabled': True, 'ok': False, 'error': 'missing log dir'}

        try:
            max_age_days = float(cfg.get('logs_retention_max_age_days', 14) or 14)
        except Exception:
            max_age_days = 14.0
        max_age_days = float(max(0.0, min(max_age_days, 365.0)))
        cutoff_s = time.time() - (max_age_days * 86400.0) if max_age_days > 0 else None

        try:
            max_total_mb = float(cfg.get('logs_retention_max_total_mb', 4096) or 4096)
        except Exception:
            max_total_mb = 4096.0
        max_total_bytes = int(max(50.0, min(max_total_mb, 1024.0 * 1024.0)) * 1024.0 * 1024.0)

        # Avoid touching files that were just written.
        grace_s = 30.0

        keep_names = {'sentinel_incidents.json'}
        heavy_exts = {'.mf4', '.zip', '.pcap', '.pcapng'}

        entries: List[Tuple[str, float, int]] = []
        total_bytes = 0
        try:
            for de in os.scandir(log_dir):
                try:
                    if not de.is_file(follow_symlinks=False):
                        continue
                    name = de.name
                    if name in keep_names:
                        continue
                    st = de.stat(follow_symlinks=False)
                    m = float(st.st_mtime)
                    sz = int(st.st_size)
                    total_bytes += max(0, sz)
                    entries.append((de.path, m, sz))
                except Exception:
                    continue
        except Exception:
            entries = []

        deleted_files = 0
        deleted_bytes = 0

        def _try_unlink(p: str, sz: int) -> bool:
            nonlocal deleted_files, deleted_bytes
            try:
                st = os.stat(p)
                if (time.time() - float(st.st_mtime)) < grace_s:
                    return False
            except Exception:
                pass
            try:
                os.unlink(p)
                deleted_files += 1
                deleted_bytes += int(max(0, sz))
                return True
            except Exception:
                return False

        # 1) Age-based deletion
        if cutoff_s is not None:
            for p, m, sz in sorted(entries, key=lambda x: x[1]):
                try:
                    if m < cutoff_s:
                        if _try_unlink(p, sz):
                            total_bytes -= int(max(0, sz))
                except Exception:
                    continue

        # 2) Quota-based deletion
        if total_bytes > max_total_bytes:
            def _prio(p: str) -> int:
                try:
                    ext = os.path.splitext(p)[1].lower()
                except Exception:
                    ext = ''
                if ext in heavy_exts:
                    return 0
                if ext in {'.txt', '.log'}:
                    return 1
                if ext in {'.html', '.json'}:
                    return 2
                return 1

            for p, m, sz in sorted(entries, key=lambda x: (_prio(x[0]), x[1])):
                if total_bytes <= max_total_bytes:
                    break
                # Skip if already deleted by age step
                if not os.path.exists(p):
                    continue
                if _try_unlink(p, sz):
                    total_bytes -= int(max(0, sz))

        out = {
            'ts_ms': int(now),
            'enabled': True,
            'reason': str(reason or ''),
            'max_age_days': float(max_age_days),
            'max_total_mb': float(max_total_mb),
            'deleted_files': int(deleted_files),
            'deleted_bytes': int(deleted_bytes),
            'remaining_bytes': int(max(0, total_bytes)),
        }
        with self._logs_retention_lock:
            self._logs_retention_last = dict(out)
        return out

    def _pipeline_update(self, **patch: Any) -> None:
        now = _now_ms()
        with self._lock:
            cur = self._pipeline if isinstance(self._pipeline, dict) else {}
            cur.update({k: v for k, v in patch.items()})
            cur['updated_ts_ms'] = int(now)
            self._pipeline = cur

    def _pipeline_set_phase(self, phase: str, *, message: str = '', **extra: Any) -> None:
        payload: Dict[str, Any] = {'phase': str(phase or '').strip() or 'unknown'}
        if message:
            payload['message'] = str(message)
        payload.update(extra)
        self._pipeline_update(**payload)

    def _incidents_db_path(self) -> str:
        try:
            d = self.log_dir_resolver()
            if d:
                return os.path.join(d, 'sentinel_incidents.json')
        except Exception:
            pass
        return 'sentinel_incidents.json'

    def _load_persistence(self) -> None:
        p = self._incidents_db_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, 'r', encoding='utf-8') as f:
                import json
                data = json.load(f)
                if isinstance(data, list):
                     with self._lock:
                        self._incidents = data
        except Exception:
            pass

    def _save_persistence(self) -> None:
        p = self._incidents_db_path()
        try:
            with self._lock:
                data = list(self._incidents)
            with open(p, 'w', encoding='utf-8') as f:
                import json
                json.dump(data, f, indent=2)
        except Exception:
            pass
    
    def clear_incidents(self) -> None:
        with self._lock:
            self._incidents = []
        self._save_persistence()

    def apply_watchlist_from_settings(self) -> None:
        cfg = self._get_cfg()
        lamps = cfg.get('lamp_mappings') if isinstance(cfg, dict) else None
        watch = []
        if isinstance(lamps, dict):
            for k in ['epc', 'gearbox']:
                m = lamps.get(k)
                if isinstance(m, dict):
                    msg = str(m.get('message') or '').strip()
                    if msg:
                        watch.append(msg)

                        # FlexRay: if the mapping includes a slot-id, also watch the
                        # current representative decoded name used at runtime.
                        try:
                            slot = _parse_slot_id(msg)
                            if slot is not None:
                                fib = getattr(self.bus_manager, 'fibex', None)
                                frames = getattr(fib, 'frames', None)
                                if isinstance(frames, dict):
                                    alt = str(frames.get(int(slot)) or '').strip()
                                    if alt:
                                        watch.append(alt)
                        except Exception:
                            pass
        try:
            self.trace.set_watch_messages(watch)
        except Exception:
            pass

    def _get_cfg(self) -> Dict[str, Any]:
        try:
            cfg = self.config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        obj = cfg.get('experimental_assistant') if isinstance(cfg, dict) else None
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _filter_dtcs_by_status(
        dtcs: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Keep only DTCs whose UDS status byte has bit 3 (0x08 = confirmed)
        or bit 7 (0x80 = warningIndicatorRequested / MIL) set.

        Returns (filtered_list, info_dict).
        """
        _RELEVANT_MASK = 0x88  # 0x08 | 0x80
        kept: List[Dict[str, Any]] = []
        skipped = 0
        for d in dtcs or []:
            if not isinstance(d, dict):
                continue
            sb_raw = str(d.get('status_byte') or '').strip()
            try:
                sb_val = int(sb_raw, 0)  # supports '0x09', '0x80', '9', etc.
            except (ValueError, TypeError):
                sb_val = -1
            if sb_val >= 0 and (sb_val & _RELEVANT_MASK):
                kept.append(d)
            else:
                skipped += 1
        return kept, {
            'status_filter_mask': hex(_RELEVANT_MASK),
            'status_kept': len(kept),
            'status_skipped': skipped,
            'status_total': len(dtcs or []),
        }

    def _filter_dtcs_for_analysis(
        self,
        *,
        dtcs: List[Dict[str, Any]],
        event_ts_ms: int,
        cfg: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Filter DTCs for Sentinel analysis.

        Always applied:
          - Status byte filter: keep only DTCs with bit 3 (0x08) or bit 7 (0x80) set.

        Optionally (sentinel_dtc_time_filter_enabled):
          - Timestamp proximity: keep DTCs within [event_ts + delay, event_ts + delay + window].

        If no DTCs match after all filters, falls back to the status-filtered list.
        """
        # ── Step 1: always filter by status byte ──
        status_filtered, status_info = self._filter_dtcs_by_status(dtcs)
        # If nothing passes the status filter, fall back to all DTCs so we
        # don't lose visibility entirely.
        if not status_filtered:
            status_filtered = list(dtcs or [])
            status_info['status_note'] = 'no dtcs matched status filter; using unfiltered list'

        info: Dict[str, Any] = {
            **status_info,
            'enabled': bool(cfg.get('sentinel_dtc_time_filter_enabled', False)),
            'event_ts_ms': int(event_ts_ms),
        }
        if not info['enabled']:
            return status_filtered, info

        try:
            delay_s = float(cfg.get('sentinel_dtc_time_delay_s', 0.0) or 0.0)
        except Exception:
            delay_s = 0.0
        try:
            window_s = float(cfg.get('sentinel_dtc_time_window_s', 300.0) or 300.0)
        except Exception:
            window_s = 300.0

        delay_ms = int(max(0.0, min(delay_s, 3600.0)) * 1000.0)
        window_ms = int(max(0.0, min(window_s, 24 * 3600.0)) * 1000.0)
        start_ms = int(event_ts_ms + delay_ms)
        end_ms = int(start_ms + window_ms)

        selected: List[Dict[str, Any]] = []
        missing_ts = 0
        for d in status_filtered:
            if not isinstance(d, dict):
                continue
            ts = d.get('timestamp_epoch_ms')
            if not isinstance(ts, int) or ts <= 0:
                missing_ts += 1
                continue
            if start_ms <= int(ts) <= end_ms:
                selected.append(d)

        info.update({
            'delay_s': float(delay_s),
            'window_s': float(window_s),
            'window_start_ts_ms': int(start_ms),
            'window_end_ts_ms': int(end_ms),
            'selected_count': int(len(selected)),
            'missing_ts_count': int(missing_ts),
            'total_count': int(len(status_filtered)),
        })

        # Avoid accidentally filtering everything out.
        if selected:
            return selected, info
        return status_filtered, {**info, 'note': 'no dtcs matched window; using status-filtered list'}

    def status(self) -> Dict[str, Any]:
        now = _now_ms()
        try:
            cfg = self._get_cfg()
        except Exception:
            cfg = {}
        try:
            stale_ms = int(cfg.get('lamp_stale_ms', 2000) or 2000)
        except Exception:
            stale_ms = 2000
        stale_ms = int(max(200, min(stale_ms, 60000)))

        def _lamp_payload(key: str) -> Dict[str, Any]:
            last_ts = self._lamp_last_ts_ms.get(key)
            age_ms = None
            is_stale = False
            try:
                if isinstance(last_ts, int) and last_ts > 0:
                    age_ms = int(now) - int(last_ts)
                    is_stale = bool(age_ms >= stale_ms)
            except Exception:
                age_ms = None
                is_stale = False

            return {
                'on': (None if is_stale else self._lamp_prev.get(key)),
                'last_ts_ms': last_ts,
                'age_ms': age_ms,
                'stale': bool(is_stale),
                'stale_ms': int(stale_ms),
                'last_value': _json_sanitize(self._lamp_last_val.get(key)),
                'last_value_num': self._lamp_last_num.get(key),
                'last_channel': self._lamp_last_channel.get(key),
            }

        # Compute effective poll interval (DoIP clamps to 3000 ms min)
        try:
            _raw_poll = int(cfg.get('mil_poll_interval_ms', 800) or 800)
            _eff_poll = max(_raw_poll, 3000) if self._mil_transport == 'doip' else _raw_poll
        except Exception:
            _eff_poll = 800

        with self._lock:
            return {
                'enabled': bool(self._enabled),
                'running': bool(self._thread and self._thread.is_alive()),
                'mil_prev': self._mil_prev,
                'mil_last_poll_ts_ms': int(self._mil_last_poll_ts_ms or 0),
                'mil_transport': str(self._mil_transport or ''),
                'mil_poll_effective_ms': int(_eff_poll),
                'last_mil_on_ts_ms': self._last_mil_on_ts_ms,
                'last_incident': _json_sanitize(self._last_incident),
                'incidents_count': len(self._incidents),
                'trace': self.trace.status(),
                'pipeline': _json_sanitize(self._pipeline),
                'scan': {
                    'last_scan_started_ts_ms': int(self._last_scan_started_ts_ms or 0),
                    'rate_limited_count': int(self._scan_rate_limited_count or 0),
                },
                'sentinel_breaker': _json_sanitize(self._sentinel_breaker_status()),
                'logs_retention': _json_sanitize(dict(self._logs_retention_last or {})),
                'lamps': {
                    'epc': _lamp_payload('epc'),
                    'gearbox': _lamp_payload('gearbox'),
                },
            }

    def _get_lamp_mappings(self) -> Dict[str, Dict[str, str]]:
        cfg = self._get_cfg()
        lamps = cfg.get('lamp_mappings') if isinstance(cfg, dict) else None
        out: Dict[str, Dict[str, str]] = {}
        if not isinstance(lamps, dict):
            return out
        for key in ['epc', 'gearbox']:
            m = lamps.get(key)
            if not isinstance(m, dict):
                continue
            msg = str(m.get('message') or '').strip()
            sig = str(m.get('signal') or '').strip()
            if msg and sig:
                out[key] = {'message': msg, 'signal': sig}
        return out

    def _maybe_trigger_lamp_incident(self, *, key: str, ts_ms: int, channel: Optional[int]) -> None:
        cfg = self._get_cfg()
        if not bool(cfg.get('enabled', False)):
            return

        try:
            debounce_ms = int(cfg.get('lamp_debounce_ms', 250) or 250)
        except Exception:
            debounce_ms = 250
        try:
            rate_limit_s = int(cfg.get('lamp_rate_limit_s', 60) or 60)
        except Exception:
            rate_limit_s = 60

        with self._lock:
            last_ts = int(self._last_lamp_incident_ts_ms.get(key, 0) or 0)
            if last_ts and (int(ts_ms) - last_ts) < int(rate_limit_s) * 1000:
                return
            # Optimistically reserve this slot to avoid multiple triggers.
            self._last_lamp_incident_ts_ms[key] = int(ts_ms)

        def _run() -> None:
            # Debounce: ensure still ON after debounce window.
            try:
                time.sleep(max(0.05, float(debounce_ms) / 1000.0))
            except Exception:
                pass
            with self._lock:
                cur = self._lamp_prev.get(key)
            if cur is not True:
                return
            try:
                self._handle_lamp_incident(kind=key, lamp_on_ts_ms=int(ts_ms), channel=channel)
            except Exception:
                return

        threading.Thread(target=_run, daemon=True).start()

    def _update_lamps_from_frame(self, frame: Dict[str, Any]) -> None:
        try:
            cfg = self._get_cfg()
            if not bool(cfg.get('enabled', False)):
                return
        except Exception:
            return

        decoded = frame.get('decoded') if isinstance(frame, dict) else None
        if not isinstance(decoded, dict):
            return
        name = str(decoded.get('name') or '').strip()
        sigs = decoded.get('signals')
        if not name or not isinstance(sigs, dict):
            return

        mappings = self._get_lamp_mappings()
        if not mappings:
            return

        try:
            ts_ms = int(frame.get('timestamp') or 0)
        except Exception:
            ts_ms = 0
        if ts_ms <= 0:
            ts_ms = _now_ms()
        try:
            ch = int(frame.get('channel') or 0)
        except Exception:
            ch = 0

        for key, m in mappings.items():
            if not isinstance(m, dict):
                continue
            if not _messages_match(name, str(m.get('message') or '').strip()):
                continue
            sig = str(m.get('signal') or '').strip()
            if not sig:
                continue
            if sig not in sigs:
                continue
            raw_val = sigs.get(sig)
            on = _boolish(raw_val)
            safe_val = _json_safe_value(raw_val)
            num_val = _to_number_or_none(raw_val)

            trigger = False
            with self._lock:
                prev = self._lamp_prev.get(key)
                self._lamp_prev[key] = bool(on)
                self._lamp_last_val[key] = safe_val
                self._lamp_last_num[key] = num_val
                self._lamp_last_ts_ms[key] = int(ts_ms)
                self._lamp_last_channel[key] = int(ch)
                if prev is False and on is True:
                    trigger = True

            if trigger:
                self._maybe_trigger_lamp_incident(key=key, ts_ms=int(ts_ms), channel=int(ch))

    def list_incidents(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._incidents[-int(max(1, limit)):])

    def enable(self) -> None:
        with self._lock:
            self._enabled = True
        self._ensure_trace_listener()
        self.apply_watchlist_from_settings()
        self._ensure_thread()

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
        self._stop_evt.set()

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name='ExperimentalAssistant', daemon=True)
        self._thread.start()

    def _ensure_trace_listener(self) -> None:
        if not self._trace_listener_installed:
            def _on_frame(frame: Dict[str, Any]):
                try:
                    self._update_lamps_from_frame(frame)
                    self.trace.add(frame)
                except Exception:
                    pass

            try:
                self.bus_manager.add_listener(_on_frame)
                self._trace_listener_installed = True
            except Exception:
                self._trace_listener_installed = False

        if self.ethernet_manager and not self._eth_listener_installed:
            def _on_eth(data: Dict[str, Any]):
                try:
                    ts_s = float(data.get('timestamp') or 0.0)
                    frame = {
                        'timestamp': int(ts_s * 1000.0),
                        'type': 'ETH',
                        'channel': 0,
                        'id': 0,
                        'data': [],
                        'payload_hex': data.get('payload_hex'),
                        'decoded': {'name': data.get('summary') or 'ETH'}
                    }
                    self.trace.add(frame)
                except Exception:
                    pass
            
            try:
                if hasattr(self.ethernet_manager, 'add_listener'):
                    self.ethernet_manager.add_listener(_on_eth)
                    self._eth_listener_installed = True
            except Exception:
                self._eth_listener_installed = False

    def _get_full_cfg(self) -> Dict[str, Any]:
        """Return the full app config (not just experimental_assistant section)."""
        try:
            cfg = self.config_store.get_config_only() or {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _resolve_doip_gateway_ip(self) -> str:
        """Resolve DoIP gateway IP from config or auto-discovery."""
        full_cfg = self._get_full_cfg()
        # 1. Try eth_settings.target_ip
        es = full_cfg.get('eth_settings') if isinstance(full_cfg.get('eth_settings'), dict) else {}
        gw = str(es.get('target_ip') or '').strip()
        if gw:
            return gw
        # 2. Try gateway_mirror.gateway_ip
        gm = full_cfg.get('gateway_mirror') if isinstance(full_cfg.get('gateway_mirror'), dict) else {}
        gw = str(gm.get('gateway_ip') or '').strip()
        if gw:
            return gw
        # 3. Auto-discover
        iface = str(es.get('interface') or '').strip() or 'eth0'
        try:
            from vag_scanner import discover_doip_gateway_ip
            gw = discover_doip_gateway_ip(iface=iface, timeout_s=1.5)
        except Exception:
            gw = None
        return str(gw or '').strip()

    def pause_doip_mil(self) -> None:
        """Pause DoIP MIL polling and release the gateway connection.

        Call this before opening a second DoIP session (e.g. Live Data)
        to avoid tester-address conflicts on the gateway.
        """
        self._doip_mil_paused = True
        self._doip_mil_paused_at = time.time()
        self._close_doip_mil_scanner()

    def resume_doip_mil(self) -> None:
        """Resume DoIP MIL polling (connection will be re-established on next cycle)."""
        self._doip_mil_paused = False
        self._doip_mil_paused_at = 0

    def _close_doip_mil_scanner(self) -> None:
        """Close persistent DoIP scanner for MIL polling."""
        with self._doip_mil_lock:
            sc = self._doip_mil_scanner
            self._doip_mil_scanner = None
        if sc:
            try:
                sc.close()
            except Exception:
                pass

    def _poll_mil_doip(self, timeout_s: float = 1.5) -> Optional[bool]:
        """Read MIL status via DoIP UDS ReadDTCInformation.

        Uses ReadDTCInformation (0x19 0x02 0x80) to check if any ECU
        has a DTC with warningIndicatorRequested (MIL) bit set.

        IMPORTANT: the candidate ECU list MUST mirror the one used by
        vag_scanner._run_live_doip (Live Data path) — otherwise the
        Sentinel queries only the gateway (0x4010), which on real VAG
        vehicles does not aggregate the engine MIL bit, while Live Data
        polls the actual powertrain ECU (typically 0x407B/0x4044) and
        sees the lamp ON.  Aggregating across all responsive ECUs makes
        the two views consistent.

        Returns: True=MIL ON, False=MIL OFF, None=no communication.
        """
        if self._doip_mil_paused:
            # Watchdog: auto-unpause after 120 s in case resume was never called
            paused_at = getattr(self, '_doip_mil_paused_at', 0)
            if paused_at and (time.time() - paused_at) > 120:
                self._doip_mil_paused = False
                self._doip_mil_paused_at = 0
            else:
                return None  # Live Data holds the gateway — skip
        try:
            from vag_scanner import DoIPGatewayScanner
        except Exception:
            return None

        full_cfg = self._get_full_cfg()
        es = full_cfg.get('eth_settings') if isinstance(full_cfg.get('eth_settings'), dict) else {}
        try:
            tester_addr = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_addr = 0x0E00

        # Engine / OBD-compliant ECU candidates.  Same priority order as
        # vag_scanner._run_live_doip so the Sentinel and Live Data agree
        # on which ECU owns the MIL.  Battery/energy ECUs first, then
        # powertrain, then standard OBD addresses.
        engine_targets = [
            0x407B, 0x4044, 0x4437, 0x40B7, 0x40B8,
            0x4010, 0x4076, 0x4078, 0x407C,
            0x4012, 0x400B, 0x0001, 0x0010,
        ]

        with self._doip_mil_lock:
            sc = self._doip_mil_scanner

        # Create or reconnect scanner if needed
        gateway_ip = ''
        if sc is None:
            gateway_ip = self._resolve_doip_gateway_ip()
            if not gateway_ip:
                return None
            try:
                sc = DoIPGatewayScanner(gateway_ip, tester_logical_address=tester_addr)
                sc.debug_doip = False  # keep quiet in polling
                sc._connect_with_recovery()
                sc._routing_activation()
                with self._doip_mil_lock:
                    self._doip_mil_scanner = sc
            except Exception:
                try:
                    if sc:
                        sc.close()
                except Exception:
                    pass
                with self._doip_mil_lock:
                    self._doip_mil_scanner = None
                    self._doip_mil_consecutive_errors += 1
                return None

        # Send ReadDTCInformation subfunction 0x02 (reportDTCByStatusMask)
        # with mask 0x80 (warningIndicatorRequested = MIL).
        # MUST query all targets and aggregate: MIL is ON if *any* ECU
        # reports DTCs with the MIL status bit set.  Returning at the
        # first responder would match the gateway (which usually has 0
        # MIL DTCs) and falsely report OFF while the engine ECU has it
        # set.
        try:
            any_positive = False
            mil_on = False
            for target_addr in engine_targets:
                try:
                    resp = sc._uds_transact(target_addr, b'\x19\x02\x80', timeout_s=float(timeout_s))
                except Exception:
                    continue
                if resp is None:
                    continue  # ECU didn't respond, try next
                # Negative response
                if len(resp) >= 1 and resp[0] == 0x7F:
                    continue
                # Positive response: 0x59 0x02 <availability_mask> [<dtc_high> <dtc_mid> <dtc_low> <status>]*
                if len(resp) >= 3 and resp[0] == 0x59 and resp[1] == 0x02:
                    any_positive = True
                    dtc_data = resp[3:]  # skip 0x59 + sub + availability_mask
                    # Each DTC record is 4 bytes (3 DTC + 1 status)
                    num_dtcs = len(dtc_data) // 4
                    if num_dtcs > 0:
                        mil_on = True
                        break  # MIL confirmed ON; no need to keep probing

            if any_positive:
                with self._doip_mil_lock:
                    self._doip_mil_last_ok_ts_ms = _now_ms()
                    self._doip_mil_consecutive_errors = 0
                self._mil_last_poll_ts_ms = _now_ms()
                return bool(mil_on)
            # No target responded positively → no usable info
            return None
        except Exception:
            # Connection lost — teardown for next cycle reconnect
            self._close_doip_mil_scanner()
            with self._doip_mil_lock:
                self._doip_mil_consecutive_errors += 1
            return None

    def _poll_mil_once(self, channel_id: int, timeout_s: float = 0.25) -> Optional[bool]:
        """Read MIL status via OBD Mode 01 PID 01 over CAN (best-effort)."""
        try:
            from vag_scanner import VAGScanner
        except Exception:
            return None
        try:
            sc = VAGScanner(self.bus_manager, int(channel_id), emit_log=None, enable_file_log=False)
            try:
                _rpm, _speed, mil_on, _cnt = sc.read_live_once(timeout_s=float(timeout_s))
            finally:
                try:
                    sc.close()
                except Exception:
                    pass
            if mil_on is None:
                return None
            self._mil_last_poll_ts_ms = _now_ms()
            return bool(mil_on)
        except Exception:
            return None

    def _detect_mil_transport(self, cfg: Dict[str, Any]) -> str:
        """Decide which transport to use for MIL polling.

        Returns 'doip', 'can', or '' (unknown).
        Config field: diagnostic_transport = 'can' | 'doip' | 'auto' | 'auto_prefer_can'
        """
        transport = str(cfg.get('diagnostic_transport') or 'doip').strip().lower()
        if transport == 'can':
            return 'can'
        if transport == 'doip':
            return 'doip'

        # Auto-detect: check if DoIP gateway is reachable / configured.
        full_cfg = self._get_full_cfg()
        es = full_cfg.get('eth_settings') if isinstance(full_cfg.get('eth_settings'), dict) else {}
        doip_enabled = bool(es.get('doip_enabled', False))
        has_gateway = bool(str(es.get('target_ip') or '').strip())
        gm = full_cfg.get('gateway_mirror') if isinstance(full_cfg.get('gateway_mirror'), dict) else {}
        has_gm_gw = bool(str(gm.get('gateway_ip') or '').strip())

        if transport == 'auto_prefer_can':
            # Try CAN first; fall back to DoIP if CAN hardware is absent.
            try:
                ch_count = 0
                if self.bus_manager and hasattr(self.bus_manager, 'channels'):
                    ch_count = len(self.bus_manager.channels or [])
            except Exception:
                ch_count = 0
            if ch_count > 0:
                return 'can'
            # No CAN channels — use DoIP if available
            if doip_enabled or has_gateway or has_gm_gw:
                return 'doip'
            return 'can'  # last resort

        # 'auto' — prefer DoIP
        if doip_enabled or has_gateway or has_gm_gw:
            return 'doip'
        return 'can'

    def _poll_mil(self, transport: str, channel_id: int, timeout_s: float) -> Optional[bool]:
        """Unified MIL poll dispatcher."""
        if transport == 'doip':
            return self._poll_mil_doip(timeout_s=timeout_s)
        return self._poll_mil_once(channel_id, timeout_s=timeout_s)

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                cfg = self._get_cfg()
                enabled = bool(cfg.get('enabled', False))
                if not enabled:
                    time.sleep(0.5)
                    continue

                channel_id = int(cfg.get('mil_channel_id', 0) or 0)
                poll_ms = int(cfg.get('mil_poll_interval_ms', 800) or 800)
                debounce_ms = int(cfg.get('mil_debounce_ms', 1200) or 1200)
                rate_limit_s = int(cfg.get('scan_rate_limit_s', 600) or 600)

                transport = self._detect_mil_transport(cfg)
                self._mil_transport = transport
                timeout_s = float(cfg.get('mil_timeout_s', 0.25) or 0.25)
                if transport == 'doip':
                    # DoIP needs more time per transaction
                    timeout_s = max(timeout_s, 1.5)
                    poll_ms = max(poll_ms, 3000)  # avoid hammering gateway

                mil = self._poll_mil(transport, channel_id, timeout_s)
                prev = self._mil_prev

                # ── [TRC Fix 2026-02-17] When poll returns None (comm error /
                #    paused), do NOT update _mil_prev.  This prevents false
                #    MIL transitions caused by None→False→True sequences after
                #    transient DoIP connection losses. ──
                if mil is None:
                    time.sleep(max(0.05, float(poll_ms) / 1000.0))
                    continue
                self._mil_prev = mil

                if prev is False and mil is True:
                    t0 = _now_ms()
                    self._last_mil_on_ts_ms = int(t0)
                    # Debounce: verify still ON after debounce window.
                    time.sleep(max(0.05, float(debounce_ms) / 1000.0))
                    mil2 = self._poll_mil(transport, channel_id, timeout_s=max(timeout_s, 0.35))
                    if mil2 is not True:
                        # false trigger
                        time.sleep(float(poll_ms) / 1000.0)
                        continue

                    # Rate limit incident generation
                    with self._lock:
                        last = self._last_incident or {}
                        last_ts = int(last.get('mil_on_ts_ms') or 0) if isinstance(last, dict) else 0
                    if last_ts and (t0 - last_ts) < int(rate_limit_s) * 1000:
                        time.sleep(float(poll_ms) / 1000.0)
                        continue

                    threading.Thread(
                        target=self._handle_mil_incident,
                        args=(channel_id, t0),
                        daemon=True,
                    ).start()

                time.sleep(max(0.05, float(poll_ms) / 1000.0))
            except Exception:
                time.sleep(0.5)

    def _handle_mil_incident(self, channel_id: int, mil_on_ts_ms: int) -> None:
        """Wait for post window, run scan, parse DTCs, and write incident HTML."""
        cfg = self._get_cfg()

        incident_id = f"mil_{mil_on_ts_ms:x}"
        self._pipeline_update(
            active=True,
            kind='mil',
            incident_id=str(incident_id),
            trigger_ts_ms=int(mil_on_ts_ms),
            phase='triggered',
            message='MIL ON detected',
            percent=5,
        )

        # Wait until +15s window has passed.
        post_s = float(cfg.get('trace_post_s', 15.0) or 15.0)
        pre_s = float(cfg.get('trace_pre_s', 15.0) or 15.0)
        end_ts_ms = int(mil_on_ts_ms + int(post_s * 1000))
        self._pipeline_set_phase('waiting_post_window', message='Waiting post window', percent=10, window_post_s=float(post_s))
        while _now_ms() < end_ts_ms and not self._stop_evt.is_set():
            time.sleep(0.2)

        scan_started = _now_ms()
        scan_report_filename = ''
        scan_finished = scan_started

        # Prefer DoIP scan report for incidents (fallback to CAN scan report).
        scan_action = ''

        try:
            self._pipeline_set_phase('scanning', message='Running scan report', percent=20)
            scan_action, scan_report_filename, scan_started, scan_finished = self._run_scan_report(int(channel_id))
        except Exception:
            scan_finished = _now_ms()

        # Extract trace frames
        trace_start = int(mil_on_ts_ms - int(pre_s * 1000))
        trace_end = int(mil_on_ts_ms + int(post_s * 1000))
        trace_frames = self.trace.slice(
            trace_start,
            trace_end,
            channel=self._incident_trace_channel(channel_id),
        )

        # Export trace window as MF4 excerpt (downloadable via /api/logs).
        trace_raw_mf4_filename = ''
        trace_mf4_filename = ''
        try:
            trace_raw_mf4_filename = str(self._export_trace_mf4(
                trace_frames,
                prefix='incident_mil_on',
                trigger_ts_ms=int(mil_on_ts_ms),
            ) or '')
        except Exception:
            trace_raw_mf4_filename = ''

        # Export decoded MF4 using the same decode/export logic as /api/mf4/export_decoded_mf4.
        try:
            trace_mf4_filename = str(self._export_trace_decoded_mf4(
                raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                trace_frames=trace_frames,
                prefix='incident_mil_on',
                trigger_ts_ms=int(mil_on_ts_ms),
            ) or '')
        except Exception:
            trace_mf4_filename = ''

        if not trace_mf4_filename:
            trace_mf4_filename = str(trace_raw_mf4_filename or '')

        lamp_snapshot = self._extract_lamp_snapshot(trace_frames, mil_on_ts_ms)

        # Parse DTCs from scan report
        dtcs: List[Dict[str, Any]] = []
        primary: Optional[Dict[str, Any]] = None
        try:
            if scan_report_filename:
                self._pipeline_set_phase('parsing_dtcs', message='Parsing DTCs from scan report', percent=35)
                log_dir = str(self.scan_report_dir_resolver() or '').strip()
                p = os.path.join(log_dir, scan_report_filename)
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    html = f.read()
                parsed = parse_vag_scan_report_html(html)
                if parsed.get('ok'):
                    dtcs = parsed.get('dtcs') or []
        except Exception:
            dtcs = []

        self._pipeline_update(dtc_count=int(len(dtcs or [])))

        # Enrich with PDX descriptions if available.
        dtc_map = None
        try:
            from vag_scanner import _load_active_pdx_dtc_map, _dtc_description
            dtc_map = _load_active_pdx_dtc_map()
            for d in dtcs:
                code = str(d.get('code') or '').strip()
                if not code:
                    continue
                desc = str(d.get('desc_report') or '').strip()
                pdx_desc = ''
                try:
                    pdx_desc = str(_dtc_description(code, dtc_map) or '').strip()
                except Exception:
                    pdx_desc = ''
                d['desc_pdx'] = pdx_desc
                d['desc'] = pdx_desc or desc
        except Exception:
            for d in dtcs:
                d['desc'] = str(d.get('desc_report') or '').strip()

        # Optional offline bench mode: if scan yields no DTCs, pull a few random codes from active PDX.
        try:
            if not dtcs and bool(cfg.get('sentinel_random_dtcs_on_empty', False)):
                try:
                    import random
                    if dtc_map is None:
                        from vag_scanner import _load_active_pdx_dtc_map
                        dtc_map = _load_active_pdx_dtc_map()
                    if isinstance(dtc_map, dict) and dtc_map:
                        try:
                            n = int(cfg.get('sentinel_random_dtcs_n', 3) or 3)
                        except Exception:
                            n = 3
                        n = max(1, min(n, 10))
                        codes = [k for k in dtc_map.keys() if isinstance(k, str) and k.strip()]
                        picks = random.sample(codes, k=min(n, len(codes))) if codes else []
                        for code in picks:
                            desc = str(dtc_map.get(code) or '').strip()
                            dtcs.append({
                                'ecu_name': '',
                                'code': str(code),
                                'active': False,
                                'desc_report': '',
                                'desc_pdx': desc,
                                'desc': desc,
                                'status_byte': '',
                                'status_desc': 'offline (PDX random pick)',
                            })
                except Exception:
                    pass
        except Exception:
            pass

        # Pre-filter: only DTCs with status 0x08 (confirmed) or 0x80 (MIL) are relevant.
        dtcs_relevant, _ = self._filter_dtcs_by_status(dtcs)
        if not dtcs_relevant:
            dtcs_relevant = list(dtcs or [])  # fallback if nothing passes

        primary = choose_primary_dtc(dtcs_relevant)
        if primary is not None:
            primary = dict(primary)
            primary['confidence'] = 'low'
            primary['severity'] = 'warning'
            if bool(primary.get('active')):
                primary['confidence'] = 'medium'
            if bool(primary.get('active')) and primary.get('desc'):
                primary['confidence'] = 'high'

        out_name = f"incident_mil_on_{time.strftime('%Y%m%d_%H%M%S', time.localtime(mil_on_ts_ms/1000.0))}.html"

        sentinel_analyses: List[Dict[str, Any]] = []
        sentinel_meta: Dict[str, Any] = {}
        try:
            dtcs_for_analysis, dtc_filter_info = self._filter_dtcs_for_analysis(dtcs=dtcs, event_ts_ms=int(mil_on_ts_ms), cfg=cfg)
            self._pipeline_update(dtc_filter=_json_sanitize(dtc_filter_info))
            if bool(cfg.get('sentinel_llm_enabled', False)) and dtcs_for_analysis:
                self._pipeline_set_phase(
                    'llm_start',
                    message='Starting per-DTC LLM analysis',
                    percent=55,
                    dtcs_selected=int(len(dtcs_for_analysis)),
                )
                sentinel_analyses = self._sentinel_analyze_dtcs(
                    dtcs=dtcs_for_analysis,
                    primary=primary,
                    incident_context={
                        'incident_id': incident_id,
                        'mil_on_ts_ms': int(mil_on_ts_ms),
                        'scan_action': str(scan_action or ''),
                        'scan_report_filename': str(scan_report_filename or ''),
                        'dtc_filter': dtc_filter_info,
                    },
                    meta_out=sentinel_meta,
                )
        except Exception:
            sentinel_analyses = []

        bundle_filename = ''
        try:
            bundle_filename = str(self._export_incident_bundle_zip(
                prefix='incident_mil_on',
                trigger_ts_ms=int(mil_on_ts_ms),
                trace_mf4_filename=str(trace_mf4_filename or ''),
                trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                scan_report_filename=str(scan_report_filename or ''),
            ) or '')
        except Exception:
            bundle_filename = ''

        final_report_name = ''
        try:
            if bool(cfg.get('sentinel_final_report_enabled', True)):
                final_report_name = f"sentinel_final_report_{incident_id}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(mil_on_ts_ms/1000.0))}.html"
        except Exception:
            final_report_name = ''

        try:
            log_dir = str(self.log_dir_resolver() or '').strip()
            os.makedirs(log_dir, exist_ok=True)

            self._pipeline_set_phase('writing_reports', message='Writing HTML reports', percent=95)
            html_out = build_incident_html(
                incident_id=incident_id,
                mil_on_ts_ms=int(mil_on_ts_ms),
                scan_started_ts_ms=int(scan_started),
                scan_finished_ts_ms=int(scan_finished),
                scan_report_filename=str(scan_report_filename or ''),
                trace_mf4_filename=str(trace_mf4_filename or ''),
                trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                primary=primary,
                dtcs=dtcs,
                trace_frames=trace_frames,
                lamp_snapshot=lamp_snapshot,
                notify_placeholder={'central_server': True, 'email': True, 'note': 'placeholder'},
                sentinel_analyses=sentinel_analyses,
            )
            with open(os.path.join(log_dir, out_name), 'w', encoding='utf-8') as f:
                f.write(html_out)

            if final_report_name:
                final_html = build_final_report_html(
                    incident_id=incident_id,
                    mil_on_ts_ms=int(mil_on_ts_ms),
                    scan_action=str(scan_action or ''),
                    scan_started_ts_ms=int(scan_started),
                    scan_finished_ts_ms=int(scan_finished),
                    scan_report_filename=str(scan_report_filename or ''),
                    trace_mf4_filename=str(trace_mf4_filename or ''),
                    trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                    bundle_filename=str(bundle_filename or ''),
                    primary=primary,
                    dtcs=dtcs,
                    lamp_snapshot=lamp_snapshot,
                    sentinel_analyses=sentinel_analyses,
                )
                with open(os.path.join(log_dir, final_report_name), 'w', encoding='utf-8') as f:
                    f.write(final_html)
        except Exception:
            out_name = ''
            final_report_name = ''

        self._pipeline_update(
            active=False,
            phase='done',
            message='Final report generated',
            percent=100,
            final_report_filename=str(final_report_name or ''),
            incident_report_filename=str(out_name or ''),
            scan_report_filename=str(scan_report_filename or ''),
        )

        incident = {
            'incident_id': incident_id,
            'mil_on_ts_ms': int(mil_on_ts_ms),
            'kind': 'mil',
            'scan_action': scan_action,
            'scan_report_filename': scan_report_filename,
            'trace_mf4_filename': trace_mf4_filename,
            'trace_raw_mf4_filename': trace_raw_mf4_filename,
            'bundle_filename': bundle_filename,
            'incident_report_filename': out_name,
            'final_report_filename': final_report_name,
            'primary': primary,
            'dtc_count': len(dtcs),
            'lamps': lamp_snapshot,
            'sentinel': {
                'enabled': bool(cfg.get('sentinel_llm_enabled', False)),
                'analyses_count': len(sentinel_analyses) if isinstance(sentinel_analyses, list) else 0,
                'analyses': (sentinel_analyses if isinstance(sentinel_analyses, list) else []),
                'meta': _json_sanitize(dict(sentinel_meta or {})),
            },
        }
        with self._lock:
            self._last_incident = incident
            self._incidents.append(incident)
        self._save_persistence()
        try:
            self._enforce_logs_retention(reason='mil_incident')
        except Exception:
            pass

    def _sentinel_system_prompt(self) -> str:
        return (
            "Sei Sentinel, un assistente diagnostico automotive. "
            "Analizza UN singolo DTC alla volta usando: codice, descrizione PDX, contesto incidente, "
            "timestamp dell'errore, chilometraggio, stato attivo/passivo e flag di status. "
            "Non inventare dati non presenti. "
            "Rispondi SEMPRE in italiano e con questo formato:\n"
            "**Sintesi**\n...\n\n"
            "**Possibili cause**\n- ...\n\n"
            "**Verifiche consigliate**\n1. ...\n\n"
            "**Rischio / severità**\n...\n\n"
            "**Note di correlazione**\n...\n"
        )

    def _sentinel_analyze_dtcs(
        self,
        *,
        dtcs: List[Dict[str, Any]],
        primary: Optional[Dict[str, Any]],
        incident_context: Optional[Dict[str, Any]] = None,
        max_dtcs_override: Optional[int] = None,
        timeout_s_override: Optional[float] = None,
        num_predict_override: Optional[int] = None,
        meta_out: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        cfg = self._get_cfg()
        meta: Dict[str, Any] = {}
        try:
            max_dtcs = int(max_dtcs_override) if max_dtcs_override is not None else int(cfg.get('sentinel_llm_max_dtcs', 3) or 3)
        except Exception:
            max_dtcs = 3
        # Allow larger batches for offline report ingestion, but keep an upper bound.
        max_dtcs = max(1, min(max_dtcs, 50))

        # Pick DTCs to analyze: primary first, then ACTIVE ones.
        picks: List[Dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(primary, dict):
            c = str(primary.get('code') or '').strip()
            if c:
                picks.append(primary)
                seen.add(c)
        for d in dtcs:
            if len(picks) >= max_dtcs:
                break
            if not isinstance(d, dict):
                continue
            c = str(d.get('code') or '').strip()
            if not c or c in seen:
                continue
            if bool(d.get('active')):
                picks.append(d)
                seen.add(c)
        for d in dtcs:
            if len(picks) >= max_dtcs:
                break
            if not isinstance(d, dict):
                continue
            c = str(d.get('code') or '').strip()
            if not c or c in seen:
                continue
            picks.append(d)
            seen.add(c)

        skip, skip_reason = self._sentinel_breaker_should_skip(cfg=cfg)
        if skip:
            meta.update({'skipped': True, 'skip_reason': str(skip_reason), 'breaker': self._sentinel_breaker_status()})
            if isinstance(meta_out, dict):
                meta_out.clear()
                meta_out.update(meta)
            return []

        # Build a dedicated agent here (no import of Flask app to avoid circular imports).
        try:
            from copilot_agent import CopilotAgent
        except Exception:
            return []

        def _env_str(key: str, default: str) -> str:
            try:
                v = os.getenv(key)
                if v is None:
                    return default
                s = str(v).strip()
                return s if s else default
            except Exception:
                return default

        def _env_int(key: str, default: int) -> int:
            try:
                return int(str(os.getenv(key, str(default))).strip())
            except Exception:
                return int(default)

        def _env_float(key: str, default: float) -> float:
            try:
                return float(str(os.getenv(key, str(default))).strip())
            except Exception:
                return float(default)

        provider = _env_str('SENTINEL_PROVIDER', _env_str('COPILOT_PROVIDER', 'ollama'))
        base_url = _env_str('SENTINEL_BASE_URL', _env_str('COPILOT_BASE_URL', 'http://127.0.0.1:11434'))
        model = _env_str('SENTINEL_MODEL', _env_str('COPILOT_MODEL', 'llama3.2:3b'))
        agent = CopilotAgent(provider=provider, base_url=base_url, model=model, timeout_s=_env_float('SENTINEL_TIMEOUT_S', _env_float('COPILOT_TIMEOUT_S', 30.0)))
        meta.update({'provider': provider, 'base_url': base_url, 'model': model, 'picks_count': int(len(picks))})

        try:
            from llm_singleflight import LLM_SINGLEFLIGHT_LOCK
        except Exception:
            LLM_SINGLEFLIGHT_LOCK = threading.Lock()  # type: ignore

        # Best-effort single-flight: wait briefly, then skip if still busy.
        try:
            lock_wait_s = float(cfg.get('sentinel_llm_lock_wait_s', 3.0) or 3.0)
        except Exception:
            lock_wait_s = 3.0
        lock_wait_s = float(max(0.0, min(lock_wait_s, 30.0)))
        if not LLM_SINGLEFLIGHT_LOCK.acquire(timeout=lock_wait_s):
            meta.update({'skipped': True, 'skip_reason': 'llm_lock_busy', 'lock_wait_s': float(lock_wait_s)})
            if isinstance(meta_out, dict):
                meta_out.clear()
                meta_out.update(meta)
            return []

        def _missing_sections(t: str) -> List[str]:
            s = str(t or '')
            required = [
                '**Sintesi**',
                '**Possibili cause**',
                '**Verifiche consigliate**',
                '**Rischio / severità**',
                '**Note di correlazione**',
            ]
            return [h for h in required if h not in s]

        try:
            out: List[Dict[str, Any]] = []
            errors = 0
            total = int(len(picks))
            for idx, d in enumerate(picks):
                code = str(d.get('code') or '').strip()
                desc = str(d.get('desc') or d.get('desc_pdx') or d.get('desc_report') or '').strip()

                try:
                    base = 60.0
                    span = 35.0
                    pct = base + (span * (float(idx) / float(max(1, total))))
                    self._pipeline_update(
                        active=True,
                        phase='llm_analyzing',
                        message=f'LLM analyzing {code}',
                        dtc_index=int(idx + 1),
                        dtc_total=int(total),
                        dtc_code=str(code),
                        percent=float(min(95.0, max(0.0, pct))),
                    )
                except Exception:
                    pass

                incident_label = 'MIL ON'
                try:
                    k = str((incident_context or {}).get('kind') or '').strip().lower()
                    if k == 'mil':
                        incident_label = 'MIL ON'
                    elif k == 'epc':
                        incident_label = 'EPC LAMP ON'
                    elif k in {'gearbox', 'cambio'}:
                        incident_label = 'GEARBOX LAMP ON'
                    elif k:
                        incident_label = f"{k.upper()} EVENT"
                except Exception:
                    incident_label = 'MIL ON'

                # Gather rich context from enriched DTC data
                ecu_name = str(d.get('ecu_name') or '').strip()
                status_byte_str = str(d.get('status_byte') or '').strip()
                status_desc_str = str(d.get('status_desc') or '').strip()
                ts_txt = str(d.get('timestamp_text') or '').strip()
                km_val = d.get('odometer_km')
                km_txt = str(d.get('km_at_fault_text') or '').strip()
                is_active = bool(d.get('active'))

                context_parts: List[str] = []
                context_parts.append(f"Contesto: incidente {incident_label}.")
                if ecu_name:
                    context_parts.append(f"ECU: {ecu_name}.")
                if is_active:
                    context_parts.append("Stato: ATTIVO.")
                else:
                    context_parts.append("Stato: PASSIVO.")
                if status_byte_str:
                    context_parts.append(f"Status byte: {status_byte_str}.")
                if status_desc_str:
                    context_parts.append(f"Flag: {status_desc_str}.")
                if ts_txt:
                    context_parts.append(f"Timestamp errore: {ts_txt}.")
                if isinstance(km_val, int):
                    context_parts.append(f"Chilometri al momento dell'errore: {km_val} km.")
                elif km_txt:
                    context_parts.append(f"Chilometri: {km_txt}.")
                if not ts_txt and not isinstance(km_val, int):
                    context_parts.append("Non sono disponibili freeze frame.")

                user_msg = (
                    f"Analizza il DTC {code}.\n"
                    f"Descrizione PDX/report: {desc or '(mancante)'}\n\n"
                    + ' '.join(context_parts) + "\n"
                    "Proponi verifiche pratiche e prioritarie."
                )
                ctx = {
                    'kind': 'sentinel_incident_dtc',
                    'incident': incident_context or {},
                    'dtc': {
                        'code': code,
                        'pdx_description': str(d.get('desc_pdx') or '').strip(),
                        'description': desc,
                        'active': is_active,
                        'ecu_name': ecu_name,
                        'status_byte': status_byte_str,
                        'status_desc': status_desc_str,
                        'timestamp_text': ts_txt,
                        'odometer_km': km_val,
                    },
                }
                try:
                    r = agent.chat(
                        system=self._sentinel_system_prompt(),
                        user=user_msg,
                        context=ctx,
                        temperature=_env_float('SENTINEL_LLM_TEMPERATURE', 0.2),
                        max_context_chars=_env_int('SENTINEL_LLM_MAX_CONTEXT_CHARS', 3500),
                        timeout_s=float(timeout_s_override) if timeout_s_override is not None else _env_float('SENTINEL_LLM_TIMEOUT_S', 360.0),
                        num_predict=int(num_predict_override) if num_predict_override is not None else _env_int('SENTINEL_LLM_NUM_PREDICT', 512),
                    )
                    if not bool(r.get('ok')):
                        raise RuntimeError(str(r.get('error') or 'provider error'))
                    content = str(r.get('content') or '').strip()
                    out.append({'code': code, 'desc': desc, 'analysis': content, 'missing_sections': _missing_sections(content)})
                except Exception as e:
                    errors += 1
                    out.append({'code': code, 'desc': desc, 'analysis': '', 'error': str(e), 'missing_sections': []})

            # Breaker update: any success resets failures; total failure increments.
            if any(bool((x or {}).get('analysis')) for x in out or []):
                try:
                    self._sentinel_breaker_note_success()
                except Exception:
                    pass
            elif errors > 0:
                try:
                    self._sentinel_breaker_note_failure(cfg=cfg, error=str((out[-1] or {}).get('error') or 'llm_error'))
                except Exception:
                    pass

            meta.update({'errors': int(errors), 'analyses_count': int(len(out))})
            meta['breaker'] = self._sentinel_breaker_status()
            if isinstance(meta_out, dict):
                meta_out.clear()
                meta_out.update(meta)
            return out
        finally:
            try:
                LLM_SINGLEFLIGHT_LOCK.release()
            except Exception:
                pass

    def _handle_lamp_incident(self, *, kind: str, lamp_on_ts_ms: int, channel: Optional[int]) -> None:
        """Generate an incident report for EPC/gearbox lamp ON events."""
        cfg = self._get_cfg()

        incident_id = f"{str(kind or 'lamp')}_{int(lamp_on_ts_ms):x}"
        self._pipeline_update(
            active=True,
            kind=str(kind or 'lamp'),
            incident_id=str(incident_id),
            trigger_ts_ms=int(lamp_on_ts_ms),
            phase='triggered',
            message=f'{str(kind or "lamp").upper()} lamp ON detected',
            percent=5,
        )

        post_s = float(cfg.get('trace_post_s', 15.0) or 15.0)
        pre_s = float(cfg.get('trace_pre_s', 15.0) or 15.0)

        end_ts_ms = int(lamp_on_ts_ms + int(post_s * 1000))
        self._pipeline_set_phase('waiting_post_window', message='Waiting post window', percent=10, window_post_s=float(post_s))
        while _now_ms() < end_ts_ms and not self._stop_evt.is_set():
            time.sleep(0.2)

        trace_start = int(lamp_on_ts_ms - int(pre_s * 1000))
        trace_end = int(lamp_on_ts_ms + int(post_s * 1000))
        ch = None
        try:
            if channel is not None:
                ch = int(channel)
        except Exception:
            ch = None

        trace_frames = self.trace.slice(
            trace_start,
            trace_end,
            channel=self._incident_trace_channel(ch),
        )
        lamp_snapshot = self._extract_lamp_snapshot(trace_frames, int(lamp_on_ts_ms))

        # Prefer DoIP scan report for lamp incidents too (fallback to CAN scan report).
        scan_action = ''
        scan_report_filename = ''
        scan_started = _now_ms()
        scan_finished = scan_started
        try:
            self._pipeline_set_phase('scanning', message='Running scan report', percent=25)
            scan_action, scan_report_filename, scan_started, scan_finished = self._run_scan_report(int(ch or 0))
        except Exception:
            scan_finished = _now_ms()

        # Export trace window as MF4 excerpt (raw) then decoded.
        trace_raw_mf4_filename = ''
        trace_mf4_filename = ''
        try:
            trace_raw_mf4_filename = str(self._export_trace_mf4(
                trace_frames,
                prefix=f"incident_{str(kind or 'lamp')}_on",
                trigger_ts_ms=int(lamp_on_ts_ms),
            ) or '')
        except Exception:
            trace_raw_mf4_filename = ''

        try:
            trace_mf4_filename = str(self._export_trace_decoded_mf4(
                raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                trace_frames=trace_frames,
                prefix=f"incident_{str(kind or 'lamp')}_on",
                trigger_ts_ms=int(lamp_on_ts_ms),
            ) or '')
        except Exception:
            trace_mf4_filename = ''

        if not trace_mf4_filename:
            trace_mf4_filename = str(trace_raw_mf4_filename or '')

        mappings = self._get_lamp_mappings()
        mapping = mappings.get(str(kind or '').strip().lower()) if isinstance(mappings, dict) else None

        # Parse DTCs from scan report (prefer HTML).
        dtcs: List[Dict[str, Any]] = []
        primary: Optional[Dict[str, Any]] = None
        dtc_map = None
        try:
            if scan_report_filename and str(scan_report_filename).lower().endswith('.html'):
                self._pipeline_set_phase('parsing_dtcs', message='Parsing DTCs from scan report', percent=40)
                log_dir = str(self.scan_report_dir_resolver() or '').strip()
                p = os.path.join(log_dir, scan_report_filename)
                with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                    html = f.read()
                parsed = parse_vag_scan_report_html(html)
                if parsed.get('ok'):
                    dtcs = parsed.get('dtcs') or []
        except Exception:
            dtcs = []

        self._pipeline_update(dtc_count=int(len(dtcs or [])))

        # Enrich with PDX descriptions if available.
        try:
            self._pipeline_set_phase('enriching_dtcs', message='Enriching DTCs with PDX', percent=50)
        except Exception:
            pass
        try:
            from vag_scanner import _load_active_pdx_dtc_map, _dtc_description
            dtc_map = _load_active_pdx_dtc_map()
            for d in dtcs:
                code = str(d.get('code') or '').strip()
                if not code:
                    continue
                desc = str(d.get('desc_report') or '').strip()
                pdx_desc = ''
                try:
                    pdx_desc = str(_dtc_description(code, dtc_map) or '').strip()
                except Exception:
                    pdx_desc = ''
                d['desc_pdx'] = pdx_desc
                d['desc'] = pdx_desc or desc
        except Exception:
            for d in dtcs:
                d['desc'] = str(d.get('desc_report') or '').strip()

        # Optional offline bench mode: if scan yields no DTCs, pull a few random codes from active PDX.
        try:
            if not dtcs and bool(cfg.get('sentinel_random_dtcs_on_empty', False)):
                try:
                    import random
                    if dtc_map is None:
                        from vag_scanner import _load_active_pdx_dtc_map
                        dtc_map = _load_active_pdx_dtc_map()
                    if isinstance(dtc_map, dict) and dtc_map:
                        try:
                            n = int(cfg.get('sentinel_random_dtcs_n', 3) or 3)
                        except Exception:
                            n = 3
                        n = max(1, min(n, 10))
                        codes = [k for k in dtc_map.keys() if isinstance(k, str) and k.strip()]
                        picks = random.sample(codes, k=min(n, len(codes))) if codes else []
                        for code in picks:
                            desc = str(dtc_map.get(code) or '').strip()
                            dtcs.append({
                                'ecu_name': '',
                                'code': str(code),
                                'active': False,
                                'desc_report': '',
                                'desc_pdx': desc,
                                'desc': desc,
                                'status_byte': '',
                                'status_desc': 'offline (PDX random pick)',
                            })
                except Exception:
                    pass
        except Exception:
            pass

        # Pre-filter: only DTCs with status 0x08 (confirmed) or 0x80 (MIL) are relevant.
        dtcs_relevant, _ = self._filter_dtcs_by_status(dtcs)
        if not dtcs_relevant:
            dtcs_relevant = list(dtcs or [])  # fallback if nothing passes

        primary = choose_primary_dtc(dtcs_relevant)
        if primary is not None:
            primary = dict(primary)
            primary['confidence'] = 'low'
            primary['severity'] = 'warning'
            if bool(primary.get('active')):
                primary['confidence'] = 'medium'
            if bool(primary.get('active')) and primary.get('desc'):
                primary['confidence'] = 'high'

        out_name = f"incident_{str(kind or 'lamp')}_on_{time.strftime('%Y%m%d_%H%M%S', time.localtime(lamp_on_ts_ms/1000.0))}.html"

        sentinel_analyses: List[Dict[str, Any]] = []
        sentinel_meta: Dict[str, Any] = {}
        try:
            dtcs_for_analysis, dtc_filter_info = self._filter_dtcs_for_analysis(dtcs=dtcs, event_ts_ms=int(lamp_on_ts_ms), cfg=cfg)
            self._pipeline_update(dtc_filter=_json_sanitize(dtc_filter_info))
            if bool(cfg.get('sentinel_llm_enabled', False)) and dtcs_for_analysis:
                self._pipeline_set_phase(
                    'llm_start',
                    message='Starting per-DTC LLM analysis',
                    percent=60,
                    dtcs_selected=int(len(dtcs_for_analysis)),
                )
                sentinel_analyses = self._sentinel_analyze_dtcs(
                    dtcs=dtcs_for_analysis,
                    primary=primary,
                    incident_context={
                        'incident_id': incident_id,
                        'kind': str(kind or 'lamp'),
                        'lamp_on_ts_ms': int(lamp_on_ts_ms),
                        'scan_action': str(scan_action or ''),
                        'scan_report_filename': str(scan_report_filename or ''),
                        'dtc_filter': dtc_filter_info,
                    },
                    meta_out=sentinel_meta,
                )
        except Exception:
            sentinel_analyses = []

        bundle_filename = ''
        try:
            bundle_filename = str(self._export_incident_bundle_zip(
                prefix=f"incident_{str(kind or 'lamp')}_on",
                trigger_ts_ms=int(lamp_on_ts_ms),
                trace_mf4_filename=str(trace_mf4_filename or ''),
                trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                scan_report_filename=str(scan_report_filename or ''),
            ) or '')
        except Exception:
            bundle_filename = ''

        final_report_name = ''
        try:
            if bool(cfg.get('sentinel_final_report_enabled', True)):
                final_report_name = f"sentinel_final_report_{incident_id}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(lamp_on_ts_ms/1000.0))}.html"
        except Exception:
            final_report_name = ''
        try:
            log_dir = str(self.log_dir_resolver() or '').strip()
            os.makedirs(log_dir, exist_ok=True)
            self._pipeline_set_phase('writing_reports', message='Writing HTML reports', percent=95)
            html_out = build_lamp_incident_html(
                incident_id=incident_id,
                kind=str(kind or ''),
                lamp_on_ts_ms=int(lamp_on_ts_ms),
                mapping=mapping,
                trace_frames=trace_frames,
                scan_report_filename=str(scan_report_filename or ''),
                trace_mf4_filename=str(trace_mf4_filename or ''),
                trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                lamp_snapshot=lamp_snapshot,
            )
            with open(os.path.join(log_dir, out_name), 'w', encoding='utf-8') as f:
                f.write(html_out)

            if final_report_name:
                final_html = build_final_report_html(
                    incident_id=incident_id,
                    mil_on_ts_ms=int(lamp_on_ts_ms),
                    event_kind=str(kind or 'lamp'),
                    event_ts_ms=int(lamp_on_ts_ms),
                    scan_action=str(scan_action or ''),
                    scan_started_ts_ms=int(scan_started),
                    scan_finished_ts_ms=int(scan_finished),
                    scan_report_filename=str(scan_report_filename or ''),
                    trace_mf4_filename=str(trace_mf4_filename or ''),
                    trace_raw_mf4_filename=str(trace_raw_mf4_filename or ''),
                    bundle_filename=str(bundle_filename or ''),
                    primary=primary,
                    dtcs=dtcs,
                    lamp_snapshot=lamp_snapshot,
                    sentinel_analyses=sentinel_analyses,
                )
                with open(os.path.join(log_dir, final_report_name), 'w', encoding='utf-8') as f:
                    f.write(final_html)
        except Exception:
            out_name = ''
            final_report_name = ''

        self._pipeline_update(
            active=False,
            phase='done',
            message='Final report generated',
            percent=100,
            final_report_filename=str(final_report_name or ''),
            incident_report_filename=str(out_name or ''),
            scan_report_filename=str(scan_report_filename or ''),
        )

        incident = {
            'incident_id': incident_id,
            'kind': str(kind or 'lamp'),
            'lamp_on_ts_ms': int(lamp_on_ts_ms),
            'channel': int(ch) if ch is not None else None,
            'scan_action': scan_action,
            'scan_report_filename': scan_report_filename,
            'trace_mf4_filename': trace_mf4_filename,
            'trace_raw_mf4_filename': trace_raw_mf4_filename,
            'bundle_filename': bundle_filename,
            'incident_report_filename': out_name,
            'final_report_filename': final_report_name,
            'primary': primary,
            'dtc_count': len(dtcs),
            'lamps': lamp_snapshot,
            'sentinel': {
                'enabled': bool(cfg.get('sentinel_llm_enabled', False)),
                'analyses_count': len(sentinel_analyses) if isinstance(sentinel_analyses, list) else 0,
                'analyses': (sentinel_analyses if isinstance(sentinel_analyses, list) else []),
                'meta': _json_sanitize(dict(sentinel_meta or {})),
            },
        }
        with self._lock:
            self._last_incident = incident
            self._incidents.append(incident)
        self._save_persistence()
        try:
            self._enforce_logs_retention(reason=f"lamp_incident_{str(kind or 'lamp')}")
        except Exception:
            pass

    def _scan_doip_params(self) -> Dict[str, Any]:
        """Build DoIP scan params like /api/scantools/run does."""
        cfg = {}
        try:
            cfg = self.config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gateway_ip = str(es.get('target_ip') or '').strip()
        gateway_iface = str(es.get('interface') or '').strip()

        auto_discover = bool(es.get('doip_auto_discover', True))
        try:
            tester_logical_address = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00

        return {
            'gateway_ip': gateway_ip,
            'gateway_iface': gateway_iface,
            'auto_discover': auto_discover,
            'tester_logical_address': tester_logical_address,
        }

    def _pick_newest_scan_report(self, *, prefer: str = 'any') -> str:
        """Return newest scan artifact filename from scan_report_dir_resolver.

        prefer: 'doip'|'can'|'any'

        Notes:
        - DoIP scan report normally writes `vag_doip_scan_report_<ts>.html`.
        - CAN scan in some deployments writes `vag_scan_<ts>.txt` (not HTML).
        """
        log_dir = str(self.scan_report_dir_resolver() or '').strip()
        if not log_dir or not os.path.isdir(log_dir):
            return ''

        def _match(n: str) -> bool:
            if not isinstance(n, str):
                return False
            nl = n.lower()
            if not (nl.endswith('.html') or nl.endswith('.txt')):
                return False
            if prefer == 'doip':
                return n.startswith('vag_doip_scan_report_')
            if prefer == 'can':
                return (
                    n.startswith('vag_scan_report_')
                    or n.startswith('vag_obd_scan_report_')
                    or n.startswith('vag_scan_')
                    or n.startswith('vag_obd_scan_')
                )
            return (
                n.startswith('vag_doip_scan_report_')
                or n.startswith('vag_scan_report_')
                or n.startswith('vag_obd_scan_report_')
                or n.startswith('vag_scan_')
                or n.startswith('vag_obd_scan_')
            )

        # Prefer HTML scan reports when present; they contain structured DTCs.
        # But avoid picking an old/stale HTML from a previous scan when a newer
        # (txt) artifact exists from the current scan.
        newest_any = ''
        newest_any_m = 0.0
        newest_html = ''
        newest_html_m = 0.0

        for n in os.listdir(log_dir):
            if not _match(n):
                continue
            p = os.path.join(log_dir, n)
            try:
                st = os.stat(p)
                m = float(st.st_mtime)
            except Exception:
                continue

            if m > newest_any_m:
                newest_any_m = m
                newest_any = n
            if isinstance(n, str) and n.lower().endswith('.html') and m > newest_html_m:
                newest_html_m = m
                newest_html = n

        if newest_any and str(newest_any).lower().endswith('.html'):
            return str(newest_any)

        # If HTML is close-in-time to the newest artifact, it's probably the
        # paired output from the same scan; otherwise, prefer newest overall.
        try:
            if newest_html and (newest_any_m - newest_html_m) <= 60.0:
                return str(newest_html)
        except Exception:
            pass

        return str(newest_any or newest_html or '')

    def _run_scan_report(self, channel_id: int) -> Tuple[str, str, int, int]:
        """Attempt DoIP scan report first, fallback to CAN scan report.

        Returns (scan_action, scan_report_filename, started_ms, finished_ms)
        """
        cfg = self._get_cfg()
        try:
            timeout_s = float(cfg.get('scan_timeout_s', 45.0) or 45.0)
        except Exception:
            timeout_s = 45.0
        timeout_s = float(max(5.0, min(timeout_s, 300.0)))

        # Global scan rate limit (protect ECU + keep system responsive)
        try:
            rate_limit_s = float(cfg.get('scan_rate_limit_s', 600) or 600)
        except Exception:
            rate_limit_s = 600.0
        rate_limit_ms = int(max(0.0, min(rate_limit_s, 24 * 3600.0)) * 1000.0)
        now_ms = _now_ms()
        with self._lock:
            last = int(self._last_scan_started_ts_ms or 0)
            if last and rate_limit_ms and (now_ms - last) < rate_limit_ms:
                self._scan_rate_limited_count = int(self._scan_rate_limited_count or 0) + 1
                fn = self._pick_newest_scan_report(prefer='any')
                return ('scan_rate_limited', str(fn or ''), int(last), int(now_ms))
            self._last_scan_started_ts_ms = int(now_ms)

        started_ms = _now_ms()
        finished_ms = started_ms

        # ── [TRC Fix 2026-02-17] Pause our own MIL polling while the scan
        #    holds a DoIP connection to avoid tester-address collision. ──
        _did_pause_mil = False
        try:
            if getattr(self, '_mil_transport', '') == 'doip':
                self.pause_doip_mil()
                _did_pause_mil = True
        except Exception:
            pass

        try:
            def _wait_done() -> None:
                nonlocal finished_ms
                t_end = time.time() + timeout_s
                while time.time() < t_end:
                    st = self.scanner_service.status() if self.scanner_service else {}
                    if not st.get('running'):
                        break
                    time.sleep(0.5)
                finished_ms = _now_ms()

            # 1) DoIP first
            try:
                params = self._scan_doip_params()
                started = bool(self.scanner_service and self.scanner_service.start_action(int(channel_id), 'vag_doip_scan_report', params=params))
                if started:
                    _wait_done()
                    fn = self._pick_newest_scan_report(prefer='doip')
                    if fn:
                        return ('vag_doip_scan_report', fn, int(started_ms), int(finished_ms))
            except Exception:
                pass

            # 2) Fallback: CAN scan report
            try:
                started_ms = _now_ms()
                started = bool(self.scanner_service and self.scanner_service.start_action(int(channel_id), 'vag_scan_report', params={}))
                if started:
                    _wait_done()
                    fn = self._pick_newest_scan_report(prefer='can')
                    if fn:
                        return ('vag_scan_report', fn, int(started_ms), int(finished_ms))
            except Exception:
                pass

            return ('', '', int(started_ms), int(_now_ms()))
        finally:
            if _did_pause_mil:
                try:
                    self.resume_doip_mil()
                except Exception:
                    pass

    def _incident_trace_channel(self, trigger_channel: Optional[int]) -> Optional[int]:
        """Return channel filter for Sentinel incident trace extraction.

        Default is multi-channel (None) so the excerpt contains physical CAN and
        mirror traffic (CAN/FlexRay/LIN) around the incident window.
        """
        cfg = self._get_cfg()
        scope = str(cfg.get('trace_channel_scope', 'all') or 'all').strip().lower()
        if scope in {'trigger_channel', 'single', 'legacy'}:
            try:
                return int(trigger_channel) if trigger_channel is not None else None
            except Exception:
                return None
        return None

    def _export_trace_mf4(self, trace_frames: List[TraceFrame], *, prefix: str, trigger_ts_ms: int) -> str:
        """Export trace_frames to a small MF4 excerpt in the logs folder.

        Returns the created basename (for /api/logs/<name>), or '' on failure.
        """
        if not trace_frames:
            return ''

        try:
            import numpy as np  # type: ignore
            import asammdf  # type: ignore
        except Exception:
            return ''

        cfg = self._get_cfg()

        # Keep every bus type except Ethernet payloads.
        frames: List[TraceFrame] = []
        for f in trace_frames:
            try:
                if str(getattr(f, 'frame_type', '') or '').upper() == 'ETH':
                    continue
            except Exception:
                pass
            frames.append(f)

        if not frames:
            return ''

        # Use relative time from the first frame to keep excerpt small & readable.
        t0_ms = int(frames[0].ts_ms)

        t_s: List[float] = []
        ids: List[int] = []
        dlcs: List[int] = []
        payload_lengths: List[int] = []
        chs: List[int] = []
        bus_types: List[int] = []
        flags: List[int] = []
        db: List[List[int]] = []

        decoded_series: Dict[str, Dict[str, List[float]]] = {}

        def _coerce_numeric(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                if isinstance(v, bool):
                    return float(1.0 if v else 0.0)
            except Exception:
                pass
            try:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return float(v)
            except Exception:
                pass
            try:
                vv = getattr(v, 'value', None)
                if vv is not None and not isinstance(vv, bool):
                    try:
                        return float(vv)
                    except Exception:
                        return None
            except Exception:
                pass
            try:
                if isinstance(v, dict) and 'value' in v:
                    return float(v.get('value'))
            except Exception:
                pass
            try:
                return float(v)
            except Exception:
                return None

        for f in frames:
            try:
                ts_ms = int(f.ts_ms)
            except Exception:
                continue
            rel_s = float(ts_ms - t0_ms) / 1000.0
            if rel_s < 0:
                continue

            data = f.data
            data_list: List[int]
            if isinstance(data, (bytes, bytearray)):
                data_list = list(data)
            else:
                try:
                    data_list = [int(x) & 0xFF for x in (data or [])]
                except Exception:
                    data_list = []
            d = (data_list + [0] * 64)[:64]
            try:
                frame_type = str(getattr(f, 'frame_type', '') or '')
            except Exception:
                frame_type = ''
            payload_len = int(max(0, min(len(data_list), 0xFFFF)))
            dlc = int(max(0, min(len(data_list), 0xFFFF)))

            t_s.append(rel_s)
            ids.append(int(f.frame_id) & 0x1FFFFFFF)
            dlcs.append(dlc)
            payload_lengths.append(payload_len)
            chs.append(int(f.channel) & 0xFF)
            bus_types.append(int(_raw_mf4_bus_type_code(frame_type)) & 0xFF)
            flags.append(int(getattr(f, 'flags', 0) or 0) & 0xFFFFFFFF)
            db.append(d)

            # Optional: include decoded numeric signals for watched messages.
            try:
                msg = str(getattr(f, 'decoded_name', '') or '').strip()
                sigs = getattr(f, 'decoded_signals', None)
                if msg and isinstance(sigs, dict) and sigs:
                    for sn, sv in sigs.items():
                        sig_name = str(sn or '').strip()
                        if not sig_name:
                            continue
                        fv = _coerce_numeric(sv)
                        if fv is None:
                            continue
                        key = f"{msg}.{sig_name}"
                        ds = decoded_series.get(key)
                        if ds is None:
                            ds = {'t': [], 'y': []}
                            decoded_series[key] = ds
                        ds['t'].append(rel_s)
                        ds['y'].append(float(fv))
            except Exception:
                pass

        if not t_s:
            return ''

        try:
            log_dir = str(self.log_dir_resolver() or '').strip()
            if not log_dir:
                return ''
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            return ''

        base = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(trigger_ts_ms/1000.0))}_trace"
        safe = os.path.basename(base) + '.mf4'
        out_path = os.path.join(log_dir, safe)

        try:
            mdf = asammdf.MDF()
            t_arr = np.asarray(t_s, dtype=np.float64)
            raw_sigs = [
                asammdf.Signal(np.asarray(ids, dtype=np.uint32), t_arr, name='CAN_ID'),
                asammdf.Signal(np.asarray(dlcs, dtype=np.uint16), t_arr, name='DLC'),
                asammdf.Signal(np.asarray(payload_lengths, dtype=np.uint16), t_arr, name='PayloadLength'),
                asammdf.Signal(np.asarray(chs, dtype=np.uint8), t_arr, name='Channel'),
                asammdf.Signal(np.asarray(bus_types, dtype=np.uint8), t_arr, name='BusType'),
                asammdf.Signal(np.asarray(flags, dtype=np.uint32), t_arr, name='Flags'),
            ]
            db_arr = np.asarray(db, dtype=np.uint8)
            for i in range(64):
                raw_sigs.append(asammdf.Signal(db_arr[:, i], t_arr, name=f'DataByte{i}'))
            mdf.append(raw_sigs, acq_name='CAN_Raw', comment='Incident trace excerpt')

            # Add decoded signals if present (limited).
            try:
                max_dec = int(cfg.get('trace_mf4_max_decoded_signals', 80) or 80)
            except Exception:
                max_dec = 80
            max_dec = int(max(0, min(max_dec, 500)))

            if decoded_series and max_dec != 0:
                names = list(decoded_series.keys())
                try:
                    names.sort(key=lambda n: len((decoded_series.get(n) or {}).get('t', [])), reverse=True)
                except Exception:
                    names = sorted(names)
                if max_dec > 0:
                    names = names[:max_dec]
                gi = 1
                for name in names:
                    ds = decoded_series.get(name) or {}
                    tt = np.asarray(ds.get('t', []), dtype=np.float64)
                    yy = np.asarray(ds.get('y', []), dtype=np.float64)
                    if tt.size == 0 or yy.size == 0:
                        continue
                    n = int(min(tt.size, yy.size))
                    if n <= 0:
                        continue
                    sig = asammdf.Signal(yy[:n], tt[:n], name=name)
                    mdf.append(sig, acq_name=f"{name}_R{gi}", comment='')
                    gi += 1

            # asammdf.save() normalizes suffix to ".mf4" via Path.with_suffix.
            # If we pass "*.mf4.tmp" it becomes "*.mf4.mf4". Keep the temp name ending in .mf4.
            if out_path.lower().endswith('.mf4'):
                tmp = out_path[:-4] + '.tmp.mf4'
            else:
                tmp = out_path + '.tmp.mf4'
            mdf.save(tmp, overwrite=True)
            if os.path.exists(tmp):
                os.replace(tmp, out_path)
            return safe
        except Exception:
            try:
                err = out_path + '.error.txt'
                with open(err, 'w', encoding='utf-8') as f:
                    f.write('trace mf4 export failed')
            except Exception:
                pass
            return ''

    def _export_trace_decoded_mf4(
        self,
        *,
        raw_mf4_filename: str,
        trace_frames: List[TraceFrame],
        prefix: str,
        trigger_ts_ms: int,
    ) -> str:
        """Export a decoded MF4 excerpt (numeric signals) from the raw excerpt.

        This mirrors the behavior of `/api/mf4/export_decoded_mf4` but runs locally
        during incident generation, and writes the output in the main logs folder
        so it is downloadable via `/api/logs/<filename>`.
        """
        if not raw_mf4_filename or not raw_mf4_filename.lower().endswith('.mf4'):
            return ''

        # Keep this parameter for API compatibility; decode now runs from raw MF4.
        _ = trace_frames

        try:
            has_live_can_dbcs = any(bool(loaders) for loaders in (self.bus_manager.dbcs or {}).values())
            has_live_arxml = bool(
                getattr(self.bus_manager, 'arxml_decoder', None)
                and getattr(self.bus_manager.arxml_decoder, 'loaded', False)
            )
            has_live_fibex = bool(
                getattr(self.bus_manager, 'fibex', None)
                and getattr(self.bus_manager.fibex, 'frames', None)
            )
        except Exception:
            has_live_can_dbcs = False
            has_live_arxml = False
            has_live_fibex = False

        if not has_live_can_dbcs and not has_live_arxml and not has_live_fibex:
            return ''

        log_dir = str(self.log_dir_resolver() or '').strip()
        if not log_dir:
            return ''
        raw_path = os.path.join(log_dir, os.path.basename(raw_mf4_filename))
        if not os.path.isfile(raw_path):
            return ''

        ts = time.strftime('%Y%m%d_%H%M%S', time.localtime(trigger_ts_ms / 1000.0))
        out_name = os.path.basename(f'{prefix}_{ts}_trace_decoded.mf4')
        out_path = os.path.join(log_dir, out_name)

        try:
            from mf4_decoded_export import MF4Decoder
        except Exception:
            from .mf4_decoded_export import MF4Decoder  # type: ignore

        pre_arxml_dec = getattr(self.bus_manager, 'arxml_decoder', None)
        pre_arxml_cat = None
        if pre_arxml_dec and getattr(pre_arxml_dec, 'loaded', False):
            try:
                from arxml_parser import get_active_catalog as _get_cat
                pre_arxml_cat = _get_cat()
            except Exception:
                pass

        decoder = MF4Decoder(
            raw_path,
            [],
            arxml_catalog=pre_arxml_cat,
            arxml_decoder=pre_arxml_dec,
            bus_manager=self.bus_manager,
        )
        decoder.export(
            out_path,
            signals=None,
            channel=None,
            start_s=None,
            end_s=None,
        )
        return out_name if os.path.isfile(out_path) else ''

    def _export_incident_bundle_zip(
        self,
        *,
        prefix: str,
        trigger_ts_ms: int,
        trace_mf4_filename: str,
        trace_raw_mf4_filename: str,
        scan_report_filename: str,
    ) -> str:
        """Create a ZIP bundle with trace MF4 + scan report.

        Returns the bundle basename (download via /api/logs/<name>) or ''.
        """
        if not (trace_mf4_filename or trace_raw_mf4_filename or scan_report_filename):
            return ''

        log_dir = str(self.log_dir_resolver() or '').strip()
        if not log_dir:
            return ''
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            return ''

        scan_dir = str(self.scan_report_dir_resolver() or '').strip()

        def _safe_join(base_dir: str, name: str) -> str:
            bn = os.path.basename(str(name or '').strip())
            if not bn:
                return ''
            p = os.path.join(base_dir, bn)
            return p if os.path.isfile(p) else ''

        candidates: List[Tuple[str, str]] = []
        p = _safe_join(log_dir, trace_mf4_filename)
        if p:
            candidates.append((os.path.basename(p), p))

        p = _safe_join(log_dir, trace_raw_mf4_filename)
        if p and os.path.basename(p) not in {n for (n, _p) in candidates}:
            candidates.append((os.path.basename(p), p))

        if scan_dir:
            p = _safe_join(scan_dir, scan_report_filename)
            if p and os.path.basename(p) not in {n for (n, _p) in candidates}:
                candidates.append((os.path.basename(p), p))

        if not candidates:
            return ''

        ts = time.strftime('%Y%m%d_%H%M%S', time.localtime(trigger_ts_ms / 1000.0))
        out_name = os.path.basename(f"{prefix}_{ts}_trace_ctx.zip")
        out_path = os.path.join(log_dir, out_name)

        try:
            import zipfile
            tmp = out_path + '.tmp'
            with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
                for arcname, src in candidates:
                    try:
                        z.write(src, arcname=arcname)
                    except Exception:
                        continue
            if os.path.isfile(tmp):
                os.replace(tmp, out_path)
            return out_name if os.path.isfile(out_path) else ''
        except Exception:
            try:
                tmp = out_path + '.tmp'
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return ''

    def _extract_lamp_snapshot(self, trace_frames: List[TraceFrame], mil_on_ts_ms: int) -> Dict[str, Any]:
        cfg = self._get_cfg()
        lamps = cfg.get('lamp_mappings') if isinstance(cfg, dict) else None
        if not isinstance(lamps, dict) or not trace_frames:
            return {}

        def _last_value(message: str, signal: str, *, before_ts_ms: int, after_ts_ms: int) -> Any:
            v = None
            for f in trace_frames:
                if f.ts_ms < before_ts_ms or f.ts_ms > after_ts_ms:
                    continue
                if not _messages_match(str(f.decoded_name or ''), message):
                    continue
                sigs = f.decoded_signals
                if not isinstance(sigs, dict):
                    continue
                if signal in sigs:
                    v = sigs.get(signal)
            return v

        out: Dict[str, Any] = {}
        for key in ['epc', 'gearbox']:
            m = lamps.get(key)
            if not isinstance(m, dict):
                continue
            msg = str(m.get('message') or '').strip()
            sig = str(m.get('signal') or '').strip()
            if not msg or not sig:
                continue
            val = _last_value(msg, sig, before_ts_ms=int(mil_on_ts_ms - 15000), after_ts_ms=int(mil_on_ts_ms + 15000))
            out[key] = {
                'message': msg,
                'signal': sig,
                'value_last_in_window': _json_safe_value(val),
            }
        return out
