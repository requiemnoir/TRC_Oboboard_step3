"""Master node companion module.

Adds a thin client layer to the existing kvaser_bus_manager backend so the
master Pi can talk to its sibling slave Pi (which actually owns the AUTOSAR
mirror capture).

Public API:
    SlaveClient(...)             — sync REST client for slave's /api/*
    SlaveSubscriber(...)         — SocketIO subscriber for slave's /slave NS
    bp_slave_panel               — Flask blueprint to mount under master backend
                                   (renders the "Slave Node" panel and proxies
                                    API calls so the existing UI stays SSO-friendly)
"""

from .slave_client import SlaveClient
from .slave_subscriber import SlaveSubscriber
from .blueprint import bp_slave_panel

__all__ = ["SlaveClient", "SlaveSubscriber", "bp_slave_panel"]
