from __future__ import annotations

import io
import os
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import xml.etree.ElementTree as ET


def build_comm_index_from_pdx(pdx_path: str, *, max_files: int | None = None, max_seconds: float = 30.0) -> Dict[str, Any]:
    """Build a communication/addressing index from a PDX.

    Goal
    - Extract, for each ECU-variant (typically BV_*.odx), the diagnostic transport hints and
      addressing parameters that the project defines.

    What we extract (best-effort)
    - protocol_snref: the referenced protocol layer short-name (e.g. PR_UDSOnCAN, PR_UDSOnIP)
    - CAN (ISO-15765) physical request/response IDs:
        ISO_15765_2.CP_CanPhysReqId
        ISO_15765_2.CP_CanRespUSDTId
    - DoIP (ISO-13400) logical addresses:
        ISO_13400_2.CP_DoIPLogicalEcuAddress
        ISO_13400_2.CP_DoIPLogicalGatewayAddress
        ISO_13400_2.CP_DoIPLogicalTesterAddress

    Output
      {
        ok, elapsed_ms, files_scanned, ecu_count,
        ecus: [ { source_odx, short_name, long_name, protocol_snref, can: {...}, doip: {...} } ]
      }
    """
    start = time.time()
    pdx_path = os.path.abspath(str(pdx_path))
    if not os.path.isfile(pdx_path) or not pdx_path.lower().endswith('.pdx'):
        raise ValueError('invalid pdx path')

    def time_exceeded() -> bool:
        return (time.time() - start) > float(max_seconds)

    # Regex-based extraction is intentionally used here for speed: BV_*.odx files are very large.
    re_proto = re.compile(r'PROTOCOL-SNREF[^>]*SHORT-NAME="([^"]+)"', re.IGNORECASE)
    re_short = re.compile(r'<DIAG-LAYER-CONTAINER[^>]*>\s*<SHORT-NAME>([^<]+)</SHORT-NAME>', re.IGNORECASE)
    re_long = re.compile(r'<DIAG-LAYER-CONTAINER[^>]*>[\s\S]*?<LONG-NAME[^>]*>([^<]+)</LONG-NAME>', re.IGNORECASE)

    def _comp_ref_val(key: str) -> re.Pattern:
        # COMPARAM-REF can appear multiple times; we just take the first usable numeric VALUE.
        return re.compile(
            rf'<COMPARAM-REF[^>]*ID-REF="[^"]*{re.escape(key)}"[^>]*>\s*<VALUE>([^<]+)</VALUE>',
            re.IGNORECASE,
        )

    re_can_req = _comp_ref_val('CP_CanPhysReqId')
    re_can_resp = _comp_ref_val('CP_CanRespUSDTId')
    re_doip_ecu = _comp_ref_val('CP_DoIPLogicalEcuAddress')
    re_doip_gw = _comp_ref_val('CP_DoIPLogicalGatewayAddress')
    re_doip_tester = _comp_ref_val('CP_DoIPLogicalTesterAddress')

    ecus: List[Dict[str, Any]] = []
    files_scanned = 0

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        names = [n for n in zf.namelist() if n.startswith('BV_') and n.lower().endswith('.odx')]
        if max_files is not None:
            names = names[: max(0, int(max_files))]

        for name in names:
            if time_exceeded():
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            files_scanned += 1
            txt = raw.decode('utf-8', 'ignore')

            proto = None
            m = re_proto.search(txt)
            if m:
                proto = m.group(1).strip() or None

            short_name = None
            m = re_short.search(txt)
            if m:
                short_name = m.group(1).strip() or None

            long_name = None
            m = re_long.search(txt)
            if m:
                long_name = m.group(1).strip() or None

            can_req = None
            can_resp = None
            m = re_can_req.search(txt)
            if m:
                try:
                    can_req = int(str(m.group(1)).strip())
                except Exception:
                    can_req = None
            m = re_can_resp.search(txt)
            if m:
                try:
                    can_resp = int(str(m.group(1)).strip())
                except Exception:
                    can_resp = None

            doip_ecu = None
            doip_gw = None
            doip_tester = None
            m = re_doip_ecu.search(txt)
            if m:
                try:
                    doip_ecu = int(str(m.group(1)).strip())
                except Exception:
                    doip_ecu = None
            m = re_doip_gw.search(txt)
            if m:
                try:
                    doip_gw = int(str(m.group(1)).strip())
                except Exception:
                    doip_gw = None
            m = re_doip_tester.search(txt)
            if m:
                try:
                    doip_tester = int(str(m.group(1)).strip())
                except Exception:
                    doip_tester = None

            entry: Dict[str, Any] = {
                'source_odx': name,
                'short_name': short_name,
                'long_name': long_name,
                'protocol_snref': proto,
            }

            if can_req is not None or can_resp is not None:
                entry['can'] = {
                    'phys_req_id': can_req,
                    'resp_id': can_resp,
                    'is_extended_id': bool(
                        (isinstance(can_req, int) and can_req > 0x7FF)
                        or (isinstance(can_resp, int) and can_resp > 0x7FF)
                    ),
                }

            if doip_ecu is not None or doip_gw is not None or doip_tester is not None:
                entry['doip'] = {
                    'logical_ecu_address': doip_ecu,
                    'logical_gateway_address': doip_gw,
                    'logical_tester_address': doip_tester,
                }

            # Only keep rows that contain at least one addressing block.
            if 'can' in entry or 'doip' in entry:
                ecus.append(entry)

    elapsed_ms = int((time.time() - start) * 1000)
    return {
        'ok': True,
        'elapsed_ms': elapsed_ms,
        'files_scanned': int(files_scanned),
        'ecu_count': int(len(ecus)),
        'ecus': ecus,
    }


def extract_gateway_mirror_definition_from_pdx(pdx_path: str, *, max_files: int = 40) -> Dict[str, Any]:
    """Best-effort: extract gateway 'Mirror_mode' definition from a PDX.

    This uses simple regex extraction against the gateway ECU ODX (e.g. EV_Gatew*).
    It's intended to answer "is it visible in the PDX?" and to provide enough
    structure info (byte/bit layout + target bus enum) to build a UDS write.
    """

    try:
        pdx_path = os.path.abspath(str(pdx_path))
        if not os.path.isfile(pdx_path):
            return {'ok': False, 'error': 'pdx not found'}

        with zipfile.ZipFile(pdx_path, 'r') as z:
            names = z.namelist()

            cand = [
                n
                for n in names
                if n.lower().endswith('.odx') and ('gatew' in n.lower() or 'gateway' in n.lower())
            ]
            if not cand:
                cand = [n for n in names if n.lower().endswith('.odx')]

            cand = cand[: max(1, int(max_files))]
            for odx_name in cand:
                try:
                    raw = z.read(odx_name)
                except Exception:
                    continue
                text = raw.decode('utf-8', 'ignore')
                if 'Mirror_mode' not in text and 'Mirrormode' not in text:
                    continue

                def _did_near(label: str) -> Optional[str]:
                    idx = text.find(label)
                    if idx < 0:
                        return None
                    # The DID numeric value lives in <LOWER-LIMIT> *before* the
                    # <VT> that contains our label.  Look backwards first.
                    lookback = text[max(0, idx - 600) : idx]
                    mb = re.search(r"<LOWER-LIMIT>(\d+)</LOWER-LIMIT>", lookback)
                    if mb:
                        try:
                            return f"0x{int(mb.group(1)):04X}"
                        except Exception:
                            pass
                    # Legacy fallback: look for $XXXX forward.
                    window = text[idx : idx + 2500]
                    m = re.search(r"\$([0-9A-Fa-f]{4})", window)
                    if not m:
                        return None
                    return f"0x{m.group(1).upper()}"

                did_mirror = _did_near('IDE10351') or _did_near('Mirror_mode')
                did_map = _did_near('IDE10365') or _did_near('Mirror_mode_bus_mapping')

                sm = re.search(r"<STRUCTURE[^>]*ID=\"STRUC_MirroMode\"[\s\S]*?</STRUCTURE>", text)
                if not sm:
                    continue
                s = sm.group(0)

                fields = []
                for pm in re.finditer(r"<PARAM[^>]*xsi:type=\"VALUE\"[\s\S]*?</PARAM>", s):
                    blk = pm.group(0)
                    sn = re.search(r"<SHORT-NAME>(.*?)</SHORT-NAME>", blk)
                    ln = re.search(r"<LONG-NAME[^>]*>(.*?)</LONG-NAME>", blk)
                    bp = re.search(r"<BYTE-POSITION>(\d+)</BYTE-POSITION>", blk)
                    bit = re.search(r"<BIT-POSITION>(\d+)</BIT-POSITION>", blk)
                    dop = re.search(r"<DOP-REF ID-REF=\"(.*?)\"\s*/>", blk)
                    fields.append({
                        'short': (sn.group(1).strip() if sn else ''),
                        'long': (ln.group(1).strip() if ln else ''),
                        'byte': int(bp.group(1)) if bp else None,
                        'bit': int(bit.group(1)) if bit else 0,
                        'dop': (dop.group(1).strip() if dop else ''),
                    })

                target_bus_map = {}
                tb_dop = 'DOP_TEXTTABLENotActivDataBusCanDiagnDataBusEther0255'
                dm = re.search(
                    rf"<(DATA-OBJECT-PROP|DOP)[^>]*ID=\"{re.escape(tb_dop)}\"[\s\S]*?</(DATA-OBJECT-PROP|DOP)>",
                    text,
                )
                if dm:
                    dop_txt = dm.group(0)
                    for sm2 in re.finditer(r"<COMPU-SCALE>[\s\S]*?</COMPU-SCALE>", dop_txt):
                        blk = sm2.group(0)
                        low = re.search(r"<LOWER-LIMIT>(.*?)</LOWER-LIMIT>", blk)
                        vt = re.search(r"<VT[^>]*>(.*?)</VT>", blk)
                        if low and vt:
                            target_bus_map[str(low.group(1).strip())] = str(vt.group(1).strip())

                return {
                    'ok': True,
                    'source_odx': odx_name,
                    'dids': {
                        'mirror_mode': did_mirror or '0x096F',
                        'mirror_mode_bus_mapping': did_map or '0x189A',
                    },
                    'target_bus_map': target_bus_map or {'0': 'not_active', '1': 'data_bus_can_diagnostic', '2': 'data_bus_ethernet'},
                    'fields': fields,
                }

        return {'ok': False, 'error': 'mirror definition not found in scanned ODX files'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _strip_ns(tag: str) -> str:
    if not isinstance(tag, str):
        return str(tag)
    if '}' in tag:
        return tag.split('}', 1)[1]
    return tag


def _first_text(el: ET.Element, names: Tuple[str, ...]) -> Optional[str]:
    for n in names:
        child = el.find(f'.//{{*}}{n}')
        if child is not None and isinstance(child.text, str):
            t = child.text.strip()
            if t:
                return t
    return None


@dataclass
class PdxAnalyzeOptions:
    max_xml_files: int = 12
    max_tags_per_file: int = 60
    max_dtcs: int = 200
    max_layers: int = 200
    max_protocols: int = 200
    max_services: int = 400
    max_comm_values: int = 200
    max_comparams: int = 400
    max_seconds: float = 8.0


def analyze_pdx(pdx_path: str, opts: Optional[PdxAnalyzeOptions] = None) -> Dict[str, Any]:
    opts = opts or PdxAnalyzeOptions()
    start = time.time()
    pdx_path = os.path.abspath(pdx_path)

    if not os.path.isfile(pdx_path):
        raise FileNotFoundError(pdx_path)
    if not pdx_path.lower().endswith('.pdx'):
        raise ValueError('not a .pdx file')

    report: Dict[str, Any] = {
        'ok': True,
        'pdx_path': pdx_path,
        'pdx_filename': os.path.basename(pdx_path),
        'pdx_size_bytes': int(os.path.getsize(pdx_path)),
        'zip': {},
        'zip_inventory': {},
        'odx_files': [],
        'extracted': {
            'diag_layers': [],
            'protocols': [],
            'dtcs': [],
            'uds_services': [],
            'comm': {},
            'comparams': [],
        },
        'warnings': [],
        'notes': [],
    }

    def time_exceeded() -> bool:
        return (time.time() - start) > float(opts.max_seconds)

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        infos = list(zf.infolist())
        report['zip'] = {
            'entries': len(infos),
            'total_uncompressed_bytes': int(sum(int(i.file_size) for i in infos)),
        }

        names = [i.filename for i in infos]

        # Inventory to quickly understand what's inside the PDX.
        ext_counts: Counter[str] = Counter()
        prefix_counts: Counter[str] = Counter()
        for n in names:
            nl = n.lower()
            if '.' in nl:
                ext = '.' + nl.rsplit('.', 1)[1]
            else:
                ext = ''
            ext_counts[ext] += 1
            base = os.path.basename(n)
            if '_' in base:
                prefix = base.split('_', 1)[0]
                if prefix:
                    prefix_counts[prefix] += 1
        report['zip_inventory'] = {
            'extensions': [{'ext': k, 'count': int(v)} for k, v in ext_counts.most_common(20)],
            'prefixes': [{'prefix': k, 'count': int(v)} for k, v in prefix_counts.most_common(25)],
        }

        candidates = []
        for n in names:
            nl = n.lower()
            if nl.endswith(('.xml', '.odx', '.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx-cs', '.odx-fd')):
                candidates.append(n)

        if not candidates:
            report['warnings'].append('No obvious XML/ODX files found inside PDX (zip).')
            return report

        # Prefer likely communication/protocol and ECU-variant ODX files.
        def score(name: str) -> int:
            nl = name.lower()
            s = 0
            # Transport/protocol keywords
            if 'doip' in nl or '13400' in nl:
                s += 200
            if '15765' in nl or 'can' in nl:
                s += 120
            if 'uds' in nl or '14229' in nl:
                s += 100
            if 'obd' in nl:
                s += 80
            if nl.startswith('pr_'):
                s += 90
            if nl.startswith('iso_'):
                s += 90

            # ECU / vehicle variants often contain addressing + diag services.
            if nl.startswith('bv_'):
                s += 60
            if 'ec u' in nl or 'ecu' in nl or 'ecuv' in nl or 'variant' in nl:
                s += 60

            # Generic ODX hint
            if 'odx' in nl:
                s += 10
            if 'diag' in nl:
                s += 5
            if nl.endswith(('.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx', '.xml')):
                s += 1

            # Higher score => earlier
            return -s

        # Balanced selection across categories to avoid only parsing libraries.
        # The goal is to include at least some ECU-variant documents (often BV_/EV_) where addressing
        # and service definitions live.
        candidates_sorted = sorted(candidates, key=score)

        def take(where, limit: int) -> List[str]:
            out = []
            for n in where:
                if len(out) >= limit:
                    break
                out.append(n)
            return out

        doip = [n for n in candidates_sorted if ('doip' in n.lower() or '13400' in n.lower())]
        iso_can = [n for n in candidates_sorted if ('15765' in n.lower() or 'can' in n.lower())]
        pr = [n for n in candidates_sorted if os.path.basename(n).lower().startswith('pr_')]
        ev = [n for n in candidates_sorted if os.path.basename(n).lower().startswith('ev_')]
        bv = [n for n in candidates_sorted if os.path.basename(n).lower().startswith('bv_')]

        picked: List[str] = []
        for part in [
            take(doip, 3),
            take(iso_can, 3),
            take(pr, 4),
            take(ev, 4),
            take(bv, 4),
        ]:
            for n in part:
                if n not in picked:
                    picked.append(n)

        # Fill remainder with best-scored items.
        for n in candidates_sorted:
            if len(picked) >= int(opts.max_xml_files):
                break
            if n not in picked:
                picked.append(n)

        candidates = picked[: max(1, int(opts.max_xml_files))]

        diag_layers: List[Dict[str, Any]] = []
        protocols: List[Dict[str, Any]] = []
        dtcs: List[Dict[str, Any]] = []
        services: List[Dict[str, Any]] = []
        comm_values: Dict[str, List[str]] = {}
        comparams: List[Dict[str, Any]] = []

        # Tags we want to opportunistically capture (to inform what to implement next).
        # These vary a lot across ODX dialects/vendor exports.
        interesting_leaf_tags = {
            'SERVICE-ID', 'SID',
            'REQUEST-ID', 'RESPONSE-ID',
            'PHYSICAL-REQUEST-ID', 'FUNCTIONAL-REQUEST-ID',
            'CAN-ID', 'CAN-IDENTIFIER', 'TX-ID', 'RX-ID',
            'LOGICAL-ADDRESS', 'DOIP-LOGICAL-ADDRESS',
            'VIN', 'ECU-NAME',
            'COMPARAM-REF', 'VALUE',
            'DIAG-COMM-REF', 'DIAG-COMM-SNREF',
        }

        for name in candidates:
            if time_exceeded():
                report['warnings'].append('Parsing time limit reached; results may be partial.')
                break

            try:
                raw = zf.read(name)
            except Exception as e:
                report['warnings'].append(f'Failed to read zip entry {name!r}: {e}')
                continue

            # Quick sanity: must contain angle brackets.
            if b'<' not in raw[:2048]:
                continue

            tag_counts: Counter[str] = Counter()
            root_tag: Optional[str] = None
            namespace: Optional[str] = None

            try:
                # iterparse to keep memory lower on large XMLs.
                stack: List[str] = []
                dtc_depth: Optional[int] = None
                current_dtc: Optional[Dict[str, Any]] = None
                proto_depth: Optional[int] = None
                current_comp_ref: Optional[str] = None

                for ev, el in ET.iterparse(io.BytesIO(raw), events=('start', 'end')):
                    t = str(el.tag)
                    if ev == 'start' and root_tag is None:
                        root_tag = _strip_ns(t)
                        if t.startswith('{') and '}' in t:
                            namespace = t[1:].split('}', 1)[0]

                    tn = _strip_ns(t)
                    tag_counts[tn] += 1

                    if ev == 'start':
                        stack.append(tn)
                        # Begin streaming capture for DTC blocks.
                        if dtc_depth is None and tn == 'DTC' and len(dtcs) < opts.max_dtcs:
                            dtc_depth = len(stack)
                            current_dtc = {'display_code': None, 'trouble_code': None, 'text': None}

                        # Begin streaming capture for PROTOCOL comparams.
                        if proto_depth is None and tn == 'PROTOCOL' and len(comparams) < opts.max_comparams:
                            proto_depth = len(stack)
                            current_comp_ref = None

                        # If we are inside a PROTOCOL, capture COMPARAM-REF immediately (ID-REF attribute).
                        if proto_depth is not None and tn == 'COMPARAM-REF':
                            current_comp_ref = (
                                (el.attrib.get('ID-REF') or el.attrib.get('ID_REF') or el.attrib.get('IDREF'))
                                or ((el.text or '').strip() if isinstance(el.text, str) else None)
                            )

                    if ev == 'end':
                        # Streaming DTC extraction (children may be cleared before DTC end).
                        if dtc_depth is not None and current_dtc is not None:
                            if len(stack) >= dtc_depth:
                                if tn == 'DISPLAY-TROUBLE-CODE':
                                    if not current_dtc.get('display_code'):
                                        v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                        if v:
                                            current_dtc['display_code'] = v

                                if tn in {'TROUBLE-CODE', 'DTC-CODE', 'DTC-CODED', 'DTC-NUMBER', 'CODE'}:
                                    if not current_dtc.get('trouble_code'):
                                        v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                        if v:
                                            current_dtc['trouble_code'] = v
                                if tn in {'TEXT', 'DESC', 'DESCRIPTION'}:
                                    if not current_dtc.get('text'):
                                        v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                        if v:
                                            current_dtc['text'] = v

                                if tn == 'DTC' and len(stack) == dtc_depth:
                                    # Prefer DISPLAY-TROUBLE-CODE (usually already in P/C/B/U format).
                                    code = current_dtc.get('display_code') or current_dtc.get('trouble_code')
                                    txt = current_dtc.get('text')
                                    if code or txt:
                                        dtcs.append({'code': code, 'text': txt or ''})
                                    dtc_depth = None
                                    current_dtc = None

                        # Streaming PROTOCOL comparams.
                        if proto_depth is not None:
                            if len(stack) >= proto_depth:
                                if tn == 'VALUE' and current_comp_ref and len(comparams) < opts.max_comparams:
                                    val = (el.text or '').strip() if isinstance(el.text, str) else ''
                                    if val:
                                        comparams.append({'ref': current_comp_ref, 'value': val})

                                if tn == 'COMPARAM-REF':
                                    # End of this parameter.
                                    current_comp_ref = None

                                if tn == 'PROTOCOL' and len(stack) == proto_depth:
                                    proto_depth = None
                                    current_comp_ref = None

                        if len(diag_layers) < opts.max_layers and tn in {'DIAG-LAYER', 'DIAGNOSTIC-LAYER', 'DIAG-LAYER-CONTAINER'}:
                            diag_layers.append({
                                'id': el.attrib.get('ID') or el.attrib.get('id'),
                                'short_name': _first_text(el, ('SHORT-NAME', 'SHORTNAME')),
                                'long_name': _first_text(el, ('LONG-NAME', 'LONGNAME')),
                            })

                        if len(protocols) < opts.max_protocols and tn in {'PROTOCOL', 'DIAG-PROTOCOL', 'DIAGNOSTIC-PROTOCOL'}:
                            protocols.append({
                                'id': el.attrib.get('ID') or el.attrib.get('id'),
                                'short_name': _first_text(el, ('SHORT-NAME', 'SHORTNAME')),
                                'long_name': _first_text(el, ('LONG-NAME', 'LONGNAME')),
                            })

                        if len(services) < opts.max_services and tn in {'DIAG-SERVICE', 'DIAGNOSTIC-SERVICE', 'SERVICE'}:
                            sid = _first_text(el, ('SERVICE-ID', 'SID'))
                            sn = _first_text(el, ('SHORT-NAME', 'SHORTNAME'))
                            ln = _first_text(el, ('LONG-NAME', 'LONGNAME'))
                            if sid or sn or ln:
                                services.append({'sid': sid, 'short_name': sn, 'long_name': ln})

                        # Capture interesting leaf values (best-effort).
                        if tn in interesting_leaf_tags:
                            txtv = (el.text or '').strip() if isinstance(el.text, str) else ''
                            if txtv:
                                lst = comm_values.setdefault(tn, [])
                                if len(lst) < opts.max_comm_values and txtv not in lst:
                                    lst.append(txtv)

                        # Free memory for processed elements.
                        el.clear()

                        if stack:
                            stack.pop()

                    if time_exceeded():
                        report['warnings'].append('Parsing time limit reached; results may be partial.')
                        break

            except ET.ParseError as e:
                report['warnings'].append(f'XML parse error in {name!r}: {e}')
                continue
            except Exception as e:
                report['warnings'].append(f'Failed parsing {name!r}: {e}')
                continue

            top_tags = tag_counts.most_common(int(opts.max_tags_per_file))
            report['odx_files'].append({
                'name': name,
                'root_tag': root_tag,
                'namespace': namespace,
                'top_tags': [{'tag': t, 'count': int(c)} for t, c in top_tags],
            })

        # Deduplicate small extracted lists.
        def _uniq(items: List[Dict[str, Any]], keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            seen = set()
            for it in items:
                sig = tuple((it.get(k) or '') for k in keys)
                if sig in seen:
                    continue
                seen.add(sig)
                out.append(it)
            return out

        report['extracted']['diag_layers'] = _uniq(diag_layers, ('id', 'short_name', 'long_name'))
        report['extracted']['protocols'] = _uniq(protocols, ('id', 'short_name', 'long_name'))
        report['extracted']['dtcs'] = _uniq(dtcs, ('code', 'text'))
        report['extracted']['uds_services'] = _uniq(services, ('sid', 'short_name', 'long_name'))
        report['extracted']['comm'] = {k: v for k, v in comm_values.items() if isinstance(v, list) and v}
        report['extracted']['comparams'] = _uniq(comparams, ('ref', 'value'))

        report['notes'].append(
            'This is a minimal, heuristic parser. Many PDX/ODX variants store protocol and DTC data in different sections.'
        )
        report['notes'].append(
            'Use the top_tags inventory to decide which ODX sections to implement next (DoIP params, UDS services, DTC dictionary, etc.).'
        )

    report['elapsed_ms'] = int((time.time() - start) * 1000)
    return report


def build_dtc_index_from_pdx(
    pdx_path: str,
    *,
    max_files: Optional[int] = None,
    max_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Build a DTC translation index from a PDX.

    Produces a mapping DISPLAY-TROUBLE-CODE -> description by scanning all ODX/XML
    files inside the PDX that appear to contain DTC dictionaries.

    Returns a dict like:
      { ok, elapsed_ms, files_scanned, dtc_count, map }
    """
    start = time.time()
    pdx_path = os.path.abspath(pdx_path)
    if not os.path.isfile(pdx_path):
        raise FileNotFoundError(pdx_path)

    out_map: Dict[str, str] = {}
    # Per-file DTC tracking: {code: {filename: description}}
    file_dtc_map: Dict[str, Dict[str, str]] = {}
    # EV_/BV_ file ownership map for ECU-specific description lookup: {code: {filename: desc}}
    dtc_ecu_map: Dict[str, Dict[str, str]] = {}
    # Raw TROUBLE-CODE (UDS 3-byte int) -> DISPLAY-TROUBLE-CODE mapping.
    # Critical for VW/VAG: SAE-J2012 conversion of the raw bytes does NOT match
    # the display code in the PDX (which is OEM-defined). Always prefer this
    # mapping when available. Keys are decimal strings of the integer code.
    trouble_to_display: Dict[str, str] = {}
    # Per-file variant: {trouble_int_str: {filename: display_code}}
    trouble_to_display_by_file: Dict[str, Dict[str, str]] = {}
    files_scanned = 0
    files_with_dtc_marker = 0
    dtc_blocks_seen = 0
    unique_display_codes_seen = 0

    def time_exceeded() -> bool:
        return (time.time() - start) > float(max_seconds)

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        names = [n for n in zf.namelist() if isinstance(n, str)]

        # Prefer ECU variant documents first (commonly contain DTC dictionaries), but scan broadly.
        def score(name: str) -> int:
            nl = name.lower()
            base = os.path.basename(nl)
            s = 0
            if base.startswith('ev_'):
                s += 50
            if base.startswith('bv_'):
                s += 40
            if base.startswith('bl_'):
                s += 20
            if 'gatew' in nl or 'gateway' in nl:
                s += 15
            if nl.endswith(('_d.odx', '_d.xml', '.odx-d')):
                s += 10
            if nl.endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx-fd', '.xml')):
                s += 1
            return -s

        candidates = [
            n for n in names
            if n.lower().endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx-fd', '.xml'))
        ]
        candidates = sorted(candidates, key=score)

        limit_files = int(max_files) if isinstance(max_files, int) and max_files > 0 else None

        for name in candidates:
            if time_exceeded():
                break
            if limit_files is not None and files_scanned >= limit_files:
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            if b'<' not in raw[:2048]:
                continue

            # Fast skip: ignore files that don't mention DTC sections.
            raw_head = raw[: min(len(raw), 2_000_000)]
            if (b'DISPLAY-TROUBLE-CODE' not in raw_head and b'<DTC' not in raw_head and b'DIAG-TROUBLE' not in raw_head):
                # Might still have DTCs later, but this keeps import fast.
                continue

            files_with_dtc_marker += 1

            files_scanned += 1
            stack: List[str] = []
            dtc_depth: Optional[int] = None
            cur_display: Optional[str] = None
            cur_trouble: Optional[str] = None
            cur_text: Optional[str] = None
            cur_long: Optional[str] = None
            cur_short: Optional[str] = None

            def _best_desc() -> str:
                # Prefer explicit TEXT, then LONG-NAME, then SHORT-NAME.
                for v in (cur_text, cur_long, cur_short):
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                return ''

            def _maybe_set_best(existing: Optional[str], new: Optional[str]) -> Optional[str]:
                if not isinstance(new, str):
                    return existing
                nn = new.strip()
                if not nn:
                    return existing
                if not existing:
                    return nn
                # Prefer longer, more descriptive text.
                if len(nn) > len(existing):
                    return nn
                return existing

            def _uds_dtc_to_display_code(dtc_val: int) -> str:
                v = int(dtc_val) & 0xFFFFFF
                a = (v >> 16) & 0xFF
                b = (v >> 8) & 0xFF
                c = v & 0xFF
                letter_bits = (a >> 6) & 0x03
                first_digit = (a >> 4) & 0x03
                letter = {0: 'P', 1: 'C', 2: 'B', 3: 'U'}.get(letter_bits, 'P')
                d2 = a & 0x0F
                return f"{letter}{first_digit:X}{d2:X}{b:02X}{c:02X}".upper()

            try:
                for ev, el in ET.iterparse(io.BytesIO(raw), events=('start', 'end')):
                    t = _strip_ns(str(el.tag))
                    if ev == 'start':
                        stack.append(t)
                        if dtc_depth is None and t in {'DTC', 'DIAG-TROUBLE-CODE', 'DIAGNOSTIC-TROUBLE-CODE'}:
                            dtc_depth = len(stack)
                            cur_display = None
                            cur_trouble = None
                            cur_text = None
                            cur_long = None
                            cur_short = None
                            dtc_blocks_seen += 1

                    if ev == 'end':
                        if dtc_depth is not None and len(stack) >= dtc_depth:
                            if t == 'DISPLAY-TROUBLE-CODE' and not cur_display:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    cur_display = v
                            if t in {'TROUBLE-CODE', 'DTC-CODE', 'DTC-NUMBER', 'CODE'}:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    cur_trouble = _maybe_set_best(cur_trouble, v)

                            if t in {'TEXT', 'DESC', 'DESCRIPTION'}:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    cur_text = _maybe_set_best(cur_text, v)

                            if t in {'LONG-NAME', 'LONGNAME'}:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    cur_long = _maybe_set_best(cur_long, v)

                            if t in {'SHORT-NAME', 'SHORTNAME'}:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    cur_short = _maybe_set_best(cur_short, v)

                            if t in {'DTC', 'DIAG-TROUBLE-CODE', 'DIAGNOSTIC-TROUBLE-CODE'} and len(stack) == dtc_depth:
                                code = None
                                # Parse raw TROUBLE-CODE as integer (decimal or hex).
                                trouble_int: Optional[int] = None
                                if cur_trouble:
                                    s = cur_trouble.strip()
                                    try:
                                        sv = s
                                        if sv.lower().startswith('0x'):
                                            trouble_int = int(sv[2:], 16)
                                        elif re.fullmatch(r"[0-9A-Fa-f]{6}", sv or '') and not re.fullmatch(r"\d+", sv):
                                            trouble_int = int(sv, 16)
                                        else:
                                            trouble_int = int(sv)
                                        if not (0 < trouble_int <= 0xFFFFFF):
                                            trouble_int = None
                                    except Exception:
                                        trouble_int = None

                                if cur_display:
                                    code = cur_display.strip()
                                elif trouble_int is not None:
                                    code = _uds_dtc_to_display_code(trouble_int)

                                if code:
                                    desc_val = _best_desc()
                                    if code not in out_map:
                                        out_map[code] = desc_val
                                        unique_display_codes_seen += 1
                                    else:
                                        # Upgrade description if we found a better one.
                                        prev = out_map.get(code) or ''
                                        if desc_val and (not prev or len(desc_val) > len(prev)):
                                            out_map[code] = desc_val
                                    # Track per-file origin
                                    if desc_val:
                                        file_key = os.path.basename(name)
                                        if code not in file_dtc_map:
                                            file_dtc_map[code] = {}
                                        file_dtc_map[code][file_key] = desc_val
                                        # Track EV_/BV_ file ownership for ECU-specific lookup
                                        bn_lower = file_key.lower()
                                        if bn_lower.startswith('ev_') or bn_lower.startswith('bv_'):
                                            if code not in dtc_ecu_map:
                                                dtc_ecu_map[code] = {}
                                            dtc_ecu_map[code][file_key] = desc_val

                                    # Always record raw TROUBLE-CODE -> DISPLAY mapping
                                    # (even when description is empty), so DoIP scans
                                    # can resolve VW/OEM display codes that don't follow
                                    # SAE J2012 encoding of the raw bytes.
                                    if trouble_int is not None and cur_display:
                                        ti_key = str(int(trouble_int))
                                        # Global mapping: keep first; downgrade only if
                                        # later we find the same trouble_int mapping to
                                        # the same display (no-op) — never overwrite
                                        # with a different display globally.
                                        if ti_key not in trouble_to_display:
                                            trouble_to_display[ti_key] = code
                                        # Per-file mapping (always recorded)
                                        file_key = os.path.basename(name)
                                        if ti_key not in trouble_to_display_by_file:
                                            trouble_to_display_by_file[ti_key] = {}
                                        trouble_to_display_by_file[ti_key][file_key] = code

                                dtc_depth = None
                                cur_display = None
                                cur_trouble = None
                                cur_text = None
                                cur_long = None
                                cur_short = None

                        el.clear()
                        if stack:
                            stack.pop()

                    if time_exceeded():
                        break
            except Exception:
                continue

    # Build by_file: only include codes with conflicting descriptions across files
    by_file: Dict[str, Dict[str, str]] = {}
    for code, origins in file_dtc_map.items():
        if len(origins) > 1:
            descs = set(origins.values())
            if len(descs) > 1:
                by_file[code] = dict(origins)

    return {
        'ok': True,
        'elapsed_ms': int((time.time() - start) * 1000),
        'files_scanned': int(files_scanned),
        'files_with_dtc_marker': int(files_with_dtc_marker),
        'dtc_blocks_seen': int(dtc_blocks_seen),
        'dtc_count': int(len(out_map)),
        'unique_display_codes_seen': int(unique_display_codes_seen),
        'map': out_map,
        'by_file': by_file,
        'dtc_ecu_map': dtc_ecu_map,
        'dtc_ecu_map_count': int(len(dtc_ecu_map)),
        'trouble_to_display': trouble_to_display,
        'trouble_to_display_by_file': trouble_to_display_by_file,
        'trouble_to_display_count': int(len(trouble_to_display)),
    }


def build_did_index_from_pdx(
    pdx_path: str,
    *,
    max_files: Optional[int] = None,
    max_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Build a best-effort DID (Data Identifier) index from a PDX.

    Extracts DATA-IDENTIFIER definitions and resolves their byte length using nearby
    BYTE-LENGTH/BIT-LENGTH information or referenced DATA-OBJECT-PROP (DOP).

    Returns a dict like:
      { ok, elapsed_ms, files_scanned, did_count, map }

    The map is keyed by "0xF190" strings, with values like:
      { short_name, long_name, byte_length, bit_length, source_file }
    """
    start = time.time()
    pdx_path = os.path.abspath(pdx_path)
    if not os.path.isfile(pdx_path):
        raise FileNotFoundError(pdx_path)

    out_map: Dict[str, Dict[str, Any]] = {}
    files_scanned = 0
    files_with_did_marker = 0

    def time_exceeded() -> bool:
        return (time.time() - start) > float(max_seconds)

    def _parse_int_maybe_hex(s: str) -> Optional[int]:
        if not isinstance(s, str):
            return None
        t = s.strip()
        if not t:
            return None
        if t.startswith('$'):
            t = '0x' + t[1:]
        try:
            return int(t, 0)
        except Exception:
            pass
        try:
            if re.fullmatch(r"[0-9A-Fa-f]{1,8}", t):
                return int(t, 16)
        except Exception:
            return None
        return None

    def _key(did: int) -> str:
        return f"0x{int(did) & 0xFFFF:04X}"

    def _maybe_upgrade(existing: Optional[Dict[str, Any]], cand: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(existing, dict):
            return cand
        out = dict(existing)
        # Prefer records with known length.
        if not out.get('byte_length') and cand.get('byte_length'):
            out.update(cand)
            return out
        # Prefer longer names.
        for k in ('long_name', 'short_name'):
            a = str(out.get(k) or '').strip()
            b = str(cand.get(k) or '').strip()
            if b and (not a or len(b) > len(a)):
                out[k] = b
        return out

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        names = [n for n in zf.namelist() if isinstance(n, str)]

        def score(name: str) -> int:
            nl = name.lower()
            base = os.path.basename(nl)
            s = 0
            if base.startswith('ev_'):
                s += 50
            if base.startswith('bv_'):
                s += 40
            if base.startswith('bl_'):
                s += 20
            if 'gatew' in nl or 'gateway' in nl:
                s += 15
            if nl.endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx-fd', '.xml')):
                s += 1
            return -s

        candidates = [
            n for n in names
            if n.lower().endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.odx-f', '.odx-fd', '.xml'))
        ]
        candidates = sorted(candidates, key=score)
        limit_files = int(max_files) if isinstance(max_files, int) and max_files > 0 else None

        for name in candidates:
            if time_exceeded():
                break
            if limit_files is not None and files_scanned >= limit_files:
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            if b'<' not in raw[:2048]:
                continue

            raw_head = raw[: min(len(raw), 2_000_000)]
            if (
                b'DATA-IDENTIFIER' not in raw_head
                and b'DOP-REF' not in raw_head
                and b'DATA-OBJECT-PROP' not in raw_head
                and b'BYTE-LENGTH' not in raw_head
                and b'BIT-LENGTH' not in raw_head
            ):
                continue

            files_with_did_marker += 1
            files_scanned += 1

            # Collect DOP lengths, then map DATA-IDENTIFIER -> DOP.
            dop_len: Dict[str, Dict[str, Any]] = {}
            did_defs: List[Dict[str, Any]] = []

            stack: List[str] = []
            dop_depth: Optional[int] = None
            dop_cur: Optional[Dict[str, Any]] = None
            did_depth: Optional[int] = None
            did_cur: Optional[Dict[str, Any]] = None

            try:
                for ev, el in ET.iterparse(io.BytesIO(raw), events=('start', 'end')):
                    t = _strip_ns(str(el.tag))
                    if ev == 'start':
                        stack.append(t)
                        if t in {'DATA-OBJECT-PROP', 'DOP'} and dop_depth is None:
                            dop_id = el.attrib.get('ID') if isinstance(el.attrib, dict) else None
                            if isinstance(dop_id, str) and dop_id:
                                dop_depth = len(stack)
                                dop_cur = {'id': dop_id, 'byte_length': None, 'bit_length': None, 'short_name': None, 'long_name': None}
                        if t in {'DATA-IDENTIFIER', 'DATAID', 'DID'} and did_depth is None:
                            did_depth = len(stack)
                            did_cur = {'did': None, 'dop_ref': None, 'short_name': None, 'long_name': None, 'byte_length': None, 'bit_length': None}

                    if ev == 'end':
                        if dop_depth is not None and dop_cur is not None and len(stack) >= dop_depth:
                            if t in {'BYTE-LENGTH', 'BYTELENGTH'} and dop_cur.get('byte_length') is None:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                iv = _parse_int_maybe_hex(v)
                                if isinstance(iv, int) and iv > 0:
                                    dop_cur['byte_length'] = int(iv)
                            if t in {'BIT-LENGTH', 'BITLENGTH'} and dop_cur.get('bit_length') is None:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                iv = _parse_int_maybe_hex(v)
                                if isinstance(iv, int) and iv > 0:
                                    dop_cur['bit_length'] = int(iv)
                            if t in {'SHORT-NAME', 'SHORTNAME'} and not dop_cur.get('short_name'):
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    dop_cur['short_name'] = v
                            if t in {'LONG-NAME', 'LONGNAME'} and not dop_cur.get('long_name'):
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    dop_cur['long_name'] = v

                            if t in {'DATA-OBJECT-PROP', 'DOP'} and len(stack) == dop_depth:
                                dop_len[str(dop_cur['id'])] = dop_cur
                                dop_depth = None
                                dop_cur = None

                        if did_depth is not None and did_cur is not None and len(stack) >= did_depth:
                            if t in {'ID', 'IDENTIFIER'} and did_cur.get('did') is None:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                iv = _parse_int_maybe_hex(v)
                                if isinstance(iv, int):
                                    did_cur['did'] = int(iv) & 0xFFFF

                            if t in {'SHORT-NAME', 'SHORTNAME'} and not did_cur.get('short_name'):
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    did_cur['short_name'] = v
                            if t in {'LONG-NAME', 'LONGNAME'} and not did_cur.get('long_name'):
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                if v:
                                    did_cur['long_name'] = v
                            if t in {'BYTE-LENGTH', 'BYTELENGTH'} and did_cur.get('byte_length') is None:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                iv = _parse_int_maybe_hex(v)
                                if isinstance(iv, int) and iv > 0:
                                    did_cur['byte_length'] = int(iv)
                            if t in {'BIT-LENGTH', 'BITLENGTH'} and did_cur.get('bit_length') is None:
                                v = (el.text or '').strip() if isinstance(el.text, str) else ''
                                iv = _parse_int_maybe_hex(v)
                                if isinstance(iv, int) and iv > 0:
                                    did_cur['bit_length'] = int(iv)

                            if t == 'DOP-REF':
                                ref = el.attrib.get('ID-REF') if isinstance(el.attrib, dict) else None
                                if isinstance(ref, str) and ref:
                                    did_cur['dop_ref'] = ref

                            if t in {'DATA-IDENTIFIER', 'DATAID', 'DID'} and len(stack) == did_depth:
                                did_defs.append(did_cur)
                                did_depth = None
                                did_cur = None

                        el.clear()
                        if stack:
                            stack.pop()

                    if time_exceeded():
                        break
            except Exception:
                continue

            for d in did_defs:
                did = d.get('did')
                if not isinstance(did, int):
                    continue
                key = _key(did)
                byte_length = d.get('byte_length')
                bit_length = d.get('bit_length')

                dop_ref = d.get('dop_ref')
                if (not isinstance(byte_length, int) or byte_length <= 0) and isinstance(dop_ref, str):
                    meta = dop_len.get(dop_ref)
                    if isinstance(meta, dict):
                        bl = meta.get('byte_length')
                        if isinstance(bl, int) and bl > 0:
                            byte_length = int(bl)
                        btl = meta.get('bit_length')
                        if isinstance(btl, int) and btl > 0:
                            bit_length = int(btl)

                rec = {
                    'short_name': str(d.get('short_name') or ''),
                    'long_name': str(d.get('long_name') or ''),
                    'byte_length': int(byte_length) if isinstance(byte_length, int) else None,
                    'bit_length': int(bit_length) if isinstance(bit_length, int) else None,
                    'source_file': name,
                }
                out_map[key] = _maybe_upgrade(out_map.get(key), rec)

    return {
        'ok': True,
        'elapsed_ms': int((time.time() - start) * 1000),
        'files_scanned': int(files_scanned),
        'files_with_did_marker': int(files_with_did_marker),
        'did_count': int(len(out_map)),
        'map': out_map,
    }


# ---------------------------------------------------------------------------
# D: Extended Data Record definitions from PDX
# ---------------------------------------------------------------------------

def build_env_data_index_from_pdx(
    pdx_path: str,
    *,
    max_files: Optional[int] = None,
    max_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Extract DTC environment/extended-data record structures from a PDX.

    Scans ODX files for ENV-DATA, ENV-DATA-DESC, DTCS → extended-data-record
    definitions, and common VW/Audi record layout patterns.

    Returns:
      {
        ok, elapsed_ms, files_scanned,
        env_data_count,
        env_data: { record_id: { short_name, long_name, byte_length, params: [...] } },
        dtc_env_mapping_count,
        dtc_env_mapping: { dtc_display_code: [record_id, ...] },
      }
    """
    start = time.time()
    pdx_path = os.path.abspath(pdx_path)
    if not os.path.isfile(pdx_path):
        raise FileNotFoundError(pdx_path)

    env_data: Dict[str, Dict[str, Any]] = {}
    dtc_env_mapping: Dict[str, List[str]] = {}
    files_scanned = 0

    def time_exceeded() -> bool:
        return (time.time() - start) > float(max_seconds)

    def _uds_dtc_to_display(dtc_val: int) -> str:
        v = int(dtc_val) & 0xFFFFFF
        a = (v >> 16) & 0xFF
        b = (v >> 8) & 0xFF
        c = v & 0xFF
        letter_bits = (a >> 6) & 0x03
        first_digit = (a >> 4) & 0x03
        letter = {0: 'P', 1: 'C', 2: 'B', 3: 'U'}.get(letter_bits, 'P')
        d2 = a & 0x0F
        return f"{letter}{first_digit:X}{d2:X}{b:02X}{c:02X}".upper()

    # Regex-based extraction for speed on large ODX files.
    re_env_data_block = re.compile(
        r'<ENV-DATA[^>]*ID="([^"]*)"[^>]*>([\s\S]*?)</ENV-DATA>',
        re.IGNORECASE,
    )
    re_short = re.compile(r'<SHORT-NAME>([^<]+)</SHORT-NAME>', re.IGNORECASE)
    re_long = re.compile(r'<LONG-NAME[^>]*>([^<]+)</LONG-NAME>', re.IGNORECASE)
    re_byte_len = re.compile(r'<BYTE-LENGTH>(\d+)</BYTE-LENGTH>', re.IGNORECASE)
    re_param = re.compile(
        r'<PARAM[^>]*>[\s\S]*?<SHORT-NAME>([^<]+)</SHORT-NAME>[\s\S]*?'
        r'(?:<BYTE-POSITION>(\d+)</BYTE-POSITION>)?[\s\S]*?'
        r'(?:<BIT-POSITION>(\d+)</BIT-POSITION>)?[\s\S]*?</PARAM>',
        re.IGNORECASE,
    )
    re_param_ln = re.compile(r'<LONG-NAME[^>]*>([^<]+)</LONG-NAME>', re.IGNORECASE)
    re_param_dop = re.compile(r'<DOP-SNREF[^>]*ID-REF="([^"]+)"', re.IGNORECASE)
    # DTC → ENV-DATA-REF mapping
    re_dtc_block = re.compile(
        r'<DTC[^>]*>([\s\S]*?)</DTC>',
        re.IGNORECASE,
    )
    re_display_code = re.compile(r'<DISPLAY-TROUBLE-CODE>([^<]+)</DISPLAY-TROUBLE-CODE>', re.IGNORECASE)
    re_trouble_code = re.compile(r'<TROUBLE-CODE>([^<]+)</TROUBLE-CODE>', re.IGNORECASE)
    re_env_ref = re.compile(r'<ENV-DATA[^>]*-REF[^>]*ID-REF="([^"]*)"', re.IGNORECASE)

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        names = [n for n in zf.namelist() if isinstance(n, str)]
        candidates = [
            n for n in names
            if n.lower().endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.xml'))
        ]
        # Prefer ECU-variant files
        def score(name: str) -> int:
            nl = name.lower()
            s = 0
            base = os.path.basename(nl)
            if base.startswith('ev_'): s += 50
            if base.startswith('bv_'): s += 40
            if 'gatew' in nl: s += 15
            if nl.endswith(('.odx', '.odx-d')): s += 1
            return -s
        candidates = sorted(candidates, key=score)
        limit = int(max_files) if isinstance(max_files, int) and max_files > 0 else None

        for name in candidates:
            if time_exceeded():
                break
            if limit is not None and files_scanned >= limit:
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            if b'<' not in raw[:2048]:
                continue

            head = raw[:min(len(raw), 2_000_000)]
            has_env = b'ENV-DATA' in head or b'ENV_DATA' in head
            has_dtc = b'<DTC' in head or b'DISPLAY-TROUBLE-CODE' in head
            if not has_env and not has_dtc:
                continue

            files_scanned += 1
            txt = raw.decode('utf-8', 'ignore')

            # Extract ENV-DATA structure definitions
            for m in re_env_data_block.finditer(txt):
                env_id = m.group(1).strip()
                body = m.group(2)
                sn = re_short.search(body)
                ln = re_long.search(body)
                bl = re_byte_len.search(body)
                params: List[Dict[str, Any]] = []
                for pm in re_param.finditer(body):
                    pb = pm.group(0)
                    ln_m = re_param_ln.search(pb)
                    dop_m = re_param_dop.search(pb)
                    params.append({
                        'short_name': pm.group(1).strip(),
                        'long_name': ln_m.group(1).strip() if ln_m else '',
                        'dop_ref': dop_m.group(1).strip() if dop_m else '',
                        'byte_position': int(pm.group(2)) if pm.group(2) else None,
                        'bit_position': int(pm.group(3)) if pm.group(3) else 0,
                    })
                env_data[env_id] = {
                    'short_name': sn.group(1).strip() if sn else '',
                    'long_name': ln.group(1).strip() if ln else '',
                    'byte_length': int(bl.group(1)) if bl else None,
                    'params': params,
                    'source_file': name,
                }

            # Extract DTC → ENV-DATA-REF mappings
            for dm in re_dtc_block.finditer(txt):
                dtc_body = dm.group(1)
                dc = re_display_code.search(dtc_body)
                tc = re_trouble_code.search(dtc_body)
                code = None
                if dc:
                    code = dc.group(1).strip().upper()
                elif tc:
                    sv = tc.group(1).strip()
                    try:
                        if sv.lower().startswith('0x'):
                            sv = sv[2:]
                        if re.fullmatch(r'[0-9A-Fa-f]{6}', sv):
                            code = _uds_dtc_to_display(int(sv, 16))
                    except Exception:
                        pass
                if not code:
                    continue
                refs = re_env_ref.findall(dtc_body)
                if refs:
                    existing = dtc_env_mapping.get(code, [])
                    for r in refs:
                        r = r.strip()
                        if r and r not in existing:
                            existing.append(r)
                    dtc_env_mapping[code] = existing

    return {
        'ok': True,
        'elapsed_ms': int((time.time() - start) * 1000),
        'files_scanned': files_scanned,
        'env_data_count': len(env_data),
        'env_data': env_data,
        'dtc_env_mapping_count': len(dtc_env_mapping),
        'dtc_env_mapping': dtc_env_mapping,
    }


# ---------------------------------------------------------------------------
# E: Snapshot (Freeze Frame) DID mapping from PDX
# ---------------------------------------------------------------------------

def build_snapshot_did_index_from_pdx(
    pdx_path: str,
    *,
    max_files: Optional[int] = None,
    max_seconds: float = 120.0,
) -> Dict[str, Any]:
    """Extract DTC → Snapshot DID mappings from a PDX.

    Scans ODX files for SNAPSHOT / FREEZE-FRAME / DTC-SNAPSHOT structures and
    extracts which DIDs are associated with each DTC's freeze frame record.

    Returns:
      {
        ok, elapsed_ms, files_scanned,
        snapshot_def_count,
        snapshot_defs: { snapshot_id: { short_name, dids: [ { did_hex, short_name, byte_length } ] } },
        dtc_snapshot_mapping_count,
        dtc_snapshot_mapping: { dtc_display_code: [snapshot_id, ...] },
      }
    """
    start = time.time()
    pdx_path = os.path.abspath(pdx_path)
    if not os.path.isfile(pdx_path):
        raise FileNotFoundError(pdx_path)

    snapshot_defs: Dict[str, Dict[str, Any]] = {}
    dtc_snapshot_mapping: Dict[str, List[str]] = {}
    files_scanned = 0

    def time_exceeded() -> bool:
        return (time.time() - start) > float(max_seconds)

    def _uds_dtc_to_display(dtc_val: int) -> str:
        v = int(dtc_val) & 0xFFFFFF
        a = (v >> 16) & 0xFF
        b = (v >> 8) & 0xFF
        c = v & 0xFF
        letter_bits = (a >> 6) & 0x03
        first_digit = (a >> 4) & 0x03
        letter = {0: 'P', 1: 'C', 2: 'B', 3: 'U'}.get(letter_bits, 'P')
        d2 = a & 0x0F
        return f"{letter}{first_digit:X}{d2:X}{b:02X}{c:02X}".upper()

    # Regex patterns for snapshot extraction
    re_snap_block = re.compile(
        r'<(?:DTC-)?SNAPSHOT[^>]*ID="([^"]*)"[^>]*>([\s\S]*?)</(?:DTC-)?SNAPSHOT>',
        re.IGNORECASE,
    )
    re_short = re.compile(r'<SHORT-NAME>([^<]+)</SHORT-NAME>', re.IGNORECASE)
    re_did_ref = re.compile(
        r'<(?:DATA-IDENTIFIER|DID|DATAID)[^>]*(?:ID-REF="([^"]*)")?[^>]*>'
        r'(?:[\s\S]*?<(?:ID|IDENTIFIER)>([^<]+)</(?:ID|IDENTIFIER)>)?',
        re.IGNORECASE,
    )
    re_byte_len = re.compile(r'<BYTE-LENGTH>(\d+)</BYTE-LENGTH>', re.IGNORECASE)

    re_dtc_block = re.compile(r'<DTC[^>]*>([\s\S]*?)</DTC>', re.IGNORECASE)
    re_display_code = re.compile(r'<DISPLAY-TROUBLE-CODE>([^<]+)</DISPLAY-TROUBLE-CODE>', re.IGNORECASE)
    re_trouble_code = re.compile(r'<TROUBLE-CODE>([^<]+)</TROUBLE-CODE>', re.IGNORECASE)
    re_snap_ref = re.compile(r'<(?:DTC-)?SNAPSHOT[^>]*-REF[^>]*ID-REF="([^"]*)"', re.IGNORECASE)

    # Also look for freeze-frame structure blocks (VW-specific)
    re_ff_block = re.compile(
        r'<FREEZE-FRAME[^>]*ID="([^"]*)"[^>]*>([\s\S]*?)</FREEZE-FRAME>',
        re.IGNORECASE,
    )

    with zipfile.ZipFile(pdx_path, 'r') as zf:
        names = [n for n in zf.namelist() if isinstance(n, str)]
        candidates = [
            n for n in names
            if n.lower().endswith(('.odx', '.odx-d', '.odx-c', '.odx-e', '.xml'))
        ]
        def score(name: str) -> int:
            nl = name.lower()
            s = 0
            base = os.path.basename(nl)
            if base.startswith('ev_'): s += 50
            if base.startswith('bv_'): s += 40
            if 'gatew' in nl: s += 15
            if nl.endswith(('.odx', '.odx-d')): s += 1
            return -s
        candidates = sorted(candidates, key=score)
        limit = int(max_files) if isinstance(max_files, int) and max_files > 0 else None

        for name in candidates:
            if time_exceeded():
                break
            if limit is not None and files_scanned >= limit:
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            if b'<' not in raw[:2048]:
                continue

            head = raw[:min(len(raw), 2_000_000)]
            if (b'SNAPSHOT' not in head and b'FREEZE-FRAME' not in head
                    and b'Snapshot' not in head and b'FreezeFrame' not in head):
                continue

            files_scanned += 1
            txt = raw.decode('utf-8', 'ignore')

            # Extract snapshot structure definitions
            for pattern in (re_snap_block, re_ff_block):
                for m in pattern.finditer(txt):
                    snap_id = m.group(1).strip()
                    body = m.group(2)
                    sn = re_short.search(body)
                    dids: List[Dict[str, Any]] = []
                    for dr in re_did_ref.finditer(body):
                        did_ref = (dr.group(1) or '').strip()
                        did_val = (dr.group(2) or '').strip()
                        did_hex = ''
                        if did_val:
                            try:
                                t = did_val
                                if t.startswith('$'):
                                    t = '0x' + t[1:]
                                iv = int(t, 0)
                                did_hex = f"0x{iv & 0xFFFF:04X}"
                            except Exception:
                                did_hex = did_val
                        bl = re_byte_len.search(body)
                        dids.append({
                            'did_hex': did_hex,
                            'did_ref': did_ref,
                            'byte_length': int(bl.group(1)) if bl else None,
                        })
                    snapshot_defs[snap_id] = {
                        'short_name': sn.group(1).strip() if sn else '',
                        'dids': dids,
                        'source_file': name,
                    }

            # Extract DTC → SNAPSHOT-REF mappings
            for dm in re_dtc_block.finditer(txt):
                dtc_body = dm.group(1)
                dc = re_display_code.search(dtc_body)
                tc = re_trouble_code.search(dtc_body)
                code = None
                if dc:
                    code = dc.group(1).strip().upper()
                elif tc:
                    sv = tc.group(1).strip()
                    try:
                        if sv.lower().startswith('0x'):
                            sv = sv[2:]
                        if re.fullmatch(r'[0-9A-Fa-f]{6}', sv):
                            code = _uds_dtc_to_display(int(sv, 16))
                    except Exception:
                        pass
                if not code:
                    continue
                refs = re_snap_ref.findall(dtc_body)
                if refs:
                    existing = dtc_snapshot_mapping.get(code, [])
                    for r in refs:
                        r = r.strip()
                        if r and r not in existing:
                            existing.append(r)
                    dtc_snapshot_mapping[code] = existing

    return {
        'ok': True,
        'elapsed_ms': int((time.time() - start) * 1000),
        'files_scanned': files_scanned,
        'snapshot_def_count': len(snapshot_defs),
        'snapshot_defs': snapshot_defs,
        'dtc_snapshot_mapping_count': len(dtc_snapshot_mapping),
        'dtc_snapshot_mapping': dtc_snapshot_mapping,
    }
