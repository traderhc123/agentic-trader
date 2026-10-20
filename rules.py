"""Built-in trading rules — plain settings, no AI needed.

These cover what people most often wrote into the AI-checked policy box,
but run as ordinary code: same answer every time, nothing to connect, and
no way to fail closed because a key expired. Every rule only ever SKIPS an
entry; exits always run.

Config keys (all optional; unset = rule off):
    rule_tickers        ["SPY", "QQQ"]  only trade these underlyings
    rule_skip_tickers   ["TSLA"]        never trade these
    rule_sides          "calls" | "puts" | "both"
    rule_min_strike     10              skip strikes below this (e.g. penny names)
    rule_skip_0dte      true            skip contracts expiring the same day
    rule_loss_streak    2               after N losing exits in a row today,
                                        no new entries until tomorrow
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SIDES = ("both", "calls", "puts")


def parse_tickers(value):
    """'spy, QQQ nvda' / ['spy'] -> ['SPY', 'QQQ', 'NVDA'] (invalid dropped)."""
    if isinstance(value, str):
        parts = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return []
    out = []
    for p in parts:
        t = p.strip().upper().lstrip("$")
        if t and _TICKER.match(t) and t not in out:
            out.append(t)
    return out


def _num(value, cast=float):
    try:
        return max(0, cast(value or 0))
    except (TypeError, ValueError):
        return 0


def loss_streak(trades):
    """Losing exits in a row at the end of `trades` (today's log, oldest
    first). Exits with no P&L estimate are ignored; a win or breakeven
    resets the count."""
    n = 0
    for t in trades:
        if t.get("action") != "exit" or t.get("est_pnl_usd") is None:
            continue
        try:
            n = n + 1 if float(t["est_pnl_usd"]) < 0 else 0
        except (TypeError, ValueError):
            continue
    return n


def check(cfg, ev, today_trades=(), now=None):
    """(True, "") if the entry passes every rule, else (False, reason)."""
    if ev.get("event") != "ENTERED":
        return True, ""
    ticker = str(ev.get("ticker", "")).upper()
    allow = parse_tickers(cfg.get("rule_tickers"))
    if allow and ticker not in allow:
        return False, f"{ticker} isn't in your ticker list ({', '.join(allow)})"
    if ticker in parse_tickers(cfg.get("rule_skip_tickers")):
        return False, f"{ticker} is on your skip list"
    sides = str(cfg.get("rule_sides") or "both").lower()
    kind = "calls" if str(ev.get("type", "")).upper().startswith("C") else "puts"
    if sides in ("calls", "puts") and kind != sides:
        return False, f"you trade {sides} only"
    floor = _num(cfg.get("rule_min_strike"))
    try:
        strike = float(ev.get("strike") or 0)
    except (TypeError, ValueError):
        strike = 0.0
    if floor and strike < floor:
        return False, f"strike ${strike:g} is under your ${floor:g} minimum"
    if cfg.get("rule_skip_0dte"):
        today = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ).date().isoformat()
        if str(ev.get("expiry")) == today:
            return False, "it expires today and you skip same-day (0DTE) contracts"
    limit = _num(cfg.get("rule_loss_streak"), int)
    if limit and loss_streak(today_trades) >= limit:
        return False, (f"{limit} losing exit(s) in a row today — no new entries "
                       "until tomorrow")
    return True, ""


def active(cfg):
    return bool(describe(cfg))


def describe(cfg):
    """Short summary, e.g. 'SPY, QQQ only · calls only · stop after 2 losses'."""
    bits = []
    allow = parse_tickers(cfg.get("rule_tickers"))
    if allow:
        bits.append(", ".join(allow) + " only")
    skip = parse_tickers(cfg.get("rule_skip_tickers"))
    if skip:
        bits.append("skip " + ", ".join(skip))
    sides = str(cfg.get("rule_sides") or "both").lower()
    if sides in ("calls", "puts"):
        bits.append(f"{sides} only")
    floor = _num(cfg.get("rule_min_strike"))
    if floor:
        bits.append(f"strike ≥ ${floor:g}")
    if cfg.get("rule_skip_0dte"):
        bits.append("no 0DTE")
    limit = _num(cfg.get("rule_loss_streak"), int)
    if limit:
        bits.append(f"stop after {limit} loss{'es' if limit != 1 else ''} in a row")
    return " · ".join(bits)


def apply_settings(cfg, data):
    """Copy rule fields from a wizard/plan payload into cfg. Keys absent from
    `data` are left alone; present-but-empty turns that rule off."""
    if "rule_tickers" in data:
        cfg["rule_tickers"] = parse_tickers(data.get("rule_tickers"))
    if "rule_skip_tickers" in data:
        cfg["rule_skip_tickers"] = parse_tickers(data.get("rule_skip_tickers"))
    if "rule_sides" in data:
        s = str(data.get("rule_sides") or "both").lower()
        cfg["rule_sides"] = s if s in SIDES else "both"
    if "rule_min_strike" in data:
        cfg["rule_min_strike"] = float(_num(data.get("rule_min_strike")))
    if "rule_skip_0dte" in data:
        cfg["rule_skip_0dte"] = bool(data.get("rule_skip_0dte"))
    if "rule_loss_streak" in data:
        cfg["rule_loss_streak"] = int(_num(data.get("rule_loss_streak"), int))
    return cfg
