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


def key_brokers():
    """Key-based brokers that ship a wizard descriptor (Robinhood is OAuth and
    handled separately). Any adapter with a WIZARD dict + connect() appears in
    the setup UI automatically — including ones the agent writes via self-edit."""
    out = []
    for mod in BROKERS.values():
        w = getattr(mod, "WIZARD", None)
        if w and callable(getattr(mod, "connect", None)):
            out.append(w)
    return out
