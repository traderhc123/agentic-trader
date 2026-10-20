"""Dry-run preview: the trades this setup WOULD have taken.

In dry-run the agent can show what it would have done even with no paid
signal feed. It reads AgentHC's free list of recently CLOSED paper trades
(main pick, past week; the same rows the public track record shows; never
an open position) and checks each one against YOUR built-in rules (tickers,
calls/puts, min strike, no 0DTE, loss stop). The dashboard lists every trade
as "would take" or "your rules would skip it (why)", with AgentHC's paper
P&L.

Limits, shown on the dashboard: these are closed trades only, so they show
up after the exit, not in real time; your AI policy (if any), daily caps and
budget aren't applied; P&L is AgentHC's paper result, not your fills.
Nothing here places orders. Hypothetical, educational, not advice.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import rules
from net_check import http as _http

MARKET_TZ = ZoneInfo("America/New_York")
DAYS = 7
REFRESH_MARKET_S = 15 * 60      # while the market is open
REFRESH_IDLE_S = 3 * 60 * 60    # nights / weekends
RETRY_S = 5 * 60                # after a failed fetch
MAX_ROWS = 40
BREAKEVEN_MAX_PCT = 6.0


def _home():
    return os.path.expanduser(os.getenv("AGENT_HOME", "~/.agentic-trader"))


def _path():
    return os.path.join(_home(), "dry_run_preview.json")


def wanted(cfg):
    """Only in dry-run and only for the AgentHC source (it's AgentHC's
    journal). Owner can turn it off with `preview off`."""
    return (bool(cfg.get("dry_run")) and cfg.get("source", "agenthc") == "agenthc"
            and cfg.get("dry_run_preview", True) is not False)


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    try:
        os.makedirs(_home(), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _path())
    except OSError:
        pass


def fetch(days=DAYS):
    """The free closed-trades list, newest first. Raises on network/HTTP
    errors (the caller retries later)."""
    from sources.agenthc_day_trade_ideas import AGENTHC_API, FEED_PATH
    r = _http.get(f"{AGENTHC_API}{FEED_PATH}/closed", params={"days": days},
                  timeout=15)
    if r.status_code == 404:
        return None   # server doesn't offer it yet
    r.raise_for_status()
    trades = (r.json() or {}).get("trades") or []
    return [t for t in trades if isinstance(t, dict)]


def _when(t):
    try:
        return datetime.fromisoformat(str(t.get("entry_time"))).astimezone(MARKET_TZ)
    except (TypeError, ValueError):
        return None


def evaluate(cfg, trades):
    """Each closed trade → a dashboard row with this setup's verdict. Walks
    oldest first so the loss-stop rule sees the earlier paper exits of the
    same day (as the live agent would)."""
    rows, today_log, day = [], [], None
    for t in sorted(trades, key=lambda x: str(x.get("entry_time") or x.get("date"))):
        d = str(t.get("date") or "")[:10]
        if d != day:
            day, today_log = d, []
        side = str(t.get("direction") or "").lower()
        ev = {"event": "ENTERED", "ticker": str(t.get("ticker") or "").upper(),
              "type": "P" if side.startswith("put") else "C",
              "strike": t.get("strike"), "expiry": t.get("expiry")}
        ok, why = rules.check(cfg, ev, today_log, now=_when(t))
        try:
            pnl = float(t.get("paper_pnl_pct"))
        except (TypeError, ValueError):
            pnl = None
        if ok and pnl is not None:
            # stands in for an exit the live agent would have logged today
            today_log.append({"action": "exit", "est_pnl_usd": pnl})
        strike = t.get("strike")
        rows.append({
            "date": d,
            "entry_time": t.get("entry_time"),
            "exit_time": t.get("exit_time"),
            "contract": (f"{ev['ticker']} {strike:g}{ev['type']}"
                         if isinstance(strike, (int, float)) else ev["ticker"])
                        + (f" exp {t['expiry']}" if t.get("expiry") else ""),
            "hold_minutes": t.get("hold_minutes"),
            "paper_pnl_pct": pnl,
            "exit_kind": t.get("exit_kind") or "",
            "would_take": ok,
            "skip_reason": why,
        })
    rows.reverse()   # newest first for the dashboard
    return rows[:MAX_ROWS]


def refresh(cfg, market_open, now=None, fetcher=fetch):
    """Fetch again when due. Never raises. Returns the saved data."""
    now = now or time.time()
    data = load()
    every = REFRESH_MARKET_S if market_open else REFRESH_IDLE_S
    due = data.get("next_at") or (data.get("fetched_at", 0) + every)
    if data.get("trades") is not None and now < due:
        return data
    try:
        trades = fetcher()
    except Exception as exc:
        data.update(next_at=now + RETRY_S, error=str(exc)[:160])
        _save(data)
        return data
    if trades is None:
        data.update(trades=[], unavailable=True, next_at=now + REFRESH_IDLE_S)
    else:
        data = {"fetched_at": now, "trades": trades, "next_at": now + every}
    _save(data)
    return data


def summary(cfg, data=None):
    """What the dashboard shows (None when the preview is off)."""
    if not wanted(cfg):
        return None
    data = load() if data is None else data
    rows = evaluate(cfg, data.get("trades") or [])
    taken = [r for r in rows if r["would_take"] and r["paper_pnl_pct"] is not None]
    # W/BE/L: 0% to +6% is breakeven (round-trip costs eat a small gain)
    wins = sum(1 for r in taken if r["paper_pnl_pct"] > BREAKEVEN_MAX_PCT)
    return {
        "days": DAYS,
        "rows": rows,
        "would_take": len(taken),
        "wins": wins,
        "breakeven": sum(1 for r in taken
                         if 0 <= r["paper_pnl_pct"] <= BREAKEVEN_MAX_PCT),
        "losses": sum(1 for r in taken if r["paper_pnl_pct"] < 0),
        "avg_paper_pnl_pct": (round(sum(r["paper_pnl_pct"] for r in taken) / len(taken), 1)
                              if taken else None),
        "updated_at": data.get("fetched_at"),
        "error": data.get("error") if not data.get("trades") else "",
        "unavailable": bool(data.get("unavailable")),
    }
