#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

# Allow running from repo root or kvaser_bus_manager/
THIS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(THIS_DIR, '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backend.dbc_catalog_db import DbcCatalogDb


def main() -> int:
    ap = argparse.ArgumentParser(description='Import DBC files into persistent dbc_catalog.db (SQLite)')
    ap.add_argument('--dbc-dir', default=os.path.join(ROOT_DIR, 'databases', 'dbc'))
    ap.add_argument('--out-dir', default=os.path.join(ROOT_DIR, 'logs', 'monitor'))
    ap.add_argument('--include-signals', default='1', help='1/0')
    ap.add_argument('--force', default='0', help='1/0')
    args = ap.parse_args()

    dbc_dir = os.path.abspath(args.dbc_dir)
    out_dir = os.path.abspath(args.out_dir)
    include_signals = str(args.include_signals).strip().lower() in {'1', 'true', 'yes', 'on'}
    force = str(args.force).strip().lower() in {'1', 'true', 'yes', 'on'}

    if not os.path.isdir(dbc_dir):
        print(f'ERROR: DBC dir not found: {dbc_dir}', file=sys.stderr)
        return 2

    os.makedirs(out_dir, exist_ok=True)
    db = DbcCatalogDb(base_dir=out_dir)

    names = []
    for n in sorted(os.listdir(dbc_dir)):
        if not isinstance(n, str):
            continue
        if not n.lower().endswith('.dbc'):
            continue
        if os.path.basename(n) != n:
            continue
        names.append(n)

    if not names:
        print(f'No .dbc files found in {dbc_dir}. Nothing to import.')
        return 0

    imported = 0
    skipped = 0
    errors = 0

    for name in names:
        path = os.path.join(dbc_dir, name)
        try:
            r = db.import_dbc_file(
                dbc_name=name,
                path=path,
                include_signals=include_signals,
                force=force,
            )
            if r.get('imported'):
                imported += 1
            elif r.get('skipped'):
                skipped += 1
            print(f"{name}: ok (imported={bool(r.get('imported'))}, messages={r.get('messages_count')}, signals={r.get('signals_count')})")
        except Exception as e:
            errors += 1
            print(f"{name}: ERROR: {e}", file=sys.stderr)

    print(f'Done. imported={imported} skipped={skipped} errors={errors} out_dir={out_dir}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
