"""DBC loader — thin re-export shim.

All decoding logic is centralized in ``mf4_standalone_decoder``.
This module re-exports the public API so existing imports continue to work.
"""
import os as _os
import sys as _sys

# Ensure the project root is on sys.path so the package is importable.
_PROJECT_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

from mf4_standalone_decoder.dbc_loader import (  # noqa: F401, E402
    DBCLoader,
    load_dbc_database,
    _normalize_decoded_signals,
    _sanitize_dbc_text,
    _load_arxml_deduped,
)
