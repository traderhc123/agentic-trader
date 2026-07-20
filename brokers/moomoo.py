"""Broker adapter: moomoo (via the OpenD gateway).

moomoo's API is gateway-based: you run their **OpenD** desktop/CLI program
(moomoo.com → API, log in with your moomoo account once), and this adapter
talks to it locally. Two prerequisites beyond config:

    1. OpenD running (default 127.0.0.1:11111)
    2. the SDK:  .venv/bin/pip install moomoo-api

Native PAPER MODE (moomoo "SIMULATE") is the default — real options trading
requires options approval on the account plus your trade password to unlock
REAL orders. Long single-leg only, matching the agent's contract: buy-to-open
on ENTERED, sell on EXITED for positions this agent opened.

Orders are placed as marketable LIMIT at the ask (market orders on options
are not reliably accepted through OpenD), qty from the same budget sizing as
the other brokers.
"""

_TIMEOUT = 15
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 11111

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
            "moomoo SDK not installed — run:  .venv/bin/pip install moomoo-api"
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
    print("running on this machine, plus:  .venv/bin/pip install moomoo-api")
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


def _size(cfg, price):
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
            return (max(1, int(cfg.get("contracts_per_trade", 1))),
                    "no quote — fell back to fixed contracts")
    return max(1, int(cfg.get("contracts_per_trade", 1))), "fixed contract count"


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
                return None
            ret, data = ctx.unlock_trade(pwd)
            if ret != sdk.RET_OK:
                print(f"  ORDER FAILED: trade unlock rejected: {data}")
                return None
        trd_side = sdk.TrdSide.BUY if side == "buy" else sdk.TrdSide.SELL
        ret, data = ctx.place_order(
            price=round(float(price), 2), qty=int(qty), code=code,
            trd_side=trd_side, order_type=sdk.OrderType.NORMAL, trd_env=env)
        if ret != sdk.RET_OK:
            print(f"  ORDER FAILED: {data}")
            return None
        try:
            oid = str(data.iloc[0]["order_id"])
        except Exception:
            oid = "submitted"
        print(f"  moomoo order {side} x{qty} {code} @ ${price:.2f} -> {oid}")
        return oid
    except Exception as exc:
        print(f"  ORDER FAILED: {exc}")
        return None
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass


def execute(cl, cfg, event, state):
    """Act on one normalized event. Returns True if state changed."""
    cfg = cl.cfg if isinstance(cl, _Client) else cfg
    try:
        sdk = _sdk()
    except RuntimeError as exc:
        print(f"SKIPPED: {exc}")
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
            return False
        qty, note = _size(cfg, ask)
        if qty < 1:
            print(f"SKIPPED {contract}: {note}")
            return False
        print(f"ENTERED event -> buying {qty}x {contract} ({note})")
        if _order(cfg, sdk, code, "buy", qty, ask):
            state["positions"][pos_key] = {"option_id": code, "qty": qty,
                                           "opened_event": event.get("event_id")}
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
            return False
        print(f"EXITED event -> selling {pos['qty']}x {contract}")
        if _order(cfg, sdk, code, "sell", pos["qty"], price):
            del state["positions"][pos_key]
            return True
    return False
