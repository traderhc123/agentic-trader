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
            return (min(qty, int(cfg.get("max_contracts_per_trade", 25))),
                    f"${budget:,.0f} budget @ ~${price:.2f} → {qty}x")
        if budget > 0:
            return (max(1, int(cfg.get("contracts_per_trade", 1))),
                    "no quote — fell back to fixed contracts")
    return max(1, int(cfg.get("contracts_per_trade", 1))), "fixed contract count"


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


def execute(cl, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
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
        if _order(cfg, occ, "buy", qty):
            state["positions"][pos_key] = {"option_id": occ, "qty": qty,
                                           "opened_event": event.get("event_id")}
            return True
    elif event["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if not pos:
            return False
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        if _order(cfg, pos.get("option_id", occ), "sell", pos["qty"]):
            del state["positions"][pos_key]
            return True
    return False
