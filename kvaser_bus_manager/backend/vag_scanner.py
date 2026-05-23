try:
    import can
except Exception:
    can = None

_BusABC = can.BusABC if can is not None else object
import time
import threading
import queue
import sys
import os
import socket
import struct
import fcntl
import zipfile
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Avoid overlapping ScanTools runs (especially discovery) which can cause
# intermittent "Nodes found: 0" when multiple threads fight for the same bus.
_SCANTOOLS_DISCOVERY_LOCK = threading.Lock()
_SCANTOOLS_DISCOVERY_IN_PROGRESS = False


# Cache PDX indexes in-process to avoid repeated JSON reads during a scan.
_PDX_DTC_MAP_CACHE: Optional[Dict[str, str]] = None
_PDX_DTC_MAP_CACHE_SRC: Optional[str] = None
_PDX_DID_INDEX_CACHE: Optional[Dict[int, Dict[str, Any]]] = None
_PDX_DID_INDEX_CACHE_SRC: Optional[str] = None
_PDX_COMM_INDEX_CACHE: Optional[Dict[str, Any]] = None
_PDX_COMM_INDEX_CACHE_SRC: Optional[str] = None
_PDX_CLEAR_PROFILE_CACHE: Optional[Dict[int, Dict[str, Any]]] = None
_PDX_CLEAR_PROFILE_CACHE_SRC: Optional[str] = None
_PDX_DTC_ECU_MAP_CACHE: Optional[Dict[str, Dict[str, str]]] = None
_PDX_DTC_ECU_MAP_CACHE_SRC: Optional[str] = None
_PDX_ENV_DATA_CACHE: Optional[Dict[str, Any]] = None
_PDX_ENV_DATA_CACHE_SRC: Optional[str] = None
# Raw TROUBLE-CODE (UDS 3-byte int) -> DISPLAY-TROUBLE-CODE map. VW/VAG OEM
# display codes do NOT match the SAE-J2012 conversion of the raw bytes, so we
# must look them up in the PDX index. Keys are decimal strings of the int.
_PDX_TROUBLE_TO_DISPLAY_CACHE: Optional[Dict[str, str]] = None
_PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def _uds_dtc_to_display_code(dtc_val: int) -> str:
    """Convert a 3-byte UDS DTC value into a standard P/C/B/Uxxxxx code."""
    v = int(dtc_val) & 0xFFFFFF
    b1 = (v >> 16) & 0xFF
    b2 = (v >> 8) & 0xFF
    b3 = v & 0xFF

    letter_bits = (b1 >> 6) & 0x03
    letter = {0: 'P', 1: 'C', 2: 'B', 3: 'U'}.get(letter_bits, 'P')
    d1 = (b1 >> 4) & 0x03
    d2 = b1 & 0x0F
    return f"{letter}{d1:X}{d2:X}{b2:02X}{b3:02X}"


def _obd_dtc_to_display_code(b1: int, b2: int) -> str:
    """Convert 2-byte OBD DTC encoding into a standard P/C/B/Uxxxx code."""
    b1 = int(b1) & 0xFF
    b2 = int(b2) & 0xFF
    letter_bits = (b1 >> 6) & 0x03
    letter = {0: 'P', 1: 'C', 2: 'B', 3: 'U'}.get(letter_bits, 'P')
    d1 = (b1 >> 4) & 0x03
    d2 = b1 & 0x0F
    return f"{letter}{d1:X}{d2:X}{b2:02X}"


def _normalize_dtc_code(code: Optional[str]) -> str:
    """Normalize a DTC code for map lookup.

    PDX export quirks vary (case/whitespace). Keep it conservative.
    """
    if not isinstance(code, str):
        return ''
    return code.strip().upper()


def _dtc_lookup_candidates(code: Optional[str]) -> List[str]:
    """Generate a small, ordered set of alternative DTC keys for the PDX map.

    We keep this *conservative* to avoid wrong matches:
    - direct normalized
    - stripped of separators
    - common OEM truncation variants (P0000XX -> PXX)
    - base DTC with failure-type 00 (P012300 when we have P012311)
    - suffix-unique match handled separately in `_dtc_description`
    """
    c = _normalize_dtc_code(code)
    if not c:
        return []

    # 1) direct
    out: List[str] = [c]

    # 2) remove common separators/spaces
    c2 = re.sub(r'[^0-9A-Z]', '', c)
    if c2 and c2 != c:
        out.append(c2)

    # 3) Try 5-char OBD-ish form (P0123) from longer OEM strings like P012300
    # (Some tools append bytes/status; keep only the base 5 if present).
    m = re.fullmatch(r'([PCBU][0-9A-F]{4})[0-9A-F]{1,3}', c2)
    if m:
        out.append(m.group(1))

    # 4) OEM quirk: P0000XX sometimes stored as PXX.
    m = re.fullmatch(r'([PCBU])0000([0-9A-F]{2})', c2)
    if m:
        out.append(f"{m.group(1)}{m.group(2)}")

    # 5) VW/Audi: 7-char DTC with failure-type byte (last 2 hex digits).
    #    If e.g. P012311 is not found, try base code P012300 (generic).
    m7 = re.fullmatch(r'([PCBU][0-9A-F]{4})([0-9A-F]{2})', c2)
    if m7 and m7.group(2) != '00':
        base = m7.group(1) + '00'
        if base not in out:
            out.append(base)

    # De-dup while preserving order
    seen: set = set()
    uniq: List[str] = []
    for k in out:
        if k not in seen:
            uniq.append(k)
            seen.add(k)
    return uniq


def _dtc_description(code: Optional[str], dtc_map: Optional[Dict[str, str]]) -> str:
    """Resolve a DTC description using the PDX map, with safe fallbacks."""
    if not isinstance(dtc_map, dict):
        return ''
    c = _normalize_dtc_code(code)
    if not c:
        return ''

    # Direct + conservative variants first.
    for k in _dtc_lookup_candidates(c):
        desc = dtc_map.get(k)
        if isinstance(desc, str) and desc.strip():
            return desc.strip()

    # Last-resort fallback for truncated OEM codes (e.g. P0000B3) where the PDX
    # index stores a longer fully-qualified code (e.g. P0627B3). We only accept
    # the match if it is UNIQUE and letter-consistent to avoid wrong mappings.
    #
    # Rules:
    # - Only for patterns P0000XX or P0000XXX (2-3 hex digits)
    # - Search dtc_map keys starting with same letter and ending with that suffix
    # - Use only if exactly one candidate exists
    m = re.fullmatch(r'([PCBU])0000([0-9A-F]{2,3})', re.sub(r'[^0-9A-Z]', '', c))
    if m:
        letter = m.group(1)
        suffix = m.group(2)
        try:
            candidates = [
                k
                for k in dtc_map.keys()
                if isinstance(k, str) and k.startswith(letter) and k.endswith(suffix)
            ]
        except Exception:
            candidates = []
        if len(candidates) == 1:
            desc = dtc_map.get(candidates[0])
            if isinstance(desc, str) and desc.strip():
                return desc.strip()
    return ''


def _extract_odx_component(filename: str) -> str:
    """Extract the component root from a BV/EV ODX filename.

    Examples:
        BV_AirbaUDS_011200_d.odx -> 'Airba'
        EV_AirbaVW31SMEAU65x_003004_d.odx -> 'Airba'
        BV_GatewUDS_002008_d.odx -> 'Gatew'
        EV_GatewNPLB7XX_002003_d.odx -> 'Gatew'
        BL_ECMDFCC_001089.odx -> 'ECMDFCC'
    """
    bn = os.path.basename(str(filename or ''))
    # Remove file extension(s)
    bn = re.sub(r'(_d)?\.(odx|xml|odx-[a-z])$', '', bn, flags=re.IGNORECASE)
    # Remove prefix
    for pre in ('DLC_BV_', 'DLC_EV_', 'DLC_BL_', 'BV_', 'EV_', 'BL_'):
        if bn.startswith(pre):
            bn = bn[len(pre):]
            break
    # Remove trailing version numbers _011200 etc.
    bn = re.sub(r'_\d{3,}.*$', '', bn)
    # Remove trailing 'UDS'
    bn = re.sub(r'UDS$', '', bn)
    return bn.strip()


# Global cache for per-file DTC data
_PDX_DTC_BY_FILE_CACHE: Optional[Dict[str, Dict[str, str]]] = None
_PDX_DTC_BY_FILE_CACHE_SRC: Optional[str] = None


def _dtc_description_for_ecu(
    code: Optional[str],
    dtc_map: Optional[Dict[str, str]],
    by_file: Optional[Dict[str, Dict[str, str]]],
    source_odx: Optional[str],
    dtc_ecu_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """ECU-aware DTC description lookup.

    Priority order:
    1. Direct file match: dtc_ecu_map[code][source_odx_basename]
    2. Component-prefix match in by_file (conflicts only)
    3. Global dtc_map fallback
    """
    c = _normalize_dtc_code(code)
    if not c:
        return ''

    if isinstance(source_odx, str) and source_odx.strip():
        ecu_file = os.path.basename(source_odx.strip())

        # Priority 1: exact file match in dtc_ecu_map (EV_/BV_ files only)
        if isinstance(dtc_ecu_map, dict):
            file_map = dtc_ecu_map.get(c)
            if isinstance(file_map, dict):
                desc = file_map.get(ecu_file)
                if isinstance(desc, str) and desc.strip():
                    return desc.strip()
                # Try lookup candidates (e.g. base code P012300 for P012311)
                for alt in _dtc_lookup_candidates(c)[1:]:
                    file_map_alt = dtc_ecu_map.get(alt)
                    if isinstance(file_map_alt, dict):
                        desc = file_map_alt.get(ecu_file)
                        if isinstance(desc, str) and desc.strip():
                            return desc.strip()

        # Priority 2: component-prefix match in by_file (conflict resolution fallback)
        if isinstance(by_file, dict):
            file_descs = by_file.get(c)
            if isinstance(file_descs, dict) and len(file_descs) > 1:
                ecu_root = _extract_odx_component(source_odx)
                if ecu_root and len(ecu_root) >= 3:
                    best_desc = ''
                    best_score = -1
                    for fn, desc in file_descs.items():
                        if not isinstance(desc, str) or not desc.strip():
                            continue
                        fn_root = _extract_odx_component(fn)
                        score = 0
                        if fn_root == ecu_root:
                            score = 100
                        elif fn_root.startswith(ecu_root) or ecu_root.startswith(fn_root):
                            score = 80
                        else:
                            cp = 0
                            for i in range(min(len(fn_root), len(ecu_root))):
                                if fn_root[i] == ecu_root[i]:
                                    cp += 1
                                else:
                                    break
                            if cp >= 5:
                                score = 50 + cp
                        bn = os.path.basename(fn)
                        if bn.startswith('EV_'):
                            score += 10
                        elif bn.startswith('BV_'):
                            score += 5
                        if score > best_score:
                            best_score = score
                            best_desc = desc.strip()
                    if best_desc and best_score >= 50:
                        return best_desc

    # Priority 3: global lookup
    return _dtc_description(code, dtc_map)


def _load_active_pdx_dtc_map() -> Dict[str, str]:
    """Load DISPLAY-TROUBLE-CODE -> TEXT map from the active PDX, if present."""
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
        if not os.path.isfile(cfg_path):
            return {}
        import json
        with open(cfg_path, 'r', encoding='utf-8') as fp:
            raw = json.load(fp)
        cfg = raw.get('config') if isinstance(raw, dict) else None
        if not isinstance(cfg, dict):
            return {}
        proj = cfg.get('project')
        if not isinstance(proj, dict) or proj.get('kind') != 'pdx':
            return {}
        fn = str(proj.get('filename') or '').strip()
        if not fn:
            return {}
        dtc_path = os.path.join(base_dir, 'projects', 'pdx', fn + '.dtc_index.json')

        global _PDX_DTC_MAP_CACHE, _PDX_DTC_MAP_CACHE_SRC
        if _PDX_DTC_MAP_CACHE is not None and _PDX_DTC_MAP_CACHE_SRC == dtc_path:
            return _PDX_DTC_MAP_CACHE

        # Self-heal: if dtc index is missing, attempt a best-effort rebuild.
        if not os.path.isfile(dtc_path):
            try:
                from .pdx_parser import build_dtc_index_from_pdx
                pdx_path = os.path.join(base_dir, 'projects', 'pdx', fn)
                if os.path.isfile(pdx_path):
                    idx = build_dtc_index_from_pdx(pdx_path, max_files=None, max_seconds=120.0)
                    try:
                        with open(dtc_path, 'w', encoding='utf-8') as fp:
                            json.dump(idx, fp, indent=2, sort_keys=True)
                    except Exception:
                        pass
            except Exception:
                pass

        if not os.path.isfile(dtc_path):
            return {}

        with open(dtc_path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        m = data.get('map') if isinstance(data, dict) else None
        # Self-heal: if the cached index lacks the trouble_to_display map
        # (older index format), rebuild from PDX so VW OEM display codes can
        # be resolved correctly.
        if isinstance(data, dict) and 'trouble_to_display_by_file' not in data:
            try:
                from .pdx_parser import build_dtc_index_from_pdx
                pdx_path = os.path.join(base_dir, 'projects', 'pdx', fn)
                if os.path.isfile(pdx_path):
                    idx = build_dtc_index_from_pdx(pdx_path, max_files=None, max_seconds=180.0)
                    try:
                        with open(dtc_path, 'w', encoding='utf-8') as fp:
                            json.dump(idx, fp, indent=2, sort_keys=True)
                    except Exception:
                        pass
                    data = idx
                    m = data.get('map')
            except Exception:
                pass
        _PDX_DTC_MAP_CACHE = m if isinstance(m, dict) else {}
        _PDX_DTC_MAP_CACHE_SRC = dtc_path
        # Also cache by_file data for ECU-scoped lookups
        global _PDX_DTC_BY_FILE_CACHE, _PDX_DTC_BY_FILE_CACHE_SRC
        bf = data.get('by_file') if isinstance(data, dict) else None
        _PDX_DTC_BY_FILE_CACHE = bf if isinstance(bf, dict) else {}
        _PDX_DTC_BY_FILE_CACHE_SRC = dtc_path
        # Cache dtc_ecu_map (EV_/BV_ file ownership)
        global _PDX_DTC_ECU_MAP_CACHE, _PDX_DTC_ECU_MAP_CACHE_SRC
        em = data.get('dtc_ecu_map') if isinstance(data, dict) else None
        _PDX_DTC_ECU_MAP_CACHE = em if isinstance(em, dict) else {}
        _PDX_DTC_ECU_MAP_CACHE_SRC = dtc_path
        # Cache trouble-code -> display-code maps (VW OEM-correct lookup)
        global _PDX_TROUBLE_TO_DISPLAY_CACHE, _PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE
        td = data.get('trouble_to_display') if isinstance(data, dict) else None
        _PDX_TROUBLE_TO_DISPLAY_CACHE = td if isinstance(td, dict) else {}
        tdf = data.get('trouble_to_display_by_file') if isinstance(data, dict) else None
        _PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE = tdf if isinstance(tdf, dict) else {}
        return _PDX_DTC_MAP_CACHE
    except Exception:
        return {}


def _load_active_pdx_dtc_by_file() -> Dict[str, Dict[str, str]]:
    """Return the by_file DTC data (populated as side effect of _load_active_pdx_dtc_map)."""
    global _PDX_DTC_BY_FILE_CACHE
    if _PDX_DTC_BY_FILE_CACHE is None:
        _load_active_pdx_dtc_map()
    return _PDX_DTC_BY_FILE_CACHE if isinstance(_PDX_DTC_BY_FILE_CACHE, dict) else {}


def _load_active_pdx_dtc_ecu_map() -> Dict[str, Dict[str, str]]:
    """Return the dtc_ecu_map (EV_/BV_ file ownership, populated as side effect of _load_active_pdx_dtc_map)."""
    global _PDX_DTC_ECU_MAP_CACHE
    if _PDX_DTC_ECU_MAP_CACHE is None:
        _load_active_pdx_dtc_map()
    return _PDX_DTC_ECU_MAP_CACHE if isinstance(_PDX_DTC_ECU_MAP_CACHE, dict) else {}


def _load_active_pdx_trouble_to_display() -> Dict[str, str]:
    """Return raw TROUBLE-CODE (decimal-string int) -> DISPLAY-TROUBLE-CODE map."""
    global _PDX_TROUBLE_TO_DISPLAY_CACHE
    if _PDX_TROUBLE_TO_DISPLAY_CACHE is None:
        _load_active_pdx_dtc_map()
    return _PDX_TROUBLE_TO_DISPLAY_CACHE if isinstance(_PDX_TROUBLE_TO_DISPLAY_CACHE, dict) else {}


def _load_active_pdx_trouble_to_display_by_file() -> Dict[str, Dict[str, str]]:
    """Return per-file trouble-code -> display-code map for ECU-scoped lookup."""
    global _PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE
    if _PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE is None:
        _load_active_pdx_dtc_map()
    return _PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE if isinstance(_PDX_TROUBLE_TO_DISPLAY_BY_FILE_CACHE, dict) else {}


def _resolve_display_code_for_ecu(
    uds_dtc: int,
    source_odx: Optional[str],
    trouble_by_file: Optional[Dict[str, Dict[str, str]]],
    trouble_global: Optional[Dict[str, str]],
) -> str:
    """Resolve OEM DISPLAY-TROUBLE-CODE from raw UDS DTC integer.

    VW/VAG and other OEMs use display codes that do NOT match the SAE-J2012
    encoding of the raw 3-byte trouble code. Always prefer the PDX mapping.

    Priority:
    1. Per-file map at the ECU's source ODX.
    2. Component-prefix match across files (when raw int is shared, e.g. EV_RDK*).
    3. Global trouble_to_display map.
    4. SAE-J2012 fallback (legacy behaviour).
    """
    if not isinstance(uds_dtc, int) or uds_dtc <= 0:
        return ''
    key = str(int(uds_dtc) & 0xFFFFFF)

    # Priority 1: per-file exact match
    if isinstance(trouble_by_file, dict) and isinstance(source_odx, str) and source_odx.strip():
        ecu_file = os.path.basename(source_odx.strip())
        file_map = trouble_by_file.get(key)
        if isinstance(file_map, dict):
            v = file_map.get(ecu_file)
            if isinstance(v, str) and v.strip():
                return v.strip()

            # Priority 2: component-prefix match (BV_<X>... and EV_<X>... share root)
            ecu_root = _extract_odx_component(source_odx)
            if ecu_root and len(ecu_root) >= 3:
                best = ''
                best_score = -1
                for fn, disp in file_map.items():
                    if not isinstance(disp, str) or not disp.strip():
                        continue
                    fn_root = _extract_odx_component(fn)
                    score = 0
                    if fn_root == ecu_root:
                        score = 100
                    elif fn_root.startswith(ecu_root) or ecu_root.startswith(fn_root):
                        score = 80
                    else:
                        cp = 0
                        for i in range(min(len(fn_root), len(ecu_root))):
                            if fn_root[i] == ecu_root[i]:
                                cp += 1
                            else:
                                break
                        if cp >= 5:
                            score = 50 + cp
                    bn = os.path.basename(fn)
                    if bn.startswith('EV_'):
                        score += 10
                    elif bn.startswith('BV_'):
                        score += 5
                    if score > best_score:
                        best_score = score
                        best = disp.strip()
                if best and best_score >= 50:
                    return best

    # Priority 3: global mapping
    if isinstance(trouble_global, dict):
        v = trouble_global.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Priority 4: SAE-J2012 fallback
    return _uds_dtc_to_display_code(uds_dtc)


def _load_active_pdx_env_data_index() -> Dict[str, Any]:
    """Load ENV-DATA index from the active PDX, if present.

    Returns {env_data: {id: {short_name, long_name, byte_length, params}},
              dtc_env_mapping: {dtc_code: [env_id, ...]}}
    """
    try:
        import json
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
        if not os.path.isfile(cfg_path):
            return {}
        with open(cfg_path, 'r', encoding='utf-8') as fp:
            raw = json.load(fp)
        cfg = raw.get('config') if isinstance(raw, dict) else None
        if not isinstance(cfg, dict):
            return {}
        proj = cfg.get('project')
        if not isinstance(proj, dict) or proj.get('kind') != 'pdx':
            return {}
        fn = str(proj.get('filename') or '').strip()
        if not fn:
            return {}
        env_path = os.path.join(base_dir, 'projects', 'pdx', fn + '.env_data_index.json')

        global _PDX_ENV_DATA_CACHE, _PDX_ENV_DATA_CACHE_SRC
        if _PDX_ENV_DATA_CACHE is not None and _PDX_ENV_DATA_CACHE_SRC == env_path:
            return _PDX_ENV_DATA_CACHE

        # Self-heal: build from PDX if cache file is missing
        if not os.path.isfile(env_path):
            try:
                from .pdx_parser import build_env_data_index_from_pdx
                pdx_path = os.path.join(base_dir, 'projects', 'pdx', fn)
                if os.path.isfile(pdx_path):
                    idx = build_env_data_index_from_pdx(pdx_path, max_files=None, max_seconds=120.0)
                    try:
                        with open(env_path, 'w', encoding='utf-8') as fp:
                            json.dump(idx, fp, indent=2, sort_keys=True)
                    except Exception:
                        pass
            except Exception:
                pass

        if not os.path.isfile(env_path):
            _PDX_ENV_DATA_CACHE = {}
            _PDX_ENV_DATA_CACHE_SRC = env_path
            return {}

        with open(env_path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        _PDX_ENV_DATA_CACHE = data if isinstance(data, dict) else {}
        _PDX_ENV_DATA_CACHE_SRC = env_path
        return _PDX_ENV_DATA_CACHE
    except Exception:
        return {}


def _load_active_pdx_did_index() -> Dict[int, Dict[str, Any]]:
    """Load DID metadata map from the active PDX, if present.

    Returns mapping: did_int -> {short_name,long_name,byte_length,bit_length,...}
    """
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
        if not os.path.isfile(cfg_path):
            return {}
        import json
        with open(cfg_path, 'r', encoding='utf-8') as fp:
            raw = json.load(fp)
        cfg = raw.get('config') if isinstance(raw, dict) else None
        if not isinstance(cfg, dict):
            return {}
        proj = cfg.get('project')
        if not isinstance(proj, dict) or proj.get('kind') != 'pdx':
            return {}
        fn = str(proj.get('filename') or '').strip()
        if not fn:
            return {}
        did_path = os.path.join(base_dir, 'projects', 'pdx', fn + '.did_index.json')

        global _PDX_DID_INDEX_CACHE, _PDX_DID_INDEX_CACHE_SRC
        if _PDX_DID_INDEX_CACHE is not None and _PDX_DID_INDEX_CACHE_SRC == did_path:
            return _PDX_DID_INDEX_CACHE

        # Self-heal: if did index is missing, attempt a best-effort rebuild.
        if not os.path.isfile(did_path):
            try:
                from .pdx_parser import build_did_index_from_pdx
                pdx_path = os.path.join(base_dir, 'projects', 'pdx', fn)
                if os.path.isfile(pdx_path):
                    idx = build_did_index_from_pdx(pdx_path, max_files=None, max_seconds=120.0)
                    try:
                        with open(did_path, 'w', encoding='utf-8') as fp:
                            json.dump(idx, fp, indent=2, sort_keys=True)
                    except Exception:
                        pass
            except Exception:
                pass

        if not os.path.isfile(did_path):
            return {}

        with open(did_path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        m = data.get('map') if isinstance(data, dict) else None
        if not isinstance(m, dict):
            _PDX_DID_INDEX_CACHE = {}
            _PDX_DID_INDEX_CACHE_SRC = did_path
            return _PDX_DID_INDEX_CACHE

        out: Dict[int, Dict[str, Any]] = {}
        for k, v in m.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            kk = k.strip()
            try:
                did = int(kk, 0) if kk.lower().startswith('0x') else int(kk, 16)
            except Exception:
                continue
            out[int(did) & 0xFFFF] = v

        _PDX_DID_INDEX_CACHE = out
        _PDX_DID_INDEX_CACHE_SRC = did_path
        return _PDX_DID_INDEX_CACHE
    except Exception:
        return {}


def _get_active_pdx_path() -> Optional[str]:
    """Return absolute path to the active PDX file (if configured)."""
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
        if not os.path.isfile(cfg_path):
            return None
        import json
        with open(cfg_path, 'r', encoding='utf-8') as fp:
            raw = json.load(fp)
        cfg = raw.get('config') if isinstance(raw, dict) else None
        if not isinstance(cfg, dict):
            return None
        proj = cfg.get('project')
        if not isinstance(proj, dict) or proj.get('kind') != 'pdx':
            return None
        fn = str(proj.get('filename') or '').strip()
        if not fn:
            return None
        pdx_path = os.path.join(base_dir, 'projects', 'pdx', fn)
        return pdx_path if os.path.isfile(pdx_path) else None
    except Exception:
        return None


def _load_active_pdx_comm_index() -> Dict[str, Any]:
    """Load ECU comm/addressing index from the active PDX, if present.

    This is used to drive scanning strictly from project-defined protocol/addressing
    instead of heuristics/ranges.
    """
    try:
        import json
        pdx_path = _get_active_pdx_path()
        if not pdx_path or not os.path.isfile(pdx_path):
            return {}

        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        fn = os.path.basename(pdx_path)
        comm_path = os.path.join(base_dir, 'projects', 'pdx', fn + '.comm_index.json')

        global _PDX_COMM_INDEX_CACHE, _PDX_COMM_INDEX_CACHE_SRC
        if _PDX_COMM_INDEX_CACHE is not None and _PDX_COMM_INDEX_CACHE_SRC == comm_path:
            return _PDX_COMM_INDEX_CACHE

        idx = None
        if os.path.isfile(comm_path):
            try:
                with open(comm_path, 'r', encoding='utf-8') as fp:
                    idx = json.load(fp)
            except Exception:
                idx = None

        if idx is None:
            try:
                # Support both module layouts:
                # - imported as package:   kvaser_bus_manager.backend.vag_scanner
                # - imported as top-level: vag_scanner (PYTHONPATH points to backend/)
                try:
                    from .pdx_parser import build_comm_index_from_pdx  # type: ignore
                except Exception:
                    from pdx_parser import build_comm_index_from_pdx  # type: ignore

                idx = build_comm_index_from_pdx(pdx_path, max_files=None, max_seconds=60.0)
                try:
                    tmp = comm_path + '.tmp'
                    os.makedirs(os.path.dirname(comm_path), exist_ok=True)
                    with open(tmp, 'w', encoding='utf-8') as fp:
                        json.dump(idx, fp, indent=2, sort_keys=True)
                    os.replace(tmp, comm_path)
                except Exception:
                    pass
            except Exception:
                idx = None

        _PDX_COMM_INDEX_CACHE = idx if isinstance(idx, dict) else {}
        _PDX_COMM_INDEX_CACHE_SRC = comm_path
        return _PDX_COMM_INDEX_CACHE
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Per-ECU ClearDiagnosticInformation profile derived from the PDX/ODX layers.
# ---------------------------------------------------------------------------
#
# Background. ISO 14229 ClearDiagnosticInformation (SID 0x14) takes a 3-byte
# "Group of DTC". VAG VW80124 base ECUs inherit the generic service
#   `DiagnServi_ClearDiagnInfor`             -> 14 FF FF FF (all groups)
# from the functional group `FG_AllUDSSyste`.
# OBD-affected ECUs (engine, transmission, traction battery, drive motor,
# thermal mgmt, OBC, DC/DC HV, ...) explicitly mark `DiagnServi_ClearDiagnInfor`
# as <NOT-INHERITED-DIAG-COMM> from `FG_AllUDSSyste`, and instead inherit
# `DiagnServi_ClearDiagnInforWWHOBD` from `FG_AllOBDSyste` which encodes
#   14 FF FF 33  (Emissions-system / ZEV propulsion systems group)
# (DOP_TEXTTABLEGroupOfDTCsWWHOBD: FFFF33 -> "Emissions-system group / ZEV
#  propulsion systems group", FFFFFF -> "All Groups").
#
# This parser opens each ECU's source ODX in the active PDX, reads the
# <PARENT-REF>/<NOT-INHERITED-DIAG-COMMS> blocks, and produces a list of clear
# variants the ECU is expected to accept, in priority order.
# Result is cached in-process and persisted next to the comm_index JSON.

_FG_UDS = 'FG_AllUDSSyste'
_FG_OBD = 'FG_AllOBDSyste'
_FG_EMISSIONS = 'FG_AllEmissRelatUDSSyste'
_SVC_CLEAR_UDS = 'DiagnServi_ClearDiagnInfor'
_SVC_CLEAR_WWHOBD = 'DiagnServi_ClearDiagnInforWWHOBD'
_SVC_CLEAR_OBD_LEGACY = 'DiagnServi_ClearResetEmissRelatDiagnInfor'


def _pdx_profile_is_obd_mil_candidate(profile: Dict[str, Any]) -> bool:
    """Return True for ECUs whose PDX layer can own emissions/MIL state."""
    if not isinstance(profile, dict):
        return False
    parents = {str(p) for p in (profile.get('parents') or [])}
    if _FG_OBD in parents or _FG_EMISSIONS in parents:
        return True
    short = str(profile.get('short_name') or '').lower()
    return any(token in short for token in (
        'engincontrmodul',
        'transcontrmodul',
        'drivemotorcontrmodul',
        'batteenergcontrmodul',
        'battechargcontrmodul',
        'thermmanag',
        'dcdcconve',
    ))


def _pdx_profile_mil_priority(profile: Dict[str, Any]) -> int:
    """Lower values are better candidates for OBD Mode 01 MIL status."""
    short = str((profile or {}).get('short_name') or '').lower()
    if 'engincontrmodul' in short:
        return 0
    if 'transcontrmodul' in short:
        return 1
    if 'drivemotorcontrmodul' in short:
        return 2
    if 'batteenergcontrmodul' in short:
        return 3
    if 'thermmanag' in short or 'battechargcontrmodul' in short or 'dcdcconve' in short:
        return 4
    if _pdx_profile_is_obd_mil_candidate(profile):
        return 5
    return 9


def _parse_odx_clear_capabilities(odx_text: str) -> Dict[str, Any]:
    """Parse a single base-variant ODX for inherited Clear* services.

    Returns:
      {
        'parents': [parent_id_ref, ...],
        'not_inherited': {parent_id_ref: set([SHORT-NAME, ...])},
      }
    """
    parents: List[str] = []
    not_inh: Dict[str, set] = {}
    for pr in re.finditer(
        r'<PARENT-REF\b[^>]*ID-REF="([^"]+)"[^>]*>(.*?)</PARENT-REF>',
        odx_text,
        re.S,
    ):
        parent = pr.group(1)
        body = pr.group(2)
        parents.append(parent)
        excluded = set(
            re.findall(r'<DIAG-COMM-SNREF\s+SHORT-NAME="([^"]+)"', body)
        )
        not_inh[parent] = excluded
    return {'parents': parents, 'not_inherited': not_inh}


def _derive_clear_variants(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build an ordered list of ClearDiagnosticInformation requests this ECU
    is expected to accept, derived from inheritance.

    Each entry is:
      {'sid': int, 'group': bytes, 'label': str, 'session': int|None}
    where `session` is the diagnostic session that ODIS uses before issuing the
    clear (None means "current session" / try in extended).
    """
    parents = set(parsed.get('parents') or [])
    ni = parsed.get('not_inherited') or {}
    uds_excluded = _SVC_CLEAR_UDS in ni.get(_FG_UDS, set())
    wwhobd_excluded = _SVC_CLEAR_WWHOBD in ni.get(_FG_OBD, set())
    has_uds_parent = _FG_UDS in parents
    has_obd_parent = _FG_OBD in parents

    variants: List[Dict[str, Any]] = []
    # Priority 1: generic VW80124 clear (all groups) when inherited.
    if has_uds_parent and not uds_excluded:
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\xFF',
            'label': 'UDS Clear All (FFFFFF)',
            'session': 0x03,
        })
    # Priority 2: WWH-OBD clear (emissions group) when inherited.
    if has_obd_parent and not wwhobd_excluded:
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\x33',
            'label': 'WWH-OBD Clear Emissions (FFFF33)',
            'session': 0x03,
        })
    # If only one is listed, append the other as a best-effort fallback. Real
    # vehicles sometimes return NRC 0x11 even when ODX inheritance suggests
    # support, and vice-versa, so keeping both attempts is safer than failing.
    have_groups = {v['group'] for v in variants}
    if b'\xFF\xFF\xFF' not in have_groups:
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\xFF',
            'label': 'UDS Clear All (FFFFFF, fallback)',
            'session': 0x03,
        })
    if b'\xFF\xFF\x33' not in have_groups:
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\x33',
            'label': 'WWH-OBD Clear Emissions (FFFF33, fallback)',
            'session': 0x03,
        })
    # If we still have nothing to offer (no parents discovered), fall back to
    # default-session attempts; some standalone ECUs only accept clear in 0x01.
    if not variants:
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\x33',
            'label': 'WWH-OBD Clear Emissions (FFFF33, default session)',
            'session': 0x01,
        })
        variants.append({
            'sid': 0x14, 'group': b'\xFF\xFF\xFF',
            'label': 'UDS Clear All (FFFFFF, default session)',
            'session': 0x01,
        })
    return variants


def _build_pdx_clear_profile(pdx_path: str, comm_index: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Build {logical_ecu_address: {odx, variants:[...]}} from the PDX zip."""
    out: Dict[int, Dict[str, Any]] = {}
    if not pdx_path or not os.path.isfile(pdx_path):
        return out
    rows = comm_index.get('ecus') if isinstance(comm_index, dict) else None
    if not isinstance(rows, list):
        return out
    try:
        with zipfile.ZipFile(pdx_path) as z:
            names_lc = {n.lower(): n for n in z.namelist()}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                doip = r.get('doip') or {}
                la = doip.get('logical_ecu_address') if isinstance(doip, dict) else None
                if not isinstance(la, int):
                    continue
                key = int(la) & 0xFFFF
                if key == 0:
                    continue
                odx_name = str(r.get('source_odx') or '').strip()
                variants: List[Dict[str, Any]] = []
                parsed: Dict[str, Any] = {}
                if odx_name:
                    real = names_lc.get(odx_name.lower())
                    if real:
                        try:
                            txt = z.read(real).decode('utf-8', errors='ignore')
                            parsed = _parse_odx_clear_capabilities(txt)
                            variants = _derive_clear_variants(parsed)
                        except Exception:
                            variants = []
                if not variants:
                    # Conservative default if no ODX info: try standard then WWH-OBD.
                    variants = [
                        {'sid': 0x14, 'group': b'\xFF\xFF\xFF',
                         'label': 'UDS Clear All (FFFFFF)', 'session': 0x03},
                        {'sid': 0x14, 'group': b'\xFF\xFF\x33',
                         'label': 'WWH-OBD Clear Emissions (FFFF33)', 'session': 0x03},
                    ]
                out[key] = {
                    'short_name': r.get('short_name'),
                    'source_odx': odx_name,
                    'parents': parsed.get('parents') or [],
                    'not_inherited': {
                        k: sorted(list(v))
                        for k, v in (parsed.get('not_inherited') or {}).items()
                    },
                    'variants': [
                        {'sid': v['sid'], 'group_hex': v['group'].hex().upper(),
                         'label': v['label'], 'session': v.get('session')}
                        for v in variants
                    ],
                }
                out[key]['obd_mil_candidate'] = _pdx_profile_is_obd_mil_candidate(out[key])
    except Exception:
        return out
    return out


def _load_active_pdx_clear_profile() -> Dict[int, Dict[str, Any]]:
    """Return per-ECU ClearDiagnosticInformation profile for the active PDX."""
    try:
        pdx_path = _get_active_pdx_path()
        if not pdx_path:
            return {}
        base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
        fn = os.path.basename(pdx_path)
        cache_path = os.path.join(base_dir, 'projects', 'pdx', fn + '.clear_profile.json')

        global _PDX_CLEAR_PROFILE_CACHE, _PDX_CLEAR_PROFILE_CACHE_SRC
        if _PDX_CLEAR_PROFILE_CACHE is not None and _PDX_CLEAR_PROFILE_CACHE_SRC == cache_path:
            return _PDX_CLEAR_PROFILE_CACHE

        loaded: Optional[Dict[int, Dict[str, Any]]] = None
        if os.path.isfile(cache_path):
            try:
                import json
                with open(cache_path, 'r', encoding='utf-8') as fp:
                    raw = json.load(fp)
                if isinstance(raw, dict):
                    loaded = {int(k, 0) if isinstance(k, str) else int(k): v
                              for k, v in raw.items()}
            except Exception:
                loaded = None

        if loaded is None:
            ci = _load_active_pdx_comm_index()
            loaded = _build_pdx_clear_profile(pdx_path, ci)
            try:
                import json
                tmp = cache_path + '.tmp'
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                serial = {f'0x{k:04X}': v for k, v in loaded.items()}
                with open(tmp, 'w', encoding='utf-8') as fp:
                    json.dump(serial, fp, indent=2, sort_keys=True)
                os.replace(tmp, cache_path)
            except Exception:
                pass

        _PDX_CLEAR_PROFILE_CACHE = loaded
        _PDX_CLEAR_PROFILE_CACHE_SRC = cache_path
        return loaded
    except Exception:
        return {}


def _pdx_extract_uds_on_can_defaults(pdx_path: Optional[str]) -> Dict[str, int]:
    """Best-effort extraction of UDS-on-CAN default IDs from the active PDX.

    In VAG PDX bundles, ISO_15765_3_on_ISO_15765_2 ODX often contains:
      - ISO_15765_2.CP_CanPhysReqId  (tester -> ECU) default 2016 (0x7E0)
      - ISO_15765_2.CP_CanRespUSDTId (ECU -> tester) default 2024 (0x7E8)

    Returns a dict with keys: can_phys_req_id, can_resp_usdt_id.
    """
    out: Dict[str, int] = {}
    try:
        if not pdx_path or not os.path.isfile(pdx_path):
            return out

        with zipfile.ZipFile(pdx_path) as z:
            # This filename exists in our bundled PDX; keep fallback if it changes.
            target = None
            for n in z.namelist():
                if n.lower() == 'iso_15765_3_on_iso_15765_2_003012.odx':
                    target = n
                    break
            if not target:
                return out
            txt = z.read(target).decode('utf-8', errors='ignore')

        def _extract_default(short_name: str) -> Optional[int]:
            # Locate COMPARAM by SHORT-NAME and read PHYSICAL-DEFAULT-VALUE.
            # Intentionally simple (regex) for speed and robustness.
            m = re.search(
                rf"<COMPARAM[^>]*>\s*<SHORT-NAME>{re.escape(short_name)}</SHORT-NAME>[\s\S]*?<PHYSICAL-DEFAULT-VALUE>([^<]+)</PHYSICAL-DEFAULT-VALUE>",
                txt,
                re.IGNORECASE,
            )
            if not m:
                return None
            raw = str(m.group(1) or '').strip()
            try:
                return int(raw, 10)
            except Exception:
                return None

        phys = _extract_default('CP_CanPhysReqId')
        resp = _extract_default('CP_CanRespUSDTId')
        if isinstance(phys, int):
            out['can_phys_req_id'] = int(phys)
        if isinstance(resp, int):
            out['can_resp_usdt_id'] = int(resp)
        return out
    except Exception:
        return {}


def _get_uds_on_can_base_ids() -> Tuple[int, int]:
    """Return baseline (tx_id, rx_id) for UDS-on-CAN.

    Uses the PDX defaults if available; otherwise falls back to 0x7E0/0x7E8.
    """
    tx_id = 0x7E0
    rx_id = 0x7E8
    try:
        pdx_path = _get_active_pdx_path()
        defaults = _pdx_extract_uds_on_can_defaults(pdx_path)
        if isinstance(defaults.get('can_phys_req_id'), int):
            tx_id = int(defaults['can_phys_req_id']) & 0x1FFFFFFF
        if isinstance(defaults.get('can_resp_usdt_id'), int):
            rx_id = int(defaults['can_resp_usdt_id']) & 0x1FFFFFFF
    except Exception:
        pass
    return int(tx_id), int(rx_id)


def _get_log_dir_default() -> str:
    """Return the default log dir.

    If the main Flask app sets KBSM_LOG_DIR, use it so scan artifacts follow the
    configured persistent storage directory.
    """
    env = str(os.getenv('KBSM_LOG_DIR') or '').strip()
    if env:
        return os.path.abspath(env)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(base_dir, '..', 'logs'))

# Try to import isotp, if not available, we can't scan UDS/ISOTP
try:
    import isotp
except ImportError:
    isotp = None


def _hex_bytes(b: bytes, *, max_len: int = 4096) -> str:
    if not b:
        return ''
    bb = bytes(b)
    if len(bb) > int(max_len):
        bb = bb[: int(max_len)]
    return ' '.join(f"{x:02X}" for x in bb)


def _did_name(meta: Optional[Dict[str, Any]]) -> str:
    if not isinstance(meta, dict):
        return ''
    ln = str(meta.get('long_name') or '').strip()
    sn = str(meta.get('short_name') or '').strip()
    return ln or sn


def _decode_did_value(did: int, raw: bytes, did_index: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    meta = did_index.get(int(did) & 0xFFFF) if isinstance(did_index, dict) else None
    name = _did_name(meta)
    out: Dict[str, Any] = {
        'did': f"0x{int(did) & 0xFFFF:04X}",
        'name': name,
        'raw_hex': _hex_bytes(raw),
    }
    # Common best-effort decodes (without assuming scaling).
    try:
        if raw and len(raw) in (1, 2, 3, 4, 8):
            out['uint_be'] = int.from_bytes(raw, 'big', signed=False)
    except Exception:
        pass
    try:
        if raw and all(32 <= int(x) < 127 for x in raw):
            out['ascii'] = raw.decode('ascii', errors='ignore').strip()
    except Exception:
        pass
    return out


def _parse_uds_snapshot_response(resp: bytes, did_index: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse UDS ReadDTCInformation subfunction 0x04 response (best-effort)."""
    if not resp or len(resp) < 7:
        return None
    if resp[0] != 0x59 or resp[1] != 0x04:
        return None

    # Expected (common): 59 04 DTC(3) Status RecordNumber NumDIDs [DID(2) data...]...
    dtc_val = (resp[2] << 16) | (resp[3] << 8) | resp[4]
    status = int(resp[5])
    rec_no = int(resp[6])
    idx = 7
    num = int(resp[idx]) if idx < len(resp) else 0
    idx += 1
    items: List[Dict[str, Any]] = []

    for _ in range(max(0, num)):
        if idx + 2 > len(resp):
            break
        did = (resp[idx] << 8) | resp[idx + 1]
        idx += 2
        meta = did_index.get(int(did) & 0xFFFF) if isinstance(did_index, dict) else None
        bl = meta.get('byte_length') if isinstance(meta, dict) else None
        if isinstance(bl, int) and bl > 0 and idx + bl <= len(resp):
            raw = resp[idx : idx + bl]
            idx += bl
            items.append(_decode_did_value(did, raw, did_index))
        else:
            # Unknown length: store remaining as tail and stop.
            tail = resp[idx:]
            items.append(_decode_did_value(did, tail, did_index))
            idx = len(resp)
            break

    tail_hex = _hex_bytes(resp[idx:]) if idx < len(resp) else ''
    return {
        'dtc': f"0x{dtc_val:06X}",
        'status': status,
        'record_number': rec_no,
        'items': items,
        'tail_hex': tail_hex,
        'raw_hex': _hex_bytes(resp),
    }


def _parse_uds_extended_data_response(resp: bytes, did_index: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse UDS ReadDTCInformation subfunction 0x06 response (best-effort).

    Extended data commonly contains occurrence counters, aging counters,
    and sometimes timestamps (BCD or epoch).  We attempt to decode these.
    """
    if not resp or len(resp) < 7:
        return None
    if resp[0] != 0x59 or resp[1] != 0x06:
        return None

    dtc_val = (resp[2] << 16) | (resp[3] << 8) | resp[4]
    status = int(resp[5])
    rec_no = int(resp[6])
    data = resp[7:] if len(resp) > 7 else b''

    result: Dict[str, Any] = {
        'dtc': f"0x{dtc_val:06X}",
        'status': status,
        'record_number': rec_no,
        'data_hex': _hex_bytes(data),
        'raw_hex': _hex_bytes(resp),
    }

    # Best-effort decode of common VW/Audi extended data record layouts.
    # VW commonly uses record 0x01 with:
    #   byte 0:    occurrence counter
    #   byte 1:    aging counter (down-counter)
    #   byte 2:    aged counter
    #   byte 3-8:  BCD timestamp (YYMMDD hhmmss) or other OEM-specific
    # Other OEMs may pack data differently. We try heuristically.
    decoded: Dict[str, Any] = {}
    if data:
        # Occurrence counter (first byte is almost always occurrence counter)
        if len(data) >= 1:
            decoded['occurrence_counter'] = int(data[0])
        if len(data) >= 2:
            decoded['aging_counter'] = int(data[1])
        if len(data) >= 3:
            decoded['aged_counter'] = int(data[2])

        # Try BCD timestamp at various offsets
        for offset in (3, 4, 2):
            if len(data) >= offset + 6:
                y = _bcd_to_int(data[offset])
                mo = _bcd_to_int(data[offset + 1])
                d = _bcd_to_int(data[offset + 2])
                hh = _bcd_to_int(data[offset + 3])
                mm = _bcd_to_int(data[offset + 4])
                ss = _bcd_to_int(data[offset + 5])
                if all(isinstance(x, int) for x in (y, mo, d, hh, mm, ss)):
                    year = 2000 + int(y)
                    if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31 and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 0 <= int(ss) <= 59:
                        decoded['timestamp_iso'] = f"{year:04d}-{int(mo):02d}-{int(d):02d}T{int(hh):02d}:{int(mm):02d}:{int(ss):02d}"
                        decoded['timestamp_offset'] = offset
                        break

        # Try 4-byte epoch at tail (some OEMs)
        if 'timestamp_iso' not in decoded and len(data) >= 7:
            tail4 = data[-4:]
            try:
                epoch = int.from_bytes(tail4, 'big', signed=False)
                if 1104537600 <= epoch <= 4102444800:  # 2005..2100
                    import datetime
                    dt = datetime.datetime.utcfromtimestamp(epoch)
                    decoded['timestamp_iso'] = dt.replace(tzinfo=datetime.timezone.utc).isoformat()
                    decoded['timestamp_source'] = 'epoch_tail4'
            except Exception:
                pass

    if decoded:
        result['decoded'] = decoded
    return result


def _guess_odometer_km(extra: Dict[str, Any]) -> Optional[int]:
    """Try to infer an odometer-like integer from decoded snapshot items."""
    if not isinstance(extra, dict):
        return None
    snaps = extra.get('snapshots')
    if not isinstance(snaps, list):
        return None
    for s in snaps:
        if not isinstance(s, dict):
            continue
        for it in (s.get('items') or []):
            if not isinstance(it, dict):
                continue
            name = str(it.get('name') or '').lower()
            did = str(it.get('did') or '').upper()
            if 'mile' in name or 'odo' in name or 'km' in name or did in {'0xF40D', '0xF40E'}:
                v = it.get('uint_be')
                if isinstance(v, int) and 0 <= v < 10_000_000:
                    return int(v)
    return None


def _bcd_to_int(b: int) -> Optional[int]:
    try:
        b = int(b) & 0xFF
        hi = (b >> 4) & 0x0F
        lo = b & 0x0F
        if hi > 9 or lo > 9:
            return None
        return hi * 10 + lo
    except Exception:
        return None


def _guess_timestamp(extra: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Best-effort timestamp extraction from snapshot/extended data.

    We don't assume a single OEM format; we try a few safe patterns:
    - ASCII strings containing something date/time-ish
    - Common 6-byte BCD YYMMDDhhmmss
    - Common 7-byte BCD YYYYMMDDhhmmss (YYYY as 2 bytes BCD)
    - Common 8-byte Unix epoch seconds (big-endian)
    """
    if not isinstance(extra, dict):
        return None

    # 0) Check decoded timestamp from extended data (already parsed by _parse_uds_extended_data_response)
    ext = extra.get('extended_data')
    if isinstance(ext, list):
        for e in ext:
            if not isinstance(e, dict):
                continue
            decoded = e.get('decoded')
            if isinstance(decoded, dict):
                ts_iso = decoded.get('timestamp_iso')
                if isinstance(ts_iso, str) and ts_iso.strip():
                    return {'kind': 'extdata_decoded', 'timestamp_iso': ts_iso.strip(), 'from': {'source': 'extdata_decoded'}}

    candidates: List[Dict[str, Any]] = []

    # 1) From snapshot decoded items
    snaps = extra.get('snapshots')
    if isinstance(snaps, list):
        for s in snaps:
            if not isinstance(s, dict):
                continue
            for it in (s.get('items') or []):
                if not isinstance(it, dict):
                    continue
                name = str(it.get('name') or '')
                did = str(it.get('did') or '')
                raw_hex = str(it.get('raw_hex') or '')
                ascii_s = it.get('ascii')
                if isinstance(ascii_s, str) and ascii_s.strip():
                    candidates.append({'source': 'snapshot_ascii', 'did': did, 'name': name, 'value': ascii_s.strip()})
                if isinstance(raw_hex, str) and raw_hex.strip():
                    candidates.append({'source': 'snapshot_raw', 'did': did, 'name': name, 'value': raw_hex.strip()})

    # 2) From extended data raw
    ext = extra.get('extended_data')
    if isinstance(ext, list):
        for e in ext:
            if not isinstance(e, dict):
                continue
            hx = str(e.get('data_hex') or '').strip()
            if hx:
                candidates.append({'source': 'extdata_raw', 'did': '', 'name': '', 'value': hx})

    # Helper to parse hex string to bytes
    def _hex_to_bytes(h: str) -> Optional[bytes]:
        try:
            parts = [p for p in (h or '').split(' ') if p]
            if not parts:
                return None
            return bytes(int(p, 16) & 0xFF for p in parts)
        except Exception:
            return None

    # Try patterns
    for c in candidates:
        v = c.get('value')

        # ASCII date/time (very common: "2026-02-03 14:40:09", "03.02.2026 14:40:09", etc.)
        if isinstance(v, str) and any(ch.isdigit() for ch in v) and (':' in v or '-' in v or '.' in v or '/' in v):
            # keep raw; don't over-parse
            s = v.strip()
            if len(s) <= 64:
                return {'kind': 'ascii', 'text': s, 'from': c}

        b = _hex_to_bytes(v) if isinstance(v, str) else None
        if not b:
            continue

        # 6-byte BCD YYMMDDhhmmss
        if len(b) >= 6:
            y = _bcd_to_int(b[0])
            mo = _bcd_to_int(b[1])
            d = _bcd_to_int(b[2])
            hh = _bcd_to_int(b[3])
            mm = _bcd_to_int(b[4])
            ss = _bcd_to_int(b[5])
            if all(isinstance(x, int) for x in (y, mo, d, hh, mm, ss)):
                # Heuristic: year 00..99 -> 2000..2099
                year = 2000 + int(y)
                if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31 and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 0 <= int(ss) <= 59:
                    iso = f"{year:04d}-{int(mo):02d}-{int(d):02d}T{int(hh):02d}:{int(mm):02d}:{int(ss):02d}"
                    return {'kind': 'bcd6', 'timestamp_iso': iso, 'from': c}

        # 8-byte big-endian epoch seconds
        if len(b) == 8:
            try:
                epoch = int.from_bytes(b, 'big', signed=False)
                # sanity range: 2005..2100
                if 1104537600 <= epoch <= 4102444800:
                    # avoid importing datetime at top; local import ok
                    import datetime
                    dt = datetime.datetime.utcfromtimestamp(epoch)
                    return {'kind': 'epoch', 'timestamp_iso': dt.replace(tzinfo=datetime.timezone.utc).isoformat(), 'from': c}
            except Exception:
                pass

    return None


def _dtc_classify_idex(status: int) -> str:
    """IDEX-compatible DTC classification.

    - ACTIVE:   TestFailed (0x01) set → fault is currently present
    - PASSIVE:  Confirmed (0x08) but NOT TestFailed → stored/historical fault
    - SPORADIC: FailedSinceClear (0x20) without Confirmed and without TestFailed
    - PASSIVE:  Everything else (not tested, cleared, etc.)
    """
    if status & 0x01:
        return 'ACTIVE'
    if status & 0x08:
        return 'PASSIVE'
    if status & 0x20:
        return 'SPORADIC'
    return 'PASSIVE'


@dataclass(frozen=True)
class DtcItem:
    code: str
    uds_dtc: Optional[int]
    status_byte: Optional[int]
    status_desc: str
    active: bool
    raw: str
    description: str = ""
    dtc_class: str = ""  # IDEX-compatible: 'ACTIVE', 'PASSIVE', 'SPORADIC'
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EcuReport:
    tx_id: int
    rx_id: int
    name: str
    dtcs: List[DtcItem]
    obd: Dict[str, Any] = field(default_factory=dict)
    ident: Dict[str, Any] = field(default_factory=dict)
    contacted: bool = True


def _ecu_system_group(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ('engine', 'motor', 'battery', 'becm', 'bms', 'inverter', 'transmission', 'gearbox', 'tcm', 'ecm', 'powertrain', 'fuel', 'emission', 'exhaust')):
        return 'Powertrain'
    if any(k in n for k in ('airbag', 'srs', 'abs', 'esc', 'esc', 'brake', 'restraint', 'belt', 'crash', 'occupant', 'sensor cluster')):
        return 'Safety'
    if any(k in n for k in ('climate', 'hvac', 'a/c', ' ac ', 'heater', 'door', 'window', 'seat', 'mirror', 'wiper', 'sunroof', 'parking', 'comfort', 'light', 'lamp', 'body')):
        return 'Comfort / Body'
    if any(k in n for k in ('radio', 'navigation', 'display', 'media', 'head unit', 'audio', 'camera', 'infotainment', 'mmi', 'hmi', 'screen')):
        return 'Infotainment'
    if any(k in n for k in ('gateway', 'central elect', 'bcm', 'body control', 'network', 'can bus', 'diag')):
        return 'Network / Gateway'
    return 'Other'


def _write_vag_html_report(
    path: str,
    reports: List[EcuReport],
    *,
    title: str,
    subtitle: str,
    dtc_map: Optional[Dict[str, str]] = None,
    pdx_info: Optional[Dict[str, Any]] = None,
    comm_index: Optional[Dict[str, Any]] = None,
    vin: str = '',
    not_contacted: Optional[List[Dict[str, Any]]] = None,
    env_data_index: Optional[Dict[str, Any]] = None,
) -> None:
    def esc(s: str) -> str:
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    def _status_bits_html(status_byte: Optional[int]) -> str:
        if not isinstance(status_byte, int):
            return ''
        bits = [
            ('TF', 'Bit 0: TestFailed', bool(status_byte & 0x01)),
            ('OC', 'Bit 1: TestFailedThisOpCycle', bool(status_byte & 0x02)),
            ('PD', 'Bit 2: PendingDTC', bool(status_byte & 0x04)),
            ('CF', 'Bit 3: ConfirmedDTC', bool(status_byte & 0x08)),
            ('NC', 'Bit 4: TestNotCompletedSinceClear', bool(status_byte & 0x10)),
            ('FC', 'Bit 5: TestFailedSinceClear', bool(status_byte & 0x20)),
            ('NO', 'Bit 6: TestNotCompletedThisOpCycle', bool(status_byte & 0x40)),
            ('WI', 'Bit 7: WarningIndicatorRequested', bool(status_byte & 0x80)),
        ]
        parts = []
        for label, tip, is_set in bits:
            cls = 'sbit sbit-on' if is_set else 'sbit sbit-off'
            parts.append(f"<span class='{cls}' title='{esc(tip)}'>{esc(label)}</span>")
        return f"<div class='status-bits'>{''.join(parts)}</div><div class='mono muted' style='font-size:10px;'>0x{status_byte:02X}</div>"

    def _ident_grid_html(ident: Dict[str, Any]) -> str:
        if not isinstance(ident, dict) or not ident:
            return ''
        LABELS = [
            ('spare_part_number', 'Spare Part No.'),
            ('hw_version', 'HW Version'),
            ('sw_version', 'SW Version'),
            ('supplier', 'Supplier'),
            ('serial_number', 'Serial No.'),
            ('reprogram_date', 'Reprogram Date'),
        ]
        cells = []
        for key, label in LABELS:
            val = str(ident.get(key) or '').strip()
            if val:
                cells.append(
                    f"<div class='ident-cell'>"
                    f"<div class='ident-label'>{esc(label)}</div>"
                    f"<div class='ident-value mono'>{esc(val)}</div>"
                    f"</div>"
                )
        if not cells:
            return ''
        return f"<div class='ident-grid'>{''.join(cells)}</div>"

    def _snapshot_html(snaps: Any) -> str:
        if not isinstance(snaps, list) or not snaps:
            return ''
        parts = []
        for s0 in snaps[:3]:
            if not isinstance(s0, dict):
                continue
            rn = s0.get('record_number')
            items = s0.get('items') or []
            rows_ff = []
            for it in items[:60]:
                if not isinstance(it, dict):
                    continue
                nm = str(it.get('name') or it.get('did') or '').strip()
                val = it.get('uint_be')
                asc = str(it.get('ascii') or '').strip()
                rawh = str(it.get('raw_hex') or '').strip()
                pretty = str(val) if isinstance(val, int) else (asc or rawh)
                if nm and pretty:
                    rows_ff.append(f"<tr><td class='ff-label'>{esc(nm)}</td><td class='mono ff-val'>{esc(pretty)}</td></tr>")
            if rows_ff:
                hdr = f"Snapshot #{esc(str(rn))}" if rn is not None else 'Snapshot'
                parts.append(
                    f"<div class='ff-section'>"
                    f"<div class='ff-title'>{hdr}</div>"
                    f"<table class='ff-table'><tbody>{''.join(rows_ff)}</tbody></table>"
                    f"</div>"
                )
            else:
                tail = str(s0.get('tail_hex') or s0.get('raw_hex') or '').strip()
                if tail:
                    parts.append(f"<div class='ff-section muted mono' style='font-size:11px;'>{esc(tail)}</div>")
        return ''.join(parts)

    def _extdata_html(ext: Any) -> str:
        if not isinstance(ext, list) or not ext:
            return ''
        e0 = ext[0] if isinstance(ext[0], dict) else None
        if not e0:
            return ''
        dec = e0.get('decoded') or {}
        parts = []
        for key, label in (('occurrence_counter', 'Occurrences'), ('aging_counter', 'Aging'), ('aged_counter', 'Aged')):
            v = dec.get(key)
            if isinstance(v, int):
                parts.append(f"<div class='kpi-mini'><div class='kpi-mini-val'>{v}</div><div class='kpi-mini-lbl'>{esc(label)}</div></div>")
        return f"<div class='kpi-mini-row'>{''.join(parts)}</div>" if parts else ''

    css = """
:root {
    color-scheme: dark;
    --panel: #0d0f14;
    --stroke: #232631;
    --stroke2: #2e3443;
    --text: #f3f5f7;
    --muted: #aab0bb;
    --accent: #f6c000;
    --accent2: #ffdd55;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    padding: 20px;
    background:
        radial-gradient(900px 600px at 8% 0%, rgba(246,192,0,0.10), transparent 55%),
        radial-gradient(800px 500px at 100% 20%, rgba(246,192,0,0.06), transparent 55%),
        linear-gradient(180deg, #07080b, #05060a 60%, #07080b);
    color: var(--text);
    min-height: 100vh;
}
.wrap { max-width: 1200px; margin: 0 auto; }
.topbar {
    height: 4px;
    background: linear-gradient(90deg, var(--accent), rgba(246,192,0,0.0));
    margin-bottom: 18px;
    filter: drop-shadow(0 0 8px rgba(246,192,0,0.4));
}
.muted { color: var(--muted); }
.card {
    border: 1px solid var(--stroke);
    border-radius: 12px;
    padding: 16px 18px;
    background: linear-gradient(160deg, rgba(15,17,23,0.98), rgba(10,12,16,0.98));
    box-shadow: 0 0 0 1px rgba(246,192,0,0.05), 0 12px 32px rgba(0,0,0,0.50);
    position: relative;
    overflow: hidden;
    margin-bottom: 12px;
}
.card::before {
    content: '';
    position: absolute;
    top: -60px; right: -100px;
    width: 220px; height: 220px;
    background: radial-gradient(circle, rgba(246,192,0,0.12), transparent 60%);
    pointer-events: none;
}
/* Vehicle header */
.vin-display {
    font-size: 28px;
    font-weight: 900;
    letter-spacing: 3px;
    font-family: ui-monospace, monospace;
    color: var(--accent2);
    margin-bottom: 6px;
}
.vin-empty {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 1px;
    color: var(--muted);
    margin-bottom: 6px;
}
.scan-meta { color: var(--muted); font-size: 13px; margin-top: 4px; }
.hdr-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px;
    border: 1px solid rgba(246,192,0,0.35); border-radius: 999px;
    color: var(--accent2); font-size: 12px; letter-spacing: 0.5px;
    text-transform: uppercase; background: rgba(246,192,0,0.06);
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px rgba(246,192,0,0.20); }
.grid-hdr { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: start; }
/* KPI row */
.kpi-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 4px; }
.kpi-box {
    flex: 1; min-width: 100px;
    border: 1px solid var(--stroke); border-radius: 10px;
    padding: 10px 14px;
    background: rgba(255,255,255,0.02);
    text-align: center;
}
.kpi-val { font-size: 26px; font-weight: 900; color: var(--text); line-height: 1; }
.kpi-lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; margin-top: 4px; }
.kpi-active .kpi-val { color: var(--accent2); }
.kpi-sporadic .kpi-val { color: #ff9933; }
/* Tables */
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid var(--stroke); padding: 8px 10px; vertical-align: top; }
th { text-align: left; font-size: 11px; color: #c0c5d0; letter-spacing: 0.3px; text-transform: uppercase; }
.num { text-align: right; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
a { color: var(--accent2); text-decoration: none; }
a:hover { text-decoration: underline; }
/* ECU detail sections */
details { border: 1px solid var(--stroke); border-radius: 12px; overflow: hidden; background: var(--panel); margin-bottom: 8px; }
summary { cursor: pointer; padding: 12px 14px; list-style: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
summary::-webkit-details-marker { display: none; }
.box { padding: 10px 14px 14px; }
.pill {
    display: inline-block; padding: 2px 10px;
    border: 1px solid var(--stroke2); border-radius: 999px;
    font-size: 11px; color: #c0c5d0; background: rgba(255,255,255,0.02);
}
.pill-active { border-color: rgba(246,192,0,0.40); color: var(--accent2); background: rgba(246,192,0,0.07); }
.pill-sporadic { border-color: rgba(255,153,51,0.40); color: #ff9933; background: rgba(255,153,51,0.06); }
/* DTC state labels */
.state { font-weight: 700; letter-spacing: 0.4px; font-size: 11px; text-transform: uppercase; }
.state-active { color: var(--accent2); }
.state-passive { color: var(--muted); }
.state-sporadic { color: #ff9933; }
/* Status byte bits */
.status-bits { display: flex; gap: 3px; flex-wrap: nowrap; margin-bottom: 2px; }
.sbit {
    display: inline-block; width: 22px; height: 16px; line-height: 16px;
    text-align: center; font-size: 9px; font-weight: 700; border-radius: 3px;
    cursor: default; user-select: none;
}
.sbit-on { background: rgba(246,192,0,0.25); color: var(--accent2); border: 1px solid rgba(246,192,0,0.5); }
.sbit-off { background: rgba(255,255,255,0.04); color: #555; border: 1px solid #333; }
/* ECU identification grid */
.ident-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 8px;
    margin: 10px 0 14px;
    padding: 10px 12px;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--stroke);
    border-radius: 8px;
}
.ident-cell {}
.ident-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 2px; }
.ident-value { font-size: 13px; color: var(--text); }
/* Freeze frame table */
.ff-section { margin: 8px 0; }
.ff-title { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 4px; }
.ff-table { width: auto; min-width: 300px; font-size: 12px; }
.ff-table td { border-bottom: 1px solid rgba(255,255,255,0.05); padding: 3px 8px; }
.ff-label { color: var(--muted); min-width: 160px; }
.ff-val { color: var(--text); }
/* Extended data KPI mini */
.kpi-mini-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 6px 0; }
.kpi-mini { text-align: center; padding: 4px 10px; background: rgba(255,255,255,0.03); border: 1px solid var(--stroke); border-radius: 6px; }
.kpi-mini-val { font-size: 18px; font-weight: 900; color: var(--text); }
.kpi-mini-lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; }
/* Filter bar */
.filter-bar { display: flex; gap: 6px; margin: 10px 0; flex-wrap: wrap; }
.filter-btn {
    padding: 4px 12px; border: 1px solid var(--stroke2); border-radius: 999px;
    background: rgba(255,255,255,0.02); color: var(--muted); font-size: 11px;
    cursor: pointer; letter-spacing: 0.3px; text-transform: uppercase; transition: all 0.12s;
}
.filter-btn:hover { border-color: var(--accent); color: var(--accent2); }
.filter-btn.active { border-color: var(--accent); color: var(--accent2); background: rgba(246,192,0,0.10); }
/* DTC table */
.dtc td:last-child { max-width: 320px; }
.desc { color: var(--muted); font-size: 12px; }
.note { font-size: 11px; color: var(--muted); margin-top: 5px; }
.ctx-detail { margin-top: 6px; }
/* Group header */
.group-hdr {
    font-size: 11px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase;
    color: var(--muted); padding: 8px 0 4px;
    border-bottom: 1px solid var(--stroke); margin: 14px 0 6px;
}
/* Not-contacted */
.not-contacted-list { columns: 2; font-size: 12px; color: var(--muted); padding: 4px 0; }
.not-contacted-list li { margin-bottom: 3px; list-style: none; padding-left: 12px; position: relative; }
.not-contacted-list li::before { content: '—'; position: absolute; left: 0; }
/* Section header inside summary */
.ecu-name { font-weight: 700; font-size: 14px; }
/* Print */
@media print {
    body { background: #fff; color: #000; padding: 0; }
    .topbar { display: none; }
    .card { border: 1px solid #ccc; box-shadow: none; background: #fff; }
    .card::before { display: none; }
    details { border: 1px solid #ddd; }
    details[open] > summary { background: #f5f5f5; }
    .sbit-on { background: #ffe; border-color: #cc0; color: #550; }
    .sbit-off { background: #f5f5f5; border-color: #ccc; color: #999; }
    .filter-bar, .filter-btn { display: none; }
    a { color: #000; text-decoration: none; }
    .state-active { color: #c70; }
    .state-passive { color: #666; }
    .state-sporadic { color: #c60; }
}
"""

    _SENTINEL_CODES = {'UDS', 'UDS19', 'OBD', 'KWP'}
    pi = pdx_info if isinstance(pdx_info, dict) else {}
    pdx_name = str(pi.get('pdx_filename') or '').strip()
    scan_src = str(pi.get('scan_source') or '').strip()
    comm_ecu_count = pi.get('comm_ecu_count')
    scan_vin = str(vin or '').strip()
    gen_ts = time.strftime('%Y-%m-%d %H:%M:%S')

    # Global DTC counts
    total_active = sum(
        1 for e in reports for d in e.dtcs
        if (getattr(d, 'dtc_class', '') or '').upper() == 'ACTIVE'
        and not (d.code in _SENTINEL_CODES and d.uds_dtc is None)
    )
    total_passive = sum(
        1 for e in reports for d in e.dtcs
        if (getattr(d, 'dtc_class', '') or '').upper() == 'PASSIVE'
        and not (d.code in _SENTINEL_CODES and d.uds_dtc is None)
    )
    total_sporadic = sum(
        1 for e in reports for d in e.dtcs
        if (getattr(d, 'dtc_class', '') or '').upper() == 'SPORADIC'
        and not (d.code in _SENTINEL_CODES and d.uds_dtc is None)
    )
    nc_count = len(not_contacted) if isinstance(not_contacted, list) else 0

    # --- Vehicle header ---
    vin_html = (
        f"<div class='vin-display'>{esc(scan_vin)}</div>"
        if scan_vin else
        "<div class='vin-empty'>VIN: Not read</div>"
    )
    pdx_pills = ''
    if pdx_name:
        pdx_pills += f"<span class='pill pill-active' style='margin-right:6px;'>PDX: {esc(pdx_name)}</span>"
    if scan_src:
        pdx_pills += f"<span class='pill' style='margin-right:6px;'>Source: {esc(scan_src)}</span>"
    if isinstance(comm_ecu_count, int):
        pdx_pills += f"<span class='pill'>{comm_ecu_count} ECUs in PDX</span>"
    pdx_pills_html = f"<div style='margin-top:8px;'>{pdx_pills}</div>" if pdx_pills else ''

    # --- Summary rows ---
    sum_rows: List[str] = []
    for idx, ecu in enumerate(reports, 1):
        real_dtcs = [d for d in ecu.dtcs if not (d.code in _SENTINEL_CODES and d.uds_dtc is None)]
        a = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'ACTIVE')
        p = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'PASSIVE')
        s = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'SPORADIC')
        ident_ecu = ecu.ident if isinstance(getattr(ecu, 'ident', None), dict) else {}
        hw = esc(str(ident_ecu.get('hw_version') or '—'))
        sw = esc(str(ident_ecu.get('sw_version') or '—'))
        sum_rows.append(
            f"<tr>"
            f"<td><a href='#ecu-{idx}'>{esc(ecu.name)}</a></td>"
            f"<td class='mono'>{hw}</td><td class='mono'>{sw}</td>"
            f"<td class='num'>{a if a else '—'}</td>"
            f"<td class='num' style='color:var(--muted)'>{p if p else '—'}</td>"
            f"<td class='num' style='color:#ff9933'>{s if s else '—'}</td>"
            f"<td class='num'>{len(real_dtcs)}</td>"
            f"</tr>"
        )

    # --- ECU detail sections ---
    sections: List[str] = []
    for idx, ecu in enumerate(reports, 1):
        ecu_id = f"ecu-{idx}"
        real_dtcs = [d for d in ecu.dtcs if not (d.code in _SENTINEL_CODES and d.uds_dtc is None)]
        sentinel_dtcs = [d for d in ecu.dtcs if d.code in _SENTINEL_CODES and d.uds_dtc is None]
        active = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'ACTIVE')
        passive = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'PASSIVE')
        sporadic = sum(1 for d in real_dtcs if (getattr(d, 'dtc_class', '') or '').upper() == 'SPORADIC')

        ident_ecu = ecu.ident if isinstance(getattr(ecu, 'ident', None), dict) else {}
        ident_html = _ident_grid_html(ident_ecu)

        dtc_rows_html: List[str] = []
        for d in real_dtcs:
            dtc_cls = (getattr(d, 'dtc_class', '') or '').upper()
            if dtc_cls not in ('ACTIVE', 'PASSIVE', 'SPORADIC'):
                dtc_cls = 'ACTIVE' if d.active else 'PASSIVE'
            state_cls = f"state-{dtc_cls.lower()}"
            desc_txt = (d.description or '').strip() if isinstance(getattr(d, 'description', None), str) else ''
            if not desc_txt:
                desc_txt = _dtc_description(d.code, dtc_map)

            extra = d.extra if isinstance(getattr(d, 'extra', None), dict) else {}
            km = extra.get('odometer_km')
            km_txt = f"{km} km" if isinstance(km, int) else ''
            ts_iso = extra.get('timestamp_iso')
            ts_txt = extra.get('timestamp_text')
            scan_ts = extra.get('scan_timestamp_iso')
            ts_display = ''
            if isinstance(ts_iso, str) and ts_iso.strip():
                ts_display = ts_iso.strip()
            elif isinstance(ts_txt, str) and ts_txt.strip():
                ts_display = ts_txt.strip()
            elif isinstance(scan_ts, str) and scan_ts.strip():
                ts_display = f"{scan_ts.strip()} (scan)"

            ctx_parts: List[str] = []
            if km_txt:
                ctx_parts.append(f"<span class='pill pill-active'>KM@fault: {esc(km_txt)}</span>")
            if ts_display and not (scan_ts and ts_display.endswith('(scan)')):
                ctx_parts.append(f"<span class='pill'>ts: {esc(ts_display)}</span>")

            ff_html = ''
            try:
                snaps = extra.get('snapshots')
                ff_html = _snapshot_html(snaps)
                if not ff_html and extra.get('snapshot_nrc_raw'):
                    ff_html = f"<div class='note mono'>Snap NRC: {esc(str(extra['snapshot_nrc_raw']))}</div>"
                elif not ff_html and extra.get('snapshot_raw'):
                    ff_html = f"<div class='note mono'>{esc(str(extra['snapshot_raw']))}</div>"
            except Exception:
                pass

            ext_html = ''
            try:
                ext_html = _extdata_html(extra.get('extended_data'))
                if not ext_html and extra.get('extdata_nrc_raw'):
                    ext_html = f"<div class='note muted'>ExtData NRC: {esc(str(extra['extdata_nrc_raw']))}</div>"
            except Exception:
                pass

            ctx_pills = f"<div style='margin-top:4px;'>{''.join(ctx_parts)}</div>" if ctx_parts else ''
            detail_body = ''
            if ff_html or ext_html:
                detail_body = (
                    "<details class='ctx-detail'><summary class='muted' style='font-size:11px;'>Context data</summary>"
                    f"<div class='box'>{ff_html}{ext_html}</div></details>"
                )

            desc_cell = f"<span class='desc'>{esc(desc_txt)}</span>{ctx_pills}{detail_body}"

            dtc_rows_html.append(
                f"<tr data-dtc-class='{esc(dtc_cls)}'>"
                f"<td class='mono' style='white-space:nowrap'>{esc(d.code)}</td>"
                f"<td><span class='state {state_cls}'>{esc(dtc_cls)}</span></td>"
                f"<td>{_status_bits_html(d.status_byte)}</td>"
                f"<td class='mono' style='font-size:11px;white-space:nowrap'>{esc(ts_display)}</td>"
                f"<td class='mono'>{esc(km_txt)}</td>"
                f"<td class='mono' style='font-size:10px;color:var(--muted)'>{esc(d.raw)}</td>"
                f"<td>{desc_cell}</td>"
                "</tr>"
            )

        if not dtc_rows_html:
            dtc_rows_html.append("<tr><td colspan='7' class='muted' style='padding:12px;'>No DTCs reported.</td></tr>")

        # OBD blocks (Mode 0A and Mode 06) — preserved from original
        obd_blocks: List[str] = []
        try:
            obd = ecu.obd if isinstance(getattr(ecu, 'obd', None), dict) else {}
            m0a = obd.get('mode0A_dtcs')
            if isinstance(m0a, list):
                rows0a = []
                for code in m0a[:200]:
                    c0 = str(code or '').strip()
                    if c0:
                        d0 = _dtc_description(c0, dtc_map)
                        rows0a.append(f"<tr><td class='mono'>{esc(c0)}</td><td class='desc'>{esc(d0)}</td></tr>")
                if not rows0a:
                    rows0a.append("<tr><td colspan='2' class='muted'>No Mode 0A DTCs.</td></tr>")
                obd_blocks.append(
                    "<details><summary class='muted'>OBD Mode 0A (Permanent DTCs)</summary>"
                    "<div class='box'><table><thead><tr><th>DTC</th><th>Description</th></tr></thead>"
                    f"<tbody>{''.join(rows0a)}</tbody></table></div></details>"
                )
            m06 = obd.get('mode06')
            if isinstance(m06, dict):
                note06 = str(m06.get('note') or '').strip()
                raw_hex06 = str(m06.get('raw_hex') or '').strip()
                tests06 = m06.get('tests')
                sum06 = m06.get('summary') if isinstance(m06.get('summary'), dict) else None
                tbl06 = ''
                if isinstance(tests06, list) and tests06:
                    rr = []
                    for t in tests06[:500]:
                        if not isinstance(t, dict):
                            continue
                        pf = t.get('pass')
                        pf_txt = 'PASS' if pf is True else ('FAIL' if pf is False else 'N/A')
                        rr.append(f"<tr><td class='mono'>0x{int(t.get('tid') or 0):02X}</td><td class='mono'>0x{int(t.get('cid') or 0):02X}</td><td class='mono'>{esc(str(t.get('value')))}</td><td class='mono'>{esc(str(t.get('min')))}</td><td class='mono'>{esc(str(t.get('max')))}</td><td class='mono'>{esc(pf_txt)}</td></tr>")
                    if rr:
                        tbl06 = f"<table><thead><tr><th>TID</th><th>CID</th><th>Value</th><th>Min</th><th>Max</th><th>Result</th></tr></thead><tbody>{''.join(rr)}</tbody></table>"
                sp06 = ''
                if sum06:
                    sp06 = f"<div class='note'><span class='pill pill-active'>PASS {int(sum06.get('pass') or 0)}</span> <span class='pill'>FAIL {int(sum06.get('fail') or 0)}</span> <span class='pill'>N/A {int(sum06.get('unknown') or 0)}</span></div>"
                if note06 or raw_hex06 or tbl06:
                    obd_blocks.append(
                        "<details><summary class='muted'>OBD Mode 06 (On-board Monitor Tests)</summary>"
                        f"<div class='box'>{sp06}"
                        + (f"<div class='muted'>{esc(note06)}</div>" if note06 else '')
                        + tbl06
                        + (f"<details><summary class='muted'>Raw frames</summary><div class='box mono muted' style='font-size:11px;'>{esc(raw_hex06)}</div></details>" if raw_hex06 else '')
                        + "</div></details>"
                    )
        except Exception:
            pass

        sum_pills = (
            f"<span class='pill pill-active'>active {active}</span>"
            f"<span class='pill'>passive {passive}</span>"
            f"<span class='pill pill-sporadic'>sporadic {sporadic}</span>"
            f"<span class='pill'>{len(real_dtcs)} total</span>"
        )
        sentinel_html = ''
        if sentinel_dtcs:
            hints = [esc(str(s.status_desc or s.raw or '?').strip()) for s in sentinel_dtcs if s.status_desc or s.raw]
            if hints:
                sentinel_html = (
                    "<details><summary class='muted' style='font-size:11px;'>Diagnostic hints</summary>"
                    "<div class='box mono muted' style='font-size:11px;'>" + "<br>".join(hints[:20]) + "</div></details>"
                )

        sections.append(
            f"<details id='{ecu_id}'>"
            f"<summary><span class='ecu-name'>{esc(ecu.name)}</span>{sum_pills}</summary>"
            f"<div class='box'>"
            f"{ident_html}"
            "<div class='filter-bar'>"
            "<button class='filter-btn active' onclick='filterDtc(this,\"ALL\")'>All</button>"
            "<button class='filter-btn' onclick='filterDtc(this,\"ACTIVE\")'>Active</button>"
            "<button class='filter-btn' onclick='filterDtc(this,\"PASSIVE\")'>Passive</button>"
            "<button class='filter-btn' onclick='filterDtc(this,\"SPORADIC\")'>Sporadic</button>"
            "</div>"
            "<table class='dtc'>"
            "<thead><tr>"
            "<th>DTC</th><th>State</th><th>Status Byte</th><th>Timestamp</th>"
            "<th>KM</th><th>Raw</th><th>Description</th>"
            "</tr></thead>"
            f"<tbody>{''.join(dtc_rows_html)}</tbody>"
            "</table>"
            + ''.join(obd_blocks)
            + sentinel_html
            + "<div class='note muted' style='margin-top:8px;'>Descriptions from active PDX when available.</div>"
            "</div></details>"
        )

    # --- Not-contacted ECUs ---
    nc_html = ''
    if isinstance(not_contacted, list) and not_contacted:
        nc_items = []
        for e in not_contacted[:200]:
            if not isinstance(e, dict):
                continue
            ln = str(e.get('long_name') or e.get('short_name') or '').strip()
            doip = e.get('doip') or {}
            la = doip.get('logical_ecu_address') if isinstance(doip, dict) else None
            addr_txt = f"0x{int(la):04X}" if isinstance(la, int) else ''
            nc_items.append(f"<li>{esc(ln)}{' — ' + addr_txt if addr_txt else ''}</li>")
        nc_html = (
            f"<details style='margin-top:8px;'>"
            f"<summary class='muted'>Not contacted — {len(not_contacted)} ECU(s) from PDX that did not respond</summary>"
            f"<div class='box'><ul class='not-contacted-list'>{''.join(nc_items)}</ul></div>"
            "</details>"
        )

    brand_main = 'EV-Q Onboard Manager'
    brand_sub = 'TRC Project'

    html = f"""<!doctype html>
<html lang='en'>
<head>
    <meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>{esc(brand_main)} — {esc(title)}</title>
    <style>{css}</style>
</head>
<body>
<div class='wrap'>
<div class='topbar'></div>

<div class='card'>
    <div class='grid-hdr'>
        <div>
            <div style='font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:4px;'>{esc(brand_main)} &bull; {esc(brand_sub)}</div>
            {vin_html}
            <div class='scan-meta'>{esc(subtitle)}</div>
            <div class='scan-meta'>Generated: {gen_ts}</div>
            {pdx_pills_html}
        </div>
        <div class='hdr-badge'><span class='dot'></span>Diagnostic Report</div>
    </div>
    <div class='kpi-row'>
        <div class='kpi-box'><div class='kpi-val'>{len(reports)}</div><div class='kpi-lbl'>ECUs Contacted</div></div>
        <div class='kpi-box kpi-active'><div class='kpi-val'>{total_active}</div><div class='kpi-lbl'>Active DTCs</div></div>
        <div class='kpi-box'><div class='kpi-val'>{total_passive}</div><div class='kpi-lbl'>Passive DTCs</div></div>
        <div class='kpi-box kpi-sporadic'><div class='kpi-val'>{total_sporadic}</div><div class='kpi-lbl'>Sporadic DTCs</div></div>
        <div class='kpi-box'><div class='kpi-val'>{nc_count}</div><div class='kpi-lbl'>Not Contacted</div></div>
    </div>
</div>

<div class='card'>
    <div style='font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;'>ECU Summary</div>
    <table>
        <thead><tr><th>ECU</th><th>HW Version</th><th>SW Version</th><th class='num' style='color:var(--accent2)'>Active</th><th class='num'>Passive</th><th class='num' style='color:#ff9933'>Sporadic</th><th class='num'>Total</th></tr></thead>
        <tbody>
            {''.join(sum_rows) if sum_rows else "<tr><td colspan='7' class='muted'>No ECUs scanned.</td></tr>"}
        </tbody>
    </table>
</div>

<div>
    {''.join(sections)}
    {nc_html}
</div>

</div>
<script>
function filterDtc(btn, cls) {{
    var bar = btn.parentElement;
    bar.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    var tbody = bar.parentElement.querySelector('table.dtc tbody');
    if (!tbody) return;
    tbody.querySelectorAll('tr[data-dtc-class]').forEach(function(r) {{
        r.style.display = (cls === 'ALL' || r.getAttribute('data-dtc-class') === cls) ? '' : 'none';
    }});
}}
</script>
</body>
</html>"""

    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


def _write_diagra_xml_report(path: str, reports: List[EcuReport], *, title: str, subtitle: str, dtc_map: Optional[Dict[str, str]] = None) -> None:
    """Write a simple XML export intended for external tools (Diagra-like consumption).

    This is not iDEX-compatible; it's a pragmatic, self-contained export that includes:
    - ECU list
    - DTCs (with PDX description when available)
    - snapshot/ext-data context
    - OBD Mode 0A DTCs
    - OBD Mode 06 decoded tests
    """
    import xml.etree.ElementTree as XET

    def _desc(code: str) -> str:
        return _dtc_description(code, dtc_map)

    root = XET.Element('diagnosticReport')
    root.set('generated', time.strftime('%Y-%m-%dT%H:%M:%S'))

    meta = XET.SubElement(root, 'meta')
    XET.SubElement(meta, 'title').text = str(title)
    XET.SubElement(meta, 'subtitle').text = str(subtitle)

    ecus_el = XET.SubElement(root, 'ecus')

    for ecu in reports:
        ecu_el = XET.SubElement(ecus_el, 'ecu')
        ecu_el.set('name', str(ecu.name))
        ecu_el.set('tx_id', f"0x{int(ecu.tx_id):03X}")
        ecu_el.set('rx_id', f"0x{int(ecu.rx_id):03X}")

        dtcs_el = XET.SubElement(ecu_el, 'dtcs')
        for d in ecu.dtcs:
            d_el = XET.SubElement(dtcs_el, 'dtc')
            d_el.set('code', str(d.code))
            d_el.set('active', '1' if bool(d.active) else '0')
            if d.status_byte is not None:
                d_el.set('status_byte', f"0x{int(d.status_byte):02X}")
            d_el.set('status_desc', str(d.status_desc))
            XET.SubElement(d_el, 'raw').text = str(d.raw)
            # Prefer description already in DtcItem (PDX-enriched), fallback to lookup.
            desc_val = (d.description or '').strip() if isinstance(getattr(d, 'description', None), str) else ''
            if not desc_val:
                desc_val = _desc(str(d.code))
            XET.SubElement(d_el, 'description').text = desc_val

            extra = d.extra if isinstance(getattr(d, 'extra', None), dict) else {}
            if isinstance(extra.get('odometer_km'), int):
                XET.SubElement(d_el, 'odometer_km').text = str(int(extra['odometer_km']))

            # Timestamp: per-DTC fault timestamp or scan timestamp
            for tk in ('timestamp_iso', 'timestamp_text', 'scan_timestamp_iso'):
                tv = extra.get(tk)
                if isinstance(tv, str) and tv.strip():
                    XET.SubElement(d_el, 'timestamp').text = tv.strip()
                    break

            snaps = extra.get('snapshots')
            if isinstance(snaps, list) and snaps:
                snaps_el = XET.SubElement(d_el, 'snapshots')
                for s in snaps[:4]:
                    if not isinstance(s, dict):
                        continue
                    s_el = XET.SubElement(snaps_el, 'snapshot')
                    if 'record_number' in s:
                        s_el.set('record_number', str(s.get('record_number')))
                    items = s.get('items')
                    if isinstance(items, list):
                        for it in items[:200]:
                            if not isinstance(it, dict):
                                continue
                            it_el = XET.SubElement(s_el, 'item')
                            it_el.set('did', str(it.get('did') or ''))
                            it_el.set('name', str(it.get('name') or ''))
                            if isinstance(it.get('uint_be'), int):
                                it_el.set('uint_be', str(int(it.get('uint_be'))))
                            if it.get('ascii'):
                                it_el.set('ascii', str(it.get('ascii')))
                            if it.get('raw_hex'):
                                it_el.set('raw_hex', str(it.get('raw_hex')))

            ext = extra.get('extended_data')
            if isinstance(ext, list) and ext:
                ext_el = XET.SubElement(d_el, 'extendedData')
                for e in ext[:4]:
                    if not isinstance(e, dict):
                        continue
                    e_el = XET.SubElement(ext_el, 'record')
                    if 'record_number' in e:
                        e_el.set('record_number', str(e.get('record_number')))
                    if e.get('data_hex'):
                        XET.SubElement(e_el, 'data_hex').text = str(e.get('data_hex'))
                    # Decoded fields (occurrence, aging, timestamp)
                    dec = e.get('decoded')
                    if isinstance(dec, dict):
                        if isinstance(dec.get('occurrence_counter'), int):
                            XET.SubElement(e_el, 'occurrence_counter').text = str(int(dec['occurrence_counter']))
                        if isinstance(dec.get('aging_counter'), int):
                            XET.SubElement(e_el, 'aging_counter').text = str(int(dec['aging_counter']))
                        if isinstance(dec.get('aged_counter'), int):
                            XET.SubElement(e_el, 'aged_counter').text = str(int(dec['aged_counter']))
                        if isinstance(dec.get('timestamp_iso'), str):
                            XET.SubElement(e_el, 'timestamp').text = dec['timestamp_iso']

    tree = XET.ElementTree(root)
    try:
        XET.indent(tree, space='  ', level=0)  # py3.9+
    except Exception:
        pass
    tree.write(path, encoding='utf-8', xml_declaration=True)


def _write_diagra_csv_report(path: str, reports: List[EcuReport], *, dtc_map: Optional[Dict[str, str]] = None) -> None:
    import csv

    def _desc(code: str) -> str:
        return _dtc_description(code, dtc_map)

    with open(path, 'w', encoding='utf-8', newline='') as fp:
        w = csv.writer(fp)
        w.writerow([
            'ecu_name', 'tx_id', 'rx_id', 'kind',
            'dtc_code', 'dtc_desc', 'active', 'status_byte', 'status_desc', 'raw', 'odometer_km',
            'timestamp',
            'mode', 'tid', 'cid', 'value', 'min', 'max', 'result',
            'context_key', 'context_value',
        ])

        for ecu in reports:
            ecu_name = str(ecu.name)
            tx = f"0x{int(ecu.tx_id):03X}"
            rx = f"0x{int(ecu.rx_id):03X}"

            for d in ecu.dtcs:
                extra = d.extra if isinstance(getattr(d, 'extra', None), dict) else {}
                km = extra.get('odometer_km') if isinstance(extra.get('odometer_km'), int) else ''
                # Timestamp: prefer per-DTC fault timestamp, then scan timestamp
                ts = ''
                for tk in ('timestamp_iso', 'timestamp_text', 'scan_timestamp_iso'):
                    tv = extra.get(tk)
                    if isinstance(tv, str) and tv.strip():
                        ts = tv.strip()
                        break
                # Description: prefer DtcItem.description (enriched from PDX), fallback to lookup
                desc_val = (d.description or '').strip() if isinstance(getattr(d, 'description', None), str) else ''
                if not desc_val:
                    desc_val = _desc(str(d.code))
                w.writerow([
                    ecu_name, tx, rx, 'DTC',
                    str(d.code), desc_val,
                    '1' if bool(d.active) else '0',
                    ('' if d.status_byte is None else f"0x{int(d.status_byte):02X}"),
                    str(d.status_desc), str(d.raw),
                    km,
                    ts,
                    '', '', '', '', '', '', '',
                    '', '',
                ])

                snaps = extra.get('snapshots')
                if isinstance(snaps, list):
                    for s in snaps[:2]:
                        if not isinstance(s, dict):
                            continue
                        items = s.get('items')
                        if not isinstance(items, list):
                            continue
                        for it in items[:200]:
                            if not isinstance(it, dict):
                                continue
                            label = str(it.get('name') or it.get('did') or '').strip()
                            val = it.get('uint_be')
                            if isinstance(val, int):
                                vv = str(val)
                            else:
                                vv = str(it.get('ascii') or it.get('raw_hex') or '')
                            if not label and not vv:
                                continue
                            w.writerow([
                                ecu_name, tx, rx, 'CONTEXT',
                                str(d.code), _desc(str(d.code)),
                                '1' if bool(d.active) else '0',
                                ('' if d.status_byte is None else f"0x{int(d.status_byte):02X}"),
                                str(d.status_desc), str(d.raw),
                                km,
                                'UDS_SNAPSHOT', '', '', '', '', '', '',
                                label, vv,
                            ])

            obd = ecu.obd if isinstance(getattr(ecu, 'obd', None), dict) else {}
            m0a = obd.get('mode0A_dtcs')
            if isinstance(m0a, list):
                for code in m0a[:400]:
                    c = str(code or '').strip()
                    if not c:
                        continue
                    w.writerow([
                        ecu_name, tx, rx, 'OBD',
                        c, _desc(c),
                        '', '', '', '', '',
                        'MODE0A', '', '', '', '', '', '',
                        '', '',
                    ])

            m06 = obd.get('mode06')
            if isinstance(m06, dict):
                tests = m06.get('tests')
                if isinstance(tests, list):
                    for t in tests[:2000]:
                        if not isinstance(t, dict):
                            continue
                        tid = t.get('tid')
                        cid = t.get('cid')
                        pf = t.get('pass')
                        pf_txt = 'PASS' if pf is True else ('FAIL' if pf is False else 'N/A')
                        w.writerow([
                            ecu_name, tx, rx, 'OBD',
                            '', '',
                            '', '', '', '', '',
                            'MODE06',
                            ('' if tid is None else f"0x{int(tid):02X}"),
                            ('' if cid is None else f"0x{int(cid):02X}"),
                            ('' if t.get('value') is None else str(int(t.get('value')))),
                            ('' if t.get('min') is None else str(int(t.get('min')))),
                            ('' if t.get('max') is None else str(int(t.get('max')))),
                            pf_txt,
                            '', '',
                        ])


def _try_get_iface_ipv4(ifname: str) -> Optional[str]:
    """Best-effort: return IPv4 address of a Linux interface (e.g. 'eth0')."""
    if not ifname:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = struct.pack('256s', ifname[:15].encode('utf-8'))
        res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
        ip = socket.inet_ntoa(res[20:24])
        return str(ip)
    except Exception:
        return None


def _try_get_iface_index(ifname: str) -> Optional[int]:
    """Best-effort: return interface index for IPv6 multicast config."""
    if not ifname:
        return None
    try:
        return int(socket.if_nametoindex(str(ifname)))
    except Exception:
        return None


def _discover_doip_gateway_ip_v6(*, iface: Optional[str] = None, timeout_s: float = 1.2) -> Optional[str]:
    """Best-effort DoIP discovery via IPv6 multicast (ISO 13400).

    Many automotive DoIP gateways operate on IPv6 (often link-local) and respond to
    Vehicle Identification Requests sent to a link-local multicast group.

    Returns an IPv6 address string. If the response is link-local and a scope id is
    present, appends a zone id (e.g. "fe80::1%eth0") so TCP connect can succeed.
    """
    msg = struct.pack('!BBHL', 0x02, 0xFD, 0x0001, 0x0000)

    # Default to all-nodes link-local multicast. Some stacks/gateways may use a
    # different group; allow overriding via env var.
    mcast_addr = str(os.getenv('KBSM_DOIP_DISCOVERY_IPV6_MCAST') or 'ff02::1').strip()
    if not mcast_addr:
        mcast_addr = 'ff02::1'

    ifindex = _try_get_iface_index(str(iface)) if iface else None

    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        s.settimeout(max(0.05, float(timeout_s)))
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        except Exception:
            pass

        if ifindex is not None:
            try:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, ifindex)
            except Exception:
                pass

        # Bind to wildcard; on Linux this is typically sufficient for receiving.
        try:
            s.bind(('::', 0))
        except Exception:
            pass

        # For link-local multicast, scope id is required. Provide it when we know iface.
        dest = (mcast_addr, 13400, 0, int(ifindex or 0))
        try:
            s.sendto(msg, dest)
        except Exception:
            return None

        end = time.time() + max(0.05, float(timeout_s))
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            except Exception:
                break
            if not data or len(data) < 8:
                continue
            try:
                ver, inv, ptype, _length = struct.unpack('!BBHL', data[:8])
            except Exception:
                continue
            if ver != 0x02 or inv != 0xFD:
                continue
            if int(ptype) not in (0x0002, 0x0003, 0x0004):
                continue

            src_ip = str(addr[0] or '').strip()
            scope_id = 0
            try:
                # recvfrom() for AF_INET6 returns (ip, port, flowinfo, scopeid)
                scope_id = int(addr[3]) if len(addr) >= 4 else 0
            except Exception:
                scope_id = 0

            if not src_ip:
                continue

            # If link-local, include zone id when we can. This is crucial for TCP connect.
            if src_ip.lower().startswith('fe80:'):
                if iface:
                    return f"{src_ip}%{iface}"
                if scope_id:
                    try:
                        zone = str(socket.if_indextoname(int(scope_id)) or '').strip()
                        if zone:
                            return f"{src_ip}%{zone}"
                    except Exception:
                        pass
            return src_ip
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def discover_doip_gateway_ip(*, iface: Optional[str] = None, timeout_s: float = 1.2) -> Optional[str]:
    """Best-effort DoIP discovery.

    Default order (matches common "OEM tool" setups):
    1) IPv6 multicast (often link-local gateway)
    2) IPv4 broadcast (legacy)

    Set env var KBSM_DOIP_DISCOVERY_PREFER_IPV6=0 to try IPv4 first.
    """

    prefer_ipv6 = str(os.getenv('KBSM_DOIP_DISCOVERY_PREFER_IPV6') or '1').strip().lower() not in (
        '0', 'false', 'no', 'off'
    )

    if prefer_ipv6:
        ip6 = _discover_doip_gateway_ip_v6(iface=iface, timeout_s=timeout_s)
        if ip6:
            return ip6

    # IPv4 broadcast fallback
    msg = struct.pack('!BBHL', 0x02, 0xFD, 0x0001, 0x0000)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(max(0.05, float(timeout_s)))

        bind_ip = None
        if iface:
            bind_ip = _try_get_iface_ipv4(str(iface))
        try:
            s.bind(((bind_ip or ''), 0))
        except Exception:
            try:
                s.bind(('', 0))
            except Exception:
                pass

        try:
            s.sendto(msg, ('255.255.255.255', 13400))
        except Exception:
            return None

        end = time.time() + max(0.05, float(timeout_s))
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            except Exception:
                break
            if not data or len(data) < 8:
                continue
            try:
                ver, inv, ptype, _length = struct.unpack('!BBHL', data[:8])
            except Exception:
                continue
            if ver != 0x02 or inv != 0xFD:
                continue
            if int(ptype) not in (0x0002, 0x0003, 0x0004):
                continue
            src_ip = str(addr[0] or '').strip()
            if src_ip:
                return src_ip

        # If IPv4 failed and we didn't try IPv6 before, try it now.
        if not prefer_ipv6:
            ip6 = _discover_doip_gateway_ip_v6(iface=iface, timeout_s=timeout_s)
            if ip6:
                return ip6

        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


class DoIPGatewayScanner:
    """Best-effort DoIP scan through gateway.

    This is intentionally minimal and designed to work with common VAG gateway setups:
    - TCP/13400
    - Routing Activation with tester logical address
    - ECU discovery by probing logical target addresses with TesterPresent (0x3E)
    - DTC read via UDS ReadDTCInformation (0x19 0x02 0xFF)
    """

    def __init__(self, gateway_ip: str, *, emit_log=None, tester_logical_address: int = 0x0E00):
        self.gateway_ip = str(gateway_ip).strip()
        self.port = 13400
        self.emit_log = emit_log
        self.tester_addr = int(tester_logical_address) & 0xFFFF
        self.sock: Optional[socket.socket] = None

        # Enable verbose DoIP debug logging when troubleshooting mirror mode.
        # Off by default to avoid spamming logs during normal operation.
        try:
            self.debug_doip = str(os.getenv('DOIP_DEBUG', '') or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')
        except Exception:
            self.debug_doip = False

        self.did_index: Dict[int, Dict[str, Any]] = {}

        self.log_dir = _get_log_dir_default()
        os.makedirs(self.log_dir, exist_ok=True)

    def log(self, msg: str) -> None:
        try:
            print(msg)
        except Exception:
            pass
        if self.emit_log:
            try:
                self.emit_log(msg)
            except Exception:
                pass

    def close(self) -> None:
        s = self.sock
        self.sock = None
        if s:
            try:
                s.close()
            except Exception:
                pass

    def run_scan_report(self, *, ecu_addresses: Optional[List[int]] = None) -> Tuple[str, List[int]]:
        """Return (report_filename, discovered_ecu_addresses)."""
        if not self.gateway_ip:
            raise ValueError('missing gateway_ip')

        self.log(f"=== VAG DoIP Scan (Gateway {self.gateway_ip}) ===")
        self._connect_with_recovery()
        self._routing_activation()

        # Optional: enable gateway Mirror mode (Ethernet) before discovery.
        # Many vehicles expose only the gateway over DoIP; other ECUs become reachable
        # only after enabling mirroring/tunneling on the gateway.
        #
        # Env controls:
        #   DOIP_MIRROR_ENABLE=1            enable mirror before scan (default: 0)
        #   DOIP_MIRROR_DRY_RUN=1           log what would be written but don't write (default: 0)
        #   DOIP_MIRROR_TARGET_ADDR=0x0E80  gateway ECU logical address (optional)
        #   DOIP_MIRROR_DEST_IP=...         destination IP for mirrored stream (optional)
        #   DOIP_MIRROR_DEST_PORT=...       destination port for mirrored stream (optional)
        #   DOIP_MIRROR_CAN=1,2,3           CAN channels to mirror (optional)
        #   DOIP_MIRROR_LIN=1,2             LIN channels to mirror (optional)
        #   DOIP_MIRROR_FR=A,B              FlexRay channels to mirror (optional)
        try:
            if str(os.getenv('DOIP_MIRROR_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}:
                self._maybe_enable_gateway_mirror()
        except Exception:
            pass

        # Some gateways reset/close the TCP socket after enabling mirror mode.
        # Reconnect automatically so the scan can continue and a report is still generated.
        try:
            if self.sock is None:
                raise RuntimeError('no socket')
            self.sock.send(b'')
        except Exception:
            try:
                self._close_sock()
            except Exception:
                pass
            self._connect()
            self._routing_activation()

        # Prefer protocol/addressing defined by the active PDX when available.
        # This makes the scan faster, more accurate, and avoids probing unknown LAs.
        comm_index = _load_active_pdx_comm_index()
        pdx_doip_addrs = self._pdx_doip_ecu_addresses(comm_index)
        if ecu_addresses is None and pdx_doip_addrs:
            self.log(f"DoIP: Using PDX ECU address list ({len(pdx_doip_addrs)})")
            discovered = self._discover_ecus(pdx_doip_addrs)
            scan_source = 'PDX comm index'
        else:
            discovered = self._discover_ecus(ecu_addresses)
            scan_source = 'User whitelist' if ecu_addresses else 'Heuristic range'
        if not discovered:
            raise RuntimeError('No ECUs discovered via DoIP (check gateway IP / ignition / routing activation).')

        # Load DID and env_data indexes once so we can decode per-DTC context.
        self.did_index = _load_active_pdx_did_index()
        self.log(f"Active PDX DID index: {len(self.did_index)} entries")
        env_data_index = _load_active_pdx_env_data_index()
        self.log(f"Active PDX ENV-DATA index: {len(env_data_index.get('env_data') or {})} records")

        # Map ECU logical address -> PDX ECU metadata (if available)
        pdx_meta = self._pdx_doip_meta_by_addr(comm_index)

        # Read VIN from gateway (first discovered ECU, typically the gateway)
        scan_vin = ''
        for ta_vin in discovered[:3]:
            try:
                resp = self._uds_transact(int(ta_vin), bytes([0x22, 0xF1, 0x90]), timeout_s=1.0)
                if resp and len(resp) >= 3 and resp[0] == 0x62 and resp[1] == 0xF1 and resp[2] == 0x90:
                    raw_vin = resp[3:]
                    txt = ''.join(chr(b) if 32 <= b < 127 else '' for b in raw_vin).strip()
                    if len(txt) >= 10:
                        scan_vin = txt[:17]
                        self.log(f"VIN: {scan_vin} (from 0x{ta_vin:04X})")
                        break
            except Exception:
                pass

        # Collect not-contacted ECUs from PDX comm_index
        discovered_set = {int(a) & 0xFFFF for a in discovered}
        not_contacted: List[Dict[str, Any]] = []
        if isinstance(comm_index, dict) and isinstance(comm_index.get('ecus'), list):
            for e in comm_index['ecus']:
                if not isinstance(e, dict):
                    continue
                doip = e.get('doip') or {}
                la = doip.get('logical_ecu_address') if isinstance(doip, dict) else None
                if isinstance(la, int) and (int(la) & 0xFFFF) not in discovered_set:
                    not_contacted.append(e)

        reports: List[EcuReport] = []
        for ta in discovered:
            name = f"ECU LA 0x{ta:04X}"
            meta = pdx_meta.get(int(ta) & 0xFFFF) if isinstance(pdx_meta, dict) else None
            if isinstance(meta, dict):
                sn = str(meta.get('short_name') or '').strip()
                ln = str(meta.get('long_name') or '').strip()
                if sn and ln and ln.lower() != sn.lower():
                    name = f"{name} ({sn} — {ln})"
                elif sn:
                    name = f"{name} ({sn})"
                elif ln:
                    name = f"{name} ({ln})"
            ident = self._read_ecu_full_ident(ta)
            dtcs = self._read_dtcs_best_effort(ta)
            active = sum(1 for d in dtcs if d.active)
            passive = sum(1 for d in dtcs if (not d.active))
            self.log(f"ECU 0x{ta:04X}: active={active} passive={passive} total={len(dtcs)}")
            reports.append(EcuReport(
                tx_id=int(self.tester_addr), rx_id=int(ta), name=name,
                dtcs=dtcs, ident=ident, contacted=True,
            ))

        dtc_map = _load_active_pdx_dtc_map()
        dtc_by_file = _load_active_pdx_dtc_by_file()
        dtc_ecu_map = _load_active_pdx_dtc_ecu_map()
        trouble_to_display = _load_active_pdx_trouble_to_display()
        trouble_to_display_by_file = _load_active_pdx_trouble_to_display_by_file()
        self.log(
            f"Active PDX DTC index: {len(dtc_map)} entries | ecu_map: {len(dtc_ecu_map)} | "
            f"by_file: {len(dtc_by_file)} | trouble->display: {len(trouble_to_display)}"
        )

        # Populate DtcItem.description from PDX map at scan time (frozen→rebuild).
        scan_iso = time.strftime('%Y-%m-%dT%H:%M:%S')
        if dtc_map:
            enriched_reports: List[EcuReport] = []
            for ecu in reports:
                ecu_la = int(ecu.rx_id) & 0xFFFF
                ecu_meta = pdx_meta.get(ecu_la) if isinstance(pdx_meta, dict) else None
                ecu_source_odx = str(ecu_meta.get('source_odx') or '') if isinstance(ecu_meta, dict) else ''
                new_dtcs: List[DtcItem] = []
                for d in ecu.dtcs:
                    # Resolve OEM display code from raw UDS DTC integer first.
                    # SAE-J2012 conversion does NOT match VW/VAG OEM codes.
                    resolved_code = ''
                    if isinstance(d.uds_dtc, int) and d.uds_dtc > 0:
                        resolved_code = _resolve_display_code_for_ecu(
                            d.uds_dtc,
                            ecu_source_odx or None,
                            trouble_to_display_by_file,
                            trouble_to_display,
                        )
                    code_for_lookup = resolved_code or d.code
                    if ecu_source_odx:
                        desc = _dtc_description_for_ecu(
                            code_for_lookup, dtc_map, dtc_by_file, ecu_source_odx, dtc_ecu_map,
                        ) if code_for_lookup else ''
                    else:
                        desc = _dtc_description(code_for_lookup, dtc_map) if code_for_lookup else ''
                    new_extra = dict(d.extra) if isinstance(getattr(d, 'extra', None), dict) else {}
                    if not new_extra.get('timestamp_iso') and not new_extra.get('timestamp_text'):
                        new_extra['scan_timestamp_iso'] = scan_iso
                    # Preserve the SAE-encoded code as a fallback reference for debugging.
                    if resolved_code and resolved_code != d.code:
                        new_extra.setdefault('sae_code', d.code)
                    new_dtcs.append(DtcItem(
                        code=resolved_code or d.code,
                        uds_dtc=d.uds_dtc,
                        status_byte=d.status_byte,
                        status_desc=d.status_desc,
                        active=d.active,
                        raw=d.raw,
                        description=desc or d.description,
                        dtc_class=d.dtc_class,
                        extra=new_extra,
                    ))
                enriched_reports.append(EcuReport(
                    tx_id=ecu.tx_id, rx_id=ecu.rx_id, name=ecu.name,
                    dtcs=new_dtcs, obd=ecu.obd, ident=ecu.ident, contacted=True,
                ))
            reports = enriched_reports
            self.log(f"PDX descriptions enriched into {sum(1 for e in reports for d in e.dtcs if d.description)} DTC items")

        pdx_path = _get_active_pdx_path()
        pdx_filename = os.path.basename(pdx_path) if pdx_path else ''

        html_name = f"vag_doip_scan_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
        html_path = os.path.join(self.log_dir, html_name)
        _write_vag_html_report(
            html_path,
            reports,
            title='VAG DoIP Scan Report',
            subtitle=f"Gateway: {self.gateway_ip} | Tester: 0x{self.tester_addr:04X}",
            dtc_map=dtc_map,
            pdx_info={
                'pdx_filename': pdx_filename,
                'scan_source': scan_source,
                'comm_ecu_count': (len(comm_index.get('ecus')) if isinstance(comm_index, dict) and isinstance(comm_index.get('ecus'), list) else None),
            },
            comm_index=comm_index if isinstance(comm_index, dict) and comm_index else None,
            vin=scan_vin,
            not_contacted=not_contacted,
            env_data_index=env_data_index,
        )

        xml_name = html_name.replace('.html', '.diagra.xml')
        csv_name = html_name.replace('.html', '.diagra.csv')
        try:
            _write_diagra_xml_report(
                os.path.join(self.log_dir, xml_name),
                reports,
                title='VAG DoIP Scan Report',
                subtitle=f"Gateway: {self.gateway_ip} | Tester: 0x{self.tester_addr:04X}",
                dtc_map=dtc_map,
            )
            _write_diagra_csv_report(
                os.path.join(self.log_dir, csv_name),
                reports,
                dtc_map=dtc_map,
            )
            self.log(f"Diagra export saved: {xml_name}")
            self.log(f"Download: /api/logs/{xml_name}")
            self.log(f"Diagra export saved: {csv_name}")
            self.log(f"Download: /api/logs/{csv_name}")
        except Exception as e:
            self.log(f"Diagra export failed: {e}")
        return html_name, discovered

    def scan_mode06_doip(self, *, ecu_addresses: Optional[List[int]] = None) -> None:
        """Run OBD Mode 06 (on-board monitoring test results) via DoIP UDS.

        [TRC 2026-02-17] DoIP counterpart of VAGScanner.scan_mode06().
        Discovers ECUs via DoIP, then sends Service 0x06 requests over UDS
        to each ECU to read on-board monitor test results (TIDs).
        """
        if not self.gateway_ip:
            raise ValueError('missing gateway_ip')

        self.log(f"=== DoIP Mode 06 Scan (Gateway {self.gateway_ip}) ===")
        self._connect_with_recovery()
        self._routing_activation()

        # Determine ECU addresses
        comm_index = _load_active_pdx_comm_index()
        pdx_doip_addrs = self._pdx_doip_ecu_addresses(comm_index)

        scan_list = ecu_addresses
        if scan_list is None and pdx_doip_addrs:
            self.log(f"DoIP: Using PDX ECU address list ({len(pdx_doip_addrs)})")
            scan_list = pdx_doip_addrs

        discovered = self._discover_ecus(scan_list)
        if not discovered:
            self.log("DoIP Mode 06: No ECUs discovered.")
            return

        self.log(f"DoIP Mode 06: {len(discovered)} ECU(s) alive")

        total_pass = 0
        total_fail = 0
        total_unknown = 0

        for ta in discovered:
            self.log(f"\n── ECU 0x{ta:04X} ──")
            # Try extended session for richer data access
            try:
                self._uds_transact(int(ta), b'\x10\x03', timeout_s=0.8)
            except Exception:
                pass

            # Step 1: query supported TIDs via Service 06 TID 00
            resp00 = self._uds_transact(int(ta), b'\x06\x00', timeout_s=2.5)
            if not resp00:
                self.log(f"  ECU 0x{ta:04X}: No response to Mode 06")
                continue
            if resp00[0] == 0x7F:
                nrc = resp00[2] if len(resp00) >= 3 else 0
                self.log(f"  ECU 0x{ta:04X}: Mode 06 rejected (NRC 0x{nrc:02X})")
                continue
            if resp00[0] != 0x46:
                self.log(f"  ECU 0x{ta:04X}: Unexpected response: {resp00.hex()}")
                continue

            # Parse supported TID bitmask from 06 00 response
            supported_tids: List[int] = []
            if len(resp00) >= 6 and resp00[1] == 0x00:
                mask = resp00[2:6]
                for bit in range(32):
                    byte_i = bit // 8
                    bit_i = 7 - (bit % 8)
                    if (mask[byte_i] >> bit_i) & 1:
                        supported_tids.append(bit + 1)

            self.log(f"  Supported TIDs: {[f'0x{t:02X}' for t in supported_tids]}")

            # Step 2: query each supported TID
            tests: List[Dict[str, Any]] = []
            for tid in supported_tids[:32]:
                resp = self._uds_transact(int(ta), bytes([0x06, int(tid) & 0xFF]), timeout_s=2.5)
                if not resp or resp[0] != 0x46:
                    continue
                # Parse TID frame: 46 <TID> [CID(1) VALUE(2) MIN(2) MAX(2)]*
                data = resp[2:]
                stride = 7
                i = 0
                while i + stride <= len(data):
                    cid = int(data[i])
                    value = int.from_bytes(data[i+1:i+3], 'big', signed=False)
                    vmin = int.from_bytes(data[i+3:i+5], 'big', signed=False)
                    vmax = int.from_bytes(data[i+5:i+7], 'big', signed=False)
                    passed = (vmin <= value <= vmax) if (vmin != 0 or vmax != 0) else None
                    tests.append({
                        'tid': tid, 'cid': cid, 'value': value,
                        'min': vmin, 'max': vmax, 'pass': passed,
                    })
                    i += stride

            passed_cnt = sum(1 for t in tests if t.get('pass') is True)
            failed_cnt = sum(1 for t in tests if t.get('pass') is False)
            unknown_cnt = sum(1 for t in tests if t.get('pass') is None)
            total_pass += passed_cnt
            total_fail += failed_cnt
            total_unknown += unknown_cnt

            self.log(f"  Tests: {len(tests)} total — {passed_cnt} pass, {failed_cnt} fail, {unknown_cnt} unknown")

            failing = [t for t in tests if t.get('pass') is False]
            if failing:
                self.log(f"  ⚠ Failing tests:")
                for t in failing[:20]:
                    self.log(f"    TID 0x{t['tid']:02X} CID 0x{t['cid']:02X}: value={t['value']} min={t['min']} max={t['max']}")
            elif tests:
                self.log(f"  ✓ All tests passed")

        self.log(f"\n=== DoIP Mode 06 Summary: {total_pass} pass, {total_fail} fail, {total_unknown} unknown ===")

    def clear_dtcs_doip(self, *, ecu_addresses: Optional[List[int]] = None) -> None:
        """Clear DTCs (Mode 04/UDS 0x14) on discovered ECUs via DoIP."""
        if not self.gateway_ip:
            raise ValueError('missing gateway_ip')

        self.log(f"=== DoIP Clear DTCs (Gateway {self.gateway_ip}) ===")
        self._connect_with_recovery()
        self._routing_activation()

        # Determine which addresses to probe/clear
        comm_index = _load_active_pdx_comm_index()
        pdx_doip_addrs = self._pdx_doip_ecu_addresses(comm_index)

        scan_list = None
        if ecu_addresses is None and pdx_doip_addrs:
            self.log(f"DoIP: Using PDX ECU address list for discovery ({len(pdx_doip_addrs)})")
            scan_list = pdx_doip_addrs
        else:
            scan_list = ecu_addresses

        # Verify connectivity
        discovered = self._discover_ecus(scan_list)
        if not discovered:
            self.log("DoIP: No ECUs discovered.")
            return

        self.log(f"DoIP: Clearing DTCs on {len(discovered)} ECUs (per-ECU ODX profile, ISO 14229 / VW80124)...")

        # ISO 14229 NRC table (subset relevant to ClearDiagnosticInformation per
        # PDX DOP_TEXTTABLENegatRespoCodesClearDiagnInfor).
        nrc_names = {
            0x10: 'General reject',
            0x11: 'Service not supported',
            0x12: 'Sub-function not supported',
            0x13: 'Incorrect message length or invalid format',
            0x14: 'Response too long',
            0x21: 'Busy - repeat request',
            0x22: 'Conditions not correct',
            0x24: 'Request sequence error',
            0x25: 'No response from subnet component',
            0x26: 'Failure prevents execution of requested action',
            0x31: 'Request out of range',
            0x33: 'Security access denied',
            0x34: 'Authentication required',
            0x35: 'Invalid key',
            0x36: 'Exceeded number of attempts',
            0x37: 'Required time delay not expired',
            0x70: 'Upload/Download not accepted',
            0x72: 'General programming failure',
            0x78: 'Request correctly received - response pending',
            0x7E: 'Sub-function not supported in active session',
            0x7F: 'Service not supported in active session',
            0x83: 'Engine is running',
            0x84: 'Engine is not running',
        }

        # Per-ECU profile derived from PDX/ODX inheritance graph.
        # See _build_pdx_clear_profile() for semantics. For each ECU the profile
        # provides an ordered list of (sid, group_bytes, session) variants:
        #   * Standard VW80124 ECUs:          14 FF FF FF in ExtendedSession
        #   * OBD-affected ECUs (ECM, TCM,
        #     BECM, DMCM, OBC, DCDC, ...):    14 FF FF 33 (WWH-OBD) in ExtendedSession
        #   * Mixed (gateway-class):          tries both, in order
        # On NRC 0x22/0x24/0x7E/0x7F the next variant is attempted; if that
        # variant requires a different session it is entered first.
        clear_profile = _load_active_pdx_clear_profile()

        def _do_session(ta_u16: int, sub: int) -> Tuple[bool, Optional[int]]:
            """Send 0x10 sub-function. Return (entered_ok, nrc_or_None)."""
            try:
                r = self._uds_transact(ta_u16, bytes([0x10, sub & 0xFF]), timeout_s=1.0)
            except Exception:
                return False, None
            if r and len(r) >= 2 and r[0] == 0x50 and r[1] == (sub & 0xFF):
                return True, None
            if r and len(r) >= 3 and r[0] == 0x7F:
                return False, int(r[2])
            return False, None

        def _attempt_clear(ta_u16: int, variant: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
            """Issue one ClearDiagnosticInformation variant.

            Returns (positive_response_first_byte, nrc) where exactly one is set.
            If both are None the ECU did not answer.
            """
            sid = int(variant.get('sid', 0x14)) & 0xFF
            grp = bytes(variant.get('group') or b'\xFF\xFF\xFF')
            req = bytes([sid]) + grp
            try:
                r = self._uds_transact(ta_u16, req, timeout_s=2.0)
            except Exception:
                return None, None
            if r and r[0] == (sid + 0x40):
                return int(r[0]), None
            if r and len(r) >= 3 and r[0] == 0x7F:
                return None, int(r[2])
            return None, None

        def _attempt_obd_mode04(ta_u16: int) -> Tuple[Optional[int], Optional[int]]:
            """OBD-II Service $04 ClearDiagnosticInformation/ResetMIL (single byte).

            ISO 15031-5 / SAE J1979 emissions clear:
              request : 0x04
              positive: 0x44
              negative: 0x7F 0x04 <NRC>
            On the powertrain ECUs (engine/transmission/HV/ORVR/EVAP) this is
            the only request that turns OFF the MIL — UDS 0x14 does NOT clear
            emissions DTCs nor the MIL on OBD-II compliant ECUs (per ISO 14229
            Annex on legislated emissions). Always issued in default session.
            """
            try:
                r = self._uds_transact(ta_u16, b'\x04', timeout_s=2.0)
            except Exception:
                return None, None
            if r and r[0] == 0x44:
                return int(r[0]), None
            if r and len(r) >= 3 and r[0] == 0x7F and r[1] == 0x04:
                return None, int(r[2])
            # Some ECUs answer with bare 0x7F + NRC (no service id echo).
            if r and len(r) >= 2 and r[0] == 0x7F:
                return None, int(r[-1])
            return None, None

        def _read_confirmed_dtcs(ta_u16: int) -> Optional[List[Tuple[int, int]]]:
            """ReadDTCInformation 0x19 02 0x08 (confirmed mask).

            Returns list of (dtc_24bit, status_byte) for DTCs whose
            statusOfDTC has the Confirmed bit (0x08) set, or None if the
            ECU did not answer / answered negatively. Used to discover
            residual DTCs that survived a successful group ClearDTC.
            """
            try:
                r = self._uds_transact(ta_u16, b'\x19\x02\x08', timeout_s=2.0)
            except Exception:
                return None
            if not r or r[0] != 0x59 or len(r) < 3:
                return None
            # Format: 59 02 <availability_mask> [<DTC_HI><DTC_MID><DTC_LO><status>]*
            body = r[3:]
            out: List[Tuple[int, int]] = []
            for off in range(0, len(body) - 3, 4):
                dtc = (body[off] << 16) | (body[off + 1] << 8) | body[off + 2]
                st = int(body[off + 3])
                if dtc == 0:
                    continue
                # Only report still-confirmed entries.
                if st & 0x08:
                    out.append((dtc, st))
            return out

        def _per_dtc_clear(ta_u16: int, dtc_24bit: int) -> Tuple[Optional[int], Optional[int]]:
            """ClearDiagnosticInformation 0x14 with a SPECIFIC 3-byte DTC.

            ISO 14229 §11.5: groupOfDTC parameter may be either a group
            code (e.g. 0xFFFFFF) or a specific DTC. Some VAG ECUs accept
            per-DTC clear even when group clear is a positive no-op for
            firmware-asserted state DTCs. Returns (positive_first_byte,
            nrc) with exactly one set; both None on no answer.
            """
            req = bytes([0x14,
                         (dtc_24bit >> 16) & 0xFF,
                         (dtc_24bit >> 8) & 0xFF,
                         dtc_24bit & 0xFF])
            try:
                r = self._uds_transact(ta_u16, req, timeout_s=2.0)
            except Exception:
                return None, None
            if r and r[0] == 0x54:
                return int(r[0]), None
            if r and len(r) >= 3 and r[0] == 0x7F:
                return None, int(r[2])
            return None, None

        def _fmt_dtc_code(dtc_24bit: int) -> str:
            """Format a 3-byte DTC as the human-readable 5-char code (e.g. P0123)."""
            hi = (dtc_24bit >> 16) & 0xFF
            mid = (dtc_24bit >> 8) & 0xFF
            lo = dtc_24bit & 0xFF
            family_bits = (hi >> 6) & 0x03
            family = 'PCBU'[family_bits]
            return f"{family}{(hi & 0x3F):02X}{mid:02X}{lo:02X}"

        count_ok = 0
        residuals_summary: List[Tuple[int, str, int, int]] = []  # (ta, tag, before, after)
        for ta in discovered:
            ta_u16 = int(ta) & 0xFFFF
            prof = clear_profile.get(ta_u16) or {}
            raw_variants = prof.get('variants') or []
            # Materialise variants (group_hex -> bytes) for this run; if no profile
            # entry exists, use the conservative default ladder.
            variants: List[Dict[str, Any]] = []
            if raw_variants:
                for v in raw_variants:
                    try:
                        gh = str(v.get('group_hex') or 'FFFFFF')
                        variants.append({
                            'sid': int(v.get('sid', 0x14)) & 0xFF,
                            'group': bytes.fromhex(gh),
                            'label': str(v.get('label') or f'SID 0x{int(v.get("sid",0x14)):02X} {gh}'),
                            'session': v.get('session'),
                        })
                    except Exception:
                        continue
            if not variants:
                variants = [
                    {'sid': 0x14, 'group': b'\xFF\xFF\xFF',
                     'label': 'UDS Clear All (FFFFFF)', 'session': 0x03},
                    {'sid': 0x14, 'group': b'\xFF\xFF\x33',
                     'label': 'WWH-OBD Clear Emissions (FFFF33)', 'session': 0x03},
                ]

            short = str(prof.get('short_name') or '').strip()
            tag = f" [{short}]" if short else ''
            is_obd_mil_candidate = _pdx_profile_is_obd_mil_candidate(prof)
            current_session = 0x01  # assume default after RA / discovery probe
            cleared = False
            last_nrc: Optional[int] = None
            tried_default_fallback = False
            obd_mode04_ok = False
            obd_mode04_nrc: Optional[int] = None

            def _reset_mil_via_obd_mode04(reason: str) -> Tuple[bool, Optional[int]]:
                nonlocal current_session
                if current_session != 0x01:
                    ok, ses_nrc = _do_session(ta_u16, 0x01)
                    if ok:
                        current_session = 0x01
                    elif ses_nrc is not None:
                        self.log(f"ECU 0x{ta_u16:04X}{tag}: default session before OBD Mode $04 refused (NRC=0x{ses_nrc:02X} {nrc_names.get(ses_nrc,'?')}); trying Mode $04 anyway")
                fb_byte, fb_nrc = _attempt_obd_mode04(ta_u16)
                if fb_byte is not None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: MIL reset OK (OBD Mode $04, {reason})")
                    return True, None
                if fb_nrc is not None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: OBD Mode $04 → NRC=0x{fb_nrc:02X} {nrc_names.get(fb_nrc,'?')}")
                    return False, fb_nrc
                self.log(f"ECU 0x{ta_u16:04X}{tag}: OBD Mode $04 → no response")
                return False, None

            for vi, variant in enumerate(variants):
                wanted_session = variant.get('session')
                if isinstance(wanted_session, int) and wanted_session != current_session:
                    ok, ses_nrc = _do_session(ta_u16, wanted_session)
                    if ok:
                        current_session = wanted_session
                    elif ses_nrc in (0x12, 0x7E, 0x7F, 0x11):
                        # ECU does not support this session: try variant in current
                        # session anyway (some ECUs accept clear in default).
                        pass
                    elif ses_nrc is not None:
                        self.log(f"ECU 0x{ta_u16:04X}{tag}: session 0x{wanted_session:02X} refused (NRC=0x{ses_nrc:02X} {nrc_names.get(ses_nrc,'?')}); trying clear anyway")

                first_byte, nrc = _attempt_clear(ta_u16, variant)
                if first_byte is not None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear OK ({variant['label']})")
                    count_ok += 1
                    cleared = True
                    break
                if nrc is None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear no response ({variant['label']})")
                    last_nrc = None
                    continue
                last_nrc = nrc
                # NRC 0x22 in extended session: retry once in default session.
                if nrc == 0x22 and not tried_default_fallback and current_session != 0x01:
                    tried_default_fallback = True
                    ok, _ = _do_session(ta_u16, 0x01)
                    if ok:
                        current_session = 0x01
                        first_byte2, nrc2 = _attempt_clear(ta_u16, variant)
                        if first_byte2 is not None:
                            self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear OK ({variant['label']}, default session)")
                            count_ok += 1
                            cleared = True
                            break
                        if nrc2 is not None:
                            last_nrc = nrc2
                # On 0x11/0x12/0x31 (service/sub/range), try next variant.
                # On 0x22/0x24/0x7E/0x7F (conditions/sequence/session), try next variant.
                if nrc in (0x11, 0x12, 0x22, 0x24, 0x31, 0x7E, 0x7F):
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: {variant['label']} → NRC=0x{nrc:02X} {nrc_names.get(nrc,'?')}; trying next variant")
                    continue
                # Otherwise (e.g. 0x33 security, 0x21 busy, 0x72 programming):
                # report and stop trying further variants.
                self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear Failed (NRC=0x{nrc:02X} {nrc_names.get(nrc,'?')})")
                cleared = False
                break

            if not cleared:
                # ── Final fallback: OBD-II Mode $04 (ISO 15031-5).
                # Powertrain / emissions-related ECUs (ECM, TCM, BECM, OBC,
                # DCDC, ORVR, EVAP, NOx) often reject UDS 0x14 because
                # their legislated emissions DTC store can ONLY be cleared
                # via the OBD-II Mode $04 request.  Crucially this is also
                # the ONLY request that turns the MIL OFF.  We always try
                # it as a last resort regardless of the previous NRC, so a
                # 0x33 (security) or 0x22 (conditions) on UDS 0x14 still
                # gets a fair shot at clearing emissions data.
                obd_mode04_ok, obd_mode04_nrc = _reset_mil_via_obd_mode04('final fallback')
                if obd_mode04_ok:
                    count_ok += 1
                    cleared = True
            elif is_obd_mil_candidate:
                # A positive UDS ClearDiagnosticInformation response does not
                # necessarily reset the legislated emissions store or the MIL.
                # The PDX marks these ECUs through FG_AllOBDSyste /
                # FG_AllEmissRelatUDSSyste, so run Mode $04 explicitly.
                obd_mode04_ok, obd_mode04_nrc = _reset_mil_via_obd_mode04('PDX emissions/MIL ECU')

            if cleared:
                # ── Residual-DTC pass: a positive 0x54 from a group clear
                # does NOT guarantee that every confirmed DTC is actually
                # gone.  Some VAG ECUs treat groupOfDTC=0xFFFFFF as a
                # no-op for firmware-asserted "state" DTCs (Dev mode,
                # SFD-unlocked, persistent network monitors).  Read the
                # confirmed-DTC list back and try a per-DTC clear for
                # each surviving entry, then log the truthful outcome.
                residual = _read_confirmed_dtcs(ta_u16)
                if residual is None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: residual check skipped (0x19 02 not supported)")
                elif not residual:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: ✓ no confirmed DTCs after clear")
                else:
                    n_before = len(residual)
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: {n_before} confirmed DTC(s) survived group clear; trying per-DTC clear")
                    per_ok = 0
                    for dtc, st in residual:
                        pb, pn = _per_dtc_clear(ta_u16, dtc)
                        if pb is not None:
                            per_ok += 1
                        else:
                            nrc_str = f"NRC=0x{pn:02X}" if pn is not None else "no response"
                            self.log(f"  ↳ {_fmt_dtc_code(dtc)} (status=0x{st:02X}): per-DTC clear failed ({nrc_str})")
                    # Re-read to confirm the truthful end state.
                    residual2 = _read_confirmed_dtcs(ta_u16)
                    n_after = len(residual2) if residual2 is not None else -1
                    if n_after == 0:
                        self.log(f"ECU 0x{ta_u16:04X}{tag}: ✓ all DTCs cleared after per-DTC pass ({per_ok}/{n_before})")
                    elif n_after > 0:
                        codes = ", ".join(_fmt_dtc_code(d) for d, _ in residual2)
                        self.log(f"ECU 0x{ta_u16:04X}{tag}: ⚠ {n_after} DTC(s) persist (firmware re-asserted): {codes}")
                        residuals_summary.append((ta_u16, tag, n_before, n_after))
                    else:
                        self.log(f"ECU 0x{ta_u16:04X}{tag}: residual re-read failed")

            if not cleared:
                if last_nrc is None:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear Failed (no response to any variant)")
                else:
                    self.log(f"ECU 0x{ta_u16:04X}{tag}: Clear Failed (last NRC=0x{last_nrc:02X} {nrc_names.get(last_nrc,'?')})")

        self.log(f"DoIP: Cleared {count_ok}/{len(discovered)} ECUs.")
        if residuals_summary:
            total = sum(n for _, _, _, n in residuals_summary)
            self.log(f"DoIP: ⚠ {total} firmware-asserted DTC(s) persist across {len(residuals_summary)} ECU(s) "
                     "(state indicators such as Dev-Mode, SFD-unlocked, lost-comm — re-asserted by ECU monitors).")

        # ── Final MIL verification: query OBD-II Mode 01 PID $01 (Monitor
        # status since DTCs cleared) on the OBD-compliant ECUs to confirm
        # whether the MIL is actually off after the clear pass.  This is
        # the ground truth — the gateway / ICM lamp is just a mirror of
        # the engine ECU's MIL bit.  We probe the same powertrain
        # candidate list the Live Data path uses and report the first
        # ECU that answers.
        try:
            obd_candidates: List[int] = []
            discovered_for_mil = sorted(
                (int(a) & 0xFFFF for a in discovered),
                key=lambda a: (_pdx_profile_mil_priority(clear_profile.get(a) or {}), a),
            )
            for a_u16 in discovered_for_mil:
                prof = clear_profile.get(a_u16) or {}
                if _pdx_profile_is_obd_mil_candidate(prof) and a_u16 not in obd_candidates:
                    obd_candidates.append(a_u16)
            for a_u16 in discovered_for_mil:
                if (a_u16 & 0xFF00) in (0x0000, 0x4000) and a_u16 not in obd_candidates:
                    obd_candidates.append(a_u16)
            # Always include the well-known ISO 13400-4 OBD addresses too.
            for extra in (0x0001, 0x0010, 0x4010, 0x407B, 0x4044):
                if extra not in obd_candidates:
                    obd_candidates.append(extra)
            mil_state: Optional[bool] = None
            mil_dtc_count: Optional[int] = None
            mil_source: Optional[int] = None
            for ta in obd_candidates:
                try:
                    r = self._uds_transact(int(ta) & 0xFFFF, b'\x01\x01', timeout_s=1.0)
                except Exception:
                    continue
                # Positive OBD response: 0x41 0x01 <A> <B> <C> <D>
                if r and len(r) >= 6 and r[0] == 0x41 and r[1] == 0x01:
                    a = r[2]
                    mil_state = bool(a & 0x80)
                    mil_dtc_count = int(a & 0x7F)
                    mil_source = int(ta) & 0xFFFF
                    break
            if mil_state is None:
                self.log("DoIP: MIL state could not be verified (no OBD ECU answered Mode 01 PID $01)")
            else:
                tag_addr = f"0x{mil_source:04X}" if mil_source is not None else "?"
                if mil_state:
                    self.log(f"DoIP: ⚠ MIL still ON (source ECU {tag_addr}, {mil_dtc_count} stored DTC(s)).")
                else:
                    self.log(f"DoIP: ✓ MIL is OFF (verified on ECU {tag_addr}, {mil_dtc_count} stored DTC(s)).")
        except Exception as e:
            self.log(f"DoIP: MIL verification skipped ({e})")

    def _maybe_enable_gateway_mirror(self) -> None:
        """Best-effort: enable gateway mirror mode (Ethernet) via UDS WriteDID.

        Failure must not abort scanning; we log and continue.
        """
        dry_run = str(os.getenv('DOIP_MIRROR_DRY_RUN', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}

        def _parse_int_env(k: str, default: int = 0) -> int:
            try:
                v = str(os.getenv(k, '') or '').strip()
                if not v:
                    return int(default)
                return int(v, 0)
            except Exception:
                return int(default)

        target_addr = _parse_int_env('DOIP_MIRROR_TARGET_ADDR', 0) & 0xFFFF
        dest_ip = str(os.getenv('DOIP_MIRROR_DEST_IP', '') or '').strip()
        dest_port = _parse_int_env('DOIP_MIRROR_DEST_PORT', 0) & 0xFFFF

        # Best-effort fallback: try reading destination/target from app_config.json if present.
        if not dest_ip or not dest_port or not target_addr:
            try:
                base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
                cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
                if os.path.isfile(cfg_path):
                    import json
                    with open(cfg_path, 'r', encoding='utf-8') as fp:
                        raw = json.load(fp)
                    cfg = raw.get('config') if isinstance(raw, dict) else None
                    if isinstance(cfg, dict):
                        gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
                        if not target_addr:
                            try:
                                ta = str(gm.get('target_addr') or '').strip()
                                if ta:
                                    target_addr = int(ta, 0) & 0xFFFF
                            except Exception:
                                pass
                        if not dest_ip:
                            dest_ip = str(gm.get('dest_ip') or '').strip()
                        if not dest_port:
                            try:
                                dest_port = int(gm.get('dest_port') or 0) & 0xFFFF
                            except Exception:
                                dest_port = 0
            except Exception:
                pass

        # If target_addr is still unknown, auto-discover it by probing the mirror DID.
        if not target_addr:
            try:
                target_addr = int(self._discover_gateway_mirror_target_addr()) & 0xFFFF
            except Exception:
                target_addr = 0
        if not target_addr:
            self.log('DoIP: Mirror requested but gateway target LA unknown. Auto-discovery failed. Set DOIP_MIRROR_TARGET_ADDR or configure gateway_mirror.target_addr.')
            return
        if not dest_ip or not dest_port:
            self.log('DoIP: Mirror requested but dest_ip/dest_port missing. Set DOIP_MIRROR_DEST_IP/DOIP_MIRROR_DEST_PORT (or configure gateway_mirror).')
            return

        # Resolve mirror DID from PDX when possible.
        did = 0x096F
        try:
            pdx_path = _get_active_pdx_path()
            if pdx_path:
                from .pdx_parser import extract_gateway_mirror_definition_from_pdx
                d = extract_gateway_mirror_definition_from_pdx(pdx_path)
                if isinstance(d, dict) and d.get('ok') and isinstance(d.get('dids'), dict):
                    did_s = str(d['dids'].get('mirror_mode') or '').strip()
                    if did_s:
                        did = int(did_s, 0) & 0xFFFF
        except Exception:
            did = 0x096F

        def _parse_list_env(k: str) -> List[str]:
            raw = str(os.getenv(k, '') or '').strip()
            if not raw:
                return []
            return [p.strip() for p in raw.split(',') if p.strip()]

        can_list: List[int] = []
        for p in _parse_list_env('DOIP_MIRROR_CAN'):
            try:
                can_list.append(int(p, 0))
            except Exception:
                pass
        lin_list: List[int] = []
        for p in _parse_list_env('DOIP_MIRROR_LIN'):
            try:
                lin_list.append(int(p, 0))
            except Exception:
                pass
        fr_list: List[str] = []
        for p in _parse_list_env('DOIP_MIRROR_FR'):
            fr_list.append(str(p).strip().upper())

        try:
            from .gateway_mirror import build_mirror_mode_write_request
            req = build_mirror_mode_write_request(
                did=int(did) & 0xFFFF,
                target_bus='ethernet',
                can=can_list,
                flexray=fr_list,
                lin=lin_list,
                dest_ip=dest_ip,
                dest_port=int(dest_port),
            )
        except Exception as e:
            self.log(f"DoIP: Mirror payload build failed: {e}")
            return

        self.log(f"DoIP: Mirror mode request: target=0x{int(target_addr) & 0xFFFF:04X} did=0x{int(req.did) & 0xFFFF:04X} dest={dest_ip}:{int(dest_port)} payload_len={len(req.payload)}")
        if dry_run:
            self.log('DoIP: Mirror DRY-RUN enabled; not writing DID.')
            return

        # Try a small set of sessions; gateway_mirror API uses 0x03/0x40, but 0x01 is safest.
        try:
            for sess in (0x01, 0x03, 0x40):
                r = self._uds_transact(int(target_addr), bytes([0x10, int(sess) & 0xFF]), timeout_s=1.0)
                if r and r[:1] == b'\x50':
                    break
        except Exception:
            pass

        try:
            resp = self._uds_transact(
                int(target_addr),
                bytes([0x2E, (int(req.did) >> 8) & 0xFF, int(req.did) & 0xFF]) + (req.payload or b''),
                timeout_s=2.5,
            )
            if resp and resp[:1] == b'\x6E':
                self.log('DoIP: Mirror mode enabled (WriteDID positive response).')
            elif resp and len(resp) >= 3 and resp[0] == 0x7F:
                self.log(f"DoIP: Mirror write negative response: svc=0x{resp[1]:02X} nrc=0x{resp[2]:02X} raw={resp.hex()}")
            else:
                self.log('DoIP: Mirror write had no/unknown response (continuing scan).')
        except Exception as e:
            self.log(f"DoIP: Mirror write failed: {e}")

    def _discover_gateway_mirror_target_addr(self) -> Optional[int]:
        """Best-effort: discover the gateway ECU logical address that supports Mirror_mode DID.

        Strategy:
        - resolve mirror DID from PDX (fallback 0x096F)
        - probe candidate LAs (0x0E80 + 0x0001..0x00FF by default)
        - for each LA, optionally try session transitions (safe->extended->developer)
        - ReadDataByIdentifier(0x22) mirror DID; accept positive response 0x62

        Env controls:
          DOIP_MIRROR_DISCOVERY_START / END  (default 0x0001..0x00FF)
          DOIP_MIRROR_DISCOVERY_TRY_SESSIONS=1 (default 1)
        """
        # mirror DID
        did = 0x096F
        try:
            pdx_path = _get_active_pdx_path()
            if pdx_path:
                from .pdx_parser import extract_gateway_mirror_definition_from_pdx
                d = extract_gateway_mirror_definition_from_pdx(pdx_path)
                if isinstance(d, dict) and d.get('ok') and isinstance(d.get('dids'), dict):
                    did_s = str(d['dids'].get('mirror_mode') or '').strip()
                    if did_s:
                        did = int(did_s, 0) & 0xFFFF
        except Exception:
            did = 0x096F

        did_hi = (int(did) >> 8) & 0xFF
        did_lo = int(did) & 0xFF

        # Candidate list
        start = 0x0001
        end = 0x00FF
        try:
            s0 = str(os.getenv('DOIP_MIRROR_DISCOVERY_START', '') or '').strip()
            e0 = str(os.getenv('DOIP_MIRROR_DISCOVERY_END', '') or '').strip()
            if s0:
                start = int(s0, 0) & 0xFFFF
            if e0:
                end = int(e0, 0) & 0xFFFF
        except Exception:
            start, end = 0x0001, 0x00FF
        if end < start:
            start, end = end, start

        # Many VAG gateways expose diagnostics on 0x4010; try it early.
        candidates = [0x4010, 0x0E80] + list(range(int(start), int(end) + 1))
        # De-dup while preserving order
        seen = set()
        cand2: List[int] = []
        for c in candidates:
            cc = int(c) & 0xFFFF
            if cc and cc not in seen:
                seen.add(cc)
                cand2.append(cc)
        candidates = cand2

        try_sessions = str(os.getenv('DOIP_MIRROR_DISCOVERY_TRY_SESSIONS', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}
        sessions = (0x01, 0x03, 0x40) if try_sessions else ()

        self.log(f"DoIP: Discovering mirror target LA via DID 0x{int(did):04X} (candidates={len(candidates)})")
        tested = 0
        for la in candidates:
            tested += 1
            la = int(la) & 0xFFFF
            # Optional session attempts
            if sessions:
                for sess in sessions:
                    try:
                        r = self._uds_transact(int(la), bytes([0x10, int(sess) & 0xFF]), timeout_s=0.35)
                        if r and r[:1] == b'\x50':
                            break
                    except Exception:
                        pass
                try:
                    time.sleep(0.05)
                except Exception:
                    pass

            resp = self._uds_transact(int(la), bytes([0x22, did_hi, did_lo]), timeout_s=0.35)
            if not resp:
                continue
            if len(resp) >= 3 and resp[0] == 0x62 and resp[1] == did_hi and resp[2] == did_lo:
                self.log(f"DoIP: Mirror target discovered: 0x{int(la):04X} (tested={tested})")
                return int(la)

        self.log(f"DoIP: Mirror target discovery failed (tested={tested})")
        return None

    def _pdx_doip_ecu_addresses(self, comm_index: Any) -> List[int]:
        """Extract a sorted, de-duplicated list of DoIP logical ECU addresses from comm_index."""
        out: List[int] = []
        try:
            if not isinstance(comm_index, dict):
                return []
            rows = comm_index.get('ecus')
            if not isinstance(rows, list):
                return []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                doip = r.get('doip')
                if not isinstance(doip, dict):
                    continue
                la = doip.get('logical_ecu_address')
                if isinstance(la, int):
                    out.append(int(la) & 0xFFFF)
        except Exception:
            return []
        out = sorted(set([x for x in out if x != 0]))
        return out

    def _pdx_doip_meta_by_addr(self, comm_index: Any) -> Dict[int, Dict[str, Any]]:
        """Build logical_ecu_address -> {short_name,long_name,source_odx,protocol_snref,...} map."""
        out: Dict[int, Dict[str, Any]] = {}
        try:
            if not isinstance(comm_index, dict):
                return out
            rows = comm_index.get('ecus')
            if not isinstance(rows, list):
                return out
            for r in rows:
                if not isinstance(r, dict):
                    continue
                doip = r.get('doip')
                if not isinstance(doip, dict):
                    continue
                la = doip.get('logical_ecu_address')
                if not isinstance(la, int):
                    continue
                key = int(la) & 0xFFFF
                if key == 0:
                    continue
                out[key] = {
                    'short_name': r.get('short_name'),
                    'long_name': r.get('long_name'),
                    'source_odx': r.get('source_odx'),
                    'protocol_snref': r.get('protocol_snref'),
                }
        except Exception:
            return out
        return out

    # ---- DoIP framing ----
    def _run_cmd(self, argv: List[str]) -> Tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr) without raising."""
        try:
            p = subprocess.run(argv, capture_output=True, text=True)
            return int(p.returncode), str(p.stdout or ''), str(p.stderr or '')
        except Exception as e:
            return 127, '', str(e)

    def _ensure_iface_ready_for_linklocal(self, iface: str, *, timeout_s: float = 10.0) -> bool:
        """Best-effort recovery for flaky IPv6 link-local on Linux.

        When the gateway is a link-local IPv6 address (fe80::/10) and the NIC loses
        its IPv6 state, connect() may throw OSError 101 (Network is unreachable).
        This tries to re-arm the interface and wait for a link-local + route.

        Returns True if the interface looks ready, False otherwise.
        """
        try:
            iface = (iface or '').strip()
            if not iface:
                return False

            # Quick probe: any inet6 address on the iface.
            rc, out, err = self._run_cmd(['ip', '-6', 'addr', 'show', 'dev', iface])
            if rc == 0 and ' inet6 ' in out:
                return True

            # Mirror install/ensure_eth0_ipv6_ll.sh logic (best-effort):
            # - ensure IPv6 isn't disabled
            # - if addr_gen_mode is "none" (1), switch to stable privacy (2)
            # - wait for a non-tentative fe80:: address
            try:
                self._run_cmd(['sysctl', f'net.ipv6.conf.{iface}.disable_ipv6=0'])
            except Exception:
                pass
            try:
                m_rc, m_out, _ = self._run_cmd(['cat', f'/proc/sys/net/ipv6/conf/{iface}/addr_gen_mode'])
                if m_rc == 0 and str(m_out or '').strip() == '1':
                    self._run_cmd(['sh', '-c', f'echo 2 > /proc/sys/net/ipv6/conf/{iface}/addr_gen_mode'])
            except Exception:
                pass

            self.log(f"DoIP: IPv6 appears missing on {iface}. Attempting to re-activate interface…")

            # Strategy A (best): bounce link (needs CAP_NET_ADMIN)
            rc1, out1, err1 = self._run_cmd(['ip', 'link', 'set', 'dev', iface, 'down'])
            time.sleep(0.4)
            rc2, out2, err2 = self._run_cmd(['ip', 'link', 'set', 'dev', iface, 'up'])

            if rc1 != 0 or rc2 != 0:
                self.log(
                    f"DoIP: Interface bounce not permitted or failed (rc_down={rc1}, rc_up={rc2}). "
                    f"stderr_down={err1.strip()[:200]} stderr_up={err2.strip()[:200]}"
                )

                # Strategy B (fallback): ask NetworkManager to reconnect the device.
                # This often works in systemd services even when ip link is blocked.
                rc_nm, _, _ = self._run_cmd(['nmcli', '-t', '-f', 'STATE', 'general'])
                if rc_nm == 0:
                    self.log(f"DoIP: Trying NetworkManager reconnect for {iface}…")
                    self._run_cmd(['nmcli', 'dev', 'disconnect', iface])
                    time.sleep(0.6)
                    self._run_cmd(['nmcli', 'dev', 'connect', iface])

            end = time.time() + max(1.0, float(timeout_s))
            while time.time() < end:
                a_rc, a_out, _ = self._run_cmd(['ip', '-6', 'addr', 'show', 'dev', iface])
                r_rc, r_out, _ = self._run_cmd(['ip', '-6', 'route', 'show', 'dev', iface])
                # Require a *ready* link-local: fe80:: and NOT tentative.
                ok_addr = (
                    a_rc == 0
                    and 'inet6 fe80:' in a_out
                    and ' tentative' not in a_out
                )
                ok_route = (r_rc == 0 and ('fe80::/64' in r_out or 'fe80::/10' in r_out))
                if ok_addr and ok_route:
                    self.log(f"DoIP: Interface {iface} IPv6 ready.")
                    return True
                # If address exists but route is missing, try to add it.
                if ok_addr and (r_rc == 0) and ('fe80::/64' not in r_out):
                    self._run_cmd(['ip', '-6', 'route', 'add', 'fe80::/64', 'dev', iface])
                time.sleep(0.5)
        except Exception:
            return False
        return False

    def _connect_with_recovery(self) -> None:
        """Connect to DoIP gateway, with one recovery attempt for Errno 101."""
        for attempt in range(3):
            try:
                self._connect()
                return
            except OSError as e:
                # 101 = Network is unreachable
                if getattr(e, 'errno', None) != 101:
                    raise

                ip = str(self.gateway_ip).strip()
                iface = ''
                if '%' in ip:
                    try:
                        _, iface = ip.split('%', 1)
                        iface = (iface or '').strip()
                    except Exception:
                        iface = ''

                # Allow env override when gateway_ip doesn't carry a zone-id.
                iface = iface or str(os.getenv('DOIP_GATEWAY_IFACE', '') or '').strip()
                if not iface:
                    raise

                # First failure: do recovery and retry.
                if attempt == 0:
                    ok = self._ensure_iface_ready_for_linklocal(iface)
                    if not ok:
                        raise

                # Subsequent attempts: just wait a bit (service startup races).
                wait_s = 0.8 + (attempt * 0.8)
                self.log(f"DoIP: Network unreachable; retrying connect in {wait_s:.1f}s (attempt {attempt+1}/3)…")
                try:
                    self.close()
                except Exception:
                    pass
                time.sleep(wait_s)
                continue

    def _connect(self) -> None:
        self.close()
        # Dual-stack connect (IPv4/IPv6).
        # NOTE: Python's `socket.create_connection()` is unreliable with IPv6 link-local
        # literals that include a zone-id (e.g. "fe80::1%eth0"). On some systems it
        # fails with EADDRNOTAVAIL / EINVAL. Use an explicit AF_INET6+scopeid tuple.
        ip = str(self.gateway_ip).strip()
        scopeid = 0
        host = ip
        if '%' in ip:
            host, zone = ip.split('%', 1)
            zone = zone.strip()
            try:
                scopeid = socket.if_nametoindex(zone)
            except Exception:
                scopeid = 0

        s = None
        if ':' in host:
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((host, int(self.port), 0, int(scopeid)))
        else:
            s = socket.create_connection((host, self.port), timeout=3.0)
        try:
            s.settimeout(1.0)
        except Exception:
            pass
        self.sock = s
        self.log("DoIP: TCP connected")

    def _doip_send(self, payload_type: int, payload: bytes) -> None:
        if not self.sock:
            raise RuntimeError('DoIP socket not connected')
        header = struct.pack('!BBHL', 0x02, 0xFD, int(payload_type) & 0xFFFF, len(payload))
        if getattr(self, 'debug_doip', False):
            self.log(f"DoIP TX: ptype=0x{int(payload_type) & 0xFFFF:04X} len={len(payload)}")
        data = header + (payload or b'')
        try:
            self.sock.sendall(data)
            return
        except (BrokenPipeError, ConnectionResetError) as e:
            # Gateway closed the TCP socket. This commonly happens after a
            # rejected Routing Activation, an Alive Check timeout, or an idle
            # close. Try to transparently reconnect + re-activate and resend
            # ONCE. Routing-activation/diagnostic-message frames are stateless
            # at the transport layer, so a one-shot retry is safe.
            ptype = int(payload_type) & 0xFFFF
            if ptype == 0x0005 or getattr(self, '_doip_in_recovery', False):
                # Don't recurse during routing activation itself.
                raise
            self.log(f"DoIP: send failed ({e}); reconnecting and retrying once…")
            self._doip_in_recovery = True
            try:
                try:
                    self.close()
                except Exception:
                    pass
                self._connect_with_recovery()
                self._routing_activation()
            finally:
                self._doip_in_recovery = False
            if not self.sock:
                raise
            self.sock.sendall(data)
            return
        except OSError as e:
            # EPIPE on some platforms shows up as plain OSError(32, ...).
            if getattr(e, 'errno', None) == 32 and not getattr(self, '_doip_in_recovery', False) and (int(payload_type) & 0xFFFF) != 0x0005:
                self.log(f"DoIP: send failed (EPIPE); reconnecting and retrying once…")
                self._doip_in_recovery = True
                try:
                    try:
                        self.close()
                    except Exception:
                        pass
                    self._connect_with_recovery()
                    self._routing_activation()
                finally:
                    self._doip_in_recovery = False
                if not self.sock:
                    raise
                self.sock.sendall(data)
                return
            raise

    def _recv_exact(self, n: int) -> bytes:
        if not self.sock:
            raise RuntimeError('DoIP socket not connected')
        buf = b''
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('DoIP socket closed')
            buf += chunk
        return buf

    def _doip_recv(self, timeout_s: float = 1.0) -> Tuple[int, bytes]:
        if not self.sock:
            raise RuntimeError('DoIP socket not connected')
        self.sock.settimeout(max(0.05, float(timeout_s)))
        hdr = self._recv_exact(8)
        ver, inv, ptype, length = struct.unpack('!BBHL', hdr)
        if (ver & 0xFF) != 0x02:
            raise ValueError(f'Unexpected DoIP version: {ver}')
        payload = self._recv_exact(int(length)) if int(length) > 0 else b''
        if getattr(self, 'debug_doip', False):
            # Keep it short; payload can be large.
            preview = payload[:32].hex()
            self.log(f"DoIP RX: ptype=0x{int(ptype):04X} len={int(length)} payload[0:32]={preview}")
        return int(ptype), payload

    def _routing_activation(self) -> None:
        # Payload: SA(2) + ActivationType(1) + Reserved(1) + ISOReserved(4) + OEMReserved(4)
        # 0x01 (WWH-OBD) is the standard for VAG MLBevo/MEB and is widely
        # accepted; 0x00 (default) is rejected by some gateways which then
        # close the TCP socket. Try 0x01 first to minimise broken-pipe risk.
        activation_types = [0x01, 0x00]
        # Some gateways are strict about the tester source address (SA). Try a small
        # set of common VAG tester logical addresses.
        tester_candidates = [int(self.tester_addr) & 0xFFFF, 0x0E80, 0x0E00, 0x0F00, 0x1000]
        # De-dup while preserving order
        seen = set()
        tc2 = []
        for sa in tester_candidates:
            sa = int(sa) & 0xFFFF
            if sa and sa not in seen:
                seen.add(sa)
                tc2.append(sa)
        tester_candidates = tc2
        relaxed = str(os.getenv('DOIP_RA_RELAXED', '') or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')

        last_ptype: Optional[int] = None
        last_nack_code: Optional[int] = None

        # ISO 13400-2: SA(2) + ActType(1) + Reserved(4) + OEM Reserved(4) = 11 bytes.
        # Ensure we don't send 12 bytes!
        for sa in tester_candidates:
            for act_type in activation_types:
                payload = struct.pack('!HBII', int(sa) & 0xFFFF, int(act_type) & 0xFF, 0x00000000, 0x00000000)
                self.log(f"DoIP: Routing activation request (activation_type=0x{act_type:02X}, tester=0x{int(sa) & 0xFFFF:04X})")
                self._doip_send(0x0005, payload)

                # Drain responses until we see a routing activation response or timeout.
                t0 = time.time()
                while time.time() - t0 < 2.0:
                    try:
                        ptype, resp = self._doip_recv(timeout_s=0.5)
                    except socket.timeout:
                        break
                    except Exception:
                        break

                    last_ptype = int(ptype)

                    if ptype == 0x0006:
                        # ISO 13400-2 Routing Activation Response payload:
                        #   TesterLogicalAddress(2) + EntityLogicalAddress(2)
                        #   + RoutingActivationResponseCode(1) + Reserved(4)
                        #   [+ OEM-specific(4)]
                        # Only response code 0x10 means "routing successfully
                        # activated". Any other code is a refusal and the gateway
                        # will typically close the TCP socket shortly after,
                        # which previously caused a Broken pipe on the next send.
                        rcode: Optional[int] = None
                        try:
                            if len(resp) >= 5:
                                rcode = int(resp[4]) & 0xFF
                        except Exception:
                            rcode = None
                        if rcode == 0x10:
                            # Success. Update tester address for subsequent UDS transactions.
                            self.tester_addr = int(sa) & 0xFFFF
                            self.log('DoIP: Routing activation response received')
                            return
                        last_nack_code = rcode if rcode is not None else last_nack_code
                        self.log(
                            f"DoIP: Routing activation refused "
                            f"(response_code=0x{(rcode if rcode is not None else 0):02X}); "
                            f"trying next combination"
                        )
                        # Gateway may have half-closed; force reconnect before
                        # the next attempt so we don't write to a dead socket.
                        try:
                            self.close()
                        except Exception:
                            pass
                        try:
                            self._connect_with_recovery()
                        except Exception as ex:
                            self.log(f"DoIP: reconnect after RA refusal failed: {ex}")
                            raise
                        break

                    # Generic NACK payload is 1 byte (reason code)
                    if ptype == 0x0000 and resp:
                        try:
                            last_nack_code = int(resp[0]) & 0xFF
                            self.log(f"DoIP: Generic NACK during routing activation (code=0x{last_nack_code:02X})")
                        except Exception:
                            pass

        msg = f"DoIP: Routing activation failed (last_ptype={last_ptype}, last_nack=0x{(last_nack_code if last_nack_code is not None else 0):02X})"
        if relaxed:
            self.log(msg)
            return
        raise RuntimeError(msg)

    # ---- UDS over DoIP ----
    def _uds_transact(self, target_addr: int, uds_req: bytes, timeout_s: float = 1.2) -> Optional[bytes]:
        ta = int(target_addr) & 0xFFFF
        payload = struct.pack('!HH', self.tester_addr, ta) + (uds_req or b'')
        if getattr(self, 'debug_doip', False):
            self.log(f"UDS TX: SA=0x{self.tester_addr:04X} TA=0x{ta:04X} uds={bytes(uds_req or b'').hex()}")
        self._doip_send(0x8001, payload)

        # IMPORTANT:
        # - 0x8001 is the diagnostic message payload (contains UDS bytes)
        # - 0x8002 / 0x8003 are diagnostic ACK/NACK payloads (may contain no UDS)
        # Treating ACKs as UDS responses causes desync, missing ECUs, and unstable scans.
        ack_seen = 0
        saw_ack_for_request = False

        end = time.time() + max(0.05, float(timeout_s))
        while time.time() < end:
            try:
                ptype, resp = self._doip_recv(timeout_s=max(0.05, min(0.5, end - time.time())))
            except socket.timeout:
                continue
            except Exception:
                return None

            # We only care about diagnostic messages and their ACKs.
            if ptype not in (0x8001, 0x8002, 0x8003):
                continue
            if len(resp) < 4:
                continue

            sa, rta = struct.unpack('!HH', resp[:4])
            # We only want responses from ECU -> tester.
            if int(sa) != ta or int(rta) != int(self.tester_addr):
                continue

            # ACK/NACK: correlated but NOT a UDS response.
            if ptype in (0x8002, 0x8003):
                ack_seen += 1
                if ptype == 0x8002:
                    saw_ack_for_request = True
                if getattr(self, 'debug_doip', False):
                    self.log(f"UDS {'ACK' if ptype==0x8002 else 'NACK'}: SA=0x{sa:04X} TA=0x{rta:04X} payload={resp.hex()}")
                continue

            # ptype == 0x8001: actual diagnostic payload; must contain at least 1 byte UDS.
            uds = resp[4:]
            if not uds:
                continue
            
            if getattr(self, 'debug_doip', False):
                self.log(f"UDS RX: SA=0x{sa:04X} TA=0x{rta:04X} uds={uds.hex()}")
            
            # Handle NRC 0x78 (Response Pending) automatically
            if len(uds) >= 3 and uds[0] == 0x7F and uds[2] == 0x78:
                if getattr(self, 'debug_doip', False):
                    self.log('DoIP: Saw NRC 0x78 (Response Pending). Waiting...')
                # Extend timeout and keep listening
                end = time.time() + 5.0
                continue

            return bytes(uds)

        # If we saw only ACKs but no UDS response, treat as no response.
        # Some gateways may ACK delivery but the ECU may not respond (session/security).
        # Exposing this state helps higher-level code to decide whether to retry/reconnect.
        return None

    def _discover_ecus(self, ecu_addresses: Optional[List[int]]) -> List[int]:
        if ecu_addresses:
            candidates = [int(x) & 0xFFFF for x in ecu_addresses]
            self.log(f"DoIP: Using ECU whitelist ({len(candidates)})")
        else:
            # Conservative default range. Fast enough for field use, avoids scanning full 0x0000..0xFFFF.
            # Allow overriding via env for vehicles where ECUs live outside 0x0001..0x00FF.
            # Examples:
            #   DOIP_DISCOVERY_END=0x0FFF
            #   DOIP_DISCOVERY_START=0x0001 DOIP_DISCOVERY_END=0x00FF
            start = 0x0001
            end = 0x00FF
            try:
                s0 = str(os.getenv('DOIP_DISCOVERY_START', '') or '').strip()
                e0 = str(os.getenv('DOIP_DISCOVERY_END', '') or '').strip()
                if s0:
                    start = int(s0, 0) & 0xFFFF
                if e0:
                    end = int(e0, 0) & 0xFFFF
            except Exception:
                start, end = 0x0001, 0x00FF
            if end < start:
                start, end = end, start
            # Bound the default to something sane unless user explicitly widens it.
            candidates = list(range(int(start), int(end) + 1))
            self.log(f"DoIP: Probing logical addresses 0x{start:04X}..0x{end:04X} ({len(candidates)})")

        # Discovery probe strategy
        # Many ECUs do not respond to TesterPresent in their initial state.
        # We therefore support a (conservative) fallback:
        #  - try 0x3E 00
        #  - if no response: request Default Session (0x10 0x01), then retry 0x3E 00
        #  - optionally try a gentle identification DID (0x22 F1 87)
        #
        # Env toggles:
        #  DOIP_DISCOVERY_MODE=safe|aggressive   (default: safe)
        #  DOIP_DISCOVERY_TRY_DID=1              (default: 0)
        #  DOIP_DISCOVERY_DELAY_MS=20            (default: 10)
        try:
            mode = str(os.getenv('DOIP_DISCOVERY_MODE', 'safe') or 'safe').strip().lower()
        except Exception:
            mode = 'safe'
        aggressive = mode in ('aggressive', 'full', '1', 'true', 'yes', 'on')
        try_did = str(os.getenv('DOIP_DISCOVERY_TRY_DID', '0') or '0').strip().lower() in ('1', 'true', 'yes', 'on')
        try:
            delay_ms = int(str(os.getenv('DOIP_DISCOVERY_DELAY_MS', '10') or '10').strip(), 10)
        except Exception:
            delay_ms = 10
        delay_s = max(0.0, min(0.2, float(delay_ms) / 1000.0))

        def _is_present(resp: Optional[bytes]) -> bool:
            if not resp:
                return False
            # Positive 7E 00, or Negative 7F 3E xx.
            if resp[:1] == b'\x7E':
                return True
            if len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x3E:
                return True
            return False

        found: List[int] = []
        for la in candidates:
            la = int(la) & 0xFFFF

            # 1) Fast probe: TesterPresent
            resp = self._uds_transact(la, bytes([0x3E, 0x00]), timeout_s=0.25)
            if _is_present(resp):
                found.append(int(la))
                if delay_s:
                    time.sleep(delay_s)
                continue

            # 2) Safe fallback: Default Session then re-probe
            #    (This is typically less invasive than 0x10 0x03.)
            # In safe mode, we only do this if there's no response at all.
            if resp is None:
                _ = self._uds_transact(la, bytes([0x10, 0x01]), timeout_s=0.45)
                resp2 = self._uds_transact(la, bytes([0x3E, 0x00]), timeout_s=0.25)
                if _is_present(resp2):
                    found.append(int(la))
                    if delay_s:
                        time.sleep(delay_s)
                    continue

            # 3) Optional: try a gentle read DID probe.
            #    In aggressive mode we try it even if we got a negative response on 0x3E.
            if try_did and (aggressive or resp is None):
                did_resp = self._uds_transact(la, bytes([0x22, 0xF1, 0x87]), timeout_s=0.55)
                if did_resp and (did_resp[:1] == b'\x62' or (len(did_resp) >= 3 and did_resp[0] == 0x7F and did_resp[1] == 0x22)):
                    found.append(int(la))
                    if delay_s:
                        time.sleep(delay_s)
                    continue

            if delay_s:
                time.sleep(delay_s)

        found = sorted(set(found))
        self.log(f"DoIP: Discovery complete. ECUs found: {len(found)}")
        return found

    def _decode_dtc_status(self, status: int) -> str:
        flags = []
        if status & 0x01:
            flags.append('TestFailed')
        if status & 0x02:
            flags.append('FailedThisCycle')
        if status & 0x04:
            flags.append('Pending')
        if status & 0x08:
            flags.append('Confirmed')
        if status & 0x10:
            flags.append('NotTestedSinceClear')
        if status & 0x20:
            flags.append('FailedSinceClear')
        if status & 0x40:
            flags.append('NotTestedThisCycle')
        if status & 0x80:
            flags.append('WarningRequested')
        return ', '.join(flags) if flags else 'OK'

    def _dtc_active_from_status(self, status: int) -> bool:
        # IDEX-compatible: only TestFailed (0x01) means currently active.
        return bool(status & 0x01)

    def _parse_uds_dtc_records(self, data: bytes) -> List[DtcItem]:
        if len(data) < 4:
            return []
        recs = data[3:]
        out: List[DtcItem] = []
        stride = 4
        i = 0
        while i + stride <= len(recs):
            dtc_val = (recs[i] << 16) | (recs[i + 1] << 8) | recs[i + 2]
            status = int(recs[i + 3])
            if dtc_val != 0:
                code = _uds_dtc_to_display_code(dtc_val)
                desc = self._decode_dtc_status(status)
                active = self._dtc_active_from_status(status)
                dtc_class = _dtc_classify_idex(status)
                raw = f"{recs[i]:02X} {recs[i+1]:02X} {recs[i+2]:02X} {status:02X}"
                out.append(DtcItem(code=code, uds_dtc=int(dtc_val), status_byte=status, status_desc=desc, active=active, raw=raw, description="", dtc_class=dtc_class))
            i += stride
        return out

    def _try_read_ident_strings(self, target_addr: int) -> str:
        dids = [0xF187, 0xF18A, 0xF189, 0xF18C]
        for did in dids:
            hi = (did >> 8) & 0xFF
            lo = did & 0xFF
            resp = self._uds_transact(int(target_addr), bytes([0x22, hi, lo]), timeout_s=0.6)
            if not resp or len(resp) < 3:
                continue
            if resp[0] != 0x62 or resp[1] != hi or resp[2] != lo:
                continue
            raw = resp[3:]
            try:
                txt = ''.join(chr(b) if 32 <= b < 127 else ' ' for b in raw).strip()
            except Exception:
                txt = ''
            txt = ' '.join(txt.split())
            if txt:
                return txt[:80]
        return ''

    def _read_ecu_full_ident(self, target_addr: int) -> Dict[str, Any]:
        """Read standard ECU identification DIDs (F187–F191) and VIN (F190)."""
        DID_DEFS = [
            (0xF190, 'vin'),
            (0xF187, 'spare_part_number'),
            (0xF191, 'hw_version'),
            (0xF189, 'sw_version'),
            (0xF18A, 'supplier'),
            (0xF18B, 'reprogram_date'),
            (0xF18C, 'serial_number'),
        ]
        result: Dict[str, Any] = {}
        for did, key in DID_DEFS:
            hi = (did >> 8) & 0xFF
            lo = did & 0xFF
            resp = self._uds_transact(int(target_addr), bytes([0x22, hi, lo]), timeout_s=0.8)
            if not resp or len(resp) < 3:
                continue
            if resp[0] != 0x62 or resp[1] != hi or resp[2] != lo:
                continue
            raw = resp[3:]
            try:
                txt = ''.join(chr(b) if 32 <= b < 127 else ' ' for b in raw).strip()
                txt = ' '.join(txt.split())
            except Exception:
                txt = ''
            if txt:
                result[key] = txt
        return result

    def _read_dtcs_best_effort(self, target_addr: int, *, status_mask: int = 0xFF) -> List[DtcItem]:
        """Read DTCs from ECU. status_mask controls which DTCs are returned:
        0xFF = all, 0x01 = only TestFailed (active), 0x09 = active+confirmed, 0x08 = confirmed only.
        """
        mask = int(status_mask) & 0xFF
        # UDS 0x19 0x02 <mask>
        uds = self._uds_transact(int(target_addr), bytes([0x19, 0x02, mask]), timeout_s=1.8)
        if uds and len(uds) >= 3 and uds[0] == 0x59 and uds[1] == 0x02:
            return self._enrich_dtcs_with_context(int(target_addr), self._parse_uds_dtc_records(uds))
        # Some ECUs require a session
        _ = self._uds_transact(int(target_addr), bytes([0x10, 0x03]), timeout_s=0.8)
        uds2 = self._uds_transact(int(target_addr), bytes([0x19, 0x02, mask]), timeout_s=1.8)
        if uds2 and len(uds2) >= 3 and uds2[0] == 0x59 and uds2[1] == 0x02:
            return self._enrich_dtcs_with_context(int(target_addr), self._parse_uds_dtc_records(uds2))
        # Best-effort: expose raw negative response if present
        if uds and uds[:1] == b'\x7f':
            raw = ' '.join(f"{b:02X}" for b in uds)
            return [DtcItem(code='UDS', uds_dtc=None, status_byte=None, status_desc='Negative response (see raw)', active=False, raw=raw, description="")]
        return []

    def _enrich_dtcs_with_context(self, target_addr: int, dtcs: List[DtcItem]) -> List[DtcItem]:
        if not dtcs:
            return []

        # Per-DTC enrichment (snapshot/ext-data) provides timestamp, odometer, etc.
        # Enabled by default; can be disabled with env variable.
        # Env controls:
        #   DOIP_ENRICH_DTC_CONTEXT=0   -> disable enrichment
        #   DOIP_ENRICH_SKIP_ADDRS=0x0002,0x1234 -> skip enrichment for specific logical addresses
        enable = str(os.getenv('DOIP_ENRICH_DTC_CONTEXT', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
        if not enable:
            return list(dtcs)
        self.log(f"DoIP DTC enrichment: probing {len(dtcs)} DTCs on 0x{int(target_addr):04X}")

        skip_raw = str(os.getenv('DOIP_ENRICH_SKIP_ADDRS', '0x0002') or '').strip()
        skip: set = set()
        if skip_raw:
            for part in skip_raw.split(','):
                p = str(part or '').strip()
                if not p:
                    continue
                try:
                    skip.add(int(p, 0) & 0xFFFF)
                except Exception:
                    continue
        if (int(target_addr) & 0xFFFF) in skip:
            return list(dtcs)

        did_index = self.did_index if isinstance(self.did_index, dict) else {}
        out: List[DtcItem] = []
        for d in dtcs:
            if not isinstance(d, DtcItem) or not isinstance(d.uds_dtc, int):
                out.append(d)
                continue

            extra: Dict[str, Any] = {}
            dtc_val = int(d.uds_dtc) & 0xFFFFFF
            dtc_bytes = dtc_val.to_bytes(3, 'big', signed=False)

            # Snapshot records (km + environment): 19 04 <DTC(3)> <record=FF>
            snap_resp = self._uds_transact(int(target_addr), bytes([0x19, 0x04]) + dtc_bytes + bytes([0xFF]), timeout_s=1.6)
            if snap_resp:
                if snap_resp[:1] == b'\x7f':
                    extra['snapshot_nrc_raw'] = _hex_bytes(snap_resp)
                else:
                    parsed = _parse_uds_snapshot_response(bytes(snap_resp), did_index)
                    if parsed:
                        extra['snapshots'] = [parsed]
                    else:
                        extra['snapshot_raw'] = _hex_bytes(snap_resp)

            # Extended data records: 19 06 <DTC(3)> <record=FF>
            ext_resp = self._uds_transact(int(target_addr), bytes([0x19, 0x06]) + dtc_bytes + bytes([0xFF]), timeout_s=1.6)
            if ext_resp:
                if ext_resp[:1] == b'\x7f':
                    extra['extdata_nrc_raw'] = _hex_bytes(ext_resp)
                else:
                    parsed = _parse_uds_extended_data_response(bytes(ext_resp), did_index)
                    if parsed:
                        extra['extended_data'] = [parsed]
                    else:
                        extra['extdata_raw'] = _hex_bytes(ext_resp)

            km = _guess_odometer_km(extra)
            if isinstance(km, int):
                extra['odometer_km'] = int(km)

            ts = _guess_timestamp(extra)
            if isinstance(ts, dict):
                extra['timestamp_guess'] = ts
                if isinstance(ts.get('timestamp_iso'), str):
                    extra['timestamp_iso'] = ts.get('timestamp_iso')
                elif isinstance(ts.get('text'), str):
                    extra['timestamp_text'] = ts.get('text')

            out.append(DtcItem(
                code=d.code,
                uds_dtc=d.uds_dtc,
                status_byte=d.status_byte,
                status_desc=d.status_desc,
                active=d.active,
                raw=d.raw,
                description=d.description,
                dtc_class=d.dtc_class,
                extra=extra,
            ))
        return out

class VirtualCanBus(_BusABC):
    def __init__(self, bus_manager, channel_id):
        if can is None:
            raise ImportError("python-can non installato")
        self.bus_manager = bus_manager
        self.channel_id = channel_id
        self.queue = queue.Queue()
        self.channel_info = f"Virtual Channel {channel_id}"
        
        # Register listener
        self.bus_manager.add_listener(self._on_message)
        
        # Super init
        super().__init__(channel=channel_id, bustype='virtual')

    def _on_message(self, frame):
        if frame.get('channel') == self.channel_id:
            # Convert dict frame to can.Message
            # flags: 4 is canMSG_EXT
            is_extended = (frame.get('flags', 0) & 4) != 0
            msg = can.Message(
                arbitration_id=frame['id'],
                data=bytearray(frame['data']),
                dlc=frame['dlc'],
                is_extended_id=is_extended,
                timestamp=frame.get('timestamp', time.time())
            )
            self.queue.put(msg)

    def send(self, msg, timeout=None):
        ok = self.bus_manager.send_message(
            self.channel_id,
            msg.arbitration_id,
            list(msg.data),
            msg.is_extended_id
        )
        if not ok:
            # Ensure ScanTools does not silently "scan" without actually sending frames.
            try:
                raise can.CanError(f"CAN write failed on channel {self.channel_id}")
            except Exception:
                raise RuntimeError(f"CAN write failed on channel {self.channel_id}")

    def _recv_internal(self, timeout):
        try:
            return self.queue.get(timeout=timeout), False
        except queue.Empty:
            return None, False

    def shutdown(self):
        self.bus_manager.remove_listener(self._on_message)
        try:
            super().shutdown()
        except Exception:
            pass

# ========================================================
# VAG Scanner Logic (Adapted)
# ========================================================
OBD_FUNCTIONAL = 0x7DF
OBD_RESPONSES = range(0x7E8, 0x7F0)

ECU_MAP = {
    0x7E8: "Engine",
    0x7E9: "Transmission",
    0x7EA: "ABS/ESP",
    0x7EB: "Airbag",
    0x7EC: "ECU-C",
    0x7ED: "ECU-D",
    0x7EE: "ECU-E",
    0x7EF: "ECU-F",
}

class VAGScanner:
    def __init__(self, bus_manager, channel_id, emit_log=None, *, enable_file_log: bool = True):
        self.bus_manager = bus_manager
        self.bus = VirtualCanBus(bus_manager, channel_id)
        self.emit_log = emit_log
        self._real_bus_checked = False
        self._live_pid00_mask: Optional[int] = None
        self._live_pid00_source_id: Optional[int] = None
        self._live_warned_pid00 = False
        self._live_warned_speed_unsupported = False
        self._live_warned_rpm_unsupported = False
        self.log_filename = None
        if bool(enable_file_log):
            log_dir = _get_log_dir_default()
            os.makedirs(log_dir, exist_ok=True)
            self.log_filename = os.path.join(log_dir, f"vag_scan_{time.strftime('%Y%m%d_%H%M%S')}.txt")
            self._init_log()

    def _ensure_real_vehicle_bus(self) -> bool:
        """Hard gate to avoid running scans on virtual/mock/simulated CAN.

        This is intentionally strict: if we can't prove the CAN backend is real,
        we fail early with a clear message.
        """
        if self._real_bus_checked:
            return True

        # 1) Refuse when CAN driver is mocked (missing Kvaser drivers).
        try:
            if hasattr(self, 'bus_manager') and self.bus_manager and self.bus_manager.can_driver_is_mock():
                self.log("ERROR: CAN driver is in MOCK mode (no real Kvaser driver). Refusing scan.")
                return False
        except Exception:
            # Can't determine -> be conservative.
            self.log("ERROR: Unable to determine CAN driver mode. Refusing scan.")
            return False

        # 2) Refuse when ECU simulation is enabled.
        try:
            if hasattr(self, 'bus_manager') and self.bus_manager and bool(getattr(self.bus_manager, 'simulate_ecu', False)):
                self.log("ERROR: ECU simulation (KBSM_SIM_ECU) is enabled. Disable it for real vehicle scans.")
                return False
        except Exception:
            pass

        # 3) Active handshake on the bus: send Mode 01 PID 00 and require valid 0x41 response.
        #    This proves: we CAN transmit + we CAN receive from a real ECU.
        try:
            self.log("Verifying real vehicle connectivity (OBD Mode 01 PID 00)…")
            msg = can.Message(
                arbitration_id=OBD_FUNCTIONAL,
                data=[0x02, 0x01, 0x00, 0, 0, 0, 0, 0],
                is_extended_id=False,
            )
            self.bus.send(msg)

            responders = 0
            end_time = time.time() + 1.5
            while time.time() < end_time:
                rx_msg = self.bus.recv(0.1)
                if not rx_msg:
                    continue
                if rx_msg.arbitration_id not in OBD_RESPONSES:
                    continue
                data = list(getattr(rx_msg, 'data', b'') or [])
                # Typical: 06 41 00 XX XX XX XX 00
                if len(data) >= 3 and data[1] == 0x41 and data[2] == 0x00:
                    responders += 1
            if responders <= 0:
                self.log("ERROR: No valid OBD responses received. Not connected to a real vehicle bus (or wrong bitrate/channel).")
                return False

            self._real_bus_checked = True
            self.log(f"Real CAN OK: received {responders} OBD responses.")
            return True
        except Exception as e:
            self.log(f"ERROR: Real bus verification failed: {e}")
            return False

    def _init_log(self):
        try:
            if not self.log_filename:
                return
            os.makedirs(os.path.dirname(self.log_filename), exist_ok=True)
            with open(self.log_filename, "w") as f:
                f.write("===== VAG DTC SCANNER REPORT =====\n")
                f.write(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("==================================\n\n")
        except Exception as e:
            print(f"Log init error: {e}")

    def log(self, msg):
        print(msg)
        if self.emit_log:
            try:
                self.emit_log(msg)
            except Exception:
                pass
        if self.log_filename:
            try:
                with open(self.log_filename, "a") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            except Exception:
                pass

    def scan_obd(self):
        # Ensure we're on a real vehicle before scanning.
        if not self._ensure_real_vehicle_bus():
            return

        self.log("Starting OBD/UDS ECU Scan...")

        # Prefer strict PDX-driven addressing when available.
        comm_index = _load_active_pdx_comm_index()
        pdx_entries: List[Dict[str, Any]] = []
        try:
            entries = comm_index.get('ecus') if isinstance(comm_index, dict) else None
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    can_info = e.get('can') if isinstance(e.get('can'), dict) else {}
                    tx_id = can_info.get('phys_req_id')
                    rx_id = can_info.get('resp_id')
                    if not isinstance(tx_id, int) or not isinstance(rx_id, int):
                        continue
                    pdx_entries.append({
                        'tx_id': int(tx_id) & 0x1FFFFFFF,
                        'rx_id': int(rx_id) & 0x1FFFFFFF,
                        'is_extended_id': bool(can_info.get('is_extended_id') is True),
                        'name': str(e.get('long_name') or e.get('short_name') or '').strip(),
                        'protocol': str(e.get('protocol_snref') or '').strip(),
                        'source_odx': str(e.get('source_odx') or '').strip(),
                    })
        except Exception:
            pdx_entries = []

        responders_pairs: List[Dict[str, Any]] = []
        if pdx_entries:
            # Only scan UDS-on-CAN entries from PDX (avoid inventing protocol selection).
            uds_entries = [e for e in pdx_entries if str(e.get('protocol') or '').strip().upper() == 'PR_UDSONCAN']
            if not uds_entries:
                uds_entries = pdx_entries

            uds_entries.sort(key=lambda x: (int(x.get('rx_id') or 0), int(x.get('tx_id') or 0)))
            self.log(f"PDX comm index ECUs (CAN): {len(uds_entries)}")
            responders_pairs = uds_entries
        else:
            # Fallback: use functional discovery (Mode 01 PID 00) to list responding ECUs,
            # then query each ECU physically (7E0..7E7 -> 7E8..7EF) via ISO-TP.
            msg = can.Message(
                arbitration_id=OBD_FUNCTIONAL,
                data=[0x02, 0x01, 0x00, 0, 0, 0, 0, 0],
                is_extended_id=False,
            )
            self.bus.send(msg)

            end_time = time.time() + 2.0
            responders: List[int] = []
            seen = set()
            while time.time() < end_time:
                rx_msg = self.bus.recv(0.1)
                if rx_msg and rx_msg.arbitration_id in OBD_RESPONSES:
                    rid = int(rx_msg.arbitration_id)
                    if rid not in seen:
                        seen.add(rid)
                        responders.append(rid)
                        ecu_name = ECU_MAP.get(rid, f"Unknown (0x{rid:03X})")
                        self.log(f"Found ECU: {ecu_name}")

            if not responders:
                self.log("No OBD/UDS ECUs found.")
                return

            responders_pairs = [
                {'tx_id': int(rx_id) - 8, 'rx_id': int(rx_id), 'is_extended_id': False, 'name': ECU_MAP.get(int(rx_id), f"ECU 0x{int(rx_id):03X}"), 'protocol': ''}
                for rx_id in responders
            ]

        did_index = _load_active_pdx_did_index()
        dtc_map = _load_active_pdx_dtc_map()
        dtc_by_file = _load_active_pdx_dtc_by_file()

        # Read DTCs using best-effort lookup (UDS 0x19 first; fallback to OBD).
        for ep in responders_pairs:
            try:
                tx_id = int(ep.get('tx_id'))
                rx_id = int(ep.get('rx_id'))
                is_ext = bool(ep.get('is_extended_id') is True)
            except Exception:
                continue
            ecu_name = str(ep.get('name') or '').strip() or ECU_MAP.get(rx_id, f"ECU 0x{rx_id:03X}")
            ecu_source_odx = str(ep.get('source_odx') or '').strip()
            self.log(f"Scanning DTCs for {ecu_name} (TX 0x{tx_id:X} -> RX 0x{rx_id:X}{' ext' if is_ext else ''})...")
            dtcs = self._read_dtcs_best_effort(tx_id, int(rx_id), did_index=did_index, is_extended_id=is_ext)
            if not dtcs:
                self.log("  No DTCs reported (or not supported).")
                continue
            shown = 0
            for d in dtcs[:80]:
                # If the reader produced a diagnostic sentinel (e.g., negative response / no response)
                # report it clearly instead of treating it as a real DTC.
                try:
                    if isinstance(d, DtcItem) and (d.code in {'UDS', 'UDS19', 'OBD', 'KWP'}) and not d.uds_dtc:
                        msg = (d.status_desc or '').strip() or 'DTC read not available'
                        self.log(f"  [{d.code}] {msg}")
                        shown += 1
                        continue
                except Exception:
                    pass
                # Prefer per-DTC enriched description, otherwise use ECU-aware PDX lookup.
                desc = (d.description or '').strip()
                if not desc:
                    try:
                        if ecu_source_odx and dtc_by_file:
                            desc = _dtc_description_for_ecu(str(d.code), dtc_map, dtc_by_file, ecu_source_odx)
                        else:
                            desc = _dtc_description(str(d.code), dtc_map)
                    except Exception:
                        desc = ''
                dtc_cls = (getattr(d, 'dtc_class', '') or '').upper() or ('ACTIVE' if d.active else 'PASSIVE')
                self.log(f"  {d.code} | {dtc_cls} | {d.status_desc} | {desc}")
                shown += 1
            if len(dtcs) > shown:
                self.log(f"  … {len(dtcs) - shown} more")

    def _send_pid_request(self, pid: int):
        msg = can.Message(
            arbitration_id=OBD_FUNCTIONAL,
            data=[0x02, 0x01, pid, 0x00, 0x00, 0x00, 0x00, 0x00],
            is_extended_id=False,
        )
        self.bus.send(msg)

    def _parse_pid_response(self, msg, pid: int):
        try:
            data = msg.data
            if data is None or len(data) < 4:
                return None
            # OBD-II Mode 01 response: 0x41
            if data[1] != 0x41 or data[2] != pid:
                return None

            if pid == 0x0C:
                # Engine RPM: ((A*256)+B)/4
                a = data[3]
                b = data[4] if len(data) > 4 else 0
                return ((a * 256) + b) / 4.0

            if pid == 0x0D:
                # Vehicle speed (km/h): A
                return float(data[3])

            if pid == 0x01:
                # Monitor status since DTCs:
                # A: MIL status (bit7) + number of stored DTCs (bits0..6)
                a = int(data[3]) & 0xFF
                return {
                    'mil_on': bool(a & 0x80),
                    'dtc_count': int(a & 0x7F),
                }

            return None
        except Exception:
            return None

    def _live_pid_supported(self, pid: int) -> Optional[bool]:
        """Return whether a Mode 01 PID is supported (from PID 00 bitmask).

        Returns:
        - True/False when mask known
        - None when mask is unknown
        """
        try:
            if self._live_pid00_mask is None:
                return None
            p = int(pid) & 0xFF
            if p <= 0 or p > 0x20:
                return None
            bit = 1 << (32 - p)
            return bool(int(self._live_pid00_mask) & bit)
        except Exception:
            return None

    def _ensure_live_pid00_mask(self, timeout_s: float = 0.35) -> None:
        if self._live_pid00_mask is not None:
            return
        try:
            self._send_pid_request(0x00)
        except Exception:
            return

        end = time.time() + max(0.05, float(timeout_s))
        while time.time() < end:
            rx = self.bus.recv(0.05)
            if not rx:
                continue
            if rx.arbitration_id not in OBD_RESPONSES:
                continue
            data = getattr(rx, 'data', None)
            if data is None or len(data) < 7:
                continue
            # 41 00 A B C D
            if data[1] != 0x41 or data[2] != 0x00:
                continue
            a = int(data[3]) & 0xFF
            b = int(data[4]) & 0xFF
            c = int(data[5]) & 0xFF
            d = int(data[6]) & 0xFF
            self._live_pid00_mask = (a << 24) | (b << 16) | (c << 8) | d
            self._live_pid00_source_id = int(rx.arbitration_id)
            return

    def read_live_once(self, timeout_s: float = 0.25):
        """Poll RPM, Speed and MIL once.

        Returns (rpm, speed_kph, mil_on, mil_dtc_count). Values may be None.
        """
        rpm = None
        speed = None
        mil_on = None
        mil_dtc_count = None

        # Learn supported PIDs once (best-effort). This helps explain why some signals
        # can be permanently unavailable on certain vehicles.
        self._ensure_live_pid00_mask(timeout_s=min(0.5, float(timeout_s)))
        if self._live_pid00_mask is None and not self._live_warned_pid00:
            self._live_warned_pid00 = True
            msg = "Live Data: PID 00 support mask not received yet (vehicle may still respond to specific PIDs)."
            self.log(msg)
            print(f"DEBUG: {msg}")

        speed_supported = self._live_pid_supported(0x0D)
        rpm_supported = self._live_pid_supported(0x0C)
        # DEBUG force logging of support status
        print(f"DEBUG: support check 0x0D={speed_supported}, 0x0C={rpm_supported}, mask={self._live_pid00_mask}")

        if speed_supported is False and not self._live_warned_speed_unsupported:
            self._live_warned_speed_unsupported = True
            src = f"0x{int(self._live_pid00_source_id):03X}" if self._live_pid00_source_id is not None else "unknown"
            self.log(f"Live Data: PID 0x0D (vehicle speed) NOT supported per PID 00 mask (source {src}).")
        if rpm_supported is False and not self._live_warned_rpm_unsupported:
            self._live_warned_rpm_unsupported = True
            src = f"0x{int(self._live_pid00_source_id):03X}" if self._live_pid00_source_id is not None else "unknown"
            self.log(f"Live Data: PID 0x0C (RPM) NOT supported per PID 00 mask (source {src}).")

        # Serialized PID requests to avoid overwriting ECU buffer or confusion
        def _fetch(pid, wait_s=0.08):
            self._send_pid_request(pid)
            end_t = time.time() + wait_s
            while time.time() < end_t:
                # small blocking recv
                rx = self.bus.recv(max(0.01, end_t - time.time()))
                if not rx:
                    continue
                if rx.arbitration_id not in OBD_RESPONSES:
                    continue
                val = self._parse_pid_response(rx, pid)
                if val is not None:
                    return val
            return None

        if rpm_supported is not False:
            rpm = _fetch(0x0C)
        
        if speed_supported is not False:
            speed = _fetch(0x0D)

        mil_val = _fetch(0x01)
        if isinstance(mil_val, dict):
            mil_on = bool(mil_val.get('mil_on'))
            mil_dtc_count = int(mil_val.get('dtc_count') or 0)
        
        print(f"DEBUG: returning rpm={rpm}, speed={speed}, mil={mil_on}")
        return rpm, speed, mil_on, mil_dtc_count

    def discovery(self):
        """Discovery scan using TesterPresent (0x3E) on common VAG range."""
        global _SCANTOOLS_DISCOVERY_IN_PROGRESS
        with _SCANTOOLS_DISCOVERY_LOCK:
            if _SCANTOOLS_DISCOVERY_IN_PROGRESS:
                self.log("Discovery already running; skipping overlapping request")
                return
            _SCANTOOLS_DISCOVERY_IN_PROGRESS = True

        found = 0
        try:
            self.log("=== Discovery Scan (Tester Present) ===")
            comm_index = _load_active_pdx_comm_index()
            pdx_entries: List[Dict[str, Any]] = []
            try:
                entries = comm_index.get('ecus') if isinstance(comm_index, dict) else None
                if isinstance(entries, list):
                    for e in entries:
                        if not isinstance(e, dict):
                            continue
                        can_info = e.get('can') if isinstance(e.get('can'), dict) else {}
                        tx_id = can_info.get('phys_req_id')
                        rx_id = can_info.get('resp_id')
                        if not isinstance(tx_id, int) or not isinstance(rx_id, int):
                            continue
                        pdx_entries.append({
                            'tx_id': int(tx_id) & 0x1FFFFFFF,
                            'rx_id': int(rx_id) & 0x1FFFFFFF,
                            'is_extended_id': bool(can_info.get('is_extended_id') is True),
                            'name': str(e.get('long_name') or e.get('short_name') or '').strip(),
                            'protocol': str(e.get('protocol_snref') or '').strip(),
                        })
            except Exception:
                pdx_entries = []

            targets: List[Dict[str, Any]] = []
            if pdx_entries:
                uds_entries = [e for e in pdx_entries if str(e.get('protocol') or '').strip().upper() == 'PR_UDSONCAN']
                targets = uds_entries if uds_entries else pdx_entries
                targets.sort(key=lambda x: (int(x.get('rx_id') or 0), int(x.get('tx_id') or 0)))
                self.log(f"PDX discovery targets (UDS-on-CAN): {len(targets)}")
            else:
                # Fallback heuristic: probe the classic 0x7E0..0x7E7 request range.
                base_tx, _base_rx = _get_uds_on_can_base_ids()
                tx_ids = list(range(0x7E0, 0x7E8)) if not (0x7E0 <= base_tx <= 0x7E7) else list(range(0x7E0, 0x7E8))
                targets = [{'tx_id': tx, 'rx_id': None, 'is_extended_id': False, 'name': '', 'protocol': ''} for tx in tx_ids]

            for t in targets:
                try:
                    tx_id = int(t.get('tx_id'))
                except Exception:
                    continue
                exp_rx = t.get('rx_id')
                exp_rx_id = int(exp_rx) if isinstance(exp_rx, int) else None
                is_ext = bool(t.get('is_extended_id') is True)
                msg = can.Message(
                    arbitration_id=int(tx_id),
                    data=[0x02, 0x3E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                    is_extended_id=is_ext,
                )
                try:
                    self.bus.send(msg)
                except Exception:
                    continue

                time.sleep(0.005)
                end = time.time() + 0.06
                while time.time() < end:
                    rx = self.bus.recv(0.01)
                    if not rx:
                        continue
                    if exp_rx_id is not None and int(rx.arbitration_id) != int(exp_rx_id):
                        continue
                    data = getattr(rx, 'data', None)
                    if not data or len(data) < 2:
                        continue
                    is_resp = False
                    if data[1] == 0x7E:
                        is_resp = True
                    if len(data) >= 3 and data[1] == 0x7F and data[2] == 0x3E:
                        is_resp = True
                    if is_resp:
                        found += 1
                        nm = str(t.get('name') or '').strip()
                        tag = (f" {nm}" if nm else "")
                        self.log(f"Found ECU:{tag} TX 0x{tx_id:X} -> RX 0x{int(rx.arbitration_id):X}{' ext' if is_ext else ''}")
                        break

                time.sleep(0.005)

            self.log(f"Discovery complete. Nodes found: {found}")
        finally:
            with _SCANTOOLS_DISCOVERY_LOCK:
                _SCANTOOLS_DISCOVERY_IN_PROGRESS = False

    def scan_mode06(self):
        """Run OBD Mode 06 decoding on responding ECUs.

        Uses functional Mode 01 PID 00 (0x7DF) to discover responders, then
        queries Mode 06 via the existing ISO-TP transact helper.
        """
        # Ensure we're on a real vehicle before scanning.
        if not self._ensure_real_vehicle_bus():
            return

        self.log("=== OBD Mode 06 Scan ===")

        # Discover ECUs (functional request).
        try:
            msg = can.Message(
                arbitration_id=OBD_FUNCTIONAL,
                data=[0x02, 0x01, 0x00, 0, 0, 0, 0, 0],
                is_extended_id=False,
            )
            self.bus.send(msg)
        except Exception as e:
            self.log(f"Mode 06: failed to send discovery: {e}")
            return

        responders = set()
        end_time = time.time() + 1.5
        while time.time() < end_time:
            rx_msg = self.bus.recv(0.1)
            if rx_msg and rx_msg.arbitration_id in OBD_RESPONSES:
                responders.add(int(rx_msg.arbitration_id))

        if not responders:
            self.log("Mode 06: no OBD responders found.")
            return

        for rx_id in sorted(responders):
            tx_id = int(rx_id) - 8
            ecu_name = ECU_MAP.get(rx_id, f"Unknown ({hex(rx_id)})")
            self.log(f"Mode 06: {ecu_name} (TX 0x{tx_id:03X} -> RX 0x{rx_id:03X})")
            try:
                res = self._obd_read_mode06(tx_id, rx_id)
            except Exception as e:
                self.log(f"  Mode 06 failed: {e}")
                continue

            note = str((res or {}).get('note') or '').strip()
            if note:
                self.log(f"  {note}")

            tests = (res or {}).get('tests')
            if not isinstance(tests, list) or not tests:
                continue

            failing = [t for t in tests if isinstance(t, dict) and t.get('pass') is False]
            if failing:
                self.log(f"  Failing tests: {len(failing)}")
                for t in failing[:10]:
                    self.log(
                        f"    TID {t.get('tid')} CID {t.get('cid')}: value={t.get('value')} min={t.get('min')} max={t.get('max')}"
                    )
            else:
                self.log(f"  Tests decoded: {len(tests)} (no fails)")

    def vag_scan_report(self):
        """Advanced VAG scan (UDS/KWP/OBD) and generate an HTML report.

        - Discovers ECUs on typical VAG range.
        - Reads DTCs with ISO-TP when available (UDS 0x19; fallback to KWP 0x18, OBD).
        - Produces a self-contained HTML report with per-ECU sections.

        This is a NEW feature path and does not alter existing ScanTools behavior.
        """
        if can is None:
            raise ImportError("python-can non installato (pip install python-can)")
        if isotp is None:
            raise ImportError("can-isotp non installato (pip install can-isotp)")

        self.log("=== VAG OBD/UDS Scan (HTML report) ===")

        comm_index = _load_active_pdx_comm_index()
        pdx_path = _get_active_pdx_path()
        pdx_filename = os.path.basename(pdx_path) if pdx_path else ''

        did_index = _load_active_pdx_did_index()
        self.log(f"Active PDX DID index: {len(did_index)} entries")

        # Prefer strict PDX-driven addressing when available; fall back to heuristic discovery.
        pdx_ecus: List[Dict[str, Any]] = []
        try:
            entries = comm_index.get('ecus') if isinstance(comm_index, dict) else None
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    can_info = e.get('can') if isinstance(e.get('can'), dict) else {}
                    tx_id = can_info.get('phys_req_id')
                    rx_id = can_info.get('resp_id')
                    if not isinstance(tx_id, int) or not isinstance(rx_id, int):
                        continue
                    pdx_ecus.append({
                        'tx_id': int(tx_id) & 0x1FFFFFFF,
                        'rx_id': int(rx_id) & 0x1FFFFFFF,
                        'is_extended_id': bool(can_info.get('is_extended_id') is True),
                        'long_name': str(e.get('long_name') or '').strip(),
                        'short_name': str(e.get('short_name') or '').strip(),
                        'protocol_snref': str(e.get('protocol_snref') or '').strip(),
                    })
        except Exception:
            pdx_ecus = []

        ecus: List[Tuple[int, int]] = []
        if pdx_ecus:
            # Keep stable order.
            pdx_ecus.sort(key=lambda x: (int(x.get('rx_id') or 0), int(x.get('tx_id') or 0)))
            ecus = [(int(e['tx_id']), int(e['rx_id'])) for e in pdx_ecus]
            self.log(f"PDX comm index ECUs (CAN): {len(ecus)}")
        else:
            ecus = self._discover_ecus_for_report()
            if not ecus:
                self.log("No ECUs discovered. Ensure Bus System is running and vehicle is connected.")
                return

        reports: List[EcuReport] = []
        for tx_id, rx_id in ecus:
            is_ext = False
            base_name = self._ecu_name_guess(rx_id)
            if pdx_ecus:
                try:
                    meta = next((m for m in pdx_ecus if int(m.get('tx_id')) == int(tx_id) and int(m.get('rx_id')) == int(rx_id)), None)
                except Exception:
                    meta = None
                if isinstance(meta, dict):
                    is_ext = bool(meta.get('is_extended_id') is True)
                    nm = str(meta.get('long_name') or meta.get('short_name') or '').strip()
                    if nm:
                        base_name = nm

            name = base_name
            ident = self._try_read_ident_strings(tx_id, rx_id, is_extended_id=is_ext)
            if ident:
                name = f"{name} ({ident})"

            dtcs = self._read_dtcs_best_effort(tx_id, rx_id, did_index=did_index, is_extended_id=is_ext)
            obd: Dict[str, Any] = {}
            try:
                obd['mode0A_dtcs'] = self._obd_read_mode0a(tx_id, rx_id, is_extended_id=is_ext)
            except Exception:
                obd['mode0A_dtcs'] = []
            try:
                obd['mode06'] = self._obd_read_mode06(tx_id, rx_id, is_extended_id=is_ext)
            except Exception:
                obd['mode06'] = {'note': 'Mode 06 failed', 'raw_hex': ''}
            active = sum(1 for d in dtcs if d.active)
            passive = sum(1 for d in dtcs if (not d.active))

            # Human-friendly console summary (so the user can see which errors were found).
            # Keep it short to avoid flooding the log.
            codes: List[str] = []
            notes: List[str] = []
            for d in (dtcs or []):
                try:
                    if isinstance(d, DtcItem) and (d.code in {'UDS', 'OBD', 'KWP'}) and not d.uds_dtc:
                        msg = (d.status_desc or '').strip()
                        if msg:
                            notes.append(msg)
                        continue
                    c = str(getattr(d, 'code', '') or '').strip()
                    if c and c not in codes:
                        codes.append(c)
                except Exception:
                    continue

            self.log(f"ECU {name}: active={active} passive={passive} total={len(dtcs)}")
            if codes:
                self.log(f"  DTCs ({len(codes)} unique): {', '.join(codes[:20])}{' …' if len(codes) > 20 else ''}")
            if notes:
                self.log(f"  Notes: {notes[0]}")
            reports.append(EcuReport(tx_id=int(tx_id), rx_id=int(rx_id), name=name, dtcs=dtcs, obd=obd))

        html_name = f"vag_obd_scan_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
        log_dir = _get_log_dir_default()
        os.makedirs(log_dir, exist_ok=True)
        html_path = os.path.join(log_dir, html_name)
        dtc_map = _load_active_pdx_dtc_map()
        self.log(f"Active PDX DTC index: {len(dtc_map)} entries")
        scan_src = 'PDX comm index' if pdx_ecus else 'Heuristic discovery'
        subtitle2 = 'Transport: CAN (ISO-TP best-effort) | Addressing: PDX comm index' if pdx_ecus else 'Transport: CAN (ISO-TP best-effort)'
        _write_vag_html_report(
            html_path,
            reports,
            title='VAG OBD/UDS Scan Report',
            subtitle=subtitle2,
            dtc_map=dtc_map,
            pdx_info={'pdx_filename': pdx_filename, 'scan_source': scan_src, 'comm_ecu_count': (len(pdx_ecus) if pdx_ecus else None)},
            comm_index=comm_index if isinstance(comm_index, dict) and comm_index else None,
        )
        self.log(f"HTML report saved: {html_name}")
        self.log(f"Download: /api/logs/{html_name}")

        xml_name = html_name.replace('.html', '.diagra.xml')
        csv_name = html_name.replace('.html', '.diagra.csv')
        try:
            _write_diagra_xml_report(
                os.path.join(log_dir, xml_name),
                reports,
                title='VAG OBD/UDS Scan Report',
                subtitle='Transport: CAN (ISO-TP best-effort)',
                dtc_map=dtc_map,
            )
            _write_diagra_csv_report(
                os.path.join(log_dir, csv_name),
                reports,
                dtc_map=dtc_map,
            )
            self.log(f"Diagra export saved: {xml_name}")
            self.log(f"Download: /api/logs/{xml_name}")
            self.log(f"Diagra export saved: {csv_name}")
            self.log(f"Download: /api/logs/{csv_name}")
        except Exception as e:
            self.log(f"Diagra export failed: {e}")

    def _discover_ecus_for_report(self) -> List[Tuple[int, int]]:
        """Return list of (tx_id, rx_id) discovered on the CAN bus."""
        found: List[Tuple[int, int]] = []
        seen = set()

        # 1) OBD functional ping to collect classic OBD responders (0x7E8..0x7EF)
        try:
            msg = can.Message(arbitration_id=OBD_FUNCTIONAL, data=[0x02, 0x01, 0x00, 0, 0, 0, 0, 0], is_extended_id=False)
            self.bus.send(msg)
            end = time.time() + 1.2
            while time.time() < end:
                rx = self.bus.recv(0.05)
                if rx and rx.arbitration_id in OBD_RESPONSES:
                    rx_id = int(rx.arbitration_id)
                    tx_id = int(rx_id - 8)
                    if (tx_id, rx_id) not in seen:
                        found.append((tx_id, rx_id))
                        seen.add((tx_id, rx_id))
        except Exception:
            pass

        # 2) UDS physical probing (TesterPresent) using ISO-15765 physical request IDs.
        base_tx, _base_rx = _get_uds_on_can_base_ids()
        if 0x7E0 <= base_tx <= 0x7E7:
            tx_ids = list(range(0x7E0, 0x7E8))
        else:
            tx_ids = list(range(0x7E0, 0x7E8))

        for tx_id in tx_ids:
            try:
                msg = can.Message(
                    arbitration_id=tx_id,
                    data=[0x02, 0x3E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
                    is_extended_id=False,
                )
                self.bus.send(msg)
            except Exception:
                continue

            end = time.time() + 0.05
            while time.time() < end:
                rx = self.bus.recv(0.01)
                if not rx:
                    continue
                data = getattr(rx, 'data', None)
                if not data or len(data) < 2:
                    continue
                is_resp = False
                if data[1] == 0x7E:
                    is_resp = True
                if len(data) >= 3 and data[1] == 0x7F and data[2] == 0x3E:
                    is_resp = True
                if is_resp:
                    rx_id = int(rx.arbitration_id)
                    pair = (int(tx_id), rx_id)
                    if pair not in seen:
                        found.append(pair)
                        seen.add(pair)
                    break

        # Stable order for report
        found.sort(key=lambda x: (x[1], x[0]))
        self.log(f"Discovery: {len(found)} ECUs")
        return found

    def _ecu_name_guess(self, rx_id: int) -> str:
        if rx_id in ECU_MAP:
            return str(ECU_MAP[rx_id])
        return f"ECU 0x{int(rx_id):03X}"

    def _decode_dtc_status(self, status: int) -> str:
        flags = []
        if status & 0x01:
            flags.append('TestFailed')
        if status & 0x02:
            flags.append('FailedThisCycle')
        if status & 0x04:
            flags.append('Pending')
        if status & 0x08:
            flags.append('Confirmed')
        if status & 0x10:
            flags.append('NotTestedSinceClear')
        if status & 0x20:
            flags.append('FailedSinceClear')
        if status & 0x40:
            flags.append('NotTestedThisCycle')
        if status & 0x80:
            flags.append('WarningRequested')
        return ', '.join(flags) if flags else 'OK'

    def _dtc_active_from_status(self, status: int) -> bool:
        # IDEX-compatible: only TestFailed (0x01) means currently active.
        return bool(status & 0x01)

    def _isotp_transact(
        self,
        tx_id: int,
        rx_id: int,
        payload: bytes,
        timeout_s: float = 1.5,
        *,
        is_extended_id: bool = False,
    ) -> Optional[bytes]:
        """ISO-TP transaction over python-can bus using can-isotp stack."""
        if isotp is None:
            return None
        try:
            mode = isotp.AddressingMode.Normal_29bits if bool(is_extended_id) else isotp.AddressingMode.Normal_11bits
            addr = isotp.Address(mode, txid=int(tx_id), rxid=int(rx_id))
            stack = isotp.CanStack(
                bus=self.bus,
                address=addr,
                params={
                    'stmin': 0,
                    'blocksize': 0,
                    'wftmax': 0,
                    'tx_padding': 0x00,
                    'rx_flowcontrol_timeout': int(max(500, timeout_s * 1000)),
                    'rx_consecutive_frame_timeout': int(max(500, timeout_s * 1000)),
                },
            )
            stack.send(payload)
            start = time.time()
            while (time.time() - start) < timeout_s:
                stack.process()
                if stack.available():
                    try:
                        data = stack.recv()
                    except Exception:
                        data = None
                    if data:
                        return bytes(data)
                time.sleep(0.002)
            return None
        except Exception:
            return None

    def _try_read_ident_strings(self, tx_id: int, rx_id: int, *, is_extended_id: bool = False) -> str:
        """Try a few common UDS DIDs and return the first printable string."""
        # Common IDs that often return ASCII-ish info on many ECUs
        dids = [0xF187, 0xF18A, 0xF189, 0xF18C]
        for did in dids:
            hi = (did >> 8) & 0xFF
            lo = did & 0xFF
            resp = self._isotp_transact(tx_id, rx_id, bytes([0x22, hi, lo]), timeout_s=1.2, is_extended_id=is_extended_id)
            if not resp or len(resp) < 3:
                continue
            if resp[0] != 0x62 or resp[1] != hi or resp[2] != lo:
                continue
            raw = resp[3:]
            try:
                txt = ''.join(chr(b) if 32 <= b < 127 else ' ' for b in raw).strip()
            except Exception:
                txt = ''
            txt = ' '.join(txt.split())
            if txt:
                return txt[:80]
        return ''

    def _read_dtcs_best_effort(
        self,
        tx_id: int,
        rx_id: int,
        *,
        did_index: Optional[Dict[int, Dict[str, Any]]] = None,
        is_extended_id: bool = False,
    ) -> List[DtcItem]:
        dtcs: List[DtcItem] = []

        def _is_response_pending(resp: Optional[bytes], *, service: int) -> bool:
            try:
                if not resp or len(resp) < 3:
                    return False
                return resp[0] == 0x7F and resp[1] == (service & 0xFF) and resp[2] == 0x78
            except Exception:
                return False

        def _transact_with_pending(req: bytes, *, service: int, timeout_s: float, total_wait_s: float = 15.0) -> Optional[bytes]:
            """ISO-TP transact, but if ECU replies ResponsePending (7F <svc> 78), keep waiting/retrying.

            Many VAG ECUs respond with NRC 0x78 before sending the eventual positive response.
            """
            start = time.time()
            resp = self._isotp_transact(tx_id, rx_id, req, timeout_s=timeout_s, is_extended_id=is_extended_id)
            if not _is_response_pending(resp, service=service):
                return resp

            # Keep trying until total_wait_s; first tries are short, then slightly longer.
            pending_logged = False
            while time.time() - start < total_wait_s:
                # Log only once to avoid spamming the scan log.
                if not pending_logged:
                    try:
                        dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None,
                                            status_desc=f"ResponsePending (NRC 0x78) for service 0x{service:02X}; waiting…",
                                            active=False, raw=_hex_bytes(resp or b''), description=""))
                    except Exception:
                        pass
                    pending_logged = True
                time.sleep(0.25)
                remaining = max(0.3, min(1.2, total_wait_s - (time.time() - start)))
                resp = self._isotp_transact(tx_id, rx_id, req, timeout_s=remaining, is_extended_id=is_extended_id)
                if resp and not _is_response_pending(resp, service=service):
                    return resp
            return resp

        def _nrc_hint(resp: bytes) -> str:
            try:
                if resp and len(resp) >= 3 and resp[0] == 0x7F:
                    svc = resp[1]
                    nrc = resp[2]
                    return f"NRC 0x{nrc:02X} for service 0x{svc:02X} (raw={_hex_bytes(resp)})"
            except Exception:
                pass
            return ''

        def _try_enter_extended_session() -> bool:
            """Best-effort entry into extended diagnostic session (0x10 0x03).

            Some ECUs only answer ReadDTCInformation in extended session.
            """
            resp = self._isotp_transact(tx_id, rx_id, bytes([0x10, 0x03]), timeout_s=1.2, is_extended_id=is_extended_id)
            # Positive response: 50 03 ...
            return bool(resp and len(resp) >= 2 and resp[0] == 0x50 and resp[1] == 0x03)

        def _try_security_access_if_needed() -> None:
            """Best-effort security access.

            If ECU requires 0x27, we can't compute keys here. But requesting the
            seed is useful to detect/label 'requires security' instead of
            pretending there are no DTCs.
            """
            # Request seed for level 1 (0x01) by default.
            resp = self._isotp_transact(tx_id, rx_id, bytes([0x27, 0x01]), timeout_s=1.2, is_extended_id=is_extended_id)
            # Positive: 67 01 <seed...> ; Negative: 7F 27 xx
            return

        # Try UDS 0x19 first (ReportDTCByStatusMask = 0x02)
        uds = _transact_with_pending(bytes([0x19, 0x02, 0xFF]), service=0x19, timeout_s=2.0, total_wait_s=15.0)
        if uds and len(uds) >= 3 and uds[0] == 0x59 and uds[1] == 0x02:
            return self._enrich_dtcs_with_context_isotp(
                tx_id,
                rx_id,
                self._parse_uds_dtc_records(uds),
                did_index,
                is_extended_id=is_extended_id,
            )
        if uds and len(uds) >= 3 and uds[0] == 0x7F and uds[1] == 0x19:
            # Keep going (session/security/fallback), but do NOT treat as 'no DTCs'.
            hint = _nrc_hint(bytes(uds))
            if hint:
                dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc=hint, active=False, raw=_hex_bytes(uds), description=""))

        if not uds:
            # No response at all (timeout). Keep going with session/fallbacks but remember it.
            dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc='No response to UDS 0x19 0x02 (timeout)', active=False, raw='', description=""))

        # SAFETY: We DISABLE automatic Extended Session (0x10 0x03) and Security Access (0x27)
        # because this often causes dashboard error lights ("Christmas tree") and can put
        # drivetrain ECUs (like Transmission) into limp mode/recovery if done while driving.
        
        # entered = _try_enter_extended_session()
        # if entered:
        #     # Some ECUs require security; probe seed (best-effort).
        #     _try_security_access_if_needed()

        # uds2 = _transact_with_pending(bytes([0x19, 0x02, 0xFF]), service=0x19, timeout_s=2.2, total_wait_s=15.0)
        # if uds2 and len(uds2) >= 3 and uds2[0] == 0x59 and uds2[1] == 0x02:
        #     return self._enrich_dtcs_with_context_isotp(
        #         tx_id,
        #         rx_id,
        #         self._parse_uds_dtc_records(uds2),
        #         did_index,
        #         is_extended_id=is_extended_id,
        #     )
        # if uds2 and len(uds2) >= 3 and uds2[0] == 0x7F and uds2[1] == 0x19:
        #     hint = _nrc_hint(bytes(uds2))
        #     if hint:
        #         dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc=hint, active=False, raw=_hex_bytes(uds2), description=""))

        uds2 = None # Skip second attempt in extended session

        if not uds2:
            dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc='No response to UDS 0x19 after session switch (timeout)', active=False, raw='', description=""))

        # Fallback: UDS 0x19 subfunction 0x0A (reportSupportedDTC) on some ECUs
        uds3 = _transact_with_pending(bytes([0x19, 0x0A]), service=0x19, timeout_s=2.2, total_wait_s=15.0)
        if uds3 and len(uds3) >= 3 and uds3[0] == 0x59 and uds3[1] == 0x0A:
            # This response format varies; we don't parse to DTC list reliably here.
            raw = _hex_bytes(uds3)
            dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc='SupportedDTC returned (see raw)', active=False, raw=raw, description=""))
            return dtcs
        if uds3 and len(uds3) >= 3 and uds3[0] == 0x7F and uds3[1] == 0x19:
            hint = _nrc_hint(bytes(uds3))
            if hint:
                dtcs.append(DtcItem(code='UDS19', uds_dtc=None, status_byte=None, status_desc=hint, active=False, raw=_hex_bytes(uds3), description=""))

        # KWP 0x18 (minimal)
        kwp = self._isotp_transact(tx_id, rx_id, bytes([0x18, 0x00, 0xFF, 0x00]), timeout_s=2.0, is_extended_id=is_extended_id)
        if kwp and len(kwp) >= 2 and kwp[0] == 0x58:
            # We keep this minimal; structure varies by ECU.
            raw = ' '.join(f"{b:02X}" for b in kwp)
            dtcs.append(DtcItem(code='KWP', uds_dtc=None, status_byte=None, status_desc='KWP response (see raw)', active=False, raw=raw))
            return dtcs

        # Fallback: OBD Mode 03 / 07 (best-effort)
        obd = self._isotp_transact(tx_id, rx_id, bytes([0x03]), timeout_s=1.5, is_extended_id=is_extended_id)
        if obd and len(obd) >= 1 and obd[0] == 0x43:
            return self._parse_obd_mode03(obd)

        obd7 = self._isotp_transact(tx_id, rx_id, bytes([0x07]), timeout_s=1.5, is_extended_id=is_extended_id)
        if obd7 and len(obd7) >= 1 and obd7[0] == 0x47:
            return self._parse_obd_mode03(obd7)

        # If we got here: we either accumulated diagnostic hints (NRC/timeouts) or nothing worked.
        return dtcs

    def _enrich_dtcs_with_context_isotp(
        self,
        tx_id: int,
        rx_id: int,
        dtcs: List[DtcItem],
        did_index: Optional[Dict[int, Dict[str, Any]]],
        *,
        is_extended_id: bool = False,
    ) -> List[DtcItem]:
        if not dtcs:
            return []
        did_map = did_index if isinstance(did_index, dict) else {}
        out: List[DtcItem] = []
        for d in dtcs:
            if not isinstance(d, DtcItem) or not isinstance(d.uds_dtc, int):
                out.append(d)
                continue

            extra: Dict[str, Any] = {}
            dtc_val = int(d.uds_dtc) & 0xFFFFFF
            dtc_bytes = dtc_val.to_bytes(3, 'big', signed=False)

            snap_req = bytes([0x19, 0x04]) + dtc_bytes + bytes([0xFF])
            snap_resp = self._isotp_transact(tx_id, rx_id, snap_req, timeout_s=2.0, is_extended_id=is_extended_id)
            if snap_resp:
                if snap_resp[:1] == b'\x7f':
                    extra['snapshot_nrc_raw'] = _hex_bytes(snap_resp)
                else:
                    parsed = _parse_uds_snapshot_response(bytes(snap_resp), did_map)
                    if parsed:
                        extra['snapshots'] = [parsed]
                    else:
                        extra['snapshot_raw'] = _hex_bytes(snap_resp)

            ext_req = bytes([0x19, 0x06]) + dtc_bytes + bytes([0xFF])
            ext_resp = self._isotp_transact(tx_id, rx_id, ext_req, timeout_s=2.0, is_extended_id=is_extended_id)
            if ext_resp:
                if ext_resp[:1] == b'\x7f':
                    extra['extdata_nrc_raw'] = _hex_bytes(ext_resp)
                else:
                    parsed = _parse_uds_extended_data_response(bytes(ext_resp), did_map)
                    if parsed:
                        extra['extended_data'] = [parsed]
                    else:
                        extra['extdata_raw'] = _hex_bytes(ext_resp)

            km = _guess_odometer_km(extra)
            if isinstance(km, int):
                extra['odometer_km'] = int(km)

            ts = _guess_timestamp(extra)
            if isinstance(ts, dict):
                # Keep both structured fields and the original candidate to help tuning.
                extra['timestamp_guess'] = ts
                if isinstance(ts.get('timestamp_iso'), str):
                    extra['timestamp_iso'] = ts.get('timestamp_iso')
                elif isinstance(ts.get('text'), str):
                    extra['timestamp_text'] = ts.get('text')

            out.append(DtcItem(
                code=d.code,
                uds_dtc=d.uds_dtc,
                status_byte=d.status_byte,
                status_desc=d.status_desc,
                active=d.active,
                raw=d.raw,
                description=d.description,
                dtc_class=d.dtc_class,
                extra=extra,
            ))
        return out

    def _parse_uds_dtc_records(self, data: bytes) -> List[DtcItem]:
        # Expected: 59 02 <statusAvailabilityMask> <dtcFormatIdentifier> <records...>
        if len(data) < 4:
            return []
        recs = data[3:]
        out: List[DtcItem] = []
        stride = 4
        i = 0
        while i + stride <= len(recs):
            dtc_val = (recs[i] << 16) | (recs[i + 1] << 8) | recs[i + 2]
            status = int(recs[i + 3])
            if dtc_val != 0:
                code = _uds_dtc_to_display_code(dtc_val)
                desc = self._decode_dtc_status(status)
                active = self._dtc_active_from_status(status)
                dtc_class = _dtc_classify_idex(status)
                raw = f"{recs[i]:02X} {recs[i+1]:02X} {recs[i+2]:02X} {status:02X}"
                out.append(DtcItem(code=code, uds_dtc=int(dtc_val), status_byte=status, status_desc=desc, active=active, raw=raw, description="", dtc_class=dtc_class))
            i += stride
        return out

    def _parse_obd_mode03(self, data: bytes) -> List[DtcItem]:
        # 43 [DTC1_H] [DTC1_L] ... (Scanrasp kept it simple)
        if len(data) < 3:
            return []
        dtcs = data[1:]
        out: List[DtcItem] = []
        filtered = 0
        for i in range(0, len(dtcs), 2):
            if i + 1 >= len(dtcs):
                break
            b1 = int(dtcs[i]) & 0xFF
            b2 = int(dtcs[i + 1]) & 0xFF

            # Common padding/garbage patterns seen on some ECUs when they don't support the request properly.
            # - 00 00: explicit "no more DTC"
            # - FF FF / AA AA: filler bytes
            if (b1, b2) in {(0x00, 0x00), (0xFF, 0xFF), (0xAA, 0xAA)}:
                filtered += 1
                continue

            # Another common filler placeholder observed: 00 AA (decodes to P00AA).
            if (b1, b2) == (0x00, 0xAA):
                filtered += 1
                continue
            # If the first byte has the 2 MSBs set to 0b11 (Uxxxx) it's still valid,
            # but if it's 0xFF/0x00 already handled above.

            val = (b1 << 8) | b2
            if val == 0:
                filtered += 1
                continue

            # Heuristic: reject obviously non-sensical codes that decode to "P00AA"-style placeholders.
            # This doesn't block real codes; it only filters the extremely common AA filler cases.
            if b2 == 0xAA and (b1 & 0x0F) == 0x0A:
                filtered += 1
                continue

            code = _obd_dtc_to_display_code(dtcs[i], dtcs[i + 1])
            raw = f"{dtcs[i]:02X} {dtcs[i+1]:02X}"
            out.append(DtcItem(code=code, uds_dtc=None, status_byte=None, status_desc='OBD DTC', active=True, raw=raw, description=""))

        # Optional: store filtering stats in-band by adding a pseudo-item.
        # Kept disabled by default to avoid polluting outputs.
        # if filtered:
        #     out.append(DtcItem(code='INFO', uds_dtc=None, status_byte=None, status_desc=f'Filtered {filtered} padding entries', active=False, raw='', description=''))
        return out

    def _parse_obd_mode0a(self, data: bytes) -> List[str]:
        # 4A [DTC1_H] [DTC1_L] ...
        if not data or len(data) < 3:
            return []
        if data[0] != 0x4A:
            return []
        dtcs = data[1:]
        out: List[str] = []
        for i in range(0, len(dtcs), 2):
            if i + 1 >= len(dtcs):
                break
            val = (dtcs[i] << 8) | dtcs[i + 1]
            if val == 0:
                continue
            out.append(_obd_dtc_to_display_code(dtcs[i], dtcs[i + 1]))
        return out

    def _obd_read_mode0a(self, tx_id: int, rx_id: int, *, is_extended_id: bool = False) -> List[str]:
        # Mode 0A: Permanent DTCs
        resp = self._isotp_transact(tx_id, rx_id, bytes([0x0A]), timeout_s=1.8, is_extended_id=is_extended_id)
        if not resp:
            return []
        if resp[:1] == b'\x7f':
            return []
        return self._parse_obd_mode0a(resp)

    def _obd_read_mode06(self, tx_id: int, rx_id: int, *, is_extended_id: bool = False) -> Dict[str, Any]:
        # Mode 06: On-board monitoring test results.
        # We keep this best-effort but structured: parse 46 <TID> [CID value(2) min(2) max(2)]...

        def _parse_tid_frame(resp: bytes) -> Dict[str, Any]:
            out: Dict[str, Any] = {
                'raw_hex': _hex_bytes(resp),
                'tests': [],
                'tail_hex': '',
            }
            if not resp or len(resp) < 2:
                return out
            if resp[:1] == b'\x7f':
                # Negative response format: 7F <service> <NRC>
                # Here expected service is 0x06.
                try:
                    svc = int(resp[1]) if len(resp) > 1 else None
                    nrc = int(resp[2]) if len(resp) > 2 else None
                    out['negative_response'] = {'service': svc, 'nrc': nrc}
                    out['note'] = f"Negative response (svc=0x{svc:02X} nrc=0x{nrc:02X})" if (svc is not None and nrc is not None) else 'Negative response'
                except Exception:
                    out['note'] = 'Negative response'
                return out
            if resp[0] != 0x46:
                out['note'] = 'Unexpected response (kept raw)'
                return out
            tid = int(resp[1])
            out['tid'] = tid
            data = resp[2:]

            # Most common CAN format: repeating blocks of 7 bytes:
            # CID(1) + VALUE(2) + MIN(2) + MAX(2)
            stride = 7
            i = 0
            while i + stride <= len(data):
                cid = int(data[i])
                value = int.from_bytes(data[i + 1:i + 3], 'big', signed=False)
                vmin = int.from_bytes(data[i + 3:i + 5], 'big', signed=False)
                vmax = int.from_bytes(data[i + 5:i + 7], 'big', signed=False)
                passed = (vmin <= value <= vmax) if (vmin != 0 or vmax != 0) else None
                out['tests'].append({
                    'tid': tid,
                    'cid': cid,
                    'value': value,
                    'min': vmin,
                    'max': vmax,
                    'pass': passed,
                    'raw_hex': _hex_bytes(bytes([cid]) + data[i + 1:i + 7]),
                })
                i += stride
            if i < len(data):
                out['tail_hex'] = _hex_bytes(data[i:])
            return out

        def _supported_tids_from_06_00(resp: bytes) -> List[int]:
            # Common layout: 46 00 A B C D where A..D bitmask for TID 0x01..0x20
            if not resp or len(resp) < 6 or resp[0] != 0x46 or resp[1] != 0x00:
                return []
            mask = resp[2:6]
            out: List[int] = []
            for bit in range(32):
                byte_i = bit // 8
                bit_i = 7 - (bit % 8)
                if (mask[byte_i] >> bit_i) & 1:
                    out.append(bit + 1)
            return out

        # Step 1: ask supported TIDs.
        resp00 = self._isotp_transact(tx_id, rx_id, bytes([0x06, 0x00]), timeout_s=2.2, is_extended_id=is_extended_id)
        if not resp00:
            return {'note': 'No response', 'raw_hex': ''}
        if resp00[:1] == b'\x7f':
            # Negative response: 7F 06 <NRC>
            svc = int(resp00[1]) if len(resp00) > 1 else None
            nrc = int(resp00[2]) if len(resp00) > 2 else None
            note = f"Negative response (svc=0x{svc:02X} nrc=0x{nrc:02X})" if (svc is not None and nrc is not None) else 'Negative response'
            return {'note': note, 'raw_hex': _hex_bytes(resp00), 'negative_response': {'service': svc, 'nrc': nrc}}
        if resp00[0] != 0x46:
            return {'note': 'Unexpected response (kept raw)', 'raw_hex': _hex_bytes(resp00)}

        supported = _supported_tids_from_06_00(resp00)
        tests: List[Dict[str, Any]] = []
        tid_frames: List[Dict[str, Any]] = []

        # Some ECUs don't implement the supported mask; still parse 06 00 as a TID frame.
        parsed00 = _parse_tid_frame(resp00)
        tid_frames.append(parsed00)
        if isinstance(parsed00.get('tests'), list):
            tests.extend(parsed00['tests'])

        # Step 2: query each supported TID (limit to keep scan reasonable).
        for tid in supported[:32]:
            resp = self._isotp_transact(tx_id, rx_id, bytes([0x06, int(tid) & 0xFF]), timeout_s=2.2, is_extended_id=is_extended_id)
            if not resp:
                continue
            parsed = _parse_tid_frame(resp)
            tid_frames.append(parsed)
            if isinstance(parsed.get('tests'), list):
                tests.extend(parsed['tests'])

        # Summarize pass/fail counts.
        passed = sum(1 for t in tests if t.get('pass') is True)
        failed = sum(1 for t in tests if t.get('pass') is False)
        unknown = sum(1 for t in tests if t.get('pass') is None)
        note = f"Decoded Mode 06 tests: pass={passed} fail={failed} unknown={unknown}"

        failing_tests = [t for t in tests if t.get('pass') is False]
        return {
            'note': note,
            'summary': {'pass': passed, 'fail': failed, 'unknown': unknown, 'tests': len(tests)},
            'supported_tids': supported,
            'tests': tests,
            'failing_tests': failing_tests,
            'frames': tid_frames,
            'raw_hex': _hex_bytes(resp00),
        }

    def doip_scan_report(
        self,
        gateway_ip: str,
        *,
        ecu_addresses: Optional[List[int]] = None,
        tester_logical_address: int = 0x0E00,
    ) -> str:
        if ecu_addresses is None:
            try:
                ci = _load_active_pdx_comm_index()
                addrs: List[int] = []
                for e in (ci.get('ecus') if isinstance(ci, dict) and isinstance(ci.get('ecus'), list) else []):
                    if not isinstance(e, dict):
                        continue
                    d = e.get('doip') if isinstance(e.get('doip'), dict) else {}
                    la = d.get('logical_ecu_address')
                    if isinstance(la, int):
                        addrs.append(int(la) & 0xFFFF)
                if 0x0002 in addrs and str(os.getenv('DOIP_ALLOW_0002', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
                    addrs = [x for x in addrs if x != 0x0002]
                ecu_addresses = addrs if addrs else None
            except Exception:
                ecu_addresses = None

        scanner = DoIPGatewayScanner(
            gateway_ip,
            emit_log=self.emit_log,
            tester_logical_address=int(tester_logical_address),
        )
        try:
            html_name, discovered = scanner.run_scan_report(ecu_addresses=ecu_addresses)
            self.log(f"DoIP ECUs discovered: {len(discovered)}")
            self.log(f"HTML report saved: {html_name}")
            self.log(f"Download: /api/logs/{html_name}")
            return html_name
        finally:
            try:
                scanner.close()
            except Exception:
                pass

    def clear_dtcs(self):
        """Clear DTCs using OBD Mode 04.

        Notes:
        - Mode 04 request payload is one byte (0x04). Over ISO-TP single-frame this
          should be encoded as SF length=1 (0x01) followed by 0x04.
        - Some VAG gateways/ECUs ignore functional broadcast clear and require a
          physical request (0x7E0/0x7E8 pairs). We do both best-effort.
        """
        self.log("=== Clear DTCs (OBD Mode 04) ===")

        # When CAN is present but not actually connected/responsive, the UI will
        # fall back to DoIP clearing. Make sure this function *signals failure*
        # to the caller so it can trigger fallback.
        if not self._ensure_real_vehicle_bus():
            raise RuntimeError('no valid OBD responses (real vehicle bus check failed)')

        def _nrc_name(nrc: int) -> str:
            m = {
                0x10: 'GeneralReject',
                0x11: 'ServiceNotSupported',
                0x12: 'SubFunctionNotSupported',
                0x13: 'IncorrectMessageLengthOrInvalidFormat',
                0x22: 'ConditionsNotCorrect',
                0x31: 'RequestOutOfRange',
                0x33: 'SecurityAccessDenied',
                0x35: 'InvalidKey',
                0x36: 'ExceededNumberOfAttempts',
                0x37: 'RequiredTimeDelayNotExpired',
                0x78: 'ResponsePending',
            }
            return m.get(int(nrc) & 0xFF, 'NRC')

        def _send_obd_sf(arbitration_id: int, service: int, pid: Optional[int] = None) -> bool:
            try:
                if pid is None:
                    data = [0x01, service & 0xFF, 0, 0, 0, 0, 0, 0]
                else:
                    data = [0x02, service & 0xFF, pid & 0xFF, 0, 0, 0, 0, 0]
                self.bus.send(can.Message(arbitration_id=int(arbitration_id), data=data, is_extended_id=False))
                return True
            except Exception as e:
                self.log(f"Send failed (0x{int(arbitration_id):03X}): {e}")
                return False

        def _snapshot_mil(tag: str, *, timeout_s: float = 1.2) -> Dict[int, Dict[str, Any]]:
            out: Dict[int, Dict[str, Any]] = {}
            if not _send_obd_sf(OBD_FUNCTIONAL, 0x01, 0x01):
                return out

            end = time.time() + float(timeout_s)
            while time.time() < end:
                rx = self.bus.recv(0.1)
                if not rx or rx.arbitration_id not in OBD_RESPONSES:
                    continue
                data = bytes(getattr(rx, 'data', b'') or b'')
                if len(data) >= 6 and data[1] == 0x41 and data[2] == 0x01:
                    a = int(data[3]) & 0xFF
                    out[int(rx.arbitration_id)] = {
                        'mil_on': bool(a & 0x80),
                        'dtc_count': int(a & 0x7F),
                        'raw': data.hex(),
                    }
            if out:
                parts = []
                for rid in sorted(out):
                    name = ECU_MAP.get(rid, hex(rid))
                    parts.append(f"{name}: MIL={'ON' if out[rid]['mil_on'] else 'OFF'} DTCs={out[rid]['dtc_count']}")
                self.log(f"{tag} MIL snapshot ({len(out)} ECUs): " + "; ".join(parts))
            else:
                self.log(f"{tag} MIL snapshot: no responders")
            return out

        _snapshot_mil("Pre-clear")

        responders_pid00: List[int] = []
        if _send_obd_sf(OBD_FUNCTIONAL, 0x01, 0x00):
            end = time.time() + 1.2
            seen = set()
            while time.time() < end:
                rx = self.bus.recv(0.1)
                if not rx or rx.arbitration_id not in OBD_RESPONSES:
                    continue
                if rx.arbitration_id in seen:
                    continue
                data = bytes(getattr(rx, 'data', b'') or b'')
                if len(data) >= 3 and data[1] == 0x41 and data[2] == 0x00:
                    seen.add(rx.arbitration_id)
                    responders_pid00.append(int(rx.arbitration_id))

        if responders_pid00:
            names = [str(ECU_MAP.get(r, hex(r))) for r in sorted(responders_pid00)]
            self.log(f"OBD responders (PID 00): {', '.join(names)}")
        else:
            self.log("OBD responders (PID 00): none")

        if not _send_obd_sf(OBD_FUNCTIONAL, 0x04):
            return

        end_time = time.time() + 2.0
        responders = set()
        ok_mode04 = set()
        pending_mode04 = set()
        nrc_mode04: Dict[int, int] = {}

        while time.time() < end_time:
            rx = self.bus.recv(0.1)
            if not rx or rx.arbitration_id not in OBD_RESPONSES:
                continue
            if rx.arbitration_id in responders:
                continue
            responders.add(rx.arbitration_id)

            data = bytes(getattr(rx, 'data', b'') or b'')
            name = ECU_MAP.get(rx.arbitration_id, hex(rx.arbitration_id))
            if len(data) >= 2 and data[1] == 0x44:
                ok_mode04.add(rx.arbitration_id)
                self.log(f"ECU responded: {name} (Mode 04 positive)")
            elif len(data) >= 4 and data[1] == 0x7F and data[2] == 0x04:
                nrc = int(data[3]) & 0xFF
                nrc_mode04[int(rx.arbitration_id)] = nrc
                if nrc == 0x78:
                    pending_mode04.add(rx.arbitration_id)
                self.log(f"ECU responded: {name} (Mode 04 negative NRC 0x{nrc:02X} {_nrc_name(nrc)})")
            else:
                self.log(f"ECU responded: {name} (raw={data.hex()})")

        if pending_mode04:
            self.log(f"Mode 04 pending from {len(pending_mode04)} ECU(s); waiting for final response (up to 15s)…")
            wait_end = time.time() + 15.0
            while time.time() < wait_end and pending_mode04:
                rx = self.bus.recv(0.25)
                if not rx or rx.arbitration_id not in pending_mode04:
                    continue
                data = bytes(getattr(rx, 'data', b'') or b'')
                name = ECU_MAP.get(rx.arbitration_id, hex(rx.arbitration_id))
                if len(data) >= 2 and data[1] == 0x44:
                    ok_mode04.add(rx.arbitration_id)
                    pending_mode04.discard(rx.arbitration_id)
                    self.log(f"ECU responded: {name} (Mode 04 final positive)")
                    continue
                if len(data) >= 4 and data[1] == 0x7F and data[2] == 0x04:
                    nrc = int(data[3]) & 0xFF
                    nrc_mode04[int(rx.arbitration_id)] = nrc
                    if nrc != 0x78:
                        pending_mode04.discard(rx.arbitration_id)
                        self.log(f"ECU responded: {name} (Mode 04 final negative NRC 0x{nrc:02X} {_nrc_name(nrc)})")

            if pending_mode04:
                names = [str(ECU_MAP.get(a, hex(a))) for a in sorted(pending_mode04)]
                self.log(f"Mode 04 still pending after 15s: {', '.join(names)}")

        if not responders:
            self.log("No ECUs responded to clear request.")
        else:
            self.log(f"Clear request sent. Responders: {len(responders)} (positive={len(ok_mode04)})")

        if responders_pid00:
            self.log(f"Attempting physical Mode 04 clear on {len(responders_pid00)} ECU(s)…")
            phys_ok = 0
            for rx_id in sorted(responders_pid00):
                tx_id = int(rx_id) - 8
                if not _send_obd_sf(tx_id, 0x04):
                    continue
                wait_end = time.time() + 1.2
                while time.time() < wait_end:
                    rx = self.bus.recv(0.1)
                    if not rx or int(rx.arbitration_id) != int(rx_id):
                        continue
                    data = bytes(getattr(rx, 'data', b'') or b'')
                    name = ECU_MAP.get(int(rx_id), hex(int(rx_id)))
                    if len(data) >= 2 and data[1] == 0x44:
                        phys_ok += 1
                        self.log(f"ECU responded: {name} (physical Mode 04 positive)")
                        break
                    if len(data) >= 4 and data[1] == 0x7F and data[2] == 0x04:
                        nrc = int(data[3]) & 0xFF
                        self.log(f"ECU responded: {name} (physical Mode 04 negative NRC 0x{nrc:02X} {_nrc_name(nrc)})")
                        break
            self.log(f"Physical Mode 04 positives: {phys_ok}/{len(responders_pid00)}")

        if responders and not ok_mode04 and any(v == 0x22 for v in nrc_mode04.values()):
            self.log("Mode 04 refused due to ConditionsNotCorrect (NRC 0x22). Try ignition ON, engine OFF, gear in P/N, stable voltage.")

        time.sleep(1.0)
        post = _snapshot_mil("Post-clear")
        if post and any(v.get('mil_on') for v in post.values()):
            self.log("MIL still reported ON after clear. Either a fault is still present, an ECU requires session/security for clearing, or a key-cycle/drive-cycle is needed.")

        # Best-effort physical clear via UDS 0x14 (ClearDiagnosticInformation) per-ECU.
        # Some ECUs ignore the functional OBD broadcast and require a physical UDS request
        # (often in specific diagnostic session). We'll probe discovered ECUs and try.
        try:
            pairs = self._discover_ecus_for_report()
        except Exception:
            pairs = []

        if not pairs:
            self.log("No physical ECUs discovered for per-ECU UDS clear.")
            return

        self.log(f"Attempting per-ECU UDS clear on {len(pairs)} ECUs (0x14).")
        cleared = []
        for tx_id, rx_id in pairs:
            try:
                # Try entering extended session (best-effort) before clear
                try:
                    _ = self._isotp_transact(tx_id, rx_id, bytes([0x10, 0x03]), timeout_s=0.8)
                except Exception:
                    pass

                name = self._ecu_name_guess(rx_id)

                def _is_pending(resp: Optional[bytes], *, service: int) -> bool:
                    try:
                        return bool(resp and len(resp) >= 3 and resp[0] == 0x7F and resp[1] == (service & 0xFF) and resp[2] == 0x78)
                    except Exception:
                        return False

                def _transact_with_pending(req: bytes, *, service: int, timeout_s: float, total_wait_s: float = 15.0) -> Optional[bytes]:
                    start = time.time()
                    resp0 = self._isotp_transact(tx_id, rx_id, req, timeout_s=timeout_s)
                    if not _is_pending(resp0, service=service):
                        return resp0
                    # Wait/retry until we get final positive/negative (or timeout)
                    while time.time() - start < total_wait_s:
                        time.sleep(0.25)
                        remaining = max(0.3, min(1.2, total_wait_s - (time.time() - start)))
                        r = self._isotp_transact(tx_id, rx_id, req, timeout_s=remaining)
                        if r and not _is_pending(r, service=service):
                            return r
                    return resp0

                # Send ClearDiagnosticInformation (0x14).
                # Some stacks use "groupOfDTC"=0xFFFFFF (all DTCs). We'll try both, but
                # we won't keep retrying if the ECU says 'service not supported' (NRC 0x11).
                reqs = [bytes([0x14]), bytes([0x14, 0xFF, 0xFF, 0xFF])]
                resp = None
                for req in reqs:
                    resp = _transact_with_pending(req, service=0x14, timeout_s=1.2, total_wait_s=15.0)
                    if resp and len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x14 and resp[2] == 0x11:
                        # ServiceNotSupported: no point trying other parameterization.
                        break
                    # If we got any response (positive or other NRC), stop here.
                    if resp is not None:
                        break
                name = self._ecu_name_guess(rx_id)
                if resp and len(resp) >= 2 and resp[0] == 0x54 and resp[1] == 0x14:
                    cleared.append((tx_id, rx_id))
                    self.log(f"UDS clear acknowledged by {name} (tx=0x{tx_id:03X} rx=0x{rx_id:03X})")
                elif resp and resp[0] == 0x7F and resp[1] == 0x14:
                    self.log(f"UDS clear negative response from {name}: NRC 0x{resp[2]:02X} {_nrc_name(resp[2])} (raw={_hex_bytes(resp)})")
                else:
                    self.log(f"No/unknown response to UDS clear from {name} (tx=0x{tx_id:03X} rx=0x{rx_id:03X})")
            except Exception as e:
                self.log(f"Per-ECU clear failed for 0x{tx_id:03X}->0x{rx_id:03X}: {e}")

        if cleared:
            self.log(f"Per-ECU UDS clear responders: {len(cleared)}")
        else:
            self.log("No ECUs accepted UDS clear (0x14). Some modules require security/session or manual repair.")

    def _read_dtc(self, tx_id):
        # Legacy helper kept for backwards compatibility.
        # tx_id here is actually expected to be the ECU physical request ID (e.g. 0x7E0),
        # not the response ID. Prefer _read_dtcs_best_effort() for a complete scan.
        msg = can.Message(arbitration_id=int(tx_id), data=[0x02, 0x03, 0x00, 0, 0, 0, 0, 0], is_extended_id=False)
        self.bus.send(msg)

        rx_msg = self.bus.recv(1.0)
        if not rx_msg:
            self.log("  No response")
            return
        data = bytes(getattr(rx_msg, 'data', b'') or b'')
        if len(data) >= 3 and data[1] == 0x43:
            self.log("  Mode 03 positive response (raw): " + data.hex())
        else:
            self.log("  Mode 03 not supported / negative (raw): " + data.hex())

    def close(self):
        self.bus.shutdown()

class VAGScannerService:
    def __init__(self, bus_manager, socketio=None):
        self.bus_manager = bus_manager
        self.socketio = socketio
        self.running = False
        self.thread = None
        self._stop_evt = threading.Event()
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            'running': False,
            'mode': None,          # 'scan'|'action'|'live'
            'action': None,
            'channel_id': None,
            'started_at': None,
            'finished_at': None,
            'last_error': None,
            'last_summary': None,
        }
        # Optional ExperimentalAssistantService ref (set by app.py wiring) so
        # that DoIP actions can pause Sentinel MIL polling and avoid the
        # tester-address (0x0E00) collision on the gateway that causes the
        # post-routing-activation Broken-pipe loop.
        self._sentinel: Any = None

    def _pause_sentinel_mil(self) -> bool:
        """Pause Sentinel MIL DoIP polling. Returns True if pause was issued."""
        s = self._sentinel
        if s is not None and hasattr(s, 'pause_doip_mil'):
            try:
                s.pause_doip_mil()
                # let gateway release any prior routing activation
                time.sleep(0.3)
                return True
            except Exception:
                return False
        return False

    def _resume_sentinel_mil(self) -> None:
        s = self._sentinel
        if s is not None and hasattr(s, 'resume_doip_mil'):
            try:
                s.resume_doip_mil()
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        """Return current ScanTools status (safe to expose via API)."""
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **updates):
        try:
            with self._status_lock:
                self._status.update(updates)
        except Exception:
            pass

    def _emit(self, line: str):
        if not self.socketio:
            return
        try:
            self.socketio.emit('scan_log', {'line': line})
        except Exception:
            pass

    def _emit_vehicle_data(self, channel_id: int, rpm, speed_kph, mil_on=None, mil_dtc_count=None, *, extra: Optional[Dict[str, Any]] = None):
        if not self.socketio:
            return
        payload = {
            "channel_id": channel_id,
            "ts": time.time(),
            "rpm": rpm,
            "speed_kph": speed_kph,
            "mil_on": mil_on,
            "mil_dtc_count": mil_dtc_count,
        }
        if extra:
            for k in ('coolant_temp', 'battery_soc', 'battery_voltage', 'odometer', 'vin'):
                if k in extra:
                    payload[k] = extra[k]
        try:
            self.socketio.emit('vehicle_data', payload)
        except Exception:
            pass

    def start_scan(self, channel_id):
        if self.running:
            return False
        self.running = True
        self._set_status(
            running=True,
            mode='scan',
            action='scan_obd',
            channel_id=int(channel_id),
            started_at=time.time(),
            finished_at=None,
            last_error=None,
            last_summary=None,
        )
        self.thread = threading.Thread(target=self._run_scan, args=(channel_id,))
        self.thread.start()
        return True

    def start_action(self, channel_id: int, action: str, *, params: Optional[dict] = None) -> bool:
        if self.running:
            return False
        self.running = True
        self._stop_evt.clear()
        self._set_status(
            running=True,
            mode='action',
            action=str(action),
            channel_id=int(channel_id),
            started_at=time.time(),
            finished_at=None,
            last_error=None,
            last_summary=None,
        )
        self.thread = threading.Thread(target=self._run_action, args=(channel_id, action, params or {}), daemon=True)
        self.thread.start()
        return True

    def start_live(self, channel_id: int, interval_s: float = 0.2, *,
                   transport: str = 'can', doip_params: Optional[Dict[str, Any]] = None) -> bool:
        if self.running:
            return False
        self.running = True
        self._stop_evt.clear()
        self._set_status(
            running=True,
            mode='live',
            action='live',
            channel_id=int(channel_id),
            started_at=time.time(),
            finished_at=None,
            last_error=None,
            last_summary=None,
        )
        if transport == 'doip':
            self.thread = threading.Thread(
                target=self._run_live_doip,
                args=(interval_s, doip_params or {}),
                daemon=True,
            )
        else:
            self.thread = threading.Thread(target=self._run_live, args=(channel_id, interval_s), daemon=True)
        self.thread.start()
        return True

    def stop_live(self) -> bool:
        if not self.running:
            return False
        self._stop_evt.set()
        t = self.thread
        if t:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        return True

    def _run_scan(self, channel_id):
        scanner = None
        try:
            scanner = VAGScanner(self.bus_manager, channel_id, emit_log=self._emit)
            scanner.scan_obd()
            self._set_status(last_summary='scan_obd finished')
        except Exception as e:
            print(f"Scan failed: {e}")
            self._emit(f"Scan failed: {e}")
            self._set_status(last_error=str(e))
        finally:
            if scanner:
                scanner.close()
            self.running = False
            self._set_status(running=False, finished_at=time.time())

    def _run_action(self, channel_id: int, action: str, params: dict):
        scanner = None
        # Pause Sentinel MIL DoIP polling for any DoIP-based action so the
        # background MIL scanner (tester 0x0E00) doesn't race with the
        # foreground action's routing-activation, which manifests as a
        # post-RA Broken-pipe loop and "ECUs found: 0".
        _doip_actions = {
            'vag_doip_scan_report',
            'doip_clear_dtcs',
            'doip_mode06',
            'doip_recover_network',
            'clear_dtcs',  # may fall back to DoIP
        }
        _sentinel_was_paused = False
        if action in _doip_actions:
            _sentinel_was_paused = self._pause_sentinel_mil()
            if _sentinel_was_paused:
                try:
                    self._emit('Sentinel MIL DoIP polling paused for the duration of this action')
                except Exception:
                    pass
        try:
            if action == 'self_test':
                self._run_self_test(int(channel_id), params or {})
                self._set_status(last_summary='self_test finished')
                return

            if action == 'vag_doip_scan_report':
                gateway_ip = str((params or {}).get('gateway_ip') or '').strip()
                gateway_iface = str((params or {}).get('gateway_iface') or '').strip()
                auto_discover = bool((params or {}).get('auto_discover', True))
                try:
                    tester_logical_address = int((params or {}).get('tester_logical_address', 0x0E00) or 0x0E00)
                except Exception:
                    tester_logical_address = 0x0E00

                # If user didn't explicitly set a tester address, prefer the most common PDX value.
                if 'tester_logical_address' not in (params or {}):
                    try:
                        ci = _load_active_pdx_comm_index()
                        vals: List[int] = []
                        for e in (ci.get('ecus') if isinstance(ci, dict) and isinstance(ci.get('ecus'), list) else []):
                            if not isinstance(e, dict):
                                continue
                            d = e.get('doip') if isinstance(e.get('doip'), dict) else {}
                            v = d.get('logical_tester_address')
                            if isinstance(v, int):
                                vals.append(int(v) & 0xFFFF)
                        if vals:
                            from collections import Counter
                            tester_logical_address = Counter(vals).most_common(1)[0][0]
                    except Exception:
                        pass

                # Prefer PDX-driven ECU target list unless explicitly provided.
                ecu_addresses = None
                try:
                    raw_addrs = (params or {}).get('ecu_addresses')
                    if isinstance(raw_addrs, list):
                        tmp = []
                        for x in raw_addrs:
                            try:
                                tmp.append(int(x, 0) & 0xFFFF if isinstance(x, str) else int(x) & 0xFFFF)
                            except Exception:
                                continue
                        ecu_addresses = tmp if tmp else None
                except Exception:
                    ecu_addresses = None

                if ecu_addresses is None:
                    try:
                        ci = _load_active_pdx_comm_index()
                        addrs: List[int] = []
                        for e in (ci.get('ecus') if isinstance(ci, dict) and isinstance(ci.get('ecus'), list) else []):
                            if not isinstance(e, dict):
                                continue
                            d = e.get('doip') if isinstance(e.get('doip'), dict) else {}
                            la = d.get('logical_ecu_address')
                            if isinstance(la, int):
                                addrs.append(int(la) & 0xFFFF)
                        # Safety: never probe 0x0002 unless explicitly allowed.
                        if 0x0002 in addrs and str(os.getenv('DOIP_ALLOW_0002', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
                            addrs = [x for x in addrs if x != 0x0002]
                        ecu_addresses = addrs if addrs else None
                    except Exception:
                        ecu_addresses = None

                if not gateway_ip and auto_discover:
                    self._emit('DoIP: Target IP not set. Running discovery (UDP/13400 broadcast)…')
                    gateway_ip = discover_doip_gateway_ip(iface=gateway_iface or None, timeout_s=1.2) or ''
                if not gateway_ip:
                    raise ValueError('DoIP gateway not found. Set Automotive Ethernet → Target IP or ensure the vehicle DoIP network is reachable.')

                doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_logical_address)
                try:
                    html_name, discovered = doip.run_scan_report(ecu_addresses=ecu_addresses)
                    self._emit(f"DoIP ECUs discovered: {len(discovered)}")
                    self._emit(f"HTML report saved: {html_name}")
                    self._emit(f"Download: /api/logs/{html_name}")
                    self._set_status(last_summary=f"DoIP report saved: {html_name}; ECUs: {len(discovered)}")
                finally:
                    try:
                        doip.close()
                    except Exception:
                        pass
                return

            if action == 'doip_clear_dtcs':
                gateway_ip = str((params or {}).get('gateway_ip') or '').strip()
                gateway_iface = str((params or {}).get('gateway_iface') or '').strip()
                
                if not gateway_ip:
                    self._emit('DoIP: Target IP not set. Running discovery (UDP/13400 broadcast)…')
                    gateway_ip = discover_doip_gateway_ip(iface=gateway_iface or None, timeout_s=1.2) or ''
                
                if not gateway_ip:
                    raise ValueError('DoIP gateway not found. Set Automotive Ethernet settings or ensure vehicle is connected.')

                tester_logical_address = 0x0E00
                try:
                    t = (params or {}).get('tester_logical_address')
                    if t:
                        tester_logical_address = int(t)
                except Exception:
                    pass

                doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_logical_address)
                try:
                    doip.clear_dtcs_doip()
                    self._set_status(last_summary='doip_clear_dtcs finished')
                finally:
                    doip.close()
                return

            if action == 'doip_mode06':
                gateway_ip = str((params or {}).get('gateway_ip') or '').strip()
                gateway_iface = str((params or {}).get('gateway_iface') or '').strip()

                if not gateway_ip:
                    self._emit('DoIP: Target IP not set. Running discovery (UDP/13400 broadcast)\u2026')
                    gateway_ip = discover_doip_gateway_ip(iface=gateway_iface or None, timeout_s=1.2) or ''

                if not gateway_ip:
                    raise ValueError('DoIP gateway not found. Set Automotive Ethernet settings or ensure vehicle is connected.')

                tester_logical_address = 0x0E00
                try:
                    t = (params or {}).get('tester_logical_address')
                    if t:
                        tester_logical_address = int(t)
                except Exception:
                    pass

                doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_logical_address)
                try:
                    doip.scan_mode06_doip()
                    self._set_status(last_summary='doip_mode06 finished')
                finally:
                    doip.close()
                return

            if action == 'doip_recover_network':
                gateway_iface = str((params or {}).get('gateway_iface') or '').strip()
                if not gateway_iface:
                    gateway_iface = str(os.getenv('DOIP_GATEWAY_IFACE', '') or '').strip()

                doip = DoIPGatewayScanner('fe80::1', emit_log=self._emit)
                try:
                    self._emit(f"DoIP: Attempting to recover/fix IPv6 Link-Local on '{gateway_iface}'...")
                    ok = doip._ensure_iface_ready_for_linklocal(gateway_iface)
                    if ok:
                        self._emit("DoIP: Interface IPv6 appears ready (fe80:: assigned + route).")
                        self._set_status(last_summary='recovery finished: success')
                    else:
                        self._emit("DoIP: Recovery procedure finished but interface check failed.")
                        self._set_status(last_summary='recovery finished: failed')
                finally:
                    doip.close()
                return

            if can is None:
                raise ImportError("python-can non installato (pip install python-can)")

            scanner = VAGScanner(self.bus_manager, channel_id, emit_log=self._emit)
            if action == 'scan_obd':
                scanner.scan_obd()
                self._set_status(last_summary='scan_obd finished')
            elif action == 'discovery':
                scanner.discovery()
                self._set_status(last_summary='discovery finished')
            elif action in {'mode06', 'scan_mode06', 'obd_mode06'}:
                scanner.scan_mode06()
                self._set_status(last_summary='mode06 finished')
            elif action == 'clear_dtcs':
                try:
                    scanner.clear_dtcs()
                    self._set_status(last_summary='clear_dtcs finished')
                except Exception as e:
                    # Always fallback to DoIP clear when CAN clear fails.
                    # This covers cases where the channel is "active" but not connected to a real vehicle bus.
                    try:
                        self._emit(f"CAN clear_dtcs failed: {e}. Falling back to DoIP clear...")
                    except Exception:
                        pass

                    # Resolve gateway settings from params (preferred) or app config.
                    gateway_ip = str((params or {}).get('gateway_ip') or '').strip()
                    gateway_iface = str((params or {}).get('gateway_iface') or '').strip()
                    auto_discover = bool((params or {}).get('auto_discover', True))
                    tester_logical_address = 0x0E00
                    try:
                        t = (params or {}).get('tester_logical_address')
                        if t is not None and str(t).strip() != '':
                            tester_logical_address = int(t, 0) if isinstance(t, str) else int(t)
                    except Exception:
                        tester_logical_address = 0x0E00

                    if not gateway_ip:
                        try:
                            base_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
                            cfg_path = os.path.join(base_dir, 'config', 'app_config.json')
                            if os.path.isfile(cfg_path):
                                import json
                                with open(cfg_path, 'r', encoding='utf-8') as fp:
                                    raw = json.load(fp)
                                cfg = raw.get('config') if isinstance(raw, dict) else None
                                es = cfg.get('eth_settings') if isinstance(cfg, dict) and isinstance(cfg.get('eth_settings'), dict) else {}
                                gateway_ip = str(es.get('target_ip') or '').strip()
                                gateway_iface = gateway_iface or str(es.get('interface') or '').strip()
                                auto_discover = bool(es.get('doip_auto_discover', auto_discover))
                                if (params or {}).get('tester_logical_address') is None:
                                    try:
                                        tester_logical_address = int(es.get('doip_tester_logical_address', tester_logical_address) or tester_logical_address)
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    if not gateway_ip and auto_discover:
                        try:
                            self._emit('DoIP: Target IP not set. Running discovery (UDP/13400 broadcast)…')
                        except Exception:
                            pass
                        gateway_ip = discover_doip_gateway_ip(iface=gateway_iface or None, timeout_s=1.2) or ''

                    if not gateway_ip:
                        raise RuntimeError(f"CAN clear_dtcs failed and DoIP gateway not found: {e}")

                    doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_logical_address)
                    try:
                        doip.clear_dtcs_doip()
                        self._set_status(last_summary='clear_dtcs finished (DoIP fallback)')
                    finally:
                        try:
                            doip.close()
                        except Exception:
                            pass
            elif action in {'vag_scan_report', 'vag_obd_scan_report'}:
                scanner.vag_scan_report()
                self._set_status(last_summary='vag_scan_report finished')
            else:
                scanner.log(f"Unknown action: {action}")
                self._set_status(last_error=f"unknown action: {action}")
        except Exception as e:
            print(f"ScanTools failed: {e}")
            self._emit(f"ScanTools failed: {e}")
            self._set_status(last_error=str(e))
        finally:
            if scanner:
                try:
                    scanner.close()
                except Exception:
                    pass
            if _sentinel_was_paused:
                self._resume_sentinel_mil()
                try:
                    self._emit('Sentinel MIL DoIP polling resumed')
                except Exception:
                    pass
            self.running = False
            self._set_status(running=False, finished_at=time.time())

    def _run_self_test(self, channel_id: int, params: Dict[str, Any]) -> None:
        """Run a safe, best-effort self test.

        Goals:
        - verify basic CAN readiness (if a channel is active)
        - verify DoIP reachability + routing activation (if vehicle network reachable)
        - produce actionable output in ScanTools console

        This must be non-invasive and must not leave the system in a different mode.
        """
        t0 = time.time()
        self._emit("=== SELF TEST START ===")

        # -----------------
        # Basic environment
        # -----------------
        try:
            import sys
            self._emit(f"Python: {sys.version.split()[0]}")
        except Exception:
            pass

        # -----------------
        # CAN checks (best effort)
        # -----------------
        try:
            bm = self.bus_manager
            is_mock = False
            try:
                is_mock = bool(getattr(bm, 'can_driver_is_mock', lambda: False)())
            except Exception:
                is_mock = False
            try:
                sim = bool(getattr(bm, 'simulate_ecu', False))
            except Exception:
                sim = False

            self._emit(f"CAN driver mock: {is_mock}")
            self._emit(f"ECU simulation enabled: {sim}")

            active_channels = []
            try:
                with bm.lock:
                    active_channels = list(getattr(bm, 'handlers', {}).keys())
            except Exception:
                active_channels = []
            self._emit(f"Active CAN channels: {sorted([int(x) for x in active_channels]) if active_channels else 'none'}")

            # Only try an on-bus handshake if python-can is available and the selected channel looks active.
            if can is None:
                self._emit("CAN test skipped: python-can not available")
            else:
                if channel_id and (int(channel_id) in [int(x) for x in active_channels]):
                    try:
                        s = VAGScanner(bm, int(channel_id), emit_log=self._emit)
                        try:
                            ok = s._ensure_real_vehicle_bus()
                            self._emit(f"CAN handshake (OBD PID 00) OK: {bool(ok)}")

                            # PDX-driven CAN diagnostics probe (UDS over ISO-TP).
                            # This verifies that the addressing/protocol derived from PDX is usable.
                            ci = _load_active_pdx_comm_index()
                            ecus = ci.get('ecus') if isinstance(ci, dict) else None
                            if isotp is None:
                                self._emit("PDX CAN probe skipped: can-isotp not available")
                            elif not isinstance(ecus, list) or not ecus:
                                self._emit("PDX CAN probe skipped: comm index not available")
                            else:
                                # Filter to UDS-on-CAN entries when available.
                                targets = []
                                for e in ecus:
                                    if not isinstance(e, dict):
                                        continue
                                    if str(e.get('protocol_snref') or '').strip().upper() not in {'PR_UDSONCAN', ''}:
                                        continue
                                    c = e.get('can') if isinstance(e.get('can'), dict) else {}
                                    tx_id = c.get('phys_req_id')
                                    rx_id = c.get('resp_id')
                                    if not isinstance(tx_id, int) or not isinstance(rx_id, int):
                                        continue
                                    targets.append({
                                        'tx_id': int(tx_id) & 0x1FFFFFFF,
                                        'rx_id': int(rx_id) & 0x1FFFFFFF,
                                        'is_extended_id': bool(c.get('is_extended_id') is True),
                                        'name': str(e.get('long_name') or e.get('short_name') or '').strip(),
                                    })
                                if not targets:
                                    self._emit("PDX CAN probe: no usable CAN endpoints in comm index")
                                else:
                                    try:
                                        limit = int(os.getenv('CAN_PDX_SELFTEST_LIMIT', '8') or '8')
                                    except Exception:
                                        limit = 8
                                    limit = max(1, min(32, limit))
                                    targets = targets[:limit]

                                    ok_ecus = 0
                                    self._emit(f"PDX CAN probe: testing {len(targets)} ECU(s) (UDS TesterPresent)…")
                                    for t in targets:
                                        tx = int(t['tx_id'])
                                        rx = int(t['rx_id'])
                                        is_ext = bool(t.get('is_extended_id') is True)
                                        nm = str(t.get('name') or '').strip() or f"0x{rx:X}"
                                        resp = s._isotp_transact(tx, rx, bytes([0x3E, 0x00]), timeout_s=0.6, is_extended_id=is_ext)
                                        if resp and (resp[:1] == b'\x7e' or (len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x3E)):
                                            ok_ecus += 1
                                            self._emit(f"  {nm}: RESP")
                                        else:
                                            self._emit(f"  {nm}: no/unknown")
                                    self._emit(f"PDX CAN probe responded: {ok_ecus}/{len(targets)}")
                        finally:
                            try:
                                s.close()
                            except Exception:
                                pass
                    except Exception as e:
                        self._emit(f"CAN handshake failed: {e}")
                else:
                    self._emit("CAN handshake skipped: no active channel selected")
        except Exception as e:
            self._emit(f"CAN self-test error (ignored): {e}")

        # -----------------
        # DoIP checks (best effort)
        # -----------------
        gateway_ip = str((params or {}).get('gateway_ip') or '').strip()
        gateway_iface = str((params or {}).get('gateway_iface') or '').strip()
        auto_discover = bool((params or {}).get('auto_discover', True))
        try:
            tester_logical_address = int((params or {}).get('tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00

        if not gateway_ip and auto_discover:
            self._emit('DoIP: Target IP not set. Running discovery (UDP/13400 broadcast)…')
            try:
                gateway_ip = discover_doip_gateway_ip(iface=gateway_iface or None, timeout_s=1.2) or ''
            except Exception as e:
                self._emit(f"DoIP discovery failed: {e}")

        if not gateway_ip:
            self._emit('DoIP test skipped: gateway IP not configured/found')
        else:
            self._emit(f"DoIP gateway: {gateway_ip} (tester=0x{tester_logical_address:04X})")
            doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_logical_address)
            try:
                # Minimal connectivity checks
                doip._connect()
                doip._routing_activation()

                # Probe a small, safe subset of logical addresses.
                # Avoid 0x0002 (gearbox) by default.
                probe_raw = str(os.getenv('DOIP_SELFTEST_PROBE_ADDRS', '0x0001,0x0003,0x0007') or '').strip()
                probe = []
                for part in probe_raw.split(','):
                    p = str(part or '').strip()
                    if not p:
                        continue
                    try:
                        probe.append(int(p, 0) & 0xFFFF)
                    except Exception:
                        continue
                if not probe:
                    probe = [0x0001, 0x0003, 0x0007]
                if 0x0002 in probe and str(os.getenv('DOIP_SELFTEST_ALLOW_0002', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
                    probe = [x for x in probe if x != 0x0002]
                    self._emit('DoIP: NOTE skipping ECU 0x0002 in self-test (safety)')

                ok_count = 0
                for la in probe[:12]:
                    try:
                        resp = doip._uds_transact(int(la), bytes([0x3E, 0x00]), timeout_s=0.35)
                        if resp and (resp[:1] == b'\x7e' or (len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x3E)):
                            ok_count += 1
                            self._emit(f"DoIP probe 0x{la:04X}: RESP")
                        else:
                            self._emit(f"DoIP probe 0x{la:04X}: no/unknown")
                    except Exception as e:
                        self._emit(f"DoIP probe 0x{la:04X}: error {e}")
                self._emit(f"DoIP probes responded: {ok_count}/{min(len(probe), 12)}")
            except Exception as e:
                self._emit(f"DoIP self-test failed: {e}")
            finally:
                try:
                    doip.close()
                except Exception:
                    pass

        dt = time.time() - t0
        self._emit(f"=== SELF TEST DONE ({dt:.1f}s) ===")

    # ------------------------------------------------------------------ #
    #  DID candidate table for Live Data probing                          #
    #  Each entry: (did_int, label, parser_key, min_resp_bytes)           #
    #  parser_key selects the decode logic in _parse_did_value().         #
    # ------------------------------------------------------------------ #
    _LIVE_DID_CANDIDATES: List[tuple] = [
        # --- RPM / motor speed (ICE & EV) ---
        (0xF40C, 'RPM (ISO)',            'rpm_iso',      5),  # ISO 15031-5 PID 0x0C
        (0x010C, 'RPM (OBD)',            'rpm_obd',      4),  # OBD SID $01 PID $0C via UDS
        (0x2000, 'RPM (VAG meas.blk)',   'rpm_vag',      5),  # VAG measuring block
        (0xF449, 'Motor speed',          'rpm_iso',      5),  # Some EV motor RPM
        (0x1001, 'Motor RPM (mfr)',      'rpm_2b',       5),  # Manufacturer 2-byte RPM
        # --- Vehicle speed ---
        (0xF40D, 'Speed (ISO)',          'speed_iso',    4),  # ISO 15031-5 PID 0x0D
        (0x010D, 'Speed (OBD)',          'speed_obd',    4),  # OBD SID $01 PID $0D via UDS
        (0xF44E, 'Speed (ISO alt)',      'speed_iso',    4),  # Some vehicles
        # --- Coolant temp ---
        (0xF405, 'Coolant temp (ISO)',   'temp_iso',     4),  # ISO PID 0x05
        (0x0105, 'Coolant temp (OBD)',   'temp_obd',     4),
        # --- Battery SOC / HV voltage (EV/Hybrid) ---
        (0x028C, 'HV Battery SOC (VAG)', 'soc_2b',       5),  # VAG MEB/MLB SOC (2-byte, /100)
        (0x0302, 'Display SOC (VAG)',    'soc_2b',       5),  # VAG display SOC
        (0x1E3B, 'HV SOC (mfr)',         'soc_2b',       5),  # Manufacturer SOC
        (0xF45B, 'HV SOC (ISO)',         'soc_pct',      4),  # Hybrid battery SOC %
        (0x015B, 'HV SOC (OBD)',         'soc_pct',      4),
        (0x028E, 'HV Voltage (VAG)',     'voltage_hv',   5),  # VAG MEB HV battery voltage (/100)
        (0x0302, 'HV Pack V (VAG)',      'voltage_hv',   5),  # VAG alternate
        (0xF442, 'Control module V',     'voltage_12v',  5),  # 12V system voltage (/1000)
        (0x0142, 'Control module V(OBD)','voltage_12v',  5),
        # --- Odometer ---
        (0xF4A6, 'Odometer (ISO)',       'odo_iso',      6),  # ISO PID 0xA6  (4-byte)
        (0xF190, 'VIN',                  'vin',          5),  # VIN (good connectivity check)
    ]

    @staticmethod
    def _parse_did_value(resp: bytes, did: int, parser_key: str):
        """Decode a positive ReadDataByIdentifier response. Returns (label, value, unit)."""
        hi = (did >> 8) & 0xFF
        lo = did & 0xFF
        if not resp or resp[0] != 0x62:
            return None
        if len(resp) < 3:
            return None
        if resp[1] != hi or resp[2] != lo:
            return None
        data = resp[3:]
        if not data:
            return None

        try:
            if parser_key in ('rpm_iso', 'rpm_vag', 'rpm_2b'):
                if len(data) >= 2:
                    raw = (data[0] << 8) | data[1]
                    return ('rpm', raw / 4.0, 'rpm')
            elif parser_key == 'rpm_obd':
                if len(data) >= 2:
                    return ('rpm', ((data[0] << 8) | data[1]) / 4.0, 'rpm')
            elif parser_key in ('speed_iso', 'speed_obd'):
                if len(data) >= 1:
                    return ('speed', float(data[0]), 'km/h')
            elif parser_key in ('temp_iso', 'temp_obd'):
                if len(data) >= 1:
                    return ('coolant_temp', float(data[0]) - 40.0, '°C')
            elif parser_key == 'soc_2b':
                if len(data) >= 2:
                    raw = (data[0] << 8) | data[1]
                    pct = raw / 100.0
                    if 0.0 <= pct <= 100.0:
                        return ('battery_soc', pct, '%')
                if len(data) >= 1:
                    return ('battery_soc', float(data[0]) * 100.0 / 255.0, '%')
            elif parser_key == 'soc_pct':
                if len(data) >= 1:
                    return ('battery_soc', float(data[0]) * 100.0 / 255.0, '%')
            elif parser_key == 'voltage_hv':
                if len(data) >= 2:
                    raw = (data[0] << 8) | data[1]
                    return ('battery_voltage', raw / 100.0, 'V')
            elif parser_key == 'voltage_12v':
                if len(data) >= 2:
                    raw = (data[0] << 8) | data[1]
                    return ('battery_voltage', raw / 1000.0, 'V')
            elif parser_key == 'odo_iso':
                if len(data) >= 4:
                    val = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
                    return ('odometer', val / 10.0, 'km')
                elif len(data) >= 3:
                    val = (data[0] << 16) | (data[1] << 8) | data[2]
                    return ('odometer', float(val), 'km')
            elif parser_key == 'vin':
                txt = ''.join(chr(b) if 32 <= b < 127 else '' for b in data).strip()
                if txt:
                    return ('vin', txt, '')
        except Exception:
            pass
        return None

    def _run_live_doip(self, interval_s: float, doip_params: dict):
        """Live Data via DoIP — auto-probes available DIDs (RPM, Speed, SOC,
        coolant temp, battery voltage, odometer, MIL) on the best responding ECU."""
        doip: Optional[DoIPGatewayScanner] = None
        sentinel = doip_params.get('_sentinel')  # ExperimentalAssistantService ref
        try:
            gateway_ip = str(doip_params.get('gateway_ip') or '').strip()
            if not gateway_ip:
                gateway_ip = discover_doip_gateway_ip(
                    iface=str(doip_params.get('gateway_iface') or '') or None,
                    timeout_s=1.5,
                ) or ''
            if not gateway_ip:
                raise ValueError('DoIP gateway not found. Set Automotive Ethernet → Target IP.')

            tester_addr = 0x0E01
            try:
                tester_addr = int(doip_params.get('live_tester_logical_address') or
                                  doip_params.get('tester_logical_address') or 0x0E01)
            except Exception:
                tester_addr = 0x0E01

            # Pause Sentinel MIL polling to avoid tester-address conflict on gateway.
            if sentinel and hasattr(sentinel, 'pause_doip_mil'):
                sentinel.pause_doip_mil()
                self._emit('DoIP Live: paused Sentinel MIL polling to avoid gateway conflict')
                time.sleep(0.5)  # let gateway release old connection

            # ECU candidates — real VAG vehicles use 0x40xx logical addresses.
            # Battery/energy ECUs first so they claim SOC & voltage categories
            # before engine ECUs can shadow them with 12 V system values.
            engine_targets = [0x407B, 0x4044, 0x4437, 0x40B7, 0x40B8,
                              0x4010, 0x4076, 0x4078, 0x407C,
                              0x4012, 0x400B, 0x0001, 0x0010]

            self._emit(f"=== Live Data (DoIP) started — gateway {gateway_ip} ===")
            doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_addr)
            doip.debug_doip = False
            doip._connect_with_recovery()
            doip._routing_activation()
            time.sleep(0.3)  # let gateway stabilise after routing activation

            # ---- Phase 1: find responding ECUs ---- #
            alive_ecus: List[int] = []
            for t in engine_targets:
                try:
                    resp = doip._uds_transact(t, b'\x3E\x00', timeout_s=0.8)
                    if resp and (resp[:1] == b'\x7e' or (len(resp) >= 3 and resp[0] == 0x7F and resp[1] == 0x3E)):
                        alive_ecus.append(t)
                        self._emit(f"DoIP Live: ECU 0x{t:04X} alive (TesterPresent)")
                except Exception:
                    pass

            if not alive_ecus:
                # Fallback: try ReadDTC
                for t in engine_targets:
                    try:
                        resp = doip._uds_transact(t, b'\x19\x02\xFF', timeout_s=1.0)
                        if resp and len(resp) >= 3 and (resp[0] == 0x59 or (resp[0] == 0x7F and resp[1] == 0x19)):
                            alive_ecus.append(t)
                            self._emit(f"DoIP Live: ECU 0x{t:04X} alive (ReadDTC)")
                    except Exception:
                        pass

            if not alive_ecus:
                alive_ecus = [engine_targets[0]]
                self._emit(f"DoIP Live: no ECU confirmed, defaulting to 0x{alive_ecus[0]:04X}")

            self._emit(f"DoIP Live: {len(alive_ecus)} ECU(s) alive: {', '.join(f'0x{e:04X}' for e in alive_ecus)}")

            # ---- Phase 2: DID discovery probe ---- #
            # For each alive ECU, try to open extended session then probe DIDs.
            # Track which (ecu, did, parser) combos return positive data.
            active_dids: List[tuple] = []  # [(target, did, label, parser_key)]
            categories_found: set = set()  # track which categories we already have

            for ecu in alive_ecus:
                # Try extended session for richer data access.
                try:
                    resp = doip._uds_transact(ecu, b'\x10\x03', timeout_s=0.8)
                    if resp and len(resp) >= 2 and resp[0] == 0x50:
                        self._emit(f"DoIP Live: extended session on 0x{ecu:04X}")
                except Exception:
                    pass

                for (did, label, parser_key, min_len) in self._LIVE_DID_CANDIDATES:
                    # Skip if we already found this data category from another ECU.
                    category = parser_key.split('_')[0]  # rpm, speed, temp, soc, voltage, odo, vin
                    if category in categories_found:
                        continue
                    hi = (did >> 8) & 0xFF
                    lo = did & 0xFF
                    try:
                        resp = doip._uds_transact(ecu, bytes([0x22, hi, lo]), timeout_s=1.0)
                        if resp and len(resp) >= min_len and resp[0] == 0x62 and resp[1] == hi and resp[2] == lo:
                            parsed = self._parse_did_value(resp, did, parser_key)
                            if parsed:
                                active_dids.append((ecu, did, label, parser_key))
                                categories_found.add(category)
                                self._emit(f"DoIP Live: ✓ DID 0x{did:04X} ({label}) on ECU 0x{ecu:04X} → {parsed[1]} {parsed[2]}")
                            else:
                                self._emit(f"DoIP Live: DID 0x{did:04X} ({label}) on 0x{ecu:04X} → positive but unparseable")
                        elif resp and resp[0] == 0x7F and len(resp) >= 3:
                            nrc = resp[2]
                            nrc_names = {0x12: 'subFunctionNotSupported', 0x13: 'incorrectMessageLength',
                                         0x14: 'responseTooLong', 0x22: 'conditionsNotCorrect',
                                         0x31: 'requestOutOfRange', 0x33: 'securityAccessDenied',
                                         0x72: 'generalProgrammingFailure', 0x7E: 'subFunctionNotSupportedInActiveSession',
                                         0x7F: 'serviceNotSupportedInActiveSession'}
                            nrc_str = nrc_names.get(nrc, f'0x{nrc:02X}')
                            self._emit(f"DoIP Live: ✗ DID 0x{did:04X} ({label}) on 0x{ecu:04X} → NRC {nrc_str}")
                    except Exception as e:
                        self._emit(f"DoIP Live: ✗ DID 0x{did:04X} ({label}) on 0x{ecu:04X} → {e}")
                    time.sleep(0.05)  # small gap between probes

            self._emit(f"DoIP Live: probing complete — {len(active_dids)} DID(s) active")
            if active_dids:
                self._emit(f"DoIP Live: active DIDs: {', '.join(f'0x{d:04X}({l})' for _, d, l, _ in active_dids)}")
            else:
                self._emit("DoIP Live: ⚠ no data DIDs responded — will still poll MIL status")

            # Use first alive ECU as MIL target (preferring 0x4010 gateway ECU)
            mil_target = alive_ecus[0]

            reconnect_backoff_s = 1.0
            consecutive_errors = 0

            # ---- Phase 3: polling loop ---- #
            while not self._stop_evt.is_set():
                rpm = None
                speed = None
                mil_on = None
                mil_dtc_count = None
                extra = {}

                try:
                    # Keep extended session alive on all ECUs with active DIDs
                    session_ecus = set(e for e, _, _, _ in active_dids)
                    session_ecus.add(mil_target)
                    for ecu in session_ecus:
                        try:
                            doip._uds_transact(ecu, b'\x10\x03', timeout_s=0.5)
                        except Exception:
                            pass

                    # Read all active DIDs
                    for (ecu, did, label, parser_key) in active_dids:
                        hi = (did >> 8) & 0xFF
                        lo = did & 0xFF
                        resp = doip._uds_transact(ecu, bytes([0x22, hi, lo]), timeout_s=1.2)
                        parsed = self._parse_did_value(resp, did, parser_key)
                        if parsed:
                            key, val, unit = parsed
                            if key == 'rpm':
                                rpm = val
                            elif key == 'speed':
                                speed = val
                            elif key in ('coolant_temp', 'battery_soc', 'battery_voltage', 'odometer', 'vin'):
                                extra[key] = val
                        time.sleep(0.08)  # gap between requests

                    # MIL — ReadDTCInformation reportDTCByStatusMask with mask 0x80
                    resp = doip._uds_transact(mil_target, b'\x19\x02\x80', timeout_s=1.2)
                    if resp and len(resp) >= 3 and resp[0] == 0x59 and resp[1] == 0x02:
                        dtc_data = resp[3:]
                        num_dtcs = len(dtc_data) // 4
                        mil_on = bool(num_dtcs > 0)
                        mil_dtc_count = num_dtcs

                    consecutive_errors = 0
                    reconnect_backoff_s = 1.0
                except Exception as e:
                    consecutive_errors += 1
                    self._emit(f"DoIP Live: comm error — {e}. Reconnecting (attempt {consecutive_errors})...")
                    try:
                        doip.close()
                    except Exception:
                        pass
                    doip = None
                    # Exponential backoff: 1s, 2s, 4s, max 10s
                    time.sleep(min(reconnect_backoff_s, 10.0))
                    reconnect_backoff_s = min(reconnect_backoff_s * 2, 10.0)
                    try:
                        doip = DoIPGatewayScanner(gateway_ip, emit_log=self._emit, tester_logical_address=tester_addr)
                        doip.debug_doip = False
                        doip._connect_with_recovery()
                        doip._routing_activation()
                        time.sleep(0.3)
                        # Re-open extended session on all ECUs that need it
                        for ecu in alive_ecus:
                            try:
                                doip._uds_transact(ecu, b'\x10\x03', timeout_s=0.8)
                            except Exception:
                                pass
                    except Exception as e2:
                        self._emit(f"DoIP Live: reconnect failed — {e2}")

                self._emit_vehicle_data(0, rpm, speed, mil_on, mil_dtc_count, extra=extra)
                time.sleep(max(0.5, float(interval_s)))

            self._emit("=== Live Data (DoIP) stopped ===")
            self._set_status(last_summary='live_doip stopped')
        except Exception as e:
            print(f"Live Data (DoIP) failed: {e}")
            self._emit(f"Live Data (DoIP) failed: {e}")
            self._set_status(last_error=str(e))
        finally:
            if doip:
                try:
                    doip.close()
                except Exception:
                    pass
            # Resume Sentinel MIL polling.
            if sentinel and hasattr(sentinel, 'resume_doip_mil'):
                try:
                    sentinel.resume_doip_mil()
                    self._emit('DoIP Live: resumed Sentinel MIL polling')
                except Exception:
                    pass
            self.running = False
            self._set_status(running=False, finished_at=time.time())

    def _run_live(self, channel_id: int, interval_s: float):
        scanner = None
        try:
            if can is None:
                raise ImportError("python-can non installato (pip install python-can)")

            self._emit(f"=== Live Data started on channel {channel_id} ===")
            scanner = VAGScanner(self.bus_manager, channel_id, emit_log=self._emit)

            while not self._stop_evt.is_set():
                rpm, speed, mil_on, mil_dtc_count = scanner.read_live_once(timeout_s=0.25)
                self._emit_vehicle_data(channel_id, rpm, speed, mil_on, mil_dtc_count)
                time.sleep(max(0.05, float(interval_s)))

            self._emit("=== Live Data stopped ===")
            self._set_status(last_summary='live stopped')
        except Exception as e:
            print(f"Live Data failed: {e}")
            self._emit(f"Live Data failed: {e}")
            self._set_status(last_error=str(e))
        finally:
            if scanner:
                try:
                    scanner.close()
                except Exception:
                    pass
            self.running = False
            self._set_status(running=False, finished_at=time.time())

