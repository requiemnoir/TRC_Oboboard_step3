#!/usr/bin/env python3
"""Validate decoded MF4 readability and consistency for MDA-like usage.

Checks performed:
- raw mirror presence (CAN/FR)
- decoded file can be opened and scanned
- channel/sample readability on a representative subset
- monotonic timestamps and relative-time sanity
- normalized overlap with reference (handles CANxxx./FLX prefixes)
- round-trip save/open structural integrity
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import numpy as np
from asammdf import MDF


def _norm_name(name: str) -> str:
    s = str(name or "").strip()
    s = re.sub(r"^CAN\d+\.", "", s)
    s = re.sub(r"^FLX\s*Monitoring\s*[AB]:\d+\.", "", s)
    s = re.sub(r"^LIN\d+\.", "", s)
    return s


def validate(raw_path: Path, decoded_path: Path, reference_path: Path) -> dict:
    out: dict = {
        "raw": str(raw_path),
        "decoded": str(decoded_path),
        "reference": str(reference_path),
        "checks": {},
    }

    if not raw_path.exists():
        out["checks"]["raw_exists"] = False
        out["pass"] = False
        return out
    if not decoded_path.exists():
        out["checks"]["decoded_exists"] = False
        out["pass"] = False
        return out
    if not reference_path.exists():
        out["checks"]["reference_exists"] = False
        out["pass"] = False
        return out

    out["checks"]["raw_exists"] = True
    out["checks"]["decoded_exists"] = True
    out["checks"]["reference_exists"] = True

    m_raw = MDF(str(raw_path))
    m_dec = MDF(str(decoded_path))
    m_ref = MDF(str(reference_path))
    try:
        # Raw mirror presence
        raw_names = set(m_raw.channels_db.keys())
        if "Channel" in raw_names:
            ch = np.asarray(m_raw.get("Channel").samples)
            mirror_can = int(np.sum((ch >= 100) & (ch < 200)))
            mirror_fr = int(np.sum((ch >= 200) & (ch < 300)))
        else:
            mirror_can = 0
            mirror_fr = 0
        out["checks"]["raw_mirror_can_rows"] = mirror_can
        out["checks"]["raw_mirror_fr_rows"] = mirror_fr
        out["checks"]["raw_mirror_present"] = bool((mirror_can + mirror_fr) > 0)

        # Decoded basic structure
        dec_names = sorted(set(m_dec.channels_db.keys()))
        ref_names = sorted(set(m_ref.channels_db.keys()))
        out["checks"]["decoded_channel_count"] = len(dec_names)
        out["checks"]["decoded_group_count"] = int(len(getattr(m_dec, "groups", []) or []))
        out["checks"]["decoded_has_channels"] = len(dec_names) > 0

        eth_like = [n for n in dec_names if n.startswith("Ethernet.") or n.startswith("XCP:") or n.startswith("ETH_") or n.startswith("DOIP_") or n.startswith("SOMEIP_")]
        out["checks"]["ethernet_like_channels"] = len(eth_like)

        # Sample readability
        sample = dec_names[:500]
        ok_read = 0
        nonempty = 0
        monotonic = 0
        rel_time = 0
        for n in sample:
            try:
                s = m_dec.get(n)
                t = np.asarray(s.timestamps)
                y = np.asarray(s.samples)
                ok_read += 1
                if y.size > 0:
                    nonempty += 1
                if t.size > 1 and np.all(np.diff(t) >= -1e-12):
                    monotonic += 1
                if t.size > 0 and float(np.nanmax(t)) < 1e7:
                    rel_time += 1
            except Exception:
                continue

        out["checks"]["sample_checked"] = len(sample)
        out["checks"]["sample_read_ok"] = ok_read
        out["checks"]["sample_nonempty"] = nonempty
        out["checks"]["sample_monotonic_ts"] = monotonic
        out["checks"]["sample_relative_time"] = rel_time

        # Normalized overlap with reference
        n_dec = {_norm_name(x) for x in dec_names}
        n_ref = {_norm_name(x) for x in ref_names}
        inter = n_dec & n_ref
        out["checks"]["normalized_overlap_count"] = len(inter)
        out["checks"]["normalized_overlap_pct_vs_decoded"] = round(100.0 * len(inter) / max(1, len(n_dec)), 2)
        out["checks"]["normalized_overlap_pct_vs_reference"] = round(100.0 * len(inter) / max(1, len(n_ref)), 2)

        # Round-trip save/open integrity
        roundtrip_ok = False
        roundtrip_channels = 0
        try:
            with tempfile.TemporaryDirectory() as td:
                rt_path = Path(td) / "rt.mf4"
                m_dec.save(str(rt_path), overwrite=True)
                m_rt = MDF(str(rt_path))
                try:
                    roundtrip_ok = True
                    roundtrip_channels = len(set(m_rt.channels_db.keys()))
                finally:
                    m_rt.close()
        except Exception:
            roundtrip_ok = False
            roundtrip_channels = 0

        out["checks"]["roundtrip_open_ok"] = roundtrip_ok
        out["checks"]["roundtrip_channel_count"] = roundtrip_channels

        # PASS/FAIL gates for MDA-friendly readiness
        gates = {
            "raw_mirror_present": bool(out["checks"]["raw_mirror_present"]),
            "decoded_has_channels": bool(out["checks"]["decoded_has_channels"]),
            "sample_readability": ok_read >= max(1, int(0.98 * len(sample))),
            "sample_nonempty": nonempty >= max(1, int(0.98 * len(sample))),
            "sample_monotonic": monotonic >= max(1, int(0.98 * len(sample))),
            "sample_relative_time": rel_time >= max(1, int(0.95 * len(sample))),
            "normalized_overlap": len(inter) >= 500,
            "roundtrip_open_ok": bool(roundtrip_ok),
        }

        out["gates"] = gates
        out["pass"] = all(gates.values())
    finally:
        try:
            m_raw.close()
        except Exception:
            pass
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
    parser = argparse.ArgumentParser(description="Validate MF4 decoded trace for MDA readability")
    parser.add_argument("--raw", required=True, help="Raw MF4 path")
    parser.add_argument("--decoded", required=True, help="Decoded MF4 path")
    parser.add_argument("--reference", required=True, help="Reference MF4 path")
    args = parser.parse_args()

    report = validate(Path(args.raw), Path(args.decoded), Path(args.reference))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
