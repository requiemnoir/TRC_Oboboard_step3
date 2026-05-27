"""Wire schemas for master ↔ slave dialog.

Plain dataclasses + ``.to_dict()`` / ``.from_dict()`` — keeps the protocol
JSON-friendly without pulling in Pydantic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


PROTOCOL_VERSION = "1.0"

# Default ports — overridable via env (TRC_SLAVE_API_PORT, TRC_SLAVE_WS_PORT).
NODE_API_PORT = 8001
NODE_WS_PORT = 9001

# Default static IPs on the private LAN (master_node/install + slave_node/install
# both configure netplan with these unless overridden).
DEFAULT_MASTER_IP = "192.168.50.10"
DEFAULT_SLAVE_IP = "192.168.50.20"


@dataclass
class HealthResponse:
    """GET /api/health on slave."""

    node_role: str               # always "slave"
    protocol_version: str
    hostname: str
    uptime_s: float
    capture_active: bool
    boot_ts: float
    git_sha: Optional[str] = None
    branch: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HealthResponse":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class CaptureStatus:
    """GET /api/capture/status on slave."""

    active: bool
    session_id: Optional[str]
    session_started_ts: Optional[float]
    frame_count: int
    dropped_count: int
    parts: int
    bytes_written: int
    fps_1s: float
    fps_60s: float
    queue_depth: int
    disk_free_mb: float
    last_error: Optional[str] = None
    last_error_ts: Optional[float] = None
    # AUTOSAR Bus Mirror listener stats
    udp_packets_rx: int = 0
    udp_packets_rx_per_s: float = 0.0
    udp_bytes_rx: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureCommand:
    """POST /api/capture/{start|stop|snapshot} body."""

    action: str                          # "start" | "stop" | "snapshot" | "rotate"
    reason: Optional[str] = None
    snapshot_pre_s: float = 15.0
    snapshot_post_s: float = 15.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommandResult:
    """POST /api/cmd/exec result."""

    ok: bool
    rc: int
    stdout: str
    stderr: str
    elapsed_s: float
    cmd: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LogLine:
    """One streamed log entry."""

    ts: float
    level: str          # "INFO" / "WARN" / "ERROR" / "DEBUG"
    component: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MF4FileInfo:
    """One MF4 segment on slave's disk."""

    name: str
    size_bytes: int
    mtime: float
    session_id: str
    frame_count: Optional[int] = None
    finalized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Mf4ListResponse:
    files: List[MF4FileInfo]
    total_size_bytes: int
    log_dir: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "total_size_bytes": self.total_size_bytes,
            "log_dir": self.log_dir,
        }
