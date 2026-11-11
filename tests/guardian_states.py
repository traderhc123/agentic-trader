"""Named dashboard states for the /v2 ("Guardian") dashboard, as view-models.

Every state here is built by the real builder (view_model.build_status) from
made-up settings, memory and trade-log rows at one fixed moment, so the
screens can be opened, screenshotted and tested with no broker, wallet or
network. Nothing in the product imports this file.

  STATES                  {name: (cfg, state, rows, runtime)}
  status(name)            the Status the agent would serve for that state
  activity(name, ...)     an ActivityPage of the same rows
  python tests/guardian_states.py --out DIR    write one JSON file per state
  python tests/guardian_dev_server.py          serve them to the built UI
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import view_model as vm  # noqa: E402

# Monday 2026-11-09, 2:41:07 PM ET: a trading day, market open
NOW = datetime(2026, 11, 9, 19, 41, 7, tzinfo=timezone.utc)
TODAY = "2026-11-09"
ACCT = "robinhood:0123456789"

CFG = {"source": "agenthc", "broker": "robinhood", "dry_run": True,
       "robinhood_account": "5QR74821", "max_entries_per_day": 4,
       "max_daily_loss_usd": 300, "max_open_positions": 8,
       "sizing_mode": "budget", "budget_per_trade_usd": 500,
       "other_sizing_mode": "budget", "other_budget_per_trade_usd": 150,
       "agenthc_api_key": "", "feed_pass": {"token": "fixture-pass"}}


def at(hh, mm=0, day=TODAY):
    """An ISO UTC stamp for hh:mm ET on `day` (EST, UTC-5)."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hh, minute=mm, tzinfo=timezone.utc) + timedelta(hours=5)
    return d.isoformat(timespec="seconds")


def runtime(**over):
    t = NOW.timestamp()
    rt = {"running": True, "started_at": t - 7200, "heartbeat_at": t - 4,
          "wait_s": 30.0, "poll_at": t - 4, "poll_ok": True,
          "feed_access": True, "signal_at": t - 4987,  # 1:18 PM ET
          "broker": {"id": "robinhood", "account_tag": ACCT, "connected": True,
                     "down_since": None, "last_ok_at": t - 7,
                     "last_error": None, "buying_power": 2431.18,
                     "buying_power_at": t - 7}}
    rt.update(over)
    return rt


def _pass_state(days_left=13, status="active"):
    from sources import agenthc_day_trade_ideas as hc
    return {"pass_status": {hc._token_id("fixture-pass"): {
        "status": status, "days_left": days_left}},
        "paid_days": ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
                      "2026-11-06", TODAY]}


def _text(c):
    sym, expiry, strike, side = c
    return f"{sym} {expiry} ${float(strike)} {side}"


def _key(c):
    sym, expiry, strike, side = c
    return f"{sym}|{expiry}|{float(strike)}|{side}"


def _row(action, c, ts, mode, book="main", **over):
    r = {"action": action, "event_id": f"{action}|{_key(c)}|{ts}",
         "contract": _text(c), "ts": ts, "book": book}
    ui = {"mode": mode}
    for k in ("check", "code", "params", "qty", "mark", "sent", "filled"):
        if k in over:
            ui[k] = over.pop(k)
    if action in ("entry", "exit"):
        ui["book"] = "main" if book == "main" else "other"
        r.pop("book")
    r.update(over)
    r["ui"] = ui
    return r


def entry(c, ts, qty, mark, real, mode=None, book="main"):
    return _row("entry", c, ts, mode or ("live" if real else "practice"), book,
                dry=not real, reason="passed your rules", qty=qty, mark=mark,
                sent=real, filled=real)


def exit_(c, ts, qty, pnl, real, mode=None, book="main", **over):
    r = _row("exit", c, ts, mode or ("live" if real else "practice"), book,
             dry=not real, reason="exit signal", est_pnl_usd=pnl, qty=qty,
             sent=real, filled=real)
    r["qty"] = qty
    r.update(over)
    return r


SPY = ("SPY", "2026-11-13", 700, "C")
AAPL = ("AAPL", "2026-11-20", 285, "C")
IWM_TODAY = ("IWM", TODAY, 245, "C")
QQQ = ("QQQ", "2026-11-13", 640, "C")
NVDA = ("NVDA", "2026-11-13", 232.5, "P")
AMD = ("AMD", "2026-11-13", 262.5, "C")


def _history(real_today):
    """The trade log, oldest first: a real-money week, then today traded in
    `real_today`'s book."""
    m = "live"
    d2, d3, d4, d5, d6 = ("2026-11-02", "2026-11-03", "2026-11-04",
                          "2026-11-05", "2026-11-06")
    p28, p29, p30 = "2026-10-28", "2026-10-29", "2026-10-30"
    rows = [
        # an older practice week
        entry(("SPY", "2026-10-30", 688, "C"), at(10, 2, p28), 1, 2.10, False),
        exit_(("SPY", "2026-10-30", 688, "C"), at(11, 40, p28), 1, 41.0, False),
        entry(("QQQ", "2026-10-30", 630, "P"), at(12, 5, p28), 1, 1.75, False),
        exit_(("QQQ", "2026-10-30", 630, "P"), at(14, 30, p28), 1, 15.0, False),
        entry(("NVDA", "2026-10-30", 225, "C"), at(10, 15, p29), 2, 1.90, False),
        exit_(("NVDA", "2026-10-30", 225, "C"), at(13, 2, p29), 2, 210.0, False),
        entry(("IWM", "2026-10-30", 243, "C"), at(10, 30, p30), 1, 1.31, False),
        exit_(("IWM", "2026-10-30", 243, "C"), at(15, 45, p30), 1, -54.0, False,
              cause="expiry_guard"),
        # the real-money week
        _row("policy_veto", ("AMD", d6, 255, "C"), at(9, 38, d2), m,
             check="written_rules",
             reason="The signal arrived at 9:38 AM ET, and your written rules "
                    "allow no new trades before 9:45 AM ET."),
        entry(("SPY", d4, 694, "C"), at(10, 12, d2), 2, 2.35, True),
        exit_(("SPY", d4, 694, "C"), at(13, 26, d2), 2, 188.0, True),
        _row("skip_no_quote", ("PLTR", d6, 187.5, "C"), at(14, 2, d2), m,
             check="trade_size", reason="no price quote for the contract"),
        entry(("QQQ", d6, 628, "C"), at(10, 3, d3), 4, 1.21, True),
        _row("entry_unfilled", ("META", d6, 742.5, "C"), at(10, 44, d3), m,
             check="broker_order", sent=True, filled=False,
             reason="the order was placed but never filled; the broker "
                    "cancelled it"),
        _row("order_failed", ("AAPL", d6, 282.5, "P"), at(11, 30, d3), m,
             check="broker_order", sent=True, filled=False,
             reason="the broker rejected the order: not enough buying power",
             hint="Add cash to your broker account, or lower your trade size."),
        exit_(("QQQ", d6, 628, "C"), at(12, 58, d3), 4, -316.0, True),
        _row("skip_daily_loss", ("AMD", d6, 257.5, "C"), at(14, 21, d3), m,
             check="daily_loss",
             params={"max_loss_usd": 300.0, "loss_usd": 316.0}),
        entry(("NVDA", d6, 228, "C"), at(9, 58, d4), 4, 1.18, True),
        entry(("SPY", d6, 697, "P"), at(10, 20, d4), 2, 1.92, True),
        entry(("IWM", d6, 244, "C"), at(10, 52, d4), 1, 1.44, True),
        _row("exit_partial", ("NVDA", d6, 228, "C"), at(11, 36, d4), m,
             sent=True, reason="the exit only partly filled: 2 of 4 contracts"),
        exit_(("NVDA", d6, 228, "C"), at(11, 36, d4), 2, 106.0, True),
        exit_(("NVDA", d6, 228, "C"), at(11, 37, d4), 2, 96.0, True,
              cause="exit_remainder"),
        exit_(("IWM", d6, 244, "C"), at(12, 5, d4), 1, 26.0, True),
        entry(("AMZN", d6, 262.5, "C"), at(13, 11, d4), 2, 2.02, True),
        _row("skip_daily_cap", ("GOOGL", d6, 301, "C"), at(13, 40, d4), m,
             check="daily_cap", params={"cap": 4}),
        exit_(("SPY", d6, 697, "P"), at(14, 48, d4), 2, -96.0, True),
        exit_(("AMZN", d6, 262.5, "C"), at(15, 31, d4), 2, 74.0, True),
        _row("broker_disconnected", ("QQQ", d6, 633, "P"), at(9, 51, d5), m,
             check="broker_order", sent=False,
             reason="the broker connection is down (login expired)",
             hint="Sign in to your broker again in Settings."),
        _row("skip_ai_needed", ("AMZN", d6, 264, "C"), at(10, 5, d5), m,
             check="written_rules",
             reason="your written rules need the AI helper, and it is not "
                    "connected",
             hint="Reconnect the AI helper in Settings, or turn off written "
                  "rules."),
        entry(("SPY", d6, 699, "C"), at(10, 47, d5), 2, 2.26, True),
        _row("skip_stale", ("NVDA", d6, 231, "P"), at(11, 20, d5), m,
             check="signal_fresh", params={"max_age_s": 300},
             reason="signal was 300s+ old when the agent saw it (computer "
                    "asleep or offline?)"),
        _row("exit_failed", ("SPY", d6, 699, "C"), at(13, 2, d5), m,
             sent=True, reason="the exit order didn't go through; retrying"),
        exit_(("SPY", d6, 699, "C"), at(13, 3, d5), 2, 102.0, True),
        entry(("META", "2026-11-13", 745, "C"), at(13, 35, d5), 1, 4.60, True),
        entry(("IWM", d6, 246, "P"), at(14, 5, d5), 1, 1.52, True),
        {**exit_(("META", "2026-11-13", 745, "C"), at(15, 6, d5), 1, 55.0, True),
         "manual": True, "exit_price": 5.15,
         "closed_at": at(15, 6, d5)},
        entry(SPY, at(9, 49, d6), 1, 4.12, True),
        entry(AAPL, at(10, 33, d6), 2, 2.31, True),
        entry(IWM_TODAY, at(11, 5, d6), 1, 1.86, True),
        _row("ledger_adopt", AAPL, at(11, 42, d6), m, dry=False, qty=1,
             reason="the broker shows 2 contracts, the records showed 1"),
        _row("skip_position_cap", ("NFLX", "2026-11-13", 1180, "C"),
             at(11, 50, d6), m, check="position_cap", params={"max_open": 8}),
        entry(("QQQ", "2026-11-13", 636, "P"), at(12, 18, d6), 1, 3.05, True),
        exit_(("QQQ", "2026-11-13", 636, "P"), at(14, 10, d6), 1, -74.0, True),
        exit_(("IWM", d6, 246, "P"), at(15, 45, d6), 1, -57.0, True,
              cause="expiry_guard"),
    ]
    tm = "live" if real_today else "practice"
    rows += [
        entry(QQQ, at(9, 41), 1, 4.35, real_today),
        _row("skip_budget", ("MSFT", "2026-11-13", 525, "C"), at(10, 5), tm,
             check="trade_size", params={"cost_usd": 1120.0},
             reason="1 contract ≈ $1,120 exceeds per-trade budget $500"),
        entry(NVDA, at(10, 22), 2, 2.44, real_today),
        entry(("IWM", "2026-11-13", 246, "C"), at(10, 40), 1, 1.12, real_today),
        exit_(("IWM", "2026-11-13", 246, "C"), at(11, 31), 1, 64.0, real_today),
        _row("policy_veto", ("META", "2026-11-13", 748, "C"), at(11, 48), tm,
             check="written_rules",
             reason="META reports earnings this week, and your written rules "
                    "skip single stocks in their earnings week."),
        _row("skip_rule", ("TSLA", "2026-11-13", 452.5, "C"), at(12, 15), tm,
             check="your_rules", code="rule_skip_list",
             params={"ticker": "TSLA"}, reason="TSLA is on your skip list"),
        _row("skip_budget", ("COST", "2026-11-13", 1010, "C"), at(12, 42), tm,
             check="trade_size", params={"cost_usd": 1240.0},
             reason="1 contract ≈ $1,240 exceeds per-trade budget $500"),
        _row("skip_budget", ("AVGO", "2026-11-13", 395, "C"), at(13, 5), tm,
             "shadow", check="trade_size",
             params={"cost_usd": 480.0},
             reason="1 contract ≈ $480 exceeds per-trade budget $150"),
        _row("skip_budget", ("AMZN", "2026-11-13", 265, "P"), at(13, 18), tm,
             "shadow", check="trade_size",
             params={"cost_usd": 295.0},
             reason="1 contract ≈ $295 exceeds per-trade budget $150"),
    ]
    return rows


def _cap_rows(real_today):
    tm = "live" if real_today else "practice"
    return [entry(AMD, at(13, 30), 1, 1.38, real_today, book="other"),
            _row("skip_daily_cap", ("GOOGL", "2026-11-13", 305, "C"),
                 at(14, 10), tm, check="daily_cap", params={"cap": 4})]


def _pos(c, qty, mark, real, opened):
    if real:
        return {"option_id": "x-" + c[0], "qty": qty, "live": True,
                "entry_mark": mark, "acct": ACCT,
                "opened_at": datetime.fromisoformat(opened).timestamp()}
    return {"option_id": "dry", "qty": qty, "dry": True, "entry_mark": mark}


def _state(real_today, extra=()):
    d6 = "2026-11-06"
    pos = {_key(SPY): _pos(SPY, 1, 4.12, True, at(9, 49, d6)),
           _key(AAPL): _pos(AAPL, 2, 2.31, True, at(10, 33, d6)),
           _key(IWM_TODAY): _pos(IWM_TODAY, 1, 1.86, True, at(11, 5, d6)),
           _key(QQQ): _pos(QQQ, 1, 4.35, real_today, at(9, 41)),
           _key(NVDA): _pos(NVDA, 2, 2.44, real_today, at(10, 22))}
    for c, qty, mark, opened in extra:
        pos[_key(c)] = _pos(c, qty, mark, real_today, opened)
    return {"positions": pos, "seen": [], **_pass_state()}


def _numbered(rows):
    return [{**r, "n": i} for i, r in enumerate(rows)]


LIVE = {**CFG, "dry_run": False}
_BROKER = runtime()["broker"]
_AMD_POS = [(AMD, 1, 1.38, at(13, 30))]

# name -> (cfg, state, rows, runtime)
STATES = {
    "practice": (CFG, _state(False), _history(False), runtime()),
    "live": (LIVE, _state(True), _history(True), runtime()),
    "paused-new-trades": ({**LIVE, "paused": True}, _state(True),
                          _history(True), runtime()),
    "practice-paused-new-trades": ({**CFG, "paused": True}, _state(False),
                                   _history(False), runtime()),
    # a full hold: the agent places no orders at all until the owner resumes
    "state-hold": (LIVE, _state(True), _history(True),
                   runtime(hold="state_hold",
                           hold_since="2026-11-09T18:02:00+00:00")),
    "handoff-hold": (LIVE, _state(True), _history(True),
                     runtime(hold="handoff_hold")),
    "restarting": (LIVE, _state(True), _history(True),
                   runtime(hold="restarting")),
    "limit-hit": (LIVE, _state(True, _AMD_POS),
                  _history(True) + _cap_rows(True), runtime()),
    "broker-disconnected": (
        LIVE, _state(True), _history(True),
        runtime(broker={**_BROKER, "connected": False,
                        "down_since": NOW.timestamp() - 2587,  # 1:58 PM ET
                        "last_ok_at": NOW.timestamp() - 2590,
                        "last_error": "login expired",
                        "buying_power_at": NOW.timestamp() - 2590})),
    "low-cash": (LIVE, _state(True), _history(True),
                 runtime(broker={**_BROKER, "buying_power": 38.5})),
    "not-answering": (LIVE, _state(True), _history(True),
                      runtime(heartbeat_at=NOW.timestamp() - 1500,
                              poll_at=NOW.timestamp() - 1500)),
    "stopped": (LIVE, _state(True), _history(True), None),
    "empty-first-run": (CFG, {"positions": {}, "seen": [],
                              **_pass_state(13)}, [],
                        runtime(signal_at=None)),
    "empty-live": (LIVE, {"positions": {}, "seen": [], **_pass_state(13)}, [],
                   runtime(signal_at=None)),
    # the prepaid signal days ran out: exit signals still arrive, entries don't
    "pass-exits-only": (CFG, {**_state(False), **_pass_state(0, "exits_only")},
                        _history(False), runtime(feed_access=False)),
}


def _with(rows, **ui):
    """A copy of a row with more facts recorded on it."""
    return {**rows, "ui": {**rows["ui"], **ui}}


_LAPSED = ("Your AgentHC feed pass has run out — the agent gets no signals "
           "until you buy more signal days (dashboard → Settings → Signal "
           "source).")
_REFUSED = ("AgentHC refused the day-pass your agent's wallet paid for today. "
            "The agent will NOT buy another one today. Until AgentHC accepts "
            "it the agent gets no signals — no new entries, and no exit "
            "signals for positions it holds, so watch those yourself.")
_T = NOW.timestamp()
STATES.update({
    # the prepaid pass is used up past its exits-only days, no wallet
    # auto-pay: no exit signal can arrive, so nothing sells on one
    "pass-lapsed": (LIVE, {**_state(True), **_pass_state(0, "used_up"),
                           "pass_notice": _LAPSED},
                    _history(True), runtime(feed_access=False)),
    # AgentHC refused the day-pass the wallet paid for today
    "day-pass-refused": (
        {**LIVE, "feed_pass": None, "lnbits_url": "https://wallet.example",
         "day_pass_recurring": True},
        {**_state(True), "l402": {"token": "t", "expires_at": _T + 3600,
                                  "refused_at": _T - 600},
         "daypass_alert": "daypass_refused_notified", "pass_notice": _REFUSED},
        _history(True), runtime(feed_access=False)),
    # the last read of the feed failed
    "feed-down": (LIVE, _state(True), _history(True),
                  runtime(poll_ok=False, feed_access=None)),
    # the owner switched the expiry sell off
    "expiry-guard-off": ({**LIVE, "expiry_guard_et": "off"}, _state(True),
                         _history(True), runtime()),
    # past the (earlier) expiry sell time, IWM has no bid: the guard leaves it
    "expiry-no-bid": (
        {**LIVE, "expiry_guard_et": "14:30"},
        {**_state(True), "positions": {
            **_state(True)["positions"],
            _key(IWM_TODAY): {**_pos(IWM_TODAY, 1, 1.86, True, at(11, 5, "2026-11-06")),
                              "expiry_no_bid": TODAY}}},
        _history(True), runtime()),
    # orders the broker gave no clear answer on, a sell still working, a sale
    # with no estimate
    "check-these-orders": (LIVE, _state(True), _history(True) + [
        _row("order_failed", ("TSLA", "2026-11-13", 460, "C"), at(14, 20),
             "live", check="broker_order", code="buy_unconfirmed", sent=True,
             reason="Robinhood didn't answer the buy order for 1x TSLA, and "
                    "it isn't listed yet — the agent won't buy again"),
        _row("exit_failed", ("AMD", "2026-11-13", 262.5, "C"), at(14, 25),
             "live", code="sell_unconfirmed", sent=True,
             reason="Robinhood didn't answer the sell order for 1x AMD"),
        _with(exit_(("MSFT", "2026-11-13", 530, "C"), at(14, 30), 2, 12.0,
                    True), filled=False),
        exit_(("META", "2026-11-13", 750, "C"), at(14, 35), 1, None, True),
    ], runtime()),
    # the prepaid pass is used up, but the wallet's auto-pay bought today's
    # day-pass and the agent reads the feed with it; the old lapse notice is
    # still in its memory
    "wallet-over-lapsed-pass": (
        {**LIVE, "lnbits_url": "https://wallet.example",
         "day_pass_recurring": True},
        {**_state(True), **_pass_state(0, "used_up"),
         "l402": {"token": "t", "expires_at": _T + 3600},
         "pass_notice": _LAPSED},
        _history(True), runtime(feed_access=True)),
    # the agent could not check the prepaid pass with AgentHC
    "pass-unchecked": (LIVE, {**_state(True), "pass_status": {}},
                       _history(True), runtime(feed_access=False)),
    # the broker is down and the agent says so too, in its classic words:
    # one note on the screen (the dashboard's), the agent's folded into it
    "broker-down-agent-says-so": (
        LIVE, _state(True), _history(True),
        runtime(broker={**_BROKER, "connected": False,
                        "down_since": NOW.timestamp() - 2587,
                        "last_ok_at": NOW.timestamp() - 2590,
                        "last_error": "login expired",
                        "buying_power_at": NOW.timestamp() - 2590},
                notices=[{"level": "urgent", "code": "broker",
                          "condition": "broker_down",
                          "text": "Robinhood connection is DOWN since 1:58 "
                                  "PM ET. Fix: reconnect it in Settings → "
                                  "Broker."},
                         {"level": "warn", "code": "note",
                          "text": "Records could not be written to disk.",
                          "condition": "records_unwritten"}])),
    # the AI helper proposed a code change that waits for the owner
    "proposal-pending": (
        LIVE, _state(True), _history(True),
        runtime(proposal={"explanation": "Adds a connector for another broker.",
                          "warnings": [], "changes": [{
                              "path": "brokers/example.py", "content": "x = 1\n",
                              "diff": "--- a/brokers/example.py\n+++ b/brokers/"
                                      "example.py\n@@ -0,0 +1 @@\n+x = 1\n"}]})),
    # AgentHC changed the agreement since the owner accepted it
    "agreement-changed": (LIVE, _state(True), _history(True),
                          runtime(consent_ok=False)),
    # LIVE on a broker's paper-trading account
    "paper-account": (
        {**LIVE, "broker": "alpaca", "alpaca_paper": True},
        {"positions": {}, "seen": [], **_pass_state()},
        _history(True)[:-10] + [
            _with(entry(QQQ, at(9, 41), 1, 4.35, True), paper=True),
            _with(exit_(QQQ, at(11, 31), 1, 64.0, True), paper=True)],
        runtime(broker={**_BROKER, "id": "alpaca",
                        "account_tag": "alpaca:paper"})),
})


# ── the no-signal sell and the pass's last days ─────────────────────────────

_RUN_OUT = ("Your signal pass has run out: no new trades until you buy more "
            "signal days (dashboard → Settings → Signal source).")


def _signal_loss():
    """The prepaid pass ran out at 1:00 PM ET with real positions open: no
    exit signal can reach the agent, it warned the owner at 1:05 PM, and it
    sells what it holds at 3:30 PM unless signals are back. The running
    agent hands over its own line about it (agent.signal_loss_banner)."""
    import agent as A
    cfg = LIVE
    st = {**_state(True), **_pass_state(0, "used_up"), "pass_notice": _RUN_OUT,
          "signal_loss": {"since": _T - 6060, "cause": "pass_ended",
                          "warned_at": _T - 5760, "warned_day": TODAY}}
    banner = A.signal_loss_banner(cfg, st, NOW.astimezone(A.MARKET_TZ))
    return (cfg, st, _history(True), runtime(feed_access=False, notices=[
        {**w, "code": "signal_loss", "condition": "positions_unmanaged"}
        for w in banner]))


def _signal_loss_feed():
    """The prepaid pass is active, but AgentHC answers feed_not_live from
    1:00 PM ET: each read raises nothing and sees no exits. The agent
    records the loss and arms its 3:30 PM sale (review: the positions
    must not say they sell on their exit signals)."""
    import agent as A
    cfg = LIVE
    st = {**_state(True), **_pass_state(3, "active"),
          "signal_loss": {"since": _T - 6060, "cause": "feed_not_live",
                          "warned_at": _T - 5760, "warned_day": TODAY}}
    banner = A.signal_loss_banner(cfg, st, NOW.astimezone(A.MARKET_TZ))
    return (cfg, st, _history(True), runtime(feed_access=False, notices=[
        {**w, "code": "signal_loss", "condition": "positions_unmanaged"}
        for w in banner]))


STATES.update({
    "signal-loss-sell": _signal_loss(),
    "signal-loss-feed-not-live": _signal_loss_feed(),
    # one signal day left on the prepaid pass; then its last day
    "pass-one-day-left": (LIVE, {**_state(True), **_pass_state(1, "active")},
                          _history(True), runtime()),
    "pass-last-day": (LIVE, {**_state(True), **_pass_state(0, "active"),
                             "pass_charged_day": TODAY},
                      _history(True), runtime()),
    # the 08:30 renewal of the wallet day-pass failed; the running one still
    # reads entries until 3:00 PM ET (review F5: the alert is on /v2 too)
    "daypass-renewal-failed": (
        {**LIVE, "feed_pass": None, "lnbits_url": "https://wallet.example",
         "day_pass_recurring": True},
        {**_state(True), "l402": {"token": "t", "expires_at": _T + 1133},
         "daypass_alert": "daypass_failed_notified",
         "pass_notice": ("Your agent's wallet could not pay for today's "
                         "day-pass (10,000 sats): the wallet does not hold "
                         "enough sats — add sats in the dashboard (Settings → "
                         "Signal source). Nothing was paid. The pass it has "
                         "runs until 3:00 PM ET; it opens no new trades today "
                         "because that is before the close.")},
        _history(True), runtime(feed_access=True)),
})


def _stress():
    """Hostile and oversized data: long names, huge numbers, unknown kinds,
    rows from before the facts were recorded."""
    long = "W" * 400
    rows = _history(False) + [
        {"action": "something_new", "contract": long, "ts": at(13, 40),
         "reason": "<script>alert(1)</script> " + long},
        {"action": "entry", "event_id": "old", "ts": at(13, 41),
         "contract": "BRK.B 2027-01-15 $99999.5 C", "dry": False,
         "reason": "old row"},
        {"action": "exit", "event_id": "old-x", "ts": at(13, 42),
         "contract": "BRK.B 2027-01-15 $99999.5 C", "dry": False,
         "est_pnl_usd": -1234567.89},
        {"action": "skip_rule", "ts": "not a time", "contract": None},
        {"action": "exit", "ts": at(13, 43), "contract": "SPY", "dry": True,
         "est_pnl_usd": 98765432.1},
    ]
    st = _state(False)
    st["positions"]["not|a|key"] = {"qty": "many", "live": True}
    st["positions"]["BRK.B|2027-01-15|99999.5|C"] = {
        "option_id": "big", "qty": 250, "live": True, "entry_mark": 812.345,
        "acct": ACCT, "opened_at": NOW.timestamp() - 90000}
    st["positions"][_key(("GOOGL", "2026-12-18", 305, "P"))] = {
        "option_id": "dry", "qty": 3, "dry": True}
    cfg = {**CFG, "max_entries_per_day": 40, "max_open_positions": 60,
           "sizing_mode": "percent", "percent_per_trade": 2.5,
           "other_sizing_mode": "", "max_daily_loss_usd": 0}
    rt = runtime(notices=[
        {"level": "urgent", "code": "expiry", "text": "IWM $245 call expires "
         "today and was not sold by 3:50 PM ET. " + long},
        {"level": "warn", "code": "update", "text": "An update is ready."},
        {"level": "info", "code": "note", "text": "Signals resumed."}])
    return cfg, st, rows, rt


STATES["stress"] = _stress()


def status(name, now=NOW):
    cfg, state, rows, rt = STATES[name]
    return vm.build_status(cfg, state, _numbered(rows), rt, now=now)


def activity(name, before=None, limit=None):
    _cfg, _state_, rows, _rt = STATES[name]
    return vm.activity_page(_numbered(rows), before=before,
                            limit=limit or vm.ACTIVITY_LIMIT)


if __name__ == "__main__":
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        os.makedirs(out, exist_ok=True)
        for name in STATES:
            with open(os.path.join(out, name + ".json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump(status(name), f, indent=1)
        print(f"wrote {len(STATES)} states to {out}")
    else:
        print(__doc__)
