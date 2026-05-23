import csv
import json
import time
import os
import re
import threading
from queue import Queue
from collections import deque
from array import array

try:
    import asammdf
except Exception:
    asammdf = None

try:
    import numpy as np
except Exception:
    np = None

RAW_MF4_PAYLOAD_BYTES = 64


def _raw_mf4_bus_type_code(frame_type):
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


def _raw_mf4_byte_names():
    return [f'db{i}' for i in range(RAW_MF4_PAYLOAD_BYTES)]


def _mf4_should_skip_message(msg: dict) -> bool:
    try:
        return str((msg or {}).get('type') or '').strip().upper() == 'EVENT'
    except Exception:
        return False

class BusLogger:
    def __init__(self, log_dir=None):
        if log_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.abspath(os.path.join(base_dir, '..', 'logs'))
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.active = False
        self.queue = Queue()
        self.thread = None
        self.files = {} # handle for txt, csv, json
        self.formats = []
        self.mdf_buffer = []
        self.start_time = 0
        self.on_file_saved = None
        # Pre-roll buffer (stores recent messages even when not logging)
        self._preroll = deque(maxlen=int(os.getenv('LOG_PREROLL_MAX', '5000') or 5000))

        # Power-loss tolerance tuning
        try:
            self._io_flush_interval_s = float(os.getenv('LOG_FLUSH_INTERVAL_S', '1.0') or 1.0)
        except Exception:
            self._io_flush_interval_s = 1.0
        self._io_flush_interval_s = max(0.1, min(self._io_flush_interval_s, 30.0))

        self._fsync_enabled = str(os.getenv('LOG_FSYNC', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}

        # MF4 chunking behavior.
        # Default interval is 30 seconds to provide frequent crash-safe saves;
        # chunk size can be tuned via runtime config (see set_mf4_chunk_size_mb).
        try:
            self._mf4_chunk_interval_s = float(os.getenv('MF4_CHUNK_INTERVAL_S', '15.0') or 15.0)
        except Exception:
            self._mf4_chunk_interval_s = 30.0
        self._mf4_chunk_interval_s = max(1.0, min(self._mf4_chunk_interval_s, 3600.0))

        # Absolute safety cap for samples kept in memory for a single MF4 part.
        # With raw-only buffering (arrays of primitives) we can safely keep more
        # samples than with full Python dict buffering.
        try:
            self._mf4_chunk_max_msgs = int(os.getenv('MF4_CHUNK_MAX_MSGS', '6000000') or 6000000)
        except Exception:
            self._mf4_chunk_max_msgs = 6000000
        self._mf4_chunk_max_msgs = max(200, min(self._mf4_chunk_max_msgs, 10000000))

        # Target size per MF4 part (default 100MB). Used to derive a message-count
        # flush threshold based on the observed bytes/message.
        self._mf4_chunk_target_bytes = int(100 * 1024 * 1024)
        try:
            env_mb = float(os.getenv('MF4_CHUNK_SIZE_MB', '') or 0.0)
        except Exception:
            env_mb = 0.0
        if env_mb and env_mb > 0:
            self._mf4_chunk_target_bytes = int(max(1.0, min(env_mb, 4096.0)) * 1024 * 1024)

        # Bytes/message estimate used to translate target part size (MB) into a message-count
        # threshold. This must converge quickly across very different sources (CAN vs ETH).
        # A too-small value creates oversized parts; a too-large value creates too many tiny parts.
        try:
            # Raw-frame MF4 is typically tens of bytes per frame when stored as signals.
            self._mf4_bytes_per_msg_est = float(os.getenv('MF4_BYTES_PER_MSG_EST', '64') or 64.0)
        except Exception:
            self._mf4_bytes_per_msg_est = 64.0
        # Raw-frame MF4 can be very compact; allow estimates below 128B/msg.
        self._mf4_bytes_per_msg_est = float(max(16.0, min(self._mf4_bytes_per_msg_est, 65536.0)))

        # Time-based part split: finalize the current part and start a new one
        # after this many seconds, even if the size target hasn't been reached.
        # 0 = disabled (only size-based splits).
        try:
            self._mf4_part_time_limit_s = float(os.getenv('MF4_PART_TIME_LIMIT_S', '600') or 600.0)
        except Exception:
            self._mf4_part_time_limit_s = 600.0
        self._mf4_part_time_limit_s = max(0.0, min(self._mf4_part_time_limit_s, 86400.0))

        # Intermediate flush interval: write the in-progress part to disk every
        # N estimated bytes to limit data loss on power failure.
        # The buffer is NOT cleared — the file is overwritten each time.
        try:
            self._mf4_flush_interval_bytes = int(float(os.getenv('MF4_FLUSH_INTERVAL_MB', '5') or 5.0) * 1024 * 1024)
        except Exception:
            self._mf4_flush_interval_bytes = int(10 * 1024 * 1024)
        self._mf4_flush_interval_bytes = max(1 * 1024 * 1024, min(self._mf4_flush_interval_bytes, 4096 * 1024 * 1024))

        # Tracks the start time of the current part (reset on each part split).
        self._mf4_part_start_time = 0.0
        # Estimated bytes at the last intermediate flush (reset on each part split).
        self._mf4_last_intermediate_est_bytes = 0.0

        # Control how much DBC-decoded content is persisted in text/CSV/JSON logs.
        # Decoding can be very verbose; writing it per frame inflates logs by orders of magnitude.
        # Values: 'none' (default), 'name', 'full'
        self._log_decoded_mode_override = None

        self._last_io_flush = 0.0
        self._mf4_part_index = 0
        # Raw-only MF4 chunk buffers (primitive fields) to keep memory bounded.
        self._mf4_raw = None
        self._mf4_last_flush = 0.0

        # MF4 behavior overrides (runtime-configurable via /api/config).
        # When None, fall back to environment variable MF4_INCLUDE_DECODED (default enabled).
        self._mf4_include_decoded_override = None
        # Raw-frame channels can be disabled to keep MF4 compact and MDA-friendly.
        self._mf4_include_raw_override = None

        # MF4 merge status (for UI/API visibility). Merge runs in background to
        # avoid blocking stop requests on large sessions.
        self._mf4_merge_thread = None
        self._mf4_merge_in_progress = False
        self._mf4_merge_base_name = None
        self._mf4_merge_error = None
        self._mf4_merge_started_ts = None
        self._mf4_merge_finished_ts = None

        # Incremental merge: merge each chunk into the consolidated MF4
        # immediately after flushing, instead of one big merge on stop().
        # This keeps peak RAM bounded to (consolidated + one chunk) and
        # distributes CPU load over the entire recording session.
        self._mf4_incremental_ok = True

    def set_mf4_include_decoded(self, enabled) -> None:
        """Control whether MF4 includes DBC-decoded channels.

        - True  -> include decoded channels in MF4
        - False -> raw frames only
        - None  -> use env MF4_INCLUDE_DECODED (default enabled)
        """
        if enabled is None:
            self._mf4_include_decoded_override = None
            return
        self._mf4_include_decoded_override = bool(enabled)

    def set_mf4_include_raw(self, enabled) -> None:
        """Control whether MF4 includes raw frame channels.

        - True  -> include raw frame channels (ID, DLC, bytes, flags)
        - False -> decoded signals only
        - None  -> derive from config/env
        """
        if enabled is None:
            self._mf4_include_raw_override = None
            return
        self._mf4_include_raw_override = bool(enabled)

    def set_mf4_chunk_size_mb(self, mb: float | int | None) -> None:
        """Set target size (MB) for each MF4 part.

        This is a *target* (best-effort). The logger still enforces a hard cap on
        buffered messages for memory safety.
        """
        if mb is None:
            return
        try:
            v = float(mb)
        except Exception:
            raise ValueError('mf4_chunk_size_mb must be a number')
        if v <= 0:
            raise ValueError('mf4_chunk_size_mb must be > 0')
        # Keep bounds sane for embedded targets.
        v = max(1.0, min(v, 4096.0))
        self._mf4_chunk_target_bytes = int(v * 1024 * 1024)

    def set_mf4_part_time_limit_s(self, seconds: float | int | None) -> None:
        """Set time limit (seconds) for each MF4 part.

        When the elapsed time since the part started exceeds this limit, the
        part is finalized and a new one begins.  0 = disabled.
        """
        if seconds is None:
            return
        try:
            v = float(seconds)
        except Exception:
            raise ValueError('mf4_part_time_limit_s must be a number')
        if v < 0:
            raise ValueError('mf4_part_time_limit_s must be >= 0')
        self._mf4_part_time_limit_s = max(0.0, min(v, 86400.0))

    def set_mf4_flush_interval_mb(self, mb: float | int | None) -> None:
        """Set intermediate flush interval (MB).

        The current in-progress MF4 part is written to disk (overwriting the
        previous version) every *mb* megabytes of estimated data.  This limits
        data loss on sudden power failure.
        """
        if mb is None:
            return
        try:
            v = float(mb)
        except Exception:
            raise ValueError('mf4_flush_interval_mb must be a number')
        if v <= 0:
            raise ValueError('mf4_flush_interval_mb must be > 0')
        v = max(1.0, min(v, 4096.0))
        self._mf4_flush_interval_bytes = int(v * 1024 * 1024)

    def set_log_decoded_mode(self, mode: str | None) -> None:
        """Control decoded content persisted to text/CSV/JSON logs.

        - 'none': do not persist decoded payloads
        - 'name': persist only the decoded message name
        - 'full': persist full decoded structure (can be huge)
        - None: use env LOG_DECODED_MODE (default 'none')
        """
        if mode is None:
            self._log_decoded_mode_override = None
            return
        m = str(mode).strip().lower()
        if m not in {'none', 'name', 'full'}:
            raise ValueError("log_decoded_mode must be one of: none, name, full")
        self._log_decoded_mode_override = m

    def _log_decoded_mode_effective(self) -> str:
        v = getattr(self, '_log_decoded_mode_override', None)
        if v is not None:
            return str(v)
        return str(os.getenv('LOG_DECODED_MODE', 'none')).strip().lower() or 'none'

    def _include_message_in_logs(self, msg: dict) -> bool:
        """Return True if this message should be persisted to disk.

        By default, we only persist CAN/CANFD-like frames. High-rate sources like
        Ethernet packets can generate massive logs very quickly.

        Override with env `LOG_INCLUDE_ETH=1` (or `LOG_INCLUDE_ALL=1`).
        """
        try:
            if str(os.getenv('LOG_INCLUDE_ALL', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}:
                return True
        except Exception:
            pass
        t = ''
        try:
            t = str(msg.get('type') or '').strip().upper()
        except Exception:
            t = ''
        if t == 'ETH':
            try:
                return str(os.getenv('LOG_INCLUDE_ETH', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
            except Exception:
                return False
        # Default: keep everything else (CAN, CANFD, FlexRay, etc.)
        return True

    def _mf4_include_decoded_effective(self) -> bool:
        v = getattr(self, '_mf4_include_decoded_override', None)
        if v is not None:
            return bool(v)
        # Default to including decoded channels so MF4 files are self-contained.
        # Set MF4_INCLUDE_DECODED=0 to disable and reduce file size/CPU.
        return str(os.getenv('MF4_INCLUDE_DECODED', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}

    def _mf4_include_raw_effective(self) -> bool:
        v = getattr(self, '_mf4_include_raw_override', None)
        if v is not None:
            return bool(v)
        env_v = os.getenv('MF4_INCLUDE_RAW', None)
        if env_v is not None and str(env_v).strip() != '':
            return str(env_v).strip().lower() not in {'0', 'false', 'no', 'off'}
        # Default to decoded-only MF4 when decoded channels are enabled.
        return not bool(self._mf4_include_decoded_effective())

    def _mf4_channel_label(self, msg: dict) -> str:
        try:
            ft = str(msg.get('type') or '').strip().upper()
        except Exception:
            ft = ''
        if ft in {'FLEXRAY', 'FLEX', 'FR'}:
            return 'FlexRay'
        if ft == 'LIN':
            return 'LIN'
        if ft in {'ETH', 'ETHERNET'}:
            return 'Ethernet'
        try:
            ch = msg.get('channel', None)
            if ch is not None and str(ch).strip() != '':
                return f"CAN{int(ch)}"
        except Exception:
            pass
        return 'CAN'

    def _sanitize_mf4_name_part(self, value) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        text = re.sub(r'[^0-9A-Za-z_]+', '_', text)
        text = re.sub(r'_+', '_', text).strip('_')
        return text

    def _decoded_mf4_signal_name(self, msg: dict, signal_name: str) -> str:
        sig = self._sanitize_mf4_name_part(signal_name)
        if not sig:
            return ''
        channel_name = self._sanitize_mf4_name_part(self._mf4_channel_label(msg))
        return '.'.join(p for p in [channel_name, sig] if p)

    def _decoded_mf4_signal_name_with_message(
        self,
        msg: dict,
        message_name: str,
        signal_name: str,
        *,
        frame_id: int | None = None,
    ) -> str:
        sig = self._sanitize_mf4_name_part(signal_name)
        if not sig:
            return ''
        channel_name = self._sanitize_mf4_name_part(self._mf4_channel_label(msg))
        msg_name = self._sanitize_mf4_name_part(message_name)
        if not msg_name and frame_id is not None:
            msg_name = f'ID{(int(frame_id) & 0x1FFFFFFF):X}'
        return '.'.join(p for p in [channel_name, msg_name, sig] if p)

    def set_log_dir(self, log_dir: str) -> None:
        """Update the base directory used for new sessions.

        This is only allowed when logging is not active.
        """
        if self.active:
            raise RuntimeError('cannot change log_dir while logging is active')
        if log_dir is None:
            raise ValueError('log_dir is required')
        log_dir = str(log_dir).strip()
        if not log_dir:
            raise ValueError('log_dir is empty')
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

    def start(self, formats=['csv', 'txt', 'json', 'mf4']):
        if self.active:
            return
        
        self.formats = list(formats)
        if 'mf4' in self.formats and (asammdf is None or np is None):
            print("MF4 requested but dependencies missing. Install 'asammdf' and 'numpy' or disable MF4.")
            self.formats = [f for f in self.formats if f != 'mf4']
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        base_name = os.path.join(self.log_dir, f"session_{timestamp}")
        self.base_name = base_name
        # A stable identifier for the session; unlike `base_name`, this is never
        # temporarily rewritten during MF4 chunk flushes.
        self.session_base_name = base_name
        self.start_time = time.time()

        self._last_io_flush = time.time()
        self._mf4_part_index = 0
        self._mf4_chunk_buffer = []
        self._mf4_last_flush = time.time()

        # Reset merge state for the new session.
        self._mf4_merge_in_progress = False
        self._mf4_merge_base_name = None
        self._mf4_merge_error = None
        self._mf4_merge_started_ts = None
        self._mf4_merge_finished_ts = None
        self._mf4_incremental_ok = True

        if 'txt' in formats:
            # line-buffered for better crash tolerance
            self.files['txt'] = open(f"{base_name}.log", 'w', buffering=1)
        
        if 'csv' in formats:
            self.files['csv'] = open(f"{base_name}.csv", 'w', newline='', buffering=1)
            self.csv_writer = csv.writer(self.files['csv'])
            self.csv_writer.writerow(["Timestamp", "Channel", "ID", "DLC", "Data", "Flags", "Decoded"])
        
        if 'json' in formats:
            self.files['json'] = open(f"{base_name}.json", 'w', buffering=1)

        if 'mf4' in self.formats:
            # Keep chunk buffer in memory; periodically flush to disk as multiple MF4 parts.
            # This prevents losing the entire MF4 if power is cut mid-session.
            self.mdf_buffer = []
            self._mf4_raw = {
                # Use arrays of primitives to keep memory bounded even for 100MB parts.
                't': array('d'),
                'id': array('I'),
                'dlc': array('H'),
                'payload_len': array('H'),
                'ch': array('B'),
                'bus_type': array('B'),
                'flags': array('I'),
                **{name: array('B') for name in _raw_mf4_byte_names()},
            }
            # Legacy dict buffering is only needed when decoded channels are enabled.
            self._mf4_chunk_buffer = []
            self._mf4_part_index = 0
            self._mf4_last_flush = time.time()
            self._mf4_part_start_time = time.time()
            self._mf4_last_intermediate_est_bytes = 0.0

            # Emit effective MF4 settings (helps diagnose unexpectedly huge files).
            try:
                include_decoded = bool(self._mf4_include_decoded_effective())
                merge_on_stop = str(os.getenv('MF4_MERGE_ON_STOP', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
                incremental = bool(self._incremental_merge_enabled())
                print(
                    f"MF4 settings: include_decoded={include_decoded} "
                    f"chunk_interval_s={self._mf4_chunk_interval_s} "
                    f"chunk_max_msgs={self._mf4_chunk_max_msgs} "
                    f"chunk_target_mb={int(round(float(getattr(self, '_mf4_chunk_target_bytes', 0)) / (1024 * 1024)))} "
                    f"part_time_limit_s={self._mf4_part_time_limit_s} "
                    f"flush_interval_mb={int(round(float(getattr(self, '_mf4_flush_interval_bytes', 0)) / (1024 * 1024)))} "
                    f"merge_on_stop={merge_on_stop} "
                    f"incremental_merge={incremental}"
                )
            except Exception:
                pass

        self.active = True
        # Flush pre-roll into the queue first (best-effort)
        try:
            preroll_copy = list(self._preroll)
            self._preroll.clear()
            for m in preroll_copy:
                self.queue.put(m)
        except Exception:
            pass
        self.thread = threading.Thread(target=self._process_queue)
        self.thread.start()
        print(f"Logging started. Formats: {self.formats}")

    def stop(self):
        self.active = False

        def _finalize_stop():
            # Flush remaining MF4 chunk (best-effort)
            if 'mf4' in self.formats:
                try:
                    self._flush_mf4_chunk(force=True)
                except Exception:
                    pass

                # Best-effort: also produce a single consolidated MF4 for easier consumption
                # by common MDF tools (which typically expect a single file per session).
                # Run merge in background to avoid blocking stop on large sessions.
                try:
                    self._start_mf4_merge_background(base_name=getattr(self, 'session_base_name', None) or getattr(self, 'base_name', None))
                except Exception:
                    pass

            # Final flush+fsync before closing
            try:
                self._flush_files(force=True)
            except Exception:
                pass

            for f in list(self.files.values()):
                try:
                    f.close()
                except Exception:
                    pass
            self.files = {}
            self.thread = None
            print("Logging stopped.")

        if self.thread:
            # Avoid blocking indefinitely on queue-drain or I/O; if the worker is slow/hung,
            # finalize in the background once it exits.
            try:
                stop_join_timeout_s = float(os.getenv('LOG_STOP_JOIN_TIMEOUT_S', '10.0') or 10.0)
            except Exception:
                stop_join_timeout_s = 10.0
            stop_join_timeout_s = max(0.0, min(stop_join_timeout_s, 120.0))
            try:
                self.thread.join(timeout=stop_join_timeout_s)
            except Exception:
                pass
            try:
                if self.thread.is_alive():
                    print(f"Warning: logger thread did not stop within {stop_join_timeout_s}s; deferring finalization")

                    def _deferred():
                        try:
                            # Wait longer in the background; if it never exits, at least Stop returns.
                            self.thread.join(timeout=300.0)
                        except Exception:
                            pass
                        try:
                            if self.thread and (not self.thread.is_alive()):
                                _finalize_stop()
                        except Exception:
                            pass

                    threading.Thread(target=_deferred, daemon=True).start()
                    return
            except Exception:
                pass

        _finalize_stop()

    def _incremental_merge_enabled(self) -> bool:
        """Return True if incremental (per-chunk) MF4 merging is enabled."""
        return str(os.getenv('MF4_INCREMENTAL_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}

    def _incremental_merge_new_part(self, part_path: str) -> bool:
        """Merge a newly-written part into the running consolidated MF4.

        Called after each chunk flush.  The consolidated file grows
        incrementally so that stop() does not need a heavy all-at-once merge.

        Returns True if the part was successfully consumed (merged + deleted).
        Returns False if the part file should be kept for the end-of-session
        fallback merge.
        """
        if asammdf is None or np is None:
            return False
        if not part_path or not os.path.exists(part_path):
            return False

        session_base = getattr(self, 'session_base_name', None) or getattr(self, 'base_name', '')
        if not session_base:
            return False

        sb = str(session_base)
        for ext in ('.mf4', '.mdf', '.dat'):
            if sb.lower().endswith(ext):
                sb = sb[: -len(ext)]
                break
        if sb.lower().endswith('.tmp'):
            sb = sb[:-4]

        out_path = f"{sb}.mf4"
        tmp_path = f"{sb}.incr_tmp.mf4"

        # ---- First chunk: just move it to become the consolidated file ----
        if not os.path.exists(out_path):
            try:
                os.replace(part_path, out_path)
                return True
            except OSError:
                pass
            try:
                import shutil
                shutil.copy2(part_path, tmp_path)
                os.replace(tmp_path, out_path)
                os.remove(part_path)
                return True
            except Exception:
                return False

        # ---- Subsequent chunks: merge consolidated + new part ----
        import inspect

        def _call_safe(fn, *args, **kwargs):
            try:
                sig = inspect.signature(fn)
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            except Exception:
                pass
            return fn(*args, **kwargs)

        # Strategy 1: asammdf.MDF.concatenate (most memory-efficient;
        # works with file-backed data groups and copies block-by-block)
        for concat_fn_label, concat_thunk in [
            ('MDF.concatenate', lambda: _call_safe(asammdf.MDF.concatenate, [out_path, part_path], sync=True)),
            ('asammdf.concatenate', lambda: _call_safe(getattr(asammdf, 'concatenate', None) or asammdf.MDF.concatenate, [out_path, part_path], sync=True)),
        ]:
            try:
                merged = concat_thunk()
                if merged is not None:
                    merged.save(tmp_path, overwrite=True)
                    if os.path.exists(tmp_path):
                        os.replace(tmp_path, out_path)
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                        return True
            except Exception:
                continue

        # Strategy 2: flatten merge (2 files only — bounded RAM)
        try:
            self._merge_mf4_parts_flatten(
                part_paths=[out_path, part_path],
                out_path=out_path,
                tmp_path=tmp_path,
            )
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except Exception:
                pass
            return True
        except Exception:
            pass

        # All strategies failed — keep the part for end-of-session merge
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False

    def _start_mf4_merge_background(self, base_name: str | None) -> None:
        # Always reflect merge decision in status fields so the UI/API can
        # distinguish "still running" vs "skipped".
        now = time.time()
        if not base_name:
            self._mf4_merge_in_progress = False
            self._mf4_merge_base_name = None
            self._mf4_merge_error = 'merge skipped: missing base_name'
            self._mf4_merge_started_ts = now
            self._mf4_merge_finished_ts = now
            return
        if str(os.getenv('MF4_MERGE_ON_STOP', '1')).strip().lower() in {'0', 'false', 'no', 'off'}:
            self._mf4_merge_in_progress = False
            self._mf4_merge_base_name = str(base_name)
            self._mf4_merge_error = 'merge skipped: MF4_MERGE_ON_STOP disabled'
            self._mf4_merge_started_ts = now
            self._mf4_merge_finished_ts = now
            return
        if asammdf is None:
            self._mf4_merge_in_progress = False
            self._mf4_merge_base_name = str(base_name)
            self._mf4_merge_error = 'merge skipped: asammdf not available'
            self._mf4_merge_started_ts = now
            self._mf4_merge_finished_ts = now
            return

        t = getattr(self, '_mf4_merge_thread', None)
        try:
            if t is not None and t.is_alive():
                return
        except Exception:
            pass

        self._mf4_merge_in_progress = True
        self._mf4_merge_base_name = str(base_name)
        self._mf4_merge_error = None
        self._mf4_merge_started_ts = time.time()
        self._mf4_merge_finished_ts = None

        def _worker(base_snapshot: str):
            try:
                self._finalize_single_mf4_from_parts(base_name=base_snapshot)
            except Exception as e:
                try:
                    self._mf4_merge_error = str(e)
                except Exception:
                    self._mf4_merge_error = 'merge failed'
            finally:
                self._mf4_merge_in_progress = False
                self._mf4_merge_finished_ts = time.time()

        self._mf4_merge_thread = threading.Thread(target=_worker, args=(str(base_name),), daemon=True)
        self._mf4_merge_thread.start()

    def _finalize_single_mf4_from_parts(self, base_name: str | None = None) -> None:
        """Create a single `${base_name}.mf4` from `${base_name}_partXXXX.mf4` chunks.

        Chunking is kept for crash/power-loss tolerance; this step improves usability
        for external tools by providing a single file per session.

        Controlled by env `MF4_MERGE_ON_STOP` (default enabled).
        """
        if str(os.getenv('MF4_MERGE_ON_STOP', '1')).strip().lower() in {'0', 'false', 'no', 'off'}:
            return
        if asammdf is None:
            return
        base = base_name if base_name is not None else getattr(self, 'base_name', None)
        if not base:
            return

        # Defensive: base should not include an extension (e.g. `.mf4`).
        try:
            b = str(base)
            low = b.lower()
            for ext in ('.mf4', '.mdf', '.dat'):
                if low.endswith(ext):
                    b = b[: -len(ext)]
                    break
            if b.lower().endswith('.tmp'):
                b = b[:-4]
            base = b
        except Exception:
            pass

        # If incremental merge succeeded during recording, the consolidated
        # file is already complete — skip the expensive all-at-once merge.
        incr_ok = getattr(self, '_mf4_incremental_ok', False)
        if incr_ok and self._incremental_merge_enabled():
            consolidated = f"{base}.mf4"
            if os.path.exists(consolidated) and os.path.getsize(consolidated) > 0:
                # Double-check: no unmerged part files should remain.
                folder_chk = os.path.dirname(str(base)) or '.'
                base_file_chk = os.path.basename(str(base))
                prefix_chk = base_file_chk + '_part'
                leftover = [n for n in os.listdir(folder_chk) if n.startswith(prefix_chk) and n.lower().endswith('.mf4') and '.tmp.' not in n.lower()]
                if not leftover:
                    print(f"MF4 incremental merge already complete: {consolidated}")
                    return
                # Some parts were not merged; fall through to standard merge
                # but only merge the leftover parts into the existing output.
                print(f"MF4 incremental merge had {len(leftover)} leftover part(s); merging remaining")

        folder = os.path.dirname(str(base)) or '.'
        base_file = os.path.basename(str(base))
        prefix = base_file + '_part'
        try:
            # Only include real part files: `${base}_partNNNN.mf4`.
            # Ignore temp artifacts such as `${base}_partNNNN.tmp.mf4` which are not valid MDF.
            import re
            # Accept legacy artifacts like `.mf4.mf4` so we can repair old sessions.
            part_re = re.compile(r'^' + re.escape(base_file) + r'_part(\d+)\.mf4(?:\.mf4)?$', re.IGNORECASE)
            names = []
            tmp_names = []
            for n in os.listdir(folder):
                if not n.startswith(prefix):
                    continue
                if n.lower().endswith('.tmp.mf4') or '.tmp.' in n.lower():
                    tmp_names.append(n)
                    continue
                if part_re.match(n):
                    names.append(n)
        except Exception:
            return

        # Best-effort cleanup of leftover temp files from crash/power loss.
        # Keep this conservative: only delete files that clearly match our temp naming.
        try:
            if tmp_names and str(os.getenv('MF4_DELETE_TEMP_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                for n in tmp_names:
                    try:
                        os.remove(os.path.join(folder, n))
                    except Exception:
                        continue
        except Exception:
            pass
        if not names:
            return

        def _part_key(name: str) -> int:
            try:
                import re
                m = re.search(r'_part(\d+)\.mf4(?:\.mf4)?$', name, re.IGNORECASE)
                if not m:
                    return 10**9
                return int(m.group(1))
            except Exception:
                return 10**9

        names.sort(key=_part_key)
        part_paths = [os.path.join(folder, n) for n in names]

        out_path = f"{base}.mf4"
        tmp_path = f"{base}.tmp.mf4"

        # If there's only one part, create/update a stable `${base}.mf4` alias.
        # Do NOT early-return just because out_path exists: an older/incomplete out_path
        # may have been created before we discovered all parts.
        if len(part_paths) == 1:
            src = part_paths[0]

            # If an out_path already exists but is NOT the same file, it may contain an earlier
            # chunk (e.g. a previous "single part" merge ran before all parts were discovered).
            # In that case, preserve data by merging the existing out_path together with `src`.
            try:
                if os.path.exists(out_path) and (not os.path.samefile(src, out_path)):
                    self._merge_mf4_parts_flatten(part_paths=[out_path, src], out_path=out_path, tmp_path=tmp_path)
                    try:
                        if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                            os.remove(src)
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            # If out_path already points to the same file, we are done.
            try:
                if os.path.exists(out_path) and os.path.samefile(src, out_path):
                    try:
                        if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                            os.remove(src)
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            # Create tmp then atomically replace.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

            try:
                os.link(src, tmp_path)
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, out_path)
                try:
                    if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                        os.remove(src)
                except Exception:
                    pass
                return
            except Exception:
                pass

            try:
                import shutil
                shutil.copy2(src, tmp_path)
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, out_path)
                try:
                    if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                        os.remove(src)
                except Exception:
                    pass
            except Exception:
                pass
            return

        # Multiple parts: flatten-merge into a single MDF to avoid duplicated channel
        # names across groups (which many tools show as "fragmented" signals/messages).
        # This rebuilds a single raw group + a single decoded group.
        try:
            self._merge_mf4_parts_flatten(part_paths=part_paths, out_path=out_path, tmp_path=tmp_path)

            # If we got here, the consolidated file exists; we can delete parts.
            try:
                if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                    for p in part_paths:
                        try:
                            os.remove(p)
                        except Exception:
                            continue
            except Exception:
                pass
            return
        except Exception as e:
            # Persist details and fall back to asammdf concatenate if possible.
            try:
                with open(f"{base}.mf4.merge_error.txt", 'w') as f:
                    f.write(f"flatten merge failed: {e}\n")
            except Exception:
                pass

        # Fallback: try to concatenate/stack using whatever API is available.
        import inspect

        def _call_with_supported_kwargs(fn, *args, **kwargs):
            try:
                sig = inspect.signature(fn)
                filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
                return fn(*args, **filtered)
            except Exception:
                return fn(*args, **kwargs)

        mdf = None
        last_err = None
        candidates = []

        # Candidate 1: classmethod MDF.concatenate
        try:
            if hasattr(asammdf, 'MDF') and hasattr(asammdf.MDF, 'concatenate'):
                candidates.append(('MDF.concatenate(paths)', lambda: _call_with_supported_kwargs(asammdf.MDF.concatenate, part_paths, sync=True)))
        except Exception:
            pass

        # Candidate 2: classmethod MDF.stack
        try:
            if hasattr(asammdf, 'MDF') and hasattr(asammdf.MDF, 'stack'):
                candidates.append(('MDF.stack(paths)', lambda: _call_with_supported_kwargs(asammdf.MDF.stack, part_paths)))
        except Exception:
            pass

        # Candidate 3: module-level concatenate
        try:
            if hasattr(asammdf, 'concatenate'):
                candidates.append(('asammdf.concatenate(paths)', lambda: _call_with_supported_kwargs(asammdf.concatenate, part_paths, sync=True)))
        except Exception:
            pass

        # Candidate 4: try with MDF instances instead of paths
        def _load_all():
            return [asammdf.MDF(p) for p in part_paths]

        try:
            if hasattr(asammdf, 'MDF') and hasattr(asammdf.MDF, 'concatenate'):
                candidates.append(('MDF.concatenate(mdfs)', lambda: _call_with_supported_kwargs(asammdf.MDF.concatenate, _load_all(), sync=True)))
        except Exception:
            pass

        for label, thunk in candidates:
            try:
                mdf = thunk()
                if mdf is not None:
                    break
            except Exception as e:
                last_err = f"{label}: {e}"
                mdf = None

        if mdf is None:
            if last_err:
                try:
                    with open(f"{base}.mf4.merge_error.txt", 'w') as f:
                        f.write(str(last_err))
                except Exception:
                    pass
            return

        # Save atomically
        try:
            mdf.save(tmp_path, overwrite=True)
            if os.path.exists(tmp_path):
                os.replace(tmp_path, out_path)

            # If we got here, the consolidated file exists; we can delete parts.
            try:
                if str(os.getenv('MF4_DELETE_PARTS_ON_MERGE', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}:
                    for p in part_paths:
                        try:
                            os.remove(p)
                        except Exception:
                            continue
            except Exception:
                pass
        except Exception as e:
            try:
                with open(f"{base}.mf4.merge_error.txt", 'w') as f:
                    f.write(f"save/replace failed: {e}")
            except Exception:
                pass

    def _merge_mf4_parts_flatten(self, part_paths, out_path: str, tmp_path: str) -> None:
        """Flatten-merge MF4 parts into a consolidated MF4 with unique channel names.

        The default concatenate() behavior often keeps each part as a separate data group,
        which results in multiple occurrences of the same channel name (e.g., CAN_ID) and
        appears as fragmented signals/messages in many MDF tools.
        """
        if asammdf is None or np is None:
            raise RuntimeError('asammdf/numpy not available')

        # Raw frame channels we always put in the first group.
        raw_names = {'CAN_ID', 'DLC', 'PayloadLength', 'Channel', 'BusType', 'Flags'} | {f'DataByte{i}' for i in range(RAW_MF4_PAYLOAD_BYTES)}

        def _append_series(dst: dict, name: str, t, y) -> None:
            if name == 'time':
                return
            if t is None or y is None:
                return
            try:
                t = np.asarray(t, dtype=np.float64)
            except Exception:
                return
            try:
                y = np.asarray(y)
            except Exception:
                return
            if t.size == 0 or y.size == 0:
                return
            n = int(min(t.size, y.size))
            if n <= 0:
                return
            entry = dst.get(name)
            if entry is None:
                entry = {'t': [], 'y': []}
                dst[name] = entry
            entry['t'].append(t[:n])
            entry['y'].append(y[:n])

        series = {}

        def _open_mdf(path: str):
            try:
                return asammdf.MDF(path, memory='minimum')
            except Exception:
                return asammdf.MDF(path)

        # Collect all channel occurrences across all parts.
        for p in list(part_paths or []):
            m = _open_mdf(p)
            db = getattr(m, 'channels_db', None)
            if not isinstance(db, dict):
                continue
            for name, occs in db.items():
                if not occs:
                    continue
                # occs: list[(group, index)]
                for group, index in occs:
                    try:
                        sig = m.get(name, group=group, index=index)
                    except Exception:
                        continue
                    try:
                        _append_series(series, str(name), sig.timestamps, sig.samples)
                    except Exception:
                        continue

        if not series:
            raise RuntimeError('no signals found in MF4 parts')

        # Build consolidated MDF: raw group then decoded group.
        out = asammdf.MDF()

        raw_sigs = []
        decoded_sigs = []

        for name, entry in series.items():
            try:
                t_parts = entry.get('t') or []
                y_parts = entry.get('y') or []
                t = np.concatenate(t_parts) if t_parts else None
                y = np.concatenate(y_parts) if y_parts else None
            except Exception:
                continue
            if t is None or y is None or t.size == 0 or y.size == 0:
                continue

            # Ensure chronological order for better tool compatibility.
            # Optimization: if parts are already monotonic in time, skip argsort.
            try:
                monotonic = True
                prev_last = None
                for tp in t_parts:
                    if tp is None:
                        continue
                    try:
                        if tp.size == 0:
                            continue
                        first = float(tp[0])
                        last = float(tp[-1])
                        if prev_last is not None and first < prev_last:
                            monotonic = False
                            break
                        prev_last = last
                    except Exception:
                        monotonic = False
                        break
                if not monotonic:
                    order = np.argsort(t)
                    t = t[order]
                    y = y[order]
            except Exception:
                pass

            try:
                sig = asammdf.Signal(y, t, name=str(name))
            except Exception:
                continue

            if str(name) in raw_names:
                raw_sigs.append(sig)
            else:
                decoded_sigs.append(sig)

        # Keep stable ordering.
        def _name_key(s):
            try:
                return str(getattr(s, 'name', ''))
            except Exception:
                return ''

        raw_sigs.sort(key=_name_key)
        decoded_sigs.sort(key=_name_key)

        if raw_sigs:
            out.append(raw_sigs)
        if decoded_sigs:
            out.append(decoded_sigs)

        out.save(tmp_path, overwrite=True)
        if os.path.exists(tmp_path):
            os.replace(tmp_path, out_path)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def log(self, message):
        # Always keep a small pre-roll buffer; write to disk only when active.
        try:
            self._preroll.append(message)
        except Exception:
            pass
        if self.active:
            self.queue.put(message)

    def _process_queue(self):
        while self.active or not self.queue.empty():
            try:
                msg = self.queue.get(timeout=1)
                self._write_to_files(msg)

                # Periodic flush to reduce data loss on sudden power-off.
                now = time.time()
                if now - float(self._last_io_flush or 0.0) >= float(self._io_flush_interval_s):
                    try:
                        self._flush_files(force=False)
                    except Exception:
                        pass
                    self._last_io_flush = now

                if 'mf4' in self.formats:
                    try:
                        self._flush_mf4_chunk(force=False)
                    except Exception:
                        pass
            except:
                continue

        # Final flush at thread end
        try:
            self._flush_files(force=True)
        except Exception:
            pass
        if 'mf4' in self.formats:
            try:
                self._flush_mf4_chunk(force=True)
            except Exception:
                pass

    def _write_to_files(self, msg):
        if not self._include_message_in_logs(msg if isinstance(msg, dict) else {}):
            return
        decoded_mode = self._log_decoded_mode_effective()
        decoded_name = ''
        decoded_payload = None
        capture_origin = ''
        try:
            dec = msg.get('decoded')
            if isinstance(dec, dict):
                decoded_name = str(dec.get('name') or '')
                decoded_payload = dec
        except Exception:
            decoded_name = ''
            decoded_payload = None
        try:
            capture_origin = str(msg.get('capture_origin') or '').strip().lower()
        except Exception:
            capture_origin = ''

        # TXT
        if 'txt' in self.formats:
            if decoded_mode == 'full':
                dec_txt = msg.get('decoded', '')
            elif decoded_mode == 'name':
                dec_txt = decoded_name
            else:
                dec_txt = ''
            origin_txt = f" Origin:{capture_origin}" if capture_origin else ''
            line = f"[{msg['timestamp']}] Ch:{msg.get('channel',0)} {msg['type']} ID:{hex(msg['id'])} DLC:{msg['dlc']} Data:{msg['data']}{origin_txt} {dec_txt}\n"
            self.files['txt'].write(line)
        
        # CSV
        if 'csv' in self.formats:
            if decoded_mode == 'full':
                decoded_cell = json.dumps(decoded_payload or {}, ensure_ascii=False)
            elif decoded_mode == 'name':
                decoded_cell = decoded_name
            else:
                decoded_cell = ''
            self.csv_writer.writerow([
                msg['timestamp'], 
                msg.get('channel', 0),
                hex(msg['id']), 
                msg['dlc'], 
                msg['data'], 
                msg['flags'],
                decoded_cell,
            ])
        
        # JSON
        if 'json' in self.formats:
            if decoded_mode == 'full':
                out = msg
            elif decoded_mode == 'name':
                out = dict(msg)
                if 'decoded' in out:
                    out['decoded'] = {'name': decoded_name} if decoded_name else {}
            else:
                out = dict(msg)
                out.pop('decoded', None)
            self.files['json'].write(json.dumps(out, ensure_ascii=False) + "\n")

        # MF4 Buffer
        if 'mf4' in self.formats:
            # Raw-only buffering for MF4 (decoded channels are controlled separately).
            # This keeps memory usage low enough to reach large part sizes (e.g., 100MB)
            # without creating many small parts.
            try:
                if not isinstance(msg, dict):
                    return
                if _mf4_should_skip_message(msg):
                    return
                include_decoded = bool(self._mf4_include_decoded_effective())
                if include_decoded:
                    # Fallback to legacy dict buffering when decoded channels are enabled.
                    self._mf4_chunk_buffer.append(msg)
                    return

                raw = self._mf4_raw
                if not isinstance(raw, dict):
                    return

                ts = msg.get('timestamp')
                try:
                    ts_f = float(ts)
                except Exception:
                    ts_f = time.time() * 1000.0
                # Heuristic: values > 1e11 are ms epoch; otherwise seconds.
                t_s = (ts_f / 1000.0) if ts_f > 1e11 else ts_f

                raw['t'].append(float(t_s))
                raw['id'].append(int(msg.get('id', 0)) & 0x1FFFFFFF)
                raw['dlc'].append(int(msg.get('dlc', 0)) & 0xFFFF)
                raw['payload_len'].append(int(msg.get('dlc', 0)) & 0xFFFF)

                ch_raw = msg.get('channel', 0)
                try:
                    ch_val = int(ch_raw)
                except Exception:
                    ch_val = 255 if str(ch_raw).strip().upper() == 'ETH' else 0
                raw['ch'].append(int(ch_val) & 0xFF)
                raw['bus_type'].append(int(_raw_mf4_bus_type_code(msg.get('type', 'CAN'))) & 0xFF)
                raw['flags'].append(int(msg.get('flags', 0)) & 0xFFFFFFFF)

                # Persist enough payload bytes for FlexRay/LIN offline decode.
                payload = msg.get('data')
                data_list = []
                try:
                    if isinstance(payload, list):
                        data_list = [int(x) & 0xFF for x in payload]
                    elif isinstance(payload, (bytes, bytearray)):
                        data_list = list(payload)
                    elif isinstance(payload, str):
                        s = payload.strip().lower()
                        s = ''.join([c for c in s if c in '0123456789abcdef'])
                        if s:
                            if len(s) % 2 == 1:
                                s = '0' + s
                            data_list = list(bytes.fromhex(s))
                except Exception:
                    data_list = []
                d = (data_list + [0] * RAW_MF4_PAYLOAD_BYTES)[:RAW_MF4_PAYLOAD_BYTES]
                for i, name in enumerate(_raw_mf4_byte_names()):
                    raw[name].append(d[i])
            except Exception:
                # Best-effort only; never break the logging loop.
                return

    def _flush_files(self, force: bool = False) -> None:
        """Flush (and optionally fsync) text/CSV/JSON logs."""
        if not self.files:
            return
        for f in list(self.files.values()):
            try:
                f.flush()
                if self._fsync_enabled:
                    os.fsync(f.fileno())
            except Exception:
                continue

    def _intermediate_flush_mf4(self, include_decoded: bool) -> None:
        """Write the current in-progress buffer to disk without clearing it.

        This overwrites the current part file so that, on power loss, the most
        recent snapshot of the part is already on disk (losing at most one flush
        interval of data).
        """
        if asammdf is None or np is None:
            return

        session_base = getattr(self, 'session_base_name', None) or getattr(self, 'base_name', None) or ''
        try:
            sb = str(session_base)
            low = sb.lower()
            for ext in ('.mf4', '.mdf', '.dat'):
                if low.endswith(ext):
                    sb = sb[: -len(ext)]
                    break
            if sb.lower().endswith('.tmp'):
                sb = sb[:-4]
            session_base = sb
        except Exception:
            pass

        part = int(self._mf4_part_index)
        out_path = f"{session_base}_part{part:04d}.mf4"
        tmp_path = f"{session_base}_part{part:04d}.tmp.mf4"

        old_buffer = self.mdf_buffer
        old_base = self.base_name
        try:
            if include_decoded:
                buf = getattr(self, '_mf4_chunk_buffer', None) or []
                if not buf:
                    return
                self.mdf_buffer = list(buf)
                self.base_name = f"{session_base}_part{part:04d}.tmp"
                self._write_mf4()
            else:
                raw = getattr(self, '_mf4_raw', None)
                if not isinstance(raw, dict) or not raw.get('t'):
                    return
                # Snapshot the current buffer (shallow copy of array slices).
                raw_snap = {
                    't': list(raw['t']),
                    'id': list(raw['id']),
                    'dlc': list(raw['dlc']),
                    'payload_len': list(raw['payload_len']),
                    'ch': list(raw['ch']),
                    'bus_type': list(raw['bus_type']),
                    'flags': list(raw['flags']),
                    **{name: list(raw[name]) for name in _raw_mf4_byte_names()},
                }
                self.mdf_buffer = []
                self.base_name = f"{session_base}_part{part:04d}.tmp"
                self._write_mf4_raw(raw_snap)
            if os.path.exists(tmp_path):
                os.replace(tmp_path, out_path)
        except Exception:
            pass
        finally:
            self.base_name = old_base
            self.mdf_buffer = old_buffer

    def _flush_mf4_chunk(self, force: bool = False) -> None:
        """Write the current MF4 chunk as a standalone MF4 part.

        This is intentionally chunked (multiple files) so that a sudden power loss
        can only lose the last chunk in memory, while previous parts remain valid.
        """
        if asammdf is None or np is None:
            return

        # Decide which buffer we are using:
        # - raw-only: self._mf4_raw (default when decoded is disabled)
        # - legacy: self._mf4_chunk_buffer (when decoded is enabled)
        include_decoded = bool(self._mf4_include_decoded_effective())
        if include_decoded:
            buf = getattr(self, '_mf4_chunk_buffer', None)
            if not buf:
                return
            buf_len = len(buf)
        else:
            raw = getattr(self, '_mf4_raw', None)
            if not isinstance(raw, dict) or not raw.get('t'):
                return
            buf_len = len(raw.get('t') or [])

        now = time.time()

        # Derive a message-count threshold from target size and observed bytes/message.
        # Always respect the hard safety cap (_mf4_chunk_max_msgs).
        try:
            target_bytes = int(getattr(self, '_mf4_chunk_target_bytes', 0) or 0)
        except Exception:
            target_bytes = 0
        try:
            bpm = float(getattr(self, '_mf4_bytes_per_msg_est', 1024.0) or 1024.0)
        except Exception:
            bpm = 1024.0
        bpm = float(max(16.0, min(bpm, 65536.0)))

        target_msgs = None
        if target_bytes > 0:
            try:
                target_msgs = int(max(200, int(target_bytes / bpm)))
            except Exception:
                target_msgs = None

        # Estimate current data size in bytes.
        est_current_bytes = float(buf_len) * bpm

        # --- Time-based part split ---
        part_time_limit = float(getattr(self, '_mf4_part_time_limit_s', 0) or 0)
        part_start = float(getattr(self, '_mf4_part_start_time', 0) or 0)
        time_expired = (part_time_limit > 0 and buf_len > 0 and
                        (now - part_start) >= part_time_limit)

        # --- Size-based part split ---
        size_reached = (target_msgs is not None and buf_len >= int(target_msgs))

        should_split = bool(force) or time_expired or size_reached

        # --- Intermediate flush (power-loss safety) ---
        flush_interval = int(getattr(self, '_mf4_flush_interval_bytes', 0) or 0)
        last_intermediate = float(getattr(self, '_mf4_last_intermediate_est_bytes', 0) or 0)
        needs_intermediate = (flush_interval > 0 and buf_len > 0 and
                              (est_current_bytes - last_intermediate) >= flush_interval)

        if not should_split and not needs_intermediate:
            return

        # --- Intermediate flush path (write to disk but keep buffer) ---
        if needs_intermediate and not should_split:
            self._intermediate_flush_mf4(include_decoded)
            self._mf4_last_intermediate_est_bytes = est_current_bytes
            return

        # Split into one or more MF4 parts. This avoids producing oversized parts
        # under high load and prevents pathological filenames if base_name is
        # temporarily rewritten during atomic writes.
        parts_written = 0

        try:
            max_parts_per_call = int(os.getenv('MF4_MAX_PARTS_PER_FLUSH', '3') or 3)
        except Exception:
            max_parts_per_call = 3
        max_parts_per_call = int(max(1, min(max_parts_per_call, 20)))

        # Use a stable base name for part naming.
        session_base = getattr(self, 'session_base_name', None) or getattr(self, 'base_name', None) or getattr(self, 'log_dir', None) or ''
        if not session_base:
            session_base = self.base_name

        # Defensive: ensure session_base does NOT already include an extension.
        # If it does (e.g. accidental `.mf4`), part paths become `..._part0001.mf4.mf4` and
        # merge-on-stop won't recognize them.
        try:
            sb = str(session_base)
        except Exception:
            sb = session_base
        try:
            low = str(sb).lower()
            for ext in ('.mf4', '.mdf', '.dat'):
                if low.endswith(ext):
                    sb = str(sb)[: -len(ext)]
                    break
            if str(sb).lower().endswith('.tmp'):
                sb = str(sb)[:-4]
        except Exception:
            pass
        session_base = sb

        # Determine max messages per part based on target size and a conservative
        # bytes/message estimate.
        max_msgs_per_part = int(self._mf4_chunk_max_msgs)
        if target_msgs is not None:
            try:
                max_msgs_per_part = int(max(200, min(int(target_msgs), int(self._mf4_chunk_max_msgs))))
            except Exception:
                max_msgs_per_part = int(self._mf4_chunk_max_msgs)

        while True:
            include_decoded = bool(self._mf4_include_decoded_effective())
            if include_decoded:
                buf = getattr(self, '_mf4_chunk_buffer', None)
                if not buf:
                    break
                cur_len = len(buf)
            else:
                raw = getattr(self, '_mf4_raw', None)
                if not isinstance(raw, dict) or not raw.get('t'):
                    break
                cur_len = len(raw.get('t') or [])

            if (not force) and parts_written >= max_parts_per_call:
                break

            if include_decoded:
                chunk = list(buf[:max_msgs_per_part])
                if not chunk:
                    break
            else:
                n_take = int(min(max_msgs_per_part, cur_len))
                if n_take <= 0:
                    break
                raw_chunk = {
                    't': raw['t'][:n_take],
                    'id': raw['id'][:n_take],
                    'dlc': raw['dlc'][:n_take],
                    'payload_len': raw['payload_len'][:n_take],
                    'ch': raw['ch'][:n_take],
                    'bus_type': raw['bus_type'][:n_take],
                    'flags': raw['flags'][:n_take],
                    **{name: raw[name][:n_take] for name in _raw_mf4_byte_names()},
                }

            part = int(self._mf4_part_index)
            base = f"{session_base}_part{part:04d}"
            out_path = f"{base}.mf4"
            tmp_base = f"{base}.tmp"
            tmp_path = f"{tmp_base}.mf4"

            old_buffer = self.mdf_buffer
            old_base = self.base_name
            wrote = False
            try:
                if include_decoded:
                    self.mdf_buffer = chunk
                else:
                    self.mdf_buffer = []
                self.base_name = tmp_base
                if include_decoded:
                    self._write_mf4()
                else:
                    self._write_mf4_raw(raw_chunk)
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, out_path)
                    wrote = True
            finally:
                self.base_name = old_base
                self.mdf_buffer = old_buffer

            if not wrote:
                break

            self._notify_file_saved(
                out_path,
                kind='mf4_part',
                extra={'part_index': int(part)},
            )

            # Update bytes/message estimate using an exponential moving average.
            # This adapts after the first written part and keeps subsequent parts close
            # to the configured target size.
            try:
                sz = os.path.getsize(out_path)
                denom = (len(chunk) if include_decoded else int(len(raw_chunk.get('t') or [])))
                if sz > 0 and denom > 0:
                    obs = float(sz) / float(denom)
                    old = float(getattr(self, '_mf4_bytes_per_msg_est', 256.0) or 256.0)
                    # Faster convergence so size targeting works within a few parts.
                    est = (0.5 * old) + (0.5 * obs)
                    self._mf4_bytes_per_msg_est = float(max(16.0, min(est, 65536.0)))
            except Exception:
                pass

            # Drop the flushed messages.
            try:
                if include_decoded:
                    self._mf4_chunk_buffer = self._mf4_chunk_buffer[len(chunk):]
                else:
                    for k in ('t', 'id', 'dlc', 'payload_len', 'ch', 'bus_type', 'flags', *_raw_mf4_byte_names()):
                        del raw[k][:n_take]
            except Exception:
                pass

            self._mf4_part_index = part + 1
            self._mf4_last_flush = time.time()
            self._mf4_part_start_time = time.time()
            self._mf4_last_intermediate_est_bytes = 0.0
            parts_written += 1

            # Incremental merge: fold this part into the consolidated MF4
            # right away so stop() doesn't have to do a heavy all-at-once merge.
            if self._incremental_merge_enabled():
                try:
                    if not self._incremental_merge_new_part(out_path):
                        self._mf4_incremental_ok = False
                except Exception:
                    self._mf4_incremental_ok = False

        # Best-effort fsync the directory entry (not portable everywhere; ok if it fails)
        try:
            if self._fsync_enabled:
                # If no part was written, out_path may be undefined; guard.
                last_out = None
                try:
                    last_out = locals().get('out_path')
                except Exception:
                    last_out = None
                fd = os.open(os.path.dirname(str(last_out or self.base_name)) or '.', os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except Exception:
            pass

    def _notify_file_saved(self, file_path: str, *, kind: str, extra: dict | None = None) -> None:
        cb = getattr(self, 'on_file_saved', None)
        if not callable(cb):
            return
        if not file_path:
            return

        payload = {
            'path': str(file_path),
            'name': os.path.basename(str(file_path)),
            'kind': str(kind or 'file_saved'),
            'timestamp_ms': int(time.time() * 1000),
            'session_base_name': getattr(self, 'session_base_name', None) or getattr(self, 'base_name', None),
        }
        try:
            if os.path.exists(file_path):
                payload['size_bytes'] = int(os.path.getsize(file_path))
        except Exception:
            pass
        if isinstance(extra, dict):
            payload.update(extra)

        try:
            cb(payload)
        except Exception:
            pass

    def _write_mf4(self):
        if asammdf is None or np is None:
            print("MF4 write skipped: missing asammdf/numpy")
            return
        try:
            mdf = asammdf.MDF()

            timestamps: list[float] = []
            ids: list[int] = []
            dlcs: list[int] = []
            payload_lengths: list[int] = []
            data_bytes: list[list[int]] = []
            channels: list[int] = []
            bus_types: list[int] = []
            flags_list: list[int] = []

            # Optional: decoded signals (if DBC decode is active upstream)
            decoded_series: dict[str, dict[str, list[float]]] = {}
            # Keep compact CANx.Signal names when unique; split to
            # CANx.Message.Signal if multiple messages share the same signal key.
            decoded_base_owner: dict[str, str] = {}
            decoded_collided_bases: set[str] = set()

            include_decoded = bool(self._mf4_include_decoded_effective())
            include_raw = bool(self._mf4_include_raw_effective())
            try:
                decoded_max_signals = int(os.getenv('MF4_DECODED_MAX_SIGNALS', '0') or 0)
            except Exception:
                decoded_max_signals = 0
            decoded_max_signals = max(0, decoded_max_signals)

            def _coerce_numeric(v):
                if v is None:
                    return None
                try:
                    if isinstance(v, bool):
                        return float(1.0 if v else 0.0)
                except Exception:
                    pass
                try:
                    if isinstance(v, (int, float)):
                        return float(v)
                except Exception:
                    pass
                try:
                    if hasattr(v, 'dtype') and hasattr(v, 'item'):
                        return float(v.item())
                except Exception:
                    pass
                try:
                    vv = getattr(v, 'value', None)
                    if vv is not None:
                        if isinstance(vv, bool):
                            return float(1.0 if vv else 0.0)
                        if isinstance(vv, (int, float)):
                            return float(vv)
                        try:
                            return float(vv)
                        except Exception:
                            return None
                except Exception:
                    pass
                try:
                    return float(v)
                except Exception:
                    return None

            for msg in self.mdf_buffer:
                if _mf4_should_skip_message(msg):
                    continue
                ts = msg.get('timestamp')
                try:
                    ts_f = float(ts)
                except Exception:
                    ts_f = time.time() * 1000.0
                # Heuristic: values > 1e11 are ms epoch; otherwise seconds.
                t_s = (ts_f / 1000.0) if ts_f > 1e11 else ts_f
                timestamps.append(float(t_s))

                # decoded signals (best-effort numeric only)
                try:
                    if not include_decoded:
                        raise KeyError('decoded disabled')
                    dec = msg.get('decoded')
                    if isinstance(dec, dict):
                        sigs = dec.get('signals')
                        if isinstance(sigs, dict):
                            msg_name = str(dec.get('name') or '').strip()
                            for sn, sv in sigs.items():
                                name = str(sn or '').strip()
                                if not name:
                                    continue
                                fv = _coerce_numeric(sv)
                                if fv is None:
                                    continue
                                base_name = self._decoded_mf4_signal_name(msg, name)
                                if not base_name:
                                    continue
                                msg_series_name = self._decoded_mf4_signal_name_with_message(
                                    msg,
                                    msg_name,
                                    name,
                                    frame_id=msg.get('id'),
                                )
                                if not msg_series_name:
                                    msg_series_name = base_name

                                if base_name in decoded_collided_bases:
                                    series_name = msg_series_name
                                else:
                                    owner_name = decoded_base_owner.get(base_name)
                                    if owner_name is None:
                                        decoded_base_owner[base_name] = msg_series_name
                                        series_name = base_name
                                    elif owner_name == msg_series_name:
                                        series_name = base_name
                                    else:
                                        decoded_collided_bases.add(base_name)
                                        prev = decoded_series.pop(base_name, None)
                                        if prev is not None:
                                            prev_dst = decoded_series.get(owner_name)
                                            if prev_dst is None:
                                                decoded_series[owner_name] = prev
                                            else:
                                                prev_dst['t'].extend(prev.get('t', []))
                                                prev_dst['y'].extend(prev.get('y', []))
                                        series_name = msg_series_name

                                ds = decoded_series.get(series_name)
                                if ds is None:
                                    ds = {'t': [], 'y': []}
                                    decoded_series[series_name] = ds
                                ds['t'].append(float(t_s))
                                ds['y'].append(float(fv))
                except Exception:
                    pass

                ids.append(int(msg.get('id', 0)))
                dlcs.append(int(msg.get('dlc', 0)))
                payload_lengths.append(int(msg.get('dlc', 0)))

                ch_raw = msg.get('channel', 0)
                try:
                    ch_val = int(ch_raw)
                except Exception:
                    ch_val = 255 if str(ch_raw).strip().upper() == 'ETH' else 0
                channels.append(int(ch_val))
                bus_types.append(int(_raw_mf4_bus_type_code(msg.get('type', 'CAN'))))
                flags_list.append(int(msg.get('flags', 0)))

                raw = msg.get('data')
                data_list: list[int] = []
                try:
                    if isinstance(raw, list):
                        data_list = [int(x) & 0xFF for x in raw]
                    elif isinstance(raw, (bytes, bytearray)):
                        data_list = list(raw)
                    elif isinstance(raw, str):
                        s = raw.strip().lower()
                        s = ''.join([c for c in s if c in '0123456789abcdef'])
                        if s:
                            if len(s) % 2 == 1:
                                s = '0' + s
                            data_list = list(bytes.fromhex(s))
                except Exception:
                    data_list = []

                d = (data_list + [0] * RAW_MF4_PAYLOAD_BYTES)[:RAW_MF4_PAYLOAD_BYTES]
                data_bytes.append(d)

            if not timestamps:
                return

            t = np.asarray(timestamps, dtype=np.float64)
            raw_sigs = [
                asammdf.Signal(np.asarray(ids, dtype=np.uint32), t, name='CAN_ID'),
                asammdf.Signal(np.asarray(dlcs, dtype=np.uint16), t, name='DLC'),
                asammdf.Signal(np.asarray(payload_lengths, dtype=np.uint16), t, name='PayloadLength'),
                asammdf.Signal(np.asarray(channels, dtype=np.uint8), t, name='Channel'),
                asammdf.Signal(np.asarray(bus_types, dtype=np.uint8), t, name='BusType'),
                asammdf.Signal(np.asarray(flags_list, dtype=np.uint32), t, name='Flags'),
            ]

            db = np.asarray(data_bytes, dtype=np.uint8)
            for i in range(RAW_MF4_PAYLOAD_BYTES):
                raw_sigs.append(asammdf.Signal(db[:, i], t, name=f'DataByte{i}'))

            # Raw frames group (optional)
            if include_raw:
                mdf.append(raw_sigs, acq_name='CAN_Raw', comment='')

            # Decoded signals: one channel group per signal to match the imported MF4 style
            if include_decoded and decoded_series:
                names = list(decoded_series.keys())

                if decoded_max_signals > 0 and len(names) > decoded_max_signals:
                    try:
                        names.sort(key=lambda n: len((decoded_series.get(n) or {}).get('t', [])), reverse=True)
                        names = names[:decoded_max_signals]
                    except Exception:
                        names = sorted(names)[:decoded_max_signals]
                else:
                    names = sorted(names)

                group_index = 1
                for name in names:
                    ds = decoded_series.get(name) or {}
                    t_arr = np.asarray(ds.get('t', []), dtype=np.float64)
                    y_arr = np.asarray(ds.get('y', []), dtype=np.float64)
                    if t_arr.size == 0 or y_arr.size == 0:
                        continue
                    n = int(min(t_arr.size, y_arr.size))
                    if n <= 0:
                        continue
                    sig = asammdf.Signal(y_arr[:n], t_arr[:n], name=name)
                    mdf.append(sig, acq_name=f"{name}_R{group_index}", comment='')
                    group_index += 1

            mdf.save(f"{self.base_name}.mf4", overwrite=True)
            print(f"MF4 saved: {self.base_name}.mf4")
        except Exception as e:
            try:
                err_path = f"{self.base_name}.mf4.error.txt"
                with open(err_path, 'w') as f:
                    f.write(str(e))
            except Exception:
                pass
            print(f"Error writing MF4: {e}")

    def _write_mf4_raw(self, raw_chunk: dict) -> None:
        """Write a raw-only MF4 part from primitive arrays.

        raw_chunk keys: t, id, dlc, payload_len, ch, bus_type, flags, db0..db63
        """
        if asammdf is None or np is None:
            return
        try:
            t = np.asarray(raw_chunk.get('t', []), dtype=np.float64)
            if t.size == 0:
                return
            ids = np.asarray(raw_chunk.get('id', []), dtype=np.uint32)
            dlc = np.asarray(raw_chunk.get('dlc', []), dtype=np.uint16)
            payload_len = np.asarray(raw_chunk.get('payload_len', raw_chunk.get('dlc', [])), dtype=np.uint16)
            ch = np.asarray(raw_chunk.get('ch', []), dtype=np.uint8)
            bus_type = np.asarray(raw_chunk.get('bus_type', []), dtype=np.uint8)
            flags = np.asarray(raw_chunk.get('flags', []), dtype=np.uint32)
            if bus_type.size == 0:
                bus_type = np.ones_like(ids, dtype=np.uint8)

            mdf = asammdf.MDF()
            raw_sigs = [
                asammdf.Signal(ids, t, name='CAN_ID'),
                asammdf.Signal(dlc, t, name='DLC'),
                asammdf.Signal(payload_len, t, name='PayloadLength'),
                asammdf.Signal(ch, t, name='Channel'),
                asammdf.Signal(bus_type, t, name='BusType'),
                asammdf.Signal(flags, t, name='Flags'),
            ]
            for i in range(RAW_MF4_PAYLOAD_BYTES):
                b = np.asarray(raw_chunk.get(f'db{i}', []), dtype=np.uint8)
                raw_sigs.append(asammdf.Signal(b, t, name=f'DataByte{i}'))
            mdf.append(raw_sigs)
            mdf.save(f"{self.base_name}.mf4", overwrite=True)
            print(f"MF4 saved: {self.base_name}.mf4")
        except Exception as e:
            try:
                err_path = f"{self.base_name}.mf4.error.txt"
                with open(err_path, 'w') as f:
                    f.write(str(e))
            except Exception:
                pass
