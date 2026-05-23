import os
import sys


BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))


from comparison_engine import _safe_float
from dbc_loader import DBCLoader


DBC_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "databases",
        "dbc",
        "MLBevo_Gen2_MLBevo_CCAN_KMatrix_V8.24.00F_20220602_SEn.dbc",
    )
)


def test_dbc_loader_normalizes_choice_signals_to_numeric_with_text_sidecar():
    loader = DBCLoader()
    assert loader.load(DBC_PATH)

    decoded = loader.decode(0x3C0, [0] * 8)

    assert decoded is not None
    assert decoded["name"] == "Klemmen_Status_01"
    assert decoded["signals"]["ZAS_Kl_15"] == 0
    assert decoded["signals"]["ZAS_Kl_15_txt"] == "aus"


class _EnumLike:
    def __init__(self, value):
        self.value = value


def test_safe_float_accepts_enum_like_value_objects():
    assert _safe_float(_EnumLike(1)) == 1.0
    assert _safe_float(_EnumLike(False)) == 0.0