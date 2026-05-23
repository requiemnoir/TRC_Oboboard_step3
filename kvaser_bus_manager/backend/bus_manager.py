import threading
import time
import os
from queue import Queue, Empty, Full
LIVE_TRAFFIC_ENABLED = str(os.getenv('KBSM_LIVE_TRAFFIC_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
try:
    import canlib.canlib as canlib
except (ImportError, OSError, SystemExit):
    # Fallback for development without drivers
    class MockCanLib:
        canBITRATE_500K = -2
        canBITRATE_250K = -3
        canBITRATE_125K = -4
        canBITRATE_1M = -1
        canOPEN_ACCEPT_VIRTUAL = 0
        canDRIVER_NORMAL = 4
        class canError(Exception): pass
        class canNoMsg(Exception): pass
        
        def getNumberOfChannels(self): return 2
        
        class ChannelData:
            def __init__(self, i):
                self.channel_name = f"Virtual Channel {i}"
                self.card_upc_no = f"00-00000-00000-{i}"

        class MockChannel:
            def __init__(self, ch_num):
                self.ch_num = ch_num
            def setBusOutputControl(self, x): pass
            def setBusParams(self, x): pass
            def busOn(self): pass
            def busOff(self): pass
            def close(self): pass
            def read(self, timeout=0):
                # By default, the mock driver should NOT generate traffic.
                # Enable synthetic frames explicitly with KBSM_MOCK_CAN_TRAFFIC=1
                # for UI/dev demos.
                try:
                    enable = str(os.getenv('KBSM_MOCK_CAN_TRAFFIC', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
                except Exception:
                    enable = False

                if not enable:
                    raise MockCanLib.canNoMsg

                # When enabled, simulate no message most of the time to avoid flooding.
                import random
                if random.random() > 0.1:
                    raise MockCanLib.canNoMsg
                
                class MockFrame:
                    id = 0x123
                    data = [0, 1, 2, 3, 4, 5, 6, 7]
                    dlc = 8
                    flags = 0
                    time = int(time.time() * 1000)
                return MockFrame()
            def write(self, id, data, flags=0, dlc=None): pass

        def openChannel(self, ch, flags=0):
            return self.MockChannel(ch)

    canlib = MockCanLib()

from can_handler import CANHandler
try:
    # True when canlib drivers are missing and we fall back to a mock.
    from can_handler import IS_MOCK as CAN_DRIVER_IS_MOCK
except Exception:
    CAN_DRIVER_IS_MOCK = False
from flexray_handler import FlexRayHandler
from dbc_loader import DBCLoader
from fibex_loader import FibexLoader
from logger import BusLogger
from diagnostics import Diagnostics
try:
    from arxml_decoder import ArxmlDecoder
except ImportError:
    ArxmlDecoder = None  # type: ignore

class BusManager:
    def __init__(self, socketio, logger):
        self.socketio = socketio
        self.running = False
        self.handlers = {} # channel_id -> handler
        # channel_id -> list[DBCLoader]
        self.dbcs = {}
        self.dbc = DBCLoader()  # generic loader used by /api/upload_dbc validation
        self.fibex = FibexLoader()
        self.logger = logger
        self.diag = Diagnostics()
        self.last_stats = None
        self.thread = None
        self.lock = threading.Lock()
        self.listeners = []
        self.bitrate_by_channel = {}
        # Optional callable: (bus_type: str, channel_id: int) -> source_id | None
        # Used by monitoring/comparison features.
        self.source_id_resolver = None
        # Map DBC filename → source_id for mirror catch-all channels (ch 99).
        # Populated by _load_mirror_dbcs() in app.py so inject_frame can
        # pick the correct source_id based on which DBC decoded the frame.
        self.mirror_dbc_source_map: dict = {}
        # Opt-in ECU simulation for dev/testing without a vehicle.
        # Enable via env var KBSM_SIM_ECU=1 or by passing {"simulate_ecu": true} to /api/start.
        self.simulate_ecu = str(os.getenv('KBSM_SIM_ECU', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
        # ARXML-based decoder (covers ALL buses, fallback when DBC fails)
        self.arxml_decoder = ArxmlDecoder() if ArxmlDecoder else None
        # Mirror virtual channel → bus hint for ARXML disambiguation
        self._channel_bus_hint = {
            100: 'CCAN', 200: 'CCAN',
            101: 'HCAN',
            102: 'DiagCAN',
            103: 'CAN4',
            104: 'ECAN',
            105: 'ICAN',
            106: 'KCAN',
        }
        self._ui_emit_interval_s = max(0.0, float(os.getenv('KBSM_UI_BUS_EMIT_INTERVAL_S', '0.02') or 0.02))
        self._ui_emit_batch_max = max(1, int(os.getenv('KBSM_UI_BUS_EMIT_BATCH_MAX', '256') or 256))
        self._ui_emit_queue_max = max(self._ui_emit_batch_max, int(os.getenv('KBSM_UI_BUS_QUEUE_MAX', '4096') or 4096))
        # Modalità "latest snapshot" lato UI Live Traffic: dedupe per
        # (channel, arb_id) nel batch prima dell'emit socket.io.
        # Con KBSM_UI_BUS_EMIT_INTERVAL_S=1.0 e KBSM_UI_BUS_DEDUPE_LATEST=1
        # la UI riceve ~N_unique_ids righe/sec (~1000) invece di
        # ~36k frame/sec.  Il log MF4 NON è influenzato.
        self._ui_dedupe_latest = str(os.getenv('KBSM_UI_BUS_DEDUPE_LATEST', '0')).strip().lower() in {'1','true','yes','on'}
        self._pending_ui_frames = []
        self._ui_emit_lock = threading.Lock()
        self._ui_emit_timer = None
        self._ui_emit_queue_high_watermark = 0
        self._timeline_live_enabled = False
        self._ui_stream_stats_lock = threading.Lock()
        self._ui_stream_stats = {
            'since_ts': float(time.time()),
            'live': self._new_stream_sink_stats(),
            'timeline': self._new_stream_sink_stats(),
        }
        self._ui_priority_flexray_slot_ids = self._parse_int_csv_env('KBSM_UI_PRIORITY_FLEXRAY_SLOTS', '8')
        self._ui_priority_signal_names = self._parse_str_csv_env(
            'KBSM_UI_PRIORITY_SIGNALS',
            'ZAS_Kl_15,ZAS_Kl_S,ZAS_Kl_X',
        )
        self._ui_priority_message_tokens = self._parse_str_csv_env(
            'KBSM_UI_PRIORITY_MESSAGE_TOKENS',
            'Klemmen_Status_01',
        )
        self._ui_priority_all_flexray = str(
            os.getenv('KBSM_UI_PRIORITY_ALL_FLEXRAY', '1')
        ).strip().lower() in {'1', 'true', 'yes', 'on'}

        # --- Capture pipeline (decoupled reader/processor) ---------------------
        # Each handler is drained by a dedicated reader thread that pushes raw
        # frames into a shared queue. The main bus loop consumes from this queue
        # and performs decode/log/UI emit. This prevents the canlib driver RX
        # buffer from overflowing while Python-side processing (DBC decoding,
        # MF4 buffering, websocket emits, …) is still running, which used to
        # cause periodic 50–600 ms holes in recorded MF4 streams (e.g.
        # MO_Drehzahl_01 on FlexRay slot 32).
        try:
            self._rx_queue_max = int(os.getenv('KBSM_RX_QUEUE_MAX', '131072') or 131072)
        except Exception:
            self._rx_queue_max = 65536
        self._rx_queue_max = max(1024, min(self._rx_queue_max, 1_000_000))
        try:
            self._rx_drain_batch = int(os.getenv('KBSM_RX_DRAIN_BATCH', '256') or 256)
        except Exception:
            self._rx_drain_batch = 256
        self._rx_drain_batch = max(1, min(self._rx_drain_batch, 65536))
        self._rx_queue: Queue = Queue(maxsize=self._rx_queue_max)
        self._reader_threads: dict = {}
        self._reader_stop_event = threading.Event()
        self._rx_dropped_total = 0
        self._rx_queue_high_watermark = 0
        self._rx_stats_lock = threading.Lock()

    @staticmethod
    def _parse_int_csv_env(name: str, default: str) -> set[int]:
        raw = str(os.getenv(name, default) or default)
        out: set[int] = set()
        for part in raw.split(','):
            token = str(part or '').strip()
            if not token:
                continue
            try:
                out.add(int(token))
            except Exception:
                continue
        return out

    @staticmethod
    def _parse_str_csv_env(name: str, default: str) -> tuple[str, ...]:
        raw = str(os.getenv(name, default) or default)
        return tuple(token for token in (str(part or '').strip() for part in raw.split(',')) if token)

    @staticmethod
    def _new_stream_sink_stats() -> dict:
        return {
            'offered_frames': 0,
            'emitted_frames': 0,
            'dropped_frames': 0,
            'immediate_frames': 0,
            'batches': 0,
            'offered_by_type': {},
            'emitted_by_type': {},
            'dropped_by_type': {},
        }

    @staticmethod
    def _normalize_frame_type(frame_type) -> str:
        try:
            ft = str(frame_type or 'CAN').strip().upper()
        except Exception:
            ft = 'CAN'
        if ft in {'FLEX', 'FR'}:
            return 'FLEXRAY'
        if ft == 'CANFD':
            return 'CAN-FD'
        return ft or 'CAN'

    @staticmethod
    def _increment_type_counter(bucket: dict, frame_type: str, count: int = 1) -> None:
        key = str(frame_type or 'CAN').strip().upper() or 'CAN'
        bucket[key] = int(bucket.get(key, 0)) + int(count)

    def _record_stream_offered(self, entry: dict) -> None:
        frame_type = str(entry.get('type') or 'CAN')
        with self._ui_stream_stats_lock:
            for sink in ('live', 'timeline'):
                if not bool(entry.get(sink)):
                    continue
                stats = self._ui_stream_stats[sink]
                stats['offered_frames'] = int(stats.get('offered_frames', 0)) + 1
                self._increment_type_counter(stats.get('offered_by_type', {}), frame_type, 1)

    def _record_stream_dropped(self, entry: dict) -> None:
        frame_type = str(entry.get('type') or 'CAN')
        with self._ui_stream_stats_lock:
            for sink in ('live', 'timeline'):
                if not bool(entry.get(sink)):
                    continue
                stats = self._ui_stream_stats[sink]
                stats['dropped_frames'] = int(stats.get('dropped_frames', 0)) + 1
                self._increment_type_counter(stats.get('dropped_by_type', {}), frame_type, 1)

    def _record_stream_emitted(self, sink: str, entries: list[dict], *, immediate: bool = False) -> None:
        if sink not in {'live', 'timeline'} or not entries:
            return
        with self._ui_stream_stats_lock:
            stats = self._ui_stream_stats[sink]
            stats['batches'] = int(stats.get('batches', 0)) + 1
            stats['emitted_frames'] = int(stats.get('emitted_frames', 0)) + len(entries)
            if immediate:
                stats['immediate_frames'] = int(stats.get('immediate_frames', 0)) + len(entries)
            emitted_by_type = stats.get('emitted_by_type', {})
            for entry in entries:
                self._increment_type_counter(emitted_by_type, str(entry.get('type') or 'CAN'), 1)

    def _build_ui_entry(self, frame, *, live_enabled: bool, timeline_enabled: bool) -> dict:
        return {
            'frame': frame,
            'live': bool(live_enabled),
            'timeline': bool(timeline_enabled),
            'type': self._normalize_frame_type(frame.get('type') if isinstance(frame, dict) else None),
        }

    def _should_emit_immediately(self, frame) -> bool:
        if not isinstance(frame, dict):
            return False
        if self._normalize_frame_type(frame.get('type')) != 'FLEXRAY':
            return False
        if self._ui_priority_all_flexray:
            return True

        try:
            if int(frame.get('id', -1)) in self._ui_priority_flexray_slot_ids:
                return True
        except Exception:
            pass

        decoded = frame.get('decoded') if isinstance(frame.get('decoded'), dict) else None
        if not isinstance(decoded, dict):
            return False

        try:
            msg_name = str(decoded.get('name') or '')
        except Exception:
            msg_name = ''
        for token in self._ui_priority_message_tokens:
            if token and token in msg_name:
                return True

        sigs = decoded.get('signals') if isinstance(decoded.get('signals'), dict) else {}
        if isinstance(sigs, dict):
            for sig_name in self._ui_priority_signal_names:
                if sig_name and sig_name in sigs:
                    return True
        return False

    def _emit_ui_entries(self, entries: list[dict], *, immediate: bool = False) -> None:
        if not self.socketio or not entries:
            return

        live_entries = [entry for entry in entries if bool(entry.get('live'))]
        timeline_entries = [entry for entry in entries if bool(entry.get('timeline'))]

        if live_entries:
            live_batch = [entry.get('frame') for entry in live_entries]
            try:
                if len(live_batch) == 1:
                    self.socketio.emit('bus_data', live_batch[0])
                else:
                    self.socketio.emit('bus_data_batch', live_batch)
                self._record_stream_emitted('live', live_entries, immediate=immediate)
            except Exception:
                pass

        if timeline_entries:
            timeline_batch = [entry.get('frame') for entry in timeline_entries]
            try:
                if len(timeline_batch) == 1:
                    self.socketio.emit('timeline_bus_data', timeline_batch[0])
                else:
                    self.socketio.emit('timeline_bus_data_batch', timeline_batch)
                self._record_stream_emitted('timeline', timeline_entries, immediate=immediate)
            except Exception:
                pass

    def get_ui_stream_stats(self) -> dict:
        with self._ui_emit_lock:
            pending = len(self._pending_ui_frames)
            high_watermark = int(self._ui_emit_queue_high_watermark)
        with self._ui_stream_stats_lock:
            stats = {
                'since_ts': float(self._ui_stream_stats.get('since_ts', time.time())),
                'live': dict(self._ui_stream_stats.get('live', {})),
                'timeline': dict(self._ui_stream_stats.get('timeline', {})),
            }

        for sink in ('live', 'timeline'):
            sink_stats = stats.get(sink, {}) if isinstance(stats.get(sink), dict) else {}
            offered = int(sink_stats.get('offered_frames', 0) or 0)
            emitted = int(sink_stats.get('emitted_frames', 0) or 0)
            dropped = int(sink_stats.get('dropped_frames', 0) or 0)
            sink_stats['sampling_ratio'] = round((float(emitted) / float(offered)) if offered > 0 else 1.0, 4)
            sink_stats['drop_ratio'] = round((float(dropped) / float(offered)) if offered > 0 else 0.0, 4)
            stats[sink] = sink_stats

        stats['queue'] = {
            'pending_frames': pending,
            'high_watermark': high_watermark,
            'max_frames': int(self._ui_emit_queue_max),
        }
        stats['config'] = {
            'emit_interval_s': float(self._ui_emit_interval_s),
            'batch_max': int(self._ui_emit_batch_max),
            'queue_max': int(self._ui_emit_queue_max),
            'dedupe_latest': bool(self._ui_dedupe_latest),
            'priority_all_flexray': bool(self._ui_priority_all_flexray),
            'priority_flexray_slots': sorted(int(x) for x in self._ui_priority_flexray_slot_ids),
            'priority_signal_names': list(self._ui_priority_signal_names),
            'priority_message_tokens': list(self._ui_priority_message_tokens),
        }
        return stats

    def set_timeline_live_enabled(self, enabled: bool) -> None:
        with self._ui_emit_lock:
            self._timeline_live_enabled = bool(enabled)

    def is_timeline_live_enabled(self) -> bool:
        with self._ui_emit_lock:
            return bool(self._timeline_live_enabled)

    def _flush_ui_frames(self):
        batch = []
        with self._ui_emit_lock:
            self._ui_emit_timer = None
            if not self._pending_ui_frames:
                return
            if (not LIVE_TRAFFIC_ENABLED) and (not self._timeline_live_enabled):
                self._pending_ui_frames = []
                return
            batch = self._pending_ui_frames
            self._pending_ui_frames = []

        # Modalità "latest snapshot": per ogni (channel, arb_id) tieni
        # solo il frame più recente del batch. Riduce drasticamente il
        # carico verso il browser senza perdere informazione (la UI
        # Live Traffic mostra il valore corrente di ogni signal, non
        # uno scroll esaustivo). Il logger MF4 resta INTATTO: questa
        # logica agisce solo sull'emit socket.io.
        if self._ui_dedupe_latest and len(batch) > 1:
            seen = {}
            for entry in batch:
                fr = entry.get('frame') or {}
                key = (int(fr.get('channel', 0)), int(fr.get('id', 0)), str(fr.get('type', 'CAN')))
                seen[key] = entry   # ultimo per key vince (insertion order)
            batch = list(seen.values())

        self._emit_ui_entries(batch, immediate=False)

    def _schedule_ui_flush_locked(self):
        if self._ui_emit_timer is not None:
            return
        timer = threading.Timer(self._ui_emit_interval_s, self._flush_ui_frames)
        timer.daemon = True
        self._ui_emit_timer = timer
        timer.start()

    def _emit_ui_frame(self, frame):
        if not self.socketio:
            return
        live_enabled = bool(LIVE_TRAFFIC_ENABLED)
        timeline_enabled = self.is_timeline_live_enabled()
        if (not live_enabled) and (not timeline_enabled):
            return

        entry = self._build_ui_entry(frame, live_enabled=live_enabled, timeline_enabled=timeline_enabled)
        self._record_stream_offered(entry)

        if self._should_emit_immediately(frame):
            self._emit_ui_entries([entry], immediate=True)
            return

        if self._ui_emit_interval_s <= 0:
            self._emit_ui_entries([entry], immediate=False)
            return

        flush_now = False
        timer = None
        dropped_entries = []
        with self._ui_emit_lock:
            while len(self._pending_ui_frames) >= self._ui_emit_queue_max:
                try:
                    dropped_entries.append(self._pending_ui_frames.pop(0))
                except Exception:
                    break
            self._pending_ui_frames.append(entry)
            if len(self._pending_ui_frames) > self._ui_emit_queue_high_watermark:
                self._ui_emit_queue_high_watermark = len(self._pending_ui_frames)
            if len(self._pending_ui_frames) >= self._ui_emit_batch_max:
                flush_now = True
                timer = self._ui_emit_timer
                self._ui_emit_timer = None
            else:
                self._schedule_ui_flush_locked()

        for dropped_entry in dropped_entries:
            self._record_stream_dropped(dropped_entry)

        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if flush_now:
            self._flush_ui_frames()

    def decode_frame(self, channel_id, arb_id, data, flags=0, frame_type="CAN"):
        """Decode a raw frame using the full live pipeline (DBC → ARXML → FIBEX).

        Returns the decoded dict (``{"name": ..., "signals": {...}}``) or
        ``None``.  Pure decode — no logging, no listener notification, no UI
        emission.
        """
        decoded = None
        fid = int(arb_id)
        raw_data = list(data)
        ft = str(frame_type or '').strip().upper() or 'CAN'

        with self.lock:
            loaders = self.dbcs.get(channel_id)

        # --- DBC decode ---
        if loaders and ft == 'CAN':
            for loader in loaders:
                try:
                    decoded = loader.decode(fid, raw_data)
                except Exception:
                    decoded = None
                if decoded:
                    break

        # --- ARXML CAN fallback (with bus hint) ---
        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and ft == 'CAN':
            try:
                bus_hint = self._channel_bus_hint.get(int(channel_id), '')
                if bus_hint:
                    decoded = self.arxml_decoder.decode_with_bus(
                        fid, raw_data, bus_hint=bus_hint)
                else:
                    decoded = self.arxml_decoder.decode(fid, raw_data)
            except Exception:
                decoded = None

        # --- ARXML LIN fallback ---
        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and ft == 'LIN':
            try:
                decoded = self.arxml_decoder.decode_lin(fid, raw_data)
            except Exception:
                decoded = None

        # --- FlexRay decode via FIBEX ---
        if decoded is None and ft in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                cyc = None
                try:
                    cyc = int(flags) & 0x3F
                except Exception:
                    cyc = None
                decoded = self.fibex.decode(fid, raw_data, cycle=cyc)
            except Exception:
                decoded = None

        # --- ARXML FlexRay fallback ---
        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and ft in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                decoded = self.arxml_decoder.decode_flexray(fid, raw_data)
            except Exception:
                decoded = None

        return decoded

    def inject_frame(self, channel_id, arb_id, data, flags=0, frame_type="CAN", capture_origin=None):
        """Inject a synthetic frame into the normal processing pipeline."""
        frame = {
            "id": int(arb_id),
            "data": list(data),
            "dlc": int(len(data)),
            "flags": int(flags),
            "timestamp": int(time.time() * 1000),
            "type": frame_type,
            "channel": int(channel_id),
        }

        try:
            origin = str(capture_origin or '').strip().lower()
        except Exception:
            origin = ''
        if origin:
            frame['capture_origin'] = origin

        # Monitoring metadata (must be JSON-serializable for Socket.IO)
        try:
            if callable(self.source_id_resolver):
                ft = str(frame_type or '').strip().upper()
                bus_type = 'FLEXRAY' if ft in {'FLEXRAY', 'FLEX', 'FR'} else ('LIN' if ft == 'LIN' else 'CAN')
                frame['source_id'] = self.source_id_resolver(bus_type, int(channel_id))
        except Exception:
            pass

        # Decode
        decoded = None
        with self.lock:
            loaders = self.dbcs.get(channel_id)
            listeners_copy = self.listeners[:]
        matched_loader = None
        if loaders and str(frame_type or '').upper() == 'CAN':
            for loader in loaders:
                try:
                    decoded = loader.decode(frame['id'], frame['data'])
                except Exception:
                    decoded = None
                if decoded:
                    matched_loader = loader
                    break

        # For mirror catch-all channel (99), re-resolve source_id based on the
        # DBC that actually decoded the frame.  This ensures the comparison
        # engine matches rules to the correct physical source.
        if decoded and matched_loader and int(channel_id) == 99 and self.mirror_dbc_source_map:
            fn = getattr(matched_loader, 'filename', None)
            if fn and fn in self.mirror_dbc_source_map:
                frame['source_id'] = self.mirror_dbc_source_map[fn]

        # ARXML fallback: if DBC decode failed, try ARXML-based decoder.
        # This covers buses where DBC is missing or fails to load (e.g. HCAN).
        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and str(frame_type or '').upper() == 'CAN':
            try:
                bus_hint = self._channel_bus_hint.get(int(channel_id), '')
                if bus_hint:
                    decoded = self.arxml_decoder.decode_with_bus(
                        frame['id'], frame['data'], bus_hint=bus_hint)
                else:
                    decoded = self.arxml_decoder.decode(
                        frame['id'], frame['data'])
            except Exception:
                decoded = None

        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and str(frame_type or '').upper() == 'LIN':
            try:
                decoded = self.arxml_decoder.decode_lin(frame['id'], frame['data'])
            except Exception:
                decoded = None

        # FlexRay decode (best-effort) via FIBEX
        if decoded is None and str(frame_type or '').upper() in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                # FlexRay cycle counters are 6-bit values (0..63). Mirror frames
                # pass the cycle directly; direct readers may pack it into flags.
                cyc = None
                try:
                    cyc = int(flags) & 0x3F
                except Exception:
                    cyc = None
                decoded = self.fibex.decode(frame['id'], frame['data'], cycle=cyc)
            except Exception:
                decoded = None

        # ARXML FlexRay fallback: if FIBEX decode failed, try ARXML decoder.
        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and str(frame_type or '').upper() in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                decoded = self.arxml_decoder.decode_flexray(
                    frame['id'], frame['data'])
            except Exception:
                decoded = None

        if decoded:
            frame['decoded'] = decoded

        # Update diagnostics + log
        try:
            self.diag.update(frame)
        except Exception:
            pass
        try:
            self.logger.log(frame)
        except Exception:
            pass

        # Notify listeners
        for listener in listeners_copy:
            try:
                listener(frame)
            except Exception:
                pass
        
        # Emit to UI with small batching to avoid per-frame websocket overhead.
        self._emit_ui_frame(frame)

    def inject_decoded_frame(self, frame):
        """Inject a pre-decoded frame (e.g. from signal-based MF4 replay)."""
        # Ensure timestamp (ms)
        if 'timestamp' not in frame:
            frame['timestamp'] = int(time.time() * 1000)

        # Ensure a JSON-safe source_id if possible
        try:
            if 'source_id' not in frame and callable(self.source_id_resolver):
                ft = str(frame.get('type') or 'CAN').strip().upper()
                bus_type = 'FLEXRAY' if ft in {'FLEXRAY', 'FLEX', 'FR'} else 'CAN'
                ch_raw = frame.get('channel')
                try:
                    channel = int(ch_raw)
                except Exception:
                    channel = 0
                frame['source_id'] = self.source_id_resolver(bus_type, channel)
        except Exception:
            pass
            
        with self.lock:
            listeners_copy = self.listeners[:]

        for listener in listeners_copy:
            try:
                listener(frame)
            except Exception:
                pass
        
        # Also emit to UI for liveliness
        self._emit_ui_frame(frame)

    def _simulate_obd_responses(self, tx_id, data):
        """Return list of (rx_id, rx_data) for a given OBD/TesterPresent request."""
        try:
            if not data or len(data) < 2:
                return []

            # Tester Present / Discovery (0x3E)
            if len(data) >= 3 and data[1] == 0x3E:
                # Respond on physical RX id when possible; otherwise default to 0x7E8.
                rx_id = (int(tx_id) + 8) if (0x700 <= int(tx_id) <= 0x7F7) else 0x7E8
                rx_data = [0x02, 0x7E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                return [(rx_id, rx_data)]

            # OBD functional/physical addressing for modes
            mode = int(data[1])
            pid = int(data[2]) if len(data) >= 3 else None

            # Mode 01 PID 00 - Supported PIDs
            if mode == 0x01 and pid == 0x00:
                rx_data = [0x06, 0x41, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]
                return [(0x7E8, rx_data)]

            # Mode 01 PID 0C - RPM
            if mode == 0x01 and pid == 0x0C:
                rpm = 2400
                raw = int(rpm * 4)
                a = (raw >> 8) & 0xFF
                b = raw & 0xFF
                rx_data = [0x04, 0x41, 0x0C, a, b, 0x00, 0x00, 0x00]
                return [(0x7E8, rx_data)]

            # Mode 01 PID 0D - Speed
            if mode == 0x01 and pid == 0x0D:
                speed = 88
                rx_data = [0x03, 0x41, 0x0D, speed, 0x00, 0x00, 0x00, 0x00]
                return [(0x7E8, rx_data)]

            # Mode 03 - Read DTCs
            if mode == 0x03:
                rx_data = [0x04, 0x43, 0x02, 0x01, 0x23, 0x00, 0x00, 0x00]
                return [(0x7E8, rx_data)]

            # Mode 04 - Clear DTCs
            if mode == 0x04:
                rx_data = [0x02, 0x44, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
                return [(0x7E8, rx_data)]

            return []
        except Exception:
            return []

    def add_listener(self, callback):
        with self.lock:
            if callback not in self.listeners:
                self.listeners.append(callback)

    def remove_listener(self, callback):
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)

    def send_message(self, channel_id, arb_id, data, is_extended=False):
        handler = None
        flags = 4 if is_extended else 0
        with self.lock:
            handler = self.handlers.get(channel_id)

        ok = False
        if handler:
            # NOTE: CANHandler.write signature is (arb_id, data, dlc=None, flags=0).
            # Pass data by keyword to avoid accidentally binding it to 'dlc'.
            ok = bool(handler.write(arb_id, data=data, flags=flags))

        # Inject simulated ECU responses when enabled.
        # This allows ScanTools/Live Data to work without a real vehicle.
        if self.simulate_ecu and not is_extended:
            tx_id = int(arb_id)
            if tx_id == 0x7DF or (0x7E0 <= tx_id <= 0x7E7) or (0x700 <= tx_id <= 0x7F7):
                for rx_id, rx_data in self._simulate_obd_responses(tx_id, list(data)):
                    self.inject_frame(channel_id, rx_id, rx_data, flags=0, frame_type="CAN")

        return ok

    def list_interfaces(self):
        try:
            num_channels = canlib.getNumberOfChannels()
        except:
            return []
            
        interfaces = []
        for i in range(num_channels):
            try:
                ch_data = canlib.ChannelData(i)
                name = ch_data.channel_name
                card_upc = ch_data.card_upc_no
                interfaces.append({"id": i, "name": name, "upc": str(card_upc)})
            except:
                continue
        return interfaces

    def can_driver_is_mock(self) -> bool:
        """Return True if CAN I/O is using a mock driver (not real vehicle CAN)."""
        try:
            return bool(CAN_DRIVER_IS_MOCK)
        except Exception:
            return False

    def list_dbcs(self, dbc_folder):
        import os
        return [f for f in os.listdir(dbc_folder) if f.endswith('.dbc')]

    def preload_dbcs(self, channels):
        """Load per-channel DBCs without starting/opening the bus.

        This is useful for simulation/injection modes where we still want
        Live Traffic to show decoded message names even if no CAN hardware
        is available.
        """
        loaded = 0
        new_dbcs: dict = {}
        for ch_conf in channels or []:
            if not isinstance(ch_conf, dict):
                continue
            try:
                ch_id = int(ch_conf.get('id'))
            except Exception:
                continue
            dbc_paths = []
            if isinstance(ch_conf.get('dbcs'), list):
                dbc_paths = [p for p in ch_conf.get('dbcs') if p]
            else:
                dbc_paths = [ch_conf.get('dbc')] if ch_conf.get('dbc') else []

            loaders = []
            for dbc_path in dbc_paths:
                try:
                    loader = DBCLoader()
                    if loader.load(dbc_path):
                        loaders.append(loader)
                except Exception:
                    continue
            if loaders:
                # Accumulate loaders for the same channel ID (e.g. mirror ch 99
                # needs CCAN + HCAN + DiagCAN) instead of overwriting.
                if ch_id in new_dbcs:
                    new_dbcs[ch_id].extend(loaders)
                else:
                    new_dbcs[ch_id] = loaders

        with self.lock:
            # Merge/update (do not wipe existing loaders).
            self.dbcs.update(new_dbcs)
            loaded = len(new_dbcs)
        return loaded

    def load_arxml_catalog(self, catalog=None) -> int:
        """Load (or reload) the ARXML-based decoder.

        If *catalog* is None, the active singleton from arxml_parser is used.
        Returns the number of indexed CAN frames, or 0 on failure.
        """
        if not self.arxml_decoder:
            return 0
        if catalog is None:
            try:
                from arxml_parser import get_active_catalog
                catalog = get_active_catalog()
            except Exception:
                return 0
        if catalog is None:
            return 0
        count = self.arxml_decoder.load_from_catalog(catalog)
        if count:
            print(f"[ARXML] Decoder loaded: {self.arxml_decoder.can_frame_count} CAN frames, "
                  f"{self.arxml_decoder.fr_frame_count} FlexRay frames, "
                  f"{self.arxml_decoder.id_count} CAN IDs, "
                  f"{self.arxml_decoder.fr_slot_count} FR slots", flush=True)
        return count

    def start_bus(self, config):
        """
        Config structure:
        {
            "channels": [
                {"id": 0, "type": "CAN", "bitrate": -2, "dbc": "path/to/file.dbc"},
                ...
            ]
        }
        """
        # Allow restarting if already running (stop first implicitly or just update)
        # But for simplicity, if running, we return False or stop first.
        if self.running:
            self.stop_bus()

        # Allow enabling/disabling ECU simulation per start request.
        if isinstance(config, dict) and 'simulate_ecu' in config:
            try:
                self.simulate_ecu = bool(config.get('simulate_ecu'))
            except Exception:
                pass

        # Build new state off-lock; opening hardware can block.
        channels = config.get('channels', []) if isinstance(config, dict) else []

        # Best-effort: load FIBEX for FlexRay decoding.
        # This is intentionally resilient (never blocks bus start on FIBEX problems).
        try:
            fibex_paths: list[str] = []
            for ch_conf in channels:
                if not isinstance(ch_conf, dict):
                    continue
                bt = str(ch_conf.get('type', 'CAN') or '').strip().upper()
                if bt in {'CAN'}:
                    continue

                paths: list[str] = []
                if isinstance(ch_conf.get('fibexes'), list):
                    paths = [str(p or '').strip() for p in ch_conf.get('fibexes') if str(p or '').strip()]
                else:
                    p = str(ch_conf.get('fibex') or '').strip()
                    paths = [p] if p else []

                for p in paths:
                    if p and os.path.isfile(p):
                        fibex_paths.append(p)

            # Load the first valid FIBEX. (FibexLoader currently supports a single active DB.)
            if fibex_paths:
                try:
                    self.fibex.load(fibex_paths[0])
                except Exception:
                    pass
        except Exception:
            pass

        new_handlers = {}
        new_dbcs = {}
        new_bitrates = {}

        for ch_conf in channels:
            if not isinstance(ch_conf, dict):
                continue
            try:
                ch_id = int(ch_conf['id'])
            except Exception:
                continue
            bus_type = ch_conf.get('type', 'CAN')
            try:
                bitrate = int(ch_conf.get('bitrate', canlib.canBITRATE_500K))
            except Exception:
                bitrate = int(canlib.canBITRATE_500K)
            new_bitrates[ch_id] = bitrate
            dbc_paths = []
            if isinstance(ch_conf.get('dbcs'), list):
                dbc_paths = [p for p in ch_conf.get('dbcs') if p]
            else:
                dbc_paths = [ch_conf.get('dbc')] if ch_conf.get('dbc') else []

            # Load DBC(s) even if hardware open fails (enables decoding for injected frames).
            if dbc_paths and bus_type == "CAN":
                loaders = []
                for dbc_path in dbc_paths:
                    try:
                        loader = DBCLoader()
                        if loader.load(dbc_path):
                            loaders.append(loader)
                    except Exception:
                        pass
                if loaders:
                    new_dbcs[ch_id] = loaders

            # Init Handler (may block on real hardware/driver)
            if bus_type == "CAN":
                handler = CANHandler(ch_id, bitrate, listen_only=bool(ch_conf.get('listen_only')))
            else:
                handler = FlexRayHandler(ch_id)

            try:
                if handler.open():
                    new_handlers[ch_id] = handler
                else:
                    print(f"Failed to open channel {ch_id}")
            except Exception as e:
                print(f"Exception opening channel {ch_id}: {e}")

        with self.lock:
            self.handlers = new_handlers
            # Replace DBC loaders with the new set (but keep decode available even if no handlers).
            self.dbcs = new_dbcs
            self.bitrate_by_channel = new_bitrates

        if not new_handlers:
            return False

        self.running = True
        # Start dedicated reader threads BEFORE the consumer loop so frames
        # begin draining the canlib driver RX queue immediately.
        self._reader_stop_event.clear()
        # Drain any leftover frames from a previous session.
        try:
            while True:
                self._rx_queue.get_nowait()
        except Empty:
            pass
        with self._rx_stats_lock:
            self._rx_dropped_total = 0
            self._rx_queue_high_watermark = 0
        self._reader_threads = {}
        for ch_id, handler in new_handlers.items():
            t = threading.Thread(
                target=self._reader_loop,
                args=(ch_id, handler),
                name=f"bus-reader-{ch_id}",
                daemon=True,
            )
            t.start()
            self._reader_threads[ch_id] = t

        self.thread = threading.Thread(target=self._bus_loop, daemon=True)
        self.thread.start()
        return True

    def stop_bus(self):
        self.running = False
        # Signal reader threads to stop; they hold short blocking reads
        # (timeout=10ms) so they will exit promptly.
        try:
            self._reader_stop_event.set()
        except Exception:
            pass
        try:
            with self._ui_emit_lock:
                timer = self._ui_emit_timer
                self._ui_emit_timer = None
        except Exception:
            timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._flush_ui_frames()
        if self.thread:
            try:
                self.thread.join(timeout=1.5)
            except Exception:
                pass
            # If the thread is still alive (e.g., driver read blocks), don't hang the API.
            try:
                if self.thread.is_alive():
                    print('Warning: bus loop thread did not stop within timeout')
            except Exception:
                pass
            self.thread = None

        # Join reader threads (they should have exited via _reader_stop_event).
        readers = list(self._reader_threads.items())
        self._reader_threads = {}
        for ch_id, rt in readers:
            try:
                rt.join(timeout=1.0)
                if rt.is_alive():
                    print(f'Warning: reader thread for channel {ch_id} did not stop within timeout')
            except Exception:
                pass
        
        with self.lock:
            for h in self.handlers.values():
                h.close()
            self.handlers = {}
            self.bitrate_by_channel = {}
        
        # self.logger.stop() # Decoupled logging from bus stop
        return True

    def start_logging(self, formats):
        self.logger.start(formats)

    def stop_logging(self):
        self.logger.stop()

    def load_dbc(self, path):
        return self.dbc.load(path)

    def load_fibex(self, path):
        return self.fibex.load(path)

    def _reader_loop(self, ch_id, handler):
        """Dedicated capture thread per handler.

        Drains canlib reads as fast as possible and pushes raw frames into
        ``self._rx_queue``. Runs independently of decoding/logging so that
        slow processing cannot starve the driver RX buffer (which would
        otherwise drop FlexRay/CAN frames — visible as 50–600 ms holes in
        recorded MF4 traces).
        """
        rx_queue = self._rx_queue
        stop_event = self._reader_stop_event
        while self.running and not stop_event.is_set():
            try:
                frame = handler.read()
            except Exception:
                # Never let driver hiccups kill the reader; brief backoff.
                time.sleep(0.001)
                continue
            if frame is None:
                # Handler.read() already blocks ~10ms internally on no-msg.
                continue
            try:
                frame['channel'] = ch_id
            except Exception:
                continue
            try:
                rx_queue.put_nowait(frame)
                # Track watermark for diagnostics.
                qsize = rx_queue.qsize()
                if qsize > self._rx_queue_high_watermark:
                    with self._rx_stats_lock:
                        if qsize > self._rx_queue_high_watermark:
                            self._rx_queue_high_watermark = qsize
            except Full:
                # Consumer is too slow even with the decoupled architecture —
                # drop oldest to keep the most recent frames (better than
                # arbitrary loss in the middle of a burst).
                with self._rx_stats_lock:
                    self._rx_dropped_total += 1
                try:
                    rx_queue.get_nowait()
                    rx_queue.put_nowait(frame)
                except (Empty, Full):
                    pass

    def _process_frame(self, frame):
        """Decode + diagnostics + log + listeners + UI emit for one frame."""
        ch_id = frame.get('channel')

        # Monitoring metadata (must be JSON-serializable for Socket.IO)
        try:
            if callable(self.source_id_resolver):
                ft = str(frame.get('type') or 'CAN').strip().upper()
                bus_type = 'FLEXRAY' if ft in {'FLEXRAY', 'FLEX', 'FR'} else ('LIN' if ft == 'LIN' else 'CAN')
                frame['source_id'] = self.source_id_resolver(bus_type, int(ch_id))
        except Exception:
            pass

        # Decode
        decoded = None
        try:
            loaders = self.dbcs.get(ch_id)
            if loaders and str(frame.get('type') or '').upper() == 'CAN':
                for loader in loaders:
                    decoded = loader.decode(frame['id'], frame['data'])
                    if decoded:
                        break
        except Exception:
            decoded = None

        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded:
            try:
                ft = str(frame.get('type') or '').upper()
                if ft == 'CAN':
                    bus_hint = self._channel_bus_hint.get(int(ch_id), '')
                    if bus_hint:
                        decoded = self.arxml_decoder.decode_with_bus(frame['id'], frame['data'], bus_hint=bus_hint)
                    else:
                        decoded = self.arxml_decoder.decode(frame['id'], frame['data'])
                elif ft == 'LIN':
                    decoded = self.arxml_decoder.decode_lin(frame['id'], frame['data'])
            except Exception:
                decoded = None

        if decoded is None and str(frame.get('type') or '').upper() in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                decoded = self.fibex.decode(frame['id'], frame['data'])
            except Exception:
                decoded = None

        if decoded is None and self.arxml_decoder and self.arxml_decoder.loaded \
                and str(frame.get('type') or '').upper() in {'FLEXRAY', 'FLEX', 'FR'}:
            try:
                decoded = self.arxml_decoder.decode_flexray(frame['id'], frame['data'])
            except Exception:
                decoded = None

        if decoded:
            frame['decoded'] = decoded

        # Update Diagnostics
        try:
            self.diag.update(frame)
        except Exception:
            pass

        # Log
        try:
            self.logger.log(frame)
        except Exception:
            pass

        # Notify listeners
        with self.lock:
            listeners_copy = self.listeners[:]
        for listener in listeners_copy:
            try:
                listener(frame)
            except Exception as e:
                print(f"Listener error: {e}")

        # Emit to UI in batches to reduce websocket/JSON overhead.
        self._emit_ui_frame(frame)

    def _bus_loop(self):
        last_stats_emit = 0.0
        rx_queue = self._rx_queue
        batch_size = self._rx_drain_batch
        while self.running:
            data_found = False
            # Drain a batch of frames produced by the per-channel reader threads.
            try:
                first = rx_queue.get(timeout=0.05)
            except Empty:
                first = None
            if first is not None:
                data_found = True
                self._process_frame(first)
                drained = 1
                while drained < batch_size:
                    try:
                        nxt = rx_queue.get_nowait()
                    except Empty:
                        break
                    self._process_frame(nxt)
                    drained += 1

            if not data_found:
                # Queue.get already waited up to 50ms; avoid extra sleep.
                pass

            # Emit stats periodically (throttled)
            now = time.time()
            if (now - last_stats_emit) >= 0.5:
                last_stats_emit = now
                try:
                    with self.lock:
                        br = dict(self.bitrate_by_channel)
                except Exception:
                    br = {}
                stats = self.diag.calculate_load(bitrate_by_channel=br)
                try:
                    self.last_stats = stats
                except Exception:
                    pass
                self.socketio.emit('bus_stats', stats)

    def get_rx_capture_stats(self) -> dict:
        """Return diagnostics for the decoupled capture pipeline.

        Useful to detect logger-side frame loss (the kind that previously
        caused 50–600 ms holes in MO_Drehzahl_01 on FlexRay slot 32).
        """
        with self._rx_stats_lock:
            return {
                'rx_queue_size': self._rx_queue.qsize(),
                'rx_queue_capacity': self._rx_queue_max,
                'rx_queue_high_watermark': self._rx_queue_high_watermark,
                'rx_dropped_total': self._rx_dropped_total,
                'rx_drain_batch': self._rx_drain_batch,
                'reader_threads': len(self._reader_threads),
            }
