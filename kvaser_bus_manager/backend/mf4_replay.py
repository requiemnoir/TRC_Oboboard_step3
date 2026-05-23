from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import re


@dataclass
class MF4ReplayStatus:
    running: bool = False
    file: str | None = None
    speed: float = 1.0
    loop: bool = False
    channel_mode: str = 'as_recorded'  # as_recorded|force
    force_channel: int | None = None
    start_s: float = 0.0
    end_s: float | None = None
    max_fps: float = 0.0
    frames_total: int | None = None
    frames_sent: int = 0
    enum_tracks: int | None = None
    started_at_ms: int | None = None
    stopped_at_ms: int | None = None
    last_error: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'running': bool(self.running),
            'file': self.file,
            'speed': float(self.speed or 0.0),
            'loop': bool(self.loop),
            'channel_mode': str(self.channel_mode or ''),
            'force_channel': self.force_channel,
            'start_s': float(self.start_s or 0.0),
            'end_s': self.end_s,
            'max_fps': float(self.max_fps or 0.0),
            'frames_total': self.frames_total,
            'frames_sent': int(self.frames_sent or 0),
            'enum_tracks': self.enum_tracks,
            'started_at_ms': self.started_at_ms,
            'stopped_at_ms': self.stopped_at_ms,
            'last_error': self.last_error,
        }


class MF4ReplayService:
    """Replay raw CAN frames from an MF4 file into the live pipeline.

    This is intended for "offline processing":
    - decode via preloaded DBCs
    - feed ComparisonEngine / AnomalyEngine listeners

        Supports raw-CAN MF4s that contain CAN identifier, DLC and 8 payload bytes.
        Channel naming varies by tool/vendor; we try common layouts including:
            - this project's layout: CAN_ID, DLC, DataByte0..7 (+ optional Channel, Flags)
            - Vector-like layouts: ID/DLC + DataBytes (array) and/or prefixed names
    """

    def __init__(
        self,
        *,
        bus_manager: Any,
        find_log_file: Callable[[str], Optional[str]],
        preload_dbcs: Optional[Callable[[], None]] = None,
    ):
        self._manager = bus_manager
        self._find_log_file = find_log_file
        self._preload_dbcs = preload_dbcs

        self._lock = threading.Lock()
        self._status = MF4ReplayStatus()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return self._status.to_dict()

    def stop(self) -> Dict[str, Any]:
        th = None
        with self._lock:
            self._stop_evt.set()
            th = self._thread

        if th and th.is_alive():
            try:
                th.join(timeout=2.0)
            except Exception:
                pass

        with self._lock:
            self._status.running = False
            self._status.stopped_at_ms = int(time.time() * 1000)
            self._thread = None
            return self._status.to_dict()

    def start(
        self,
        *,
        filename: str,
        speed: float = 1.0,
        loop: bool = False,
        channel_mode: str = 'as_recorded',
        force_channel: int | None = None,
        start_s: float = 0.0,
        end_s: float | None = None,
        max_fps: float = 0.0,
    ) -> Dict[str, Any]:
        fn = str(filename or '').strip()
        if not fn or not fn.lower().endswith('.mf4'):
            raise ValueError('invalid file')

        cm = str(channel_mode or '').strip().lower() or 'as_recorded'
        if cm not in {'as_recorded', 'force'}:
            cm = 'as_recorded'

        try:
            sp = float(speed)
        except Exception:
            sp = 1.0
        if sp < 0.0:
            sp = 0.0

        try:
            st = float(start_s)
        except Exception:
            st = 0.0
        if st < 0.0:
            st = 0.0

        en: float | None
        if end_s is None or str(end_s).strip() == '':
            en = None
        else:
            try:
                en = float(end_s)
            except Exception:
                en = None
            if en is not None and en <= st:
                en = None

        try:
            mf = float(max_fps)
        except Exception:
            mf = 0.0
        if mf < 0.0:
            mf = 0.0

        fc: int | None
        if cm == 'force':
            if force_channel is None:
                raise ValueError('force_channel required when channel_mode=force')
            try:
                fc = int(force_channel)
            except Exception:
                raise ValueError('invalid force_channel')
            if fc < 0 or fc > 64:
                raise ValueError('invalid force_channel')
        else:
            fc = None

        path = self._find_log_file(fn)
        if not path:
            raise FileNotFoundError('file not found')

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('replay already running')

            self._stop_evt.clear()
            self._status = MF4ReplayStatus(
                running=True,
                file=fn,
                speed=sp,
                loop=bool(loop),
                channel_mode=cm,
                force_channel=fc,
                start_s=st,
                end_s=en,
                max_fps=mf,
                frames_total=None,
                frames_sent=0,
                started_at_ms=int(time.time() * 1000),
                stopped_at_ms=None,
                last_error=None,
            )

            th = threading.Thread(target=self._worker, args=(path,), daemon=True)
            self._thread = th
            th.start()
            return self._status.to_dict()

    def _replay_measured(self, mdf) -> None:
        """New method: Replay decoded signals (measured data) by iterating the dataframe."""
        import time
        import pandas as pd

        with self._lock:
            start_s = float(self._status.start_s or 0.0)
            end_s = self._status.end_s
            speed = float(self._status.speed or 1.0)
            loop = self._status.loop
            channel_mode = self._status.channel_mode
            force_channel = self._status.force_channel
            max_fps = float(self._status.max_fps or 0.0)

        print(f"Signal Replay started from {start_s}s (speed={speed})")

        # For measured replay we must inject *real* CAN frames (id + payload) so the
        # system behaves like live interface traffic. We approximate this by encoding
        # payload bytes using the DBCs currently loaded for the selected channel.
        tgt_channel = 0
        try:
            if channel_mode == 'force' and force_channel is not None:
                tgt_channel = int(force_channel)
        except Exception:
            tgt_channel = 0

        # Build reverse mapping using loaded DBCs.
        #
        # The measured MF4 signal naming is often *not* a plain cantools signal name.
        # Common variants we must handle at replay time:
        #   - "Signal.<sig>"
        #   - "<MsgName>_<SigName>" (e.g. "ESP_21_CRC" where msg="ESP_21" sig="CRC")
        #   - plain "<SigName>"
        #
        # So we keep:
        #   - signal_name -> message (first match)
        #   - message_name -> message
        #   - per-message set of valid signal names
        #
        # Important: measured MF4s often store enum signals as *strings* (e.g.
        # MO_EPCL='EPCL_gelb_Stoerung'). cantools expects the *numeric* value when
        # encoding a CAN payload. We therefore build a reverse lookup from label->code
        # from the loaded DBC choices and coerce string/bytes measured values.
        signal_to_msgobj: dict[str, Any] = {}
        msgname_to_msgobj: dict[str, Any] = {}
        sigset_by_msgid: dict[int, set[str]] = {}
        enum_sig_to_msgobj: dict[str, Any] = {}
        msgnames_by_len: list[str] = []
        col_to_msgsig: dict[str, tuple[Any, str, str] | None] = {}
        mux_defaults_by_msgname: dict[str, dict[str, float | int]] = {}
        defaults_by_msgid: dict[int, dict[str, Any]] = {}
        state_by_msgid: dict[int, dict[str, Any]] = {}
        encode_fail_logged: set[str] = set()
        choices_inv_by_msgid_sig: dict[int, dict[str, dict[str, int]]] = {}

        def _norm_choice(s: str) -> str:
            try:
                return re.sub(r'[^a-z0-9]+', '', str(s or '').strip().lower())
            except Exception:
                return ''

        def _bytes_like_to_str(v: Any) -> Any:
            # numpy.bytes_ isn't always an instance of bytes; try tobytes() first.
            try:
                if hasattr(v, 'tobytes') and callable(getattr(v, 'tobytes')):
                    v = v.tobytes()
            except Exception:
                pass
            try:
                if isinstance(v, (bytes, bytearray)):
                    return v.decode('utf-8', errors='ignore')
            except Exception:
                pass
            return v

        def _coerce_measured_value(msg: Any, sig_name: str, sv: Any) -> Any:
            """Coerce a measured MF4 signal value into something cantools can encode."""
            sv = _bytes_like_to_str(sv)

            # Preserve bool/int/float directly.
            if isinstance(sv, bool):
                return 1 if sv else 0
            if isinstance(sv, (int, float)):
                return sv

            # Try to parse numeric strings.
            if isinstance(sv, str):
                sraw = sv.strip()
                if not sraw:
                    return sv
                # Handle representations like "b'EPCL_gelb_Stoerung'".
                if (sraw.startswith("b'") and sraw.endswith("'")) or (sraw.startswith('b"') and sraw.endswith('"')):
                    sraw = sraw[2:-1]

                try:
                    return float(sraw) if ('.' in sraw or 'e' in sraw.lower()) else int(sraw)
                except Exception:
                    pass

                # Enum/text -> numeric code via DBC choices (label -> int)
                try:
                    mid = id(msg)
                    inv_for_msg = choices_inv_by_msgid_sig.get(mid)
                    if inv_for_msg is None:
                        inv_for_msg = {}
                        for sig in getattr(msg, 'signals', []) or []:
                            try:
                                sn = str(getattr(sig, 'name', '') or '').strip()
                                if not sn:
                                    continue
                                choices = getattr(sig, 'choices', None)
                                if not isinstance(choices, dict) or not choices:
                                    continue
                                inv: dict[str, int] = {}
                                for code, label in choices.items():
                                    nk = _norm_choice(label)
                                    if nk:
                                        try:
                                            inv[nk] = int(code)
                                        except Exception:
                                            continue
                                if inv:
                                    inv_for_msg[sn] = inv
                            except Exception:
                                continue
                        choices_inv_by_msgid_sig[mid] = inv_for_msg

                    inv = inv_for_msg.get(sig_name) if isinstance(inv_for_msg, dict) else None
                    if isinstance(inv, dict) and inv:
                        nk = _norm_choice(sraw)
                        if nk in inv:
                            return int(inv[nk])
                except Exception:
                    pass

                # Heuristic fallback for common lamp enums if no choices found.
                sl = sraw.lower()
                try:
                    if any(tok in sl for tok in ['aus', 'off', 'inactive', 'disabled', 'kein']):
                        return 0
                    if 'rot' in sl or 'red' in sl:
                        return 3
                    if 'leistungsbeschraenkung' in sl or 'leistungsbeschränkung' in sl:
                        return 2
                    if 'gelb' in sl or 'yellow' in sl or 'stoerung' in sl or 'störung' in sl or 'fehler' in sl or 'fault' in sl or 'error' in sl:
                        return 1
                except Exception:
                    pass

                return sv

            # Last resort: try float conversion.
            try:
                return float(sv)
            except Exception:
                return sv

        # Preload enum/categorical signals via mdf.get() (change-point compressed) so we can
        # replay them even if iter_to_dataframe(raster=...) drops or NaNs string channels.
        # We keep this list small to avoid heavy MF4 reads on low RAM.
        enum_tracks: list[dict[str, Any]] = []
        enum_tracks_built = False
        enum_track_sigs: set[str] = set()
        try:
            loaders = []
            try:
                if getattr(self._manager, 'dbcs', None):
                    loaders = list(self._manager.dbcs.get(int(tgt_channel), []) or [])
            except Exception:
                loaders = []

            for loader in loaders:
                db = getattr(loader, 'db', None)
                if not db or not getattr(db, 'messages', None):
                    continue
                for msg in db.messages:
                    try:
                        mname = str(getattr(msg, 'name', '') or '').strip()
                    except Exception:
                        mname = ''
                    if not mname:
                        continue

                    if mname not in msgname_to_msgobj:
                        msgname_to_msgobj[mname] = msg

                    # Precompute multiplexer defaults (needed for encoding multiplexed messages)
                    if mname not in mux_defaults_by_msgname:
                        mux_defaults_by_msgname[mname] = {}

                    # Precompute defaults for all signals in this message (so encode doesn't KeyError)
                    mid = id(msg)
                    if mid not in defaults_by_msgid:
                        dvals: dict[str, Any] = {}
                        sigset_by_msgid[mid] = set()
                        for s in getattr(msg, 'signals', []) or []:
                            try:
                                sname = str(getattr(s, 'name', '') or '').strip()
                                if not sname:
                                    continue
                                sigset_by_msgid[mid].add(sname)
                                sv = getattr(s, 'initial', None)
                                if sv is None:
                                    sv = getattr(s, 'minimum', None)
                                if sv is None:
                                    # Prefer int 0 for integer-like signals
                                    sv = 0
                                dvals[sname] = sv
                            except Exception:
                                continue
                        defaults_by_msgid[mid] = dvals
                    for s in getattr(msg, 'signals', []) or []:
                        try:
                            sname = str(getattr(s, 'name', '') or '').strip()
                        except Exception:
                            sname = ''
                        if not sname:
                            continue
                        if bool(getattr(s, 'is_multiplexer', False)) and sname not in mux_defaults_by_msgname[mname]:
                            try:
                                sv = getattr(s, 'initial', None)
                                if sv is None:
                                    sv = getattr(s, 'minimum', None)
                                if sv is None:
                                    sv = 0
                                mux_defaults_by_msgname[mname][sname] = sv
                            except Exception:
                                pass

                        # Map signal -> message object (first match wins)
                        if sname not in signal_to_msgobj:
                            signal_to_msgobj[sname] = msg

                        # Track enum-like signals (choices present) for out-of-band replay.
                        try:
                            choices = getattr(s, 'choices', None)
                            if isinstance(choices, dict) and choices and sname not in enum_sig_to_msgobj:
                                enum_sig_to_msgobj[sname] = msg
                        except Exception:
                            pass

                    for sig in getattr(msg, 'signals', []) or []:
                        try:
                            sname = str(getattr(sig, 'name', '') or '').strip()
                        except Exception:
                            sname = ''
                        if sname and sname not in signal_to_msgobj:
                            signal_to_msgobj[sname] = msg

            # Cache message-name list (longest-first) for prefix matching.
            try:
                msgnames_by_len = sorted(msgname_to_msgobj.keys(), key=lambda x: len(x), reverse=True)
            except Exception:
                msgnames_by_len = list(msgname_to_msgobj.keys())

            print(
                f"Measured replay: channel={tgt_channel}, signals_mapped={len(signal_to_msgobj)}"
            )
        except Exception as e:
            print(f"Warning: could not build DBC mapping for measured replay: {e}")
        
        try:
             # Debug logging removed
             pass
        except: pass

        try:
            first_pass = True
            while first_pass or loop:
                first_pass = False
                if self._stop_evt.is_set():
                    break

                wall_start = time.time()
                last_emit_wall = time.monotonic()
                min_period = (1.0 / float(max_fps)) if (max_fps and max_fps > 0.0) else 0.0
                
                # Check 8.x iterator
                # chunk_ram_size in bytes. 10MB default.
                # raster=0.01 forces 100Hz resampling, avoiding expensive time union of 11k signals
                print("Creating iterator with raster=0.01...")
                selected_channels = None
                try:
                    # Huge measured MF4s can contain >10k channels; iterating all of them can take a very
                    # long time before any frames are emitted. For replay we only need channels that can
                    # be mapped to *some* DBC signal.
                    channels_db = getattr(mdf, 'channels_db', {}) or {}
                    avail = set(str(k) for k in channels_db.keys()) if isinstance(channels_db, dict) else set()

                    chans: list[Any] = []

                    def _add(name: str) -> None:
                        if not name:
                            return
                        if avail and name not in avail:
                            return
                        occ = None
                        try:
                            occ = channels_db.get(name) if isinstance(channels_db, dict) else None
                        except Exception:
                            occ = None

                        # If the same channel name exists multiple times in the MF4, passing the plain
                        # string name will raise an error. Disambiguate using (name, group, index).
                        if isinstance(occ, tuple) and len(occ) >= 1 and isinstance(occ[0], tuple):
                            try:
                                g, idx = occ[0]
                                if len(occ) > 1:
                                    chans.append((name, int(g), int(idx)))
                                    return
                            except Exception:
                                pass

                        chans.append(name)

                    for k in (signal_to_msgobj.keys() if isinstance(signal_to_msgobj, dict) else []):
                        kk = str(k or '').strip()
                        if not kk:
                            continue
                        _add(kk)
                        _add(f"Signal.{kk}")

                    selected_channels = chans if chans else None
                except Exception:
                    selected_channels = None

                iterator = mdf.iter_to_dataframe(
                    channels=selected_channels,
                    chunk_ram_size=10*1024*1024,
                    time_from_zero=True,
                    time_as_date=False,
                    raster=0.01,
                )
                print("Iterator created.")

                # Build enum tracks once per replay run (per while-loop pass).
                if not enum_tracks_built:
                    enum_tracks_built = True
                    try:
                        channels_db = getattr(mdf, 'channels_db', {}) or {}
                        avail = set(str(k) for k in channels_db.keys()) if isinstance(channels_db, dict) else set()
                    except Exception:
                        avail = set()

                    # Prefer a small cap; always include MO_EPCL if present.
                    try:
                        candidates = list(enum_sig_to_msgobj.keys())
                    except Exception:
                        candidates = []
                    try:
                        candidates = sorted(candidates, key=lambda x: (0 if str(x) == 'MO_EPCL' else 1, len(str(x))), reverse=False)
                    except Exception:
                        pass

                    max_enum = 24
                    loaded = 0
                    for sig_name in candidates:
                        if loaded >= max_enum:
                            break
                        try:
                            msg = enum_sig_to_msgobj.get(sig_name)
                            if msg is None:
                                continue
                            # Find a MF4 channel key for this signal.
                            key = None
                            if sig_name in avail:
                                key = sig_name
                            elif f"Signal.{sig_name}" in avail:
                                key = f"Signal.{sig_name}"
                            if not key:
                                continue

                            # Load the channel (best effort). This can be expensive; keep it limited.
                            try:
                                sig = mdf.get(key)
                            except Exception:
                                continue

                            ts = getattr(sig, 'timestamps', None)
                            smp = getattr(sig, 'samples', None)
                            if ts is None or smp is None:
                                continue

                            # Compress into change-points within the requested time window.
                            pts: list[tuple[float, Any]] = []
                            last = object()
                            try:
                                for tsv, sv in zip(ts, smp):
                                    try:
                                        tsv_f = float(tsv)
                                    except Exception:
                                        continue
                                    # Apply start/end bounds to reduce memory.
                                    if tsv_f < float(start_s or 0.0):
                                        continue
                                    if end_s is not None and tsv_f > float(end_s):
                                        break
                                    vv = _coerce_measured_value(msg, str(sig_name), sv)
                                    if vv != last:
                                        pts.append((tsv_f, vv))
                                        last = vv
                            except Exception:
                                continue

                            if not pts:
                                continue

                            try:
                                mname = str(getattr(msg, 'name', '') or '').strip()
                            except Exception:
                                mname = ''
                            enum_tracks.append({'mid': id(msg), 'msg': msg, 'mname': mname, 'sig': str(sig_name), 'pts': pts, 'idx': 0, 'last_emit_idx': None})
                            loaded += 1
                        except Exception:
                            continue

                    # Cache set of enum-track signal names so we can prefer the full-track state
                    # over any rastered dataframe values (which can be inconsistent at chunk boundaries
                    # for categorical channels).
                    try:
                        enum_track_sigs = {str(tr.get('sig') or '') for tr in (enum_tracks or []) if str(tr.get('sig') or '').strip()}
                    except Exception:
                        enum_track_sigs = set()

                    if enum_tracks:
                        try:
                            with self._lock:
                                try:
                                    self._status.enum_tracks = int(len(enum_tracks))
                                except Exception:
                                    self._status.enum_tracks = len(enum_tracks)
                        except Exception:
                            pass
                    else:
                        try:
                            with self._lock:
                                self._status.enum_tracks = 0
                        except Exception:
                            pass
                
                chunk_idx = 0
                for df in iterator:
                    chunk_idx += 1
                    print(f"Processing Chunk {chunk_idx}, shape={df.shape}")
                    if self._stop_evt.is_set(): break
                    if df.empty: continue
                    
                    if df.index[-1] < start_s: 
                        print(f"Skipping chunk (end={df.index[-1]} < start={start_s})")
                        continue
                    if df.index[0] < start_s:
                        df = df[df.index >= start_s]
                        
                    if end_s is not None:
                        if df.index[0] > end_s: break 
                        if df.index[-1] > end_s:
                            df = df[df.index <= end_s]
                    
                    if df.empty: continue

                    for timestamp, row in df.iterrows():
                        if self._stop_evt.is_set(): break

                        # Apply enum/categorical tracks up to current timestamp.
                        pending_enum_emit: set[int] = set()
                        try:
                            ts_now = float(timestamp)
                        except Exception:
                            ts_now = None
                        if ts_now is not None and enum_tracks:
                            for tr in enum_tracks:
                                try:
                                    pts = tr.get('pts') or []
                                    idx = int(tr.get('idx') or 0)
                                    prev_idx = idx
                                    # Advance to the latest point <= current time.
                                    while (idx + 1) < len(pts) and float(pts[idx + 1][0]) <= ts_now:
                                        idx += 1
                                    tr['idx'] = idx
                                    mid = int(tr.get('mid') or 0)
                                    sig_name = str(tr.get('sig') or '')
                                    if not sig_name or mid <= 0:
                                        continue
                                    if mid not in state_by_msgid:
                                        state_by_msgid[mid] = dict(defaults_by_msgid.get(mid, {}) or {})
                                    state_by_msgid[mid][sig_name] = pts[idx][1]

                                    # Emit at least once, and on each change point.
                                    last_emit_idx = tr.get('last_emit_idx')
                                    if last_emit_idx is None or int(last_emit_idx) != int(idx) or int(prev_idx) != int(idx):
                                        tr['last_emit_idx'] = int(idx)
                                        pending_enum_emit.add(mid)
                                except Exception:
                                    continue
                        
                        rel_time = float(timestamp) - start_s 
                        if rel_time < 0: continue
                            
                        elapsed_real = time.time() - wall_start
                        target_delay = (rel_time / speed) - elapsed_real
                        if target_delay > 0:
                            # Sleep in small chunks for responsive stop.
                            end_t = time.monotonic() + float(target_delay)
                            while not self._stop_evt.is_set():
                                left = end_t - time.monotonic()
                                if left <= 0:
                                    break
                                time.sleep(min(0.05, left))
                            
                        signals = row.to_dict()
                        signals = {k: v for k, v in signals.items() if pd.notna(v)}
                        if not signals: continue

                        def _resolve_measured_col(col: str):
                            """Resolve MF4 column name to (cantools_msg, signal_name, msg_name)"""
                            cached = col_to_msgsig.get(col)
                            if cached is not None or col in col_to_msgsig:
                                return cached

                            raw = str(col or '').strip()
                            if not raw:
                                col_to_msgsig[col] = None
                                return None

                            s = raw
                            if s.startswith('Signal.') and len(s) > 7:
                                s = s.split('.', 1)[1]

                            # 1) Direct signal name
                            msg = signal_to_msgobj.get(s) or signal_to_msgobj.get(raw)
                            if msg is not None:
                                try:
                                    mname = str(getattr(msg, 'name', '') or '').strip()
                                except Exception:
                                    mname = ''
                                if mname:
                                    col_to_msgsig[col] = (msg, s, mname)
                                    return col_to_msgsig[col]

                            # 2) Message-prefixed form: <MsgName>_<SigName>
                            try:
                                for mname in (msgnames_by_len or []):
                                    prefix = mname + '_'
                                    if not s.startswith(prefix):
                                        continue
                                    candidate = s[len(prefix):]
                                    msg2 = msgname_to_msgobj.get(mname)
                                    if msg2 is None:
                                        continue
                                    sigset = sigset_by_msgid.get(id(msg2)) or set()
                                    if candidate in sigset:
                                        col_to_msgsig[col] = (msg2, candidate, mname)
                                        return col_to_msgsig[col]
                                    if s in sigset:
                                        col_to_msgsig[col] = (msg2, s, mname)
                                        return col_to_msgsig[col]
                            except Exception:
                                pass

                            # 3) Dotted form: <MsgName>.<SigName>
                            if '.' in s:
                                try:
                                    mname, cand = s.split('.', 1)
                                    msg2 = msgname_to_msgobj.get(mname)
                                    if msg2 is not None:
                                        sigset = sigset_by_msgid.get(id(msg2)) or set()
                                        if cand in sigset:
                                            col_to_msgsig[col] = (msg2, cand, mname)
                                            return col_to_msgsig[col]
                                except Exception:
                                    pass

                            # 4) Fallback: last underscore split if prefix equals a message name
                            if '_' in s:
                                try:
                                    pref, cand = s.rsplit('_', 1)
                                    msg2 = msgname_to_msgobj.get(pref)
                                    if msg2 is not None:
                                        sigset = sigset_by_msgid.get(id(msg2)) or set()
                                        if cand in sigset:
                                            col_to_msgsig[col] = (msg2, cand, pref)
                                            return col_to_msgsig[col]
                                except Exception:
                                    pass

                            col_to_msgsig[col] = None
                            return None

                        # Group signals by their owning cantools message
                        by_msg: dict[int, dict[str, Any]] = {}
                        for col, sval in signals.items():
                            resolved = _resolve_measured_col(str(col))
                            if resolved is None:
                                continue
                            msg, sig_name, mname = resolved

                            mid = id(msg)
                            entry = by_msg.get(mid)
                            if entry is None:
                                entry = {'msg': msg, 'mname': mname, 'sigs': {}}
                                by_msg[mid] = entry
                            entry['sigs'][sig_name] = sval

                        # Ensure enum-only messages get emitted when enum tracks change.
                        if pending_enum_emit and enum_tracks:
                            try:
                                for tr in enum_tracks:
                                    try:
                                        mid = int(tr.get('mid') or 0)
                                        if mid <= 0 or mid not in pending_enum_emit:
                                            continue
                                        if mid in by_msg:
                                            continue
                                        msg = tr.get('msg')
                                        if msg is None:
                                            continue
                                        mname = str(tr.get('mname') or '').strip()
                                        by_msg[mid] = {'msg': msg, 'mname': mname, 'sigs': {}}
                                    except Exception:
                                        continue
                            except Exception:
                                pass

                        # Encode and inject mapped messages as real CAN frames (id + data)
                        for _, entry in by_msg.items():
                            msg = entry.get('msg') if isinstance(entry, dict) else None
                            msigs = entry.get('sigs') if isinstance(entry, dict) else None
                            mname = entry.get('mname') if isinstance(entry, dict) else ''
                            if msg is None or not isinstance(msigs, dict):
                                continue

                            try:
                                # Keep a running state per message: start from defaults once,
                                # then update with measured values as they appear.
                                mid = id(msg)
                                if mid not in state_by_msgid:
                                    state_by_msgid[mid] = dict(defaults_by_msgid.get(mid, {}) or {})
                                values = state_by_msgid[mid]
                                for sn, sv in (msigs or {}).items():
                                    # Prefer enum/categorical signals from the preloaded change-point tracks.
                                    # Rastered dataframe values for categorical channels can glitch and cause
                                    # spurious state reverts.
                                    try:
                                        if enum_track_sigs and str(sn) in enum_track_sigs:
                                            continue
                                    except Exception:
                                        pass
                                    # Cast pandas/numpy types to plain Python
                                    try:
                                        values[sn] = _coerce_measured_value(msg, str(sn), sv)
                                    except Exception:
                                        # Best-effort: keep old state if coercion fails.
                                        continue

                                # Ensure multiplexer signals have a value
                                for mux_name, mux_default in (mux_defaults_by_msgname.get(mname) or {}).items():
                                    if mux_name and mux_name not in values:
                                        values[mux_name] = mux_default

                                frame_id_raw = int(getattr(msg, 'frame_id', 0) or 0)
                                arb_id = int(frame_id_raw) & 0x1FFFFFFF
                                flags = 4 if ((frame_id_raw & 0x80000000) != 0 or arb_id > 0x7FF) else 0

                                encoded = msg.encode(values, scaling=True, padding=True, strict=False)
                                payload = [int(x) & 0xFF for x in list(encoded)]

                                # Inject through the normal CAN pipeline
                                self._manager.inject_frame(int(tgt_channel), int(arb_id), payload, flags=int(flags), frame_type="CAN")
                                with self._lock:
                                    self._status.frames_sent += 1

                                # Optional global throttle to avoid overwhelming Socket.IO.
                                # Measured MF4 replay can generate many messages per timestamp.
                                if min_period > 0.0:
                                    next_t = last_emit_wall + float(min_period)
                                    while not self._stop_evt.is_set():
                                        left = next_t - time.monotonic()
                                        if left <= 0:
                                            break
                                        time.sleep(min(0.05, left))
                                    last_emit_wall = max(next_t, time.monotonic())
                            except Exception as e:
                                # Log once per message name to avoid spamming
                                if mname not in encode_fail_logged:
                                    encode_fail_logged.add(mname)
                                    print(f"Measured replay encode failed for {mname}: {e}")
                                # If we can't encode (missing signals/out-of-range), skip.
                                # Do not inject placeholder/empty frames.
                                continue

                        sent = self._status.frames_sent
                        # Debug removed

                if self._stop_evt.is_set():
                    break
                if not loop:
                    print("Replay Finished")
                    break
                else:
                    print("Replay Loop")

        except Exception as e:
             print(f"Replay Error: {e}")
             self._status.last_error = str(e)
        finally:
            with self._lock:
                self._status.running = False
                self._status.stopped_at_ms = int(time.time() * 1000)

    def _worker(self, path: str) -> None:
        pass
        
        try:
            if callable(self._preload_dbcs):
                try:
                    self._preload_dbcs()
                except Exception:
                    pass

            try:
                import numpy as np
            except Exception as e:
                raise RuntimeError(f'missing dependency: numpy ({e})')

            try:
                import asammdf
            except Exception as e:
                raise RuntimeError(f'missing dependency: asammdf ({e})')

            def _norm(s: str) -> str:
                return re.sub(r'[^a-z0-9]+', '', str(s or '').lower())

            def _key_map(mdf) -> Dict[str, str]:
                try:
                    keys = list(getattr(mdf, 'channels_db', {}).keys())
                except Exception:
                    keys = []
                out: Dict[str, str] = {}
                for k in keys:
                    try:
                        ks = str(k)
                    except Exception:
                        continue
                    nk = _norm(ks)
                    if nk and nk not in out:
                        out[nk] = ks
                return out

            def _find_key(mdf, candidates: list[str]) -> str | None:
                try:
                    keys = set(str(k) for k in getattr(mdf, 'channels_db', {}).keys())
                except Exception:
                    keys = set()
                for c in candidates:
                    if c in keys:
                        return c
                km = _key_map(mdf)
                for c in candidates:
                    k = km.get(_norm(c))
                    if k:
                        return k
                return None

            def _get_sig(mdf, name: str):
                try:
                    return mdf.get(name)
                except Exception:
                    return None

            def _get_sig_any(mdf, candidates: list[str]):
                k = _find_key(mdf, candidates)
                return (_get_sig(mdf, k) if k else None), k

            def _payload_from_sig(np, data_sig):
                samp = getattr(data_sig, 'samples', [])
                arr = np.asarray(samp)

                # Common: 2D uint8 array (n, 8)
                if getattr(arr, 'ndim', 0) == 2 and arr.shape[1] >= 8:
                    cols = []
                    for i in range(8):
                        try:
                            cols.append(np.asarray(arr[:, i], dtype=np.uint8))
                        except Exception:
                            cols.append(np.asarray(arr[:, i]).astype(np.uint8, copy=False))
                    return cols

                # Sometimes: 1D object array of bytes/bytearray
                if getattr(arr, 'ndim', 0) == 1 and arr.size > 0:
                    try:
                        first = arr[0]
                    except Exception:
                        first = None
                    if isinstance(first, (bytes, bytearray)):
                        out = [np.zeros(arr.size, dtype=np.uint8) for _ in range(8)]
                        for idx in range(arr.size):
                            b = arr[idx]
                            if not isinstance(b, (bytes, bytearray)):
                                continue
                            for i in range(min(8, len(b))):
                                out[i][idx] = b[i]
                        return out

                raise ValueError('mf4 does not contain raw CAN payload channels')

            mdf = None
            try:
                try:
                    mdf = asammdf.MDF(path, memory='minimum')
                except Exception:
                    mdf = asammdf.MDF(path)

                can_id_sig, can_id_key = _get_sig_any(mdf, [
                    'CAN_ID', 'ID', 'Identifier',
                    'CAN_DataFrame.CAN_ID', 'CAN_DataFrame.ID', 'CAN_DataFrame.Identifier',
                    'CAN_Frame.CAN_ID', 'CAN_Frame.ID',
                ])
                dlc_sig, dlc_key = _get_sig_any(mdf, [
                    'DLC', 'Length', 'DataLength',
                    'CAN_DataFrame.DLC', 'CAN_DataFrame.Length', 'CAN_DataFrame.DataLength',
                ])
                if can_id_sig is None or dlc_sig is None:
                    # Fallback: Signal Replay Mode (measured data)
                    print("MF4Replay: Raw CAN channels missing. Switching to Signal Replay Mode.")
                    self._replay_measured(mdf)
                    return

                # Payload can be either up-to-64 separate signals or a single array/bytes signal.
                # FlexRay frames can be up to 254 bytes; CAN/CAN-FD up to 64.
                payload = None
                db_sigs = []
                for i in range(64):
                    s, _ = _get_sig_any(mdf, [
                        f'DataByte{i}',
                        f'CAN_DataFrame.DataByte{i}',
                        f'CAN_Frame.DataByte{i}',
                        f'DataBytes[{i}]', f'DataBytes{i}',
                        f'PayloadByte{i}', f'Byte{i}',
                    ])
                    if s is None:
                        break
                    db_sigs.append(s)
                if len(db_sigs) < 8:
                    db_sigs = []  # need at least 8 columns (CAN minimum)

                if db_sigs:
                    payload = [np.asarray(getattr(s, 'samples', []), dtype=np.uint8) for s in db_sigs]
                else:
                    data_sig, _ = _get_sig_any(mdf, [
                        'DataBytes', 'Data', 'Payload',
                        'CAN_DataFrame.DataBytes', 'CAN_DataFrame.Data', 'CAN_DataFrame.Payload',
                        'CAN_Frame.DataBytes', 'CAN_Frame.Data',
                    ])
                    if data_sig is None:
                        raise ValueError('mf4 does not contain raw CAN payload (no DataByte0..7 or DataBytes)')
                    payload = _payload_from_sig(np, data_sig)

                ch_sig, _ = _get_sig_any(mdf, ['Channel', 'BusChannel', 'CAN_DataFrame.Channel', 'CAN_DataFrame.BusChannel'])
                fl_sig, _ = _get_sig_any(mdf, ['Flags', 'CAN_DataFrame.Flags', 'CAN_Frame.Flags'])

                # Timestamps: avoid boolean checks on NumPy arrays (ambiguous truth value).
                t_src = getattr(can_id_sig, 'timestamps', None)
                if t_src is None:
                    t_src = getattr(dlc_sig, 'timestamps', None)
                if t_src is None:
                    t = np.asarray([], dtype=np.float64)
                else:
                    t = np.asarray(t_src, dtype=np.float64)
                can_id = np.asarray(getattr(can_id_sig, 'samples', []), dtype=np.uint32)
                dlc = np.asarray(getattr(dlc_sig, 'samples', []), dtype=np.uint16)

                if ch_sig is not None:
                    ch = np.asarray(getattr(ch_sig, 'samples', []), dtype=np.uint16)
                else:
                    ch = None

                if fl_sig is not None:
                    fl = np.asarray(getattr(fl_sig, 'samples', []), dtype=np.uint32)
                else:
                    fl = None

                n = int(min(
                    t.size,
                    can_id.size,
                    dlc.size,
                    *[arr.size for arr in payload],
                    (ch.size if ch is not None else 10**18),
                    (fl.size if fl is not None else 10**18),
                ))

                if n <= 0:
                    raise ValueError('mf4 raw table is empty')

                # Slice to common length
                t = t[:n]
                can_id = can_id[:n]
                dlc = dlc[:n]
                payload = [arr[:n] for arr in payload]
                if ch is not None:
                    ch = ch[:n]
                if fl is not None:
                    fl = fl[:n]

                # Sort by time if needed (best-effort)
                try:
                    order = np.argsort(t)
                    t = t[order]
                    can_id = can_id[order]
                    dlc = dlc[order]
                    payload = [arr[order] for arr in payload]
                    if ch is not None:
                        ch = ch[order]
                    if fl is not None:
                        fl = fl[order]
                except Exception:
                    pass

                t0 = float(t[0])
                t_rel = t - t0

                with self._lock:
                    start_s = float(self._status.start_s or 0.0)
                    end_s = self._status.end_s

                i0 = 0
                try:
                    if start_s > 0.0:
                        i0 = int(np.searchsorted(t_rel, start_s, side='left'))
                except Exception:
                    i0 = 0

                i1 = n
                try:
                    if end_s is not None:
                        i1 = int(np.searchsorted(t_rel, float(end_s), side='left'))
                        i1 = max(i0, min(i1, n))
                except Exception:
                    i1 = n

                total = max(0, i1 - i0)
                with self._lock:
                    self._status.frames_total = int(total)

                # Replay loop
                while not self._stop_evt.is_set():
                    sent = 0
                    last_wall = time.monotonic()
                    last_t = float(t_rel[i0]) if i0 < n else 0.0
                    min_period = (1.0 / float(max(self._status.max_fps, 0.0))) if float(self._status.max_fps or 0.0) > 0.0 else 0.0

                    for idx in range(i0, i1):
                        if self._stop_evt.is_set():
                            break

                        # Timing
                        dt = 0.0
                        try:
                            cur_t = float(t_rel[idx])
                            dt = max(0.0, cur_t - last_t)
                            last_t = cur_t
                        except Exception:
                            dt = 0.0

                        sp = float(self._status.speed or 0.0)
                        if sp > 0.0 and dt > 0.0:
                            target_sleep = dt / sp
                        else:
                            target_sleep = 0.0

                        if min_period > 0.0:
                            # Ensure we don't exceed max_fps even if timestamps are dense.
                            target_sleep = max(target_sleep, min_period)

                        if target_sleep > 0.0:
                            # Sleep in small chunks for responsive stop.
                            end_t = time.monotonic() + target_sleep
                            while not self._stop_evt.is_set():
                                left = end_t - time.monotonic()
                                if left <= 0:
                                    break
                                time.sleep(min(0.05, left))

                        # Build frame
                        try:
                            fid = int(can_id[idx])
                            frame_dlc = int(dlc[idx])
                            if frame_dlc < 0:
                                frame_dlc = 0
                            if frame_dlc > len(payload):
                                frame_dlc = len(payload)

                            if self._status.channel_mode == 'force' and self._status.force_channel is not None:
                                channel_id = int(self._status.force_channel)
                            else:
                                channel_id = int(ch[idx]) if ch is not None else 0

                            flags = int(fl[idx]) if fl is not None else 0

                            data = [int(payload[b][idx]) & 0xFF for b in range(frame_dlc)]

                            if 200 <= int(channel_id) < 250:
                                frame_type = 'FLEXRAY'
                            elif 150 <= int(channel_id) < 200:
                                frame_type = 'LIN'
                            else:
                                frame_type = 'CAN'

                            self._manager.inject_frame(channel_id, fid, data, flags=flags, frame_type=frame_type)
                            sent += 1
                        except Exception:
                            # Keep going on single-frame issues.
                            continue

                        # Periodically publish progress
                        now_wall = time.monotonic()
                        if (now_wall - last_wall) >= 0.5:
                            last_wall = now_wall
                            with self._lock:
                                self._status.frames_sent = int(self._status.frames_sent + sent)
                            sent = 0

                    with self._lock:
                        self._status.frames_sent = int(self._status.frames_sent + sent)

                    if self._stop_evt.is_set():
                        break

                    with self._lock:
                        do_loop = bool(self._status.loop)

                    if not do_loop:
                        break

                # finished
                with self._lock:
                    self._status.running = False
                    self._status.stopped_at_ms = int(time.time() * 1000)

            finally:
                try:
                    if mdf is not None:
                        mdf.close()
                except Exception:
                    pass

        except Exception as e:
            with self._lock:
                self._status.running = False
                self._status.stopped_at_ms = int(time.time() * 1000)
                self._status.last_error = str(e)
        finally:
            with self._lock:
                self._thread = None
                self._stop_evt.set()
