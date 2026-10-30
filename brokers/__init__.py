"""Broker adapters — where orders actually execute.

Currently: Robinhood agentic MCP, Alpaca (paper or live), and moomoo (paper
or live, via their local OpenD gateway). Other platforms: copy alpaca.py's
shape (setup/client/execute + a WIZARD descriptor) — PRs welcome, or let the
agent build one via self-edit.

A broker module exposes:
    setup(cfg) -> cfg               interactive wizard (auth + account pick)
    client(cfg)                     authenticated client or None
    execute(client, cfg, event, state) -> bool   act on one normalized event

On an EXITED that sold, execute pops the row, lowers its qty, or (sale
still working) sets row["pending"] = {"side": "sell", "requested": n, ...}.
With the read hooks below, a True return that leaves the row unchanged
means "accepted but nothing sold": the agent re-reads the account and
retries on the next poll instead of logging an exit.

Optional read-only hooks, used to notice trades the owner closed in the
broker app (the agent looks them up with getattr, so adapters without them
behave exactly as before). Neither ever raises, and "unknown" is never
reported as "not held":

    held_option_qty(client, cfg, option_ids) -> {option_id: int} | None
        LONG contracts held, ONLY for ids the broker positively answered for
        and that have no working order or other pending activity. An explicit
        0 means "positively not held"; an id missing from the dict means
        unknown; None means the whole read is unknown. Ids that aren't in
        this broker's id format are left out.

    closed_fills(client, cfg, option_id, since_iso, own_order_ids,
                 exclude_agentic=False) -> [fill] | None
        Filled (or partly filled) sell-to-close orders on option_id that are
        NOT in own_order_ids, newest first: {"order_id": str, "qty": int >= 1,
        "price": float | None (per share), "at": str | None, "by": str | None}.
        None means it couldn't be read; a partial list is allowed.
        own_order_ids is the row's "order_ids" plus state["agent_order_ids"]:
        an adapter with this hook appends the id of every order it places to
        that list (last 200), so the agent's sell of an earlier position in
        the same contract never passes for the owner's.

    VERIFIES_FILLS = False          module constant for adapters that book an
                                    entry when the order is accepted, without
                                    checking that it filled (default True).

    CLOSE_ONLY_EXITS = True         module constant; set it ONLY when the
                                    adapter's exit orders can never open a
                                    new position (Robinhood sells with
                                    position_effect "close"). After an exit
                                    that sold nothing, the agent sells again
                                    only once a read shows the contracts
                                    still held; with this set, unclear reads
                                    stop holding the exit back after 10 min.
"""

import hashlib

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


def account_tag(cfg):
    """Which broker account (and paper/live mode) the agent trades now, e.g.
    "robinhood:3f2a…" or "alpaca:paper". Stored on each position so a row
    opened in one account is never compared with, or sold from, another.
    Never contains a raw account number. Alpaca and moomoo use the mode, not
    the key or the gateway address, so rotating a key keeps the rows."""
    cfg = cfg or {}
    name = str(cfg.get("broker") or "robinhood")  # same default as `run`
    if name == "robinhood":
        acct = str(cfg.get("robinhood_account") or "")
        ident = hashlib.sha1(acct.encode()).hexdigest()[:10]
    elif name == "alpaca":
        ident = "paper" if cfg.get("alpaca_paper", True) else "live"
    elif name == "moomoo":
        ident = "paper" if cfg.get("moomoo_paper", True) else "real"
    else:
        ident = ""
    return f"{name}:{ident}"


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
