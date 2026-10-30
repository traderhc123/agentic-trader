"""Broker adapter: Alpaca (paper or live).

Simple key-pair auth, real options trading, and a NATIVE PAPER MODE — the
safest way to trial this agent end-to-end with zero real dollars (paper keys
from a free alpaca.markets account; flip to live keys later).

Orders use Alpaca's /v2/orders with the raw OCC option symbol
(e.g. SPY260710C00752000), market, day — long single-leg only, matching the
agent's contract: buy-to-open on ENTERED, sell on EXITED for positions this
agent opened. Requires options approval on the Alpaca account.
"""

import re
import time

import requests

import broker_events

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
    """Per-share quote for the agent's P&L estimate (0.0 when unavailable)."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    return _quote_price(cfg, _occ_symbol(event))


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


def _order(cfg, occ, side, qty):
    resp = requests.post(f"{_base(cfg)}/v2/orders", headers=_headers(cfg),
                         json={"symbol": occ, "qty": str(int(qty)),
                               "side": side, "type": "market",
                               "time_in_force": "day"},
                         timeout=_TIMEOUT)
    if resp.status_code not in (200, 201):
        print(f"  ORDER FAILED HTTP {resp.status_code}: {resp.text[:200]}")
        broker_events.report("order_failed" if side == "buy" else "exit_failed",
                             f"Alpaca rejected the {side} order (HTTP "
                             f"{resp.status_code}): {resp.text[:300]}")
        return None
    oid = resp.json().get("id", "submitted")
    print(f"  alpaca order {side} x{qty} -> {oid}")
    return oid


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


def _confirm_fill(cfg, oid, qty):
    """Poll the order briefly; returns the filled quantity.

    Market DAY orders on options nearly always fill in seconds (paper mode
    instantly). A rejected/cancelled order returns its partial fill (0 = no
    phantom position recorded). An order still working after the polls is
    CANCELLED and settled with what actually filled — assuming the requested
    qty is how ledger/broker divergence starts (with a post-cancel recheck
    for the fill-vs-cancel race)."""
    import time
    if oid == "submitted":
        return qty
    for _ in range(_CONFIRM_TRIES):
        time.sleep(_CONFIRM_WAIT_S)
        status, filled = _order_state(cfg, oid)
        if status == "filled":
            return filled or qty
        if status in ("rejected", "canceled", "cancelled", "expired"):
            print(f"  order {status} — filled {filled}/{qty}")
            return filled
    try:
        requests.delete(f"{_base(cfg)}/v2/orders/{oid}",
                        headers=_headers(cfg), timeout=_TIMEOUT)
        time.sleep(2)  # let the cancel settle; catch a fill-vs-cancel race
        status, filled = _order_state(cfg, oid)
        if status == "filled":
            return filled or qty
        print(f"  order didn't fill in "
              f"{_CONFIRM_TRIES * _CONFIRM_WAIT_S:.0f}s — cancelled; "
              f"filled {filled}/{qty}")
        return filled
    except Exception:
        # cancel path itself failed — fall back to the optimistic assumption
        print(f"  order still working and cancel failed — assuming {qty}x "
              "(check your Alpaca dashboard)")
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
        qty, note = _size(cfg, occ)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            broker_events.report(
                "skip_budget" if "exceeds your" in note else
                "skip_no_quote" if "no quote" in note else "skip_sizing", note)
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        oid = _order(cfg, occ, "buy", qty)
        if oid:
            filled = _confirm_fill(cfg, oid, qty)
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
        if not pos:
            return False
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        oid = _order(cfg, pos.get("option_id", occ), "sell", pos["qty"])
        if oid:
            sold = _confirm_fill(cfg, oid, pos["qty"])
            remaining = int(pos["qty"]) - sold
            if remaining > 0:
                pos["qty"] = remaining
                _stamp_order(pos, oid)
                print(f"  ⚠️ exit only filled {sold}/{pos['qty'] + sold} — "
                      f"{remaining}x still held; close manually if needed")
                broker_events.report("exit_partial",
                                     f"exit filled {sold}/{pos['qty'] + sold} — "
                                     f"{remaining}x {contract} still held")
            else:
                del state["positions"][pos_key]
            return True
    return False


_ORDER_IDS_KEPT = 20


def _stamp_order(pos, oid):
    """Remember the agent's own order (so the owner's sells can be told
    apart from it) and when it was placed (holdings lag fresh orders)."""
    if oid and oid != "submitted":
        ids = [i for i in pos.get("order_ids") or [] if i != str(oid)]
        pos["order_ids"] = (ids + [str(oid)])[-_ORDER_IDS_KEPT:]
    pos["last_order_at"] = time.time()


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
