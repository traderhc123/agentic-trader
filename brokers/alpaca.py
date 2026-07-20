"""Broker adapter: Alpaca (paper or live).

Simple key-pair auth, real options trading, and a NATIVE PAPER MODE — the
safest way to trial this agent end-to-end with zero real dollars (paper keys
from a free alpaca.markets account; flip to live keys later).

Orders use Alpaca's /v2/orders with the raw OCC option symbol
(e.g. SPY260710C00752000), market, day — long single-leg only, matching the
agent's contract: buy-to-open on ENTERED, sell on EXITED for positions this
agent opened. Requires options approval on the Alpaca account.
"""

import requests

_TIMEOUT = 15

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


def _size(cfg, occ):
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
    return max(1, int(cfg.get("contracts_per_trade", 1))), "fixed contract count"


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
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        oid = _order(cfg, occ, "buy", qty)
        if oid:
            filled = _confirm_fill(cfg, oid, qty)
            if filled < 1:
                print(f"  entry never filled — no position recorded ({contract})")
                return False
            state["positions"][pos_key] = {"option_id": occ, "qty": filled,
                                           "opened_event": event.get("event_id")}
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
                print(f"  ⚠️ exit only filled {sold}/{pos['qty'] + sold} — "
                      f"{remaining}x still held; close manually if needed")
            else:
                del state["positions"][pos_key]
            return True
    return False
