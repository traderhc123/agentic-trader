"""Broker adapters — where orders actually execute.

Currently: Robinhood agentic MCP and Alpaca (paper or live). moomoo requires
their OpenD desktop gateway, so it ships as a community-adapter opportunity —
copy alpaca.py's shape (setup/client/execute) and point it at your local
OpenD; PRs welcome.

A broker module exposes:
    setup(cfg) -> cfg               interactive wizard (auth + account pick)
    client(cfg)                     authenticated client or None
    execute(client, cfg, event, state) -> bool   act on one normalized event
"""

from . import alpaca, robinhood

BROKERS = {"robinhood": robinhood, "alpaca": alpaca}
