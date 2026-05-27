"""Bearer-token auth between master and slave.

The token is generated at install time on the slave (16-byte hex) and
written to:
  - /etc/trc-node-token         (root:root 0600)
  - /home/boss/.trc-node-token  (boss:boss 0600, optional convenience)

The master is configured (via its install script) to point at
/etc/trc-node-token. Both nodes use it as a static Bearer token over HTTP
on the private LAN. NOT internet-grade auth — purely to prevent stray
LAN clients from poking the slave's command exec endpoint.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Dict, Optional


DEFAULT_TOKEN_FILE = "/etc/trc-node-token"


def generate_token(n_bytes: int = 16) -> str:
    """Return a fresh random hex token."""
    return secrets.token_hex(n_bytes)


def load_token(path: str = DEFAULT_TOKEN_FILE) -> Optional[str]:
    """Read the token from disk. Returns None if missing/unreadable."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def header_for_token(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def check_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time compare. Returns False if either side is empty."""
    if not provided or not expected:
        return False
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
