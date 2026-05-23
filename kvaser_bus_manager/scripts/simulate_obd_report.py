#!/usr/bin/env python3
"""Simulate a complete OBD report and save it as an HTML file.

This is a *mock* report generator meant for demos/tests, without requiring a real vehicle.
It does not copy any external report content; it generates plausible, synthetic data.

Output
- Writes an HTML file into the log directory (defaults to ../logs or $KBSM_LOG_DIR)
- Prints the filename so it can be downloaded via /api/logs/<name> if the backend is running

Usage
  ./.venv/bin/python scripts/simulate_obd_report.py
  ./.venv/bin/python scripts/simulate_obd_report.py --vin WAUZZZFA0JN000001 --odometer-km 123456
"""

from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


def _log_dir_default() -> Path:
    env = str(os.getenv("KBSM_LOG_DIR") or "").strip()
    if env:
        return Path(env).resolve()
    # repo_root/scripts -> repo_root
    root = Path(__file__).resolve().parents[1]
    return (root / "logs").resolve()


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rand_hex(n: int) -> str:
    return "".join(random.choice("0123456789ABCDEF") for _ in range(n))


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@dataclass(frozen=True)
class Dtc:
    code: str
    status: str  # ACTIVE/PENDING/PERMANENT/STORED
    status_byte: Optional[int]
    raw: str
    description: str


@dataclass(frozen=True)
class Ecu:
    name: str
    address: str
    protocol: str
    dtcs: List[Dtc]


def _make_mock_data(vin: str, *, seed: int = 0) -> Dict:
    if seed:
        random.seed(seed)

    # Basic vehicle snapshot
    make = random.choice(["VAG", "VW", "Audi", "Skoda", "Seat"])
    model = random.choice(["A3", "A4", "Golf", "Passat", "Octavia", "Leon"])
    year = random.choice([2016, 2017, 2018, 2019, 2020, 2021, 2022])

    # Readiness monitors (typical subset)
    readiness = {
        "MIL": random.choice(["OFF", "ON"]),
        "DTCs_Stored": random.choice(["NO", "YES"]),
        "Misfire": random.choice(["READY", "NOT READY"]),
        "FuelSystem": random.choice(["READY", "NOT READY"]),
        "Comprehensive": "READY",
        "Catalyst": random.choice(["READY", "NOT READY"]),
        "HeatedCatalyst": random.choice(["READY", "NOT READY"]),
        "Evap": random.choice(["READY", "NOT READY"]),
        "SecondaryAir": random.choice(["READY", "NOT READY"]),
        "ACRefrigerant": random.choice(["READY", "NOT READY"]),
        "O2Sensor": random.choice(["READY", "NOT READY"]),
        "O2Heater": random.choice(["READY", "NOT READY"]),
        "EGR": random.choice(["READY", "NOT READY"]),
    }

    # Live data snapshot (Mode 01)
    live = {
        "EngineRPM": round(random.uniform(700, 2400), 0),
        "VehicleSpeed_kph": round(random.uniform(0, 130), 0),
        "CoolantTemp_C": round(random.uniform(70, 102), 0),
        "IntakeAirTemp_C": round(random.uniform(15, 55), 0),
        "ThrottlePos_pct": round(random.uniform(8, 42), 1),
        "FuelLevel_pct": round(random.uniform(10, 95), 1),
        "MAF_gps": round(random.uniform(2.0, 18.0), 2),
        "MAP_kPa": round(random.uniform(25, 95), 0),
        "Battery_V": round(random.uniform(12.0, 14.6), 2),
        "Runtime_s": int(random.uniform(30, 3600)),
    }

    # Freeze frame (Mode 02)
    freeze = {
        "DTC": random.choice(["P0300", "P0420", "P0171", "P0130"]),
        "EngineRPM": round(random.uniform(900, 3000), 0),
        "VehicleSpeed_kph": round(random.uniform(0, 120), 0),
        "CoolantTemp_C": round(random.uniform(60, 100), 0),
        "Load_pct": round(random.uniform(10, 85), 1),
    }

    # Supported PIDs (illustrative)
    supported_pids = [
        "01-00", "01-20", "01-40", "01-60", "01-80", "01-A0",
        "09-00", "09-02 (VIN)", "09-04 (CAL ID)", "09-06 (CVN)",
    ]

    # ECUs (mock)
    def dtc(code: str, status: str) -> Dtc:
        sb = random.choice([0x20, 0x28, 0x2F, 0x08, 0x00])
        raw = " ".join(_rand_hex(2) for _ in range(8))
        return Dtc(code=code, status=status, status_byte=sb, raw=raw, description="")

    engine_dtcs = [dtc("P0300", "ACTIVE"), dtc("P0420", "PENDING")] if readiness["MIL"] == "ON" else [dtc("P0130", "STORED")]
    abs_dtcs = [dtc("C0035", "STORED")] if random.random() < 0.3 else []
    airbag_dtcs = [dtc("B1000", "STORED")] if random.random() < 0.2 else []

    ecus = [
        Ecu(name="Engine", address="0x7E0/0x7E8", protocol="ISO-TP (11-bit)", dtcs=engine_dtcs),
        Ecu(name="Transmission", address="0x7E1/0x7E9", protocol="ISO-TP (11-bit)", dtcs=[]),
        Ecu(name="ABS/ESP", address="0x7E2/0x7EA", protocol="ISO-TP (11-bit)", dtcs=abs_dtcs),
        Ecu(name="Airbag", address="0x7E3/0x7EB", protocol="ISO-TP (11-bit)", dtcs=airbag_dtcs),
    ]

    # Mode 09 identifiers
    cal_id = f"{make}-{model}-{year}-CAL-{_rand_hex(8)}"
    cvn = _rand_hex(8)

    counts = {
        "active": sum(1 for e in ecus for d in e.dtcs if d.status == "ACTIVE"),
        "pending": sum(1 for e in ecus for d in e.dtcs if d.status == "PENDING"),
        "stored": sum(1 for e in ecus for d in e.dtcs if d.status in {"STORED", "ACTIVE"}),
        "permanent": sum(1 for e in ecus for d in e.dtcs if d.status == "PERMANENT"),
    }

    return {
        "vehicle": {"make": make, "model": model, "year": year, "vin": vin},
        "readiness": readiness,
        "live": live,
        "freeze": freeze,
        "supported_pids": supported_pids,
        "mode09": {"vin": vin, "cal_id": cal_id, "cvn": cvn},
        "ecus": ecus,
        "counts": counts,
    }


def _render_html(data: Dict, *, title: str) -> str:
    vehicle = data["vehicle"]
    readiness = data["readiness"]
    live = data["live"]
    freeze = data["freeze"]
    supported_pids = data["supported_pids"]
    mode09 = data["mode09"]
    ecus: List[Ecu] = data["ecus"]
    counts = data["counts"]

    css = """
    :root {
      color-scheme: dark;
      --bg0: #07080b;
      --bg1: #0c0f14;
      --panel: rgba(13,15,20,0.96);
      --stroke: #232631;
      --stroke2: #2e3443;
      --text: #f3f5f7;
      --muted: #aab0bb;
      --accent: #f6c000;
      --accent2: #ffdd55;
    }
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      margin: 18px;
      background:
        radial-gradient(900px 600px at 8% 0%, rgba(246,192,0,0.10), transparent 55%),
        radial-gradient(800px 500px at 100% 20%, rgba(246,192,0,0.06), transparent 55%),
        linear-gradient(180deg, var(--bg0), #05060a 60%, var(--bg0));
      color: var(--text);
    }
    .wrap { max-width: 1100px; margin: 0 auto; }
    .topbar { height: 10px; background: linear-gradient(90deg, var(--accent), rgba(246,192,0,0.0)); border-radius: 999px; margin-bottom: 14px; filter: drop-shadow(0 0 10px rgba(246,192,0,0.25)); }
    .card {
      border: 1px solid var(--stroke);
      border-radius: 14px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(13,15,20,0.96), rgba(10,12,16,0.96));
      box-shadow: 0 0 0 1px rgba(246,192,0,0.06), 0 16px 40px rgba(0,0,0,0.55);
      position: relative;
      overflow: hidden;
    }
    .card:before {
      content: '';
      position: absolute;
      top: -80px;
      right: -140px;
      width: 260px;
      height: 260px;
      background: radial-gradient(circle, rgba(246,192,0,0.18), transparent 60%);
      transform: rotate(18deg);
    }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0.6px; text-transform: uppercase; }
    h2 { margin: 0 0 8px 0; font-size: 14px; letter-spacing: 0.5px; text-transform: uppercase; }
    .muted { color: var(--muted); }
    .grid2 { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }
    .badge { display: inline-flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid rgba(246,192,0,0.35); border-radius: 999px; color: var(--accent2); font-size: 12px; letter-spacing: 0.5px; text-transform: uppercase; background: rgba(246,192,0,0.06); }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px rgba(246,192,0,0.18); }
    .kpi { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 12px; }
    .k { border: 1px solid var(--stroke2); border-radius: 14px; padding: 10px; background: rgba(255,255,255,0.02); }
    .k .t { color: var(--muted); font-size: 12px; letter-spacing: 0.4px; text-transform: uppercase; }
    .k .v { margin-top: 6px; font-size: 18px; font-weight: 800; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--stroke); padding: 10px 10px; vertical-align: top; }
    th { text-align: left; font-size: 12px; color: #d7dbe2; letter-spacing: 0.3px; text-transform: uppercase; }
    .num { text-align: right; width: 100px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    details { margin-top: 10px; border: 1px solid var(--stroke); border-radius: 14px; overflow: hidden; background: rgba(13,15,20,0.90); }
    summary { cursor: pointer; padding: 12px 12px; list-style: none; }
    summary::-webkit-details-marker { display: none; }
    .box { padding: 10px 12px 14px 12px; }
    .pill { display: inline-block; padding: 2px 10px; border: 1px solid var(--stroke2); border-radius: 999px; margin-left: 6px; font-size: 12px; color: #d7dbe2; background: rgba(255,255,255,0.02); }
    .pill-strong { border-color: rgba(246,192,0,0.40); color: var(--accent2); background: rgba(246,192,0,0.06); }
    .state { font-weight: 800; letter-spacing: 0.4px; }
    .state-active { color: var(--accent2); }
    .state-muted { color: var(--muted); }
    .footer { margin-top: 12px; color: var(--muted); font-size: 12px; }
    """

    # ECU summary table
    ecu_rows = []
    for e in ecus:
        active = sum(1 for d in e.dtcs if d.status == "ACTIVE")
        pending = sum(1 for d in e.dtcs if d.status == "PENDING")
        total = len(e.dtcs)
        ecu_rows.append(
            f"<tr><td>{_esc(e.name)}</td><td class='mono'>{_esc(e.address)}</td><td>{_esc(e.protocol)}</td><td class='num'>{active}</td><td class='num'>{pending}</td><td class='num'>{total}</td></tr>"
        )

    # ECU detail sections
    ecu_sections = []
    for e in ecus:
        active = sum(1 for d in e.dtcs if d.status == "ACTIVE")
        pending = sum(1 for d in e.dtcs if d.status == "PENDING")
        total = len(e.dtcs)
        dtc_rows = []
        for d in e.dtcs:
            sb = "" if d.status_byte is None else f"0x{int(d.status_byte):02X}"
            state_cls = "state-active" if d.status == "ACTIVE" else "state-muted"
            dtc_rows.append(
                "<tr>"
                f"<td class='mono'>{_esc(d.code)}</td>"
                f"<td><span class='state {state_cls}'>{_esc(d.status)}</span></td>"
                f"<td class='mono'>{_esc(sb)}</td>"
                f"<td class='mono'>{_esc(d.raw)}</td>"
                f"<td class='muted'>{_esc(d.description)}</td>"
                "</tr>"
            )
        if not dtc_rows:
            dtc_rows.append("<tr><td colspan='5' class='muted'>No DTCs reported.</td></tr>")

        ecu_sections.append(
            f"<details>"
            f"<summary><strong>{_esc(e.name)}</strong> <span class='pill pill-strong'>active {active}</span> <span class='pill'>pending {pending}</span> <span class='pill'>total {total}</span></summary>"
            "<div class='box'>"
            "<table>"
            "<thead><tr><th>DTC</th><th>Status</th><th>Status Byte</th><th>Raw</th><th>Description</th></tr></thead>"
            f"<tbody>{''.join(dtc_rows)}</tbody>"
            "</table>"
            "</div>"
            "</details>"
        )

    # Readiness rows
    rd_rows = []
    for k, v in readiness.items():
        rd_rows.append(f"<tr><td>{_esc(k)}</td><td class='mono'>{_esc(str(v))}</td></tr>")

    # Live rows
    live_rows = []
    for k, v in live.items():
        live_rows.append(f"<tr><td>{_esc(k)}</td><td class='mono'>{_esc(str(v))}</td></tr>")

    # Freeze rows
    fr_rows = []
    for k, v in freeze.items():
        fr_rows.append(f"<tr><td>{_esc(k)}</td><td class='mono'>{_esc(str(v))}</td></tr>")

    pid_rows = "".join(f"<span class='pill'>{_esc(p)}</span>" for p in supported_pids)

    html = f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{_esc(title)}</title>
  <style>{css}</style>
</head>
<body>
  <div class='wrap'>
    <div class='topbar'></div>

    <div class='card'>
      <div class='grid2'>
        <div>
          <div style='font-weight: 900; font-size: 20px; letter-spacing: 0.6px;'>Kvaser Bus Manager</div>
          <div class='muted' style='margin-top: 2px;'>Simulated OBD Report</div>
          <h1 style='margin-top: 10px;'>{_esc(title)}</h1>
          <div class='muted'>Generated: {_esc(_ts())}</div>
          <div class='muted'>Vehicle: {_esc(vehicle['make'])} {_esc(vehicle['model'])} ({vehicle['year']})</div>
          <div class='muted'>VIN: <span class='mono'>{_esc(vehicle['vin'])}</span></div>
        </div>
        <div class='badge'><span class='dot'></span> OBD / Diagnostics</div>
      </div>

      <div class='kpi'>
        <div class='k'><div class='t'>MIL</div><div class='v'>{_esc(readiness['MIL'])}</div></div>
        <div class='k'><div class='t'>Active DTCs</div><div class='v'>{counts['active']}</div></div>
        <div class='k'><div class='t'>Pending DTCs</div><div class='v'>{counts['pending']}</div></div>
        <div class='k'><div class='t'>Stored DTCs</div><div class='v'>{counts['stored']}</div></div>
      </div>

      <div class='footer'>This is synthetic data for demo/testing only.</div>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>ECU Summary</h2>
      <table>
        <thead><tr><th>ECU</th><th>Address</th><th>Protocol</th><th class='num'>Active</th><th class='num'>Pending</th><th class='num'>Total</th></tr></thead>
        <tbody>
          {''.join(ecu_rows) if ecu_rows else "<tr><td colspan='6' class='muted'>No ECUs found.</td></tr>"}
        </tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Readiness (Mode 01)</h2>
      <table>
        <thead><tr><th>Monitor</th><th>State</th></tr></thead>
        <tbody>{''.join(rd_rows)}</tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Live Snapshot (Mode 01)</h2>
      <table>
        <thead><tr><th>PID</th><th>Value</th></tr></thead>
        <tbody>{''.join(live_rows)}</tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Freeze Frame (Mode 02)</h2>
      <table>
        <thead><tr><th>Field</th><th>Value</th></tr></thead>
        <tbody>{''.join(fr_rows)}</tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Mode 09 Identifiers</h2>
      <table>
        <thead><tr><th>Item</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>VIN (09-02)</td><td class='mono'>{_esc(mode09['vin'])}</td></tr>
          <tr><td>CAL ID (09-04)</td><td class='mono'>{_esc(mode09['cal_id'])}</td></tr>
          <tr><td>CVN (09-06)</td><td class='mono'>{_esc(mode09['cvn'])}</td></tr>
        </tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Supported PID Ranges</h2>
      <div style='margin-top: 8px; line-height: 2.1;'>
        {pid_rows}
      </div>
    </div>

    <div style='margin-top: 12px;'>
      {''.join(ecu_sections)}
    </div>

    <div class='footer'>Generated by simulate_obd_report.py</div>
  </div>
</body>
</html>"""

    return html


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a simulated OBD report (HTML)")
    ap.add_argument("--vin", default="WAUZZZFA0JN000001", help="VIN to print in the report")
    ap.add_argument("--title", default="OBD Diagnostic Report", help="Report title")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (0 disables)")
    ap.add_argument("--out", default="", help="Output HTML path (defaults to logs/obd_report_<timestamp>.html)")
    args = ap.parse_args()

    log_dir = _log_dir_default()
    log_dir.mkdir(parents=True, exist_ok=True)

    data = _make_mock_data(str(args.vin), seed=int(args.seed))
    html = _render_html(data, title=str(args.title))

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        name = f"obd_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
        out_path = (log_dir / name).resolve()

    out_path.write_text(html, encoding="utf-8")
    print(str(out_path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
