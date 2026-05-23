#!/usr/bin/env python3
import os
import sys
import time


def _import_backend_vag_scanner():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    proj_dir = os.path.abspath(os.path.join(base_dir, '..'))
    backend_dir = os.path.join(proj_dir, 'backend')
    sys.path.insert(0, backend_dir)
    import vag_scanner  # type: ignore
    return vag_scanner


vag_scanner = _import_backend_vag_scanner()
DtcItem = vag_scanner.DtcItem
EcuReport = vag_scanner.EcuReport
_write_vag_html_report = vag_scanner._write_vag_html_report


def _dtc(code: str, status: int, raw: str, active: bool) -> DtcItem:
    # Status meaning mirrors the scanner logic
    def _decode(status_byte: int) -> str:
        flags = []
        if status_byte & 0x01:
            flags.append('TestFailed')
        if status_byte & 0x02:
            flags.append('FailedThisCycle')
        if status_byte & 0x04:
            flags.append('Pending')
        if status_byte & 0x08:
            flags.append('Confirmed')
        if status_byte & 0x10:
            flags.append('NotTestedSinceClear')
        if status_byte & 0x20:
            flags.append('FailedSinceClear')
        if status_byte & 0x40:
            flags.append('NotTestedThisCycle')
        if status_byte & 0x80:
            flags.append('WarningRequested')
        return ', '.join(flags) if flags else 'OK'

    return DtcItem(
        code=code,
        status_byte=status,
        status_desc=_decode(status),
        active=bool(active),
        raw=raw,
        description='',
    )


def generate_obd_uds_report(log_dir: str) -> str:
    reports = [
        EcuReport(
            tx_id=0x7E0,
            rx_id=0x7E8,
            name='Engine (1.8TFSI ECU)',
            dtcs=[
                _dtc('P0016A3', 0x0B, '00 16 A3 0B', True),
                _dtc('P030000', 0x04, '03 00 00 04', True),
                _dtc('P042000', 0x10, '04 20 00 10', False),
            ],
        ),
        EcuReport(
            tx_id=0x7E1,
            rx_id=0x7E9,
            name='Transmission (DQ250)',
            dtcs=[
                _dtc('P17BF00', 0x08, '17 BF 00 08', True),
            ],
        ),
        EcuReport(
            tx_id=0x7E2,
            rx_id=0x7EA,
            name='ABS/ESP (MK60)',
            dtcs=[
                _dtc('P012101', 0x40, '01 21 01 40', False),
            ],
        ),
        EcuReport(
            tx_id=0x7E3,
            rx_id=0x7EB,
            name='Airbag (J234)',
            dtcs=[],
        ),
    ]

    name = f"sample_vag_obd_uds_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
    path = os.path.join(log_dir, name)
    _write_vag_html_report(
        path,
        reports,
        title='VAG OBD/UDS Scan Report (SIMULATED)',
        subtitle='Transport: CAN (ISO-TP) | Vehicle: Audi/VAG (example)',
    )
    return name


def generate_doip_report(log_dir: str) -> str:
    reports = [
        EcuReport(
            tx_id=0x0E00,
            rx_id=0x0019,
            name='ECU LA 0x0019 (Gateway)',
            dtcs=[
                _dtc('P160900', 0x20, '16 09 00 20', False),
            ],
        ),
        EcuReport(
            tx_id=0x0E00,
            rx_id=0x0021,
            name='ECU LA 0x0021 (Engine)',
            dtcs=[
                _dtc('P0016A3', 0x0B, '00 16 A3 0B', True),
                _dtc('P04EF00', 0x01, '04 EF 00 01', True),
            ],
        ),
        EcuReport(
            tx_id=0x0E00,
            rx_id=0x0030,
            name='ECU LA 0x0030 (ABS/ESP)',
            dtcs=[
                _dtc('P012101', 0x40, '01 21 01 40', False),
            ],
        ),
        EcuReport(
            tx_id=0x0E00,
            rx_id=0x0042,
            name='ECU LA 0x0042 (Infotainment)',
            dtcs=[
                _dtc('P0B2000', 0x10, '0B 20 00 10', False),
            ],
        ),
    ]

    name = f"sample_vag_doip_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
    path = os.path.join(log_dir, name)
    _write_vag_html_report(
        path,
        reports,
        title='VAG DoIP Scan Report (SIMULATED)',
        subtitle='Transport: DoIP via Gateway | Tester: 0x0E00 | ECU addresses simulated',
    )
    return name


def main() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    proj_dir = os.path.abspath(os.path.join(base_dir, '..'))
    log_dir = os.path.join(proj_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    obd_name = generate_obd_uds_report(log_dir)
    doip_name = generate_doip_report(log_dir)

    print('Generated reports:')
    print(f"- {obd_name}")
    print(f"- {doip_name}")
    print('\nOpen locally or via the app log download route:')
    print(f"- /api/logs/{obd_name}")
    print(f"- /api/logs/{doip_name}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
