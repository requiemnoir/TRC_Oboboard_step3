"""
mirror_udp_listener.py — Bridge live-data tra mirror_logger e KBM.

Apre un socket UDP su :30490 (o porta configurabile) PARALLELO al
mirror_logger.MirrorCapture (AF_PACKET). Su Linux i due tipi di socket
ricevono entrambi i pacchetti senza interferenza: il mirror_logger cattura
a livello link layer + filtro BPF; questo listener legge a livello UDP.

I pacchetti mirror in formato AUTOSAR ISO 23150 (e i 3 fallback supportati
da MirrorParser: VAG SOME/IP, IronBird, RawCAN-in-UDP) vengono decodificati
in RawFrame e iniettati nel BusManager via inject_frame(capture_origin='mirror').

Il pipeline `inject_frame()` si occupa di:
  • decode DBC/ARXML/FIBEX
  • notify listeners (Sentinel incluso)
  • emit socket.io 'bus_data' / 'bus_data_batch' al frontend

Risultato: la UI Live Traffic mostra i frame del bus mirror ricevuti dal
mirror_logger come se arrivassero da canlib, con badge "mirror" nell'etichetta.

Attivazione:
  KBSM_MIRROR_LISTEN_ENABLED=1   (default: off, per non rompere setup esistenti)
  KBSM_MIRROR_LISTEN_PORT=30490
  KBSM_MIRROR_LISTEN_HOST=0.0.0.0   (es. 127.0.0.1 per loopback only)
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Permetti l'import di MirrorParser dal modulo mirror_logger dello stesso repo
_MIRROR_LOGGER_DIR = Path(__file__).resolve().parent.parent.parent / 'mirror_logger'
if str(_MIRROR_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_MIRROR_LOGGER_DIR))

try:
    from mirror_parser import MirrorParser, RawFrame  # type: ignore
    _HAS_MIRROR_PARSER = True
except Exception as _e:                                # pragma: no cover
    MirrorParser = None      # type: ignore
    RawFrame = None          # type: ignore
    _HAS_MIRROR_PARSER = False
    _MIRROR_PARSER_IMPORT_ERR = _e


class MirrorUDPListener:
    """Listener UDP del bus mirror per la UI Live Data del KBM.

    Uso:
        listener = MirrorUDPListener(bus_manager=manager,
                                     port=30490, host='0.0.0.0')
        listener.start()
        ...
        listener.stop()
    """

    _RCVBUF_BYTES = 16 * 1024 * 1024     # 16 MB
    _MAX_DGRAM    = 65535

    def __init__(
        self,
        bus_manager,
        *,
        port: int = 30490,
        host: str = '0.0.0.0',
        on_error: Optional[Callable[[str], None]] = None,
    ):
        if not _HAS_MIRROR_PARSER:
            raise ImportError(
                f'MirrorParser non importabile da {_MIRROR_LOGGER_DIR}: '
                f'{_MIRROR_PARSER_IMPORT_ERR}'
            )
        if bus_manager is None:
            raise ValueError('bus_manager è obbligatorio')

        self.bus_manager = bus_manager
        self.port = int(port)
        self.host = str(host or '0.0.0.0')
        self._on_error = on_error or (lambda _msg: None)

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_evt = threading.Event()

        # Statistiche
        self._lock = threading.Lock()
        self._pkts_recv = 0
        self._frames_emitted = 0
        self._parse_errors = 0
        self._inject_errors = 0
        self._last_recv_ts: float = 0.0
        self._stats_window_start = time.monotonic()
        self._stats_window_pkts = 0
        self._stats_window_frames = 0
        self._pps = 0.0
        self._fps = 0.0

        # Parser AUTOSAR + fallback (riusa quello del mirror_logger)
        self._parser = MirrorParser(
            callback=self._on_frame_extracted,
            dedupe_window_s=0.0,   # UDP: nessun dedup
        )

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._RCVBUF_BYTES)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        sock.settimeout(0.5)
        sock.bind((self.host, self.port))
        self._sock = sock
        self._running = True
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._recv_loop,
            name='kbm-mirror-udp-listener',
            daemon=True,
        )
        self._thread.start()

        # Periodic stats log (off di default; abilita con
        # KBSM_MIRROR_LISTEN_STATS_LOG_S=30).
        try:
            log_interval = float(os.environ.get('KBSM_MIRROR_LISTEN_STATS_LOG_S', '0') or '0')
        except ValueError:
            log_interval = 0.0
        if log_interval > 0:
            self._stats_log_thread = threading.Thread(
                target=self._stats_log_loop,
                args=(log_interval,),
                name='kbm-mirror-udp-stats',
                daemon=True,
            )
            self._stats_log_thread.start()

        print(
            f'[MirrorUDPListener] in ascolto su udp://{self.host}:{self.port} '
            f'(MirrorParser AUTOSAR/VAG/IronBird/RawCAN)',
            flush=True,
        )

    def _stats_log_loop(self, interval_s: float) -> None:
        while self._running and not self._stop_evt.wait(interval_s):
            try:
                s = self.stats()
                print(
                    f'[MirrorUDPListener] pkts={s["pkts_received"]} '
                    f'frames={s["frames_emitted"]} parse_err={s["parse_errors"]} '
                    f'inject_err={s["inject_errors"]} pps={s["pps"]:.1f} fps={s["fps"]:.1f}',
                    flush=True,
                )
            except Exception:
                pass

    def stop(self, timeout_s: float = 3.0) -> None:
        self._running = False
        self._stop_evt.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None

    # ------------------------------------------------------------------
    # Callback parser: per ogni frame estratto inietta nel BusManager
    # ------------------------------------------------------------------

    def _on_frame_extracted(self, raw_frame: 'RawFrame') -> None:
        try:
            # BusManager.inject_frame fa: process_frame + decode +
            # listener notify + emit socketio('bus_data'/'bus_data_batch').
            # capture_origin='mirror' viene riconosciuto dal frontend
            # (vedi buildBusLogRow in app.js) per aggiungere il badge.
            self.bus_manager.inject_frame(
                channel_id=int(raw_frame.channel_id),
                arb_id=int(raw_frame.arb_id),
                data=raw_frame.data,
                flags=int(raw_frame.flags or 0),
                frame_type=str(raw_frame.frame_type or 'CAN'),
                capture_origin='mirror',
            )
            with self._lock:
                self._frames_emitted += 1
                self._stats_window_frames += 1
        except Exception as e:
            with self._lock:
                self._inject_errors += 1
            self._on_error(f'inject_frame err: {e}')

    # ------------------------------------------------------------------
    # Recv loop
    # ------------------------------------------------------------------

    def _recv_loop(self) -> None:
        sock = self._sock
        parse = self._parser.parse

        while self._running:
            try:
                data, _addr = sock.recvfrom(self._MAX_DGRAM)
            except socket.timeout:
                self._refresh_rate()
                continue
            except (OSError, ValueError):
                if self._running:
                    self._on_error('recv error, retrying')
                continue

            if not data:
                continue

            now_pkt = time.time()
            try:
                parse(data, ts_pkt=now_pkt)
            except Exception as e:
                with self._lock:
                    self._parse_errors += 1
                self._on_error(f'parse err: {e}')

            with self._lock:
                self._pkts_recv += 1
                self._stats_window_pkts += 1
                self._last_recv_ts = now_pkt

            self._refresh_rate()

    def _refresh_rate(self) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._stats_window_start
            if elapsed < 2.0:
                return
            self._pps = self._stats_window_pkts / elapsed
            self._fps = self._stats_window_frames / elapsed
            self._stats_window_pkts = 0
            self._stats_window_frames = 0
            self._stats_window_start = now

    # ------------------------------------------------------------------
    # Stats / health
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                'running':         self._running,
                'port':            self.port,
                'host':            self.host,
                'pkts_received':   self._pkts_recv,
                'frames_emitted':  self._frames_emitted,
                'parse_errors':    self._parse_errors,
                'inject_errors':   self._inject_errors,
                'pps':             round(self._pps, 1),
                'fps':             round(self._fps, 1),
                'last_recv_ts':    self._last_recv_ts,
            }


# ---------------------------------------------------------------------------
# Factory di avvio condizionale dal app.py del KBM
# ---------------------------------------------------------------------------

def maybe_start(bus_manager, *, config_resolver: Optional[Callable[[], dict]] = None) -> Optional[MirrorUDPListener]:
    """Helper: avvia il listener solo se KBSM_MIRROR_LISTEN_ENABLED=1.

    Ritorna l'istanza se avviato, None altrimenti.
    """
    flag = str(os.environ.get('KBSM_MIRROR_LISTEN_ENABLED', '0')).strip().lower()
    if flag not in {'1', 'true', 'yes', 'on'}:
        return None
    if not _HAS_MIRROR_PARSER:
        print(
            '[MirrorUDPListener] DISABILITATO: MirrorParser non importabile '
            f'(controlla {_MIRROR_LOGGER_DIR})',
            flush=True,
        )
        return None
    port_raw = os.environ.get('KBSM_MIRROR_LISTEN_PORT')
    if port_raw is None or str(port_raw).strip() == '':
        port_raw = 30490
        if callable(config_resolver):
            try:
                cfg = config_resolver() or {}
                gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
                port_raw = gm.get('dest_port') or cfg.get('mirror_dest_port') or 30490
            except Exception:
                port_raw = 30490
    try:
        port = int(str(port_raw), 0) & 0xFFFF
    except Exception:
        port = 30490
    if port <= 0:
        port = 30490
    host = str(os.environ.get('KBSM_MIRROR_LISTEN_HOST', '0.0.0.0') or '0.0.0.0').strip()
    try:
        listener = MirrorUDPListener(bus_manager, port=port, host=host)
        listener.start()
        return listener
    except OSError as e:
        # Tipicamente: porta occupata (se il KBM gira sullo stesso host del
        # mirror_logger CON la cattura AF_PACKET su QEMU SLIRP, il bind UDP
        # può fallire). In quel caso loggiamo e proseguiamo senza listener.
        print(f'[MirrorUDPListener] bind fallito su :{port}: {e}', flush=True)
        return None
    except Exception as e:
        print(f'[MirrorUDPListener] errore avvio: {e}', flush=True)
        return None
