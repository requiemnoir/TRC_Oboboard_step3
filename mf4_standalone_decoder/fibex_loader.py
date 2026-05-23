import os
import re
import xml.etree.ElementTree as etree

class FibexLoader:
    def __init__(self):
        # slot_id (or similar) -> display name
        self.frames: dict[int, str] = {}
        # Full list of frame-triggerings (for UI/catalog). Each entry references a slot_id.
        self.triggerings: list[dict] = []
        # slot_id -> list of signal metadata (for UI)
        self.signals: dict[int, list[dict]] = {}
        # slot_id -> list of signal decode defs (internal)
        self._signal_defs: dict[int, list[dict]] = {}
        # slot_id -> list of triggering variants with their frame_ref and signal defs
        self._variants: dict[int, list[dict]] = {}
        self.filename = None
        # Optimised decode caches (populated lazily)
        self._prepared_base: dict[int, list] = {}
        self._prepared_cycle_cache: dict[tuple[int, int], list] = {}

    def _dedup_keep_order(self, items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for it in items:
            s = str(it or '').strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _localname(self, tag: str) -> str:
        if not tag:
            return ''
        # ElementTree represents namespaced tags like '{ns}FRAME'
        if '}' in tag:
            return tag.split('}', 1)[1]
        return tag

    def _parse_int_loose(self, s: str) -> int | None:
        if s is None:
            return None
        t = str(s).strip()
        if not t:
            return None
        # Common patterns: '0x123', '123', sometimes embedded digits.
        try:
            return int(t, 0)
        except Exception:
            pass
        m = re.search(r'(0x[0-9a-fA-F]+|\d+)', t)
        if not m:
            return None
        try:
            return int(m.group(1), 0)
        except Exception:
            return None

    def load(self, filepath):
        try:
            self.frames = {}
            self.triggerings = []
            self.signals = {}
            self._signal_defs = {}
            self._variants = {}
            self._prepared_base = {}
            self._prepared_cycle_cache = {}

            # Stream parse to handle large XML files (KMatrix exports can be huge).
            # Goal: provide "human" names AND decodeable signal definitions.
            # We build: FRAME-TRIGGERING(slot-id) -> FRAME -> PDU(s) -> SIGNAL-INSTANCE -> SIGNAL -> CODING(bit-length)

            want_name = {'SHORT-NAME', 'NAME'}
            target_tags = {
                'PDU', 'FRAME', 'FRAME-TRIGGERING',
                'SIGNAL', 'CODING',
                'SIGNAL-INSTANCE', 'PDU-INSTANCE',
                'COMPU-SCALE',
                'COMPU-METHOD', 'SCALE-CONSTR',
                'COMPU-NUMERATOR', 'COMPU-DENOMINATOR',
            }

            # Parsed maps (IDs are strings; final frame_id is int slot-id)
            pdu_by_id: dict[str, dict] = {}
            frame_by_id: dict[str, dict] = {}
            signal_by_id: dict[str, dict] = {}
            coding_by_id: dict[str, dict] = {}
            frame_triggerings: list[dict] = []

            stack: list[dict] = []

            def _nearest(tag: str) -> dict | None:
                for ctx in reversed(stack):
                    if ctx.get('tag') == tag:
                        return ctx
                return None

            for ev, el in etree.iterparse(filepath, events=('start', 'end')):
                ln = self._localname(getattr(el, 'tag', ''))

                if ev == 'start':
                    if ln in target_tags:
                        ctx = {
                            'tag': ln,
                            'id': (el.attrib.get('ID') or el.attrib.get('Id') or el.attrib.get('id') or '').strip() or None,
                            'name': None,
                            'desc': None,
                            'refs': [],
                            'fields': {},
                        }
                        # Capture attributes that matter (e.g., ENCODING on CODED-TYPE nested under CODING)
                        if ln == 'CODED-TYPE':
                            pass
                        stack.append(ctx)
                    # Capture CODED-TYPE attributes while inside a CODING
                    if ln == 'CODED-TYPE':
                        cctx = _nearest('CODING')
                        if cctx is not None:
                            enc = (el.attrib.get('ENCODING') or el.attrib.get('encoding') or '').strip()
                            if enc and not cctx['fields'].get('encoding'):
                                cctx['fields']['encoding'] = enc.upper()
                    continue

                # ev == 'end'
                if stack:
                    # Assign SHORT-NAME/NAME to the nearest open target that doesn't have one yet.
                    if ln in want_name:
                        t = (el.text or '').strip()
                        if t:
                            for ctx in reversed(stack):
                                if ctx.get('name') is None and ctx.get('tag') in target_tags:
                                    ctx['name'] = t
                                    break

                    # Best-effort descriptions
                    if ln in {'DESC', 'DESCRIPTION'}:
                        t = (el.text or '').strip()
                        if t:
                            for ctx in reversed(stack):
                                if ctx.get('desc') is None and ctx.get('tag') in {'SIGNAL', 'PDU', 'FRAME'}:
                                    ctx['desc'] = t
                                    break

                    # Numeric fields
                    if ln in {'SLOT-ID', 'BASE-CYCLE', 'CYCLE-REPETITION', 'BIT-POSITION', 'PDU-UPDATE-BIT-POSITION', 'BYTE-LENGTH', 'BIT-LENGTH'}:
                        t = (el.text or '').strip()
                        v = self._parse_int_loose(t)
                        if v is not None:
                            # Associate based on field type
                            if ln in {'SLOT-ID', 'BASE-CYCLE', 'CYCLE-REPETITION'}:
                                ft = _nearest('FRAME-TRIGGERING')
                                if ft is not None:
                                    ft['fields'][ln.lower().replace('-', '_')] = v
                            elif ln == 'BYTE-LENGTH':
                                for tg in ('PDU', 'FRAME'):
                                    c = _nearest(tg)
                                    if c is not None and c['fields'].get('byte_length') is None:
                                        c['fields']['byte_length'] = v
                                        break
                            elif ln == 'BIT-LENGTH':
                                c = _nearest('CODING')
                                if c is not None and c['fields'].get('bit_length') is None:
                                    c['fields']['bit_length'] = v
                            elif ln == 'BIT-POSITION':
                                # SIGNAL-INSTANCE or PDU-INSTANCE
                                c = _nearest('SIGNAL-INSTANCE') or _nearest('PDU-INSTANCE')
                                if c is not None:
                                    c['fields']['bit_position'] = v
                            elif ln == 'PDU-UPDATE-BIT-POSITION':
                                c = _nearest('PDU-INSTANCE')
                                if c is not None:
                                    c['fields']['pdu_update_bit_position'] = v

                    if ln == 'IS-HIGH-LOW-BYTE-ORDER':
                        t = (el.text or '').strip().lower()
                        b = t in {'1', 'true', 'yes', 'on'}
                        c = _nearest('SIGNAL-INSTANCE') or _nearest('PDU-INSTANCE')
                        if c is not None:
                            c['fields']['is_high_low_byte_order'] = b

                    # Capture category (TEXTTABLE/LINEAR etc) within CODING
                    if ln == 'CATEGORY':
                        t = (el.text or '').strip()
                        if t:
                            c = _nearest('COMPU-METHOD')
                            if c is not None and not c['fields'].get('category'):
                                c['fields']['category'] = t.strip().upper()
                            else:
                                c = _nearest('CODING')
                                if c is not None and not c['fields'].get('category'):
                                    c['fields']['category'] = t.strip().upper()

                    # COMPU-SCALE text-table values
                    if ln in {'LOWER-LIMIT', 'UPPER-LIMIT', 'VT'}:
                        t = (el.text or '').strip()
                        cs = _nearest('COMPU-SCALE')
                        if cs is not None and t:
                            if ln == 'VT':
                                cs['fields']['vt'] = t
                            else:
                                v = self._parse_int_loose(t)
                                if v is not None:
                                    cs['fields'][ln.lower().replace('-', '_')] = v

                    if ln == 'V':
                        t = (el.text or '').strip()
                        try:
                            v = float(t)
                        except Exception:
                            v = None
                        if v is not None:
                            num = _nearest('COMPU-NUMERATOR')
                            if num is not None:
                                vals = num['fields'].get('values')
                                if not isinstance(vals, list):
                                    vals = []
                                vals.append(v)
                                num['fields']['values'] = vals
                            den = _nearest('COMPU-DENOMINATOR')
                            if den is not None:
                                vals = den['fields'].get('values')
                                if not isinstance(vals, list):
                                    vals = []
                                vals.append(v)
                                den['fields']['values'] = vals

                    # Capture ID references inside current target.
                    idref = None
                    try:
                        idref = (el.attrib.get('ID-REF') or el.attrib.get('ID_REF') or el.attrib.get('id-ref') or el.attrib.get('id_ref'))
                    except Exception:
                        idref = None
                    if idref:
                        stack[-1]['refs'].append((ln, str(idref).strip()))
                    else:
                        if ln.endswith('-REF'):
                            t = (el.text or '').strip()
                            if t:
                                stack[-1]['refs'].append((ln, t))

                    # Closing a target element? finalize.
                    if ln == stack[-1].get('tag'):
                        ctx = stack.pop()
                        tag = ctx.get('tag')
                        ctx_id = ctx.get('id')
                        ctx_name = (ctx.get('name') or '').strip() or None
                        ctx_desc = (ctx.get('desc') or '').strip() or None
                        refs = list(ctx.get('refs') or [])
                        fields = dict(ctx.get('fields') or {})

                        if tag == 'CODING':
                            if ctx_id:
                                coding_by_id[ctx_id] = {
                                    'id': ctx_id,
                                    'name': ctx_name,
                                    'bit_length': fields.get('bit_length'),
                                    'encoding': fields.get('encoding') or 'UNSIGNED',
                                    'category': fields.get('category') or '',
                                    'text_table': dict(fields.get('text_table') or {}),
                                    'linear_num': list(fields.get('linear_num') or []),
                                    'linear_den': list(fields.get('linear_den') or []),
                                }

                        elif tag == 'COMPU-NUMERATOR':
                            cs = _nearest('COMPU-SCALE')
                            if cs is not None:
                                cs['fields']['numerator_values'] = list(fields.get('values') or [])

                        elif tag == 'COMPU-DENOMINATOR':
                            cs = _nearest('COMPU-SCALE')
                            if cs is not None:
                                cs['fields']['denominator_values'] = list(fields.get('values') or [])

                        elif tag == 'SCALE-CONSTR':
                            cm = _nearest('COMPU-METHOD')
                            if cm is not None:
                                if fields.get('lower_limit') is not None and cm['fields'].get('scale_lower') is None:
                                    cm['fields']['scale_lower'] = fields.get('lower_limit')
                                if fields.get('upper_limit') is not None and cm['fields'].get('scale_upper') is None:
                                    cm['fields']['scale_upper'] = fields.get('upper_limit')

                        elif tag == 'COMPU-SCALE':
                            cm = _nearest('COMPU-METHOD')
                            if cm is not None:
                                method_category = str(cm['fields'].get('category') or '').upper()
                                lo = fields.get('lower_limit')
                                up = fields.get('upper_limit')
                                vt = fields.get('vt')
                                if method_category == 'TEXTTABLE' and lo is not None and up is not None and lo == up and vt:
                                    tt = cm['fields'].get('text_table')
                                    if not isinstance(tt, dict):
                                        tt = {}
                                    tt[int(lo)] = str(vt)
                                    cm['fields']['text_table'] = tt
                                elif method_category == 'LINEAR' and not cm['fields'].get('linear_num'):
                                    nums = list(fields.get('numerator_values') or [])
                                    dens = list(fields.get('denominator_values') or [])
                                    if nums:
                                        cm['fields']['linear_num'] = nums
                                        cm['fields']['linear_den'] = dens

                        elif tag == 'COMPU-METHOD':
                            c = _nearest('CODING')
                            if c is not None:
                                method_category = str(fields.get('category') or '').upper()
                                if method_category == 'TEXTTABLE':
                                    tt = c['fields'].get('text_table')
                                    if not isinstance(tt, dict):
                                        tt = {}
                                    tt.update(dict(fields.get('text_table') or {}))
                                    c['fields']['text_table'] = tt
                                elif method_category == 'LINEAR' and not c['fields'].get('linear_num'):
                                    nums = list(fields.get('linear_num') or [])
                                    dens = list(fields.get('linear_den') or [])
                                    if nums:
                                        c['fields']['linear_num'] = nums
                                        c['fields']['linear_den'] = dens

                        elif tag == 'SIGNAL':
                            if ctx_id and ctx_name:
                                coding_ref = None
                                for rtag, rid in refs:
                                    if rtag == 'CODING-REF':
                                        coding_ref = rid
                                        break
                                signal_by_id[ctx_id] = {
                                    'id': ctx_id,
                                    'name': ctx_name,
                                    'desc': ctx_desc,
                                    'coding_ref': coding_ref,
                                }

                        elif tag == 'SIGNAL-INSTANCE':
                            # Attach to nearest PDU
                            p = _nearest('PDU')
                            if p is not None and p.get('id'):
                                pdu_id = p.get('id')
                                lst = p['fields'].get('signal_instances')
                                if not isinstance(lst, list):
                                    lst = []
                                sig_ref = None
                                for rtag, rid in refs:
                                    if rtag == 'SIGNAL-REF':
                                        sig_ref = rid
                                        break
                                if sig_ref:
                                    lst.append({
                                        'signal_ref': sig_ref,
                                        'bit_position': int(fields.get('bit_position') or 0),
                                        'is_high_low_byte_order': bool(fields.get('is_high_low_byte_order', False)),
                                    })
                                    p['fields']['signal_instances'] = lst

                        elif tag == 'PDU':
                            if ctx_id and ctx_name:
                                pdu_by_id[ctx_id] = {
                                    'id': ctx_id,
                                    'name': ctx_name,
                                    'desc': ctx_desc,
                                    'byte_length': fields.get('byte_length'),
                                    'signal_instances': list(fields.get('signal_instances') or []),
                                }

                        elif tag == 'PDU-INSTANCE':
                            # Attach to nearest FRAME
                            fr = _nearest('FRAME')
                            if fr is not None and fr.get('id'):
                                frame_id = fr.get('id')
                                lst = fr['fields'].get('pdu_instances')
                                if not isinstance(lst, list):
                                    lst = []
                                pdu_ref = None
                                for rtag, rid in refs:
                                    if rtag == 'PDU-REF':
                                        pdu_ref = rid
                                        break
                                if pdu_ref:
                                    lst.append({
                                        'pdu_ref': pdu_ref,
                                        'bit_position': int(fields.get('bit_position') or 0),
                                        'pdu_update_bit_position': fields.get('pdu_update_bit_position'),
                                        'is_high_low_byte_order': bool(fields.get('is_high_low_byte_order', False)),
                                    })
                                    fr['fields']['pdu_instances'] = lst

                        elif tag == 'FRAME':
                            if ctx_id and ctx_name:
                                frame_by_id[ctx_id] = {
                                    'id': ctx_id,
                                    'name': ctx_name,
                                    'desc': ctx_desc,
                                    'byte_length': fields.get('byte_length'),
                                    'pdu_instances': list(fields.get('pdu_instances') or []),
                                }

                        elif tag == 'FRAME-TRIGGERING':
                            frame_ref = None
                            for rtag, rid in refs:
                                if rtag == 'FRAME-REF' and frame_ref is None:
                                    frame_ref = rid
                                    break
                            slot_id = fields.get('slot_id')
                            if slot_id is not None:
                                frame_triggerings.append({
                                    'slot_id': int(slot_id),
                                    'base_cycle': int(fields.get('base_cycle') or 0),
                                    'cycle_repetition': int(fields.get('cycle_repetition') or 0),
                                    'name': ctx_name,
                                    'frame_ref': frame_ref,
                                })

                el.clear()

            # Resolve PDU -> signals with bit lengths from CODING
            pdu_sig_defs: dict[str, list[dict]] = {}
            for pdu_id, pdu in pdu_by_id.items():
                defs: list[dict] = []
                for inst in (pdu.get('signal_instances') or []):
                    try:
                        sig_ref = str(inst.get('signal_ref') or '').strip()
                        sig = signal_by_id.get(sig_ref) if sig_ref else None
                        if not sig:
                            continue
                        sig_name = str(sig.get('name') or '').strip()
                        if not sig_name:
                            continue
                        coding_ref = sig.get('coding_ref')
                        coding = coding_by_id.get(str(coding_ref)) if coding_ref else None
                        bit_length = None
                        encoding = 'UNSIGNED'
                        text_table = {}
                        if coding:
                            bit_length = coding.get('bit_length')
                            encoding = str(coding.get('encoding') or 'UNSIGNED').upper()
                            tt = coding.get('text_table')
                            if isinstance(tt, dict):
                                text_table = dict(tt)
                            linear_num = list(coding.get('linear_num') or [])
                            linear_den = list(coding.get('linear_den') or [])
                        else:
                            linear_num = []
                            linear_den = []
                        if bit_length is None:
                            continue
                        defs.append({
                            'name': sig_name,
                            'desc': sig.get('desc') or '',
                            'start_bit': int(inst.get('bit_position') or 0),
                            'bit_length': int(bit_length),
                            'encoding': encoding,
                            'text_table': text_table,
                            'linear_num': linear_num,
                            'linear_den': linear_den,
                            'is_high_low_byte_order': bool(inst.get('is_high_low_byte_order', False)),
                        })
                    except Exception:
                        continue
                pdu_sig_defs[pdu_id] = defs

            # Resolve FRAME -> signals (apply PDU instance bit offset)
            frame_sig_defs: dict[str, list[dict]] = {}
            for frame_id, fr in frame_by_id.items():
                defs: list[dict] = []
                for pi in (fr.get('pdu_instances') or []):
                    try:
                        pdu_ref = str(pi.get('pdu_ref') or '').strip()
                        base = int(pi.get('bit_position') or 0)
                        pdu_hl = bool(pi.get('is_high_low_byte_order', False))
                        pdu_update_bit = pi.get('pdu_update_bit_position')
                        for sd in (pdu_sig_defs.get(pdu_ref) or []):
                            d = dict(sd)
                            d['start_bit'] = int(d.get('start_bit') or 0) + base
                            d['is_high_low_byte_order'] = bool(d.get('is_high_low_byte_order', False)) or pdu_hl
                            d['pdu_update_bit_position'] = pdu_update_bit
                            defs.append(d)
                    except Exception:
                        continue
                frame_sig_defs[frame_id] = defs

            # Build frames + signals keyed by slot-id.
            for ft in frame_triggerings:
                slot_id = int(ft.get('slot_id') or 0)
                frame_ref = ft.get('frame_ref')
                if not slot_id or not frame_ref:
                    continue
                fr = frame_by_id.get(str(frame_ref))
                # Human name: prefer PDU names from first PDU in the frame
                pdu_names: list[str] = []
                try:
                    for pi in (fr.get('pdu_instances') if fr else []) or []:
                        pdu_ref = str(pi.get('pdu_ref') or '').strip()
                        pn = (pdu_by_id.get(pdu_ref) or {}).get('name')
                        if pn:
                            pdu_names.append(str(pn))
                except Exception:
                    pdu_names = []
                pdu_names = self._dedup_keep_order(pdu_names)

                frame_name = (fr or {}).get('name') if isinstance(fr, dict) else None
                ft_name = (ft.get('name') or '').strip() or None

                if pdu_names:
                    if len(pdu_names) == 1:
                        base = pdu_names[0]
                    else:
                        base = f"{pdu_names[0]} + {pdu_names[1]}"
                        if len(pdu_names) > 2:
                            base = f"{base} (+{len(pdu_names) - 2} more)"
                else:
                    base = str(frame_name or ft_name or f'FlexRaySlot{slot_id}').strip()

                extras: list[str] = []
                if frame_name and str(frame_name).strip() and str(frame_name).strip() != base:
                    extras.append(f"frame {str(frame_name).strip()}")
                if ft_name and ft_name != base and ft_name != frame_name:
                    if not re.match(r'^FRAME_\d+_', ft_name, flags=re.IGNORECASE):
                        extras.append(ft_name)
                # Include cycle fields to avoid collapsing many triggerings into identical rows.
                try:
                    bc = int(ft.get('base_cycle') or 0)
                    cr = int(ft.get('cycle_repetition') or 0)
                    if bc or cr:
                        extras.append(f"cycle {bc}/{cr}")
                except Exception:
                    pass
                # Always include slot-id for determinism
                extras.append(f"slot {slot_id}")

                display = base
                if extras:
                    display = f"{base} ({', '.join(extras[:3])})"

                # Keep one representative name for runtime decode (slot-id keyed).
                if slot_id not in self.frames:
                    self.frames[slot_id] = display

                # Keep all triggerings for UI/catalog listing.
                try:
                    self.triggerings.append({
                        'slot_id': slot_id,
                        'base_cycle': int(ft.get('base_cycle') or 0),
                        'cycle_repetition': int(ft.get('cycle_repetition') or 0),
                        'name': display,
                        'frame_ref': str(frame_ref),
                    })
                except Exception:
                    self.triggerings.append({'slot_id': slot_id, 'name': display})

                # NOTE: the same slot-id can have multiple FRAME-TRIGGERING variants
                # (different base_cycle/cycle_repetition) pointing to different FRAMEs.
                # Runtime decode currently uses only slot-id, so we keep a merged union
                # of signal defs across all variants rather than overwriting.
                sig_defs = list(frame_sig_defs.get(str(frame_ref)) or [])

                existing = list(self._signal_defs.get(slot_id) or [])
                merged: list[dict] = []
                seen_keys: set[tuple] = set()

                for d in existing + sig_defs:
                    try:
                        name = str(d.get('name') or '').strip()
                        if not name:
                            continue
                        key = (
                            name,
                            int(d.get('start_bit') or 0),
                            int(d.get('bit_length') or 0),
                            str(d.get('encoding') or 'UNSIGNED').upper(),
                            bool(d.get('is_high_low_byte_order', False)),
                        )
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        merged.append(d)
                    except Exception:
                        continue

                self._signal_defs[slot_id] = merged

                # Keep variant-specific defs for cycle-aware runtime decode.
                try:
                    bc = int(ft.get('base_cycle') or 0)
                    cr = int(ft.get('cycle_repetition') or 0)
                except Exception:
                    bc, cr = 0, 0
                try:
                    vlist = self._variants.get(slot_id)
                    if not isinstance(vlist, list):
                        vlist = []
                    vlist.append({
                        'base_cycle': bc,
                        'cycle_repetition': cr,
                        'frame_ref': str(frame_ref),
                        'name': display,
                        'signal_defs': sig_defs,
                    })
                    self._variants[slot_id] = vlist
                except Exception:
                    pass

                # For UI: de-dup by signal name only.
                ui_seen: set[str] = set()
                ui_sigs: list[dict] = []
                for d in merged:
                    n = str(d.get('name') or '').strip()
                    if not n or n in ui_seen:
                        continue
                    ui_seen.add(n)
                    ui_sigs.append({'name': n, 'comment': d.get('desc') or ''})
                self.signals[slot_id] = ui_sigs

            self.filename = os.path.basename(filepath)
            print(f"Loaded FIBEX: {self.filename} with {len(self.frames)} frames.")
            return True
        except Exception as e:
            print(f"Error loading FIBEX: {e}")
            return False

    # ── optimised decode helpers ──────────────────────────────────────────

    def _prepare_sig_list(self, raw_defs: list[dict]) -> list[tuple]:
        """Convert signal-def dicts to pre-processed tuples for fast decode."""
        prepared: list[tuple] = []
        for d in raw_defs:
            name = str(d.get('name') or '').strip()
            if not name:
                continue
            start = int(d.get('start_bit') or 0)
            blen = int(d.get('bit_length') or 0)
            if blen <= 0:
                continue

            mask = (1 << blen) - 1
            is_hl = bool(d.get('is_high_low_byte_order', False))

            enc = str(d.get('encoding') or 'UNSIGNED').upper()
            is_signed = (enc == 'SIGNED')
            sign_bit = (1 << (blen - 1)) if is_signed else 0
            sign_sub = (1 << blen) if is_signed else 0

            update_bit_raw = d.get('pdu_update_bit_position')
            update_bit = int(update_bit_raw) if update_bit_raw is not None else -1

            tt = d.get('text_table')
            if not isinstance(tt, dict) or not tt:
                tt = None

            # Pre-compute linear scaling coefficients
            linear_num = d.get('linear_num') or []
            linear_den = d.get('linear_den') or []
            has_linear = False
            lin_a = 0.0
            lin_b = 1.0
            if isinstance(linear_num, (list, tuple)) and linear_num:
                denom = 1.0
                if isinstance(linear_den, (list, tuple)) and linear_den:
                    if len(linear_den) == 1:
                        denom = float(linear_den[0])
                    elif len(linear_den) >= 2 and float(linear_den[1]) == 0.0:
                        denom = float(linear_den[0])
                if denom != 0.0:
                    offset = float(linear_num[0]) if len(linear_num) >= 1 else 0.0
                    factor = float(linear_num[1]) if len(linear_num) >= 2 else 1.0
                    has_linear = True
                    lin_a = offset / denom
                    lin_b = factor / denom

            name_txt = f"{name}_txt"

            prepared.append((
                name, name_txt, start, blen, mask,
                is_hl, is_signed, sign_bit, sign_sub,
                update_bit, tt, has_linear, lin_a, lin_b,
            ))
        return prepared

    def _get_prepared_defs(self, fid: int, cycle: int | None) -> list:
        """Return prepared signal-def tuples for *(fid, cycle)*, with caching."""
        if cycle is None:
            pdefs = self._prepared_base.get(fid)
            if pdefs is None:
                raw = self._signal_defs.get(fid) or []
                pdefs = self._prepare_sig_list(raw)
                self._prepared_base[fid] = pdefs
            return pdefs

        cyc = int(cycle) & 0x3F
        cache_key = (fid, cyc)
        pdefs = self._prepared_cycle_cache.get(cache_key)
        if pdefs is not None:
            return pdefs

        # Merge variant defs for this (fid, cycle)
        vlist = self._variants.get(fid) or []
        if not vlist:
            pdefs = self._get_prepared_defs(fid, None)
            self._prepared_cycle_cache[cache_key] = pdefs
            return pdefs

        merged_defs: list[dict] = []
        seen_keys: set[tuple] = set()
        for v in vlist:
            bc = int(v.get('base_cycle') or 0)
            cr = int(v.get('cycle_repetition') or 0)
            if cr <= 0:
                match = True
            else:
                match = (cyc >= bc) and ((cyc - bc) % cr == 0)
            if not match:
                continue
            vdefs = v.get('signal_defs')
            if isinstance(vdefs, list):
                for d in vdefs:
                    key = (
                        str(d.get('name') or ''),
                        int(d.get('start_bit') or 0),
                        int(d.get('bit_length') or 0),
                    )
                    if key not in seen_keys:
                        seen_keys.add(key)
                        merged_defs.append(d)

        if merged_defs:
            pdefs = self._prepare_sig_list(merged_defs)
        else:
            # No variant matched — fall back to base defs
            raw = self._signal_defs.get(fid) or []
            pdefs = self._prepare_sig_list(raw)

        self._prepared_cycle_cache[cache_key] = pdefs
        return pdefs

    # ── decode ───────────────────────────────────────────────────────────

    def decode(self, frame_id, data, cycle: int | None = None):
        try:
            fid = int(frame_id)
        except Exception:
            return None

        if fid not in self.frames:
            return None

        frame_name = self.frames.get(fid) or f'FlexRay {fid}'

        # Convert data to bytes
        if isinstance(data, (bytes, bytearray)):
            b = data if isinstance(data, bytes) else bytes(data)
        elif data:
            try:
                b = bytes(data)
            except Exception:
                b = bytes()
        else:
            b = bytes()

        sigs: dict = {'raw_hex': b.hex()}

        if not b:
            return {'name': frame_name, 'signals': sigs}

        # Get pre-processed signal defs (cached per fid+cycle)
        pdefs = self._get_prepared_defs(fid, cycle)
        if not pdefs:
            return {'name': frame_name, 'signals': sigs}

        # Pre-compute integer representations for O(1) bit extraction
        int_le = int.from_bytes(b, byteorder='little')
        int_be = int.from_bytes(b, byteorder='big')
        total_bits = len(b) << 3

        for (name, name_txt, start, blen, mask,
             is_hl, is_signed, sign_bit, sign_sub,
             update_bit, tt, has_linear, lin_a, lin_b) in pdefs:

            # Bounds check
            if start + blen > total_bits:
                continue

            # Update-bit gate
            if update_bit >= 0:
                if update_bit >= total_bits:
                    continue
                if not ((int_le >> update_bit) & 1):
                    continue

            # Inline bit extraction (no function call, no per-bit loop)
            if is_hl:
                raw_int = (int_be >> (total_bits - start - blen)) & mask
            else:
                raw_int = (int_le >> start) & mask

            # Signed handling
            if is_signed and (raw_int & sign_bit):
                raw_int -= sign_sub

            display_v = float(raw_int)

            # Text table / linear scaling
            if tt is not None and raw_int in tt:
                sigs[name_txt] = str(tt[raw_int])
            elif has_linear:
                display_v = lin_a + lin_b * raw_int

            sigs[name] = display_v

        return {'name': frame_name, 'signals': sigs}

    @staticmethod
    def _extract_bits(data: bytes, start_bit: int, bit_length: int, is_high_low_byte_order: bool) -> int | None:
        if bit_length <= 0:
            return None
        if start_bit < 0:
            return None

        total_bits = len(data) * 8
        if start_bit + bit_length > total_bits:
            return None

        # Little-endian bit numbering (Intel): bit 0 is LSB of byte 0.
        if not is_high_low_byte_order:
            value = 0
            for i in range(bit_length):
                bit_index = start_bit + i
                byte_index = bit_index >> 3
                bit_in_byte = bit_index & 7
                bit = (data[byte_index] >> bit_in_byte) & 1
                value |= (bit << i)
            return value

        # High-low byte order (Motorola-like): treat bit 0 as MSB of byte 0,
        # and consume bits MSB->LSB within each byte.
        value = 0
        for i in range(bit_length):
            bit_index = start_bit + i
            byte_index = bit_index >> 3
            bit_from_msb = bit_index & 7
            bit = (data[byte_index] >> (7 - bit_from_msb)) & 1
            value = (value << 1) | bit
        return value
