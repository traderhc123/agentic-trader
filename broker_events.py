"""Why a trade didn't happen — so the owner can see it.

Broker adapters call `report(kind, message)` wherever they drop a trade
(order rejected, budget too small for one contract, no quote, contract not
found, order never filled). The agent collects these around each broker call
and writes them to the trade log the dashboard shows, with one notification
per problem kind per day. Before this, those cases only reached the console,
so "polling but never trading" had no visible cause.

Adapters that never call `report` still work: the agent falls back to a
generic "the broker didn't place the order" entry.
"""
import re

_events = []

# kind -> plain-English label (the dashboard keeps its own copy for display)
KINDS = {
    "order_failed": "the broker rejected the order",
    "skip_budget": "one contract costs more than your per-trade budget",
    "skip_no_quote": "no price quote for the contract",
    "skip_no_contract": "the option contract couldn't be found at the broker",
    "entry_unfilled": "the order was placed but never filled",
    "exit_failed": "the exit order didn't go through",
    "exit_partial": "the exit only partly filled",
    "broker_no_order": "the broker didn't place the order",
    "broker_disconnected": "the broker connection is down (login expired "
                           "or the broker couldn't be reached)",
    "broker_lookup_failed": "the broker returned an error looking up the "
                            "contract",
}

# Problems worth a notification (once per kind per day). Intentional skips —
# caps, dry-run — are logged only.
NOTIFY = {"order_failed", "skip_budget", "skip_no_quote", "skip_no_contract",
          "entry_unfilled", "exit_failed", "exit_partial", "broker_no_order",
          "skip_stale", "skip_sizing", "broker_disconnected",
          "broker_lookup_failed"}


def report(kind, message):
    """Called by broker adapters. Never raises."""
    try:
        _events.append({"kind": str(kind), "message": str(message)[:400]})
    except Exception:
        pass


def begin():
    _events.clear()


def drain():
    out = list(_events)
    _events.clear()
    return out


_HINTS = (
    (re.compile(r"buying.?power|insufficient|not enough|funds|cash", re.I),
     "Not enough buying power — add cash to the broker account or lower your "
     "per-trade budget."),
    (re.compile(r"option.{0,40}(approv|level|enabled|permission|not allowed)|"
                r"(approv|level).{0,20}option", re.I),
     "Options trading isn't approved on this account — apply in your broker's "
     "app."),
    (re.compile(r"401|403|unauthori[sz]ed|forbidden|token|expired|log ?in", re.I),
     "The broker login looks expired — reconnect it in Settings → Broker."),
    (re.compile(r"market.{0,10}closed|outside.{0,20}hours|halt", re.I),
     "The market or this contract wasn't trading at that moment."),
    (re.compile(r"rate.?limit|throttl|too many requests|\b429\b", re.I),
     "Robinhood was rate-limiting requests at that moment (usually brief). "
     "The agent already retried once."),
)


def hint(message):
    """A one-line fix for a broker error message, or ''."""
    for rx, text in _HINTS:
        if rx.search(message or ""):
            return text
    return ""


def is_cash_problem(message):
    return bool(_HINTS[0][0].search(message or ""))
