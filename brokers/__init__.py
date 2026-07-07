"""Broker adapters — where orders actually execute.

Currently: Robinhood's agentic-trading MCP (dedicated Agentic account).
A broker module exposes:
    setup(cfg) -> cfg               interactive wizard (auth + account pick)
    client(cfg)                     authenticated client or None
    execute(client, cfg, event, state) -> bool   act on one normalized event
"""

from . import robinhood

BROKERS = {"robinhood": robinhood}
