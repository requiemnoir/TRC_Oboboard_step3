"""TRC Onboard master/slave protocol — shared by both nodes.

The repo is deployed as either a "master node" (control + UI + AI) or a
"slave node" (dedicated AUTOSAR mirror capture). They communicate via
two channels on a private LAN segment (default 192.168.50.0/24):

  - HTTP REST on slave port :8001 — master polls slave state, sends commands
  - WebSocket (Flask-SocketIO) on slave port :9001 — slave streams decoded
    frames live to master

Plus an emergency USB serial console (gadget mode) at /dev/ttyGS0 on each Pi
for debug when Ethernet is unavailable.

This package defines the wire schema and auth so both sides agree without
hand-rolling JSON.
"""

from .api import (
    NODE_API_PORT,
    NODE_WS_PORT,
    DEFAULT_MASTER_IP,
    DEFAULT_SLAVE_IP,
    PROTOCOL_VERSION,
    HealthResponse,
    CaptureStatus,
    CaptureCommand,
    LogLine,
    CommandResult,
    MF4FileInfo,
    Mf4ListResponse,
)
from .auth import generate_token, load_token, header_for_token, check_token

__all__ = [
    "NODE_API_PORT",
    "NODE_WS_PORT",
    "DEFAULT_MASTER_IP",
    "DEFAULT_SLAVE_IP",
    "PROTOCOL_VERSION",
    "HealthResponse",
    "CaptureStatus",
    "CaptureCommand",
    "LogLine",
    "CommandResult",
    "MF4FileInfo",
    "Mf4ListResponse",
    "generate_token",
    "load_token",
    "header_for_token",
    "check_token",
]
