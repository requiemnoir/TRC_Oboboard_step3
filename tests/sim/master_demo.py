"""Minimal Flask wrapper to demo the master_node slave-panel UI without
pulling in the full kvaser_bus_manager backend (which needs canlib + many
deps that don't all install on macOS).

Run:
    TRC_SLAVE_IP=127.0.0.1 TRC_SLAVE_API_PORT=18002 \
        python tests/sim/master_demo.py

Opens:
    http://127.0.0.1:5050/                     landing page
    http://127.0.0.1:5050/slave-node/          full slave panel (proxied)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repo root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from flask import Flask, redirect, render_template_string

from master_node import bp_slave_panel


LANDING = """<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>TRC Master — demo landing</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
           background: #0a0f17; color: #e6ebf2; padding: 40px;
           max-width: 720px; margin: 0 auto; }
    h1 { color: #ff9933; border-bottom: 2px solid #ff9933; padding-bottom: 10px; }
    a { color: #ffb366; text-decoration: none; padding: 12px 20px;
        background: #131a25; border: 1px solid #2a3441;
        border-radius: 6px; display: inline-block; margin: 8px 0;
        font-family: monospace; font-size: 14px; }
    a:hover { border-color: #ff9933; }
    .grid { display: flex; flex-direction: column; gap: 8px; }
    code { background: #1a2233; padding: 2px 6px; border-radius: 3px;
           font-size: 12px; color: #79b8ff; }
    .note { background: #131a25; border-left: 3px solid #ff9933;
            padding: 12px 16px; margin: 16px 0; font-size: 13px;
            color: #c9d1d9; }
  </style>
</head>
<body>
  <h1>🛰️ TRC Master · demo</h1>
  <p class="note">
    Questa è una pagina minima di test che mostra <b>solo il pannello
    master_node /slave-node/</b>. Sul Pi reale il backend kvaser_bus_manager
    intero gira a <code>:5000/</code> e questo pannello è una sezione.
  </p>

  <div class="grid">
    <a href="/slave-node/">🟠 /slave-node/ — pannello slave (master UI)</a>
    <a href="http://127.0.0.1:18002/" target="_blank">⚡ http://127.0.0.1:18002/ — UI locale del slave (debug)</a>
  </div>

  <h2 style="color:#ff9933; margin-top:30px;">Endpoint API diretti</h2>
  <div class="grid">
    <a href="/slave-node/api/status">/slave-node/api/status (JSON)</a>
    <a href="/slave-node/api/health">/slave-node/api/health (JSON)</a>
    <a href="/slave-node/api/mf4">/slave-node/api/mf4 (lista MF4)</a>
    <a href="/slave-node/api/logs?lines=20">/slave-node/api/logs?lines=20</a>
  </div>

  <h2 style="color:#ff9933; margin-top:30px;">Slave info</h2>
  <p>master_node punta a: <code>{{ slave_ip }}:{{ slave_port }}</code></p>
</body>
</html>"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "trc-demo"
    app.register_blueprint(bp_slave_panel, url_prefix="/slave-node")

    @app.route("/")
    def landing():
        return render_template_string(
            LANDING,
            slave_ip=os.getenv("TRC_SLAVE_IP", "127.0.0.1"),
            slave_port=os.getenv("TRC_SLAVE_API_PORT", "18002"),
        )

    return app


if __name__ == "__main__":
    app = create_app()
    print(">>> master demo @ http://127.0.0.1:5050/", flush=True)
    print(f">>> slave target: {os.getenv('TRC_SLAVE_IP', '127.0.0.1')}:"
          f"{os.getenv('TRC_SLAVE_API_PORT', '18002')}", flush=True)
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
