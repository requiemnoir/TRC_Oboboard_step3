#!/usr/bin/env python3
"""Generate an MDF4 (.mf4) trace with DBC-decoded CAN signals **and**
FIBEX-decoded FlexRay signals from the VAG mirror payload.

The output MF4 contains one channel per decoded signal (e.g. ESP_v_Signal,
EPS_Zahnstangen_Pos, BMS_IstSpannung, BCM_02_CRC …) and is directly readable
in Vector MDA, ETAS INCA, CANape, or asammdf GUI.

Usage:
    python3 scripts/generate_decoded_mf4.py [options]

Examples:
    # Default: use golden payload, repeat 500×, write to logs/
    python3 scripts/generate_decoded_mf4.py

    # Custom output & repeats
    python3 scripts/generate_decoded_mf4.py --repeat 1000 --out /tmp/vehicle_trace
"""

import argparse
import glob
import os
import struct
import sys
import time

# Ensure backend importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "backend"))

import numpy as np
from asammdf import MDF, Signal
from dbc_loader import load_dbc_database
from fibex_loader import FibexLoader


# ---------------------------------------------------------------------------
#  VAG mirror parser (standalone, mirrors ethernet_capture logic)
#  Now supports both CAN (ntype=1) and FlexRay (ntype=0)
# ---------------------------------------------------------------------------
def parse_vag_mirror_payload(payload: bytes):
    """Parse a raw VAG mirror inner payload (after stripping SOME/IP header)
    and return list of (bus_ch, frame_id, data_bytes, net_type_str, ts_offset_us).

    net_type_str is "CAN" or "FlexRay".
    For FlexRay frames, frame_id is the SlotID (high byte of the 2-byte id field).
    ts_offset_us is the intra-packet timestamp offset in microseconds,
    derived from the 2-byte timestamp field in each record:
      - CAN:     high_byte × 256 µs  (low byte is always 0x00)
      - FlexRay: raw value in µs (macrotick)
    """
    if not payload or len(payload) < 16:
        return []

    def _try_at(off):
        if off + 10 > len(payload):
            return None

        ts_raw = struct.unpack("!H", payload[off : off + 2])[0]
        bus_ch = payload[off + 2]
        ntype = payload[off + 3]
        raw_id = struct.unpack("!H", payload[off + 6 : off + 8])[0]
        dlc = struct.unpack("!H", payload[off + 8 : off + 10])[0]

        if bus_ch > 15:
            return None

        if ntype == 1:
            # CAN frame — ts is high_byte * 256 µs
            if dlc > 64 or dlc == 0:
                return None
            end = off + 10 + dlc
            if end > len(payload):
                return None
            ts_us = (ts_raw >> 8) * 256
            return (10 + dlc, bus_ch, raw_id, payload[off + 10 : end], "CAN", ts_us)
        elif ntype == 0:
            # FlexRay frame: id field = [SlotID:1][CycleCount:1]
            slot_id = (raw_id >> 8) & 0xFF
            cycle = raw_id & 0xFF
            if slot_id == 0:
                return None  # slot 0 not valid; likely status block
            if dlc < 8 or dlc > 254:
                return None
            if cycle > 63:
                return None
            end = off + 10 + dlc
            if end > len(payload):
                return None
            # FlexRay ts: if low byte is 0x00, same encoding as CAN (bus-ch > 1),
            # otherwise raw µs macrotick value
            if (ts_raw & 0xFF) == 0x00 and bus_ch > 1:
                ts_us = (ts_raw >> 8) * 256
            else:
                ts_us = ts_raw
            return (10 + dlc, bus_ch, slot_id, payload[off + 10 : end], "FlexRay", ts_us)
        else:
            return None

    best_frames, best_score = [], -1
    for start in [4, 3, 2, 0]:
        frames, score, i = [], 0, start
        while i + 10 <= len(payload):
            hit = _try_at(i)
            if hit is None:
                i += 1
                continue
            consumed, bus_ch, frame_id, data, net_type, ts_us = hit
            frames.append((bus_ch, frame_id, bytes(data), net_type, ts_us))
            if net_type == "FlexRay":
                score += 2
            else:
                score += 1
            i += consumed
        if frames and score > best_score:
            best_frames, best_score = frames, score
    return best_frames


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Generate decoded-signal MF4 from mirror payload (CAN + FlexRay)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument(
        "--sample",
        default=os.path.join(ROOT_DIR, "tests", "data", "vag_mirror_single_payload.bin"),
        help="Path to raw SOME/IP mirror payload (default: golden sample)",
    )
    src.add_argument(
        "--samples-dir",
        default=None,
        help="Directory containing multiple payload files (each file is one packet)",
    )
    src.add_argument(
        "--samples-glob",
        default=None,
        help="Glob pattern for multiple payload files (each file is one packet)",
    )

    ap.add_argument(
        "--repeat",
        type=int,
        default=500,
        help="Number of times to repeat the payload (simulate packets). Used only with --sample.",
    )
    ap.add_argument("--out", default=os.path.join(ROOT_DIR, "logs", "mirror_decoded"), help="Output path (without extension)")
    ap.add_argument("--dbc-dir", default=os.path.join(ROOT_DIR, "databases", "dbc"), help="Directory containing .dbc files")
    ap.add_argument("--fibex-dir", default=os.path.join(ROOT_DIR, "databases", "fibex"), help="Directory containing .xml FIBEX files")
    args = ap.parse_args()

    # --- 1a. Load all DBC databases (CAN) ---
    dbc_files = sorted(glob.glob(os.path.join(args.dbc_dir, "*.dbc")))
    databases = []
    for f in dbc_files:
        try:
            db = load_dbc_database(f)
            databases.append(db)
            print(f"  DBC loaded: {os.path.basename(f)} ({len(db.messages)} messages)")
        except Exception as e:
            print(f"  DBC skip:   {os.path.basename(f)} ({e})")

    # Build CAN decode lookup: frame_id → [(message, db), ...]
    # Multiple DBCs can define the same frame id (different buses/variants);
    # we try candidates until one decodes successfully.
    decode_map: dict[int, list[tuple]] = {}
    for db in databases:
        for msg in db.messages:
            fid = msg.frame_id
            decode_map.setdefault(fid, []).append((msg, db))
    print(f"  CAN decode map: {len(decode_map)} unique frame IDs from {len(databases)} DBC(s)")

    # --- 1b. Load FIBEX databases (FlexRay) ---
    fibex_files = sorted(glob.glob(os.path.join(args.fibex_dir, "*.xml")))
    fibex = FibexLoader()
    fibex_loaded = False
    for f in fibex_files:
        try:
            fibex.load(f)
            fibex_loaded = True
            print(f"  FIBEX loaded: {os.path.basename(f)} ({len(fibex.frames)} frames, "
                  f"{sum(len(v) for v in fibex._signal_defs.values())} signal defs)")
        except Exception as e:
            print(f"  FIBEX skip: {os.path.basename(f)} ({e})")

    if not databases and not fibex_loaded:
        print("ERROR: no DBC or FIBEX databases loaded")
        sys.exit(1)

    # --- 2. Load & parse payload(s) ---
    def _strip_someip(raw_bytes: bytes) -> bytes:
        if len(raw_bytes) >= 20:
            srv = struct.unpack("!H", raw_bytes[0:2])[0]
            met = struct.unpack("!H", raw_bytes[2:4])[0]
            if srv == 0x02FD and met == 0xF302:
                return raw_bytes[16:]
        return raw_bytes

    payload_files: list[str]
    if args.samples_dir:
        payload_files = sorted(
            os.path.join(args.samples_dir, f)
            for f in os.listdir(args.samples_dir)
            if os.path.isfile(os.path.join(args.samples_dir, f))
        )
    elif args.samples_glob:
        payload_files = sorted(glob.glob(args.samples_glob))
    else:
        payload_files = [args.sample]

    if not payload_files:
        print("ERROR: no payload files found")
        sys.exit(1)

    packets_frames = []  # list[list[(bus_ch, frame_id, data, net_type, ts_offset_us)]]
    for fp in payload_files:
        try:
            with open(fp, "rb") as fh:
                raw = fh.read()
        except Exception:
            continue

        inner = _strip_someip(raw)
        frames = parse_vag_mirror_payload(inner)
        if frames:
            # Sort frames by intra-packet timestamp for monotonic output
            frames.sort(key=lambda f: f[4])
            packets_frames.append(frames)

    if not packets_frames:
        print("ERROR: no frames parsed from provided payload(s)")
        sys.exit(1)

    # In single-sample mode we repeat the same packet to synthesize time series.
    if not args.samples_dir and not args.samples_glob:
        packets_frames = packets_frames * max(1, int(args.repeat))

    can_count = sum(1 for f in packets_frames[0] if f[3] == "CAN")
    fr_count = sum(1 for f in packets_frames[0] if f[3] == "FlexRay")
    print(f"  Parsed {len(packets_frames[0])} frames per payload packet ({can_count} CAN, {fr_count} FlexRay)")
    print(f"  Packets used: {len(packets_frames)}")

    # --- 3. Decode all frames × repeats, collect per-signal timeseries ---
    signal_data: dict[str, dict] = {}
    # Raw channel groups
    raw_can_timestamps = []
    raw_can_ids = []
    raw_can_dlcs = []
    raw_can_buses = []
    raw_fr_timestamps = []
    raw_fr_slots = []
    raw_fr_dlcs = []
    raw_fr_buses = []

    t0 = time.time()
    # VAG MLBevo mirror sends UDP packets at ~15 Hz (every ~67 ms).
    # Each packet contains CAN+FlexRay records with intra-packet timestamp
    # offsets covering one FlexRay cycle (~65 ms for CAN, ~1-2 ms for FR).
    MIRROR_PACKET_INTERVAL_S = 0.067  # 67 ms between UDP packets

    total_can_decoded = 0
    total_fr_decoded = 0
    total_raw = 0

    unique_ids_seen: set[int] = set()
    unique_ids_decoded: set[int] = set()

    for rep, template_frames in enumerate(packets_frames):
        pkt_base = rep * MIRROR_PACKET_INTERVAL_S
        for idx, frame_tuple in enumerate(template_frames):
            bus_ch, frame_id, data, net_type, ts_offset_us = frame_tuple
            # Absolute timestamp = packet base + intra-packet offset (µs → s)
            ts = pkt_base + ts_offset_us * 1e-6

            if net_type == "CAN":
                arb_id = frame_id & 0x1FFFFFFF
                unique_ids_seen.add(arb_id)
                raw_can_timestamps.append(ts)
                raw_can_ids.append(arb_id)
                raw_can_dlcs.append(len(data))
                raw_can_buses.append(100 + bus_ch)

                # DBC decode
                candidates = decode_map.get(arb_id)
                if not candidates:
                    total_raw += 1
                    continue

                decoded = None
                msg = None
                for cand_msg, _cand_db in candidates:
                    try:
                        payload = bytes(data)
                        if len(payload) < cand_msg.length:
                            payload = payload + b'\x00' * (cand_msg.length - len(payload))
                        decoded = cand_msg.decode(payload)
                        msg = cand_msg
                        break
                    except Exception:
                        continue

                if decoded is None or msg is None:
                    total_raw += 1
                    continue

                unique_ids_decoded.add(arb_id)
                total_can_decoded += 1
                for sig_name, sig_val in decoded.items():
                    if isinstance(sig_val, str):
                        numeric_val = float(hash(sig_val) & 0xFFFFFFFF)
                    elif isinstance(sig_val, (int, float)):
                        numeric_val = float(sig_val)
                    else:
                        continue

                    full_name = f"{msg.name}.{sig_name}"
                    if full_name not in signal_data:
                        unit = ""
                        for s in msg.signals:
                            if s.name == sig_name:
                                unit = s.unit or ""
                                break
                        signal_data[full_name] = {
                            "timestamps": [], "values": [], "unit": unit,
                            "is_enum": isinstance(sig_val, str), "bus": "CAN",
                        }
                    signal_data[full_name]["timestamps"].append(ts)
                    signal_data[full_name]["values"].append(numeric_val)

            elif net_type == "FlexRay":
                slot_id = frame_id
                raw_fr_timestamps.append(ts)
                raw_fr_slots.append(slot_id)
                raw_fr_dlcs.append(len(data))
                raw_fr_buses.append(200 + bus_ch)

                # FIBEX decode
                if not fibex_loaded or slot_id not in fibex.frames:
                    total_raw += 1
                    continue

                result = fibex.decode(slot_id, data)
                if result is None:
                    total_raw += 1
                    continue

                fr_name = result.get("name", f"FR_Slot{slot_id}")
                sigs = result.get("signals", {})
                if not sigs or (len(sigs) == 1 and "raw_hex" in sigs):
                    total_raw += 1
                    continue

                total_fr_decoded += 1
                for sig_name, sig_val in sigs.items():
                    if sig_name == "raw_hex":
                        continue
                    if sig_name.endswith("_txt"):
                        continue  # text-table labels, skip for numeric MF4
                    if not isinstance(sig_val, (int, float)):
                        continue

                    full_name = f"FR_{sig_name}"
                    if full_name not in signal_data:
                        signal_data[full_name] = {
                            "timestamps": [], "values": [], "unit": "",
                            "is_enum": False, "bus": "FlexRay",
                        }
                    signal_data[full_name]["timestamps"].append(ts)
                    signal_data[full_name]["values"].append(float(sig_val))

    elapsed = time.time() - t0
    total_frames = sum(len(frames) for frames in packets_frames)
    print(f"  Processed {total_frames} frames in {elapsed:.2f}s")
    print(f"    CAN decoded:     {total_can_decoded}")
    print(f"    FlexRay decoded: {total_fr_decoded}")
    print(f"    Raw-only:        {total_raw}")
    print(f"  Unique signals:    {len(signal_data)}")
    if unique_ids_seen:
        print(f"  Unique CAN IDs seen:    {len(unique_ids_seen)}")
        print(f"  Unique CAN IDs decoded: {len(unique_ids_decoded)}")

    # --- 4. Build MF4 ---
    mdf = MDF()

    # Group 1: Decoded CAN numeric signals
    can_numeric = []
    for sig_name, sd in sorted(signal_data.items()):
        if sd["bus"] != "CAN" or sd["is_enum"]:
            continue
        ts_arr = np.array(sd["timestamps"], dtype=np.float64)
        val_arr = np.array(sd["values"], dtype=np.float64)
        sig = Signal(samples=val_arr, timestamps=ts_arr, name=sig_name, unit=sd["unit"])
        can_numeric.append(sig)

    if can_numeric:
        mdf.append(can_numeric)
        print(f"  MF4 group 1: {len(can_numeric)} CAN numeric signals")

    # Group 2: Decoded FlexRay numeric signals
    fr_numeric = []
    for sig_name, sd in sorted(signal_data.items()):
        if sd["bus"] != "FlexRay":
            continue
        ts_arr = np.array(sd["timestamps"], dtype=np.float64)
        val_arr = np.array(sd["values"], dtype=np.float64)
        sig = Signal(samples=val_arr, timestamps=ts_arr, name=sig_name, unit=sd["unit"])
        fr_numeric.append(sig)

    if fr_numeric:
        mdf.append(fr_numeric)
        print(f"  MF4 group 2: {len(fr_numeric)} FlexRay numeric signals")

    # Group 3: Enum/state signals (CAN)
    enum_signals = []
    for sig_name, sd in sorted(signal_data.items()):
        if not sd["is_enum"]:
            continue
        ts_arr = np.array(sd["timestamps"], dtype=np.float64)
        val_arr = np.array(sd["values"], dtype=np.float64)
        sig = Signal(samples=val_arr, timestamps=ts_arr, name=sig_name, unit="enum")
        enum_signals.append(sig)

    if enum_signals:
        mdf.append(enum_signals)
        print(f"  MF4 group 3: {len(enum_signals)} enum signals")

    # Group 4: Raw CAN frames
    if raw_can_timestamps:
        raw_ts = np.array(raw_can_timestamps, dtype=np.float64)
        raw_sigs = [
            Signal(samples=np.array(raw_can_ids, dtype=np.uint32), timestamps=raw_ts, name="CAN_ID", unit=""),
            Signal(samples=np.array(raw_can_dlcs, dtype=np.uint8), timestamps=raw_ts, name="CAN_DLC", unit="bytes"),
            Signal(samples=np.array(raw_can_buses, dtype=np.uint16), timestamps=raw_ts, name="CAN_Channel", unit=""),
        ]
        mdf.append(raw_sigs)
        print(f"  MF4 group 4: raw CAN (ID+DLC+Channel), {len(raw_ts)} samples")

    # Group 5: Raw FlexRay frames
    if raw_fr_timestamps:
        fr_ts = np.array(raw_fr_timestamps, dtype=np.float64)
        fr_sigs = [
            Signal(samples=np.array(raw_fr_slots, dtype=np.uint16), timestamps=fr_ts, name="FR_SlotID", unit=""),
            Signal(samples=np.array(raw_fr_dlcs, dtype=np.uint8), timestamps=fr_ts, name="FR_DLC", unit="bytes"),
            Signal(samples=np.array(raw_fr_buses, dtype=np.uint16), timestamps=fr_ts, name="FR_Channel", unit=""),
        ]
        mdf.append(fr_sigs)
        print(f"  MF4 group 5: raw FlexRay (SlotID+DLC+Channel), {len(fr_ts)} samples")

    # --- 5. Write MF4 ---
    out_mf4 = args.out + ".mf4"
    os.makedirs(os.path.dirname(out_mf4) or ".", exist_ok=True)
    mdf.save(out_mf4, overwrite=True)
    size_kb = os.path.getsize(out_mf4) / 1024
    print(f"\n✅  Written: {out_mf4}  ({size_kb:.1f} KB)")

    # --- 6. Print signal summary ---
    print(f"\n{'─'*72}")
    print(f" CAN signal summary (first 20 numeric):")
    print(f"{'─'*72}")
    count = 0
    for sig_name, sd in sorted(signal_data.items()):
        if sd["bus"] != "CAN" or sd["is_enum"]:
            continue
        last_val = sd["values"][-1] if sd["values"] else 0
        unit = sd["unit"]
        print(f"  {sig_name:<48} = {last_val:>12.4f}  [{unit}]")
        count += 1
        if count >= 20:
            remaining = sum(1 for s, d in signal_data.items() if d["bus"] == "CAN" and not d["is_enum"]) - 20
            if remaining > 0:
                print(f"  ... and {remaining} more CAN numeric signals")
            break

    print(f"\n{'─'*72}")
    print(f" FlexRay signal summary (first 30 numeric):")
    print(f"{'─'*72}")
    count = 0
    for sig_name, sd in sorted(signal_data.items()):
        if sd["bus"] != "FlexRay":
            continue
        last_val = sd["values"][-1] if sd["values"] else 0
        unit = sd["unit"]
        print(f"  {sig_name:<48} = {last_val:>12.4f}  [{unit}]")
        count += 1
        if count >= 30:
            remaining = sum(1 for s, d in signal_data.items() if d["bus"] == "FlexRay") - 30
            if remaining > 0:
                print(f"  ... and {remaining} more FlexRay signals")
            break

    if count == 0:
        print("  (no FlexRay signals decoded — check FIBEX file)")

    print(f"{'─'*72}")


if __name__ == "__main__":
    main()
