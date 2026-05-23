"""ARXML decoder — thin re-export shim.

All decoding logic is centralized in ``mf4_standalone_decoder``.
This module re-exports the public API so existing imports continue to work.
"""
import os as _os
import sys as _sys

_PROJECT_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

from mf4_standalone_decoder.arxml_decoder import ArxmlDecoder  # noqa: F401, E402
