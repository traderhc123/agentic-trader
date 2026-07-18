"""Broker adapter: Robinhood agentic-trading MCP.

Places long single-leg options orders (buy-to-open / sell-to-close, market,
good-for-day) in the user's dedicated Robinhood Agentic account. During the
9:30-9:35 AM ET open, where Robinhood rejects market orders, orders go out
as marketable limits instead. The account must be agentic-enabled,
options-approved, and funded — the setup wizard checks and explains each.
"""

import os
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .robinhood_mcp import RobinhoodMCP, content_json, tool_ok

MARKET_TZ = ZoneInfo("America/New_York")

# Order-state vocabulary + fill fields verified live against the agentic
# account 2026-07-17 (same MCP schema the AgentHC production mirror uses).
_FILLED = ("filled",)
_DEAD = ("cancelled", "canceled", "rejected", "failed", "expired", "voided")
_SETTLE_TRIES = 3           # quick inline polls right after placing
_SETTLE_WAIT_S = 5.0
_PENDING_GIVEUP_S = 30 * 60  # cancel + settle any order still working after this

# Robinhood rejects market option orders outside 9:35 AM-4:00 PM ET. Feed
# events consumed just after the open can land inside the 9:30-9:35 window,
# so orders firing before this cutoff go out as marketable limit orders
# instead; the 5s pad covers clock skew.
_OPENING_BLACKOUT_END = (9, 35, 5)


def _in_opening_blackout(now=None):
    now = now or datetime.now(MARKET_TZ)
    return (now.hour, now.minute, now.second) < _OPENING_BLACKOUT_END


def _token_path():
    home = os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))
    return os.path.join(home, "robinhood_oauth.json")


def client(cfg):
    rh = RobinhoodMCP(_token_path())
    return rh if rh.is_authenticated() else None


def setup(cfg):
    """Interactive OAuth + Agentic-account discovery + options-level check."""
    rh = RobinhoodMCP(_token_path())
    if not rh.is_authenticated():
        url, pending = rh.auth_start()
        print("\n1. Open this URL in your browser (logged into Robinhood) and approve:")
        print(f"\n   {url}\n")
        print("2. You'll land on a dead http://127.0.0.1:8721/... page — expected.")
        print("   Copy the FULL URL from the address bar. Codes expire in minutes.")
        redirect = input("\nPaste the full redirect URL: ").strip()
        rh.auth_finish(pending, redirect)
        print("Robinhood tokens stored (0600).")

    payload = content_json(rh.call_tool("get_accounts", {})) or {}
    agentic = None
    for acct in ((payload.get("data") or {}).get("accounts") or []):
        num = str(acct.get("account_number", ""))
        print(f"  account ••••{num[-4:]} type={acct.get('type')} "
              f"agentic_allowed={acct.get('agentic_allowed')} "
              f"option_level={acct.get('option_level') or 'NONE'}")
        if acct.get("agentic_allowed") and acct.get("state") == "active":
            agentic = acct
    if not agentic:
        print("\nNo agentic-allowed account found. Finish Agentic-account onboarding")
        print("in the Robinhood app, then rerun setup.")
        sys.exit(1)
    cfg["robinhood_account"] = str(agentic["account_number"])
    print(f"\nAgentic account ••••{cfg['robinhood_account'][-4:]} selected.")
    if not agentic.get("option_level") or agentic.get("option_level") == "option_level_0":
        print("⚠️  Options are NOT enabled on this account — orders will be rejected")
        print("   until approved. Apply: https://applink.robinhood.com/upgrade_options"
              f"?account_number={cfg['robinhood_account']}")
    return cfg


def resolve_instrument(rh, event):
    # NO tradability filter: since ~2026-07-17 the server returns ZERO rows
    # when tradability is passed (even though every row IS tradable) — it
    # silently killed every entry in AgentHC's production mirror until the
    # filter was dropped there. Keep parity with that verified behavior.
    result = rh.call_tool("get_option_instruments", {
        "chain_symbol": event["ticker"],
        "expiration_dates": event["expiry"],
        "strike_price": f"{float(event['strike']):.4f}",
        "type": "call" if event["type"] == "C" else "put",
        "state": "active",
    })
    payload = content_json(result)

    def find_id(obj):
        if isinstance(obj, dict):
            if obj.get("id") and (obj.get("chain_symbol") or obj.get("strike_price")):
                return str(obj["id"])
            for v in obj.values():
                f = find_id(v)
                if f:
                    return f
        elif isinstance(obj, list):
            for v in obj:
                f = find_id(v)
                if f:
                    return f
        return ""

    return find_id(payload) if payload else ""


def _find_number(obj, keys):
    """Walk a payload for the first parseable number under any of `keys`."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                try:
                    v = float(obj[k])
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
        for v in obj.values():
            found = _find_number(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_number(v, keys)
            if found:
                return found
    return 0.0


def quote_price(rh, option_id, side="buy"):
    """Approximate fill price per share: ask for buys, bid for sells (falls
    back to mark). 0.0 when no quote is available."""
    result = rh.call_tool("get_option_quotes", {"instrument_ids": [option_id]})
    payload = content_json(result)
    if payload is None:
        return 0.0
    keys = (["ask_price", "adjusted_mark_price", "mark_price"] if side == "buy"
            else ["bid_price", "adjusted_mark_price", "mark_price"])
    return _find_number(payload, keys)


def _blackout_limit_price(rh, option_id, side):
    """Marketable-limit price (per share) during the opening blackout; None
    once market orders are allowed. Buys pay up to ask+5% (rounded up to a
    $0.05 tick); sells accept bid-5% (rounded down, floor $0.01) — aggressive
    enough to fill immediately, like the market order it replaces. With no
    usable quote the caller falls through to a market order, whose rejection
    is printed by place()."""
    if not _in_opening_blackout():
        return None
    price = quote_price(rh, option_id, side=side)
    if price <= 0:
        return None
    cents = int(round(price * 100))
    if side == "buy":
        buffered = -(-cents * 105 // 100)      # ceil(price * 1.05)
        ticked = -(-buffered // 5) * 5         # round UP to a $0.05 tick
    else:
        buffered = cents * 95 // 100           # floor(price * 0.95)
        ticked = max(1, buffered // 5 * 5)     # round DOWN, floor $0.01
    return ticked / 100.0


def size_contracts(rh, cfg, option_id):
    """How many contracts to buy, per the user's sizing config.

    sizing_mode "contracts": fixed contracts_per_trade.
    sizing_mode "budget": floor(budget_per_trade_usd / (price*100)); if even
    one contract exceeds the budget, SKIP (respecting the budget beats
    forcing a trade). Returns (qty, note) — qty 0 means skip.
    """
    if str(cfg.get("sizing_mode", "contracts")) == "budget":
        budget = float(cfg.get("budget_per_trade_usd", 0) or 0)
        if budget <= 0:
            return 0, "budget mode but budget_per_trade_usd unset — skipped"
        price = quote_price(rh, option_id)
        if price <= 0:
            return 0, "no quote available for budget sizing — skipped"
        qty = int(budget // (price * 100))
        if qty < 1:
            return 0, (f"1 contract ≈ ${price * 100:,.0f} exceeds your "
                       f"${budget:,.0f} per-trade budget — skipped")
        qty = min(qty, int(cfg.get("max_contracts_per_trade", 25)))
        return qty, f"${budget:,.0f} budget @ ~${price:.2f} → {qty} contract(s)"
    return max(1, int(cfg.get("contracts_per_trade", 1))), "fixed contract count"


def _extract_order_id(result):
    """Walk the tool payload for an order id (`order_id` or `id`) — the
    wrapper shape varies, and without a real id fills can't be verified."""
    body = content_json(result)

    def _find(obj):
        if isinstance(obj, dict):
            oid = obj.get("order_id") or obj.get("id")
            if oid:
                return str(oid)
            for v in obj.values():
                f = _find(v)
                if f:
                    return f
        elif isinstance(obj, list):
            for v in obj:
                f = _find(v)
                if f:
                    return f
        return ""

    return _find(body) if body else ""


def _order_status(rh, account, order_id):
    """(state, filled_qty) for order_id via get_option_orders; (None, 0) when
    it can't be found / the call fails — caller keeps polling. Orders carry
    `state` + string-decimal `processed_quantity`."""
    try:
        body = content_json(rh.call_tool("get_option_orders",
                                         {"account_number": account})) or {}

        def _find(obj):
            if isinstance(obj, dict):
                if str(obj.get("id")) == str(order_id):
                    return obj
                for v in obj.values():
                    hit = _find(v)
                    if hit is not None:
                        return hit
            elif isinstance(obj, list):
                for v in obj:
                    hit = _find(v)
                    if hit is not None:
                        return hit
            return None

        order = _find(body)
        if not order:
            return None, 0
        st = str(order.get("state") or order.get("status") or "").lower()
        try:
            filled = int(float(order.get("processed_quantity") or 0))
        except (TypeError, ValueError):
            filled = 0
        return st, filled
    except Exception:
        return None, 0


def _cancel_order(rh, account, order_id):
    try:
        return tool_ok(rh.call_tool("cancel_option_order", {
            "account_number": account, "order_id": str(order_id)}))
    except Exception:
        return False


def place(rh, cfg, option_id, side, effect, qty):
    """Submit the order. Returns {"order_id": ..., "was_limit": bool} or None."""
    limit_price = _blackout_limit_price(rh, option_id, side)
    args = {
        "account_number": cfg["robinhood_account"],
        "legs": [{"option_id": option_id, "side": side, "position_effect": effect}],
        "quantity": str(int(qty)),
        "type": "market" if limit_price is None else "limit",
        "time_in_force": "gfd",
        "ref_id": str(uuid.uuid4()),
    }
    if limit_price is not None:
        # Schema: price (per-share premium) required for limit, omitted for market.
        args["price"] = f"{limit_price:.2f}"
        print(f"  opening blackout (pre-9:35 ET) — {side} placed as "
              f"marketable limit @ ${limit_price:.2f}")
    result = rh.call_tool("place_option_order", args)
    if not tool_ok(result):
        print(f"  ORDER FAILED: {str(result)[:300]}")
        return None
    oid = _extract_order_id(result)
    print(f"  order {side}/{effect} x{qty} -> {oid or 'submitted (no id)'}")
    return {"order_id": oid, "was_limit": limit_price is not None}


def _finalize_pending(state, pos_key, filled_total):
    """Terminal order state reached: reconcile the ledger with what actually
    filled, so an unfilled buy never leaves a phantom position and a partial
    exit keeps the remainder."""
    pos = state["positions"].get(pos_key)
    if not pos:
        return
    pend = pos.pop("pending", None)
    if not pend:
        return
    filled_total = int(filled_total)
    if pend["side"] == "buy":
        # ADD to qty: a blackout-limit partial banked into qty at conversion
        # must not be overwritten by the follow-up market order's own fill.
        total = int(pos.get("qty", 0)) + filled_total
        if total > 0:
            pos["qty"] = total
            print(f"  fill verified: bought {total}x ({pos_key})")
        else:
            del state["positions"][pos_key]
            print(f"  entry never filled — no position recorded ({pos_key})")
    else:
        remaining = int(pos.get("qty", 0)) - filled_total
        if remaining <= 0:
            del state["positions"][pos_key]
            print(f"  fill verified: sold {filled_total}x ({pos_key})")
        else:
            pos["qty"] = remaining
            print(f"  ⚠️ exit only filled {filled_total}/{pend['requested']} — "
                  f"{remaining}x still held ({pos_key}); reconcile keeps working "
                  "it, or close manually in the app")


def _record_pending(state, pos_key, placed, side, qty):
    state["positions"][pos_key]["pending"] = {
        "order_id": placed["order_id"], "side": side, "requested": int(qty),
        "filled": 0, "was_limit": bool(placed["was_limit"]),
        "placed_at": time.time(),
    }


def _settle_fast(rh, cfg, state, pos_key):
    """Quick inline polls right after placing — market orders usually fill in
    seconds. Anything still working is left pending for reconcile()."""
    pos = state["positions"].get(pos_key)
    pend = (pos or {}).get("pending")
    if not pend or not pend["order_id"]:
        return
    for _ in range(_SETTLE_TRIES):
        time.sleep(_SETTLE_WAIT_S)
        st, filled = _order_status(rh, cfg.get("robinhood_account", ""),
                                   pend["order_id"])
        pend["filled"] = max(pend["filled"], filled)
        if st in _FILLED:
            _finalize_pending(state, pos_key,
                             pend["filled"] or pend["requested"])
            return
        if st in _DEAD:
            _finalize_pending(state, pos_key, pend["filled"])
            return


def reconcile(rh, cfg, state, save_state):
    """Progress pending (unverified) orders — called once per poll cycle by
    the agent loop. A recorded position is not trusted until its order reaches
    a terminal state: blackout limits convert to market once the window ends,
    and orders that die unfilled remove/adjust the position instead of leaving
    a phantom (a position the ledger has but the account doesn't)."""
    account = cfg.get("robinhood_account", "")
    changed = False
    for pos_key, pos in list(state.get("positions", {}).items()):
        pend = pos.get("pending")
        if not pend:
            continue
        if not pend.get("order_id"):
            # can't ever verify — assume the request as placed (legacy behavior)
            _finalize_pending(state, pos_key, pend["requested"])
            changed = True
            continue
        st, filled = _order_status(rh, account, pend["order_id"])
        pend["filled"] = max(pend.get("filled", 0), filled)
        expired = time.time() - pend.get("placed_at", 0) > _PENDING_GIVEUP_S
        if st in _FILLED:
            _finalize_pending(state, pos_key, pend["filled"] or pend["requested"])
            changed = True
        elif st in _DEAD:
            _finalize_pending(state, pos_key, pend["filled"])
            changed = True
        elif st is None:
            if expired:  # unfindable for 30 min — settle with what we saw fill
                _finalize_pending(state, pos_key, pend["filled"])
                changed = True
        else:  # order is working
            convert = pend.get("was_limit") and not _in_opening_blackout()
            if not (convert or expired):
                continue
            if not _cancel_order(rh, account, pend["order_id"]):
                continue  # cancel refused (may be mid-fill) — re-poll next cycle
            time.sleep(2)  # let the cancel settle; catch a fill-vs-cancel race
            st2, filled2 = _order_status(rh, account, pend["order_id"])
            pend["filled"] = max(pend["filled"], filled2)
            remaining = pend["requested"] - pend["filled"]
            if st2 in _FILLED or remaining <= 0 or expired:
                _finalize_pending(state, pos_key,
                                  pend["filled"] or (pend["requested"]
                                                     if st2 in _FILLED else 0))
                changed = True
                continue
            # Bank what the dying order already filled BEFORE re-pending, so
            # the follow-up order's finalize only accounts for its own fills.
            already = pend["filled"]
            if already:
                if pend["side"] == "buy":
                    pos["qty"] = int(pos.get("qty", 0)) + already
                else:
                    pos["qty"] = max(0, int(pos.get("qty", 0)) - already)
                pend["filled"] = 0
            # blackout over: re-place the remainder as MARKET (guaranteed fill)
            replaced = place(rh, cfg, pos["option_id"], pend["side"],
                             "open" if pend["side"] == "buy" else "close",
                             remaining)
            if replaced:
                _record_pending(state, pos_key, replaced, pend["side"], remaining)
                print(f"  blackout limit converted to market x{remaining} "
                      f"({pos_key})")
            else:
                _finalize_pending(state, pos_key, 0)
            changed = True
    if changed:
        save_state(state)


def execute(rh, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    if event["event"] == "ENTERED":
        if pos_key in state["positions"]:
            return False
        option_id = resolve_instrument(rh, event)
        if not option_id:
            print(f"  could not resolve instrument for {contract} — skipped")
            return False
        qty, note = size_contracts(rh, cfg, option_id)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note} — your configuration)")
        placed = place(rh, cfg, option_id, "buy", "open", qty)
        if placed:
            state["positions"][pos_key] = {"option_id": option_id, "qty": 0,
                                           "opened_event": event.get("event_id")}
            _record_pending(state, pos_key, placed, "buy", qty)
            _settle_fast(rh, cfg, state, pos_key)
            return True
    elif event["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if not pos:
            return False  # we never opened this one
        pend = pos.get("pending")
        if pend and pend["side"] == "buy":
            # Fast exit while the entry is still working: cancel the buy
            # first so a late fill can never leave an orphaned long.
            _cancel_order(rh, cfg.get("robinhood_account", ""),
                          pend["order_id"])
            time.sleep(2)
            _, filled = _order_status(rh, cfg.get("robinhood_account", ""),
                                      pend["order_id"])
            _finalize_pending(state, pos_key,
                              max(filled, pend.get("filled", 0)))
            pos = state["positions"].get(pos_key)
            if not pos:
                print(f"  entry never filled — nothing to sell ({contract})")
                return True
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        placed = place(rh, cfg, pos["option_id"], "sell", "close", pos["qty"])
        if placed:
            _record_pending(state, pos_key, placed, "sell", pos["qty"])
            _settle_fast(rh, cfg, state, pos_key)
            return True
    return False
