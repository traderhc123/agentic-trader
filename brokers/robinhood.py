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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import broker_events

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


def verify(cfg):
    """(ok, message) from a LIVE call to Robinhood. A saved login file is
    not proof the login still works — an expired one looked "connected"
    while every lookup failed (2026-10-21)."""
    rh = RobinhoodMCP(_token_path())
    if not rh.is_authenticated():
        return False, "not connected — no saved Robinhood login"
    try:
        payload = content_json(rh.call_tool("get_accounts", {}))
    except Exception as exc:
        return False, f"can't reach Robinhood ({type(exc).__name__})"
    if payload is None:
        return False, rh.last_error or "Robinhood didn't answer"
    want = str(cfg.get("robinhood_account") or "")
    for acct in ((payload.get("data") or {}).get("accounts") or []):
        num = str(acct.get("account_number", ""))
        if want and num != want:
            continue
        if not (acct.get("agentic_allowed") and acct.get("state") == "active"):
            continue
        note = f"connected ✓ (live check, account ••••{num[-4:]})"
        if not acct.get("option_level") or acct.get("option_level") == "option_level_0":
            note += " — ⚠️ options not approved on this account yet"
        return True, note
    return False, ("logged in, but the agentic account "
                   + (f"••••{want[-4:]} " if want else "")
                   + "isn't active — finish Agentic onboarding in the "
                   "Robinhood app, then rerun setup")


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


# Why the last resolve_instrument() came back empty — read by execute() so
# the skip says what Robinhood actually answered (an error, zero rows, an
# unrecognized shape) instead of always "no tradable contract".
_LAST_LOOKUP = {}
_RESOLVE_RETRY_WAIT_S = 3.0


def _result_text(result, limit=300):
    """Raw text of an MCP tool result, trimmed — for the log only."""
    try:
        parts = [str(i.get("text", "")) for i in (result or {}).get("content", [])
                 if isinstance(i, dict)]
        text = " ".join(p for p in parts if p).strip()
    except Exception:
        text = ""
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _count_rows(obj):
    if isinstance(obj, dict):
        own = 1 if (obj.get("strike_price") or obj.get("expiration_date")) else 0
        return own + sum(_count_rows(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_rows(v) for v in obj)
    return 0


def _describe_lookup(result, payload):
    """{'kind': error|no_data|no_rows|no_id|ok, 'detail': str, 'raw': str}."""
    raw = _result_text(result)
    if result is None:
        return {"kind": "no_data", "detail": "no answer from Robinhood", "raw": ""}
    if isinstance(result, dict) and result.get("isError"):
        return {"kind": "error", "detail": raw or "Robinhood returned an error",
                "raw": raw}
    if payload is None:
        return {"kind": "no_data",
                "detail": "Robinhood answered without contract data", "raw": raw}
    if isinstance(payload, dict) and (payload.get("error") or payload.get("errors")):
        err = payload.get("error") or payload.get("errors")
        return {"kind": "error", "detail": str(err)[:200], "raw": raw}
    rows = _count_rows(payload)
    if rows == 0:
        return {"kind": "no_rows", "detail": "Robinhood returned 0 matching contracts",
                "raw": raw}
    return {"kind": "ok", "detail": f"{rows} row(s)", "raw": raw}


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
    _LAST_LOOKUP.clear()
    _LAST_LOOKUP.update(_describe_lookup(result, payload))

    def find_id(obj):
        if isinstance(obj, dict):
            if obj.get("id") and (obj.get("chain_symbol") or obj.get("strike_price")
                                  or obj.get("expiration_date")):
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


def account_value(rh, cfg):
    """Total account value for %-of-account sizing (0.0 when unavailable).

    get_portfolio returns total_value, cash, buying_power, equity_value and
    options_value (read on a live Agentic account 2026-07-27). total_value
    is the whole account; equity_value is STOCK holdings only, so it is
    never used. Falls back to buying power, which is never larger for a
    cash account, so a schema change sizes smaller, not bigger."""
    try:
        payload = content_json(rh.call_tool(
            "get_portfolio", {"account_number": cfg.get("robinhood_account", "")}))
    except Exception:
        return 0.0
    if payload is None:
        return 0.0
    return (_find_number(payload, ["total_value"])
            or _find_number(payload, ["buying_power", "cash"]))


def buying_power(rh, cfg):
    """Cash available for new orders, or None when it can't be read (so an
    unreadable portfolio never looks like a $0 account)."""
    try:
        payload = content_json(rh.call_tool(
            "get_portfolio", {"account_number": cfg.get("robinhood_account", "")}))
    except Exception:
        return None
    if payload is None:
        return None
    for keys in (["buying_power"], ["cash"]):
        if _has_key(payload, keys[0]):
            return float(_find_number(payload, keys) or 0.0)
    return None


def _has_key(obj, key):
    if isinstance(obj, dict):
        return key in obj or any(_has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key(v, key) for v in obj)
    return False


def mark_price(rh, cfg, event, side="buy"):
    """Per-share quote for the agent's P&L estimate (0.0 when unavailable)."""
    option_id = resolve_instrument(rh, event)
    return quote_price(rh, option_id, side) if option_id else 0.0


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


def _skip_kind(note):
    if "exceeds your" in note:
        return "skip_budget"
    if "no quote" in note:
        return "skip_no_quote"
    return "skip_sizing"


def size_contracts(rh, cfg, option_id):
    """How many contracts to buy, per the user's sizing config.

    sizing_mode "contracts": fixed contracts_per_trade.
    sizing_mode "budget": floor(budget_per_trade_usd / (price*100)); if even
    one contract exceeds the budget, SKIP (respecting the budget beats
    forcing a trade). Returns (qty, note) — qty 0 means skip.
    sizing_mode "percent" is resolved to "budget" by the agent first.
    """
    if str(cfg.get("sizing_mode")) == "percent":
        # The agent turns % of account into a dollar budget before calling
        # the broker; reaching here means that step was skipped. Never fall
        # back to a fixed count the user didn't choose.
        return 0, "%-of-account sizing not resolved to a budget — skipped"
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
        qty = min(qty, int(cfg.get("max_contracts_per_trade", 100)))
        return qty, f"${budget:,.0f} budget @ ~${price:.2f} → {qty} contract(s)"
    return min(max(1, int(cfg.get("contracts_per_trade", 1))),
               int(cfg.get("max_contracts_per_trade", 100))), "fixed contract count"


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
    `state` + string-decimal `processed_quantity`.

    Asks for the one order by id: the unfiltered list is a single page,
    newest first, so once the owner also trades in the app the agent's own
    order can drop off it. Falls back to the unfiltered list if Robinhood
    rejects the filter or answers without the order."""
    try:
        result = rh.call_tool("get_option_orders", {
            "account_number": account, "order_id": str(order_id)})
        filtered = tool_ok(result)
        if not filtered and not getattr(rh, "last_error", None):
            result = rh.call_tool("get_option_orders",
                                  {"account_number": account})
        body = content_json(result) or {}

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
        if not order and filtered and not getattr(rh, "last_error", None):
            # the filter was accepted but the order isn't in the answer:
            # look in the plain list before calling it unfindable
            order = _find(content_json(rh.call_tool(
                "get_option_orders", {"account_number": account})) or {})
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
        broker_events.report("order_failed" if side == "buy" else "exit_failed",
                             f"Robinhood rejected the {side} order: "
                             f"{_error_text(result)}")
        return None
    oid = _extract_order_id(result)
    print(f"  order {side}/{effect} x{qty} -> {oid or 'submitted (no id)'}")
    return {"order_id": oid, "was_limit": limit_price is not None}


def _error_text(result):
    """The readable part of a failed tool result (Robinhood's own reason)."""
    body = content_json(result)
    if isinstance(body, dict):
        for k in ("detail", "error", "message", "non_field_errors", "reason"):
            if body.get(k):
                return str(body[k])[:300]
    return str(result)[:300]


def _finalize_pending(state, pos_key, filled_total, report_unfilled=True):
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
            _order_settled(pos)
            print(f"  fill verified: bought {total}x ({pos_key})")
        else:
            del state["positions"][pos_key]
            print(f"  entry never filled — no position recorded ({pos_key})")
            if report_unfilled:
                broker_events.report(
                    "entry_unfilled",
                    f"buy order for {pos_key.replace('|', ' ')} was placed but "
                    "never filled (cancelled or rejected by Robinhood)")
    else:
        remaining = int(pos.get("qty", 0)) - filled_total
        if remaining <= 0:
            del state["positions"][pos_key]
            print(f"  fill verified: sold {filled_total}x ({pos_key})")
        else:
            pos["qty"] = remaining
            _order_settled(pos)
            print(f"  ⚠️ exit only filled {filled_total}/{pend['requested']} — "
                  f"{remaining}x still held ({pos_key}); reconcile keeps working "
                  "it, or close manually in the app")
            broker_events.report(
                "exit_partial",
                f"exit filled {filled_total}/{pend['requested']} — {remaining}x "
                f"{pos_key.replace('|', ' ')} still held; close it in the "
                "Robinhood app if it doesn't clear")


def _record_pending(state, pos_key, placed, side, qty):
    pos = state["positions"][pos_key]
    pos["pending"] = {
        "order_id": placed["order_id"], "side": side, "requested": int(qty),
        "filled": 0, "was_limit": bool(placed["was_limit"]),
        "placed_at": time.time(),
    }
    _stamp_order(pos, placed["order_id"])
    _remember_order(state, placed["order_id"])


_ORDER_IDS_KEPT = 20
_AGENT_ORDERS_KEPT = 200


def _remember_order(state, order_id):
    """Every order the agent places, kept after its position is gone: the
    agent's sell of an earlier position in the same contract must never
    pass for the owner's sale (brokers/__init__.py)."""
    if order_id:
        ids = [i for i in state.get("agent_order_ids") or [] if i != str(order_id)]
        state["agent_order_ids"] = (ids + [str(order_id)])[-_AGENT_ORDERS_KEPT:]


def _stamp_order(pos, order_id):
    """Remember the agent's own order (so the owner's sells can be told
    apart from it) and when it was placed (holdings lag fresh orders)."""
    if order_id:
        ids = [i for i in pos.get("order_ids") or [] if i != str(order_id)]
        pos["order_ids"] = (ids + [str(order_id)])[-_ORDER_IDS_KEPT:]
    pos["last_order_at"] = time.time()


def _order_settled(pos):
    """The row survived an order: restart the holdings grace period and
    forget any shortfall seen while the order was working."""
    pos["last_order_at"] = time.time()
    pos.pop("miss", None)


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
        _LAST_LOOKUP.clear()
        option_id = resolve_instrument(rh, event)
        if not option_id and not getattr(rh, "last_error", None):
            # One retry: a lookup right after the open can hit a brief
            # Robinhood error or rate limit, and a skip here loses the trade.
            first = dict(_LAST_LOOKUP)
            print(f"  contract lookup for {contract} came back empty "
                  f"({first.get('detail') or 'no detail'}) — retrying in "
                  f"{_RESOLVE_RETRY_WAIT_S:g}s")
            time.sleep(_RESOLVE_RETRY_WAIT_S)
            _LAST_LOOKUP.clear()
            option_id = resolve_instrument(rh, event)
            if option_id:
                print(f"  retry found {contract}")
        if not option_id:
            down = getattr(rh, "last_error", None)
            if down:
                # The lookup never reached Robinhood (expired login, network)
                # — not a missing contract. Say so, so the fix is obvious.
                print(f"  could not look up {contract} — Robinhood connection "
                      f"is down: {down} — skipped")
                broker_events.report("broker_disconnected",
                                     f"couldn't look up {contract}: Robinhood "
                                     f"connection is down — {down}")
                return False
            why = dict(_LAST_LOOKUP)
            kind, detail = why.get("kind", ""), why.get("detail", "")
            print(f"  could not resolve instrument for {contract} — skipped "
                  f"[{kind or 'unknown'}: {detail or 'no detail'}]"
                  + (f" Robinhood said: {why['raw']}" if why.get("raw") else ""))
            if kind in ("error", "no_data", "ok"):
                # Robinhood answered, but not with a usable contract row:
                # an error (rate limit, server problem), an empty/unparsed
                # body, or rows whose shape we didn't recognize.
                msg = {"error": f"Robinhood returned an error looking up "
                                f"{contract} (retried once): {detail}",
                       "no_data": f"Robinhood answered without contract data "
                                  f"for {contract} (retried once)",
                       "ok": f"Robinhood returned {detail} for {contract} but "
                             "no contract id the agent recognizes — please "
                             "report this with agent-run.log"}[kind]
                broker_events.report("broker_lookup_failed", msg)
                return False
            broker_events.report("skip_no_contract",
                                 f"Robinhood has no tradable contract {contract}"
                                 + (" (0 matching contracts, retried once)"
                                    if kind == "no_rows" else ""))
            return False
        qty, note = size_contracts(rh, cfg, option_id)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            broker_events.report(_skip_kind(note), note)
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
                              max(filled, pend.get("filled", 0)),
                              report_unfilled=False)  # signal exited first
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


# ── what the account holds, and the owner's own sells (read-only) ───────────
# Shapes from Robinhood's MCP schema (tools/list, 2026-10-12) and live
# captures: get_option_positions -> {"data": {"positions": [...], "next"?}}
# with rows keyed by `option_id` (the instrument UUID the ledger stores) and
# decimal-string `quantity`; get_option_orders -> {"data": {"orders": [...],
# "next"?}}, newest first. Anything else reads as "unknown", never "closed".

_POS_MAX_PAGES = 5
_FILLS_MAX_PAGES = 3
_PENDING_KEYS = ("pending_sell_quantity", "pending_expiration_quantity",
                 "pending_exercise_quantity", "pending_assignment_quantity",
                 "pending_expired_quantity")


def _is_uuid(s):
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _last_seg(v):
    """Last path segment — REST rows name the instrument by URL."""
    return str(v or "").rstrip("/").rsplit("/", 1)[-1]


def _dec(v):
    """Robinhood decimal string -> float. '' and None are 0; junk raises."""
    return 0.0 if v is None or v == "" else float(v)


def _price(v):
    """A usable per-share price, or None."""
    return v if v is not None and 0 < v < float("inf") else None


def _page(body, key):
    """(rows, next cursor) of one listing page; (None, None) when the body
    isn't a listing we recognize. A null list is unknown, not empty."""
    if not isinstance(body, dict):
        return None, None
    data = body.get("data", body)
    if not isinstance(data, dict):
        return None, None
    rows = data.get(key, data.get("results"))
    if not isinstance(rows, list):
        return None, None
    return rows, data.get("next") or None


def held_option_qty(rh, cfg, option_ids):
    """{option_id: long contracts held}, or None when the account can't be
    read with certainty (see brokers/__init__.py).

    Lists every open position instead of filtering by id: a filter Robinhood
    read differently would return [] and make every row look closed. An
    agentic account only holds a few positions."""
    ids = sorted({str(i) for i in option_ids or () if _is_uuid(i)})
    if not ids:
        return {}
    acct = (cfg or {}).get("robinhood_account")
    if not acct or getattr(rh, "last_error", None):
        return None
    try:
        return _read_positions(rh, str(acct), ids)
    except Exception:
        return None


def _read_positions(rh, acct, ids):
    want = set(ids)
    held, busy = {}, set()
    args = {"account_number": acct, "nonzero": True}
    first, pages = True, 0
    while pages < _POS_MAX_PAGES:
        result = rh.call_tool("get_option_positions", dict(args))
        if getattr(rh, "last_error", None):
            return None
        if not tool_ok(result):
            if first and "nonzero" in args:
                # filter not accepted? retry once without it (zero-quantity
                # rows then come back too, and are skipped below)
                del args["nonzero"]
                first = False
                continue
            return None
        first = False
        rows, nxt = _page(content_json(result), "positions")
        if rows is None:
            return None
        pages += 1
        for row in rows:
            if row is not None and not _count_position(row, want, held, busy):
                return None
        if not nxt:
            return {i: held.get(i, 0) for i in ids if i not in busy}
        if str(nxt).startswith("http"):
            return None  # a REST page URL: not the MCP cursor we know
        args["cursor"] = str(nxt)
    return None  # still more pages: never answer from part of the list


def _count_position(row, want, held, busy):
    """Add one position row to held/busy. False when the row isn't one we
    recognize, which makes the whole read unknown."""
    if not isinstance(row, dict):
        return False
    # Never row["id"]: on REST rows that's the POSITION id, not the contract.
    oid = str(row.get("option_id") or _last_seg(row.get("option")))
    qkey = "quantity" if "quantity" in row else "qty" if "qty" in row else None
    if not oid or qkey is None:
        return False
    if str(row.get("type") or "").lower() == "short":
        return True
    try:
        q = int(round(_dec(row[qkey])))
    except (TypeError, ValueError, OverflowError):
        return oid not in want
    try:
        pending = sum(_dec(row.get(k)) for k in _PENDING_KEYS)
    except (TypeError, ValueError):
        pending = 1  # can't tell: treat as busy
    if not pending <= 0:
        busy.add(oid)  # a sale/expiry/exercise in flight: count unknown
    if q > 0:
        held[oid] = held.get(oid, 0) + q
    return True


def closed_fills(rh, cfg, option_id, since_iso=None, own_order_ids=(),
                 exclude_agentic=False):
    """Filled sell-to-close orders on option_id that the agent didn't place,
    newest first, or None when orders can't be read (brokers/__init__.py).
    exclude_agentic also skips orders placed through the agentic API — for
    positions from before the agent kept its own order ids."""
    acct = (cfg or {}).get("robinhood_account")
    if not acct or not _is_uuid(option_id) or getattr(rh, "last_error", None):
        return None
    own = {str(o) for o in own_order_ids or ()}
    try:
        return _read_fills(rh, str(acct), str(option_id), since_iso, own,
                           exclude_agentic)
    except Exception:
        return None


def _day_before(since_iso):
    """'YYYY-MM-DD' of the day before since_iso (a UTC ISO time) — the
    order filter's slack for time zones. '' when unreadable."""
    try:
        day = date.fromisoformat(str(since_iso)[:10])
    except (TypeError, ValueError):
        return ""
    return (day - timedelta(days=1)).isoformat()


def _read_fills(rh, acct, option_id, since_iso, own, exclude_agentic):
    args = {"account_number": acct}
    since = _day_before(since_iso)
    if since:
        args["created_at_gte"] = since
    out, pages, first = [], 0, True
    while pages < _FILLS_MAX_PAGES:
        result = rh.call_tool("get_option_orders", dict(args))
        down = getattr(rh, "last_error", None)
        orders, nxt = ((None, None) if down or not tool_ok(result)
                       else _page(content_json(result), "orders"))
        if orders is None:
            if first and not down and "created_at_gte" in args:
                del args["created_at_gte"]  # filter not accepted: retry once
                first = False
                continue
            return out if pages else None  # a partial list only under-covers
        first = False
        pages += 1
        for o in orders:
            fill = _owner_fill(o, option_id, own, exclude_agentic)
            if fill:
                out.append(fill)
        if not nxt or str(nxt).startswith("http"):
            break
        args["cursor"] = str(nxt)
    return out


def _owner_fill(o, option_id, own, exclude_agentic):
    """One order -> {order_id, qty, price, at, by} when it sold-to-close
    option_id and isn't the agent's own; None otherwise."""
    if not isinstance(o, dict):
        return None
    oid = str(o.get("id") or "")
    if not oid or oid in own:
        return None
    # an agent order the owner approved in the app reads placed_agent
    # "user" with agent_initiated true
    if exclude_agentic and (str(o.get("placed_agent") or "").lower() == "agentic"
                            or o.get("agent_initiated") is True):
        return None
    legs = o.get("legs") if isinstance(o.get("legs"), list) else []
    leg = next((g for g in legs if _sells_to_close(g, option_id)), None)
    if leg is None:
        return None
    try:
        filled = _leg_fill(o, leg, len(legs))
        qty = int(round(filled[0])) if filled else 0
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    if qty < 1:
        return None
    return {"order_id": oid, "qty": qty, "price": _price(filled[1]),
            "at": filled[2] or o.get("updated_at"), "by": o.get("placed_agent")}


def _sells_to_close(leg, option_id):
    if not isinstance(leg, dict):
        return False
    leg_id = str(leg.get("option_id") or _last_seg(leg.get("option")))
    return (leg_id == option_id and str(leg.get("side")).lower() == "sell"
            and str(leg.get("position_effect") or "close").lower() == "close")


def _leg_fill(o, leg, n_legs):
    """(contracts, per-share price or None, time) filled on this leg, or None
    when it can't be told. Market orders carry price "" — never parse it."""
    execs = leg.get("executions")
    if isinstance(execs, list) and execs:
        qty = notional = 0.0
        priced, at = True, None
        for e in execs:
            try:
                q, p = _dec(e.get("quantity")), _dec(e.get("price"))
            except (AttributeError, TypeError, ValueError):
                continue
            if not q > 0:
                continue
            qty += q
            notional += p * q
            priced = priced and p > 0
            ts = e.get("timestamp")
            if ts and (at is None or str(ts) > at):
                at = str(ts)
        return qty, (notional / qty if qty > 0 and priced else None), at
    if n_legs != 1:
        return None
    # no executions listed: order totals (processed_premium is total dollars)
    q = _dec(o.get("processed_quantity"))
    premium = _dec(o.get("processed_premium"))
    mult = _dec(o.get("trade_value_multiplier")) or 100.0
    price = premium / (q * mult) if q > 0 and premium > 0 else None
    return q, price, o.get("updated_at")
