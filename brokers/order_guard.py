"""One signal, one order.

An order request that gets no clear answer — a timeout, a dropped
connection, a gateway error — may or may not have reached the broker.
Sending it again is how an account ends up holding twice what the agent's
ledger shows, with nothing ever selling the extra contracts (and a repeated
plain sell can open a short). The adapters use this module so that can't
happen:

  * order_key(): the id sent with every order (Alpaca client_order_id,
    Robinhood ref_id). It is derived from the signal, so the same signal
    always sends the same id and the broker can turn a repeat down — even
    after the agent lost its memory (killed mid-order). A sell that got a
    clear answer (accepted or refused) is over: the next sell for that
    signal is a new order with the next id (the position row's "sell_seq").

  * state["unconfirmed_orders"][pos_key]: an order whose request got no
    clear answer and that the broker couldn't show right away. While it is
    there the adapter looks the order up before doing anything else with
    that contract, and adopts it when it exists (a buy adopted on a later
    loop is queued in state["adopted_buys"]; the agent logs it as the entry
    it is, so the daily entry cap counts it). A BUY is never sent again
    (the signal is recorded as handled and the owner is told to check the
    broker app); a SELL goes out again only once the broker says the first
    one isn't there — or, at a broker whose sells can only close a position,
    when it can't be looked up.
"""

import hashlib
import time
import uuid
from datetime import datetime, timezone

STATE_KEY = "unconfirmed_orders"
ADOPTED_KEY = "adopted_buys"  # buys found on a later loop, for the agent to log
BUY_KEEP_S = 600          # a buy the broker doesn't list: keep looking this long
GIVE_UP_S = 24 * 3600     # ...and this long while the broker can't be asked
SKEW_S = 900              # this computer's clock vs the broker's, generously

_NAMESPACE = uuid.UUID("6f1d3a52-8c0e-4b7e-9d53-2a4c1e7b9f10")


def order_key(account, event, side, seq=0):
    """The id sent with the order this signal places: the same for every
    attempt of the same order. Without an event id (an order the adapter
    re-places itself) there is nothing to derive it from: a fresh id.

    It has the form of the random (version 4) UUID the agent has always
    sent, so a broker that checks the form sees nothing new."""
    event_id = (event or {}).get("event_id")
    if not event_id:
        return str(uuid.uuid4())
    name = f"{_NAMESPACE}|{account}|{event_id}|{side}|{int(seq)}"
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def sell_seq(pos):
    """How many sells of this position already got a clear answer."""
    try:
        return max(0, int((pos or {}).get("sell_seq") or 0))
    except (TypeError, ValueError):
        return 0


def unconfirmed(state):
    """{pos_key: note} of the orders whose outcome is still unknown."""
    notes = state.get(STATE_KEY)
    return notes if isinstance(notes, dict) else {}


def get(state, pos_key):
    """The note for pos_key, or None. An entry that isn't one (a damaged
    state file) is removed."""
    notes = unconfirmed(state)
    rec = notes.get(pos_key)
    if rec is not None and not isinstance(rec, dict):
        drop(state, pos_key)
        return None
    return rec


def note(state, pos_key, side, key, qty, option_id, **more):
    """Remember an order whose request got no clear answer."""
    notes = state.get(STATE_KEY)
    if not isinstance(notes, dict):
        notes = state[STATE_KEY] = {}
    rec = {"side": side, "key": key, "qty": int(qty), "option_id": option_id,
           "at": time.time()}
    rec.update({k: v for k, v in more.items() if v is not None})
    notes[pos_key] = rec
    return rec


def drop(state, pos_key):
    notes = state.get(STATE_KEY)
    if isinstance(notes, dict):
        notes.pop(pos_key, None)
        if not notes:
            state.pop(STATE_KEY, None)


def adopted_buy(state, pos_key, rec, order_id):
    """A buy whose request got no clear answer (note `rec`) was found at
    the broker on a later loop. Queued for the agent, which logs it as the
    entry it is (agent._log_adopted_buys): the daily entry cap and the P&L
    estimate are counted from the trade log. A note with "replaces" set is
    an order the adapter re-placed for an entry that is in the log already
    ("logged": no second entry row)."""
    queue = state.get(ADOPTED_KEY)
    if not isinstance(queue, list):
        queue = state[ADOPTED_KEY] = []
    queue.append({"key": pos_key, "event_id": rec.get("event_id"),
                  "order_id": str(order_id), "at": rec.get("at"),
                  "logged": bool(rec.get("replaces"))})


def take_adopted(state):
    """The queued buys (adopted_buy), removed from state."""
    queue = state.pop(ADOPTED_KEY, None)
    if not isinstance(queue, list):
        return []
    return [r for r in queue if isinstance(r, dict) and isinstance(r.get("key"), str)]


def age(rec, now=None):
    try:
        return (time.time() if now is None else now) - float(rec.get("at") or 0)
    except (TypeError, ValueError):
        return float("inf")


def epoch(iso):
    """Broker timestamp ("2026-10-08T17:27:05.074736Z", any number of
    fraction digits) -> epoch seconds, or None when it can't be read."""
    try:
        text = str(iso).strip().replace("Z", "+00:00")
        head, dot, rest = text.partition(".")
        if dot:  # python 3.10 only takes 3 or 6 fraction digits
            n = 0
            while n < len(rest) and rest[n].isdigit():
                n += 1
            text = f"{head}.{rest[:n][:6].ljust(6, '0')}{rest[n:]}"
        made = datetime.fromisoformat(text)
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        return made.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def from_this_attempt(order_id, created_iso, sent_at, known_ids):
    """True when an order found at the broker can be the one a request sent
    at `sent_at` left there: not an order the ledger already has, and not
    placed long before the request went out. SKEW_S allows for clock skew,
    and for an agent restarted mid-order that sends the same signal again
    minutes later. (An OLDER order under the same id means the broker
    refused the new request as a repeat of it; taking that one over again
    would book contracts the account may no longer hold.)"""
    if not order_id or str(order_id) in {str(i) for i in known_ids or ()}:
        return False
    made = epoch(created_iso)
    try:
        return made is not None and made >= float(sent_at) - SKEW_S
    except (TypeError, ValueError):
        return False


def lost_text(broker_name, side, qty, contract):
    """Plain words for "we don't know whether this order went through"."""
    return (f"the connection to {broker_name} failed while the agent was sending "
            f"the {side} order for {qty}x {contract}, so it can't tell whether "
            "the order went through.")
