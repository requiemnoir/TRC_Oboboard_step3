#!/usr/bin/env python3

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from asammdf import MDF


DEFAULT_SIGNALS = [
    'FlexRay.ZAS_Kl_15',
    'FlexRay.MO_Drehzahl_01',
]


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _has_channel(mdf: MDF, name: str) -> bool:
    try:
        mdf.get(name)
        return True
    except Exception:
        return False


def _looks_like_raw_frame_mf4(mdf: MDF) -> bool:
    required = ['CAN_ID', 'Channel', 'BusType', 'Flags']
    return all(_has_channel(mdf, name) for name in required)


def _load_fibex_slot_map(workspace: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    cfg_path = workspace / 'config' / 'app_config.json'
    if not cfg_path.exists():
        return out

    try:
        with cfg_path.open('r', encoding='utf-8') as fh:
            cfg = json.load(fh)
        data_sources = ((cfg or {}).get('config') or {}).get('data_sources') or []
        fibex_name = ''
        for item in data_sources:
            if str((item or {}).get('type') or '').strip().lower() == 'flexray':
                fibex_name = str((item or {}).get('fibex_name') or '').strip()
                if fibex_name:
                    break
        if not fibex_name:
            return out

        import sys
        sys.path.insert(0, str(workspace / 'backend'))
        from fibex_loader import FibexLoader  # type: ignore

        fibex_path = workspace / 'databases' / 'fibex' / fibex_name
        if not fibex_path.exists():
            return out

        loader = FibexLoader()
        if not loader.load(str(fibex_path)):
            return out

        for slot_id, signals in (loader.signals or {}).items():
            variants = loader._variants.get(slot_id, []) or []
            variant_summary = [
                {
                    'base_cycle': variant.get('base_cycle'),
                    'cycle_repetition': variant.get('cycle_repetition'),
                    'name': variant.get('name'),
                    'frame_ref': variant.get('frame_ref'),
                }
                for variant in variants
            ]
            for signal in signals or []:
                name = str((signal or {}).get('name') or '').strip()
                if not name:
                    continue
                out.setdefault(name, []).append(
                    {
                        'slot_id': int(slot_id),
                        'comment': str((signal or {}).get('comment') or ''),
                        'variants': variant_summary,
                    }
                )
    except Exception:
        return out

    return out


def _summarize_signal(mdf: MDF, signal_name: str, gap_factor: float) -> dict[str, Any] | None:
    try:
        sig = mdf.get(signal_name)
    except Exception:
        return None

    timestamps = np.asarray(sig.timestamps, dtype=np.float64)
    samples = np.asarray(sig.samples)
    if timestamps.size == 0:
        return None

    delta = np.diff(timestamps)
    median_dt = float(np.median(delta)) if delta.size else None
    threshold = (median_dt * float(gap_factor)) if median_dt is not None else None
    gap_rows: list[dict[str, Any]] = []
    if threshold is not None:
        idx = np.where(delta > threshold)[0]
        for i in idx.tolist():
            row = {
                'index': int(i),
                'from_ts': float(timestamps[i]),
                'to_ts': float(timestamps[i + 1]),
                'delta_s': float(delta[i]),
            }
            try:
                row['from_value'] = _safe_float(samples[i])
                row['to_value'] = _safe_float(samples[i + 1])
            except Exception:
                pass
            gap_rows.append(row)

    return {
        'signal': signal_name,
        'sample_count': int(timestamps.size),
        'start_ts': float(timestamps[0]),
        'end_ts': float(timestamps[-1]),
        'median_dt_s': median_dt,
        'max_dt_s': float(delta.max()) if delta.size else None,
        'gap_threshold_s': threshold,
        'gap_count': len(gap_rows),
        'gaps': gap_rows,
        'timestamps': timestamps,
        'samples': samples,
    }


def _write_decoded_trace_csv(out_path: Path, summaries: list[dict[str, Any]]) -> None:
    with out_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(['signal', 'timestamp_s', 'value', 'delta_s', 'is_gap'])
        for summary in summaries:
            timestamps = summary['timestamps']
            samples = summary['samples']
            threshold = summary.get('gap_threshold_s')
            for i in range(int(timestamps.size)):
                delta = None if i == 0 else float(timestamps[i] - timestamps[i - 1])
                is_gap = bool(threshold is not None and delta is not None and delta > float(threshold))
                writer.writerow([
                    summary['signal'],
                    float(timestamps[i]),
                    _safe_float(samples[i]),
                    delta,
                    1 if is_gap else 0,
                ])


def _extract_raw_flexray_trace(raw_mf4_path: Path, slot_ids: set[int], gap_factor: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not raw_mf4_path.exists():
        return [], []

    mdf = MDF(str(raw_mf4_path))
    try:
        if not _looks_like_raw_frame_mf4(mdf):
            return [], []

        timestamps = np.asarray(mdf.get('CAN_ID').timestamps, dtype=np.float64)
        frame_ids = np.asarray(mdf.get('CAN_ID').samples, dtype=np.uint32)
        channels = np.asarray(mdf.get('Channel').samples, dtype=np.uint16)
        bus_types = np.asarray(mdf.get('BusType').samples, dtype=np.uint8)
        flags = np.asarray(mdf.get('Flags').samples, dtype=np.uint32)

        mask = (bus_types == 3)
        if slot_ids:
            slot_arr = np.fromiter(sorted(slot_ids), dtype=np.uint32)
            mask &= np.isin(frame_ids, slot_arr)

        timestamps = timestamps[mask]
        frame_ids = frame_ids[mask]
        channels = channels[mask]
        flags = flags[mask]

        rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for slot_id in sorted(set(int(x) for x in frame_ids.tolist())):
            slot_mask = (frame_ids == slot_id)
            t = timestamps[slot_mask]
            ch = channels[slot_mask]
            fl = flags[slot_mask]
            if t.size == 0:
                continue
            order = np.argsort(t)
            t = t[order]
            ch = ch[order]
            fl = fl[order]
            delta = np.diff(t)
            median_dt = float(np.median(delta)) if delta.size else None
            threshold = (median_dt * float(gap_factor)) if median_dt is not None else None
            gap_count = 0
            for i in range(int(t.size)):
                dt = None if i == 0 else float(t[i] - t[i - 1])
                is_gap = bool(threshold is not None and dt is not None and dt > float(threshold))
                if is_gap:
                    gap_count += 1
                rows.append(
                    {
                        'slot_id': int(slot_id),
                        'timestamp_s': float(t[i]),
                        'channel': int(ch[i]),
                        'cycle': int(fl[i]) & 0x3F,
                        'flags': int(fl[i]),
                        'delta_s': dt,
                        'is_gap': 1 if is_gap else 0,
                    }
                )
            summaries.append(
                {
                    'slot_id': int(slot_id),
                    'sample_count': int(t.size),
                    'start_ts': float(t[0]),
                    'end_ts': float(t[-1]),
                    'median_dt_s': median_dt,
                    'max_dt_s': float(delta.max()) if delta.size else None,
                    'gap_threshold_s': threshold,
                    'gap_count': int(gap_count),
                }
            )
        return rows, summaries
    finally:
        try:
            mdf.close()
        except Exception:
            pass


def _write_raw_trace_csv(out_path: Path, rows: list[dict[str, Any]]) -> None:
    with out_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(['slot_id', 'timestamp_s', 'channel', 'cycle', 'flags', 'delta_s', 'is_gap'])
        for row in rows:
            writer.writerow([
                row['slot_id'],
                row['timestamp_s'],
                row['channel'],
                row['cycle'],
                row['flags'],
                row['delta_s'],
                row['is_gap'],
            ])


def _report_payload(
    *,
    session_base: str,
    decoded_mf4_path: Path,
    raw_candidate_path: Path,
    decoded_summaries: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    raw_summaries: list[dict[str, Any]],
    signal_slots: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    decoded_public = []
    for summary in decoded_summaries:
        decoded_public.append(
            {
                'signal': summary['signal'],
                'sample_count': summary['sample_count'],
                'start_ts': summary['start_ts'],
                'end_ts': summary['end_ts'],
                'median_dt_s': summary['median_dt_s'],
                'max_dt_s': summary['max_dt_s'],
                'gap_threshold_s': summary['gap_threshold_s'],
                'gap_count': summary['gap_count'],
                'gaps': summary['gaps'],
                'fibex_slots': signal_slots.get(summary['signal'].split('.', 1)[-1], []),
            }
        )

    if raw_rows:
        conclusion = 'raw_flexray_trace_available_compare_decoded'
    else:
        conclusion = 'cannot_determine_if_loss_starts_in_ethernet_for_this_session'

    return {
        'session_base': session_base,
        'decoded_mf4': str(decoded_mf4_path),
        'raw_candidate_mf4': str(raw_candidate_path),
        'raw_candidate_has_trace': bool(raw_rows),
        'conclusion': conclusion,
        'decoded_signals': decoded_public,
        'raw_flexray_slots': raw_summaries,
        'notes': [
            'Decoded MF4 gaps reflect missing persisted decoded samples, not UI downsampling.',
            'If raw_flexray_slots is empty, this session lacks a saved raw FlexRay artifact suitable for proving whether the loss is already in Ethernet transport.',
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Analyze FlexRay trace gaps across decoded and raw artifacts.')
    parser.add_argument('--workspace', default='.', help='Workspace root')
    parser.add_argument('--session-base', required=True, help='Session base name, for example session_20260316_174423')
    parser.add_argument('--decoded-mf4', default='', help='Override decoded MF4 path')
    parser.add_argument('--raw-mf4', default='', help='Override raw MF4 path')
    parser.add_argument('--signals', nargs='*', default=DEFAULT_SIGNALS, help='Decoded signal names to inspect')
    parser.add_argument('--gap-factor', type=float, default=2.0, help='Gap threshold multiplier applied to the median sample period')
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    logs_dir = workspace / 'logs'
    exports_dir = logs_dir / 'exports'
    exports_dir.mkdir(parents=True, exist_ok=True)

    session_base = str(args.session_base).strip()
    decoded_mf4_path = Path(args.decoded_mf4).resolve() if args.decoded_mf4 else (logs_dir / f'{session_base}.mf4')
    raw_candidate_path = Path(args.raw_mf4).resolve() if args.raw_mf4 else decoded_mf4_path

    if not decoded_mf4_path.exists():
        raise FileNotFoundError(f'decoded MF4 not found: {decoded_mf4_path}')

    signal_slots = _load_fibex_slot_map(workspace)

    decoded_mdf = MDF(str(decoded_mf4_path))
    try:
        decoded_summaries = []
        target_slot_ids: set[int] = set()
        for signal_name in args.signals:
            summary = _summarize_signal(decoded_mdf, signal_name, float(args.gap_factor))
            if summary is None:
                continue
            decoded_summaries.append(summary)
            short_name = signal_name.split('.', 1)[-1]
            for slot_info in signal_slots.get(short_name, []):
                try:
                    target_slot_ids.add(int(slot_info.get('slot_id')))
                except Exception:
                    continue
    finally:
        try:
            decoded_mdf.close()
        except Exception:
            pass

    decoded_trace_csv = exports_dir / f'{session_base}_flexray_decoded_trace.csv'
    raw_trace_csv = exports_dir / f'{session_base}_flexray_raw_trace.csv'
    report_json = exports_dir / f'{session_base}_flexray_gap_report.json'

    _write_decoded_trace_csv(decoded_trace_csv, decoded_summaries)

    raw_rows, raw_summaries = _extract_raw_flexray_trace(raw_candidate_path, target_slot_ids, float(args.gap_factor))
    _write_raw_trace_csv(raw_trace_csv, raw_rows)

    report = _report_payload(
        session_base=session_base,
        decoded_mf4_path=decoded_mf4_path,
        raw_candidate_path=raw_candidate_path,
        decoded_summaries=decoded_summaries,
        raw_rows=raw_rows,
        raw_summaries=raw_summaries,
        signal_slots=signal_slots,
    )
    with report_json.open('w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)

    print(json.dumps({
        'decoded_trace_csv': str(decoded_trace_csv),
        'raw_trace_csv': str(raw_trace_csv),
        'report_json': str(report_json),
        'decoded_signals_found': [item['signal'] for item in decoded_summaries],
        'raw_slot_summaries': raw_summaries,
        'conclusion': report['conclusion'],
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())