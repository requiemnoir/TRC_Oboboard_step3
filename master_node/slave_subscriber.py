"""SocketIO subscriber: master receives live frames + log lines from slave.

Runs in a background thread. Pushes received frames into the master's existing
bus_manager pipeline (so the live UI works just like with local capture) and
optionally proxies log lines into the master's structured logger.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

import socketio

from node_protocol import DEFAULT_SLAVE_IP, NODE_API_PORT, header_for_token, load_token


log = logging.getLogger("master.slave_sub")


class SlaveSubscriber:
    """Long-lived SocketIO client to slave's /slave namespace.

    Hooks:
        on_frame(frame_dict)   — called for each frame event (downsampled by slave)
        on_log(log_dict)       — called for each log line streamed from slave
        on_snapshot(payload)   — called when slave fires a snapshot event
        on_connect()           — connection established
        on_disconnect()        — connection lost (will auto-reconnect)
    """

    def __init__(
        self,
        slave_ip: Optional[str] = None,
        slave_port: int = NODE_API_PORT,
        token_file: Optional[str] = None,
        on_frame: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_log: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_snapshot: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self.slave_ip = slave_ip or os.getenv("TRC_SLAVE_IP", DEFAULT_SLAVE_IP)
        self.slave_port = int(os.getenv("TRC_SLAVE_API_PORT", slave_port))
        token = load_token(token_file or os.getenv("TRC_SLAVE_TOKEN_FILE", "/etc/trc-node-token"))
        if token is None:
            token = os.getenv("TRC_SLAVE_TOKEN")
        self._token = token
        self.on_frame = on_frame
        self.on_log = on_log
        self.on_snapshot = on_snapshot
        self.on_connect_cb = on_connect
        self.on_disconnect_cb = on_disconnect

        self._sio: Optional[socketio.Client] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _build_client(self) -> "socketio.Client":
        sio = socketio.Client(
            reconnection=True,
            reconnection_delay=1,
            reconnection_delay_max=8,
            randomization_factor=0.2,
            logger=False,
            engineio_logger=False,
        )

        @sio.event(namespace="/slave")
        def connect():
            log.info("slave subscriber connected (%s)", self._url())
            if self.on_connect_cb:
                self.on_connect_cb()

        @sio.event(namespace="/slave")
        def disconnect():
            log.warning("slave subscriber disconnected")
            if self.on_disconnect_cb:
                self.on_disconnect_cb()

        @sio.on("frame", namespace="/slave")
        def on_frame(data):
            if self.on_frame:
                try:
                    self.on_frame(data)
                except Exception as exc:
                    log.exception("on_frame: %s", exc)

        @sio.on("log", namespace="/slave")
        def on_log(data):
            if self.on_log:
                try:
                    self.on_log(data)
                except Exception as exc:
                    log.exception("on_log: %s", exc)

        @sio.on("snapshot", namespace="/slave")
        def on_snapshot(data):
            if self.on_snapshot:
                try:
                    self.on_snapshot(data)
                except Exception as exc:
                    log.exception("on_snapshot: %s", exc)

        return sio

    def _url(self) -> str:
        return f"http://{self.slave_ip}:{self.slave_port}"

    def _run(self) -> None:
        headers = header_for_token(self._token) if self._token else {}
        while not self._stop.is_set():
            try:
                self._sio = self._build_client()
                self._sio.connect(
                    self._url(),
                    namespaces=["/slave"],
                    headers=headers,
                    transports=["websocket", "polling"],
                    wait_timeout=10,
                )
                self._sio.wait()
            except Exception as exc:
                log.warning("slave subscriber connect failed: %s; retry in 3s", exc)
                self._stop.wait(3.0)
            finally:
                try:
                    if self._sio is not None:
                        self._sio.disconnect()
                except Exception:
                    pass

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="slave-subscriber", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sio is not None:
            try:
                self._sio.disconnect()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=4.0)

    def is_connected(self) -> bool:
        return self._sio is not None and getattr(self._sio, "connected", False)
