import cantools
import os
import re
import tempfile


def _normalize_decoded_signals(decoded: dict) -> dict:
    out = {}
    for name, value in (decoded or {}).items():
        choice_name = None
        choice_value = None
        try:
            choice_name = getattr(value, 'name', None)
            choice_value = getattr(value, 'value', None)
        except Exception:
            choice_name = None
            choice_value = None

        if choice_name is not None:
            out[f"{name}_txt"] = str(choice_name)

        if choice_value is not None:
            try:
                if isinstance(choice_value, bool):
                    out[name] = int(choice_value)
                elif isinstance(choice_value, (int, float)):
                    out[name] = choice_value
                else:
                    out[name] = float(choice_value)
                continue
            except Exception:
                pass

        out[name] = value

    return out


def _sanitize_dbc_text(text: str) -> str:
    """Best-effort sanitation for Vector-style DBC quirks.

    - Removes attribute sections (BA_*), which are not required for decode/encode
      but frequently contain Vector-specific values that cantools rejects.
    - Rewrites extended message IDs by setting the DBC extended-frame flag bit
      (0x80000000). cantools uses this bit to mark a message as extended.
    """

    # Vector/OEM DBCs often contain attributes we don't need for decode/encode.
    # Some of these can break parsing in cantools; drop all BA_* sections.
    try:
        kept = []
        for line in text.splitlines():
            s = line.lstrip()
            if (
                s.startswith('BA_')
                or s.startswith('BA_DEF_')
                or s.startswith('BA_DEF_DEF_')
                or s.startswith('BA_DEF_REL_')
                or s.startswith('BA_DEF_DEF_REL_')
            ):
                continue
            kept.append(line)
        text = '\n'.join(kept) + '\n'
    except Exception:
        pass

    # Extended IDs: cantools expects the DBC id to have bit 31 set for extended frames.
    # Many Vector DBCs store 29-bit IDs without this flag. Fix by rewriting:
    #   id_dbc := id | 0x80000000
    try:
        mapping: dict[int, int] = {}

        for m in re.finditer(r'^\s*BO_\s+(\d+)\s+\S+\s*:\s*(\d+)\s+\S+\s*$', text, flags=re.M):
            try:
                mid = int(m.group(1))
            except Exception:
                continue
            # If it's already flagged, leave it.
            if mid & 0x80000000:
                continue
            # Mark as extended if it exceeds 11-bit standard range.
            if mid > 0x7FF:
                mapping[mid] = (mid | 0x80000000)

        if not mapping:
            return text

        def _sub(pat: str, s: str) -> str:
            def repl(mm):
                prefix, id_s, suffix = mm.group(1), mm.group(2), mm.group(3)
                try:
                    val = int(id_s)
                except Exception:
                    return mm.group(0)
                new_val = mapping.get(val)
                if new_val is None:
                    return mm.group(0)
                return f"{prefix}{new_val}{suffix}"
            return re.sub(pat, repl, s, flags=re.M)

        # Rewrite common message-id reference sites.
        text = _sub(r'^(\s*BO_\s+)(\d+)(\s+)', text)
        text = _sub(r'^(\s*CM_\s+BO_\s+)(\d+)(\s+)', text)
        text = _sub(r'^(\s*CM_\s+SG_\s+)(\d+)(\s+)', text)
        text = _sub(r'^(\s*VAL_\s+)(\d+)(\s+)', text)
        text = _sub(r'^(\s*BO_TX_BU_\s+)(\d+)(\s*:\s*)', text)
        text = _sub(r'^(\s*SIG_GROUP_\s+)(\d+)(\s+)', text)
        text = _sub(r'^(\s*SIG_VALTYPE_\s+)(\d+)(\s+)', text)

        return text
    except Exception:
        return text


def _load_arxml_deduped(filepath: str):
    """Load an ARXML file after removing duplicate AUTOSAR-path elements.

    Some OEM ARXML files (e.g. MLBevo) contain duplicate elements that resolve
    to the same AUTOSAR path (built from nested SHORT-NAME values).  cantools
    rejects these.  This helper walks the tree, builds full paths, and removes
    duplicate elements so cantools can proceed.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(filepath)
    root = tree.getroot()

    # Detect the AUTOSAR namespace from the root tag.
    ns = ''
    m = re.match(r'\{(.*?)\}', root.tag)
    if m:
        ns = m.group(1)

    sn_tag = f'{{{ns}}}SHORT-NAME' if ns else 'SHORT-NAME'

    # First pass: collect (parent, child) pairs whose full AUTOSAR path is a
    # duplicate.  We walk depth-first, building the path from nested SHORT-NAMEs.
    seen_paths: set[str] = set()
    to_remove: list[tuple] = []   # (parent, child)

    def _walk(el, path):
        sn_el = el.find(sn_tag)
        cur_path = path
        if sn_el is not None and (sn_el.text or '').strip():
            cur_path = path + '/' + sn_el.text.strip()
            if cur_path in seen_paths:
                return True   # signal caller to remove this element
            seen_paths.add(cur_path)

        for child in list(el):
            is_dup = _walk(child, cur_path)
            if is_dup:
                to_remove.append((el, child))
        return False

    _walk(root, '')

    if not to_remove:
        # No duplicates found — re-raise original error.
        return cantools.database.load_file(filepath, strict=False)

    for parent, child in to_remove:
        try:
            parent.remove(child)
        except ValueError:
            pass

    # Write sanitized XML to a temp file and load with cantools.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile('wb', suffix='.arxml', delete=False) as tf:
            tree.write(tf, xml_declaration=True, encoding='utf-8')
            tmp_path = tf.name
        return cantools.database.load_file(tmp_path, strict=False)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def load_dbc_database(filepath: str):
    """Load a DBC (or ARXML/KCD) into a cantools database with best-effort compatibility."""
    ext = os.path.splitext(filepath)[1].lower()

    # Non-DBC formats: cantools handles them natively via extension-based auto-detection.
    # Skip the DBC sanitize fallback — it would misinterpret XML/binary content as DBC.
    if ext in ('.arxml', '.kcd', '.cdd', '.sym'):
        try:
            return cantools.database.load_file(filepath, strict=False)
        except Exception as exc:
            if ext == '.arxml':
                exc_str = str(exc).lower()
                if 'multiple elements' in exc_str:
                    # Some ARXML files contain duplicate SHORT-NAME paths (e.g. TP
                    # config nodes).  Strip duplicates and retry.
                    return _load_arxml_deduped(filepath)
                if 'non-unique' in exc_str:
                    # OEM ARXML files may violate cantools' uniqueness constraints
                    # (e.g. duplicate PDU-TO-FRAME-MAPPING).  cantools cannot
                    # parse these; callers should use arxml_parser instead.
                    raise ValueError(
                        f'ARXML file is incompatible with cantools ({exc}). '
                        f'Use arxml_parser.parse_arxml() instead.'
                    ) from exc
            raise

    # First try: strict=False helps with many OEM/Vector variations.
    try:
        return cantools.database.load_file(filepath, strict=False)
    except Exception:
        pass

    # Fallback: sanitize content and load a temporary copy.
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception:
        # Re-raise original by trying once more to keep a meaningful stack.
        return cantools.database.load_file(filepath, strict=False)

    sanitized = _sanitize_dbc_text(text)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.sanitized.dbc', delete=False, encoding='utf-8') as tf:
            tf.write(sanitized)
            tmp_path = tf.name

        return cantools.database.load_file(tmp_path, strict=False)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

class DBCLoader:
    def __init__(self):
        self.db = None
        self.filename = None

    def load(self, filepath):
        try:
            self.db = load_dbc_database(filepath)
            self.filename = os.path.basename(filepath)
            print(f"Loaded DBC: {self.filename}")
            return True
        except Exception as e:
            print(f"Error loading DBC: {e}")
            return False

    def decode(self, frame_id, data):
        if not self.db:
            return None
        try:
            message = self.db.get_message_by_frame_id(frame_id)
            decoded = message.decode(bytes(data))
            return {
                "name": message.name,
                "signals": _normalize_decoded_signals(decoded)
            }
        except KeyError:
            return None # Unknown ID
        except Exception as e:
            # print(f"Decode error: {e}")
            return None
