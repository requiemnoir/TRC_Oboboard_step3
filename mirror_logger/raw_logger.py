"""
raw_logger.py — Logger MF4 raw-only ad alta fedeltà.

Hot-path performance:
- buffer pre-allocato in numpy structured array (recarray)
- nessun bytearray dinamico, nessun loop Python per bytes payload
- 1 sola colonna `payload` (uint8[64]) invece di 64 colonne db0..db63
- worker drena la queue in batch fino a 1024 frame
- flush via asammdf.append() di Signal con view zero-copy sul recarray

Schema MF4:
  t        : float64  — secondi epoch (asse MDF)
  ts_pkt   : float64  — Scapy packet.time (high-res kernel)
  ts_ns    : uint64   — nanosecondo epoch wall-clock (timestamp principale)
  ch       : uint16   — channel_id (100+net=CAN, 200+net=FlexRay, 150+net=LIN)
  bus_type : uint8    — 1=CAN 2=CAN-FD 3=FlexRay 4=LIN
  arb_id   : uint32   — CAN arb-ID / FlexRay slot-ID
  flags    : uint32   — cycle FlexRay / CAN flags
  dlc      : uint8    — lunghezza dati effettiva (0-64)
  payload  : uint8[64]— payload raw (zero-padded a 64)
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Optional

from mirror_parser import RawFrame

try:
    from asammdf import MDF, Signal
    import numpy as np
    _HAS_MF4 = True
except Exception:
    _HAS_MF4 = False
    np = None       # type: ignore
    MDF = None      # type: ignore
    Signal = None   # type: ignore


_PAYLOAD_BYTES = 64

_BUS_TYPE_CODE = {
    'CAN':     1,
    'CAN-FD':  2,
    'FLEXRAY': 3,
    'LIN':     4,
}


def _bus_code(frame_type: str) -> int:
    return _BUS_TYPE_CODE.get(str(frame_type or '').upper(), 1)


_FRAME_DTYPE = None
if _HAS_MF4:
    _FRAME_DTYPE = np.dtype([
        ('t',        '<f8'),
        ('ts_pkt',   '<f8'),
        ('ts_ns',    '<u8'),
        ('ch',       '<u2'),
        ('bus_type', 'u1'),
        ('_pad1',    'u1'),
        ('arb_id',   '<u4'),
        ('flags',    '<u4'),
        ('dlc',      'u1'),
        ('_pad2',    '3u1'),
        ('payload',  f'({_PAYLOAD_BYTES},)u1'),
    ])

# Pre-allocazione: blocchi di 64k frame ≈ 6 MB ciascuno
_BLOCK_FRAMES = 65_536


# ---------------------------------------------------------------------------
# RawLogger
# ---------------------------------------------------------------------------

class RawLogger:
    """Logger MF4 raw thread-safe con queue interna."""

    def __init__(
        self,
        log_dir: str | None = None,
        *,
        chunk_interval_s: float = 15.0,
        chunk_max_frames: int   = 2_000_000,
        flush_interval_frames: int = 5_000,
        flush_interval_s: float = 10.0,
        queue_max: int          = 524_288,
        put_timeout_ms: float   = 25.0,
    ):
        if not _HAS_MF4:
            raise ImportError('asammdf + numpy richiesti per RawLogger')

        if log_dir is None:
            log_dir = str(Path(__file__).parent / 'logs')
        self.log_dir = str(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

        self._chunk_interval_s    = max(1.0, float(chunk_interval_s))
        self._chunk_max_frames    = max(1000, int(chunk_max_frames))
        self._flush_interval_frames = max(100, int(flush_interval_frames))
        self._flush_interval_s = max(0.0, float(flush_interval_s))
        self._queue_max           = max(1024, int(queue_max))
        self._put_timeout_s = max(0.0, min(float(put_timeout_ms) / 1000.0, 2.0))

        # Intermediate flush abilitato solo se ha senso rispetto al chunk:
        # se l'intervallo intermedio supera (o eguaglia) quello del chunk, è
        # ridondante (il chunk flush lo precede). Idem se l'utente lo annulla
        # impostando flush_interval_s <= 0.
        self._intermediate_enabled = (
            self._flush_interval_s > 0.0
            and self._flush_interval_s < self._chunk_interval_s
        )
        # Soglia minima di "frame nuovi dall'ultimo flush": evita di
        # ri-scrivere l'intero file MF4 quando il delta è trascurabile.
        self._intermediate_min_new_frames = max(
            self._flush_interval_frames // 4, 250,
        )

        self._queue: Queue[RawFrame | None] = Queue(maxsize=self._queue_max)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.active         = False
        self.session_id     = ''
        self.base_path      = ''
        self.start_time_ns  = 0
        self.frame_count    = 0
        self.dropped_count  = 0
        self.flush_errors   = 0
        self._last_drop_log = 0.0

        self._blocks: list = []
        self._cur_idx = 0
        self._buf_count = 0
        self._part_index = 0
        self._last_flush_count = 0
        self._chunk_start_time = 0.0
        self._last_intermediate_flush = 0.0

        self._stats_lock = threading.Lock()
        self._io_lock = threading.RLock()

    # ------------------------------------------------------------------
    def start(self) -> str:
        if self.active:
            raise RuntimeError('logger già attivo')
        ts = time.strftime('%Y%m%d_%H%M%S')
        self.session_id    = ts
        self.base_path     = os.path.join(self.log_dir, f'session_{ts}')
        self.start_time_ns = time.time_ns()
        self.frame_count   = 0
        self.dropped_count = 0
        self.flush_errors = 0
        self._part_index   = 0
        self._last_flush_count = 0
        self._chunk_start_time = time.monotonic()
        self._last_intermediate_flush = time.monotonic()
        self._reset_buffer()

        self.active = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, name='mirror-raw-logger', daemon=True,
        )
        self._thread.start()
        print(f'[RawLogger] sessione → {self.base_path}', flush=True)
        return self.base_path

    def stop(self, timeout_s: float = 30.0) -> dict:
        if not self.active:
            return {}
        self.active = False
        self._stop_event.set()
        # Best-effort: inserisci il sentinel; se la queue è piena, fai
        # spazio scartando i frame più vecchi (a quel punto siamo già in
        # uscita) per garantire che il worker riceva il None e non resti
        # bloccato fino al timeout.
        try:
            self._queue.put_nowait(None)
        except Full:
            try:
                while True:
                    self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=timeout_s)
        stats = self._session_stats()
        print(f'[RawLogger] terminata — {stats}', flush=True)
        return stats

    def force_flush(self, *, timeout_s: float = 5.0) -> bool:
        """Forza un flush sincrono del chunk corrente su disco.

        Usato per incident snapshot cross-process: garantisce che tutto ciò
        che è già stato accodato fino ora sia visibile nei file MF4 prima
        che il chiamante li copi.

        Restituisce True se il flush è andato a buon fine, False altrimenti.
        Non interrompe la sessione: il logger continua a girare normalmente.
        """
        if not self.active or self._thread is None:
            return False
        if not hasattr(self, '_flush_request_lock'):
            self._flush_request_lock = threading.Lock()
        acquired = self._flush_request_lock.acquire(timeout=timeout_s)
        if not acquired:
            return False
        try:
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            with self._io_lock:
                while time.monotonic() < deadline:
                    try:
                        frame = self._queue.get_nowait()
                    except Empty:
                        break
                    if frame is None:
                        try:
                            self._queue.put_nowait(None)
                        except Exception:
                            pass
                        break
                    self._append(frame)

                # Materializza e scrive il chunk corrente con `finalize=False`
                # — non incrementa part_index, può essere ri-flushato dal worker.
                self._flush_current_part(finalize=False)
                self._last_flush_count = self._buf_count
                self._last_intermediate_flush = time.monotonic()
            return True
        except Exception as e:
            print(f'[RawLogger] force_flush err: {e}', flush=True)
            return False
        finally:
            self._flush_request_lock.release()

    def current_part_path(self) -> Optional[str]:
        """Path del file MF4 in scrittura nella sessione corrente."""
        if not self.active:
            return None
        return f'{self.base_path}_p{self._part_index:04d}.mf4'

    def log(self, frame: RawFrame) -> None:
        if not self.active:
            return
        try:
            if self._put_timeout_s > 0:
                self._queue.put(frame, timeout=self._put_timeout_s)
            else:
                self._queue.put_nowait(frame)
        except Full:
            with self._stats_lock:
                self.dropped_count += 1
            now = time.monotonic()
            if now - self._last_drop_log >= 5.0:
                self._last_drop_log = now
                print(
                    f'[RawLogger] ATTENZIONE: queue piena ({self._queue_max}) — frame scartati={self.dropped_count}',
                    flush=True,
                )
        except Exception:
            with self._stats_lock:
                self.dropped_count += 1

    def stats(self) -> dict:
        with self._stats_lock:
            fc = self.frame_count
            dc = self.dropped_count
        elapsed = max(1e-3, (time.time_ns() - self.start_time_ns) / 1e9)
        return {
            'active':        self.active,
            'session_id':    self.session_id,
            'frame_count':   fc,
            'dropped_count': dc,
            'elapsed_s':     round(elapsed, 1),
            'fps':           round(fc / elapsed, 1),
            'drop_ratio':    round(dc / max(1, fc + dc), 4),
            'flush_errors':  self.flush_errors,
            'part_index':    self._part_index,
            'queue_size':    self._queue.qsize(),
            'queue_max':     self._queue_max,
        }

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        BATCH = 1024
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 50   # ~50s di problemi sequenziali → abort

        while True:
            try:
                try:
                    first = self._queue.get(timeout=1.0)
                except Empty:
                    if self._stop_event.is_set():
                        break
                    self._maybe_intermediate_flush(force=False)
                    self._maybe_flush_chunk(force=False)
                    consecutive_errors = 0
                    continue

                if first is None:
                    break

                self._append(first)
                for _ in range(BATCH - 1):
                    try:
                        nxt = self._queue.get_nowait()
                    except Empty:
                        break
                    if nxt is None:
                        self._stop_event.set()
                        break
                    self._append(nxt)

                self._maybe_intermediate_flush(force=False)
                self._maybe_flush_chunk(force=False)
                # Hot-path OK → reset error counter
                consecutive_errors = 0

            except Exception as e:
                # Auto-recovery: invece di morire silenziosamente, contiamo
                # gli errori. Cause possibili: asammdf sollevato OSError per
                # disco pieno, IOError per filesystem read-only, MemoryError
                # in caso di queue troppo grande, ecc. Il worker continua
                # e proverà di nuovo al prossimo ciclo. Se gli errori
                # persistono per >50 cicli (~50s a queue piena) abortiamo
                # per evitare loop infinito sintomatico.
                with self._stats_lock:
                    self.flush_errors += 1
                consecutive_errors += 1
                print(
                    f'[RawLogger] worker err #{consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}: {e}',
                    flush=True,
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(
                        f'[RawLogger] worker abort dopo {MAX_CONSECUTIVE_ERRORS} '
                        f'errori consecutivi — sessione corrotta, stop forzato',
                        flush=True,
                    )
                    break
                # Pausa breve prima di ritentare per non spammare i log
                try:
                    self._stop_event.wait(0.2)
                except Exception:
                    pass

        # Tentativo finale di flush (best-effort)
        try:
            self._maybe_flush_chunk(force=True)
        except Exception as e:
            print(f'[RawLogger] flush finale err: {e}', flush=True)

    def _append(self, f: RawFrame) -> None:
        with self._io_lock:
            if self._cur_idx >= _BLOCK_FRAMES:
                self._blocks.append(np.zeros(_BLOCK_FRAMES, dtype=_FRAME_DTYPE))
                self._cur_idx = 0

            rec = self._blocks[-1][self._cur_idx]
            rec['t']        = f.ts_ns / 1e9
            rec['ts_pkt']   = f.ts_pkt or 0.0
            rec['ts_ns']    = f.ts_ns
            rec['ch']       = f.channel_id & 0xFFFF
            rec['bus_type'] = _bus_code(f.frame_type)
            rec['arb_id']   = f.arb_id & 0xFFFFFFFF
            rec['flags']    = f.flags & 0xFFFFFFFF

            data = f.data
            n = min(len(data), _PAYLOAD_BYTES)
            rec['dlc'] = n
            if n > 0:
                rec['payload'][:n] = np.frombuffer(data, dtype=np.uint8, count=n)
            # i blocchi sono pre-azzerati (np.zeros), il padding rimane 0

            self._cur_idx   += 1
            self._buf_count += 1
        with self._stats_lock:
            self.frame_count += 1

    def _maybe_intermediate_flush(self, force: bool) -> None:
        if self._buf_count <= 0 or not self._intermediate_enabled:
            return
        frames_delta = self._buf_count - self._last_flush_count
        if frames_delta < self._intermediate_min_new_frames:
            return
        elapsed = time.monotonic() - self._last_intermediate_flush
        needs_time = force or elapsed >= self._flush_interval_s
        needs_frames = frames_delta >= self._flush_interval_frames
        if needs_time or needs_frames:
            self._flush_current_part(finalize=False)
            self._last_flush_count = self._buf_count
            self._last_intermediate_flush = time.monotonic()

    def _maybe_flush_chunk(self, force: bool) -> None:
        elapsed = time.monotonic() - self._chunk_start_time
        if force or self._buf_count >= self._chunk_max_frames or elapsed >= self._chunk_interval_s:
            if self._buf_count > 0:
                self._flush_current_part(finalize=True)
                self._reset_buffer()
                self._chunk_start_time = time.monotonic()
                self._last_flush_count = 0
                self._part_index += 1

    def _materialize(self) -> 'np.ndarray':
        if not self._blocks:
            return np.zeros(0, dtype=_FRAME_DTYPE)
        if len(self._blocks) == 1:
            return self._blocks[0][:self._cur_idx]
        full = self._blocks[:-1]
        last = self._blocks[-1][:self._cur_idx]
        return np.concatenate(full + [last])

    def _flush_current_part(self, finalize: bool) -> None:
        with self._io_lock:
            if self._buf_count == 0:
                return

            n = self._buf_count
            try:
                data = self._materialize()
                t_arr = data['t']

                def _sig(field: str) -> Signal:
                    return Signal(samples=data[field], timestamps=t_arr, name=field)

                signals = [
                    _sig('ts_pkt'),
                    _sig('ts_ns'),
                    _sig('ch'),
                    _sig('bus_type'),
                    _sig('arb_id'),
                    _sig('flags'),
                    _sig('dlc'),
                    Signal(samples=data['payload'], timestamps=t_arr, name='payload'),
                ]

                mdf = MDF()
                mdf.append(signals, comment='mirror_raw')
                path = f'{self.base_path}_p{self._part_index:04d}.mf4'
                mdf.save(path, overwrite=True)
                mdf.close()
                # fsync della directory: garantisce che il nuovo file (o la
                # rinomina su overwrite) sia visibile dopo un crash.
                try:
                    dir_fd = os.open(os.path.dirname(path) or '.', os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except (OSError, AttributeError):
                    pass

                if finalize:
                    size_mb = os.path.getsize(path) / 1_048_576
                    print(
                        f'[RawLogger] parte p{self._part_index:04d} — '
                        f'{n} frame, {size_mb:.1f} MB → {os.path.basename(path)}',
                        flush=True,
                    )
            except Exception as e:
                with self._stats_lock:
                    self.flush_errors += 1
                print(f'[RawLogger] errore flush MF4: {e}', flush=True)

    def _reset_buffer(self) -> None:
        self._blocks = [np.zeros(_BLOCK_FRAMES, dtype=_FRAME_DTYPE)]
        self._cur_idx = 0
        self._buf_count = 0

    def _session_stats(self) -> dict:
        with self._stats_lock:
            return {
                'session_id':    self.session_id,
                'frame_count':   self.frame_count,
                'dropped_count': self.dropped_count,
                'parts':         self._part_index,
                'base_path':     self.base_path,
            }
