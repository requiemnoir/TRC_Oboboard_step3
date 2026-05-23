"""Centralized MF4 decoding package.

All decoding logic (DBC, ARXML, FIBEX, MF4 raw→decoded conversion)
lives here.  The rest of the application imports from this package.

Public API
----------
Low-level loaders / decoders:
    DBCLoader, load_dbc_database, _normalize_decoded_signals   (dbc_loader)
    ArxmlDecoder                                                (arxml_decoder)
    ArxmlCatalog, parse_arxml, parse_arxml_files,
        get_active_catalog, load_catalog_from_directory,
        list_arxml_files                                        (arxml_parser)
    FibexLoader                                                 (fibex_loader)

High-level MF4 decoder classes:
    MF4Decoder              — app-level decoder (supports bus_manager)
    StandaloneMF4Decoder    — CLI / offline decoder (parallel, cached)

MF4 export helpers:
    export_decoded_mf4_from_raw
    load_raw_frame_table, load_raw_can_table
    export_ethernet_numeric_mf4, merge_ethernet_numeric_channels_into_mf4
"""

from .dbc_loader import DBCLoader, load_dbc_database, _normalize_decoded_signals
from .arxml_parser import (
    ArxmlCatalog,
    parse_arxml,
    parse_arxml_files,
    get_active_catalog,
    load_catalog_from_directory,
    list_arxml_files,
)
from .arxml_decoder import ArxmlDecoder
from .fibex_loader import FibexLoader
from .decode_mf4 import (
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

__all__ = [
    # Loaders
    'DBCLoader', 'load_dbc_database', '_normalize_decoded_signals',
    'ArxmlDecoder',
    'ArxmlCatalog', 'parse_arxml', 'parse_arxml_files',
    'get_active_catalog', 'load_catalog_from_directory', 'list_arxml_files',
    'FibexLoader',
    # Decoder classes
    'MF4Decoder', 'StandaloneMF4Decoder',
    # Export helpers
    'export_decoded_mf4_from_raw',
    'load_raw_frame_table', 'load_raw_can_table',
    'export_ethernet_numeric_mf4', 'merge_ethernet_numeric_channels_into_mf4',
    # Utility
    'CAN_ID_MASK', '_coerce_numeric', '_safe_mf4_group_name',
    '_mask_can_id', '_bus_type_to_frame_type',
    '_sanitize_mf4_name_part', '_live_mf4_channel_label',
    '_live_mf4_signal_name', '_live_mf4_signal_name_with_message',
    '_mf4_has_ethernet_metrics',
]
