#!/usr/bin/env python3

import os
import sys
import time

# Allow importing backend modules when running from repo root
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(BASE_DIR, 'backend'))

from vag_scanner import (
    DtcItem,
    EcuReport,
    _get_log_dir_default,
    _parse_uds_snapshot_response,
    _parse_uds_extended_data_response,
    _write_vag_html_report,
)


def main() -> int:
    did_index = {
        0xF40D: {'long_name': 'Odometer', 'byte_length': 4},
        0xF190: {'long_name': 'VIN', 'byte_length': 17},
        0x0C00: {'long_name': 'EngineSpeedRaw', 'byte_length': 2},
    }

    # Fake snapshot response: 59 04 DTC(3) Status Record FF NumDIDs=1 DID=F40D val=0x00004B10 (19216)
    snap = bytes.fromhex('59 04 12 34 56 08 FF 01 F4 0D 00 00 4B 10')
    snap_parsed = _parse_uds_snapshot_response(snap, did_index)

    # Fake extended data response: 59 06 DTC(3) Status Record FF data...
    ext = bytes.fromhex('59 06 12 34 56 08 FF 01 02 03 04 05 06')
    ext_parsed = _parse_uds_extended_data_response(ext, did_index)

    dtc = DtcItem(
        code='P123456',
        uds_dtc=0x123456,
        status_byte=0x08,
        status_desc='Confirmed',
        active=False,
        raw='12 34 56 08',
        description='',
        extra={
            'snapshots': [snap_parsed] if snap_parsed else [],
            'extended_data': [ext_parsed] if ext_parsed else [],
            'odometer_km': 19216,
        },
    )

    reports = [EcuReport(tx_id=0x0E00, rx_id=0x0001, name='ECU LA 0x0001 (SIM)', dtcs=[dtc])]
    dtc_map = {'P123456': 'Example translated DTC description (from PDX map)'}

    log_dir = _get_log_dir_default()
    os.makedirs(log_dir, exist_ok=True)
    out_name = f"simulated_idex_like_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
    out_path = os.path.join(log_dir, out_name)

    _write_vag_html_report(
        out_path,
        reports,
        title='Simulated Diagnostic Report',
        subtitle='Offline simulation (no vehicle) - verifies km/context rendering',
        dtc_map=dtc_map,
    )

    print(out_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
