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
    rule_loss_streak    2               after N losing trades in a row today,
                                        no new entries until tomorrow (a
                                        position sold in pieces is one trade)
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


def exit_results(trades):
    """One estimated P&L (USD) per closed-or-closing position in `trades`
    (log rows, oldest first), in the order each was last sold; None when
    no row of it carried an estimate.

    A position sold in pieces logs several exit rows: a partial fill and
    the remainder the agent re-sells, or a partial close in the broker app
    followed by the agent's exit. Rows marked "partial" leave the position
    open, so the next exit row of the same contract (and mode: practice or
    real) is the same position: its P&L is added to that one result, never
    counted as a second trade. An entry row in that contract starts a new
    position. A partly sold position counts at once with what it has lost
    so far.

    The key has no book: entry and exit rows don't carry one, and the agent
    holds one position per contract (state["positions"] is keyed by
    contract), so a contract's exits are one position's. If two books can
    ever hold the same contract at once, write the book on entry and exit
    rows and add it to this key — or one book's partial exit would merge
    with the other book's exit and two losses would count as one."""
    results, open_ = [], {}
    for t in trades:
        key = (bool(t.get("dry")), t.get("contract"))
        if t.get("action") == "entry":
            open_.pop(key, None)  # a new position: its exits are its own
        if t.get("action") != "exit":
            continue
        r = open_.pop(key, None)
        if r is None:
            r = [None]
        else:  # moves to where this position was last sold (by identity:
            # two results can hold the same number)
            del results[next(i for i, x in enumerate(results) if x is r)]
        v = t.get("est_pnl_usd")
        if v is not None and not isinstance(v, bool):
            try:
                r[0] = (r[0] or 0.0) + float(v)
            except (TypeError, ValueError):
                pass
        results.append(r)
        if t.get("partial"):
            open_[key] = r
    return [r[0] for r in results]


def loss_streak(trades):
    """Losing positions in a row at the end of `trades` (today's log, oldest
    first). A position sold in pieces is one result (exit_results). Exits
    with no P&L estimate are ignored; a win or breakeven resets the count."""
    n = 0
    for v in exit_results(trades):
        if v is None:
            continue
        n = n + 1 if v < 0 else 0
    return n


def check(cfg, ev, today_trades=(), now=None):
    """(True, "") if the entry passes every rule, else (False, reason)."""
    return check_detail(cfg, ev, today_trades, now)[:2]


def check_detail(cfg, ev, today_trades=(), now=None):
    """check(), plus which rule said no as data: (ok, reason, code, params).
    `code` is "" when the entry passes, else one of RULE_CODES; `params`
    holds that rule's own numbers and names. For the dashboard, which must
    not parse the sentence."""
    if ev.get("event") != "ENTERED":
        return True, "", "", {}
    ticker = str(ev.get("ticker", "")).upper()
    allow = parse_tickers(cfg.get("rule_tickers"))
    if allow and ticker not in allow:
        return (False, f"{ticker} isn't in your ticker list ({', '.join(allow)})",
                "rule_not_in_list", {"ticker": ticker})
    if ticker in parse_tickers(cfg.get("rule_skip_tickers")):
        return (False, f"{ticker} is on your skip list",
                "rule_skip_list", {"ticker": ticker})
    sides = str(cfg.get("rule_sides") or "both").lower()
    kind = "calls" if str(ev.get("type", "")).upper().startswith("C") else "puts"
    if sides in ("calls", "puts") and kind != sides:
        return False, f"you trade {sides} only", "rule_side", {"sides": sides}
    floor = _num(cfg.get("rule_min_strike"))
    try:
        strike = float(ev.get("strike") or 0)
    except (TypeError, ValueError):
        strike = 0.0
    if floor and strike < floor:
        return (False, f"strike ${strike:g} is under your ${floor:g} minimum",
                "rule_min_strike", {"strike": strike, "min_strike": floor})
    if cfg.get("rule_skip_0dte"):
        today = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ).date().isoformat()
        if str(ev.get("expiry")) == today:
            return (False, "it expires today and you skip same-day (0DTE) contracts",
                    "rule_0dte", {})
    limit = _num(cfg.get("rule_loss_streak"), int)
    if limit and loss_streak(today_trades) >= limit:
        return (False, (f"{limit} losing exit(s) in a row today — no new entries "
                        "until tomorrow"), "rule_loss_streak", {"losses": limit})
    return True, "", "", {}


RULE_CODES = ("rule_not_in_list", "rule_skip_list", "rule_side",
              "rule_min_strike", "rule_0dte", "rule_loss_streak")


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
