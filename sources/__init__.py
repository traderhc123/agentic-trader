"""Signal sources — pluggable inputs for the agentic trader.

EVENT CONTRACT — every source's ``poll(cfg, state)`` returns a list of dicts:

    {
      "event": "ENTERED" | "EXITED",   # declarative FACT, never an instruction
      "ticker": "SPY",
      "expiry": "2026-07-10",           # YYYY-MM-DD
      "strike": 752.0,
      "type": "C" | "P",
      "event_id": "<unique, stable string>",
      "occurred_at": "2026-07-10T14:31:05+00:00",  # optional: when it happened
                                        # (ISO-8601 with an offset). An ENTERED
                                        # older than max_event_age_s isn't bought.
      "age_unknown": True,              # optional: no occurred_at and the source
                                        # can't tell how old the entry is —
                                        # an ENTERED like that is never bought
      "paper_pnl_pct": 14.2,            # optional, EXITED only
      "message": "...",                 # optional human-readable line
    }

Sources report facts about what a journal/system/human recorded; the AGENT
decides (per the user's configuration) whether to act. Whether to trade at
all, sizing, and risk are always the user's decisions — see DISCLAIMER.md.

A source module exposes:
    NAME         short id used in config ("source": NAME)
    DESCRIPTION  one line shown in the setup wizard
    setup(cfg) -> cfg     interactive wizard step (may mutate + return cfg)
    poll(cfg, state) -> list[event]   called every heartbeat; must not raise
    had_access() -> bool  optional: False when the last poll couldn't read
                          entry signals (e.g. no feed pass yet)
"""

import math

# ── a signal can never mean "sell to open" ─────────────────────────────────
# The agent buys options to open and sells to close what it bought; nothing
# else (brokers/order_guard.py). The event contract has no way to ask for a
# short, and a source must not invent one: an event of a type other than
# ENTERED / EXITED, an entry that says it sells or writes, or an entry with a
# negative quantity is dropped with a logged reason (never_short_reason).
# A bare "short" is NOT one of those words: feeds use it for a bearish idea
# (a put the agent BUYS), and every entry is a buy to open anyway
# (order_guard.check_order); the filter is only defence in depth.

_SIDE_KEYS = ("side", "order_side", "action", "position_effect",
              "position_intent", "intent", "trade_side")
_SELLISH = {"sell", "sell_to_open", "sell to open", "sto", "sell_short",
            "short_sell", "sellshort", "write", "writes", "open_short"}
_QTY_KEYS = ("qty", "quantity", "contracts", "size", "count")


def never_short_reason(raw, event):
    """Why a raw signal must be ignored because it could mean opening a
    short ("" = it may go on). `event` is its normalized type ("ENTERED",
    "EXITED" or whatever the source sent)."""
    if event not in ("ENTERED", "EXITED"):
        return (f"event type {str(event)[:40]!r} isn't ENTERED or EXITED — the "
                "agent only buys to open and sells to close")
    if event != "ENTERED" or not isinstance(raw, dict):
        return ""
    for k in _SIDE_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip().lower() in _SELLISH:
            return (f"an entry with {k}={v.strip()[:20]!r} would sell to open "
                    "(write an option) — the agent never does that")
    for k in _QTY_KEYS:
        v = raw.get(k)
        if isinstance(v, bool) or v is None:
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(n) or n < 0:
            return (f"an entry with {k}={v!r} (a negative or broken quantity "
                    "means a short) — the agent never sells to open")
    return ""


_LOGGED = set()      # (source, signal, reason) already logged this run
_LOGGED_MAX = 5000


def log_ignored(source, raw, reason):
    """The one line an ignored signal leaves in the log — once per run: a
    feed or commands file shows the same line again on every poll."""
    import hashlib
    import json
    from platform_compat import safe_print
    try:
        seen = json.dumps(raw, sort_keys=True, default=str)
    except (TypeError, ValueError):
        seen = repr(raw)
    key = hashlib.sha256(f"{source}|{seen}|{reason}".encode("utf-8")).hexdigest()
    if key in _LOGGED:
        return
    if len(_LOGGED) >= _LOGGED_MAX:
        _LOGGED.clear()
    _LOGGED.add(key)
    ident = ""
    if isinstance(raw, dict):
        ident = " ".join(str(raw.get(k)) for k in ("ticker", "expiry", "strike", "type")
                         if raw.get(k) is not None)
    safe_print(f"{source}: ignored a signal{(' (' + ident + ')') if ident else ''}"
               f": {reason}")


from . import agenthc_day_trade_ideas, generic_json_url, manual_file  # noqa: E402

SOURCES = {
    agenthc_day_trade_ideas.NAME: agenthc_day_trade_ideas,
    manual_file.NAME: manual_file,
    generic_json_url.NAME: generic_json_url,
}
