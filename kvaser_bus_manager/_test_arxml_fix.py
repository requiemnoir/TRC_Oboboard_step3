import sys, os
sys.path.insert(0, 'backend')
from dbc_loader import load_dbc_database

print('Test 1: ARXML')
try:
    db = load_dbc_database('databases/arxml/MLBevo_Gen1_Autosar_V8.21.05F_20210616_EICR.arxml')
    print('cantools OK:', len(list(db.messages)), 'msgs')
except Exception as e:
    s = str(e)[:200]
    if 'DBC:' in s and 'Invalid syntax' in s:
        print('FAIL (still DBC fallback):', s[:120])
    else:
        print('OK - arxml-specific error (not DBC fallback):', s[:120])

print()
print('Test 2: DBC')
dbcs = [f for f in os.listdir('databases/dbc') if f.endswith('.dbc')][:1]
if dbcs:
    try:
        db = load_dbc_database(os.path.join('databases/dbc', dbcs[0]))
        print('OK:', dbcs[0], len(list(db.messages)), 'msgs')
    except Exception as e:
        print('FAIL:', str(e)[:100])

print()
print('Test 3: ARXML catalog + decoder')
from arxml_parser import load_catalog_from_directory
from arxml_decoder import ArxmlDecoder
cat = load_catalog_from_directory('databases/arxml')
dec = ArxmlDecoder()
n = dec.load_from_catalog(cat)
print(f'Decoder: {n} frames, {dec.can_frame_count} CAN, {dec.fr_frame_count} FR')
groups = dec.list_can_signals()
print(f'list_can_signals(): {len(groups)} groups')
if groups:
    print(f'  First group: {groups[0]["message"]}, {len(groups[0]["signals"])} signals')
