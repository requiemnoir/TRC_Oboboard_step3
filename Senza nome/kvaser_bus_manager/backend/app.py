from flask import Flask, render_template, request, jsonify, send_from_directory, send_file, Response
from flask_socketio import SocketIO
import threading
import time
import os
import io
import re
import subprocess
import zipfile
import json
import socket as pysocket
import struct
import uuid
import sys
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
try:
    import canlib.canlib as canlib
except (ImportError, OSError, SystemExit):
    class MockCanLib:
        canBITRATE_500K = -2
    canlib = MockCanLib()

from bus_manager import BusManager
from ethernet_manager import EthernetManager
from logger import BusLogger
from vag_scanner import VAGScannerService

from shared_frame_buffer import SharedFrameBuffer
from camera_manager import CameraManager
from video_streamer import MJPEGStreamer
from video_recorder import VideoRecorder
from custom_object_matcher import CustomObjectMatcher
from config_store import ConfigStore
from pdx_parser import analyze_pdx, PdxAnalyzeOptions, build_dtc_index_from_pdx, build_did_index_from_pdx, extract_gateway_mirror_definition_from_pdx

# Monitoring / Comparison engine (MVP)
from data_source_manager import DataSourceManager
from violation_logger import ViolationLogger
from dbc_catalog_db import DbcCatalogDb
from comparison_engine import ComparisonEngine

# AI / Anomaly + Rule suggestion
from anomaly_logger import AnomalyLogger
from anomaly_engine import AnomalyEngine
from rule_suggester import suggest_rules_from_session_csv

# MF4 offline replay -> inject into CAN pipeline
from mf4_replay import MF4ReplayService

# Local LLM Copilot (Ollama / OpenAI-compatible)
from copilot_agent import CopilotAgent

# Experimental assistant (MIL sentinel)
from experimental_assistant import ExperimentalAssistantService

# XCP on CAN — Master implementation (ASAM XCP Part 2 + Part 5)
from xcp_can_client import XcpCanClient, default_xcp_can_config, normalize_xcp_can_config, DEFAULT_GEARBOX_SIGNALS
# XCP file parsers: A2L, LAB, MAP, SYM, SKB (Seed & Key Binary)
from a2l_xcp_parser import (
    parse_a2l, parse_lab, parse_glc, parse_map_file, parse_sym_file, parse_skb_file,
    filter_measurements_by_lab, build_daq_lists_from_selection,
    resolve_lab_events,
    A2lParseResult, A2lXcpConfig, SkbParseResult,
)


_session_meta_lock = threading.Lock()
_display_status_lock = threading.Lock()
_display_last_saved_file = None


_copilot_rule_drafts_lock = threading.Lock()
_copilot_rule_drafts: Dict[str, Dict[str, Any]] = {}


def _copilot_prune_rule_drafts(*, max_age_s: float = 15 * 60.0) -> None:
    try:
        now_s = float(time.time())
    except Exception:
        now_s = 0.0
    with _copilot_rule_drafts_lock:
        dead = []
        for k, v in list(_copilot_rule_drafts.items()):
            try:
                created_s = float((v or {}).get('created_s') or 0.0)
            except Exception:
                created_s = 0.0
            if not created_s or (now_s - created_s) > float(max_age_s or 0.0):
                dead.append(k)
        for k in dead:
            _copilot_rule_drafts.pop(k, None)


def _copilot_parse_simple_op(m: str) -> str | None:
    s = str(m or '').lower()
    # Explicit operators first.
    if '>=' in s or '≥' in s:
        return 'ge'
    if '<=' in s or '≤' in s:
        return 'le'
    if '!=' in s or '≠' in s:
        return 'ne'
    # '=' is ambiguous with other uses, keep it late.
    if '==' in s:
        return 'eq'
    if '>' in s:
        return 'gt'
    if '<' in s:
        return 'lt'

    # Italian phrases.
    if any(k in s for k in ['almeno', 'minimo', 'non meno di', '>=', 'maggiore o uguale', 'uguale o maggiore']):
        return 'ge'
    if any(k in s for k in ['massimo', 'non più di', 'non piu di', 'minore o uguale', 'uguale o minore']):
        return 'le'
    if any(k in s for k in ['oltre', 'sopra', 'maggiore di', 'più di', 'piu di', 'supera', 'superiore a']):
        return 'gt'
    if any(k in s for k in ['sotto', 'inferiore a', 'minore di', 'meno di']):
        return 'lt'
    if any(k in s for k in ['diverso', 'differente', 'non uguale']):
        return 'ne'
    if any(k in s for k in ['uguale', 'pari a', '==', '=']):
        return 'eq'
    return None


def _copilot_parse_first_number(m: str) -> float | None:
    s = str(m or '')
    # Accept 8000, 8000.0, 8,000, 8.000
    mm = re.search(r"(-?\d+(?:[\.,]\d+)?)", s)
    if not mm:
        return None
    raw = mm.group(1)
    try:
        # Heuristic: if there's a comma and no dot -> decimal comma.
        if ',' in raw and '.' not in raw:
            raw = raw.replace(',', '.')
        # If both separators appear, assume thousands separators and strip commas.
        raw = raw.replace(',', '')
        return float(raw)
    except Exception:
        return None


def _copilot_rule_metric(m: str) -> str | None:
    s = str(m or '').lower()
    if any(k in s for k in ['rpm', 'giri', 'drehzahl', 'rev/min', 'r/min']):
        return 'rpm'
    if any(k in s for k in ['veloc', 'km/h', 'kph', 'speed', 'geschwind']):
        return 'speed'
    if any(k in s for k in ['marcia', 'gear', 'gang', 'fahrstufe']):
        return 'gear'
    if any(k in s for k in ['coppia', 'torque', 'drehmoment', 'moment']):
        return 'torque'
    return None


def _copilot_split_composite_clauses(user_msg: str) -> tuple[list[str], str] | tuple[None, None]:
    """Split a message into clauses joined by AND/OR tokens.

    Returns (clauses, mode) where mode in {'and','or'}.
    If no conjunction is found, returns ([msg], 'and').
    If both AND and OR are mixed, returns (None, None).
    """
    m = str(user_msg or '').strip()
    if not m:
        return ([], 'and')

    ml = ' ' + re.sub(r"\s+", " ", m.lower()) + ' '

    # Split while keeping conjunction tokens.
    toks = re.split(r"\b(e|and|oppure|or)\b", ml)
    if not toks or len(toks) == 1:
        return ([m], 'and')

    parts: list[str] = []
    conjs: list[str] = []
    for i, t in enumerate(toks):
        t = str(t or '').strip()
        if not t:
            continue
        if i % 2 == 1:
            conjs.append(t)
        else:
            parts.append(t)

    if not conjs:
        return ([m], 'and')

    has_and = any(c in {'e', 'and'} for c in conjs)
    has_or = any(c in {'oppure', 'or'} for c in conjs)
    if has_and and has_or:
        return (None, None)

    mode = 'or' if has_or else 'and'
    # Map back to original text-ish clauses: use the split lowercased clauses; good enough for parsing.
    clauses = [p.strip() for p in parts if p.strip()]
    return (clauses, mode)


def _copilot_parse_clause(clause: str) -> Dict[str, Any] | None:
    """Parse a single clause into {metric, op, value}."""
    c = str(clause or '').strip()
    if not c:
        return None
    metric = _copilot_rule_metric(c)
    if metric is None:
        return None
    op = _copilot_parse_simple_op(c)
    val = _copilot_parse_first_number(c)

    # Allow implicit equality for gear-like requests (e.g., "marcia 6").
    if op is None and metric == 'gear' and val is not None:
        op = 'eq'

    if op is None:
        # Default is gt for numeric metrics, eq for gear.
        op = 'eq' if metric == 'gear' else 'gt'

    if val is None:
        return None

    return {'metric': metric, 'op': op, 'value': float(val)}


def _copilot_pick_signal_for_metric(dbc_search: Dict[str, Any] | None, metric: str) -> Dict[str, Any] | None:
    if not isinstance(dbc_search, dict):
        return None
    results = dbc_search.get('results')
    if not isinstance(results, list) or not results:
        return None

    want = str(metric or '').lower()
    best = None
    best_score = -10**9

    for block in results:
        if not isinstance(block, dict):
            continue
        dbc_name = str(block.get('dbc_name') or '').strip()
        items = block.get('items') if isinstance(block.get('items'), list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            sig = str(it.get('signal') or '').strip()
            msg = str(it.get('message') or '').strip()
            comm = str(it.get('signal_comment') or '').strip().lower()
            ls = sig.lower()
            score = 0

            if want == 'rpm':
                if ls == 'mo_drehzahl_01':
                    score += 200
                if 'drehzahl' in ls or 'rpm' in ls or 'giri' in comm:
                    score += 60
                if ls.startswith('mo_'):
                    score += 10

            elif want == 'speed':
                if ls == 'esp_v_signal' or 'v_signal' in ls:
                    score += 150
                if 'geschwind' in ls or 'veloc' in comm:
                    score += 30
                if 'radgeschw' in ls:
                    score += 10
                if any(x in ls for x in ['qualifier', 'qualit', 'qbit', 'valid', 'status']):
                    score -= 60
                if any(x in comm for x in ['qualifier', 'quality', 'valid', 'status']):
                    score -= 20

            elif want == 'gear':
                if 'gangposition' in ls:
                    score += 170
                if 'fahrstufe' in comm or 'wahlhebel' in comm or 'getriebe' in comm or 'marcia' in comm:
                    score += 25
                if 'qbit' in ls or 'qualit' in comm:
                    score -= 10

            elif want == 'torque':
                if ls == 'mo_istmoment_vkm':
                    score += 200
                if 'ist-moment des verbrennungsmotors' in comm:
                    score += 80
                if 'moment' in ls or 'torque' in comm or 'coppia' in comm:
                    score += 30
                if any(x in ls for x in ['faktor', 'begr', 'max', 'min']):
                    score -= 10
                if any(x in ls for x in ['eps_', 'lenkmoment']):
                    score -= 30

            # small tie-breakers
            if msg:
                score += 1
            if sig:
                score += 1

            if score > best_score and msg and sig:
                best_score = score
                best = {
                    'dbc_name': dbc_name,
                    'message': msg,
                    'signal': sig,
                    'frame_id': it.get('frame_id'),
                    'signal_comment': str(it.get('signal_comment') or '').strip(),
                }

    return best


def _copilot_metric_terms(metric: str) -> str:
    m = str(metric or '').strip().lower()
    if m == 'rpm':
        return 'drehzahl rpm motore giri'
    if m == 'speed':
        return 'v_signal speed velocità geschwindigkeit'
    if m == 'gear':
        return 'gangposition gear marcia fahrstufe getriebe'
    if m == 'torque':
        return 'istmoment moment torque coppia drehmoment'
    return m


def _copilot_build_metric_dbc_search(snapshot: Dict[str, Any] | None, metric: str) -> Dict[str, Any]:
    """Small deterministic DBC lookup aimed at selecting a good signal for a known metric."""
    terms = _copilot_metric_terms(metric)
    dbc_names: list[str] = []
    try:
        src = snapshot.get('sources') if isinstance(snapshot, dict) and isinstance(snapshot.get('sources'), dict) else {}
        items = src.get('items') if isinstance(src, dict) and isinstance(src.get('items'), list) else []
        for s in items:
            if not isinstance(s, dict):
                continue
            name = str(s.get('dbc_name') or '').strip()
            if not name:
                continue
            if os.path.basename(name) != name:
                continue
            if name not in dbc_names:
                dbc_names.append(name)
    except Exception:
        dbc_names = []

    # fallback to a few DBCs on disk
    if not dbc_names:
        try:
            for name in os.listdir(UPLOAD_FOLDER_DBC):
                if isinstance(name, str) and name.lower().endswith('.dbc') and os.path.basename(name) == name:
                    dbc_names.append(name)
        except Exception:
            pass
        dbc_names = dbc_names[:3]

    results = []
    for dbc_name in dbc_names[:3]:
        try:
            r = dbc_catalog_db.search_signals(query=terms, dbc_name=dbc_name, limit=20)
            if not isinstance(r, dict) or not r.get('ok'):
                continue
            items = r.get('items') if isinstance(r.get('items'), list) else []
            if items:
                results.append({'dbc_name': dbc_name, 'items': items})
        except Exception:
            continue

    return {
        'enabled': True,
        'terms': terms,
        'dbcs_checked': dbc_names[:3],
        'results': results,
    }


def _copilot_pick_source_id(snapshot: Dict[str, Any] | None, *, dbc_name: str | None = None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    src = snapshot.get('sources') if isinstance(snapshot.get('sources'), dict) else {}
    items = src.get('items') if isinstance(src.get('items'), list) else []
    sources = []
    for s in items:
        if not isinstance(s, dict):
            continue
        sid = str(s.get('id') or '').strip()
        if not sid:
            continue
        sources.append(s)
    if not sources:
        return None
    if len(sources) == 1:
        return str(sources[0].get('id') or '').strip() or None

    if dbc_name:
        dn = str(dbc_name).strip()
        for s in sources:
            if str(s.get('dbc_name') or '').strip() == dn:
                return str(s.get('id') or '').strip() or None

    # fallback: prefer enabled sources
    for s in sources:
        if bool(s.get('enabled', True)):
            return str(s.get('id') or '').strip() or None
    return str(sources[0].get('id') or '').strip() or None


def _copilot_is_rule_wizard_question(user_msg: str) -> bool:
    m = str(user_msg or '').lower()
    if not m:
        return False
    if any(k in m for k in ['crea una regola', 'creami una regola', 'aggiungi una regola', 'imposta una regola', 'crea regola', 'create a rule']):
        return True
    # Also accept conditional creation phrasing like
    # “regola che attivi una violation se ...”, but avoid audit questions such
    # as “quale regola risulta attiva nello snapshot?”.
    has_rule_create_intent = any(k in m for k in ['regola che', 'rule that'])
    has_trigger_intent = any(k in m for k in ['violation', 'violazioni', 'scatta', 'attivi', 'attiva'])
    if 'regola' in m and has_rule_create_intent and has_trigger_intent and ' se ' in f' {m} ':
        return True
    return False


def _copilot_build_rule_examples_answer() -> str:
    return "\n".join([
        "Regole (velocità / RPM / marcia / coppia) — esempi concreti (deterministico):",
        "",
        "Dove si fanno: pagina `/comparison` → `Add rule`.",
        "Una regola confronta `A` (un segnale) contro `B` (un altro segnale o una costante). Se è vera, genera una riga in `/violations`.",
        "",
        "Operatori supportati:",
        "- `gt` (A > B + threshold)",
        "- `ge` (A ≥ B + threshold)",
        "- `lt` (A < B - threshold)",
        "- `le` (A ≤ B - threshold)",
        "- `eq` (|A-B| ≤ threshold)",
        "- `ne` (|A-B| > threshold)",
        "- `delta_abs` (|A-B| > threshold)",
        "- `delta_pct` (|A-B|/|B| > threshold %)",
        "- `missing` (segnale mancante da `missing_timeout_s`)",
        "",
        "Soglie (`threshold`) e debounce (`debounce_s`) — come sceglierli:",
        "- `threshold` è una *tolleranza/hysteresis*: per `gt/ge/lt/le` sposta il punto di trigger; per `eq/ne/delta_*` è la tolleranza sul confronto.",
        "- `debounce_s` limita la frequenza eventi: anche se la condizione resta vera, non spammi violazioni ogni frame.",
        "",
        "Esempi tipici:",
        "1) RPM alti (motore oltre 8000 rpm)",
        "- A = (Source) `MO_Drehzahl_01` (RPM)",
        "- op = `gt`, B const = 8000", 
        "- threshold consigliato: 0..50 (es. 50 → scatta sopra 8050)",
        "- debounce consigliato: 1..3s (es. 2s)",
        "",
        "2) Velocità oltre 130 km/h", 
        "- A = `ESP_v_Signal`", 
        "- op = `gt`, B const = 130", 
        "- threshold consigliato: 0..2 (es. 1)",
        "- debounce consigliato: 2..5s (es. 3s)",
        "",
        "3) Marcia specifica (es. marcia = 6)",
        "- A = `MO_Gangposition`", 
        "- op = `eq`, B const = 6", 
        "- threshold consigliato: 0..0.2 (se il segnale non è intero)",
        "- debounce consigliato: 0.5..2s", 
        "",
        "4) Coppia motore (es. coppia reale > 300 Nm)",
        "- A = `MO_IstMoment_VKM` (se disponibile nel tuo DBC)",
        "- op = `gt`, B const = 300",
        "- threshold consigliato: 0..10 Nm",
        "- debounce consigliato: 1..3s",
        "",
        "Suggerimento pratico: se un segnale è rumoroso, usa *sia* `threshold` (piccola hysteresis) *sia* `debounce_s` (anti-spam).",
        "",
        "Se vuoi, posso anche CREARTI la regola via chat con conferma, ad es.:",
        "- `crea una regola che attivi una violation se i giri motore sono oltre 8000`",
    ]).strip()


def _copilot_try_handle_rule_wizard(msg: str, snapshot: Dict[str, Any], chat_ctx: Dict[str, Any]) -> str | None:
    """Guided deterministic rule creation: draft -> optional source selection -> confirm -> save."""
    m = str(msg or '').strip()
    ml = m.lower()
    if not m:
        return None

    _copilot_prune_rule_drafts()

    # Commands: confirm/cancel, set source.
    mm_confirm = re.match(r"^\s*(conferma|confirm)\s+([a-z0-9_\-]{6,})\s*$", ml)
    mm_cancel = re.match(r"^\s*(annulla|cancella|cancel)\s+([a-z0-9_\-]{6,})\s*$", ml)
    mm_source = re.match(r"^\s*(source|sorgente|usa\s+source|usa\s+sorgente)\s+([a-z0-9_\-]{6,})\s+([a-z0-9_\-\.]+)\s*$", ml)

    if mm_cancel:
        did = mm_cancel.group(2)
        with _copilot_rule_drafts_lock:
            existed = did in _copilot_rule_drafts
            _copilot_rule_drafts.pop(did, None)
        if existed:
            return f"Ok, bozza `{did}` annullata."
        return f"Non trovo la bozza `{did}` (forse è scaduta)."

    if mm_source:
        did = mm_source.group(2)
        source_id = mm_source.group(3)
        with _copilot_rule_drafts_lock:
            d = _copilot_rule_drafts.get(did)
            if not isinstance(d, dict):
                d = None
            if d:
                rule = d.get('rule') if isinstance(d.get('rule'), dict) else {}
                a = rule.get('a') if isinstance(rule.get('a'), dict) else {}
                a['source_id'] = source_id
                rule['a'] = a
                d['rule'] = rule
                d['needs_source'] = False
                _copilot_rule_drafts[did] = d
        if not d:
            return f"Non trovo la bozza `{did}` (forse è scaduta)."
        # Validate source exists
        try:
            src_items = (snapshot.get('sources') or {}).get('items') if isinstance(snapshot.get('sources'), dict) else []
        except Exception:
            src_items = []
        if isinstance(src_items, list) and not any(isinstance(s, dict) and str(s.get('id') or '') == source_id for s in src_items):
            return f"Ho aggiornato la bozza `{did}`, ma la sorgente `{source_id}` non risulta tra quelle configurate. Controlla `/sources` e riprova."
        return "\n".join([
            f"Ok: bozza `{did}` aggiornata con source `{source_id}`.",
            f"Se vuoi salvarla davvero, rispondi: `conferma {did}`",
        ]).strip()

    if mm_confirm:
        did = mm_confirm.group(2)
        with _copilot_rule_drafts_lock:
            d = _copilot_rule_drafts.get(did)
        if not isinstance(d, dict):
            return f"Non trovo la bozza `{did}` (forse è scaduta)."
        rule = d.get('rule') if isinstance(d.get('rule'), dict) else None
        if not isinstance(rule, dict):
            return f"Bozza `{did}` non valida. Rifai la richiesta di creazione regola."
        a = rule.get('a') if isinstance(rule.get('a'), dict) else {}
        if not str(a.get('source_id') or '').strip():
            # Need source selection
            try:
                src_items = (snapshot.get('sources') or {}).get('items') if isinstance(snapshot.get('sources'), dict) else []
            except Exception:
                src_items = []
            options = []
            if isinstance(src_items, list):
                for s in src_items:
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get('id') or '').strip()
                    if not sid:
                        continue
                    nm = str(s.get('name') or sid).strip()
                    dbc = str(s.get('dbc_name') or '').strip()
                    options.append(f"- `{sid}` — {nm}{(' (DBC ' + dbc + ')') if dbc else ''}")
            return "\n".join([
                f"Prima di salvare la bozza `{did}` devo sapere quale sorgente usare per il segnale A.",
                "Scegline una e rispondi così:",
                f"- `source {did} <source_id>`",
                "",
                "Sorgenti disponibili:",
                *(options[:10] if options else ["- (nessuna sorgente configurata)"]),
            ]).strip()

        try:
            r = comparison_engine.upsert_rule(rule)
            # Keep engine hot (upsert already saves) but reload is cheap and makes sure state is consistent.
            try:
                comparison_engine.reload()
            except Exception:
                pass
            with _copilot_rule_drafts_lock:
                _copilot_rule_drafts.pop(did, None)
            return "\n".join([
                f"Fatto: regola salvata (id `{r.id}`).",
                "- La trovi in `/comparison`.",
                "- Quando scatta, la vedi in `/violations`.",
            ]).strip()
        except Exception as e:
            return f"Non riesco a salvare la regola `{did}`: {str(e)}"

    # Create draft
    if not _copilot_is_rule_wizard_question(m):
        return None

    clauses, mode = _copilot_split_composite_clauses(m)
    if clauses is None or mode is None:
        return "Ho capito che vuoi una regola composta, ma hai mischiato `e/and` con `oppure/or`. Riscrivi usando solo AND oppure solo OR. Esempio: `crea una regola velocità > 130 e marcia = 6`."

    parsed: list[Dict[str, Any]] = []
    for c in (clauses or []):
        pc = _copilot_parse_clause(c)
        if pc:
            parsed.append(pc)

    if not parsed:
        return "Per creare una regola via chat dimmi su cosa: `rpm/giri`, `velocità`, `marcia`, oppure `coppia` (con una soglia). Esempio: `crea una regola rpm > 8000`."

    # If the user wrote multiple clauses but one is missing number/op, ask to clarify.
    if len(clauses or []) >= 2 and len(parsed) < len(clauses or []):
        return "Ho capito che vuoi una regola composta (AND/OR), ma almeno una parte non ha una soglia numerica. Esempio valido: `crea una regola velocità > 130 e marcia = 6`."

    def pick_signal(metric: str) -> Dict[str, Any] | None:
        dbc_search = None
        try:
            dbc_search = (chat_ctx or {}).get('dbc_search') if isinstance(chat_ctx, dict) else None
        except Exception:
            dbc_search = None
        p = _copilot_pick_signal_for_metric(dbc_search if isinstance(dbc_search, dict) else {}, metric)
        # Metric-focused fallback
        try:
            ls0 = str((p or {}).get('signal') or '').lower()
            comm0 = str((p or {}).get('signal_comment') or '').lower()
        except Exception:
            ls0 = ''
            comm0 = ''
        suspicious = False
        if p:
            if metric == 'rpm' and not any(k in (ls0 + ' ' + comm0) for k in ['drehzahl', 'rpm', 'giri']):
                suspicious = True
            if metric == 'speed' and not any(k in (ls0 + ' ' + comm0) for k in ['v_signal', 'geschwind', 'speed', 'veloc']):
                suspicious = True
            if metric == 'gear' and not any(k in (ls0 + ' ' + comm0) for k in ['gang', 'gear', 'marcia', 'fahrstufe']):
                suspicious = True
            if metric == 'torque' and not any(k in (ls0 + ' ' + comm0) for k in ['moment', 'torque', 'coppia', 'drehmoment']):
                suspicious = True
        if suspicious or not p:
            try:
                metric_search = _copilot_build_metric_dbc_search(snapshot, metric)
                p2 = _copilot_pick_signal_for_metric(metric_search, metric)
                if p2:
                    p = p2
            except Exception:
                pass
        return p

    # Map each clause to a message/signal.
    enriched: list[Dict[str, Any]] = []
    for pc in parsed:
        metric = str(pc.get('metric') or '').strip().lower()
        p = pick_signal(metric)
        if not p:
            metric_name = {'rpm': 'RPM', 'speed': 'velocità', 'gear': 'marcia', 'torque': 'coppia'}.get(metric, metric)
            return "\n".join([
                f"Posso creare la regola, ma non riesco a trovare il segnale per `{metric_name}` nel catalogo DBC.",
                "- Verifica DBC associato in `/sources`.",
                "- Oppure dimmi `Message.Signal` esatto e lo imposto.",
            ]).strip()
        pc2 = dict(pc)
        pc2['pick'] = p
        enriched.append(pc2)

    # Primary clause becomes the main rule; others become conditions.
    primary = enriched[0]
    metric = str(primary.get('metric') or '').strip().lower()
    op = str(primary.get('op') or 'gt').strip().lower()
    val = float(primary.get('value') or 0.0)
    pick = primary.get('pick') if isinstance(primary.get('pick'), dict) else {}

    # Recommend threshold + debounce defaults per metric.
    thr = 0.0
    debounce = 2.0
    if metric == 'rpm':
        thr = 50.0
        debounce = 2.0
    elif metric == 'speed':
        thr = 1.0
        debounce = 3.0
    elif metric == 'gear':
        thr = 0.0
        debounce = 1.0
    elif metric == 'torque':
        thr = 5.0
        debounce = 2.0

    severity = 'warning'
    if any(k in ml for k in ['critica', 'critical', 'critico']):
        severity = 'critical'
    if any(k in ml for k in ['info', 'informativa']):
        severity = 'info'

    source_id = _copilot_pick_source_id(snapshot, dbc_name=str(pick.get('dbc_name') or '').strip() or None)
    # If multiple sources exist and we can't confidently pick, ask the user.
    try:
        src_items = (snapshot.get('sources') or {}).get('items') if isinstance(snapshot.get('sources'), dict) else []
        multi_sources = isinstance(src_items, list) and len([s for s in src_items if isinstance(s, dict) and str(s.get('id') or '').strip()]) > 1
    except Exception:
        multi_sources = False
    needs_source = False
    if multi_sources and not str(source_id or '').strip():
        needs_source = True
        source_id = ''

    # Build rule draft.
    draft_id = f"draft_{uuid.uuid4().hex[:10]}"
    op_sym = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<=', 'eq': '==', 'ne': '!='}.get(op, op)
    metric_name = {'rpm': 'RPM', 'speed': 'Velocità', 'gear': 'Marcia', 'torque': 'Coppia'}.get(metric, metric)

    # Conditions from remaining clauses.
    conditions: list[dict] = []
    for extra in enriched[1:]:
        emetric = str(extra.get('metric') or '').strip().lower()
        eop = str(extra.get('op') or 'gt').strip().lower()
        eval0 = float(extra.get('value') or 0.0)
        epick = extra.get('pick') if isinstance(extra.get('pick'), dict) else {}

        # Per-metric threshold for condition.
        eth = 0.0
        if emetric == 'rpm':
            eth = 50.0
        elif emetric == 'speed':
            eth = 1.0
        elif emetric == 'gear':
            eth = 0.0
        elif emetric == 'torque':
            eth = 5.0

        esource = _copilot_pick_source_id(snapshot, dbc_name=str(epick.get('dbc_name') or '').strip() or None)
        if needs_source:
            esource = ''
        conditions.append({
            'a': {
                'source_id': str(esource or '').strip(),
                'message': str(epick.get('message') or '').strip(),
                'signal': str(epick.get('signal') or '').strip(),
            },
            'op': eop,
            'b_kind': 'const',
            'b_const': float(eval0),
            'threshold': float(eth),
            'missing_timeout_s': 0.5,
        })

    rule_obj = {
        'id': f"copilot_{uuid.uuid4().hex[:12]}",
        'name': f"{metric_name} {op_sym} {val}" + ((" " + mode.upper() + " ...") if conditions else ''),
        'enabled': True,
        'severity': severity,
        'a': {
            'source_id': str(source_id or '').strip(),
            'message': str(pick.get('message') or '').strip(),
            'signal': str(pick.get('signal') or '').strip(),
        },
        'op': op,
        'b_kind': 'const',
        'b_const': float(val),
        'threshold': float(thr),
        'debounce_s': float(debounce),
        'missing_timeout_s': 0.5,
        'conditions_mode': str(mode or 'and') if str(mode or 'and') in {'and', 'or'} else 'and',
        'conditions': conditions,
        'actions': [{'kind': 'log_csv'}, {'kind': 'emit_ws'}],
    }

    with _copilot_rule_drafts_lock:
        _copilot_rule_drafts[draft_id] = {
            'created_s': float(time.time()),
            'rule': rule_obj,
            'needs_source': bool(needs_source or not str(rule_obj['a'].get('source_id') or '').strip()),
        }

    # Build response with confirmations.
    lines = [
        "Ho preparato una BOZZA di regola (non è ancora salvata):",
        f"- Bozza id: `{draft_id}`",
        f"- Nome: {rule_obj['name']} (severity={severity})",
        f"- A: {rule_obj['a']['message']}.{rule_obj['a']['signal']}" + (f" (source={rule_obj['a']['source_id']})" if str(rule_obj['a'].get('source_id') or '').strip() else " (source da scegliere)"),
        f"- Condizione: op={op} vs const {val} (threshold={thr})",
        f"- Debounce: {debounce}s",
    ]

    if conditions:
        lines.append(f"- Conditions mode: {rule_obj['conditions_mode']} ({len(conditions)} conditions)")
        lines.append("- Conditions:")
        for c in conditions[:8]:
            a0 = c.get('a') if isinstance(c.get('a'), dict) else {}
            lines.append(
                f"  - {a0.get('message')}.{a0.get('signal')} (source={a0.get('source_id')}) {c.get('op')} const {c.get('b_const')} (thr {c.get('threshold')})"
            )
        if len(conditions) > 8:
            lines.append(f"  - ... (+{len(conditions) - 8} altre)")

    lines.extend(["", "Conferme:"])

    if not str(rule_obj['a'].get('source_id') or '').strip():
        # List sources
        options = []
        try:
            src_items = (snapshot.get('sources') or {}).get('items') if isinstance(snapshot.get('sources'), dict) else []
        except Exception:
            src_items = []
        if isinstance(src_items, list):
            for s in src_items:
                if not isinstance(s, dict):
                    continue
                sid = str(s.get('id') or '').strip()
                if not sid:
                    continue
                nm = str(s.get('name') or sid).strip()
                dbc = str(s.get('dbc_name') or '').strip()
                options.append(f"- `{sid}` — {nm}{(' (DBC ' + dbc + ')') if dbc else ''}")
        lines.extend([
            "1) Scegli la sorgente da usare:",
            f"- `source {draft_id} <source_id>`",
            "Sorgenti disponibili:",
            *(options[:10] if options else ["- (nessuna sorgente configurata)"]),
            "",
            "2) Poi salva davvero:",
            f"- `conferma {draft_id}`",
            "",
            f"Per annullare: `annulla {draft_id}`",
        ])
        return "\n".join(lines).strip()

    lines.extend([
        f"- Per salvarla davvero: `conferma {draft_id}`",
        f"- Per annullare: `annulla {draft_id}`",
    ])
    return "\n".join(lines).strip()


def _session_meta_filename(base: str) -> str:
    return f"{base}.meta.json"


def _session_base_from_pathlike(pathlike) -> str | None:
    try:
        b = os.path.basename(str(pathlike or '')).strip()
    except Exception:
        return None
    if not b or not b.startswith('session_'):
        return None
    if '.' in b:
        b = b.split('.', 1)[0]
    return b


def _find_session_meta_path(base: str) -> str | None:
    name = _session_meta_filename(base)
    return _find_log_file(name)


def _read_session_meta(base: str) -> dict:
    path = _find_session_meta_path(base)
    if not path:
        return {'base': base, 'events': []}
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return {'base': base, 'events': []}
        if obj.get('base') != base:
            obj['base'] = base
        if not isinstance(obj.get('events'), list):
            obj['events'] = []
        return obj
    except Exception:
        return {'base': base, 'events': []}


def _write_session_meta(base: str, patch: dict) -> None:
    if not base or not base.startswith('session_'):
        return
    if not isinstance(patch, dict):
        return

    # Write alongside the main session artifacts (prefer primary log folder).
    try:
        meta_name = _session_meta_filename(base)
        base_mf4 = _find_log_file(f"{base}.mf4")
        base_mp4 = _find_log_file(f"{base}.mp4")
        if base_mf4:
            folder = os.path.dirname(str(base_mf4))
        elif base_mp4:
            folder = os.path.dirname(str(base_mp4))
        else:
            folder = str(LOG_FOLDER)
        os.makedirs(folder, exist_ok=True)
        out_path = os.path.join(folder, meta_name)
    except Exception:
        return

    with _session_meta_lock:
        cur = _read_session_meta(base)
        # Merge patch shallowly
        for k, v in (patch or {}).items():
            cur[k] = v
        cur['base'] = base
        if not isinstance(cur.get('events'), list):
            cur['events'] = []

        # Atomic write
        tmp_path = out_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(cur, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


def _append_session_event(base: str, name: str, *, ts_ms: int | None = None, details: dict | None = None) -> None:
    if not base or not base.startswith('session_'):
        return
    try:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
    except Exception:
        ts_ms = 0
    evt = {
        'ts_ms': int(ts_ms or 0),
        'name': str(name or '').strip(),
        'details': details if isinstance(details, dict) else {},
    }
    cur = _read_session_meta(base)
    evs = cur.get('events') if isinstance(cur.get('events'), list) else []
    evs.append(evt)
    _write_session_meta(base, {'events': evs[-2000:]})
from gateway_mirror import build_mirror_mode_write_request, default_mirror_definition

app = Flask(__name__, template_folder="../frontend/templates", static_folder="../frontend/static")
app.config['SECRET_KEY'] = 'kvaser_secret!'
# Ensure UI updates propagate even on kiosk/browsers with sticky cache.
# This is especially important on Raspberry installs where the URL stays the same.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0


@app.after_request
def _no_cache_static_assets(response):
    try:
        path = request.path or ''
        if path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store, max-age=0, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
    except Exception:
        pass
    return response
# NOTE: Python 3.13 removed ssl.wrap_socket; eventlet currently breaks.
# Force threading mode to keep the app runnable regardless of eventlet presence.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Directories (absolute; do not depend on cwd)
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BACKEND_DIR, '..'))
UPLOAD_FOLDER_DBC = os.path.join(PROJECT_DIR, 'databases', 'dbc')
UPLOAD_FOLDER_FIBEX = os.path.join(PROJECT_DIR, 'databases', 'fibex')
UPLOAD_FOLDER_ARXML = os.path.join(PROJECT_DIR, 'databases', 'arxml')
UPLOAD_FOLDER_A2L  = os.path.join(PROJECT_DIR, 'databases', 'a2l')
PROJECTS_DIR = os.path.join(PROJECT_DIR, 'projects')
UPLOAD_FOLDER_PDX = os.path.join(PROJECTS_DIR, 'pdx')
# DEFAULT_LOG_FOLDER = os.path.join(PROJECTS_DIR, 'logs')
DEFAULT_LOG_FOLDER = '/mnt/ssd/logs'
LOG_FOLDER = DEFAULT_LOG_FOLDER
os.makedirs(UPLOAD_FOLDER_DBC, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_FIBEX, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_ARXML, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_A2L,  exist_ok=True)
os.makedirs(UPLOAD_FOLDER_PDX, exist_ok=True)


def _safe_basename(name: str) -> str | None:
    """Return a safe basename, or None if unsafe."""
    n = str(name or '').strip()
    if not n:
        return None
    b = os.path.basename(n)
    if not b or b != n:
        return None
    return b


def _pdx_path_for(filename: str) -> str | None:
    base = _safe_basename(filename)
    if not base:
        return None
    if not base.lower().endswith('.pdx'):
        return None
    return os.path.join(UPLOAD_FOLDER_PDX, base)


def _pdx_report_path_for(filename: str) -> str | None:
    pdx_path = _pdx_path_for(filename)
    if not pdx_path:
        return None
    return pdx_path + '.report.json'


def _get_active_pdx_path() -> str | None:
    try:
        cfg = config_store.get_config_only() or {}
        proj = cfg.get('project') if isinstance(cfg, dict) else None
        if not isinstance(proj, dict) or proj.get('kind') != 'pdx':
            return None
        fn = str(proj.get('filename') or '').strip()
        if not fn:
            return None
        return _pdx_path_for(fn)
    except Exception:
        return None


def _ensure_writable_dir(path: str) -> str:
    """Create dir (if needed) and verify it is writable."""
    path = os.path.abspath(str(path))
    os.makedirs(path, exist_ok=True)
    test_name = f".kvbm_write_test_{os.getpid()}_{int(time.time()*1000)}"
    test_path = os.path.join(path, test_name)
    try:
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(test_path)
    except Exception as e:
        raise PermissionError(f"storage path not writable: {path} ({e})")
    return path


def _resolve_storage_output_dir(cfg=None) -> str:
    """Resolve the configured output dir (supports absolute paths and project-relative paths)."""
    out = ''
    try:
        storage = (cfg or {}).get('storage')
        if isinstance(storage, dict):
            out = str(storage.get('output_dir') or '').strip()
    except Exception:
        out = ''

    if not out:
        return os.path.abspath(DEFAULT_LOG_FOLDER)
    if not os.path.isabs(out):
        out = os.path.join(PROJECT_DIR, out)
    return os.path.abspath(out)

# Legacy location used by some components when paths were relative (e.g., backend/logs).
LEGACY_LOG_FOLDER = os.path.join(BACKEND_DIR, 'logs')


def _iter_log_folders():
    """Return log folders (primary + legacy) in priority order."""
    folders = []
    for p in [LOG_FOLDER, DEFAULT_LOG_FOLDER, LEGACY_LOG_FOLDER]:
        try:
            rp = os.path.realpath(p)
        except Exception:
            rp = p
        if rp and rp not in folders:
            folders.append(rp)
    return folders


def _iter_export_log_folders():
    """Return exports subfolders inside allowed log folders."""
    folders = []
    for folder in _iter_log_folders():
        try:
            rp = os.path.realpath(os.path.join(folder, 'exports'))
        except Exception:
            rp = os.path.join(folder, 'exports')
        if rp and rp not in folders:
            folders.append(rp)
    return folders


def _normalize_log_relative_path(filename: str) -> str | None:
    """Allow safe log basenames and files under the exports subfolder."""
    raw = str(filename or '').strip().replace('\\', '/')
    if not raw:
        return None
    parts = [p for p in raw.split('/') if p not in {'', '.'}]
    if not parts or any(p == '..' for p in parts):
        return None
    if len(parts) == 1:
        return _safe_basename(parts[0])
    if len(parts) == 2 and parts[0] == 'exports':
        base = _safe_basename(parts[1])
        if base:
            return f'exports/{base}'
    return None


def _find_log_file(filename: str):
    """Resolve a log filename to an absolute path inside allowed log folders."""
    rel_name = _normalize_log_relative_path(filename)
    if not rel_name:
        return None
    if rel_name.startswith('exports/'):
        base_name = rel_name.split('/', 1)[1]
        folders = _iter_export_log_folders()
    else:
        base_name = rel_name
        folders = _iter_log_folders()
    for folder in folders:
        try:
            file_path = os.path.join(folder, base_name)
            if os.path.isfile(file_path):
                return file_path
        except Exception:
            continue
    return None


def _list_mf4_files(include_exports: bool = False):
    """Collect MF4 files from allowed log folders for UI consumers."""
    files_by_name = {}
    part_re = re.compile(r'^(?P<base>.+)_part\d{3,}\.mf4$', re.IGNORECASE)

    search_roots = [(folder, 'logs') for folder in _iter_log_folders()]
    if include_exports:
        search_roots.extend((folder, 'exports') for folder in _iter_export_log_folders())

    for priority, (folder, bucket) in enumerate(search_roots):
        try:
            if not os.path.exists(folder):
                continue
            for f in os.listdir(folder):
                if not isinstance(f, str):
                    continue
                if not f.lower().endswith('.mf4'):
                    continue
                low = f.lower()
                if low.endswith('.tmp.mf4') or '.tmp.' in low:
                    continue
                m_part = part_re.match(f)
                if m_part:
                    base_name = f"{m_part.group('base')}.mf4"
                    try:
                        if os.path.isfile(os.path.join(folder, base_name)):
                            continue
                    except Exception:
                        pass
                if low.endswith('.eth.mf4'):
                    continue
                path = os.path.join(folder, f)
                if not os.path.isfile(path):
                    continue

                rel_name = f if bucket == 'logs' else f'exports/{f}'
                label = f if bucket == 'logs' else f'Export · {f}'

                try:
                    st = os.stat(path)
                    prev = files_by_name.get(rel_name)
                    if (
                        prev is None
                        or priority < int(prev.get('_priority', 999999))
                        or (
                            priority == int(prev.get('_priority', 999999))
                            and st.st_mtime > float(prev.get('_mtime', 0.0))
                        )
                    ):
                        files_by_name[rel_name] = {
                            'name': rel_name,
                            'basename': f,
                            'label': label,
                            'bucket': bucket,
                            'size': int(st.st_size),
                            '_mtime': float(st.st_mtime),
                            '_priority': int(priority),
                        }
                except Exception:
                    continue
        except Exception:
            continue

    out = list(files_by_name.values())
    out.sort(key=lambda x: float(x.get('_mtime', 0.0)), reverse=True)
    for item in out:
        item.pop('_mtime', None)
        item.pop('_priority', None)
    return out

CONFIG_PATH = os.path.join(PROJECT_DIR, 'config', 'app_config.json')
config_store = ConfigStore(CONFIG_PATH)


def _bootstrap_config_from_example_if_missing() -> None:
    """Create config/app_config.json from the tracked example if missing.

    This keeps a fresh clone runnable out-of-the-box while still allowing the
    runtime config to stay local/unversioned.
    """
    try:
        if os.path.isfile(CONFIG_PATH):
            return

        example_path = os.path.join(PROJECT_DIR, 'config', 'app_config.example.json')
        if not os.path.isfile(example_path):
            return

        with open(example_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, dict) and isinstance(data.get('config'), dict):
            cfg = data.get('config')
        elif isinstance(data, dict):
            cfg = data
        else:
            return

        config_store.save(cfg)
    except Exception:
        # Best-effort only: app must still start even if config init fails.
        return


_bootstrap_config_from_example_if_missing()
_saved_config = config_store.get_config_only()

# Ensure a coherent, persisted system_mode exists (legacy configs may not have it).
try:
    sm = str(_saved_config.get('system_mode') or '').strip().lower()
except Exception:
    sm = ''
if sm not in {'simulation', 'real'}:
    try:
        es0 = _saved_config.get('eth_settings') if isinstance(_saved_config.get('eth_settings'), dict) else {}
        iface0 = str((es0 or {}).get('interface') or '').strip().lower()
    except Exception:
        iface0 = ''
    inferred = 'simulation' if iface0 == 'lo' else 'real'
    try:
        config_store.update({'system_mode': inferred})
        _saved_config = config_store.get_config_only()
    except Exception:
        pass

# Apply persisted storage directory (if any).
try:
    LOG_FOLDER = _ensure_writable_dir(_resolve_storage_output_dir(_saved_config))
except Exception:
    LOG_FOLDER = os.path.abspath(DEFAULT_LOG_FOLDER)
    os.makedirs(LOG_FOLDER, exist_ok=True)

# Expose to other modules that default to ../logs.
try:
    os.environ['KBSM_LOG_DIR'] = str(LOG_FOLDER)
except Exception:
    pass

# Persisted setting: whether to include MP4 video during logging sessions.
try:
    _video_recording_enabled = bool(_saved_config.get('video_recording_enabled', True))
except Exception:
    _video_recording_enabled = True

# Initialize Shared Logger
shared_logger = BusLogger(log_dir=LOG_FOLDER)

# Apply persisted MF4 settings (if present)
try:
    if 'mf4_include_decoded' in (_saved_config or {}):
        shared_logger.set_mf4_include_decoded(bool(_saved_config.get('mf4_include_decoded')))
except Exception:
    pass

try:
    if 'mf4_include_raw' in (_saved_config or {}):
        shared_logger.set_mf4_include_raw(bool(_saved_config.get('mf4_include_raw')))
        try:
            os.environ['MF4_INCLUDE_RAW'] = '1' if bool(_saved_config.get('mf4_include_raw')) else '0'
        except Exception:
            pass
except Exception:
    pass

# Apply persisted decoded-persistence mode for text/CSV/JSON logs (if present)
try:
    if 'log_decoded_mode' in (_saved_config or {}):
        shared_logger.set_log_decoded_mode(_saved_config.get('log_decoded_mode'))
        try:
            os.environ['LOG_DECODED_MODE'] = str(_saved_config.get('log_decoded_mode'))
        except Exception:
            pass
except Exception:
    pass

try:
    if 'mf4_chunk_size_mb' in (_saved_config or {}):
        shared_logger.set_mf4_chunk_size_mb(_saved_config.get('mf4_chunk_size_mb'))
except Exception:
    pass

try:
    if 'mf4_part_time_limit_min' in (_saved_config or {}):
        shared_logger.set_mf4_part_time_limit_s(float(_saved_config.get('mf4_part_time_limit_min')) * 60.0)
except Exception:
    pass

try:
    if 'mf4_flush_interval_mb' in (_saved_config or {}):
        shared_logger.set_mf4_flush_interval_mb(_saved_config.get('mf4_flush_interval_mb'))
except Exception:
    pass

# Disable MF4 part merging: keep each ~100 MB chunk as a separate file.
os.environ['MF4_INCREMENTAL_MERGE'] = '0'
os.environ['MF4_MERGE_ON_STOP'] = '0'


def _ensure_eth_running_for_logging() -> None:
    """Best-effort: start Ethernet capture so session logs include Ethernet traffic."""
    try:
        already = bool(getattr(eth_manager, 'capture', None) or getattr(eth_manager, 'doip', None) or getattr(eth_manager, 'xcp', None))
    except Exception:
        already = False
    if already:
        return

    cfg = {}
    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}

    es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}

    def _pick_iface() -> str | None:
        try:
            names = [name for _, name in pysocket.if_nameindex()]
        except Exception:
            names = []

        # Prefer likely Ethernet/Wi‑Fi interfaces; avoid loopback and virtual links.
        preferred_prefixes = ('eth', 'en', 'wlan', 'wl', 'usb', 'br')
        banned_prefixes = ('lo', 'can', 'vcan', 'tun', 'tap', 'docker', 'veth')

        candidates = []
        for n in names:
            nn = str(n or '').strip()
            if not nn:
                continue
            low = nn.lower()
            if low.startswith(banned_prefixes):
                continue
            candidates.append(nn)

        for p in preferred_prefixes:
            for c in candidates:
                if c.lower().startswith(p):
                    return c

        return candidates[0] if candidates else None

    try:
        iface = str(es.get('interface') or '').strip()
    except Exception:
        iface = ''
    if not iface:
        picked = _pick_iface()
        if not picked:
            return
        iface = picked

    # If the user explicitly disabled pcap capture, honor it.
    try:
        pcap_enabled = bool(es.get('pcap_enabled', True))
    except Exception:
        pcap_enabled = True

    try:
        eth_manager.start({
            'interface': iface,
            'pcap_enabled': pcap_enabled,
            'doip_enabled': bool(es.get('doip_enabled', False)),
            'someip_enabled': bool(es.get('someip_enabled', False)),
            'xcp_enabled': bool(es.get('xcp_enabled', False)),
            'doip_ip': str(es.get('target_ip') or '127.0.0.1').strip(),
            'xcp_ip': str(es.get('target_ip') or '127.0.0.1').strip(),
            'xcp_port': int(es.get('xcp_port', 5555) or 5555),
        })
        _log_event('eth_autostart_for_logging', {'interface': iface, 'pcap_enabled': pcap_enabled})

        # If the user never configured Ethernet settings, persist a sensible default
        # so standalone boot can bring Ethernet capture up automatically.
        try:
            if not isinstance(es, dict) or not str(es.get('interface') or '').strip():
                config_store.update({'eth_settings': {
                    'interface': iface,
                    'target_ip': str(es.get('target_ip') or '').strip(),
                    'pcap_enabled': bool(pcap_enabled),
                    'doip_enabled': bool(es.get('doip_enabled', False)),
                    'someip_enabled': bool(es.get('someip_enabled', False)),
                    'xcp_enabled': bool(es.get('xcp_enabled', False)),
                    'xcp_port': int(es.get('xcp_port', 5555) or 5555),
                }})
        except Exception:
            pass
    except Exception as e:
        try:
            _log_event('eth_autostart_for_logging_failed', {'error': str(e), 'interface': iface})
        except Exception:
            pass


def _infer_enabled_can_mirror_buses() -> list[int]:
    """Infer gateway mirror CAN network IDs (1-based) from enabled CAN sources."""
    inferred: list[int] = []
    try:
        for src in (data_source_manager.list_sources() or []):
            try:
                if not isinstance(src, dict):
                    continue
                if str(src.get('type') or '').strip().upper() != 'CAN':
                    continue
                if not bool(src.get('enabled', True)):
                    continue
                cfg_s = src.get('config') if isinstance(src.get('config'), dict) else {}
                ch_id = int(cfg_s.get('channel_id'))
                bus_num = int(ch_id) + 1
                if 1 <= bus_num <= 8 and bus_num not in inferred:
                    inferred.append(bus_num)
            except Exception:
                continue
    except Exception:
        return []
    return inferred


def _prepare_gateway_mirror_for_logging() -> None:
    """Best-effort runtime hardening for CAN+DoIP mirror capture consistency."""
    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}

    try:
        profile_name = str(cfg.get('profile') or '').strip().lower()
    except Exception:
        profile_name = ''

    try:
        gm_raw = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
        gm_cfg = _normalize_gateway_mirror_config(gm_raw)
    except Exception:
        gm_raw = {}
        gm_cfg = _gateway_mirror_defaults()

    try:
        mirror_port = int(gm_cfg.get('dest_port') or 0)
        if mirror_port > 0:
            eth_manager.set_mirror_port(mirror_port)
    except Exception:
        pass

    if profile_name != 'can_doip':
        return

    try:
        can_cfg = gm_cfg.get('can') if isinstance(gm_cfg.get('can'), list) else []
    except Exception:
        can_cfg = []
    if can_cfg:
        return

    inferred = _infer_enabled_can_mirror_buses()
    if not inferred:
        return

    try:
        gm_new = dict(gm_raw) if isinstance(gm_raw, dict) else {}
        gm_new['can'] = inferred
        config_store.update({'gateway_mirror': gm_new})
    except Exception:
        return

    try:
        _rebuild_mirror_channel_map()
    except Exception:
        pass

    try:
        _log_event('gateway_mirror_can_autofill', {
            'profile': 'can_doip',
            'can': inferred,
            'source': 'runtime_start',
        })
    except Exception:
        pass


def _bus_channels_from_logger_config(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Build BusManager channel payloads from persisted logger channel rows."""
    if not isinstance(cfg, dict):
        return []

    raw_channels = cfg.get('logger_channels') if isinstance(cfg.get('logger_channels'), list) else []
    out: list[Dict[str, Any]] = []
    seen_ids: set[int] = set()

    for ch in raw_channels:
        if not isinstance(ch, dict):
            continue
        try:
            ch_id = int(ch.get('id'))
        except Exception:
            continue
        if ch_id in seen_ids:
            continue
        seen_ids.add(ch_id)

        item: Dict[str, Any] = {
            'id': ch_id,
            'type': 'CAN',
        }

        try:
            bitrate = int(ch.get('bitrate') or 0)
        except Exception:
            bitrate = 0
        if bitrate:
            item['bitrate'] = bitrate

        dbc_names: list[str] = []
        try:
            if isinstance(ch.get('dbc_names'), list):
                dbc_names = [str(x or '').strip() for x in ch.get('dbc_names') if str(x or '').strip()]
            else:
                dbc_name = str(ch.get('dbc_name') or '').strip()
                if dbc_name:
                    dbc_names = [dbc_name]
        except Exception:
            dbc_names = []

        dbc_names = [name for name in dbc_names if name and os.path.basename(name) == name]
        if dbc_names:
            dbc_paths = [os.path.join(UPLOAD_FOLDER_DBC, os.path.basename(name)) for name in dbc_names]
            item['dbc_names'] = list(dbc_names)
            item['dbc_name'] = dbc_names[0]
            item['dbcs'] = dbc_paths
            item['dbc'] = dbc_paths[0]

        out.append(item)

    return out


def _logger_channels_from_start_payload(channels: list[Any]) -> list[Dict[str, Any]]:
    """Build persisted logger channel rows from a /api/start payload."""
    if not isinstance(channels, list):
        return []

    out: list[Dict[str, Any]] = []
    seen_ids: set[int] = set()

    for ch in channels:
        if not isinstance(ch, dict):
            continue
        try:
            ch_id = int(ch.get('id'))
        except Exception:
            continue
        if ch_id in seen_ids:
            continue
        seen_ids.add(ch_id)

        item: Dict[str, Any] = {
            'id': ch_id,
        }

        try:
            bitrate = int(ch.get('bitrate') or 0)
        except Exception:
            bitrate = 0
        if bitrate:
            item['bitrate'] = bitrate

        dbc_names: list[str] = []
        try:
            if isinstance(ch.get('dbc_names'), list):
                dbc_names = [str(x or '').strip() for x in ch.get('dbc_names') if str(x or '').strip()]
            else:
                dbc_name = str(ch.get('dbc_name') or '').strip()
                if dbc_name:
                    dbc_names = [dbc_name]
        except Exception:
            dbc_names = []

        dbc_names = [name for name in dbc_names if name and os.path.basename(name) == name]
        if dbc_names:
            item['dbc_names'] = list(dbc_names)
            item['dbc_name'] = dbc_names[0]

        out.append(item)

    return out


def _reconcile_runtime_bus_with_logger_config(cfg: Dict[str, Any]) -> None:
    """Keep the running bus aligned with the Channel Configuration rows.

    The acquisition UI persists CAN channel rows under ``logger_channels``. If the
    bus system was already started earlier, removing those rows in the UI should
    stop stale Kvaser channels instead of leaving them active in the background.
    """
    try:
        if not bool(getattr(manager, 'running', False)):
            return
    except Exception:
        return

    desired_channels = _bus_channels_from_logger_config(cfg)
    desired_ids = sorted(int(ch.get('id')) for ch in desired_channels if isinstance(ch, dict) and ch.get('id') is not None)

    try:
        with manager.lock:
            running_ids = sorted(int(cid) for cid in getattr(manager, 'handlers', {}).keys())
    except Exception:
        running_ids = []

    if desired_ids == running_ids:
        return

    if not desired_channels:
        try:
            manager.stop_bus()
        except Exception:
            pass
        return

    try:
        _kickoff_bus_start_async({'channels': desired_channels})
    except Exception:
        pass


def _log_event(event_name: str, details: dict) -> None:
    try:
        if not getattr(shared_logger, 'active', False):
            return

        # Persist session event markers for timeline review.
        try:
            base = _session_base_from_pathlike(getattr(shared_logger, 'session_base_name', None) or getattr(shared_logger, 'base_name', None))
            if base:
                _append_session_event(base, str(event_name), ts_ms=int(time.time() * 1000), details=details if isinstance(details, dict) else {})
        except Exception:
            pass

        shared_logger.log({
            'timestamp': int(time.time() * 1000),
            'type': 'EVENT',
            'id': 0,
            'dlc': 0,
            'data': [],
            'flags': 0,
            'decoded': {'event': event_name, 'details': details},
        })
    except Exception:
        pass


def _reset_display_last_saved_file() -> None:
    global _display_last_saved_file
    with _display_status_lock:
        _display_last_saved_file = None


def _record_display_saved_file(payload: dict) -> None:
    global _display_last_saved_file
    if not isinstance(payload, dict):
        return

    try:
        session_base = _session_base_from_pathlike(payload.get('session_base_name'))
    except Exception:
        session_base = None

    saved = {
        'name': str(payload.get('name') or '').strip(),
        'path': str(payload.get('path') or '').strip(),
        'kind': str(payload.get('kind') or 'file_saved').strip(),
        'timestamp_ms': int(payload.get('timestamp_ms') or int(time.time() * 1000)),
        'size_bytes': int(payload.get('size_bytes') or 0),
        'part_index': payload.get('part_index'),
        'session_base': session_base,
    }
    if not saved['name'] and saved['path']:
        saved['name'] = os.path.basename(saved['path'])

    with _display_status_lock:
        _display_last_saved_file = dict(saved)

    if session_base:
        try:
            _append_session_event(
                session_base,
                'recording_file_saved',
                ts_ms=saved['timestamp_ms'],
                details=dict(saved),
            )
        except Exception:
            pass


def _get_display_last_saved_file(session_base: str | None = None) -> dict | None:
    cached = None
    with _display_status_lock:
        if isinstance(_display_last_saved_file, dict):
            cached = dict(_display_last_saved_file)

    if cached and (not session_base or cached.get('session_base') == session_base):
        return cached

    if not session_base:
        return cached

    try:
        meta = _read_session_meta(session_base)
        events = meta.get('events') if isinstance(meta.get('events'), list) else []
        for evt in reversed(events):
            if not isinstance(evt, dict):
                continue
            if str(evt.get('name') or '').strip() != 'recording_file_saved':
                continue
            details = evt.get('details') if isinstance(evt.get('details'), dict) else {}
            out = dict(details)
            out['timestamp_ms'] = int(evt.get('ts_ms') or out.get('timestamp_ms') or 0)
            out['session_base'] = session_base
            return out
    except Exception:
        pass

    return cached


shared_logger.on_file_saved = _record_display_saved_file


# Shared Webcam Pipeline (always available)
_camera_buffer = SharedFrameBuffer()


def _parse_formats(val: str):
    parts = [p.strip() for p in (val or '').split(',') if p.strip()]
    return parts if parts else ['csv', 'txt']


_motion_trigger_enabled = str(os.getenv('CAM_MOTION_TRIGGER', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
_motion_trigger_formats = _parse_formats(os.getenv('CAM_MOTION_TRIGGER_FORMATS', 'csv,txt'))

# YOLO trigger (runtime configurable via API; env vars only provide defaults)
_yolo_trigger_armed = str(os.getenv('CAM_YOLO_TRIGGER', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
_yolo_trigger_formats = _parse_formats(os.getenv('CAM_YOLO_TRIGGER_FORMATS', os.getenv('CAM_MOTION_TRIGGER_FORMATS', 'csv,txt')))
_yolo_trigger_classes = [c.strip() for c in (os.getenv('CAM_YOLO_CLASSES', '') or '').split(',') if c.strip()]

# Custom object trigger (trained locally; runs alongside YOLO)
_custom_trigger_armed = str(os.getenv('CAM_CUSTOM_TRIGGER', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
_custom_trigger_formats = _parse_formats(os.getenv('CAM_CUSTOM_TRIGGER_FORMATS', 'csv,txt'))
_custom_trigger_objects = [c.strip() for c in (os.getenv('CAM_CUSTOM_OBJECTS', '') or '').split(',') if c.strip()]
_custom_trigger_threshold = int(os.getenv('CAM_CUSTOM_THRESHOLD', '20') or 20)
_custom_trigger_fps = float(os.getenv('CAM_CUSTOM_FPS', '1.0') or 1.0)
_custom_trigger_cooldown_s = float(os.getenv('CAM_CUSTOM_COOLDOWN_S', '2.0') or 2.0)

# Composite trigger rules
_trigger_rules = {
    'enabled': bool(_saved_config.get('trigger_rules', {}).get('enabled', False)),
    'mode': str(_saved_config.get('trigger_rules', {}).get('mode', 'any') or 'any'),  # any|all
    'sources': list(_saved_config.get('trigger_rules', {}).get('sources', ['yolo'])),
    'window_s': float(_saved_config.get('trigger_rules', {}).get('window_s', 2.0) or 2.0),
    'cooldown_s': float(_saved_config.get('trigger_rules', {}).get('cooldown_s', 2.0) or 2.0),
    'formats': list(_saved_config.get('trigger_rules', {}).get('formats', [])),
    'video_preroll_s': float(_saved_config.get('trigger_rules', {}).get('video_preroll_s', 0.0) or 0.0),
    # Auto-stop is intentionally opt-in: acquisition should run until user stop.
    'auto_stop_enabled': bool(_saved_config.get('trigger_rules', {}).get('auto_stop_enabled', False)),
    # Auto-stop trigger-started logging after inactivity (seconds). 0 disables.
    'auto_stop_s': float(_saved_config.get('trigger_rules', {}).get('auto_stop_s', 0.0) or 0.0),
}
_trigger_state = {
    'last': {},
    'last_start_s': 0.0,
}

# Track trigger activity for auto-stop and to avoid affecting manually started sessions.
_last_trigger_event_s = 0.0
_log_started_by_trigger = False
_log_started_source = None  # 'manual'|'yolo'|'motion'|'custom'|'can'|'eth'
_trigger_activity_s = {
    'yolo': 0.0,
    'motion': 0.0,
    'custom': 0.0,
    'can': 0.0,
    'eth': 0.0,
    'kl15': 0.0,
}
_suppress_until_clear = {
    'yolo': False,
    'motion': False,
    'custom': False,
    'can': False,
    'eth': False,
    'kl15': False,
}

# When user presses Stop, do not allow trigger auto-start again
# until the user explicitly re-arms that trigger.
_manual_stop_latch = {
    'yolo': False,
    'motion': False,
    'custom': False,
    'can': False,
    'eth': False,
    'kl15': False,
}

# Track YOLO presence edge at the app level (CameraManager emits present state periodically).
_yolo_prev_present_app = False


def _reset_yolo_edge_state() -> None:
    global _yolo_prev_present_app
    _yolo_prev_present_app = False


def _to_number(v):
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if s == '':
            return None
        return float(s)
    except Exception:
        return None


def _eval_condition(actual, op: str, expected_raw) -> bool:
    op = str(op or 'eq').strip().lower()

    # Numeric comparisons when possible
    a_num = _to_number(actual)
    e_num = _to_number(expected_raw)
    if op in {'gt', 'gte', 'lt', 'lte'}:
        if a_num is None or e_num is None:
            return False
        if op == 'gt':
            return a_num > e_num
        if op == 'gte':
            return a_num >= e_num
        if op == 'lt':
            return a_num < e_num
        if op == 'lte':
            return a_num <= e_num

    # Equality/inequality (numeric if both numeric, else string)
    if a_num is not None and e_num is not None:
        if op == 'ne':
            return a_num != e_num
        return a_num == e_num

    a_str = '' if actual is None else str(actual)
    e_str = '' if expected_raw is None else str(expected_raw)
    if op == 'ne':
        return a_str != e_str
    return a_str == e_str


# CAN message trigger (DBC-based)
_restore_armed_on_boot = str(os.getenv('KBSM_RESTORE_ARMED', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
_can_trigger_cfg = {
    'armed': bool((_saved_config.get('can_trigger') or {}).get('armed', False)) if _restore_armed_on_boot else False,
    'channel_id': int((_saved_config.get('can_trigger') or {}).get('channel_id', 0) or 0),
    'dbc_name': str((_saved_config.get('can_trigger') or {}).get('dbc_name', '') or ''),
    'message': str((_saved_config.get('can_trigger') or {}).get('message', '') or ''),
    'signal': str((_saved_config.get('can_trigger') or {}).get('signal', '') or ''),
    'start_op': str((_saved_config.get('can_trigger') or {}).get('start_op', 'eq') or 'eq'),
    'start_value': (_saved_config.get('can_trigger') or {}).get('start_value', 1),
    # Auto-stop is intentionally opt-in: acquisition should run until user stop.
    'auto_stop_enabled': bool((_saved_config.get('can_trigger') or {}).get('auto_stop_enabled', False)),
    # If enabled and >0, stop a CAN-trigger-started session when the configured
    # message is not observed for this many seconds.
    'no_message_stop_s': float((_saved_config.get('can_trigger') or {}).get('no_message_stop_s', 2.0) or 2.0),
    'stop_op': str((_saved_config.get('can_trigger') or {}).get('stop_op', 'eq') or 'eq'),
    'stop_value': (_saved_config.get('can_trigger') or {}).get('stop_value', 0),
    'formats': list((_saved_config.get('can_trigger') or {}).get('formats', [])),
}
_can_trigger_state = {
    'active': False,
    'last_match_ms': 0,
    'last_seen_ms': 0,
}


# Ethernet trigger (start logging on first packet when armed)
_eth_trigger_cfg = {
    'armed': bool((_saved_config.get('eth_trigger') or {}).get('armed', False)) if _restore_armed_on_boot else False,
    'formats': list((_saved_config.get('eth_trigger') or {}).get('formats', [])) or ['csv', 'txt'],
    'cooldown_s': float((_saved_config.get('eth_trigger') or {}).get('cooldown_s', 2.0) or 2.0),
}
_eth_trigger_state = {
    'last_start_s': 0.0,
}


# ── KL_15 ignition monitor ─────────────────────────────────────────────
# Automatically start recording when ignition (KL_15 / ZAS_Kl_15) is detected
# on any decoded CAN frame, and stop when the signal disappears or goes to 0.
_kl15_monitor_cfg = {
    'enabled': bool((_saved_config.get('kl15_monitor') or {}).get('enabled', False)),
    'formats': list((_saved_config.get('kl15_monitor') or {}).get('formats', [])) or ['mf4'],
    # Signal names to look for (first match wins).
    'signal_names': list((_saved_config.get('kl15_monitor') or {}).get('signal_names', [])) or [
        'ZAS_Kl_15', 'KL_15', 'KL15', 'Klemme_15',
    ],
    # Message name substring filter (empty = accept any message).
    'message_filter': str((_saved_config.get('kl15_monitor') or {}).get('message_filter', 'Klemmen_Status') or ''),
    # Value considered "ignition ON" (signal > threshold → ON).
    'on_threshold': float((_saved_config.get('kl15_monitor') or {}).get('on_threshold', 0.5) or 0.5),
    # Debounce: require the signal to stay OFF for this many seconds before stopping.
    'off_debounce_s': float((_saved_config.get('kl15_monitor') or {}).get('off_debounce_s', 3.0) or 3.0),
}
_kl15_state = {
    'detected': False,          # True while KL_15 signal value > threshold
    'recording': False,         # True while recording was started by KL_15
    'last_on_ts': 0.0,          # epoch when last ON was seen
    'last_off_ts': 0.0,         # epoch when signal first went OFF
    'last_value': None,         # most recent raw signal value
    'last_signal_name': None,   # which signal name matched
    'last_message_name': None,  # which CAN message carried the signal
}


def _on_can_frame_for_kl15(frame: dict) -> None:
    """BusManager listener: auto-start/stop logging based on KL_15 ignition signal."""
    global _kl15_state
    try:
        if not bool(_kl15_monitor_cfg.get('enabled')):
            return

        decoded = frame.get('decoded') if isinstance(frame, dict) else None
        if not isinstance(decoded, dict):
            return

        msg_name = str(decoded.get('name') or '')
        msg_filter = str(_kl15_monitor_cfg.get('message_filter') or '')
        if msg_filter and msg_filter not in msg_name:
            return

        sigs = decoded.get('signals')
        if not isinstance(sigs, dict):
            return

        # Find the first matching signal name
        matched_name = None
        matched_value = None
        for candidate in (_kl15_monitor_cfg.get('signal_names') or []):
            if candidate and candidate in sigs:
                matched_name = candidate
                matched_value = sigs[candidate]
                break
        if matched_name is None:
            return

        # Coerce value to float
        try:
            val_f = float(matched_value)
        except Exception:
            val_f = 0.0

        threshold = float(_kl15_monitor_cfg.get('on_threshold', 0.5) or 0.5)
        is_on = val_f > threshold
        now = time.time()

        _kl15_state['last_value'] = matched_value
        _kl15_state['last_signal_name'] = matched_name
        _kl15_state['last_message_name'] = msg_name

        if is_on:
            _kl15_state['detected'] = True
            _kl15_state['last_on_ts'] = now
            _kl15_state['last_off_ts'] = 0.0  # reset off debounce

            # Start recording if not already active
            if not bool(getattr(shared_logger, 'active', False)):
                formats = list(_kl15_monitor_cfg.get('formats') or []) or ['mf4']
                _kl15_state['recording'] = True
                _start_logging_async(formats, started_by='kl15', details={
                    'signal': matched_name,
                    'message': msg_name,
                    'value': matched_value,
                })
        else:
            # Signal is OFF
            if _kl15_state.get('detected'):
                if _kl15_state.get('last_off_ts', 0.0) <= 0.0:
                    _kl15_state['last_off_ts'] = now

                debounce = float(_kl15_monitor_cfg.get('off_debounce_s', 3.0) or 3.0)
                off_since = float(_kl15_state.get('last_off_ts') or now)

                if (now - off_since) >= debounce:
                    _kl15_state['detected'] = False
                    # Stop recording if it was started by KL_15
                    if _kl15_state.get('recording') and bool(getattr(shared_logger, 'active', False)):
                        _kl15_state['recording'] = False
                        _stop_logging_async(details={
                            'reason': 'kl15_off',
                            'signal': matched_name,
                            'message': msg_name,
                            'value': matched_value,
                        })
    except Exception:
        return


def _start_logging_async(formats, *, started_by: str, details: dict | None = None):
    def _do():
        global _log_started_by_trigger, _log_started_source
        try:
            if getattr(shared_logger, 'active', False):
                return
            _ensure_eth_running_for_logging()
            _prepare_gateway_mirror_for_logging()
            _reset_display_last_saved_file()
            manager.start_logging(formats)
            eth_manager.start_logging(formats)
            _log_started_by_trigger = True
            _log_started_source = str(started_by or 'trigger')
            try:
                _log_event(f'{started_by}_trigger_start_logging', details or {})
            except Exception:
                pass
            try:
                _recording_sync_event.set()
            except Exception:
                pass
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def _stop_logging_async(*, details: dict | None = None):
    def _do():
        global _log_started_by_trigger, _log_started_source
        try:
            if not getattr(shared_logger, 'active', False):
                return
            prev_src = str(_log_started_source or '').strip().lower()
            manager.stop_logging()
            eth_manager.stop_logging()
            _log_started_by_trigger = False
            _log_started_source = None
            if prev_src == 'yolo':
                _reset_yolo_edge_state()
            try:
                _log_event('can_trigger_stop_logging', details or {})
            except Exception:
                pass
            try:
                _recording_sync_event.set()
            except Exception:
                pass
        except Exception:
            pass

    threading.Thread(target=_do, daemon=True).start()


def _on_can_frame_for_trigger(frame: dict) -> None:
    """BusManager listener: starts/stops logging based on decoded DBC signal conditions."""
    try:
        if not bool(_can_trigger_cfg.get('armed')):
            return
        if bool(_manual_stop_latch.get('can')) and not bool(getattr(shared_logger, 'active', False)):
            return

        # Must match channel
        ch = int(frame.get('channel', -1))
        if ch != int(_can_trigger_cfg.get('channel_id', 0)):
            return

        decoded = frame.get('decoded') if isinstance(frame, dict) else None
        if not isinstance(decoded, dict):
            return
        msg_name = decoded.get('name')
        sigs = decoded.get('signals')
        if not msg_name or not isinstance(sigs, dict):
            return

        if str(msg_name) != str(_can_trigger_cfg.get('message') or ''):
            return

        now_ms = int(time.time() * 1000)
        try:
            _can_trigger_state['last_seen_ms'] = now_ms
        except Exception:
            pass

        sig_name = str(_can_trigger_cfg.get('signal') or '')
        if not sig_name or sig_name not in sigs:
            return
        actual = sigs.get(sig_name)

        formats = list(_can_trigger_cfg.get('formats') or [])
        if not formats:
            formats = ['csv', 'txt']

        if not bool(getattr(shared_logger, 'active', False)):
            # Start condition
            if _eval_condition(actual, _can_trigger_cfg.get('start_op'), _can_trigger_cfg.get('start_value')):
                _can_trigger_state['active'] = True
                _can_trigger_state['last_match_ms'] = now_ms
                _start_logging_async(formats, started_by='can', details={
                    'channel_id': ch,
                    'message': msg_name,
                    'signal': sig_name,
                    'value': actual,
                })
            return

        # If logging is active, check stop condition (opt-in)
        if bool(_log_started_by_trigger) and bool(_can_trigger_state.get('active')) and bool(_can_trigger_cfg.get('auto_stop_enabled')):
            if _eval_condition(actual, _can_trigger_cfg.get('stop_op'), _can_trigger_cfg.get('stop_value')):
                _can_trigger_state['active'] = False
                _can_trigger_state['last_match_ms'] = now_ms
                _stop_logging_async(details={
                    'channel_id': ch,
                    'message': msg_name,
                    'signal': sig_name,
                    'value': actual,
                })
    except Exception:
        return


def _can_trigger_watchdog_loop():
    """Stop CAN-trigger-started logging when the configured message disappears."""
    while True:
        try:
            enabled = bool(_can_trigger_cfg.get('auto_stop_enabled'))
            timeout_s = float(_can_trigger_cfg.get('no_message_stop_s') or 0.0)
            if not enabled or timeout_s <= 0:
                time.sleep(0.5)
                continue

            if not bool(getattr(shared_logger, 'active', False)):
                time.sleep(0.25)
                continue

            if not bool(_log_started_by_trigger):
                time.sleep(0.25)
                continue

            src = str(_log_started_source or '').strip().lower()
            if src != 'can':
                time.sleep(0.25)
                continue

            if not bool(_can_trigger_state.get('active')):
                time.sleep(0.25)
                continue

            last_seen_ms = int(_can_trigger_state.get('last_seen_ms') or 0)
            if last_seen_ms <= 0:
                time.sleep(0.25)
                continue

            now_ms = int(time.time() * 1000)
            if (now_ms - last_seen_ms) >= int(timeout_s * 1000):
                try:
                    _can_trigger_state['active'] = False
                    _can_trigger_state['last_match_ms'] = now_ms
                except Exception:
                    pass
                _stop_logging_async(details={
                    'reason': 'no_message_timeout',
                    'timeout_s': timeout_s,
                    'channel_id': int(_can_trigger_cfg.get('channel_id', 0) or 0),
                    'message': str(_can_trigger_cfg.get('message') or ''),
                })
        except Exception:
            pass
        time.sleep(0.25)


def _apply_saved_config_defaults() -> None:
    """Apply persisted config to in-memory defaults on startup."""
    global _motion_trigger_enabled, _motion_trigger_formats
    global _yolo_trigger_armed, _yolo_trigger_formats, _yolo_trigger_classes
    global _custom_trigger_armed, _custom_trigger_formats, _custom_trigger_objects
    global _custom_trigger_threshold, _custom_trigger_fps, _custom_trigger_cooldown_s

    cfg = _saved_config or {}
    # To keep the system predictable, do not auto-arm triggers on startup by default.
    # Opt-in with KBSM_RESTORE_ARMED=1.
    restore_armed = str(os.getenv('KBSM_RESTORE_ARMED', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
    try:
        m = cfg.get('motion') if isinstance(cfg.get('motion'), dict) else None
        if m:
            if restore_armed and 'armed' in m:
                _motion_trigger_enabled = bool(m.get('armed'))
            if isinstance(m.get('formats'), list):
                _motion_trigger_formats = [str(x).strip() for x in m.get('formats') if str(x).strip()] or _motion_trigger_formats
    except Exception:
        pass

    try:
        y = cfg.get('yolo') if isinstance(cfg.get('yolo'), dict) else None
        if y:
            if restore_armed and 'armed' in y:
                _yolo_trigger_armed = bool(y.get('armed'))
            if isinstance(y.get('formats'), list):
                _yolo_trigger_formats = [str(x).strip() for x in y.get('formats') if str(x).strip()] or _yolo_trigger_formats
            if isinstance(y.get('classes'), list):
                _yolo_trigger_classes = [str(x).strip() for x in y.get('classes') if str(x).strip()]
    except Exception:
        pass

    try:
        c = cfg.get('custom') if isinstance(cfg.get('custom'), dict) else None
        if c:
            if restore_armed and 'armed' in c:
                _custom_trigger_armed = bool(c.get('armed'))
            if isinstance(c.get('formats'), list):
                _custom_trigger_formats = [str(x).strip() for x in c.get('formats') if str(x).strip()] or _custom_trigger_formats
            if isinstance(c.get('objects'), list):
                _custom_trigger_objects = [str(x).strip() for x in c.get('objects') if str(x).strip()]
            if c.get('threshold') is not None:
                _custom_trigger_threshold = int(c.get('threshold'))
            if c.get('fps') is not None:
                _custom_trigger_fps = float(c.get('fps'))
            if c.get('cooldown_s') is not None:
                _custom_trigger_cooldown_s = float(c.get('cooldown_s'))
    except Exception:
        pass


_apply_saved_config_defaults()


def _on_camera_trigger(details: dict) -> None:
    global _last_trigger_event_s, _log_started_by_trigger, _log_started_source, _yolo_prev_present_app
    trigger_kind = (details or {}).get('trigger') or 'motion'

    # Always honor the selected trigger sources (UI checkbox list), even when
    # the composite rule engine is disabled. This prevents unwanted starts from
    # deselected sources (e.g., motion) from blocking the desired trigger.
    try:
        allowed_sources = [str(s).strip() for s in (_trigger_rules.get('sources') or []) if str(s).strip()]
    except Exception:
        allowed_sources = []
    if allowed_sources and str(trigger_kind) not in set(allowed_sources):
        return

    if trigger_kind == 'yolo':
        if not _yolo_trigger_armed:
            return
        formats = _yolo_trigger_formats
        event_name = 'yolo_trigger_start_logging'
        present_val = (details or {}).get('present')
        if present_val is None:
            # Backward compatibility: older camera callbacks may only send detections.
            present = bool((details or {}).get('detections'))
        else:
            # Use the explicit presence flag; detections can include non-selected classes.
            present = bool(present_val)
        # Start only on rising edge; keep updating activity timestamp while present.
        rising = bool(present) and (not bool(_yolo_prev_present_app))
        _yolo_prev_present_app = bool(present)
    elif trigger_kind == 'custom':
        if not _custom_trigger_armed:
            return
        formats = _custom_trigger_formats
        event_name = 'custom_trigger_start_logging'
        # custom callback only fires on match, treat as present
        present = True
    else:
        if not _motion_trigger_enabled:
            return
        formats = _motion_trigger_formats
        event_name = 'motion_trigger_start_logging'
        # motion callback only fires on motion, treat as present
        present = True

    # If user has manually stopped, don't auto-start again until explicit re-arm.
    try:
        if bool(_manual_stop_latch.get(trigger_kind)) and not bool(getattr(shared_logger, 'active', False)):
            return
    except Exception:
        pass

    # Update activity timestamp while recording so auto-stop only happens after absence.
    try:
        if present and bool(getattr(shared_logger, 'active', False)) and bool(_log_started_by_trigger):
            ts = float((details or {}).get('timestamp_s') or time.time())
            _trigger_activity_s[str(trigger_kind)] = ts
            # Keep a global copy for debugging/legacy behavior.
            _last_trigger_event_s = ts
            return
    except Exception:
        pass

    # Only start logging on a positive presence.
    if not present:
        return

    # YOLO triggers should only start on a rising edge.
    # This prevents immediate re-trigger while an object remains in view.
    if trigger_kind == 'yolo' and not rising and not bool(getattr(shared_logger, 'active', False)):
        return

    # Composite rule engine (optional)
    try:
        if _trigger_rules.get('enabled'):
            now_s = float(details.get('timestamp_s') or time.time())
            src = trigger_kind
            sources = [str(s).strip() for s in (_trigger_rules.get('sources') or []) if str(s).strip()]
            if src not in sources:
                return
            _trigger_state['last'][src] = now_s

            # global cooldown
            cd = float(_trigger_rules.get('cooldown_s') or 0.0)
            if (now_s - float(_trigger_state.get('last_start_s') or 0.0)) < cd:
                return

            mode = str(_trigger_rules.get('mode') or 'any').lower().strip()
            win = float(_trigger_rules.get('window_s') or 0.0)

            should_start = False
            if mode == 'all':
                should_start = True
                for s in sources:
                    ts = _trigger_state['last'].get(s)
                    if ts is None or (now_s - float(ts)) > win:
                        should_start = False
                        break
            else:
                should_start = True

            if not should_start:
                return

            # allow rules to override formats
            rule_formats = [str(x).strip() for x in (_trigger_rules.get('formats') or []) if str(x).strip()]
            if rule_formats:
                formats = rule_formats
            else:
                # union defaults
                merged = []
                for f in (_motion_trigger_formats + _yolo_trigger_formats + _custom_trigger_formats):
                    if f not in merged:
                        merged.append(f)
                formats = merged or formats

            _trigger_state['last_start_s'] = now_s
            event_name = f"composite_trigger_start_logging:{src}:{mode}"
    except Exception:
        pass

    try:
        if getattr(shared_logger, 'active', False):
            return
        _ensure_eth_running_for_logging()
        _prepare_gateway_mirror_for_logging()
        _reset_display_last_saved_file()
        manager.start_logging(formats)
        eth_manager.start_logging(formats)
        _log_started_by_trigger = True
        _log_started_source = str(trigger_kind)
        ts = float((details or {}).get('timestamp_s') or time.time())
        _trigger_activity_s[str(trigger_kind)] = ts
        _last_trigger_event_s = ts
        _log_event(event_name, {'formats': formats, **(details or {})})
    except Exception:
        pass


def _on_eth_trigger(packet: dict) -> None:
    """Start logging when Ethernet traffic is observed (when armed)."""
    global _log_started_by_trigger, _log_started_source, _last_trigger_event_s

    if not bool(_eth_trigger_cfg.get('armed')):
        return

    # If user has manually stopped, don't auto-start again until explicit re-arm.
    try:
        if bool(_manual_stop_latch.get('eth')) and not bool(getattr(shared_logger, 'active', False)):
            return
    except Exception:
        pass

    now_s = float(time.time())

    # While recording (trigger-started), keep updating activity timestamp.
    try:
        if bool(getattr(shared_logger, 'active', False)) and bool(_log_started_by_trigger):
            # Only let Ethernet traffic extend sessions that were *started by Ethernet*.
            if str(_log_started_source or '') == 'eth':
                _trigger_activity_s['eth'] = now_s
                _last_trigger_event_s = now_s
            return
    except Exception:
        pass

    # Cooldown / spam protection.
    try:
        cd = float(_eth_trigger_cfg.get('cooldown_s') or 0.0)
    except Exception:
        cd = 0.0
    try:
        last = float(_eth_trigger_state.get('last_start_s') or 0.0)
    except Exception:
        last = 0.0
    if (now_s - last) < cd:
        return

    if bool(getattr(shared_logger, 'active', False)):
        return

    formats = [str(x).strip() for x in (_eth_trigger_cfg.get('formats') or []) if str(x).strip()]
    if not formats:
        formats = ['csv', 'txt']

    try:
        _prepare_gateway_mirror_for_logging()
        _reset_display_last_saved_file()
        manager.start_logging(formats)
        eth_manager.start_logging(formats)
        _log_started_by_trigger = True
        _log_started_source = 'eth'
        _eth_trigger_state['last_start_s'] = now_s
        _trigger_activity_s['eth'] = now_s
        _last_trigger_event_s = now_s
        _log_event('eth_trigger_start_logging', {'formats': formats, 'packet': packet or {}})
    except Exception:
        pass


@app.route('/api/trigger/rules', methods=['GET', 'POST'])
def trigger_rules_config():
    global _trigger_rules
    if request.method == 'POST':
        data = request.json or {}
        try:
            if 'enabled' in data:
                _trigger_rules['enabled'] = bool(data.get('enabled'))
            if 'mode' in data and str(data.get('mode')).lower() in {'any', 'all'}:
                _trigger_rules['mode'] = str(data.get('mode')).lower()
            if isinstance(data.get('sources'), list):
                _trigger_rules['sources'] = [str(x).strip() for x in data.get('sources') if str(x).strip()]
            if data.get('window_s') is not None:
                _trigger_rules['window_s'] = float(data.get('window_s'))
            if data.get('cooldown_s') is not None:
                _trigger_rules['cooldown_s'] = float(data.get('cooldown_s'))
            if isinstance(data.get('formats'), list):
                _trigger_rules['formats'] = [str(x).strip() for x in data.get('formats') if str(x).strip()]
            if data.get('video_preroll_s') is not None:
                _trigger_rules['video_preroll_s'] = float(data.get('video_preroll_s'))
                try:
                    if hasattr(_camera_manager, 'set_preroll_runtime'):
                        _camera_manager.set_preroll_runtime(seconds=float(_trigger_rules['video_preroll_s']))
                except Exception:
                    pass
            if 'auto_stop_enabled' in data:
                _trigger_rules['auto_stop_enabled'] = bool(data.get('auto_stop_enabled'))
            if data.get('auto_stop_s') is not None:
                _trigger_rules['auto_stop_s'] = float(data.get('auto_stop_s'))
        except Exception:
            pass

        try:
            config_store.update({'trigger_rules': dict(_trigger_rules)})
        except Exception:
            pass

    return jsonify(dict(_trigger_rules))


@app.route('/api/trigger/yolo', methods=['GET', 'POST'])
def yolo_trigger_config():
    """Configure YOLO trigger from the UI.

    POST body:
      {
        "armed": true|false,
        "classes": ["person","car"],
                "formats": ["csv","mf4",...],
                "conf": 0.5,
                "imgsz": 320,
                "fps": 1.0,
                "cooldown_s": 2.0,
                "model": "yolov8n.pt"
      }
    """
    global _yolo_trigger_armed, _yolo_trigger_formats, _yolo_trigger_classes

    if request.method == 'POST':
        data = request.json or {}

        # Optional runtime settings for YOLO inference
        yolo_conf = None
        yolo_imgsz = None
        yolo_fps = None
        yolo_cooldown_s = None
        yolo_model = None
        try:
            if 'conf' in data and data.get('conf') is not None:
                yolo_conf = float(data.get('conf'))
            if 'imgsz' in data and data.get('imgsz') is not None:
                yolo_imgsz = int(data.get('imgsz'))
            if 'fps' in data and data.get('fps') is not None:
                yolo_fps = float(data.get('fps'))
            if 'cooldown_s' in data and data.get('cooldown_s') is not None:
                yolo_cooldown_s = float(data.get('cooldown_s'))
            if 'model' in data and data.get('model') is not None:
                yolo_model = str(data.get('model')).strip()
        except Exception:
            # ignore parse errors; keep existing runtime settings
            yolo_conf = None
            yolo_imgsz = None
            yolo_fps = None
            yolo_cooldown_s = None
            yolo_model = None

        if 'formats' in data and isinstance(data.get('formats'), list):
            _yolo_trigger_formats = [str(x).strip() for x in data.get('formats') if str(x).strip()]
            if not _yolo_trigger_formats:
                _yolo_trigger_formats = ['csv', 'txt']

        if 'classes' in data and isinstance(data.get('classes'), list):
            _yolo_trigger_classes = [str(x).strip() for x in data.get('classes') if str(x).strip()]

        if 'armed' in data:
            _yolo_trigger_armed = bool(data.get('armed'))
            if _yolo_trigger_armed:
                # Lazy-start camera only when needed.
                try:
                    _camera_manager.start()
                except Exception:
                    pass
                try:
                    _manual_stop_latch['yolo'] = False
                except Exception:
                    pass
                # Reset app-level edge state so it can trigger immediately even if the
                # object is already in view when (re)arming.
                try:
                    global _yolo_prev_present_app
                    _yolo_prev_present_app = False
                except Exception:
                    pass

        # Enable/disable YOLO inference in the camera pipeline.
        try:
            classes_raw = ','.join(_yolo_trigger_classes)
            if hasattr(_camera_manager, 'set_yolo_runtime'):
                rearm = bool('armed' in data and bool(_yolo_trigger_armed))
                _camera_manager.set_yolo_runtime(
                    enabled=bool(_yolo_trigger_armed),
                    rearm=rearm,
                    classes_raw=classes_raw,
                    model_name=yolo_model,
                    conf=yolo_conf,
                    imgsz=yolo_imgsz,
                    fps=yolo_fps,
                    cooldown_s=yolo_cooldown_s,
                )
        except Exception:
            pass

        _log_event('yolo_trigger_config', {
            'armed': _yolo_trigger_armed,
            'formats': _yolo_trigger_formats,
            'classes': _yolo_trigger_classes,
        })

        try:
            existing_cfg = {}
            try:
                existing_cfg = config_store.get_config_only() or {}
            except Exception:
                existing_cfg = {}

            existing_yolo = existing_cfg.get('yolo') if isinstance(existing_cfg, dict) else None
            yolo_cfg = dict(existing_yolo) if isinstance(existing_yolo, dict) else {}

            # Always persist armed/formats/classes.
            yolo_cfg['armed'] = bool(_yolo_trigger_armed)
            yolo_cfg['formats'] = list(_yolo_trigger_formats)
            yolo_cfg['classes'] = list(_yolo_trigger_classes)

            # Only overwrite runtime settings if provided in this request.
            if yolo_conf is not None:
                yolo_cfg['conf'] = float(yolo_conf)
            if yolo_imgsz is not None:
                yolo_cfg['imgsz'] = int(yolo_imgsz)
            if yolo_fps is not None:
                yolo_cfg['fps'] = float(yolo_fps)
            if yolo_cooldown_s is not None:
                yolo_cfg['cooldown_s'] = float(yolo_cooldown_s)
            if yolo_model is not None and str(yolo_model).strip():
                yolo_cfg['model'] = str(yolo_model).strip()

            config_store.update({'yolo': yolo_cfg})
        except Exception:
            pass

    cam_status = {}
    try:
        cam_status = _camera_manager.status() if hasattr(_camera_manager, 'status') else {}
    except Exception:
        cam_status = {}

    return jsonify({
        'armed': bool(_yolo_trigger_armed),
        'latched': bool(_manual_stop_latch.get('yolo', False)),
        'formats': list(_yolo_trigger_formats),
        'classes': list(_yolo_trigger_classes),
        'conf': cam_status.get('yolo_conf'),
        'imgsz': cam_status.get('yolo_imgsz'),
        'fps': cam_status.get('yolo_fps'),
        'cooldown_s': cam_status.get('yolo_cooldown_s'),
        'model': cam_status.get('yolo_model'),
        'last_error': cam_status.get('yolo_last_error'),
    })


@app.route('/api/yolo/test', methods=['POST'])
def yolo_test_once():
    """Run a single YOLO inference on the latest camera frame.

    POST body (optional):
      {"conf":0.5,"imgsz":320,"classes":["person"],"model":"yolov8n.pt"}
    """
    data = request.json or {}
    conf = data.get('conf') if isinstance(data, dict) else None
    imgsz = data.get('imgsz') if isinstance(data, dict) else None
    model = data.get('model') if isinstance(data, dict) else None
    classes = data.get('classes') if isinstance(data, dict) else None

    classes_raw = None
    if isinstance(classes, list):
        classes_raw = ','.join([str(x).strip() for x in classes if str(x).strip()])

    try:
        # Ensure camera pipeline is running for tests.
        try:
            _camera_manager.start()
        except Exception:
            pass
        if not hasattr(_camera_manager, 'yolo_test'):
            return jsonify({'ok': False, 'error': 'yolo test not available'}), 501
        res = _camera_manager.yolo_test(
            conf=float(conf) if conf is not None else None,
            imgsz=int(imgsz) if imgsz is not None else None,
            classes_raw=classes_raw,
            model_name=str(model).strip() if model is not None else None,
        )
        code = 200 if res.get('ok') else 500
        return jsonify(res), code
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/trigger/custom', methods=['GET', 'POST'])
def custom_trigger_config():
    """Configure custom-object trigger from the UI.

    POST body:
      {"armed":true,"objects":["my_obj"],"formats":["csv"],"threshold":20,"fps":1.0,"cooldown_s":2.0}
    """
    global _custom_trigger_armed, _custom_trigger_formats, _custom_trigger_objects
    global _custom_trigger_threshold, _custom_trigger_fps, _custom_trigger_cooldown_s

    if request.method == 'POST':
        data = request.json or {}

        if 'formats' in data and isinstance(data.get('formats'), list):
            _custom_trigger_formats = [str(x).strip() for x in data.get('formats') if str(x).strip()]
            if not _custom_trigger_formats:
                _custom_trigger_formats = ['csv', 'txt']

        if 'objects' in data and isinstance(data.get('objects'), list):
            _custom_trigger_objects = [str(x).strip() for x in data.get('objects') if str(x).strip()]

        if 'armed' in data:
            _custom_trigger_armed = bool(data.get('armed'))
            if _custom_trigger_armed:
                # Lazy-start camera only when needed.
                try:
                    _camera_manager.start()
                except Exception:
                    pass
                try:
                    _manual_stop_latch['custom'] = False
                except Exception:
                    pass

        try:
            if 'threshold' in data and data.get('threshold') is not None:
                _custom_trigger_threshold = int(data.get('threshold'))
            if 'fps' in data and data.get('fps') is not None:
                _custom_trigger_fps = float(data.get('fps'))
            if 'cooldown_s' in data and data.get('cooldown_s') is not None:
                _custom_trigger_cooldown_s = float(data.get('cooldown_s'))
        except Exception:
            pass

        try:
            if hasattr(_camera_manager, 'set_custom_runtime'):
                _camera_manager.set_custom_runtime(
                    enabled=bool(_custom_trigger_armed),
                    objects_raw=','.join(_custom_trigger_objects),
                    fps=float(_custom_trigger_fps),
                    cooldown_s=float(_custom_trigger_cooldown_s),
                    threshold=int(_custom_trigger_threshold),
                )
        except Exception:
            pass

        _log_event('custom_trigger_config', {
            'armed': _custom_trigger_armed,
            'formats': _custom_trigger_formats,
            'objects': _custom_trigger_objects,
            'threshold': _custom_trigger_threshold,
            'fps': _custom_trigger_fps,
            'cooldown_s': _custom_trigger_cooldown_s,
        })

        try:
            config_store.update({'custom': {
                'armed': bool(_custom_trigger_armed),
                'formats': list(_custom_trigger_formats),
                'objects': list(_custom_trigger_objects),
                'threshold': int(_custom_trigger_threshold),
                'fps': float(_custom_trigger_fps),
                'cooldown_s': float(_custom_trigger_cooldown_s),
            }})
        except Exception:
            pass

    cam_status = {}
    try:
        cam_status = _camera_manager.status() if hasattr(_camera_manager, 'status') else {}
    except Exception:
        cam_status = {}

    return jsonify({
        'armed': bool(_custom_trigger_armed),
        'formats': list(_custom_trigger_formats),
        'objects': list(_custom_trigger_objects),
        'threshold': int(_custom_trigger_threshold),
        'fps': float(_custom_trigger_fps),
        'cooldown_s': float(_custom_trigger_cooldown_s),
        'last_error': cam_status.get('custom_last_error'),
    })


@app.route('/api/custom/objects', methods=['GET'])
def custom_objects_list():
    try:
        return jsonify({'ok': True, 'objects': _custom_matcher.list_objects()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'objects': []}), 500


@app.route('/api/custom/objects/capture', methods=['POST'])
def custom_objects_capture():
    data = request.json or {}
    name = str((data or {}).get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400

    frame, _ts = _camera_manager.get_latest_raw_frame()
    if frame is None:
        return jsonify({'ok': False, 'error': 'no frame available'}), 503

    try:
        res = _custom_matcher.capture_sample(name, frame)
        return jsonify({'ok': True, **res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/custom/objects/train', methods=['POST'])
def custom_objects_train():
    data = request.json or {}
    name = str((data or {}).get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    try:
        res = _custom_matcher.train(name)
        return jsonify({'ok': True, **res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/custom/test', methods=['POST'])
def custom_test_once():
    data = request.json or {}
    threshold = None
    names = None
    try:
        if isinstance((data or {}).get('threshold'), (int, float)):
            threshold = int((data or {}).get('threshold'))
    except Exception:
        threshold = None

    if isinstance((data or {}).get('objects'), list):
        names = [str(x).strip() for x in (data or {}).get('objects') if str(x).strip()]

    # Ensure camera pipeline is running for tests.
    try:
        _camera_manager.start()
    except Exception:
        pass

    frame, _ts = _camera_manager.get_latest_raw_frame()
    if frame is None:
        return jsonify({'ok': False, 'error': 'no frame available'}), 503

    try:
        res = _custom_matcher.detect(frame, names_filter=names, threshold=int(threshold) if threshold is not None else 20)
        return jsonify({'ok': True, 'result': res})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


_custom_matcher = CustomObjectMatcher(base_dir=os.path.join(PROJECT_DIR, 'custom_objects'))

_camera_manager = CameraManager(
    frame_buffer=_camera_buffer,
    device=os.getenv('CAM_DEVICE'),
    output_fps=float(os.getenv('CAM_FPS', '10') or 10),
    width=int(os.getenv('CAM_WIDTH')) if os.getenv('CAM_WIDTH') else None,
    height=int(os.getenv('CAM_HEIGHT')) if os.getenv('CAM_HEIGHT') else None,
    jpeg_quality=int(os.getenv('CAM_JPEG_QUALITY', '80') or 80),
    event_sink=_log_event,
    motion_callback=_on_camera_trigger,
    custom_matcher=_custom_matcher,
)

# Camera can be noisy on systems without a usable /dev/video* device.
# Autostart only when explicitly enabled or when a trigger is armed.
try:
    cam_autostart = str(os.getenv('CAM_AUTOSTART', '0') or '0').strip().lower() in {'1', 'true', 'yes', 'on'}
except Exception:
    cam_autostart = False

try:
    if cam_autostart or bool(_motion_trigger_enabled) or bool(_yolo_trigger_armed) or bool(_custom_trigger_armed):
        _camera_manager.start()
except Exception:
    pass

# Ensure YOLO inference state matches the current trigger config on startup.
try:
    if hasattr(_camera_manager, 'set_yolo_runtime'):
        _camera_manager.set_yolo_runtime(
            enabled=bool(_yolo_trigger_armed),
            classes_raw=','.join(_yolo_trigger_classes),
        )
except Exception:
    pass

try:
    if hasattr(_camera_manager, 'set_preroll_runtime'):
        _camera_manager.set_preroll_runtime(seconds=float(_trigger_rules.get('video_preroll_s') or 0.0))
except Exception:
    pass

# Ensure custom matcher state matches the current trigger config on startup.
try:
    if hasattr(_camera_manager, 'set_custom_runtime'):
        _camera_manager.set_custom_runtime(
            enabled=bool(_custom_trigger_armed),
            objects_raw=','.join(_custom_trigger_objects),
            fps=float(_custom_trigger_fps),
            cooldown_s=float(_custom_trigger_cooldown_s),
            threshold=int(_custom_trigger_threshold),
        )
except Exception:
    pass
_camera_streamer = MJPEGStreamer(_camera_buffer)

# Video recorder (FFmpeg) synced with CAN logging
_video_recorder = VideoRecorder(frame_source=_camera_manager.get_latest_raw_frame)
_recording_sync_event = threading.Event()


def _recording_sync_loop():
    last_active = False
    last_base = None
    last_session_base = None
    last_video_attempt_session_base = None
    last_video_attempt_s = 0.0
    video_started_for_session = False
    video_failure_logged_for_session = False
    while True:
        try:
            active = bool(getattr(shared_logger, 'active', False))
            # NOTE: `base_name` can change transiently during MF4 part flushes.
            # For video + metadata we must use a stable session identifier.
            base = getattr(shared_logger, 'base_name', None)
            session_base = getattr(shared_logger, 'session_base_name', None) or base

            # If video recording is disabled, make sure we never run the recorder.
            if not bool(_video_recording_enabled):
                try:
                    if bool(_video_recorder.status().get('recording', False)):
                        _video_recorder.stop()
                except Exception:
                    pass
                video_started_for_session = False
                video_failure_logged_for_session = False
                last_video_attempt_session_base = None
                last_video_attempt_s = 0.0
                last_active = active
                last_base = base
                last_session_base = session_base
                _recording_sync_event.wait(timeout=0.10)
                _recording_sync_event.clear()
                continue

            session_changed = bool(session_base and session_base != last_session_base)
            if session_changed or (active and not last_active):
                video_started_for_session = False
                video_failure_logged_for_session = False
                last_video_attempt_session_base = None
                last_video_attempt_s = 0.0

            # Start video recording for the active session.
            # Retry when the first attempt fails transiently (e.g. no camera frame yet).
            if active and session_base and not video_started_for_session:
                now_s = time.time()
                same_attempt_session = (session_base == last_video_attempt_session_base)
                should_attempt = False
                if not same_attempt_session:
                    should_attempt = True
                else:
                    retry_s = 1.0
                    if (now_s - float(last_video_attempt_s or 0.0)) >= retry_s:
                        should_attempt = True

                if should_attempt:
                    last_video_attempt_session_base = session_base
                    last_video_attempt_s = now_s
                    out_path = f"{session_base}.mp4"
                    pre = []
                    try:
                        if float(_trigger_rules.get('video_preroll_s') or 0.0) > 0 and hasattr(_camera_manager, 'get_preroll_bytes'):
                            pre = _camera_manager.get_preroll_bytes()
                    except Exception:
                        pre = []
                    # Make MP4 recording reliable even when no UI client is streaming.
                    try:
                        _camera_manager.start()
                    except Exception:
                        pass
                    started = _video_recorder.start(out_path, pre_roll_bytes=pre)
                    try:
                        # Capture start time as close as possible to a successful start.
                        now_ms = int(time.time() * 1000)
                        b = _session_base_from_pathlike(session_base)
                        if started and b:
                            _write_session_meta(b, {
                                'video_start_epoch_ms': int(now_ms),
                                'video_path': str(out_path),
                            })
                    except Exception:
                        pass
                    if started:
                        video_started_for_session = True
                        video_failure_logged_for_session = False
                        _log_event('video_recording_started', {
                            'output': out_path,
                            **_video_recorder.status(),
                        })
                    elif not video_failure_logged_for_session:
                        video_failure_logged_for_session = True
                        _log_event('video_recording_failed', {
                            'output': out_path,
                            **_video_recorder.status(),
                        })

            if (not active) and last_active:
                _video_recorder.stop()
                video_started_for_session = False
                video_failure_logged_for_session = False
                last_video_attempt_session_base = None
                last_video_attempt_s = 0.0
                try:
                    now_ms = int(time.time() * 1000)
                except Exception:
                    now_ms = None
                try:
                    b = _session_base_from_pathlike(last_session_base or last_base)
                    if b and now_ms is not None:
                        _write_session_meta(b, {
                            'video_stop_epoch_ms': int(now_ms),
                        })
                except Exception:
                    pass
                _log_event('video_recording_stopped', _video_recorder.status())

            last_active = active
            last_base = base
            last_session_base = session_base
        except Exception:
            pass
        _recording_sync_event.wait(timeout=0.10)
        _recording_sync_event.clear()


@app.route('/api/video/recording', methods=['GET', 'POST'])
def video_recording_config():
    """Enable/disable MP4 recording that is synced with CAN logging.

    When disabled, CAN/Ethernet MF4 recording still works; only the MP4 video
    recorder is suppressed.

    GET:  {"enabled": true, "status": {...}}
    POST: {"enabled": false}
    """
    global _video_recording_enabled
    if request.method == 'GET':
        return jsonify({'enabled': bool(_video_recording_enabled), 'status': _video_recorder.status()})

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    if 'enabled' not in data:
        return jsonify({'ok': False, 'error': 'missing enabled'}), 400
    try:
        _video_recording_enabled = bool(data.get('enabled'))
    except Exception:
        _video_recording_enabled = True

    # Persist
    try:
        config_store.update({'video_recording_enabled': bool(_video_recording_enabled)})
    except Exception:
        pass

    # If disabling while active, stop immediately.
    if not bool(_video_recording_enabled):
        try:
            _video_recorder.stop()
        except Exception:
            pass
        try:
            _recording_sync_event.set()
        except Exception:
            pass

    return jsonify({'ok': True, 'enabled': bool(_video_recording_enabled), 'status': _video_recorder.status()})


def _trigger_autostop_loop():
    """Stop logging after trigger inactivity.

    Only applies to sessions started by camera triggers.
    """
    global _log_started_by_trigger, _log_started_source, _last_trigger_event_s
    while True:
        try:
            # Auto-stop is opt-in. Default behavior is to keep acquisition running
            # until the user explicitly presses Stop.
            env_enabled = str(os.getenv('KBSM_TRIGGER_AUTOSTOP', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
            cfg_enabled = bool(_trigger_rules.get('auto_stop_enabled'))
            if not (env_enabled or cfg_enabled):
                time.sleep(0.5)
                continue

            auto_stop_s = float(_trigger_rules.get('auto_stop_s') or 0.0)
            if auto_stop_s > 0 and bool(getattr(shared_logger, 'active', False)) and bool(_log_started_by_trigger):
                # Auto-stop only for camera-trigger-started sessions.
                src = str(_log_started_source or '').strip().lower()
                if src not in {'yolo', 'motion', 'custom'}:
                    time.sleep(0.5)
                    continue
                now_s = time.time()
                last_s = float(_trigger_activity_s.get(src) or 0.0)
                # Fallbacks for legacy state
                if last_s <= 0:
                    last_s = float(_last_trigger_event_s or 0.0)
                if last_s <= 0:
                    last_s = float(_trigger_state.get('last_start_s') or 0.0)
                if last_s > 0 and (now_s - last_s) >= auto_stop_s:
                    try:
                        manager.stop_logging()
                        eth_manager.stop_logging()
                        _log_event('trigger_autostop', {'after_s': auto_stop_s})
                    except Exception:
                        pass
                    _log_started_by_trigger = False
                    _log_started_source = None
                    if src == 'yolo':
                        _reset_yolo_edge_state()
        except Exception:
            pass
        time.sleep(0.5)

# Initialize Managers
manager = BusManager(socketio, shared_logger)
eth_manager = EthernetManager(socketio, shared_logger)
# Connect Ethernet Frame Injection to BusManager (Iron Bird setup)
eth_manager.set_mirror_injection_callback(manager.inject_frame)

# Align mirror listening port with gateway_mirror config so capture matches
# the dest_port the gateway is told to send traffic to.
try:
    _init_cfg = config_store.get_config_only() or {}
    _init_gm = _init_cfg.get('gateway_mirror') if isinstance(_init_cfg.get('gateway_mirror'), dict) else {}
    _init_mirror_port = _init_gm.get('dest_port')
    if _init_mirror_port:
        eth_manager.set_mirror_port(int(_init_mirror_port))
except Exception:
    pass

scanner_service = VAGScannerService(manager, socketio)


def _log_dir_resolver() -> str:
    return str(LOG_FOLDER)


experimental_assistant = ExperimentalAssistantService(
    bus_manager=manager,
    config_store=config_store,
    scanner_service=scanner_service,
    log_dir_resolver=_log_dir_resolver,
    scan_report_dir_resolver=_log_dir_resolver,
    ethernet_manager=eth_manager,
)

# Wire the Sentinel ref into the scanner service so DoIP actions can pause
# Sentinel MIL polling and avoid tester-address (0x0E00) collisions on the
# gateway, which manifest as a post-RoutingActivation Broken-pipe loop and
# "Discovery complete. ECUs found: 0".
try:
    scanner_service._sentinel = experimental_assistant
except Exception:
    pass

# ── XCP on CAN — lazy singleton factory ──────────────────────────────────────
_xcp_can_client: Optional['XcpCanClient'] = None
_xcp_can_lock = threading.Lock()

# In-memory cache for the last parsed A2L file
_xcp_a2l_result: Optional['A2lParseResult'] = None
_xcp_a2l_path:   Optional[str]              = None   # on-disk path of the last uploaded A2L
_xcp_a2l_lock   = threading.Lock()

# In-memory cache for loaded Seed & Key Binary (.skb)
_xcp_skb_result: Optional['SkbParseResult'] = None
_xcp_skb_lock   = threading.Lock()
_xcp_skb_path:  Optional[str] = None

# Per-signal acquisition config from last GLC/LAB import
_xcp_signal_acq: Dict[str, Dict[str, Any]] = {}
_xcp_signal_acq_lock = threading.Lock()


def _get_xcp_can_client(autocreate: bool = True) -> Optional['XcpCanClient']:
    """Return (and lazily create) the XcpCanClient singleton."""
    global _xcp_can_client
    with _xcp_can_lock:
        if _xcp_can_client is None and autocreate:
            cfg = normalize_xcp_can_config((config_store.get_config_only() or {}).get('xcp_can'))
            _xcp_can_client = XcpCanClient(
                bus_manager=manager,
                config=cfg,
                socketio=socketio,
                mf4_logger_cb=getattr(eth_manager, 'log_xcp', None),
            )
        return _xcp_can_client


# Start the assistant loop only if enabled in config; otherwise it stays dormant.
try:
    _ea_cfg = (config_store.get_config_only() or {}).get('experimental_assistant')
    if isinstance(_ea_cfg, dict) and bool(_ea_cfg.get('enabled', False)):
        experimental_assistant.enable()
except Exception:
    pass

# Initialize Monitoring components
_monitor_dir = os.path.join(LOG_FOLDER, 'monitor')
data_source_manager = DataSourceManager(config_store, dbc_dir=UPLOAD_FOLDER_DBC, fibex_dir=UPLOAD_FOLDER_FIBEX)
try:
    data_source_manager.ensure_default_can_sources()
except Exception:
    pass
violation_logger = ViolationLogger(base_dir=_monitor_dir, enable_csv=True)
comparison_engine = ComparisonEngine(config_store, violation_logger, socketio=socketio)

# Persistent DBC message catalog (SQLite)
dbc_catalog_db = DbcCatalogDb(base_dir=_monitor_dir)

# Initialize AI components (lightweight anomaly scoring + suggestions)
anomaly_logger = AnomalyLogger(base_dir=_monitor_dir, enable_csv=True)
anomaly_engine = AnomalyEngine(config_store, socketio=socketio, anomaly_logger=anomaly_logger, violation_logger=violation_logger)

# ── Mirror channel → physical channel mapping ────────────────────────
# When the gateway mirrors CAN bus N, frames arrive on virtual
# channel_ids that depend on the wire format:
#   • Iron Bird / Raw CAN-in-UDP : channel 99
#   • AUTOSAR Bus Mirroring      : channel 100 + NetworkID
#   • FlexRay mirror             : channel 200 + NetworkID
#   • LIN mirror                 : channel 150 + NetworkID
#
# To allow ComparisonEngine rules (which reference physical source_ids
# like "src_8aadba2ac08a") to match mirror frames, we map the virtual
# channel back to the physical CAN/FlexRay bus that is being mirrored.
# The mapping is derived from the gateway_mirror config (e.g. can:[2]).
#
# _mirror_channel_map is rebuilt each time mirror starts; it maps
# virtual_channel_id → physical_channel_id.
_mirror_channel_map: Dict[int, int] = {}


def _rebuild_mirror_channel_map() -> None:
    """Rebuild the mirror-channel → physical-channel lookup from config."""
    global _mirror_channel_map
    new_map: Dict[int, int] = {}
    try:
        cfg = config_store.get_config_only() or {}
        gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
        can_buses = gm.get('can') if isinstance(gm.get('can'), list) else []
        flexray_buses = gm.get('flexray') if isinstance(gm.get('flexray'), list) else []

        can_phys_channels: set[int] = set()
        fr_phys_channels: set[int] = set()
        try:
            for src in (data_source_manager.list_sources() or []):
                if not isinstance(src, dict) or not bool(src.get('enabled', True)):
                    continue
                st = str(src.get('type') or '').strip().upper()
                cfg_s = src.get('config') if isinstance(src.get('config'), dict) else {}
                if st == 'CAN':
                    try:
                        can_phys_channels.add(int(cfg_s.get('channel_id')))
                    except Exception:
                        pass
                elif st == 'FLEXRAY':
                    try:
                        fr_phys_channels.add(int(cfg_s.get('channel_id')))
                    except Exception:
                        pass
        except Exception:
            pass

        def _resolve_phys(bus_num: int, available: set[int], *, prefer_one_based: bool) -> int:
            b = int(bus_num)
            candidates = [b - 1, b] if prefer_one_based else [b, b - 1]
            for c in candidates:
                if c in available:
                    return int(c)
            if prefer_one_based and b > 0:
                return int(b - 1)
            return int(b)

        first_can_phys = None

        for bus_num in can_buses:
            try:
                net_id = int(bus_num)
                phys = _resolve_phys(net_id, can_phys_channels, prefer_one_based=True)
            except Exception:
                continue
            # Iron Bird / Raw CAN channel 99 → map to first configured CAN
            if 99 not in new_map:
                first_can_phys = int(phys)
                new_map[99] = phys
            # AUTOSAR/VAG mirror channel 100 + network_id → physical CAN channel
            new_map[100 + net_id] = phys

        # Fallback channel 99 for cases where CAN buses exist but did not map.
        if 99 not in new_map:
            if first_can_phys is None and can_phys_channels:
                try:
                    first_can_phys = min(int(x) for x in can_phys_channels)
                except Exception:
                    first_can_phys = None
            if first_can_phys is not None:
                new_map[99] = int(first_can_phys)

        # FlexRay: map configured network IDs to physical channels.
        for fr in flexray_buses:
            try:
                if str(fr).strip().upper() == 'A':
                    net_id = 1
                    phys_hint = 0
                elif str(fr).strip().upper() == 'B':
                    net_id = 2
                    phys_hint = 1
                else:
                    net_id = int(fr)
                    phys_hint = int(fr)
            except Exception:
                continue
            try:
                phys = _resolve_phys(phys_hint if phys_hint is not None else net_id, fr_phys_channels, prefer_one_based=True)
            except Exception:
                phys = int(phys_hint if phys_hint is not None else net_id)
            new_map[200 + int(net_id)] = int(phys)
    except Exception:
        pass
    _mirror_channel_map = new_map


# Build initial map from saved config
_rebuild_mirror_channel_map()


# Provide a resolver (bus_type, channel_id) -> source_id
def _resolve_source_id(bus_type: str, channel_id: int):
    try:
        bt = str(bus_type or '').strip().upper()
        cid = int(channel_id)

        # Direct lookup first (physical channels 0-3).
        if bt == 'CAN':
            sid = data_source_manager.find_can_source_by_channel(cid)
            if sid:
                return sid
            # Fallback: mirror virtual channel → physical channel.
            phys = _mirror_channel_map.get(cid)
            if phys is not None:
                sid = data_source_manager.find_can_source_by_channel(int(phys))
                if sid:
                    return sid
            # Channel 99 (Iron Bird / Raw catch-all): the frame could
            # originate from any CAN bus.  Return the first enabled CAN
            # source so the frame at least has *a* source_id and is not
            # silently dropped by ComparisonEngine.
            if cid == 99:
                try:
                    for s in (data_source_manager.list_sources() or []):
                        if isinstance(s, dict) and str(s.get('type', '')).upper() == 'CAN' and s.get('enabled'):
                            return s.get('id')
                except Exception:
                    pass
            return None

        if bt in {'FLEXRAY', 'FLEX', 'FR'}:
            sid = data_source_manager.find_flexray_source_by_channel(cid)
            if sid:
                return sid
            phys = _mirror_channel_map.get(cid)
            if phys is not None:
                return data_source_manager.find_flexray_source_by_channel(int(phys))
            return None

        return None
    except Exception:
        return None

try:
    setattr(manager, 'source_id_resolver', _resolve_source_id)
except Exception:
    pass

# Allow Ethernet packets to drive trigger-started logging when configured.
try:
    setattr(eth_manager, 'trigger_cb', _on_eth_trigger)
except Exception:
    pass

# Listen to decoded CAN frames for CAN message trigger.
try:
    manager.add_listener(_on_can_frame_for_trigger)
except Exception:
    pass

# Listen to decoded CAN frames for KL_15 ignition auto-recording.
try:
    manager.add_listener(_on_can_frame_for_kl15)
except Exception:
    pass

# Listen to decoded CAN frames for comparison rules.
try:
    manager.add_listener(comparison_engine.on_frame)
except Exception:
    pass

# Listen to decoded CAN frames for anomaly scoring.
try:
    manager.add_listener(anomaly_engine.on_frame)
except Exception:
    pass


def _preload_dbcs_from_sources() -> None:
    """Load DBCs for enabled CAN sources so MF4 replay can decode."""
    try:
        sources = data_source_manager.list_sources() or []
    except Exception:
        sources = []

    mappings = []
    seen = set()
    for s in sources:
        try:
            if not isinstance(s, dict):
                continue
            if str(s.get('type') or '').strip().upper() != 'CAN':
                continue
            if not bool(s.get('enabled', True)):
                continue
            cfg = s.get('config') or {}
            ch_id = int(cfg.get('channel_id'))
            dbc_name = str(s.get('dbc_name') or '').strip()
            if not dbc_name or os.path.basename(dbc_name) != dbc_name:
                continue
            dbc_path = os.path.join(UPLOAD_FOLDER_DBC, dbc_name)
            if not os.path.isfile(dbc_path):
                continue
            key = (ch_id, dbc_path)
            if key in seen:
                continue
            seen.add(key)
            mappings.append({'id': ch_id, 'dbc': dbc_path})
        except Exception:
            continue

    if mappings:
        try:
            manager.preload_dbcs(mappings)
        except Exception:
            pass

    # Also load DBCs for mirror virtual channels so that mirror data
    # arriving immediately (autostart) is decoded from the first frame.
    try:
        _load_mirror_dbcs()  # noqa: F821  — defined later but safe at runtime
    except NameError:
        pass  # not yet defined during early module init; autostart will call it later
    except Exception:
        pass


mf4_replay_service = MF4ReplayService(
    bus_manager=manager,
    find_log_file=_find_log_file,
    preload_dbcs=_preload_dbcs_from_sources,
)

# Eagerly load DBCs for all configured sources + mirror channels so that
# live traffic / mirror data is decoded from the very first frame.
# NOTE: the real eager load including mirror channels happens in __main__
# after all functions are defined. This module-level call handles the
# case where the module is imported (e.g. by tests) rather than run directly.
try:
    _preload_dbcs_from_sources()
except Exception:
    pass


copilot_agent = CopilotAgent()
_copilot_snapshot_lock = threading.Lock()
_copilot_snapshot_cache: Dict[str, Any] = {'ts_ms': 0, 'data': {}}


def _sentinel_env_str(name: str, default: str = '') -> str:
    try:
        v = os.getenv(name)
        if v is None:
            return str(default)
        return str(v)
    except Exception:
        return str(default)


def _sentinel_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _sentinel_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _sentinel_system_prompt() -> str:
    return (
        "Sei Sentinel, un assistente diagnostico per DTC (VAG/UDS/OBD).\n"
        "Rispondi in italiano, in modo operativo e verificabile.\n\n"
        "Vincoli:\n"
        "- Non inventare dati specifici del veicolo se non presenti nel contesto JSON.\n"
        "- Se la descrizione PDX è presente, usala come fonte primaria.\n"
        "- Se mancano dettagli (modello motore, ECU, freeze frame), elenca assunzioni e chiedi cosa serve.\n"
        "- Output richiesto: sezioni brevi con bullet, massimo ~25 righe.\n\n"
        "Formato (obbligatorio):\n"
        "1) Sintesi (1-2 righe)\n"
        "2) Possibili cause (3-6 bullet)\n"
        "3) Verifiche consigliate (3-6 bullet, in ordine)\n"
        "4) Rischio/urgenza (bassa/media/alta + perché)\n"
        "5) Note su correlazione (se nel contesto c'è timestamp/odometro o lamp event)\n"
    )


def _sentinel_make_agent() -> CopilotAgent:
    # Separate “channel” for Sentinel: dedicated env vars so Copilot UI tuning doesn't affect it.
    provider = _sentinel_env_str('SENTINEL_PROVIDER', _sentinel_env_str('COPILOT_PROVIDER', 'ollama'))
    base_url = _sentinel_env_str('SENTINEL_BASE_URL', _sentinel_env_str('COPILOT_BASE_URL', 'http://127.0.0.1:11434'))
    model = _sentinel_env_str('SENTINEL_MODEL', _sentinel_env_str('COPILOT_MODEL', 'llama3.2:3b'))
    timeout_s = _sentinel_env_float('SENTINEL_TIMEOUT_S', _sentinel_env_float('COPILOT_TIMEOUT_S', 30.0))
    return CopilotAgent(provider=provider, base_url=base_url, model=model, timeout_s=timeout_s)


sentinel_agent = _sentinel_make_agent()

# Sentinel LLM uses the same single-flight lock by default to avoid saturating CPU-only devices.
# NOTE: bound after Copilot lock initialization below.
_sentinel_llm_lock = None
_sentinel_llm_last_error: Dict[str, Any] = {'ts_ms': 0, 'error': ''}
_sentinel_llm_inflight_since_s: float = 0.0
_sentinel_llm_last_start_s: float = 0.0

# LLM safety: only allow one in-flight Ollama request at a time.
# If a request times out, Ollama may keep computing in the background; we enter a short
# cooldown so follow-up questions fail fast instead of piling up and timing out.
try:
    from llm_singleflight import LLM_SINGLEFLIGHT_LOCK

    _copilot_llm_lock = LLM_SINGLEFLIGHT_LOCK
except Exception:
    _copilot_llm_lock = threading.Lock()
_copilot_llm_cooldown_until_s: float = 0.0
_copilot_llm_last_error: Dict[str, Any] = {'ts_ms': 0, 'error': ''}
_copilot_ollama_last_stop_s: float = 0.0
_copilot_llm_inflight_since_s: float = 0.0
_copilot_llm_last_start_s: float = 0.0

# Bind Sentinel lock to the Copilot single-flight lock (default behavior).
try:
    _sentinel_llm_lock = _copilot_llm_lock
except Exception:
    _sentinel_llm_lock = threading.Lock()


def _copilot_maybe_stop_ollama_model(reason: str) -> None:
    """Best-effort: stop the Ollama model to kill runaway runners after timeouts.

    This prevents a common failure mode where the HTTP client times out but Ollama keeps
    generating in the background, pegging CPU for a long time.

    Controlled via env:
      - COPILOT_OLLAMA_STOP_ON_TIMEOUT: default on
      - COPILOT_OLLAMA_STOP_MIN_INTERVAL_S: default 30s
    """
    try:
        enabled = str(os.getenv('COPILOT_OLLAMA_STOP_ON_TIMEOUT', '1') or '1').strip().lower() in {'1', 'true', 'yes', 'on'}
        if not enabled:
            return
    except Exception:
        return

    now_s = float(time.time())
    try:
        min_interval = float(os.getenv('COPILOT_OLLAMA_STOP_MIN_INTERVAL_S', '30') or 30)
    except Exception:
        min_interval = 30.0

    global _copilot_ollama_last_stop_s
    try:
        last = float(_copilot_ollama_last_stop_s or 0.0)
    except Exception:
        last = 0.0
    if last and (now_s - last) < min_interval:
        return
    _copilot_ollama_last_stop_s = now_s

    model = None
    try:
        model = str(getattr(copilot_agent, 'model', '') or '').strip() or None
    except Exception:
        model = None
    if not model:
        model = str(os.getenv('COPILOT_MODEL', 'llama3.2:3b') or 'llama3.2:3b').strip()

    def _worker():
        try:
            # Avoid hanging request threads: run in background with a hard timeout.
            subprocess.run(['ollama', 'stop', model], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6, check=False)
        except Exception:
            pass

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass


def _copilot_ollama_watchdog_loop() -> None:
    """Background safety loop: stop Ollama if a runner stays pegged.

    Rationale: even if the UI/app times out or answers deterministically, an Ollama runner
    can keep generating in the background, keeping CPU at 100% for minutes.

    Env:
      - COPILOT_OLLAMA_WATCHDOG: default on
      - COPILOT_OLLAMA_WATCHDOG_INTERVAL_S: default 2
      - COPILOT_OLLAMA_RUNNER_CPU_THRESHOLD: default 180 (percent)
      - COPILOT_OLLAMA_RUNNER_GRACE_S: default 8
      - COPILOT_OLLAMA_MAX_INFLIGHT_S: default 25
    """
    try:
        enabled = str(os.getenv('COPILOT_OLLAMA_WATCHDOG', '1') or '1').strip().lower() in {'1', 'true', 'yes', 'on'}
        if not enabled:
            return
    except Exception:
        return

    def _env_f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)) or default)
        except Exception:
            return float(default)

    interval_s = max(0.5, _env_f('COPILOT_OLLAMA_WATCHDOG_INTERVAL_S', 2.0))
    cpu_thresh = max(50.0, _env_f('COPILOT_OLLAMA_RUNNER_CPU_THRESHOLD', 150.0))
    grace_s = max(1.0, _env_f('COPILOT_OLLAMA_RUNNER_GRACE_S', 6.0))
    max_inflight_s = max(5.0, _env_f('COPILOT_OLLAMA_MAX_INFLIGHT_S', 140.0))
    recent_window_s = max(10.0, _env_f('COPILOT_OLLAMA_RECENT_WINDOW_S', 120.0))

    high_since_s: float = 0.0

    while True:
        try:
            time.sleep(interval_s)
        except Exception:
            pass

        # Read runner CPU cheaply.
        runner_cpu = 0.0
        try:
            out = subprocess.check_output(['ps', '-eo', 'pcpu,cmd'], stderr=subprocess.DEVNULL, timeout=1.5)
            txt = out.decode('utf-8', errors='ignore')
            for line in txt.splitlines():
                if 'ollama runner' not in line:
                    continue
                parts = line.strip().split(None, 1)
                if not parts:
                    continue
                try:
                    runner_cpu = max(runner_cpu, float(parts[0]))
                except Exception:
                    continue
        except Exception:
            continue

        now_s = float(time.time())
        if runner_cpu >= cpu_thresh:
            if not high_since_s:
                high_since_s = now_s
        else:
            high_since_s = 0.0
            continue

        # Decide whether to stop.
        locked = False
        try:
            locked = _copilot_llm_lock.locked()
        except Exception:
            locked = False

        # Only intervene if Copilot used LLM recently (avoid killing external Ollama usage).
        recent = False
        try:
            last_start = float(_copilot_llm_last_start_s or 0.0)
            if last_start and (now_s - last_start) <= recent_window_s:
                recent = True
        except Exception:
            pass
        try:
            last_err_ms = int((_copilot_llm_last_error or {}).get('ts_ms') or 0)
            if last_err_ms and (now_s - (last_err_ms / 1000.0)) <= recent_window_s:
                recent = True
        except Exception:
            pass
        if not recent:
            continue

        inflight_age = 0.0
        try:
            inflight_since = float(_copilot_llm_inflight_since_s or 0.0)
            inflight_age = (now_s - inflight_since) if inflight_since else 0.0
        except Exception:
            inflight_age = 0.0

        # Stop if:
        # - runner has been high CPU for grace period AND no request is in-flight (common runaway), OR
        # - request is in-flight too long (protect app responsiveness).
        if (high_since_s and (now_s - high_since_s) >= grace_s and not locked) or (locked and inflight_age >= max_inflight_s):
            _copilot_maybe_stop_ollama_model('watchdog')
            high_since_s = 0.0


def _copilot_build_snapshot() -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)

    # Cache for a short time to avoid heavy DB reads per keystroke.
    with _copilot_snapshot_lock:
        try:
            ts_ms = int(_copilot_snapshot_cache.get('ts_ms') or 0)
        except Exception:
            ts_ms = 0
        if ts_ms > 0 and (now_ms - ts_ms) < 1500:
            cached = _copilot_snapshot_cache.get('data')
            if isinstance(cached, dict) and cached:
                return dict(cached)

    snapshot: Dict[str, Any] = {
        'ts_ms': now_ms,
        'app': {
            'pid': int(os.getpid()),
            'host': str(pysocket.gethostname()),
        },
    }

    try:
        snapshot['logging'] = {
            'active': bool(getattr(shared_logger, 'active', False)),
            'base': str(getattr(shared_logger, 'session_base', '') or ''),
        }
    except Exception:
        snapshot['logging'] = {'active': False}

    try:
        sources = data_source_manager.list_sources() or []
        snapshot['sources'] = {
            'total': int(len(sources)),
            'enabled': int(sum(1 for s in sources if isinstance(s, dict) and bool(s.get('enabled', True)))),
            'items': [
                {
                    'id': s.get('id'),
                    'name': s.get('name'),
                    'type': s.get('type'),
                    'enabled': bool(s.get('enabled', True)),
                    'dbc_name': s.get('dbc_name'),
                }
                for s in sources
                if isinstance(s, dict)
            ][:50],
        }
    except Exception:
        snapshot['sources'] = {'error': 'unavailable'}

    try:
        rules = comparison_engine.list_rules() or []
        snapshot['rules'] = {
            'total': int(len(rules)),
            'enabled': int(sum(1 for r in rules if isinstance(r, dict) and bool(r.get('enabled', True)))),
            'items': [
                {
                    'id': r.get('id'),
                    'name': r.get('name'),
                    'enabled': bool(r.get('enabled', True)),
                    'severity': r.get('severity'),
                    'op': r.get('op'),
                    'a': r.get('a'),
                    'b_kind': r.get('b_kind'),
                    'b': r.get('b'),
                    'b_const': r.get('b_const'),
                    'threshold': r.get('threshold'),
                    'conditions_mode': r.get('conditions_mode'),
                    'conditions': r.get('conditions'),
                }
                for r in rules
                if isinstance(r, dict)
            ][:50],
        }
    except Exception:
        snapshot['rules'] = {'error': 'unavailable'}

    try:
        snapshot['violations'] = {
            'stats_24h': violation_logger.stats_last_24h(),
            'recent': (violation_logger.query(limit=10, offset=0, desc=True) or {}).get('items', []),
        }
    except Exception:
        snapshot['violations'] = {'error': 'unavailable'}

    try:
        snapshot['ai'] = anomaly_engine.status()
    except Exception:
        snapshot['ai'] = {'error': 'unavailable'}

    try:
        snapshot['mf4_replay'] = mf4_replay_service.status()
    except Exception:
        snapshot['mf4_replay'] = {'error': 'unavailable'}

    with _copilot_snapshot_lock:
        _copilot_snapshot_cache['ts_ms'] = now_ms
        _copilot_snapshot_cache['data'] = dict(snapshot)

    return snapshot


def _copilot_system_prompt() -> str:
    return (
        "Sei la guida virtuale di EV-Q Onboard Manager (TRC Project).\n"
        "Rispondi in italiano, in modo operativo e sintetico.\n"
        "Vincolo di forma: massimo 10 punti o 10 step numerati; frasi corte.\n\n"
        "Vincoli:\n"
        "- Non inventare dati: usa SOLO lo snapshot JSON fornito e le info generali dell'app.\n"
        "- Se nello snapshot trovi 'dbc_search', usalo come fonte primaria per rispondere a domande del tipo: 'qual è il nome del segnale che...'.\n"
        "  In quel caso, rispondi con: DBC, Messaggio, Segnale (e breve descrizione se presente).\n"
        "- Non inventare pulsanti/campi: se non sei sicuro del nome, descrivi l'azione genericamente oppure usa SOLO i nomi presenti in ui_actions.\n"
        "- Se manca un dato nello snapshot, chiedi chiarimenti o indica dove vederlo in UI.\n"
        "- Non proporre modifiche distruttive senza conferma esplicita.\n\n"
        "Pagine UI:\n"
        "- /sources: sorgenti CAN + DBC e MF4 replay\n"
        "- /comparison: regole di confronto con condizioni AND/OR\n"
        "- /violations: dashboard violazioni (SQLite+CSV)\n"
        "- /ai: anomaly detection + suggerimenti\n"
        "- /copilot: chat assistente\n\n"
        "Obiettivo: aiutare l'utente a configurare sorgenti, creare regole, interpretare violazioni/anomalie e usare MF4 replay."
    )


def _copilot_build_chat_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact context for LLM prompts.

    Keep this small/structured to reduce prompt-eval latency on CPU.
    """
    if not isinstance(snapshot, dict):
        snapshot = {}

    out: Dict[str, Any] = {
        'ts_ms': snapshot.get('ts_ms'),
        'app': snapshot.get('app') if isinstance(snapshot.get('app'), dict) else {},
        'logging': snapshot.get('logging') if isinstance(snapshot.get('logging'), dict) else {},
    }

    # UI actions cheatsheet (so the assistant references real labels)
    out['ui_actions'] = {
        '/sources': [
            'Refresh',
            '+ Add Source',
            'Export JSON',
            'Import JSON',
            'Preview',
            'Test',
            'MF4 Replay: Refresh files',
            'MF4 Replay: Status',
            'MF4 Replay: Upload',
        ],
        '/comparison': [
            '+ Add Rule',
            'Conditions (AND/OR): add one or more conditions',
            'Save (rule modal)',
        ],
        '/violations': ['Refresh', 'Clear', 'Demo Violation'],
        '/ai': ['Refresh', 'Enabled', 'Train'],
        '/copilot': ['Send', 'Refresh', 'Load snapshot', 'Quick prompts'],
    }

    # Sources summary (keep only a few items)
    src = snapshot.get('sources') if isinstance(snapshot.get('sources'), dict) else {}
    items = src.get('items') if isinstance(src.get('items'), list) else []
    out['sources'] = {
        'total': src.get('total'),
        'enabled': src.get('enabled'),
        'items': [
            {
                'id': (s or {}).get('id'),
                'name': (s or {}).get('name'),
                'type': (s or {}).get('type'),
                'enabled': (s or {}).get('enabled'),
                'dbc_name': (s or {}).get('dbc_name'),
            }
            for s in items[:4]
            if isinstance(s, dict)
        ],
    }

    # Rules summary (a few rules, omit conditions details)
    rr = snapshot.get('rules') if isinstance(snapshot.get('rules'), dict) else {}
    ritems = rr.get('items') if isinstance(rr.get('items'), list) else []
    out['rules'] = {
        'total': rr.get('total'),
        'enabled': rr.get('enabled'),
        'items': [
            {
                'id': (r or {}).get('id'),
                'name': (r or {}).get('name'),
                'enabled': (r or {}).get('enabled'),
                'severity': (r or {}).get('severity'),
                'op': (r or {}).get('op'),
                'a': (r or {}).get('a'),
                'b_kind': (r or {}).get('b_kind'),
                'b': (r or {}).get('b'),
                'b_const': (r or {}).get('b_const'),
                'threshold': (r or {}).get('threshold'),
                'conditions_mode': (r or {}).get('conditions_mode'),
                'conditions_count': len((r or {}).get('conditions') or []) if isinstance((r or {}).get('conditions'), list) else 0,
            }
            for r in ritems[:6]
            if isinstance(r, dict)
        ],
    }

    # Violations summary (stats + last few descriptions)
    vv = snapshot.get('violations') if isinstance(snapshot.get('violations'), dict) else {}
    recent = vv.get('recent') if isinstance(vv.get('recent'), list) else []
    out['violations'] = {
        'stats_24h': vv.get('stats_24h'),
        'recent': [
            {
                'ts_ms': (v or {}).get('ts_ms'),
                'severity': (v or {}).get('severity'),
                'rule_name': (v or {}).get('rule_name'),
                'description': (v or {}).get('description'),
            }
            for v in recent[:5]
            if isinstance(v, dict)
        ],
    }

    # AI summary (omit large lists)
    ai = snapshot.get('ai') if isinstance(snapshot.get('ai'), dict) else {}
    out['ai'] = {
        'ok': ai.get('ok'),
        'enabled': ai.get('enabled'),
        'mode': ai.get('mode'),
        'threshold': ai.get('threshold'),
        'model_trained': ai.get('model_trained'),
        'model_trained_at_ms': ai.get('model_trained_at_ms'),
        'last_scores': ai.get('last_scores'),
        'train': ai.get('train'),
    }

    # MF4 replay summary
    mr = snapshot.get('mf4_replay') if isinstance(snapshot.get('mf4_replay'), dict) else {}
    out['mf4_replay'] = {
        'running': mr.get('running'),
        'file': mr.get('file'),
        'speed': mr.get('speed'),
        'loop': mr.get('loop'),
        'frames_sent': mr.get('frames_sent'),
        'frames_total': mr.get('frames_total'),
        'last_error': mr.get('last_error'),
    }

    return out


def _copilot_should_do_dbc_lookup(user_msg: str) -> bool:
    m = str(user_msg or '').strip().lower()
    if not m:
        return False
    needles = [
        'segnale', 'signal', 'dbc', 'marcia', 'gear', 'prnd', 'fahrstufe', 'gang',
        'veloc', 'speed', 'rpm', 'giri', 'coppia',
    ]
    return any(n in m for n in needles)


def _copilot_extract_search_terms(user_msg: str) -> str:
    import re

    m = str(user_msg or '').lower()
    # Simple keyword extraction: keep alnum/_ tokens, drop common stopwords.
    tokens = re.findall(r"[a-z0-9_]{2,}", m)
    stop = {
        'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'una', 'uno',
        'del', 'della', 'dello', 'dei', 'degli', 'delle',
        'di', 'a', 'da', 'in', 'su', 'per', 'con', 'tra', 'fra',
        'che', 'cos', 'cosa', 'qual', 'quale', 'quali', 'nome',
        'indica', 'indichi', 'indicare', 'mostra', 'mostri', 'sapere',
        'se', 'si', 'no', 'nel', 'nella', 'dentro', 'questo', 'questa',
        'unico', 'una_sola', 'frase',
        # DBC filename / project noise
        'mlbevo', 'ccan', 'dcan', 'hcan', 'kmatrix', 'v8', '00f', '20220602', 'sen', 'vp',
    }
    keep = [t for t in tokens if t not in stop]
    # Add domain synonyms to reduce misses.
    if 'marcia' in m or 'gear' in m:
        keep += ['gangposition', 'fahrstufe', 'getriebe', 'wahlhebel', 'prnd']
    if 'veloc' in m or 'speed' in m:
        keep += ['geschwindigkeit', 'v_signal']
    if 'rpm' in m or 'giri' in m:
        keep += ['nmot', 'motordrehzahl', 'engine', 'drehzahl']
    if 'coppia' in m or 'torque' in m or 'drehmoment' in m or 'moment' in m:
        # Engine/drive torque is usually modeled as "Moment" in these DBCs.
        keep += [
            'mom', 'moment', 'momente', 'drehmoment', 'torque',
            # common qualifiers
            'ist', 'soll', 'anforderung', 'wunsch', 'req', 'requested', 'actual',
            # engine prefix hints
            'mo_',
        ]
        # Common concrete signal tokens found in VAG/MQB style DBCs.
        if any(k in m for k in ['reale', 'attuale', 'actual', 'ist', 'istmoment']):
            keep += ['istmoment', 'vkm']
        if any(k in m for k in ['richiest', 'target', 'obiettivo', 'requested', 'req', 'soll', 'wunsch']):
            keep += ['eingriffsmoment', 'momentanforderung', 'sollmoment']

    # De-dup while preserving order.
    out = []
    seen = set()
    for t in keep:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)

    # For torque queries, aggressively prioritize a few strong tokens so they
    # survive the term cap and surface the right signals quickly.
    if 'coppia' in m or 'torque' in m or 'drehmoment' in m or 'moment' in m:
        prio: list[str] = []
        if any(k in m for k in ['reale', 'attuale', 'actual', 'ist', 'misurat', 'istmoment']):
            prio += ['mo_istmoment_vkm', 'istmoment', 'vkm']
        if any(k in m for k in ['richiest', 'target', 'obiettivo', 'requested', 'req', 'soll', 'wunsch', 'comand', 'setpoint', 'desider']):
            prio += ['br_eingriffsmoment', 'eingriffsmoment', 'momentanforderung', 'sollmoment']
        prio += ['mo_', 'moment', 'torque', 'drehmoment']

        # De-dup prio while preserving order.
        prio_out: list[str] = []
        prio_seen = set()
        for p in prio:
            p = str(p or '').strip().lower()
            if not p or p in prio_seen:
                continue
            prio_seen.add(p)
            prio_out.append(p)

        out = prio_out + [t for t in out if t not in prio_seen]

    return ' '.join(out[:10])


def _copilot_is_signal_name_question(user_msg: str) -> bool:
    m = str(user_msg or '').strip().lower()
    if not m:
        return False
    # Typical intents: "nome del segnale", "quale segnale indica", etc.
    patterns = [
        'nome del segnale',
        'quale segnale',
        'quali segnali',
        'che segnale',
        'signal name',
        'nome segnale',
        'indic',
    ]
    # Accept both singular/plural: segnale/segnali.
    if any(p in m for p in patterns) and ('segnal' in m or 'signal' in m or 'dbc' in m):
        return True
    # Also treat short direct queries as lookup.
    if ('marcia' in m or 'gear' in m) and ('segnal' in m or 'dbc' in m or 'indic' in m):
        return True
    # Torque lookups are commonly asked without the word "segnale".
    if ('coppia' in m or 'torque' in m or 'drehmoment' in m or 'moment' in m) and ('motore' in m or 'engine' in m):
        return True
    return False


def _copilot_is_ai_config_question(user_msg: str) -> bool:
    m = str(user_msg or '').strip().lower()
    if not m:
        return False
    if ' ai' not in f' {m} ' and 'anomal' not in m:
        return False

    patterns = [
        # Match many Italian conjugations (configuro/configuri/configurare/configurazione...)
        'configur',
        'regola ai', 'regole ai',
        'anomaly', 'anomalia', 'anomalie',
        # Many users ask specifically about turning AI anomalies into violations.
        'violation', 'violazioni',
        'anomaly → violation', 'anomaly->violation',
        'threshold', 'soglia',
        'train', 'training', 'addestra', 'addestrare',
        'vector', 'compare', 'confronto',
        '/ai',
    ]
    return any(p in m for p in patterns)


def _copilot_is_violation_config_question(user_msg: str) -> bool:
    m = str(user_msg or '').strip().lower()
    if not m:
        return False
    # If user is already covered by AI config path, let that handler answer.
    if _copilot_is_ai_config_question(m):
        return False

    # Common intents: "come configuro una violation", "come creare una violazione/regola", etc.
    has_violation_word = ('violation' in m) or ('violaz' in m)
    has_how = any(k in m for k in ('come', 'how', 'configur', 'impost', 'crea', 'aggiung', 'setup'))
    if has_violation_word and has_how:
        return True

    # Also accept explicit page mention.
    if '/comparison' in m and any(k in m for k in ('violation', 'violaz', 'regola', 'rule')):
        return True
    return False


def _copilot_build_violation_config_answer(snapshot: Dict[str, Any] | None = None) -> str:
    """Deterministic step-by-step guide for configuring violations via comparison rules."""
    enabled_rules = None
    total_rules = None
    try:
        if isinstance(snapshot, dict):
            rules = snapshot.get('comparison_rules')
            if isinstance(rules, list):
                total_rules = len(rules)
                enabled_rules = sum(1 for r in rules if isinstance(r, dict) and r.get('enabled'))
    except Exception:
        pass

    lines = [
        "Configurare una violation (regola) — guida rapida e completa:",
        "",
        "La pagina giusta è `/comparison` (Rules): lì definisci le regole che, quando vere, generano una riga in `/violations`.",
        "",
        "1) Apri `/comparison` e crea (o modifica) una regola",
        "- Premi `Add rule` (o seleziona una regola esistente).",
        "- Dai un `Name` chiaro (es. `speed_high`).",
        "- Metti `Enabled` su ON.",
        "",
        "2) Scegli il segnale A (quello da monitorare)",
        "- In `A` seleziona: `Source` → `Message` → `Signal`.",
        "",
        "3) Scegli l’operatore e la soglia",
        "- `Op`: es. `ge` (>=), `gt` (>), `le` (<=), `lt` (<), `eq` (=), `ne` (!=).",
        "- Se vuoi confrontare contro un valore fisso: imposta `B kind = const` e inserisci `B const`.",
        "- Se vuoi confrontare contro un altro segnale: imposta `B kind = signal` e seleziona il segnale B.",
        "",
        "4) (Opzionale) Condizioni aggiuntive AND/OR",
        "- Aggiungi una o più `Conditions` (ognuna è un confronto extra).",
        "- Imposta `Conditions mode` su `and` oppure `or`.",
        "",
        "5) Antirimbalzo e dati mancanti",
        "- `Debounce (s)`: quanto la condizione deve restare vera prima di creare la violation (es. 0.5–2.0s).",
        "- `Missing timeout (s)`: dopo quanto un segnale mancante viene considerato “non valido”.",
        "",
        "6) Azioni della regola",
        "- Lascia attive le azioni utili: `emit_ws` (live in UI) e logging (CSV/DB) se ti serve storico.",
        "",
        "7) Salva e verifica",
        "- Premi `Save`/`Update`.",
        "- Vai su `/violations`: dovresti vedere eventi nel `Live feed` (WS) e nello storico quando la condizione scatta.",
        "",
        "Debug veloce se non vedi violazioni:",
        "- Controlla che il DBC della sorgente sia caricato e che il segnale cambi davvero.",
        "- Prova una soglia facilissima (es. `>= 0`) per vedere se almeno scatta.",
        "- Usa `Demo Violation` in `/violations` per testare la pipeline end-to-end.",
        "",
        "API utili:",
        "- `GET /api/comparison/rules` (lista regole)",
        "- `PUT /api/comparison/rules` (salva regole)",
        "- `GET /api/violations` (storico)",
        "- `GET /api/violations/statistics` (conteggi 24h)",
    ]

    if total_rules is not None and enabled_rules is not None:
        lines.append("")
        lines.append(f"Snapshot: regole abilitate={enabled_rules}/{total_rules}")

    return "\n".join(lines).strip()


def _copilot_is_rules_status_question(user_msg: str) -> bool:
    m = str(user_msg or '').strip().lower()
    if not m:
        return False
    if not any(k in m for k in ('regola', 'regole', 'rule', 'rules')):
        return False
    if any(k in m for k in ('crea', 'creami', 'aggiungi', 'imposta', 'create', 'add')):
        return False
    return any(k in m for k in ('attiva', 'attive', 'abilitata', 'abilitate', 'snapshot', 'configurata', 'configurate', 'risulta', 'risultano'))


def _copilot_build_rules_status_answer(snapshot: Dict[str, Any] | None = None) -> str:
    if not isinstance(snapshot, dict):
        return ''
    rules = snapshot.get('rules') if isinstance(snapshot.get('rules'), dict) else {}
    items = rules.get('items') if isinstance(rules.get('items'), list) else []
    if not items:
        total = rules.get('total')
        enabled = rules.get('enabled')
        if total is not None or enabled is not None:
            return f"Snapshot regole: abilitate={enabled or 0}/{total or 0}. Nessuna regola visibile nei dettagli dello snapshot."
        return 'Nello snapshot non risultano regole di confronto configurate.'

    lines = [
        f"Snapshot regole: abilitate={rules.get('enabled', 0)}/{rules.get('total', len(items))}.",
    ]
    for rule in items:
        if not isinstance(rule, dict) or not bool(rule.get('enabled')):
            continue
        name = str(rule.get('name') or rule.get('id') or 'regola').strip()
        a = rule.get('a') if isinstance(rule.get('a'), dict) else {}
        msg = str(a.get('message') or '').strip()
        sig = str(a.get('signal') or '').strip()
        op = str(rule.get('op') or '').strip()
        b_kind = str(rule.get('b_kind') or '').strip()
        b_const = rule.get('b_const')
        threshold = rule.get('threshold')

        target = ''
        if b_kind == 'const' and b_const is not None:
            target = f" vs const {b_const}"
        elif b_kind:
            target = f" vs {b_kind}"
        signal_ref = f"{msg}.{sig}" if msg and sig else (sig or msg or 'segnale non specificato')
        lines.append(f"- `{name}`: {signal_ref} {op}{target}; threshold={threshold}.")

    if len(lines) == 1:
        lines.append('- Nessuna regola abilitata tra quelle incluse nello snapshot compatto.')
    return '\n'.join(lines).strip()


def _copilot_faq_target(user_msg: str) -> str | None:
    """Return a deterministic FAQ key for common troubleshooting/how-to questions."""
    m = str(user_msg or '').strip().lower()
    if not m:
        return None

    def has(*words: str) -> bool:
        return any(w in m for w in words)

    # Violations troubleshooting
    if has('non vedo violaz', 'nessuna violaz', 'perché non vedo violaz', 'debug violaz', 'no violations'):
        return 'violations_debug'

    # MF4 replay / testing without vehicle
    if has('mf4 replay', 'replay mf4', 'test senza', 'senza veicolo', 'offline replay', 'riprodu', 'playback'):
        return 'mf4_replay'

    # Sources / CAN setup
    if (has('configur', 'impost', 'setup') and has('sorgent', 'source', 'can0', 'can1', 'bitrate', '/sources')):
        return 'sources_setup'

    # CAN interface / Kvaser driver troubleshooting
    if has('kvaser', 'canlib', 'python-canlib', 'canlib not available', 'no can channels', 'found 0 channels', 'bus off', 'error passive', 'error warning', 'canstat', 'check_can_status'):
        return 'can_interface_debug'
    if has('configur', 'impost', 'setup') and has('interfaccia can', 'can interface', 'driver can', 'kvaser'):
        return 'can_interface_setup'

    # DBC loading
    if (has('dbc') and has('caric', 'selezion', 'scegli', 'assoc', 'load', 'import')):
        return 'dbc_setup'

    # DBC upload / import into catalog DB
    if has('dbc') and has('upload', 'carica file', 'caricare file', 'aggiungere dbc', 'mettere dbc', '/api/upload_dbc'):
        return 'dbc_upload'
    if has('dbc') and has('catalogo', 'catalog', 'indicizz', 'importa nel catalogo', '/dbc_catalog', '/api/dbc/import'):
        return 'dbc_catalog_import'

    # DBC concept
    if 'dbc' in m and any(k in m for k in ('cosa sono', "cos'e", "cos’è", 'che cosa', 'a cosa serve', 'spieg')):
        return 'dbc_explain'

    # CAN concept
    if any(k in m for k in ('can bus', 'bus can', 'rete can')) and any(k in m for k in ('cosa', "cos'e", "cos’è", 'spieg', 'a cosa serve')):
        return 'can_explain'

    # Rule examples (speed/rpm/gear/torque + debounce/threshold)
    if ('regola' in m or 'rules' in m) and any(k in m for k in ('rpm', 'giri', 'veloc', 'km/h', 'marcia', 'gear', 'coppia', 'torque', 'debounce', 'soglia', 'threshold', 'isteresi', 'hysteresis')):
        if any(k in m for k in ('esempio', 'esempi', 'come', 'operator', 'op', 'soglia', 'debounce')):
            return 'rule_examples'

    # DoIP setup
    if has('doip', 'do-ip', 'data over ip', 'iso-13400', 'iso 13400', 'gateway doip', 'scan doip'):
        if has('abilit', 'attiv', 'configur', 'impost', 'setup', 'connet', 'connession', 'discover', 'discovering'):
            return 'doip_setup'
        if has('non funz', 'errore', 'timeout', 'non trova', 'no gateway', 'discover fall', 'non si connette'):
            return 'doip_debug'

    # Simulation / testing without vehicle
    if has('simula', 'simulare', 'traffic', 'traffico', 'senza veicolo', 'dev', 'test rapido', 'random_can'):
        return 'simulation'

    # Websocket issues
    if has('ws:', 'websocket', 'socket', 'disconnected', 'non si aggiorna', 'non aggiorna'):
        return 'ws_debug'

    # Logging formats
    if has('csv', 'mf4', 'log', 'registr', 'record', 'salva', 'session_'):
        if has('come', 'how', 'configur', 'impost', 'dove'):
            return 'logging'

    # Start/stop logging explicitly
    if has('avvia log', 'start log', 'ferma log', 'stop log', '/api/log/start', '/api/log/stop', 'log status', '/api/log/status'):
        return 'logging_control'

    # Where are files stored
    if has('dove trovo', 'dove finis', 'percorso', 'path', 'cartella', 'directory') and has('log', 'logs', 'mf4', 'csv', 'session_', 'violaz'):
        return 'file_locations'

    # First install / first run checklist
    if has('prima install', 'installazione', 'primo avvio', 'first run', 'setup iniziale', 'checklist', 'getting started', 'start here'):
        return 'first_run'

    # Full rules how-to
    if has('come creo', 'come creare', 'come faccio', 'how do i', 'guida') and has('regola', 'rules', '/comparison'):
        return 'rules_full'

    return None


def _copilot_build_faq_answer(key: str, snapshot: Dict[str, Any] | None = None) -> str:
    k = str(key or '').strip().lower()

    if k == 'violations_debug':
        return "\n".join([
            "Perché non vedo violazioni? — checklist deterministica:",
            "",
            "1) Verifica che esista almeno una regola abilitata",
            "- Vai su `/comparison`.",
            "- Controlla `Enabled`=ON e che la regola abbia `A` configurato (Source/Message/Signal).",
            "",
            "2) Verifica che il segnale A stia davvero aggiornando",
            "- Vai su `/sources` e controlla che la sorgente sia `Enabled` e con DBC selezionato.",
            "- Se il DBC non è caricato, i segnali non possono decodificare.",
            "",
            "3) Semplifica la condizione per un test veloce",
            "- Imposta una regola facile: es. `A >= 0` con `Debounce (s)=0`.",
            "- Oppure usa un `B const` vicino al valore reale.",
            "",
            "4) Controlla la pagina `/violations`",
            "- Guarda `WS:` (deve essere connected).",
            "- Premi `Refresh` e prova `Apply` senza filtri.",
            "",
            "5) Test end-to-end",
            "- Premi `Demo Violation` su `/violations` (solo localhost): se compare nel live feed/storico, la pipeline funziona.",
            "",
            "API utili:",
            "- `GET /api/comparison/rules` (regole)",
            "- `GET /api/violations` e `GET /api/violations/statistics` (storico/statistiche)",
            "- `GET /api/system/stats` (carico/CPU)",
        ]).strip()

    if k == 'mf4_replay':
        return "\n".join([
            "MF4 Replay — come testare regole senza veicolo (deterministico):",
            "",
            "Obiettivo: riprodurre un file `.mf4` e reiniettare i frame nel pipeline CAN per far scattare regole e violations.",
            "",
            "1) Prepara una sorgente CAN con DBC",
            "- Vai su `/sources`.",
            "- Su CAN0 (o la sorgente che usi per il replay) seleziona il DBC corretto.",
            "",
            "2) Configura il replay",
            "- Vai nella sezione MF4 Replay (se presente nella Home / pagina dedicata nel tuo build).",
            "- Scegli `filename` (es. `session_....mf4`).",
            "- Imposta `speed` e `loop` se vuoi ripetere.",
            "- Se il log contiene più canali, scegli `channel_mode`/`force_channel`.",
            "",
            "3) Avvia il replay e verifica i segnali",
            "- Avvia e controlla che i contatori/valori (es. in `/comparison` o dashboard) si muovano.",
            "",
            "4) Verifica violazioni",
            "- Vai su `/violations` per live feed e storico.",
            "",
            "API utili (se le usi via script):",
            "- Stato: `GET /api/mf4_replay/status` (se disponibile)",
            "- Config: `GET/PUT /api/config` → `mf4_replay` (se usi config store)",
        ]).strip()

    if k == 'sources_setup':
        return "\n".join([
            "Configurare una sorgente CAN (Sources) — guida deterministica:",
            "",
            "1) Apri `/sources`",
            "- Assicurati che la sorgente (es. `CAN0`) sia `Enabled`.",
            "",
            "2) Imposta parametri CAN",
            "- `Channel ID`: 0 per CAN0, 1 per CAN1, ...",
            "- `Bitrate`: tipicamente 500000 (dipende dal veicolo).",
            "- `CAN FD`: ON solo se il bus è FD.",
            "",
            "3) Associa un DBC",
            "- Seleziona il `DBC name` per abilitare decodifica messaggi/segnali.",
            "",
            "4) Salva",
            "- Premi `Save`/`Update` e poi verifica che la sorgente produca dati.",
            "",
            "Se non vedi segnali:",
            "- Controlla connessione hardware / driver Kvaser.",
            "- Verifica bitrate corretto.",
            "- Verifica che il DBC sia quello giusto per quel bus (CCAN/HCAN/DCAN).",
        ]).strip()

    if k == 'dbc_setup':
        return "\n".join([
            "DBC — come caricarlo/usarli correttamente (deterministico):",
            "",
            "1) Seleziona il DBC nella sorgente",
            "- Vai su `/sources`.",
            "- In `DBC name` scegli il file corretto (CCAN/HCAN/DCAN).",
            "- Salva.",
            "",
            "2) Verifica il contenuto",
            "- Apri `/dbc_catalog` per vedere messaggi + segnali + descrizioni.",
            "",
            "3) Usa il DBC per creare regole",
            "- Vai su `/comparison` e seleziona `Source → Message → Signal`.",
            "",
            "API utile:",
            "- Ricerca deterministica: `GET /api/dbc/search_db?dbc_name=...&q=...`",
        ]).strip()

    if k == 'dbc_upload':
        return "\n".join([
            "Upload DBC — dove caricarli e come verificarli (deterministico):",
            "",
            "Opzione A) UI (consigliata)",
            "- Vai su `/` (Home) → sezione `Databases` → `Upload DBC`.",
            "- Seleziona uno o più file `.dbc`.",
            "- Al termine, apri `/sources` e scegli il DBC nel campo `DBC` della sorgente.",
            "",
            "Opzione B) API",
            "- `POST /api/upload_dbc` (multipart form-data, campo `file`).",
            "",
            "Dove finiscono i file:",
            "- La directory è `kvaser_bus_manager/databases/dbc/` (backend: `UPLOAD_FOLDER_DBC`).",
            "",
            "Verifica rapida:",
            "- `GET /api/dbcs` (lista DBC disponibili)",
            "- `/dbc_catalog` per esplorare messaggi e segnali",
            "- `GET /api/dbc/describe?dbc_name=...` per un riassunto",
        ]).strip()

    if k == 'dbc_catalog_import':
        return "\n".join([
            "Catalogo DBC (DB indicizzato) — come importare e usare la ricerca (deterministico):",
            "",
            "Per avere ricerca veloce per segnali/messaggi (usata anche da Copilot), importa i DBC nel catalogo SQLite.",
            "",
            "1) Carica i DBC (se non ci sono)",
            "- Usa `Upload DBC` nella Home oppure `POST /api/upload_dbc`.",
            "",
            "2) Import/indicizzazione",
            "- `POST /api/dbc/import` per importare i DBC presenti in `databases/dbc`.",
            "- Poi vai su `/dbc_catalog` e premi `Refresh`/ricarica la pagina.",
            "",
            "3) Ricerca",
            "- `GET /api/dbc/search_db?dbc_name=<file.dbc>&q=<termine>`.",
            "",
            "Nota:",
            "- Se cambi DBC o ne aggiungi di nuovi, ripeti l’import per aggiornare l’indice.",
        ]).strip()

    if k == 'can_interface_setup':
        return "\n".join([
            "Configurare l’interfaccia CAN (Kvaser) — guida pratica (deterministica):",
            "",
            "1) Driver Kvaser",
            "- Assicurati di aver installato i driver Kvaser e che `python-canlib` sia disponibile nell’ambiente Python del servizio.",
            "",
            "2) Verifica che il sistema veda i canali",
            "- Esegui lo script: `python3 kvaser_bus_manager/check_can_status.py`.",
            "- Devi vedere `Found N channels` con N>0.",
            "",
            "3) Configura la sorgente in UI",
            "- Vai su `/sources` → `+ Add Source` (o modifica una sorgente CAN).",
            "- Imposta `CAN Channel` (0..3) e `Bitrate` (es. 500k).",
            "- Se il bus è CAN-FD, abilita `CAN-FD`.",
            "- Seleziona il DBC nel campo `DBC`.",
            "",
            "4) Test veloce",
            "- In `/sources` usa `Test Connection (5s)` per verificare che arrivino frame/decoding.",
            "",
            "Se non funziona:",
            "- Se `canlib not available`: mancano driver o libreria nel venv del servizio.",
            "- Se `Found 0 channels`: hardware non visto (USB/cavo/permessi).",
            "- Se `BUS OFF`: bitrate errato o bus non cablato correttamente.",
        ]).strip()

    if k == 'can_interface_debug':
        return "\n".join([
            "CAN non funziona / canali non visibili — debug deterministico:",
            "",
            "1) Controllo driver Python",
            "- Se compare `canlib not available`, il backend non riesce a importare `canlib.canlib`.",
            "- Verifica che i driver Kvaser e `python-canlib` siano installati nell’ambiente che esegue `kvbm.service`.",
            "",
            "2) Controllo canali",
            "- `python3 kvaser_bus_manager/check_can_status.py` → deve mostrare almeno 1 canale.",
            "",
            "3) Stato bus",
            "- Se `BUS OFF` o `ERROR PASSIVE/WARNING`: quasi sempre bitrate sbagliato o problemi di cablaggio/terminazione.",
            "",
            "4) App-level",
            "- `/sources`: channel/bitrate/FD corretti + DBC selezionato.",
            "- `/api/system/stats`: se CPU è al 100% potrebbe perdere frame.",
        ]).strip()

    if k == 'doip_setup':
        return "\n".join([
            "Abilitare DoIP (Data Over IP / ISO-13400) — guida deterministica:",
            "",
            "1) Apri la pagina DoIP",
            "- Vai su `/doip`.",
            "",
            "2) Seleziona l’interfaccia veicolo",
            "- `Interface`: tipicamente `eth0` (rete veicolo).",
            "- Lascia `wlan0` per internet (di solito deve restare la default route).",
            "",
            "3) Target IP",
            "- Lascia vuoto per auto-discovery (consigliato).",
            "- Se lo conosci, inserisci l’IP del gateway (IPv6 link-local tipo `fe80::...%eth0` o IPv4 `169.254.x.x`).",
            "",
            "4) Abilita DoIP",
            "- Attiva `DoIP enabled`.",
            "- Se `Target IP` è vuoto, lascia ON `Auto-discover gateway`.",
            "- `Tester Logical Addr`: default `0x0E00` va bene nella maggior parte dei casi.",
            "",
            "5) Salva e verifica",
            "- Premi `Save`.",
            "- Premi `Discover` per popolare l’IP se necessario.",
            "- Premi `Refresh Status` per controllare IP e route.",
            "",
            "API equivalenti:",
            "- `GET/POST /api/doip/config`",
            "- `POST /api/doip/discover`",
            "- `GET /api/doip/status`",
        ]).strip()

    if k == 'doip_debug':
        return "\n".join([
            "DoIP non si connette / discovery non trova gateway — debug deterministico:",
            "",
            "1) Verifica rete su `/doip` → `Network Status`",
            "- `eth0` deve avere un IP (IPv4 o IPv6 link-local).",
            "- La default route dovrebbe restare su `wlan0` (non su `eth0`).",
            "",
            "2) Prova discovery manuale",
            "- In `/doip` premi `Discover`.",
            "- Oppure `POST /api/doip/discover` con `iface=eth0`.",
            "",
            "3) Se hai un IP gateway noto",
            "- Compila `Target IP` e salva.",
            "- Per IPv6 link-local ricorda lo scope: `fe80::...%eth0`.",
            "",
            "4) Se una scan DoIP fallisce",
            "- Controlla che `DoIP enabled` sia ON (stack Ethernet).",
            "- Aumenta timeout lato tool/scan se necessario.",
        ]).strip()

    if k == 'simulation':
        return "\n".join([
            "Simulare dati senza veicolo — opzioni rapide (deterministico):",
            "",
            "Opzione A) ECU simulata (per ScanTools/OBD demo)",
            "- Avvia il backend con env `KBSM_SIM_ECU=1`.",
            "- Questo abilita risposte OBD/TesterPresent simulate in `BusManager`.",
            "",
            "Opzione B) Script di traffico CAN",
            "- Usa gli script in `kvaser_bus_manager/scripts/` (es. `random_can_traffic_1min.py`, `dbc_can_simulator.py`).",
            "- Carica un DBC e usa `/comparison` + `/violations` per verificare end-to-end.",
            "",
            "Opzione C) MF4 Replay",
            "- Vedi FAQ `MF4 Replay` per riprodurre un `.mf4` e testare le regole offline.",
        ]).strip()

    if k == 'dbc_explain':
        return "\n".join([
            "Cosa sono i DBC (CAN database) — spiegazione deterministica:",
            "",
            "Un file `.dbc` è un ‘dizionario’ del traffico CAN: descrive come interpretare gli ID dei messaggi e come trasformare i byte dei frame in segnali fisici (con nome, unità, scala, offset, limiti, descrizioni).",
            "",
            "In pratica un DBC contiene:",
            "- **Messaggi**: nome + CAN ID (es. `Motor_14` a `0x3BE`).",
            "- **Segnali**: campi dentro al messaggio (bit start/length, endianness, signed/unsigned).",
            "- **Conversione fisica**: `value = raw * factor + offset` e unità (km/h, rpm, Nm...).",
            "- **Enum/valori**: mapping di stati (es. marcia, modalità) e commenti/descrizioni.",
            "",
            "Come lo usa questa app:",
            "- Se associ un DBC a una sorgente in `/sources`, l’app può **decodificare** i frame in segnali leggibili.",
            "- In `/comparison` puoi creare regole scegliendo `Source → Message → Signal` (questo dipende dal DBC).",
            "- In `/dbc_catalog` puoi esplorare messaggi/segnali e descrizioni.",
            "- Copilot fa ricerche deterministiche nel catalogo (`/api/dbc/search_db`) per suggerire i segnali corretti.",
        ]).strip()

    if k == 'can_explain':
        return "\n".join([
            "Cos’è il CAN bus — spiegazione deterministica:",
            "",
            "CAN (Controller Area Network) è un bus seriale usato in automotive per scambiare messaggi brevi e robusti tra centraline (ECU).",
            "",
            "Caratteristiche chiave:",
            "- È un bus **broadcast**: i frame vanno a tutti, e ogni ECU filtra quelli che le interessano.",
            "- Ogni frame ha un **ID** (priorità + significato) e fino a 8 byte dati (CAN classico) o più in CAN FD.",
            "- Il significato dei byte non è standard: serve un **DBC** per decodificarli in segnali (rpm, velocità, marcia, ecc.).",
            "",
            "In questa app:",
            "- `/sources` configura interfacce e bitrate.",
            "- Il DBC abilita la decodifica.",
            "- `/comparison` crea regole; `/violations` mostra gli eventi.",
        ]).strip()

    if k == 'ws_debug':
        return "\n".join([
            "WS: disconnected / UI non aggiorna — debug deterministico:",
            "",
            "1) Verifica che il backend sia su 5000",
            "- Apri `/api/config` (deve rispondere).",
            "",
            "2) Controlla badge `WS:`",
            "- Su `/violations` e `/ai` deve passare a `connected`.",
            "",
            "3) Se sei dietro proxy/reverse-proxy",
            "- Assicurati che WebSocket/Socket.IO siano permessi.",
            "",
            "4) Riavvio rapido",
            "- `sudo systemctl restart kvbm.service`.",
        ]).strip()

    if k == 'logging':
        return "\n".join([
            "Logging (CSV/MF4) — dove si configura e come verificare (deterministico):",
            "",
            "1) Scegli i formati",
            "- Nelle configurazioni (Home/Settings) imposta i formati default: `csv`, `mf4`.",
            "- Alcuni trigger possono avere formati separati (es. yolo/custom/can/eth).",
            "",
            "2) Verifica lo stato logger",
            "- Usa `GET /api/log/status`.",
            "",
            "3) Dove finiscono i file",
            "- I file `session_...` vengono salvati sotto `logs/` e/o `logs/monitor/` a seconda del tipo.",
            "- Le violations possono avere anche CSV giornalieri `violations_YYYYMMDD.csv`.",
            "",
            "4) Test rapido",
            "- Avvia un evento (regola o demo) e verifica che compaiano nuovi file o righe in DB/dashboard.",
        ]).strip()

    if k == 'logging_control':
        return "\n".join([
            "Avviare/Fermare logging — comandi e verifica (deterministico):",
            "",
            "UI (consigliata):",
            "- Dalla Home usa i pulsanti Start/Stop logging (se presenti nel tuo build).",
            "",
            "API:",
            "- Avvio: `POST /api/log/start`",
            "- Stato: `GET /api/log/status`",
            "- Stop: `POST /api/log/stop`",
            "",
            "Gestione file:",
            "- Lista: `GET /api/logs`",
            "- Download: `GET /api/logs/<filename>`",
            "- Cancella singolo: `DELETE /api/logs/<filename>`",
            "- Cancella tutto: `DELETE /api/logs`",
            "",
            "Debug rapido:",
            "- Se lo stato non cambia, controlla `/api/health` e `sudo systemctl status kvbm.service`.",
        ]).strip()

    if k == 'file_locations':
        return "\n".join([
            "Dove finiscono file e output (logs/MF4/CSV/violations) — guida deterministica:",
            "",
            "Percorsi tipici nel progetto:",
            "- Log directory: `kvaser_bus_manager/logs/`",
            "- Log monitor/violations: `kvaser_bus_manager/logs/monitor/`",
            "- DBC caricati: `kvaser_bus_manager/databases/dbc/`",
            "",
            "Come verificarlo via API:",
            "- `GET /api/health` (include `log_dir` e spazio disco)",
            "- `GET /api/logs` (lista file log esposti dalla UI)",
            "- `GET /api/mf4/files` (lista MF4 disponibili)",
            "",
            "Tip pratici:",
            "- Se carichi un `.dbc`, poi lo selezioni in `/sources` → campo DBC.",
            "- Se fai MF4 Replay, i file vengono scelti dalla lista in `/sources` → sezione MF4 Replay (e/o da `GET /api/mf4/files`).",
        ]).strip()

    if k == 'first_run':
        return "\n".join([
            "Primo avvio (checklist) — TRC Onboard / EV-Q Onboard Manager:",
            "",
            "1) Servizio up",
            "- Apri `GET /api/health` (deve tornare `ok=true`).",
            "",
            "2) Carica DBC",
            "- Home → `Databases` → `Upload DBC` (oppure `POST /api/upload_dbc`).",
            "- (Opzionale) Importa nel catalogo: `POST /api/dbc/import` e verifica in `/dbc_catalog`.",
            "",
            "3) Configura sorgente CAN",
            "- Vai su `/sources` → `+ Add Source`.",
            "- Imposta `CAN Channel`, `Bitrate`, (eventuale) `CAN-FD`, e seleziona un DBC.",
            "- Premi `Test Connection (5s)`.",
            "",
            "4) Crea una regola",
            "- Vai su `/comparison` e crea una regola semplice (es. velocità > soglia).",
            "",
            "5) Verifica violations",
            "- Vai su `/violations` e controlla `WS: connected` + storico.",
            "",
            "6) (Opzionale) Logging",
            "- Avvia con `POST /api/log/start` e controlla `GET /api/log/status`.",
            "",
            "7) (Opzionale) DoIP",
            "- Vai su `/doip`, lascia Target IP vuoto e usa `Discover`.",
        ]).strip()

    if k == 'rules_full':
        return "\n".join([
            "Guida completa regole (Comparison Rules) — come creare una violation affidabile:",
            "",
            "1) Pre-requisiti",
            "- In `/sources` la sorgente CAN deve essere `Enabled` e con DBC selezionato.",
            "",
            "2) Crea la regola in `/comparison`",
            "- `Name`: descrittivo (es. `speed_over_140`).",
            "- `Enabled`: ON.",
            "- `A`: seleziona `Source → Message → Signal`.",
            "- `Op`: `gt/ge/lt/le/eq/ne`.",
            "- `B kind`:",
            "  - `const`: confronto con soglia fissa (`B const`).",
            "  - `signal`: confronto con un secondo segnale B.",
            "",
            "3) Condizioni multiple AND/OR",
            "- Aggiungi `Conditions` (ogni condition è un confronto extra).",
            "- Imposta `Conditions mode` su `and` o `or`.",
            "",
            "4) Stabilità e dati mancanti",
            "- `Debounce (s)`: evita falsi positivi su spike (tipico 0.5–2s).",
            "- `Missing timeout (s)`: definisce quando un segnale assente invalida la regola.",
            "",
            "5) Verifica in `/violations`",
            "- Controlla `WS: connected` e usa `Refresh` + `Apply`.",
            "- Se non vedi nulla, prova temporaneamente una soglia facile (es. `>= 0`) + debounce 0.",
        ]).strip()

    if k == 'rule_examples':
        return _copilot_build_rule_examples_answer()

    return ''


def _copilot_page_help_target(user_msg: str) -> str | None:
    """Return a known page path (e.g. '/violations') if the user is asking what a page does."""
    m = str(user_msg or '').strip().lower()
    if not m:
        return None

    # If the user mentions the path explicitly, it's usually a page-help question.
    if '/violations' in m:
        return '/violations'

    # Also accept plain 'violations' when paired with 'pagina' / 'cosa fa' intents.
    help_intents = ('cosa fa', 'a cosa serve', 'spiegami', 'spiega', 'descrivi', 'come funziona', 'pagina')
    if 'violations' in m or 'violazioni' in m:
        if any(k in m for k in help_intents):
            return '/violations'
    return None


def _copilot_build_page_help_answer(page: str, snapshot: Dict[str, Any] | None = None) -> str:
    p = str(page or '').strip().lower()
    if p != '/violations':
        return ''

    lines = [
        "Pagina `/violations` — cosa fa (spiegazione deterministica):",
        "",
        "Obiettivo:",
        "- Mostra le violazioni generate dalle regole di confronto (pagina `/comparison`) e, se attivo, anche quelle create dall’AI (pagina `/ai`).",
        "- Ti permette di vedere eventi live (websocket) e storico filtrabile (SQLite).",
        "",
        "Cosa trovi dentro:",
        "- **Status**: conteggi ultime 24h per severity + badge `WS:` (connessione websocket) + pulsanti `Refresh`, `Clear`, `Demo Violation`.",
        "- **Filtri**: `Severity`, `Rule`, `Limit` + bottone `Apply`.",
        "- **Live feed**: eventi in tempo reale (socket event `violation`), mantiene le ultime ~60 righe.",
        "- **History**: tabella con `ts`, `severity`, `rule`, `description`, `diff`, `threshold`.",
        "",
        "Azioni principali:",
        "- `Refresh`: ricarica regole, statistiche e storico.",
        "- `Apply`: applica i filtri e ricarica lo storico.",
        "- `Clear`: cancella lo storico salvato (opzionalmente anche i CSV giornalieri `violations_YYYYMMDD.csv`).",
        "- `Demo Violation` (solo localhost): genera una violazione di test end-to-end per verificare che dashboard + storage + WS funzionino.",
        "",
        "API collegate (utile per debug):",
        "- `GET /api/violations/statistics` (conteggi 24h)",
        "- `GET /api/violations?severity=&rule_id=&limit=&offset=` (storico filtrabile/paginato)",
        "- `POST /api/violations/clear` (cancella storico)",
        "",
        "Note:",
        "- Le violazioni sono persistite in SQLite per filtri/paginazione rapidi; opzionalmente anche in CSV giornalieri sotto `logs/monitor/`.",
    ]

    # Best-effort add a tiny hint from snapshot.
    try:
        if isinstance(snapshot, dict):
            comps = snapshot.get('comparison_rules')
            if isinstance(comps, list) and comps:
                enabled = sum(1 for r in comps if isinstance(r, dict) and r.get('enabled'))
                lines.append(f"- Snapshot: regole confronto abilitate={enabled}/{len(comps)}")
    except Exception:
        pass

    return "\n".join(lines).strip()


def _copilot_build_ai_config_answer(snapshot: Dict[str, Any] | None = None) -> str:
    # Best-effort: include current values if present in snapshot.
    cfg = {}
    st = {}
    try:
        if isinstance(snapshot, dict):
            st = snapshot.get('ai') if isinstance(snapshot.get('ai'), dict) else {}
    except Exception:
        st = {}

    # We can't reliably fetch /api/ai/config here without network calls; keep it UI/API oriented.
    lines = [
        "Configurare una regola AI (Anomaly Detection) — guida completa:",
        "",
        "1) Apri la pagina AI",
        "- Vai su `/ai`.",
        "- Clicca `Refresh` per caricare la configurazione corrente e lo stato (trained / WS).",
        "",
        "2) Abilita la detezione live",
        "- Attiva lo switch `Enabled`.",
        "- Imposta `Training / scoring mode`:",
        "  - `Vector (multi-signal)`: modello su un vettore di più segnali.",
        "  - `Confronto (A vs B)`: modello sul delta tra due segnali (A−B o Δ%).",
        "",
        "3) Regola i parametri principali",
        "- `Threshold (mean |z|)`: soglia di anomalia (default tipico 6.0).",
        "  - Se vedi troppi falsi positivi: aumenta (es. 8–12).",
        "  - Se non vedi anomalie ma ti aspetti eventi: riduci (es. 4–6).",
        "- `Sample every (ms)`: ogni quanto campionare (più basso = più CPU + più sensibilità).",
        "- `Min complete ratio`: percentuale minima di campioni ‘validi’ richiesta.",
        "",
        "4A) Modalità Vector: scegli i segnali da monitorare",
        "- Nella sezione `Signals (from Sources/DBC)` seleziona:",
        "  - `Source` → `Message` → `Signal`.",
        "- Premi `+ Add signal` per aggiungerlo alla lista.",
        "- Ripeti per tutti i segnali che vuoi includere nel vettore.",
        "- Usa `Clear` se vuoi ripartire da zero.",
        "",
        "4B) Modalità Confronto: seleziona A e B",
        "- Scegli `Compare op` (Delta o Delta %).",
        "- Seleziona `Source/Message/Signal` per A e per B.",
        "",
        "5) Salva la configurazione",
        "- Premi `Save config`.",
        "- Clicca `Refresh` e verifica che i valori restino impostati.",
        "",
        "6) Addestra il modello (consigliato prima di abilitare scoring continuo)",
        "- Imposta `Train (live)` duration (es. 120s) su un tratto di guida ‘normale’.",
        "- Premi `Train (live)` e attendi completamento.",
        "- Controlla `Model trained` (deve passare a yes e mostrare timestamp/mode).",
        "",
        "7) Verifica feed anomalie",
        "- Guarda la colonna `Anomaly feed` (evento WS: `anomaly`).",
        "- Se vuoi vedere lo storico: `Load history`.",
        "",
        "8) (Opzionale) Trasformare anomalie in Violations",
        "- Nel box `Anomaly → Violation` abilita `Create violation`.",
        "- Scegli `Severity` e `Debounce (s)`.",
        "- Imposta `Rule ID` e `Rule name` (es. `ai_anomaly`).",
        "- Salva (`Save config`) e verifica su `/violations` che arrivino nuove righe.",
        "",
        "API (alternativa alla UI):",
        "- Leggi config: `GET /api/ai/config`",
        "- Aggiorna config: `PUT /api/ai/config` (JSON)",
        "- Stato: `GET /api/ai/status`",
        "- Training live: `POST /api/ai/train_live`",
        "- Storico anomalie: `GET /api/ai/anomalies`",
        "",
        "Troubleshooting rapido:",
        "- Nessuna anomalia: assicurati che il modello sia trained e che `Enabled` sia ON.",
        "- Troppi eventi: alza `Threshold` e/o aumenta `Sample every (ms)`.",
        "- UI non aggiorna: premi `Refresh` e controlla badge `WS:`.",
    ]

    if isinstance(st, dict) and st:
        try:
            trained = bool(st.get('model_trained'))
            mode = str(st.get('mode') or '').strip()
            lines.append("")
            lines.append(f"Stato attuale (snapshot): trained={trained} mode={mode or '—'}")
        except Exception:
            pass

    return "\n".join(lines).strip()


def _copilot_router_decision(system: str, user_msg: str, chat_ctx: Dict[str, Any]) -> str:
    """Ask the model to choose between deterministic handlers vs normal chat.

    Returns one of: 'deterministic_dbc', 'deterministic_ai', 'llm'.
    Falls back to 'llm' on any failure.
    """
    try:
        m = str(user_msg or '').strip().lower()
        # Safety/perf: hard force deterministic on queries known to benefit from grounding.
        if _copilot_is_signal_name_question(m):
            return 'deterministic_dbc'
        if _copilot_is_ai_config_question(m):
            return 'deterministic_ai'

        # Optional router feature flag.
        if str(os.getenv('COPILOT_LLM_ROUTER', '1')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
            return 'llm'

        router_sys = (
            "You are a routing function. Decide how the assistant should answer.\n"
            "Return ONLY one token from this set: deterministic_dbc | deterministic_ai | llm\n"
            "Rules:\n"
            "- deterministic_dbc: user asks which CAN/DBC signal/message corresponds to a concept (gear/speed/rpm/torque) or asks for 'nome segnale'.\n"
            "- deterministic_ai: user asks how to configure AI anomaly detection / rules in the web UI (/ai).\n"
            "- llm: everything else.\n"
        )

        # Tiny context only (avoid slow router).
        small_ctx = {
            'hint': 'route_only',
            'dbc_has_results': bool(((chat_ctx or {}).get('dbc_search') or {}).get('results')),
            'pages': ['/ai', '/dbc_catalog', '/comparison', '/copilot'],
        }

        resp = copilot_agent.chat(
            system=router_sys,
            user=user_msg,
            context=small_ctx,
            temperature=0.0,
            max_context_chars=1200,
            timeout_s=float(os.getenv('COPILOT_ROUTER_TIMEOUT_S', '2.5') or 2.5),
            num_predict=int(os.getenv('COPILOT_ROUTER_NUM_PREDICT', '4') or 4),
        )
        if not bool(resp.get('ok')):
            return 'llm'
        token = str(resp.get('content') or '').strip().lower()
        if token in {'deterministic_dbc', 'deterministic_ai', 'llm'}:
            return token
        return 'llm'

    except Exception:
        return 'llm'


def _copilot_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _copilot_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _copilot_build_deterministic_signal_answer(dbc_search: Dict[str, Any], user_msg: str = '') -> str:
    """Format a grounded answer from dbc_search results (no LLM)."""
    if not isinstance(dbc_search, dict):
        return ''
    results = dbc_search.get('results')
    if not isinstance(results, list) or not results:
        return ''

    m = str(user_msg or '').lower()
    want_gear = ('marcia' in m or 'gear' in m or 'fahrstufe' in m)
    want_speed = ('veloc' in m or 'speed' in m or 'v_signal' in m or 'geschwind' in m)
    want_rpm = ('rpm' in m or 'giri' in m or 'drehzahl' in m)
    want_torque = ('coppia' in m or 'torque' in m or 'drehmoment' in m or 'moment' in m)
    torque_requested = want_torque and any(k in m for k in ['richiest', 'target', 'obiettivo', 'requested', 'req', 'soll', 'wunsch', 'comand', 'setpoint', 'desider'])
    torque_actual = want_torque and not torque_requested and any(k in m for k in ['reale', 'attuale', 'actual', 'ist', 'misurat', 'istmoment'])

    # Flatten a small list of candidates.
    flat = []
    for block in results:
        if not isinstance(block, dict):
            continue
        dbc_name = str(block.get('dbc_name') or '').strip()
        items = block.get('items') if isinstance(block.get('items'), list) else []
        for it in items[:20]:
            if not isinstance(it, dict):
                continue
            sig = str(it.get('signal') or '')
            msg = str(it.get('message') or '')
            comm = str(it.get('signal_comment') or '')
            try:
                score = int(it.get('score') or 0)
            except Exception:
                score = 0
            ls = sig.lower()
            lc = comm.lower()

            # Intent-specific boosts.
            if want_gear:
                if 'gangposition' in ls:
                    score += 40
                if 'fahrstufe' in lc or 'wahlhebel' in lc or 'getriebe' in lc:
                    score += 10
                if 'qualit' in lc or 'qbit' in ls:
                    score -= 3

            if want_speed:
                if ls == 'esp_v_signal' or 'v_signal' in ls:
                    score += 30
                if 'radgeschw' in ls:
                    score += 10

            if want_rpm:
                if ls == 'mo_drehzahl_01' or ('drehzahl' in ls and ls.startswith('mo_')):
                    score += 30

            if want_torque:
                if ls == 'mo_istmoment_vkm':
                    score += 80
                if 'ist-moment des verbrennungsmotors' in lc:
                    score += 60
                if torque_actual:
                    if 'istmoment' in ls and ls.startswith('mo_'):
                        score += 25
                    if 'ist-moment' in lc and 'motor' in lc:
                        score += 20
                if torque_requested:
                    if ls == 'br_eingriffsmoment':
                        score += 70
                    if 'momentanforderung an den motor' in lc:
                        score += 50
                    if 'sollmoment' in ls or 'sollmoment' in lc:
                        score += 10

                # Penalize common non-target torque-ish signals.
                if any(x in ls for x in ['faktor', 'begr', 'max', 'min']) or any(x in lc for x in ['faktor', 'begrenz', 'maximal', 'minimal']):
                    score -= 10
                if any(x in ls for x in ['eps_', 'lenkmoment']) or ('lenkmoment' in lc):
                    score -= 20
                if any(x in ls for x in ['generator', 'reku']) or any(x in lc for x in ['generator', 'rekuperation']):
                    score -= 10
            flat.append((score, dbc_name, it))

    if not flat:
        return ''

    flat.sort(key=lambda x: (-x[0], x[1], str((x[2] or {}).get('message') or ''), str((x[2] or {}).get('signal') or '')))
    top = flat[:5]

    lines = [
        "Risultati DBC (ricerca deterministica):",
    ]
    for _, dbc_name, it in top:
        msg = str(it.get('message') or '')
        sig = str(it.get('signal') or '')
        fid = it.get('frame_id')
        comment = str(it.get('signal_comment') or '').strip().replace('\n', ' ')
        if len(comment) > 140:
            comment = comment[:137] + '...'
        lines.append(f"- DBC: {dbc_name} | Msg: {msg} (0x{int(fid or 0):X}) | Sig: {sig} | {comment}")

    if want_gear:
        lines.append("\nNota: per *marcia inserita*, il candidato migliore è tipicamente `MO_Gangposition` (con `MO_QBit_Gangposition` come qualità/validità).")
    elif want_speed:
        lines.append("\nNota: per *velocità veicolo*, qui il candidato principale è `ESP_v_Signal` (oppure le singole `ESP_*_Radgeschw` per velocità ruota).")
    elif want_rpm:
        lines.append("\nNota: per *RPM motore*, qui il candidato principale è `MO_Drehzahl_01`.")
    elif want_torque:
        if torque_actual:
            lines.append("\nNota: per *coppia reale/attuale*, in queste DBC il match migliore è spesso `MO_IstMoment_VKM` (commento: Ist-Moment del motore termico).")
        elif torque_requested:
            lines.append("\nNota: per *coppia richiesta/target*, spesso NON esiste un unico `MO_*SollMoment*` del motore; il candidato più vicino è `BR_Eingriffsmoment` (richiesta rapida ASR/MSR verso il motore, con fattore in `MO_Faktor_Momente_02`).")
        else:
            lines.append("\nNota: per *coppia motore*, cerca `MO_*Mom*`/`MO_*Moment*` (attuale = `Ist...`, richiesta = `Soll...`/`Anforderung...`).")
    return '\n'.join(lines).strip()


def _copilot_build_dbc_search_context(snapshot: Dict[str, Any], user_msg: str) -> Dict[str, Any]:
    """Build a small deterministic DBC lookup result for grounding Copilot answers."""
    if not _copilot_should_do_dbc_lookup(user_msg):
        return {'enabled': False}

    terms = _copilot_extract_search_terms(user_msg)
    if not terms:
        return {'enabled': True, 'terms': '', 'results': []}

    m = str(user_msg or '').lower()
    want_torque = ('coppia' in m or 'torque' in m or 'drehmoment' in m or 'moment' in m)

    # For torque questions the best signals may live on a different bus/DBC than the
    # currently configured sources (e.g., HCAN vs CCAN). Do a global catalog search.
    if want_torque:
        try:
            rg = dbc_catalog_db.search_signals(query=terms, dbc_name=None, limit=30)
        except Exception:
            rg = {}

        grouped = []
        if isinstance(rg, dict) and rg.get('ok') and isinstance(rg.get('items'), list):
            by_dbc = {}
            order = []
            for it in rg.get('items'):
                if not isinstance(it, dict):
                    continue
                dn = str(it.get('dbc_name') or '').strip()
                if not dn:
                    continue
                if dn not in by_dbc:
                    by_dbc[dn] = []
                    order.append(dn)
                if len(by_dbc[dn]) < 12:
                    by_dbc[dn].append(it)
            for dn in order[:3]:
                grouped.append({'dbc_name': dn, 'items': by_dbc.get(dn) or []})

        return {
            'enabled': True,
            'terms': terms,
            'dbcs_checked': None,
            'results': grouped,
            'hint': "Usa questi risultati per rispondere con nomi esatti (message/signal). Se la lista è vuota, non indovinare: chiedi quale DBC usare o importa il catalogo in /dbc_catalog.",
        }

    # Collect candidate DBCs from sources (full snapshot, not the compact chat ctx).
    src = snapshot.get('sources') if isinstance(snapshot.get('sources'), dict) else {}
    items = src.get('items') if isinstance(src.get('items'), list) else []
    dbc_names = []
    for s in items:
        if not isinstance(s, dict):
            continue
        name = str(s.get('dbc_name') or '').strip()
        if not name:
            continue
        if os.path.basename(name) != name:
            continue
        if name not in dbc_names:
            dbc_names.append(name)

    # Fallback: if snapshot doesn't include items, try a few known dbcs from disk.
    if not dbc_names:
        try:
            for name in os.listdir(UPLOAD_FOLDER_DBC):
                if isinstance(name, str) and name.lower().endswith('.dbc') and os.path.basename(name) == name:
                    dbc_names.append(name)
        except Exception:
            pass
        dbc_names = dbc_names[:3]

    results = []
    for dbc_name in dbc_names[:3]:
        try:
            r = dbc_catalog_db.search_signals(query=terms, dbc_name=dbc_name, limit=12)
            if not r.get('ok'):
                continue
            if r.get('items'):
                results.append({'dbc_name': dbc_name, 'items': r.get('items')})
                continue

            # Best-effort auto-import if missing in DB (avoid heavy loops).
            meta = dbc_catalog_db.get_dbc_meta(dbc_name)
            if not meta and str(os.getenv('COPILOT_DBC_AUTO_IMPORT', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}:
                path = os.path.join(UPLOAD_FOLDER_DBC, dbc_name)
                dbc_catalog_db.import_dbc_file(dbc_name=dbc_name, path=path, include_signals=True, force=False)
                r2 = dbc_catalog_db.search_signals(query=terms, dbc_name=dbc_name, limit=12)
                if r2.get('ok') and r2.get('items'):
                    results.append({'dbc_name': dbc_name, 'items': r2.get('items')})
        except Exception:
            continue

    return {
        'enabled': True,
        'terms': terms,
        'dbcs_checked': dbc_names[:3],
        'results': results,
        'hint': "Usa questi risultati per rispondere con nomi esatti (message/signal). Se la lista è vuota, non indovinare: chiedi quale DBC usare o importa il catalogo in /dbc_catalog.",
    }


@app.route('/sources', methods=['GET'])
def sources_page():
    return render_template('source_config.html')


@app.route('/comparison', methods=['GET'])
def comparison_page():
    return render_template('comparison_rules.html')


@app.route('/violations', methods=['GET'])
def violations_page():
    return render_template('violations_dashboard.html')


@app.route('/ai', methods=['GET'])
def ai_page():
    return render_template('ai_dashboard.html')


@app.route('/copilot', methods=['GET'])
def copilot_page():
    return render_template('copilot.html')


@app.route('/dbc_catalog', methods=['GET'])
def dbc_catalog_page():
    return render_template('dbc_catalog.html')


@app.route('/fibex_catalog', methods=['GET'])
def fibex_catalog_page():
    return render_template('fibex_catalog.html')


@app.route('/experimental', methods=['GET'])
def experimental_page():
    return render_template('experimental_mode.html')


@app.route('/timeline', methods=['GET'])
def timeline_page():
    return render_template('timeline.html')


def _experimental_defaults() -> dict:
    return {
        'enabled': False,
        'mil_channel_id': 0,
        'mil_poll_interval_ms': 800,
        'mil_debounce_ms': 1200,
        'mil_timeout_s': 0.25,
        'scan_rate_limit_s': 600,
        'scan_timeout_s': 45.0,
        'lamp_debounce_ms': 250,
        'lamp_rate_limit_s': 60,
        'diagnostic_transport': 'can',
        'trace_pre_s': 15.0,
        'trace_post_s': 15.0,
        # Sentinel (LLM) analysis + report options
        'sentinel_llm_enabled': False,
        'sentinel_llm_max_dtcs': 9,
        'sentinel_llm_lock_wait_s': 3.0,
        # LLM circuit breaker: avoids repeated stalls when provider is down/busy.
        'sentinel_llm_breaker_enabled': True,
        'sentinel_llm_breaker_failures': 3,
        'sentinel_llm_breaker_cooldown_s': 900.0,
        'sentinel_llm_breaker_decay_s': 1800.0,
        'sentinel_random_dtcs_on_empty': False,
        'sentinel_random_dtcs_n': 3,
        'sentinel_final_report_enabled': True,
        # Optional DTC correlation window around MIL/lamp event.
        # If enabled, only DTCs whose scan-report timestamp falls within:
        #   [event_ts + delay, event_ts + delay + window]
        'sentinel_dtc_time_filter_enabled': False,
        'sentinel_dtc_time_delay_s': 0.0,
        'sentinel_dtc_time_window_s': 300.0,
        # Logs retention/quota (protect against disk-full over long drives)
        'logs_retention_enabled': True,
        'logs_retention_max_age_days': 14,
        'logs_retention_max_total_mb': 4096,
        'logs_retention_min_interval_s': 120.0,
        'lamp_mappings': {
            'epc': {'message': '', 'signal': ''},
            'gearbox': {'message': '', 'signal': ''},
        },
    }


def _get_experimental_settings() -> dict:
    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}
    cur = cfg.get('experimental_assistant') if isinstance(cfg, dict) else None
    if not isinstance(cur, dict):
        cur = {}
    out = _experimental_defaults()
    out.update(cur)
    return out


@app.route('/api/experimental/settings', methods=['GET', 'PUT'])
def api_experimental_settings():
    if request.method == 'GET':
        return jsonify({'ok': True, 'settings': _get_experimental_settings()})

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    patch = data.get('settings') if 'settings' in data else data
    if not isinstance(patch, dict):
        return jsonify({'ok': False, 'error': 'settings must be an object'}), 400

    cur = _get_experimental_settings()
    cur.update(patch)
    try:
        config_store.update({'experimental_assistant': cur})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Start/stop background service based on enabled flag.
    try:
        if bool(cur.get('enabled', False)):
            experimental_assistant.enable()
        else:
            experimental_assistant.disable()
        try:
            experimental_assistant.apply_watchlist_from_settings()
        except Exception:
            pass
    except Exception:
        pass

    return jsonify({'ok': True, 'settings': cur, 'status': experimental_assistant.status()})


@app.route('/api/experimental/status', methods=['GET'])
def api_experimental_status():
    return jsonify({'ok': True, 'status': experimental_assistant.status(), 'settings': _get_experimental_settings()})


@app.route('/api/experimental/incidents', methods=['GET'])
def api_experimental_incidents():
    try:
        limit = int(request.args.get('limit', '25'))
    except Exception:
        limit = 25
    return jsonify({'ok': True, 'incidents': experimental_assistant.list_incidents(limit=limit)})


@app.route('/api/experimental/incidents', methods=['DELETE'])
def api_experimental_incidents_clear():
    experimental_assistant.clear_incidents()
    return jsonify({'ok': True})


@app.route('/api/experimental/simulate_mil_on', methods=['POST'])
def api_experimental_simulate_mil_on():
    # Local-only helper: safe for test benches and offline simulation.
    try:
        remote = str(request.remote_addr or '')
    except Exception:
        remote = ''
    if remote not in {'127.0.0.1', '::1'} and str(os.getenv('KBSM_ALLOW_REMOTE_SIM', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return jsonify({'ok': False, 'error': 'simulate endpoint is local-only (set KBSM_ALLOW_REMOTE_SIM=1 to override)'}), 403

    data = request.json or {}
    if not isinstance(data, dict):
        data = {}
    try:
        channel_id = int(data.get('channel_id', _get_experimental_settings().get('mil_channel_id', 0)))
    except Exception:
        channel_id = 0
    t0 = int(time.time() * 1000)

    try:
        experimental_assistant.enable()
    except Exception:
        pass

    try:
        threading.Thread(target=experimental_assistant._handle_mil_incident, args=(channel_id, t0), daemon=True).start()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True, 'mil_on_ts_ms': t0, 'note': 'incident generation started'})


@app.route('/api/experimental/simulate_lamp_on', methods=['POST'])
def api_experimental_simulate_lamp_on():
    # Local-only helper: simulate EPC/gearbox lamp OFF->ON edge using decoded frame injection.
    try:
        remote = str(request.remote_addr or '')
    except Exception:
        remote = ''
    if remote not in {'127.0.0.1', '::1'} and str(os.getenv('KBSM_ALLOW_REMOTE_SIM', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return jsonify({'ok': False, 'error': 'simulate endpoint is local-only (set KBSM_ALLOW_REMOTE_SIM=1 to override)'}), 403

    data = request.json or {}
    if not isinstance(data, dict):
        data = {}

    kind = str(data.get('kind') or 'epc').strip().lower()
    if kind not in {'epc', 'gearbox', 'cambio'}:
        kind = 'epc'
    if kind == 'cambio':
        kind = 'gearbox'

    # Pick mapping from settings unless explicitly overridden.
    settings = _get_experimental_settings() or {}
    lamps = settings.get('lamp_mappings') if isinstance(settings, dict) else None
    mapping = (lamps or {}).get(kind) if isinstance(lamps, dict) else None

    msg = str(data.get('message') or (mapping or {}).get('message') or '').strip()
    sig = str(data.get('signal') or (mapping or {}).get('signal') or '').strip()
    if not msg or not sig:
        return jsonify({'ok': False, 'error': f'missing mapping for {kind}: set experimental lamp_mappings or provide message/signal'}), 400

    try:
        channel_id = int(data.get('channel_id', settings.get('mil_channel_id', 0)) or 0)
    except Exception:
        channel_id = 0

    # Ensure service enabled and listeners installed.
    try:
        experimental_assistant.enable()
        experimental_assistant.apply_watchlist_from_settings()
    except Exception:
        pass

    t0 = int(time.time() * 1000)

    # Best-effort: when we can find a DBC that contains this message,
    # also inject raw CAN bytes (id + data). This makes the incident trace
    # decodable by the "export_decoded" pipeline.
    def _try_build_raw_payloads():
        try:
            from dbc_loader import load_dbc_database
        except Exception:
            return None

        # Prefer DBCs configured for this channel.
        cfg = {}
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        dbc_paths: list[str] = []
        try:
            chans = cfg.get('logger_channels') if isinstance(cfg, dict) else None
            ch_obj = chans[int(channel_id)] if isinstance(chans, list) and int(channel_id) < len(chans) else None
            dbc_names = ch_obj.get('dbc_names') if isinstance(ch_obj, dict) else None
            if isinstance(dbc_names, list):
                for name in dbc_names:
                    base = os.path.basename(str(name or '').strip())
                    if not base:
                        continue
                    p = os.path.join(UPLOAD_FOLDER_DBC, base)
                    if os.path.isfile(p):
                        dbc_paths.append(p)
        except Exception:
            pass

        # Fallback: scan a few on-disk DBCs.
        if not dbc_paths:
            try:
                for fn in sorted(os.listdir(UPLOAD_FOLDER_DBC)):
                    if not fn.lower().endswith('.dbc'):
                        continue
                    p = os.path.join(UPLOAD_FOLDER_DBC, fn)
                    if os.path.isfile(p):
                        dbc_paths.append(p)
                    if len(dbc_paths) >= 8:
                        break
            except Exception:
                pass

        if not dbc_paths:
            return None

        # Encode helper: fill missing required signals with 0 until it encodes.
        def _encode_with_defaults(message, updates):
            vals = dict(updates or {})
            for _ in range(64):
                try:
                    b = message.encode(vals)
                    return bytes(b)
                except KeyError as e:
                    missing = str(e).strip().strip('"\'')
                    if missing and missing not in vals:
                        vals[missing] = 0
                        continue
                    return None
                except Exception as e:
                    s = str(e)
                    # Common cantools error shapes:
                    # - The signal "X" is required for encoding.
                    # - The signal 'X' is required
                    # - Missing required signals: "A", "B" ...
                    missing_names = re.findall(r"signal\s+[\"']([^\"']+)[\"']", s)
                    if not missing_names and ('required' in s.lower() or 'missing' in s.lower()):
                        missing_names = re.findall(r"[\"']([^\"']+)[\"']", s)

                    added = False
                    for missing in missing_names:
                        if missing and missing not in vals:
                            vals[missing] = 0
                            added = True

                    if added:
                        continue
                    return None
            return None

        for p in dbc_paths:
            try:
                db = load_dbc_database(p)
                message = db.get_message_by_name(msg)
                # Only proceed if this signal exists on that message.
                try:
                    if sig not in [s.name for s in message.signals]:
                        continue
                except Exception:
                    pass

                b_off = _encode_with_defaults(message, {sig: 0})
                b_on = _encode_with_defaults(message, {sig: 1})
                if b_off is None or b_on is None:
                    continue
                return {
                    'id': int(message.frame_id),
                    'off': list(b_off),
                    'on': list(b_on),
                    'dbc': os.path.basename(p),
                }
            except Exception:
                continue
        return None

    raw = _try_build_raw_payloads()

    def _inject(val, *, ts_ms):
        try:
            frame = {
                'timestamp': int(ts_ms),
                'type': 'CAN',
                'channel': int(channel_id),
                'id': int(raw.get('id')) if isinstance(raw, dict) and raw.get('id') is not None else 0,
                'data': list(raw.get('off' if int(val) == 0 else 'on')) if isinstance(raw, dict) else [],
                'decoded': {
                    'name': msg,
                    'signals': {sig: val},
                },
            }
            # Route through normal listener pipeline.
            manager.inject_decoded_frame(frame)
            return True
        except Exception:
            return False

    # Create an explicit OFF->ON edge so prev=False then ON=True.
    ok1 = _inject(0, ts_ms=t0)
    try:
        time.sleep(0.05)
    except Exception:
        pass
    ok2 = _inject(1, ts_ms=t0 + 50)

    return jsonify({
        'ok': True,
        'kind': kind,
        'channel_id': channel_id,
        'message': msg,
        'signal': sig,
        'injected': bool(ok1 and ok2),
        'raw_injected': bool(isinstance(raw, dict)),
        'raw_dbc': (raw.get('dbc') if isinstance(raw, dict) else None),
        'raw_id': (raw.get('id') if isinstance(raw, dict) else None),
        'note': 'lamp OFF->ON injected; incident may appear after debounce + trace window',
        'ts_ms': t0,
    })


@app.route('/api/copilot/status', methods=['GET'])
def api_copilot_status():
    try:
        st = copilot_agent.status()
    except Exception as e:
        st = {'ok': False, 'error': str(e)}
    return jsonify({'ok': True, 'status': st})


@app.route('/api/copilot/snapshot', methods=['GET'])
def api_copilot_snapshot():
    return jsonify({'ok': True, 'snapshot': _copilot_build_snapshot()})


@app.route('/api/copilot/chat', methods=['POST'])
def api_copilot_chat():
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    msg = str(data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'missing message'}), 400
    if len(msg) > 2500:
        msg = msg[:2500]

    snapshot = _copilot_build_snapshot()
    chat_ctx = _copilot_build_chat_context(snapshot)
    try:
        chat_ctx['dbc_search'] = _copilot_build_dbc_search_context(snapshot, msg)
    except Exception:
        chat_ctx['dbc_search'] = {'enabled': False, 'error': 'lookup failed'}

    # Deterministic UI help (avoid slow LLM on low-power devices).
    try:
        page = _copilot_page_help_target(msg)
        if page:
            ans = _copilot_build_page_help_answer(page, snapshot)
            if ans:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': ans})
    except Exception:
        pass

    # Deterministic guided rule creation (draft + confirm flow).
    # Must run before generic “how to configure violations” guidance so that
    # prompts like “crea una regola che attivi una violation se ...” create a draft.
    try:
        ans = _copilot_try_handle_rule_wizard(msg, snapshot, chat_ctx if isinstance(chat_ctx, dict) else {})
        if ans:
            return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': ans})
    except Exception as e:
        try:
            print(f"[copilot] rule wizard error: {e}")
        except Exception:
            pass
        return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': f"Errore interno nel wizard regole: {str(e)}"})

    # Deterministic help: configuring violations (comparison rules).
    try:
        if _copilot_is_violation_config_question(msg):
            guide = _copilot_build_violation_config_answer(snapshot)
            if guide:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': guide})
    except Exception:
        pass

    # Deterministic snapshot answer: current comparison-rule status.
    try:
        if _copilot_is_rules_status_question(msg):
            ans = _copilot_build_rules_status_answer(snapshot)
            if ans:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': ans})
    except Exception:
        pass

    # Deterministic FAQ answers for frequent how-to/troubleshooting questions.
    try:
        fk = _copilot_faq_target(msg)
        if fk:
            ans = _copilot_build_faq_answer(fk, snapshot)
            if ans:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': ans})
    except Exception:
        pass

    # Router: let the model decide deterministic vs LLM (with safe fallbacks).
    decision = _copilot_router_decision(_copilot_system_prompt(), msg, chat_ctx if isinstance(chat_ctx, dict) else {})
    if decision == 'deterministic_dbc':
        try:
            direct = _copilot_build_deterministic_signal_answer(
                chat_ctx.get('dbc_search') if isinstance(chat_ctx, dict) else {},
                msg,
            )
            if direct:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': direct})
        except Exception:
            pass
    if decision == 'deterministic_ai':
        try:
            guide = _copilot_build_ai_config_answer(snapshot)
            if guide:
                return jsonify({'ok': True, 'provider': 'deterministic', 'model': None, 'content': guide})
        except Exception:
            pass
    # Mildly lower temperature for "assistant/guide" style.
    # LLM single-flight + cooldown: avoid repeated timeouts when Ollama is still computing.
    now_s = float(time.time())
    try:
        cd_until = float(_copilot_llm_cooldown_until_s or 0.0)
    except Exception:
        cd_until = 0.0
    if cd_until and now_s < cd_until:
        retry_after = max(1, int(cd_until - now_s))
        resp = jsonify({
            'ok': False,
            'provider': 'ollama',
            'error': f'LLM cooling down after timeout; retry in ~{retry_after}s',
            'retry_after_s': retry_after,
        })
        resp.status_code = 503
        resp.headers['Retry-After'] = str(retry_after)
        return resp

    if not _copilot_llm_lock.acquire(blocking=False):
        resp = jsonify({
            'ok': False,
            'provider': 'ollama',
            'error': 'LLM busy (previous request still running). Retry in a few seconds.',
            'retry_after_s': 5,
        })
        resp.status_code = 429
        resp.headers['Retry-After'] = '5'
        return resp

    global _copilot_llm_inflight_since_s, _copilot_llm_last_start_s
    try:
        _copilot_llm_last_start_s = float(time.time())
        _copilot_llm_inflight_since_s = float(_copilot_llm_last_start_s)
    except Exception:
        pass

    try:
        resp = copilot_agent.chat(
            system=_copilot_system_prompt(),
            user=msg,
            context=chat_ctx,
            temperature=0.2,
            # Reduce prompt size for latency on CPU-only inference.
            max_context_chars=_copilot_env_int('COPILOT_LLM_MAX_CONTEXT_CHARS', 2000),
            # Hard bounds to avoid runaway CPU if the model gets verbose.
            # Raspberry Pi class devices can take >120s even for short replies.
            timeout_s=_copilot_env_float('COPILOT_LLM_TIMEOUT_S', 240.0),
            # Default higher to avoid truncated answers; keep bounded via env.
            num_predict=_copilot_env_int('COPILOT_LLM_NUM_PREDICT', 128),
        )
    except Exception as e:
        try:
            _copilot_llm_last_error['ts_ms'] = int(time.time() * 1000)
            _copilot_llm_last_error['error'] = str(e)
        except Exception:
            pass
        return jsonify({'ok': False, 'provider': 'ollama', 'error': str(e)}), 500
    finally:
        try:
            _copilot_llm_lock.release()
        except Exception:
            pass
        try:
            _copilot_llm_inflight_since_s = 0.0
        except Exception:
            pass

    # If we timed out, enter a short cooldown window.
    try:
        if not bool(resp.get('ok')):
            err = str(resp.get('error') or '').lower()
            if 'timed out' in err or 'timeout' in err:
                _copilot_llm_cooldown_until_s = float(time.time()) + float(_copilot_env_float('COPILOT_LLM_COOLDOWN_S', 15.0))
                _copilot_maybe_stop_ollama_model('timeout')
            _copilot_llm_last_error['ts_ms'] = int(time.time() * 1000)
            _copilot_llm_last_error['error'] = str(resp.get('error') or '')
    except Exception:
        pass

    if not bool(resp.get('ok')):
        return jsonify({'ok': False, 'error': resp.get('error') or 'copilot provider error', 'provider': resp.get('provider')}), 502
    return jsonify({'ok': True, 'provider': resp.get('provider'), 'model': resp.get('model'), 'content': resp.get('content')})


@app.route('/api/sentinel/status', methods=['GET'])
def api_sentinel_status():
    try:
        st = sentinel_agent.status()
    except Exception as e:
        st = {'ok': False, 'error': str(e)}
    return jsonify({
        'ok': True,
        'status': st,
        'sentinel': {
            'inflight_since_s': _sentinel_llm_inflight_since_s,
            'last_start_s': _sentinel_llm_last_start_s,
            'last_error': _sentinel_llm_last_error,
        },
    })


@app.route('/api/sentinel/chat', methods=['POST'])
def api_sentinel_chat():
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    msg = str(data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'missing message'}), 400
    if len(msg) > 5000:
        msg = msg[:5000]

    ctx = data.get('context')
    if not isinstance(ctx, dict):
        ctx = {}

    lock = _sentinel_llm_lock or _copilot_llm_lock
    if not lock.acquire(blocking=False):
        resp = jsonify({'ok': False, 'provider': 'ollama', 'error': 'LLM busy (another request still running). Retry in a few seconds.', 'retry_after_s': 8})
        resp.status_code = 429
        resp.headers['Retry-After'] = '8'
        return resp

    global _sentinel_llm_inflight_since_s, _sentinel_llm_last_start_s
    try:
        _sentinel_llm_last_start_s = float(time.time())
        _sentinel_llm_inflight_since_s = float(_sentinel_llm_last_start_s)
    except Exception:
        pass

    started = time.time()
    try:
        resp = sentinel_agent.chat(
            system=_sentinel_system_prompt(),
            user=msg,
            context=ctx,
            temperature=float(_sentinel_env_float('SENTINEL_LLM_TEMPERATURE', 0.2)),
            max_context_chars=_sentinel_env_int('SENTINEL_LLM_MAX_CONTEXT_CHARS', 3500),
            timeout_s=_sentinel_env_float('SENTINEL_LLM_TIMEOUT_S', 360.0),
            num_predict=_sentinel_env_int('SENTINEL_LLM_NUM_PREDICT', 220),
        )
    except Exception as e:
        try:
            _sentinel_llm_last_error['ts_ms'] = int(time.time() * 1000)
            _sentinel_llm_last_error['error'] = str(e)
        except Exception:
            pass
        return jsonify({'ok': False, 'provider': 'ollama', 'error': str(e)}), 500
    finally:
        try:
            lock.release()
        except Exception:
            pass
        try:
            _sentinel_llm_inflight_since_s = 0.0
        except Exception:
            pass

    if not bool(resp.get('ok')):
        return jsonify({'ok': False, 'provider': resp.get('provider') or 'ollama', 'error': resp.get('error') or 'provider error'}), 502
    return jsonify({
        'ok': True,
        'provider': resp.get('provider'),
        'model': resp.get('model'),
        'latency_ms': int((time.time() - started) * 1000),
        'content': resp.get('content'),
    })


@app.route('/api/sentinel/test_random_dtcs', methods=['POST'])
def api_sentinel_test_random_dtcs():
    # Local-only helper: run real LLM analyses on random PDX DTCs and generate an HTML report.
    try:
        remote = str(request.remote_addr or '')
    except Exception:
        remote = ''
    if remote not in {'127.0.0.1', '::1'} and str(os.getenv('KBSM_ALLOW_REMOTE_SIM', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return jsonify({'ok': False, 'error': 'test endpoint is local-only (set KBSM_ALLOW_REMOTE_SIM=1 to override)'}), 403

    data = request.json or {}
    if not isinstance(data, dict):
        data = {}
    try:
        n = int(data.get('n', 3))
    except Exception:
        n = 3
    n = max(1, min(10, n))
    try:
        seed = int(data.get('seed', 0) or 0)
    except Exception:
        seed = 0

    # Load active PDX DTC map (code -> description).
    try:
        from vag_scanner import _load_active_pdx_dtc_map
        dtc_map = _load_active_pdx_dtc_map() or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to load active PDX dtc map: {e}'}), 500

    if not isinstance(dtc_map, dict) or not dtc_map:
        return jsonify({'ok': False, 'error': 'no active PDX DTC map found; select a PDX project first (/projects)'}), 400

    try:
        import random
        codes = [k for k in dtc_map.keys() if isinstance(k, str) and k.strip()]
        if not codes:
            return jsonify({'ok': False, 'error': 'active PDX DTC map is empty'}), 400
        if seed:
            random.seed(seed)
        picks = random.sample(codes, k=min(n, len(codes)))
    except Exception as e:
        return jsonify({'ok': False, 'error': f'random selection failed: {e}'}), 500

    lock = _sentinel_llm_lock or _copilot_llm_lock
    if not lock.acquire(blocking=False):
        resp = jsonify({'ok': False, 'provider': 'ollama', 'error': 'LLM busy (another request still running). Retry in a few seconds.', 'retry_after_s': 15})
        resp.status_code = 429
        resp.headers['Retry-After'] = '15'
        return resp

    started = time.time()
    results: list[dict] = []
    errors: list[str] = []

    try:
        for code in picks:
            desc = str(dtc_map.get(code) or '').strip()
            user_msg = (
                f"Analizza il DTC {code}.\n"
                f"Descrizione PDX: {desc or '(mancante)'}\n\n"
                "Contesto: test automatico su banco; non ho freeze frame. "
                "Proponi verifiche generiche ma pratiche (cablaggio, connettori, alimentazioni, sensori/attuatori, plausibilità)."
            )
            ctx = {
                'kind': 'sentinel_smoketest',
                'dtc': {'code': code, 'pdx_description': desc},
                'note': 'Random DTC selected from active PDX dtc_index.json',
            }
            try:
                r = sentinel_agent.chat(
                    system=_sentinel_system_prompt(),
                    user=user_msg,
                    context=ctx,
                    temperature=float(_sentinel_env_float('SENTINEL_LLM_TEMPERATURE', 0.2)),
                    max_context_chars=_sentinel_env_int('SENTINEL_LLM_MAX_CONTEXT_CHARS', 3500),
                    timeout_s=_sentinel_env_float('SENTINEL_LLM_TIMEOUT_S', 360.0),
                    num_predict=_sentinel_env_int('SENTINEL_LLM_NUM_PREDICT', 220),
                )
                if not bool(r.get('ok')):
                    raise RuntimeError(str(r.get('error') or 'provider error'))
                results.append({'code': code, 'pdx_description': desc, 'analysis': str(r.get('content') or '').strip()})
            except Exception as e:
                errors.append(f"{code}: {e}")
                results.append({'code': code, 'pdx_description': desc, 'analysis': '', 'error': str(e)})
    finally:
        try:
            lock.release()
        except Exception:
            pass

    # Build and write a minimal HTML report into the main log folder (so /api/logs can serve it).
    try:
        import html as _html
        from datetime import datetime

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = f"sentinel_smoketest_{ts}_n{len(results)}"
        html_name = base + '.html'
        json_name = base + '.json'

        html_parts = [
            '<!doctype html>',
            '<html><head><meta charset="utf-8">',
            f"<title>Sentinel Smoke Test — { _html.escape(base) }</title>",
            '<style>body{font-family:system-ui,Segoe UI,Roboto,Arial;margin:24px;background:#0b0f14;color:#e6edf3}a{color:#7cc0ff}pre{white-space:pre-wrap;background:#111826;border:1px solid #223;padding:12px;border-radius:8px} .card{border:1px solid #223;border-radius:10px;padding:14px;margin:12px 0;background:#0f1622} .muted{color:#9fb0c0} .err{color:#ff8a8a}</style>',
            '</head><body>',
            f"<h1>Sentinel Smoke Test</h1>",
            f"<div class='muted'>Generated: { _html.escape(ts) } — model={ _html.escape(str(getattr(sentinel_agent,'model','') or '')) }</div>",
            f"<div class='muted'>Active PDX DTC map size: {len(dtc_map)}</div>",
        ]
        if errors:
            html_parts.append(f"<div class='err'>Errors: { _html.escape('; '.join(errors[:10])) }</div>")

        for it in results:
            code = _html.escape(str(it.get('code') or ''))
            desc = _html.escape(str(it.get('pdx_description') or ''))
            analysis = str(it.get('analysis') or '').strip()
            err = str(it.get('error') or '').strip()
            html_parts.append("<div class='card'>")
            html_parts.append(f"<h2 class='mono'>{code}</h2>")
            if desc:
                html_parts.append(f"<div class='muted'>{desc}</div>")
            if err:
                html_parts.append(f"<div class='err'>LLM error: { _html.escape(err) }</div>")
            if analysis:
                html_parts.append('<pre>' + _html.escape(analysis) + '</pre>')
            html_parts.append('</div>')

        html_parts.append('</body></html>')
        html_text = '\n'.join(html_parts)

        html_path = os.path.join(str(LOG_FOLDER), html_name)
        json_path = os.path.join(str(LOG_FOLDER), json_name)
        with open(html_path, 'w', encoding='utf-8') as fp:
            fp.write(html_text)
        try:
            with open(json_path, 'w', encoding='utf-8') as fp:
                json.dump({'dtcs': results, 'errors': errors, 'ts': ts, 'model': getattr(sentinel_agent, 'model', None)}, fp, indent=2, ensure_ascii=False)
        except Exception:
            pass
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to write report: {e}', 'dtcs': results, 'errors': errors}), 500

    return jsonify({
        'ok': True,
        'picked': picks,
        'dtcs': results,
        'errors': errors,
        'report_html': html_name,
        'report_json': json_name,
        'log_dir': str(LOG_FOLDER),
        'elapsed_s': round(time.time() - started, 3),
    })


@app.route('/api/sentinel/ingest_scan_report', methods=['POST'])
def api_sentinel_ingest_scan_report():
    """Upload a scan report HTML, extract DTCs, run real local LLM analysis, and generate a unified final report.

    Multipart form-data:
      - file: HTML report
      - mil_on_time_hms: optional, e.g. 22:48:22
      - mil_on_date: optional, e.g. 08.09.2025 or 2025-09-08
      - mil_on_iso: optional, e.g. 2025-09-08T22:48:22
      - mil_on_ts_ms: optional epoch ms (highest priority)
      - channel_id: optional int
      - analyze: optional bool (default true)
      - timeout_s: optional float for LLM calls
      - num_predict: optional int for LLM calls
      - max_dtcs: optional int (default: analyze all parsed, capped at 50)

    Note: local-only by default. Set KBSM_ALLOW_REMOTE_SIM=1 to override.
    """
    # Local-only by default.
    try:
        remote = str(request.remote_addr or '')
    except Exception:
        remote = ''
    if remote not in {'127.0.0.1', '::1'} and str(os.getenv('KBSM_ALLOW_REMOTE_SIM', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return jsonify({'ok': False, 'error': 'ingest endpoint is local-only (set KBSM_ALLOW_REMOTE_SIM=1 to override)'}), 403

    try:
        from werkzeug.utils import secure_filename
    except Exception:
        secure_filename = None

    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'missing file field'}), 400
    f = request.files.get('file')
    if f is None:
        return jsonify({'ok': False, 'error': 'missing file'}), 400

    orig = str(getattr(f, 'filename', '') or '').strip()
    if not orig:
        return jsonify({'ok': False, 'error': 'missing filename'}), 400
    safe = secure_filename(orig) if secure_filename else orig
    safe = str(safe or '').strip()
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid filename'}), 400
    if not safe.lower().endswith(('.html', '.htm')):
        return jsonify({'ok': False, 'error': 'only .html/.htm files are allowed'}), 400

    def _form_bool(name: str, default: bool) -> bool:
        try:
            v = request.form.get(name)
            if v is None:
                return bool(default)
            s = str(v).strip().lower()
            if s in {'1', 'true', 'yes', 'on'}:
                return True
            if s in {'0', 'false', 'no', 'off'}:
                return False
            return bool(default)
        except Exception:
            return bool(default)

    def _form_int(name: str, default: int) -> int:
        try:
            v = request.form.get(name)
            if v is None or str(v).strip() == '':
                return int(default)
            return int(str(v).strip())
        except Exception:
            return int(default)

    def _form_float(name: str, default: float) -> float:
        try:
            v = request.form.get(name)
            if v is None or str(v).strip() == '':
                return float(default)
            return float(str(v).strip())
        except Exception:
            return float(default)

    def _parse_mil_ts_ms() -> int:
        # Priority 1: explicit epoch
        try:
            v = request.form.get('mil_on_ts_ms')
            if v is not None and str(v).strip() != '':
                return int(float(str(v).strip()))
        except Exception:
            pass

        # Priority 2: ISO
        iso = str(request.form.get('mil_on_iso') or '').strip()
        if iso:
            try:
                from datetime import datetime
                iso2 = iso.replace(' ', 'T')
                dt = datetime.fromisoformat(iso2)
                return int(dt.timestamp() * 1000)
            except Exception:
                pass

        # Priority 3: date + time
        hms = str(request.form.get('mil_on_time_hms') or '').strip()
        date = str(request.form.get('mil_on_date') or '').strip()

        # If date not provided, try derive from filename like dd.mm.yyyy_HH.MM.SS
        if not date:
            m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})_(\d{2})\.(\d{2})\.(\d{2})', orig)
            if m:
                date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

        if date and hms:
            try:
                from datetime import datetime
                d = date
                if re.match(r'^\d{2}\.\d{2}\.\d{4}$', d):
                    dd, mm, yyyy = d.split('.')
                    d = f"{yyyy}-{mm}-{dd}"
                dt = datetime.fromisoformat(d + 'T' + hms)
                return int(dt.timestamp() * 1000)
            except Exception:
                pass

        # Fallback: now
        return int(time.time() * 1000)

    mil_on_ts_ms = int(_parse_mil_ts_ms())
    channel_id = _form_int('channel_id', 0)
    analyze = _form_bool('analyze', True)
    timeout_s = request.form.get('timeout_s')
    timeout_s_val = None
    try:
        if timeout_s is not None and str(timeout_s).strip() != '':
            timeout_s_val = float(str(timeout_s).strip())
    except Exception:
        timeout_s_val = None

    num_predict_val = None
    try:
        v = request.form.get('num_predict')
        if v is not None and str(v).strip() != '':
            num_predict_val = int(str(v).strip())
    except Exception:
        num_predict_val = None

    max_dtcs_val = None
    try:
        v = request.form.get('max_dtcs')
        if v is not None and str(v).strip() != '':
            max_dtcs_val = int(str(v).strip())
    except Exception:
        max_dtcs_val = None

    # Save uploaded HTML into logs for traceability and /api/logs serving.
    dest_dir = os.path.realpath(LOG_FOLDER)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass
    try:
        ts = time.strftime('%Y%m%d_%H%M%S')
    except Exception:
        ts = str(int(time.time()))
    base, ext = os.path.splitext(safe)
    scan_report_name = f"uploaded_scan_report_{ts}_{base}{ext}"
    scan_report_name = scan_report_name[:220]
    scan_report_path = os.path.join(dest_dir, scan_report_name)
    try:
        f.save(scan_report_path)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to save upload: {e}'}), 500

    try:
        with open(scan_report_path, 'r', encoding='utf-8', errors='ignore') as fp:
            html_text = fp.read()
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to read saved upload: {e}', 'scan_report_filename': scan_report_name}), 500

    # Parse DTCs
    try:
        from experimental_assistant import parse_vag_scan_report_html, choose_primary_dtc, build_final_report_html
        parsed = parse_vag_scan_report_html(html_text)
        dtcs = list(parsed.get('dtcs') or []) if isinstance(parsed, dict) else []
    except Exception as e:
        return jsonify({'ok': False, 'error': f'parse failed: {e}', 'scan_report_filename': scan_report_name}), 500

    # Fallback generic extraction if parser found nothing.
    if not dtcs:
        try:
            # Find common DTC patterns in HTML/text. Keep best-effort descriptions nearby.
            text = re.sub(r'<[^>]+>', ' ', html_text)
            text = re.sub(r'\s+', ' ', text)
            found = []
            for m in re.finditer(r'\b([PBCU][0-9A-F]{4,5})\b', text, flags=re.IGNORECASE):
                code = (m.group(1) or '').upper()
                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 160)
                snippet = text[start:end].strip()
                found.append({'code': code, 'active': False, 'desc_report': snippet, 'status_byte': '', 'status_desc': 'ingest (generic parse)'})
            # De-dup preserving order
            seen = set()
            dtcs = []
            for d in found:
                c = str(d.get('code') or '').strip()
                if not c or c in seen:
                    continue
                seen.add(c)
                dtcs.append(d)
        except Exception:
            dtcs = []

    # Enrich with PDX
    try:
        from vag_scanner import _load_active_pdx_dtc_map, _dtc_description
        dtc_map = _load_active_pdx_dtc_map()
        for d in dtcs:
            code = str(d.get('code') or '').strip()
            if not code:
                continue
            desc_report = str(d.get('desc_report') or '').strip()
            try:
                pdx_desc = str(_dtc_description(code, dtc_map) or '').strip()
            except Exception:
                pdx_desc = ''
            d['desc_pdx'] = pdx_desc
            d['desc'] = pdx_desc or desc_report
    except Exception:
        for d in dtcs:
            d['desc'] = str(d.get('desc_report') or '').strip()

    primary = None
    try:
        primary = choose_primary_dtc(dtcs)
        if primary is not None:
            primary = dict(primary)
            primary['confidence'] = 'low'
            primary['severity'] = 'warning'
            if bool(primary.get('active')):
                primary['confidence'] = 'medium'
            if bool(primary.get('active')) and primary.get('desc'):
                primary['confidence'] = 'high'
    except Exception:
        primary = None

    incident_id = f"mil_{mil_on_ts_ms:x}"
    started = time.time()

    sentinel_analyses = []
    analysis_errors = []
    analysis_note = ''
    if analyze and dtcs:
        # Temporarily run analysis using the ExperimentalAssistant helper (single-flight locked).
        try:
            ctx = {
                'incident_id': incident_id,
                'mil_on_ts_ms': int(mil_on_ts_ms),
                'scan_action': 'ingest_scan_report',
                'scan_report_filename': scan_report_name,
                'channel_id': int(channel_id),
                'source_filename': orig,
            }
            want_n = len(dtcs)
            if max_dtcs_val is not None:
                want_n = int(max_dtcs_val)
            if timeout_s_val is None:
                timeout_s_val = 900.0
            if num_predict_val is None:
                num_predict_val = 450
            analysis_note = f"analyzing {want_n} DTC(s) (dtc_count={len(dtcs)}), timeout_s={timeout_s_val}, num_predict={num_predict_val}"
            sentinel_analyses = experimental_assistant._sentinel_analyze_dtcs(
                dtcs=dtcs,
                primary=primary,
                incident_context=ctx,
                max_dtcs_override=want_n,
                timeout_s_override=timeout_s_val,
                num_predict_override=num_predict_val,
            )
        except Exception as e:
            analysis_errors.append(str(e))
            sentinel_analyses = []

    # Build unified final report
    try:
        final_report_name = f"sentinel_final_report_ingest_{incident_id}_{ts}.html"
        final_html = build_final_report_html(
            incident_id=incident_id,
            mil_on_ts_ms=int(mil_on_ts_ms),
            scan_action='ingest_scan_report',
            scan_started_ts_ms=int(mil_on_ts_ms),
            scan_finished_ts_ms=int(mil_on_ts_ms),
            scan_report_filename=str(scan_report_name or ''),
            trace_mf4_filename='',
            trace_raw_mf4_filename='',
            bundle_filename='',
            primary=primary,
            dtcs=dtcs,
            lamp_snapshot={},
            sentinel_analyses=sentinel_analyses,
        )
        with open(os.path.join(dest_dir, final_report_name), 'w', encoding='utf-8') as fp:
            fp.write(final_html)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to write final report: {e}', 'scan_report_filename': scan_report_name, 'dtc_count': len(dtcs)}), 500

    return jsonify({
        'ok': True,
        'incident_id': incident_id,
        'mil_on_ts_ms': int(mil_on_ts_ms),
        'scan_report_filename': scan_report_name,
        'dtc_count': len(dtcs),
        'primary': primary,
        'sentinel': {
            'enabled': bool(analyze),
            'analyses_count': len(sentinel_analyses) if isinstance(sentinel_analyses, list) else 0,
            'analyses': (sentinel_analyses if isinstance(sentinel_analyses, list) else []),
            'errors': analysis_errors,
            'note': analysis_note,
        },
        'final_report_filename': final_report_name,
        'elapsed_s': round(time.time() - started, 3),
        'log_dir': str(dest_dir),
    })


@app.route('/api/sentinel/analyze_dtcs', methods=['POST'])
def api_sentinel_analyze_dtcs():
    """Analyze provided DTC objects with the local Sentinel LLM and generate a unified final report.

    JSON body:
      {
        "mil_on_iso": "2025-09-08T22:48:22" | optional,
        "mil_on_ts_ms": 123 | optional,
        "channel_id": 0 | optional,
        "dtcs": [ {"code": "P060C62", "desc_report": "...", "active": false, ...}, ... ],
        "analyze": true | optional,
        "timeout_s": 900 | optional (per DTC),
        "num_predict": 450 | optional,
        "max_dtcs": 9 | optional
      }

    Local-only by default. Set KBSM_ALLOW_REMOTE_SIM=1 to override.
    """
    # Local-only by default.
    try:
        remote = str(request.remote_addr or '')
    except Exception:
        remote = ''
    if remote not in {'127.0.0.1', '::1'} and str(os.getenv('KBSM_ALLOW_REMOTE_SIM', '0')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return jsonify({'ok': False, 'error': 'analyze endpoint is local-only (set KBSM_ALLOW_REMOTE_SIM=1 to override)'}), 403

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}

    analyze = bool(data.get('analyze', True))
    try:
        channel_id = int(data.get('channel_id', 0) or 0)
    except Exception:
        channel_id = 0

    mil_on_ts_ms = 0
    try:
        if data.get('mil_on_ts_ms') is not None:
            mil_on_ts_ms = int(float(data.get('mil_on_ts_ms')))
    except Exception:
        mil_on_ts_ms = 0
    if mil_on_ts_ms <= 0:
        mil_on_iso = str(data.get('mil_on_iso') or '').strip()
        if mil_on_iso:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(mil_on_iso.replace(' ', 'T'))
                mil_on_ts_ms = int(dt.timestamp() * 1000)
            except Exception:
                mil_on_ts_ms = 0
    if mil_on_ts_ms <= 0:
        mil_on_ts_ms = int(time.time() * 1000)

    dtcs_in = data.get('dtcs')
    if not isinstance(dtcs_in, list) or not dtcs_in:
        return jsonify({'ok': False, 'error': 'missing dtcs[]'}), 400

    # Normalize DTC objects.
    dtcs: list[dict] = []
    for it in dtcs_in:
        if not isinstance(it, dict):
            continue
        code = str(it.get('code') or '').strip().upper()
        if not code:
            continue
        d = dict(it)
        d['code'] = code
        # Ensure desc fields exist
        if 'desc_report' not in d:
            d['desc_report'] = str(d.get('desc') or '')
        # Ensure active boolean
        d['active'] = bool(d.get('active', False))
        dtcs.append(d)

    if not dtcs:
        return jsonify({'ok': False, 'error': 'dtcs[] empty after normalization'}), 400

    # Optional enrich with PDX.
    try:
        from vag_scanner import _load_active_pdx_dtc_map, _dtc_description
        dtc_map = _load_active_pdx_dtc_map()
        for d in dtcs:
            c = str(d.get('code') or '').strip()
            if not c:
                continue
            desc_report = str(d.get('desc_report') or '').strip()
            try:
                pdx_desc = str(_dtc_description(c, dtc_map) or '').strip()
            except Exception:
                pdx_desc = ''
            d['desc_pdx'] = pdx_desc
            d['desc'] = pdx_desc or desc_report
    except Exception:
        for d in dtcs:
            d['desc'] = str(d.get('desc_report') or '').strip()

    try:
        from experimental_assistant import choose_primary_dtc, build_final_report_html
    except Exception as e:
        return jsonify({'ok': False, 'error': f'backend missing experimental assistant helpers: {e}'}), 500

    primary = None
    try:
        primary = choose_primary_dtc(dtcs)
        if primary is not None:
            primary = dict(primary)
            primary['confidence'] = 'low'
            primary['severity'] = 'warning'
            if bool(primary.get('active')):
                primary['confidence'] = 'medium'
            if bool(primary.get('active')) and primary.get('desc'):
                primary['confidence'] = 'high'
    except Exception:
        primary = None

    try:
        max_dtcs = int(data.get('max_dtcs')) if data.get('max_dtcs') is not None else len(dtcs)
    except Exception:
        max_dtcs = len(dtcs)
    max_dtcs = max(1, min(max_dtcs, 50))

    timeout_s = None
    try:
        if data.get('timeout_s') is not None:
            timeout_s = float(data.get('timeout_s'))
    except Exception:
        timeout_s = None
    if timeout_s is None:
        timeout_s = 900.0
    timeout_s = float(max(30.0, min(timeout_s, 3600.0)))

    num_predict = None
    try:
        if data.get('num_predict') is not None:
            num_predict = int(data.get('num_predict'))
    except Exception:
        num_predict = None
    if num_predict is None:
        num_predict = 450
    num_predict = int(max(64, min(num_predict, 4096)))

    incident_id = f"mil_{mil_on_ts_ms:x}"
    started = time.time()

    sentinel_analyses: list[dict] = []
    analysis_errors: list[str] = []
    missing_sections_total = 0
    if analyze and dtcs:
        try:
            ctx = {
                'incident_id': incident_id,
                'mil_on_ts_ms': int(mil_on_ts_ms),
                'scan_action': 'analyze_dtcs',
                'scan_report_filename': '',
                'channel_id': int(channel_id),
                'note': 'DTCs provided by user (manual paste)',
            }
            sentinel_analyses = experimental_assistant._sentinel_analyze_dtcs(
                dtcs=dtcs,
                primary=primary,
                incident_context=ctx,
                max_dtcs_override=max_dtcs,
                timeout_s_override=timeout_s,
                num_predict_override=num_predict,
            )
            try:
                for a in sentinel_analyses or []:
                    ms = a.get('missing_sections') if isinstance(a, dict) else None
                    if isinstance(ms, list) and ms:
                        missing_sections_total += len(ms)
            except Exception:
                pass
        except Exception as e:
            analysis_errors.append(str(e))
            sentinel_analyses = []

    # Write unified final report
    dest_dir = os.path.realpath(LOG_FOLDER)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass
    try:
        ts = time.strftime('%Y%m%d_%H%M%S')
    except Exception:
        ts = str(int(time.time()))
    final_report_name = f"sentinel_final_report_manual_{incident_id}_{ts}.html"
    try:
        final_html = build_final_report_html(
            incident_id=incident_id,
            mil_on_ts_ms=int(mil_on_ts_ms),
            scan_action='analyze_dtcs',
            scan_started_ts_ms=int(mil_on_ts_ms),
            scan_finished_ts_ms=int(mil_on_ts_ms),
            scan_report_filename='',
            trace_mf4_filename='',
            trace_raw_mf4_filename='',
            bundle_filename='',
            primary=primary,
            dtcs=dtcs,
            lamp_snapshot={},
            sentinel_analyses=sentinel_analyses,
        )
        with open(os.path.join(dest_dir, final_report_name), 'w', encoding='utf-8') as fp:
            fp.write(final_html)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to write final report: {e}', 'dtc_count': len(dtcs)}), 500

    return jsonify({
        'ok': True,
        'incident_id': incident_id,
        'mil_on_ts_ms': int(mil_on_ts_ms),
        'dtc_count': len(dtcs),
        'primary': primary,
        'sentinel': {
            'enabled': bool(analyze),
            'analyses_count': len(sentinel_analyses) if isinstance(sentinel_analyses, list) else 0,
            'analyses': (sentinel_analyses if isinstance(sentinel_analyses, list) else []),
            'errors': analysis_errors,
            'missing_sections_total': int(missing_sections_total),
            'note': f"timeout_s={timeout_s}, num_predict={num_predict}, max_dtcs={max_dtcs}",
        },
        'final_report_filename': final_report_name,
        'elapsed_s': round(time.time() - started, 3),
        'log_dir': str(dest_dir),
    })


@app.route('/api/monitor/dbcs', methods=['GET'])
def monitor_list_dbcs():
    return jsonify({'ok': True, 'dbcs': data_source_manager.list_dbcs()})


def _mf4_replay_saved_config() -> dict:
    try:
        cfg = config_store.get_config_only()
    except Exception:
        cfg = {}
    obj = cfg.get('mf4_replay') if isinstance(cfg, dict) else None
    return obj if isinstance(obj, dict) else {}


def _mf4_replay_sanitize_config(obj: dict) -> dict:
    if not isinstance(obj, dict):
        obj = {}

    out = {}
    fn = str(obj.get('filename') or '').strip()
    if fn and fn.lower().endswith('.mf4') and os.path.basename(fn) == fn:
        out['filename'] = fn
    else:
        out['filename'] = ''

    try:
        sp = float(obj.get('speed') if obj.get('speed') is not None else 1.0)
    except Exception:
        sp = 1.0
    if sp < 0.0:
        sp = 0.0
    out['speed'] = sp

    out['loop'] = bool(obj.get('loop', False))

    cm = str(obj.get('channel_mode') or 'as_recorded').strip().lower() or 'as_recorded'
    if cm not in {'as_recorded', 'force'}:
        cm = 'as_recorded'
    out['channel_mode'] = cm

    fc = obj.get('force_channel')
    try:
        fc_i = int(fc) if fc is not None and str(fc).strip() != '' else 0
    except Exception:
        fc_i = 0
    out['force_channel'] = fc_i

    try:
        st = float(obj.get('start_s') if obj.get('start_s') is not None else 0.0)
    except Exception:
        st = 0.0
    if st < 0.0:
        st = 0.0
    out['start_s'] = st

    en_raw = obj.get('end_s')
    if en_raw is None or str(en_raw).strip() == '':
        out['end_s'] = None
    else:
        try:
            en = float(en_raw)
        except Exception:
            en = None
        if en is not None and en <= st:
            en = None
        out['end_s'] = en

    try:
        mf = float(obj.get('max_fps') if obj.get('max_fps') is not None else 0.0)
    except Exception:
        mf = 0.0
    if mf < 0.0:
        mf = 0.0
    out['max_fps'] = mf

    return out


@app.route('/api/mf4/replay/config', methods=['GET', 'PUT'])
def mf4_replay_config():
    if request.method == 'GET':
        try:
            cur = _mf4_replay_sanitize_config(_mf4_replay_saved_config())
            return jsonify({'ok': True, 'config': cur})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    patch = request.json or {}
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        cur = _mf4_replay_saved_config()
        merged = dict(cur)
        merged.update(patch)
        sanitized = _mf4_replay_sanitize_config(merged)
        try:
            config_store.update({'mf4_replay': sanitized})
        except Exception:
            pass
        return jsonify({'ok': True, 'config': sanitized})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/mf4/replay/status', methods=['GET'])
def mf4_replay_status():
    try:
        return jsonify({'ok': True, 'status': mf4_replay_service.status()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/replay/stop', methods=['POST'])
def mf4_replay_stop():
    try:
        st = mf4_replay_service.stop()
        return jsonify({'ok': True, 'status': st})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/replay/start', methods=['POST'])
def mf4_replay_start():
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    saved = _mf4_replay_saved_config()
    merged = dict(saved)
    merged.update(data)
    sanitized = _mf4_replay_sanitize_config(merged)
    filename = str(sanitized.get('filename') or '').strip()
    speed = float(sanitized.get('speed') or 0.0)
    loop = bool(sanitized.get('loop', False))
    channel_mode = str(sanitized.get('channel_mode') or 'as_recorded').strip().lower()
    force_channel = sanitized.get('force_channel')
    start_s = float(sanitized.get('start_s') or 0.0)
    end_s = sanitized.get('end_s', None)
    max_fps = float(sanitized.get('max_fps') or 0.0)

    try:
        st = mf4_replay_service.start(
            filename=filename,
            speed=speed,
            loop=loop,
            channel_mode=channel_mode,
            force_channel=force_channel,
            start_s=start_s,
            end_s=end_s,
            max_fps=max_fps,
        )
        try:
            # Persist the last-used replay settings.
            config_store.update({'mf4_replay': sanitized})
        except Exception:
            pass
        return jsonify({'ok': True, 'status': st})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


def _is_local_request() -> bool:
    """Best-effort localhost-only check for demo helpers."""
    try:
        ra = str(getattr(request, 'remote_addr', '') or '').strip()
    except Exception:
        ra = ''
    return ra in {'127.0.0.1', '::1'}


@app.route('/api/monitor/demo_frame', methods=['POST'])
def monitor_demo_frame():
    """Inject decoded frames directly into the comparison engine (localhost only).

    This is a lightweight test helper to validate rule logic (including multi-signal
    conditions) without requiring a real CAN interface or a DBC encoder.

    Payload supports either a single frame or a list of frames:
      {"source_id": "src_...", "channel": 0, "name": "Msg", "signals": {"Sig": 1.23}}
      {"frames": [ ...same fields... ]}
    """
    if not _is_local_request() and not (str(os.getenv('KBSM_ALLOW_DEMO_INJECT', '')).strip() in {'1', 'true', 'yes', 'on'}):
        return jsonify({'ok': False, 'error': 'demo endpoint allowed only from localhost'}), 403

    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    frames = data.get('frames')
    if frames is None:
        frames = [data]
    if not isinstance(frames, list):
        return jsonify({'ok': False, 'error': 'frames must be a list'}), 400

    injected = 0
    errors: list[str] = []

    for i, f in enumerate(frames):
        try:
            if not isinstance(f, dict):
                continue

            # Allow shorthand name/signals or a full decoded object.
            decoded = f.get('decoded') if isinstance(f.get('decoded'), dict) else None
            if decoded is None:
                decoded = {
                    'name': str(f.get('name') or '').strip(),
                    'signals': f.get('signals') if isinstance(f.get('signals'), dict) else {},
                }

            msg_name = str(decoded.get('name') or '').strip()
            sigs = decoded.get('signals')
            if not msg_name or not isinstance(sigs, dict):
                raise ValueError('missing decoded.name/signals')

            try:
                channel = int(f.get('channel') if f.get('channel') is not None else data.get('channel') or 0)
            except Exception:
                channel = 0

            try:
                ts_ms = int(f.get('timestamp_ms') or f.get('ts_ms') or int(time.time() * 1000))
            except Exception:
                ts_ms = int(time.time() * 1000)

            try:
                sid = str(f.get('source_id') or data.get('source_id') or '').strip()
            except Exception:
                sid = ''

            if sid:
                def _resolver(bus_type: str, channel_id: int):
                    try:
                        if str(bus_type or '').strip().upper() != 'CAN':
                            return None
                        return sid
                    except Exception:
                        return None
            else:
                _resolver = _resolve_source_id

            comparison_engine.on_frame({
                'channel': channel,
                'timestamp': ts_ms,
                'decoded': decoded,
                '_source_id_resolver': _resolver,
            })
            injected += 1
        except Exception as e:
            errors.append(f"{i}: {e}")
            continue

    return jsonify({'ok': True, 'injected': injected, 'errors': errors[:10]})


@app.route('/api/monitor/demo_violation', methods=['POST'])
def monitor_demo_violation():
    """Generate a demo violation end-to-end (localhost only).

    Flow: pick a DBC -> pick first message+signal -> set CAN0 source dbc ->
    preload DBC -> create temp rule -> inject frame -> delete temp rule.
    """
    if not _is_local_request() and not (str(os.getenv('KBSM_ALLOW_DEMO_INJECT', '')).strip() in {'1', 'true', 'yes', 'on'}):
        return jsonify({'ok': False, 'error': 'demo endpoint allowed only from localhost'}), 403

    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    try:
        channel_id = int(data.get('channel_id', 0))
    except Exception:
        channel_id = 0

    dbc_name = str(data.get('dbc_name') or '').strip()
    if not dbc_name:
        dbcs = data_source_manager.list_dbcs()
        if not dbcs:
            return jsonify({'ok': False, 'error': 'no dbc files found'}), 400
        dbc_name = dbcs[0]

    if os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400

    dbc_path = os.path.join(UPLOAD_FOLDER_DBC, dbc_name)
    if not os.path.isfile(dbc_path):
        return jsonify({'ok': False, 'error': 'dbc not found'}), 404

    # Ensure the CAN source mapping exists and points to this DBC
    try:
        src_id = data_source_manager.find_can_source_by_channel(channel_id)
        if not src_id:
            data_source_manager.ensure_default_can_sources()
            src_id = data_source_manager.find_can_source_by_channel(channel_id)
        if not src_id:
            return jsonify({'ok': False, 'error': 'no CAN source for channel'}), 400
        data_source_manager.upsert_source({
            'id': src_id,
            'name': f'CAN{channel_id}',
            'type': 'CAN',
            'enabled': True,
            'config': {'channel_id': channel_id, 'bitrate': 500000, 'can_fd': False},
            'dbc_name': dbc_name,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': f'source setup failed: {e}'}), 400

    # Load DBC for decoding injected frames
    try:
        manager.preload_dbcs([{'id': channel_id, 'dbc': dbc_path}])
    except Exception:
        pass

    # Pick message/signal and build a payload that will exceed threshold
    try:
        import cantools
        db = cantools.database.load_file(dbc_path, strict=False)
        if not getattr(db, 'messages', None):
            return jsonify({'ok': False, 'error': 'dbc has no messages'}), 400
        msg = db.messages[0]
        msg_name = str(msg.name)
        frame_id_raw = int(getattr(msg, 'frame_id', 0) or 0)
        arb_id = int(frame_id_raw) & 0x1FFFFFFF
        flags = 4 if ((frame_id_raw & 0x80000000) != 0 or arb_id > 0x7FF) else 0
        if not getattr(msg, 'signals', None):
            return jsonify({'ok': False, 'error': 'selected message has no signals'}), 400
        sig = msg.signals[0]
        sig_name = str(sig.name)

        # Choose values that are valid for encoding
        values = {}
        for s in msg.signals:
            v = 0.0
            try:
                if s.minimum is not None:
                    v = float(s.minimum)
            except Exception:
                v = 0.0
            values[s.name] = v

        want = float(values.get(sig_name, 0.0)) + float(data.get('delta', 10.0) or 10.0)
        try:
            if sig.maximum is not None and want > float(sig.maximum):
                mn = float(sig.minimum) if sig.minimum is not None else 0.0
                mx = float(sig.maximum)
                want = mn + (mx - mn) * 0.5
        except Exception:
            pass
        values[sig_name] = want
        encoded = msg.encode(values)
        payload = [int(x) & 0xFF for x in list(encoded)]
    except Exception as e:
        return jsonify({'ok': False, 'error': f'dbc encode failed: {e}'}), 400

    keep_rule = bool(data.get('keep_rule', False))

    # Create temp rule, inject, then remove rule
    rule_id = None
    try:
        rule = comparison_engine.upsert_rule({
            'name': f'DEMO {dbc_name} {msg_name}.{sig_name}',
            'enabled': True,
            'severity': str(data.get('severity') or 'warning').strip().lower() or 'warning',
            'a': {'source_id': src_id, 'message': msg_name, 'signal': sig_name},
            'op': str(data.get('op') or 'delta_abs').strip().lower() or 'delta_abs',
            'b_kind': 'const',
            'b_const': float(data.get('b_const', 0) or 0),
            'threshold': float(data.get('threshold', 1.0) or 1.0),
            'debounce_s': float(data.get('debounce_s', 0.0) or 0.0),
            'missing_timeout_s': float(data.get('missing_timeout_s', 0.5) or 0.5),
            'actions': [{'kind': 'log_csv'}, {'kind': 'emit_ws'}],
        })
        rule_id = rule.id

        manager.inject_frame(channel_id, arb_id, payload, flags=int(flags), frame_type='CAN')
        return jsonify({
            'ok': True,
            'source_id': src_id,
            'dbc_name': dbc_name,
            'message': msg_name,
            'signal': sig_name,
            'arb_id': arb_id,
            'flags': int(flags),
            'payload': payload,
            'rule_id': rule_id,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if rule_id and not keep_rule:
            try:
                comparison_engine.delete_rule(rule_id)
            except Exception:
                pass


@app.route('/api/monitor/demo_anomaly', methods=['POST'])
def monitor_demo_anomaly():
    """Generate a demo anomaly (and optionally a violation via AI config) end-to-end (localhost only).

    This is primarily for validating the anomaly→violation bridge.
    """
    if not _is_local_request() and not (str(os.getenv('KBSM_ALLOW_DEMO_INJECT', '')).strip() in {'1', 'true', 'yes', 'on'}):
        return jsonify({'ok': False, 'error': 'demo endpoint allowed only from localhost'}), 403

    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    try:
        score = float(data.get('score') or 10.0)
    except Exception:
        score = 10.0
    try:
        threshold = float(data.get('threshold') or 6.0)
    except Exception:
        threshold = 6.0

    # Best-effort pick a real CAN source_id if available.
    try:
        sid = str(data.get('source_id') or '').strip()
    except Exception:
        sid = ''
    if not sid:
        try:
            srcs = data_source_manager.list_sources() or []
            can_src = next((s for s in srcs if isinstance(s, dict) and str(s.get('type') or '').upper() == 'CAN'), None)
            sid = str((can_src or {}).get('id') or '').strip()
        except Exception:
            sid = ''
    if not sid:
        sid = 'CAN0'

    try:
        ts_ms = int(time.time() * 1000)
    except Exception:
        ts_ms = 0

    evt = {
        'id': f"anom_demo_{ts_ms}",
        'ts_ms': ts_ms,
        'source_id': sid,
        'score': float(score),
        'threshold': float(threshold),
        'top': [{'key': 'demo', 'abs_z': float(score), 'value': float(score), 'median': 0.0, 'mad': 1.0}],
        'details': {'kind': 'demo'},
    }

    # Use engine post hook so it respects config (emit/log + anomaly->violation bridge).
    try:
        # Do not force anything; config decides whether it becomes a violation.
        anomaly_engine._post_evt(evt, emit_ws=True, log_db=True)  # type: ignore[attr-defined]
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True, 'event': evt})


@app.route('/api/ai/config', methods=['GET', 'PUT'])
def ai_config():
    if request.method == 'GET':
        try:
            return jsonify(anomaly_engine.get_config())
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        out = anomaly_engine.update_config(data)
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai/status', methods=['GET'])
def ai_status():
    try:
        return jsonify(anomaly_engine.status())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ai/train_live', methods=['POST'])
def ai_train_live():
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        duration_s = float(data.get('duration_s') or 120.0)
    except Exception:
        duration_s = 120.0
    try:
        max_samples = int(data.get('max_samples') or 2000)
    except Exception:
        max_samples = 2000
    try:
        out = anomaly_engine.train_live(duration_s=duration_s, max_samples=max_samples)
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/ai/anomalies', methods=['GET'])
def ai_list_anomalies():
    try:
        source_id = (request.args.get('source_id') or '').strip() or None
        limit = int(request.args.get('limit') or 200)
        desc = str(request.args.get('desc') or 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
        since_ms = request.args.get('since_ms')
        until_ms = request.args.get('until_ms')
        since_v = int(since_ms) if (since_ms is not None and str(since_ms).strip() != '') else None
        until_v = int(until_ms) if (until_ms is not None and str(until_ms).strip() != '') else None
    except Exception:
        source_id, limit, desc, since_v, until_v = None, 200, True, None, None
    try:
        return jsonify(anomaly_logger.query(source_id=source_id, since_ms=since_v, until_ms=until_v, limit=limit, desc=desc))
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ai/suggest_rules', methods=['POST'])
def ai_suggest_rules():
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    base = str(data.get('base') or '').strip()
    if not base:
        return jsonify({'ok': False, 'error': 'base required (session_...)'}), 400
    if '.' in base:
        base = base.split('.', 1)[0]
    if not base.startswith('session_'):
        return jsonify({'ok': False, 'error': 'invalid base'}), 400

    # Prefer CSV for rule suggestion (decoded payloads live in the Decoded column)
    csv_name = f"{base}.csv"
    csv_path = _find_log_file(csv_name)
    if not csv_path:
        return jsonify({'ok': False, 'error': 'session csv not found'}), 404

    def _ch_to_source(ch: int) -> str | None:
        try:
            return data_source_manager.find_can_source_by_channel(int(ch))
        except Exception:
            return None

    try:
        min_count = int(data.get('min_count') or 200)
    except Exception:
        min_count = 200
    try:
        max_samples = int(data.get('max_samples_per_signal') or 5000)
    except Exception:
        max_samples = 5000
    try:
        margin_fraction = float(data.get('margin_fraction') or 0.05)
    except Exception:
        margin_fraction = 0.05
    severity = str(data.get('severity') or 'warning').strip().lower() or 'warning'

    try:
        out = suggest_rules_from_session_csv(
            csv_path,
            channel_to_source_id=_ch_to_source,
            min_count=min_count,
            max_samples_per_signal=max_samples,
            margin_fraction=margin_fraction,
            severity=severity,
        )
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ai/apply_suggestions', methods=['POST'])
def ai_apply_suggestions():
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    rules = data.get('rules')
    if not isinstance(rules, list) or not rules:
        return jsonify({'ok': False, 'error': 'rules[] required'}), 400

    applied = 0
    errors = []
    for obj in rules:
        if not isinstance(obj, dict):
            continue
        # Strip AI helper fields
        obj.pop('_ai_reason', None)
        try:
            comparison_engine.upsert_rule(obj)
            applied += 1
        except Exception as e:
            errors.append(str(e))

    try:
        comparison_engine.reload()
    except Exception:
        pass

    return jsonify({'ok': True, 'applied': applied, 'errors': errors[:30]})


@app.route('/api/sources', methods=['GET', 'POST'])
def api_sources():
    if request.method == 'GET':
        sources = data_source_manager.list_sources()

        # Back-compat / UX: Channel Configuration stores DBC selection under
        # config_store.logger_channels.{dbc_names|dbc_name}. DataSourceManager
        # stores per-source dbc_name (single). If the user has associated DBCs
        # to a CAN channel but did not explicitly set dbc_name on the source,
        # surface the channel's DBCs here so Rules/Signal Picker can work.
        try:
            cfg = config_store.get_config_only() or {}
            chans = cfg.get('logger_channels') if isinstance(cfg, dict) else None
            chans = chans if isinstance(chans, list) else []
            dbcs_by_ch: dict[int, list[str]] = {}
            for c in chans:
                if not isinstance(c, dict):
                    continue
                try:
                    ch_id = int(c.get('id'))
                except Exception:
                    continue
                names: list[str] = []
                try:
                    if isinstance(c.get('dbc_names'), list) and c.get('dbc_names'):
                        names = [str(x or '').strip() for x in c.get('dbc_names') if str(x or '').strip()]
                    else:
                        dn = str(c.get('dbc_name') or '').strip()
                        if dn:
                            names = [dn]
                except Exception:
                    names = []
                cleaned = [os.path.basename(n) for n in names if os.path.basename(str(n or '').strip())]
                if cleaned:
                    dbcs_by_ch[ch_id] = cleaned

            for s in sources:
                try:
                    if not isinstance(s, dict):
                        continue
                    if str(s.get('type') or '') != 'CAN':
                        continue
                    cfg_s = s.get('config') if isinstance(s.get('config'), dict) else {}
                    ch_id = int(cfg_s.get('channel_id'))
                    names = dbcs_by_ch.get(ch_id) or []
                    if not names:
                        continue
                    # Expose all configured DBCs for UI convenience.
                    s['dbc_names'] = list(names)
                    # Only auto-fill dbc_name if source didn't set it.
                    if not str(s.get('dbc_name') or '').strip():
                        s['dbc_name'] = names[0]
                except Exception:
                    continue
        except Exception:
            pass

        return jsonify({'ok': True, 'sources': sources})

    obj = request.json or {}
    if not isinstance(obj, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        src = data_source_manager.upsert_source(obj)
        return jsonify({'ok': True, 'source': src.to_dict()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/sources/<source_id>', methods=['GET', 'PUT', 'DELETE'])
def api_source_item(source_id: str):
    sid = str(source_id or '').strip()
    if not sid:
        return jsonify({'ok': False, 'error': 'missing source_id'}), 400

    if request.method == 'GET':
        s = data_source_manager.get_source(sid)
        if not s:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        return jsonify({'ok': True, 'source': s.to_dict()})

    if request.method == 'DELETE':
        ok = data_source_manager.delete_source(sid)
        return jsonify({'ok': bool(ok)})

    patch = request.json or {}
    if not isinstance(patch, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    prev = data_source_manager.get_source(sid)
    if not prev:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    merged = prev.to_dict()
    merged.update(patch)
    merged['id'] = sid
    try:
        src = data_source_manager.upsert_source(merged)
        return jsonify({'ok': True, 'source': src.to_dict()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/sources/export', methods=['GET'])
def api_sources_export():
    return jsonify({'ok': True, 'sources': data_source_manager.list_sources()})


@app.route('/api/sources/import', methods=['POST'])
def api_sources_import():
    obj = request.json or {}
    if not isinstance(obj, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    raw = obj.get('sources')
    if not isinstance(raw, list):
        return jsonify({'ok': False, 'error': 'missing sources[]'}), 400
    saved = 0
    errors = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            data_source_manager.upsert_source(item)
            saved += 1
        except Exception as e:
            errors.append(str(e))
    return jsonify({'ok': True, 'saved': saved, 'errors': errors[:20]})


@app.route('/api/sources/<source_id>/test', methods=['POST'])
def api_source_test(source_id: str):
    sid = str(source_id or '').strip()
    s = data_source_manager.get_source(sid)
    if not s:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    if s.type != 'CAN':
        return jsonify({'ok': False, 'error': 'MVP supports CAN test only'}), 400
    try:
        ch_id = int((s.config or {}).get('channel_id'))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid channel_id'}), 400

    if not bool(getattr(manager, 'running', False)):
        return jsonify({'ok': False, 'error': 'bus is not running. Start Bus System first.'}), 400

    frames = []
    ids = set()
    start_s = time.time()
    done = threading.Event()

    def _cap(frame):
        try:
            if int(frame.get('channel')) != ch_id:
                return
            if len(frames) >= 80:
                done.set()
                return
            ids.add(int(frame.get('id') or 0))
            ts = int(frame.get('timestamp') or 0)
            data = frame.get('data')
            if isinstance(data, (bytes, bytearray)):
                data_hex = data.hex()
            elif isinstance(data, list):
                data_hex = ' '.join([f"{int(x)&0xFF:02X}" for x in data])
            else:
                data_hex = str(data)
            dec = frame.get('decoded')
            dec_s = ''
            if isinstance(dec, dict):
                name = str(dec.get('name') or '')
                sigs = dec.get('signals')
                if isinstance(sigs, dict) and name:
                    # keep it compact
                    pairs = []
                    for k, v in list(sigs.items())[:10]:
                        pairs.append(f"{k}={v}")
                    dec_s = name + ' ' + ', '.join(pairs)
            frames.append({
                'ts': time.strftime('%H:%M:%S', time.localtime(ts / 1000.0)) if ts else '',
                'id': hex(int(frame.get('id') or 0)),
                'data': data_hex,
                'decoded': dec_s,
            })
        except Exception:
            return

    manager.add_listener(_cap)
    try:
        done.wait(timeout=5.0)
    finally:
        try:
            manager.remove_listener(_cap)
        except Exception:
            pass

    elapsed = max(0.001, time.time() - start_s)
    fps = int(len(frames) / elapsed)
    return jsonify({'ok': True, 'frames': frames, 'stats': {'fps': fps, 'unique_ids': len(ids)}})


@app.route('/api/comparison/rules', methods=['GET', 'POST'])
def api_comparison_rules():
    if request.method == 'GET':
        return jsonify({'ok': True, 'rules': comparison_engine.list_rules()})
    obj = request.json or {}
    if not isinstance(obj, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        r = comparison_engine.upsert_rule(obj)
        return jsonify({'ok': True, 'rule': r.to_dict()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/comparison/rules/<rule_id>', methods=['GET', 'PUT', 'DELETE'])
def api_comparison_rule_item(rule_id: str):
    rid = str(rule_id or '').strip()
    if not rid:
        return jsonify({'ok': False, 'error': 'missing rule_id'}), 400

    if request.method == 'DELETE':
        ok = comparison_engine.delete_rule(rid)
        return jsonify({'ok': bool(ok)})

    if request.method == 'GET':
        rules = {r.get('id'): r for r in (comparison_engine.list_rules() or []) if isinstance(r, dict)}
        r = rules.get(rid)
        if not r:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        return jsonify({'ok': True, 'rule': r})

    patch = request.json or {}
    if not isinstance(patch, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    rules = {r.get('id'): r for r in (comparison_engine.list_rules() or []) if isinstance(r, dict)}
    prev = rules.get(rid)
    if not prev:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    merged = dict(prev)
    merged.update(patch)
    merged['id'] = rid
    try:
        r = comparison_engine.upsert_rule(merged)
        return jsonify({'ok': True, 'rule': r.to_dict()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/comparison/rules/reload', methods=['POST'])
def api_comparison_rules_reload():
    try:
        comparison_engine.reload()
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/violations', methods=['GET'])
def api_violations_query():
    def _int_arg(name):
        v = request.args.get(name)
        if v is None or v == '':
            return None
        try:
            return int(float(v))
        except Exception:
            return None

    start_ms = _int_arg('start_ms')
    end_ms = _int_arg('end_ms')
    severity = (request.args.get('severity') or '').strip().lower() or None
    rule_id = (request.args.get('rule_id') or '').strip() or None
    limit = _int_arg('limit') or 200
    offset = _int_arg('offset') or 0
    desc = str(request.args.get('desc') or 'true').strip().lower() not in {'0', 'false', 'no'}
    try:
        return jsonify(violation_logger.query(
            start_ms=start_ms,
            end_ms=end_ms,
            severity=severity,
            rule_id=rule_id,
            limit=limit,
            offset=offset,
            desc=desc,
        ))
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/violations/statistics', methods=['GET'])
def api_violations_stats():
    try:
        return jsonify(violation_logger.stats_last_24h())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/violations/clear', methods=['POST'])
def api_violations_clear():
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    delete_csv = bool(data.get('delete_csv', False))
    try:
        out = violation_logger.clear(delete_csv=delete_csv)
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


_dbc_describe_cache = {}


def _truthy(v) -> bool:
    return str(v or '').strip().lower() in {'1', 'true', 'yes', 'on'}


@app.route('/api/dbc/describe', methods=['GET'])
def describe_dbc():
    """Describe a DBC file in databases/dbc: messages + signals (for UI selection)."""
    dbc_name = (request.args.get('dbc_name') or '').strip()
    if not dbc_name or os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400

    include_comments = _truthy(request.args.get('include_comments', '1'))

    path = os.path.join(UPLOAD_FOLDER_DBC, dbc_name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'dbc not found'}), 404

    try:
        st = os.stat(path)
        cache_key = (dbc_name, float(st.st_mtime), int(st.st_size), 'describe', bool(include_comments))
    except Exception:
        cache_key = (dbc_name, None, None, 'describe', bool(include_comments))

    cached = _dbc_describe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        from dbc_loader import load_dbc_database
        db = load_dbc_database(path)
        msgs = []
        for m in (db.messages or []):
            sigs = []
            for s in (m.signals or []):
                sigs.append({
                    'name': s.name,
                    'unit': getattr(s, 'unit', None),
                    'comment': (getattr(s, 'comment', None) if include_comments else None),
                    'is_signed': bool(getattr(s, 'is_signed', False)),
                    'is_float': bool(getattr(s, 'is_float', False)),
                    'minimum': getattr(s, 'minimum', None),
                    'maximum': getattr(s, 'maximum', None),
                })
            msgs.append({
                'name': m.name,
                'frame_id': int(getattr(m, 'frame_id', 0) or 0),
                'length': int(getattr(m, 'length', 0) or 0),
                'comment': (getattr(m, 'comment', None) if include_comments else None),
                'signals': sigs,
            })
        out = {'ok': True, 'dbc_name': dbc_name, 'messages': msgs}
        # Keep this cache small to avoid unbounded growth.
        if len(_dbc_describe_cache) > 12:
            _dbc_describe_cache.clear()
        _dbc_describe_cache[cache_key] = out
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dbc/catalog', methods=['GET'])
def dbc_catalog():
    """Return a compact list of messages with descriptions (comments).

    Intended for: "message catalog" usage (Copilot context, UI tooltips, etc).

    Query:
      - dbc_name: required
      - max_messages: default 500
    """
    dbc_name = (request.args.get('dbc_name') or '').strip()
    if not dbc_name or os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400

    try:
        max_messages = int(request.args.get('max_messages') or 500)
    except Exception:
        max_messages = 500
    max_messages = max(1, min(max_messages, 5000))

    include_signals = _truthy(request.args.get('include_signals', '0'))
    try:
        max_signals_per_msg = int(request.args.get('max_signals_per_msg') or 200)
    except Exception:
        max_signals_per_msg = 200
    max_signals_per_msg = max(1, min(max_signals_per_msg, 5000))

    # Use describe endpoint logic + cache by delegating to describe handler via cache.
    # We call the loader directly to avoid recursively calling Flask handlers.
    path = os.path.join(UPLOAD_FOLDER_DBC, dbc_name)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'dbc not found'}), 404

    try:
        st = os.stat(path)
        cache_key = (
            dbc_name,
            float(st.st_mtime),
            int(st.st_size),
            'catalog',
            bool(include_signals),
            int(max_messages),
            int(max_signals_per_msg),
        )
    except Exception:
        cache_key = (dbc_name, None, None, 'catalog', bool(include_signals), int(max_messages), int(max_signals_per_msg))

    cached = _dbc_describe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        from dbc_loader import load_dbc_database
        db = load_dbc_database(path)
        msgs = []
        for m in (db.messages or []):
            msg = {
                'name': getattr(m, 'name', None),
                'frame_id': int(getattr(m, 'frame_id', 0) or 0),
                'length': int(getattr(m, 'length', 0) or 0),
                'comment': getattr(m, 'comment', None),
            }
            if include_signals:
                sigs = []
                for s in (getattr(m, 'signals', None) or []):
                    sigs.append({
                        'name': getattr(s, 'name', None),
                        'unit': getattr(s, 'unit', None),
                        'comment': getattr(s, 'comment', None),
                    })
                    if len(sigs) >= max_signals_per_msg:
                        break
                msg['signals'] = sigs

            msgs.append(msg)
            if len(msgs) >= max_messages:
                break
        out = {'ok': True, 'dbc_name': dbc_name, 'messages': msgs, 'count': int(len(msgs))}
        _dbc_describe_cache[cache_key] = out

        # Optional persistence (best-effort, never blocks the response)
        if _truthy(request.args.get('persist', '0')):
            try:
                dbc_catalog_db.import_dbc_file(
                    dbc_name=dbc_name,
                    path=path,
                    include_signals=bool(include_signals),
                    force=False,
                )
            except Exception:
                pass
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dbc/dbcs', methods=['GET'])
def api_dbc_db_list():
    """List DBCs already imported into the persistent catalog db."""
    try:
        return jsonify({'ok': True, 'items': dbc_catalog_db.list_dbcs()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dbc/import', methods=['POST'])
def api_dbc_db_import():
    """Import one or all .dbc files from databases/dbc into the persistent catalog db."""
    data = request.json or {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    dbc_name = str(data.get('dbc_name') or '').strip()
    import_all = bool(data.get('import_all', False))
    include_signals = bool(data.get('include_signals', True))
    force = bool(data.get('force', False))

    to_import = []
    if import_all or not dbc_name:
        try:
            for name in os.listdir(UPLOAD_FOLDER_DBC):
                if not isinstance(name, str):
                    continue
                if not name.lower().endswith('.dbc'):
                    continue
                if os.path.basename(name) != name:
                    continue
                to_import.append(name)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500
    else:
        if os.path.basename(dbc_name) != dbc_name:
            return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400
        to_import = [dbc_name]

    imported = 0
    skipped = 0
    errors = []
    details = []
    for name in to_import:
        path = os.path.join(UPLOAD_FOLDER_DBC, name)
        try:
            r = dbc_catalog_db.import_dbc_file(
                dbc_name=name,
                path=path,
                include_signals=include_signals,
                force=force,
            )
            details.append(r)
            if r.get('imported'):
                imported += 1
            elif r.get('skipped'):
                skipped += 1
        except Exception as e:
            errors.append({'dbc_name': name, 'error': str(e)})

    return jsonify({
        'ok': True,
        'requested': int(len(to_import)),
        'imported': int(imported),
        'skipped': int(skipped),
        'errors': errors,
        'details': details,
    })


@app.route('/api/dbc/catalog_db', methods=['GET'])
def api_dbc_db_catalog():
    """Serve the DBC catalog from the persistent db (fast, stable)."""
    dbc_name = (request.args.get('dbc_name') or '').strip()
    if not dbc_name or os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400

    include_signals = _truthy(request.args.get('include_signals', '0'))
    try:
        max_messages = int(request.args.get('max_messages') or 500)
    except Exception:
        max_messages = 500
    try:
        max_signals_per_msg = int(request.args.get('max_signals_per_msg') or 200)
    except Exception:
        max_signals_per_msg = 200

    try:
        out = dbc_catalog_db.get_catalog(
            dbc_name=dbc_name,
            include_signals=bool(include_signals),
            max_messages=max_messages,
            max_signals_per_msg=max_signals_per_msg,
        )
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dbc/signals_db', methods=['GET'])
def api_dbc_db_signals_for_message():
    dbc_name = (request.args.get('dbc_name') or '').strip()
    msg_name = (request.args.get('message') or '').strip()
    if not dbc_name or os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400
    if not msg_name:
        return jsonify({'ok': False, 'error': 'missing message'}), 400

    try:
        limit = int(request.args.get('limit') or 2000)
    except Exception:
        limit = 2000
    try:
        offset = int(request.args.get('offset') or 0)
    except Exception:
        offset = 0

    try:
        out = dbc_catalog_db.get_signals_for_message(
            dbc_name=dbc_name,
            msg_name=msg_name,
            limit=limit,
            offset=offset,
        )
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/dbc/search_db', methods=['GET'])
def api_dbc_db_search_signals():
    """Search signals in the persistent DBC catalog DB by keyword.

    Params:
      - q: search query (required)
      - dbc_name: restrict to one DBC (optional)
      - limit: max results (optional)
    """
    q = str(request.args.get('q') or '').strip()
    if not q:
        return jsonify({'ok': False, 'error': 'missing q'}), 400

    dbc_name = str(request.args.get('dbc_name') or '').strip()
    if dbc_name and os.path.basename(dbc_name) != dbc_name:
        return jsonify({'ok': False, 'error': 'invalid dbc_name'}), 400

    try:
        limit = int(request.args.get('limit') or 50)
    except Exception:
        limit = 50

    try:
        out = dbc_catalog_db.search_signals(query=q, dbc_name=(dbc_name or None), limit=limit)
        return jsonify(out)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/trigger/can', methods=['GET', 'POST'])
def can_trigger_config():
    global _can_trigger_cfg
    if request.method == 'GET':
        return jsonify({
            'armed': bool(_can_trigger_cfg.get('armed')),
            'channel_id': int(_can_trigger_cfg.get('channel_id', 0) or 0),
            'dbc_name': str(_can_trigger_cfg.get('dbc_name') or ''),
            'message': str(_can_trigger_cfg.get('message') or ''),
            'signal': str(_can_trigger_cfg.get('signal') or ''),
            'start_op': str(_can_trigger_cfg.get('start_op') or 'eq'),
            'start_value': _can_trigger_cfg.get('start_value'),
            'auto_stop_enabled': bool(_can_trigger_cfg.get('auto_stop_enabled')),
            'no_message_stop_s': float(_can_trigger_cfg.get('no_message_stop_s') or 0.0),
            'stop_op': str(_can_trigger_cfg.get('stop_op') or 'eq'),
            'stop_value': _can_trigger_cfg.get('stop_value'),
            'formats': list(_can_trigger_cfg.get('formats') or []),
        })

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    # Update config (best-effort parsing)
    try:
        if 'channel_id' in data:
            _can_trigger_cfg['channel_id'] = int(data.get('channel_id') or 0)
    except Exception:
        pass
    try:
        if 'dbc_name' in data:
            _can_trigger_cfg['dbc_name'] = str(data.get('dbc_name') or '').strip()
    except Exception:
        pass
    try:
        if 'message' in data:
            _can_trigger_cfg['message'] = str(data.get('message') or '').strip()
    except Exception:
        pass
    try:
        if 'signal' in data:
            _can_trigger_cfg['signal'] = str(data.get('signal') or '').strip()
    except Exception:
        pass
    try:
        if 'start_op' in data:
            _can_trigger_cfg['start_op'] = str(data.get('start_op') or 'eq').strip().lower()
        if 'stop_op' in data:
            _can_trigger_cfg['stop_op'] = str(data.get('stop_op') or 'eq').strip().lower()
    except Exception:
        pass
    if 'start_value' in data:
        _can_trigger_cfg['start_value'] = data.get('start_value')
    if 'stop_value' in data:
        _can_trigger_cfg['stop_value'] = data.get('stop_value')

    if 'auto_stop_enabled' in data:
        try:
            _can_trigger_cfg['auto_stop_enabled'] = bool(data.get('auto_stop_enabled'))
        except Exception:
            pass
    if 'no_message_stop_s' in data:
        try:
            _can_trigger_cfg['no_message_stop_s'] = float(data.get('no_message_stop_s') or 0.0)
        except Exception:
            pass

    if 'formats' in data and isinstance(data.get('formats'), list):
        _can_trigger_cfg['formats'] = [str(x).strip() for x in data.get('formats') if str(x).strip()]

    if 'armed' in data:
        _can_trigger_cfg['armed'] = bool(data.get('armed'))
        if _can_trigger_cfg['armed']:
            try:
                _manual_stop_latch['can'] = False
            except Exception:
                pass

    try:
        config_store.update({'can_trigger': {
            'armed': bool(_can_trigger_cfg.get('armed')),
            'channel_id': int(_can_trigger_cfg.get('channel_id', 0) or 0),
            'dbc_name': str(_can_trigger_cfg.get('dbc_name') or ''),
            'message': str(_can_trigger_cfg.get('message') or ''),
            'signal': str(_can_trigger_cfg.get('signal') or ''),
            'start_op': str(_can_trigger_cfg.get('start_op') or 'eq'),
            'start_value': _can_trigger_cfg.get('start_value'),
            'auto_stop_enabled': bool(_can_trigger_cfg.get('auto_stop_enabled')),
            'no_message_stop_s': float(_can_trigger_cfg.get('no_message_stop_s') or 0.0),
            'stop_op': str(_can_trigger_cfg.get('stop_op') or 'eq'),
            'stop_value': _can_trigger_cfg.get('stop_value'),
            'formats': list(_can_trigger_cfg.get('formats') or []),
        }})
    except Exception:
        pass

    return jsonify({'ok': True, **_can_trigger_cfg})


@app.route('/api/kl15', methods=['GET', 'POST'])
def kl15_monitor_api():
    """KL_15 ignition monitor: auto-start/stop recording based on ignition signal.

    GET returns current config and live state.
    POST body (all fields optional):
      {
        "enabled": true|false,
        "formats": ["mf4"],
        "signal_names": ["ZAS_Kl_15", "KL_15"],
        "message_filter": "Klemmen_Status",
        "on_threshold": 0.5,
        "off_debounce_s": 3.0
      }
    """
    global _kl15_monitor_cfg, _kl15_state

    if request.method == 'POST':
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

        if 'enabled' in data:
            _kl15_monitor_cfg['enabled'] = bool(data.get('enabled'))
            # Reset state when toggling
            if not _kl15_monitor_cfg['enabled']:
                _kl15_state['detected'] = False
                _kl15_state['recording'] = False
                _kl15_state['last_off_ts'] = 0.0

        if isinstance(data.get('formats'), list):
            fmts = [str(x).strip() for x in data.get('formats') if str(x).strip()]
            _kl15_monitor_cfg['formats'] = fmts or ['mf4']

        if isinstance(data.get('signal_names'), list):
            names = [str(x).strip() for x in data.get('signal_names') if str(x).strip()]
            if names:
                _kl15_monitor_cfg['signal_names'] = names

        if 'message_filter' in data:
            _kl15_monitor_cfg['message_filter'] = str(data.get('message_filter') or '')

        if 'on_threshold' in data:
            try:
                _kl15_monitor_cfg['on_threshold'] = float(data.get('on_threshold') or 0.5)
            except Exception:
                pass

        if 'off_debounce_s' in data:
            try:
                _kl15_monitor_cfg['off_debounce_s'] = float(data.get('off_debounce_s') or 3.0)
            except Exception:
                pass

        # Persist to config
        try:
            config_store.update({'kl15_monitor': {
                'enabled': bool(_kl15_monitor_cfg.get('enabled')),
                'formats': list(_kl15_monitor_cfg.get('formats') or []),
                'signal_names': list(_kl15_monitor_cfg.get('signal_names') or []),
                'message_filter': str(_kl15_monitor_cfg.get('message_filter') or ''),
                'on_threshold': float(_kl15_monitor_cfg.get('on_threshold') or 0.5),
                'off_debounce_s': float(_kl15_monitor_cfg.get('off_debounce_s') or 3.0),
            }})
        except Exception:
            pass

    return jsonify({
        'config': {
            'enabled': bool(_kl15_monitor_cfg.get('enabled')),
            'formats': list(_kl15_monitor_cfg.get('formats') or []),
            'signal_names': list(_kl15_monitor_cfg.get('signal_names') or []),
            'message_filter': str(_kl15_monitor_cfg.get('message_filter') or ''),
            'on_threshold': float(_kl15_monitor_cfg.get('on_threshold') or 0.5),
            'off_debounce_s': float(_kl15_monitor_cfg.get('off_debounce_s') or 3.0),
        },
        'state': {
            'detected': bool(_kl15_state.get('detected')),
            'recording': bool(_kl15_state.get('recording')),
            'last_on_ts': float(_kl15_state.get('last_on_ts') or 0.0),
            'last_off_ts': float(_kl15_state.get('last_off_ts') or 0.0),
            'last_value': _kl15_state.get('last_value'),
            'last_signal_name': _kl15_state.get('last_signal_name'),
            'last_message_name': _kl15_state.get('last_message_name'),
        },
    })


@app.route('/api/trigger/eth', methods=['GET', 'POST'])
def eth_trigger_config():
    """Configure Ethernet trigger.

    GET returns current config.
    POST body:
      {
        "armed": true|false,
        "formats": ["csv","mf4",...],
        "cooldown_s": 2.0
      }
    """
    global _eth_trigger_cfg

    if request.method == 'POST':
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

        try:
            if 'armed' in data:
                _eth_trigger_cfg['armed'] = bool(data.get('armed'))
                if _eth_trigger_cfg['armed']:
                    try:
                        _manual_stop_latch['eth'] = False
                    except Exception:
                        pass
            if isinstance(data.get('formats'), list):
                fmts = [str(x).strip() for x in data.get('formats') if str(x).strip()]
                _eth_trigger_cfg['formats'] = fmts or ['csv', 'txt']
            if data.get('cooldown_s') is not None:
                _eth_trigger_cfg['cooldown_s'] = float(data.get('cooldown_s'))
        except Exception:
            pass

        try:
            config_store.update({'eth_trigger': {
                'armed': bool(_eth_trigger_cfg.get('armed')),
                'formats': list(_eth_trigger_cfg.get('formats') or []),
                'cooldown_s': float(_eth_trigger_cfg.get('cooldown_s') or 0.0),
            }})
        except Exception:
            pass

        _log_event('eth_trigger_config', dict(_eth_trigger_cfg))

    return jsonify({
        'armed': bool(_eth_trigger_cfg.get('armed')),
        'formats': list(_eth_trigger_cfg.get('formats') or []),
        'cooldown_s': float(_eth_trigger_cfg.get('cooldown_s') or 0.0),
    })


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        data = request.json or {}
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'config must be an object'}), 400
        try:
            # Enforce system mode constraints (keeps UI/config coherent)
            try:
                cur_cfg = config_store.get_config_only() or {}
            except Exception:
                cur_cfg = {}
            try:
                data = _enforce_system_mode_patch(current_cfg=cur_cfg, patch=data)
            except Exception:
                pass

            # If CAN+DoIP profile is selected and mirror CAN list is empty,
            # infer gateway_mirror.can from enabled CAN sources.
            try:
                profile_name = str(data.get('profile') or cur_cfg.get('profile') or '').strip().lower()
            except Exception:
                profile_name = ''
            if profile_name == 'can_doip':
                try:
                    gm_cur = cur_cfg.get('gateway_mirror') if isinstance(cur_cfg.get('gateway_mirror'), dict) else {}
                    gm_patch = data.get('gateway_mirror') if isinstance(data.get('gateway_mirror'), dict) else {}
                    gm_eff = dict(gm_cur)
                    gm_eff.update(gm_patch)

                    can_cfg = gm_eff.get('can') if isinstance(gm_eff.get('can'), list) else []
                    if not can_cfg:
                        inferred_can = []
                        for src in (data_source_manager.list_sources() or []):
                            try:
                                if not isinstance(src, dict):
                                    continue
                                if str(src.get('type') or '').strip().upper() != 'CAN':
                                    continue
                                if not bool(src.get('enabled', True)):
                                    continue
                                cfg_s = src.get('config') if isinstance(src.get('config'), dict) else {}
                                ch_id = int(cfg_s.get('channel_id'))
                                bus_num = int(ch_id) + 1
                                if 1 <= bus_num <= 8 and bus_num not in inferred_can:
                                    inferred_can.append(bus_num)
                            except Exception:
                                continue

                        if inferred_can:
                            gm_eff['can'] = inferred_can
                            data = dict(data)
                            data['gateway_mirror'] = gm_eff
                except Exception:
                    pass

            # Apply MF4 decoded-channel selection immediately (persisted via config_store.update below).
            if 'mf4_include_decoded' in data:
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change MF4 decoded setting while logging is active'}), 409
                try:
                    shared_logger.set_mf4_include_decoded(bool(data.get('mf4_include_decoded')))
                    # Keep env in sync for any components still reading env.
                    os.environ['MF4_INCLUDE_DECODED'] = '1' if bool(data.get('mf4_include_decoded')) else '0'
                except Exception:
                    pass

            if 'mf4_include_raw' in data:
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change MF4 raw setting while logging is active'}), 409
                try:
                    shared_logger.set_mf4_include_raw(bool(data.get('mf4_include_raw')))
                    os.environ['MF4_INCLUDE_RAW'] = '1' if bool(data.get('mf4_include_raw')) else '0'
                except Exception:
                    pass

            # Control decoded payload verbosity for txt/csv/json logs.
            # Safe to change while logging is active (affects subsequent writes only).
            if 'log_decoded_mode' in data:
                try:
                    shared_logger.set_log_decoded_mode(data.get('log_decoded_mode'))
                    os.environ['LOG_DECODED_MODE'] = str(data.get('log_decoded_mode'))
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 400

            # Apply storage directory immediately (persisted via config_store.update below).
            if 'storage' in data and isinstance(data.get('storage'), dict):
                # Do not allow changing while logging is active.
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change storage while logging is active'}), 409
                try:
                    if getattr(eth_manager, 'mf4_logger', None) is not None:
                        return jsonify({'ok': False, 'error': 'cannot change storage while ethernet mf4 logging is active'}), 409
                except Exception:
                    pass

                try:
                    resolved = _ensure_writable_dir(_resolve_storage_output_dir({'storage': data.get('storage')}))
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 400

                try:
                    global LOG_FOLDER
                    LOG_FOLDER = str(resolved)
                    shared_logger.set_log_dir(LOG_FOLDER)
                    try:
                        os.environ['KBSM_LOG_DIR'] = str(LOG_FOLDER)
                    except Exception:
                        pass
                    try:
                        eth_manager.log_dir = getattr(shared_logger, 'log_dir', LOG_FOLDER)
                    except Exception:
                        pass
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 500

            # Apply MF4 chunk size immediately (persisted via config_store.update below).
            if 'mf4_chunk_size_mb' in data:
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change MF4 chunk size while logging is active'}), 409
                try:
                    shared_logger.set_mf4_chunk_size_mb(data.get('mf4_chunk_size_mb'))
                    # Keep env in sync for any components still reading env.
                    os.environ['MF4_CHUNK_SIZE_MB'] = str(data.get('mf4_chunk_size_mb'))
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 400

            # Apply MF4 part time limit (minutes in config, seconds internally).
            if 'mf4_part_time_limit_min' in data:
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change MF4 part time limit while logging is active'}), 409
                try:
                    shared_logger.set_mf4_part_time_limit_s(float(data.get('mf4_part_time_limit_min')) * 60.0)
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 400

            # Apply MF4 intermediate flush interval (MB).
            if 'mf4_flush_interval_mb' in data:
                if bool(getattr(shared_logger, 'active', False)):
                    return jsonify({'ok': False, 'error': 'cannot change MF4 flush interval while logging is active'}), 409
                try:
                    shared_logger.set_mf4_flush_interval_mb(data.get('mf4_flush_interval_mb'))
                except Exception as e:
                    return jsonify({'ok': False, 'error': str(e)}), 400

            # Keep runtime toggles in sync when config is updated.
            if 'video_recording_enabled' in data:
                try:
                    global _video_recording_enabled
                    _video_recording_enabled = bool(data.get('video_recording_enabled'))
                    if not _video_recording_enabled:
                        try:
                            _video_recorder.stop()
                        except Exception:
                            pass
                except Exception:
                    pass
            saved = config_store.update(data)
            try:
                _rebuild_mirror_channel_map()
            except Exception:
                pass
            try:
                if 'logger_channels' in data:
                    saved_cfg = saved.get('config') if isinstance(saved, dict) else None
                    _reconcile_runtime_bus_with_logger_config(saved_cfg if isinstance(saved_cfg, dict) else {})
            except Exception:
                pass
            return jsonify({'ok': True, **saved})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True, **(config_store.load() or {})})


def _group_sessions() -> dict:
    """Group log files by base session name (session_YYYYmmdd_HHMMSS)."""
    sessions = {}
    for folder in _iter_log_folders():
        if not os.path.isdir(folder):
            continue
        try:
            for f in os.listdir(folder):
                if not f.startswith('session_'):
                    continue
                # Skip incomplete artifacts that can appear during rolling writes.
                # These should not show up in the GUI as a session file.
                if f.endswith('.tmp.mf4') or f.endswith('.tmp'):
                    continue
                base = f
                if '.' in base:
                    base = base.split('.', 1)[0]

                # Normalize chunked MF4 files: session_..._part0003.mf4 -> session_...
                # This keeps sessions coherent in the GUI even when MF4 is chunked.
                try:
                    if '_part' in base:
                        head, tail = base.rsplit('_part', 1)
                        if head.startswith('session_') and tail.isdigit() and len(tail) == 4:
                            base = head
                except Exception:
                    pass
                sessions.setdefault(base, []).append(f)
        except Exception:
            continue
    return sessions


@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    sessions = _group_sessions()
    out = []
    for base, files in sorted(sessions.items(), reverse=True):
        out.append({'base': base, 'files': sorted(files)})
    return jsonify({'ok': True, 'sessions': out})


@app.route('/api/session/bundle', methods=['GET'])
def session_bundle():
    """Return a ZIP bundle containing all artifacts for a session base."""
    base = (request.args.get('base') or '').strip()
    if not base:
        return jsonify({'ok': False, 'error': 'base required'}), 400
    # normalize (strip extension)
    if '.' in base:
        base = base.split('.', 1)[0]
    if not base.startswith('session_'):
        return jsonify({'ok': False, 'error': 'invalid base'}), 400

    candidates = []
    try:
        for folder in _iter_log_folders():
            if not os.path.isdir(folder):
                continue
            for f in os.listdir(folder):
                # Bundle both single-file artifacts (session_....csv) and chunked artifacts
                # (session_..._part0003.mf4). Skip incomplete temp chunks.
                if not (f.startswith(base + '.') or f.startswith(base + '_')):
                    continue
                if f.endswith('.tmp.mf4') or f.endswith('.tmp'):
                    continue
                if f not in candidates:
                    candidates.append(f)
    except Exception:
        candidates = []

    if not candidates:
        return jsonify({'ok': False, 'error': 'session not found'}), 404

    # MF4 usability: prefer a single consolidated `${base}.mf4`.
    # If chunk parts exist, attempt to generate the merged file on-demand,
    # then exclude all parts from the bundle.
    try:
        import re

        merged_name = f"{base}.mf4"
        part_re = re.compile(r'^' + re.escape(base) + r'_part\d{4}\.mf4$', flags=re.IGNORECASE)
        part_names = [f for f in candidates if part_re.match(str(f or ''))]

        merged_path = _find_log_file(merged_name)

        # If we have parts but no merged file yet, try to merge now.
        # This covers cases where the app was restarted or stop() did not run.
        if (not merged_path) and part_names:
            # Do not attempt to merge while logging is active for the same base.
            try:
                if bool(getattr(shared_logger, 'active', False)):
                    cur = getattr(shared_logger, 'session_base_name', None) or getattr(shared_logger, 'base_name', None)
                    try:
                        if cur and os.path.basename(str(cur)) == str(base):
                            return jsonify({'ok': False, 'error': 'logging still active for this session; stop logging before bundling'}), 409
                    except Exception:
                        pass
            except Exception:
                pass

            # Pick the folder that contains most parts (or the newest part) and merge there.
            folder_stats = {}
            for name in list(part_names):
                p = _find_log_file(name)
                if not p:
                    continue
                folder = os.path.dirname(str(p)) or '.'
                st = folder_stats.setdefault(folder, {'count': 0, 'newest_mtime': 0.0})
                st['count'] += 1
                try:
                    mtime = float(os.stat(p).st_mtime)
                    if mtime > float(st.get('newest_mtime', 0.0) or 0.0):
                        st['newest_mtime'] = mtime
                except Exception:
                    pass

            chosen_folder = None
            if folder_stats:
                # sort by part count, then newest mtime
                chosen_folder = sorted(folder_stats.items(), key=lambda kv: (int(kv[1].get('count', 0) or 0), float(kv[1].get('newest_mtime', 0.0) or 0.0)), reverse=True)[0][0]

            if chosen_folder:
                merge_fn = getattr(shared_logger, '_finalize_single_mf4_from_parts', None)
                if callable(merge_fn):
                    try:
                        merge_fn(base_name=os.path.join(str(chosen_folder), str(base)))
                    except Exception:
                        pass

            merged_path = _find_log_file(merged_name)

        # If merged exists, ensure it's included and remove all chunk parts.
        if merged_path:
            if merged_name not in candidates:
                candidates.append(merged_name)
            candidates = [f for f in candidates if not part_re.match(str(f or ''))]
        else:
            # Still no merged file but chunk parts exist -> fail fast with diagnostics.
            if part_names:
                err_name = f"{base}.mf4.merge_error.txt"
                err_path = _find_log_file(err_name)
                details = None
                if err_path:
                    try:
                        with open(err_path, 'r', encoding='utf-8', errors='ignore') as f:
                            details = f.read().strip()[:4000]
                    except Exception:
                        details = None
                return jsonify({
                    'ok': False,
                    'error': 'MF4 merge did not produce a single file for this session',
                    'hint': 'Check /api/log/status for mf4_merge.error and verify asammdf/numpy are installed.',
                    'merge_error_file': err_name if err_path else None,
                    'merge_error_details': details,
                }), 409
    except Exception:
        pass

    # Also include video/audio artifacts if present (same base)
    for ext in ['mp4', 'wav', 'aac']:
        name = f"{base}.{ext}"
        if _find_log_file(name) and name not in candidates:
            candidates.append(name)

    cfg = config_store.get_config_only()
    manifest = {
        'base': base,
        'created_at_ms': int(time.time() * 1000),
        'files': sorted(candidates),
        'config': cfg,
    }

    # Build the ZIP on disk to avoid holding large sessions in RAM.
    # Use ZIP_STORED (no compression) to reduce CPU usage/heat on RPi.
    tmp_path = None
    try:
        from flask import after_this_request
        import tempfile

        with tempfile.NamedTemporaryFile(prefix=f'{base}_', suffix='.zip', delete=False) as tmp:
            tmp_path = tmp.name

        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr('manifest.json', json.dumps(manifest, indent=2, sort_keys=True))
            for f in sorted(candidates):
                p = _find_log_file(f)
                if not p:
                    continue
                try:
                    z.write(p, arcname=os.path.join('logs', f))
                except Exception:
                    continue

        @after_this_request
        def _cleanup_tmp_bundle(resp):
            try:
                if tmp_path and os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return resp

        return send_file(
            tmp_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{base}_bundle.zip',
            conditional=True,
            max_age=0,
        )
    except Exception as e:
        try:
            if tmp_path and os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/display')
def display_status_page():
    return render_template('display_status.html')

@app.route('/api/interfaces', methods=['GET'])
def get_interfaces():
    return jsonify(manager.list_interfaces())

@app.route('/api/dbcs', methods=['GET'])
def get_dbcs():
    return jsonify(manager.list_dbcs(UPLOAD_FOLDER_DBC))

@app.route('/api/fibexs', methods=['GET'])
def get_fibexs():
    if not os.path.exists(UPLOAD_FOLDER_FIBEX):
        return jsonify([])

    allowed_exts = ('.xml', '.fibex', '.arxml')
    out = []
    try:
        for f in os.listdir(UPLOAD_FOLDER_FIBEX):
            p = os.path.join(UPLOAD_FOLDER_FIBEX, f)
            if not os.path.isfile(p):
                continue
            if not str(f).lower().endswith(allowed_exts):
                continue
            out.append(f)
    except Exception:
        return jsonify([])

    out.sort(key=lambda s: str(s).lower())
    return jsonify(out)

@app.route('/api/fibex/describe', methods=['GET'])
def describe_fibex():
    from fibex_loader import FibexLoader

    name = request.args.get('fibex_name', '').strip()
    base = _safe_basename(name)
    if not base:
        return jsonify({'ok': False, 'error': 'invalid fibex_name'}), 400

    allowed_exts = ('.xml', '.fibex', '.arxml')
    if not base.lower().endswith(allowed_exts):
        return jsonify({'ok': False, 'error': 'unsupported file extension'}), 400

    path = os.path.join(UPLOAD_FOLDER_FIBEX, base)
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
         
    loader = FibexLoader()
    loader.load(path)
    
    msgs = []
    triggerings = getattr(loader, 'triggerings', None)
    if isinstance(triggerings, list) and triggerings:
        def _tkey(t: dict):
            try:
                return (
                    int(t.get('slot_id') or 0),
                    int(t.get('base_cycle') or 0),
                    int(t.get('cycle_repetition') or 0),
                    str(t.get('name') or '').lower(),
                )
            except Exception:
                return (0, 0, 0, str(t.get('name') or '').lower())

        for t in sorted(triggerings, key=_tkey):
            try:
                slot_id = int(t.get('slot_id') or 0)
            except Exception:
                slot_id = 0
            sigs = loader.signals.get(slot_id) if isinstance(getattr(loader, 'signals', None), dict) else None
            if not isinstance(sigs, list):
                sigs = []
            msgs.append({
                'name': t.get('name') or '',
                'frame_id': slot_id,
                'base_cycle': int(t.get('base_cycle') or 0),
                'cycle_repetition': int(t.get('cycle_repetition') or 0),
                'signals': sigs,
            })
    else:
        for fid, fname in sorted(loader.frames.items(), key=lambda kv: str(kv[1]).lower()):
            sigs = loader.signals.get(fid) if isinstance(getattr(loader, 'signals', None), dict) else None
            if not isinstance(sigs, list):
                sigs = []
            msgs.append({
                'name': fname,
                'frame_id': fid,
                'signals': sigs,
            })
        
    return jsonify({'ok': True, 'fibex_name': base, 'messages': msgs})

@app.route('/api/logs', methods=['GET'])
def list_logs():
    import re

    def _is_tmp_artifact(name: str) -> bool:
        # Match files like *.tmp, *.tmp.mf4, *.tmp.zip, etc.
        # (We intentionally do not match names that merely contain "tmp".)
        try:
            return bool(re.search(r'\.tmp($|\.)', str(name or ''), flags=re.IGNORECASE))
        except Exception:
            return False

    # Collapse MF4 chunk parts:
    # - If a merged session_*.mf4 exists, hide all session_*_part####.mf4
    # - Otherwise, keep only the newest part per base session to avoid list explosion
    part_re = re.compile(r'^(?P<base>.+)_part\d{4}\.mf4$', flags=re.IGNORECASE)

    files_by_name = {}
    for folder in _iter_log_folders():
        try:
            if not os.path.exists(folder):
                continue
            for f in os.listdir(folder):
                # Never expose internal app artifacts as deletable log files.
                if f in {'webapp.out', 'webapp.pid'}:
                    continue
                # Hide in-progress/atomic-write temp files.
                if _is_tmp_artifact(f):
                    continue
                path = os.path.join(folder, f)
                if not os.path.isfile(path):
                    continue
                try:
                    st = os.stat(path)
                    # Prefer primary folder; otherwise take newest mtime.
                    prev = files_by_name.get(f)
                    if prev is None or folder == os.path.realpath(LOG_FOLDER) or st.st_mtime > float(prev.get('_mtime', 0.0)):
                        files_by_name[f] = {"name": f, "size": int(st.st_size), "_mtime": float(st.st_mtime)}
                except Exception:
                    continue
        except Exception:
            continue

    # Second pass: collapse MF4 parts, but preserve total size metadata.
    names = set(files_by_name.keys())
    part_groups = {}
    for name, meta in list(files_by_name.items()):
        m = part_re.match(str(name))
        if not m:
            continue
        base = m.group('base')
        merged_name = f'{base}.mf4'
        # If merged exists, remove all parts.
        if merged_name in names:
            files_by_name.pop(name, None)
            continue
        grp = part_groups.setdefault(base, {
            'total_size': 0,
            'part_count': 0,
            'newest_name': None,
            'newest_mtime': 0.0,
        })
        try:
            grp['total_size'] += int(meta.get('size', 0) or 0)
        except Exception:
            pass
        try:
            grp['part_count'] += 1
        except Exception:
            pass
        try:
            mtime = float(meta.get('_mtime', 0.0) or 0.0)
        except Exception:
            mtime = 0.0
        if grp['newest_name'] is None or mtime >= float(grp.get('newest_mtime', 0.0) or 0.0):
            grp['newest_name'] = name
            grp['newest_mtime'] = mtime

    if part_groups:
        for base, grp in part_groups.items():
            newest_name = grp.get('newest_name')
            if not newest_name:
                continue
            # Remove older parts for this base.
            for name in list(files_by_name.keys()):
                m = part_re.match(str(name))
                if not m:
                    continue
                if m.group('base') != base:
                    continue
                if name != newest_name:
                    files_by_name.pop(name, None)
            # Annotate the remaining part with total size metadata.
            meta = files_by_name.get(newest_name)
            if isinstance(meta, dict):
                meta['size_total'] = int(grp.get('total_size', meta.get('size', 0)) or 0)
                meta['part_count'] = int(grp.get('part_count', 1) or 1)

    out = list(files_by_name.values())
    out.sort(key=lambda x: float(x.get('_mtime', 0.0)), reverse=True)
    for i in out:
        i.pop('_mtime', None)
    return jsonify(out)


@app.route('/api/health', methods=['GET'])
def api_health():
    """Healthcheck rapido per uso in vettura.

    Non avvia hardware: riporta solo stato e prerequisiti.
    """
    import shutil

    health = {
        'ok': True,
        'time_ms': int(time.time() * 1000),
        'log_dir': str(LOG_FOLDER),
        'logging_active': bool(getattr(shared_logger, 'active', False)),
        'ethernet_mf4_logging_active': False,
        'disk': {},
        'deps': {},
    }

    try:
        health['ethernet_mf4_logging_active'] = bool(getattr(eth_manager, 'mf4_logger', None) is not None)
    except Exception:
        health['ethernet_mf4_logging_active'] = False

    # Disk usage for log dir (and project dir as fallback)
    try:
        usage = shutil.disk_usage(LOG_FOLDER)
        health['disk'] = {
            'path': str(LOG_FOLDER),
            'total_bytes': int(usage.total),
            'used_bytes': int(usage.used),
            'free_bytes': int(usage.free),
        }
    except Exception:
        try:
            usage = shutil.disk_usage(PROJECT_DIR)
            health['disk'] = {
                'path': str(PROJECT_DIR),
                'total_bytes': int(usage.total),
                'used_bytes': int(usage.used),
                'free_bytes': int(usage.free),
            }
        except Exception:
            health['disk'] = {}

    # Writable check
    try:
        _ensure_writable_dir(LOG_FOLDER)
        health['log_dir_writable'] = True
    except Exception as e:
        health['log_dir_writable'] = False
        health['log_dir_writable_error'] = str(e)
        health['ok'] = False

    # Dependencies
    try:
        import numpy  # noqa: F401
        health['deps']['numpy'] = True
    except Exception as e:
        health['deps']['numpy'] = False
        health['deps']['numpy_error'] = str(e)
        health['ok'] = False
    try:
        import asammdf  # noqa: F401
        health['deps']['asammdf'] = True
    except Exception as e:
        health['deps']['asammdf'] = False
        health['deps']['asammdf_error'] = str(e)

    return jsonify(health)


@app.route('/api/system/stats', methods=['GET'])
def api_system_stats():
    """Lightweight system stats for UI (CPU temp, CPU %, RAM %).

    Implemented without psutil to keep dependencies minimal.
    """

    def _read_cpu_temp_c() -> float | None:
        # Raspberry Pi / Linux thermal interface
        try:
            p = '/sys/class/thermal/thermal_zone0/temp'
            if os.path.isfile(p):
                raw = open(p, 'r').read().strip()
                v = float(raw)
                # Many systems expose milli-degrees C
                if v > 1000:
                    v = v / 1000.0
                return float(v)
        except Exception:
            pass
        # Fallback: vcgencmd (if available)
        try:
            import subprocess
            out = subprocess.check_output(['vcgencmd', 'measure_temp'], stderr=subprocess.STDOUT, timeout=1.0)
            s = out.decode('utf-8', 'ignore').strip()
            # temp=47.8'C
            if '=' in s:
                s = s.split('=', 1)[1]
            s = s.replace("'C", '').replace('C', '').strip()
            return float(s)
        except Exception:
            return None

    def _read_proc_stat() -> tuple[int, int] | None:
        """Return (busy_ticks, total_ticks) from /proc/stat."""
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
            if not line.startswith('cpu '):
                return None
            parts = line.split()
            vals = [int(x) for x in parts[1:]]
            # user nice system idle iowait irq softirq steal guest guest_nice
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            busy = total - idle
            return busy, total
        except Exception:
            return None

    def _cpu_percent_sample(window_s: float = 0.12) -> float | None:
        a = _read_proc_stat()
        if not a:
            return None
        time.sleep(max(0.02, float(window_s)))
        b = _read_proc_stat()
        if not b:
            return None
        busy_a, total_a = a
        busy_b, total_b = b
        d_busy = busy_b - busy_a
        d_total = total_b - total_a
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, (d_busy / d_total) * 100.0))

    def _ram_percent() -> float | None:
        try:
            mem = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if ':' not in line:
                        continue
                    k, v = line.split(':', 1)
                    mem[k.strip()] = v.strip()
            total_kb = float(mem.get('MemTotal', '0 kB').split()[0])
            avail_kb = float(mem.get('MemAvailable', '0 kB').split()[0])
            if total_kb <= 0:
                return None
            used_kb = max(0.0, total_kb - avail_kb)
            return max(0.0, min(100.0, (used_kb / total_kb) * 100.0))
        except Exception:
            return None

    temp_c = _read_cpu_temp_c()
    cpu_pct = _cpu_percent_sample()
    ram_pct = _ram_percent()

    return jsonify({
        'ok': True,
        'cpu_temp_c': temp_c,
        'cpu_percent': cpu_pct,
        'ram_percent': ram_pct,
        'ts_ms': int(time.time() * 1000),
    })


@app.route('/api/system/power', methods=['POST'])
def api_system_power():
    """Shutdown/reboot the host.

    POST JSON:
      {"action": "shutdown"|"reboot", "confirm": true, "sudo_password": "..." (optional)}

    Behavior:
      - If running as root: executes directly.
      - Else tries passwordless sudo (sudoers NOPASSWD).
      - If sudo requires password and none provided: returns need_password=true.
      - If password provided: validates via `sudo -v` and then schedules action.

    Note: password is never stored.
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    action = str(data.get('action') or '').strip().lower()
    if action not in {'shutdown', 'reboot'}:
        return jsonify({'ok': False, 'error': 'invalid action'}), 400
    if not bool(data.get('confirm')):
        return jsonify({'ok': False, 'error': 'missing confirm=true'}), 400

    dry_run = bool(data.get('dry_run'))

    sudo_password = str(data.get('sudo_password') or '').rstrip('\n')

    try:
        import subprocess
        import threading
        import time as _time
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Use absolute systemctl path when available.
    systemctl_path = '/usr/bin/systemctl' if os.path.exists('/usr/bin/systemctl') else 'systemctl'
    systemctl_base = [systemctl_path, '--no-ask-password']
    verb = 'poweroff' if action == 'shutdown' else 'reboot'
    cmd = systemctl_base + [verb]
    # Safe probe that should never power off/reboot.
    cmd_probe = systemctl_base + ['--dry-run', verb]

    def _run_check(argv: list[str], *, stdin_text: str | None = None, timeout_s: float = 2.5):
        try:
            p = subprocess.run(
                argv,
                input=(stdin_text.encode('utf-8') if stdin_text is not None else None),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=float(timeout_s),
                check=False,
            )
            out = (p.stdout or b'').decode('utf-8', errors='ignore')
            err = (p.stderr or b'').decode('utf-8', errors='ignore')
            return int(p.returncode), out, err
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout'
        except Exception as e:
            return 1, '', str(e)

    def _needs_password(stderr: str) -> bool:
        s = str(stderr or '').lower()
        return any(k in s for k in [
            'a password is required',
            'password required',
            'authentication is required',
            'interactive authentication required',
            'sudo:',
        ]) and ('password' in s or 'authentication' in s)

    def _needs_auth(stderr: str) -> bool:
        s = str(stderr or '').lower()
        return any(k in s for k in [
            'interactive authentication required',
            'authentication is required',
            'access denied',
            'not authorized',
            'permission denied',
        ])

    def _schedule(argv: list[str], *, stdin_text: str | None = None):
        def _worker():
            try:
                _time.sleep(0.25)
            except Exception:
                pass
            try:
                p = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if stdin_text is not None and getattr(p, 'stdin', None) is not None:
                    try:
                        p.stdin.write(stdin_text.encode('utf-8', errors='ignore'))
                        p.stdin.flush()
                    except Exception:
                        pass
                    try:
                        p.stdin.close()
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            try:
                p = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE if stdin_text is not None else None,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if stdin_text is not None and getattr(p, 'stdin', None) is not None:
                    try:
                        p.stdin.write(stdin_text.encode('utf-8', errors='ignore'))
                        p.stdin.flush()
                    except Exception:
                        pass
                    try:
                        p.stdin.close()
                    except Exception:
                        pass
            except Exception:
                pass

    # If running as root, schedule directly.
    try:
        if int(os.geteuid()) == 0:
            if dry_run:
                return jsonify({'ok': True, 'action': action, 'scheduled': False, 'dry_run': True, 'need_password': False})
            _schedule(cmd)
            return jsonify({'ok': True, 'action': action, 'scheduled': True, 'need_password': False})
    except Exception:
        pass

    # Try via polkit/logind without sudo.
    # Use a dry-run probe first to avoid accidental power actions.
    rc0, _, err0 = _run_check(cmd_probe)
    if rc0 == 0:
        if dry_run:
            return jsonify({'ok': True, 'action': action, 'scheduled': False, 'dry_run': True, 'need_password': False})
        _schedule(cmd)
        return jsonify({'ok': True, 'action': action, 'scheduled': True, 'need_password': False})

    # Try passwordless sudo (requires the service to be allowed to elevate).
    rc, _, err = _run_check(['sudo', '-n'] + cmd_probe)
    if rc == 0:
        if dry_run:
            return jsonify({'ok': True, 'action': action, 'scheduled': False, 'dry_run': True, 'need_password': False})
        _schedule(['sudo', '-n'] + cmd)
        return jsonify({'ok': True, 'action': action, 'scheduled': True, 'need_password': False})

    # If sudo needs password and we don't have one, ask UI to prompt.
    if not sudo_password:
        if _needs_password(err):
            return jsonify({'ok': False, 'error': 'sudo password required', 'need_password': True}), 403
        return jsonify({'ok': False, 'error': (err.strip() or 'permission denied'), 'need_password': False}), 403

    # Validate password and (if ok) schedule the actual action using sudo -S.
    rc2, _, err2 = _run_check(['sudo', '-S', '-v'], stdin_text=sudo_password + '\n', timeout_s=3.0)
    if rc2 != 0:
        if _needs_password(err2) or 'incorrect password' in str(err2).lower() or 'sorry' in str(err2).lower():
            return jsonify({'ok': False, 'error': 'invalid sudo password', 'need_password': True}), 403
        return jsonify({'ok': False, 'error': (err2.strip() or 'sudo failed'), 'need_password': False}), 403

    if dry_run:
        return jsonify({'ok': True, 'action': action, 'scheduled': False, 'dry_run': True, 'need_password': False})

    # Avoid relying on tty-scoped sudo timestamp from services.
    _schedule(['sudo', '-S'] + cmd, stdin_text=sudo_password + '\n')
    return jsonify({'ok': True, 'action': action, 'scheduled': True, 'need_password': False})


@app.route('/api/mf4/files', methods=['GET'])
def mf4_list_files():
    """List available MF4 files in log folders."""
    include_exports = str(request.args.get('include_exports') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    return jsonify(_list_mf4_files(include_exports=include_exports))


@app.route('/api/mf4/raw_channels', methods=['GET'])
def mf4_list_raw_channels():
    """List unique CAN Channel values present in a raw MF4 (CAN_ID/DLC/DataByte*).

    Query params:
      - file: mf4 filename present in logs
    """
    filename = str(request.args.get('file') or '').strip()
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith('.mf4'):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    mf4_path = _find_log_file(filename)
    if not mf4_path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    try:
        import numpy as np
        import asammdf
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        mdf = asammdf.MDF(mf4_path)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'cannot open mf4: {e}'}), 409

    try:
        ch = None
        for ch_name in ['Channel', 'BusChannel', 'CAN_DataFrame.Channel', 'CAN_DataFrame.BusChannel', 'CAN_Frame.Channel']:
            try:
                ch_sig = mdf.get(ch_name)
                ch = np.asarray(getattr(ch_sig, 'samples', []), dtype=np.uint16)
                break
            except Exception:
                ch = None
        if ch is None:
            ch = np.asarray([], dtype=np.uint16)
        if ch.size == 0:
            return jsonify({'ok': True, 'channels': []})
        vals = np.unique(ch).tolist()
        # Hide sentinel/non-CAN values by default (255 used for ETH mapping).
        out = []
        for v in vals:
            try:
                iv = int(v)
            except Exception:
                continue
            if iv == 255:
                continue
            out.append(iv)
        out.sort()

        suggestions, details = _mf4_suggest_can_dbcs_for_file(mf4_path)
        items: list[dict[str, Any]] = []
        suggested_channel = None
        best_score = -1
        for iv in out:
            meta = _mf4_describe_raw_channel(iv)
            scored = details.get(int(iv), []) or []
            suggested_dbcs = suggestions.get(int(iv), []) or []
            item = {
                **meta,
                'suggested_dbcs': suggested_dbcs,
                'best_dbc': suggested_dbcs[0] if suggested_dbcs else '',
                'match_count': int(scored[0].get('match_count') or 0) if scored else 0,
            }
            items.append(item)
            if item['match_count'] > best_score and item['best_dbc']:
                best_score = int(item['match_count'])
                suggested_channel = int(iv)

        return jsonify({
            'ok': True,
            'channels': out,
            'items': items,
            'suggested_channel': suggested_channel,
        })
    finally:
        try:
            mdf.close()
        except Exception:
            pass


@app.route('/api/mf4/info', methods=['GET'])
def mf4_info():
    """Return lightweight info about an MF4 file.

    Used to distinguish RAW-CAN MF4 (CAN_ID/DLC/DataByte*) vs measured/decoded MF4.
    """
    filename = str(request.args.get('file') or '').strip()
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith(('.mf4', '.mdf', '.dat')):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    path = _find_log_file(filename)
    if not path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    try:
        import asammdf
    except Exception:
        asammdf = None
    if asammdf is None:
        return jsonify({'ok': False, 'error': 'missing dependency: asammdf'}), 500

    mdf = None
    try:
        # memory='minimum' speeds up opening large files by only reading headers
        mdf = asammdf.MDF(path, memory='minimum')
        try:
            keys = set(str(k) for k in getattr(mdf, 'channels_db', {}).keys())
        except Exception:
            keys = set()

        # Raw-CAN detection: accept multiple common naming layouts.
        # Layout A (this project): CAN_ID, DLC, DataByte0..7
        # Layout B (common vendor tools): ID, DLC, DataBytes (array) or DataBytes[0..7]
        has_id = ('CAN_ID' in keys) or ('ID' in keys) or ('Identifier' in keys) or ('CAN_DataFrame.ID' in keys) or ('CAN_DataFrame.CAN_ID' in keys)
        has_dlc = ('DLC' in keys) or ('Length' in keys) or ('DataLength' in keys) or ('CAN_DataFrame.DLC' in keys)
        has_bytes = all((f'DataByte{i}' in keys) for i in range(8))
        has_bytes = has_bytes or all((f'CAN_DataFrame.DataByte{i}' in keys) for i in range(8))
        has_bytes = has_bytes or ('DataBytes' in keys) or ('CAN_DataFrame.DataBytes' in keys) or any((f'DataBytes[{i}]' in keys) for i in range(8))
        has_raw = bool(has_id and has_dlc and has_bytes)
        kind = 'raw_can' if has_raw else 'measured'

        # Detect decoded channels beyond the raw CAN set.
        _RAW_NAMES = {
            'CAN_ID', 'ID', 'Identifier', 'CAN_DataFrame.ID', 'CAN_DataFrame.CAN_ID',
            'DLC', 'Length', 'DataLength', 'CAN_DataFrame.DLC',
            'Channel', 'BusChannel', 'CAN_DataFrame.Channel', 'CAN_DataFrame.BusChannel', 'CAN_Frame.Channel',
            'Flags', 'CAN_DataFrame.Flags', 'CAN_Frame.Flags',
            'DataBytes', 'CAN_DataFrame.DataBytes',
            # Metadata fields present in raw-CAN MF4 that are NOT decoded signals.
            'BusType', 'PayloadLength', 'Dir', 'BusLoad',
            'time', 't',
        }
        # DataByte0..63 cover both standard CAN (8 bytes) and CAN FD (up to 64 bytes).
        _RAW_NAMES.update(f'DataByte{i}' for i in range(64))
        _RAW_NAMES.update(f'CAN_DataFrame.DataByte{i}' for i in range(64))
        _RAW_NAMES.update(f'DataBytes[{i}]' for i in range(64))
        decoded_channel_names = sorted(k for k in keys if k not in _RAW_NAMES)
        has_decoded_channels = bool(decoded_channel_names)

        try:
            groups_count = len(getattr(mdf, 'groups', []) or [])
        except Exception:
            groups_count = None
        try:
            channels_count = len(keys)
        except Exception:
            channels_count = None
        try:
            mdf_version = getattr(mdf, 'version', None)
        except Exception:
            mdf_version = None

        return jsonify({
            'ok': True,
            'file': filename,
            'kind': kind,
            'has_raw_channels': bool(has_raw),
            'has_decoded_channels': has_decoded_channels,
            'decoded_channel_names': decoded_channel_names if has_decoded_channels else [],
            'groups': groups_count,
            'channels': channels_count,
            'mdf_version': mdf_version,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        try:
            if mdf is not None:
                mdf.close()
        except Exception:
            pass


@app.route('/api/mf4/upload', methods=['POST'])
def mf4_upload_file():
    """Upload an MF4 file into the primary log folder (LOG_FOLDER).

    Multipart form-data:
      field name: file
    """
    try:
        from werkzeug.utils import secure_filename
    except Exception:
        secure_filename = None

    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'missing file field'}), 400

    f = request.files.get('file')
    if f is None:
        return jsonify({'ok': False, 'error': 'missing file'}), 400

    orig = str(getattr(f, 'filename', '') or '').strip()
    if not orig:
        return jsonify({'ok': False, 'error': 'missing filename'}), 400

    name = secure_filename(orig) if secure_filename else orig
    name = str(name or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'invalid filename'}), 400
    if not name.lower().endswith(('.mf4', '.mdf', '.dat')):
        return jsonify({'ok': False, 'error': 'only .mf4, .mdf, .dat files are allowed'}), 400

    dest_dir = os.path.realpath(LOG_FOLDER)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass

    base, ext = os.path.splitext(name)
    dest_name = name
    dest_path = os.path.join(dest_dir, dest_name)

    # Avoid overwriting existing files.
    if os.path.exists(dest_path):
        try:
            ts = time.strftime('%Y%m%d_%H%M%S')
        except Exception:
            ts = str(int(time.time()))
        dest_name = f"{base}_{ts}{ext}"
        dest_path = os.path.join(dest_dir, dest_name)
        i = 1
        while os.path.exists(dest_path) and i < 1000:
            dest_name = f"{base}_{ts}_{i}{ext}"
            dest_path = os.path.join(dest_dir, dest_name)
            i += 1

    try:
        f.save(dest_path)
        try:
            st = os.stat(dest_path)
            size = int(st.st_size)
        except Exception:
            size = None
        return jsonify({'ok': True, 'name': dest_name, 'size': size})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/signals', methods=['GET'])
def mf4_list_signals():
    """List signal names available in an MF4 file."""
    filename = str(request.args.get('file') or '').strip()
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith(('.mf4', '.mdf', '.dat')):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    path = _find_log_file(filename)
    if not path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    try:
        import asammdf
    except Exception:
        asammdf = None

    if asammdf is None:
        return jsonify({'ok': False, 'error': "missing dependency: asammdf"}), 500

    try:
        # memory='minimum' is crucial for large files on limited RAM
        mdf = asammdf.MDF(path, memory='minimum')
        try:
            names = list(getattr(mdf, 'channels_db', {}).keys())
        except Exception:
            names = []
        try:
            mdf.close()
        except Exception:
            pass
        names = sorted({str(n) for n in names if n is not None and str(n).strip()})
        return jsonify({'ok': True, 'signals': names})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/data', methods=['POST'])
def mf4_get_data():
    """Fetch decimated samples for selected signals from an MF4 file.

    Body:
      {
        "file": "session_....mf4",
        "signals": ["RPM", "VehicleSpeed"],
        "start_s": 0.0,
        "end_s": 12.5,
        "max_points": 5000
      }
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    filename = str(data.get('file') or '').strip()
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith(('.mf4', '.mdf', '.dat')):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    signals = data.get('signals')
    if not isinstance(signals, list) or not signals:
        return jsonify({'ok': False, 'error': 'signals must be a non-empty list'}), 400
    signals = [str(s).strip() for s in signals if str(s).strip()]
    if not signals:
        return jsonify({'ok': False, 'error': 'signals must be a non-empty list'}), 400

    try:
        start_s = data.get('start_s', None)
        start_s = None if start_s is None else float(start_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid start_s'}), 400

    try:
        end_s = data.get('end_s', None)
        end_s = None if end_s is None else float(end_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid end_s'}), 400

    try:
        max_points = int(data.get('max_points', 5000) or 5000)
    except Exception:
        max_points = 5000
    max_points = max(100, min(max_points, 20000))

    # Optional: use absolute epoch seconds for windowing and/or output.
    t_mode = str(data.get('t_mode') or '').strip().lower()
    if t_mode not in {'', 'rel', 'relative', 'abs', 'absolute'}:
        return jsonify({'ok': False, 'error': 'invalid t_mode'}), 400
    try:
        start_abs_s = data.get('start_abs_s', None)
        start_abs_s = None if start_abs_s is None else float(start_abs_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid start_abs_s'}), 400
    try:
        end_abs_s = data.get('end_abs_s', None)
        end_abs_s = None if end_abs_s is None else float(end_abs_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid end_abs_s'}), 400

    path = _find_log_file(filename)
    if not path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    try:
        import asammdf
    except Exception:
        asammdf = None
    try:
        import numpy as np
    except Exception:
        np = None

    if asammdf is None or np is None:
        missing = []
        if asammdf is None:
            missing.append('asammdf')
        if np is None:
            missing.append('numpy')
        return jsonify({'ok': False, 'error': f"missing dependency: {', '.join(missing)}"}), 500

    mdf = None
    try:
        mdf = asammdf.MDF(path)
        series = []

        def _decode_to_text_array(arr):
            """Best-effort convert MF4 samples to a unicode numpy array."""
            try:
                # For numpy bytes dtype (|S*), this produces clean decoded strings.
                if hasattr(arr, 'dtype') and getattr(arr.dtype, 'kind', None) in {'S', 'a'}:
                    return arr.astype('U', copy=False)
            except Exception:
                pass
            try:
                # For unicode dtype
                if hasattr(arr, 'dtype') and getattr(arr.dtype, 'kind', None) == 'U':
                    return arr
            except Exception:
                pass
            # Fallback: elementwise stringify
            try:
                return np.asarray([str(x) for x in arr], dtype=np.dtype('U'))
            except Exception:
                return np.asarray([], dtype=np.dtype('U'))

        for name in signals:
            try:
                sig = mdf.get(name)
                t = np.asarray(getattr(sig, 'timestamps', []), dtype=np.float64)
                y = np.asarray(getattr(sig, 'samples', []))
                unit = str(getattr(sig, 'unit', '') or '')

                if t.size == 0 or y.size == 0:
                    continue

                # Ensure same length
                try:
                    n = int(min(t.size, y.size))
                    t = t[:n]
                    y = y[:n]
                except Exception:
                    pass

                # Sort by timestamp (MF4 can contain chunked/unsorted data)
                try:
                    order = np.argsort(t)
                    t = t[order]
                    y = y[order]
                except Exception:
                    pass

                # Some MF4 sources mix absolute epoch seconds and relative seconds in the same channel.
                # Keep the dominant cluster to avoid huge negative/positive time axes.
                try:
                    thr = 1e7
                    mask_epoch = t > thr
                    if mask_epoch.any() and (~mask_epoch).any():
                        if int(mask_epoch.sum()) >= int((~mask_epoch).sum()):
                            t = t[mask_epoch]
                            y = y[mask_epoch]
                        else:
                            t = t[~mask_epoch]
                            y = y[~mask_epoch]
                except Exception:
                    pass

                if t.size == 0 or y.size == 0:
                    continue

                if start_s is not None:
                    t_start = t[0] + float(start_s)
                else:
                    t_start = None
                if end_s is not None:
                    t_end = t[0] + float(end_s)
                else:
                    t_end = None

                # Absolute override
                if start_abs_s is not None:
                    t_start = float(start_abs_s)
                if end_abs_s is not None:
                    t_end = float(end_abs_s)

                i0 = 0
                i1 = int(t.size)
                if t_start is not None:
                    i0 = int(np.searchsorted(t, t_start, side='left'))
                if t_end is not None:
                    i1 = int(np.searchsorted(t, t_end, side='right'))
                i0 = max(0, min(i0, int(t.size)))
                i1 = max(i0, min(i1, int(t.size)))

                t = t[i0:i1]
                y = y[i0:i1]
                if t.size == 0 or y.size == 0:
                    continue

                # Downsample to max_points (simple stride)
                if int(t.size) > max_points:
                    step = int(np.ceil(float(t.size) / float(max_points)))
                    step = max(1, step)
                    t = t[::step]
                    y = y[::step]

                # Best-effort numeric conversion
                categorical = False
                categories = None
                hover_text = None
                try:
                    y_num = y.astype(np.float64, copy=False)
                except Exception:
                    # Non-numeric signal: treat as categorical/enum.
                    categorical = True
                    y_txt = _decode_to_text_array(y)
                    if y_txt.size == 0:
                        continue
                    # Normalize values (strip NULs/spaces)
                    try:
                        y_txt = np.char.strip(np.char.replace(y_txt, '\x00', ''))
                    except Exception:
                        pass
                    # Map categories in order of appearance (stable)
                    cat_map = {}
                    cat_list = []
                    y_idx = np.empty((int(y_txt.size),), dtype=np.float64)
                    for i, s in enumerate(y_txt.tolist()):
                        ss = str(s)
                        if ss not in cat_map:
                            cat_map[ss] = float(len(cat_list))
                            cat_list.append(ss)
                        y_idx[i] = cat_map[ss]
                    y_num = y_idx
                    categories = cat_list
                    hover_text = y_txt.tolist()

                # Normalize per-signal time to start at 0
                try:
                    if t_mode in {'abs', 'absolute'}:
                        t_out = t.astype(np.float64, copy=False)
                    else:
                        t_out = (t - float(t[0])).astype(np.float64, copy=False)
                except Exception:
                    t_out = t

                # JSON cannot represent NaN/Infinity; drop non-finite samples.
                try:
                    mask = np.isfinite(t_out) & np.isfinite(y_num)
                    if mask is not None:
                        t_out = t_out[mask]
                        y_num = y_num[mask]
                        if hover_text is not None:
                            hover_text = [hover_text[i] for i, keep in enumerate(mask.tolist()) if keep]
                except Exception:
                    pass

                try:
                    if t_out is None or y_num is None or int(getattr(t_out, 'size', 0)) == 0 or int(getattr(y_num, 'size', 0)) == 0:
                        continue
                except Exception:
                    pass

                series.append({
                    'name': name,
                    'unit': unit,
                    '_t': t_out,
                    '_y': y_num,
                    'categorical': bool(categorical),
                    'categories': categories,
                    '_text': hover_text,
                })
            except Exception:
                continue

        if not series:
            return jsonify({'ok': True, 'series': []})

        out_series = []
        for s in series:
            t_vals = s['_t'].tolist()
            y_list = s['_y'].tolist()
            out = {
                'name': s['name'],
                'unit': s.get('unit', ''),
                't': t_vals,
                'y': y_list,
            }
            if s.get('categorical'):
                out['categorical'] = True
                out['categories'] = s.get('categories') or []
                txt = s.get('_text')
                if isinstance(txt, list) and len(txt) == len(t_vals):
                    out['text'] = txt
            out_series.append(out)

        return jsonify({'ok': True, 'series': out_series})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        try:
            if mdf is not None:
                mdf.close()
        except Exception:
            pass


def _resolve_dbc_path(dbc_name: str):
    safe = os.path.basename(str(dbc_name or '').strip())
    if not safe or safe != str(dbc_name or '').strip():
        return None
    # Search across all database folders (DBC, ARXML, FIBEX).
    for folder in (UPLOAD_FOLDER_DBC, UPLOAD_FOLDER_ARXML, UPLOAD_FOLDER_FIBEX):
        path = os.path.join(folder, safe)
        if os.path.isfile(path):
            return path
    return None


def _mf4_load_raw_table(path: str):
    """Load MF4 raw frame table.

    Returns `(t_seconds, can_id_u32, payload_len_u16, data_u8[N,M], channel_u16, flags_u32)` or `None`.
    """
    import asammdf
    import numpy as np

    # memory='minimum' avoids pre-loading all channel data into RAM, which
    # dramatically speeds up opening large MF4 files.
    mdf = asammdf.MDF(path, memory='minimum')
    try:
        # Accept multiple common naming layouts.
        def _get_first(names: list[str]):
            for n in names:
                try:
                    return mdf.get(n)
                except Exception:
                    continue
            return None

        can_sig = _get_first(['CAN_ID', 'ID', 'Identifier', 'CAN_DataFrame.CAN_ID', 'CAN_DataFrame.ID'])
        dlc_sig = _get_first(['PayloadLength', 'DLC', 'Length', 'DataLength', 'CAN_DataFrame.DLC'])
        if can_sig is None or dlc_sig is None:
            # Not a raw-CAN MF4 (e.g., measurement with already-decoded channels)
            return None

        # Optional meta columns
        ch_sig = _get_first(['Channel', 'BusChannel', 'CAN_DataFrame.Channel', 'CAN_DataFrame.BusChannel', 'CAN_Frame.Channel'])
        fl_sig = _get_first(['Flags', 'CAN_DataFrame.Flags', 'CAN_Frame.Flags'])

        t = np.asarray(getattr(can_sig, 'timestamps', []), dtype=np.float64)
        can_id = np.asarray(getattr(can_sig, 'samples', []), dtype=np.uint32)
        dlc = np.asarray(getattr(dlc_sig, 'samples', []), dtype=np.uint16)

        if ch_sig is None:
            ch = np.zeros_like(can_id, dtype=np.uint16)
        else:
            ch = np.asarray(getattr(ch_sig, 'samples', []), dtype=np.uint16)

        if fl_sig is None:
            fl = np.zeros_like(can_id, dtype=np.uint32)
        else:
            fl = np.asarray(getattr(fl_sig, 'samples', []), dtype=np.uint32)

        # Payload bytes
        bytes_cols = []
        data_bytes_sig = _get_first(['DataBytes', 'CAN_DataFrame.DataBytes'])
        if data_bytes_sig is not None:
            try:
                raw = np.asarray(getattr(data_bytes_sig, 'samples', []))
                if raw.ndim == 2 and raw.shape[1] >= 1:
                    data = raw.astype(np.uint8, copy=False)
                    bytes_cols = None  # sentinel: already built
                else:
                    # If vendor stores as bytes object array, fall back to per-byte channels.
                    data = None
            except Exception:
                data = None
        else:
            data = None

        if bytes_cols is not None and data is None:
            # Per-byte channels
            for i in range(64):
                s = _get_first([f'DataByte{i}', f'CAN_DataFrame.DataByte{i}', f'DataBytes[{i}]', f'CAN_DataFrame.DataBytes[{i}]'])
                if s is None:
                    if i < 8:
                        return None
                    break
                bytes_cols.append(np.asarray(getattr(s, 'samples', []), dtype=np.uint8))

        if t.size == 0 or can_id.size == 0:
            return None

        if data is not None:
            n = int(min(t.size, can_id.size, dlc.size, ch.size, fl.size, int(data.shape[0])))
        else:
            n = int(min(t.size, can_id.size, dlc.size, ch.size, fl.size, *(c.size for c in bytes_cols)))
        t = t[:n]
        can_id = can_id[:n]
        dlc = dlc[:n]
        ch = ch[:n]
        fl = fl[:n]
        if data is not None:
            data = data[:n]
        else:
            bytes_cols = [c[:n] for c in bytes_cols]
            data = np.stack(bytes_cols, axis=1) if bytes_cols else np.zeros((n, 8), dtype=np.uint8)

        # Sort by time
        try:
            order = np.argsort(t)
            t = t[order]
            can_id = can_id[order]
            dlc = dlc[order]
            data = data[order]
            ch = ch[order]
            fl = fl[order]
        except Exception:
            pass

        # Keep dominant timestamp cluster if epoch+relative mixed.
        try:
            thr = 1e7
            mask_epoch = t > thr
            if mask_epoch.any() and (~mask_epoch).any():
                if int(mask_epoch.sum()) >= int((~mask_epoch).sum()):
                    t = t[mask_epoch]
                    can_id = can_id[mask_epoch]
                    dlc = dlc[mask_epoch]
                    data = data[mask_epoch]
                    ch = ch[mask_epoch]
                    fl = fl[mask_epoch]
                else:
                    t = t[~mask_epoch]
                    can_id = can_id[~mask_epoch]
                    dlc = dlc[~mask_epoch]
                    data = data[~mask_epoch]
                    ch = ch[~mask_epoch]
                    fl = fl[~mask_epoch]
        except Exception:
            pass

        if t.size == 0:
            return None
        return t, can_id, dlc, data, ch, fl
    finally:
        try:
            mdf.close()
        except Exception:
            pass


def _mf4_configured_dbcs_by_channel() -> dict[int, list[str]]:
    """Return mapping channel_id -> list of configured DBC basenames.

    Uses config_store logger_channels.{dbc_names|dbc_name}.
    """
    out: dict[int, list[str]] = {}
    try:
        cfg = config_store.get_config_only() or {}
        chans = cfg.get('logger_channels') if isinstance(cfg, dict) else None
        chans = chans if isinstance(chans, list) else []
        for c in chans:
            if not isinstance(c, dict):
                continue
            try:
                ch_id = int(c.get('id'))
            except Exception:
                continue
            names: list[str] = []
            try:
                if isinstance(c.get('dbc_names'), list) and c.get('dbc_names'):
                    names = [str(x or '').strip() for x in c.get('dbc_names') if str(x or '').strip()]
                else:
                    dn = str(c.get('dbc_name') or '').strip()
                    if dn:
                        names = [dn]
            except Exception:
                names = []
            if names:
                out[ch_id] = [os.path.basename(n) for n in names if os.path.basename(n)]
    except Exception:
        return {}
    return out


def _mf4_candidate_can_dbcs() -> list[str]:
    """Return likely CAN DBC basenames for offline MF4 decode/export.

    Preference order:
      1. config.logger_channels
      2. configured CAN data sources
      3. all on-disk DBCs as fallback
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add_name(name: Any) -> None:
        try:
            base = os.path.basename(str(name or '').strip())
        except Exception:
            base = ''
        if not base or base in seen:
            return
        seen.add(base)
        out.append(base)

    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}

    try:
        chans = cfg.get('logger_channels') if isinstance(cfg, dict) else None
        chans = chans if isinstance(chans, list) else []
        for ch in chans:
            if not isinstance(ch, dict):
                continue
            names = ch.get('dbc_names') if isinstance(ch.get('dbc_names'), list) else None
            if names:
                for name in names:
                    _add_name(name)
            else:
                _add_name(ch.get('dbc_name'))
    except Exception:
        pass

    try:
        for src in (data_source_manager.list_sources() or []):
            if not isinstance(src, dict) or str(src.get('type') or '').upper() != 'CAN':
                continue
            _add_name(src.get('dbc_name'))
    except Exception:
        pass

    if out:
        return out

    try:
        for fn in sorted(os.listdir(UPLOAD_FOLDER_DBC)):
            if fn.lower().endswith('.dbc'):
                _add_name(fn)
    except Exception:
        pass
    return out


def _mirror_channel_bus_name(channel_id: int) -> str:
    """Return a short human-readable bus name for a mirror channel.

    Derives the name from the DBC loaded for this virtual channel in
    ``manager.dbcs``, e.g. ``'CCAN'``, ``'HCAN'``, ``'DiagCAN'``.
    Falls back to the ``_mirror_channel_map`` physical-channel index when
    no DBC is loaded.  Returns ``''`` for non-mirror / unknown channels.
    """
    import re as _re
    cid = int(channel_id)
    if not (99 <= cid < 250):
        return ''
    try:
        with manager.lock:
            loaders = manager.dbcs.get(cid)
        if loaders:
            for ldr in loaders:
                base = getattr(ldr, 'filename', '') or ''
                if not base:
                    continue
                # Pattern: …_CCAN_KMatrix… or …_HCAN_KMatrix… or …_DiagCAN_KMatrix…
                m = _re.search(r'_([A-Za-z]+CAN)_KMatrix', base)
                if m:
                    return m.group(1)
                # Generic fallback: strip extension, keep tail
                stem = base.rsplit('.', 1)[0]
                return stem[-20:] if len(stem) > 20 else stem
    except Exception:
        pass
    # Fallback: physical channel from mirror map.
    phys = _mirror_channel_map.get(cid)
    if phys is not None:
        return f'phys{phys}'
    return ''


def _mf4_describe_raw_channel(channel_id: int) -> dict[str, Any]:
    try:
        cid = int(channel_id)
    except Exception:
        cid = 0

    bn = _mirror_channel_bus_name(cid)

    if cid == 99:
        return {
            'id': cid,
            'bus_type': 'MIRROR_CAN_CATCHALL',
            'label': 'CH 99 - Mirror CAN catch-all',
            'bus_name': bn,
            'network_id': None,
        }
    if 100 <= cid < 150:
        net_id = cid - 100
        lbl = f'CH {cid} - Mirror {bn}' if bn else f'CH {cid} - Mirror CAN net {net_id}'
        return {
            'id': cid,
            'bus_type': 'MIRROR_CAN',
            'label': lbl,
            'bus_name': bn,
            'network_id': net_id,
        }
    if 150 <= cid < 200:
        net_id = cid - 150
        lbl = f'CH {cid} - Mirror {bn}' if bn else f'CH {cid} - Mirror LIN net {net_id}'
        return {
            'id': cid,
            'bus_type': 'MIRROR_LIN',
            'label': lbl,
            'bus_name': bn,
            'network_id': net_id,
        }
    if 200 <= cid < 250:
        net_id = cid - 200
        lbl = f'CH {cid} - Mirror {bn}' if bn else f'CH {cid} - Mirror FlexRay net {net_id}'
        return {
            'id': cid,
            'bus_type': 'MIRROR_FLEXRAY',
            'label': lbl,
            'bus_name': bn,
            'network_id': net_id,
        }
    if cid == 255:
        return {
            'id': cid,
            'bus_type': 'ETH',
            'label': 'CH 255 - Ethernet synthetic',
            'bus_name': '',
            'network_id': None,
        }
    return {
        'id': cid,
        'bus_type': 'CAN',
        'label': f'CH {cid} - Physical CAN',
        'bus_name': '',
        'network_id': None,
    }


def _mf4_suggest_can_dbcs_for_file(mf4_path: str) -> tuple[dict[int, list[str]], dict[int, list[dict[str, Any]]]]:
    """Suggest likely CAN DBCs per raw MF4 channel based on present frame IDs."""
    suggestions: dict[int, list[str]] = {}
    details: dict[int, list[dict[str, Any]]] = {}

    try:
        import numpy as np
        from dbc_loader import load_dbc_database
    except Exception:
        return suggestions, details

    try:
        raw = _mf4_load_raw_table(mf4_path)
        if not raw:
            return suggestions, details
        _t, can_id, _dlc, _data, ch, _flags = raw
    except Exception:
        return suggestions, details

    candidate_names = _mf4_candidate_can_dbcs()
    if not candidate_names:
        return suggestions, details

    dbc_id_sets: dict[str, set[int]] = {}
    for dbc_name in candidate_names:
        dbc_path = _resolve_dbc_path(dbc_name)
        if not dbc_path:
            continue
        try:
            db = load_dbc_database(dbc_path)
        except Exception:
            continue
        ids: set[int] = set()
        for msg in getattr(db, 'messages', []) or []:
            try:
                ids.add(int(getattr(msg, 'frame_id')))
            except Exception:
                continue
        if ids:
            dbc_id_sets[dbc_name] = ids

    if not dbc_id_sets:
        return suggestions, details

    try:
        unique_channels = [int(v) for v in np.unique(ch).tolist()]
    except Exception:
        unique_channels = sorted({int(v) for v in (ch.tolist() if hasattr(ch, 'tolist') else [])})

    for channel_id in sorted(unique_channels):
        meta = _mf4_describe_raw_channel(channel_id)
        if str(meta.get('bus_type') or '').upper() not in {'CAN', 'MIRROR_CAN', 'MIRROR_CAN_CATCHALL'}:
            continue
        try:
            mask = (ch == int(channel_id))
            present_ids = set(int(v) for v in np.unique(can_id[mask]).tolist())
        except Exception:
            present_ids = set()
        if not present_ids:
            continue

        scored: list[dict[str, Any]] = []
        for dbc_name, known_ids in dbc_id_sets.items():
            try:
                matches = len(present_ids & known_ids)
            except Exception:
                matches = 0
            if matches > 0:
                scored.append({'dbc': dbc_name, 'match_count': int(matches)})

        if not scored:
            continue

        scored.sort(key=lambda item: (-int(item.get('match_count') or 0), str(item.get('dbc') or '').lower()))
        details[int(channel_id)] = scored
        suggestions[int(channel_id)] = [str(item.get('dbc') or '') for item in scored if str(item.get('dbc') or '')]

    return suggestions, details


def _mf4_auto_dbcs_for_file(mf4_path: str, channel_filter: int | None = None) -> tuple[list[str], dict[int, list[str]], dict[int, list[dict[str, Any]]]]:
    """Resolve DBC basenames for offline decode/export, preferring file-based suggestions."""
    cfg_map = _mf4_configured_dbcs_by_channel()
    suggestions, details = _mf4_suggest_can_dbcs_for_file(mf4_path)

    def _dedupe(names: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            base = os.path.basename(str(name or '').strip())
            if not base or base in seen:
                continue
            seen.add(base)
            out.append(base)
        return out

    if channel_filter is not None:
        names = list(suggestions.get(int(channel_filter), []) or [])
        if not names:
            names = list(cfg_map.get(int(channel_filter), []) or [])
        if not names:
            try:
                phys = _mirror_channel_map.get(int(channel_filter))
            except Exception:
                phys = None
            if phys is not None:
                names = list(cfg_map.get(int(phys), []) or [])
        if not names and int(channel_filter) == 99:
            names = _mf4_candidate_can_dbcs()
        return _dedupe(names), suggestions, details

    merged: list[str] = []
    for channel_id in sorted(suggestions.keys()):
        merged.extend(suggestions.get(channel_id) or [])
    for channel_id in sorted(cfg_map.keys()):
        merged.extend(cfg_map.get(channel_id) or [])
    if not merged:
        merged = _mf4_candidate_can_dbcs()
    # Filter to only DBC/ARXML files that actually exist on disk.
    # This prevents "dbc not found: simulation.dbc" errors from placeholder sources.
    result = [n for n in _dedupe(merged) if _resolve_dbc_path(n)]
    return result, suggestions, details


def _mf4_channel_decode_family(channel_id: int | None) -> str:
    if channel_id is None:
        return 'CAN'
    meta = _mf4_describe_raw_channel(int(channel_id))
    bus_type = str(meta.get('bus_type') or '').upper()
    if bus_type == 'MIRROR_LIN':
        return 'LIN'
    if bus_type == 'MIRROR_FLEXRAY':
        return 'FLEXRAY'
    return 'CAN'


def _raw_ch_to_label(ch_id: int) -> str:
    """Map raw MF4 channel number to a bus label matching the live logger format.

    The logger uses ``_mf4_channel_label(msg)`` which produces:
      - ``'FlexRay'`` for FLEXRAY type  (channels 200-249)
      - ``'LIN'``     for LIN type      (channels 150-199)
      - ``'Ethernet'``for ETH type      (channel 255)
      - ``'CAN{ch}'`` for everything else
    """
    if 200 <= ch_id < 250:
        return 'FlexRay'
    if 150 <= ch_id < 200:
        return 'LIN'
    if ch_id == 255:
        return 'Ethernet'
    return f'CAN{ch_id}'


def _mf4_list_noncan_decoded_groups(mf4_path: str, channel_id: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        import numpy as np
    except Exception as e:
        return None, str(e)

    family = _mf4_channel_decode_family(channel_id)
    if family not in {'LIN', 'FLEXRAY'}:
        return None, 'unsupported bus family'

    raw = _mf4_load_raw_table(mf4_path)
    if not raw:
        return None, 'mf4 non contiene tabella raw compatibile'

    _t, can_id, _dlc, _data, ch, _flags = raw
    try:
        present_ids = set(int(v) for v in np.unique(can_id[ch == int(channel_id)]).tolist())
    except Exception:
        present_ids = set()

    if not present_ids:
        return [], None

    if family == 'LIN':
        dec = getattr(manager, 'arxml_decoder', None)
        if not dec or not getattr(dec, 'loaded', False):
            return None, 'LIN decode unavailable: ARXML decoder not loaded'
        if int(getattr(dec, 'lin_frame_count', 0) or 0) <= 0:
            return None, 'LIN decode unavailable: active ARXML catalog contains no LIN frames'
        try:
            return list(dec.list_lin_signals(only_ids=present_ids) or []), None
        except Exception as e:
            return None, str(e)

    groups: list[dict[str, Any]] = []
    fibex = getattr(manager, 'fibex', None)
    try:
        frames = getattr(fibex, 'frames', {}) or {}
        sig_defs = getattr(fibex, '_signal_defs', {}) or {}
        for slot_id in sorted(present_ids):
            defs = list(sig_defs.get(int(slot_id)) or [])
            if not defs:
                continue
            msg_name = str(frames.get(int(slot_id)) or f'FlexRay {int(slot_id)}').strip()
            seen: set[str] = set()
            sigs: list[dict[str, Any]] = []
            for d in defs:
                name = str((d or {}).get('name') or '').strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                sigs.append({'key': f'{msg_name}.{name}', 'unit': ''})
            if sigs:
                groups.append({'message': msg_name, 'slot_id': int(slot_id), 'signals': sigs})
    except Exception:
        groups = []

    if groups:
        return groups, None

    dec = getattr(manager, 'arxml_decoder', None)
    if dec and getattr(dec, 'loaded', False):
        try:
            groups = list(dec.list_fr_signals(only_slots=present_ids) or [])
        except Exception as e:
            return None, str(e)
        return groups, None

    return None, 'FlexRay decode unavailable: no FIBEX or ARXML decoder loaded'


def _mf4_decode_noncan_series(
    *,
    mf4_path: str,
    channel_id: int,
    signals: list[str],
    start_s: float | None,
    end_s: float | None,
    start_abs_s: float | None,
    end_abs_s: float | None,
    max_points: int,
    t_mode: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    import math
    try:
        import numpy as np
    except Exception as e:
        return None, str(e)

    family = _mf4_channel_decode_family(channel_id)
    if family not in {'LIN', 'FLEXRAY'}:
        return None, 'unsupported bus family'

    raw = _mf4_load_raw_table(mf4_path)
    if not raw:
        return None, 'mf4 non contiene tabella raw compatibile'
    t_abs, can_id, dlc, payload, ch, fl = raw

    try:
        mask = (ch == int(channel_id))
        t_abs = t_abs[mask]
        can_id = can_id[mask]
        dlc = dlc[mask]
        payload = payload[mask]
        fl = fl[mask]
    except Exception:
        pass

    if t_abs.size == 0:
        return [], None

    base = float(t_abs[0])
    t_start = None if start_s is None else base + float(start_s)
    t_end = None if end_s is None else base + float(end_s)
    if start_abs_s is not None:
        t_start = float(start_abs_s)
    if end_abs_s is not None:
        t_end = float(end_abs_s)

    i0 = 0
    i1 = int(t_abs.size)
    if t_start is not None:
        i0 = int(np.searchsorted(t_abs, t_start, side='left'))
    if t_end is not None:
        i1 = int(np.searchsorted(t_abs, t_end, side='right'))
    i0 = max(0, min(i0, int(t_abs.size)))
    i1 = max(i0, min(i1, int(t_abs.size)))

    t_abs = t_abs[i0:i1]
    can_id = can_id[i0:i1]
    dlc = dlc[i0:i1]
    payload = payload[i0:i1]
    fl = fl[i0:i1]
    if t_abs.size == 0:
        return [], None

    req: dict[str, set[str]] = {}
    # Keys now arrive as "ChannelLabel.SignalName" (e.g. "FlexRay.AB_Anzeige_Fussg").
    # Build a set of bare signal names for matching against ARXML/FIBEX decode output.
    wanted_sigs: dict[str, str] = {}   # bare_sig_name → original key
    for key in signals:
        if '.' not in str(key):
            continue
        _lbl, sig = str(key).split('.', 1)
        sig = str(sig).strip()
        if sig:
            wanted_sigs[sig] = str(key).strip()
    if not wanted_sigs:
        return [], None

    groups, err = _mf4_list_noncan_decoded_groups(mf4_path, channel_id)
    if err:
        return None, err
    unit_map: dict[str, str] = {}
    for group in (groups or []):
        msg_name = str(group.get('message') or '').strip()
        for sig in (group.get('signals') or []):
            key = str((sig or {}).get('key') or '').strip()
            if key:
                unit_map[key] = str((sig or {}).get('unit') or '').strip()

    if int(t_abs.size) > max_points:
        try:
            total = int(t_abs.size)
            ids, counts = np.unique(can_id, return_counts=True)
            quotas = np.maximum(1, np.floor((counts.astype(np.float64) / float(total)) * float(max_points)).astype(int))
            while int(quotas.sum()) > int(max_points):
                j = int(np.argmax(quotas))
                if int(quotas[j]) <= 1:
                    break
                quotas[j] -= 1
            chosen = []
            for fid, q in zip(ids.tolist(), quotas.tolist()):
                idxs = np.nonzero(can_id == fid)[0]
                n = int(idxs.size)
                if n <= 0:
                    continue
                if q >= n:
                    chosen.append(idxs)
                    continue
                take = np.linspace(0, n - 1, num=int(q), dtype=int)
                chosen.append(idxs[take])
            if chosen:
                keep = np.unique(np.concatenate(chosen))
                keep.sort()
                t_abs = t_abs[keep]
                can_id = can_id[keep]
                dlc = dlc[keep]
                payload = payload[keep]
                fl = fl[keep]
        except Exception:
            step = max(1, int(np.ceil(float(t_abs.size) / float(max_points))))
            t_abs = t_abs[::step]
            can_id = can_id[::step]
            dlc = dlc[::step]
            payload = payload[::step]
            fl = fl[::step]

    def _coerce_numeric_local(v: Any) -> float | None:
        if v is None:
            return None
        try:
            if isinstance(v, bool):
                return float(1.0 if v else 0.0)
        except Exception:
            pass
        try:
            if isinstance(v, (int, float)):
                return float(v)
        except Exception:
            pass
        try:
            if hasattr(v, 'dtype') and hasattr(v, 'item'):
                return float(v.item())
        except Exception:
            pass
        try:
            vv = getattr(v, 'value', None)
            if vv is not None:
                return float(vv)
        except Exception:
            pass
        try:
            return float(v)
        except Exception:
            return None

    t0 = float(t_abs[0])
    t_rel = (t_abs - t0).astype(np.float64, copy=False)
    t_out = t_abs.astype(np.float64, copy=False) if t_mode in {'abs', 'absolute'} else t_rel
    out = {k: {'name': k, 'unit': unit_map.get(k, ''), 't': [], 'y': []} for k in signals if '.' in str(k)}

    dec = getattr(manager, 'arxml_decoder', None)
    fibex = getattr(manager, 'fibex', None)
    for idx in range(int(t_abs.size)):
        fid = int(can_id[idx])
        try:
            payload_width = int(payload.shape[1]) if getattr(payload, 'ndim', 1) >= 2 else 8
        except Exception:
            payload_width = 8
        ln = max(0, min(payload_width, int(dlc[idx]) if idx < int(dlc.size) else payload_width))
        b = bytes(payload[idx][:ln].tolist())
        decoded = None
        if family == 'LIN':
            if not dec or not getattr(dec, 'loaded', False):
                continue
            try:
                decoded = dec.decode_lin(fid, b)
            except Exception:
                decoded = None
        else:
            try:
                cyc = int(fl[idx]) & 0xFF
            except Exception:
                cyc = 0
            try:
                decoded = fibex.decode(fid, b, cycle=cyc) if fibex else None
            except Exception:
                decoded = None
            if decoded is None and dec and getattr(dec, 'loaded', False):
                try:
                    decoded = dec.decode_flexray(fid, b)
                except Exception:
                    decoded = None
        if not decoded:
            continue
        sig_map = decoded.get('signals') if isinstance(decoded.get('signals'), dict) else {}
        for sig_name, value in sig_map.items():
            sig_name = str(sig_name).strip()
            if sig_name not in wanted_sigs:
                continue
            fv = _coerce_numeric_local(value)
            if fv is None:
                continue
            try:
                if not math.isfinite(float(fv)):
                    continue
            except Exception:
                continue
            key = wanted_sigs[sig_name]
            slot = out.get(key)
            if slot is None:
                slot = {'name': key, 'unit': unit_map.get(key, ''), 't': [], 'y': []}
                out[key] = slot
            slot['t'].append(float(t_out[idx]))
            slot['y'].append(float(fv))

    return [entry for entry in out.values() if entry['t'] and entry['y']], None


def _mf4_write_series_to_mf4(out_path: str, series: list[dict[str, Any]]) -> None:
    from asammdf import MDF, Signal
    import numpy as np

    def _safe_mf4_group_name_local(s: str) -> str:
        s = str(s or '').strip()
        if not s:
            return 'Signal'
        out = []
        for ch in s:
            out.append(ch if ch.isalnum() or ch in {'_', '-', '.'} else '_')
        name = ''.join(out).strip('._-')
        return name[:120] if name else 'Signal'

    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    out_tmp = out_path[:-4] + '.tmp.mf4' if out_path.lower().endswith('.mf4') else out_path + '.tmp.mf4'

    mdf_out = MDF(version='4.10')
    try:
        for entry in series:
            name = str(entry.get('name') or '').strip()
            t_vec = np.asarray(entry.get('t') or [], dtype=np.float64)
            y_vec = np.asarray(entry.get('y') or [], dtype=np.float64)
            if not name or t_vec.size == 0 or y_vec.size == 0:
                continue
            acq = _safe_mf4_group_name_local(name)
            time_sig = Signal(samples=t_vec, timestamps=t_vec, name='time', unit='s')
            val_sig = Signal(samples=y_vec, timestamps=t_vec, name=name, unit=str(entry.get('unit') or ''))
            mdf_out.append([time_sig, val_sig], acq_name=acq)

        mdf_out.save(out_tmp, overwrite=True)
        saved_path = out_tmp if os.path.exists(out_tmp) else out_tmp + '.mf4'
        os.replace(saved_path, out_path)
    finally:
        try:
            mdf_out.close()
        except Exception:
            pass
        for candidate in (out_tmp, out_tmp + '.mf4'):
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except Exception:
                pass


@app.route('/api/mf4/decoded_signals', methods=['GET'])
def mf4_list_decoded_signals():
    filename = str(request.args.get('file') or '').strip()
    dbc_names = request.args.getlist('dbc')
    dbc_names = [str(x or '').strip() for x in (dbc_names or []) if str(x or '').strip()]
    auto = str(request.args.get('auto') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    ch_filter = request.args.get('channel', None)
    ch_filter_val = None
    if ch_filter is not None and str(ch_filter).strip() != '':
        try:
            ch_filter_val = int(str(ch_filter).strip())
        except Exception:
            return jsonify({'ok': False, 'error': 'invalid channel'}), 400
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith('.mf4'):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    mf4_path = _find_log_file(filename)
    if not mf4_path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    family = _mf4_channel_decode_family(ch_filter_val)
    if ch_filter_val is not None and family in {'LIN', 'FLEXRAY'}:
        groups, err = _mf4_list_noncan_decoded_groups(mf4_path, int(ch_filter_val))
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        # Re-label groups to use channel label (e.g. "FlexRay", "LIN") to
        # match the live logger convention.
        ch_label = _raw_ch_to_label(int(ch_filter_val))
        relabelled: dict[str, dict[str, str]] = {}
        for grp in (groups or []):
            for s in ((grp or {}).get('signals') or []):
                raw_key = str((s or {}).get('key') or '').strip()
                sn = raw_key.split('.', 1)[1] if '.' in raw_key else raw_key
                unit = str((s or {}).get('unit') or '').strip()
                if sn:
                    relabelled.setdefault(ch_label, {})[sn] = unit
        out_groups = []
        for lbl in sorted(relabelled.keys()):
            sig_map = relabelled[lbl]
            sigs = [{'key': f'{lbl}.{sn}', 'unit': sig_map.get(sn, '')} for sn in sorted(sig_map)]
            if sigs:
                out_groups.append({'message': lbl, 'signals': sigs})
        return jsonify({'ok': True, 'groups': out_groups})

    if not dbc_names and auto:
        dbc_names, _sug_map, _sug_details = _mf4_auto_dbcs_for_file(mf4_path, ch_filter_val)

    if not dbc_names:
        return jsonify({'ok': False, 'error': 'missing dbc'}), 400
    dbc_paths = []
    for dbc_name in dbc_names:
        dbc_path = _resolve_dbc_path(dbc_name)
        if not dbc_path:
            return jsonify({'ok': False, 'error': f'dbc not found: {dbc_name}'}), 404
        dbc_paths.append(dbc_path)

    try:
        import numpy as np
        from dbc_loader import load_dbc_database
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        raw = _mf4_load_raw_table(mf4_path)
        if not raw:
            return jsonify({'ok': False, 'error': 'mf4 non contiene tabella CAN raw (CAN_ID/DLC/DataByte*). Usa la vista segnali MF4 “diretti” oppure genera un MF4 raw dal logger.'}), 400
        t, can_id, dlc, data, ch, fl = raw

        if ch_filter_val is not None:
            try:
                mask = (ch == int(ch_filter_val))
                can_id = can_id[mask]
                ch = ch[mask]
            except Exception:
                pass

        present_ids = set(np.unique(can_id).tolist())

        # Determine per-bus-type IDs when no channel filter is set (mixed-bus raw MF4).
        # Channel 200-249 = MIRROR_FLEXRAY, 150-199 = MIRROR_LIN, others = CAN.
        def _split_ids_by_bus():
            can_only: set = set()
            fr_only: set = set()
            lin_only: set = set()
            try:
                unique_chs = np.unique(ch).tolist()
            except Exception:
                return present_ids, set(), set()
            for chn in unique_chs:
                chn_i = int(chn)
                ids_on_ch = set(np.unique(can_id[ch == chn]).tolist())
                if 200 <= chn_i < 250:
                    fr_only |= ids_on_ch
                elif 150 <= chn_i < 200:
                    lin_only |= ids_on_ch
                else:
                    can_only |= ids_on_ch
            return can_only, fr_only, lin_only

        if ch_filter_val is None:
            can_ids, fr_ids, lin_ids = _split_ids_by_bus()
        else:
            meta = _mf4_describe_raw_channel(ch_filter_val)
            bus_type = str(meta.get('bus_type') or '').upper()
            if 'FLEXRAY' in bus_type:
                can_ids, fr_ids, lin_ids = set(), present_ids, set()
            elif 'LIN' in bus_type:
                can_ids, fr_ids, lin_ids = set(), set(), present_ids
            else:
                can_ids, fr_ids, lin_ids = present_ids, set(), set()

        # Build frame_id → set of channel labels (matching the live logger format).
        # IMPORTANT: only include CAN channels here.  FlexRay (200-249) and
        # LIN (150-199) share the CAN_ID column with small slot/frame IDs
        # that collide with CAN arbitration IDs.  If we included them, DBC
        # messages would accidentally populate 'FlexRay'/'LIN' labels and
        # the auto-merge block below would skip FIBEX/ARXML FlexRay decode.
        _unique_id_ch = np.unique(np.column_stack([can_id.reshape(-1, 1), ch.reshape(-1, 1)]), axis=0)
        id_labels: dict[int, set[str]] = {}
        for _row in _unique_id_ch:
            _fid_r, _ch_r = int(_row[0]), int(_row[1])
            if 150 <= _ch_r < 250:
                continue  # skip FlexRay/LIN; handled by auto-merge below
            id_labels.setdefault(_fid_r, set()).add(_raw_ch_to_label(_ch_r))

        # Merge messages/signals across multiple DBCs (and ARXML catalogs)
        # keyed by channel label (e.g. 'CAN0', 'CAN100', 'FlexRay', 'LIN')
        # to match the live logger's signal naming convention.
        merged: dict[str, dict[str, str]] = {}

        def _merge_arxml_signals(arxml_groups: list, force_labels: set[str] | None = None) -> None:
            for grp in (arxml_groups or []):
                msg_name = str((grp or {}).get('message') or '').strip()
                if not msg_name:
                    continue
                labels: set[str] = set()
                if force_labels:
                    labels = force_labels
                else:
                    _gid = (grp or {}).get('frame_id')
                    if _gid is None:
                        _gid = (grp or {}).get('slot_id')
                    if _gid is not None:
                        labels = id_labels.get(int(_gid), set())
                if not labels:
                    labels = {msg_name}
                for label in labels:
                    mm = merged.setdefault(label, {})
                    for s in ((grp or {}).get('signals') or []):
                        raw_key = str((s or {}).get('key') or '').strip()
                        # key is "MsgName.SignalName"
                        sn = raw_key.split('.', 1)[1] if '.' in raw_key else raw_key
                        unit = str((s or {}).get('unit') or '').strip()
                        if sn and sn not in mm:
                            mm[sn] = unit

        for dbc_path in dbc_paths:
            if dbc_path.lower().endswith('.arxml'):
                # Route through ARXML decoder (already loaded at startup or load on demand).
                dec = getattr(manager, 'arxml_decoder', None)
                if not (dec and getattr(dec, 'loaded', False)):
                    # Fallback: load on demand from the specific file.
                    try:
                        from arxml_parser import parse_arxml
                        from arxml_decoder import ArxmlDecoder as _ArxmlDecoder
                        _cat = parse_arxml(dbc_path)
                        dec = _ArxmlDecoder()
                        dec.load_from_catalog(_cat)
                    except Exception as _ae:
                        return jsonify({'ok': False, 'error': f'ARXML load error: {_ae}'}), 500
                # CAN signals (filtered to CAN channels only to avoid FlexRay slot ID collisions)
                if can_ids:
                    _merge_arxml_signals(dec.list_can_signals(only_ids=can_ids))
                # FlexRay signals (from MIRROR_FLEXRAY channels)
                if fr_ids:
                    try:
                        _merge_arxml_signals(dec.list_fr_signals(only_slots=fr_ids), force_labels={'FlexRay'})
                    except Exception:
                        pass
                # LIN signals (from MIRROR_LIN channels)
                if lin_ids:
                    try:
                        _merge_arxml_signals(dec.list_lin_signals(only_ids=lin_ids), force_labels={'LIN'})
                    except Exception:
                        pass
                continue

            db = load_dbc_database(dbc_path)
            for m in getattr(db, 'messages', []) or []:
                try:
                    fid = int(getattr(m, 'frame_id'))
                except Exception:
                    continue
                # Find which channel labels this frame_id appears on.
                labels = id_labels.get(fid, set()) | id_labels.get(fid & 0x1FFFFFFF, set())
                if not labels:
                    continue
                for label in labels:
                    mm = merged.setdefault(label, {})
                    for s in getattr(m, 'signals', []) or []:
                        try:
                            sn = str(getattr(s, 'name') or '').strip()
                            unit = str(getattr(s, 'unit') or '').strip()
                        except Exception:
                            continue
                        if not sn:
                            continue
                        if sn not in mm:
                            mm[sn] = unit
                        # Include _txt variant for signals with choices
                        # (matches the live decoder's _normalize_decoded_signals output)
                        choices = getattr(s, 'choices', None)
                        if choices and isinstance(choices, dict):
                            txt_sn = f'{sn}_txt'
                            if txt_sn not in mm:
                                mm[txt_sn] = ''

        # ── Auto-merge FlexRay (FIBEX) and LIN (ARXML) when auto mode ──
        # The DBC-based loop above only covers CAN.  When "All channels"
        # is selected with auto=True and the MF4 contains FlexRay or LIN
        # channels, merge their signals from the FIBEX loader or ARXML
        # decoder that is already loaded on the global manager.
        if auto and fr_ids and 'FlexRay' not in merged:
            try:
                fibex = getattr(manager, 'fibex', None)
                sig_defs = getattr(fibex, '_signal_defs', {}) or {}
                fr_frames = getattr(fibex, 'frames', {}) or {}
                mm_fr = merged.setdefault('FlexRay', {})
                for slot_id in sorted(fr_ids):
                    defs = list(sig_defs.get(int(slot_id)) or [])
                    for d in defs:
                        sn = str((d or {}).get('name') or '').strip()
                        if sn and sn not in mm_fr:
                            mm_fr[sn] = ''
            except Exception:
                pass
            # Fallback: ARXML decoder
            if not merged.get('FlexRay'):
                _adec = getattr(manager, 'arxml_decoder', None)
                if _adec and getattr(_adec, 'loaded', False):
                    try:
                        _merge_arxml_signals(
                            _adec.list_fr_signals(only_slots=fr_ids),
                            force_labels={'FlexRay'},
                        )
                    except Exception:
                        pass

        if auto and lin_ids and 'LIN' not in merged:
            _adec = getattr(manager, 'arxml_decoder', None)
            if _adec and getattr(_adec, 'loaded', False):
                try:
                    _merge_arxml_signals(
                        _adec.list_lin_signals(only_ids=lin_ids),
                        force_labels={'LIN'},
                    )
                except Exception:
                    pass

        groups = []
        for label in sorted(merged.keys()):
            sig_map = merged[label]
            sigs = [{'key': f'{label}.{sn}', 'unit': str(sig_map.get(sn) or '')} for sn in sorted(sig_map.keys())]
            if sigs:
                bn = ''
                if label.startswith('CAN'):
                    try:
                        bn = _mirror_channel_bus_name(int(label[3:]))
                    except Exception:
                        pass
                groups.append({'message': label, 'bus_name': bn, 'signals': sigs})
        return jsonify({'ok': True, 'groups': groups})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/decoded_data', methods=['POST'])
def mf4_get_decoded_data():
    import math
    def _coerce_numeric(v):
        """Best-effort conversion of cantools decoded values to float.

        Supports:
          - int/float/bool
          - numpy scalar numbers
          - cantools NamedSignalValue-like objects via .value
        """
        if v is None:
            return None
        try:
            if isinstance(v, bool):
                return float(1.0 if v else 0.0)
        except Exception:
            pass
        try:
            if isinstance(v, (int, float)):
                return float(v)
        except Exception:
            pass
        try:
            # numpy scalars, etc.
            if hasattr(v, 'dtype') and hasattr(v, 'item'):
                return float(v.item())
        except Exception:
            pass
        try:
            vv = getattr(v, 'value', None)
            if vv is not None:
                if isinstance(vv, bool):
                    return float(1.0 if vv else 0.0)
                if isinstance(vv, (int, float)):
                    return float(vv)
                try:
                    return float(vv)
                except Exception:
                    return None
        except Exception:
            pass
        try:
            return float(v)
        except Exception:
            return None

    data_in = request.get_json(silent=True) or {}
    if not isinstance(data_in, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    filename = str(data_in.get('file') or '').strip()
    dbc_name = str(data_in.get('dbc') or '').strip()
    dbc_names = data_in.get('dbcs')
    if isinstance(dbc_names, list):
        dbc_names = [str(x or '').strip() for x in dbc_names if str(x or '').strip()]
    else:
        dbc_names = []
    auto = bool(data_in.get('auto'))
    channel = data_in.get('channel', None)
    ch_filter_val = None
    if channel is not None and str(channel).strip() != '':
        try:
            ch_filter_val = int(str(channel).strip())
        except Exception:
            return jsonify({'ok': False, 'error': 'invalid channel'}), 400
    signals = data_in.get('signals')
    if not filename or not filename.lower().endswith('.mf4'):
        return jsonify({'ok': False, 'error': 'invalid file'}), 400
    family = _mf4_channel_decode_family(ch_filter_val)
    if family == 'CAN' and (not dbc_name) and (not dbc_names) and (not auto):
        return jsonify({'ok': False, 'error': 'missing dbc'}), 400
    if not isinstance(signals, list) or not signals:
        return jsonify({'ok': False, 'error': 'signals must be a non-empty list'}), 400

    signals = [str(s).strip() for s in signals if str(s).strip()]
    if not signals:
        return jsonify({'ok': False, 'error': 'signals must be a non-empty list'}), 400

    try:
        start_s = data_in.get('start_s', None)
        start_s = None if start_s is None else float(start_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid start_s'}), 400
    try:
        end_s = data_in.get('end_s', None)
        end_s = None if end_s is None else float(end_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid end_s'}), 400
    try:
        max_points = int(data_in.get('max_points', 5000) or 5000)
    except Exception:
        max_points = 5000
    max_points = max(100, min(max_points, 20000))

    # Optional: use absolute epoch seconds for windowing and/or output.
    t_mode = str(data_in.get('t_mode') or '').strip().lower()
    if t_mode not in {'', 'rel', 'relative', 'abs', 'absolute'}:
        return jsonify({'ok': False, 'error': 'invalid t_mode'}), 400
    try:
        start_abs_s = data_in.get('start_abs_s', None)
        start_abs_s = None if start_abs_s is None else float(start_abs_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid start_abs_s'}), 400
    try:
        end_abs_s = data_in.get('end_abs_s', None)
        end_abs_s = None if end_abs_s is None else float(end_abs_s)
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid end_abs_s'}), 400

    mf4_path = _find_log_file(filename)
    if not mf4_path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404
    if family in {'LIN', 'FLEXRAY'}:
        if ch_filter_val is None:
            return jsonify({'ok': False, 'error': f'{family} decoded view requires selecting a specific raw channel'}), 400
        series, err = _mf4_decode_noncan_series(
            mf4_path=mf4_path,
            channel_id=int(ch_filter_val),
            signals=signals,
            start_s=start_s,
            end_s=end_s,
            start_abs_s=start_abs_s,
            end_abs_s=end_abs_s,
            max_points=max_points,
            t_mode=t_mode,
        )
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        return jsonify({'ok': True, 'series': series or []})

    # Resolve DBC list.
    if not dbc_names and dbc_name:
        dbc_names = [dbc_name]
    if not dbc_names and auto:
        dbc_names, _sug_map, _sug_details = _mf4_auto_dbcs_for_file(mf4_path, ch_filter_val)

    if not dbc_names:
        return jsonify({'ok': False, 'error': 'missing dbc'}), 400

    dbc_paths = []
    for dn in dbc_names:
        p = _resolve_dbc_path(dn)
        if not p:
            return jsonify({'ok': False, 'error': f'dbc not found: {dn}'}), 404
        dbc_paths.append(p)

    try:
        import numpy as np
        from dbc_loader import load_dbc_database
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Parse requested keys into label -> signal set.
    # Keys arrive as "ChannelLabel.SignalName" (e.g. "CAN100.AB_Anzeige_Fussg").
    req = {}          # label → {sig_names}
    all_wanted_sigs = set()  # all bare signal names (for pre-filter)
    for k in signals:
        if '.' not in k:
            continue
        try:
            label, sig = k.split('.', 1)
        except Exception:
            continue
        label = str(label).strip()
        sig = str(sig).strip()
        if not label or not sig:
            continue
        req.setdefault(label, set()).add(sig)
        all_wanted_sigs.add(sig)
    if not req:
        return jsonify({'ok': True, 'series': []})

    try:
        raw = _mf4_load_raw_table(mf4_path)
        if not raw:
            return jsonify({'ok': False, 'error': 'mf4 non contiene tabella CAN raw (CAN_ID/DLC/DataByte*). Questo file sembra già “misurato/decodificato” (stile ETAS/INCA). Usa /api/mf4/data per plottare i canali direttamente.'}), 400
        t_abs, can_id, dlc, payload, ch, fl = raw

        if ch_filter_val is not None:
            try:
                mask = (ch == int(ch_filter_val))
                t_abs = t_abs[mask]
                can_id = can_id[mask]
                dlc = dlc[mask]
                payload = payload[mask]
                ch = ch[mask]
                fl = fl[mask]
            except Exception:
                pass

        if t_abs.size == 0:
            return jsonify({'ok': True, 'series': []})

        # Apply time window relative to first sample (default), or absolute epoch seconds if provided.
        base = float(t_abs[0])
        t_start = None if start_s is None else base + float(start_s)
        t_end = None if end_s is None else base + float(end_s)
        if start_abs_s is not None:
            t_start = float(start_abs_s)
        if end_abs_s is not None:
            t_end = float(end_abs_s)

        i0 = 0
        i1 = int(t_abs.size)
        if t_start is not None:
            i0 = int(np.searchsorted(t_abs, t_start, side='left'))
        if t_end is not None:
            i1 = int(np.searchsorted(t_abs, t_end, side='right'))
        i0 = max(0, min(i0, int(t_abs.size)))
        i1 = max(i0, min(i1, int(t_abs.size)))

        t_abs = t_abs[i0:i1]
        can_id = can_id[i0:i1]
        dlc = dlc[i0:i1]
        payload = payload[i0:i1]
        ch = ch[i0:i1]
        if t_abs.size == 0:
            return jsonify({'ok': True, 'series': []})

        # Load DBCs (ordered). First matching DBC wins on collisions.
        # ARXML paths are routed through manager.arxml_decoder instead of cantools.
        db_maps = []
        messages_by_name = {}
        arxml_dec_for_data = None
        for p in dbc_paths:
            if p.lower().endswith('.arxml'):
                _adec = getattr(manager, 'arxml_decoder', None)
                if _adec and getattr(_adec, 'loaded', False):
                    arxml_dec_for_data = _adec
                else:
                    try:
                        from arxml_parser import parse_arxml
                        from arxml_decoder import ArxmlDecoder as _ArxmlDecoder
                        _cat2 = parse_arxml(p)
                        _adec2 = _ArxmlDecoder()
                        _adec2.load_from_catalog(_cat2)
                        arxml_dec_for_data = _adec2
                    except Exception:
                        pass
                continue
            db = load_dbc_database(p)
            m_by_id = {}
            for m in getattr(db, 'messages', []) or []:
                try:
                    fid = int(getattr(m, 'frame_id'))
                    name = str(getattr(m, 'name') or '').strip()
                except Exception:
                    continue
                m_by_id[fid] = m
                # Also index by the 29-bit masked ID so lookup works with
                # MF4 raw data (logger strips bit 31 before writing).
                masked = fid & 0x1FFFFFFF
                if masked != fid and masked not in m_by_id:
                    m_by_id[masked] = m
                if name and name not in messages_by_name:
                    messages_by_name[name] = m
            db_maps.append(m_by_id)

        # Build reverse choice maps for _txt signal support.
        # Keyed by bare signal name (e.g. "AB_Anzeige_Fussg_txt") since the
        # output key is now "ChannelLabel.SignalName_txt".
        _choice_reverse: dict[str, dict[str, float]] = {}
        for _mbn, _msg in messages_by_name.items():
            for _s in getattr(_msg, 'signals', []) or []:
                _sn = str(getattr(_s, 'name', '') or '').strip()
                _ch = getattr(_s, 'choices', None)
                if _sn and _ch and isinstance(_ch, dict):
                    _txt_key = f'{_sn}_txt'
                    if _txt_key not in _choice_reverse:
                        _rev = {str(v): float(k) for k, v in _ch.items()}
                        if _rev:
                            _choice_reverse[_txt_key] = _rev

        # Filter to only frames whose DBC messages contain any of the
        # requested signal names (signal names are now independent of
        # message names since keys use channel labels).
        wanted_ids = set()
        for _mbn, _msg in messages_by_name.items():
            _msg_sigs = {str(getattr(_s, 'name', '') or '').strip() for _s in (getattr(_msg, 'signals', []) or [])}
            # Also check _txt variants
            _msg_sigs_full = _msg_sigs | {f'{_sn}_txt' for _sn in _msg_sigs}
            if _msg_sigs_full & all_wanted_sigs:
                try:
                    _fid = int(getattr(_msg, 'frame_id'))
                    wanted_ids.add(_fid)
                    wanted_ids.add(_fid & 0x1FFFFFFF)
                except Exception:
                    pass
        if arxml_dec_for_data:
            known = getattr(arxml_dec_for_data, 'known_ids', lambda: set())()
            wanted_ids |= known
            # Also include FlexRay slot IDs and LIN frame IDs whose
            # signals are among the wanted set.
            try:
                for _fr_grp in (arxml_dec_for_data.list_fr_signals() or []):
                    _fr_sigs = {(str((s or {}).get('key') or '').split('.', 1)[1] if '.' in str((s or {}).get('key') or '') else str((s or {}).get('key') or '')) for s in ((_fr_grp or {}).get('signals') or [])}
                    if _fr_sigs & all_wanted_sigs:
                        _sid = (_fr_grp or {}).get('slot_id')
                        if _sid is not None:
                            wanted_ids.add(int(_sid))
            except Exception:
                pass
            try:
                for _lin_grp in (arxml_dec_for_data.list_lin_signals() or []):
                    _lin_sigs = {(str((s or {}).get('key') or '').split('.', 1)[1] if '.' in str((s or {}).get('key') or '') else str((s or {}).get('key') or '')) for s in ((_lin_grp or {}).get('signals') or [])}
                    if _lin_sigs & all_wanted_sigs:
                        _fid2 = (_lin_grp or {}).get('frame_id')
                        if _fid2 is not None:
                            wanted_ids.add(int(_fid2))
            except Exception:
                pass

        if wanted_ids:
            try:
                wanted_arr = np.fromiter((int(x) for x in wanted_ids), dtype=can_id.dtype)
                mask = np.isin(can_id, wanted_arr)
                t_abs = t_abs[mask]
                can_id = can_id[mask]
                dlc = dlc[mask]
                payload = payload[mask]
                ch = ch[mask]
            except Exception:
                pass
            if t_abs.size == 0:
                return jsonify({'ok': True, 'series': []})

        # Downsample (stratified per CAN-ID)
        if int(t_abs.size) > max_points:
            try:
                total = int(t_abs.size)
                ids, counts = np.unique(can_id, return_counts=True)
                if ids.size > 0:
                    quotas = np.maximum(
                        1,
                        np.floor((counts.astype(np.float64) / float(total)) * float(max_points)).astype(int),
                    )
                    while int(quotas.sum()) > int(max_points):
                        j = int(np.argmax(quotas))
                        if int(quotas[j]) <= 1:
                            break
                        quotas[j] -= 1

                    chosen = []
                    for fid, q in zip(ids.tolist(), quotas.tolist()):
                        idxs = np.nonzero(can_id == fid)[0]
                        n = int(idxs.size)
                        if n <= 0:
                            continue
                        if q >= n:
                            chosen.append(idxs)
                            continue
                        take = np.linspace(0, n - 1, num=int(q), dtype=int)
                        chosen.append(idxs[take])

                    if chosen:
                        keep = np.unique(np.concatenate(chosen))
                        keep.sort()
                        t_abs = t_abs[keep]
                        can_id = can_id[keep]
                        dlc = dlc[keep]
                        payload = payload[keep]
                        ch = ch[keep]
            except Exception:
                step = int(np.ceil(float(t_abs.size) / float(max_points)))
                step = max(1, step)
                t_abs = t_abs[::step]
                can_id = can_id[::step]
                dlc = dlc[::step]
                payload = payload[::step]
                ch = ch[::step]

        t0 = float(t_abs[0])
        t_rel = (t_abs - t0).astype(np.float64, copy=False)
        t_out = t_abs.astype(np.float64, copy=False) if t_mode in {'abs', 'absolute'} else t_rel

        # Pre-compute units for requested keys
        # Pre-compute units keyed by bare signal name (used for all channel labels).
        units_by_sig: dict[str, str] = {}
        for _mbn, _msg in messages_by_name.items():
            for s in getattr(_msg, 'signals', []) or []:
                try:
                    sn = str(getattr(s, 'name') or '').strip()
                    u = str(getattr(s, 'unit') or '').strip()
                    if sn and sn not in units_by_sig:
                        units_by_sig[sn] = u
                except Exception:
                    continue
        # Add units from ARXML decoder if present
        if arxml_dec_for_data:
            try:
                for grp in (arxml_dec_for_data.list_can_signals() or []):
                    for s in ((grp or {}).get('signals') or []):
                        k = str((s or {}).get('key') or '').strip()
                        u = str((s or {}).get('unit') or '').strip()
                        sn = k.split('.', 1)[1] if '.' in k else k
                        if sn and sn not in units_by_sig:
                            units_by_sig[sn] = u
            except Exception:
                pass

        def _unit_for_key(key: str) -> str:
            """Look up unit for a 'Label.SignalName' key."""
            sn = key.split('.', 1)[1] if '.' in key else key
            return units_by_sig.get(sn, '')

        out = {k: {'name': k, 'unit': _unit_for_key(k), 't': [], 'y': []} for k in signals if '.' in k}

        try:
            from dbc_loader import _normalize_decoded_signals as _norm_sig
        except Exception:
            _norm_sig = None

        for idx in range(int(t_rel.size)):
            fid = int(can_id[idx])
            ch_val = int(ch[idx])
            label = _raw_ch_to_label(ch_val)

            # Check if any signals from this channel label are wanted.
            needed = req.get(label)
            if not needed:
                continue

            m = None
            for m_by_id in db_maps:
                mm = m_by_id.get(fid)
                if mm is not None:
                    m = mm
                    break

            payload_width = int(payload.shape[1]) if getattr(payload, 'ndim', 0) >= 2 else 0
            ln_w = max(0, min(payload_width, int(dlc[idx]) if payload_width else 0))
            b = bytes(payload[idx][:ln_w].tolist()) if payload_width else b''

            if m is not None:
                try:
                    ml = int(getattr(m, 'length', 8) or 8)
                except Exception:
                    ml = 8
                try:
                    dl = int(dlc[idx])
                except Exception:
                    dl = ml
                ln2 = max(0, min(payload_width, max(ml, min(dl, payload_width))))
                b2 = bytes(payload[idx][:ln2].tolist()) if payload_width else b''
                try:
                    try:
                        raw_decoded = m.decode(b2)
                    except Exception:
                        raw_decoded = m.decode(b2)
                except Exception:
                    continue
                # Normalize like the live decoder: produces both numeric
                # and _txt companion signals from NamedSignalValue objects.
                if _norm_sig is not None:
                    try:
                        decoded_vals = _norm_sig(raw_decoded)
                    except Exception:
                        decoded_vals = raw_decoded
                else:
                    decoded_vals = raw_decoded
            elif arxml_dec_for_data is not None:
                try:
                    ar = arxml_dec_for_data.decode(fid, b)
                except Exception:
                    continue
                if not ar:
                    continue
                decoded_vals = ar.get('signals') or {}
            else:
                continue

            for sn in needed:
                key = f'{label}.{sn}'
                if key not in out:
                    out[key] = {'name': key, 'unit': _unit_for_key(key), 't': [], 'y': []}
                try:
                    val = decoded_vals.get(sn)
                except Exception:
                    val = None
                if val is None:
                    continue
                fv = _coerce_numeric(val)
                if fv is None and sn.endswith('_txt'):
                    # Map choice text to numeric code via reverse choice map
                    rev = _choice_reverse.get(sn)
                    if rev:
                        fv = rev.get(str(val))
                if fv is None:
                    continue
                try:
                    if not math.isfinite(float(fv)):
                        continue
                except Exception:
                    continue
                out[key]['t'].append(float(t_out[idx]))
                out[key]['y'].append(float(fv))

        series = [v for v in out.values() if v['t'] and v['y']]

        # Fallback: any requested signal that got no data may be a FlexRay/LIN
        # signal decoded from a MIRROR channel (200-249 = FR, 150-199 = LIN).
        # When no channel filter is specified, decode them via _mf4_decode_noncan_series.
        if ch_filter_val is None:
            missing_sigs = [k for k in signals if '.' in str(k) and not out.get(k, {}).get('t')]
            if missing_sigs:
                try:
                    _raw_ch = _mf4_load_raw_table(mf4_path)
                    if _raw_ch:
                        _raw_ch_arr = _raw_ch[4]  # channel array
                        _noncan_channels = sorted(set(
                            int(v) for v in np.unique(_raw_ch_arr)
                            if 150 <= int(v) < 250
                        ))
                        for _ncch in _noncan_channels:
                            _nc_series, _ = _mf4_decode_noncan_series(
                                mf4_path=mf4_path,
                                channel_id=_ncch,
                                signals=missing_sigs,
                                start_s=start_s,
                                end_s=end_s,
                                start_abs_s=None,
                                end_abs_s=None,
                                max_points=max_points,
                                t_mode=t_mode or '',
                            )
                            for _ncs in (_nc_series or []):
                                _k = str(_ncs.get('name') or '')
                                if _k and not out.get(_k, {}).get('t'):
                                    out[_k] = _ncs
                                    series.append(_ncs)
                            missing_sigs = [k for k in missing_sigs if not out.get(k, {}).get('t')]
                            if not missing_sigs:
                                break
                except Exception:
                    pass

        return jsonify({'ok': True, 'series': series})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/mf4/export_decoded_mf4', methods=['POST'])
def mf4_export_decoded_mf4():
    """Export a coded MF4 from a raw MF4 using the active live decoder.

    Body:
      - file: source mf4 in logs

        By default viewer DBC/channel/message/signal/time selections are ignored
        and this route replays the full raw MF4 through BusManager's live decode
        configuration.

        Optional: set ``respect_view_selection=true`` to export only the selected
        ``signals``/``channel``/``start_s``/``end_s`` window from the MF4 viewer.
    """

    data_in = request.json or {}
    if not isinstance(data_in, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    filename = str(data_in.get('file') or '').strip()
    if not filename or not filename.lower().endswith('.mf4'):
        return jsonify({'ok': False, 'error': 'invalid file'}), 400

    respect_view_selection = bool(data_in.get('respect_view_selection'))
    selected_signals = None
    selected_channel = None
    selected_start_s = None
    selected_end_s = None

    if respect_view_selection:
        raw_signals = data_in.get('signals')
        if isinstance(raw_signals, list):
            cleaned = []
            for s in raw_signals:
                ss = str(s or '').strip()
                if ss and '.' in ss:
                    cleaned.append(ss)
            if cleaned:
                selected_signals = cleaned

        try:
            ch_raw = data_in.get('channel', None)
            if ch_raw is not None and str(ch_raw).strip() != '':
                selected_channel = int(ch_raw)
        except Exception:
            selected_channel = None

        def _opt_nonneg_float(v):
            try:
                if v is None:
                    return None
                sv = str(v).strip()
                if sv == '':
                    return None
                fv = float(sv)
                if fv < 0.0 or fv != fv or fv in {float('inf'), float('-inf')}:
                    return None
                return fv
            except Exception:
                return None

        selected_start_s = _opt_nonneg_float(data_in.get('start_s', None))
        selected_end_s = _opt_nonneg_float(data_in.get('end_s', None))
        if selected_start_s is not None and selected_end_s is not None and selected_end_s < selected_start_s:
            selected_end_s = None

    mf4_path = _find_log_file(filename)
    if not mf4_path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    # Ensure mirror-channel DBCs and FIBEX are loaded so the export
    # decodes ALL channels (mirror CAN 101/102/… and FlexRay 201/…),
    # not just the physical CAN channels.
    try:
        _load_mirror_dbcs()
    except Exception:
        pass

    try:
        with manager.lock:
            has_live_can_dbcs = any(bool(loaders) for loaders in manager.dbcs.values())
        has_live_arxml = bool(
            getattr(manager, 'arxml_decoder', None)
            and getattr(manager.arxml_decoder, 'loaded', False)
        )
        has_live_fibex = bool(
            getattr(manager, 'fibex', None)
            and getattr(manager.fibex, 'frames', None)
        )
    except Exception:
        has_live_can_dbcs = False
        has_live_arxml = False
        has_live_fibex = False

    # Best-effort: load FIBEX from data_sources if not already loaded.
    if not has_live_fibex:
        try:
            _ds_cfg = config_store.get_config_only() or {}
            _ds_sources = _ds_cfg.get('data_sources') if isinstance(_ds_cfg.get('data_sources'), list) else []
            for _src in _ds_sources:
                if not isinstance(_src, dict):
                    continue
                if str(_src.get('type') or '').strip().upper() != 'FLEXRAY':
                    continue
                _fibex_name = str(_src.get('fibex_name') or '').strip()
                if not _fibex_name:
                    continue
                _fibex_path = os.path.join(UPLOAD_FOLDER_FIBEX, os.path.basename(_fibex_name))
                if os.path.isfile(_fibex_path):
                    manager.load_fibex(_fibex_path)
                    has_live_fibex = True
                    break
        except Exception:
            pass

    if not has_live_can_dbcs and not has_live_arxml and not has_live_fibex:
        return jsonify({
            'ok': False,
            'error': 'live decoding is not configured; load the runtime databases first',
        }), 409

    try:
        import time as _time
        from mf4_decoded_export import (
            MF4Decoder,
            _mf4_has_ethernet_metrics,
            export_ethernet_numeric_mf4,
            merge_ethernet_numeric_channels_into_mf4,
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        # Reuse the already-loaded ARXML catalog/decoder from startup to avoid
        # re-parsing the 200 MB+ ARXML file (which would spike RAM by ~1 GB).
        _pre_arxml_dec = getattr(manager, 'arxml_decoder', None)
        _pre_arxml_cat = None
        if _pre_arxml_dec and getattr(_pre_arxml_dec, 'loaded', False):
            try:
                from arxml_parser import get_active_catalog as _get_cat
                _pre_arxml_cat = _get_cat()
            except Exception:
                pass

        decoder = MF4Decoder(mf4_path, [],
                             arxml_catalog=_pre_arxml_cat,
                             arxml_decoder=_pre_arxml_dec,
                             bus_manager=manager)

        out_dir = os.path.join(LOG_FOLDER, 'exports')
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        base_name = os.path.basename(filename)
        stem = base_name[:-4] if base_name.lower().endswith('.mf4') else base_name
        ts = _time.strftime('%Y%m%d_%H%M%S')
        out_name = f'{stem}_coded_{ts}.mf4'
        out_path = os.path.join(out_dir, out_name)

        t0_epoch = None
        try:
            decoder.export(
                out_path,
                signals=selected_signals,
                channel=selected_channel,
                start_s=selected_start_s,
                end_s=selected_end_s,
            )
            # Extract the epoch timestamp of the first raw frame so Ethernet
            # channels can be relativized to the same time base.
            try:
                if decoder._raw is not None and len(decoder._raw) > 0:
                    t0_epoch = float(decoder._raw[0][0])
            except Exception:
                pass
        except Exception as dec_err:
            # Ethernet logger traces (.eth.mf4) do not contain CAN_ID/DLC/DataByte*
            # and therefore cannot be replayed through MF4Decoder.
            # Fallback: export numeric Ethernet metrics channels directly.
            try:
                if _mf4_has_ethernet_metrics(mf4_path):
                    export_ethernet_numeric_mf4(mf4_path, out_path)
                else:
                    raise dec_err
            except Exception:
                raise dec_err

        try:
            raw_stem = os.path.splitext(os.path.basename(mf4_path))[0]
            if raw_stem and not raw_stem.endswith('.eth'):
                session_stem = raw_stem
                if '_part' in session_stem:
                    session_stem = session_stem.rsplit('_part', 1)[0]
                eth_candidate = os.path.join(os.path.dirname(mf4_path), f'{session_stem}.eth.mf4')
                include_ethernet_merge = True
                if respect_view_selection and selected_signals:
                    include_ethernet_merge = any(
                        str(s).startswith('Ethernet.') or str(s).startswith('XCP:')
                        for s in selected_signals
                    )

                if include_ethernet_merge and os.path.isfile(eth_candidate) and os.path.abspath(eth_candidate) != os.path.abspath(mf4_path):
                    merge_ethernet_numeric_channels_into_mf4(
                        out_path,
                        eth_candidate,
                        t0_epoch=t0_epoch,
                        start_s=selected_start_s,
                        end_s=selected_end_s,
                    )
        except Exception:
            pass

        return jsonify({'ok': True, 'file': out_name, 'relative_name': f'exports/{out_name}', 'path': out_path})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/mf4/decode_csv', methods=['GET'])
def mf4_decode_csv_download():
    """Decode a raw MF4 (CAN_ID/DLC/DataByte*) into a per-frame CSV download.

    Query params:
      - file: mf4 filename present in logs
      - dbc: repeatable dbc name(s) to try in order (first successful decode wins)
      - max_frames: optional safety cap
    """
    filename = str(request.args.get('file') or '').strip()
    dbc_names = request.args.getlist('dbc')
    dbc_names = [str(x or '').strip() for x in (dbc_names or []) if str(x or '').strip()]
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file'}), 400
    if not filename.lower().endswith('.mf4'):
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400
    if filename.lower().endswith('.tmp.mf4'):
        return jsonify({'ok': False, 'error': 'cannot decode incomplete tmp mf4'}), 400
    if not dbc_names:
        return jsonify({'ok': False, 'error': 'missing dbc'}), 400

    try:
        max_frames = int(request.args.get('max_frames', 0) or 0)
    except Exception:
        max_frames = 0
    max_frames = max(0, min(max_frames, 2_000_000))

    mf4_path = _find_log_file(filename)
    if not mf4_path:
        return jsonify({'ok': False, 'error': 'file not found'}), 404

    dbc_paths = []
    for dbc_name in dbc_names:
        dbc_path = _resolve_dbc_path(dbc_name)
        if not dbc_path:
            return jsonify({'ok': False, 'error': f'dbc not found: {dbc_name}'}), 404
        dbc_paths.append(dbc_path)

    try:
        import io
        import csv
        import json
        import numpy as np
        import asammdf
        from dbc_loader import load_dbc_database
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Load DBCs (ordered)
    db_maps = []
    for p in dbc_paths:
        db = load_dbc_database(p)
        m_by_id = {}
        for m in getattr(db, 'messages', []) or []:
            try:
                fid = int(getattr(m, 'frame_id'))
            except Exception:
                continue
            m_by_id[fid] = m
        db_maps.append(m_by_id)

    # Read raw table from MF4
    try:
        mdf = asammdf.MDF(mf4_path)
    except Exception as e:
        # Often happens when file is still being written.
        return jsonify({'ok': False, 'error': f'cannot open mf4: {e}'}), 409

    try:
        try:
            can_sig = mdf.get('CAN_ID')
            dlc_sig = mdf.get('DLC')
        except Exception:
            return jsonify({'ok': False, 'error': 'mf4 does not contain raw CAN table (CAN_ID/DLC/DataByte*)'}), 400

        t = np.asarray(getattr(can_sig, 'timestamps', []), dtype=np.float64)
        can_id = np.asarray(getattr(can_sig, 'samples', []), dtype=np.uint32)
        dlc = np.asarray(getattr(dlc_sig, 'samples', []), dtype=np.uint16)

        try:
            ch_sig = mdf.get('Channel')
            ch = np.asarray(getattr(ch_sig, 'samples', []), dtype=np.uint16)
        except Exception:
            ch = np.zeros_like(can_id, dtype=np.uint16)
        try:
            fl_sig = mdf.get('Flags')
            fl = np.asarray(getattr(fl_sig, 'samples', []), dtype=np.uint32)
        except Exception:
            fl = np.zeros_like(can_id, dtype=np.uint32)

        bytes_cols = []
        for i in range(8):
            s = mdf.get(f'DataByte{i}')
            bytes_cols.append(np.asarray(getattr(s, 'samples', []), dtype=np.uint8))

        if t.size == 0 or can_id.size == 0:
            return jsonify({'ok': False, 'error': 'mf4 contains no data'}), 400

        n = int(min(t.size, can_id.size, dlc.size, ch.size, fl.size, *(c.size for c in bytes_cols)))
        if max_frames and n > max_frames:
            n = int(max_frames)

        t = t[:n]
        can_id = can_id[:n]
        dlc = dlc[:n]
        ch = ch[:n]
        fl = fl[:n]
        data = np.stack([c[:n] for c in bytes_cols], axis=1)
    finally:
        try:
            mdf.close()
        except Exception:
            pass

    # Streaming CSV response to keep RAM usage low.
    def _iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['timestamp_ms', 'channel', 'id_hex', 'dlc', 'flags', 'message_name', 'signals_json', 'data_hex'])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        for i in range(int(n)):
            fid = int(can_id[i])
            frame_dlc = int(dlc[i])
            frame_ch = int(ch[i])
            frame_flags = int(fl[i])
            # MF4 timestamps are stored as seconds.
            ts_ms = int(float(t[i]) * 1000.0)

            payload = bytes(int(x) & 0xFF for x in data[i, : max(0, min(frame_dlc, 8))].tolist())

            msg_name = ''
            sig_json = '{}'
            decoded_ok = False
            for m_by_id in db_maps:
                m = m_by_id.get(fid)
                if not m:
                    continue
                try:
                    try:
                        decoded = m.decode(payload, decode_choices=False)
                    except TypeError:
                        decoded = m.decode(payload)
                    msg_name = str(getattr(m, 'name', '') or '')
                    sig_json = json.dumps(decoded or {}, ensure_ascii=False)
                    decoded_ok = True
                    break
                except Exception:
                    continue

            if not decoded_ok:
                sig_json = '{}'

            writer.writerow([
                ts_ms,
                frame_ch,
                hex(fid),
                frame_dlc,
                frame_flags,
                msg_name,
                sig_json,
                payload.hex(),
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    out_name = os.path.basename(filename).rsplit('.', 1)[0] + '.decoded.csv'
    resp = Response(_iter_csv(), mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = f'attachment; filename="{out_name}"'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/logs', methods=['DELETE'])
def delete_all_logs():
    # Don't delete while logging is active (files may be open).
    try:
        if getattr(shared_logger, 'active', False):
            return jsonify({"status": "busy", "error": "logging active"}), 409
    except Exception:
        pass
    try:
        if getattr(eth_manager, 'mf4_logger', None) is not None:
            return jsonify({"status": "busy", "error": "ethernet mf4 logging active"}), 409
    except Exception:
        pass

    deleted = []
    errors = []
    try:
        any_folder = False
        for folder in _iter_log_folders():
            if not os.path.exists(folder):
                continue
            any_folder = True
            for name in os.listdir(folder):

                if name in {'webapp.out', 'webapp.pid'}:
                    continue
                file_path = os.path.join(folder, name)
                if not os.path.isfile(file_path):
                    continue
                try:
                    os.remove(file_path)
                    deleted.append(name)
                except Exception as e:
                    errors.append({"name": name, "error": str(e)})
        if not any_folder:
            return jsonify({"status": "ok", "deleted": [], "errors": []})
        return jsonify({"status": "ok", "deleted": deleted, "errors": errors})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/scan', methods=['POST'])
def start_scan():
    data = request.json or {}
    try:
        channel_id = int(data.get('channel_id', 0))
    except Exception:
        return jsonify({"status": "error", "error": "invalid channel_id"}), 400

    if scanner_service.start_scan(channel_id):
        return jsonify({"status": "started"})

    return jsonify({"status": "error", "error": "scan already running"}), 409


@app.route('/api/scan/read_dtc_single', methods=['POST'])
def read_dtc_single_ecu():
    """Read DTCs from a single ECU via DoIP without full scan.

    Request JSON:
      target_address: str|int  — ECU logical address (e.g. "0x4005" or 16389)
      filter: str              — "all" (default), "active", "confirmed", "pending"
      include_extended: bool   — fetch extended data records (default true)
      gateway_ip: str          — override gateway IP (optional, uses config if omitted)
    """
    data = request.json or {}
    try:
        ta_raw = data.get('target_address')
        if isinstance(ta_raw, str):
            target_address = int(ta_raw.strip(), 0) & 0xFFFF
        else:
            target_address = int(ta_raw) & 0xFFFF
    except Exception:
        return jsonify({"status": "error", "error": "invalid target_address"}), 400

    dtc_filter = str(data.get('filter', 'all')).strip().lower()
    mask_map = {'all': 0xFF, 'active': 0x01, 'confirmed': 0x08, 'pending': 0x04}
    status_mask = mask_map.get(dtc_filter, 0xFF)
    include_extended = bool(data.get('include_extended', True))

    # Resolve gateway IP
    gateway_ip = str(data.get('gateway_ip', '')).strip()
    if not gateway_ip:
        try:
            cfg = config_store.get_config_only() or {}
            es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
            gateway_ip = str(es.get('target_ip') or '').strip()
        except Exception:
            pass
    if not gateway_ip:
        try:
            from vag_scanner import discover_doip_gateway_ip
            gateway_ip = discover_doip_gateway_ip(timeout_s=1.0) or ''
        except Exception:
            pass
    if not gateway_ip:
        return jsonify({"status": "error", "error": "no gateway IP configured or discoverable"}), 400

    try:
        from vag_scanner import DoIPGatewayScanner, _load_active_pdx_dtc_map, _dtc_description, _load_active_pdx_did_index

        doip = DoIPGatewayScanner(gateway_ip)
        doip._connect_with_recovery()
        doip._routing_activation()
        doip.did_index = _load_active_pdx_did_index()

        dtcs = doip._read_dtcs_best_effort(target_address, status_mask=status_mask)
        dtc_map = _load_active_pdx_dtc_map()

        result = []
        for d in dtcs:
            desc = _dtc_description(d.code, dtc_map) if d.code and dtc_map else ''
            entry = {
                'code': d.code,
                'uds_dtc': f"0x{d.uds_dtc:06X}" if isinstance(d.uds_dtc, int) else None,
                'status_byte': f"0x{d.status_byte:02X}" if isinstance(d.status_byte, int) else None,
                'status_desc': d.status_desc,
                'dtc_class': d.dtc_class or ('ACTIVE' if d.active else 'PASSIVE'),
                'active': d.active,
                'raw': d.raw,
                'description': desc or d.description,
            }
            extra = d.extra if isinstance(getattr(d, 'extra', None), dict) else {}
            if extra.get('odometer_km') is not None:
                entry['odometer_km'] = extra['odometer_km']
            if extra.get('timestamp_iso'):
                entry['timestamp_iso'] = extra['timestamp_iso']
            elif extra.get('timestamp_text'):
                entry['timestamp_text'] = extra['timestamp_text']
            if extra.get('extended_data'):
                entry['extended_data'] = extra['extended_data']
            if extra.get('snapshots'):
                entry['snapshots'] = extra['snapshots']
            result.append(entry)

        doip.close()

        active_count = sum(1 for d in result if d.get('dtc_class') == 'ACTIVE')
        passive_count = sum(1 for d in result if d.get('dtc_class') == 'PASSIVE')
        sporadic_count = sum(1 for d in result if d.get('dtc_class') == 'SPORADIC')

        return jsonify({
            "status": "ok",
            "target_address": f"0x{target_address:04X}",
            "filter": dtc_filter,
            "status_mask": f"0x{status_mask:02X}",
            "dtc_count": len(result),
            "active": active_count,
            "passive": passive_count,
            "sporadic": sporadic_count,
            "dtcs": result,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/scantools/run', methods=['POST'])
def scantools_run():
    # Keep scan/report output directory coherent with the backend's active
    # logging directory (LOG_FOLDER). Some ScanTools paths and helper scripts
    # fall back to ../logs unless an env var is set.
    try:
        os.environ['KBSM_LOG_DIR'] = str(LOG_FOLDER)
        os.environ['SCANTOOLS_REPORT_DIR'] = str(LOG_FOLDER)
    except Exception:
        pass

    data = request.json or {}
    try:
        channel_id = int(data.get('channel_id', 0))
    except Exception:
        return jsonify({"status": "error", "error": "invalid channel_id"}), 400

    action = (data.get('action') or '').strip()
    if not action:
        return jsonify({"status": "error", "error": "missing action"}), 400

    # DoIP scan is Ethernet-based and does not require CAN channel to be active.
    if action == 'vag_doip_scan_report':
        cfg = {}
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gateway_ip = str(es.get('target_ip') or '').strip()
        gateway_iface = str(es.get('interface') or '').strip()

        # Respect system_mode as authoritative for interface selection.
        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''
        if sm == 'simulation':
            gateway_iface = 'lo'
        elif sm == 'real':
            gateway_iface = 'eth0'

        auto_discover = bool(es.get('doip_auto_discover', True))
        try:
            tester_logical_address = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00

        try:
            live_tester_logical_address = int(es.get('doip_live_tester_logical_address', 0x0E01) or 0x0E01)
        except Exception:
            live_tester_logical_address = 0x0E01

        started = scanner_service.start_action(channel_id, action, params={
            'gateway_ip': gateway_ip,
            'gateway_iface': gateway_iface,
            'auto_discover': auto_discover,
            'tester_logical_address': tester_logical_address,
            'live_tester_logical_address': live_tester_logical_address,
        })
        if started:
            return jsonify({"status": "started"})
        return jsonify({"status": "busy", "error": "scanner busy"}), 409

    # Manual IPv6 recovery action.
    if action == 'doip_recover_network':
        cfg = {}
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gateway_iface = str(es.get('interface') or '').strip()

        # Respect system_mode as authoritative for interface selection.
        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''
        if sm == 'simulation':
            gateway_iface = 'lo'
        elif sm == 'real':
            gateway_iface = 'eth0'

        started = scanner_service.start_action(channel_id, action, params={
            'gateway_iface': gateway_iface,
        })
        if started:
            return jsonify({"status": "started"})
        return jsonify({"status": "busy", "error": "scanner busy"}), 409

    # Self-test is designed to be safe and can run without an active CAN channel.
    # If a channel is active, the test will also validate basic CAN TX/RX.
    if action == 'self_test':
        cfg = {}
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gateway_ip = str(es.get('target_ip') or '').strip()
        gateway_iface = str(es.get('interface') or '').strip()

        # Respect system_mode as authoritative for interface selection.
        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''
        if sm == 'simulation':
            gateway_iface = 'lo'
        elif sm == 'real':
            gateway_iface = 'eth0'

        auto_discover = bool(es.get('doip_auto_discover', True))
        try:
            tester_logical_address = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00

        started = scanner_service.start_action(channel_id, action, params={
            'gateway_ip': gateway_ip,
            'gateway_iface': gateway_iface,
            'auto_discover': auto_discover,
            'tester_logical_address': tester_logical_address,
        })
        if started:
            return jsonify({"status": "started"})
        return jsonify({"status": "busy", "error": "scanner busy"}), 409

    # DoIP-only actions (Automotive Ethernet) that bypass the CAN channel check.
    # These talk to the vehicle gateway via TCP/13400 and don't need a CAN bus
    # to be opened. Each action gets its standard params from eth_settings.
    if action in ('doip_clear_dtcs', 'doip_mode06'):
        cfg = {}
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gateway_ip = str(es.get('target_ip') or '').strip()
        gateway_iface = str(es.get('interface') or '').strip()

        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''
        if sm == 'simulation':
            gateway_iface = 'lo'
        elif sm == 'real':
            gateway_iface = 'eth0'

        auto_discover = bool(es.get('doip_auto_discover', True))
        try:
            tester_logical_address = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00

        # Allow per-request overrides from the UI payload.
        req_params = data if isinstance(data, dict) else {}
        if 'gateway_ip' in req_params and str(req_params.get('gateway_ip') or '').strip():
            gateway_ip = str(req_params.get('gateway_ip') or '').strip()
        if 'gateway_iface' in req_params and str(req_params.get('gateway_iface') or '').strip():
            gateway_iface = str(req_params.get('gateway_iface') or '').strip()
        if 'tester_logical_address' in req_params and req_params.get('tester_logical_address') not in (None, ''):
            try:
                tester_logical_address = int(req_params.get('tester_logical_address'))
            except Exception:
                pass

        started = scanner_service.start_action(channel_id, action, params={
            'gateway_ip': gateway_ip,
            'gateway_iface': gateway_iface,
            'auto_discover': auto_discover,
            'tester_logical_address': tester_logical_address,
        })
        if started:
            return jsonify({"status": "started"})
        return jsonify({"status": "busy", "error": "scanner busy"}), 409

    # Require the Bus system to be running and the selected channel to be active
    try:
        with manager.lock:
            channel_active = channel_id in manager.handlers
            # When bus start is asynchronous, there can be a short window where
            # the CAN channel is opening but handlers aren't populated yet.
            if not channel_active:
                try:
                    br = getattr(manager, 'bitrate_by_channel', {}) or {}
                    channel_active = channel_id in br and bool(getattr(manager, 'running', False))
                except Exception:
                    pass
    except Exception:
        channel_active = False

    # Small grace period for async bus start.
    if not channel_active:
        try:
            for _ in range(8):
                time.sleep(0.25)
                with manager.lock:
                    if channel_id in getattr(manager, 'handlers', {}):
                        channel_active = True
                        break
        except Exception:
            pass

    if not channel_active:
        # Special case: 'clear_dtcs' can fallback to DoIP if CAN is not active.
        if action == 'clear_dtcs':
            cfg = {}
            try:
                cfg = config_store.get_config_only() or {}
            except Exception:
                cfg = {}
            es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
            gateway_ip = str(es.get('target_ip') or '').strip()
            gateway_iface = str(es.get('interface') or '').strip()
            
            # Switch action to DoIP specific clear
            doip_action = 'doip_clear_dtcs'
            
            started = scanner_service.start_action(channel_id, doip_action, params={
                'gateway_ip': gateway_ip,
                'gateway_iface': gateway_iface,
                # Reuse existing params if any
                **data
            })
            if started:
                return jsonify({"status": "started", "mode": "doip_fallback"})
            return jsonify({"status": "busy", "error": "scanner busy"}), 409

        return jsonify({
            "status": "error",
            "error": "channel not active; start Bus System first and include this channel"
        }), 409

    # Safety: avoid giving a false impression of "in-vehicle" operation.
    # If ECU simulation is enabled or CAN drivers are missing (mock mode), refuse unless overridden.
    allow_mock = str(os.getenv('KBSM_ALLOW_MOCK_SCAN', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}
    try:
        if bool(getattr(manager, 'simulate_ecu', False)) and not allow_mock:
            return jsonify({
                "status": "error",
                "error": "ECU simulation is enabled (KBSM_SIM_ECU=1). Disable it for real vehicle scans or set KBSM_ALLOW_MOCK_SCAN=1 for dev.",
            }), 409
    except Exception:
        pass
    try:
        if bool(getattr(manager, 'can_driver_is_mock', lambda: False)()) and not allow_mock:
            return jsonify({
                "status": "error",
                "error": "CAN driver is running in mock mode (no real Kvaser/canlib). Install/enable CAN drivers for real vehicle scans or set KBSM_ALLOW_MOCK_SCAN=1 for dev.",
            }), 503
    except Exception:
        pass

    # Default path for ScanTools actions (CAN-based etc.). If a handler above
    # didn't return, start the action with its request payload (if any).
    started = scanner_service.start_action(channel_id, action, params=data)
    if started:
        return jsonify({"status": "started"})
    return jsonify({"status": "busy", "error": "scanner busy"}), 409


@app.route('/api/scantools/status', methods=['GET'])
def scantools_status():
    """Return ScanTools runtime status (running/action/channel and last result)."""
    try:
        st = scanner_service.status() if scanner_service else {}
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    return jsonify({"status": "ok", "scantools": st})


@app.route('/api/scantools/live/start', methods=['POST'])
def scantools_live_start():
    data = request.json or {}
    try:
        channel_id = int(data.get('channel_id', 0))
    except Exception:
        channel_id = 0

    # Resolve transport — default to DoIP
    transport = str(data.get('transport') or '').strip().lower()
    if transport not in ('can', 'doip'):
        # Auto-detect from config
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        ea = cfg.get('experimental_assistant') if isinstance(cfg.get('experimental_assistant'), dict) else {}
        dt = str(ea.get('diagnostic_transport') or 'doip').strip().lower()
        if dt == 'can':
            transport = 'can'
        elif dt == 'doip':
            transport = 'doip'
        else:
            # auto / auto_prefer_can: check if DoIP gateway is available
            es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
            gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
            has_gw = bool(str(es.get('target_ip') or '').strip()) or bool(str(gm.get('gateway_ip') or '').strip())
            transport = 'doip' if has_gw or bool(es.get('doip_enabled', False)) else 'can'

    if transport == 'doip':
        # DoIP live data — no CAN channel needed
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
        gateway_ip = str(es.get('target_ip') or '').strip()
        if not gateway_ip:
            gateway_ip = str(gm.get('gateway_ip') or '').strip()
        gateway_iface = str(es.get('interface') or '').strip() or 'eth0'
        try:
            tester_logical_address = int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00)
        except Exception:
            tester_logical_address = 0x0E00
        try:
            live_tester_logical_address = int(es.get('doip_live_tester_logical_address', 0x0E01) or 0x0E01)
        except Exception:
            live_tester_logical_address = 0x0E01

        interval_s = data.get('interval_s', 1.0)
        try:
            interval_s = max(0.5, float(interval_s))
        except Exception:
            interval_s = 1.0

        started = scanner_service.start_live(channel_id, interval_s=interval_s, transport='doip', doip_params={
            'gateway_ip': gateway_ip,
            'gateway_iface': gateway_iface,
            'tester_logical_address': tester_logical_address,
            'live_tester_logical_address': live_tester_logical_address,
            '_sentinel': experimental_assistant,
        })
        if started:
            return jsonify({"status": "started", "transport": "doip"})
        return jsonify({"status": "busy", "error": "scanner busy"}), 409

    # CAN transport — requires active channel
    try:
        with manager.lock:
            channel_active = channel_id in manager.handlers
    except Exception:
        channel_active = False

    if not channel_active:
        return jsonify({
            "status": "error",
            "error": "channel not active; start Bus System first and include this channel"
        }), 409

    interval_s = data.get('interval_s', 0.2)
    try:
        interval_s = float(interval_s)
    except Exception:
        interval_s = 0.2

    if scanner_service.start_live(channel_id, interval_s=interval_s):
        return jsonify({"status": "started", "transport": "can"})
    return jsonify({"status": "busy", "error": "scanner busy"}), 409


@app.route('/api/scantools/live/stop', methods=['POST'])
def scantools_live_stop():
    stopped = scanner_service.stop_live()
    # Safety net: ensure Sentinel MIL polling is resumed even if the
    # live-data thread hasn't reached its finally block yet.
    try:
        if experimental_assistant and hasattr(experimental_assistant, 'resume_doip_mil'):
            experimental_assistant.resume_doip_mil()
    except Exception:
        pass
    return jsonify({"status": "stopped" if stopped else "not_running"})

@app.route('/api/logs/<path:filename>', methods=['GET'])
def download_log(filename):
    import re
    file_path = _find_log_file(filename)
    if not file_path:
        return jsonify({"status": "not_found"}), 404
    # If this is a chunked MF4 part and multiple parts exist for the same base,
    # return a ZIP containing all parts so users get the full session.
    try:
        normalized_name = _normalize_log_relative_path(filename) or str(filename or '')
        parent_prefix = 'exports/' if str(normalized_name).startswith('exports/') else ''
        part_re = re.compile(r'^(?P<base>.+)_part\d{4}\.mf4$', flags=re.IGNORECASE)
        m = part_re.match(os.path.basename(filename))
    except Exception:
        m = None
        parent_prefix = ''
    if m:
        base = m.group('base')
        merged_name = f'{parent_prefix}{base}.mf4'
        merged_path = _find_log_file(merged_name)
        if merged_path:
            return send_file(merged_path, as_attachment=True, download_name=os.path.basename(merged_path))

        # Collect all parts for this base across log folders.
        parts = []
        scan_folders = _iter_export_log_folders() if parent_prefix else _iter_log_folders()
        for folder in scan_folders:
            try:
                if not os.path.isdir(folder):
                    continue
                for f in os.listdir(folder):
                    if not isinstance(f, str):
                        continue
                    if not part_re.match(f):
                        continue
                    if f.lower().endswith('.tmp.mf4') or f.lower().endswith('.tmp'):
                        continue
                    path = os.path.join(folder, f)
                    if os.path.isfile(path) and f.startswith(base + '_part'):
                        parts.append((f, path))
            except Exception:
                continue

        if len(parts) > 1:
            try:
                from flask import after_this_request
                import tempfile
                import zipfile

                with tempfile.NamedTemporaryFile(prefix=f'{base}_parts_', suffix='.zip', delete=False) as tmp:
                    tmp_path = tmp.name

                parts.sort(key=lambda x: x[0])
                with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as z:
                    for name, path in parts:
                        try:
                            z.write(path, arcname=name)
                        except Exception:
                            continue

                @after_this_request
                def _cleanup_tmp_bundle(resp):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    return resp

                return send_file(
                    tmp_path,
                    mimetype='application/zip',
                    as_attachment=True,
                    download_name=f'{base}_parts.zip',
                )
            except Exception:
                pass
    return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))


@app.route('/api/logs/<path:filename>', methods=['DELETE'])
def delete_log(filename):
    # Don't delete while logging is active (files may be open).
    try:
        if getattr(shared_logger, 'active', False):
            return jsonify({"status": "busy", "error": "logging active"}), 409
    except Exception:
        pass
    try:
        if getattr(eth_manager, 'mf4_logger', None) is not None:
            return jsonify({"status": "busy", "error": "ethernet mf4 logging active"}), 409
    except Exception:
        pass

    # Defensive: only allow deleting files inside LOG_FOLDER
    safe_name = _normalize_log_relative_path(filename)
    if not safe_name:
        return jsonify({"status": "error", "error": "invalid filename"}), 400

    if os.path.basename(safe_name) in {'webapp.out', 'webapp.pid'}:
        return jsonify({"status": "error", "error": "cannot delete system log"}), 403

    file_path = _find_log_file(safe_name)
    if not file_path:
        return jsonify({"status": "not_found"}), 404

    try:
        real_file = os.path.realpath(file_path)
        allowed = False
        for folder in _iter_log_folders():
            try:
                real_logs = os.path.realpath(folder)
                if real_file.startswith(real_logs + os.sep):
                    allowed = True
                    break
            except Exception:
                continue
        if not allowed:
            return jsonify({"status": "error", "error": "invalid path"}), 400
    except Exception:
        return jsonify({"status": "error", "error": "invalid path"}), 400

    try:
        os.remove(file_path)
        return jsonify({"status": "deleted", "name": safe_name})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/camera/status', methods=['GET'])
def camera_status():
    return jsonify(_camera_manager.status())


@app.route('/api/can/inject', methods=['POST'])
def inject_can_frame():
    """Inject a synthetic CAN frame into the pipeline (dev/test helper).

        Body:
            {"channel_id":0,"id":291,"data":[1,2,3,4,5,6,7,8]}
            {"channel":0,"id":291,"data":[1,2,3,4,5,6,7,8]}
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        channel_id = int(data.get('channel_id', data.get('channel', 0)))
        arb_id = int(data.get('id', 0))
        payload = data.get('data')
        if not isinstance(payload, list):
            return jsonify({'ok': False, 'error': 'data must be a list of bytes'}), 400
        payload = [int(x) & 0xFF for x in payload]
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        manager.inject_frame(channel_id, arb_id, payload, flags=0, frame_type='CAN')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/can/inject_burst', methods=['POST'])
def inject_can_burst():
    """Inject many synthetic CAN frames in one request (dev/test helper).

    Body:
      {"channel_id":0,"count":200}

    This is intended for stress testing without overwhelming the HTTP server with
    one request per frame.
    """
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400
    try:
        channel_id = int(data.get('channel_id', data.get('channel', 0)))
        count = int(data.get('count', 0))
        if count <= 0 or count > 5000:
            return jsonify({'ok': False, 'error': 'count must be 1..5000'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

    try:
        import random
        for _ in range(count):
            arb_id = random.randint(0x100, 0x7FF)
            payload = [random.randint(0, 255) for _ in range(8)]
            manager.inject_frame(channel_id, arb_id, payload, flags=0, frame_type='CAN')
        return jsonify({'ok': True, 'count': count})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/bus/stats', methods=['GET'])
def bus_stats():
    """Return last bus stats snapshot (bus_load/errors/uptime).

    Mirrors the Socket.IO event 'bus_stats' but via HTTP for test automation.
    """
    try:
        stats = getattr(manager, 'last_stats', None)
        if not isinstance(stats, dict) or not stats:
            try:
                with manager.lock:
                    br = dict(getattr(manager, 'bitrate_by_channel', {}) or {})
            except Exception:
                br = {}
            try:
                stats = manager.diag.calculate_load(bitrate_by_channel=br)
                try:
                    manager.last_stats = stats
                except Exception:
                    pass
            except Exception:
                stats = {}
        return jsonify(stats if isinstance(stats, dict) else {})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/bus/stream_stats', methods=['GET'])
def bus_stream_stats():
    """Return live Socket.IO bus-stream fidelity statistics.

    Exposes how many frames were offered to the live/timeline streams versus
    how many the server actually emitted after batching and queue pressure.
    """
    try:
        if hasattr(manager, 'get_ui_stream_stats'):
            return jsonify(manager.get_ui_stream_stats())
        return jsonify({})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _bus_stats_loop():
    """Emit bus_stats periodically even if the bus loop isn't running.

    When CAN hardware can't be opened (or during injection-only tests), the
    normal bus loop won't run and stats would stay at 0. This loop computes
    Diagnostics load once per second, but only when manager.running is False
    to avoid double-counting with the real bus loop.
    """
    while True:
        try:
            if not bool(getattr(manager, 'running', False)):
                try:
                    with manager.lock:
                        br = dict(getattr(manager, 'bitrate_by_channel', {}) or {})
                except Exception:
                    br = {}
                stats = manager.diag.calculate_load(bitrate_by_channel=br)
                try:
                    manager.last_stats = stats
                except Exception:
                    pass
                try:
                    socketio.emit('bus_stats', stats)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1.0)


@app.route('/api/can/inject_batch', methods=['POST'])
def inject_can_batch():
    """Inject multiple synthetic CAN frames in one request (dev/test helper).

    Body examples:
      {"frames":[{"channel_id":0,"id":291,"data":[1,2,3,4,5,6,7,8]}, ...]}
      [{"channel":0,"id":291,"data":[1,2,3,4,5,6,7,8]}, ...]
    """
    data = request.json
    frames = None
    if isinstance(data, dict):
        frames = data.get('frames')
    elif isinstance(data, list):
        frames = data

    if not isinstance(frames, list) or not frames:
        return jsonify({'ok': False, 'error': 'frames must be a non-empty list'}), 400

    injected = 0
    errors: list[str] = []

    for item in frames:
        if not isinstance(item, dict):
            errors.append('frame must be an object')
            continue
        try:
            channel_id = int(item.get('channel_id', item.get('channel', 0)))
            arb_id = int(item.get('id', 0))
            payload = item.get('data')
            if not isinstance(payload, list):
                errors.append('data must be a list')
                continue
            payload = [int(x) & 0xFF for x in payload]
            manager.inject_frame(channel_id, arb_id, payload, flags=0, frame_type='CAN')
            injected += 1
        except Exception as e:
            errors.append(str(e))
            continue

    return jsonify({'ok': True, 'injected': injected, 'errors': errors[:5], 'error_count': len(errors)})


@app.route('/api/can/inject_batch_fast', methods=['POST'])
def inject_can_batch_fast():
    """High-throughput batch injector for stress testing.

    Like /api/can/inject_batch, but allows disabling decode and Socket.IO emission
    to reduce per-frame overhead.

    Body examples:
      {"frames":[...],"options":{"decode":false,"emit":false,"log":true,"diag":true}}
      [{...}, {...}]  (options default)
    """
    data = request.json
    frames = None
    options = {}
    if isinstance(data, dict):
        frames = data.get('frames')
        options = data.get('options') if isinstance(data.get('options'), dict) else {}
    elif isinstance(data, list):
        frames = data

    if not isinstance(frames, list) or not frames:
        return jsonify({'ok': False, 'error': 'frames must be a non-empty list'}), 400

    decode = bool(options.get('decode', False))
    emit = bool(options.get('emit', False))
    log = bool(options.get('log', True))
    diag = bool(options.get('diag', True))
    notify_listeners = bool(options.get('listeners', True))

    injected = 0
    errors: list[str] = []

    # Snapshot shared structures once per request
    try:
        with manager.lock:
            dbcs = dict(manager.dbcs)
            listeners_copy = manager.listeners[:]
    except Exception:
        dbcs = {}
        listeners_copy = []

    for item in frames:
        if not isinstance(item, dict):
            errors.append('frame must be an object')
            continue
        try:
            channel_id = int(item.get('channel_id', item.get('channel', 0)))
            arb_id = int(item.get('id', 0))
            payload = item.get('data')
            if not isinstance(payload, list):
                errors.append('data must be a list')
                continue
            payload = [int(x) & 0xFF for x in payload]
        except Exception as e:
            errors.append(str(e))
            continue

        frame = {
            'id': int(arb_id),
            'data': list(payload),
            'dlc': int(len(payload)),
            'flags': int(item.get('flags', 0) or 0),
            'timestamp': int(time.time() * 1000),
            'type': 'CAN',
            'channel': int(channel_id),
        }

        if decode:
            loaders = dbcs.get(channel_id)
            if loaders:
                try:
                    for loader in loaders:
                        decoded = loader.decode(frame['id'], frame['data'])
                        if decoded:
                            frame['decoded'] = decoded
                            break
                except Exception:
                    pass

        if diag:
            try:
                manager.diag.update(frame)
            except Exception:
                pass

        if log:
            try:
                manager.logger.log(frame)
            except Exception:
                pass

        # Notify listeners (optional; can still be useful for internal watchers)
        if notify_listeners and listeners_copy:
            for listener in listeners_copy:
                try:
                    listener(frame)
                except Exception:
                    pass

        if emit and str(os.getenv('KBSM_LIVE_TRAFFIC_ENABLE', '0')).strip().lower() in {'1', 'true', 'yes', 'on'}:
            try:
                manager.socketio.emit('bus_data', frame)
            except Exception:
                pass

        injected += 1

    return jsonify({'ok': True, 'injected': injected, 'errors': errors[:5], 'error_count': len(errors)})


@app.route('/api/debug/trigger/fire', methods=['POST'])
def debug_fire_trigger():
    """Debug-only helper to simulate camera triggers without hardware.

    Disabled by default. Enable with env var `KBSM_ALLOW_DEBUG=1`.

    Body examples:
      {"trigger":"yolo","present":true,"detections":[{"name":"person","cls":0,"conf":0.9}]}
      {"trigger":"motion","present":true}
      {"trigger":"custom","present":true}
    """
    allow = str(os.getenv('KBSM_ALLOW_DEBUG', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
    if not allow:
        return jsonify({'ok': False, 'error': 'debug disabled'}), 404

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    trig = str(data.get('trigger') or 'motion').strip().lower()
    if trig not in {'yolo', 'motion', 'custom'}:
        return jsonify({'ok': False, 'error': 'invalid trigger'}), 400

    details = {
        'timestamp_s': float(time.time()),
        'trigger': trig,
        'present': bool(data.get('present', True)),
    }
    if trig == 'yolo':
        det = data.get('detections')
        if isinstance(det, list):
            details['detections'] = det
        else:
            details['detections'] = [{'name': 'person', 'cls': 0, 'conf': 0.9}]

    try:
        _on_camera_trigger(details)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/debug/eth/inject', methods=['POST'])
def debug_eth_inject():
    """Debug-only helper to simulate Ethernet packets.

    Disabled by default. Enable with env var `KBSM_ALLOW_DEBUG=1`.

    Body example:
      {
        "interface":"lo",
        "src":"127.0.0.1",
        "dst":"127.0.0.1",
        "proto":17,
        "length":128,
        "summary":"SOME/IP test",
        "payload_hex":"deadbeef"
      }

    Logs to:
      - session CSV/TXT (as channel=ETH)
      - Ethernet MF4 (if mf4 logging is active)
    """
    allow = str(os.getenv('KBSM_ALLOW_DEBUG', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
    if not allow:
        return jsonify({'ok': False, 'error': 'debug disabled'}), 404

    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'payload must be an object'}), 400

    ts = float(time.time())
    interface = str(data.get('interface') or 'eth').strip() or 'eth'
    src = str(data.get('src') or '0.0.0.0')
    dst = str(data.get('dst') or '0.0.0.0')
    try:
        proto = int(data.get('proto') or 0)
    except Exception:
        proto = 0
    try:
        length = int(data.get('length') or 0)
    except Exception:
        length = 0
    summary = str(data.get('summary') or f'ETH:{interface} {src}->{dst} proto={proto} len={length}')
    payload_hex = str(data.get('payload_hex') or '')

    # MF4 Ethernet logger (if active)
    try:
        eth_manager.log_raw_eth(ts, src, dst, proto, length)
    except Exception:
        pass

    # Main logger + websocket (re-use existing formatting)
    try:
        eth_manager._emit_packet({
            'timestamp': ts,
            'summary': summary,
            'layers': 'IP/UDP' if proto == 17 else 'IP/TCP' if proto == 6 else 'IP',
            'length': length,
            'payload_hex': payload_hex,
        })
    except Exception:
        pass

    return jsonify({'ok': True})


@app.route('/api/camera/stream', methods=['GET'])
def camera_stream():
    # Ensure camera pipeline is running when a client requests the MJPEG stream.
    # This keeps the UI functional even when CAM_AUTOSTART=0 and no triggers are armed.
    try:
        _camera_manager.start()
    except Exception:
        pass
    return _camera_streamer.response()


@app.route('/api/video/status', methods=['GET'])
def video_status():
    return jsonify(_video_recorder.status())


@app.route('/api/video/file', methods=['GET'])
def video_file_stream():
    """Stream a session MP4 for in-browser playback (Range requests).

    Query:
      base=session_YYYYmmdd_HHMMSS
    """
    base = (request.args.get('base') or '').strip()
    if not base:
        return jsonify({'ok': False, 'error': 'base required'}), 400
    if '.' in base:
        base = base.split('.', 1)[0]
    if not base.startswith('session_'):
        return jsonify({'ok': False, 'error': 'invalid base'}), 400

    mp4_name = f"{base}.mp4"
    mp4_path = _find_log_file(mp4_name)
    if not mp4_path:
        return jsonify({'ok': False, 'error': 'mp4 not found'}), 404
    try:
        return send_file(
            mp4_path,
            mimetype='video/mp4',
            as_attachment=False,
            conditional=True,
            max_age=0,
            download_name=os.path.basename(mp4_path),
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/session/manifest', methods=['GET'])
def session_manifest():
    """Describe a session's artifacts and timeline sync metadata."""
    base = (request.args.get('base') or '').strip()
    if not base:
        return jsonify({'ok': False, 'error': 'base required'}), 400
    if '.' in base:
        base = base.split('.', 1)[0]
    if not base.startswith('session_'):
        return jsonify({'ok': False, 'error': 'invalid base'}), 400

    mf4_name = f"{base}.mf4"
    mp4_name = f"{base}.mp4"
    mf4_path = _find_log_file(mf4_name)
    mp4_path = _find_log_file(mp4_name)

    meta = _read_session_meta(base)
    video_start_s = None
    try:
        vms = meta.get('video_start_epoch_ms')
        if isinstance(vms, (int, float)):
            video_start_s = float(vms) / 1000.0
    except Exception:
        video_start_s = None

    mf4_t0_s = None
    mf4_t1_s = None
    if mf4_path:
        try:
            raw = _mf4_load_raw_table(mf4_path)
            if raw is not None:
                t_abs = raw[0]
                try:
                    import numpy as np
                    mf4_t0_s = float(np.min(t_abs)) if t_abs is not None and t_abs.size else None
                    mf4_t1_s = float(np.max(t_abs)) if t_abs is not None and t_abs.size else None
                except Exception:
                    try:
                        mf4_t0_s = float(t_abs[0])
                        mf4_t1_s = float(t_abs[-1])
                    except Exception:
                        mf4_t0_s = None
                        mf4_t1_s = None
        except Exception:
            pass

    sync_source = None
    try:
        if video_start_s is not None:
            sync_source = 'meta'
        elif mf4_t0_s is not None:
            sync_source = 'mf4_t0'
    except Exception:
        sync_source = None

    effective_start_s = None
    try:
        effective_start_s = video_start_s if video_start_s is not None else mf4_t0_s
    except Exception:
        effective_start_s = None

    return jsonify({
        'ok': True,
        'base': base,
        'files': {
            'mf4': mf4_name if mf4_path else None,
            'mp4': mp4_name if mp4_path else None,
            'meta': _session_meta_filename(base) if _find_session_meta_path(base) else None,
        },
        'video': {
            'start_epoch_s': video_start_s,
            'start_epoch_s_effective': effective_start_s,
            'sync_source': sync_source,
            'stop_epoch_s': (float(meta.get('video_stop_epoch_ms')) / 1000.0) if isinstance(meta.get('video_stop_epoch_ms'), (int, float)) else None,
        },
        'mf4': {
            't0_epoch_s': mf4_t0_s,
            't1_epoch_s': mf4_t1_s,
        },
        'events': meta.get('events') if isinstance(meta.get('events'), list) else [],
    })


# Bus start can block on hardware/driver; run it asynchronously to keep the API responsive.
_bus_start_lock = threading.Lock()
_bus_start_in_progress = False
_bus_start_last_error = None
_bus_start_last_result = None
_bus_start_last_ts = None


def _start_bus_worker(payload: dict) -> None:
    global _bus_start_in_progress, _bus_start_last_error, _bus_start_last_result, _bus_start_last_ts
    try:
        try:
            channels = payload.get('channels', []) if isinstance(payload, dict) else []
            manager.preload_dbcs(channels)
        except Exception:
            pass
        ok = bool(manager.start_bus(payload))
        with _bus_start_lock:
            _bus_start_last_result = ok
            if ok:
                _bus_start_last_error = None
            else:
                # Provide extra diagnostics for the UI/runtime endpoint.
                try:
                    cfg_channels = payload.get('channels', []) if isinstance(payload, dict) else []
                except Exception:
                    cfg_channels = []
                try:
                    # Best-effort: may fail if dependencies missing.
                    if hasattr(manager, 'list_interfaces'):
                        ifaces = manager.list_interfaces()
                    else:
                        ifaces = None
                except Exception:
                    ifaces = None
                _bus_start_last_error = f"start_bus returned False (channels={cfg_channels}, interfaces={ifaces})"
            _bus_start_last_ts = time.time()
    except Exception as e:
        with _bus_start_lock:
            _bus_start_last_result = False
            _bus_start_last_error = str(e)
            _bus_start_last_ts = time.time()
    finally:
        with _bus_start_lock:
            _bus_start_in_progress = False


def _kickoff_bus_start_async(payload: dict) -> bool:
    """Start the bus in a background thread (shared by API + autostart).

    Returns True if a start was queued, False if one is already in progress.
    """
    global _bus_start_in_progress
    with _bus_start_lock:
        if _bus_start_in_progress:
            return False
        _bus_start_in_progress = True

    threading.Thread(target=_start_bus_worker, args=(payload,), daemon=True).start()
    return True

@app.route('/api/start', methods=['POST'])
def start_bus():
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"status": "failed", "error": "payload must be an object"}), 400

    channels = data.get('channels', [])
    if not isinstance(channels, list):
        return jsonify({"status": "failed", "error": "channels must be a list"}), 400

    # Backward-compatible start payload support.
    # Older clients send: {can_channel: 0, bitrate: 500000, simulate_ecu: false}
    # BusManager.start_bus expects: {channels: [{id, bitrate, type, dbc}]}
    if not channels:
        try:
            ch_id = int(data.get('can_channel')) if data.get('can_channel') is not None else None
        except Exception:
            ch_id = None
        try:
            bitrate = int(data.get('bitrate')) if data.get('bitrate') is not None else None
        except Exception:
            bitrate = None

        if ch_id is not None:
            ch_obj = {'id': ch_id, 'type': 'CAN'}
            if bitrate is not None:
                ch_obj['bitrate'] = bitrate
            channels = [ch_obj]
            data['channels'] = channels

    try:
        logger_channels_patch = _logger_channels_from_start_payload(channels)
        if logger_channels_patch:
            config_store.update({'logger_channels': logger_channels_patch})
    except Exception:
        pass

    # Process/resolve DBC paths
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        dbc_names = []
        if isinstance(ch.get('dbc_names'), list):
            dbc_names = [str(x or '').strip() for x in ch.get('dbc_names')]
        else:
            dbc_name = str(ch.get('dbc_name') or '').strip()
            dbc_names = [dbc_name] if dbc_name else []

        # Sanitize (no paths) and drop empties
        dbc_names = [n for n in dbc_names if n and os.path.basename(n) == n]

        if dbc_names:
            dbc_paths = [os.path.join(UPLOAD_FOLDER_DBC, os.path.basename(n)) for n in dbc_names]
            ch['dbcs'] = dbc_paths
            # Backward compatibility: keep a single path too
            ch['dbc'] = dbc_paths[0]

    # Start in background to avoid request timeouts when hardware open blocks.
    queued = _kickoff_bus_start_async({'channels': channels, **{k: v for k, v in data.items() if k != 'channels'}})
    if not queued:
        # Preserve existing frontend behavior: return 'started' to keep UI usable.
        return jsonify({"status": "started", "async": True, "note": "start already in progress"})
    # Keep legacy contract for the frontend (it expects status==='started').
    return jsonify({"status": "started", "async": True})

@app.route('/api/stop', methods=['POST'])
def stop_bus():
    manager.stop_bus()
    return jsonify({"status": "stopped"})

@app.route('/api/log/start', methods=['POST'])
def start_log():
    global _log_started_by_trigger, _log_started_source
    global _manual_stop_latch
    # If a previous stop got stuck (or the process was interrupted), clear stale state
    # so a manual Start can't be permanently blocked by "stop_in_progress".
    try:
        global _acq_stop_in_progress, _acq_stop_thread, _acq_stop_lock, _acq_stop_started_ts
        if '_acq_stop_lock' not in globals():
            _acq_stop_lock = threading.Lock()
            _acq_stop_in_progress = False
            _acq_stop_thread = None
            _acq_stop_started_ts = None
        with _acq_stop_lock:
            if bool(_acq_stop_in_progress):
                alive = False
                try:
                    alive = bool(_acq_stop_thread and _acq_stop_thread.is_alive())
                except Exception:
                    alive = False
                age_s = 0.0
                try:
                    if _acq_stop_started_ts is not None:
                        age_s = float(time.time()) - float(_acq_stop_started_ts)
                except Exception:
                    age_s = 0.0
                # If the worker isn't alive anymore, or it has been "stopping" for a long time,
                # clear the latch and allow a new session.
                if (not alive) or (age_s >= 180.0):
                    _acq_stop_in_progress = False
                    _acq_stop_thread = None
                    _acq_stop_started_ts = None
    except Exception:
        pass
    data = request.json or {}
    cfg = config_store.get_config_only() or {}
    default_formats = cfg.get('formats_default')
    if not isinstance(default_formats, list) or not default_formats:
        default_formats = ['mf4']

    formats = data.get('formats')
    if isinstance(formats, list):
        formats = [str(x).strip() for x in formats if str(x).strip()]
    else:
        formats = [str(x).strip() for x in default_formats if str(x).strip()]

    if not formats:
        formats = ['mf4']

    # Manual start should begin a fresh capture window. Drop any pre-roll
    # backlog accumulated while idle to avoid old frames polluting a short
    # user-requested recording (e.g. a 5-second validation trace).
    try:
        preroll = getattr(shared_logger, '_preroll', None)
        if preroll is not None:
            preroll.clear()
    except Exception:
        pass

    _reset_display_last_saved_file()
    _ensure_eth_running_for_logging()
    _prepare_gateway_mirror_for_logging()
    manager.start_logging(formats)
    eth_manager.start_logging(formats)
    _log_started_by_trigger = False
    _log_started_source = 'manual'
    try:
        _recording_sync_event.set()
    except Exception:
        pass
    try:
        for k in list(_manual_stop_latch.keys()):
            _manual_stop_latch[k] = False
    except Exception:
        pass
    return jsonify({"status": "logging_started"})


@app.route('/api/acq/start', methods=['POST'])
def acq_start():
    """Unified acquisition start (single logger entry point)."""
    return start_log()


@app.route('/api/log/status', methods=['GET'])
def log_status():
    base = getattr(shared_logger, 'session_base_name', None)
    if base is None:
        base = getattr(shared_logger, 'base_name', None)
    formats = getattr(shared_logger, 'formats', None)
    try:
        mdf_len = len(getattr(shared_logger, 'mdf_buffer', []) or [])
    except Exception:
        mdf_len = None
    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}
    try:
        bus_channels = sorted(int(cid) for cid in getattr(manager, 'handlers', {}).keys())
    except Exception:
        bus_channels = []
    try:
        logger_cfg_ids = sorted(int(ch.get('id')) for ch in _bus_channels_from_logger_config(cfg) if ch.get('id') is not None)
    except Exception:
        logger_cfg_ids = []
    try:
        gm_cfg = _get_gateway_mirror_config()
    except Exception:
        gm_cfg = _gateway_mirror_defaults()
    return jsonify({
        'active': bool(getattr(shared_logger, 'active', False)),
        'stopping': bool(globals().get('_acq_stop_in_progress', False)),
        'base_name': base,
        'formats': formats,
        'mf4_buffer_len': mdf_len,
        'mf4_merge': {
            'in_progress': bool(getattr(shared_logger, '_mf4_merge_in_progress', False)),
            'base_name': getattr(shared_logger, '_mf4_merge_base_name', None),
            'error': getattr(shared_logger, '_mf4_merge_error', None),
            'started_ts': getattr(shared_logger, '_mf4_merge_started_ts', None),
            'finished_ts': getattr(shared_logger, '_mf4_merge_finished_ts', None),
        },
        'video': _video_recorder.status(),
        'started_source': _log_started_source,
        'kl15': {
            'enabled': bool(_kl15_monitor_cfg.get('enabled')),
            'detected': bool(_kl15_state.get('detected')),
            'recording': bool(_kl15_state.get('recording')),
            'last_value': _kl15_state.get('last_value'),
        },
        'inputs': {
            'bus_running': bool(getattr(manager, 'running', False)),
            'bus_channels': bus_channels,
            'logger_channels_config': logger_cfg_ids,
            'eth_running': bool(getattr(getattr(eth_manager, 'capture', None), 'running', False)),
            'eth_interface': str((getattr(eth_manager, 'config', {}) or {}).get('interface') or ''),
            'gateway_mirror_enabled': bool(gm_cfg.get('enabled')),
            'gateway_mirror_can': list(gm_cfg.get('can') or []),
            'gateway_mirror_virtual_channels': sorted(int(cid) for cid in _mirror_channel_map.keys()),
            'gateway_mirror_map': {str(int(cid)): int(phys) for cid, phys in _mirror_channel_map.items()},
        },
    })


@app.route('/api/display/status', methods=['GET'])
def api_display_status():
    base_name = getattr(shared_logger, 'session_base_name', None)
    if base_name is None:
        base_name = getattr(shared_logger, 'base_name', None)

    session_base = _session_base_from_pathlike(base_name)
    active = bool(getattr(shared_logger, 'active', False))
    start_time_s = float(getattr(shared_logger, 'start_time', 0.0) or 0.0)
    now_s = time.time()
    started_at_ms = int(start_time_s * 1000.0) if start_time_s > 0 else None
    uptime_s = max(0.0, now_s - start_time_s) if active and start_time_s > 0 else 0.0

    return jsonify({
        'ok': True,
        'recording': active,
        'started_at_ms': started_at_ms,
        'uptime_s': uptime_s,
        'session_base': session_base,
        'started_source': _log_started_source,
        'last_saved_file': _get_display_last_saved_file(session_base=session_base),
        'timestamp_ms': int(now_s * 1000.0),
    })


@app.route('/api/timeline/live_stream', methods=['GET', 'POST'])
def timeline_live_stream():
    if request.method == 'GET':
        return jsonify({'ok': True, 'enabled': bool(manager.is_timeline_live_enabled())})

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', False)) if isinstance(data, dict) else False
    manager.set_timeline_live_enabled(enabled)
    return jsonify({'ok': True, 'enabled': bool(manager.is_timeline_live_enabled())})


@app.route('/api/runtime/status', methods=['GET'])
def runtime_status():
    """Lightweight status endpoint for standalone validation."""
    try:
        bus_running = bool(getattr(manager, 'running', False))
    except Exception:
        bus_running = False
    try:
        bus_channels = sorted([int(x) for x in (getattr(manager, 'handlers', {}) or {}).keys()])
    except Exception:
        bus_channels = []

    try:
        dbc_channels = sorted([int(x) for x in (getattr(manager, 'dbcs', {}) or {}).keys()])
    except Exception:
        dbc_channels = []

    try:
        eth_running = bool(getattr(eth_manager, 'capture', None) or getattr(eth_manager, 'doip', None) or getattr(eth_manager, 'xcp', None))
    except Exception:
        eth_running = False
    try:
        eth_cfg = getattr(eth_manager, 'config', {}) or {}
        eth_interface = eth_cfg.get('interface')
    except Exception:
        eth_interface = None

    try:
        with _bus_start_lock:
            bus_starting = bool(_bus_start_in_progress)
            bus_start_last_error = _bus_start_last_error
            bus_start_last_result = _bus_start_last_result
            bus_start_last_ts = _bus_start_last_ts
    except Exception:
        bus_starting = False
        bus_start_last_error = None
        bus_start_last_result = None
        bus_start_last_ts = None

    return jsonify({
        'bus': {
            'running': bus_running,
            'channels': bus_channels,
            'dbc_channels': dbc_channels,
            'stream': manager.get_ui_stream_stats() if hasattr(manager, 'get_ui_stream_stats') else {},
            'starting': bus_starting,
            'last_start': {
                'ok': bus_start_last_result,
                'error': bus_start_last_error,
                'ts': bus_start_last_ts,
            },
        },
        'eth': {
            'running': eth_running,
            'interface': eth_interface,
            'stats': eth_manager.get_stats() if hasattr(eth_manager, 'get_stats') else {},
        },
        'acq': {
            'logging_active': bool(getattr(shared_logger, 'active', False)),
            'started_by_trigger': bool(_log_started_by_trigger),
            'started_source': _log_started_source,
        },
        'triggers': {
            'yolo_armed': bool(_yolo_trigger_armed),
            'can_armed': bool(_can_trigger_cfg.get('armed')),
            'eth_armed': bool(_eth_trigger_cfg.get('armed')),
        },
        'standalone': {
            'autostart_enabled': str(os.getenv('KBSM_AUTOSTART', '')).strip().lower() in {'1', 'true', 'yes', 'on'},
            'restore_armed_enabled': str(os.getenv('KBSM_RESTORE_ARMED', '')).strip().lower() in {'1', 'true', 'yes', 'on'},
            'debug_enabled': str(os.getenv('KBSM_ALLOW_DEBUG', '')).strip().lower() in {'1', 'true', 'yes', 'on'},
        },
    })

@app.route('/api/log/stop', methods=['POST'])
def stop_log():
    global _log_started_by_trigger, _log_started_source
    global _manual_stop_latch

    # Avoid blocking the HTTP request on slow queue draining or MF4 merge.
    global _acq_stop_in_progress, _acq_stop_thread, _acq_stop_lock, _acq_stop_started_ts, _acq_stop_last_error
    if '_acq_stop_lock' not in globals():
        _acq_stop_lock = threading.Lock()
        _acq_stop_in_progress = False
        _acq_stop_thread = None
        _acq_stop_started_ts = None
        _acq_stop_last_error = None

    with _acq_stop_lock:
        if bool(_acq_stop_in_progress):
            return jsonify({"status": "stop_in_progress"}), 202
        _acq_stop_in_progress = True
        _acq_stop_started_ts = float(time.time())
        _acq_stop_last_error = None

        def _worker():
            global _acq_stop_in_progress
            global _acq_stop_started_ts, _acq_stop_last_error
            global _log_started_by_trigger, _log_started_source
            global _manual_stop_latch
            try:
                try:
                    manager.stop_logging()
                except Exception:
                    pass
                try:
                    eth_manager.stop_logging()
                except Exception:
                    pass
            except Exception as e:
                try:
                    _acq_stop_last_error = str(e)
                except Exception:
                    pass
            finally:
                _log_started_by_trigger = False
                _log_started_source = None
                try:
                    _recording_sync_event.set()
                except Exception:
                    pass
                # Reset edge state so a re-arm can start cleanly.
                try:
                    _reset_yolo_edge_state()
                except Exception:
                    pass
                # Latch trigger auto-start until user re-arms.
                try:
                    for k in list(_manual_stop_latch.keys()):
                        _manual_stop_latch[k] = True
                except Exception:
                    pass
                _acq_stop_in_progress = False
                try:
                    _acq_stop_started_ts = None
                except Exception:
                    pass

        _acq_stop_thread = threading.Thread(target=_worker, daemon=True)
        _acq_stop_thread.start()

    return jsonify({"status": "stopping"}), 202


@app.route('/api/acq/stop', methods=['POST'])
def acq_stop():
    """Unified acquisition stop (single logger entry point)."""
    return stop_log()

@app.route('/api/upload_dbc', methods=['POST'])
def upload_dbc():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    path = os.path.join(UPLOAD_FOLDER_DBC, file.filename)
    file.save(path)
    manager.load_dbc(path)
    return jsonify({"status": "uploaded", "filename": file.filename})

@app.route('/api/upload_fibex', methods=['POST'])
def upload_fibex():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']

    filename = _safe_basename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    allowed_exts = ('.xml', '.fibex', '.arxml')
    if not filename.lower().endswith(allowed_exts):
        return jsonify({"error": "Unsupported file extension"}), 400

    # If the file is .arxml, redirect it to the ARXML (AUTOSAR) folder & parser
    if filename.lower().endswith('.arxml'):
        dest = os.path.join(UPLOAD_FOLDER_ARXML, filename)
        file.save(dest)
        try:
            from arxml_parser import load_catalog_from_directory
            load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
        except Exception as e:
            logging.warning('arxml auto-parse after fibex redirect: %s', e)
        return jsonify({"status": "uploaded", "filename": filename,
                        "note": "ARXML detected — moved to AUTOSAR catalogue"})

    path = os.path.join(UPLOAD_FOLDER_FIBEX, filename)
    file.save(path)
    manager.load_fibex(path)
    return jsonify({"status": "uploaded", "filename": filename})


# ── ARXML (AUTOSAR) upload & catalogue ──────────────────────────────────────

@app.route('/api/upload_arxml', methods=['POST'])
def upload_arxml():
    """Upload one or more .arxml files and re-parse the catalogue."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    uploaded = request.files.getlist('file')
    if not uploaded:
        return jsonify({'error': 'No selected file'}), 400

    ok_count = 0
    errors = []
    for f in uploaded:
        filename = _safe_basename(f.filename)
        if not filename:
            errors.append('Invalid filename')
            continue
        if not filename.lower().endswith('.arxml'):
            errors.append(f'{filename}: not .arxml')
            continue
        dest = os.path.join(UPLOAD_FOLDER_ARXML, filename)
        f.save(dest)
        ok_count += 1

    # Re-parse entire arxml directory to refresh the catalogue
    cat_summary = {}
    try:
        from arxml_parser import load_catalog_from_directory
        cat = load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
        cat_summary = cat.summary()
    except Exception as e:
        errors.append(f'Parse error: {e}')

    return jsonify({
        'status': 'uploaded',
        'uploaded_count': ok_count,
        'catalog': cat_summary,
        'errors': errors,
    })


@app.route('/api/arxml/catalog', methods=['GET'])
def arxml_get_catalog():
    """Return the current ARXML catalogue (summary or full)."""
    detail = request.args.get('detail', 'summary')
    try:
        from arxml_parser import get_active_catalog, load_catalog_from_directory
        cat = get_active_catalog()
        if cat is None:
            cat = load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
        if detail == 'full':
            return jsonify({'ok': True, 'catalog': cat.to_dict()})
        return jsonify({'ok': True, 'catalog': cat.summary()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/arxml/files', methods=['GET'])
def arxml_list_files():
    """List uploaded .arxml files."""
    try:
        from arxml_parser import list_arxml_files
        files = list_arxml_files(UPLOAD_FOLDER_ARXML)
        return jsonify({'ok': True, 'files': files})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/arxml/delete', methods=['POST'])
def arxml_delete_file():
    """Delete an uploaded .arxml file and re-parse."""
    data = request.json if isinstance(request.json, dict) else {}
    filename = _safe_basename(data.get('filename', ''))
    if not filename or not filename.lower().endswith('.arxml'):
        return jsonify({'ok': False, 'error': 'Invalid filename'}), 400

    path = os.path.join(UPLOAD_FOLDER_ARXML, filename)
    if os.path.isfile(path):
        os.remove(path)

    try:
        from arxml_parser import load_catalog_from_directory
        cat = load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
        return jsonify({'ok': True, 'catalog': cat.summary()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/arxml/reload', methods=['POST'])
def arxml_reload_catalog():
    """Force re-parse all .arxml files."""
    try:
        from arxml_parser import load_catalog_from_directory
        cat = load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
        return jsonify({'ok': True, 'catalog': cat.summary()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/projects/pdx/import', methods=['POST'])
def import_pdx_project():
    """Import a PDX project and return a parse report.

    Multipart form-data:
      field name: file
    """
    try:
        from werkzeug.utils import secure_filename
    except Exception:
        secure_filename = None

    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'missing file field'}), 400

    f = request.files.get('file')
    if f is None:
        return jsonify({'ok': False, 'error': 'missing file'}), 400

    orig = str(getattr(f, 'filename', '') or '').strip()
    if not orig:
        return jsonify({'ok': False, 'error': 'missing filename'}), 400

    name = secure_filename(orig) if secure_filename else orig
    name = str(name or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'invalid filename'}), 400
    if not name.lower().endswith('.pdx'):
        return jsonify({'ok': False, 'error': 'only .pdx files are allowed'}), 400

    dest_dir = os.path.realpath(UPLOAD_FOLDER_PDX)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass

    base, ext = os.path.splitext(name)
    dest_name = name
    dest_path = os.path.join(dest_dir, dest_name)
    if os.path.exists(dest_path):
        try:
            ts = time.strftime('%Y%m%d_%H%M%S')
        except Exception:
            ts = str(int(time.time()))
        dest_name = f"{base}_{ts}{ext}"
        dest_path = os.path.join(dest_dir, dest_name)

    try:
        f.save(dest_path)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to save file: {e}'}), 500

    try:
        report = analyze_pdx(dest_path, PdxAnalyzeOptions())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Cache report next to the uploaded PDX for fast reload.
    try:
        report_path = dest_path + '.report.json'
        with open(report_path, 'w', encoding='utf-8') as fp:
            json.dump(report, fp, indent=2, sort_keys=True)
    except Exception:
        pass

    # Build and cache a DTC translation index (used by VAG scan reports).
    try:
        # Exhaustive scan: aim for full DTC coverage.
        dtc_index = build_dtc_index_from_pdx(dest_path, max_files=None, max_seconds=120.0)
        dtc_path = dest_path + '.dtc_index.json'
        with open(dtc_path, 'w', encoding='utf-8') as fp:
            json.dump(dtc_index, fp, indent=2, sort_keys=True)
    except Exception:
        dtc_index = None

    # Best-effort: also cache DID lengths/names to decode freeze-frame / extended data.
    try:
        did_index = build_did_index_from_pdx(dest_path, max_files=None, max_seconds=120.0)
        did_path = dest_path + '.did_index.json'
        with open(did_path, 'w', encoding='utf-8') as fp:
            json.dump(did_index, fp, indent=2, sort_keys=True)
    except Exception:
        did_index = None

    # Persist a minimal summary as the current project.
    try:
        extracted = report.get('extracted') if isinstance(report, dict) else {}
        diag_layers = (extracted or {}).get('diag_layers') if isinstance(extracted, dict) else []
        protocols = (extracted or {}).get('protocols') if isinstance(extracted, dict) else []
        # Prefer the cached DTC index count (covers full PDX), fallback to heuristic report.
        dtc_count = None
        try:
            if isinstance(dtc_index, dict):
                dtc_count = int(dtc_index.get('dtc_count') or 0)
        except Exception:
            dtc_count = None
        if not dtc_count:
            dtcs = (extracted or {}).get('dtcs') if isinstance(extracted, dict) else []
            dtc_count = int(len(dtcs) if isinstance(dtcs, list) else 0)
        summary = {
            'kind': 'pdx',
            'filename': dest_name,
            'stored_under': 'projects/pdx',
            'imported_at_ms': int(time.time() * 1000),
            'diag_layers_count': int(len(diag_layers) if isinstance(diag_layers, list) else 0),
            'protocols_count': int(len(protocols) if isinstance(protocols, list) else 0),
            'dtcs_count': int(dtc_count or 0),
        }
        config_store.update({'project': summary})
    except Exception:
        pass

    return jsonify(report)


@app.route('/api/projects/pdx/list', methods=['GET'])
def list_pdx_projects():
    """List stored PDX files and current active project."""
    active = None
    try:
        cfg = config_store.get_config_only() or {}
        proj = cfg.get('project') if isinstance(cfg, dict) else None
        if isinstance(proj, dict) and proj.get('kind') == 'pdx':
            active = proj.get('filename')
    except Exception:
        active = None

    items = []
    try:
        for name in sorted(os.listdir(UPLOAD_FOLDER_PDX)):
            if not isinstance(name, str):
                continue
            if not name.lower().endswith('.pdx'):
                continue
            full = os.path.join(UPLOAD_FOLDER_PDX, name)
            if not os.path.isfile(full):
                continue

            stat = None
            try:
                stat = os.stat(full)
            except Exception:
                stat = None

            counts = None
            rpt = full + '.report.json'
            dtc_idx = full + '.dtc_index.json'
            if os.path.isfile(rpt):
                try:
                    with open(rpt, 'r', encoding='utf-8') as fp:
                        data = json.load(fp)
                    ext = data.get('extracted') if isinstance(data, dict) else None
                    if isinstance(ext, dict):
                        counts = {
                            'diag_layers': int(len(ext.get('diag_layers') or [])),
                            'protocols': int(len(ext.get('protocols') or [])),
                            'dtcs': int(len(ext.get('dtcs') or [])),
                        }
                except Exception:
                    counts = None

            # Prefer dtc_index count when available.
            if os.path.isfile(dtc_idx):
                try:
                    with open(dtc_idx, 'r', encoding='utf-8') as fp:
                        dti = json.load(fp)
                    if isinstance(dti, dict) and isinstance(dti.get('dtc_count'), int):
                        if counts is None:
                            counts = {}
                        counts['dtcs'] = int(dti.get('dtc_count') or 0)
                except Exception:
                    pass

            items.append({
                'filename': name,
                'size_bytes': int(getattr(stat, 'st_size', 0) or 0),
                'mtime_ms': int((getattr(stat, 'st_mtime', 0) or 0) * 1000),
                'has_cached_report': bool(os.path.isfile(rpt)),
                'counts': counts,
            })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True, 'active': active, 'items': items})


@app.route('/api/projects/pdx/select', methods=['POST'])
def select_pdx_project():
    """Select an existing PDX as the active project."""
    data = request.get_json(silent=True) or {}
    filename = data.get('filename')
    path = _pdx_path_for(str(filename or ''))
    if not path:
        return jsonify({'ok': False, 'error': 'invalid filename'}), 400
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'pdx not found'}), 404

    counts = {}
    rpt = _pdx_report_path_for(os.path.basename(path))
    if rpt and os.path.isfile(rpt):
        try:
            with open(rpt, 'r', encoding='utf-8') as fp:
                rep = json.load(fp)
            ext = rep.get('extracted') if isinstance(rep, dict) else None
            if isinstance(ext, dict):
                counts = {
                    'diag_layers_count': int(len(ext.get('diag_layers') or [])),
                    'protocols_count': int(len(ext.get('protocols') or [])),
                    'dtcs_count': int(len(ext.get('dtcs') or [])),
                }
        except Exception:
            counts = {}

    # Prefer full DTC index count when available.
    try:
        dtc_idx = path + '.dtc_index.json'
        if os.path.isfile(dtc_idx):
            with open(dtc_idx, 'r', encoding='utf-8') as fp:
                dti = json.load(fp)
            if isinstance(dti, dict) and dti.get('dtc_count') is not None:
                counts['dtcs_count'] = int(dti.get('dtc_count') or 0)
    except Exception:
        pass

    try:
        summary = {
            'kind': 'pdx',
            'filename': os.path.basename(path),
            'stored_under': 'projects/pdx',
            'selected_at_ms': int(time.time() * 1000),
            **counts,
        }
        config_store.update({'project': summary})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': True, 'active': os.path.basename(path)})


@app.route('/api/projects/pdx/report', methods=['GET'])
def get_pdx_report():
    """Return the analysis report for a stored PDX (cached or generated on demand)."""
    filename = request.args.get('filename', type=str) or ''
    path = _pdx_path_for(filename)
    if not path:
        return jsonify({'ok': False, 'error': 'invalid filename'}), 400
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'pdx not found'}), 404

    rpt = _pdx_report_path_for(os.path.basename(path))
    if rpt and os.path.isfile(rpt):
        try:
            with open(rpt, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                return jsonify(data)
        except Exception:
            pass

    try:
        report = analyze_pdx(path, PdxAnalyzeOptions())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        if rpt:
            with open(rpt, 'w', encoding='utf-8') as fp:
                json.dump(report, fp, indent=2, sort_keys=True)
    except Exception:
        pass

    return jsonify(report)


def _get_iface_ipv4_best_effort(ifname: str) -> str | None:
    """Best-effort IPv4 fetch for a Linux interface."""
    try:
        import fcntl  # Linux only
        s = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
        ifreq = struct.pack('256s', str(ifname)[:15].encode('utf-8'))
        res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
        return str(pysocket.inet_ntoa(res[20:24]))
    except Exception:
        return None


def _get_default_route_iface_best_effort() -> str | None:
    """Return the interface name used for the default route, if available."""
    try:
        with open('/proc/net/route', 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
        for line in lines[1:]:
            parts = [p for p in line.split() if p]
            if len(parts) < 2:
                continue
            iface, dest = parts[0], parts[1]
            if dest == '00000000':
                return str(iface)
    except Exception:
        pass
    return None


@app.route('/doip', methods=['GET'])
def doip_config_page():
    return render_template('doip_config.html')


@app.route('/xcp_can', methods=['GET'])
def xcp_can_page():
    return render_template('xcp_can.html')


@app.route('/api/doip/status', methods=['GET'])
def doip_status():
    # Purely informational; does not change OS settings.
    return jsonify({
        'status': 'ok',
        'interfaces': {
            'eth0': {'ipv4': _get_iface_ipv4_best_effort('eth0')},
            'wlan0': {'ipv4': _get_iface_ipv4_best_effort('wlan0')},
        },
        'default_route_iface': _get_default_route_iface_best_effort(),
    })


@app.route('/api/doip/config', methods=['GET', 'POST'])
def doip_config_api():
    if request.method == 'GET':
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''
        if sm not in {'simulation', 'real'}:
            sm = None

        # Always expose the *effective* interface. If system_mode is active it is authoritative.
        if sm == 'simulation':
            effective_iface = 'lo'
        elif sm == 'real':
            effective_iface = 'eth0'
        else:
            effective_iface = str(es.get('interface') or 'eth0').strip() or 'eth0'
        return jsonify({
            'status': 'ok',
            'system_mode': sm,
            'interface_locked': bool(sm in {'simulation', 'real'}),
            'doip': {
                'iface': effective_iface,
                'target_ip': str(es.get('target_ip') or ''),
                'doip_enabled': bool(es.get('doip_enabled', False)),
                'auto_discover': bool(es.get('doip_auto_discover', True)),
                'tester_logical_address': int(es.get('doip_tester_logical_address', 0x0E00) or 0x0E00),
                # Dedicated tester address for Live Data (DoIP), to allow running
                # concurrently with Sentinel MIL polling.
                'live_tester_logical_address': int(es.get('doip_live_tester_logical_address', 0x0E01) or 0x0E01),
            }
        })

    data = request.json or {}
    doip = data.get('doip') if isinstance(data.get('doip'), dict) else {}
    iface = str(doip.get('iface') or doip.get('interface') or 'eth0').strip() or 'eth0'
    target_ip = str(doip.get('target_ip') or '').strip()
    doip_enabled = bool(doip.get('doip_enabled', True))
    auto_discover = bool(doip.get('auto_discover', True))

    # Accept int or hex string (e.g. "0x0E00").
    tla = 0x0E00
    try:
        raw_tla = doip.get('tester_logical_address', 0x0E00)
        if isinstance(raw_tla, str):
            tla = int(raw_tla.strip(), 0)
        else:
            tla = int(raw_tla)
    except Exception:
        tla = 0x0E00

    # Dedicated tester logical address for DoIP Live (int or hex string).
    live_tla = 0x0E01
    try:
        raw_live_tla = doip.get('live_tester_logical_address', 0x0E01)
        if isinstance(raw_live_tla, str):
            live_tla = int(raw_live_tla.strip(), 0)
        else:
            live_tla = int(raw_live_tla)
    except Exception:
        live_tla = 0x0E01

    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}

    # Keep interface coherent with the selected system mode.
    try:
        sm = str(cfg.get('system_mode') or '').strip().lower()
    except Exception:
        sm = ''
    if sm == 'simulation':
        iface = 'lo'
    elif sm == 'real':
        iface = 'eth0'

    es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
    es = dict(es)
    es.update({
        'interface': iface,
        'target_ip': target_ip,
        'doip_enabled': bool(doip_enabled),
        'doip_auto_discover': bool(auto_discover),
        'doip_tester_logical_address': int(tla) & 0xFFFF,
        'doip_live_tester_logical_address': int(live_tla) & 0xFFFF,
    })

    try:
        config_store.update({'eth_settings': es})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

    return jsonify({'status': 'ok'})


@app.route('/api/doip/discover', methods=['POST'])
def doip_discover_api():
    data = request.json or {}
    iface = str(data.get('iface') or '').strip() or None
    timeout_s = data.get('timeout_s', 1.2)
    try:
        timeout_s = float(timeout_s)
    except Exception:
        timeout_s = 1.2

    # If iface is not provided, fall back to the effective config interface.
    if not iface:
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        try:
            sm = str(cfg.get('system_mode') or '').strip().lower()
        except Exception:
            sm = ''

        if sm == 'simulation':
            iface = 'lo'
        elif sm == 'real':
            iface = 'eth0'
        else:
            es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
            iface = str(es.get('interface') or '').strip() or None

    try:
        from vag_scanner import discover_doip_gateway_ip
        ip = discover_doip_gateway_ip(iface=iface, timeout_s=timeout_s)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

    return jsonify({'status': 'ok', 'gateway_ip': ip})

# --- ETHERNET ROUTES ---

@app.route('/api/eth/start', methods=['POST'])
def start_eth():
    config = request.json or {}
    if not isinstance(config, dict):
        return jsonify({'status': 'error', 'error': 'invalid config'}), 400

    # If DoIP is enabled and target IP is empty, follow DoIP Settings behavior:
    # - use saved config target_ip if present
    # - if still empty and auto-discover is enabled, run discovery (IPv6-first)
    try:
        doip_enabled = bool(config.get('doip_enabled'))
    except Exception:
        doip_enabled = False

    if doip_enabled:
        doip_ip = str(config.get('doip_ip') or '').strip()
        iface = str(config.get('interface') or '').strip()

        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}

        if not iface:
            iface = str(es.get('interface') or 'eth0').strip() or 'eth0'

        if not doip_ip:
            doip_ip = str(es.get('target_ip') or '').strip()

        if not doip_ip and bool(es.get('doip_auto_discover', True)):
            try:
                from vag_scanner import discover_doip_gateway_ip
                doip_ip = str(discover_doip_gateway_ip(iface=iface or None, timeout_s=1.2) or '').strip()
            except Exception:
                doip_ip = ''

        config['interface'] = iface
        config['doip_ip'] = doip_ip

    # Ensure interface is always set even when doip is disabled
    if 'interface' not in config or not config['interface']:
        try:
            cfg = config_store.get_config_only() or {}
        except Exception:
            cfg = {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        config['interface'] = str(es.get('interface') or 'eth0').strip() or 'eth0'

    eth_manager.start(config)
    return jsonify({"status": "eth_started"})

@app.route('/api/eth/stop', methods=['POST'])
def stop_eth():
    eth_manager.stop()
    return jsonify({"status": "eth_stopped"})

@app.route('/api/eth/status')
def eth_status():
    return jsonify(eth_manager.get_stats())

@app.route('/api/eth/doip/uds', methods=['POST'])
def send_doip_uds():
    data = request.json
    sid = int(data.get('sid'), 16)
    did = int(data.get('did', 0), 16)
    payload = bytes.fromhex(data.get('data', ''))
    # Optional: target logical address (ECU) for UDS over DoIP.
    # Accept both target_addr and target_address for compatibility.
    target = data.get('target_addr', None)
    if target is None:
        target = data.get('target_address', None)
    target_addr = None
    if target is not None and str(target).strip() != '':
        try:
            target_addr = int(str(target).strip(), 16)
        except Exception:
            try:
                target_addr = int(target)
            except Exception:
                target_addr = None

    eth_manager.send_uds(sid, did, payload, target_addr=target_addr)
    return jsonify({"status": "sent"})


@app.route('/api/gateway/mirror/definition', methods=['GET'])
def gateway_mirror_definition():
    """Return mirror definition from active PDX (when available)."""
    pdx_path = _get_active_pdx_path()
    if pdx_path and os.path.isfile(pdx_path):
        out = extract_gateway_mirror_definition_from_pdx(pdx_path)
        # Always provide a usable fallback definition.
        if not isinstance(out, dict) or not out.get('ok'):
            return jsonify({
                'ok': False,
                'active_pdx': os.path.basename(pdx_path),
                'error': (out.get('error') if isinstance(out, dict) else 'unknown'),
                'fallback': default_mirror_definition(),
            })
        return jsonify({
            'ok': True,
            'active_pdx': os.path.basename(pdx_path),
            'definition': out,
            'fallback': default_mirror_definition(),
        })

    return jsonify({
        'ok': False,
        'error': 'no active pdx selected',
        'fallback': default_mirror_definition(),
    })


def _parse_int_maybe_hex(v, *, default: int = 0) -> int:
    if v is None:
        return int(default)
    # Accept numeric types directly.
    try:
        if isinstance(v, bool):
            return int(default)
        if isinstance(v, (int, float)):
            return int(v)
    except Exception:
        pass

    s = str(v).strip()
    if not s:
        return int(default)

    # Be tolerant of UI strings like "0x0E80 (gateway)", "LA=0x0E80", or "0E80h".
    try:
        import re
        m = re.search(r"0x([0-9a-fA-F]{1,8})", s)
        if m:
            return int(m.group(1), 16)
        m = re.search(r"\b([0-9a-fA-F]{1,8})h\b", s)
        if m:
            return int(m.group(1), 16)
    except Exception:
        pass

    try:
        if s.lower().startswith('0x'):
            return int(s, 16)
        # If it's purely decimal digits, treat it as decimal.
        # (Important for ports like "35000" which would otherwise be incorrectly parsed as hex.)
        if s.isdigit():
            return int(s, 10)
        # allow hex without 0x only when it contains at least one hex letter
        if any(c in 'abcdefABCDEF' for c in s) and all(c in '0123456789abcdefABCDEF' for c in s) and len(s) <= 8:
            return int(s, 16)
        return int(s, 10)
    except Exception:
        return int(default)


def _effective_eth_iface_from_config(cfg: Dict[str, Any]) -> str | None:
    """Return the effective Ethernet interface based on system_mode.

    Keeps behavior consistent with DoIP settings and prevents stale eth_settings.interface.
    """
    if not isinstance(cfg, dict):
        return None
    try:
        sm = str(cfg.get('system_mode') or '').strip().lower()
    except Exception:
        sm = ''
    if sm == 'simulation':
        return 'lo'
    if sm == 'real':
        return 'eth0'
    es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
    try:
        iface = str((es or {}).get('interface') or '').strip()
    except Exception:
        iface = ''
    return iface or None


def _gateway_mirror_defaults() -> Dict[str, Any]:
    return {
        'enabled': False,
        'autostart': False,
        'auto_discover_ip': True,
        'gateway_ip': '',
        'target_addr': '',  # DoIP logical address, e.g. "0x0E80"
        'tester_logical_address': '0x0E00',
        'target_bus': 'ethernet',
        'dest_ip': '',
        'dest_port': 0,
        'can': [],
        'flexray': [],
        'lin': [],
    }


def _normalize_gateway_mirror_config(raw: Any) -> Dict[str, Any]:
    d = _gateway_mirror_defaults()
    if isinstance(raw, dict):
        d.update(raw)

    enabled = bool(d.get('enabled'))
    autostart = bool(d.get('autostart'))
    auto_discover_ip = bool(d.get('auto_discover_ip', True))

    gateway_ip = str(d.get('gateway_ip') or '').strip()
    target_addr_i = _parse_int_maybe_hex(d.get('target_addr') or d.get('target_address'), default=0) & 0xFFFF
    tester_la_i = _parse_int_maybe_hex(d.get('tester_logical_address'), default=0x0E00) & 0xFFFF
    target_bus = str(d.get('target_bus') or 'ethernet').strip() or 'ethernet'
    dest_ip = str(d.get('dest_ip') or '').strip()
    dest_port_i = _parse_int_maybe_hex(d.get('dest_port'), default=0) & 0xFFFF

    def _norm_list_int(xs, lo: int, hi: int):
        out = []
        if not isinstance(xs, list):
            return out
        for x in xs:
            try:
                xi = int(x)
            except Exception:
                continue
            if lo <= xi <= hi and xi not in out:
                out.append(xi)
        return out

    can_list = _norm_list_int(d.get('can'), 1, 8)
    lin_list = _norm_list_int(d.get('lin'), 1, 3)

    flexray_list = []
    if isinstance(d.get('flexray'), list):
        for x in d.get('flexray'):
            s = str(x or '').strip().upper()
            if s in {'A', 'B'} and s not in flexray_list:
                flexray_list.append(s)

    return {
        'enabled': enabled,
        'autostart': autostart,
        'auto_discover_ip': auto_discover_ip,
        'gateway_ip': gateway_ip,
        'target_addr': (f"0x{target_addr_i:04X}" if target_addr_i else ''),
        'tester_logical_address': f"0x{tester_la_i:04X}",
        'target_bus': target_bus,
        'dest_ip': dest_ip,
        'dest_port': int(dest_port_i),
        'can': can_list,
        'flexray': flexray_list,
        'lin': lin_list,
    }


def _get_gateway_mirror_config() -> Dict[str, Any]:
    try:
        cfg = config_store.get_config_only() or {}
    except Exception:
        cfg = {}
    raw = cfg.get('gateway_mirror') if isinstance(cfg, dict) else None
    return _normalize_gateway_mirror_config(raw)


def _resolve_mirror_did_from_active_pdx(default_did: int = 0x096F) -> int:
    did = int(default_did)
    pdx_path = _get_active_pdx_path()
    if pdx_path and os.path.isfile(pdx_path):
        try:
            d = extract_gateway_mirror_definition_from_pdx(pdx_path)
            if isinstance(d, dict) and d.get('ok'):
                did_s = ((d.get('dids') or {}).get('mirror_mode') if isinstance(d.get('dids'), dict) else None)
                if did_s:
                    did = _parse_int_maybe_hex(did_s, default=did)
        except Exception:
            pass
    return int(did)


def _resolve_gateway_ip_for_mirror(cfg: Dict[str, Any]) -> str:
    """Resolve gateway IP from mirror cfg, with optional best-effort discovery.

    If auto-discovery succeeds, persists gateway_ip into config.
    """
    cfg = _normalize_gateway_mirror_config(cfg)
    gateway_ip = str(cfg.get('gateway_ip') or '').strip()
    if gateway_ip:
        return gateway_ip

    if not bool(cfg.get('auto_discover_ip', True)):
        return ''

    try:
        from vag_scanner import discover_doip_gateway_ip
        app_cfg = config_store.get_config_only() or {}
        iface = _effective_eth_iface_from_config(app_cfg) or ''
        gateway_ip = str(discover_doip_gateway_ip(iface=iface or None, timeout_s=1.2) or '').strip()
    except Exception:
        gateway_ip = ''

    if gateway_ip:
        try:
            current = config_store.get_config_only() or {}
            gm = current.get('gateway_mirror') if isinstance(current.get('gateway_mirror'), dict) else {}
            gm = dict(gm)
            gm['gateway_ip'] = gateway_ip
            config_store.update({'gateway_mirror': gm})
        except Exception:
            pass
    return gateway_ip


def _discover_gateway_mirror_target_addr(*, gateway_ip: str, tester_addr: int, did: int, timeout_s: float = 0.18) -> int:
    """Best-effort discovery of the gateway ECU logical address supporting Mirror DID."""
    gateway_ip = str(gateway_ip or '').strip()
    if not gateway_ip:
        return 0
    tester_addr = int(tester_addr) & 0xFFFF
    did = int(did) & 0xFFFF

    # Try a small set of likely DIDs; 0x096F is the correct CalibData DID for MLBevo.
    did_candidates: list[int] = []
    for d in [did, 0x096F, 0x2A3C, 0x2A20]:
        dd = int(d) & 0xFFFF
        if dd and dd not in did_candidates:
            did_candidates.append(dd)

    candidates: list[int] = []
    # Common VAG gateway LA
    candidates.append(0x0E80)

    # If we have an active PDX, add its DoIP ECU list (fast + accurate).
    try:
        from vag_scanner import _load_active_pdx_comm_index  # type: ignore
        comm_index = _load_active_pdx_comm_index()
        if isinstance(comm_index, dict) and isinstance(comm_index.get('ecus'), list):
            for r in comm_index.get('ecus'):
                if not isinstance(r, dict):
                    continue
                doip = r.get('doip')
                if not isinstance(doip, dict):
                    continue
                la = doip.get('logical_ecu_address')
                if isinstance(la, int):
                    candidates.append(int(la) & 0xFFFF)
    except Exception:
        pass

    # Many VAG ECUs/entries show up in the 0x40xx range (seen in DoIP scans)
    candidates.extend(range(0x4000, 0x4100))
    # Legacy conservative range
    candidates.extend(range(0x0001, 0x0100))

    # De-dup while preserving order
    seen: set[int] = set()
    cand2: list[int] = []
    for c in candidates:
        cc = int(c) & 0xFFFF
        if cc and cc not in seen:
            seen.add(cc)
            cand2.append(cc)

    try:
        from vag_scanner import DoIPGatewayScanner
        from gateway_mirror import build_mirror_mode_write_request

        scanner = DoIPGatewayScanner(gateway_ip, tester_logical_address=tester_addr)
        try:
            scanner._connect()
            scanner._routing_activation()

            # Pre-build a safe probe write: target_bus=not_active (does not enable mirroring).
            probe_req_by_did: dict[int, bytes] = {}
            for dd in did_candidates:
                req = build_mirror_mode_write_request(
                    did=int(dd) & 0xFFFF,
                    target_bus='not_active',
                    can=[],
                    flexray=[],
                    lin=[],
                    dest_ip='::',
                    dest_port=0,
                )
                probe_req_by_did[int(dd) & 0xFFFF] = bytes([0x2E, (dd >> 8) & 0xFF, dd & 0xFF]) + (req.payload or b'')

            for la in cand2:
                # Many gateways require at least basic diag traffic and/or a non-default session
                tp = scanner._uds_transact(int(la), bytes([0x3E, 0x00]), timeout_s=float(timeout_s))
                if not tp:
                    continue
                # Try Extended session (best-effort). Ignore failures.
                try:
                    scanner._uds_transact(int(la), bytes([0x10, 0x03]), timeout_s=max(0.25, float(timeout_s)))
                except Exception:
                    pass

                # First try ReadDID (cheap) for any candidate DID.
                for dd in did_candidates:
                    did_hi = (int(dd) >> 8) & 0xFF
                    did_lo = int(dd) & 0xFF
                    resp = scanner._uds_transact(int(la), bytes([0x22, did_hi, did_lo]), timeout_s=max(0.25, float(timeout_s)))
                    if resp and len(resp) >= 3 and resp[0] == 0x62 and resp[1] == did_hi and resp[2] == did_lo:
                        return int(la) & 0xFFFF

                # Fallback: probe by safe write. Some ECUs block reads but allow writes in session.
                for dd, uds_write in probe_req_by_did.items():
                    resp2 = scanner._uds_transact(int(la), uds_write, timeout_s=max(0.35, float(timeout_s)))
                    if resp2 and len(resp2) >= 3 and resp2[0] == 0x6E:
                        return int(la) & 0xFFFF
        finally:
            try:
                scanner.close()
            except Exception:
                pass
    except Exception:
        return 0

    return 0


def _load_mirror_dbcs() -> int:
    """Load DBC files for mirror virtual channel IDs.

    For each mirror virtual channel (100+N, 200+N) we load the DBC of the
    matching physical CAN source.  For channel 99 (Iron Bird / Raw — no
    network-ID) we load **all** available CAN DBCs so any frame can be
    decoded regardless of its originating bus.

    Returns the number of virtual channels loaded.
    """
    try:
        _rebuild_mirror_channel_map()
    except Exception:
        pass

    if not _mirror_channel_map:
        return 0

    mappings: list = []
    seen_ch: set = set()

    try:
        sources = data_source_manager.list_sources() or []
    except Exception:
        sources = []

    # Build physical-channel → dbc_path and collect *all* CAN DBC paths.
    # Also build DBC filename → source_id map so inject_frame can
    # re-resolve source_id for mirror catch-all channel (99).
    phys_dbc: Dict[int, str] = {}
    all_can_dbcs: list = []
    dbc_fn_to_source: Dict[str, str] = {}
    for s in sources:
        try:
            if not isinstance(s, dict) or str(s.get('type', '')).upper() != 'CAN':
                continue
            cfg_s = s.get('config') or {}
            ch = int(cfg_s.get('channel_id'))
            dbc_name = str(s.get('dbc_name') or '').strip()
            source_id = str(s.get('id') or '').strip()
            if not dbc_name:
                continue
            dbc_path = os.path.join(UPLOAD_FOLDER_DBC, os.path.basename(dbc_name))
            if os.path.isfile(dbc_path):
                phys_dbc[ch] = dbc_path
                if dbc_path not in all_can_dbcs:
                    all_can_dbcs.append(dbc_path)
                # Map DBC basename → source_id for mirror re-resolution
                if source_id:
                    dbc_fn_to_source[os.path.basename(dbc_name)] = source_id
        except Exception:
            continue

    for virt_ch, phys_ch in _mirror_channel_map.items():
        if virt_ch in seen_ch:
            continue
        seen_ch.add(virt_ch)

        if virt_ch == 99:
            # Channel 99 (Iron Bird / Raw): load ALL CAN DBCs so any
            # arbitration ID can be decoded regardless of originating bus.
            for dp in all_can_dbcs:
                mappings.append({'id': 99, 'dbc': dp})
        else:
            dbc_path = phys_dbc.get(int(phys_ch))
            if dbc_path:
                mappings.append({'id': int(virt_ch), 'dbc': dbc_path})

    loaded = 0
    if mappings:
        try:
            loaded = manager.preload_dbcs(mappings)
            print(f"[MIRROR] Preloaded DBC for {loaded} virtual mirror channels: "
                  f"{[(m['id'], os.path.basename(m['dbc'])) for m in mappings]}", flush=True)
        except Exception as e:
            print(f"[MIRROR] DBC preload error: {e}", flush=True)

    # Expose DBC→source_id map on bus_manager so inject_frame can
    # re-resolve source_id for mirror catch-all channel 99.
    if dbc_fn_to_source:
        try:
            manager.mirror_dbc_source_map = dict(dbc_fn_to_source)
            print(f"[MIRROR] DBC→source map: {dbc_fn_to_source}", flush=True)
        except Exception:
            pass

    return loaded


def _gateway_mirror_send_from_config(enable: bool, cfg: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
    cfg = _normalize_gateway_mirror_config(cfg)

    gateway_ip = _resolve_gateway_ip_for_mirror(cfg)
    target_addr = _parse_int_maybe_hex(cfg.get('target_addr') or cfg.get('target_address') or cfg.get('gateway_target_addr') or cfg.get('gateway_la'), default=0)
    # VAG DoIP gateways commonly accept tester SA 0x0E00 (External Tester).
    # Avoid 0x0E80 default for tester if the target is also 0x0E80.
    tester_addr = _parse_int_maybe_hex(cfg.get('tester_logical_address'), default=0x0E00) & 0xFFFF
    
    if target_addr and tester_addr == target_addr:
        # Prevent SourceAddress == TargetAddress
        tester_addr = 0x0E00 if target_addr != 0x0E00 else 0x0E01
        
    dest_ip = str(cfg.get('dest_ip') or '').strip()
    dest_port = _parse_int_maybe_hex(cfg.get('dest_port'), default=0) & 0xFFFF

    if not gateway_ip:
        return {'ok': False, 'error': 'missing gateway_ip (set it in settings or enable auto-discovery)'}, 400
    did = _resolve_mirror_did_from_active_pdx(0x096F)
    if not target_addr:
        # Best-effort auto-discovery to make UI "Start Mirror" resilient.
        try:
            discovered = _discover_gateway_mirror_target_addr(gateway_ip=gateway_ip, tester_addr=tester_addr, did=did)
        except Exception:
            discovered = 0
        if discovered:
            target_addr = int(discovered) & 0xFFFF
            try:
                current = config_store.get_config_only() or {}
                gm = current.get('gateway_mirror') if isinstance(current.get('gateway_mirror'), dict) else {}
                gm = dict(gm)
                gm['gateway_ip'] = gateway_ip
                gm['target_addr'] = f"0x{int(target_addr) & 0xFFFF:04X}"
                gm['tester_logical_address'] = f"0x{int(tester_addr) & 0xFFFF:04X}"
                config_store.update({'gateway_mirror': gm})
            except Exception:
                pass
        else:
            return {'ok': False, 'error': 'missing target_addr (DoIP logical address of the gateway ECU)'}, 400

    if enable:
        # Auto-fill dest_ip with our own address on the effective interface.
        # This must match the interface that EthernetCapture is listening on,
        # otherwise the gateway sends mirror data to an address we never see.
        # We also re-detect if the current dest_ip is IPv4, because we prefer IPv6 Link-Local
        # which avoids ARP/Routing issues on the gateway side.
        if not dest_ip or (dest_ip and ':' not in dest_ip):
            try:
                import subprocess as _sp
                app_cfg = config_store.get_config_only() or {}
                _iface = _effective_eth_iface_from_config(app_cfg) or 'eth0'
                # Try IPv6 link-local first (preferred for automotive DoIP).
                _out = _sp.check_output(
                    ['ip', '-6', 'addr', 'show', 'dev', _iface, 'scope', 'link'],
                    text=True, timeout=3,
                )
                import re as _re
                _m = _re.search(r'inet6\s+([0-9a-f:]+)/', _out)
                if _m:
                    dest_ip = f"{_m.group(1)}%{_iface}"
                elif not dest_ip:
                    # Fallback to IPv4 address on the same interface ONLY if we had nothing.
                    _out4 = _sp.check_output(
                        ['ip', '-4', 'addr', 'show', 'dev', _iface],
                        text=True, timeout=3,
                    )
                    _m4 = _re.search(r'inet\s+([0-9.]+)/', _out4)
                    if _m4:
                        dest_ip = _m4.group(1)
            except Exception:
                pass
        # Auto-fill dest_port with a sensible default (standard mirror data port).
        if not dest_port:
            dest_port = 30490
        if not dest_ip or not dest_port:
            return {'ok': False, 'error': 'missing dest_ip or dest_port'}, 400
        # Persist auto-filled dest_ip/dest_port so the UI shows them.
        try:
            _cur = config_store.get_config_only() or {}
            _gm = _cur.get('gateway_mirror') if isinstance(_cur.get('gateway_mirror'), dict) else {}
            _gm = dict(_gm)
            _changed = False
            if dest_ip and not _gm.get('dest_ip'):
                _gm['dest_ip'] = dest_ip; _changed = True
            if dest_port and not _gm.get('dest_port'):
                _gm['dest_port'] = int(dest_port); _changed = True
            if _changed:
                config_store.update({'gateway_mirror': _gm})
        except Exception:
            pass
        # Keep the Ethernet capture mirror port aligned with what we told the
        # gateway so incoming mirror traffic is actually recognized.
        try:
            eth_manager.set_mirror_port(int(dest_port))
        except Exception:
            pass
    else:
        # For not_active, IP/port are typically ignored; keep a safe filler.
        if not dest_ip:
            dest_ip = '::'
        if not dest_port:
            dest_port = 0

    # Pre-build standard request (long format)
    req_long = build_mirror_mode_write_request(
        did=did,
        target_bus=(cfg.get('target_bus', 'ethernet') if enable else 'not_active'),
        can=(cfg.get('can') if enable else []),
        flexray=(cfg.get('flexray') if enable else []),
        lin=(cfg.get('lin') if enable else []),
        dest_ip=dest_ip,
        dest_port=int(dest_port),
    )
    
    # Pre-build short request (6-byte format)
    # [TargetBus(1)] [CAN(1)] [FR/LIN(1)] [Reserved(3)]
    # We reuse headers from req_long to extract masks
    _payload_long = req_long.payload # has 21 bytes
    if len(_payload_long) >= 3:
        # byte 0, 1, 2 match our hypothesis for short format
        _short_pl = _payload_long[0:3] + bytes([0, 0, 0])
        req_short = build_mirror_mode_write_request(
            did=did,
            target_bus='not_active', # dummy, we just replace payload
            dest_ip='::', dest_port=0
        )
        # Force payload
        object.__setattr__(req_short, 'payload', _short_pl) 
    else:
        req_short = req_long # fallback

    # Default to long
    req = req_long

    # ── [TRC Fix 2026-02-17] Pause Sentinel MIL DoIP polling to avoid
    #    tester-address collision on the gateway (both use 0x0E00).
    #    Pause lasts only the few seconds needed to write the Mirror DID. ──
    _sentinel_ref = None
    try:
        _sentinel_ref = experimental_assistant
    except Exception:
        pass
    if _sentinel_ref and hasattr(_sentinel_ref, 'pause_doip_mil'):
        try:
            _sentinel_ref.pause_doip_mil()
        except Exception:
            pass
        time.sleep(0.3)  # let gateway release old routing activation

    try:
        from vag_scanner import DoIPGatewayScanner

        # When debugging DoIP, route scanner logs into the backend log.
        emit_log = None
        try:
            if str(os.getenv('DOIP_DEBUG', '') or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on'):
                def _emit_doip(m: str) -> None:
                    # Always go to stdout (captured by nohup/systemd) so we can debug even
                    # if Flask logging handlers/filtering are configured unexpectedly.
                    try:
                        print(f"[doip] {m}", flush=True)
                    except Exception:
                        pass
                    try:
                        app.logger.info(f"[doip] {m}")
                    except Exception:
                        pass
                emit_log = _emit_doip
        except Exception:
            emit_log = None

        scanner = DoIPGatewayScanner(gateway_ip, emit_log=emit_log, tester_logical_address=tester_addr)
        try:
            scanner._connect()
            scanner._routing_activation()

            strategies = [
                (0x03, "Extended"),
                (0x40, "Developer"),
                (0x60, "EOL"),
                (0x01, "Default"),
            ]

            resp = None
            sa_done = False  # no generic SA on VAG MLBevo; kept for NRC-0x24 retry guard
            for sess_id, sess_name in strategies:
                print(f"Trying Session {sess_name} (0x{sess_id:02X})...")
                s_resp = scanner._uds_transact(int(target_addr), bytes([0x10, sess_id]), timeout_s=1.0)
                if not s_resp or s_resp[0] != 0x50:
                    # Some ECUs might be in default session and reject session transition if same.
                    # But usually 50 is returned. If silent, maybe we are not connected or wrong addr.
                    pass

                # Give ECU a moment after session transition.
                time.sleep(0.2)

                # -----------------------------------------------------------
                # [TRC Fix 2026-02-16] VAG SFD Authentication (RoutineControl
                # 0x0253).  On MLBevo gateways, WriteDID requires SFD unlock.
                # The ECU won't accept 0x27 (SA) — it uses the proprietary
                # SFD challenge/response mechanism via 0x0253.
                #
                # Discovered sequence from VAS/ODIS trace:
                #   1. WriteDID F198 (tester serial)   — accepted without auth
                #   2. WriteDID F199 (programming date) — accepted without auth
                #   3. RoutineControl 0x0253 (SFD challenge request)
                #      → ECU returns a challenge; we can't compute the response
                #        but simply initiating the challenge is enough to unlock
                #        WriteDID for low-security DIDs (0x0902, 0x096F).
                # -----------------------------------------------------------
                try:
                    # Step A: WriteDID F198 (tester serial — same bytes as VAS trace)
                    scanner._uds_transact(
                        int(target_addr),
                        bytes([0x2E, 0xF1, 0x98, 0x03, 0x86, 0xC2, 0x11, 0xB2, 0x07]),
                        timeout_s=1.0,
                    )
                    time.sleep(0.1)
                    # Step B: WriteDID F199 (programming date — BCD: YY MM DD)
                    scanner._uds_transact(
                        int(target_addr),
                        bytes([0x2E, 0xF1, 0x99, 0x26, 0x02, 0x16]),
                        timeout_s=1.0,
                    )
                    time.sleep(0.1)
                    # Step C: RoutineControl 0x0253 — SFD Challenge Request
                    sfd_resp = scanner._uds_transact(
                        int(target_addr),
                        bytes([0x31, 0x01, 0x02, 0x53, 0x01, 0x01]),
                        timeout_s=5.0,
                    )
                    if sfd_resp and sfd_resp[0] == 0x71:
                        print(f"SFD Challenge received ({len(sfd_resp)-5} bytes) — WriteDID should be unlocked")
                    elif sfd_resp and sfd_resp[0] == 0x7F:
                        print(f"SFD Challenge rejected: NRC 0x{sfd_resp[2]:02X}")
                    else:
                        print(f"SFD Challenge: unexpected response: {sfd_resp.hex() if sfd_resp else 'None'}")
                    time.sleep(0.3)
                except Exception as e:
                    print(f"Warning: SFD Authentication sequence failed: {e}")

                # ATTEMPT READ BEFORE WRITE
                # We check the length to decide which payload format to use
                # Always probe using the real DID (not dependent on which payload we later pick).
                curr_val = scanner._uds_transact(
                    int(target_addr),
                    bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]),
                    timeout_s=1.0,
                )
                
                # Default to long
                req_to_use = req_long
                if curr_val and len(curr_val) >= 3 and curr_val[0] == 0x62:
                    # RDBI response: [62] [DID_HI] [DID_LO] [DATA...]
                    data_len = len(curr_val) - 3
                    if data_len == 6:
                        print(f"Detected short mirror DID format (len={data_len}). Switching to 6-byte payload.")
                        req_to_use = req_short
                    elif data_len == 21:
                        print(f"Detected standard mirror DID format (len={data_len}). Using 21-byte payload.")
                        req_to_use = req_long
                    else:
                        print(f"Unknown DID length {data_len}, trying standard payload.")

                # Dev Messages (0x0902) — must be enabled before 0x096F.
                try:
                    _val0902 = scanner._uds_transact(int(target_addr), bytes([0x22, 0x09, 0x02]), timeout_s=0.5)
                    if _val0902 and len(_val0902) >= 4 and _val0902[0] == 0x62:
                        _curr_0902 = _val0902[3]
                        if _curr_0902 != 0x01:
                            print(f"[Mirror] Activating Dev Messages (0x0902=0x01) - was 0x{_curr_0902:02X}")
                            _wr0902 = scanner._uds_transact(int(target_addr), bytes([0x2E, 0x09, 0x02, 0x01]), timeout_s=0.5)
                            if _wr0902 and _wr0902[0] == 0x6E:
                                print("[Mirror] Dev Messages enabled OK")
                            elif _wr0902 and _wr0902[0] == 0x7F:
                                print(f"[Mirror] Dev Messages write failed: NRC 0x{_wr0902[2]:02X}")
                        else:
                            print("[Mirror] Dev Messages (0x0902) already enabled.")
                    else:
                        print("[Mirror] Forcing Dev Messages (0x0902) to 0x01 (blind)")
                        scanner._uds_transact(int(target_addr), bytes([0x2E, 0x09, 0x02, 0x01]), timeout_s=0.5)
                except Exception as e:
                    print(f"Warning: Failed to set Development Messages (0x0902): {e}")

                time.sleep(0.1)

                # Write DID
                did_to_use = int(req_to_use.did) & 0xFFFF
                resp = scanner._uds_transact(
                    int(target_addr),
                    bytes([0x2E, (did_to_use >> 8) & 0xFF, did_to_use & 0xFF]) + req_to_use.payload,
                    timeout_s=2.5,
                )
                
                if resp and resp[0] == 0x6E:
                    # Update req reference so we return the correct payload in JSON
                    req = req_to_use
                    break
                
                # Handling for specific failures:
                # Helpful console hint for common negative responses
                if resp and len(resp) >= 3 and resp[0] == 0x7F:
                    nrc = resp[2]
                    print(f"Mirror write negative response: svc=0x{resp[1]:02X} nrc=0x{nrc:02X} (raw={resp.hex()})")

                    # NRC 0x24 (requestSequenceError) — SA was required but our initial
                    # SA attempt may have failed or was skipped.  If SA unlocked after the
                    # first write attempt, retry the write once.
                    if nrc == 0x24 and sa_done:
                        time.sleep(0.1)
                        resp = scanner._uds_transact(
                            int(target_addr),
                            bytes([0x2E, (did_to_use >> 8) & 0xFF, did_to_use & 0xFF]) + req_to_use.payload,
                            timeout_s=2.5,
                        )
                        if resp and resp[0] == 0x6E:
                            req = req_to_use
                            break
                        if resp and len(resp) >= 3 and resp[0] == 0x7F:
                            print(f"Retry after SA still failed: nrc=0x{resp[2]:02X}")

                    # Auto-fallback: if RequestOutOfRange (0x31) and we have FlexRay enable, maybe try without FR?
                    if nrc == 0x31 and len(req_to_use.payload) > 3 and req_to_use.payload[2] != 0:
                         pass

                # If failed, loop continues to next strategy

                # Note: Switching sessions usually resets security, which is intended.

        finally:
            try:
                scanner.close()
            except Exception:
                pass

        ok = bool(resp) and len(resp) >= 3 and resp[0] == 0x6E

        # ------------------------------------------------------------------
        # Fallback: if write failed but ReadDID shows the mirror is already
        # active (target_bus != 0), treat it as "already configured" success.
        # This covers VAG gateways where the mirror DID is read-only at
        # runtime (NRC 0x24 in all sessions) but was configured at the
        # factory / EOL station and persists across power cycles.
        # ------------------------------------------------------------------
        mirror_already_active = False
        if not ok and enable and curr_val:
            try:
                if curr_val[0] == 0x62 and len(curr_val) >= 6:
                    existing_target_bus = curr_val[3]
                    if existing_target_bus != 0:
                        mirror_already_active = True
                        ok = True
                        print(f"Mirror DID write failed but ReadDID shows mirror "
                              f"already active (target_bus={existing_target_bus}). "
                              f"Treating as success.")
            except Exception:
                pass

        result: Dict[str, Any] = {
            'ok': ok,
            'enable': bool(enable),
            'gateway_ip': gateway_ip,
            'target_addr': f"0x{int(target_addr) & 0xFFFF:04X}",
            'tester_logical_address': f"0x{tester_addr:04X}",
            'did': f"0x{req.did:04X}",
            'payload_hex': req.payload.hex(),
            'response_hex': (resp.hex() if resp else ''),
        }
        if mirror_already_active:
            # Also read DID 0x2A00 (mirror enable flag) for diagnostic info.
            _mirror_enable_flag = None
            try:
                from vag_scanner import DoIPGatewayScanner as _Scanner2
                _sc2 = _Scanner2(gateway_ip, tester_logical_address=tester_addr)
                _sc2._connect()
                _sc2._routing_activation()
                _sc2._uds_transact(int(target_addr), bytes([0x10, 0x03]), timeout_s=0.8)
                _r2a00 = _sc2._uds_transact(int(target_addr), bytes([0x22, 0x2A, 0x00]), timeout_s=1.0)
                if _r2a00 and _r2a00[0] == 0x62 and len(_r2a00) >= 4:
                    _mirror_enable_flag = _r2a00[3]
                _sc2.close()
            except Exception:
                pass

            _note_parts = [
                'Mirror DID is read-only (write returned NRC 0x24) but '
                'the mirror is already configured per ReadDID (target_bus != 0).',
            ]
            if _mirror_enable_flag is not None:
                result['mirror_enable_flag'] = f"0x{_mirror_enable_flag:02X}"
                if _mirror_enable_flag == 0:
                    _note_parts.append(
                        'DID 0x2A00 (mirror enable flag) is 0x00 — mirror is '
                        'CONFIGURED but NOT ACTIVELY STREAMING. '
                        'The gateway requires FAZIT/Online Coding (ODIS) to '
                        'set the enable flag and start mirror data output.'
                    )
                else:
                    _note_parts.append(
                        f'DID 0x2A00 (mirror enable flag) is 0x{_mirror_enable_flag:02X} — '
                        'mirror should be actively streaming.'
                    )
            _note_parts.append(
                'Mirror traffic may arrive via DoIP TCP (port 13400) or '
                'UDP (port 30490) depending on the gateway firmware.'
            )
            result['note'] = ' '.join(_note_parts)
            if curr_val and len(curr_val) >= 6:
                result['current_did_data'] = curr_val[3:].hex()
        elif not ok and resp and len(resp) >= 3 and resp[0] == 0x7F:
            nrc_code = resp[2]
            nrc_names = {
                0x12: 'subFunctionNotSupported',
                0x13: 'incorrectMessageLengthOrInvalidFormat',
                0x14: 'responseTooLong',
                0x22: 'conditionsNotCorrect',
                0x24: 'requestSequenceError (Security Access required?)',
                0x25: 'noResponseFromSubnetComponent',
                0x31: 'requestOutOfRange',
                0x33: 'securityAccessDenied',
                0x35: 'invalidKey',
                0x36: 'exceededNumberOfAttempts',
                0x37: 'requiredTimeDelayNotExpired',
                0x70: 'uploadDownloadNotAccepted',
                0x72: 'generalProgrammingFailure',
                0x78: 'requestCorrectlyReceivedResponsePending',
            }
            result['error'] = f"UDS NRC 0x{nrc_code:02X}: {nrc_names.get(nrc_code, 'unknown')}"
        elif not ok:
            result['error'] = 'No positive response from ECU (empty or unexpected reply)'

        # ── When mirror is being enabled (ok=True), preload DBC files for
        #    the mirror virtual channel IDs so frames get decoded and
        #    ComparisonEngine / ViolationLogger can process them.
        if ok and enable:
            try:
                _load_mirror_dbcs()
            except Exception:
                pass

        return result, (200 if ok else 200)
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500
    finally:
        # ── [TRC Fix 2026-02-17] Resume Sentinel MIL polling. ──
        if _sentinel_ref and hasattr(_sentinel_ref, 'resume_doip_mil'):
            try:
                _sentinel_ref.resume_doip_mil()
            except Exception:
                pass


@app.route('/api/gateway/mirror/discover_target_addr', methods=['POST'])
def gateway_mirror_discover_target_addr():
    """Discover the gateway DoIP logical address by probing candidates for Mirror_mode DID support."""
    data = request.json if isinstance(request.json, dict) else {}
    base_cfg = _get_gateway_mirror_config()
    if isinstance(data, dict):
        # allow overrides from UI
        base_cfg.update(data)

    cfg = _normalize_gateway_mirror_config(base_cfg)
    gateway_ip = _resolve_gateway_ip_for_mirror(cfg)
    if not gateway_ip:
        return jsonify({'ok': False, 'error': 'missing gateway_ip (set it or enable auto-discovery)'}), 400

    tester_addr = _parse_int_maybe_hex(cfg.get('tester_logical_address'), default=0x0E00) & 0xFFFF
    did = _resolve_mirror_did_from_active_pdx(0x096F)

    try:
        found = _discover_gateway_mirror_target_addr(gateway_ip=gateway_ip, tester_addr=int(tester_addr), did=int(did), timeout_s=0.18)
        if not found:
            return jsonify({
                'ok': False,
                'gateway_ip': gateway_ip,
                'tester_logical_address': f"0x{tester_addr:04X}",
                'did': f"0x{int(did) & 0xFFFF:04X}",
                'error': 'no target address responded to ReadDID for mirror DID (try manual target_addr, ignition on, or security/session requirements)',
            }), 200

        target_addr = f"0x{int(found) & 0xFFFF:04X}"

        # Persist discovery result
        try:
            current = config_store.get_config_only() or {}
            gm = current.get('gateway_mirror') if isinstance(current.get('gateway_mirror'), dict) else {}
            gm = dict(gm)
            gm['gateway_ip'] = gateway_ip
            gm['target_addr'] = target_addr
            gm['tester_logical_address'] = f"0x{tester_addr:04X}"
            config_store.update({'gateway_mirror': gm})
        except Exception:
            pass

        return jsonify({
            'ok': True,
            'gateway_ip': gateway_ip,
            'tester_logical_address': f"0x{tester_addr:04X}",
            'did': f"0x{int(did) & 0xFFFF:04X}",
            'target_addr': target_addr,
            'candidates_tested': None,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/gateway/mirror/config', methods=['GET'])
def gateway_mirror_get_config():
    return jsonify({'ok': True, 'config': _get_gateway_mirror_config()})


@app.route('/api/gateway/mirror/config', methods=['POST'])
def gateway_mirror_set_config():
    data = request.json if isinstance(request.json, dict) else {}
    cfg_in = data.get('config') if isinstance(data.get('config'), dict) else data
    cfg_norm = _normalize_gateway_mirror_config(cfg_in)
    try:
        config_store.update({'gateway_mirror': cfg_norm})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    # Keep the Ethernet capture mirror port aligned with the new config
    try:
        _new_port = cfg_norm.get('dest_port')
        if _new_port:
            eth_manager.set_mirror_port(int(_new_port))
    except Exception:
        pass
    return jsonify({'ok': True, 'config': cfg_norm})


@app.route('/api/gateway/mirror/start', methods=['POST'])
def gateway_mirror_start():
    cfg = _get_gateway_mirror_config()
    # Allow POST body overrides (e.g. from UI or curl).
    data = request.get_json(silent=True) or {}
    data = data if isinstance(data, dict) else {}
    body_cfg = data.get('config') if isinstance(data.get('config'), dict) else data
    if isinstance(body_cfg, dict) and body_cfg:
        cfg.update({k: v for k, v in body_cfg.items() if v is not None and v != ''})
        cfg = _normalize_gateway_mirror_config(cfg)
    out, code = _gateway_mirror_send_from_config(True, cfg)
    # If auto-discovery found a target_addr, refresh the persisted config so UI
    # can pick it up on next load without the user having to manually save.
    if out.get('ok') and out.get('target_addr'):
        try:
            cur = config_store.get_config_only() or {}
            gm = cur.get('gateway_mirror') if isinstance(cur.get('gateway_mirror'), dict) else {}
            gm = dict(gm)
            changed = False
            for k in ('target_addr', 'gateway_ip', 'tester_logical_address'):
                if out.get(k) and str(out.get(k)) != str(gm.get(k, '')):
                    gm[k] = str(out[k])
                    changed = True
            if changed:
                config_store.update({'gateway_mirror': gm})
        except Exception:
            pass
    return jsonify(out), code


@app.route('/api/gateway/mirror/stop', methods=['POST'])
def gateway_mirror_stop():
    # ──────────────────────────────────────────────────────────────────
    # SAFETY: On VAG MLBevo gateways the mirror DID 0x096F is a
    # persistent adaptation parameter.  Writing target_bus=0 (stop)
    # deactivates the mirror, but the gateway does NOT resume
    # streaming after a subsequent WriteDID target_bus=2 — it
    # requires a KL15 power cycle (ignition off→on) to restart.
    # Because we cannot remotely cycle KL15, we MUST NOT send the
    # stop command.  The mirror is effectively always-on once
    # configured by ODIS/FAZIT.
    #
    # If force=true is passed in the request body, the stop command
    # is sent anyway (e.g., for maintenance before ignition-off).
    # ──────────────────────────────────────────────────────────────────
    data = request.get_json(silent=True) or {}
    data = data if isinstance(data, dict) else {}
    force = bool(data.get('force', False))
    if not force:
        return jsonify({
            'ok': False,
            'error': (
                'Mirror stop is disabled to prevent loss of streaming. '
                'The VAG gateway requires a KL15 power cycle to resume '
                'after stop. Pass {"force": true} to override.'
            ),
            'hint': 'Turn ignition off and on to restart the mirror after a forced stop.',
        }), 409

    cfg = _get_gateway_mirror_config()
    body_cfg = data.get('config') if isinstance(data.get('config'), dict) else data
    if isinstance(body_cfg, dict) and body_cfg:
        cfg.update({k: v for k, v in body_cfg.items() if v is not None and v != ''})
        cfg = _normalize_gateway_mirror_config(cfg)
    out, code = _gateway_mirror_send_from_config(False, cfg)
    out['warning'] = 'Mirror stopped. A KL15 power cycle (ignition off→on) is required to restart streaming.'
    return jsonify(out), code


@app.route('/api/gateway/mirror/enable', methods=['POST'])
def gateway_mirror_enable():
    """Enable gateway bus mirroring to an Ethernet destination via DoIP."""
    data = request.json if isinstance(request.json, dict) else {}

    gateway_ip = str(data.get('gateway_ip') or data.get('doip_ip') or '').strip()
    if not gateway_ip:
        # fallback: reuse configured DoIP IP, if present
        try:
            cfg = config_store.get_config_only() or {}
            es = cfg.get('eth_settings') if isinstance(cfg, dict) else None
            if isinstance(es, dict):
                gateway_ip = str(es.get('doip_ip') or es.get('target_ip') or '').strip()
        except Exception:
            gateway_ip = ''

    # Backward compatible endpoint: treat body as config override.
    merged = _get_gateway_mirror_config()
    if isinstance(data, dict):
        merged.update(data)
    out, code = _gateway_mirror_send_from_config(True, merged)
    return jsonify(out), code


@app.route('/api/gateway/mirror/disable', methods=['POST'])
def gateway_mirror_disable():
    """Disable mirroring by writing Mirror_mode with target_bus=not_active and all masks cleared."""
    data = request.json if isinstance(request.json, dict) else {}
    merged = _get_gateway_mirror_config()
    if isinstance(data, dict):
        merged.update(data)
    out, code = _gateway_mirror_send_from_config(False, merged)
    return jsonify(out), code

# Background Thread for Ethernet Stats
def eth_stats_loop():
    while True:
        stats = eth_manager.get_stats()
        socketio.emit('eth_stats', stats)
        time.sleep(1)


def _autostart_from_saved_config() -> None:
    """Standalone mode: start bus + ethernet using saved config.

    Controlled by env KBSM_AUTOSTART=1.
    """
    try:
        enabled = str(os.getenv('KBSM_AUTOSTART', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
        if not enabled:
            return

        cfg = config_store.get_config_only() or {}

        # Start CAN bus from saved channel rows
        channels = cfg.get('logger_channels') if isinstance(cfg.get('logger_channels'), list) else []
        bus_channels = []
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            try:
                cid = int(ch.get('id'))
            except Exception:
                continue
            try:
                bitrate = int(ch.get('bitrate') or 0)
            except Exception:
                bitrate = 0
            dbc_names = []
            if isinstance(ch.get('dbc_names'), list):
                dbc_names = [str(x or '').strip() for x in ch.get('dbc_names')]
            else:
                dbc_name = str(ch.get('dbc_name') or '').strip()
                dbc_names = [dbc_name] if dbc_name else []
            dbc_names = [n for n in dbc_names if n and os.path.basename(n) == n]

            dbc_paths = [os.path.join(UPLOAD_FOLDER_DBC, os.path.basename(n)) for n in dbc_names]
            bus_channels.append({
                'id': cid,
                'type': 'CAN',
                'bitrate': bitrate,
                # Keep legacy single name + add list.
                'dbc_name': (dbc_names[0] if dbc_names else ''),
                'dbc_names': dbc_names,
                # Resolve saved DBC name(s) to absolute paths so BusManager loads them.
                **({
                    'dbcs': dbc_paths,
                    'dbc': dbc_paths[0],
                } if dbc_paths else {}),
            })

        if bus_channels:
            try:
                _kickoff_bus_start_async({'channels': bus_channels})
                _log_event('autostart_bus', {'channels': bus_channels})
            except Exception as e:
                try:
                    _log_event('autostart_bus_failed', {'error': str(e)})
                except Exception:
                    pass

        # Start Ethernet capture from saved eth settings
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else None
        if es:
            # Resolve mirror port from gateway_mirror config for capture alignment.
            _gm_for_port = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else {}
            _mirror_port = _gm_for_port.get('dest_port') or None
            if _mirror_port:
                try:
                    eth_manager.set_mirror_port(int(_mirror_port))
                except Exception:
                    pass
            try:
                eth_manager.start({
                    'interface': str(es.get('interface') or 'lo').strip(),
                    'pcap_enabled': bool(es.get('pcap_enabled', True)),
                    'doip_enabled': bool(es.get('doip_enabled', False)),
                    'someip_enabled': bool(es.get('someip_enabled', False)),
                    'xcp_enabled': bool(es.get('xcp_enabled', False)),
                    'doip_ip': str(es.get('target_ip') or '127.0.0.1').strip(),
                    'xcp_ip': str(es.get('target_ip') or '127.0.0.1').strip(),
                    'xcp_port': 5555,
                    'mirror_port': _mirror_port,
                })
                _log_event('autostart_eth', dict(es))
            except Exception as e:
                try:
                    _log_event('autostart_eth_failed', {'error': str(e)})
                except Exception:
                    pass

        # Start Gateway Mirror (optional)
        try:
            gm = cfg.get('gateway_mirror') if isinstance(cfg.get('gateway_mirror'), dict) else None
            gm_cfg = _normalize_gateway_mirror_config(gm)
            if bool(gm_cfg.get('enabled')) and bool(gm_cfg.get('autostart')):
                out, _ = _gateway_mirror_send_from_config(True, gm_cfg)
                try:
                    _log_event('autostart_gateway_mirror', out)
                except Exception:
                    pass
        except Exception as e:
            try:
                _log_event('autostart_gateway_mirror_failed', {'error': str(e)})
            except Exception:
                pass
    except Exception:
        return

@app.route('/api/system/set_mode', methods=['POST'])
def system_set_mode():
    """Switch between Simulation (Iron Bird) and Real (Vehicle) modes."""
    data = request.json or {}
    mode = str(data.get('mode') or '').strip().lower()
    
    if mode not in {'simulation', 'real'}:
        return jsonify({'ok': False, 'error': 'Invalid mode. Use "simulation" or "real".'}), 400

    try:
        # Load current config
        cfg = config_store.get_config_only() or {}
        es = cfg.get('eth_settings') if isinstance(cfg.get('eth_settings'), dict) else {}
        es = dict(es)

        # Persist authoritative system mode
        cfg_mode = mode

        # Enforce Ethernet capture settings according to mode
        if cfg_mode == 'simulation':
            es['interface'] = 'lo'
            es['pcap_enabled'] = True
        else:
            es['interface'] = 'eth0'
            es['pcap_enabled'] = False

        # Enable/disable simulation CAN source for coherence
        sources = cfg.get('data_sources') if isinstance(cfg.get('data_sources'), list) else []
        new_sources = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            ss = dict(s)
            try:
                is_sim = (str(ss.get('id') or '') == 'src_sim_can') or (str(ss.get('dbc_name') or '') == 'simulation.dbc')
            except Exception:
                is_sim = False
            if is_sim:
                ss['enabled'] = (cfg_mode == 'simulation')
            new_sources.append(ss)

        config_store.update({'system_mode': cfg_mode, 'eth_settings': es, 'data_sources': new_sources})
        
        # Trigger restart in a separate thread to allow response to return
        def restart_server():
            time.sleep(1)
            print("Restarting server for mode switch...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
            
        threading.Thread(target=restart_server).start()

        return jsonify({'ok': True, 'mode': mode, 'message': 'Server restarting...'})
        
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _enforce_system_mode_patch(*, current_cfg: dict, patch: dict) -> dict:
    """Apply system_mode constraints to an incoming config patch.

    Keeps UI/config/runtime coherent by preventing accidental overrides of
    interface/pcap settings when a system mode is active.
    """
    if not isinstance(current_cfg, dict):
        current_cfg = {}
    if not isinstance(patch, dict):
        return patch

    mode = patch.get('system_mode')
    if mode is None:
        mode = current_cfg.get('system_mode')
    mode = str(mode or '').strip().lower()

    if mode not in {'simulation', 'real'}:
        return patch

    # Ensure system_mode stays persisted
    patch = dict(patch)
    patch['system_mode'] = mode

    # Enforce eth_settings keys
    es = patch.get('eth_settings')
    if not isinstance(es, dict):
        es = dict(current_cfg.get('eth_settings') or {}) if isinstance(current_cfg.get('eth_settings'), dict) else {}
    else:
        es = dict(es)

    if mode == 'simulation':
        es['interface'] = 'lo'
        es['pcap_enabled'] = True
    else:
        es['interface'] = 'eth0'
        es['pcap_enabled'] = False

    patch['eth_settings'] = es

    # Enforce simulation CAN source enablement when data_sources is being updated
    if isinstance(patch.get('data_sources'), list):
        out = []
        for s in patch.get('data_sources'):
            if not isinstance(s, dict):
                continue
            ss = dict(s)
            try:
                is_sim = (str(ss.get('id') or '') == 'src_sim_can') or (str(ss.get('dbc_name') or '') == 'simulation.dbc')
            except Exception:
                is_sim = False
            if is_sim:
                ss['enabled'] = (mode == 'simulation')
            out.append(ss)
        patch['data_sources'] = out

    return patch


# ═══════════════════════════════════════════════════════════════════════════
# XCP on CAN  — REST API
# ASAM XCP Part 2 (protocol) + Part 5 (CAN transport)
# ═══════════════════════════════════════════════════════════════════════════

# ── Configuration ────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/config', methods=['GET'])
def xcp_can_get_config():
    """Return the current XCP-on-CAN configuration."""
    cfg = normalize_xcp_can_config((config_store.get_config_only() or {}).get('xcp_can'))
    return jsonify({'ok': True, 'config': cfg})


@app.route('/api/xcp/can/config', methods=['POST'])
def xcp_can_set_config():
    """Save XCP-on-CAN configuration and recreate the client."""
    global _xcp_can_client
    body = request.get_json(force=True, silent=True) or {}
    existing = dict((config_store.get_config_only() or {}).get('xcp_can') or default_xcp_can_config())
    existing.update(body)
    existing = normalize_xcp_can_config(existing)
    config_store.update({'xcp_can': existing})
    # Destroy existing client so next request recreates with new config
    with _xcp_can_lock:
        if _xcp_can_client is not None:
            try:
                _xcp_can_client.destroy()
            except Exception:
                pass
            _xcp_can_client = None
    return jsonify({'ok': True, 'config': existing})


# ── Connection ────────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/connect', methods=['POST'])
def xcp_can_connect():
    """CONNECT command: open XCP session with slave ECU.

    If an SKB file is loaded and auto_unlock is not False in the request body,
    security access (GET_SEED → UNLOCK) is attempted automatically after
    connecting, so DAQ / measurement reads are available immediately.
    """
    body = request.get_json(force=True, silent=True) or {}
    mode = int(body.get('mode', 0x00))
    auto_unlock = body.get('auto_unlock', True)
    client = _get_xcp_can_client()
    if client is None:
        return jsonify({'ok': False, 'error': 'BusManager not ready'}), 503
    result = client.connect(mode=mode)
    if not result.get('ok'):
        return jsonify(result), 502

    # Auto-unlock when SKB is loaded
    unlock_result = None
    if auto_unlock:
        with _xcp_skb_lock:
            skb = _xcp_skb_result
        if skb and skb.is_valid and skb.raw_bytes:
            try:
                unlock_result = client.unlock_all_resources(skb.raw_bytes)
                result['unlock'] = unlock_result
            except Exception as exc:
                result['unlock'] = {'ok': False, 'error': str(exc)}

    return jsonify(result), 200


@app.route('/api/xcp/can/unlock', methods=['POST'])
def xcp_can_unlock():
    """Security access: GET_SEED → compute key from SKB → UNLOCK.

    Body (optional):
      { "resource": 0x04 }   — unlock a single resource (default: all)

    Requires an SKB file to be loaded via /api/xcp/can/skb/import.
    """
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    with _xcp_skb_lock:
        skb = _xcp_skb_result
    if not skb or not skb.is_valid or not skb.raw_bytes:
        return jsonify({'ok': False, 'error': 'No SKB file loaded. Upload one via /api/xcp/can/skb/import first.'}), 400

    body = request.get_json(force=True, silent=True) or {}
    resource = body.get('resource')

    if resource is not None:
        result = client.security_access(int(resource), skb.raw_bytes)
    else:
        result = client.unlock_all_resources(skb.raw_bytes)

    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/disconnect', methods=['POST'])
def xcp_can_disconnect():
    """DISCONNECT command: close XCP session."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True, 'message': 'no_client'})
    result = client.disconnect()
    return jsonify(result)


# ── Status & Info ─────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/status', methods=['GET'])
def xcp_can_status():
    """Return full XCP client status snapshot."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True, 'connected': False, 'message': 'not_initialised'})
    return jsonify(client.status())


@app.route('/api/xcp/can/ecu_status', methods=['POST'])
def xcp_can_ecu_status():
    """GET_STATUS: read current session state from slave ECU."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.get_status()
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/get_id', methods=['POST'])
def xcp_can_get_id():
    """GET_ID: request ECU identification string."""
    body = request.get_json(force=True, silent=True) or {}
    req_type = int(body.get('req_type', 0x01))
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.get_id(req_type=req_type)
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/comm_mode_info', methods=['POST'])
def xcp_can_comm_mode_info():
    """GET_COMM_MODE_INFO: read negotiated communication parameters from slave."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.get_comm_mode_info()
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/daq_processor_info', methods=['POST'])
def xcp_can_daq_processor_info():
    """GET_DAQ_PROCESSOR_INFO: query slave DAQ capabilities."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.get_daq_processor_info()
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/daq_event_info', methods=['POST'])
def xcp_can_daq_event_info():
    """GET_DAQ_EVENT_INFO for a specific event channel."""
    body = request.get_json(force=True, silent=True) or {}
    event_ch = int(body.get('event_channel', 0))
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.get_daq_event_info(event_ch)
    return jsonify(result), (200 if result.get('ok') else 502)


# ── Signal registry ───────────────────────────────────────────────────────────

@app.route('/api/xcp/can/signals', methods=['GET'])
def xcp_can_list_signals():
    """List all registered signals."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True, 'signals': []})
    return jsonify({'ok': True, 'signals': client.list_signals()})


@app.route('/api/xcp/can/signal/add', methods=['POST'])
def xcp_can_add_signal():
    """Add a signal definition to the registry.

    Body: { name, address, addr_ext, dtype, byte_order, unit, factor, offset,
            min, max, comment }
    """
    body = request.get_json(force=True, silent=True) or {}
    if not body.get('name') or body.get('address') is None:
        return jsonify({'ok': False, 'error': 'name and address are required'}), 400
    client = _get_xcp_can_client()
    if client is None:
        return jsonify({'ok': False, 'error': 'BusManager not ready'}), 503
    try:
        sig = client.add_signal(body)
        return jsonify({'ok': True, 'signal': sig.to_dict()})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@app.route('/api/xcp/can/signal/delete', methods=['POST'])
def xcp_can_remove_signal():
    """Remove a signal from the registry. Body: { name }"""
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    removed = client.remove_signal(name)
    return jsonify({'ok': True, 'removed': removed})


@app.route('/api/xcp/can/signals/default_gearbox', methods=['GET'])
def xcp_can_default_gearbox_signals():
    """Return the built-in gearbox signal template (replace addresses from your A2L)."""
    return jsonify({'ok': True, 'signals': DEFAULT_GEARBOX_SIGNALS})


@app.route('/api/xcp/can/signal_acq', methods=['GET'])
def xcp_can_get_signal_acq():
    """Return the persisted per-signal acquisition config (mode, raster, event, prescaler)."""
    with _xcp_signal_acq_lock:
        return jsonify({'ok': True, 'signal_acq': dict(_xcp_signal_acq)})


@app.route('/api/xcp/can/signal_acq/update', methods=['POST'])
def xcp_can_update_signal_acq():
    """Update acquisition config for one or more signals.

    Body: { "signals": { "<name>": { "event_channel": N, "prescaler": N, "mode": "daq"|"polling", "raster_ms": N }, ... } }
    """
    body = request.get_json(force=True, silent=True) or {}
    updates = body.get('signals')
    if not updates or not isinstance(updates, dict):
        return jsonify({'ok': False, 'error': 'signals dict required'}), 400
    with _xcp_signal_acq_lock:
        for name, cfg in updates.items():
            existing = _xcp_signal_acq.get(name, {})
            existing['event_channel'] = cfg.get('event_channel', existing.get('event_channel', 0))
            existing['prescaler']     = cfg.get('prescaler',     existing.get('prescaler', 1))
            existing['mode']          = cfg.get('mode',          existing.get('mode', 'daq'))
            existing['raster_ms']     = cfg.get('raster_ms',     existing.get('raster_ms', 0))
            _xcp_signal_acq[name] = existing

    # Auto-save the active project so changes persist across restarts
    _xcp_autosave_project()

    return jsonify({'ok': True, 'updated': len(updates)})


# ── A2L import ────────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/a2l/import', methods=['POST'])
def xcp_can_a2l_import():
    """Parse A2L text and register MEASUREMENT / CHARACTERISTIC signals.

    Accepts either:
      • multipart/form-data  with a file field named 'file'
      • JSON body            { 'text': '<raw a2l content>' }
    """
    client = _get_xcp_can_client()
    if client is None:
        return jsonify({'ok': False, 'error': 'BusManager not ready'}), 503

    a2l_text = ''
    if 'file' in request.files:
        try:
            a2l_text = request.files['file'].read().decode('utf-8', errors='replace')
        except Exception as exc:
            return jsonify({'ok': False, 'error': f'File read error: {exc}'}), 400
    else:
        body = request.get_json(force=True, silent=True) or {}
        a2l_text = str(body.get('text', '') or '')

    if not a2l_text.strip():
        return jsonify({'ok': False, 'error': 'No A2L content provided'}), 400

    result = client.import_a2l_signals(a2l_text)
    return jsonify(result)


# ── Memory access ─────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/upload', methods=['POST'])
def xcp_can_upload():
    """SHORT_UPLOAD (short) or UPLOAD (long) — read bytes from ECU address.

    Body: { address (hex str), length, addr_ext, mode }
    mode: 'short' (default, max ~6 bytes) | 'long' (SET_MTA + UPLOAD)
    """
    body = request.get_json(force=True, silent=True) or {}
    addr_raw = body.get('address', 0)
    try:
        address = int(str(addr_raw), 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid address'}), 400
    length   = int(body.get('length', 1))
    addr_ext = int(body.get('addr_ext', 0))
    mode     = str(body.get('mode', 'short')).lower().strip()

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    if mode == 'long':
        ok, data = client.upload(address, length, addr_ext)
    else:
        ok, data = client.short_upload(address, min(length, 6), addr_ext)

    if not ok or data is None:
        return jsonify({'ok': False, 'error': client._last_error or 'upload failed'}), 502

    return jsonify({'ok': True, 'data_hex': data.hex().upper(), 'length': len(data)})


@app.route('/api/xcp/can/download', methods=['POST'])
def xcp_can_download():
    """SHORT_DOWNLOAD (short) or DOWNLOAD (long) — write bytes to ECU.

    Body: { address (hex str), data_hex (hex string), addr_ext, mode }
    """
    body = request.get_json(force=True, silent=True) or {}
    addr_raw = body.get('address', 0)
    try:
        address = int(str(addr_raw), 0)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid address'}), 400

    data_hex = str(body.get('data_hex', '') or '').replace(' ', '')
    if not data_hex:
        return jsonify({'ok': False, 'error': 'data_hex required'}), 400
    try:
        data_bytes = bytes.fromhex(data_hex)
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid hex in data_hex'}), 400

    addr_ext = int(body.get('addr_ext', 0))
    mode     = str(body.get('mode', 'short')).lower().strip()

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    if mode == 'long' or len(data_bytes) > 5:
        ok = client.download(address, data_bytes, addr_ext)
    else:
        ok = client.short_download(address, data_bytes, addr_ext)

    if not ok:
        return jsonify({'ok': False, 'error': client._last_error or 'download failed'}), 502

    return jsonify({'ok': True, 'bytes_written': len(data_bytes)})


# ── Signal read / write ───────────────────────────────────────────────────────

@app.route('/api/xcp/can/signal/read', methods=['POST'])
def xcp_can_read_signal():
    """Read a registered signal value via SHORT_UPLOAD.

    Body: { name }
    """
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    with client._state_lock:
        sig = client._signals.get(name)
    if sig is None:
        return jsonify({'ok': False, 'error': f'Signal "{name}" not registered'}), 404

    ok, val = client.read_signal(sig)
    if not ok:
        return jsonify({'ok': False, 'error': client._last_error or 'read failed'}), 502

    return jsonify({'ok': True, 'name': name, 'value': val, 'unit': sig.unit})


@app.route('/api/xcp/can/signal/write', methods=['POST'])
def xcp_can_write_signal():
    """Write a calibration value to a registered signal via SHORT_DOWNLOAD.

    Body: { name, value }
    """
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get('name', '')).strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    if 'value' not in body:
        return jsonify({'ok': False, 'error': 'value required'}), 400

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    with client._state_lock:
        sig = client._signals.get(name)
    if sig is None:
        return jsonify({'ok': False, 'error': f'Signal "{name}" not registered'}), 404

    try:
        value = float(body['value'])
    except Exception:
        return jsonify({'ok': False, 'error': 'Invalid numeric value'}), 400

    ok = client.write_signal(sig, value)
    if not ok:
        return jsonify({'ok': False, 'error': client._last_error or 'write failed'}), 502

    return jsonify({'ok': True, 'name': name, 'value_written': value})


# ── DAQ ───────────────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/daq/setup', methods=['POST'])
def xcp_can_daq_setup():
    """Configure and allocate DAQ lists on the slave ECU.

    Body: list of DAQ list descriptors — see XcpCanClient.setup_daq() docstring.
    Example:
    [
      {
        "event_channel": 0,
        "prescaler": 1,
        "mode": 16,
        "signals": [
          {"name": "GearPosition", "address": "0x20002000", "dtype": "UBYTE"}
        ]
      }
    ]
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, list):
        body = (body or {}).get('daq_lists', [])
    if not isinstance(body, list):
        return jsonify({'ok': False, 'error': 'Expect a JSON array of DAQ list descriptors'}), 400

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    result = client.setup_daq(body)
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/daq/start', methods=['POST'])
def xcp_can_daq_start():
    """Start all selected DAQ lists (START_STOP_SYNCH 0x01)."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.start_daq()
    return jsonify(result), (200 if result.get('ok') else 502)


@app.route('/api/xcp/can/daq/stop', methods=['POST'])
def xcp_can_daq_stop():
    """Stop all DAQ lists (START_STOP_SYNCH 0x00)."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503
    result = client.stop_daq()
    return jsonify(result)


# ── Polling mode ──────────────────────────────────────────────────────────────

@app.route('/api/xcp/can/poll/start', methods=['POST'])
def xcp_can_poll_start():
    """Start background SHORT_UPLOAD polling (fallback when DAQ unavailable).

    Body: { signal_names: [...], interval_ms: 100 }
    """
    body        = request.get_json(force=True, silent=True) or {}
    names       = body.get('signal_names') or None
    interval_ms = int(body.get('interval_ms', 100))

    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': False, 'error': 'not_initialised'}), 503

    result = client.start_polling(signal_names=names, interval_ms=interval_ms)
    return jsonify(result), (200 if result.get('ok') else 409)


@app.route('/api/xcp/can/poll/stop', methods=['POST'])
def xcp_can_poll_stop():
    """Stop background polling."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True, 'message': 'no_client'})
    client.stop_polling()
    return jsonify({'ok': True})


# ── Measurement history ───────────────────────────────────────────────────────

@app.route('/api/xcp/can/measurements/<signal_name>', methods=['GET'])
def xcp_can_measurements(signal_name: str):
    """Return buffered measurement history for a signal.

    Query params: limit (default 500)
    """
    limit  = int(request.args.get('limit', 500))
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True, 'signal': signal_name, 'samples': []})
    samples = client.get_measurements(signal_name, limit=limit)
    return jsonify({'ok': True, 'signal': signal_name, 'samples': samples})


@app.route('/api/xcp/can/measurements/<signal_name>', methods=['DELETE'])
def xcp_can_measurements_clear(signal_name: str):
    """Clear measurement history for a specific signal."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True})
    client.clear_measurements(signal_name)
    return jsonify({'ok': True})


@app.route('/api/xcp/can/measurements', methods=['DELETE'])
def xcp_can_measurements_clear_all():
    """Clear all measurement history."""
    client = _get_xcp_can_client(autocreate=False)
    if client is None:
        return jsonify({'ok': True})
    client.clear_measurements()
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════════════
# XCP on CAN — File import (A2L / LAB / MAP / SYM / SBK)
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/xcp/can/a2l/parse', methods=['POST'])
def xcp_can_a2l_parse():
    """Upload and fully parse an A2L file. Returns structured data.

    Accepts multipart/form-data (field 'file') or JSON {text: ...}.
    Result is cached in _xcp_a2l_result for subsequent /browse and /events calls.

    Auto-applies detected XCP transport config (CAN IDs, byte order, max_cto/dto,
    CAN FD, extended IDs) to the running system configuration so that the user
    does not need to enter them manually.  Only non-None values are merged;
    already-configured values are overwritten ONLY if `auto_apply` != false.
    """
    global _xcp_a2l_result, _xcp_a2l_path, _xcp_can_client
    text = _read_upload_text(request, 'file')
    if text is None:
        return jsonify({'ok': False, 'error': 'No content provided (multipart file or JSON {text})'}), 400

    # Persist the uploaded A2L to disk so projects can reload it later
    a2l_filename = 'uploaded.a2l'
    if 'file' in request.files:
        raw_name = request.files['file'].filename or ''
        safe = re.sub(r'[^\w.\-]', '_', raw_name).strip('_')
        if safe.lower().endswith('.a2l'):
            a2l_filename = safe
    a2l_save_path = os.path.join(UPLOAD_FOLDER_A2L, a2l_filename)
    try:
        Path(a2l_save_path).write_text(text, encoding='utf-8')
    except Exception:
        pass  # non-critical; parse continues

    result = parse_a2l(text)
    with _xcp_a2l_lock:
        _xcp_a2l_result = result
        _xcp_a2l_path   = a2l_save_path

    # ── Auto-apply XCP config from A2L ────────────────────────────────
    auto_apply = True
    if request.content_type and 'json' in request.content_type:
        body = request.get_json(force=True, silent=True) or {}
        auto_apply = body.get('auto_apply', True)
    # Also honour query param: ?auto_apply=false
    if str(request.args.get('auto_apply', 'true')).lower() in ('false', '0', 'no'):
        auto_apply = False

    applied_config: Dict[str, Any] = {}
    if auto_apply and result.xcp_config:
        applied_config = result.xcp_config.to_app_config()
        if applied_config:
            # Merge into persistent app config
            existing_xcp = (config_store.get_config_only() or {}).get('xcp_can') or {}
            if not isinstance(existing_xcp, dict):
                existing_xcp = {}
            existing_xcp.update(applied_config)
            config_store.update({'xcp_can': existing_xcp})

            # Destroy current client so it gets recreated with new config
            with _xcp_can_lock:
                if _xcp_can_client is not None:
                    try:
                        _xcp_can_client.destroy()
                    except Exception:
                        pass
                    _xcp_can_client = None

    # Auto-save project so A2L path persists across restarts
    _xcp_autosave_project()

    return jsonify({
        'ok':             True,
        'summary':        result.to_summary(),
        'events':         [e.to_dict() for e in result.events],
        'xcp_config':     result.xcp_config.to_dict(),
        'applied_config': applied_config,
    })


@app.route('/api/xcp/can/a2l/measurements', methods=['GET'])
def xcp_can_a2l_measurements():
    """Browse MEASUREMENT objects from the last parsed A2L.

    Query params:
      q        full-text filter (name / description / unit)
      group    filter by group name
      type     MEASUREMENT | CHARACTERISTIC | ALL (default ALL)
      page     0-based page index (default 0)
      per_page page size (default 200, max 500)
    """
    with _xcp_a2l_lock:
        result = _xcp_a2l_result
    if result is None:
        return jsonify({'ok': False, 'error': 'No A2L loaded — POST /api/xcp/can/a2l/parse first'}), 404

    q        = (request.args.get('q', '') or '').lower().strip()
    grp      = (request.args.get('group', '') or '').strip()
    obj_type = (request.args.get('type', 'ALL') or 'ALL').upper().strip()
    page     = max(0, int(request.args.get('page', 0) or 0))
    per_page = max(1, min(500, int(request.args.get('per_page', 200) or 200)))

    pool = result.all_signals()
    if obj_type == 'MEASUREMENT':
        pool = result.measurements
    elif obj_type == 'CHARACTERISTIC':
        pool = result.characteristics

    if grp:
        pool = [s for s in pool if s.group == grp]
    if q:
        if '*' in q or '?' in q:
            # Wildcard / glob search: gac*pos  →  gac.*pos
            import fnmatch
            pat = fnmatch.translate('*' + q + '*')   # wraps with .* anchors
            _rx = re.compile(pat, re.IGNORECASE)
            pool = [s for s in pool
                    if _rx.search(s.name)
                    or _rx.search(s.description)
                    or _rx.search(s.unit)]
        else:
            pool = [s for s in pool
                    if q in s.name.lower()
                    or q in s.description.lower()
                    or q in s.unit.lower()]

    total  = len(pool)
    offset = page * per_page
    page_data = pool[offset: offset + per_page]

    return jsonify({
        'ok':       True,
        'total':    total,
        'page':     page,
        'per_page': per_page,
        'groups':   list(result.groups.keys()),
        'items':    [s.to_dict() for s in page_data],
    })


@app.route('/api/xcp/can/a2l/events', methods=['GET'])
def xcp_can_a2l_events():
    """Return event channels from the last parsed A2L."""
    with _xcp_a2l_lock:
        result = _xcp_a2l_result
    if result is None:
        return jsonify({'ok': True, 'events': []})
    return jsonify({'ok': True, 'events': [e.to_dict() for e in result.events]})


@app.route('/api/xcp/can/a2l/summary', methods=['GET'])
def xcp_can_a2l_summary():
    """Return summary of the last parsed A2L."""
    with _xcp_a2l_lock:
        result = _xcp_a2l_result
    if result is None:
        return jsonify({'ok': False, 'loaded': False})
    return jsonify({'ok': True, 'loaded': True, 'summary': result.to_summary()})


@app.route('/api/xcp/can/lab/import', methods=['POST'])
def xcp_can_lab_import():
    """Parse a Vector CANape .lab or VLConfig .glc file and register matching signals.

    If an A2L has been loaded, signals present in both A2L and LAB/GLC are
    auto-registered on the XcpCanClient **with acquisition configuration**
    (event_channel, prescaler, mode) derived from the LAB group names or GLC
    CcpXcpSignal blocks.
    If no A2L is loaded, signal names are returned for manual review.

    Auto-detection: if the uploaded content starts with ``<?xml`` or contains
    ``<CcpXcpSignal>``, it is treated as a Vector VLConfig .glc file;
    otherwise the classic text-based .lab parser is used.
    """
    text = _read_upload_text(request, 'file')
    if text is None:
        return jsonify({'ok': False, 'error': 'No content'}), 400

    # ── Auto-detect format ──────────────────────────────────────────────
    stripped = text.lstrip()
    is_glc = (
        stripped.startswith('<?xml')
        or '<CcpXcpSignal>' in text[:8000]
        or '<ConfigurationDataModel' in text[:2000]
    )
    lab = parse_glc(text) if is_glc else parse_lab(text)
    fmt = 'glc' if is_glc else 'lab'

    registered: List[str] = []
    not_in_a2l: List[str] = []
    # Per-signal acquisition config returned to the frontend
    signal_acq: Dict[str, Dict[str, Any]] = {}

    with _xcp_a2l_lock:
        a2l = _xcp_a2l_result

    if a2l is not None:
        # For .lab files, resolve group rasters → A2L event channels.
        # GLC files already carry explicit DaqEventId per signal — skip.
        if not is_glc:
            resolve_lab_events(lab, a2l.events)

        a2l_signals = filter_measurements_by_lab(a2l, lab)
        client = _get_xcp_can_client()
        if client:
            for s in a2l_signals:
                try:
                    client.add_signal({
                        'name':       s.name,
                        'address':    s.address,
                        'addr_ext':   s.addr_ext,
                        'dtype':      s.data_type,
                        'byte_order': s.byte_order,
                        'unit':       s.unit,
                        'factor':     s.factor,
                        'offset':     s.offset,
                        'min':        s.min_value,
                        'max':        s.max_value,
                        'comment':    s.description,
                    })
                    registered.append(s.name)
                    # Attach acquisition config from LAB for this signal
                    cfg = lab.signal_configs.get(s.name)
                    if cfg:
                        signal_acq[s.name] = {
                            'event_channel': cfg.event_channel,
                            'prescaler':     cfg.prescaler,
                            'mode':          cfg.mode,
                            'raster_ms':     cfg.raster_ms,
                            'group':         cfg.group,
                        }
                    else:
                        signal_acq[s.name] = {
                            'event_channel': 0,
                            'prescaler':     1,
                            'mode':          'daq',
                            'raster_ms':     0,
                            'group':         '',
                        }
                except Exception:
                    pass
        # Report names in LAB but not in A2L
        a2l_names = {s.name for s in a2l.all_signals()}
        not_in_a2l = [n for n in lab.signals if n not in a2l_names]
    else:
        not_in_a2l = lab.signals  # no A2L yet

    # Persist signal_acq in memory for project save
    if signal_acq:
        with _xcp_signal_acq_lock:
            _xcp_signal_acq.update(signal_acq)

    # Auto-save to disk so everything survives a restart
    _xcp_autosave_project()

    return jsonify({
        'ok':          True,
        'format':      fmt,
        'groups':      lab.groups,
        'signals':     lab.signals,
        'registered':  registered,
        'not_in_a2l':  not_in_a2l,
        'signal_acq':  signal_acq,
        'errors':      lab.errors,
    })


@app.route('/api/xcp/can/map/import', methods=['POST'])
def xcp_can_map_import():
    """Parse a linker .map file and register found symbols as XCP signals.

    Accepts multipart/form-data (field 'file') or JSON {text: ...}.
    Default dtype = FLOAT32_IEEE; set via query param ?dtype=ULONG etc.
    """
    text = _read_upload_text(request, 'file')
    if text is None:
        return jsonify({'ok': False, 'error': 'No content'}), 400

    symbols = parse_map_file(text)
    default_dtype = (request.args.get('dtype', '') or 'FLOAT32_IEEE').upper().strip()

    client = _get_xcp_can_client()
    registered: List[str] = []
    if client:
        for sym in symbols.symbols:
            try:
                client.add_signal({
                    'name':    sym.name,
                    'address': sym.address,
                    'dtype':   default_dtype,
                })
                registered.append(sym.name)
            except Exception:
                pass

    return jsonify({
        'ok':         True,
        'found':      len(symbols.symbols),
        'registered': len(registered),
        'errors':     symbols.errors,
        'symbols':    symbols.to_dict()['symbols'][:100],  # preview first 100
    })


@app.route('/api/xcp/can/skb/import', methods=['POST'])
def xcp_can_skb_import():
    """Upload a Vector Seed & Key Binary (.skb) file for XCP security access.

    The .skb file contains the algorithm to compute the key from a seed.
    It is stored in memory and referenced during GET_SEED / UNLOCK.
    Accepts multipart/form-data (field 'file').
    """
    global _xcp_skb_result, _xcp_skb_path
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'Multipart field "file" required'}), 400

    f = request.files['file']
    raw = f.read()
    filename = (f.filename or '')

    result = parse_skb_file(raw)
    if not result.is_valid:
        return jsonify({'ok': False, 'error': '; '.join(result.errors) or 'Invalid .skb file'}), 400

    # Persist to disk so projects can reload it
    skb_dir = os.path.join(UPLOAD_FOLDER_A2L, '..', 'skb')
    os.makedirs(skb_dir, exist_ok=True)
    safe_name = re.sub(r'[^\w.\-]', '_', filename).strip('_') or 'seed_key.skb'
    skb_save_path = os.path.join(skb_dir, safe_name)
    try:
        Path(skb_save_path).write_bytes(raw)
    except Exception:
        pass

    with _xcp_skb_lock:
        _xcp_skb_result = result
        _xcp_skb_path   = skb_save_path

    # Auto-save project so SKB path persists across restarts
    _xcp_autosave_project()

    return jsonify({
        'ok':               True,
        'filename':         filename,
        'file_size':        result.file_size,
        'header_signature': result.header_signature,
        'security_levels':  result.security_levels,
        'errors':           result.errors,
    })


@app.route('/api/xcp/can/sym/import', methods=['POST'])
def xcp_can_sym_import():
    """Parse a text symbol file (.sym / .map) and register as XCP signals.

    Accepts multipart/form-data (field 'file').
    Text files (.map, .sym) are parsed to extract symbol→address pairs.
    """
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'Multipart field "file" required'}), 400

    f = request.files['file']
    filename = (f.filename or '').lower()
    raw = f.read()
    text = raw.decode('utf-8', errors='replace')

    if filename.endswith('.sym'):
        symbols = parse_sym_file(text)
    else:
        symbols = parse_map_file(text)

    default_dtype = (request.args.get('dtype', '') or 'FLOAT32_IEEE').upper().strip()
    client = _get_xcp_can_client()
    registered: List[str] = []
    if client:
        for sym in symbols.symbols:
            try:
                client.add_signal({'name': sym.name, 'address': sym.address, 'dtype': default_dtype})
                registered.append(sym.name)
            except Exception:
                pass

    return jsonify({
        'ok':         True,
        'format':     'sym' if filename.endswith('.sym') else 'map',
        'found':      len(symbols.symbols),
        'registered': len(registered),
        'errors':     symbols.errors,
        'symbols':    symbols.to_dict()['symbols'][:100],
    })


# ── DAQ auto-build from browser selection ─────────────────────────────────────

@app.route('/api/xcp/can/daq/build_from_selection', methods=['POST'])
def xcp_can_daq_build_from_selection():
    """Build optimal DAQ list configuration from a user-selected signal list.

    Body:
    {
      "signals": [
        {
          "name": "GearPosition",
          "address": "0x20002000",
          "dtype": "UBYTE",
          "event_channel": 0,
          "prescaler": 1,
          ... (factor, offset, unit, byte_order)
        }, ...
      ],
      "max_dto": 8     (optional, overrides client config)
    }

    Returns the DAQ list descriptor array — can be passed directly to
    POST /api/xcp/can/daq/setup.
    """
    body = request.get_json(force=True, silent=True) or {}
    signals  = body.get('signals', [])
    if not signals:
        return jsonify({'ok': False, 'error': 'No signals provided'}), 400

    client   = _get_xcp_can_client(autocreate=False)
    max_dto  = int(body.get('max_dto', 0))
    if not max_dto:
        max_dto = client._max_dto if client else 8

    with _xcp_a2l_lock:
        a2l = _xcp_a2l_result

    events = a2l.events if a2l else []

    daq_lists = build_daq_lists_from_selection(signals, events, max_dto=max_dto)
    return jsonify({'ok': True, 'daq_lists': daq_lists})


# ── Project auto-save / auto-restore helpers ──────────────────────────────────

def _xcp_autosave_project() -> None:
    """Silently re-save the active project so runtime changes persist.

    This is called when signal_acq is modified via the UI (bulk or per-signal
    edits).  If no project was ever saved/loaded, use '_autosave' as name.
    """
    import json as _json
    try:
        proj_name = (config_store.get_config_only() or {}).get('xcp_can_last_project') or '_autosave'
        proj_dir  = os.path.join(LOG_FOLDER, 'xcp_can_projects')
        os.makedirs(proj_dir, exist_ok=True)

        client   = _get_xcp_can_client(autocreate=False)
        signals  = client.list_signals() if client else []
        cfg      = (config_store.get_config_only() or {}).get('xcp_can') or default_xcp_can_config()

        with _xcp_a2l_lock:
            a2l      = _xcp_a2l_result
            a2l_path = _xcp_a2l_path
        with _xcp_signal_acq_lock:
            signal_acq = dict(_xcp_signal_acq)
        with _xcp_skb_lock:
            skb_path = _xcp_skb_path

        project = {
            'name':        proj_name,
            'saved_at':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'config':      cfg,
            'signals':     signals,
            'signal_acq':  signal_acq,
            'skb_path':    skb_path,
            'a2l_summary': a2l.to_summary() if a2l else None,
            'a2l_path':    a2l_path,
        }
        out_path = os.path.join(proj_dir, f'{proj_name}.json')
        with open(out_path, 'w', encoding='utf-8') as fh:
            _json.dump(project, fh, indent=2, ensure_ascii=False)
        config_store.update({'xcp_can_last_project': proj_name})
    except Exception:
        pass   # best-effort — never break the caller


def _xcp_auto_restore_project() -> None:
    """On startup, auto-load the last active XCP CAN project.

    Called from _autostart_from_saved_config (daemon thread) so we have
    the full app context.  Restores signals, A2L, SKB, signal_acq — the
    same steps as the manual /api/xcp/can/project/load route.
    """
    import json as _json
    global _xcp_a2l_result, _xcp_a2l_path, _xcp_skb_result, _xcp_skb_path

    try:
        proj_name = (config_store.get_config_only() or {}).get('xcp_can_last_project')
        if not proj_name:
            return
        proj_dir = os.path.join(LOG_FOLDER, 'xcp_can_projects')
        out_path = os.path.join(proj_dir, f'{proj_name}.json')
        if not os.path.isfile(out_path):
            return
        project = _json.loads(Path(out_path).read_text(encoding='utf-8'))
    except Exception:
        return

    # Restore XCP config
    cfg = project.get('config')
    if isinstance(cfg, dict):
        config_store.update({'xcp_can': cfg})

    # Restore signals
    client = _get_xcp_can_client()
    if client:
        for s in project.get('signals', []):
            try:
                client.add_signal(s)
            except Exception:
                pass

    # Restore A2L
    a2l_path = project.get('a2l_path') or ''
    if a2l_path and os.path.isfile(a2l_path):
        try:
            a2l_text = Path(a2l_path).read_text(encoding='utf-8', errors='replace')
            result   = parse_a2l(a2l_text)
            with _xcp_a2l_lock:
                _xcp_a2l_result = result
                _xcp_a2l_path   = a2l_path
        except Exception:
            pass

    # Restore signal_acq
    proj_signal_acq = project.get('signal_acq') or {}
    if proj_signal_acq:
        with _xcp_signal_acq_lock:
            _xcp_signal_acq.clear()
            _xcp_signal_acq.update(proj_signal_acq)

    # Restore SKB
    proj_skb_path = project.get('skb_path') or ''
    if proj_skb_path and os.path.isfile(proj_skb_path):
        try:
            skb_raw = Path(proj_skb_path).read_bytes()
            skb_res = parse_skb_file(skb_raw)
            if skb_res.is_valid:
                with _xcp_skb_lock:
                    _xcp_skb_result = skb_res
                    _xcp_skb_path   = proj_skb_path
        except Exception:
            pass

    app.logger.info('[XCP] Auto-restored project "%s": %d signals, signal_acq=%d, a2l=%s, skb=%s',
                proj_name,
                len(project.get('signals', [])),
                len(proj_signal_acq),
                bool(a2l_path),
                bool(proj_skb_path and os.path.isfile(proj_skb_path)))


# ── Project save / load ────────────────────────────────────────────────────────

@app.route('/api/xcp/can/project/save', methods=['POST'])
def xcp_can_project_save():
    """Save the current XCP CAN session as a named project.

    Body: { "name": "my_project" }  (alphanumeric, used as filename)
    Saves to LOG_FOLDER/xcp_can_projects/<name>.json
    """
    import json as _json
    body = request.get_json(force=True, silent=True) or {}
    name = re.sub(r'[^\w.-]', '_', str(body.get('name', 'default'))).strip('_') or 'default'

    proj_dir = os.path.join(LOG_FOLDER, 'xcp_can_projects')
    os.makedirs(proj_dir, exist_ok=True)

    client = _get_xcp_can_client(autocreate=False)
    signals  = client.list_signals() if client else []
    cfg      = (config_store.get_config_only() or {}).get('xcp_can') or default_xcp_can_config()

    with _xcp_a2l_lock:
        a2l      = _xcp_a2l_result
        a2l_path = _xcp_a2l_path

    # Gather acquisition config & SKB path
    with _xcp_signal_acq_lock:
        signal_acq = dict(_xcp_signal_acq)
    with _xcp_skb_lock:
        skb_path = _xcp_skb_path

    project = {
        'name':        name,
        'saved_at':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'config':      cfg,
        'signals':     signals,
        'signal_acq':  signal_acq,
        'skb_path':    skb_path,
        'a2l_summary': a2l.to_summary() if a2l else None,
        'a2l_path':    a2l_path,
    }

    out_path = os.path.join(proj_dir, f'{name}.json')
    try:
        with open(out_path, 'w', encoding='utf-8') as fh:
            _json.dump(project, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    # Remember last active project name for auto-restore on restart
    config_store.update({'xcp_can_last_project': name})

    return jsonify({'ok': True, 'file': os.path.basename(out_path), 'signals': len(signals)})


@app.route('/api/xcp/can/project/list', methods=['GET'])
def xcp_can_project_list():
    """List saved XCP CAN projects."""
    import json as _json
    proj_dir = os.path.join(LOG_FOLDER, 'xcp_can_projects')
    os.makedirs(proj_dir, exist_ok=True)
    projects = []
    for p in sorted(Path(proj_dir).glob('*.json')):
        try:
            meta = _json.loads(p.read_text(encoding='utf-8'))
            projects.append({
                'name':     meta.get('name', p.stem),
                'saved_at': meta.get('saved_at', ''),
                'signals':  len(meta.get('signals', [])),
                'file':     p.name,
            })
        except Exception:
            pass
    return jsonify({'ok': True, 'projects': projects})


@app.route('/api/xcp/can/project/load', methods=['POST'])
def xcp_can_project_load():
    """Load a saved project — restores config and signal registry.

    Body: { "name": "my_project" }
    """
    global _xcp_can_client
    import json as _json
    body = request.get_json(force=True, silent=True) or {}
    name = re.sub(r'[^\w.-]', '_', str(body.get('name', ''))).strip('_')
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400

    proj_dir = os.path.join(LOG_FOLDER, 'xcp_can_projects')
    out_path = os.path.join(proj_dir, f'{name}.json')
    if not os.path.isfile(out_path):
        return jsonify({'ok': False, 'error': f'Project "{name}" not found'}), 404

    try:
        project = _json.loads(Path(out_path).read_text(encoding='utf-8'))
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    # Apply config
    cfg = project.get('config')
    if isinstance(cfg, dict):
        config_store.update({'xcp_can': cfg})
        with _xcp_can_lock:
            if _xcp_can_client is not None:
                try:
                    _xcp_can_client.destroy()
                except Exception:
                    pass
                _xcp_can_client = None

    # Restore signals
    client = _get_xcp_can_client()
    loaded = 0
    if client:
        for s in project.get('signals', []):
            try:
                client.add_signal(s)
                loaded += 1
            except Exception:
                pass

    # ── Re-parse A2L from disk so Signal Browser is populated ─────────
    global _xcp_a2l_result, _xcp_a2l_path
    a2l_reloaded = False
    a2l_summary  = project.get('a2l_summary')  # fallback static summary
    a2l_events:  list = []
    a2l_path = project.get('a2l_path') or ''
    if a2l_path and os.path.isfile(a2l_path):
        try:
            a2l_text = Path(a2l_path).read_text(encoding='utf-8', errors='replace')
            result   = parse_a2l(a2l_text)
            with _xcp_a2l_lock:
                _xcp_a2l_result = result
                _xcp_a2l_path   = a2l_path
            a2l_reloaded = True
            a2l_summary  = result.to_summary()
            a2l_events   = [e.to_dict() for e in result.events]
        except Exception:
            pass

    # ── Restore signal_acq ────────────────────────────────────────
    proj_signal_acq = project.get('signal_acq') or {}
    if proj_signal_acq:
        with _xcp_signal_acq_lock:
            _xcp_signal_acq.clear()
            _xcp_signal_acq.update(proj_signal_acq)

    # ── Restore SKB ───────────────────────────────────────────────────
    global _xcp_skb_result, _xcp_skb_path
    skb_loaded = False
    proj_skb_path = project.get('skb_path') or ''
    if proj_skb_path and os.path.isfile(proj_skb_path):
        try:
            skb_raw = Path(proj_skb_path).read_bytes()
            skb_res = parse_skb_file(skb_raw)
            if skb_res.is_valid:
                with _xcp_skb_lock:
                    _xcp_skb_result = skb_res
                    _xcp_skb_path   = proj_skb_path
                skb_loaded = True
        except Exception:
            pass

    resp: Dict[str, Any] = {
        'ok': True, 'signals_loaded': loaded, 'name': name,
        'a2l_reloaded': a2l_reloaded,
        'signal_acq':   proj_signal_acq,
        'skb_loaded':   skb_loaded,
        'skb_path':     proj_skb_path if skb_loaded else '',
    }
    if a2l_summary:
        resp['a2l_summary'] = a2l_summary
    if a2l_events:
        resp['a2l_events'] = a2l_events

    # Remember last active project name for auto-restore on restart
    config_store.update({'xcp_can_last_project': name})

    return jsonify(resp)


@app.route('/api/xcp/can/project/delete', methods=['POST'])
def xcp_can_project_delete():
    """Delete a saved project. Body: { "name": "my_project" }"""
    body = request.get_json(force=True, silent=True) or {}
    name = re.sub(r'[^\w.-]', '_', str(body.get('name', ''))).strip('_')
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    out_path = os.path.join(LOG_FOLDER, 'xcp_can_projects', f'{name}.json')
    if not os.path.isfile(out_path):
        return jsonify({'ok': False, 'error': 'not found'}), 404
    try:
        os.remove(out_path)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    return jsonify({'ok': True})


# ── Utility used by file-import routes ────────────────────────────────────────

def _read_upload_text(req: Any, field: str) -> Optional[str]:
    """Extract text content from either multipart upload or JSON body."""
    if field in req.files:
        try:
            return req.files[field].read().decode('utf-8', errors='replace')
        except Exception:
            return None
    body = req.get_json(force=True, silent=True) or {}
    text = body.get('text') or body.get('content')
    return str(text) if text else None



# ────────────────────────────────────────────────────────────────
#  TRC Server Connection – read / write heartbeat config
# ────────────────────────────────────────────────────────────────
_TRC_HB_CFG_PATH = os.environ.get('TRC_HB_CONFIG', '/etc/trc_heartbeat/config.json')

@app.route('/api/trc_server_config', methods=['GET', 'POST'])
def api_trc_server_config():
    """Read or update the TRC-heartbeat configuration file."""
    if request.method == 'GET':
        try:
            with open(_TRC_HB_CFG_PATH, 'r') as f:
                return jsonify(json.load(f))
        except FileNotFoundError:
            # Return empty defaults so the frontend can populate the form
            return jsonify({
                'trc_server_url': '', 'vpn_gateway': '',
                'ping_interval_s': 30, 'heartbeat_interval_s': 30,
                'node_name': '', 'auth_token': ''
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # POST – partial update
    patch = request.get_json(force=True, silent=True) or {}
    if not isinstance(patch, dict):
        return jsonify({'ok': False, 'error': 'body must be a JSON object'}), 400

    ALLOWED = {'trc_server_url', 'vpn_gateway', 'ping_interval_s',
               'heartbeat_interval_s', 'node_name', 'auth_token'}
    filtered = {k: v for k, v in patch.items() if k in ALLOWED}
    if not filtered:
        return jsonify({'ok': False, 'error': 'no valid keys'}), 400

    try:
        with open(_TRC_HB_CFG_PATH, 'r') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    cfg.update(filtered)

    os.makedirs(os.path.dirname(_TRC_HB_CFG_PATH), exist_ok=True)
    tmp = _TRC_HB_CFG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, _TRC_HB_CFG_PATH)

    # Restart the heartbeat service so changes take effect
    try:
        import subprocess
        subprocess.Popen(['sudo', 'systemctl', 'restart', 'trc-heartbeat.service'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    return jsonify({'ok': True, 'config': cfg})


@app.route('/api/trc_server_config/test', methods=['POST'])
def api_trc_server_config_test():
    """Quick connectivity test to the configured TRC Server."""
    import urllib.request, urllib.error
    # Accept URL from request body (form input) or fall back to saved config
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get('trc_server_url') or '').strip().rstrip('/')
    if not url:
        try:
            with open(_TRC_HB_CFG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        url = (cfg.get('trc_server_url') or '').rstrip('/')
    if not url:
        return jsonify({'ok': False, 'error': 'trc_server_url not configured'})
    test_url = url + '/api/raspberry/nodes'
    try:
        req = urllib.request.Request(test_url, method='GET')
        req.add_header('User-Agent', 'TRCOnBoard/1.0')
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.status
        return jsonify({'ok': True, 'status': code, 'url': test_url})
    except urllib.error.HTTPError as e:
        # 401/403 still means server is reachable
        return jsonify({'ok': True, 'status': e.code, 'url': test_url,
                        'note': 'server reachable (auth required)'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'url': test_url})


if __name__ == '__main__':
    # Eagerly preload DBCs for all configured data sources + mirror
    # virtual channels.  At this point every function is defined.
    try:
        _preload_dbcs_from_sources()
    except Exception:
        pass
    try:
        _load_mirror_dbcs()
    except Exception:
        pass

    # Load ARXML catalog and wire it into the bus manager's ArxmlDecoder.
    # This is the ARXML-based fallback for buses where DBC is unavailable
    # (e.g. HCAN) or for signals not covered by DBC files.
    try:
        _arxml_files = os.listdir(UPLOAD_FOLDER_ARXML)
        if any(f.lower().endswith('.arxml') for f in _arxml_files):
            from arxml_parser import load_catalog_from_directory
            _cat = load_catalog_from_directory(UPLOAD_FOLDER_ARXML)
            if _cat:
                _n = manager.load_arxml_catalog(_cat)
                print(f"[STARTUP] ARXML catalog loaded → {_n} total frames indexed "
                      f"(CAN={manager.arxml_decoder.can_frame_count if manager.arxml_decoder else 0}, "
                      f"FR={manager.arxml_decoder.fr_frame_count if manager.arxml_decoder else 0})", flush=True)
    except Exception as _e:
        print(f"[STARTUP] ARXML auto-load skipped: {_e}", flush=True)

    # Load FIBEX files for configured FlexRay data sources so mirror
    # FlexRay frames are decoded from the very first frame.
    try:
        _ds_cfg = config_store.get_config_only() or {}
        _ds_sources = _ds_cfg.get('data_sources') if isinstance(_ds_cfg.get('data_sources'), list) else []
        for _src in _ds_sources:
            if not isinstance(_src, dict):
                continue
            if str(_src.get('type') or '').strip().upper() != 'FLEXRAY':
                continue
            _fibex_name = str(_src.get('fibex_name') or '').strip()
            if not _fibex_name:
                continue
            _fibex_path = os.path.join(UPLOAD_FOLDER_FIBEX, os.path.basename(_fibex_name))
            if os.path.isfile(_fibex_path):
                manager.load_fibex(_fibex_path)
                print(f"[STARTUP] FIBEX loaded: {os.path.basename(_fibex_path)}", flush=True)
    except Exception as _e:
        print(f"[STARTUP] FIBEX auto-load skipped: {_e}", flush=True)

    # Start Ethernet Stats Thread
    threading.Thread(target=eth_stats_loop, daemon=True).start()
    threading.Thread(target=_bus_stats_loop, daemon=True).start()
    threading.Thread(target=_recording_sync_loop, daemon=True).start()
    threading.Thread(target=_trigger_autostop_loop, daemon=True).start()
    threading.Thread(target=_can_trigger_watchdog_loop, daemon=True).start()
    threading.Thread(target=_autostart_from_saved_config, daemon=True).start()
    threading.Thread(target=_xcp_auto_restore_project, daemon=True).start()
    threading.Thread(target=_copilot_ollama_watchdog_loop, daemon=True).start()
    host = str(os.getenv('KBSM_HOST', '0.0.0.0') or '0.0.0.0').strip() or '0.0.0.0'
    try:
        port = int(str(os.getenv('KBSM_PORT', '5000') or '5000').strip() or '5000')
    except Exception:
        port = 5000
    debug = str(os.getenv('KBSM_DEBUG', '0') or '0').strip().lower() in {'1', 'true', 'yes', 'on'}
    try:
        import sys
        if getattr(sys, 'stdin', None) is None or getattr(sys.stdin, 'closed', False):
            sys.stdin = open(os.devnull, 'r', encoding='utf-8', errors='ignore')
    except Exception:
        pass
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
