#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KVBM_DIR="$ROOT_DIR/kvaser_bus_manager"

PY_BIN="${PY_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$KVBM_DIR/.venv}"

LOCKFILE="$KVBM_DIR/requirements-lock.txt"
REQFILE="$KVBM_DIR/requirements.txt"

if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PY_BIN not found" >&2
  exit 1
fi

mkdir -p "$KVBM_DIR"

if [ ! -d "$VENV_DIR" ]; then
  "$PY_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip

if [ -f "$LOCKFILE" ]; then
  echo "Installing pinned dependencies from $LOCKFILE"
  "$VENV_DIR/bin/pip" install -r "$LOCKFILE"
elif [ -f "$REQFILE" ]; then
  echo "Installing dependencies from $REQFILE (no lockfile present)"
  "$VENV_DIR/bin/pip" install -r "$REQFILE"
else
  echo "ERROR: No requirements file found" >&2
  exit 1
fi

echo "OK: environment ready at $VENV_DIR"
