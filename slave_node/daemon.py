"""Slave daemon — Flask + SocketIO + mirror_logger wrapper.

Run with:
    python -m slave_node.daemon

Environment:
    TRC_SLAVE_API_PORT        (default 8001)
    TRC_SLAVE_WS_PORT         (default 9001)
    TRC_SLAVE_LOG_DIR         (default /var/log/trc-slave)
    TRC_SLAVE_MF4_DIR         (default /var/lib/trc-slave/mf4)
    TRC_SLAVE_TOKEN_FILE      (default /etc/trc-node-token)
    TRC_SLAVE_GATEWAY_IP      (optional, default from config_store)
    TRC_SLAVE_BIND            (default 0.0.0.0)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from flask_socketio import SocketIO, emit

# Sibling-import: mirror_logger lives at repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "mirror_logger"))

from node_protocol import (
    NODE_API_PORT,
    NODE_WS_PORT,
    PROTOCOL_VERSION,
    CaptureCommand,
    CaptureStatus,
    CommandResult,
    HealthResponse,
    LogLine,
    MF4FileInfo,
    Mf4ListResponse,
    check_token,
    load_token,
)

# mirror_logger imports — guarded so we degrade gracefully if missing.
try:
    from mirror_logger.mirror_parser import MirrorParser, RawFrame  # type: ignore
    from mirror_logger.raw_logger import RawLogger  # type: ignore
    from mirror_logger import doip_activator  # type: ignore
    _HAS_MIRROR = True
except Exception as _e:  # pragma: no cover - logged at boot
    _HAS_MIRROR = False
    _MIRROR_IMPORT_ERROR = repr(_e)


log = logging.getLogger("slave")


# ----------------------------------------------------------------------- state


class CaptureState:
    """In-memory state of the slave's capture subsystem."""

    def __init__(self, mf4_dir: str, log_dir: str) -> None:
        self.mf4_dir = mf4_dir
        self.log_dir = log_dir
        self.boot_ts = time.time()

        self._lock = threading.RLock()
        self._logger: Optional["RawLogger"] = None
        self._parser: Optional["MirrorParser"] = None
        self._udp_sock: Optional[socket.socket] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._udp_stop = threading.Event()

        self.session_id: Optional[str] = None
        self.session_started_ts: Optional[float] = None
        self.frame_count = 0
        self.dropped_count = 0
        self.parts = 0
        self.bytes_written = 0
        self.queue_depth = 0

        self.udp_packets_rx = 0
        self.udp_packets_rx_per_s = 0.0
        self.udp_bytes_rx = 0
        self._last_rx_count = 0
        self._last_rx_ts = self.boot_ts

        self.last_error: Optional[str] = None
        self.last_error_ts: Optional[float] = None

        # frame fps EMA
        self._fps_buf_1s: Deque[int] = deque(maxlen=10)   # 100 ms buckets
        self._fps_buf_60s: Deque[int] = deque(maxlen=60)  # 1 s buckets

    def is_active(self) -> bool:
        with self._lock:
            return self._logger is not None and self._udp_thread is not None

    def disk_free_mb(self) -> float:
        try:
            st = os.statvfs(self.mf4_dir)
            return st.f_bavail * st.f_frsize / 1024 / 1024
        except OSError:
            return 0.0

    def snapshot(self) -> CaptureStatus:
        with self._lock:
            return CaptureStatus(
                active=self.is_active(),
                session_id=self.session_id,
                session_started_ts=self.session_started_ts,
                frame_count=self.frame_count,
                dropped_count=self.dropped_count,
                parts=self.parts,
                bytes_written=self.bytes_written,
                fps_1s=sum(self._fps_buf_1s) * 1.0,
                fps_60s=sum(self._fps_buf_60s) / 60.0 if len(self._fps_buf_60s) else 0.0,
                queue_depth=self.queue_depth,
                disk_free_mb=self.disk_free_mb(),
                last_error=self.last_error,
                last_error_ts=self.last_error_ts,
                udp_packets_rx=self.udp_packets_rx,
                udp_packets_rx_per_s=self.udp_packets_rx_per_s,
                udp_bytes_rx=self.udp_bytes_rx,
            )


# ----------------------------------------------------------------- log buffer


class RingLogHandler(logging.Handler):
    """Keeps the last N log lines in memory + pushes via SocketIO."""

    def __init__(self, ring: Deque[LogLine], socketio: Optional[SocketIO]) -> None:
        super().__init__(level=logging.DEBUG)
        self.ring = ring
        self.socketio = socketio
        fmt = logging.Formatter("%(message)s")
        self.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            line = LogLine(
                ts=record.created,
                level=record.levelname,
                component=record.name,
                message=record.getMessage(),
            )
            self.ring.append(line)
            if self.socketio is not None:
                self.socketio.emit("log", line.to_dict(), namespace="/slave")
        except Exception:
            pass


# ----------------------------------------------------------------- capture i/o


def _start_capture(state: CaptureState, socketio: SocketIO, cfg: Dict[str, Any]) -> None:
    """Start UDP listener + raw logger. Idempotent."""
    if not _HAS_MIRROR:
        raise RuntimeError(f"mirror_logger import failed: {_MIRROR_IMPORT_ERROR}")

    with state._lock:
        if state.is_active():
            log.info("capture already active; ignoring start")
            return

        log.info("starting raw logger → %s", state.mf4_dir)
        rl = RawLogger(
            log_dir=state.mf4_dir,
            chunk_interval_s=float(cfg.get("chunk_interval_s", 30.0)),
            flush_interval_s=float(cfg.get("flush_interval_s", 1.0)),
            flush_interval_frames=int(cfg.get("flush_interval_frames", 500)),
            queue_max=int(cfg.get("queue_max", 131072)),
            put_timeout_ms=int(cfg.get("put_timeout_ms", 50)),
        )
        rl.start()
        state._logger = rl
        state.session_id = getattr(rl, "session_id", None)
        state.session_started_ts = time.time()
        state.frame_count = 0
        state.dropped_count = 0
        state.parts = 0
        state.bytes_written = 0
        # Reset UDP counters at start too — otherwise lifetime totals across
        # multiple capture restarts confuse the master's delta logic.
        state.udp_packets_rx = 0
        state.udp_packets_rx_per_s = 0.0
        state.udp_bytes_rx = 0
        state._last_rx_count = 0
        state._last_rx_ts = time.time()

        def on_frame(f: "RawFrame") -> None:
            try:
                rl.log(f)
                state.frame_count += 1
                # emit a downsampled live frame to master (every 32nd to avoid WS flood)
                if state.frame_count % 32 == 0:
                    socketio.emit("frame", {
                        "ts_ns": f.ts_ns, "type": f.frame_type,
                        "channel": f.channel_id, "arb_id": f.arb_id,
                        "dlc": f.dlc,
                    }, namespace="/slave")
            except Exception as exc:
                state.last_error = f"on_frame: {exc!r}"
                state.last_error_ts = time.time()

        parser = MirrorParser(callback=on_frame)
        state._parser = parser

        # UDP listener
        bind_host = cfg.get("listen_host", "0.0.0.0")
        bind_port = int(cfg.get("listen_port", 30490))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_host, bind_port))
        sock.settimeout(0.5)
        state._udp_sock = sock
        state._udp_stop.clear()

        def udp_worker() -> None:
            log.info("UDP listener bound %s:%d", bind_host, bind_port)
            while not state._udp_stop.is_set():
                try:
                    data, _ = sock.recvfrom(65535)
                    state.udp_packets_rx += 1
                    state.udp_bytes_rx += len(data)
                    parser.parse(data, ts_pkt=time.time())
                except socket.timeout:
                    continue
                except OSError as exc:
                    if state._udp_stop.is_set():
                        break
                    state.last_error = f"udp_worker: {exc!r}"
                    state.last_error_ts = time.time()
                    time.sleep(0.1)
            log.info("UDP listener stopped")

        t = threading.Thread(target=udp_worker, name="slave-udp", daemon=True)
        t.start()
        state._udp_thread = t


def _stop_capture(state: CaptureState) -> Dict[str, Any]:
    with state._lock:
        stats: Dict[str, Any] = {}
        state._udp_stop.set()
        if state._udp_sock is not None:
            try:
                state._udp_sock.close()
            except OSError:
                pass
        if state._udp_thread is not None:
            state._udp_thread.join(timeout=2.0)
        state._udp_sock = None
        state._udp_thread = None

        if state._logger is not None:
            try:
                stats = state._logger.stop(timeout_s=10.0)
            except Exception as exc:
                state.last_error = f"stop_capture: {exc!r}"
                state.last_error_ts = time.time()
            state._logger = None
        state._parser = None
        log.info("capture stopped (%s)", stats)
        return stats


def _periodic_metrics(state: CaptureState, stop: threading.Event) -> None:
    while not stop.wait(0.1):
        # update 100ms bucket
        state._fps_buf_1s.append(state.frame_count - state._last_rx_count)
        if int(time.time() * 10) % 10 == 0:
            sec_total = sum(state._fps_buf_1s)
            state._fps_buf_60s.append(sec_total)
            state.udp_packets_rx_per_s = (
                state.udp_packets_rx - state._last_rx_count
            ) / 1.0
            state._last_rx_count = state.udp_packets_rx
        state._last_rx_count = state.frame_count


# ------------------------------------------------------------------ flask app


def create_app(
    log_dir: str = "/var/log/trc-slave",
    mf4_dir: str = "/var/lib/trc-slave/mf4",
    token_file: Optional[str] = None,
):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    Path(mf4_dir).mkdir(parents=True, exist_ok=True)

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
        static_url_path="/static",
    )
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
    state = CaptureState(mf4_dir=mf4_dir, log_dir=log_dir)

    # logger setup — file + ring buffer + console
    log_path = Path(log_dir) / "slave.log"
    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))
    ring: Deque[LogLine] = deque(maxlen=2000)
    ring_h = RingLogHandler(ring, socketio)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(ring_h)
    root.addHandler(logging.StreamHandler(sys.stdout))

    log.info("slave daemon initialised; log_dir=%s mf4_dir=%s", log_dir, mf4_dir)
    if not _HAS_MIRROR:
        log.error("mirror_logger NOT available: %s", _MIRROR_IMPORT_ERROR)

    token = load_token(token_file or os.getenv("TRC_SLAVE_TOKEN_FILE", "/etc/trc-node-token"))
    if token is None:
        log.warning("no auth token found — API will be open on the LAN (DEV mode)")

    def _require_auth() -> None:
        if token is None:
            return
        if not check_token(request.headers.get("Authorization"), token):
            abort(401)

    # periodic metrics thread
    metrics_stop = threading.Event()
    threading.Thread(
        target=_periodic_metrics, args=(state, metrics_stop), daemon=True
    ).start()

    # ------------------------------------------------------------ HTTP routes

    @app.route("/")
    def index():
        return render_template(
            "status.html",
            hostname=socket.gethostname(),
            protocol_version=PROTOCOL_VERSION,
        )

    @app.route("/api/health")
    def api_health():
        _require_auth()
        resp = HealthResponse(
            node_role="slave",
            protocol_version=PROTOCOL_VERSION,
            hostname=socket.gethostname(),
            uptime_s=time.time() - state.boot_ts,
            capture_active=state.is_active(),
            boot_ts=state.boot_ts,
            git_sha=_git_sha(),
            branch=_git_branch(),
        )
        return jsonify(resp.to_dict())

    @app.route("/api/capture/status")
    def api_capture_status():
        _require_auth()
        return jsonify(state.snapshot().to_dict())

    @app.route("/api/capture/start", methods=["POST"])
    def api_capture_start():
        _require_auth()
        body = request.get_json(silent=True) or {}
        try:
            _start_capture(state, socketio, body)
        except Exception as exc:
            log.exception("capture start failed")
            return jsonify({"ok": False, "error": repr(exc)}), 500
        return jsonify({"ok": True, "status": state.snapshot().to_dict()})

    @app.route("/api/capture/stop", methods=["POST"])
    def api_capture_stop():
        _require_auth()
        stats = _stop_capture(state)
        return jsonify({"ok": True, "stop_stats": stats})

    @app.route("/api/capture/snapshot", methods=["POST"])
    def api_capture_snapshot():
        _require_auth()
        # delegate to raw_logger.force_flush + emit a "snapshot" event for master
        with state._lock:
            if state._logger is None:
                return jsonify({"ok": False, "error": "capture not active"}), 409
            ok = state._logger.force_flush(timeout_s=3.0)
        socketio.emit("snapshot", {"ts": time.time(), "ok": ok}, namespace="/slave")
        return jsonify({"ok": bool(ok)})

    @app.route("/api/logs")
    def api_logs():
        _require_auth()
        n = int(request.args.get("lines", 200))
        lvl = request.args.get("level")
        out = list(ring)[-n:]
        if lvl:
            out = [l for l in out if l.level == lvl.upper()]
        return jsonify([l.to_dict() for l in out])

    @app.route("/api/cmd/exec", methods=["POST"])
    def api_cmd_exec():
        _require_auth()
        body = request.get_json(silent=True) or {}
        cmd = str(body.get("cmd", "")).strip()
        if not cmd:
            return jsonify({"ok": False, "error": "empty cmd"}), 400
        # whitelist for non-root debug
        allow_prefix = (
            "uname", "uptime", "df", "free", "ip", "ss", "ps", "systemctl status",
            "journalctl", "lsmod", "ls", "cat /proc", "stat",
            "tail", "head", "wc", "grep", "ping", "hostname",
        )
        if not any(cmd.startswith(p) for p in allow_prefix):
            return jsonify({"ok": False, "error": "command not in allow-list",
                            "allow": allow_prefix}), 403
        t0 = time.time()
        try:
            cp = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10
            )
            res = CommandResult(
                ok=cp.returncode == 0,
                rc=cp.returncode,
                stdout=cp.stdout[-32_000:],
                stderr=cp.stderr[-8000:],
                elapsed_s=time.time() - t0,
                cmd=cmd,
            )
        except subprocess.TimeoutExpired:
            res = CommandResult(ok=False, rc=-1, stdout="", stderr="timeout",
                                elapsed_s=time.time() - t0, cmd=cmd)
        return jsonify(res.to_dict())

    @app.route("/api/mf4/list")
    def api_mf4_list():
        _require_auth()
        files = []
        total = 0
        for p in sorted(Path(mf4_dir).glob("*.mf4")):
            try:
                st = p.stat()
                # session_id from filename prefix
                stem = p.stem
                sess = stem.split("_p", 1)[0] if "_p" in stem else stem
                files.append(MF4FileInfo(
                    name=p.name, size_bytes=st.st_size, mtime=st.st_mtime,
                    session_id=sess, finalized=True,
                ))
                total += st.st_size
            except OSError:
                continue
        resp = Mf4ListResponse(files=files, total_size_bytes=total, log_dir=mf4_dir)
        return jsonify(resp.to_dict())

    @app.route("/api/mf4/<path:name>")
    def api_mf4_download(name: str):
        _require_auth()
        p = Path(mf4_dir) / name
        if not p.is_file() or ".." in name:
            abort(404)
        return send_file(p, as_attachment=True)

    @app.route("/metrics")
    def metrics():
        s = state.snapshot()
        lines = [
            "# HELP trc_slave_capture_active 1 if capture is active",
            "# TYPE trc_slave_capture_active gauge",
            f"trc_slave_capture_active {1 if s.active else 0}",
            "# TYPE trc_slave_frames_total counter",
            f"trc_slave_frames_total {s.frame_count}",
            "# TYPE trc_slave_dropped_total counter",
            f"trc_slave_dropped_total {s.dropped_count}",
            "# TYPE trc_slave_udp_packets_total counter",
            f"trc_slave_udp_packets_total {s.udp_packets_rx}",
            "# TYPE trc_slave_udp_bytes_total counter",
            f"trc_slave_udp_bytes_total {s.udp_bytes_rx}",
            "# TYPE trc_slave_disk_free_mb gauge",
            f"trc_slave_disk_free_mb {s.disk_free_mb}",
            "# TYPE trc_slave_fps_1s gauge",
            f"trc_slave_fps_1s {s.fps_1s}",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    # ------------------------------------------------------ SocketIO namespace

    @socketio.on("connect", namespace="/slave")
    def on_connect():
        log.info("master connected via SocketIO from %s", request.remote_addr)
        emit("hello", {
            "protocol_version": PROTOCOL_VERSION,
            "hostname": socket.gethostname(),
            "boot_ts": state.boot_ts,
        })

    @socketio.on("ping_master", namespace="/slave")
    def on_ping():
        emit("pong", {"ts": time.time()})

    app.config["socketio"] = socketio
    app.config["state"] = state
    app.config["token"] = token

    return app, socketio


def _git_sha() -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return cp.stdout.strip() or None
    except Exception:
        return None


def _git_branch() -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return cp.stdout.strip() or None
    except Exception:
        return None


def main() -> int:
    bind_host = os.getenv("TRC_SLAVE_BIND", "0.0.0.0")
    api_port = int(os.getenv("TRC_SLAVE_API_PORT", NODE_API_PORT))
    log_dir = os.getenv("TRC_SLAVE_LOG_DIR", "/var/log/trc-slave")
    mf4_dir = os.getenv("TRC_SLAVE_MF4_DIR", "/var/lib/trc-slave/mf4")

    app, socketio = create_app(log_dir=log_dir, mf4_dir=mf4_dir)

    # autostart capture if env set (default: on)
    if os.getenv("TRC_SLAVE_AUTOSTART", "1").lower() in ("1", "true", "yes"):
        try:
            _start_capture(app.config["state"], socketio, {})
            log.info("capture autostarted")
        except Exception as exc:
            log.exception("autostart failed: %s", exc)

    log.info("listening on %s:%d (HTTP + SocketIO)", bind_host, api_port)
    socketio.run(app, host=bind_host, port=api_port, allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
