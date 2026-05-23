#!/usr/bin/env python3
"""Standalone MF4 raw-to-decoded converter.

Reads a raw MF4 file (CAN / FlexRay / LIN frames captured by the live
logger), decodes every frame using the DBC, ARXML, and FIBEX databases
shipped alongside this script, and writes a new MF4 file containing one
time-series channel per decoded signal.

Usage
-----
    python decode_mf4.py <raw.mf4>                 # → raw_decoded.mf4
    python decode_mf4.py <raw.mf4> -o decoded.mf4  # explicit output
    python decode_mf4.py <raw.mf4> --list-signals   # show decodable signals
    python decode_mf4.py <raw.mf4> --signals "ESP_21.ESP_v_Signal,Motor_12.Motor_Moment"
    python decode_mf4.py <raw.mf4> --channel 1      # decode only CAN channel 1
    python decode_mf4.py <raw.mf4> --start 10 --end 60   # time window (seconds)

The script auto-discovers databases in the ``databases/`` sub-folders
relative to its own location.  You can override with ``--dbc``, ``--arxml``,
``--fibex`` flags.

Requirements
------------
    pip install -r requirements.txt
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import multiprocessing
import os
import pickle
import re
import shutil
import sys
import time
from array import array
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import asammdf
from asammdf import Signal as AsamSignal

# Local modules — work both as a package import and as standalone script.
try:
    from .dbc_loader import DBCLoader, load_dbc_database, _normalize_decoded_signals
    from .arxml_parser import (parse_arxml_files, parse_arxml, ArxmlCatalog,
                               get_active_catalog, load_catalog_from_directory,
                               list_arxml_files)
    from .arxml_decoder import ArxmlDecoder
    from .fibex_loader import FibexLoader
except ImportError:
    from dbc_loader import DBCLoader, load_dbc_database, _normalize_decoded_signals  # type: ignore
    from arxml_parser import (parse_arxml_files, parse_arxml, ArxmlCatalog,  # type: ignore
                              get_active_catalog, load_catalog_from_directory,
                              list_arxml_files)
    from arxml_decoder import ArxmlDecoder  # type: ignore
    from fibex_loader import FibexLoader  # type: ignore


# ---------------------------------------------------------------------------
#  Constants / helpers  (from mf4_decoded_export.py)
# ---------------------------------------------------------------------------

CAN_ID_MASK = 0x1FFFFFFF


def _coerce_numeric(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        if hasattr(v, 'dtype') and hasattr(v, 'item'):
            return float(v.item())
    except Exception:
        pass
    try:
        vv = getattr(v, 'value', None)
        if vv is not None:
            return float(vv)
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


def _safe_mf4_group_name(s: str) -> str:
    s = str(s or '').strip()
    if not s:
        return 'Signal'
    out = []
    for ch in s:
        if ch.isalnum() or ch in {'_', '-', '.'}:
            out.append(ch)
        else:
            out.append('_')
    name = ''.join(out).strip('._-')
    return name[:120] if name else 'Signal'


def _mask_can_id(raw_id: int) -> int:
    return int(raw_id) & CAN_ID_MASK


class _ProgressBar:
    """Simple terminal progress bar with ETA."""

    def __init__(self, total: int, prefix: str = '', width: int = 0):
        self.total = max(total, 1)
        self.prefix = prefix
        self.t0 = time.monotonic()
        self.last_print = 0.0
        # Auto-detect bar width from terminal
        if width <= 0:
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:
                cols = 80
            # Reserve space for prefix + percentage + ETA + framing
            reserved = len(prefix) + 40
            width = max(10, cols - reserved)
        self.width = width
        self._print(0)

    def update(self, current: int) -> None:
        now = time.monotonic()
        # Throttle redraws to ~10 Hz (avoid slowdown from excessive prints)
        if now - self.last_print < 0.1 and current < self.total:
            return
        self._print(current)
        self.last_print = now

    def _print(self, current: int) -> None:
        frac = current / self.total
        pct = frac * 100.0
        filled = int(self.width * frac)
        bar = '\u2588' * filled + '\u2591' * (self.width - filled)

        elapsed = time.monotonic() - self.t0
        if frac > 0.01 and elapsed > 0.5:
            eta = elapsed / frac * (1.0 - frac)
            if eta >= 60:
                eta_str = f'{int(eta) // 60}m{int(eta) % 60:02d}s'
            else:
                eta_str = f'{eta:.0f}s'
        else:
            eta_str = '--'

        sys.stderr.write(
            f'\r  {self.prefix} |{bar}| {pct:5.1f}%  ETA {eta_str}  ')
        sys.stderr.flush()

    def finish(self, message: str = '') -> None:
        elapsed = time.monotonic() - self.t0
        bar = '\u2588' * self.width
        if elapsed >= 60:
            elapsed_str = f'{int(elapsed) // 60}m{int(elapsed) % 60:02d}s'
        else:
            elapsed_str = f'{elapsed:.1f}s'
        line = f'\r  {self.prefix} |{bar}| 100.0%  done in {elapsed_str}'
        if message:
            line += f'  {message}'
        sys.stderr.write(line + '\n')
        sys.stderr.flush()


# ---------------------------------------------------------------------------
#  Parallel decode worker
# ---------------------------------------------------------------------------

# Module-level state populated before forking workers.
_PAR: Dict[str, Any] = {}


def _parallel_decode_chunk(chunk: Tuple[int, int]):
    """Decode a contiguous chunk of frames (worker process)."""
    chunk_start, chunk_end = chunk
    S = _PAR

    t_list = S['t_list']
    id_list = S['id_list']
    dlc_list = S['dlc_list']
    ch_list = S['ch_list']
    bt_list = S['bt_list']
    fl_list = S['fl_list']
    payload = S['payload']
    pw = S['pw']
    fcv = S['fibex_cycle_valid']
    channel_dbc = S['channel_dbc']
    all_dbc = S['all_dbc']
    arxml = S['arxml']
    fibex = S['fibex']
    units = S['units']
    req = S['req']

    _BT_MAP = {1: 'CAN', 2: 'CAN-FD', 3: 'FLEXRAY', 4: 'LIN', 5: 'ETH'}

    # Local buffers — always keyed by (base_name, msg_series_name)
    # so the parent can resolve collisions after merge.
    buffers: Dict[str, Dict] = {}       # msg_series_name → {t, y, unit}
    base_to_msgs: Dict[str, set] = {}   # base_name → {msg_series_name, …}
    decoded_count = 0
    undecoded_count = 0

    _base_name_cache: Dict[Tuple, str] = {}
    _msg_series_cache: Dict[Tuple, str] = {}

    for idx in range(chunk_start, chunk_end):
        bt = bt_list[idx]
        fid = id_list[idx]

        if bt == 1:
            ft_str = 'CAN'
        elif bt == 3:
            ft_str = 'FLEXRAY'
        elif bt == 2:
            ft_str = 'CAN-FD'
        elif bt == 4:
            ft_str = 'LIN'
        else:
            ft_str = _BT_MAP.get(bt, 'CAN')

        dl = dlc_list[idx]
        ln = min(pw, dl) if dl > 0 else 0
        frame_data = bytes(payload[idx][:ln]) if ln > 0 else b''

        ch_id = ch_list[idx]
        fl = fl_list[idx]

        result = None

        if bt == 1 or bt == 2:
            loaders = channel_dbc.get(ch_id, all_dbc)
            for loader in loaders:
                result = loader.decode(fid, frame_data)
                if result is not None:
                    break
            if result is None and fid > 0x7FF and not (fid & 0x80000000):
                fid2 = fid | 0x80000000
                for loader in loaders:
                    result = loader.decode(fid2, frame_data)
                    if result is not None:
                        break
            if result is None and arxml and arxml.loaded:
                result = arxml.decode(fid, frame_data)
                if result is None and fid > 0x7FF and not (fid & 0x80000000):
                    result = arxml.decode(fid | 0x80000000, frame_data)

        elif bt == 3:
            if fibex:
                cyc = fl & 0x3F
                cv = fcv.get(fid)
                if cv is None or cv[cyc]:
                    result = fibex.decode(fid, frame_data, cycle=cyc)
            if result is None and arxml and arxml.loaded:
                result = arxml.decode_flexray(fid, frame_data)

        elif bt == 4:
            if arxml and arxml.loaded:
                result = arxml.decode_lin(fid, frame_data)

        if result is None:
            undecoded_count += 1
            continue

        msg_name = result.get('name')
        if not msg_name:
            continue
        msg_name = str(msg_name).strip()
        if not msg_name:
            continue

        decoded_signals = result.get('signals')
        if not decoded_signals or not isinstance(decoded_signals, dict):
            continue

        if req is not None:
            needed = req.get(msg_name)
            if not needed:
                continue
        else:
            needed = None

        decoded_count += 1
        t = t_list[idx]

        for sn, val in decoded_signals.items():
            if not sn:
                continue
            sn_str = str(sn)
            if needed is not None and sn_str not in needed:
                continue

            fv = _coerce_numeric(val)
            if fv is None:
                continue

            base_key = (ft_str, ch_id, sn_str)
            base_name = _base_name_cache.get(base_key)
            if base_name is None:
                base_name = _live_mf4_signal_name(ft_str, ch_id, sn_str)
                if not base_name:
                    continue
                _base_name_cache[base_key] = base_name

            msg_key = (ft_str, ch_id, msg_name, sn_str)
            msg_series_name = _msg_series_cache.get(msg_key)
            if msg_series_name is None:
                msg_sanitized = _sanitize_mf4_name_part(msg_name)
                channel_label = _sanitize_mf4_name_part(
                    _live_mf4_channel_label(ft_str, ch_id))
                sn_sanitized = _sanitize_mf4_name_part(sn_str)
                msg_series_name = '.'.join(
                    p for p in [channel_label, msg_sanitized, sn_sanitized] if p)
                if not msg_series_name:
                    msg_series_name = base_name
                _msg_series_cache[msg_key] = msg_series_name

            # Track collision mapping
            if base_name not in base_to_msgs:
                base_to_msgs[base_name] = {msg_series_name}
            else:
                base_to_msgs[base_name].add(msg_series_name)

            # Store under long name; parent will rename non-collided
            buf = buffers.get(msg_series_name)
            if buf is None:
                unit_key = f'{msg_name}.{sn_str}'
                buf = {'t': [], 'y': [], 'unit': units.get(unit_key, '')}
                buffers[msg_series_name] = buf
            buf['t'].append(t)
            buf['y'].append(float(fv))

    return buffers, base_to_msgs, decoded_count, undecoded_count


def _bus_type_to_frame_type(bus_type: Any) -> str:
    try:
        code = int(bus_type)
    except Exception:
        code = 1
    if code == 2:
        return 'CAN-FD'
    if code == 3:
        return 'FLEXRAY'
    if code == 4:
        return 'LIN'
    if code == 5:
        return 'ETH'
    return 'CAN'


_SANITIZE_CACHE: Dict[str, str] = {}

def _sanitize_mf4_name_part(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    cached = _SANITIZE_CACHE.get(text)
    if cached is not None:
        return cached
    result = re.sub(r'[^0-9A-Za-z_]+', '_', text)
    result = re.sub(r'_+', '_', result).strip('_')
    _SANITIZE_CACHE[text] = result
    return result

# Pre-compiled regex for _sanitize_mf4_name_part (used in batch path)
_RE_NON_ALNUM = re.compile(r'[^0-9A-Za-z_]+')
_RE_MULTI_UNDERSCORE = re.compile(r'_+')


_CHANNEL_LABEL_CACHE: Dict[Tuple, str] = {}

def _live_mf4_channel_label(frame_type: Any, channel_id: Any) -> str:
    key = (frame_type, channel_id)
    cached = _CHANNEL_LABEL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        ft = str(frame_type or '').strip().upper()
    except Exception:
        ft = ''
    if ft in {'FLEXRAY', 'FLEX', 'FR'}:
        result = 'FlexRay'
    elif ft == 'LIN':
        result = 'LIN'
    elif ft in {'ETH', 'ETHERNET'}:
        result = 'Ethernet'
    else:
        try:
            ch = channel_id
            if ch is not None and str(ch).strip() != '':
                result = f'CAN{int(ch)}'
            else:
                result = 'CAN'
        except Exception:
            result = 'CAN'
    _CHANNEL_LABEL_CACHE[key] = result
    return result

# Cache for full signal name construction:
# key = (frame_type, channel_id, signal_name) → base_name
# key = (frame_type, channel_id, msg_name, signal_name) → msg_series_name
_SIGNAL_NAME_CACHE: Dict[Tuple, str] = {}
_MSG_SERIES_NAME_CACHE: Dict[Tuple, str] = {}


def _live_mf4_signal_name(frame_type: Any, channel_id: Any,
                           signal_name: Any) -> str:
    key = (frame_type, channel_id, signal_name)
    cached = _SIGNAL_NAME_CACHE.get(key)
    if cached is not None:
        return cached
    sig = _sanitize_mf4_name_part(signal_name)
    if not sig:
        return ''
    channel_name = _sanitize_mf4_name_part(
        _live_mf4_channel_label(frame_type, channel_id))
    result = '.'.join(p for p in [channel_name, sig] if p)
    _SIGNAL_NAME_CACHE[key] = result
    return result


def _live_mf4_signal_name_with_message(
    frame_type: Any,
    channel_id: Any,
    message_name: Any,
    signal_name: Any,
    *,
    frame_id: Optional[int] = None,
) -> str:
    sig = _sanitize_mf4_name_part(signal_name)
    if not sig:
        return ''
    channel_name = _sanitize_mf4_name_part(_live_mf4_channel_label(frame_type, channel_id))
    msg = _sanitize_mf4_name_part(message_name)
    if not msg and frame_id is not None:
        msg = f'ID{(int(frame_id) & CAN_ID_MASK):X}'
    return '.'.join(p for p in [channel_name, msg, sig] if p)


# ---------------------------------------------------------------------------
#  Raw frame table loader
# ---------------------------------------------------------------------------

def load_raw_frame_table(mf4_path: str):
    """Load MF4 raw frame table.

    Returns (t_abs_s, frame_id_u32, dlc_u16, payload_u8[N,M], channel_u16,
    bus_type_u8, flags_u32) or None.
    """
    mdf = asammdf.MDF(mf4_path)
    try:
        def _get_first(names: list):
            for n in names:
                try:
                    return mdf.get(n)
                except Exception:
                    continue
            return None

        can_sig = _get_first([
            'CAN_ID', 'ID', 'Identifier',
            'CAN_DataFrame.CAN_ID', 'CAN_DataFrame.ID',
        ])
        dlc_sig = _get_first([
            'PayloadLength', 'DLC', 'Length', 'DataLength',
            'CAN_DataFrame.DLC',
        ])
        if can_sig is None or dlc_sig is None:
            return None

        bt_sig = _get_first([
            'BusType', 'CAN_DataFrame.BusType', 'CAN_Frame.BusType',
        ])
        ch_sig = _get_first([
            'Channel', 'BusChannel',
            'CAN_DataFrame.Channel', 'CAN_DataFrame.BusChannel',
            'CAN_Frame.Channel',
        ])
        fl_sig = _get_first([
            'Flags', 'CAN_DataFrame.Flags', 'CAN_Frame.Flags',
        ])

        t = np.asarray(getattr(can_sig, 'timestamps', []), dtype=np.float64)
        can_id = np.asarray(getattr(can_sig, 'samples', []), dtype=np.uint32)
        dlc = np.asarray(getattr(dlc_sig, 'samples', []), dtype=np.uint16)

        ch = (
            np.asarray(getattr(ch_sig, 'samples', []), dtype=np.uint16)
            if ch_sig is not None
            else np.zeros_like(can_id, dtype=np.uint16)
        )
        bt = (
            np.asarray(getattr(bt_sig, 'samples', []), dtype=np.uint8)
            if bt_sig is not None
            else np.ones_like(can_id, dtype=np.uint8)
        )
        fl = (
            np.asarray(getattr(fl_sig, 'samples', []), dtype=np.uint32)
            if fl_sig is not None
            else np.zeros_like(can_id, dtype=np.uint32)
        )

        # --- payload bytes ---
        data = None
        data_bytes_sig = _get_first([
            'DataBytes', 'CAN_DataFrame.DataBytes',
        ])
        if data_bytes_sig is not None:
            try:
                raw = np.asarray(getattr(data_bytes_sig, 'samples', []))
                if raw.ndim == 2 and raw.shape[1] >= 1:
                    data = raw.astype(np.uint8, copy=False)
            except Exception:
                pass

        if data is None:
            cols: list = []
            for i in range(64):
                s = _get_first([
                    f'DataByte{i}',
                    f'CAN_DataFrame.DataByte{i}',
                    f'DataBytes[{i}]',
                    f'CAN_DataFrame.DataBytes[{i}]',
                ])
                if s is None:
                    if i < 8:
                        return None
                    break
                cols.append(np.asarray(getattr(s, 'samples', []), dtype=np.uint8))

            if not cols:
                return None

        if t.size == 0 or can_id.size == 0:
            return None

        if data is not None:
            n = int(min(t.size, can_id.size, dlc.size, ch.size, bt.size, fl.size,
                        int(data.shape[0])))
        else:
            n = int(min(t.size, can_id.size, dlc.size, ch.size, bt.size, fl.size,
                        *(c.size for c in cols)))
        if n <= 0:
            return None

        t = t[:n]
        can_id = can_id[:n]
        dlc = dlc[:n]
        ch = ch[:n]
        bt = bt[:n]
        fl = fl[:n]

        if data is not None:
            data = data[:n]
        else:
            data = np.stack([c[:n] for c in cols], axis=1)

        # Sort by timestamp
        order = np.argsort(t)
        t = t[order]
        can_id = can_id[order]
        dlc = dlc[order]
        data = data[order]
        ch = ch[order]
        bt = bt[order]
        fl = fl[order]

        # Keep dominant timestamp cluster if epoch + relative mixed
        thr = 1e7
        mask_epoch = t > thr
        if mask_epoch.any() and (~mask_epoch).any():
            if int(mask_epoch.sum()) >= int((~mask_epoch).sum()):
                sel = mask_epoch
            else:
                sel = ~mask_epoch
            t, can_id, dlc, data, ch, bt, fl = (
                t[sel], can_id[sel], dlc[sel], data[sel],
                ch[sel], bt[sel], fl[sel],
            )

        if t.size == 0:
            return None
        return t, can_id, dlc, data, ch, bt, fl
    finally:
        try:
            mdf.close()
        except Exception:
            pass


def load_raw_can_table(mf4_path: str):
    """Backward-compatible raw CAN loader wrapper."""
    loaded = load_raw_frame_table(mf4_path)
    if loaded is None:
        return None
    t, can_id, dlc, data, ch, _bt, fl = loaded
    return t, can_id, dlc, data, ch, fl


# ---------------------------------------------------------------------------
#  Database discovery
# ---------------------------------------------------------------------------

def _discover_databases(base_dir: str):
    """Find DBC, ARXML, and FIBEX files in databases/ sub-folders."""
    db_root = os.path.join(base_dir, 'databases')
    dbc_paths = sorted(glob.glob(os.path.join(db_root, 'dbc', '*.dbc')))
    arxml_paths = sorted(glob.glob(os.path.join(db_root, 'arxml', '*.arxml')))
    fibex_paths = sorted(
        glob.glob(os.path.join(db_root, 'fibex', '*.xml'))
        + glob.glob(os.path.join(db_root, 'fibex', '*.fibex'))
    )
    # Exclude ARXML files that ended up in the fibex folder
    fibex_paths = [p for p in fibex_paths
                   if not p.lower().endswith('.arxml')]
    return dbc_paths, arxml_paths, fibex_paths


# ---------------------------------------------------------------------------
#  MF4Decoder — reusable class for raw → decoded conversion (app-level)
# ---------------------------------------------------------------------------

class MF4Decoder:
    """Decode a raw-CAN MF4 file using the same pipeline as live CAN.

    When *bus_manager* is provided the decode path is **identical** to the
    live ``BusManager.inject_frame`` chain (DBC → ARXML with bus-hints →
    FIBEX → ARXML FlexRay fallback).  This runs each raw frame through
    ``bus_manager.decode_frame()`` so the result matches 1-to-1 what the
    live system would produce.

    Usage::

        dec = MF4Decoder('/path/to/raw.mf4', ['/path/to/file.dbc'],
                         bus_manager=manager)
        all_signals = dec.list_signals()
        dec.export('/path/to/decoded.mf4')
    """

    def __init__(self, mf4_path: str, dbc_paths: List[str], *,
                 arxml_catalog=None, arxml_decoder=None,
                 bus_manager=None):
        if not mf4_path or not os.path.isfile(mf4_path):
            raise FileNotFoundError(mf4_path)
        if not dbc_paths and bus_manager is None:
            raise ValueError('at least one DBC path is required when bus_manager is not provided')

        self.mf4_path = mf4_path
        self.dbc_paths = list(dbc_paths) if dbc_paths else []
        self._bus_manager = bus_manager

        # Pre-loaded ARXML catalog/decoder — avoids re-parsing 200 MB+ files.
        self._preloaded_catalog = arxml_catalog
        self._preloaded_arxml_decoder = arxml_decoder

        # Lazy-loaded caches
        self._raw: Optional[tuple] = None
        self._loaders: Optional[list] = None
        self._arxml_decoder = None          # ArxmlDecoder instance (if any .arxml paths)
        self._units: Optional[Dict[str, str]] = None
        self._choice_reverse: Dict[str, Dict[str, float]] = {}

    # ---- internal helpers ------------------------------------------------

    def _ensure_raw(self):
        if self._raw is None:
            self._raw = load_raw_frame_table(self.mf4_path)
        if self._raw is None:
            raise ValueError(
                'MF4 does not contain a raw frame table '
                '(CAN_ID / DLC / Channel / BusType / DataByte*)'
            )

    def _ensure_dbs(self):
        """Load DBC / ARXML databases using the same decoders used during recording."""
        if self._loaders is not None:
            return

        self._loaders = []
        self._units = {}

        # Separate ARXML paths from DBC/other paths
        arxml_paths = [p for p in self.dbc_paths if os.path.splitext(p)[1].lower() == '.arxml']
        other_paths = [p for p in self.dbc_paths if os.path.splitext(p)[1].lower() != '.arxml']

        # ── Load ARXML files via ArxmlDecoder ──
        if arxml_paths:
            try:
                # Reuse a pre-loaded decoder/catalog when available to avoid
                # re-parsing enormous ARXML files (200 MB+ → multi-GB RAM).
                if self._preloaded_arxml_decoder is not None and getattr(self._preloaded_arxml_decoder, 'loaded', False):
                    self._arxml_decoder = self._preloaded_arxml_decoder
                    catalog = self._preloaded_catalog or get_active_catalog()
                elif self._preloaded_catalog is not None:
                    catalog = self._preloaded_catalog
                    dec = ArxmlDecoder()
                    count = dec.load_from_catalog(catalog)
                    if count == 0:
                        raise ValueError(f'ARXML loaded but contains no decodable frames: {", ".join(arxml_paths)}')
                    self._arxml_decoder = dec
                else:
                    # Try module-level singleton before expensive re-parse.
                    catalog = get_active_catalog()
                    if catalog is None or not catalog.frames:
                        catalog = parse_arxml_files(arxml_paths)
                    dec = ArxmlDecoder()
                    count = dec.load_from_catalog(catalog)
                    if count == 0:
                        raise ValueError(f'ARXML loaded but contains no decodable frames: {", ".join(arxml_paths)}')
                    self._arxml_decoder = dec

                # Cache units from ARXML catalog
                for frame in catalog.frames.values():
                    if not frame.pdu_name:
                        continue
                    pdu = catalog.pdus.get(frame.pdu_name)
                    if pdu is None:
                        continue
                    fname = str(getattr(frame, 'short_name', '') or '').strip()
                    if not fname:
                        continue
                    for sig in (pdu.signals or []):
                        sn = str(getattr(sig, 'short_name', '') or '').strip()
                        unit = str(getattr(sig, 'unit', '') or '').strip()
                        if not unit and sig.compu_method and catalog.compu_methods:
                            cm = catalog.compu_methods.get(sig.compu_method)
                            if cm:
                                unit = str(getattr(cm, 'unit', '') or '').strip()
                        if sn:
                            self._units[f'{fname}.{sn}'] = unit
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f'failed to load ARXML: {exc}') from exc

        # ── Load DBC / other files via DBCLoader ──
        for p in other_paths:
            loader = DBCLoader()
            if not loader.load(p):
                raise ValueError(f'failed to load DBC: {p}')
            self._loaders.append(loader)

            # Cache units and choice reverse maps for MF4 metadata
            db = loader.db
            if db is None:
                continue
            for m in getattr(db, 'messages', []) or []:
                name = str(getattr(m, 'name', '') or '').strip()
                if not name:
                    continue
                for s in getattr(m, 'signals', []) or []:
                    try:
                        sn = str(getattr(s, 'name', '') or '').strip()
                        unit = str(getattr(s, 'unit', '') or '').strip()
                        if sn:
                            self._units[f'{name}.{sn}'] = unit
                            choices = getattr(s, 'choices', None)
                            if choices and isinstance(choices, dict):
                                txt_key = f'{name}.{sn}_txt'
                                if txt_key not in self._choice_reverse:
                                    rev = {str(v): float(k) for k, v in choices.items()}
                                    if rev:
                                        self._choice_reverse[txt_key] = rev
                    except Exception:
                        continue

    def _decode_frame(self, frame_id: int, data, *, channel: int = 0,
                      flags: int = 0, frame_type: str = 'CAN') -> Optional[Dict]:
        """Decode a single frame using the live pipeline.

        When a *bus_manager* was passed at construction time, delegates to
        ``bus_manager.decode_frame()`` which runs the **exact same** decode
        chain as live CAN (DBC → ARXML with bus-hints → FIBEX → ARXML
        FlexRay fallback).
        """
        # ── Live-identical path via BusManager ──
        if self._bus_manager is not None:
            ids_to_try = [frame_id]
            if frame_id > 0x7FF and not (frame_id & 0x80000000):
                ids_to_try.append(frame_id | 0x80000000)
            for fid in ids_to_try:
                result = self._bus_manager.decode_frame(
                    channel, fid, data, flags=flags, frame_type=frame_type)
                if result is not None:
                    return result
            return None

        # ── Standalone fallback (no bus_manager) ──
        ids_to_try = [frame_id]
        if frame_id > 0x7FF and not (frame_id & 0x80000000):
            ids_to_try.append(frame_id | 0x80000000)

        for fid in ids_to_try:
            if str(frame_type or '').upper() in {'CAN', 'CAN-FD', 'CANFD'}:
                for loader in (self._loaders or []):
                    result = loader.decode(fid, data)
                    if result is not None:
                        return result
                if self._arxml_decoder and self._arxml_decoder.loaded:
                    result = self._arxml_decoder.decode(fid, data)
                    if result is not None:
                        return result
            elif self._arxml_decoder and self._arxml_decoder.loaded and str(frame_type or '').upper() == 'LIN':
                result = self._arxml_decoder.decode_lin(fid, data)
                if result is not None:
                    return result
            elif str(frame_type or '').upper() in {'FLEXRAY', 'FLEX', 'FR'}:
                if self._arxml_decoder and self._arxml_decoder.loaded:
                    result = self._arxml_decoder.decode_flexray(fid, data)
                    if result is not None:
                        return result
        return None

    def _build_req(
        self, signals: Optional[List[str]],
    ) -> Optional[Dict[str, set]]:
        """Parse signal keys into ``{msg_name: {sig_name, …}}``."""
        if signals is None or len(signals) == 0:
            return None
        req: Dict[str, set] = {}
        for k in signals:
            parts = str(k).split('.', 1)
            if len(parts) != 2:
                continue
            msg, sig = parts[0].strip(), parts[1].strip()
            if msg and sig:
                req.setdefault(msg, set()).add(sig)
        return req if req else None

    # ---- public API ------------------------------------------------------

    def list_signals(self, channel: Optional[int] = None) -> List[str]:
        """Return all decodable ``"Message.Signal"`` keys."""
        self._ensure_raw()
        if self.dbc_paths:
            self._ensure_dbs()
        if len(self._raw) >= 7:
            t_abs, can_id, dlc, payload, ch_arr, _bt_arr, fl_arr = self._raw
        else:
            t_abs, can_id, dlc, payload, ch_arr, fl_arr = self._raw

        if channel is not None:
            mask = ch_arr == int(channel)
            can_id = can_id[mask]

        unique_ids = set(int(x) for x in np.unique(can_id))
        unique_ids.update(int(x) & CAN_ID_MASK for x in unique_ids)

        result: List[str] = []
        seen_msgs: set = set()

        all_loaders: list = list(self._loaders or [])
        if self._bus_manager is not None:
            with self._bus_manager.lock:
                for _ch_loaders in self._bus_manager.dbcs.values():
                    for _ldr in (_ch_loaders or []):
                        if _ldr not in all_loaders:
                            all_loaders.append(_ldr)

        for loader in all_loaders:
            db = loader.db
            if db is None:
                continue
            for m in getattr(db, 'messages', []) or []:
                fid = getattr(m, 'frame_id', None)
                if fid is None:
                    continue
                fid = int(fid)
                if fid not in unique_ids and (fid & CAN_ID_MASK) not in unique_ids:
                    continue
                name = str(getattr(m, 'name', '') or '').strip()
                if not name or name in seen_msgs:
                    continue
                seen_msgs.add(name)
                for s in getattr(m, 'signals', []) or []:
                    sn = str(getattr(s, 'name', '') or '').strip()
                    if sn:
                        result.append(f'{name}.{sn}')
                        choices = getattr(s, 'choices', None)
                        if choices and isinstance(choices, dict):
                            result.append(f'{name}.{sn}_txt')

        arxml_decs = []
        if self._arxml_decoder and self._arxml_decoder.loaded:
            arxml_decs.append(self._arxml_decoder)
        if self._bus_manager is not None:
            bm_arxml = getattr(self._bus_manager, 'arxml_decoder', None)
            if bm_arxml and getattr(bm_arxml, 'loaded', False) and bm_arxml not in arxml_decs:
                arxml_decs.append(bm_arxml)

        for adec in arxml_decs:
            for fid_key, entries in adec._can_index.items():
                fid = int(fid_key)
                if fid not in unique_ids and (fid & CAN_ID_MASK) not in unique_ids:
                    continue
                for entry in entries:
                    fname = str(getattr(entry, 'frame_name', '') or '').strip()
                    if not fname or fname in seen_msgs:
                        continue
                    seen_msgs.add(fname)
                    for sig in (entry.signals or []):
                        sn = str(getattr(sig, 'name', '') or '').strip()
                        if sn:
                            result.append(f'{fname}.{sn}')

        if self._bus_manager is not None:
            fibex = getattr(self._bus_manager, 'fibex', None)
            if fibex is not None:
                raw_bt = self._raw[5] if len(self._raw) >= 7 else None
                raw_cid = self._raw[1]
                if raw_bt is not None:
                    fr_mask = raw_bt == 3
                    fr_ids = set(int(x) for x in np.unique(raw_cid[fr_mask])) if fr_mask.any() else set()
                else:
                    fr_ids = set()
                for slot_id, sigs in (fibex.signals or {}).items():
                    if fr_ids and int(slot_id) not in fr_ids:
                        continue
                    ch_label = _live_mf4_channel_label('FLEXRAY', 0)
                    for sig in (sigs or []):
                        sn = str(sig.get('name', '') or '').strip()
                        if sn:
                            safe = _sanitize_mf4_name_part(ch_label)
                            result.append(f'{safe}.{sn}')

        return sorted(set(result))

    def decode(
        self,
        *,
        signals: Optional[List[str]] = None,
        channel: Optional[int] = None,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
    ) -> Dict[str, Dict[str, object]]:
        """Decode raw frames and return signal buffers.

        Returns
        -------
        dict
            ``{"Channel.Signal": {"t": array('d'), "y": array('d'),
            "unit": str}, …}``
        """
        self._ensure_raw()
        if self.dbc_paths:
            self._ensure_dbs()

        # When using bus_manager without explicit dbc_paths, populate the
        # unit cache from bus_manager's loaded DBCs.
        if self._bus_manager is not None and not self._units:
            try:
                with self._bus_manager.lock:
                    all_loaders = dict(self._bus_manager.dbcs)
                for _ch_id, loaders in all_loaders.items():
                    for loader in (loaders or []):
                        db = getattr(loader, 'db', None)
                        if db is None:
                            continue
                        for m in getattr(db, 'messages', []) or []:
                            mname = str(getattr(m, 'name', '') or '').strip()
                            if not mname:
                                continue
                            for s in getattr(m, 'signals', []) or []:
                                sn = str(getattr(s, 'name', '') or '').strip()
                                unit = str(getattr(s, 'unit', '') or '').strip()
                                if sn:
                                    ukey = f'{mname}.{sn}'
                                    if ukey not in self._units:
                                        self._units[ukey] = unit
                                    choices = getattr(s, 'choices', None)
                                    if choices and isinstance(choices, dict):
                                        txt_key = f'{mname}.{sn}_txt'
                                        if txt_key not in self._choice_reverse:
                                            rev = {str(v): float(k) for k, v in choices.items()}
                                            if rev:
                                                self._choice_reverse[txt_key] = rev
            except Exception:
                pass

        if len(self._raw) >= 7:
            t_abs, can_id, dlc, payload, ch_arr, bt_arr, fl_arr = self._raw
        else:
            t_abs, can_id, dlc, payload, ch_arr, fl_arr = self._raw
            bt_arr = np.ones_like(can_id, dtype=np.uint8)

        # --- channel filter ---
        if channel is not None:
            mask = ch_arr == int(channel)
            t_abs = t_abs[mask]
            can_id = can_id[mask]
            dlc = dlc[mask]
            payload = payload[mask]
            ch_arr = ch_arr[mask]
            bt_arr = bt_arr[mask]
            fl_arr = fl_arr[mask]

        if t_abs.size == 0:
            raise ValueError('no data in selected time/channel window')

        # --- time window ---
        base = float(t_abs[0])
        t_start = None if start_s is None else base + float(start_s)
        t_end = None if end_s is None else base + float(end_s)

        i0 = 0
        i1 = int(t_abs.size)
        if t_start is not None:
            i0 = int(np.searchsorted(t_abs, t_start, side='left'))
        if t_end is not None:
            i1 = int(np.searchsorted(t_abs, t_end, side='right'))
        i0 = max(0, min(i0, int(t_abs.size)))
        i1 = max(i0, min(i1, int(t_abs.size)))

        t_abs = t_abs[i0:i1]
        can_id = can_id[i0:i1]
        dlc = dlc[i0:i1]
        payload = payload[i0:i1]
        ch_arr = ch_arr[i0:i1]
        bt_arr = bt_arr[i0:i1]
        fl_arr = fl_arr[i0:i1]

        if t_abs.size == 0:
            raise ValueError('no data in selected time window')

        t0 = float(t_abs[0])
        t_rel = (t_abs - t0).astype(np.float64, copy=False)

        req = self._build_req(signals)

        buffers: Dict[str, Dict[str, object]] = {}
        base_owner_key: Dict[str, str] = {}
        collided_base_keys: set[str] = set()

        for idx in range(int(t_rel.size)):
            fid = int(can_id[idx])
            frame_type = _bus_type_to_frame_type(bt_arr[idx]) if bt_arr.size > idx else 'CAN'

            pw = int(payload.shape[1]) if payload.ndim >= 2 else 0
            try:
                dl = int(dlc[idx])
            except Exception:
                dl = 8
            ln = min(pw, max(0, dl))
            frame_data = list(payload[idx][:ln].tolist())

            ch_id = int(ch_arr[idx]) if ch_arr.size > idx else 0
            fl = int(fl_arr[idx]) if fl_arr.size > idx else 0

            result = self._decode_frame(fid, frame_data, channel=ch_id, flags=fl,
                                        frame_type=frame_type)
            if result is None:
                continue

            msg_name = str(result.get('name') or '').strip()
            if not msg_name:
                continue

            decoded_signals = result.get('signals')
            if not isinstance(decoded_signals, dict) or not decoded_signals:
                continue

            if req is not None:
                needed = req.get(msg_name)
                if not needed:
                    continue
            else:
                needed = None

            t = float(t_rel[idx])

            for sn, val in decoded_signals.items():
                sn_str = str(sn or '').strip()
                if not sn_str:
                    continue
                if needed is not None and sn_str not in needed:
                    continue

                fv = _coerce_numeric(val)
                if fv is None and sn_str.endswith('_txt'):
                    txt_key = f'{msg_name}.{sn_str}'
                    rev = (self._choice_reverse or {}).get(txt_key)
                    if rev:
                        fv = rev.get(str(val))
                if fv is None:
                    continue

                if abs(fv) > 2.81e14:
                    continue

                base_name = _live_mf4_signal_name(frame_type, ch_id, sn_str)
                if not base_name:
                    continue
                msg_name_key = _live_mf4_signal_name_with_message(
                    frame_type, ch_id, msg_name, sn_str, frame_id=fid)
                if not msg_name_key:
                    msg_name_key = base_name

                if base_name in collided_base_keys:
                    output_name = msg_name_key
                else:
                    owner_key = base_owner_key.get(base_name)
                    if owner_key is None:
                        base_owner_key[base_name] = msg_name_key
                        output_name = base_name
                    elif owner_key == msg_name_key:
                        output_name = base_name
                    else:
                        collided_base_keys.add(base_name)
                        prev_buf = buffers.pop(base_name, None)
                        if prev_buf is not None:
                            prev_dst = buffers.get(owner_key)
                            if prev_dst is None:
                                buffers[owner_key] = prev_buf
                            else:
                                prev_dst['t'].extend(prev_buf.get('t', []))
                                prev_dst['y'].extend(prev_buf.get('y', []))
                        output_name = msg_name_key

                unit_key = f'{msg_name}.{sn_str}'
                buf = buffers.get(output_name)
                if buf is None:
                    buf = {
                        't': array('d'),
                        'y': array('d'),
                        'unit': (self._units or {}).get(unit_key, ''),
                    }
                    buffers[output_name] = buf
                buf['t'].append(t)
                buf['y'].append(float(fv))

        return buffers

    def export(
        self,
        out_path: str,
        *,
        signals: Optional[List[str]] = None,
        channel: Optional[int] = None,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
    ) -> None:
        """Export decoded signals to a new MF4 file."""
        if not out_path or not out_path.lower().endswith('.mf4'):
            raise ValueError('out_path must end with .mf4')

        buffers = self.decode(
            signals=signals, channel=channel,
            start_s=start_s, end_s=end_s,
        )
        if not buffers:
            raise ValueError('no numeric decoded samples for selected signals')

        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        out_tmp = (out_path[:-4] + '.tmp.mf4') if out_path.lower().endswith('.mf4') else (out_path + '.tmp.mf4')

        mdf_out = asammdf.MDF(version='4.10')
        try:
            try:
                decoded_max_signals = int(os.getenv('MF4_DECODED_MAX_SIGNALS', '0') or 0)
            except Exception:
                decoded_max_signals = 0
            decoded_max_signals = max(0, decoded_max_signals)

            names = sorted(buffers.keys())
            if decoded_max_signals > 0 and len(names) > decoded_max_signals:
                try:
                    names.sort(key=lambda name: len((buffers.get(name) or {}).get('t', [])), reverse=True)
                    names = names[:decoded_max_signals]
                except Exception:
                    names = sorted(names)[:decoded_max_signals]
            else:
                names = sorted(names)

            group_index = 1
            for key in names:
                buf = buffers.get(key) or {}
                t_vec = np.asarray(buf['t'], dtype=np.float64)
                y_vec = np.asarray(buf['y'], dtype=np.float64)
                if t_vec.size == 0 or y_vec.size == 0:
                    continue
                unit = str(buf.get('unit') or '')
                val_sig = AsamSignal(
                    samples=y_vec, timestamps=t_vec, name=key, unit=unit,
                )
                mdf_out.append(val_sig, acq_name=f'{key}_R{group_index}', comment='')
                group_index += 1

            mdf_out.save(out_tmp, overwrite=True)

            saved_path = out_tmp
            if not os.path.exists(saved_path):
                alt = out_tmp + '.mf4'
                if os.path.exists(alt):
                    saved_path = alt

            os.replace(saved_path, out_path)
        finally:
            try:
                mdf_out.close()
            except Exception:
                pass
            for candidate in (out_tmp, out_tmp + '.mf4'):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
#  Backward-compatible wrapper
# ---------------------------------------------------------------------------

def export_decoded_mf4_from_raw(
    *,
    mf4_path: str,
    out_path: str,
    dbc_paths: List[str],
    signals: List[str],
    channel: Optional[int] = None,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
) -> None:
    """Export decoded numeric signals to MF4.

    ``signals`` items are keys ``"Message.Signal"``.
    Pass an empty list or ``None`` to decode **all** signals.
    """
    decoder = MF4Decoder(mf4_path, dbc_paths)
    decoder.export(
        out_path,
        signals=signals if signals else None,
        channel=channel,
        start_s=start_s,
        end_s=end_s,
    )


# ---------------------------------------------------------------------------
#  Ethernet MF4 helpers (used by app.py export endpoint)
# ---------------------------------------------------------------------------

def _mf4_has_ethernet_metrics(mf4_path: str) -> bool:
    """Return True if the MF4 contains Ethernet numeric metric channels."""
    try:
        mdf = asammdf.MDF(mf4_path)
        for group in mdf.groups:
            for ch in group.channels:
                name = str(getattr(ch, 'name', '') or '').strip()
                if name.startswith('Ethernet.') or name.startswith('XCP:'):
                    mdf.close()
                    return True
        mdf.close()
    except Exception:
        pass
    return False


def export_ethernet_numeric_mf4(
    mf4_path: str,
    out_path: str,
) -> None:
    """Export only Ethernet numeric metric channels from *mf4_path* to *out_path*."""
    src = asammdf.MDF(mf4_path)
    signals = []
    for i, group in enumerate(src.groups):
        for j, ch in enumerate(group.channels):
            name = str(getattr(ch, 'name', '') or '').strip()
            if not (name.startswith('Ethernet.') or name.startswith('XCP:')):
                continue
            try:
                sig = src.get(name, group=i, raw=False)
                if sig is not None and sig.samples.size > 0:
                    signals.append(AsamSignal(
                        samples=sig.samples.astype(np.float64),
                        timestamps=sig.timestamps.astype(np.float64),
                        name=name,
                        unit=str(getattr(sig, 'unit', '') or ''),
                    ))
            except Exception:
                continue
    src.close()

    if not signals:
        raise ValueError('no Ethernet metric channels found')

    out = asammdf.MDF()
    for sig in signals:
        out.append(sig)
    out.save(out_path, overwrite=True)
    out.close()


def merge_ethernet_numeric_channels_into_mf4(
    target_mf4_path: str,
    eth_mf4_path: str,
    *,
    t0_epoch: float | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> None:
    """Merge Ethernet numeric channels from *eth_mf4_path* into *target_mf4_path*."""
    eth = asammdf.MDF(eth_mf4_path)
    signals = []
    for i, group in enumerate(eth.groups):
        for j, ch in enumerate(group.channels):
            name = str(getattr(ch, 'name', '') or '').strip()
            if not (name.startswith('Ethernet.') or name.startswith('XCP:')):
                continue
            try:
                sig = eth.get(name, group=i, raw=False)
                if sig is None or sig.samples.size == 0:
                    continue
                ts = sig.timestamps.astype(np.float64)
                if t0_epoch is not None and ts.size > 0:
                    ts = ts - float(t0_epoch)
                mask = np.ones(ts.size, dtype=bool)
                if start_s is not None:
                    mask &= ts >= float(start_s)
                if end_s is not None:
                    mask &= ts <= float(end_s)
                if not mask.any():
                    continue
                signals.append(AsamSignal(
                    samples=sig.samples[mask].astype(np.float64),
                    timestamps=ts[mask],
                    name=name,
                    unit=str(getattr(sig, 'unit', '') or ''),
                ))
            except Exception:
                continue
    eth.close()

    if not signals:
        return

    target = asammdf.MDF(target_mf4_path)
    for sig in signals:
        target.append(sig)
    target.save(target_mf4_path, overwrite=True)
    target.close()


# ---------------------------------------------------------------------------
#  Standalone MF4 Decoder
# ---------------------------------------------------------------------------

class StandaloneMF4Decoder:
    """Decode a raw MF4 file using DBC / ARXML / FIBEX databases.

    Can be used as a library (for integration into other applications) or
    via the CLI entry-point at the bottom of this module.

    Quick-start (programmatic)::

        decoder = StandaloneMF4Decoder.from_defaults('/path/to/raw.mf4')
        out = decoder.run(output_dir='/results', threads=5, overwrite=True)

    Or with explicit database paths::

        decoder = StandaloneMF4Decoder(
            '/path/to/raw.mf4',
            dbc_paths=['a.dbc'],
            arxml_paths=['b.arxml'],
            fibex_paths=['c.xml'],
        )
        buffers = decoder.decode(workers=5)
        decoder.export('/out.mf4', workers=5)
    """

    def __init__(self, mf4_path: str, *,
                 dbc_paths: List[str] | None = None,
                 arxml_paths: List[str] | None = None,
                 fibex_paths: List[str] | None = None):
        if not mf4_path or not os.path.isfile(mf4_path):
            raise FileNotFoundError(f'MF4 file not found: {mf4_path}')

        self.mf4_path = mf4_path
        self.dbc_paths = list(dbc_paths or [])
        self.arxml_paths = list(arxml_paths or [])
        self.fibex_paths = list(fibex_paths or [])

        # Lazy caches
        self._raw: Optional[tuple] = None
        self._dbc_loaders: List[DBCLoader] = []
        # Per-channel DBC mapping (matches live system's per-source assignment)
        self._channel_dbc_loaders: Dict[int, List[DBCLoader]] = {}
        self._arxml_decoder: Optional[ArxmlDecoder] = None
        self._fibex_loader: Optional[FibexLoader] = None
        self._units: Dict[str, str] = {}
        self._choice_reverse: Dict[str, Dict[str, float]] = {}
        self._dbs_loaded = False

    # ── database loading ─────────────────────────────────────────────────

    def _ensure_raw(self):
        if self._raw is None:
            self._raw = load_raw_frame_table(self.mf4_path)
        if self._raw is None:
            raise ValueError(
                'MF4 does not contain a raw frame table '
                '(CAN_ID / DLC / Channel / BusType / DataByte*)')

    def _ensure_dbs(self):
        if self._dbs_loaded:
            return
        self._dbs_loaded = True

        # Try to load from pickle cache first
        cache_path = self._db_cache_path()
        if cache_path and os.path.isfile(cache_path):
            try:
                t0 = time.perf_counter()
                with open(cache_path, 'rb') as fh:
                    cached = pickle.load(fh)
                self._dbc_loaders = cached['dbc_loaders']
                self._channel_dbc_loaders = cached['channel_dbc']
                self._units = cached['units']
                self._choice_reverse = cached['choice_reverse']
                arxml_dec = cached.get('arxml_decoder')
                if arxml_dec is not None:
                    self._arxml_decoder = arxml_dec
                fibex = cached.get('fibex_loader')
                if fibex is not None:
                    self._fibex_loader = fibex
                elapsed = time.perf_counter() - t0
                print(f'  Loaded databases from cache in {elapsed:.1f}s')
                return
            except Exception as exc:
                print(f'  Cache load failed ({exc}), re-parsing...',
                      file=sys.stderr)

        self._load_dbs_from_source()

        # Save cache
        if cache_path:
            try:
                cached = {
                    'dbc_loaders': self._dbc_loaders,
                    'channel_dbc': self._channel_dbc_loaders,
                    'units': self._units,
                    'choice_reverse': self._choice_reverse,
                    'arxml_decoder': self._arxml_decoder,
                    'fibex_loader': self._fibex_loader,
                }
                cache_dir = os.path.dirname(cache_path)
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)
                tmp = cache_path + '.tmp'
                with open(tmp, 'wb') as fh:
                    pickle.dump(cached, fh, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, cache_path)
                sz = os.path.getsize(cache_path) / 1024 / 1024
                print(f'  Saved database cache ({sz:.1f} MB)')
            except Exception as exc:
                print(f'  Warning: failed to save cache: {exc}',
                      file=sys.stderr)

    def _db_cache_path(self) -> Optional[str]:
        """Return a cache file path keyed by the database file set."""
        all_paths = sorted(self.dbc_paths + self.arxml_paths + self.fibex_paths)
        if not all_paths:
            return None
        h = hashlib.sha256()
        for p in all_paths:
            h.update(p.encode())
            try:
                st = os.stat(p)
                h.update(f'{st.st_size}:{st.st_mtime_ns}'.encode())
            except OSError:
                h.update(b'MISSING')
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cache_dir = os.path.join(script_dir, '.cache')
        return os.path.join(cache_dir, f'db_{h.hexdigest()[:16]}.pkl')

    def _load_dbs_from_source(self):

        # Standard MLBevo bus-name → CAN channel mapping
        _BUS_CHANNEL = {
            'CCAN': 0, 'HCAN': 1, 'DiagCAN': 2, 'DIAGCAN': 2,
            'CAN4': 3, 'ECAN': 4, 'ICAN': 5, 'KCAN': 6,
        }

        # ── DBC files ──
        for p in self.dbc_paths:
            loader = DBCLoader()
            if loader.load(p):
                self._dbc_loaders.append(loader)

                # Infer channel from filename (e.g. *_CCAN_* → ch 0)
                basename = os.path.basename(p).upper()
                for bus_name, ch_id in _BUS_CHANNEL.items():
                    if f'_{bus_name}_' in basename or f'_{bus_name}.' in basename:
                        self._channel_dbc_loaders.setdefault(ch_id, []).append(loader)
                        break

                db = loader.db
                if db is None:
                    continue
                for m in getattr(db, 'messages', []) or []:
                    name = str(getattr(m, 'name', '') or '').strip()
                    if not name:
                        continue
                    for s in getattr(m, 'signals', []) or []:
                        sn = str(getattr(s, 'name', '') or '').strip()
                        unit = str(getattr(s, 'unit', '') or '').strip()
                        if sn:
                            self._units[f'{name}.{sn}'] = unit
                            choices = getattr(s, 'choices', None)
                            if choices and isinstance(choices, dict):
                                txt_key = f'{name}.{sn}_txt'
                                if txt_key not in self._choice_reverse:
                                    rev = {str(v): float(k)
                                           for k, v in choices.items()}
                                    if rev:
                                        self._choice_reverse[txt_key] = rev
            else:
                print(f'Warning: failed to load DBC: {p}', file=sys.stderr)

        # ── ARXML files ──
        if self.arxml_paths:
            try:
                catalog = parse_arxml_files(self.arxml_paths)
                dec = ArxmlDecoder()
                count = dec.load_from_catalog(catalog)
                if count > 0:
                    self._arxml_decoder = dec
                    # Cache units
                    for frame in catalog.frames.values():
                        if not frame.pdu_name:
                            continue
                        pdu = catalog.pdus.get(frame.pdu_name)
                        if pdu is None:
                            continue
                        fname = str(getattr(frame, 'short_name', '') or '').strip()
                        if not fname:
                            continue
                        for sig in (pdu.signals or []):
                            sn = str(getattr(sig, 'short_name', '') or '').strip()
                            unit = str(getattr(sig, 'unit', '') or '').strip()
                            if not unit and sig.compu_method and catalog.compu_methods:
                                cm = catalog.compu_methods.get(sig.compu_method)
                                if cm:
                                    unit = str(getattr(cm, 'unit', '') or '').strip()
                            if sn:
                                self._units[f'{fname}.{sn}'] = unit
                else:
                    print('Warning: ARXML loaded but contains no decodable frames',
                          file=sys.stderr)
            except Exception as exc:
                print(f'Warning: failed to load ARXML: {exc}', file=sys.stderr)

        # ── FIBEX files ──
        for p in self.fibex_paths:
            try:
                fl = FibexLoader()
                if fl.load(p):
                    self._fibex_loader = fl
                    break
            except Exception as exc:
                print(f'Warning: failed to load FIBEX {p}: {exc}',
                      file=sys.stderr)

    # ── single-frame decode ──────────────────────────────────────────────

    def _decode_frame(self, frame_id: int, data, *, channel: int = 0,
                      flags: int = 0,
                      frame_type: str = 'CAN') -> Optional[Dict]:
        """Decode one frame using DBC → ARXML → FIBEX cascade."""
        ids_to_try = [frame_id]
        if frame_id > 0x7FF and not (frame_id & 0x80000000):
            ids_to_try.append(frame_id | 0x80000000)

        ft_upper = str(frame_type or '').upper()

        for fid in ids_to_try:
            # CAN / CAN-FD
            if ft_upper in {'CAN', 'CAN-FD', 'CANFD'}:
                # Use per-channel DBC if available (matches live system);
                # fall back to all DBCs when no channel mapping exists.
                loaders = self._channel_dbc_loaders.get(channel,
                                                        self._dbc_loaders)
                for loader in loaders:
                    result = loader.decode(fid, data)
                    if result is not None:
                        return result
                if self._arxml_decoder and self._arxml_decoder.loaded:
                    result = self._arxml_decoder.decode(fid, data)
                    if result is not None:
                        return result

            # LIN
            elif ft_upper == 'LIN':
                if self._arxml_decoder and self._arxml_decoder.loaded:
                    result = self._arxml_decoder.decode_lin(fid, data)
                    if result is not None:
                        return result

            # FlexRay — strict cycle matching (only decode when cycle
            # actually matches a known FIBEX variant for this slot)
            elif ft_upper in {'FLEXRAY', 'FLEX', 'FR'}:
                if self._fibex_loader:
                    cyc = None
                    try:
                        cyc = int(flags) & 0x3F
                    except Exception:
                        cyc = None
                    # Pre-check: skip frames whose cycle doesn't match any
                    # variant definition. The FIBEX loader falls back to the
                    # merged signal_defs when no variant matches, which
                    # produces spurious decode results.
                    if cyc is not None and hasattr(self._fibex_loader, '_variants'):
                        vlist = self._fibex_loader._variants.get(fid) or []
                        if vlist:
                            cycle_ok = False
                            for v in vlist:
                                bc = int(v.get('base_cycle') or 0)
                                cr = int(v.get('cycle_repetition') or 0)
                                if cr <= 0:
                                    cycle_ok = True
                                    break
                                if cyc >= bc and (cyc - bc) % cr == 0:
                                    cycle_ok = True
                                    break
                            if not cycle_ok:
                                return None  # skip — no matching variant
                    result = self._fibex_loader.decode(fid, data, cycle=cyc)
                    if result is not None:
                        return result
                if self._arxml_decoder and self._arxml_decoder.loaded:
                    result = self._arxml_decoder.decode_flexray(fid, data)
                    if result is not None:
                        return result

        return None

    # ── full decode ──────────────────────────────────────────────────────

    def decode(self, *, signals: Optional[List[str]] = None,
               channel: Optional[int] = None,
               start_s: Optional[float] = None,
               end_s: Optional[float] = None,
               workers: int = 1) -> Dict[str, Dict[str, object]]:
        """Decode raw frames and return signal buffers.

        Returns {"Channel.Signal": {"t": array('d'), "y": array('d'),
                 "unit": str}, ...}
        """
        self._ensure_raw()
        self._ensure_dbs()

        t_abs, can_id, dlc, payload, ch_arr, bt_arr, fl_arr = self._raw

        # --- channel filter ---
        if channel is not None:
            mask = ch_arr == int(channel)
            t_abs = t_abs[mask]
            can_id = can_id[mask]
            dlc = dlc[mask]
            payload = payload[mask]
            ch_arr = ch_arr[mask]
            bt_arr = bt_arr[mask]
            fl_arr = fl_arr[mask]

        if t_abs.size == 0:
            raise ValueError('no data in selected time/channel window')

        # --- time window ---
        base = float(t_abs[0])
        t_start = None if start_s is None else base + float(start_s)
        t_end = None if end_s is None else base + float(end_s)

        i0 = 0
        i1 = int(t_abs.size)
        if t_start is not None:
            i0 = int(np.searchsorted(t_abs, t_start, side='left'))
        if t_end is not None:
            i1 = int(np.searchsorted(t_abs, t_end, side='right'))
        i0 = max(0, min(i0, int(t_abs.size)))
        i1 = max(i0, min(i1, int(t_abs.size)))

        t_abs = t_abs[i0:i1]
        can_id = can_id[i0:i1]
        dlc = dlc[i0:i1]
        payload = payload[i0:i1]
        ch_arr = ch_arr[i0:i1]
        bt_arr = bt_arr[i0:i1]
        fl_arr = fl_arr[i0:i1]

        if t_abs.size == 0:
            raise ValueError('no data in selected time window')

        t0 = float(t_abs[0])
        t_rel = (t_abs - t0).astype(np.float64, copy=False)

        # --- build request map ---
        req = self._build_req(signals)

        # --- pre-convert numpy arrays to Python lists (avoid numpy scalar
        #     overhead on every per-element access) ---
        n_frames = int(t_rel.size)
        t_list = t_rel.tolist()
        id_list = can_id.astype(np.int64).tolist()
        dlc_list = dlc.tolist()
        ch_list = ch_arr.tolist()
        bt_list = bt_arr.tolist()
        fl_list = fl_arr.tolist()
        pw = int(payload.shape[1]) if payload.ndim >= 2 else 0

        # --- pre-build FIBEX cycle validity lookup ---
        # For each known slot, build a 64-element bool array: valid[cycle] = True/False
        _fibex_cycle_valid: Dict[int, list] = {}
        if self._fibex_loader and hasattr(self._fibex_loader, '_variants'):
            for fid, vlist in self._fibex_loader._variants.items():
                valid = [False] * 64
                for v in vlist:
                    bc = int(v.get('base_cycle') or 0)
                    cr = int(v.get('cycle_repetition') or 0)
                    if cr <= 0:
                        valid = [True] * 64
                        break
                    for c in range(64):
                        if c >= bc and (c - bc) % cr == 0:
                            valid[c] = True
                _fibex_cycle_valid[fid] = valid

        # ==============================================================
        #  Parallel decode path (multiprocessing with fork)
        # ==============================================================
        if workers > 1:
            return self._decode_parallel(
                workers, n_frames, t_list, id_list, dlc_list,
                ch_list, bt_list, fl_list, payload, pw,
                _fibex_cycle_valid, req)

        # ==============================================================
        #  Single-threaded decode path
        # ==============================================================

        # --- pre-build bus-type → frame-type string map ---
        _BT_MAP = {1: 'CAN', 2: 'CAN-FD', 3: 'FLEXRAY', 4: 'LIN', 5: 'ETH'}

        # --- local references for hot-path functions (avoid global lookups) ---
        _decode_frame = self._decode_frame
        _fibex = self._fibex_loader
        _arxml = self._arxml_decoder
        _channel_dbc = self._channel_dbc_loaders
        _all_dbc = self._dbc_loaders
        _units = self._units
        _coerce = _coerce_numeric

        # --- decode loop ---
        # Collision-based naming (matches live MF4 logger):
        #   base_name  = "FlexRay.SignalName"  (short)
        #   msg_name_full = "FlexRay.FrameName.SignalName"  (long)
        # Use short name by default; switch to long name on collision.
        buffers: Dict[str, Dict[str, object]] = {}
        decoded_base_owner: Dict[str, str] = {}   # base_name → msg_series_name
        decoded_collided_bases: set = set()        # base_names that collided
        decoded_count = 0
        undecoded_count = 0

        # Caches for the inner signal-name loop (avoid repeated sanitization)
        _base_name_cache: Dict[Tuple, str] = {}    # (ft_str, ch, sn) → base_name
        _msg_series_cache: Dict[Tuple, str] = {}   # (ft_str, ch, msg, sn) → msg_series_name

        progress = _ProgressBar(n_frames, prefix='Decoding')

        for idx in range(n_frames):
            if idx & 0x3FF == 0:          # every 1024 frames
                progress.update(idx)
            bt = bt_list[idx]
            fid = id_list[idx]

            # Fast frame-type determination (integer comparison)
            if bt == 1:
                ft_str = 'CAN'
            elif bt == 3:
                ft_str = 'FLEXRAY'
            elif bt == 2:
                ft_str = 'CAN-FD'
            elif bt == 4:
                ft_str = 'LIN'
            else:
                ft_str = _BT_MAP.get(bt, 'CAN')

            dl = dlc_list[idx]
            ln = min(pw, dl) if dl > 0 else 0
            frame_data = bytes(payload[idx][:ln]) if ln > 0 else b''

            ch_id = ch_list[idx]
            fl = fl_list[idx]

            # --- inline fast-path decode (avoids function call overhead
            #     for the two dominant bus types) ---
            result = None

            if bt == 1 or bt == 2:
                # CAN / CAN-FD — DBC then ARXML
                loaders = _channel_dbc.get(ch_id, _all_dbc)
                for loader in loaders:
                    result = loader.decode(fid, frame_data)
                    if result is not None:
                        break
                if result is None and fid > 0x7FF and not (fid & 0x80000000):
                    fid2 = fid | 0x80000000
                    for loader in loaders:
                        result = loader.decode(fid2, frame_data)
                        if result is not None:
                            break
                if result is None and _arxml and _arxml.loaded:
                    result = _arxml.decode(fid, frame_data)
                    if result is None and fid > 0x7FF and not (fid & 0x80000000):
                        result = _arxml.decode(fid | 0x80000000, frame_data)

            elif bt == 3:
                # FlexRay — FIBEX with strict cycle match
                if _fibex:
                    cyc = fl & 0x3F
                    # Fast cycle validity check via pre-built lookup
                    cv = _fibex_cycle_valid.get(fid)
                    if cv is None or cv[cyc]:
                        result = _fibex.decode(fid, frame_data, cycle=cyc)
                if result is None and _arxml and _arxml.loaded:
                    result = _arxml.decode_flexray(fid, frame_data)

            elif bt == 4:
                # LIN
                if _arxml and _arxml.loaded:
                    result = _arxml.decode_lin(fid, frame_data)

            else:
                result = _decode_frame(fid, frame_data, channel=ch_id,
                                       flags=fl, frame_type=ft_str)

            if result is None:
                undecoded_count += 1
                continue

            msg_name = result.get('name')
            if not msg_name:
                continue
            msg_name = str(msg_name).strip()
            if not msg_name:
                continue

            decoded_signals = result.get('signals')
            if not decoded_signals or not isinstance(decoded_signals, dict):
                continue

            if req is not None:
                needed = req.get(msg_name)
                if not needed:
                    continue
            else:
                needed = None

            decoded_count += 1
            t = t_list[idx]

            for sn, val in decoded_signals.items():
                if not sn:
                    continue
                sn_str = str(sn)
                if needed is not None and sn_str not in needed:
                    continue

                fv = _coerce(val)
                if fv is None:
                    continue

                # Build short (base) and long (with message) names — cached
                base_key = (ft_str, ch_id, sn_str)
                base_name = _base_name_cache.get(base_key)
                if base_name is None:
                    base_name = _live_mf4_signal_name(ft_str, ch_id, sn_str)
                    if not base_name:
                        continue
                    _base_name_cache[base_key] = base_name

                msg_key = (ft_str, ch_id, msg_name, sn_str)
                msg_series_name = _msg_series_cache.get(msg_key)
                if msg_series_name is None:
                    msg_sanitized = _sanitize_mf4_name_part(msg_name)
                    channel_label = _sanitize_mf4_name_part(
                        _live_mf4_channel_label(ft_str, ch_id))
                    sn_sanitized = _sanitize_mf4_name_part(sn_str)
                    msg_series_name = '.'.join(
                        p for p in [channel_label, msg_sanitized, sn_sanitized] if p)
                    if not msg_series_name:
                        msg_series_name = base_name
                    _msg_series_cache[msg_key] = msg_series_name

                # Collision resolution (same algorithm as live logger)
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
                        # Collision detected — switch to long names
                        decoded_collided_bases.add(base_name)
                        prev = buffers.pop(base_name, None)
                        if prev is not None:
                            prev_dst = buffers.get(owner_name)
                            if prev_dst is None:
                                buffers[owner_name] = prev
                            else:
                                prev_dst['t'].extend(prev.get('t', []))
                                prev_dst['y'].extend(prev.get('y', []))
                        series_name = msg_series_name

                buf = buffers.get(series_name)
                if buf is None:
                    unit_key = f'{msg_name}.{sn_str}'
                    buf = {
                        't': array('d'),
                        'y': array('d'),
                        'unit': _units.get(unit_key, ''),
                    }
                    buffers[series_name] = buf
                buf['t'].append(t)
                buf['y'].append(float(fv))

        progress.finish(
            f'{decoded_count} decoded, '
            f'{undecoded_count} unmatched, '
            f'{len(buffers)} signals')
        return buffers

    # ── parallel decode ──────────────────────────────────────────────────

    def _decode_parallel(self, workers, n_frames, t_list, id_list, dlc_list,
                         ch_list, bt_list, fl_list, payload, pw,
                         fibex_cycle_valid, req):
        """Decode using multiple worker processes (fork)."""
        global _PAR

        try:
            ctx = multiprocessing.get_context('fork')
        except ValueError:
            print('  Warning: fork not available, falling back to 1 worker',
                  file=sys.stderr)
            # Fall through to single-threaded path would be complex here,
            # so just use 1 worker via pool for simplicity.
            ctx = multiprocessing.get_context()

        # Populate module-global state that workers inherit via fork/COW.
        _PAR = {
            't_list': t_list,
            'id_list': id_list,
            'dlc_list': dlc_list,
            'ch_list': ch_list,
            'bt_list': bt_list,
            'fl_list': fl_list,
            'payload': payload,
            'pw': pw,
            'fibex_cycle_valid': fibex_cycle_valid,
            'channel_dbc': self._channel_dbc_loaders,
            'all_dbc': self._dbc_loaders,
            'arxml': self._arxml_decoder,
            'fibex': self._fibex_loader,
            'units': self._units,
            'req': req,
        }

        # Split into fine-grained chunks for progress reporting.
        n_chunks = max(workers * 4, 20)
        chunk_size = max(1, (n_frames + n_chunks - 1) // n_chunks)
        chunks = []
        for i in range(0, n_frames, chunk_size):
            end = min(i + chunk_size, n_frames)
            if i < end:
                chunks.append((i, end))

        progress = _ProgressBar(len(chunks), prefix=f'Decoding ({workers}w)')

        results = []
        with ctx.Pool(workers) as pool:
            for ci, result in enumerate(pool.imap(
                    _parallel_decode_chunk, chunks)):
                results.append(result)
                progress.update(ci + 1)

        # ── Merge worker results ──
        # 1. Union collision info (base_name → set of msg_series_names)
        all_base_to_msgs: Dict[str, set] = {}
        total_decoded = 0
        total_undecoded = 0

        for w_buffers, w_b2m, w_dec, w_undec in results:
            total_decoded += w_dec
            total_undecoded += w_undec
            for base, msgs in w_b2m.items():
                if base not in all_base_to_msgs:
                    all_base_to_msgs[base] = set(msgs)
                else:
                    all_base_to_msgs[base].update(msgs)

        # 2. Determine which base_names have collisions
        collided_bases = {b for b, ms in all_base_to_msgs.items()
                         if len(ms) > 1}

        # 3. Build reverse rename map for non-collided signals
        #    msg_series_name → base_name  (only when no collision)
        msg_to_short: Dict[str, str] = {}
        for base, msgs in all_base_to_msgs.items():
            if base not in collided_bases and len(msgs) == 1:
                long = next(iter(msgs))
                if long != base:
                    msg_to_short[long] = base

        # 4. Merge buffers from all workers (in chunk order → timestamps
        #    are already in order within each signal).
        buffers: Dict[str, Dict[str, object]] = {}
        for w_buffers, _, _, _ in results:
            for long_name, wbuf in w_buffers.items():
                # Rename to short name if no collision
                final_name = msg_to_short.get(long_name, long_name)
                existing = buffers.get(final_name)
                if existing is None:
                    buffers[final_name] = {
                        't': array('d', wbuf['t']),
                        'y': array('d', wbuf['y']),
                        'unit': wbuf['unit'],
                    }
                else:
                    existing['t'].extend(wbuf['t'])
                    existing['y'].extend(wbuf['y'])

        # Clean up global state
        _PAR = {}

        progress.finish(
            f'{total_decoded} decoded, '
            f'{total_undecoded} unmatched, '
            f'{len(buffers)} signals')
        return buffers

    # ── export to MF4 ────────────────────────────────────────────────────

    def export(self, out_path: str, *, signals: Optional[List[str]] = None,
               channel: Optional[int] = None,
               start_s: Optional[float] = None,
               end_s: Optional[float] = None,
               workers: int = 1) -> None:
        """Export decoded signals to a new MF4 file."""
        if not out_path or not out_path.lower().endswith('.mf4'):
            raise ValueError('out_path must end with .mf4')

        buffers = self.decode(signals=signals, channel=channel,
                              start_s=start_s, end_s=end_s,
                              workers=workers)
        if not buffers:
            raise ValueError('no numeric decoded samples for selected signals')

        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        out_tmp = out_path[:-4] + '.tmp.mf4'
        mdf_out = asammdf.MDF(version='4.10')
        try:
            names = sorted(buffers.keys())
            n_sigs = len(names)
            progress = _ProgressBar(n_sigs, prefix='Writing ')
            group_index = 1
            for si, key in enumerate(names):
                if si & 0xFF == 0:
                    progress.update(si)
                buf = buffers.get(key) or {}
                t_vec = np.asarray(buf['t'], dtype=np.float64)
                y_vec = np.asarray(buf['y'], dtype=np.float64)
                if t_vec.size == 0 or y_vec.size == 0:
                    continue
                unit = str(buf.get('unit') or '')
                val_sig = AsamSignal(
                    samples=y_vec, timestamps=t_vec, name=key, unit=unit)
                mdf_out.append(val_sig,
                               acq_name=f'{key}_R{group_index}', comment='')
                group_index += 1
            progress.finish(f'{group_index - 1} channels')

            mdf_out.save(out_tmp, overwrite=True)

            saved_path = out_tmp
            if not os.path.exists(saved_path):
                alt = out_tmp + '.mf4'
                if os.path.exists(alt):
                    saved_path = alt

            os.replace(saved_path, out_path)
            file_size = os.path.getsize(out_path)
            if file_size >= 1024 * 1024:
                size_str = f'{file_size / 1024 / 1024:.1f} MB'
            else:
                size_str = f'{file_size / 1024:.1f} KB'
            print(f'  Written: {out_path}  '
                  f'({group_index - 1} signal channels, {size_str})')
        finally:
            try:
                mdf_out.close()
            except Exception:
                pass
            for candidate in (out_tmp, out_tmp + '.mf4'):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except Exception:
                    pass

    # ── list signals ─────────────────────────────────────────────────────

    def list_signals(self, channel: Optional[int] = None) -> List[str]:
        """Return all decodable 'Message.Signal' keys."""
        self._ensure_raw()
        self._ensure_dbs()

        t_abs, can_id, dlc, payload, ch_arr, bt_arr, fl_arr = self._raw

        if channel is not None:
            mask = ch_arr == int(channel)
            can_id = can_id[mask]
            bt_arr = bt_arr[mask]

        unique_ids = set(int(x) for x in np.unique(can_id))
        unique_ids.update(int(x) & CAN_ID_MASK for x in unique_ids)

        result: List[str] = []
        seen_msgs: set = set()

        for loader in self._dbc_loaders:
            db = loader.db
            if db is None:
                continue
            for m in getattr(db, 'messages', []) or []:
                fid = getattr(m, 'frame_id', None)
                if fid is None:
                    continue
                fid = int(fid)
                if (fid not in unique_ids
                        and (fid & CAN_ID_MASK) not in unique_ids):
                    continue
                name = str(getattr(m, 'name', '') or '').strip()
                if not name or name in seen_msgs:
                    continue
                seen_msgs.add(name)
                for s in getattr(m, 'signals', []) or []:
                    sn = str(getattr(s, 'name', '') or '').strip()
                    if sn:
                        result.append(f'{name}.{sn}')
                        choices = getattr(s, 'choices', None)
                        if choices and isinstance(choices, dict):
                            result.append(f'{name}.{sn}_txt')

        if self._arxml_decoder and self._arxml_decoder.loaded:
            for fid_key, entries in self._arxml_decoder._can_index.items():
                fid = int(fid_key)
                if (fid not in unique_ids
                        and (fid & CAN_ID_MASK) not in unique_ids):
                    continue
                for entry in entries:
                    fname = str(getattr(entry, 'frame_name', '') or '').strip()
                    if not fname or fname in seen_msgs:
                        continue
                    seen_msgs.add(fname)
                    for sig in (entry.signals or []):
                        sn = str(getattr(sig, 'name', '') or '').strip()
                        if sn:
                            result.append(f'{fname}.{sn}')

        if self._fibex_loader:
            fr_mask = bt_arr == 3
            if fr_mask.any():
                fr_ids = set(int(x) for x in np.unique(can_id[fr_mask] if channel is None else can_id))
            else:
                fr_ids = set()
            for slot_id, sigs in (self._fibex_loader.signals or {}).items():
                if fr_ids and int(slot_id) not in fr_ids:
                    continue
                for sig in (sigs or []):
                    sn = str(sig.get('name', '') or '').strip()
                    if sn:
                        result.append(f'FlexRay.{sn}')

        return sorted(set(result))

    # ── class-level convenience API ───────────────────────────────────

    @classmethod
    def from_defaults(cls, mf4_path: str, *,
                      dbc_paths: List[str] | None = None,
                      arxml_paths: List[str] | None = None,
                      fibex_paths: List[str] | None = None,
                      db_base_dir: str | None = None) -> 'StandaloneMF4Decoder':
        """Create a decoder with auto-discovered databases.

        Any database path list that is ``None`` is filled via
        auto-discovery from ``<db_base_dir>/databases/``.
        Pass an explicit empty list (``[]``) to skip a database type.

        Parameters
        ----------
        mf4_path : str
            Path to the raw MF4 recording.
        dbc_paths, arxml_paths, fibex_paths : list[str] | None
            Explicit database paths.  ``None`` → auto-discover.
        db_base_dir : str | None
            Root directory for database auto-discovery.  Defaults to the
            directory containing this script.
        """
        if db_base_dir is None:
            db_base_dir = os.path.dirname(os.path.abspath(__file__))

        auto_dbc, auto_arxml, auto_fibex = _discover_databases(db_base_dir)
        return cls(
            mf4_path,
            dbc_paths=dbc_paths if dbc_paths is not None else auto_dbc,
            arxml_paths=arxml_paths if arxml_paths is not None else auto_arxml,
            fibex_paths=fibex_paths if fibex_paths is not None else auto_fibex,
        )

    def run(self, *,
            output: str | None = None,
            output_dir: str | None = None,
            signals: List[str] | None = None,
            channel: int | None = None,
            start_s: float | None = None,
            end_s: float | None = None,
            threads: int = 5,
            no_cache: bool = False,
            overwrite: bool = False) -> str:
        """Full decode-and-export pipeline.  Returns the output file path.

        Parameters
        ----------
        output : str | None
            Explicit output file path.  Mutually exclusive with *output_dir*.
        output_dir : str | None
            Directory into which ``<input>_decoded.mf4`` is written.
        signals : list[str] | None
            ``Message.Signal`` keys to decode (default: all).
        channel : int | None
            Decode only this CAN channel number.
        start_s, end_s : float | None
            Time window in seconds (relative to first sample).
        threads : int
            Worker processes for parallel decode (1 = single-threaded).
        no_cache : bool
            Delete the database pickle cache before loading.
        overwrite : bool
            If *False* and the output file already exists, raise
            ``FileExistsError``.

        Returns
        -------
        str
            Absolute path to the written decoded MF4 file.
        """
        if no_cache:
            self.clear_cache()

        # Resolve output path
        if output:
            out_path = os.path.abspath(output)
        else:
            base, ext = os.path.splitext(self.mf4_path)
            decoded_name = f'{os.path.basename(base)}_decoded{ext}'
            if output_dir:
                od = os.path.abspath(output_dir)
                os.makedirs(od, exist_ok=True)
                out_path = os.path.join(od, decoded_name)
            else:
                out_path = os.path.join(
                    os.path.dirname(self.mf4_path), decoded_name)

        if os.path.isfile(out_path) and not overwrite:
            raise FileExistsError(
                f'Output file already exists: {out_path}')

        print(f'\nDecoding: {os.path.basename(self.mf4_path)}')
        t0 = time.time()

        self.export(
            out_path,
            signals=signals,
            channel=channel,
            start_s=start_s,
            end_s=end_s,
            workers=max(1, threads),
        )

        elapsed = time.time() - t0
        print(f'  Completed in {elapsed:.1f}s')
        return out_path

    def clear_cache(self) -> None:
        """Delete the database pickle cache (if any)."""
        cache_path = self._db_cache_path()
        if cache_path and os.path.isfile(cache_path):
            os.remove(cache_path)
            print('  Cache cleared')

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_req(signals: Optional[List[str]]) -> Optional[Dict[str, set]]:
        if signals is None or len(signals) == 0:
            return None
        req: Dict[str, set] = {}
        for k in signals:
            parts = str(k).split('.', 1)
            if len(parts) != 2:
                continue
            msg, sig = parts[0].strip(), parts[1].strip()
            if msg and sig:
                req.setdefault(msg, set()).add(sig)
        return req if req else None


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Decode a raw MF4 file into a signal-level MF4.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('input', help='Path to the raw MF4 file')
    parser.add_argument('-o', '--output', default=None,
                        help='Output decoded MF4 path '
                             '(default: <input>_decoded.mf4)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory to write the decoded file into '
                             '(filename is <input>_decoded.mf4)')
    parser.add_argument('--dbc', nargs='*', default=None,
                        help='DBC file paths (overrides auto-discovery)')
    parser.add_argument('--arxml', nargs='*', default=None,
                        help='ARXML file paths (overrides auto-discovery)')
    parser.add_argument('--fibex', nargs='*', default=None,
                        help='FIBEX XML file paths (overrides auto-discovery)')
    parser.add_argument('--channel', type=int, default=None,
                        help='Decode only this CAN channel number')
    parser.add_argument('--start', type=float, default=None,
                        help='Start time in seconds (relative to first sample)')
    parser.add_argument('--end', type=float, default=None,
                        help='End time in seconds (relative to first sample)')
    parser.add_argument('--signals', default=None,
                        help='Comma-separated list of Message.Signal keys '
                             'to decode (default: all)')
    parser.add_argument('--list-signals', action='store_true',
                        help='List decodable signals and exit')
    parser.add_argument('--no-cache', action='store_true',
                        help='Force re-parse of databases, ignoring cache')
    parser.add_argument('--threads', type=int, default=5,
                        help='Number of parallel worker processes '
                             '(default: 5, use 1 to disable)')
    args = parser.parse_args()

    mf4_path = os.path.abspath(args.input)
    if not os.path.isfile(mf4_path):
        print(f'Error: file not found: {mf4_path}', file=sys.stderr)
        sys.exit(1)

    # Build decoder with auto-discovery
    dbc = [os.path.abspath(p) for p in args.dbc] if args.dbc is not None else None
    arxml = [os.path.abspath(p) for p in args.arxml] if args.arxml is not None else None
    fibex = [os.path.abspath(p) for p in args.fibex] if args.fibex is not None else None

    decoder = StandaloneMF4Decoder.from_defaults(
        mf4_path,
        dbc_paths=dbc,
        arxml_paths=arxml,
        fibex_paths=fibex,
    )

    print(f'Databases: {len(decoder.dbc_paths)} DBC, '
          f'{len(decoder.arxml_paths)} ARXML, '
          f'{len(decoder.fibex_paths)} FIBEX')
    for p in decoder.dbc_paths + decoder.arxml_paths + decoder.fibex_paths:
        print(f'  {os.path.basename(p)}')

    # List signals mode
    if args.list_signals:
        if args.no_cache:
            decoder.clear_cache()
        print(f'\nLoading raw frames from: {os.path.basename(mf4_path)}')
        sigs = decoder.list_signals(channel=args.channel)
        print(f'\n{len(sigs)} decodable signals:')
        for s in sigs:
            print(f'  {s}')
        return

    # Resolve output path for overwrite check
    if args.output:
        out_path = os.path.abspath(args.output)
    else:
        base, ext = os.path.splitext(mf4_path)
        decoded_name = f'{os.path.basename(base)}_decoded{ext}'
        if args.output_dir:
            out_path = os.path.join(os.path.abspath(args.output_dir),
                                    decoded_name)
        else:
            out_path = os.path.join(os.path.dirname(mf4_path), decoded_name)

    overwrite = False
    if os.path.isfile(out_path):
        answer = input(
            f'Output file already exists:\n  {out_path}\n'
            f'Overwrite? [y/N] ').strip().lower()
        if answer not in ('y', 'yes'):
            print('Aborted.')
            return
        overwrite = True

    signal_list = None
    if args.signals:
        signal_list = [s.strip() for s in args.signals.split(',') if s.strip()]

    decoder.run(
        output=args.output,
        output_dir=args.output_dir,
        signals=signal_list,
        channel=args.channel,
        start_s=args.start,
        end_s=args.end,
        threads=args.threads,
        no_cache=args.no_cache,
        overwrite=overwrite,
    )


if __name__ == '__main__':
    main()
