"""Slave node — dedicated AUTOSAR Bus Mirror capture daemon.

This package is meant to run on a SECOND Raspberry Pi (or CM5) wired to the
same private LAN as the master Pi. It owns:

  - The DoIP activation request to the vehicle gateway (sends DID 0xF1A0)
  - The UDP socket on :30490 receiving the mirrored bus traffic
  - The raw MF4 writer (zero-drop guarantee)
  - A small HTTP API on :8001 used by the master to monitor and control
  - A WebSocket on :9001 to stream decoded frames live to the master
  - A small built-in status UI on http://<slave-ip>:8001/ for local debug

Boot sequence (target ≤ 8 s from power-on):
  1. systemd ``trc-slave.service`` starts ``slave_node.daemon``
  2. daemon binds UDP :30490 immediately
  3. daemon writes its token (already on disk), starts API + WS
  4. daemon sends DoIP DID 0xF1A0 to gateway (if config has gateway_mirror)
  5. capture loop runs forever
"""

from .daemon import create_app, main

__all__ = ["create_app", "main"]
