#!/usr/bin/env python3
"""Simulate a UDS-on-OBD (VAG-style) diagnostic report and save it as HTML.

This generator produces synthetic (fake) but *plausible* UDS artifacts:
- ECU/module list with VAG logical addresses
- UDS services summary (0x10, 0x22, 0x19, optional 0x27)
- Common VAG-ish DIDs (VIN F190, ECU part numbers, SW/HW ids, coding, WSC/importer/equipment)
- DTC list with UDS status bits and snapshot-like data

It does NOT reuse/copy external report contents.

Usage
  ./.venv/bin/python scripts/simulate_uds_vag_report.py
  ./.venv/bin/python scripts/simulate_uds_vag_report.py --seed 42 --vin WAUZZZFA0JN000001

Output
- Writes HTML under logs/ (defaults to ../logs or $KBSM_LOG_DIR)
"""

from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _log_dir_default() -> Path:
    env = str(os.getenv("KBSM_LOG_DIR") or "").strip()
    if env:
        return Path(env).resolve()
    root = Path(__file__).resolve().parents[1]
    return (root / "logs").resolve()


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _rand_hex(n: int) -> str:
    return "".join(random.choice("0123456789ABCDEF") for _ in range(n))


def _rand_bytes(n: int) -> str:
    return " ".join(_rand_hex(2) for _ in range(n))


def _maybe(p: float) -> bool:
    return random.random() < p


@dataclass(frozen=True)
class UdsDid:
    did: str  # e.g. F190
    name: str
    value: str
    raw: Optional[str] = None


@dataclass(frozen=True)
class UdsDtc:
    dtc: str  # e.g. P0300 or 0x123456
    status: str  # ACTIVE/STORED/PENDING
    status_byte: int
    occurrence: int
    raw: str
    text: str


@dataclass(frozen=True)
class UdsEcu:
    vag_addr: str  # e.g. 01
    name: str
    uds_physical_req: str  # CAN ID / extended ID string
    uds_physical_res: str
    sessions: List[str]
    security: str
    dids: List[UdsDid]
    dtcs: List[UdsDtc]


def _vag_module_catalog() -> List[Tuple[str, str]]:
    # (VAG logical address, module name)
    return [
        ("01", "Engine"),
        ("02", "Transmission"),
        ("03", "ABS/ESP"),
        ("08", "HVAC"),
        ("09", "Central Electrics"),
        ("13", "Adaptive Cruise"),
        ("15", "Airbag"),
        ("17", "Instrument Cluster"),
        ("19", "Gateway"),
        ("5F", "Information Electronics"),
    ]


def _make_ids_for_vag_addr(vag_addr_hex: str) -> Tuple[str, str]:
    """Return plausible diagnostic CAN identifiers for a module.

    Many VAG modules use 29-bit ISO-TP addressing (18DAxxF1). For simulation we provide both
    a human-readable string and stability in the report.
    """

    # 18DA <TA> <SA> where SA is tester address (F1)
    ta = vag_addr_hex.upper()
    req = f"0x18DA{ta}F1 (29-bit ISO-TP)"
    res = f"0x18DAF1{ta} (29-bit ISO-TP)"
    return req, res


def _make_common_dids(*, vin: str, module_hint: str) -> List[UdsDid]:
    brand_prefix = random.choice(["8V0", "5Q0", "3Q0", "4M0", "8W0", "2Q0"])
    part_suffix = random.choice(["A", "B", "C", "D", "E", "F", "H", "K"])  # not real mapping
    pn = f"{brand_prefix} 907 379 {part_suffix}" if module_hint in {"Engine", "Gateway"} else f"{brand_prefix} 920 790 {part_suffix}"

    sw = f"{random.randint(1000, 9999)}"
    hw = f"{random.randint(1000, 9999)}"
    asam = f"EV_{module_hint.replace(' ', '')}_{_rand_hex(6)}"
    coding = _rand_hex(10)
    wsc = f"{random.randint(1, 99999):05d}"
    importer = f"{random.randint(1, 999):03d}"
    equipment = f"{random.randint(1, 99999):05d}"

    return [
        UdsDid(did="F190", name="VIN", value=vin, raw=None),
        UdsDid(did="F187", name="ECU Part Number", value=pn, raw=_rand_bytes(16)),
        UdsDid(did="F189", name="ECU Software Version", value=sw, raw=_rand_bytes(8)),
        UdsDid(did="F191", name="ECU Hardware Number", value=hw, raw=_rand_bytes(8)),
        UdsDid(did="F18C", name="ASAM/ODX Dataset", value=asam, raw=_rand_bytes(12)),
        UdsDid(did="F186", name="Serial Number", value=_rand_hex(10), raw=_rand_bytes(8)),
        UdsDid(did="0600", name="Coding", value=coding, raw=_rand_bytes(8)),
        UdsDid(did="F1A5", name="Workshop Code (WSC/Importer/Equipment)", value=f"{wsc}-{importer}-{equipment}", raw=_rand_bytes(6)),
    ]


def _make_dtcs_for_module(name: str) -> List[UdsDtc]:
    # Status bits are UDS-ish; not perfect but representative.
    # Common bits: 0x08 testFailedThisOpCycle, 0x20 testFailedSinceClear, 0x40 pendingDTC, 0x80 confirmedDTC
    candidates = {
        "Engine": [
            ("P0300", "ACTIVE", 0xA8, "Random/Multiple Cylinder Misfire Detected"),
            ("P0420", "PENDING", 0x48, "Catalyst System Efficiency Below Threshold"),
            ("P0171", "STORED", 0xA0, "System Too Lean (Bank 1)"),
        ],
        "ABS/ESP": [
            ("C0035", "STORED", 0xA0, "Left Front Wheel Speed Sensor"),
            ("U0121", "PENDING", 0x48, "Lost Communication With ABS"),
        ],
        "Airbag": [
            ("B1000", "STORED", 0xA0, "Airbag Control Unit - Internal Fault"),
        ],
        "Gateway": [
            ("U0100", "PENDING", 0x48, "Lost Communication With ECM/PCM"),
        ],
        "Instrument Cluster": [
            ("U0155", "STORED", 0xA0, "Lost Communication With IPC"),
        ],
    }

    base = candidates.get(name, [])
    if not base:
        return []

    # Pick 0..2 dtcs depending on probability
    if _maybe(0.45):
        pick = random.sample(base, k=min(len(base), random.choice([1, 1, 2])))
    else:
        pick = []

    out: List[UdsDtc] = []
    for code, status, status_byte, text in pick:
        occ = random.randint(1, 25)
        raw = _rand_bytes(10)
        out.append(UdsDtc(dtc=code, status=status, status_byte=status_byte, occurrence=occ, raw=raw, text=text))
    return out


def _make_ecus(vin: str) -> List[UdsEcu]:
    modules = _vag_module_catalog()
    ecus: List[UdsEcu] = []

    for vag_addr, name in modules:
        req, res = _make_ids_for_vag_addr(vag_addr)
        sessions = ["Default (0x01)"]
        if _maybe(0.6):
            sessions.append("Extended Diagnostic (0x03)")
        if _maybe(0.25):
            sessions.append("Programming (0x02)")

        security = "Not Required"
        if "Programming" in " ".join(sessions) and _maybe(0.5):
            security = "Seed/Key (0x27)"

        dids = _make_common_dids(vin=vin, module_hint=name)

        # Add module-specific DIDs
        if name in {"Engine", "Transmission"}:
            dids.append(UdsDid(did="F40D", name="Calibration ID", value=_rand_hex(16), raw=_rand_bytes(8)))
            dids.append(UdsDid(did="F10A", name="ECU Name", value=f"{name} Controller", raw=_rand_bytes(10)))
        elif name in {"Gateway", "Central Electrics"}:
            dids.append(UdsDid(did="F197", name="System Supplier", value=random.choice(["Continental", "Bosch", "Delphi", "Valeo"]), raw=_rand_bytes(6)))

        dtcs = _make_dtcs_for_module(name)

        ecus.append(
            UdsEcu(
                vag_addr=vag_addr,
                name=name,
                uds_physical_req=req,
                uds_physical_res=res,
                sessions=sessions,
                security=security,
                dids=dids,
                dtcs=dtcs,
            )
        )

    return ecus


def _render_html(
    *,
    vin: str,
    title: str,
    ecus: List[UdsEcu],
    scan_params: Dict[str, str],
    tools: Dict[str, str],
) -> str:
    dtc_active = sum(1 for e in ecus for d in e.dtcs if d.status == "ACTIVE")
    dtc_pending = sum(1 for e in ecus for d in e.dtcs if d.status == "PENDING")
    dtc_stored = sum(1 for e in ecus for d in e.dtcs if d.status in {"ACTIVE", "STORED"})

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
      --ok: #38d39f;
      --warn: #ffdd55;
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
    .num { text-align: right; width: 120px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    details { margin-top: 10px; border: 1px solid var(--stroke); border-radius: 14px; overflow: hidden; background: rgba(13,15,20,0.90); }
    summary { cursor: pointer; padding: 12px 12px; list-style: none; }
    summary::-webkit-details-marker { display: none; }
    .box { padding: 10px 12px 14px 12px; }
    .pill { display: inline-block; padding: 2px 10px; border: 1px solid var(--stroke2); border-radius: 999px; margin-left: 6px; font-size: 12px; color: #d7dbe2; background: rgba(255,255,255,0.02); }
    .pill-strong { border-color: rgba(246,192,0,0.40); color: var(--accent2); background: rgba(246,192,0,0.06); }
    .pill-ok { border-color: rgba(56,211,159,0.40); color: var(--ok); background: rgba(56,211,159,0.06); }
    .pill-warn { border-color: rgba(255,221,85,0.45); color: var(--warn); background: rgba(255,221,85,0.06); }
    .footer { margin-top: 12px; color: var(--muted); font-size: 12px; }
    """

    # Scan params table
    sp_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='mono'>{_esc(v)}</td></tr>" for k, v in scan_params.items()
    )

    tools_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='mono'>{_esc(v)}</td></tr>" for k, v in tools.items()
    )

    # ECU summary
    ecu_summary_rows = []
    for e in ecus:
        active = sum(1 for d in e.dtcs if d.status == "ACTIVE")
        pending = sum(1 for d in e.dtcs if d.status == "PENDING")
        total = len(e.dtcs)
        ecu_summary_rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(e.vag_addr)}</td>"
            f"<td>{_esc(e.name)}</td>"
            f"<td class='mono'>{_esc(e.uds_physical_req)}</td>"
            f"<td class='num'>{active}</td>"
            f"<td class='num'>{pending}</td>"
            f"<td class='num'>{total}</td>"
            "</tr>"
        )

    # ECU details
    ecu_details = []
    for e in ecus:
        active = sum(1 for d in e.dtcs if d.status == "ACTIVE")
        pending = sum(1 for d in e.dtcs if d.status == "PENDING")
        total = len(e.dtcs)

        did_rows = []
        for did in e.dids:
            raw = "" if not did.raw else did.raw
            did_rows.append(
                "<tr>"
                f"<td class='mono'>0x{_esc(did.did)}</td>"
                f"<td>{_esc(did.name)}</td>"
                f"<td class='mono'>{_esc(did.value)}</td>"
                f"<td class='mono'>{_esc(raw)}</td>"
                "</tr>"
            )

        if not did_rows:
            did_rows.append("<tr><td colspan='4' class='muted'>No DIDs read.</td></tr>")

        dtc_rows = []
        for d in e.dtcs:
            status_cls = "pill-warn" if d.status in {"ACTIVE", "PENDING"} else "pill"
            dtc_rows.append(
                "<tr>"
                f"<td class='mono'>{_esc(d.dtc)}</td>"
                f"<td><span class='pill {status_cls}'>{_esc(d.status)}</span></td>"
                f"<td class='mono'>0x{d.status_byte:02X}</td>"
                f"<td class='num'>{d.occurrence}</td>"
                f"<td class='mono'>{_esc(d.raw)}</td>"
                f"<td class='muted'>{_esc(d.text)}</td>"
                "</tr>"
            )
        if not dtc_rows:
            dtc_rows.append("<tr><td colspan='6' class='muted'>No DTCs reported (UDS 0x19).</td></tr>")

        sess = ", ".join(e.sessions)

        ecu_details.append(
            "<details>"
            f"<summary><strong class='mono'>{_esc(e.vag_addr)}</strong> <strong>{_esc(e.name)}</strong>"
            f" <span class='pill pill-strong'>req {_esc(e.uds_physical_req.split(' ')[0])}</span>"
            f" <span class='pill'>sessions {_esc(str(len(e.sessions)))}</span>"
            f" <span class='pill pill-warn'>dtc {total}</span>"
            "</summary>"
            "<div class='box'>"
            "<div class='muted'>"
            f"UDS physical request: <span class='mono'>{_esc(e.uds_physical_req)}</span><br>"
            f"UDS physical response: <span class='mono'>{_esc(e.uds_physical_res)}</span><br>"
            f"Sessions: <span class='mono'>{_esc(sess)}</span><br>"
            f"Security access: <span class='mono'>{_esc(e.security)}</span>"
            "</div>"
            "<div style='margin-top: 12px;'>"
            "<h2>ReadDataByIdentifier (0x22)</h2>"
            "<table><thead><tr><th>DID</th><th>Name</th><th>Value</th><th>Raw</th></tr></thead>"
            f"<tbody>{''.join(did_rows)}</tbody></table>"
            "</div>"
            "<div style='margin-top: 12px;'>"
            "<h2>ReadDTCInformation (0x19)</h2>"
            "<table><thead><tr><th>DTC</th><th>Status</th><th>Status Byte</th><th class='num'>Occ</th><th>Raw</th><th>Description</th></tr></thead>"
            f"<tbody>{''.join(dtc_rows)}</tbody></table>"
            "</div>"
            "</div>"
            "</details>"
        )

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
          <div class='muted' style='margin-top: 2px;'>Simulated UDS on OBD Report (VAG)</div>
          <h1 style='margin-top: 10px;'>{_esc(title)}</h1>
          <div class='muted'>Generated: {_esc(_ts())}</div>
          <div class='muted'>VIN: <span class='mono'>{_esc(vin)}</span></div>
          <div class='muted'>Transport: ISO-TP on CAN (simulated)</div>
        </div>
        <div class='badge'><span class='dot'></span> UDS / VAG</div>
      </div>

      <div class='kpi'>
        <div class='k'><div class='t'>ECUs Detected</div><div class='v'>{len(ecus)}</div></div>
        <div class='k'><div class='t'>Active DTCs</div><div class='v'>{dtc_active}</div></div>
        <div class='k'><div class='t'>Pending DTCs</div><div class='v'>{dtc_pending}</div></div>
        <div class='k'><div class='t'>Stored DTCs</div><div class='v'>{dtc_stored}</div></div>
      </div>

      <div class='footer'>Synthetic data for demo/testing only. No real UDS traffic was captured.</div>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Scan Parameters</h2>
      <table>
        <thead><tr><th>Item</th><th>Value</th></tr></thead>
        <tbody>{sp_rows}</tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>Tooling</h2>
      <table>
        <thead><tr><th>Item</th><th>Value</th></tr></thead>
        <tbody>{tools_rows}</tbody>
      </table>
    </div>

    <div class='card' style='margin-top: 12px;'>
      <h2>ECU Summary</h2>
      <table>
        <thead><tr><th>Addr</th><th>ECU</th><th>UDS Req</th><th class='num'>Active</th><th class='num'>Pending</th><th class='num'>Total</th></tr></thead>
        <tbody>
          {''.join(ecu_summary_rows) if ecu_summary_rows else "<tr><td colspan='6' class='muted'>No ECUs.</td></tr>"}
        </tbody>
      </table>
    </div>

    <div style='margin-top: 12px;'>
      {''.join(ecu_details)}
    </div>

    <div class='footer'>Generated by simulate_uds_vag_report.py</div>
  </div>
</body>
</html>"""

    return html


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a simulated UDS-on-OBD (VAG) report (HTML)")
    ap.add_argument("--vin", default="WAUZZZFA0JN000001", help="VIN to print in the report")
    ap.add_argument("--title", default="UDS on OBD Diagnostic Report (VAG)", help="Report title")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (0 disables)")
    ap.add_argument("--out", default="", help="Output HTML path (defaults to logs/uds_vag_report_<timestamp>.html)")
    args = ap.parse_args()

    if int(args.seed):
        random.seed(int(args.seed))

    log_dir = _log_dir_default()
    log_dir.mkdir(parents=True, exist_ok=True)

    ecus = _make_ecus(str(args.vin))

    scan_params = {
        "Vehicle": "VAG (simulated)",
        "Transport": "ISO-TP on CAN (29-bit) (simulated)",
        "Tester Address": "0xF1",
        "Scan Type": "Physical addressing per module",
        "Services": "0x10, 0x22, 0x19" + (", 0x27" if any(e.security != "Not Required" for e in ecus) else ""),
        "Timestamp": _ts(),
    }

    tools = {
        "Report Generator": "Kvaser Bus Manager - Simulated",
        "Notes": "Values are synthetic; use for UI/testing only",
    }

    html = _render_html(vin=str(args.vin), title=str(args.title), ecus=ecus, scan_params=scan_params, tools=tools)

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        name = f"uds_vag_report_{time.strftime('%Y%m%d_%H%M%S')}.html"
        out_path = (log_dir / name).resolve()

    out_path.write_text(html, encoding="utf-8")
    print(str(out_path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
