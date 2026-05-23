#!/usr/bin/env python3
"""Verify raw mirror presence and decoded alignment against a reference MF4.

Usage examples:
  ./.venv/bin/python scripts/verify_trace_alignment.py \
      --raw /mnt/ssd/logs/session_20260410_085902_part0000.mf4 \
      --decoded /mnt/ssd/logs/exports/session_20260410_085902_part0000_coded_autoverify_20260410.mf4 \
      --reference /mnt/ssd/logs/exports/2025-09-15_11.25_LB63X_ema.mf4
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from asammdf import MDF


def summarize_raw(raw_path: Path) -> dict:
    out = {
        "raw_file": str(raw_path),
        "exists": raw_path.exists(),
    }
    if not raw_path.exists():
        return out

    mdf = MDF(str(raw_path))
    try:
        names = set(mdf.channels_db.keys())
        out["raw_channel_count"] = len(names)

        if "Channel" not in names:
            out["error"] = "Channel signal not found"
            return out

        ch = np.asarray(mdf.get("Channel").samples)
        out["rows"] = int(ch.size)
        out["mirror_can_rows_100_199"] = int(np.sum((ch >= 100) & (ch < 200)))
        out["mirror_flexray_rows_200_299"] = int(np.sum((ch >= 200) & (ch < 300)))
        uniq = np.unique(ch.astype(np.int64, copy=False))
        out["channel_unique_sample"] = uniq[:20].tolist()
    finally:
        try:
            mdf.close()
        except Exception:
            pass

    return out


def summarize_decoded(decoded_path: Path, reference_path: Path) -> dict:
    out = {
        "decoded_file": str(decoded_path),
        "reference_file": str(reference_path),
        "decoded_exists": decoded_path.exists(),
        "reference_exists": reference_path.exists(),
    }
    if not decoded_path.exists() or not reference_path.exists():
        return out

    m_dec = MDF(str(decoded_path))
    m_ref = MDF(str(reference_path))
    try:
        dec_names = set(m_dec.channels_db.keys())
        ref_names = set(m_ref.channels_db.keys())

        inter = dec_names & ref_names
        union = dec_names | ref_names

        out["decoded_channel_count"] = len(dec_names)
        out["reference_channel_count"] = len(ref_names)
        out["overlap_count"] = len(inter)
        out["overlap_pct_vs_decoded"] = round(100.0 * len(inter) / max(1, len(dec_names)), 2)
        out["overlap_pct_vs_reference"] = round(100.0 * len(inter) / max(1, len(ref_names)), 2)
        out["jaccard_pct"] = round(100.0 * len(inter) / max(1, len(union)), 2)
        out["overlap_sample"] = sorted(inter)[:40]
    finally:
        try:
            m_dec.close()
        except Exception:
            pass
        try:
            m_ref.close()
        except Exception:
            pass

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify mirror presence and decoded alignment")
    parser.add_argument("--raw", required=True, help="Path to raw MF4 session file")
    parser.add_argument("--decoded", required=False, help="Path to decoded/coded MF4 export")
    parser.add_argument("--reference", required=True, help="Reference decoded MF4 to compare against")
    args = parser.parse_args()

    raw_path = Path(args.raw)
    reference_path = Path(args.reference)
    decoded_path = Path(args.decoded) if args.decoded else None

    report = {
        "raw": summarize_raw(raw_path),
    }

    if decoded_path is not None:
        report["decoded"] = summarize_decoded(decoded_path, reference_path)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
