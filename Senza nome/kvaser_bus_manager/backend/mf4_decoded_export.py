"""MF4 decoded export — thin re-export shim.

All decoding logic is centralized in ``mf4_standalone_decoder``.
This module re-exports the public API so existing imports continue to work.
"""
import os as _os
import sys as _sys

_PROJECT_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

from mf4_standalone_decoder.decode_mf4 import (  # noqa: F401, E402
    MF4Decoder,
    StandaloneMF4Decoder,
    export_decoded_mf4_from_raw,
    load_raw_frame_table,
    load_raw_can_table,
    export_ethernet_numeric_mf4,
    merge_ethernet_numeric_channels_into_mf4,
    CAN_ID_MASK,
    _coerce_numeric,
    _safe_mf4_group_name,
    _mask_can_id,
    _bus_type_to_frame_type,
    _sanitize_mf4_name_part,
    _live_mf4_channel_label,
    _live_mf4_signal_name,
    _live_mf4_signal_name_with_message,
    _mf4_has_ethernet_metrics,
)
