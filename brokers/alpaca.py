"""Broker adapter: Alpaca (paper or live).

Simple key-pair auth, real options trading, and a NATIVE PAPER MODE — the
safest way to trial this agent end-to-end with zero real dollars (paper keys
from a free alpaca.markets account; flip to live keys later).

Orders use Alpaca's /v2/orders with the raw OCC option symbol
(e.g. SPY260710C00752000), market, day — long single-leg only, matching the
agent's contract: buy-to-open on ENTERED, sell-to-close on EXITED for
positions this agent opened. Every order carries Alpaca's position_intent
("buy_to_open" / "sell_to_close"), so Alpaca itself refuses a sell that
would open a short; and a sell is never sent for more than the account
holds (_order, order_guard.check_order). Requires options approval on the
Alpaca account.
"""

import math
import re
import time

import requests

import broker_events
from platform_compat import safe_print

from . import order_guard
from .pricing import exit_qty, marketable_limit

# Every status line in this module goes through safe_print: a console that
# can't show a character ("→" on a Windows cp1252 stdout) must never stop an
# order by making print() raise.
print = safe_print

_TIMEOUT = 15
_OCC_RE = re.compile(r"^[A-Z0-9.]{1,6}\d{6}[CP]\d{8}$")

# Wizard descriptor — lets the web setup render this broker's fields generically
# (any key-based broker adapter can ship one of these).
WIZARD = {
    "id": "alpaca",
    "name": "Alpaca (paper or live)",
    "fields": [
        {"id": "alpaca_key_id", "label": "API Key ID", "type": "text"},
        {"id": "alpaca_secret", "label": "API Secret", "type": "password"},
        {"id": "alpaca_paper", "label": "Paper trading (recommended)",
         "type": "checkbox", "default": True},
    ],
}


def connect(cfg, values):
    """Generic wizard connect: apply field values, verify. (ok, note)."""
    cfg["alpaca_key_id"] = str(values.get("alpaca_key_id", "")).strip()
    cfg["alpaca_secret"] = str(values.get("alpaca_secret", "")).strip()
    cfg["alpaca_paper"] = bool(values.get("alpaca_paper", True))
    cfg["broker"] = "alpaca"
    return verify(cfg)


def _base(cfg):
    return ("https://paper-api.alpaca.markets" if cfg.get("alpaca_paper", True)
            else "https://api.alpaca.markets")


def _headers(cfg):
    return {"APCA-API-KEY-ID": cfg.get("alpaca_key_id", ""),
            "APCA-API-SECRET-KEY": cfg.get("alpaca_secret", "")}


class _Client:
    """Thin holder so broker.client(cfg) has the same shape as robinhood's."""

    def __init__(self, cfg):
        self.cfg = cfg


def client(cfg):
    if not (cfg.get("alpaca_key_id") and cfg.get("alpaca_secret")):
        return None
    return _Client(cfg)


def verify(cfg):
    """Returns (ok, message) — account status + options approval level."""
    try:
        resp = requests.get(f"{_base(cfg)}/v2/account", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False, f"Alpaca rejected the keys (HTTP {resp.status_code})"
        acct = resp.json()
        level = acct.get("options_approved_level", 0)
        note = (f"connected ✓ ({'PAPER' if cfg.get('alpaca_paper', True) else 'LIVE'}"
                f", status {acct.get('status')}, options level {level})")
        if not level:
            note += " — ⚠️ options not approved on this account yet"
        return True, note
    except requests.RequestException as exc:
        return False, f"could not reach Alpaca: {exc}"


def setup(cfg):
    print("\n-- Alpaca --")
    print("Keys from alpaca.markets → dashboard → API Keys. Paper keys are the")
    print("safest start (simulated fills, zero real dollars).")
    cfg["alpaca_key_id"] = input("API Key ID: ").strip()
    cfg["alpaca_secret"] = input("API Secret: ").strip()
    paper = input("Paper trading? [Y/n]: ").strip().lower() != "n"
    cfg["alpaca_paper"] = paper
    cfg["broker"] = "alpaca"
    ok, msg = verify(cfg)
    print(msg if ok else f"⚠️  {msg} — fix and re-run setup")
    return cfg


def _occ_symbol(event):
    """Event fields -> raw OCC symbol Alpaca expects."""
    y, m, d = str(event["expiry"]).split("-")
    return (f"{event['ticker']}{y[2:]}{m}{d}{event['type']}"
            f"{int(round(float(event['strike']) * 1000)):08d}")


def _quote_price(cfg, occ):
    """Latest ask for budget sizing; 0.0 when unavailable."""
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/options/quotes/latest",
            params={"symbols": occ}, headers=_headers(cfg), timeout=_TIMEOUT)
        q = (resp.json().get("quotes", {}) or {}).get(occ, {})
        return float(q.get("ap") or 0) or float(q.get("bp") or 0)
    except Exception:
        return 0.0


def mark_price(cl, cfg, event, side="buy"):
    """Per-share quote for the agent's P&L estimate (0.0 when unavailable):
    the ask for a buy, the bid for a sell — a sale fills at the bid, so
    marking it at the ask would understate a loss."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    if side == "sell":
        return _bid(cfg, _occ_symbol(event)) or 0.0
    return _quote_price(cfg, _occ_symbol(event))


def _bid(cfg, occ):
    """Latest bid per share: 0.0 when Alpaca quotes no bid, None when the
    quote can't be read (unknown)."""
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/options/quotes/latest",
            params={"symbols": occ}, headers=_headers(cfg), timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        q = (resp.json().get("quotes", {}) or {}).get(occ)
        if not isinstance(q, dict) or q.get("bp") is None:
            return None
        bp = float(q["bp"])
        return bp if 0 <= bp < float("inf") else None
    except Exception:
        return None


def exit_bid(cl, cfg, event, option_id=None):
    """Best bid per share for the contract the agent is about to sell (the
    expiry guard skips a sell into no bid): 0.0 = no bid, None = unknown."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    occ = option_id if option_id and _OCC_RE.match(str(option_id)) else _occ_symbol(event)
    return _bid(cfg, occ)


def account_value(cl, cfg):
    """Account equity for %-of-account sizing (0.0 when unavailable)."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    try:
        resp = requests.get(f"{_base(cfg)}/v2/account", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return 0.0
        acct = resp.json()
        return float(acct.get("equity") or 0) or float(acct.get("portfolio_value") or 0)
    except Exception:
        return 0.0


def buying_power(cl, cfg):
    """Cash available for option orders, or None when unreadable."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    try:
        resp = requests.get(f"{_base(cfg)}/v2/account", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        acct = resp.json()
        for k in ("options_buying_power", "buying_power", "cash"):
            if acct.get(k) is not None:
                return float(acct[k])
    except Exception:
        pass
    return None


def _size(cfg, occ):
    if str(cfg.get("sizing_mode")) == "percent":
        # The agent turns % of account into a dollar budget before calling
        # the broker; reaching here means that step was skipped. Never fall
        # back to a fixed count the user didn't choose.
        return 0, "%-of-account sizing not resolved to a budget — skipped"
    if str(cfg.get("sizing_mode", "contracts")) == "budget":
        budget = float(cfg.get("budget_per_trade_usd", 0) or 0)
        price = _quote_price(cfg, occ)
        if budget > 0 and price > 0:
            qty = int(budget // (price * 100))
            if qty < 1:
                return 0, (f"1 contract ≈ ${price * 100:,.0f} exceeds your "
                           f"${budget:,.0f} budget — skipped")
            return (min(qty, int(cfg.get("max_contracts_per_trade", 100))),
                    f"${budget:,.0f} budget @ ~${price:.2f} → {qty}x")
        if budget > 0:
            # No quote = no way to honor the budget; skipping beats silently
            # buying an unknown-priced contract (matches the robinhood adapter).
            return 0, "no quote available for budget sizing — skipped"
    return min(max(1, int(cfg.get("contracts_per_trade", 1))),
               int(cfg.get("max_contracts_per_trade", 100))), "fixed contract count"


_CONFIRM_TRIES = 3
_CONFIRM_WAIT_S = 5.0


class _Unconfirmed(Exception):
    """The order request got no clear answer from Alpaca (a timeout, a
    dropped connection, a server error): it may or may not have gone
    through. Never answered by simply sending it again (order_guard).
    `.qty`: the contracts the request was for."""

    def __init__(self, why, qty=None):
        super().__init__(why)
        self.qty = qty


_OrderId = order_guard.OrderId  # an order id with .qty: what it was for


# Position intent per side: Alpaca refuses a sell_to_close of contracts the
# account doesn't hold instead of opening a short (cash-secured puts and
# covered calls are allowed from options level 1).
_INTENT = {("buy", "open"): "buy_to_open", ("sell", "close"): "sell_to_close"}

# Alpaca's answer to a sell_to_close with nothing (or too little) to close,
# e.g. 403 {"code": 40310000, "message": "insufficient qty available for
# order (requested: 1, available: 0)"}.
_NOTHING_TO_CLOSE_RX = re.compile(
    r"insufficient\s+qty|available:\s*0\b|position\s+(does\s+not\s+exist|not\s+found)"
    r"|no\s+(such\s+)?position|not\s+enough\s+(qty|quantity|contracts?)", re.I)


def _sell_holding(cfg, occ):
    """(long contracts of occ the account can sell right now, short
    contracts of occ) from GET /v2/positions — qty_available is the holding
    minus what open sell orders are already selling. (None, None) when the
    account can't be read; a symbol not listed is held 0."""
    try:
        resp = requests.get(f"{_base(cfg)}/v2/positions", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
    except Exception:
        return None, None
    if not isinstance(rows, list):
        return None, None
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            return None, None
        if str(row["symbol"]) != str(occ):
            continue
        try:
            q = float(row.get("qty"))
            avail = row.get("qty_available")
            a = q if avail is None else float(avail)
        except (TypeError, ValueError):
            return None, None
        if not (math.isfinite(q) and math.isfinite(a)):
            return None, None
        if str(row.get("side") or "").lower() == "short" or q < 0:
            return 0, int(round(abs(q)))
        if avail is None:
            return None, 0  # working sells can't be told apart
        return max(0, math.floor(min(q, a))), 0
    return 0, 0


def _order(cfg, occ, side, qty, limit_price=None, client_order_id=None,
           effect=None, contract=None):
    """Submit one order. Returns its id (an _OrderId: .qty is how many
    contracts it was for), or None when it wasn't sent or Alpaca refused it;
    raises _Unconfirmed when there is no clear answer.

    THE ONLY place an Alpaca order is sent. It sends nothing but a
    buy-to-open or a sell-to-close (order_guard.check_order), and a sell
    never for more than Alpaca says the account holds, less what its open
    sells are already selling. When the holdings can't be read the sell
    goes out as before: its position_intent "sell_to_close" makes Alpaca
    refuse it rather than open a short.

    client_order_id: Alpaca accepts each one once per account, so a repeat
    of the same order is refused instead of buying or selling twice (that
    refusal is raised as _Unconfirmed too: the order is there already)."""
    contract = contract or occ
    held = None
    if side == "sell":
        held, short = _sell_holding(cfg, occ)
        if short:
            msg = order_guard.short_text("Alpaca", contract, short)
            print(f"  ORDER NOT SENT: {msg}")
            broker_events.report("exit_failed", msg, reason="short_seen",
                                 short=short, option_id=occ)
            return None
    try:
        qty = order_guard.check_order(side, effect, qty, held,
                                      broker_refuses_open=True)
    except order_guard.RefusedOrder as exc:
        order_guard.report_refusal("Alpaca", side, qty, contract, exc)
        return None
    body = {"symbol": occ, "qty": str(qty), "side": side,
            "type": "market", "time_in_force": "day",
            "position_intent": _INTENT[(side, effect)]}
    if limit_price is not None:
        body.update(type="limit", limit_price=f"{limit_price:.2f}")
    if client_order_id:
        body["client_order_id"] = client_order_id
    try:
        resp = requests.post(f"{_base(cfg)}/v2/orders", headers=_headers(cfg),
                             json=body, timeout=_TIMEOUT)
        status = int(resp.status_code)
    except Exception as exc:
        raise _Unconfirmed(type(exc).__name__, qty) from exc
    if status >= 500:
        raise _Unconfirmed(f"HTTP {status}", qty)
    if status not in (200, 201):
        text = str(getattr(resp, "text", "") or "")
        if client_order_id and "client_order_id" in text:
            # "client_order_id must be unique": an order under this id is
            # already there — which one is for the caller to look up
            raise _Unconfirmed("Alpaca already has an order under this id", qty)
        print(f"  ORDER FAILED HTTP {resp.status_code}: {text[:200]}")
        if side == "sell" and _NOTHING_TO_CLOSE_RX.search(text):
            # a clear answer: the contracts aren't there to close. Not an
            # unknown outcome, and not sold again until a read shows them.
            broker_events.report(
                "exit_failed",
                f"Alpaca says your account doesn't hold {contract} to sell "
                f"(it refused the sell-to-close: {text[:200]}). Nothing was "
                "sold and no short position was opened. The agent checks the "
                "account again before any new sell.", reason="nothing_held")
            return None
        broker_events.report("order_failed" if side == "buy" else "exit_failed",
                             f"Alpaca rejected the {side} order (HTTP "
                             f"{resp.status_code}): {text[:300]}")
        return None
    try:
        oid = resp.json().get("id", "submitted")
    except Exception as exc:  # accepted, but the answer can't be read
        raise _Unconfirmed(type(exc).__name__, qty) from exc
    print(f"  alpaca order {side} x{qty} -> {oid}")
    return _OrderId(oid, qty)


def _order_by_key(cfg, key):
    """The order Alpaca holds under this client_order_id: its row, False
    when Alpaca says there is none, None when it can't be read."""
    try:
        resp = requests.get(f"{_base(cfg)}/v2/orders:by_client_order_id",
                            params={"client_order_id": key},
                            headers=_headers(cfg), timeout=_TIMEOUT)
        if resp.status_code == 404:
            return False
        row = resp.json() if resp.status_code == 200 else None
    except Exception:
        return None
    if isinstance(row, dict) and row.get("id") and row.get("client_order_id") == key:
        return row
    return None


def _account(cfg):
    """Paper and live are separate accounts: their order ids never mix."""
    return "alpaca:paper" if cfg.get("alpaca_paper", True) else "alpaca:live"


def _send(cfg, state, pos_key, event, contract, occ, side, qty, limit=None):
    """Place the order for this signal — once. Returns the order id, or
    None when no order is known to be at Alpaca: it was refused (reported
    by _order), or there was no clear answer and Alpaca doesn't show it
    (remembered in state and reported; see order_guard)."""
    pos = state["positions"].get(pos_key) if side == "sell" else None
    seq = order_guard.sell_seq(pos)
    key = order_guard.order_key(_account(cfg), event, side, seq)
    kw = {"limit_price": limit} if limit is not None else {}
    sent_at = time.time()
    try:
        oid = _order(cfg, occ, side, qty, client_order_id=key,
                     effect="open" if side == "buy" else "close",
                     contract=contract, **kw)
    except _Unconfirmed as exc:
        qty = exc.qty if isinstance(exc.qty, int) else qty
        found = _order_by_key(cfg, key)
        if not found:
            order_guard.note(state, pos_key, side, key, qty, occ,
                             event_id=event.get("event_id"), contract=contract)
            lost = order_guard.lost_text("Alpaca", side, qty, contract)
            print(f"  no answer from Alpaca to the {side} order ({exc}) and it "
                  "isn't listed — NOT sending it again without knowing")
            if side == "buy":
                broker_events.report(
                    "order_failed", lost + " It will NOT send this buy again. "
                    "Check your Alpaca dashboard: if the order is there, the "
                    "agent finds it on its own and manages the position; if it "
                    "isn't, this trade was missed.", placed=True)
            else:
                broker_events.report("exit_failed", lost + _SELL_WAITS, placed=True)
            return None
        oid = _OrderId(found["id"], qty)
        if _ours_now(found, state, sent_at):
            print(f"  no clear answer from Alpaca to the {side} order ({exc}), "
                  f"but the order is there ({oid}) — using it")
        else:
            # an OLDER order holds this id: Alpaca refused this one as a
            # repeat of it, so nothing new went in — and nothing is adopted
            print(f"  ORDER NOT PLACED: Alpaca already has an earlier order for "
                  f"this same signal ({oid})")
            broker_events.report(
                "order_failed" if side == "buy" else "exit_failed",
                "Alpaca already has an earlier order for this same signal, "
                f"so it took no new {side} order for {contract} — "
                "check your Alpaca dashboard", placed=True)
            oid = None
    if pos is not None:
        # Alpaca answered: a later sell for this signal is a new order
        pos["sell_seq"] = seq + 1
    return oid


def _ours_now(order, state, sent_at):
    """True when the order Alpaca holds under an id can be the one a
    request sent at sent_at left there (order_guard.from_this_attempt)."""
    return order_guard.from_this_attempt(
        order.get("id"), order.get("submitted_at") or order.get("created_at"),
        sent_at, state.get("agent_order_ids"))


_SELL_WAITS = (" It will not sell again until Alpaca can tell it (it asks every "
               "loop): a second sell could open a short position. Check your "
               "Alpaca dashboard and close the position there if it must be "
               "closed right now.")


def _settle_unconfirmed(cfg, state, pos_key):
    """Look up an order whose request got no clear answer (order_guard).
    "clear": nothing is waiting (any more) — it isn't at Alpaca;
    "adopted": it is there, and the ledger now has what it filled;
    "unknown": Alpaca can't say yet — never place another order on it."""
    unc = order_guard.get(state, pos_key)
    if not unc:
        return "clear"
    side, qty = unc.get("side"), int(unc.get("qty") or 0)
    pos = state["positions"].get(pos_key)
    contract = unc.get("contract") or pos_key.replace("|", " ")
    if (side == "sell") != bool(pos) or (
            side == "buy" and order_guard.age(unc) > order_guard.GIVE_UP_S):
        order_guard.drop(state, pos_key)  # nothing left it could change
        return "clear"
    found = _order_by_key(cfg, unc.get("key"))
    if found is None:
        return "unknown"
    if found is False:
        if side == "buy" and order_guard.age(unc) < order_guard.BUY_KEEP_S:
            return "unknown"  # not there now; it may still show up late
        order_guard.drop(state, pos_key)
        return "clear"
    oid = str(found["id"])
    order_guard.drop(state, pos_key)
    if not _ours_now(found, state, unc.get("at")):
        # an older order under the same id: the request was refused as a
        # repeat of it. Nothing new is at Alpaca.
        if side == "sell":
            pos["sell_seq"] = order_guard.sell_seq(pos) + 1
        return "clear"
    _remember_order(state, oid)
    print(f"  the {side} order for {contract} did reach Alpaca ({oid}) — using it")
    filled = _confirm_fill(cfg, oid, qty, side=side)
    if side == "sell":
        pos["sell_seq"] = order_guard.sell_seq(pos) + 1
        _book_sale(state, pos_key, pos, oid, filled, qty, contract)
    elif filled < 1:
        print(f"  it never filled — no position recorded ({contract})")
        broker_events.report("entry_unfilled",
                             f"the buy order for {contract} that the agent "
                             "couldn't confirm did reach Alpaca, but never filled")
    else:
        state["positions"][pos_key] = {"option_id": unc.get("option_id"),
                                       "qty": filled,
                                       "opened_event": unc.get("event_id")}
        _stamp_order(state["positions"][pos_key], oid)
        # the agent logs it as the entry it is and tells the owner
        order_guard.adopted_buy(state, pos_key, unc, oid)
    return "adopted"


def reconcile(cl, cfg, state, save_state):
    """Called by the agent once per loop, before new events: looks again
    for buy orders whose request got no clear answer. One that is at Alpaca
    after all becomes the position it bought. (An unconfirmed sell is
    settled by its exit's own retry in execute, so the sale is logged as
    that exit.) No network call unless such an order is waiting."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    changed = False
    for pos_key in list(order_guard.unconfirmed(state)):
        unc = order_guard.get(state, pos_key)
        if (unc and unc.get("side") == "sell"
                and pos_key in state.get("positions", {})):
            continue
        if _settle_unconfirmed(cfg, state, pos_key) != "unknown":
            changed = True
    if changed:
        save_state(state)


def _book_sale(state, pos_key, pos, oid, sold, qty, contract):
    """Take what an exit order sold out of the ledger row."""
    remaining = int(pos["qty"]) - sold
    if remaining > 0:
        pos["qty"] = remaining
        _stamp_order(pos, oid)
        # the agent re-sells the remainder (agent.exit_remainder_tick)
        print(f"  ⚠️ exit only filled {sold}/{qty} — {remaining}x still "
              f"held; re-selling the remaining {remaining}x next loop")
        broker_events.report("exit_partial",
                             f"exit filled {sold}/{qty} — {remaining}x "
                             f"{contract} still held; the agent re-sells "
                             f"the remaining {remaining}x next loop")
    else:
        del state["positions"][pos_key]


def _order_state(cfg, oid):
    """(status, filled_qty) from GET /v2/orders/{id}; ("", 0) on failure."""
    try:
        resp = requests.get(f"{_base(cfg)}/v2/orders/{oid}",
                            headers=_headers(cfg), timeout=_TIMEOUT)
        o = resp.json() if resp.status_code == 200 else {}
    except Exception:
        o = {}
    return (str(o.get("status", "")).lower(),
            int(float(o.get("filled_qty") or 0)))


_DONE = ("rejected", "canceled", "cancelled", "expired")


def _confirm_fill(cfg, oid, qty, side="buy"):
    """Poll the order briefly; returns the filled quantity.

    Market DAY orders on options nearly always fill in seconds (paper mode
    instantly). A rejected/cancelled order returns its partial fill (0 = no
    phantom position recorded). An order still working after the polls is
    CANCELLED and settled with what actually filled — assuming the requested
    qty is how ledger/broker divergence starts (with a post-cancel recheck
    for the fill-vs-cancel race).

    Less than qty only when the order is known to be dead: a sell reported
    as unfilled is sold again on the next poll, and if the first one fills
    after all, a plain sell on a flat account can open a short."""
    if oid == "submitted":
        return qty
    for _ in range(_CONFIRM_TRIES):
        time.sleep(_CONFIRM_WAIT_S)
        status, filled = _order_state(cfg, oid)
        if status == "filled":
            return filled or qty
        if status in _DONE:
            print(f"  order {status} — filled {filled}/{qty}")
            return filled
    try:
        resp = requests.delete(f"{_base(cfg)}/v2/orders/{oid}",
                               headers=_headers(cfg), timeout=_TIMEOUT)
        cancelled = 200 <= int(getattr(resp, "status_code", 0) or 0) < 300
        time.sleep(2)  # let the cancel settle; catch a fill-vs-cancel race
        status, filled = _order_state(cfg, oid)
    except Exception:
        cancelled, status, filled = False, "", 0
    if status == "filled":
        return filled or qty
    if status in _DONE or (cancelled and status):
        print(f"  order didn't fill in "
              f"{_CONFIRM_TRIES * _CONFIRM_WAIT_S:.0f}s — cancelled; "
              f"filled {filled}/{qty}")
        return filled
    # the cancel didn't take or can't be confirmed: the order may still
    # fill — fall back to the optimistic assumption, and say so
    print(f"  order still working and the cancel couldn't be confirmed — "
          f"assuming {qty}x (check your Alpaca dashboard)")
    broker_events.report(
        "order_failed" if side == "buy" else "exit_failed",
        f"Alpaca couldn't confirm whether the {side} order for {qty}x filled "
        "(the cancel didn't go through) — the agent assumes it did; check "
        "your Alpaca dashboard", placed=True)
    return qty


def execute(cl, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    # Prefer the cfg the caller passed (it may carry per-track sizing
    # overrides); the client's stored copy is only a fallback.
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    occ = _occ_symbol(event)
    if event["event"] == "ENTERED":
        if pos_key in state["positions"]:
            return False
        if _settle_unconfirmed(cfg, state, pos_key) == "unknown":
            # an earlier buy of this contract got no clear answer and Alpaca
            # still can't say whether it is there: never buy on top of it
            print(f"SKIPPED {contract}: an earlier buy order for it is still "
                  "unconfirmed — not buying again")
            broker_events.report(
                "order_failed", f"an earlier buy order for {contract} is still "
                "unconfirmed (the agent can't tell whether it went through), so "
                "it did not buy again — check your Alpaca dashboard",
                placed=True)
            return False
        if pos_key in state["positions"]:
            return False  # that earlier buy is the position now
        qty, note = _size(cfg, occ)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            broker_events.report(
                "skip_budget" if "exceeds your" in note else
                "skip_no_quote" if "no quote" in note else "skip_sizing", note)
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        oid = _send(cfg, state, pos_key, event, contract, occ, "buy", qty)
        if oid:
            _remember_order(state, oid)
            filled = _confirm_fill(cfg, oid, getattr(oid, "qty", qty))
            if filled < 1:
                print(f"  entry never filled — no position recorded ({contract})")
                broker_events.report("entry_unfilled",
                                     f"buy order for {contract} was placed but "
                                     "never filled")
                return False
            state["positions"][pos_key] = {"option_id": occ, "qty": filled,
                                           "opened_event": event.get("event_id")}
            _stamp_order(state["positions"][pos_key], oid)
            return True
    elif event["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if order_guard.get(state, pos_key):
            # an order for this contract got no clear answer: find out first
            settled = _settle_unconfirmed(cfg, state, pos_key)
            if settled == "unknown" and not pos:
                # the BUY may be there: keep the exit until that is known
                raise RuntimeError(
                    f"can't tell yet whether the buy order for {contract} went "
                    "through — its exit signal waits until the agent knows")
            if settled == "unknown":
                print(f"  can't tell yet whether the earlier sell of {contract} "
                      "went through — not selling again until Alpaca can say")
                broker_events.report(
                    "exit_failed", f"the agent still can't tell whether its sell "
                    f"order for {contract} went through." + _SELL_WAITS,
                    placed=True)
                return False
            if settled == "adopted" and pos:
                return True  # that sell was there: the ledger has what it sold
            pos = state["positions"].get(pos_key)
        if not pos:
            return False
        qty = exit_qty(pos, event)
        print(f"EXITED event -> selling {qty}x {contract}")
        limit = None
        if event.get("exit_limit") == "marketable":
            # expiry guard / exit remainder: a marketable limit (bid-5%)
            limit = marketable_limit(_bid(cfg, pos.get("option_id", occ)), "sell")
            print(f"  marketable sell — limit @ ${limit:.2f}"
                  if limit is not None else
                  "  marketable sell: no usable bid — placing it as a market order")
        oid = _send(cfg, state, pos_key, event, contract,
                    pos.get("option_id", occ), "sell", qty, limit)
        if oid:
            # a sell is never for more than the account holds: it may be
            # for fewer than the ledger (the rest is settled the usual way)
            qty = getattr(oid, "qty", qty)
            _remember_order(state, oid)
            sold = _confirm_fill(cfg, oid, qty, side="sell")
            _book_sale(state, pos_key, pos, oid, sold, qty, contract)
            return True
    return False


_ORDER_IDS_KEPT = 20
_AGENT_ORDERS_KEPT = 200


def _stamp_order(pos, oid):
    """Remember the agent's own order (so the owner's sells can be told
    apart from it) and when it was placed (holdings lag fresh orders)."""
    if oid and oid != "submitted":
        ids = [i for i in pos.get("order_ids") or [] if i != str(oid)]
        pos["order_ids"] = (ids + [str(oid)])[-_ORDER_IDS_KEPT:]
    pos["last_order_at"] = time.time()


def _remember_order(state, oid):
    """Every order the agent places, kept after its position is gone: the
    agent's sell of an earlier position in the same contract must never
    pass for the owner's sale (brokers/__init__.py)."""
    if oid and oid != "submitted":
        ids = [i for i in state.get("agent_order_ids") or [] if i != str(oid)]
        state["agent_order_ids"] = (ids + [str(oid)])[-_AGENT_ORDERS_KEPT:]


# ── what the account holds, and the owner's own sells (read-only) ───────────
# GET /v2/positions and /v2/orders; option rows carry the same OCC symbol the
# ledger stores. Anything but a clean 200 with the expected shape reads as
# "unknown", never as "closed". See brokers/__init__.py for the contract.

def held_option_qty(cl, cfg, option_ids):
    """{OCC symbol: long contracts held}, or None when unknown. A symbol
    with a working order (qty_available < qty) is left out."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    ids = sorted({str(i) for i in option_ids or () if _OCC_RE.match(str(i))})
    if not ids:
        return {}
    try:
        resp = requests.get(f"{_base(cfg)}/v2/positions", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
        if not isinstance(rows, list):
            return None
        held, busy = {}, set()
        for row in rows:
            if not _count_position(row, set(ids), held, busy):
                return None
        return {i: held.get(i, 0) for i in ids if i not in busy}
    except Exception:
        return None


def short_option_qty(cl, cfg, option_ids):
    """{OCC symbol: SHORT contracts held (0 = none)} for the ids asked
    about, or None when the account can't be read (brokers/__init__.py)."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    ids = sorted({str(i) for i in option_ids or () if _OCC_RE.match(str(i))})
    if not ids:
        return {}
    try:
        resp = requests.get(f"{_base(cfg)}/v2/positions", headers=_headers(cfg),
                            timeout=_TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    out = {i: 0 for i in ids}
    for row in rows:
        if not isinstance(row, dict):
            return None
        sym = str(row.get("symbol") or "")
        if sym not in out:
            continue
        try:
            q = float(row.get("qty"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(q):
            return None
        if str(row.get("side") or "").lower() == "short" or q < 0:
            out[sym] += int(round(abs(q)))
    return out


def _count_position(row, want, held, busy):
    """Add one /v2/positions row to held/busy; False = unrecognized row."""
    if not isinstance(row, dict):
        return False
    if "asset_class" in row and row["asset_class"] != "us_option":
        return True
    sym, qty = row.get("symbol"), row.get("qty")
    if not sym or qty is None:
        return False
    sym = str(sym)
    if str(row.get("side") or "").lower() == "short":
        return True
    try:
        q = int(round(float(qty)))
        avail = row.get("qty_available")
        avail = q if avail is None else int(round(float(avail)))
    except (TypeError, ValueError, OverflowError):
        return sym not in want
    if avail < q:
        busy.add(sym)  # an order is working on it: count unknown
    if q > 0:
        held[sym] = held.get(sym, 0) + q
    return True


def closed_fills(cl, cfg, option_id, since_iso=None, own_order_ids=(),
                 exclude_agentic=False):
    """Filled sells of option_id the agent didn't place, newest first, or
    None when orders can't be read. (Every order here comes through the
    API, so exclude_agentic doesn't apply.)"""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    if not _OCC_RE.match(str(option_id or "")):
        return None
    params = {"status": "closed", "symbols": option_id, "direction": "desc",
              "limit": 100}
    if since_iso:
        params["after"] = since_iso
    try:
        resp = requests.get(f"{_base(cfg)}/v2/orders", params=params,
                            headers=_headers(cfg), timeout=_TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    own = {str(o) for o in own_order_ids or ()}
    out = []
    for o in rows:
        fill = _owner_fill(o, option_id, own)
        if fill:
            out.append(fill)
    return out


def option_fills(cl, cfg, option_id, since_iso=None):
    """Every filled (or partly filled) order on option_id — buys and sells,
    the agent's and the owner's — newest first, or None when orders can't
    be read (brokers/__init__.py)."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    if not _OCC_RE.match(str(option_id or "")):
        return None
    params = {"status": "all", "symbols": option_id, "direction": "desc",
              "limit": 500}
    if since_iso:
        params["after"] = since_iso
    try:
        resp = requests.get(f"{_base(cfg)}/v2/orders", params=params,
                            headers=_headers(cfg), timeout=_TIMEOUT)
        rows = resp.json() if resp.status_code == 200 else None
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for o in rows:
        if not isinstance(o, dict) or not o.get("id") or o.get("symbol") != option_id:
            continue
        side = str(o.get("side") or "").lower()
        try:
            qty = int(round(float(o.get("filled_qty") or 0)))
        except (TypeError, ValueError, OverflowError):
            return None  # a fill we can't count: the whole answer is unknown
        if side in ("buy", "sell") and qty >= 1:
            out.append({"order_id": str(o["id"]), "side": side, "qty": qty,
                        "at": o.get("filled_at") or o.get("updated_at")})
    return out


def _owner_fill(o, option_id, own):
    if not isinstance(o, dict):
        return None
    oid = str(o.get("id") or "")
    if (not oid or oid in own or o.get("symbol") != option_id
            or str(o.get("side") or "").lower() != "sell"
            or str(o.get("position_intent") or "").lower() == "sell_to_open"):
        return None
    try:
        qty = int(round(float(o.get("filled_qty") or 0)))
    except (TypeError, ValueError, OverflowError):
        return None
    if qty < 1:
        return None
    try:
        price = float(o["filled_avg_price"])
    except (KeyError, TypeError, ValueError):
        price = None
    if price is not None and not 0 < price < float("inf"):
        price = None
    return {"order_id": oid, "qty": qty, "price": price,
            "at": o.get("filled_at") or o.get("updated_at"), "by": None}
