#!/usr/bin/env python3
"""Decode raw CAN MF4 logs to CSV using one or more DBC files.

This is intended to be run *after acquisition* when MF4 is stored as raw frames only
(`MF4_INCLUDE_DECODED=0`). It avoids inflating MF4 size and CPU/RAM at capture time.

Usage examples:
  .venv/bin/python scripts/decode_mf4_to_csv.py \
      --mf4 logs/session_20260121_152608.mf4 \
      --dbc databases/dbc/MLBevo_Gen2_MLBevo_DCAN_KMatrix_V8.24.00F_20220602_VP.dbc \
      --out decoded.csv

  # Decode a whole session from chunked parts:
  .venv/bin/python scripts/decode_mf4_to_csv.py \
      --mf4 logs/session_20260121_152608_part0000.mf4 \
      --dbc databases/dbc/DCAN.dbc --dbc databases/dbc/HCAN.dbc \
      --out decoded.csv

Notes:
- The MF4 produced by this project stores raw frames as signals:
  CAN_ID, DLC, Channel, Flags, DataByte0..DataByte7 (timestamps as seconds).
- This script reads those and runs cantools decode. It outputs one CSV row per frame.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional


def _load_dbc(path: str):
    import cantools

    # strict=False helps with many OEM/Vector quirks.
    return cantools.database.load_file(path, strict=False)


def _open_mf4(path: str):
    from asammdf import MDF

    return MDF(path)


def _bytes_from_signals(mdf, base_timestamps, prefix: str = "DataByte") -> List[List[int]]:
    # Expect DataByte0..DataByte7
    cols = []
    for i in range(8):
        sig = mdf.get(f"{prefix}{i}")
        cols.append(sig.samples)
    out = []
    n = len(base_timestamps)
    for idx in range(n):
        out.append([int(cols[i][idx]) & 0xFF for i in range(8)])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mf4", required=True, help="Path to a raw MF4 chunk or consolidated MF4")
    ap.add_argument("--dbc", action="append", default=[], help="DBC file path (repeatable)")
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument("--max-frames", type=int, default=0, help="Optional limit for quick tests")
    args = ap.parse_args()

    mf4_path = str(args.mf4)
    dbc_paths = [str(p) for p in (args.dbc or [])]
    out_path = str(args.out)

    if not dbc_paths:
        raise SystemExit("At least one --dbc is required")

    dbs = []
    for p in dbc_paths:
        dbs.append(_load_dbc(p))

    mdf = _open_mf4(mf4_path)
    try:
        can_id = mdf.get("CAN_ID")
        dlc = mdf.get("DLC")
        ch = mdf.get("Channel")
        flags = mdf.get("Flags")
        t = can_id.timestamps

        n = len(t)
        if args.max_frames and args.max_frames > 0:
            n = min(n, int(args.max_frames))

        data_bytes = _bytes_from_signals(mdf, t)[:n]

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp_ms",
                "channel",
                "id_hex",
                "dlc",
                "flags",
                "message_name",
                "signals_json",
                "data_hex",
            ])

            for i in range(n):
                ts_ms = int(float(t[i]) * 1000.0)
                frame_id = int(can_id.samples[i])
                frame_dlc = int(dlc.samples[i])
                frame_ch = int(ch.samples[i])
                frame_flags = int(flags.samples[i])
                payload = data_bytes[i][: max(0, min(frame_dlc, 8))]

                decoded_name: Optional[str] = None
                decoded_signals: Optional[Dict[str, Any]] = None
                for db in dbs:
                    try:
                        msg = db.get_message_by_frame_id(frame_id)
                    except Exception:
                        continue
                    try:
                        decoded = msg.decode(bytes(payload))
                        decoded_name = msg.name
                        decoded_signals = decoded
                        break
                    except Exception:
                        continue

                w.writerow([
                    ts_ms,
                    frame_ch,
                    hex(frame_id),
                    frame_dlc,
                    frame_flags,
                    decoded_name or "",
                    json.dumps(decoded_signals or {}, ensure_ascii=False),
                    "".join(f"{b:02x}" for b in payload),
                ])

    finally:
        try:
            mdf.close()
        except Exception:
            pass

    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
