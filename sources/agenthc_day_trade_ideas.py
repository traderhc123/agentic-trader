"""Signal source: AgentHC's Agentic Day Trade Ideas feed (optional, featured).

A machine-readable stream of AgentHC's own published, HYPOTHETICAL
paper-trading journal — declarative "ENTERED"/"EXITED" statements of fact for
its main day-trade pick. No prices, no sizing, no recommendations; exits
carry AgentHC's own paper P&L %% statistic. Educational commentary, NOT
investment advice (full terms in the feed's `disclosure` field and this
repo's DISCLAIMER.md).

Access is sats-based: the agent auto-buys a ~$50/day Lightning day-pass
(price floats with Bitcoin's USD price) from its built-in wallet, or uses a
Premium AgentHC API key. Free aggregate track record (hypothetical, labeled):
GET https://api.traderhc.com/api/v1/trading/day-trade-ideas/track-record

Only LIVE uses signal days. Every read of the feed says the agent's mode
(feed_mode: query param `mode` and header X-Agent-Mode, the same value):
"live", "dry_run" (practice) or "off" (paused). AgentHC charges a prepaid
pass's day, or a Premium key's day, only for "live"; practice and off are
served without using a day. The wallet buys a day-pass only when live.
"""

import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests  # noqa: F401  (tests stub requests.get/post; _http calls them)

from lightning_wallet import (PaymentNotSent, PaymentRefused, WalletKeyRejected,
                              fee_reserve_sats, wallet_fingerprint,
                              wallet_from_cfg, wallet_setup)
from net_check import http as _http  # steps around a broken proxy setting

MARKET_TZ = ZoneInfo("America/New_York")


def _now_et():
    return datetime.now(MARKET_TZ)


NAME = "agenthc"
DESCRIPTION = ("AgentHC Agentic Day Trade Ideas — journal events from a "
               "published paper-trade system (~$50/day in sats, or Premium key)")

AGENTHC_API = os.getenv("AGENTHC_API", "https://api.traderhc.com")
FEED_PATH = "/api/v1/trading/day-trade-ideas"

# The agent's mode, sent with EVERY read of the feed as the query param
# `mode` and the header MODE_HEADER (the same value). AgentHC charges a
# signal day (a prepaid pass's day, or a Premium key's day) only for "live";
# "dry_run" (practice) and "off" (the owner paused the agent) read the feed
# without using a day. An older AgentHC ignores both, so sending them is
# always safe. The agent's wallet buys a day-pass only when live
# (_pass_purchase_allowed).
FEED_MODES = ("live", "dry_run", "off")
MODE_HEADER = "X-Agent-Mode"


def feed_mode(cfg):
    """"live", "dry_run" or "off" for this read. The agent's loop works it
    out from the same helpers its orders use (agent._feed_mode: Pause →
    off, practice → dry_run, else live) and hands it over as
    cfg["feed_mode"]. Without that (a caller that isn't the agent's loop),
    it is read from the settings alone: paused → off, dry_run true →
    dry_run, else live."""
    cfg = cfg or {}
    mode = cfg.get("feed_mode")
    if mode in FEED_MODES:
        return mode
    if cfg.get("paused"):
        return "off"
    return "dry_run" if cfg.get("dry_run") is True else "live"


def mode_params(mode):
    """(params, headers) that say `mode` to AgentHC."""
    return {"mode": mode}, {MODE_HEADER: mode}


def _feed_get(cfg, params, headers=None, mode=None):
    """GET the feed, saying the agent's mode (feed_mode), or `mode` for a
    read that is only for exits ("off": _real_exits_read). `params` itself
    is left as it is (_check_window reads its limit)."""
    extra_params, extra_headers = mode_params(mode or feed_mode(cfg))
    return _http.get(f"{AGENTHC_API}{FEED_PATH}",
                     params={**params, **extra_params},
                     headers={**(headers or {}), **extra_headers}, timeout=15)


PASS_PATH = FEED_PATH + "/pass"
# Prepaid pass prices in USD (paid in sats at the live BTC price): 1 signal
# day or a 5-day bundle. FALLBACK only, for a server that can't be reached
# or doesn't send prices: AgentHC sends prices_usd/prices_sats with its pass
# options (pass_prices() reads them) and sets the real price on each
# invoice. The 24h wallet day-pass costs the same as 1 signal day.
DAY_PASS_USD = 50.0
PASS_PRICES_USD = {1: 50.0, 5: 225.0}
_PRICE_TTL_S = 300              # how long the server's prices are reused
_PRICE_RETRY_S = 60             # after a failed ask: try again this soon
# The server's last prices: {days: usd} / {days: sats}, or None.
_price_cache = {"at": 0.0, "usd": None, "sats": None}
PASS_LOW_DAYS = 2               # warn when this many signal days (or fewer) remain
# Server statuses that still read the feed: "exits_only" = days used up, but
# exit signals keep coming for a few market days so open positions can close.
_USABLE = ("active", "exits_only")
_PENDING_RECHECK_S = 60         # status poll cadence while an invoice is unpaid
_INVOICE_LIFETIME_S = 3600      # server invoices expire after 1h
_UNPAID_RECHECK_S = 3600        # an invoice AgentHC called unpaid: asked again this often

# Wallet day-pass (the agent pays ~$50 from its own wallet).
_DAYPASS_S = 23 * 3600          # how long the agent uses a paid day-pass token
_OPEN_MIN = 9 * 60 + 30         # regular session opens 9:30 AM ET
_BUY_LEAD_MIN = 60              # earliest purchase: this long before the open
_PAY_RETRY_S = 300              # after a purchase that failed: wait, then retry
_REFUSED_RETRY_S = 120          # a refused paid pass is re-offered this often
_INVOICE_SLACK_S = 300          # clocks differ: an invoice is "expired" this much later
_PENDING_ALERT_S = 600          # undecided this long: tell the owner
_PENDING_MAX_AGE_S = 24 * 3600  # undecided this long: stop waiting for it


# The pass notices (Discord/ntfy/Telegram + dashboard) say what the pass
# means for NEW trades only. What happens to positions the agent already
# holds is said once, by the agent's no-signal warning (agent.py,
# signal_loss_tick) and the dashboard's positions note.
_SETTINGS = "dashboard → Settings → Signal source"
PASS_LOW_TEXT = ("Your signal pass has {n} signal day(s) left. Buy more in the "
                 "dashboard (Settings → Signal source); unused days carry over.")
PASS_LAST_DAY_TEXT = ("Today is the last day your signal pass covers new "
                      f"trades. Buy more signal days ({_SETTINGS}) to keep "
                      "getting new trades. Exit signals for trades already "
                      "open keep arriving.")
PASS_USED_UP_TEXT = ("Your signal pass is used up: no new trades until you buy "
                     f"more signal days ({_SETTINGS}). Exit signals for the "
                     "trades your agent already opened still arrive.")
PASS_RUN_OUT_TEXT = ("Your signal pass has run out: no new trades until you "
                     f"buy more signal days ({_SETTINGS}).")
KEY_BALANCE_EMPTY_TEXT = (
    "AgentHC asked for payment on your Premium API key: the key's balance "
    "can't pay for today's signals. Top up your key with AgentHC. While an "
    "API key is set your agent never buys a day-pass with its wallet, so "
    "nothing was paid. Until the key is topped up the agent gets no new "
    "trades.")


class PassUnavailable(RuntimeError):
    """The feed server isn't selling prepaid passes (yet)."""


class PassSizeUnavailable(RuntimeError):
    """Passes are on sale, just not this many days (e.g. 1-day before the
    server offers it)."""


class PassSizeRefilling(PassSizeUnavailable):
    """This size is briefly off sale (HTTP 503 pass_size_temporarily_unavailable)
    while the server's Lightning node refills inbound capacity after a large
    purchase. Smaller sizes may still be on sale; this one returns in ~1h."""


PASS_REFILL_MSG = ("That size is briefly unavailable while our node refills — "
                   "try a smaller pass or again in about an hour.")


def _error_code(resp):
    """The server's error code from {"detail": {"error": ..}}, {"error": ..}
    or {"detail": ".."}; None if the body isn't JSON."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    d = body.get("detail", body)
    if isinstance(d, dict):
        return d.get("error")
    return d if isinstance(d, str) else body.get("error")


def buy_pass(days, extend_token=None):
    """Ask AgentHC for an invoice covering `days` SIGNAL days — a market day
    is only used when the agent is served an entry/exit that day. Returns the
    server body (payment_request, pass_token, amount_sats, signal_days…).
    `extend_token` (the current pass) carries its unused days over."""
    payload = {"days": int(days)}
    if extend_token:
        payload["extend"] = extend_token
    resp = _http.post(f"{AGENTHC_API}{PASS_PATH}", json=payload, timeout=20)
    if resp.status_code == 404:
        raise PassUnavailable("Prepaid passes aren't on sale yet — use a "
                              "Lightning wallet (Advanced) for now.")
    if resp.status_code == 400:
        try:
            d = resp.json().get("detail") or {}
        except ValueError:
            d = {}
        if isinstance(d, dict) and d.get("error") == "invalid_days":
            sizes = ", ".join(str(n) for n in d.get("options") or [])
            raise PassSizeUnavailable(f"{days}-day passes aren't on sale right now"
                                  + (f" — pick {sizes} signal days." if sizes else "."))
    if resp.status_code == 503 and _error_code(resp) == "pass_size_temporarily_unavailable":
        raise PassSizeRefilling(PASS_REFILL_MSG)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error") or detail
        raise RuntimeError(f"pass purchase failed ({resp.status_code}): {str(detail)[:160]}")
    return resp.json()


def _price_map(raw, cast):
    """{days: price} from the server's {"1": 50.0, "5": 225.0} (string
    keys); entries that aren't a positive size and positive price are
    dropped. None when the field is missing or nothing usable is left."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in raw.items():
        try:
            days, price = int(k), cast(v)
        except (TypeError, ValueError):
            continue
        if days > 0 and price > 0 and not isinstance(v, bool):
            out[days] = price
    return out or None


def pass_offer():
    """What the server sells right now: {"options": [1, 5] | [] | None,
    "prices_usd": {1: 50.0, 5: 225.0} | None, "prices_sats": {1: n, 5: n}
    | None}. options: [] when every size is paused (the node is refilling),
    None if the server can't be asked. The prices are None from servers
    that don't send them (the pages then show PASS_PRICES_USD). The server
    answers a days=0 request with its options and creates no invoice."""
    out = {"options": None, "prices_usd": None, "prices_sats": None}
    try:
        resp = _http.post(f"{AGENTHC_API}{PASS_PATH}", json={"days": 0}, timeout=10)
        detail = resp.json().get("detail") or {}
    except Exception:
        return out
    if not isinstance(detail, dict):
        return out
    opts = detail.get("options")
    try:
        out["options"] = sorted(int(n) for n in opts) if isinstance(opts, list) else None
    except (TypeError, ValueError):
        out["options"] = None
    out["prices_usd"] = _price_map(detail.get("prices_usd"), float)
    out["prices_sats"] = _price_map(detail.get("prices_sats"), int)
    # the server answered: its prices (None from an older server) are now
    # the ones to show
    _price_cache.update(at=time.time(), usd=out["prices_usd"],
                        sats=out["prices_sats"])
    return out


def pass_prices(max_age=_PRICE_TTL_S):
    """The pass prices to show: {"usd": {1: 50.0, 5: 225.0}, "sats":
    {1: n, 5: n} | None, "from_server": bool}. The ONE place the client
    gets AgentHC's prices (prepaid passes and the wallet day-pass alike).
    Asks the server at most every max_age seconds (any pass_offer() call
    refreshes it too); PASS_PRICES_USD (and no sats) when the server can't
    be reached and never answered, or is an older one without prices."""
    c = _price_cache
    if time.time() - c["at"] >= max_age:
        pass_offer()
        if time.time() - c["at"] >= max_age:   # not reached: keep what we had
            c["at"] = time.time() - max_age + _PRICE_RETRY_S
    usd = c["usd"]
    return {"usd": dict(usd) if usd else dict(PASS_PRICES_USD),
            "sats": dict(c["sats"]) if c["sats"] else None,
            "from_server": bool(usd)}


def day_pass_price():
    """One day's price, {"usd": 50.0, "sats": n | None, "from_server": bool}:
    1 signal day, which is also what the 24h wallet day-pass costs. sats is
    the server's own figure (None when it sent none)."""
    p = pass_prices()
    return {"usd": p["usd"].get(1) or DAY_PASS_USD,
            "sats": (p["sats"] or {}).get(1), "from_server": p["from_server"]}


def usd_words(v):
    """"$50" / "$49.50" for copy."""
    v = float(v)
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


# ── auto-pay caps ────────────────────────────────────────────────────────────
# max_autopay_sats (per invoice) and max_autopay_sats_per_day default to
# "auto": AgentHC's own 1-day price in sats + 10% (BTC can move between the
# quote and the invoice), rounded up to 1,000 sats, never above
# AUTOPAY_AUTO_CEILING_SATS. A number the owner saved is used exactly as is
# (no ceiling). 30000, the fixed default setup used to write, counts as
# auto. With no price from AgentHC the cap is AUTOPAY_FALLBACK_SATS (fails
# safe). autopay_caps() is the ONE place a cap is read.
AUTOPAY_AUTO = "auto"
AUTOPAY_FALLBACK_SATS = 30_000
AUTOPAY_AUTO_CEILING_SATS = 150_000
_AUTOPAY_MARGIN = 1.10


def _plus_margin_k(sats):
    """sats + 10%, rounded up to 1,000 (rounded to 6 places first, so float
    noise like 55000.000000000004 doesn't add a thousand)."""
    n = -(-round(float(sats) * _AUTOPAY_MARGIN, 6) // 1)
    return int(-(-n // 1000) * 1000)


def _live_btc_usd():
    """USD per BTC (the dashboard's 5-minute cached quote), 0.0 if none."""
    try:
        import webui
        return float(webui._btc_usd() or 0.0)
    except Exception:
        return 0.0


def owner_autopay_cap(value):
    """The owner's fixed cap from a saved max_autopay_sats(_per_day), or
    None for auto: missing, "auto", the old default 30000, or anything that
    isn't a whole number of sats (0 or more)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        v = value.strip().replace(",", "").replace("_", "")
        if not v.isdigit():
            return None
        value = int(v)
    if not isinstance(value, (int, float)) or value < 0 or value != int(value):
        return None
    n = int(value)
    return None if n == AUTOPAY_FALLBACK_SATS else n


def auto_autopay_cap():
    """{"sats": n, "basis": "server_sats" | "server_usd" | "fallback"}: the
    auto cap now. server_sats = AgentHC's 1-day sats + 10%; server_usd = its
    USD day price at the live BTC price + 10% (it sent no sats); fallback =
    AUTOPAY_FALLBACK_SATS (no price from AgentHC, or no BTC price)."""
    day = day_pass_price()
    base, basis = None, "fallback"
    if day.get("sats"):
        base, basis = float(day["sats"]), "server_sats"
    elif day.get("from_server"):
        btc = _live_btc_usd()
        if btc > 0:
            base, basis = float(day["usd"]) / btc * 1e8, "server_usd"
    if base is None or base <= 0:
        return {"sats": AUTOPAY_FALLBACK_SATS, "basis": "fallback"}
    sats = _plus_margin_k(base)
    return {"sats": min(sats, AUTOPAY_AUTO_CEILING_SATS), "basis": basis}


def autopay_caps(cfg):
    """The auto-pay caps in force: {"per_invoice": n, "per_day": n,
    "per_invoice_auto": bool, "per_day_auto": bool, "auto_basis": str |
    None}. per_day defaults to the per-invoice cap (one pass a day)."""
    inv = owner_autopay_cap(cfg.get("max_autopay_sats"))
    day = owner_autopay_cap(cfg.get("max_autopay_sats_per_day"))
    auto = auto_autopay_cap() if inv is None else None
    per_invoice = inv if inv is not None else auto["sats"]
    return {"per_invoice": per_invoice,
            "per_day": day if day is not None else per_invoice,
            "per_invoice_auto": inv is None, "per_day_auto": day is None,
            "auto_basis": auto["basis"] if auto else None}


def autopay_cap_words(cfg, caps=None):
    """"Auto: follows AgentHC's day price (now ≈ 61,000 sats)" or "Fixed:
    60,000 sats" — the per-invoice cap, for the owner."""
    caps = caps or autopay_caps(cfg)
    n = caps["per_invoice"]
    if not caps["per_invoice_auto"]:
        return f"Fixed: {n:,} sats"
    if caps["auto_basis"] == "fallback":
        return (f"Auto: follows AgentHC's day price (now {n:,} sats: "
                "AgentHC's price isn't known yet)")
    return f"Auto: follows AgentHC's day price (now ≈ {n:,} sats)"


AUTOPAY_HOW_TO_SET = ("say 'set autopay cap 80000' on the dashboard for a "
                      "fixed number, or 'set autopay cap auto' to follow "
                      "AgentHC's price (max_autopay_sats in config.json)")


def pass_options():
    """Pass sizes (signal days) the server sells right now, e.g. [1, 5];
    [] when every size is paused (the node is refilling); None if it can't
    be asked."""
    return pass_offer()["options"]


def pass_status(token):
    """{'status': pending|active|exits_only|used_up|superseded|expired|invalid,
    'days_left', 'valid_until', …}."""
    resp = _http.get(f"{AGENTHC_API}{PASS_PATH}",
                        headers={"Authorization": token}, timeout=15)
    if resp.status_code == 401:
        return {"status": "invalid"}
    resp.raise_for_status()
    return resp.json()


# ── tips ─────────────────────────────────────────────────────────────────────
# A tip is a thank-you to AgentHC: $100, $500 or $1,000, paid in sats at the
# live Bitcoin price. AgentHC prices the invoice from its own current quote
# (sats = ceil(usd * 1e8 / btc_usd)) and refuses when it has none. The OWNER
# pays it from their own wallet (QR / copy / lightning: link), exactly like a
# prepaid pass. It buys nothing. Nothing here pays anything: the agent's
# wallet never pays a tip (whatever max_autopay_sats says; every tier is far
# above it anyway), and no pass token, API key or config value is sent with
# these requests.

TIP_PATH = "/api/v1/payments/tip"
TIP_TIERS_USD = (100, 500, 1000)     # local hard allowlist, whatever AgentHC offers
# The /options tier sats (display only) must be within this fraction of
# amount_usd at the quote returned with them. An invoice's sats are held to
# the exact contract value instead (tip_sats_exact).
TIP_PRICE_BAND = 0.02
# AgentHC prices tips only from a quote at most this old (its pass cache
# TTL); an older quote on /options is not shown as the current price.
TIP_QUOTE_MAX_AGE_S = 900
# A BTC price (USD per BTC) outside this range is treated as broken. These
# are the bounds AgentHC itself applies before it prices a tip.
TIP_BTC_USD_RANGE = (1_000.0, 10_000_000.0)
TIPS_NOT_YET = ("Tips aren't open yet. Thanks for wanting to help — try again "
                "after the next AgentHC update.")
TIP_MISMATCH = ("AgentHC sent an invoice that doesn't match the tip you "
                "picked, so it wasn't shown. Nothing was paid.")
# Server refusals after which the tier list should be fetched again.
_TIP_REFRESH_CODES = ("tip_size_temporarily_unavailable", "tips_busy",
                      "invalid_tip_amount", "price_unavailable",
                      "node_unavailable")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class TipsUnavailable(RuntimeError):
    """The AgentHC server doesn't offer tips (not deployed yet, or off)."""


TIP_STATUS_UNKNOWN = ("Can't check this invoice right now. If your wallet "
                      "shows it paid, it reached AgentHC.")


class TipStatusUnknown(RuntimeError):
    """AgentHC answered a tip status poll with 404 (tips turned off, the tip
    record gone, or an older server). The invoice itself may still be
    payable, so this is never read as "expired"."""


class TipRefused(RuntimeError):
    """The server (or the local invoice check) refused this tip. `code` is
    the server's error code; `refresh` asks the UI to re-read the tiers."""

    def __init__(self, message, code=None, refresh=False):
        super().__init__(message)
        self.code = code
        self.refresh = refresh


def _is_tier(n):
    # bool is an int subclass: True must not pass as 1
    return type(n) is int and n in TIP_TIERS_USD


def _btc_price(v):
    """v as a float (USD per BTC) when it is a real number inside
    TIP_BTC_USD_RANGE, else None."""
    if type(v) not in (int, float) or v != v:  # rejects bool, str, None, NaN
        return None
    lo, hi = TIP_BTC_USD_RANGE
    return float(v) if lo <= v <= hi else None


def tip_sats_ok(amount_usd, amount_sats, btc_usd):
    """True when `amount_sats` is a positive int within TIP_PRICE_BAND of
    `amount_usd` dollars at `btc_usd` dollars per BTC."""
    price = _btc_price(btc_usd)
    if not _is_tier(amount_usd) or type(amount_sats) is not int \
            or amount_sats <= 0 or price is None:
        return False
    fair = amount_usd * 1e8 / price
    return abs(amount_sats - fair) <= fair * TIP_PRICE_BAND


def tip_sats_exact(amount_usd, amount_sats, btc_usd):
    """True when `amount_sats` is what AgentHC's contract says it charges for
    `amount_usd` dollars at `btc_usd` (a price in whole cents):
    ceil(amount_usd * 1e8 / btc_usd), in integer maths, give or take 1 sat."""
    price = _btc_price(btc_usd)
    if not _is_tier(amount_usd) or type(amount_sats) is not int \
            or amount_sats <= 0 or price is None:
        return False
    cents = round(price * 100)
    expected = -(-amount_usd * 10 ** 10 // cents)
    return abs(amount_sats - expected) <= 1


def _detail_message(resp):
    try:
        d = resp.json().get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(d, dict):
        return d.get("message") or None
    return d if isinstance(d, str) else None


_BECH32 = set("qpzry9x8gf2tvdw0s3jn54khce6mua7l")
_BOLT11_MULT = {"": 10 ** 11, "m": 10 ** 8, "u": 10 ** 5, "n": 100}


def bolt11_amount_msat(invoice):
    """Amount (millisatoshis) a mainnet BOLT11 invoice asks for, read from its
    human-readable part; None when it isn't a mainnet invoice, names no
    amount, or doesn't look like an invoice. Only the amount is checked (no
    checksum or signature): this catches a server sending the wrong amount,
    and the paying wallet shows the amount again before anything is paid."""
    if not isinstance(invoice, str):
        return None
    s = invoice.strip().lower()
    if not s.startswith("lnbc") or not 100 <= len(s) <= 2000:
        return None
    sep = s.rfind("1")
    if sep < 4 or not s[sep + 1:] or not set(s[sep + 1:]) <= _BECH32:
        return None
    m = re.fullmatch(r"(\d+)([munp]?)", s[4:sep])
    if not m or m.group(1).startswith("0"):
        return None
    digits, mult = int(m.group(1)), m.group(2)
    if mult == "p":
        return digits // 10 if digits % 10 == 0 else None
    return digits * _BOLT11_MULT[mult]


def bolt11_payment_hash(invoice):
    """The payment hash (64 hex chars) from a BOLT11 invoice's `p` field, or
    None when the text carries none. Every real invoice has exactly one: it
    is the id the paying wallet files the payment under, so the agent can
    ask the wallet later what became of a payment. No checksum or signature
    check (the wallet does those before it pays)."""
    for tag, vals in _bolt11_fields(invoice) or ():
        if tag == 1 and len(vals) == 52:
            bits = 0
            for v in vals:
                bits = (bits << 5) | v
            return f"{bits >> 4:064x}"
    return None


def bolt11_expiry_s(invoice):
    """Seconds a BOLT11 invoice stays payable after it was issued, from its
    `x` field (3600 when it has none — the standard's default); None when
    the text doesn't read as an invoice."""
    fields = _bolt11_fields(invoice)
    if fields is None:
        return None
    for tag, vals in fields:
        if tag == 6:
            secs = 0
            for v in vals:
                secs = (secs << 5) | v
            return secs
    return 3600


def _bolt11_fields(invoice):
    """The tagged fields of a BOLT11 invoice, in order, as (tag, [5-bit
    values]); None when the text doesn't read as an invoice."""
    if not isinstance(invoice, str):
        return None
    s = invoice.strip().lower()
    sep = s.rfind("1")
    if not s.startswith("ln") or sep < 2 or not set(s[sep + 1:]) <= _BECH32:
        return None
    alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    vals = [alphabet.index(c) for c in s[sep + 1:]]
    body = vals[7:-110]         # after the timestamp, before signature + checksum
    fields, i = [], 0
    while i + 3 <= len(body):
        tag, n = body[i], body[i + 1] * 32 + body[i + 2]
        i += 3
        if i + n > len(body):
            break               # cut short: keep what was read so far
        fields.append((tag, body[i:i + n]))
        i += n
    return fields


def _tip_quote(q):
    """The server's quote as {btc_usd, age_seconds, source}, or None when it
    is missing or its price is implausible."""
    if not isinstance(q, dict):
        return None
    price = _btc_price(q.get("btc_usd"))
    if price is None:
        return None
    age, src = q.get("age_seconds"), q.get("source")
    return {"btc_usd": price,
            "age_seconds": age if type(age) in (int, float) else None,
            "source": src if isinstance(src, str) else None}


def tip_options():
    """Tip tiers on offer. Only $ tiers on the local allowlist are kept, once
    each, in AgentHC's order. Each tier: amount_usd, amount_sats (the price
    right now, or None), available, reason. amount_sats is set to None
    unless it matches amount_usd at the quote returned with it."""
    resp = _http.get(f"{AGENTHC_API}{TIP_PATH}/options", timeout=10)
    if resp.status_code in (404, 405):
        raise TipsUnavailable(TIPS_NOT_YET)
    if resp.status_code != 200:
        raise RuntimeError(_detail_message(resp)
                           or f"tip options failed ({resp.status_code})")
    body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError("tip options: unexpected answer")
    quote = _tip_quote(body.get("quote"))
    # A quote too old for AgentHC to price with (it then offers no tier) is
    # not passed on as the current price. A shorter limit from AgentHC wins.
    max_age = body.get("quote_max_age_seconds")
    if type(max_age) not in (int, float) or not 0 < max_age < TIP_QUOTE_MAX_AGE_S:
        max_age = TIP_QUOTE_MAX_AGE_S
    if quote and not (quote["age_seconds"] is not None
                      and 0 <= quote["age_seconds"] <= max_age):
        quote = None
    tiers, seen = [], set()
    for t in body.get("tiers") or []:
        if not isinstance(t, dict) or not _is_tier(t.get("amount_usd")) \
                or t["amount_usd"] in seen:
            continue
        seen.add(t["amount_usd"])
        sats = t.get("amount_sats")
        if not quote or not tip_sats_ok(t["amount_usd"], sats, quote["btc_usd"]):
            sats = None
        tiers.append({**t, "amount_sats": sats})
    return {**body, "tiers": tiers, "quote": quote}


def create_tip(amount_usd):
    """Ask AgentHC for a tip invoice of `amount_usd` dollars (100, 500 or
    1000), paid in sats. The body sent is {"amount_usd": n} and nothing
    else. The answer is only returned when it is for that tier, its BOLT11
    invoice asks for exactly its amount_sats, and amount_sats is exactly
    what n dollars cost at the btc_usd it came with (tip_sats_exact). The
    caller still has to check that btc_usd against an independent price."""
    if not _is_tier(amount_usd):
        raise ValueError("Pick one of the tip amounts shown.")
    n = amount_usd
    resp = _http.post(f"{AGENTHC_API}{TIP_PATH}", json={"amount_usd": n},
                      timeout=20)
    if resp.status_code in (404, 405):
        raise TipsUnavailable(TIPS_NOT_YET)
    if resp.status_code in (400, 429, 503):
        code = _error_code(resp)
        raise TipRefused(_detail_message(resp)
                         or f"AgentHC couldn't make the tip invoice ({resp.status_code}).",
                         code=code, refresh=code in _TIP_REFRESH_CODES)
    if resp.status_code != 200:
        raise RuntimeError(f"tip invoice failed ({resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise TipRefused(TIP_MISMATCH, code="amount_mismatch")
    sats, quote = body.get("amount_sats"), body.get("quote")
    if (type(body.get("amount_usd")) is not int or body["amount_usd"] != n
            or type(sats) is not int or sats <= 0
            or bolt11_amount_msat(body.get("payment_request")) != sats * 1000
            or not tip_sats_exact(n, sats, body.get("btc_usd"))
            or (isinstance(quote, dict) and quote.get("btc_usd") != body["btc_usd"])
            or not isinstance(body.get("payment_hash"), str)
            or not _HEX64.fullmatch(body["payment_hash"])):
        raise TipRefused(TIP_MISMATCH, code="amount_mismatch")
    return body


def tip_status(payment_hash):
    """{'status': pending|paid|expired, …} for a tip invoice.

    Only the server's own "expired" status means expired. A 404 (tips turned
    off, the tip record lost, or an older server) raises TipStatusUnknown:
    turning tips off doesn't cancel the Lightning invoice, so it may still be
    paid, and the UI must not say nothing was charged."""
    if not isinstance(payment_hash, str) or not _HEX64.fullmatch(payment_hash):
        raise ValueError("That invoice id doesn't look right.")
    resp = _http.get(f"{AGENTHC_API}{TIP_PATH}/{payment_hash}", timeout=10)
    if resp.status_code == 404:
        raise TipStatusUnknown(_error_code(resp) or "not_found")
    if resp.status_code == 503:
        raise RuntimeError("status_unavailable")
    if resp.status_code != 200:
        raise RuntimeError(f"tip status failed ({resp.status_code})")
    return resp.json()


def _pass_tokens(cfg):
    """Prepaid pass tokens from config, newest purchase first."""
    out = []
    # feed_pass_extra: earlier invoices the owner may still pay, and paid
    # passes that aren't the main one (the dashboard files them there)
    extra = cfg.get("feed_pass_extra")
    entries = ([cfg.get("feed_pass_pending")]
               + (list(extra) if isinstance(extra, list) else [])
               + [cfg.get("feed_pass")])
    for entry in entries:
        tok = entry.get("token") if isinstance(entry, dict) else None
        if tok and tok not in out:
            out.append(tok)
    return out


def _token_id(token):
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _main_pass_token(cfg):
    main = cfg.get("feed_pass")
    return main.get("token") if isinstance(main, dict) else None


def _invoice_overdue(rec, now):
    """The pass invoice first seen in `rec` can't be paid any more (its
    lifetime, plus slack for clocks that differ, is over)."""
    try:
        first = float(rec.get("first_seen", now))
    except (TypeError, ValueError):
        first = 0.0
    return now - first > _INVOICE_LIFETIME_S + 300


def _pass_status_unknown(cfg, rec, token, now):
    """True while a saved pass status can't be relied on until AgentHC is
    asked again: an invoice past its lifetime that AgentHC has not answered
    for since (the owner may have paid it while the agent wasn't checking),
    an "unpaid" saved by an older version from elapsed time alone, or an
    "unpaid" pass that has since become the main pass (the dashboard saw it
    paid). The agent reads nothing and the wallet buys nothing meanwhile."""
    st = rec.get("status")
    if st == "pending":
        return _invoice_overdue(rec, now)
    if st == "unpaid":
        try:
            said_after_expiry = (float(rec.get("checked") or 0)
                                 > float(rec.get("first_seen") or 0)
                                 + _INVOICE_LIFETIME_S + 300)
        except (TypeError, ValueError):
            said_after_expiry = False
        return (not said_after_expiry
                or (token == _main_pass_token(cfg) and not rec.get("main")))
    return False


def _known_status(cfg, rec, token, now=None):
    """A pass's saved status, or None while it is not known (no check has
    answered yet, or _pass_status_unknown). No request."""
    rec = rec if isinstance(rec, dict) else {}
    now = time.time() if now is None else now
    return None if _pass_status_unknown(cfg, rec, token, now) else rec.get("status")


def _refresh_pass(cfg, state, save_state, force=False):
    """Return the usable (active) pass token, checking the server only when
    needed: pending invoices at most once a minute, an invoice past its
    lifetime until AgentHC answers for it (only AgentHC's answer makes it
    "unpaid", never the time alone), "unpaid" ones once an hour and as soon
    as one becomes the main pass, active passes only after their end time
    or when the feed refused them."""
    cache = state.setdefault("pass_status", {})
    now = time.time()
    main = _main_pass_token(cfg)
    best, best_until = None, 0.0
    for token in _pass_tokens(cfg):
        tid = _token_id(token)
        rec = cache.get(tid) or {}
        st = rec.get("status")
        stale = (force or st is None
                 or _pass_status_unknown(cfg, rec, token, now)
                 or (st == "pending" and now - rec.get("checked", 0) > _PENDING_RECHECK_S)
                 or (st == "unpaid" and now - rec.get("checked", 0) > _UNPAID_RECHECK_S)
                 or (st in _USABLE and now > rec.get("until_ts", 0)))
        if stale:
            try:
                fresh = pass_status(token)
            except Exception:
                fresh = None
            if fresh is not None:
                until_ts = _iso_ts(fresh.get("access_until"))
                first_seen = rec.get("first_seen", now)
                status = fresh.get("status")
                if status == "pending" and _invoice_overdue({"first_seen": first_seen}, now):
                    # AgentHC itself says not paid, and it can't be any more
                    status = "unpaid"
                rec = {"status": status, "until_ts": until_ts,
                       "days_left": fresh.get("days_left", fresh.get("trading_days_left")),
                       "checked": now, "first_seen": first_seen,
                       "main": token == main}
                if fresh.get("exits_until"):
                    rec["exits_until"] = _iso_ts(fresh["exits_until"]) or None
                cache[tid] = rec
                save_state(state)
        if rec.get("status") in _USABLE:
            # prefer a pass with days left over one in its exits-only grace
            rank = rec.get("until_ts", 0) + (1e12 if rec.get("status") == "active" else 0)
            if rank > best_until:
                best, best_until = token, rank
    return best


def _iso_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pass_notice(cfg, state, save_state, key, text):
    """Once-per-day notification (dashboard + Discord/ntfy/Telegram)."""
    day = time.strftime("%Y-%m-%d")
    if state.get(key) == day:
        return
    state[key] = day
    state["pass_notice"] = text
    save_state(state)
    print(text)
    try:
        from notifications import notify
        notify(cfg, text)
    except Exception:
        pass


def _daypass_alert(cfg, state, save_state, key, text):
    """A wallet day-pass problem the owner must hear about: once per day per
    kind (Discord/ntfy/Telegram), and on the dashboard's feed-access line
    until _daypass_alert_clear takes it off (a pass is working again)."""
    shown = state.get("daypass_alert") == key and state.get("pass_notice") == text
    state["daypass_alert"] = key
    state["pass_notice"] = text
    if state.get(key) != time.strftime("%Y-%m-%d"):
        _pass_notice(cfg, state, save_state, key, text)
    elif not shown:
        save_state(state)


def _entry_read(state, save_state):
    """The feed was just read with entry access: a saved pass notice (a
    lapse, a used-up pass) is no longer true, so it goes. A current wallet
    day-pass alert stays: it is cleared when that problem ends."""
    if state.get("pass_notice") and not state.get("daypass_alert"):
        state.pop("pass_notice", None)
        save_state(state)


def _daypass_alert_clear(state):
    """Take a day-pass alert off the dashboard. Returns True when it did."""
    if not state.pop("daypass_alert", None):
        return False
    state.pop("pass_notice", None)
    return True


def _retry_text():
    """When the agent next tries to buy a day-pass after a try that failed."""
    start = _OPEN_MIN - _BUY_LEAD_MIN
    opens = f"{start // 60 % 12 or 12}:{start % 60:02d} {'AM' if start < 720 else 'PM'} ET"
    return (f"It tries again every {_PAY_RETRY_S // 60} minutes until today's "
            f"market close; after that, from {opens} on the next market day.")


class InvoiceRefused(RuntimeError):
    """The agent decided not to pay a day-pass invoice. Nothing was sent to
    the wallet. The message is plain words for the owner."""


def _daypass_bought_day(tok):
    """The ET day a wallet day-pass token was bought (tokens from older
    versions carry no paid_day: they were bought 23h before they expire)."""
    tok = tok or {}
    if isinstance(tok.get("paid_day"), str):
        return tok["paid_day"]
    exp = tok.get("expires_at")
    if isinstance(exp, (int, float)) and exp > _DAYPASS_S:
        return datetime.fromtimestamp(exp - _DAYPASS_S, MARKET_TZ).strftime("%Y-%m-%d")
    return None


def _live_daypass(state):
    """True while a wallet-paid 24h day-pass token is still running."""
    tok = (state or {}).get("l402") or {}
    return bool(tok.get("token") and tok.get("expires_at", 0) > time.time())


def _token_status(state, token):
    return ((state.get("pass_status") or {}).get(_token_id(token)) or {}).get("status")


# ── which events a read asks for ────────────────────────────────────────────
# The feed answers with its newest `limit` events of one track ("main") or
# of every track ("all"), newest first. Two ways that lost exit signals:
#   - "other trades" switched off while the agent still held one: reading
#     track=main never listed its EXITED, so it rode to the expiry guard.
#     The agent reads track=all while its ledger (or the exits it still
#     owes) holds something known to come from other trades (a row that
#     doesn't say counts as the main pick); the agent drops other-trades
#     ENTERED events itself, and their EXITED events unless the ledger row
#     for that contract is itself an other-trades one
#     (agent._handle_feed_events) — a shadow-book exit never sells a main
#     position.
#   - 20 events of track=all are a few hours of the shadow book: an exit
#     published before a sleep or restart fell out of the window. Reads of
#     track=all, and the first read after a gap, ask for FEED_LIMIT_MAX.
#     A full window whose oldest event is newer than the last good read
#     may still have dropped one: _check_window keeps the no-signal sell
#     armed for what was held then (state["feed_gap"]).
# Swing trades (track "swing", a third category next to the main pick and
# "other trades"): AgentHC's rare multi-day calls after a VIX spike, held up
# to SWING_HOLD_SESSIONS trading sessions. The owner opts in
# (include_swing_trades, off by default); the feed has no "main + swing"
# read, so with swing on the agent reads track=all and drops other-trades
# entries itself while "other trades" is off (agent._handle_feed_events).
# A swing EXIT is owed to whoever got its ENTRY even after the pass ends:
# while a real swing position is held, a pass that has ended is still
# offered for exits only, saying mode=off (never charged, nothing bought:
# _ended_pass_exits).
SWING_TRACK = "swing"
SWING_HOLD_SESSIONS = 5


def _track_book(track):
    """The agent's book for a raw track: "main", "swing" or "shadow"."""
    t = str(track or "main")
    return t if t in ("main", SWING_TRACK) else "shadow"


FEED_LIMIT = 20          # main pick only, read every loop: plenty
FEED_LIMIT_MAX = 100     # the most AgentHC lists in one answer
FEED_GAP_S = 300         # longer than this since the last good read: a gap


def _row_track(row):
    """The track a ledger row was opened from: its own "track", else the
    one in the id of the event that opened it (only a non-main AgentHC id
    ends in "|<track>": ENTERED|T|expiry|strike|type|occurred_at[|track]),
    else None (a row that doesn't say: a practice row of an older version).
    Its "book" (agent per-book exits: "main" | "shadow" | "swing") wins over
    a track that contradicts it, as in the agent (agent._row_book /
    _row_track)."""
    track, book = row.get("track"), row.get("book")
    book = book if book in ("main", "shadow", SWING_TRACK) else None
    if (isinstance(track, str) and track
            and (book is None or _track_book(track) == book)):
        return track
    if book is not None:
        return {"main": "main", SWING_TRACK: SWING_TRACK}.get(book, "other")
    opened = row.get("opened_event")
    if isinstance(opened, str) and opened:
        parts = opened.split("|")
        return parts[6] if len(parts) >= 7 and parts[6] else "main"
    return None


def held_as_other(row):
    """True for a ledger row known to come from "other trades". A row that
    doesn't say (a practice row of an older version, a row the ledger check
    rebuilt without its opening event) counts as the main pick: with other
    trades off it never switches reads to every track, and no other-trades
    exit is let through for it — as before tracks were remembered."""
    track = _row_track(row) if isinstance(row, dict) else None
    return track is not None and track != "main"


def held_track(row):
    """The raw track a ledger row was opened from (_row_track), or None
    (a row that doesn't say: the main pick)."""
    return _row_track(row) if isinstance(row, dict) else None


def held_as_swing(row):
    """True for a ledger row known to come from swing trades."""
    return held_track(row) == SWING_TRACK


def _holds_real_swing(state):
    """The ledger holds a swing position bought with a real order (the
    reading of _holds_real_position)."""
    rows = (state or {}).get("positions")
    for row in (rows.values() if isinstance(rows, dict) else ()):
        if (isinstance(row, dict) and held_as_swing(row)
                and (row.get("live") is True or (
                    not row.get("dry") and row.get("option_id") != "dry"))):
            return True
    return False


def _holds_other_track(state):
    """The ledger, or the exits still owed, hold something known to come
    from other trades (held_as_other)."""
    rows = (state or {}).get("positions")
    for row in (rows.values() if isinstance(rows, dict) else ()):
        if held_as_other(row):
            return True
    owed = (state or {}).get("exits_to_retry")
    for ev in (owed if isinstance(owed, list) else ()):
        if isinstance(ev, dict) and str(ev.get("track", "main")) != "main":
            return True
    return False


def _last_read(state):
    rec = (state or {}).get("feed_read")
    return rec if isinstance(rec, dict) else {}


def _feed_params(cfg, state):
    """{"limit", "track"} for the next read of the feed: every track while
    "other trades" or swing trades are on (AgentHC has no "main + swing"
    read: the agent drops the tracks it doesn't take) or a non-main position
    or owed exit is held, else the main pick only."""
    track = ("all" if (cfg.get("include_other_trades")
                       or cfg.get("include_swing_trades")
                       or _holds_other_track(state))
             else "main")
    last = _last_read(state).get("local")
    gap = (not isinstance(last, (int, float)) or isinstance(last, bool)
           or not 0 <= time.time() - last <= FEED_GAP_S)
    return {"limit": FEED_LIMIT_MAX if track == "all" or gap else FEED_LIMIT,
            "track": track}


def _epoch(value):
    """An ISO time (no offset = UTC) or epoch seconds as epoch seconds, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def _id_track(event_id):
    """The track in an AgentHC event id (non-main ids end in "|<track>")."""
    parts = str(event_id).split("|")
    return parts[6] if len(parts) >= 7 and parts[6] else "main"


def _opened_mark(row):
    """What tells this position apart from a later one under the same
    contract: the event that opened it, else when (None: a practice row,
    which says neither)."""
    return row.get("opened_event") or row.get("opened_at") or None


def _check_window(cfg, state, save_state, body, params, events):
    """After a good read: remember it, and keep the no-signal sell armed
    while an exit may have fallen out of the feed's window.

    A full window (as many events as asked for) whose oldest event is newer
    than the last good read may have dropped events in between — an exit
    for a position held now among them. (With no earlier read on record:
    a full window that lists nothing the agent has handled, judged only
    by the tracks it has handled events of: the first read of every track
    after reading the main pick only is no gap by itself.) What the
    ledger holds then is kept in state["feed_gap"] (each row with the
    event or time it was opened: a position bought again later under the
    same contract is not one of them); while any of it is still held, this
    read counts as one without exit access (cause "feed_gap"): the owner
    is warned and, if no exit comes, those rows — only those — are sold at
    the no-signal sell time (agent.signal_loss_tick). Never raises."""
    try:
        raw = body.get("events") if isinstance(body, dict) else None
        raw = [e for e in (raw if isinstance(raw, list) else [])
               if isinstance(e, dict)]
        if any(e.get("practice_release") for e in raw):
            # practice: completed trades, each exit with its entry. AgentHC's
            # `limit` counts the exits, and an entry keeps its original
            # (older) time: the window is judged on the exits alone
            raw = [e for e in raw
                   if e.get("practice_release") and e.get("event") == "EXITED"]
            events = [e for e in events
                      if e.get("practice_release") and e.get("event") == "EXITED"]
        rows = state.get("positions")
        held = sorted(k for k, r in (rows.items() if isinstance(rows, dict) else ())
                      if isinstance(r, dict))
        server_now = _epoch(body.get("timestamp")) if isinstance(body, dict) else None
        prev = _last_read(state)
        if held and len(raw) >= int(params.get("limit") or FEED_LIMIT):
            times = [t for t in (_epoch(e.get("occurred_at")) for e in raw
                                 if isinstance(e, dict)) if t is not None]
            since = prev.get("server") or prev.get("local")
            if isinstance(since, (int, float)) and not isinstance(since, bool):
                lost = bool(times) and min(times) > since
            else:
                seen = set(i for i in (state.get("seen") or ())
                           if isinstance(i, str))
                tracks = {_id_track(i) for i in seen}
                alike = [e for e in events
                         if not tracks or str(e.get("track", "main")) in tracks]
                lost = bool(alike) and not any(e["event_id"] in seen
                                               for e in alike)
            if lost:
                state["feed_gap"] = {"since": time.time(), "keys": held,
                                     "opened": {k: _opened_mark(rows[k])
                                                for k in held}}
                save_state(state)
                text = ("agentic-trader: your agent was away long enough that "
                        "AgentHC published more signals than one read lists, so "
                        "an exit signal for a position you hold may have been "
                        "missed (" + ", ".join(held) + "). If no exit arrives, "
                        "the agent sells it at the no-signal sell time.")
                print(text)
                try:
                    from notifications import notify
                    notify(cfg, text)
                except Exception:
                    pass
        state["feed_read"] = {"local": time.time(), "server": server_now}
        gap = state.get("feed_gap")
        if isinstance(gap, dict):
            marks = gap.get("opened") if isinstance(gap.get("opened"), dict) else {}
            # still held, and the same position (not one bought again since)
            still = [k for k in (gap.get("keys") or ())
                     if isinstance((rows or {}).get(k), dict)
                     and _opened_mark(rows[k]) == marks.get(k)]
            if still:
                _LAST_POLL.update(exits=False, why="feed_gap")
                if still != gap.get("keys"):
                    gap["keys"] = still
                    save_state(state)
            else:
                state.pop("feed_gap", None)
                save_state(state)
    except Exception as exc:
        print(f"feed window check failed (non-fatal): {str(exc)[:150]}")


def _held_keys(state):
    rows = (state or {}).get("positions")
    return ({k for k, r in rows.items() if isinstance(r, dict)}
            if isinstance(rows, dict) else set())


def _event_key(ev):
    """The ledger's key for this event's contract (agent: state["positions"])."""
    return f"{ev['ticker']}|{ev['expiry']}|{ev['strike']}|{ev['type']}"


def _real_exits_read(cfg, state, params, headers):
    """In practice, while the agent holds a REAL position: (body, exits) of
    a second read that says mode=off — EXITED events only, never charged,
    and served to every key or pass that may read practice (and to a
    Premium key AgentHC won't serve practice to). A practice read gets
    completed trades only, and AgentHC leaves out an exit whose entry it
    can no longer pair: the exit of a position bought with real money must
    not depend on that. None when not in practice, nothing real is held,
    or the read failed (the practice read still brings the exits it can
    pair)."""
    if feed_mode(cfg) != "dry_run" or not _holds_real_position(state):
        return None
    try:
        resp = _feed_get(cfg, params, headers, mode="off")
        if getattr(resp, "status_code", 0) != 200:
            return None
        body = resp.json()
        exits = [e for e in _parse_events(body)
                 if e["event"] == "EXITED" and not e.get("practice_release")]
    except Exception:
        return None
    return body, exits


def _finish_read(cfg, state, save_state, body, params, events, headers):
    """After a good read: the window check (_check_window), and in practice
    with a real position held the exits-only read (_real_exits_read). Its
    exits for contracts the ledger holds that the practice read left out
    are added (untagged: they go the usual exit way), and the window is
    judged on it: it lists every exit, the practice read only those it
    could pair. Every other exit stays with the practice read (a practice
    trade is booked from its pair, never from a lone exit)."""
    got = _real_exits_read(cfg, state, params, headers)
    if got is None:
        _check_window(cfg, state, save_state, body, params, events)
        return events
    off_body, exits = got
    _check_window(cfg, state, save_state, off_body, params, exits)
    have = {e["event_id"] for e in events}
    held = _held_keys(state)
    return events + [e for e in exits
                     if e["event_id"] not in have and _event_key(e) in held]


def _refused_mode(resp):
    """The agent mode AgentHC says it refused ("agent_mode" in a 402 body,
    or in a 403's detail), or None."""
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    mode = body.get("agent_mode")
    if mode is None and isinstance(body.get("detail"), dict):
        mode = body["detail"].get("agent_mode")
    return mode if mode in FEED_MODES else None


PRACTICE_KEY_NOT_SERVED_TEXT = (
    "AgentHC does not serve practice to your API key: practice reads are "
    "free only for a key whose own paid tier is Premium or higher (a key "
    "lifted to Premium by $INTELL holdings is not). Nothing was charged, "
    "and your key's balance is not the problem. Practice logs no trades "
    "with this key. While your agent holds a real position it still reads "
    "that position's exit signals. Go LIVE to trade the signals with the key.")


def _practice_not_served(cfg, state, save_state, params, headers):
    """A Premium key in practice that AgentHC won't serve practice to (it
    says agent_mode dry_run in the refusal): say so — not that the key's
    balance is empty — and, while a real position is held, read its exits
    with mode=off (served to the key, never charged)."""
    _pass_notice(cfg, state, save_state, "practice_key_notified",
                 PRACTICE_KEY_NOT_SERVED_TEXT)
    got = _real_exits_read(cfg, state, params, headers)
    if got is None:
        return _no_read("feed_refused")
    body, exits = got
    _LAST_POLL.update(access=False, exits=True)   # exits only
    _check_window(cfg, state, save_state, body, params, exits)
    held = _held_keys(state)
    return [e for e in exits if _event_key(e) in held]


def _wallet_over_exits_only(cfg, state, save_state):
    """Buy today's wallet day-pass while the prepaid pass only gets exits,
    and read the feed with it. None when it was not bought (the caller
    then reads the exits with the used-up pass)."""
    try:
        resp = _feed_get(cfg, _feed_params(cfg, state))
        if resp.status_code != 402:
            return None
        if not _buy_day_pass(cfg, state, resp.json(), save_state):
            return None
    except Exception as exc:
        print(f"Day-pass purchase didn't go through ({str(exc)[:160]}); exit "
              "signals are still read with the used-up prepaid pass.")
        return None
    return poll(cfg, state, save_state, _retried=True)


def _prepaid_may_take_over(cfg, state, entries=False):
    """A prepaid pass that may give entries once the wallet day-pass ends:
    one with signal days left as last checked, or one the agent has not
    been able to check (a pass the owner just bought in the dashboard has
    no status here until the agent asks AgentHC about it). No request.
    `entries` (entry_cover): only a pass never checked yet counts as not
    known; one whose saved status went stale (an invoice past its lifetime
    AgentHC hasn't answered for, an old "unpaid") counts by that saved
    status, so new entries are refused rather than allowed on a guess. The
    renewal keeps treating it as unknown: the wallet never pays on one."""
    cache = (state or {}).get("pass_status") or {}
    if entries:
        return any(((cache.get(_token_id(t)) or {}).get("status")
                    in (None, "active")) for t in _pass_tokens(cfg))
    return any(_known_status(cfg, cache.get(_token_id(t)), t) in (None, "active")
               for t in _pass_tokens(cfg))


def _close_ts(cfg, day):
    """Epoch seconds of the market close on `day` (a date)."""
    from brokers import market_hours
    close = market_hours.close_minute(cfg, day)
    return datetime(day.year, day.month, day.day, close // 60, close % 60,
                    tzinfo=MARKET_TZ).timestamp()


def entry_cover(cfg, state, close_ts):
    """(ok, until_ts): can a position opened now still get its exit signal
    until `close_ts` (today's close)? False only while the feed is read with
    a wallet day-pass that ends before then and no other pass takes over
    (a new one is not paid yet; a prepaid pass not checked yet counts as
    taking over). until_ts = when that day-pass ends (None without one).
    An API key, a prepaid pass and other sources: always ok."""
    if cfg.get("agenthc_api_key") or not _live_daypass(state):
        return True, None
    until = float((state.get("l402") or {}).get("expires_at") or 0)
    if until >= close_ts or _prepaid_may_take_over(cfg, state, entries=True):
        return True, until
    return False, until


def _renew_daypass(cfg, state, save_state):
    """Recurring day-pass ON and the running wallet day-pass ends before
    today's close: buy today's pass first (from 08:30 ET on a trading day,
    the usual one-a-day and spending limits), so it never runs out between
    an entry and its exit. A purchase that fails or isn't decided yet
    leaves the running pass in use. Never while a prepaid pass may take
    over when the day-pass ends: its status is asked first (poll() does not
    ask while a day-pass runs, so a pass bought in the dashboard since then
    is unknown here), and a pass that can't be checked counts as taking
    over — the wallet never pays on an unknown."""
    tok = state.get("l402") or {}
    if not cfg.get("day_pass_recurring") or state.get("daypass_pending"):
        return
    now = _now_et()
    if _next_buy_time(cfg, now) is not None:
        return   # not a trading day, before 08:30, or past the close
    running = float(tok.get("expires_at") or 0)
    if running >= _close_ts(cfg, now.date()):
        return
    if _pass_tokens(cfg):
        _refresh_pass(cfg, state, save_state)
        if _prepaid_may_take_over(cfg, state):
            return
    if not _pass_purchase_allowed(cfg, state, save_state):
        return
    try:
        resp = _feed_get(cfg, _feed_params(cfg, state))
        if resp.status_code == 402:
            _buy_day_pass(cfg, state, resp.json(), save_state,
                          running_until=running)
    except Exception as exc:
        print(f"Day-pass renewal didn't go through ({str(exc)[:160]}); the "
              "running pass is still used.")


def _headers(cfg, state):
    if cfg.get("agenthc_api_key"):
        return {"X-API-Key": cfg["agenthc_api_key"]}
    tok = (state or {}).get("l402") or {}
    if tok.get("token") and tok.get("expires_at", 0) > time.time():
        return {"Authorization": tok["token"]}
    return {}


def register_key():
    name = input("Name for your agent on AgentHC (e.g. my-trade-agent): ").strip() \
        or "agentic-trader"
    resp = _http.post(f"{AGENTHC_API}/api/v1/agents/register",
                         json={"name": name}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    key = body.get("api_key") or body.get("key")
    print(f"Registered. agent_id={body.get('agent_id')}")
    print("NOTE: the feed requires PREMIUM tier — see POST /api/v1/agents/upgrade")
    print(f"      and {AGENTHC_API}/docs for upgrade options.")
    return key


def accept_terms(cfg):
    resp = _http.post(f"{AGENTHC_API}{FEED_PATH}/accept-terms",
                         headers={"X-API-Key": cfg["agenthc_api_key"]}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    print(f"AgentHC feed terms accepted (version {body.get('terms_version')}).")
    return body.get("terms_version")


def _pass_purchase_allowed(cfg, state, save_state, now=None):
    """Recurring-day-pass gate: the agent only auto-pays the ~$50 pass when
    the operator turned 'Recurring day-pass' ON (wizard Source tab / config
    day_pass_recurring). OFF (the default) = the agent never spends without
    approval — it notifies once per day and returns no events instead.

    Never buys while the market is closed, regardless of the recurring
    flag: only on a market day (weekends and market holidays excluded), from
    an hour before the 9:30 AM ET open until that day's close (1 PM on early
    closes). The feed only publishes during the session, so a pass bought on
    a Friday evening, a Saturday or the night before a holiday would run out
    having covered no market time at all — and one bought at midnight wastes
    the hours before the open. The API also stops selling passes on
    non-trading days server-side; this check keeps every consumer safe even
    against an older server.

    Never buys while the wallet is known to pay without giving the receipt
    the pass is made from (see _no_receipt_stop).

    One paid day-pass per day: never buys on a day the prepaid pass was
    already charged, and never buys a second day-pass the same day (the
    owner paid for today once already) — a lost or refused token is not a
    reason to pay again. Only an owner who set max_autopay_sats_per_day
    themselves (and has no prepaid pass) allows more, up to that many sats.

    Only LIVE buys: practice (dry_run) and a paused agent (off) never buy a
    day-pass (feed_mode). An owner whose wallet would buy (a wallet and
    'Recurring day-pass' ON) is told so once a day; with no wallet or
    Recurring off, going live would buy nothing either, so practice is not
    named as the reason. Once LIVE (or with nothing to buy), a practice /
    paused notice still on the dashboard goes."""
    mode = feed_mode(cfg)
    wallet_buys = bool(cfg.get("lnbits_url") and cfg.get("day_pass_recurring"))
    if mode == "live" or not wallet_buys or cfg.get("agenthc_api_key"):
        _clear_not_live_notice(state, save_state)
    if cfg.get("agenthc_api_key"):
        # A Premium key reads the feed with the key only: a wallet day-pass
        # would never be sent, so buying one only loses sats (poll() tells
        # the owner to top up the key instead).
        return False
    if mode != "live":
        if wallet_buys:
            _not_live_notice(state, save_state, mode)
        return False
    now = now or _now_et()
    day = now.strftime("%Y-%m-%d")
    paid_today = (state.get("pass_charged_day") == day
                  or (_one_pass_a_day(cfg)
                      and _autopay_spent_today(state, now)["count"] >= 1))
    if paid_today:
        if state.get("pass_day_skip_notified") != day:
            state["pass_day_skip_notified"] = day
            save_state(state)
            print("Day-pass purchase skipped: today is already paid for "
                  "(prepaid pass or an earlier wallet day-pass) — the wallet "
                  "will not pay a second time today.")
        return False
    next_buy = _next_buy_time(cfg, now)
    if next_buy is not None:
        if state.get("market_closed_skip_notified") != day:
            state["market_closed_skip_notified"] = day
            save_state(state)
            hour, minute = next_buy.hour, next_buy.minute
            print("Day-pass purchase waits: the market is closed, so a pass "
                  "bought now would cover no market time. The agent buys the "
                  f"next one on {next_buy:%A %b} {next_buy.day} at about "
                  f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'} ET "
                  "(only if 'Recurring day-pass' is ON).")
        return False
    if cfg.get("day_pass_recurring"):
        if _no_receipt_stop(cfg, state, save_state):
            return False  # this wallet pays but can't give the receipt
        if time.time() < float(state.get("daypass_retry_at") or 0):
            return False  # the last try failed (already reported): wait a bit
        spent = _autopay_spent_today(state, now)
        cap = autopay_caps(cfg)["per_day"]
        if spent["sats"] >= cap:
            day = now.strftime("%Y-%m-%d")
            if state.get("autopay_cap_notified") != day:
                state["autopay_cap_notified"] = day
                save_state(state)
                print(f"Day-pass auto-pay STOPPED for today: the wallet "
                      f"already paid {spent['sats']:,} sats "
                      f"({spent['count']} pass(es)) today, daily cap {cap:,} "
                      "(max_autopay_sats_per_day). If a "
                      "pass you paid for was rejected, contact the feed "
                      "operator before raising the cap. Resumes tomorrow.")
            return False
        return True
    day = time.strftime("%Y-%m-%d")
    if state.get("pass_needed_notified") != day:
        state["pass_needed_notified"] = day
        save_state(state)
        print(f"Day-pass required (~{usd_words(day_pass_price()['usd'])} in sats) "
              "but recurring payments are "
              "OFF — turn ON 'Recurring day-pass' in the wizard Source tab "
              "to let the agent buy it each day.")
    return False


PRACTICE_NO_PASS_TEXT = (
    "Practice never uses a signal day or buys a pass; only LIVE uses days. "
    "Your agent is in practice (dry-run), so it does not buy a day-pass. "
    "A signal pass you already have keeps working and is not "
    "charged for practice. With no pass, the dashboard's practice preview "
    "shows AgentHC's free list of closed trades and whether your rules would "
    "have taken each one. Go LIVE to have the wallet buy day-passes again.")
OFF_NO_PASS_TEXT = (
    "Your agent is paused, so it does not buy a day-pass, and a "
    "paused agent never uses a signal day; only LIVE uses days. A signal "
    "pass you already have still brings the exit signals for the positions "
    "you hold. Press Resume to trade (and buy day-passes) again.")


def _clear_not_live_notice(state, save_state):
    """Live again (or nothing the wallet would buy): the practice / paused
    notice is no longer true, so it leaves the dashboard."""
    changed = state.pop("not_live_pass_notified", None) is not None
    if state.get("pass_notice") in (PRACTICE_NO_PASS_TEXT, OFF_NO_PASS_TEXT):
        state.pop("pass_notice", None)
        changed = True
    if changed:
        save_state(state)


def _not_live_notice(state, save_state, mode):
    """Once a day: practice / a paused agent buys no day-pass (printed for
    the log, and on the dashboard's feed-access line; no push message —
    nothing is wrong)."""
    day = time.strftime("%Y-%m-%d")
    key = f"{mode}:{day}"
    if state.get("not_live_pass_notified") == key:
        return
    state["not_live_pass_notified"] = key
    text = OFF_NO_PASS_TEXT if mode == "off" else PRACTICE_NO_PASS_TEXT
    if not state.get("daypass_alert"):
        state["pass_notice"] = text
    save_state(state)
    print(text)


def _no_receipt_text(sats):
    return (f"Your agent's wallet paid {int(sats or 0):,} sats for a day-pass — the "
            "sats left the wallet — but the wallet cannot give the payment "
            "receipt (the \"preimage\") that AgentHC needs to open the feed, "
            "so that pass could not be used. To stop this from costing you "
            "every day, your agent will NOT buy another day-pass with this "
            "wallet. The agent gets no new trades. To start again: "
            "connect a different Lightning wallet, or buy signal days with a "
            "prepaid pass (dashboard → Settings → Signal source). If you "
            "know the wallet is fixed, switch 'Recurring day-pass' off and "
            "on again and the agent will try it once more.")


def _no_receipt_stop(cfg, state, save_state):
    """True while auto-buying is stopped because this wallet paid for a
    day-pass without giving the receipt (preimage) the pass is made from —
    buying again would lose the same sats every market day. The stop ends
    when the owner connects a different wallet, or switches 'Recurring
    day-pass' on again after it began. Reminds the owner once a day."""
    stop = state.get("daypass_no_receipt")
    if not isinstance(stop, dict):
        return False
    try:
        switched_on_at = float(cfg.get("day_pass_recurring_on_at") or 0)
    except (TypeError, ValueError):
        switched_on_at = 0.0
    if (stop.get("wallet") != wallet_fingerprint(cfg)
            or switched_on_at > float(stop.get("at") or 0)):
        state.pop("daypass_no_receipt", None)
        if state.get("daypass_alert") == "daypass_noproof_notified":
            _daypass_alert_clear(state)
        save_state(state)
        return False
    _daypass_alert(cfg, state, save_state, "daypass_noproof_notified",
                   _no_receipt_text(stop.get("sats")))
    return True


def _one_pass_a_day(cfg):
    """True unless the owner chose to allow more than one wallet day-pass a
    day by setting max_autopay_sats_per_day (never with a prepaid pass)."""
    return (bool(_pass_tokens(cfg))
            or owner_autopay_cap(cfg.get("max_autopay_sats_per_day")) is None)


def _next_buy_time(cfg, now):
    """None when a wallet day-pass may be bought right now: a market day,
    between _BUY_LEAD_MIN before the open and that day's close — so every
    pass covers market time. Otherwise the ET time buying opens again."""
    from brokers import market_hours   # holiday + early-close aware
    start = _OPEN_MIN - _BUY_LEAD_MIN
    minute = now.hour * 60 + now.minute
    for ahead in range(15):
        day = now.date() + timedelta(days=ahead)
        if not market_hours.is_trading_day(cfg, day):
            continue
        if ahead == 0:
            if minute >= market_hours.close_minute(cfg, day):
                continue
            if minute >= start:
                return None
        return datetime(day.year, day.month, day.day, start // 60, start % 60,
                        tzinfo=MARKET_TZ)
    return now + timedelta(days=1)   # 15 closed days in a row: ask again tomorrow


def _daily_autopay_cap(cfg):
    """Total sats the agent may auto-pay per market day (autopay_caps).
    Defaults to ONE invoice's worth (the per-invoice cap): a healthy feed
    never needs a second pass the same day, so a second purchase means
    something is wrong (e.g. the server keeps rejecting a valid pass) —
    without this cap the agent would re-buy on every poll and drain the
    wallet. Counted from the amounts in the invoices themselves; only
    payments that went through, or whose outcome is still unknown, count."""
    return autopay_caps(cfg)["per_day"]


def autopay_refusal_text(amount, cfg, caps=None):
    """Why a day-pass invoice above the per-invoice cap was not paid: the
    cap in force now and how to raise it."""
    caps = caps or autopay_caps(cfg)
    need = _plus_margin_k(amount)
    return (f"The day-pass costs {amount:,} sats, which is more than your "
            f"auto-pay cap for one pass ({caps['per_invoice']:,} sats), so "
            "the agent did not pay. Nothing was paid. Your cap now: "
            f"{autopay_cap_words(cfg, caps)}. If that price is right, raise "
            f"it: say 'set autopay cap {need}' on the dashboard (or set "
            f"max_autopay_sats to {need} in config.json).")


def _record_paid_day(state, day):
    """Remember an ET day the feed was paid for (prepaid-pass day charged or
    wallet day-pass bought) — the dashboard's pass-day P&L reads this list.
    Returns True when it changed (caller saves)."""
    days = state.setdefault("paid_days", [])
    if day in days:
        return False
    days.append(day)
    days.sort()
    del days[:-2000]  # ~8 years of market days
    return True


def _autopay_spent_today(state, now=None):
    day = (now or _now_et()).strftime("%Y-%m-%d")
    spent = state.get("autopay_spent") or {}
    if spent.get("day") != day:
        spent = {"day": day, "sats": 0, "count": 0}
    return spent


def _invoice_sats(invoice, claimed):
    """Whole sats the invoice ITSELF asks for (rounded up) — the only amount
    the caps are checked against, because it is what the wallet would pay.
    Raises when the invoice names no amount, or when the server's amount_sats
    field says something else: the agent never pays an amount it can't
    establish."""
    msat = bolt11_amount_msat(invoice)
    if not msat or msat <= 0:
        raise InvoiceRefused(
            "The feed sent a Lightning invoice with no readable amount, so "
            "the agent did not pay it. Nothing was paid.")
    sats = -(-msat // 1000)
    if claimed is not None:
        try:
            said = int(claimed)
        except (TypeError, ValueError):
            said = None
        if isinstance(claimed, bool) or said is None or abs(said * 1000 - msat) >= 1000:
            raise InvoiceRefused(
                f"The feed said the day-pass costs {str(claimed)[:20]} sats but "
                f"its Lightning invoice asks for {sats:,} sats, so the agent "
                "did not pay it. Nothing was paid.")
    return sats


def _count_autopay(state, pending):
    """Count a day-pass payment against its day's budget, once."""
    if pending.get("counted"):
        return
    pending["counted"] = True
    spent = _autopay_spent_today(state)
    if spent["day"] == pending.get("day"):
        spent["sats"] += int(pending.get("amount_sats") or 0)
        spent["count"] += 1
        state["autopay_spent"] = spent


def _uncount_autopay(state, pending):
    """Give the budget back for a payment that turned out not to happen."""
    if not pending.pop("counted", None):
        return
    spent = state.get("autopay_spent") or {}
    if spent.get("day") == pending.get("day"):
        spent["sats"] = max(0, int(spent.get("sats") or 0)
                            - int(pending.get("amount_sats") or 0))
        spent["count"] = max(0, int(spent.get("count") or 0) - 1)


def _daypass_paid(state, pending, preimage):
    """The wallet paid the saved purchase: turn it into the access token
    (used for 23h from when the payment was started). Caller saves."""
    _count_autopay(state, pending)
    state["l402"] = {"token": f"L402 {pending['macaroon']}:{preimage}",
                     "expires_at": float(pending.get("created_at")
                                         or time.time()) + _DAYPASS_S,
                     "paid_day": pending.get("day")}
    state.pop("daypass_pending", None)
    state.pop("daypass_retry_at", None)
    state.pop("daypass_retry_why", None)
    state.pop("daypass_no_receipt", None)   # this wallet does give receipts
    _daypass_alert_clear(state)
    if isinstance(pending.get("day"), str):
        _record_paid_day(state, pending["day"])


def _check_balance(cfg, state, save_state, wallet, paid_sats):
    """Right after a day-pass was paid: one balance read. A wallet that holds
    less than that pass cost plus the wallet's routing-fee reserve
    (fee_reserve_sats: LNbits refuses the payment without it) can't buy the
    next one, so the owner hears it the day before. A balance that can't be
    read is ignored."""
    try:
        bal = wallet.balance_sats()
        ends = datetime.fromtimestamp(float(state["l402"]["expires_at"]), MARKET_TZ)
        need = int(paid_sats or 0)
        need += fee_reserve_sats(need)
    except Exception:
        return
    if isinstance(bal, bool) or not isinstance(bal, int) or bal >= need:
        return
    _daypass_alert(
        cfg, state, save_state, "daypass_low_balance_notified",
        f"Your agent's wallet has {bal:,} sats left, less than one day-pass "
        f"plus the wallet's routing-fee reserve (about {need:,} sats). Add "
        "sats in the dashboard (Settings "
        "→ Signal source). If it cannot buy the next pass, the agent takes no "
        f"new trades from {ends.hour}:{ends.minute:02d} ET on "
        f"{ends:%A %b} {ends.day}.")


def _settle_daypass(cfg, state, save_state, wallet=None):
    """Ask the wallet what became of the saved day-pass purchase
    (state["daypass_pending"], written before the wallet was asked to pay).

    "paid"       — the wallet has the proof of payment: the pass is ready.
    "failed"     — nothing was paid; the record is dropped and its budget freed.
    "unknown"    — not decided yet (or the wallet can't be asked): the record
                   stays, counts against today's budget, and no other pass is
                   bought while it is there.
    "no_receipt" — the wallet paid but can't give the proof the pass is made
                   from: counted as paid, and auto-buying stops for this
                   wallet (see _no_receipt_stop).

    Only the wallet that was asked to pay is believed. If the owner has
    changed wallets since, the new one knows nothing about this payment, so
    it is not asked and the purchase stays undecided. And "the wallet has no
    record of it" only means "not paid" when the wallet itself turned the
    payment down, or once the invoice has expired — before that, a pay
    request that is merely stuck on its way could still go through."""
    pending = state.get("daypass_pending")
    if (not isinstance(pending, dict) or not pending.get("payment_hash")
            or not pending.get("macaroon")):
        state.pop("daypass_pending", None)
        save_state(state)
        return "failed"
    paid_from = pending.get("wallet")
    other_wallet = bool(paid_from) and paid_from != wallet_fingerprint(cfg)
    status, preimage = "unknown", ""
    if pending.get("not_sent"):
        status = "failed"       # the pay request never left this computer
    elif not other_wallet:
        wallet = wallet or wallet_from_cfg(cfg)
        check = getattr(wallet, "payment_status", None)
        if check is not None:
            try:
                status, preimage = check(pending["payment_hash"])
            except Exception:
                status, preimage = "unknown", ""
    age = time.time() - float(pending.get("created_at") or 0)
    amount = int(pending.get("amount_sats") or 0)
    if status == "paid" and preimage:
        _daypass_paid(state, pending, preimage)
        save_state(state)
        _check_balance(cfg, state, save_state, wallet, amount)
        return "paid"
    payable_s = max(_INVOICE_LIFETIME_S, bolt11_expiry_s(pending.get("invoice")) or 0)
    if status == "failed" or (status == "not_found" and (
            pending.get("refused") or age > payable_s + _INVOICE_SLACK_S)):
        _uncount_autopay(state, pending)
        state.pop("daypass_pending", None)
        if state.get("daypass_alert") == "daypass_pending_notified":
            _daypass_alert_clear(state)   # no longer "waiting for the wallet"
        save_state(state)
        return "failed"
    if status == "paid_no_proof" and age > _PENDING_ALERT_S:
        # The sats are gone and no receipt is coming. Buying again would
        # lose the same amount every market day: stop, and say so.
        _count_autopay(state, pending)
        state.pop("daypass_pending", None)
        state["daypass_no_receipt"] = {"wallet": wallet_fingerprint(cfg),
                                       "at": time.time(), "sats": amount}
        save_state(state)
        _daypass_alert(cfg, state, save_state, "daypass_noproof_notified",
                       _no_receipt_text(amount))
        return "no_receipt"
    if age > _PENDING_MAX_AGE_S:
        state.pop("daypass_pending", None)
        if state.get("daypass_alert") == "daypass_pending_notified":
            _daypass_alert_clear(state)
        save_state(state)
        print(f"Day-pass payment of {amount:,} sats is still undecided after "
              "a day — the agent stops waiting for it. Check your wallet's "
              "payment list to see whether it went through.")
        return "failed"
    if not pending.get("counted"):
        _count_autopay(state, pending)
        save_state(state)
    if age > _PENDING_ALERT_S:
        if other_wallet:
            text = (
                f"Your agent's wallet was changed while a day-pass payment of "
                f"{amount:,} sats from the previous wallet was still "
                "undecided. The agent can't ask the previous wallet about it "
                "any more, so it will not pay again from the new wallet for "
                "now — it might pay twice for the same day. Until this is "
                "sorted out the agent gets no new trades. It buys "
                "again once a day has passed since that payment. If you "
                "switch back to the previous wallet, the agent asks it again "
                "and uses the pass if it was paid. (When a wallet is "
                "replaced from the dashboard or setup, its details are saved "
                "in an old-wallet file in the agent's folder.)")
        else:
            text = (
                f"Your agent asked its wallet to pay {amount:,} sats for "
                "today's day-pass, and the wallet has not said yet whether the "
                "payment went through. The agent will not pay again while this "
                "payment is undecided, so for now the agent gets no new "
                "trades. It keeps asking the wallet and uses the pass "
                "as soon as the payment is confirmed. If the payment failed — "
                "or the wallet still has no record of it once the invoice has "
                "expired, about an hour after it was issued — the sats are "
                "still in the wallet and the agent buys again.")
        _daypass_alert(cfg, state, save_state, "daypass_pending_notified", text)
    return "unknown"


def _running_text(running_until):
    """For a failed renewal while a day-pass is still running."""
    t = datetime.fromtimestamp(float(running_until), MARKET_TZ)
    return (f"The pass it has runs until {t.hour}:{t.minute:02d} ET; it opens "
            "no new trades today because that is before the close.")


def _buy_day_pass(cfg, state, body, save_state, running_until=None):
    """Pay the 402 Lightning invoice; cache the 24h L402 token in state.
    Returns True when the pass is ready to use, False when the payment was
    sent but isn't decided yet (it is settled on a later poll). Raises when
    nothing was paid; another try is then held off for _PAY_RETRY_S, because
    every try makes the server create one more invoice. An invoice the agent
    refuses to pay is also reported to the owner (once a day): until it is
    sorted out the agent has no pass, so no signals. `running_until`: a renewal
    while the current pass still runs until then (the alerts say so).
    Never in practice or while paused (feed_mode): nothing is paid and it
    returns False (every caller asks _pass_purchase_allowed first; this is
    the last stop)."""
    if feed_mode(cfg) != "live":
        _not_live_notice(state, save_state, feed_mode(cfg))
        return False
    try:
        return _pay_day_pass(cfg, state, body, save_state, running_until)
    except Exception as exc:
        state["daypass_retry_at"] = time.time() + _PAY_RETRY_S
        state["daypass_retry_why"] = getattr(exc, "loss_code", "day_pass_failed")
        save_state(state)
        if isinstance(exc, InvoiceRefused):
            then = (_running_text(running_until) if running_until
                    else "Until a day-pass is paid the agent gets no new trades.")
            _daypass_alert(
                cfg, state, save_state, "daypass_invoice_notified",
                f"Your agent did not buy today's day-pass. {exc} {then} "
                f"{_retry_text()} If this keeps happening, update the agent "
                "or contact AgentHC.")
        raise


def _pay_day_pass(cfg, state, body, save_state, running_until=None):
    wallet = wallet_from_cfg(cfg)
    if wallet is None:
        raise RuntimeError(
            "Feed requires payment and no Lightning wallet is configured — "
            "run `python agent.py setup` (or use a Premium API key).")
    payment = body.get("payment", {})
    invoice = payment.get("payment_request")
    macaroon = payment.get("macaroon")
    if not invoice or not macaroon:
        print(f"402 without payable invoice: {str(body)[:200]}")
        raise InvoiceRefused(
            "AgentHC asked for payment but did not send a Lightning invoice "
            "the agent can pay. Nothing was paid.")
    # The amount the wallet would actually pay is the one inside the
    # invoice; the amount_sats field next to it is only cross-checked.
    amount = _invoice_sats(invoice, payment.get("amount_sats"))
    payment_hash = bolt11_payment_hash(invoice)
    if not payment_hash:
        raise InvoiceRefused(
            "The feed sent a Lightning invoice the agent can't keep track of "
            "(no payment id in it), so it did not pay it. Nothing was paid.")
    said_hash = payment.get("payment_hash")
    if said_hash and str(said_hash).lower() != payment_hash:
        # the pass (macaroon) is tied to one payment: paying a different
        # invoice would buy nothing
        raise InvoiceRefused(
            "The feed sent a Lightning invoice that is not the one its "
            "day-pass belongs to, so the agent did not pay it. Nothing was paid.")
    caps = autopay_caps(cfg)
    cap = caps["per_invoice"]
    if amount > cap:
        raise InvoiceRefused(autopay_refusal_text(amount, cfg, caps))
    spent = _autopay_spent_today(state)
    daily_cap = caps["per_day"]
    if spent["sats"] + amount > daily_cap:
        raise InvoiceRefused(
            f"A day-pass ({amount:,} sats) would take today's spending over "
            f"your daily limit: {spent['sats']:,} of {daily_cap:,} sats are "
            "already spent (max_autopay_sats_per_day in config.json), so the "
            "agent did not pay. Nothing was paid.")
    # Save the purchase BEFORE paying: if the wallet's answer is lost (a
    # timeout, a crash, a restart) the payment may still go through, and
    # only this record can turn it into the pass it paid for. It is as safe
    # from a power cut as any other save of the agent's state (save_state).
    # "wallet" says which wallet was asked — only that one can answer for it.
    pending = {"macaroon": macaroon, "payment_hash": payment_hash,
               "invoice": invoice, "amount_sats": amount,
               "created_at": time.time(),
               "day": _now_et().strftime("%Y-%m-%d"),
               "wallet": wallet_fingerprint(cfg)}
    state["daypass_pending"] = pending
    save_state(state)
    print(f"Buying 24h day-pass: paying {amount:,} sats "
          "(terms accepted by payment — see `disclosure` in the 402 body) …")
    try:
        preimage = wallet.pay_invoice(invoice)
    except Exception as exc:
        # Never assume: ask the wallet whether the payment exists (unless
        # the request provably never reached it).
        pending["refused"] = isinstance(exc, PaymentRefused)
        if isinstance(exc, PaymentNotSent):
            pending["not_sent"] = True
        outcome = _settle_daypass(cfg, state, save_state, wallet=wallet)
        if outcome == "failed":
            reason = str(exc)[:200]   # the wallet's own words: console only
            short = (isinstance(exc, PaymentRefused) and re.search(
                # LNbits' fee reserve: "You must reserve at least (N sat) to
                # cover potential routing fees" is a short balance too
                r"insufficient|not enough|balance|reserve|routing fee", reason, re.I))
            if isinstance(exc, WalletKeyRejected):
                why = ("the wallet's server did not accept the agent's "
                       "wallet key — the wallet may no longer exist (public "
                       "test servers can be reset). Check it, or set up "
                       "another wallet, in the dashboard (Settings → Signal "
                       "source)")
            elif isinstance(exc, PaymentNotSent):
                why = ("the wallet could not be reached — check this "
                       "computer's internet connection; the wallet's server "
                       "may also be down")
            elif short:
                why = ("the wallet does not hold enough sats — add sats in "
                       "the dashboard (Settings → Signal source)")
            else:
                why = "the wallet did not make the payment"
            then = (_running_text(running_until) if running_until
                    else "Until a pass is paid the agent gets no new trades.")
            _daypass_alert(
                cfg, state, save_state, "daypass_failed_notified",
                "Your agent's wallet could not pay for today's day-pass "
                f"({amount:,} sats): {why}. Nothing was paid. {then} "
                f"{_retry_text()}")
            err = RuntimeError(f"Day-pass payment failed — nothing was paid: {reason}")
            err.loss_code = "day_pass_short" if short else "day_pass_failed"
            raise err
        if outcome != "paid":
            if outcome == "unknown":
                print(f"Day-pass payment of {amount:,} sats: the wallet has "
                      "not said yet whether it went through "
                      f"({str(exc)[:120]}). The agent will not pay again "
                      "meanwhile; it asks the wallet on every poll and uses "
                      "this pass once the payment is confirmed.")
            return False
    else:
        _daypass_paid(state, pending, preimage)
        save_state(state)
        _check_balance(cfg, state, save_state, wallet, amount)
    print("Day-pass active for ~23h.")
    return True


_LAST_POLL = {"access": True, "exits": True, "why": ""}


def _no_read(why):
    """A poll() that read nothing: no entry and no exit signals. `why` is
    the exit_loss_reason() code."""
    _LAST_POLL["why"] = why
    return []


def _holds_real_position(state):
    """The agent's ledger holds a position bought with a real order (the
    reading of agent._opened_live: a row that says neither way is real)."""
    rows = (state or {}).get("positions")
    for row in (rows.values() if isinstance(rows, dict) else ()):
        if isinstance(row, dict) and (row.get("live") is True or (
                not row.get("dry") and row.get("option_id") != "dry")):
            return True
    return False


def _ended_daypass_exits(cfg, state, save_state):
    """The wallet path has no pass to read with: while a real position is
    open, offer the day-pass that ended (AgentHC can still serve the exits
    of trades entered while it ran). Returns its EXITED events (never an
    entry), or None: nothing to offer, or AgentHC answered with an error.
    A 401/402 means it doesn't (an older server: one invoice, once), so the
    token is not offered again — except while a real swing position is
    held: its exit is owed days later, so it is offered again every
    _OWED_EXITS_RETRY_S."""
    tok = (state or {}).get("l402")
    done = isinstance(tok, dict) and tok.get("exits_done")
    if done and _holds_real_swing(state) and (
            time.time() - float(tok.get("exits_done_at") or 0)
            >= _OWED_EXITS_RETRY_S):
        done = False   # a swing exit is still owed: ask again
    if (cfg.get("agenthc_api_key") or not isinstance(tok, dict)
            or not tok.get("token") or done
            or float(tok.get("expires_at") or 0) > time.time()
            or not _holds_real_position(state)):
        return None
    params = _feed_params(cfg, state)
    # practice: an exits-only read says off (a practice read would bring
    # only the exits AgentHC can pair, and as practice trades)
    practice = feed_mode(cfg) == "dry_run"
    try:
        resp = _feed_get(cfg, params, {"Authorization": tok["token"]},
                         mode="off" if practice else None)
        if resp.status_code in (401, 402):
            tok["exits_done"] = True
            tok["exits_done_at"] = time.time()
            save_state(state)
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        parsed = _parse_events(body)
    except Exception:
        return None
    _LAST_POLL["exits"] = True   # exits only: entry access stays False
    _check_window(cfg, state, save_state, body, params, parsed)
    exits = [e for e in parsed if e["event"] == "EXITED"]
    if practice:
        held = _held_keys(state)
        exits = [e for e in exits if _event_key(e) in held]
    return exits


# Prepaid pass statuses that once gave signal days and no longer read the
# feed (AgentHC's answer), and how often a refused owed-exits read is tried
# again (a server that doesn't owe them yet may start to).
_ENDED_PASS = ("expired", "used_up")
_OWED_EXITS_RETRY_S = 3600


def _ended_pass_exits(cfg, state, save_state):
    """While a REAL swing position is held and no prepaid pass reads the
    feed any more: offer the newest pass that ended for its exits only
    (AgentHC owes a swing trade's exit to whoever got its entry, even days
    after the pass ends). The read says mode=off: never charged, and the
    wallet buys nothing. Returns its EXITED events (never an entry), or
    None: nothing to offer, or AgentHC answered with an error (refused:
    asked again in _OWED_EXITS_RETRY_S)."""
    if cfg.get("agenthc_api_key") or not _holds_real_swing(state):
        return None
    cache = state.setdefault("pass_status", {})
    now = time.time()
    best, best_until = None, -1.0
    for token in _pass_tokens(cfg):
        rec = cache.get(_token_id(token))
        if not isinstance(rec, dict) or rec.get("status") not in _ENDED_PASS:
            continue
        if now - float(rec.get("owed_refused_at") or 0) < _OWED_EXITS_RETRY_S:
            continue
        until = float(rec.get("until_ts") or 0)
        if until > best_until:
            best, best_until = token, until
    if best is None:
        return None
    params = _feed_params(cfg, state)
    rec = cache[_token_id(best)]
    try:
        resp = _feed_get(cfg, params, {"Authorization": best}, mode="off")
        if resp.status_code in (401, 402, 403):
            rec["owed_refused_at"] = now
            save_state(state)
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        parsed = _parse_events(body)
    except Exception:
        return None
    if rec.pop("owed_refused_at", None) is not None:
        save_state(state)
    _LAST_POLL["exits"] = True   # exits only: entry access stays False
    _check_window(cfg, state, save_state, body, params, parsed)
    held = _held_keys(state)
    return [e for e in parsed
            if e["event"] == "EXITED" and _event_key(e) in held]


def _day_pass_loss_code(cfg, state, now=None):
    """Why the wallet path has no day-pass to read with, as the code the
    agent's no-signal warning turns into the owner's fix (no request, no
    change; the same order as _pass_purchase_allowed):
    day_pass_undecided  a payment the wallet hasn't decided yet;
    day_pass_not_live   practice or paused, and the wallet would buy
                        (a wallet, Recurring ON): only LIVE buys a day-pass;
    day_pass_off        Recurring day-pass is off: the wallet never buys;
    day_pass_short      the last payment failed: not enough sats;
    day_pass_failed     the last payment failed otherwise (wallet down or
                        gone, invoice refused, a wallet without receipts);
    no_day_pass         anything else (today already paid once, the
                        daily cap, the market closed)."""
    if state.get("daypass_pending"):
        return "day_pass_undecided"
    if (feed_mode(cfg) != "live" and cfg.get("lnbits_url")
            and cfg.get("day_pass_recurring")):
        return "day_pass_not_live"
    now = now or _now_et()
    day = now.strftime("%Y-%m-%d")
    if (state.get("pass_charged_day") == day
            or (_one_pass_a_day(cfg) and _autopay_spent_today(state, now)["count"] >= 1)
            or _next_buy_time(cfg, now) is not None):
        return "no_day_pass"
    if not cfg.get("day_pass_recurring"):
        return "day_pass_off"
    if isinstance(state.get("daypass_no_receipt"), dict):
        return "day_pass_failed"
    if time.time() < float(state.get("daypass_retry_at") or 0):
        why = state.get("daypass_retry_why")
        return why if why in ("day_pass_short", "day_pass_failed") else "day_pass_failed"
    return "no_day_pass"


def _no_day_pass(cfg, state, save_state):
    """No wallet day-pass to read with (and none bought now): the exits of
    what it still holds, with the day-pass that ended or a prepaid pass
    that ended (a swing position's exit), else nothing."""
    got = _ended_daypass_exits(cfg, state, save_state)
    if got is None:
        got = _ended_pass_exits(cfg, state, save_state)
    return got if got is not None else _no_read(_day_pass_loss_code(cfg, state))


def had_exit_access():
    """Whether the last poll() could read EXIT signals: True after every
    read the feed answered (entries and exits, or a used-up pass's exits
    only), False when it returned without reading (no pass or day-pass,
    a pass not paid or not checked, a refused day-pass, a feed that isn't
    publishing), and also after a read while an exit for a held position
    may have fallen out of the feed's window (_check_window: feed_gap).
    The agent sells a held position before the close when this stays
    False (agent.signal_loss_tick)."""
    return bool(_LAST_POLL["exits"])


def exit_loss_reason():
    """Why the last poll() read no exit signals ("" when it did):
    pass_unchecked, pass_unpaid, pass_ended, day_pass_refused,
    feed_not_live, feed_gap, or a _day_pass_loss_code (no_day_pass, day_pass_off,
    day_pass_short, day_pass_failed, day_pass_undecided). Also set when
    poll() raised because the wallet could not pay for the day-pass."""
    return "" if _LAST_POLL["exits"] else str(_LAST_POLL.get("why") or "")

# Outside the feed window (agent._feed_window_now) the agent reads this feed
# once an hour, not on every loop: it has never published a signal there.
IDLE_READS = True

# The same refusal again and again (one owner got the same 403 on every poll
# for days): after REFUSAL_REPEATS identical refusals in a row the agent
# waits REFUSAL_BACKOFF_S[0], then [1], ... (staying at the last) before the
# next read, until any different answer arrives. Exits owed and the expiry
# guard don't need the feed and keep running on every loop meanwhile;
# Resume or "check now" reads at once. Not for 401/402 (the pass and
# day-pass handshakes), feed_not_live / terms_acceptance_required (handled
# below), or 5xx (a server outage: read again soon, for the exits).
REFUSAL_REPEATS = 3
REFUSAL_BACKOFF_S = (120, 240, 480, 900)
_REFUSAL = {}


def _now_s():
    return time.time()


def _refusal_key(resp):
    """(status, error code, body hash) of a refusal the feed keeps giving,
    or None for any other answer."""
    import hashlib
    status = getattr(resp, "status_code", 0)
    if not isinstance(status, int) or not 400 <= status < 500 or status in (401, 402):
        return None
    try:
        code = _error_code(resp)
    except Exception:
        code = None
    if code in ("terms_acceptance_required", "feed_not_live"):
        return None
    code = str(code)[:80] if isinstance(code, str) and code else ""
    body = getattr(resp, "content", None)
    if not isinstance(body, (bytes, bytearray)):
        body = str(getattr(resp, "text", "") or "").encode("utf-8", "replace")
    return (status, code, hashlib.sha256(bytes(body)).hexdigest()[:16])


def _note_feed_answer(resp):
    """Count identical refusals; any other answer ends a back-off."""
    key = _refusal_key(resp)
    if key is None:
        _REFUSAL.clear()
        return
    # no exit signal from this read either: the same cause as the back-off
    # that follows (the no-signal sell, agent._note_exit_source)
    _LAST_POLL["why"] = "feed_refused"
    now = _now_s()
    if _REFUSAL.get("key") != key:
        _REFUSAL.clear()
        _REFUSAL.update(key=key, count=0, since=now)
    _REFUSAL["count"] += 1
    n = _REFUSAL["count"]
    if n >= REFUSAL_REPEATS:
        step = min(n - REFUSAL_REPEATS, len(REFUSAL_BACKOFF_S) - 1)
        _REFUSAL["next_at"] = now + REFUSAL_BACKOFF_S[step]


def refusal_wait_until():
    """Epoch seconds before which the agent should not read the feed again
    (backing off after the same refusal), or None."""
    return _REFUSAL.get("next_at")


def refusal_info():
    """{status, code, since, next_at} while backing off, else None."""
    if not _REFUSAL.get("next_at"):
        return None
    status, code, _h = _REFUSAL["key"]
    return {"status": status, "code": code, "since": _REFUSAL["since"],
            "next_at": _REFUSAL["next_at"], "count": _REFUSAL["count"]}


def payment_pending(cfg, state):
    """True while a pass invoice or a wallet day-pass payment waits to be
    decided (poll() is what checks them): the agent then reads on every
    loop, also outside the feed window."""
    if cfg.get("agenthc_api_key"):
        return False
    if isinstance(state.get("daypass_pending"), dict):
        return True
    cache = state.get("pass_status")
    cache = cache if isinstance(cache, dict) else {}
    for token in _pass_tokens(cfg):
        if _known_status(cfg, cache.get(_token_id(token)), token) in (None, "pending"):
            return True
    return False


def had_access():
    """Whether the last poll() could read ENTRY signals: False with no pass
    or day-pass (not bought yet, run out, waiting for payment) and while a
    used-up pass only gets exits. The agent starts its "was I watching when
    this signal came?" clock on a poll that had access — an entry from
    before then was never catchable, so it isn't reported as too old."""
    return bool(_LAST_POLL["access"])


def poll(cfg, state, save_state=lambda s: None, _retried=False):
    """Fetch recent journal events (normalized contract shape).

    Signal scope (config `include_other_trades`, default False): the main
    daily pick only, or main + AgentHC's "other trades" journal (its wider
    shadow book — several entries a day; your daily entry cap and budget
    still apply per trade). Swing trades (config `include_swing_trades`,
    default False, the owner's own switch): AgentHC's rare multi-day calls
    after a VIX spike, held up to SWING_HOLD_SESSIONS trading sessions.
    """
    # until the feed is actually read (below)
    _LAST_POLL.update(access=False, exits=False, why="")
    # A wallet day-pass payment saved earlier and not decided yet: ask the
    # wallet first. Paid → its token is used below; still undecided → the
    # wallet path stops further down (never a second payment for one day).
    undecided = (not cfg.get("agenthc_api_key") and state.get("daypass_pending")
                 and _settle_daypass(cfg, state, save_state) == "unknown")
    # Prepaid multi-day pass (bought in the dashboard, paid from any wallet):
    # takes precedence over the per-day wallet auto-pay. One charge per day:
    # a wallet day-pass that is already paid and still running is used up
    # first (reading with the prepaid pass now would charge a pass day on
    # top of it), and the wallet never buys while a pass's state is unknown.
    if (not cfg.get("agenthc_api_key") and _pass_tokens(cfg)
            and not _live_daypass(state)):
        token = _refresh_pass(cfg, state, save_state)
        if token:
            # A used-up pass only reads exits. With the wallet's recurring
            # day-pass ON, today's day-pass brings the entries back (exits
            # come with it); if it can't be bought now, the exits are still
            # read with the used-up pass in this same poll.
            if (not _retried and not undecided
                    and _token_status(state, token) == "exits_only"
                    and cfg.get("lnbits_url") and cfg.get("day_pass_recurring")
                    and _pass_purchase_allowed(cfg, state, save_state)):
                got = _wallet_over_exits_only(cfg, state, save_state)
                if got is not None:
                    return got
            return _poll_with_pass(cfg, state, save_state, token, _retried)
        cache = state.get("pass_status") or {}
        statuses = {_known_status(cfg, cache.get(_token_id(t)), t)
                    for t in _pass_tokens(cfg)}
        if None in statuses:
            # Status check failed (network/server error), or an expired
            # invoice AgentHC hasn't answered for yet: the pass may well
            # be fine. Wait for the next poll — never pay the wallet on an
            # unknown, or the owner pays twice for the same day.
            return _no_read("pass_unchecked")
        if not (cfg.get("lnbits_url") and cfg.get("day_pass_recurring")):
            if "pending" in statuses:
                return _no_read("pass_unpaid")  # invoice shown, not paid yet
            _pass_notice(cfg, state, save_state, "pass_expired_notified",
                         PASS_RUN_OUT_TEXT)
            # a swing position bought while the pass ran still gets its exit
            got = _ended_pass_exits(cfg, state, save_state)
            return got if got is not None else _no_read("pass_ended")
        # else: fall through to the wallet's daily auto-pay
    if (not _retried and not undecided and not cfg.get("agenthc_api_key")
            and _live_daypass(state)):
        _renew_daypass(cfg, state, save_state)
    # Skip the request entirely when access is impossible: no API key, no
    # live day-pass token, and buying one isn't allowed right now (recurring
    # OFF, or weekend). Every such GET makes the server mint a Lightning
    # invoice that is guaranteed to expire unpaid — at market-hours poll
    # cadence that's ~1,000 doomed invoices a day against the publisher's
    # node. The once-per-day notify inside _pass_purchase_allowed still fires.
    if undecided and not _headers(cfg, state):
        return _no_day_pass(cfg, state, save_state)
    if not _headers(cfg, state) and not _pass_purchase_allowed(cfg, state, save_state):
        return _no_day_pass(cfg, state, save_state)
    params = _feed_params(cfg, state)
    headers = _headers(cfg, state)
    daypass = None if cfg.get("agenthc_api_key") else (state.get("l402") or {})
    if (daypass and headers.get("Authorization") and not _retried
            and time.time() - float(daypass.get("refused_at") or 0) < _REFUSED_RETRY_S):
        # AgentHC refused this paid pass a moment ago: offer it again later
        return _no_read("day_pass_refused")
    resp = _feed_get(cfg, params, headers)
    _note_feed_answer(resp)
    if (resp.status_code in (401, 402) and not _retried
            and not cfg.get("agenthc_api_key") and _pass_tokens(cfg)
            and headers.get("Authorization")):
        # The running day-pass was refused: drop it and read with the
        # prepaid pass instead of letting the wallet buy another one.
        state.pop("l402", None)
        save_state(state)
        return poll(cfg, state, save_state, _retried=True)
    if (resp.status_code in (401, 402) and daypass and headers.get("Authorization")
            and _one_pass_a_day(cfg)
            and _daypass_bought_day(daypass) == _now_et().strftime("%Y-%m-%d")):
        # AgentHC refused a day-pass the wallet paid for TODAY. Paying again
        # is not the answer: keep the pass, tell the owner, offer it again
        # every _REFUSED_RETRY_S in case the refusal was a server hiccup.
        # (An owner who set max_autopay_sats_per_day chose re-buying, up to
        # that many sats: for them the older path below still applies.)
        daypass["refused_at"] = time.time()
        save_state(state)
        _daypass_alert(
            cfg, state, save_state, "daypass_refused_notified",
            "AgentHC refused the day-pass your agent's wallet paid for "
            "today. The agent will NOT buy another one today. It keeps "
            f"offering the paid pass every {_REFUSED_RETRY_S // 60} minutes; "
            "until AgentHC accepts it the agent gets no new trades. If this "
            "doesn't clear up, contact AgentHC with "
            "the time of the payment from your wallet's payment list.")
        return _no_read("day_pass_refused")
    if (resp.status_code in (402, 403) and cfg.get("agenthc_api_key")
            and feed_mode(cfg) == "dry_run" and _refused_mode(resp) == "dry_run"):
        # AgentHC serves practice only to a key whose own paid tier is
        # Premium+: nothing is owed, the balance is fine.
        return _practice_not_served(cfg, state, save_state, params, headers)
    if resp.status_code == 402 and cfg.get("agenthc_api_key"):
        # The Premium key's balance can't pay for today. The agent never
        # buys a wallet day-pass while a key is set (it would not be sent
        # with the key): nothing is paid, the owner is told to top up.
        _pass_notice(cfg, state, save_state, "key_balance_notified",
                     KEY_BALANCE_EMPTY_TEXT)
        return _no_read("feed_refused")
    if resp.status_code == 402 and not _retried:
        if undecided or not _pass_purchase_allowed(cfg, state, save_state):
            return _no_day_pass(cfg, state, save_state)
        try:
            bought = _buy_day_pass(cfg, state, resp.json(), save_state)
        except Exception:
            # nothing was paid (reported already); the error still reaches
            # the loop, which records this cause and not an unreadable feed
            _LAST_POLL["why"] = _day_pass_loss_code(cfg, state)
            raise
        if not bought:
            return _no_day_pass(cfg, state, save_state)
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 401 and not _retried and not cfg.get("agenthc_api_key"):
        state.pop("l402", None)  # stale day-pass token — re-buy on next 402
        save_state(state)
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("error") == "terms_acceptance_required":
            accept_terms(cfg)
            return poll(cfg, state, save_state, _retried=True)
        if isinstance(detail, dict) and detail.get("error") == "feed_not_live":
            # feed exists but isn't publishing yet — keep polling
            return _no_read("feed_not_live")
        raise RuntimeError(f"403 from feed: {str(detail)[:200]}")
    resp.raise_for_status()
    if headers.get("Authorization"):
        if daypass and daypass.pop("refused_at", None):
            _daypass_alert_clear(state)   # accepted again
            save_state(state)
        # A wallet day-pass runs 23h, so one bought in the afternoon also
        # covers the next session: that market day was paid for too. Only
        # counts while the market is open (09:30-16:00 ET on a weekday) — a
        # token that just ran into the night or pre-market covered no trading.
        now = _now_et()
        minute = now.hour * 60 + now.minute
        if (now.weekday() < 5 and 9 * 60 + 30 <= minute < 16 * 60
                and _record_paid_day(state, now.strftime("%Y-%m-%d"))):
            save_state(state)
    _LAST_POLL.update(access=True, exits=True)
    _entry_read(state, save_state)
    body = resp.json()
    events = _parse_events(body)
    return _finish_read(cfg, state, save_state, body, params, events, headers)


def _poll_with_pass(cfg, state, save_state, token, _retried):
    params = _feed_params(cfg, state)
    resp = _feed_get(cfg, params, {"Authorization": token})
    _note_feed_answer(resp)
    if resp.status_code in (401, 402) and not _retried:
        _refresh_pass(cfg, state, save_state, force=True)  # ended/refused: recheck
        return poll(cfg, state, save_state, _retried=True)
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("error") == "feed_not_live":
            return _no_read("feed_not_live")
        raise RuntimeError(f"403 from feed: {str(detail)[:200]}")
    resp.raise_for_status()
    body = resp.json()
    if body.get("pass_charged_today"):
        # The pass paid for today: the wallet must not buy a day-pass today.
        today = _now_et().strftime("%Y-%m-%d")
        changed = _record_paid_day(state, today)
        if state.get("pass_charged_day") != today:
            state["pass_charged_day"] = today
            changed = True
        if changed:
            save_state(state)
    left = body.get("pass_days_left", body.get("pass_trading_days_left"))
    exits_only = bool(body.get("pass_exits_only"))
    # a used-up pass still reads the exits: exit access, no entry access
    _LAST_POLL.update(access=not exits_only, exits=True)
    rec = state.setdefault("pass_status", {}).get(_token_id(token))
    if rec is not None:
        new_status = "exits_only" if exits_only else "active"
        until = (_iso_ts(body.get("pass_exits_until")) or None) if exits_only else None
        if (rec.get("days_left") != left or rec.get("status") != new_status
                or rec.get("exits_until") != until):
            rec["days_left"] = left
            rec["status"] = new_status
            if until:
                rec["exits_until"] = until
            else:
                rec.pop("exits_until", None)
            save_state(state)
    if exits_only:
        _pass_notice(cfg, state, save_state, "pass_used_up_notified",
                     PASS_USED_UP_TEXT)
    else:
        _entry_read(state, save_state)
        if left == 0 and body.get("pass_charged_today"):
            _pass_notice(cfg, state, save_state, "pass_last_day_notified",
                         PASS_LAST_DAY_TEXT)
        elif isinstance(left, int) and left <= PASS_LOW_DAYS:
            _pass_notice(cfg, state, save_state, "pass_low_notified",
                         PASS_LOW_TEXT.format(n=left))
    events = _parse_events(body)
    events = _finish_read(cfg, state, save_state, body, params, events,
                          {"Authorization": token})
    if exits_only:
        # a used-up pass gets no entries: should the reply carry one anyway
        # (a server bug, a malformed answer), it never reaches the agent
        events = [e for e in events if e["event"] == "EXITED"]
    return events


def pass_summary(cfg, state):
    """One line for the dashboard: what feed access the agent has right now."""
    if cfg.get("agenthc_api_key"):
        return "Premium API key"
    best = None
    for token in _pass_tokens(cfg):
        rec = (state.get("pass_status") or {}).get(_token_id(token)) or {}
        if rec.get("status") in _USABLE and (
                best is None or (rec.get("status") == "active"
                                 and best.get("status") != "active")):
            best = rec
    if best and best.get("status") == "exits_only":
        return "prepaid pass used up — exit signals only; buy more"
    if best:
        left = best.get("days_left")
        return (f"prepaid pass · {left} signal day(s) left" if left is not None
                else "prepaid pass · active")
    if _pass_tokens(cfg):
        return "prepaid pass · waiting for payment or expired"
    if cfg.get("lnbits_url"):
        return ("wallet auto-pay (recurring ON)" if cfg.get("day_pass_recurring")
                else "wallet — recurring day-pass OFF")
    return "no access configured"


# The feed's own clock: an entry's age is measured against the time AgentHC
# answered (body["timestamp"]), not this computer's clock alone — a PC clock
# that runs fast (WSL2 after a sleep, no time sync) made every fresh entry
# "too old", a slow one let old entries through. Each event carries the
# offset as clock_skew_s (this clock minus AgentHC's, seconds; agent._stale
# subtracts it). A fast clock is corrected by CLOCK_SKEW_MAX_S at most, so
# a bad server time can't make an old entry look fresh; a slow one fully
# (that only makes entries older). No server time: no correction.
CLOCK_SKEW_MAX_S = 600
CLOCK_SKEW_WARN_S = 60
_SKEW_WARNED = {"done": False}


def _clock_skew(body):
    server_now = _epoch(body.get("timestamp")) if isinstance(body, dict) else None
    if server_now is None:
        return None
    skew = time.time() - server_now
    if abs(skew) > CLOCK_SKEW_WARN_S and not _SKEW_WARNED["done"]:
        _SKEW_WARNED["done"] = True
        print(f"This computer's clock is {abs(skew) / 60:.1f} min "
              f"{'ahead of' if skew > 0 else 'behind'} AgentHC's: signal ages "
              "are measured with AgentHC's clock. Turn on automatic time sync "
              "on this computer.")
    return round(min(skew, CLOCK_SKEW_MAX_S), 3)


def _parse_events(body):
    from sources import log_ignored, never_short_reason
    events = []
    skew = _clock_skew(body)
    for ev in body.get("events", []):
        if not isinstance(ev, dict):
            continue
        why = never_short_reason(ev, ev.get("event"))
        if why:
            log_ignored("AgentHC feed", ev, why)
            continue
        try:
            # Normalize to the contract types (see sources/__init__.py) — a
            # string strike from the wire must not reach the order path.
            norm = {
                "event": ev["event"],
                "ticker": str(ev["ticker"]).upper(),
                "expiry": str(ev["expiry"]),
                "strike": float(ev["strike"]),
                "type": "C" if str(ev["type"]).upper().startswith("C") else "P",
                "occurred_at": ev.get("occurred_at"),
                "message": ev.get("message", ""),
                # pre-track feeds send no track field — those are main-pick events
                "track": str(ev.get("track", "main")),
            }
            if "paper_pnl_pct" in ev:
                norm["paper_pnl_pct"] = ev["paper_pnl_pct"]
            if ev.get("practice_release"):
                # a completed trade released to practice at its exit (a
                # mode=dry_run read): the agent books it, never trades it
                norm["practice_release"] = str(ev["practice_release"])[:40]
        except (KeyError, TypeError, ValueError):
            continue
        norm["event_id"] = (f"{norm['event']}|{norm['ticker']}|{norm['expiry']}|"
                            f"{norm['strike']}|{norm['type']}|{norm.get('occurred_at')}")
        # Suffix only for the non-default track: main-pick IDs must stay
        # byte-identical across upgrades or the seen-events dedupe resets.
        if norm["track"] != "main":
            norm["event_id"] += f"|{norm['track']}"
        if skew is not None:
            norm["clock_skew_s"] = skew
        events.append(norm)
    return events


def setup(cfg):
    print("\n-- AgentHC Agentic Day Trade Ideas (sats-based access) --")
    print("Which trade signals?")
    print("  1) Main pick only — one high-conviction day trade")
    print("  2) Main pick + \"other trades\" — AgentHC's wider journal;")
    print("     several entries a day (your daily cap + budget still apply)")
    # re-running setup keeps what is saved on Enter
    saved = "2" if cfg.get("include_other_trades") else "1"
    scope = input(f"Choose [1/2, default {saved}]: ").strip() or saved
    cfg["include_other_trades"] = scope == "2"
    print("Swing trades: rare multi-day calls after a VIX spike; holds up to "
          f"{SWING_HOLD_SESSIONS} trading days.")
    saved = "y" if cfg.get("include_swing_trades") else "n"
    swing = (input(f"Take swing trades too? [y/n, default {saved}]: ").strip()
             .lower() or saved)
    cfg["include_swing_trades"] = swing.startswith("y")
    print(f"  1) Lightning day-pass — the agent pays "
          f"~{usd_words(day_pass_price()['usd'])}/day in sats from its")
    print("     own wallet, no account needed (recommended for agents)")
    print("  2) AgentHC Premium API key (sats-purchased subscription)")
    choice = input("Choose [1/2, default 1]: ").strip() or "1"
    if choice == "2":
        if not cfg.get("agenthc_api_key"):
            have = input("Do you already have an AgentHC API key? [y/N]: ").strip().lower()
            cfg["agenthc_api_key"] = (input("Paste your API key: ").strip()
                                      if have == "y" else register_key())
        try:
            accept_terms(cfg)
        except Exception as exc:
            print(f"(terms acceptance deferred: {exc} — will retry on first run)")
    else:
        cfg = wallet_setup(cfg)
        if not wallet_from_cfg(cfg):
            print("No wallet configured and no API key — this source cannot be")
            print("used until you re-run setup and complete one of the two.")
    return cfg
