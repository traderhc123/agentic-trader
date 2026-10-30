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

Orders are placed as marketable LIMIT at the ask (market orders on options
are not reliably accepted through OpenD), qty from the same budget sizing as
the other brokers.
"""

import time

import broker_events
import platform_compat

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
    """Thin holder so broker.client(cfg) has the same shape as the others."""

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


def _ask_price(cfg, sdk, code):
    """Latest ask via OpenD quotes; 0.0 when unavailable. An options quote
    subscription on the moomoo account is required for live quotes."""
    ctx = None
    try:
        host, port = _host_port(cfg)
        ctx = sdk.OpenQuoteContext(host=host, port=port)
        ret, data = ctx.get_market_snapshot([code])
        if ret != sdk.RET_OK or data is None or not len(data):
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
    """Per-share quote for the agent's P&L estimate (0.0 when unavailable)."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
    return _ask_price(cfg, _sdk(), _moomoo_code(event))


def account_value(cl, cfg):
    """Total assets for %-of-account sizing (0.0 when unavailable)."""
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
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
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
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


def _order(cfg, sdk, code, side, qty, price):
    """Marketable limit at the given price. Returns order id or None."""
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
        return oid
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
    # Prefer the cfg the caller passed (the live config, with per-track
    # sizing), like alpaca: orders, holdings reads and the account tag the
    # agent stamps on each position then all use the same paper/real mode,
    # even after the owner flips it in the dashboard.
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
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
            print(f"SKIPPED {contract}: no quote from OpenD (options quote "
                  "subscription active on the moomoo account?)")
            broker_events.report("skip_no_quote",
                                 f"no quote from OpenD for {contract} — is the "
                                 "options quote subscription active?")
            return False
        qty, note = _size(cfg, ask)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            broker_events.report(
                "skip_budget" if "exceeds your" in note else
                "skip_no_quote" if "no quote" in note else "skip_sizing", note)
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        oid = _order(cfg, sdk, code, "buy", qty, ask)
        if oid:
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
        price = _ask_price(cfg, sdk, code)
        if price <= 0:
            print(f"SKIPPED {contract}: no quote from OpenD for exit — "
                  "will retry on the next poll")
            broker_events.report("exit_failed",
                                 f"no quote from OpenD to exit {contract}")
            return False
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        if _order(cfg, sdk, code, "sell", pos["qty"], price):
            del state["positions"][pos_key]
            return True
    return False


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
    cfg = cfg or (cl.cfg if isinstance(cl, _Client) else {})
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
