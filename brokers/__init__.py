"""Broker adapters — where orders actually execute.

Currently: Robinhood agentic MCP, Alpaca (paper or live), and moomoo (paper
or live, via their local OpenD gateway). Other platforms: copy alpaca.py's
shape (setup/client/execute + a WIZARD descriptor) — PRs welcome, or let the
agent build one via self-edit.

A broker module exposes:
    setup(cfg) -> cfg               interactive wizard (auth + account pick)
    client(cfg)                     authenticated client or None
    execute(client, cfg, event, state) -> bool   act on one normalized event
"""

from . import alpaca, moomoo, robinhood

BROKERS = {"robinhood": robinhood, "alpaca": alpaca, "moomoo": moomoo}


def broker_ready(cfg):
    """True when SOME broker is configured — registry-driven so new adapters
    (including agent-built ones) count without touching agent.py."""
    if not cfg:
        return False
    chosen = BROKERS.get(cfg.get("broker", ""))
    if chosen is not None and chosen.client(cfg) is not None:
        return True
    # pre-`broker`-key configs (early installs) — fall back to any adapter
    # that recognizes its own keys
    return any(mod.client(cfg) is not None for mod in BROKERS.values())


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
