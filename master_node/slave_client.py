"""HTTP client for the slave's REST API.

Uses urllib (stdlib only) so the master backend doesn't need extra deps.
All endpoints are idempotent except the capture ones (start/stop/snapshot).
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from node_protocol import (
    DEFAULT_SLAVE_IP,
    NODE_API_PORT,
    HealthResponse,
    CaptureStatus,
    LogLine,
    MF4FileInfo,
    Mf4ListResponse,
    CommandResult,
    header_for_token,
    load_token,
)


class SlaveClient:
    """Sync HTTP client for slave_daemon.

    Args:
        slave_ip: IP of slave Pi (default 192.168.50.20)
        slave_port: HTTP port (default 8001)
        token_file: bearer token file (default /etc/trc-node-token).
                    If file missing, falls back to TRC_SLAVE_TOKEN env var.
        timeout: per-request timeout in seconds
    """

    def __init__(
        self,
        slave_ip: Optional[str] = None,
        slave_port: int = NODE_API_PORT,
        token_file: Optional[str] = None,
        timeout: float = 4.0,
    ) -> None:
        self.slave_ip = slave_ip or os.getenv("TRC_SLAVE_IP", DEFAULT_SLAVE_IP)
        self.slave_port = int(os.getenv("TRC_SLAVE_API_PORT", slave_port))
        self.base = f"http://{self.slave_ip}:{self.slave_port}"
        self.timeout = timeout

        token = load_token(token_file or os.getenv("TRC_SLAVE_TOKEN_FILE", "/etc/trc-node-token"))
        if token is None:
            token = os.getenv("TRC_SLAVE_TOKEN")
        self._token = token

    # ------------------------------------------------------------------- io

    def _req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers.update(header_for_token(self._token))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 204:
                    return None
                ct = resp.headers.get("content-type", "")
                raw = resp.read()
                if "application/json" in ct:
                    return json.loads(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise SlaveError(f"HTTP {exc.code} {path}: {err_body}") from exc
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise SlaveUnreachable(f"slave unreachable @ {url}: {exc}") from exc

    # --------------------------------------------------------------- public

    def health(self) -> HealthResponse:
        d = self._req("GET", "/api/health")
        return HealthResponse.from_dict(d) if isinstance(d, dict) else d

    def status(self) -> CaptureStatus:
        d = self._req("GET", "/api/capture/status")
        return CaptureStatus(**d)

    def start_capture(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._req("POST", "/api/capture/start", cfg or {})

    def stop_capture(self) -> Dict[str, Any]:
        return self._req("POST", "/api/capture/stop", {})

    def snapshot(self) -> Dict[str, Any]:
        return self._req("POST", "/api/capture/snapshot", {})

    def logs(self, lines: int = 200, level: Optional[str] = None) -> List[LogLine]:
        q = f"?lines={int(lines)}"
        if level:
            q += f"&level={level}"
        raw = self._req("GET", "/api/logs" + q)
        return [LogLine(**l) for l in raw] if isinstance(raw, list) else []

    def exec_cmd(self, cmd: str) -> CommandResult:
        d = self._req("POST", "/api/cmd/exec", {"cmd": cmd})
        return CommandResult(**d)

    def list_mf4(self) -> Mf4ListResponse:
        d = self._req("GET", "/api/mf4/list")
        return Mf4ListResponse(
            files=[MF4FileInfo(**f) for f in d.get("files", [])],
            total_size_bytes=d.get("total_size_bytes", 0),
            log_dir=d.get("log_dir", ""),
        )

    def download_mf4(self, name: str) -> bytes:
        raw = self._req("GET", f"/api/mf4/{name}")
        return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()

    def is_reachable(self) -> bool:
        try:
            self.health()
            return True
        except Exception:
            return False

    def wait_until_reachable(self, timeout_s: float = 30.0, interval_s: float = 0.5) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_reachable():
                return True
            time.sleep(interval_s)
        return False


class SlaveError(Exception):
    """Slave responded but with an HTTP error."""


class SlaveUnreachable(SlaveError):
    """Network/connection error reaching the slave."""
