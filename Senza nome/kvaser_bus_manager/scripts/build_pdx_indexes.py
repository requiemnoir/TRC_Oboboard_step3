#!/usr/bin/env python3
"""Build and cache PDX-derived indexes (DTC + DID).

Why this exists
- The web app builds indexes during PDX import, but on devices already deployed you
  may have PDX files without the cached *.dtc_index.json / *.did_index.json.
- This script makes index generation explicit and repeatable.

Usage
  python -m kvaser_bus_manager.scripts.build_pdx_indexes --pdx /path/to/file.pdx

Exit codes
  0 success
  2 invalid args / file not found
  3 build failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdx", required=True, help="Path to a .pdx file")
    ap.add_argument("--max-seconds", type=float, default=120.0)
    ap.add_argument("--no-dtc", action="store_true")
    ap.add_argument("--no-did", action="store_true")
    ap.add_argument("--no-comm", action="store_true", help="Skip comm/addressing index")
    args = ap.parse_args(argv)

    pdx = os.path.abspath(str(args.pdx))
    if not os.path.isfile(pdx) or not pdx.lower().endswith(".pdx"):
        print(f"error: invalid pdx path: {pdx}", file=sys.stderr)
        return 2

    from kvaser_bus_manager.backend.pdx_parser import build_comm_index_from_pdx, build_dtc_index_from_pdx, build_did_index_from_pdx

    t0 = time.time()
    try:
        if not args.no_dtc:
            dtc = build_dtc_index_from_pdx(pdx, max_files=None, max_seconds=float(args.max_seconds))
            out = pdx + ".dtc_index.json"
            _write_json(out, dtc)
            print(f"wrote {out} (dtc_count={dtc.get('dtc_count')}, files_scanned={dtc.get('files_scanned')})")

        if not args.no_did:
            did = build_did_index_from_pdx(pdx, max_files=None, max_seconds=float(args.max_seconds))
            out = pdx + ".did_index.json"
            _write_json(out, did)
            print(f"wrote {out} (did_count={did.get('did_count')}, files_scanned={did.get('files_scanned')})")

        if not args.no_comm:
            comm = build_comm_index_from_pdx(pdx, max_files=None, max_seconds=float(args.max_seconds))
            out = pdx + ".comm_index.json"
            _write_json(out, comm)
            print(f"wrote {out} (ecu_count={comm.get('ecu_count')}, files_scanned={comm.get('files_scanned')})")

    except Exception as e:
        print(f"error: build failed: {e}", file=sys.stderr)
        return 3

    print(f"done in {int((time.time()-t0)*1000)} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
