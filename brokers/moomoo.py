"""Broker adapter: moomoo (via the OpenD gateway).

moomoo's API is gateway-based: you run their **OpenD** desktop/CLI program
(moomoo.com → API, log in with your moomoo account once), and this adapter
talks to it locally. Two prerequisites beyond config:

    1. OpenD running (default 127.0.0.1:11111)
    2. the SDK:  .venv/bin/pip install moomoo-api   (Windows: .venv\\Scripts\\pip.exe)

Native PAPER MODE (moomoo "SIMULATE") is the default — real options trading
requires options approval on the account plus your trade password to unlock
REAL orders. Long single-leg only, matching the agent's contract: buy-to-open
on ENTERED, sell on EXITED for positions this agent opened.

Orders are placed as marketable LIMIT orders — buys at the ask, sells at
the bid (market orders on options are not reliably accepted through OpenD)
— qty from the same budget sizing as the other brokers.

OpenD's place_order has no way to say "close only" for US options (its
position_id is for moomoo Japan; TrdSide.SELL on a contract the account
doesn't hold can open a short). So every sell first reads what the account
holds, and is never sent for more than that, less what working sells are
already selling — and is NOT sent at all while the holdings can't be read
(_order, order_guard.check_order).
"""

import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import broker_events
import platform_compat

from . import order_guard
from .pricing import exit_qty, marketable_limit

# Every status line in this module goes through safe_print: a console that
# can't show a character ("→" on a Windows cp1252 stdout) must never stop an
# order by making print() raise.
print = platform_compat.safe_print

_TIMEOUT = 15
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 11111

# Entries are booked when OpenD accepts the order, with no fill check — so a
# position the account never showed may simply never have filled.
VERIFIES_FILLS = False

WIZARD = {
    "id": "moomoo",
    "name": "moomoo (OpenD gateway — paper or live)",
    "fields": [
        {"id": "moomoo_host", "label": "OpenD host (blank = 127.0.0.1)",
         "type": "text"},
        {"id": "moomoo_port", "label": "OpenD port (blank = 11111)",
         "type": "text"},
        {"id": "moomoo_paper", "label": "Paper trading / SIMULATE (recommended)",
         "type": "checkbox", "default": True},
        {"id": "moomoo_trade_pwd",
         "label": "Trade password (live orders only — leave blank for paper)",
         "type": "password"},
    ],
}


def _sdk():
    """Lazy SDK import with an actionable error (the package is optional —
    it pulls pandas, so we don't make every non-moomoo user install it)."""
    try:
        import moomoo  # noqa: F401 — pip package "moomoo-api"
        return moomoo
    except ImportError:
        raise RuntimeError(
            "moomoo SDK not installed — run:  "
            f"{platform_compat.venv_pip()} install moomoo-api"
            "  (then make sure the OpenD gateway from moomoo.com/download is "
            "running and logged in)")


def _host_port(cfg):
    host = str(cfg.get("moomoo_host") or _DEFAULT_HOST).strip() or _DEFAULT_HOST
    try:
        port = int(cfg.get("moomoo_port") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        port = _DEFAULT_PORT
    return host, port


def _trd_env(cfg, sdk):
    return (sdk.TrdEnv.SIMULATE if cfg.get("moomoo_paper", True)
            else sdk.TrdEnv.REAL)


def connect(cfg, values):
    """Generic wizard connect: apply field values, verify. (ok, note)."""
    cfg["moomoo_host"] = str(values.get("moomoo_host", "")).strip() or _DEFAULT_HOST
    port = str(values.get("moomoo_port", "")).strip()
    cfg["moomoo_port"] = int(port) if port.isdigit() else _DEFAULT_PORT
    cfg["moomoo_paper"] = bool(values.get("moomoo_paper", True))
    pwd = str(values.get("moomoo_trade_pwd", "")).strip()
    if pwd:
        cfg["moomoo_trade_pwd"] = pwd
    cfg["broker"] = "moomoo"
    return verify(cfg)


class _Client:
    """Thin holder so broker.client(cfg) has the same shape as the others.

    Orders, quotes and holdings reads use this startup copy of the config
    for the gateway address and the paper/real account, so a paper/real
    switch in the dashboard applies after a restart (the agent tags
    positions from it too: agent._account_tag). How much to buy is NOT
    taken from it: sizing comes from the config passed with each event
    (execute)."""

    def __init__(self, cfg):
        self.cfg = cfg


def client(cfg):
    if cfg.get("broker") != "moomoo":
        return None
    return _Client(cfg)


def _trade_ctx(cfg, sdk):
    host, port = _host_port(cfg)
    return sdk.OpenSecTradeContext(
        filter_trdmarket=sdk.TrdMarket.US, host=host, port=port,
        security_firm=sdk.SecurityFirm.FUTUINC)


def verify(cfg):
    """Returns (ok, message) — OpenD reachable + a US trading account visible."""
    try:
        sdk = _sdk()
    except RuntimeError as exc:
        return False, str(exc)
    ctx = None
    try:
        ctx = _trade_ctx(cfg, sdk)
        ret, data = ctx.get_acc_list()
        if ret != sdk.RET_OK:
            return False, f"OpenD rejected the request: {data}"
        env = "PAPER (SIMULATE)" if cfg.get("moomoo_paper", True) else "LIVE"
        n = len(data) if data is not None else 0
        note = f"connected ✓ ({env}, {n} account(s) via OpenD)"
        if not cfg.get("moomoo_paper", True) and not cfg.get("moomoo_trade_pwd"):
            note += " — ⚠️ live orders need your trade password (re-run setup)"
        return True, note
    except Exception as exc:
        host, port = _host_port(cfg)
        return False, (f"could not reach OpenD at {host}:{port} — is the "
                       f"moomoo OpenD gateway running and logged in? ({exc})")
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def setup(cfg):
    print("\n-- moomoo --")
    print("Needs the OpenD gateway (moomoo.com → API → download OpenD, log in)")
    print("running on this machine, plus:  "
          f"{platform_compat.venv_pip()} install moomoo-api")
    host = input(f"OpenD host [{_DEFAULT_HOST}]: ").strip() or _DEFAULT_HOST
    port = input(f"OpenD port [{_DEFAULT_PORT}]: ").strip()
    cfg["moomoo_host"] = host
    cfg["moomoo_port"] = int(port) if port.isdigit() else _DEFAULT_PORT
    paper = input("Paper trading (SIMULATE)? [Y/n]: ").strip().lower() != "n"
    cfg["moomoo_paper"] = paper
    if not paper:
        pwd = input("Trade password (unlocks LIVE orders): ").strip()
        if pwd:
            cfg["moomoo_trade_pwd"] = pwd
    cfg["broker"] = "moomoo"
    ok, msg = verify(cfg)
    print(msg if ok else f"⚠️  {msg} — fix and re-run setup")
    return cfg


def _moomoo_code(event):
    """Event fields -> moomoo US option code, e.g. US.AAPL250718C325000
    (underlying + YYMMDD + C/P + strike*1000, no zero padding)."""
    y, m, d = str(event["expiry"]).split("-")
    return (f"US.{event['ticker']}{y[2:]}{m}{d}{event['type']}"
            f"{int(round(float(event['strike']) * 1000))}")


# Why the last quote read came back empty — read by execute() so a gateway
# that isn't running is reported as that, not as a missing quote.
# "down": OpenD couldn't be reached; "said": OpenD's own reason.
_QUOTE_FAIL = {}


def _quote_ctx(cfg, sdk):
    """A quote connection to OpenD. A failure to connect is noted in
    _QUOTE_FAIL before it is raised."""
    _QUOTE_FAIL.clear()
    host, port = _host_port(cfg)
    try:
        return sdk.OpenQuoteContext(host=host, port=port)
    except Exception as exc:
        _QUOTE_FAIL["down"] = str(exc)[:150] or type(exc).__name__
        raise


def _gateway_down_text(cfg):
    """Plain words for "OpenD isn't reachable", or "" when the last quote
    read did reach it."""
    if "down" not in _QUOTE_FAIL:
        return ""
    host, port = _host_port(cfg)
    return (f"can't reach the moomoo OpenD gateway at {host}:{port} — is "
            f"OpenD running and logged in? ({_QUOTE_FAIL['down']})")


def _ask_price(cfg, sdk, code):
    """Latest ask via OpenD quotes; 0.0 when unavailable. An options quote
    subscription on the moomoo account is required for live quotes."""
    ctx = None
    try:
        ctx = _quote_ctx(cfg, sdk)
        ret, data = ctx.get_market_snapshot([code])
        if ret != sdk.RET_OK or data is None or not len(data):
            if ret != sdk.RET_OK and isinstance(data, str) and data.strip():
                _QUOTE_FAIL["said"] = data.strip()[:150]
            return 0.0
        row = data.iloc[0]
        return float(row.get("ask_price") or 0) or float(row.get("last_price") or 0)
    except Exception:
        return 0.0
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def mark_price(cl, cfg, event, side="buy"):
    """Per-share quote for the agent's P&L estimate (0.0 when unavailable):
    the ask for a buy, the bid for a sell — a sale fills at the bid, so
    marking it at the ask would understate a loss."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    if side == "sell":
        return _bid_price(cfg, _sdk(), _moomoo_code(event)) or 0.0
    return _ask_price(cfg, _sdk(), _moomoo_code(event))


def _bid_price(cfg, sdk, code):
    """Latest bid via OpenD quotes: 0.0 when there is no bid, None when the
    quote can't be read (unknown)."""
    ctx = None
    try:
        ctx = _quote_ctx(cfg, sdk)
        ret, data = ctx.get_market_snapshot([code])
        if ret != sdk.RET_OK or data is None or not len(data):
            return None
        v = data.iloc[0].get("bid_price")
        if v is None:
            return None
        bid = float(v)
        return bid if 0 <= bid < float("inf") else None
    except Exception:
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def exit_bid(cl, cfg, event, option_id=None):
    """Best bid per share for the contract the agent is about to sell (the
    expiry guard skips a sell into no bid): 0.0 = no bid, None = unknown."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    try:
        sdk = _sdk()
    except RuntimeError:
        return None
    code = option_id if str(option_id or "").startswith("US.") else _moomoo_code(event)
    return _bid_price(cfg, sdk, code)


def account_value(cl, cfg):
    """Total assets for %-of-account sizing (0.0 when unavailable)."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    ctx = None
    try:
        sdk = _sdk()
        ctx = _trade_ctx(cfg, sdk)
        ret, data = ctx.accinfo_query(trd_env=_trd_env(cfg, sdk))
        if ret != sdk.RET_OK or data is None or not len(data):
            return 0.0
        row = data.iloc[0]
        return float(row.get("total_assets") or 0)
    except Exception:
        return 0.0
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def buying_power(cl, cfg):
    """Cash available for new orders, or None when unreadable."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    ctx = None
    try:
        sdk = _sdk()
        ctx = _trade_ctx(cfg, sdk)
        ret, data = ctx.accinfo_query(trd_env=_trd_env(cfg, sdk))
        if ret != sdk.RET_OK or data is None or not len(data):
            return None
        row = data.iloc[0]
        for k in ("power", "cash"):
            if row.get(k) is not None:
                return float(row.get(k))
    except Exception:
        pass
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    return None


def _size(cfg, price):
    if str(cfg.get("sizing_mode")) == "percent":
        # The agent turns % of account into a dollar budget before calling
        # the broker; reaching here means that step was skipped. Never fall
        # back to a fixed count the user didn't choose.
        return 0, "%-of-account sizing not resolved to a budget — skipped"
    if str(cfg.get("sizing_mode", "contracts")) == "budget":
        budget = float(cfg.get("budget_per_trade_usd", 0) or 0)
        if budget > 0 and price > 0:
            qty = int(budget // (price * 100))
            if qty < 1:
                return 0, (f"1 contract ≈ ${price * 100:,.0f} exceeds your "
                           f"${budget:,.0f} budget — skipped")
            return (min(qty, int(cfg.get("max_contracts_per_trade", 100))),
                    f"${budget:,.0f} budget @ ~${price:.2f} → {qty}x")
        if budget > 0:
            # Never guess a size without a price — a fixed-count fallback can
            # spend far past the budget (same rule as robinhood/alpaca).
            return 0, "no quote available for budget sizing — skipped"
        return 0, "budget mode but budget_per_trade_usd unset — skipped"
    return min(max(1, int(cfg.get("contracts_per_trade", 1))),
               int(cfg.get("max_contracts_per_trade", 100))), "fixed contract count"


def _fail_kind(side):
    return "order_failed" if side == "buy" else "exit_failed"


# OpenD's refresh_cache position and order queries are limited to 10 per 30 s
# per account (moomoo API docs, "Interface Limitations"); over the limit the
# query just fails. Its wording isn't documented, so this matches the words
# such an answer can use. A sell whose holdings read hit the limit waits the
# window out (_LIMIT_WAITS_S, 30 s in all) instead of being held back to the
# next loop — on expiry day that loop may come too late.
_LIMIT_RX = re.compile(r"freq|too\s+(many|often|fast)|rate[\s_-]*limit|频|頻", re.I)
_LIMIT_WAITS_S = (2.0, 4.0, 8.0, 16.0)


def _fresh_query(sdk, query, **kw):
    """query(**kw) -> (ret, data); a frequency-limit failure is retried
    after _LIMIT_WAITS_S, any other failure is returned at once."""
    waits = list(_LIMIT_WAITS_S)
    while True:
        ret, data = query(**kw)
        if ret == sdk.RET_OK or not waits or not _LIMIT_RX.search(str(data)):
            return ret, data
        wait = waits.pop(0)
        print(f"  OpenD: query limit reached ({str(data)[:80]}) — retrying in "
              f"{wait:g}s")
        time.sleep(wait)


def _sell_holding(cfg, sdk, code, ctx=None):
    """(long contracts of `code` the account can sell right now, short
    contracts of it), fresh from OpenD: the smaller of its can_sell_qty and
    its qty less what working sell orders on it still have to sell.
    (None, None) when either list can't be read; a code not listed is
    held 0.

    moomoo documents can_sell_qty as "holding quantity - frozen quantity",
    and a working sell freezes what it sells, so can_sell_qty may already
    leave the working sells out. Subtracting them from it again would sell
    too little (holding 5, a 2-lot sell working: can_sell_qty 3, and 3 - 2
    = 1) and leave the rest unsellable. min(can_sell, qty - working) is
    right either way.

    ctx: the trade context the order goes out through (read right before
    sending it); without one a context is opened and closed here."""
    own = ctx is None
    try:
        if own:
            ctx = _trade_ctx(cfg, sdk)
        env = _trd_env(cfg, sdk)
        ret, data = _fresh_query(sdk, ctx.position_list_query, trd_env=env,
                                 refresh_cache=True)
        if ret != sdk.RET_OK or data is None:
            return None, None
        held, can_sell, short = 0.0, 0.0, 0
        for i in range(len(data)):
            row = data.iloc[i]
            if not row.get("code"):
                return None, None
            if str(row.get("code")) != code:
                continue
            q = float(row.get("qty"))
            if not math.isfinite(q):
                return None, None
            if "SHORT" in str(row.get("position_side") or "LONG").upper() or q < 0:
                short += int(round(abs(q)))
                continue
            can = row.get("can_sell_qty")
            can = q if can is None else float(can)
            if not math.isfinite(can):
                return None, None
            held += max(0.0, q)
            can_sell += max(0.0, can)
        if short:
            return 0, short
        ret, orders = _fresh_query(sdk, ctx.order_list_query, trd_env=env,
                                   refresh_cache=True)
        if ret != sdk.RET_OK or orders is None:
            return None, None
        working = 0
        for i in range(len(orders)):
            row = orders.iloc[i]
            if str(row.get("code")) != code:
                continue
            status = str(row.get("order_status")).rsplit(".", 1)[-1].upper()
            side = str(row.get("trd_side") or "").rsplit(".", 1)[-1].upper()
            if status in _MM_DONE or not side.startswith("SELL"):
                continue
            left = float(row.get("qty") or 0) - float(row.get("dealt_qty") or 0)
            if not math.isfinite(left):
                return None, None
            working += max(0, math.ceil(left))
        return max(0, min(math.floor(can_sell), math.floor(held) - working)), 0
    except Exception:
        return None, None
    finally:
        if own and ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _expires_today(expiry):
    try:
        return str(expiry) == datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return False


def _order(cfg, sdk, code, side, qty, price, effect=None, contract=None,
           expiry=None):
    """Marketable limit at the given price. Returns the order id (an
    order_guard.OrderId: .qty is how many contracts it was for) or None.

    THE ONLY place a moomoo order is sent. It sends nothing but a
    buy-to-open or a sell-to-close (order_guard.check_order), and a sell
    only for what OpenD says the account holds, less what working sells
    are already selling. moomoo can't be told to only close a position,
    so while the holdings can't be read NO sell is sent: the owner is told,
    and the agent tries again on the next loop.

    The holdings are read through the same trade context as the order,
    right before it is sent (after the trade unlock). Nothing at moomoo
    ties the two together: a sale the owner makes in the app between the
    read and the order can still leave this sell short. That window is a
    few hundred milliseconds; no OpenD setting closes it."""
    contract = contract or code
    try:  # side, effect and quantity first: nothing is opened for a bad one
        qty = order_guard.check_order(side, effect, qty, None,
                                      broker_refuses_open=True)
    except order_guard.RefusedOrder as exc:
        order_guard.report_refusal("moomoo", side, qty, contract, exc,
                                   expires_today=_expires_today(expiry))
        return None
    ctx = None
    try:
        ctx = _trade_ctx(cfg, sdk)
        env = _trd_env(cfg, sdk)
        if env == sdk.TrdEnv.REAL:
            pwd = cfg.get("moomoo_trade_pwd", "")
            if not pwd:
                print("  ORDER BLOCKED: live moomoo orders need your trade "
                      "password — re-run setup")
                broker_events.report(_fail_kind(side), "live moomoo orders need "
                                     "your trade password — re-run setup")
                return None
            ret, data = ctx.unlock_trade(pwd)
            if ret != sdk.RET_OK:
                print(f"  ORDER FAILED: trade unlock rejected: {data}")
                broker_events.report(_fail_kind(side),
                                     f"moomoo rejected the trade unlock: {data}")
                return None
        if side == "sell":
            held, short = _sell_holding(cfg, sdk, code, ctx)
            if short:
                msg = order_guard.short_text("moomoo", contract, short)
                print(f"  ORDER NOT SENT: {msg}")
                broker_events.report("exit_failed", msg, reason="short_seen",
                                     short=short, option_id=code)
                return None
            try:
                qty = order_guard.check_order(side, effect, qty, held,
                                              broker_refuses_open=False)
            except order_guard.RefusedOrder as exc:
                order_guard.report_refusal("moomoo", side, qty, contract, exc,
                                           expires_today=_expires_today(expiry))
                return None
        trd_side = sdk.TrdSide.BUY if side == "buy" else sdk.TrdSide.SELL
        ret, data = ctx.place_order(
            price=round(float(price), 2), qty=int(qty), code=code,
            trd_side=trd_side, order_type=sdk.OrderType.NORMAL, trd_env=env)
        if ret != sdk.RET_OK:
            print(f"  ORDER FAILED: {data}")
            broker_events.report(_fail_kind(side),
                                 f"moomoo rejected the {side} order: {data}")
            return None
        try:
            oid = str(data.iloc[0]["order_id"])
        except Exception:
            oid = "submitted"
        print(f"  moomoo order {side} x{qty} {code} @ ${price:.2f} -> {oid}")
        return order_guard.OrderId(oid, qty)
    except Exception as exc:
        print(f"  ORDER FAILED: {exc}")
        broker_events.report(_fail_kind(side), f"moomoo order error: {exc}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def execute(cl, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    # Sizing comes from the cfg the caller passed, like the other adapters:
    # it is the live config (a budget changed in the dashboard), with the
    # per-track overrides and % of account already resolved to a budget.
    # The gateway address and the paper/real account stay the client's
    # startup copy (_Client).
    sizing = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    try:
        sdk = _sdk()
    except RuntimeError as exc:
        print(f"SKIPPED: {exc}")
        broker_events.report("broker_no_order", str(exc))
        return False
    contract = f"{event['ticker']} {event['expiry']} ${event['strike']:g} " \
               f"{'CALL' if event['type'] == 'C' else 'PUT'}"
    pos_key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
    code = _moomoo_code(event)
    if event["event"] == "ENTERED":
        if pos_key in state["positions"]:
            return False
        ask = _ask_price(cfg, sdk, code)
        if ask <= 0:
            down = _gateway_down_text(cfg)
            if down:
                print(f"SKIPPED {contract}: {down}")
                broker_events.report("broker_disconnected",
                                     f"couldn't get a price for {contract}: {down}")
                return False
            said = (f" (OpenD said: {_QUOTE_FAIL['said']})"
                    if _QUOTE_FAIL.get("said") else "")
            print(f"SKIPPED {contract}: no quote from OpenD (options quote "
                  f"subscription active on the moomoo account?){said}")
            broker_events.report("skip_no_quote",
                                 f"no quote from OpenD for {contract} — is the "
                                 f"options quote subscription active?{said}")
            return False
        qty, note = _size(sizing, ask)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            broker_events.report(
                "skip_budget" if "exceeds your" in note else
                "skip_no_quote" if "no quote" in note else "skip_sizing", note)
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        oid = _order(cfg, sdk, code, "buy", qty, ask, effect="open",
                     contract=contract)
        if oid:
            qty = getattr(oid, "qty", qty)
            _remember_order(state, oid)
            row = {"option_id": code, "qty": qty,
                   "opened_event": event.get("event_id"),
                   "last_order_at": time.time()}  # holdings lag fresh orders
            if oid != "submitted":
                row["order_ids"] = [oid]
            state["positions"][pos_key] = row
            return True
    elif event["event"] == "EXITED":
        pos = state["positions"].get(pos_key)
        if not pos:
            return False
        code = pos.get("option_id", code)
        # sell marketable: at the bid if we can get one, else last/ask
        # (a sell limit AT THE ASK just rests on the book unfilled)
        bid = _bid_price(cfg, sdk, code)
        price = 0.0
        if event.get("exit_limit") == "marketable":
            # expiry guard / exit remainder: bid-5% on a valid tick (pricing.py)
            price = marketable_limit(bid, "sell") or 0.0
            if price > 0:
                print(f"  marketable sell — limit @ ${price:.2f}")
        elif bid:
            price = bid
        if price <= 0:
            price = _ask_price(cfg, sdk, code)
        if price <= 0:
            down = _gateway_down_text(cfg)
            if down:
                print(f"SKIPPED {contract}: {down} — will retry on the next poll")
                broker_events.report("broker_disconnected",
                                     f"couldn't get a price to exit {contract}: "
                                     f"{down}")
                return False
            print(f"SKIPPED {contract}: no quote from OpenD for exit — "
                  "will retry on the next poll")
            broker_events.report("exit_failed",
                                 f"no quote from OpenD to exit {contract}")
            return False
        qty = exit_qty(pos, event)
        print(f"EXITED event -> selling {qty}x {contract}")
        oid = _order(cfg, sdk, code, "sell", qty, price, effect="close",
                     contract=contract, expiry=event.get("expiry"))
        if oid:
            # never more than the account holds: may be fewer than the ledger
            qty = getattr(oid, "qty", qty)
            _remember_order(state, oid)
            sold, sure = _confirm_sell(cfg, sdk, oid, qty)
            if not sure:
                # booked as sold, like before fills were checked; the agent's
                # ledger check re-adds contracts it bought that are still held
                print(f"  couldn't confirm the sell filled — assuming {qty}x "
                      "(the agent re-checks your holdings)")
                broker_events.report(
                    "exit_failed", f"moomoo couldn't confirm whether the sell "
                    f"order for {qty}x {contract} filled — the agent assumes it "
                    "did and re-checks your holdings", placed=True)
            remaining = int(pos["qty"]) - sold
            if remaining > 0:
                pos["qty"] = remaining
                pos["last_order_at"] = time.time()  # holdings lag fresh orders
                if oid != "submitted":
                    pos["order_ids"] = ([i for i in pos.get("order_ids") or []
                                         if i != oid] + [oid])[-20:]
                # the agent re-sells the remainder (agent.exit_remainder_tick)
                print(f"  ⚠️ exit only filled {sold}/{qty} — {remaining}x still "
                      f"held; re-selling the remaining {remaining}x next loop")
                broker_events.report("exit_partial",
                                     f"exit filled {sold}/{qty} — {remaining}x "
                                     f"{contract} still held; the agent re-sells "
                                     f"the remaining {remaining}x next loop")
            else:
                del state["positions"][pos_key]
            return True
    return False


# ── exit fill check ─────────────────────────────────────────────────────────
# An exit used to be booked as sold as soon as OpenD accepted it. It's now
# polled like Alpaca's: a sell still working after the polls is cancelled
# and settled with what filled (the agent re-sells the rest). Only when the
# order's state can't be read at all is it assumed sold (and reported).
# Column/enum names follow moomoo's API docs; not yet checked on a live OpenD.

_CONFIRM_TRIES = 3
_CONFIRM_WAIT_S = 5.0
_CANCEL_SETTLE_S = 2.0


def _order_fill(cfg, sdk, oid):
    """(status, dealt_qty) of one order, or None when it can't be read."""
    ctx = None
    try:
        ctx = _trade_ctx(cfg, sdk)
        ret, data = ctx.order_list_query(order_id=str(oid),
                                         trd_env=_trd_env(cfg, sdk),
                                         refresh_cache=True)
        if ret != sdk.RET_OK or data is None:
            return None
        for i in range(len(data)):
            row = data.iloc[i]
            if str(row.get("order_id")) != str(oid):
                continue
            status = str(row.get("order_status")).rsplit(".", 1)[-1].upper()
            return status, int(float(row.get("dealt_qty") or 0))
        return None
    except Exception:
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _cancel_order(cfg, sdk, oid):
    ctx = None
    try:
        ctx = _trade_ctx(cfg, sdk)
        if _trd_env(cfg, sdk) == sdk.TrdEnv.REAL and cfg.get("moomoo_trade_pwd"):
            ctx.unlock_trade(cfg["moomoo_trade_pwd"])
        ret, _data = ctx.modify_order(sdk.ModifyOrderOp.CANCEL, str(oid), 0, 0,
                                      trd_env=_trd_env(cfg, sdk))
        return ret == sdk.RET_OK
    except Exception:
        return False
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _confirm_sell(cfg, sdk, oid, qty):
    """(contracts sold, certain). Polls the order; one still working after
    the polls is cancelled and settled with what filled. certain=False:
    the order's state couldn't be read — assumed sold (qty)."""
    if oid == "submitted":
        return qty, False
    seen = False
    for _ in range(_CONFIRM_TRIES):
        time.sleep(_CONFIRM_WAIT_S)
        got = _order_fill(cfg, sdk, oid)
        if got is None:
            continue
        seen = True
        status, dealt = got
        if status == "FILLED_ALL":
            return (dealt or qty), True
        if status in _MM_DONE:
            print(f"  sell {status.lower()} — filled {dealt}/{qty}")
            return min(dealt, qty), True
    if seen:
        _cancel_order(cfg, sdk, oid)
        time.sleep(_CANCEL_SETTLE_S)
        got = _order_fill(cfg, sdk, oid)
        if got is not None:
            status, dealt = got
            if status == "FILLED_ALL":
                return (dealt or qty), True
            if status in _MM_DONE:
                print(f"  sell didn't fill in time — cancelled; filled {dealt}/{qty}")
                return min(dealt, qty), True
    return qty, False


_AGENT_ORDERS_KEPT = 200


def _remember_order(state, oid):
    """Every order the agent places, kept after its position is gone: the
    agent's sell of an earlier position in the same contract must never
    pass for the owner's sale (brokers/__init__.py)."""
    if oid and oid != "submitted":
        ids = [i for i in state.get("agent_order_ids") or [] if i != str(oid)]
        state["agent_order_ids"] = (ids + [str(oid)])[-_AGENT_ORDERS_KEPT:]


# ── what the account holds (read-only) ──────────────────────────────────────
# Column names follow moomoo's API docs and haven't been checked against a
# live OpenD yet. If they're wrong, no ledger code is ever seen held, the
# account is never trusted, and nothing is ever dropped (see agent.py).

# order states that leave nothing working
_MM_DONE = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED",
            "DELETED", "FILL_CANCELLED", "SUBMIT_FAILED", "TIMEOUT"}


def held_option_qty(cl, cfg, option_ids):
    """{moomoo code: long contracts held}, or None when unknown (see
    brokers/__init__.py). A code with any working order — the owner's, or
    the agent's own resting buy — is left out, and so is one that holds
    fewer than today's filled buys minus filled sells (the position list
    lagging a fill)."""
    # the copy execute orders with: reads and orders always look at the
    # same paper/real account
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    ids = sorted({str(i) for i in option_ids or () if str(i).startswith("US.")})
    if not ids:
        return {}
    ctx = None
    try:
        sdk = _sdk()
        ctx = _trade_ctx(cfg, sdk)
        env = _trd_env(cfg, sdk)
        # fresh from the broker, not OpenD's cache (an error reads as unknown)
        ret, data = ctx.position_list_query(trd_env=env, refresh_cache=True)
        if ret != sdk.RET_OK or data is None:
            return None
        held = {}
        for i in range(len(data)):  # works for pandas and the test fake
            row = data.iloc[i]
            code, qty = row.get("code"), row.get("qty")
            if not code or qty is None:
                return None
            if "SHORT" in str(row.get("position_side") or "LONG").upper():
                continue
            q = int(float(qty))
            if q > 0:
                held[str(code)] = held.get(str(code), 0) + q
        ret, orders = ctx.order_list_query(trd_env=env, refresh_cache=True)
        if ret != sdk.RET_OK or orders is None:
            return None
        busy, net = set(), {}
        for i in range(len(orders)):
            row = orders.iloc[i]
            code = str(row.get("code"))
            status = str(row.get("order_status")).rsplit(".", 1)[-1].upper()
            if status not in _MM_DONE:
                busy.add(code)
            elif code in ids and not _net_fill(row, net):
                busy.add(code)
        return {c: held.get(c, 0) for c in ids
                if c not in busy and held.get(c, 0) >= net.get(c, 0)}
    except Exception:
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def short_option_qty(cl, cfg, option_ids):
    """{moomoo code: SHORT contracts held (0 = none)} for the codes asked
    about, or None when the account can't be read (brokers/__init__.py)."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    ids = sorted({str(i) for i in option_ids or () if str(i).startswith("US.")})
    if not ids:
        return {}
    ctx = None
    try:
        sdk = _sdk()
        ctx = _trade_ctx(cfg, sdk)
        ret, data = ctx.position_list_query(trd_env=_trd_env(cfg, sdk),
                                            refresh_cache=True)
        if ret != sdk.RET_OK or data is None:
            return None
        out = {c: 0 for c in ids}
        for i in range(len(data)):
            row = data.iloc[i]
            code = str(row.get("code") or "")
            if code not in out:
                continue
            q = float(row.get("qty"))
            if not math.isfinite(q):
                return None
            if "SHORT" in str(row.get("position_side") or "LONG").upper() or q < 0:
                out[code] += int(round(abs(q)))
        return out
    except Exception:
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def option_fills(cl, cfg, option_id, since_iso=None):
    """Every filled (or partly filled) order on option_id — buys and sells,
    the agent's and the owner's — or None when orders can't be read
    (brokers/__init__.py): today's orders plus the order history."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    code = str(option_id or "")
    if not code.startswith("US."):
        return None
    ctx = None
    try:
        sdk = _sdk()
        ctx = _trade_ctx(cfg, sdk)
        env = _trd_env(cfg, sdk)
        ret, today = ctx.order_list_query(code=code, trd_env=env, refresh_cache=True)
        if ret != sdk.RET_OK or today is None:
            return None
        kw = {"code": code, "trd_env": env}
        if since_iso:
            kw["start"] = str(since_iso)[:10] + " 00:00:00"
        ret, past = ctx.history_order_list_query(**kw)
        if ret != sdk.RET_OK or past is None:
            return None
        out, ids = [], set()
        for data in (today, past):
            for i in range(len(data)):
                row = data.iloc[i]
                oid = str(row.get("order_id") or "")
                if not oid or oid in ids or str(row.get("code")) != code:
                    continue
                ids.add(oid)
                side = str(row.get("trd_side") or "").rsplit(".", 1)[-1].lower()
                # SELL_SHORT is a sale too; BUY_BACK only matters as an own id
                side = ("sell" if side.startswith("sell") else
                        "buy" if side.startswith("buy") else side)
                try:
                    dealt = int(float(row.get("dealt_qty") or 0))
                except (TypeError, ValueError, OverflowError):
                    return None
                if dealt >= 1 and side in ("buy", "sell"):
                    out.append({"order_id": oid, "side": side, "qty": dealt,
                                "at": row.get("updated_time") or row.get("create_time")})
        return out
    except Exception:
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def _net_fill(row, net):
    """Add one finished order's filled buys (+) or sells (-) to net[code].
    False when its side or filled quantity can't be read."""
    side = str(row.get("trd_side") or "").rsplit(".", 1)[-1].upper()
    try:
        dealt = int(float(row.get("dealt_qty") or 0))
    except (TypeError, ValueError, OverflowError):
        return False
    if dealt < 1:
        return True
    if side not in ("BUY", "SELL"):
        return False
    code = str(row.get("code"))
    net[code] = net.get(code, 0) + (dealt if side == "BUY" else -dealt)
    return True
