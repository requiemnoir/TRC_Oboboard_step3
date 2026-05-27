"""Flask blueprint that adds the Slave Node panel to the master backend.

Mount it in kvaser_bus_manager/backend/app.py with:

    from master_node import bp_slave_panel
    app.register_blueprint(bp_slave_panel, url_prefix="/slave-node")

This exposes (URL prefix included):
    /slave-node/                 — full panel page
    /slave-node/api/status       — proxy: GET slave's /api/capture/status
    /slave-node/api/health       — proxy: GET slave's /api/health
    /slave-node/api/logs         — proxy: GET slave's /api/logs
    /slave-node/api/cmd          — proxy: POST slave's /api/cmd/exec
    /slave-node/api/start        — proxy: POST slave's /api/capture/start
    /slave-node/api/stop         — proxy: POST slave's /api/capture/stop
    /slave-node/api/snapshot     — proxy: POST slave's /api/capture/snapshot
    /slave-node/api/mf4          — proxy: GET slave's /api/mf4/list
    /slave-node/api/mf4/<name>   — proxy: GET slave's /api/mf4/<name> (streaming download)

The proxy approach keeps a single origin for the operator's browser and lets
the master backend enforce its own auth on top.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from .slave_client import SlaveClient, SlaveError, SlaveUnreachable


log = logging.getLogger("master.bp_slave_panel")
bp_slave_panel = Blueprint(
    "slave_panel",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/slave-node-static",
)

_client_singleton: "SlaveClient | None" = None


def _client() -> SlaveClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = SlaveClient()
    return _client_singleton


def _err_to_response(exc: Exception, status: int = 502):
    return jsonify({"ok": False, "error": str(exc),
                    "type": type(exc).__name__}), status


# -----------------------------------------------------------------------------


@bp_slave_panel.route("/")
def panel():
    """Master UI: full-page slave panel."""
    return render_template("slave_panel.html",
                           slave_ip=_client().slave_ip,
                           slave_port=_client().slave_port)


@bp_slave_panel.route("/api/health")
def api_health():
    try:
        return jsonify(_client().health().to_dict() if hasattr(_client().health(), "to_dict")
                       else _client().health())
    except SlaveUnreachable as e:
        return _err_to_response(e, 504)
    except SlaveError as e:
        return _err_to_response(e, 502)


@bp_slave_panel.route("/api/status")
def api_status():
    try:
        return jsonify(_client().status().to_dict())
    except SlaveUnreachable as e:
        return _err_to_response(e, 504)
    except SlaveError as e:
        return _err_to_response(e, 502)


@bp_slave_panel.route("/api/logs")
def api_logs():
    try:
        lines = int(request.args.get("lines", 200))
        level = request.args.get("level")
        out = _client().logs(lines=lines, level=level)
        return jsonify([l.to_dict() for l in out])
    except (SlaveError, SlaveUnreachable) as e:
        return _err_to_response(e)


@bp_slave_panel.route("/api/cmd", methods=["POST"])
def api_cmd():
    body = request.get_json(silent=True) or {}
    cmd = str(body.get("cmd", "")).strip()
    if not cmd:
        return jsonify({"ok": False, "error": "empty cmd"}), 400
    try:
        return jsonify(_client().exec_cmd(cmd).to_dict())
    except (SlaveError, SlaveUnreachable) as e:
        return _err_to_response(e)


def _capture_action(action: str) -> Any:
    try:
        if action == "start":
            return jsonify(_client().start_capture(request.get_json(silent=True) or {}))
        if action == "stop":
            return jsonify(_client().stop_capture())
        if action == "snapshot":
            return jsonify(_client().snapshot())
    except (SlaveError, SlaveUnreachable) as e:
        return _err_to_response(e)
    return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 400


@bp_slave_panel.route("/api/start", methods=["POST"])
def api_start():
    return _capture_action("start")


@bp_slave_panel.route("/api/stop", methods=["POST"])
def api_stop():
    return _capture_action("stop")


@bp_slave_panel.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    return _capture_action("snapshot")


@bp_slave_panel.route("/api/mf4")
def api_mf4_list():
    try:
        return jsonify(_client().list_mf4().to_dict())
    except (SlaveError, SlaveUnreachable) as e:
        return _err_to_response(e)


@bp_slave_panel.route("/api/mf4/<path:name>")
def api_mf4_download(name: str):
    try:
        blob = _client().download_mf4(name)
        if isinstance(blob, str):
            blob = blob.encode()
        return send_file(
            io.BytesIO(blob),
            as_attachment=True,
            download_name=name,
            mimetype="application/octet-stream",
        )
    except (SlaveError, SlaveUnreachable) as e:
        return _err_to_response(e)
