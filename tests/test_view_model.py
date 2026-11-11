"""The dashboard's view-model (view_model.py, schema 2): the pure builder
over every state the new dashboard shows, hostile data, and the TypeScript
type the React app compiles against."""

import copy
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import agent
import view_model as vm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Monday 2026-11-09, 2:41:07 PM ET — a trading day, market open
NOW = datetime(2026, 11, 9, 19, 41, 7, tzinfo=timezone.utc)
TODAY = "2026-11-09"


def at(hh, mm=0, day=TODAY):
    """An ISO UTC stamp for hh:mm ET on `day` (EST, UTC-5)."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hh, minute=mm, tzinfo=timezone.utc) + timedelta(hours=5)
    return d.isoformat(timespec="seconds")


CFG = {"source": "agenthc", "broker": "robinhood", "dry_run": True,
       "robinhood_account": "5QR74821", "max_entries_per_day": 4,
       "max_daily_loss_usd": 300, "max_open_positions": 8,
       "sizing_mode": "budget", "budget_per_trade_usd": 500,
       "other_sizing_mode": "budget", "other_budget_per_trade_usd": 150,
       "agenthc_api_key": "k"}
ACCT = "robinhood:0123456789"


def runtime(**over):
    rt = {"running": True, "started_at": NOW.timestamp() - 7200,
          "heartbeat_at": NOW.timestamp() - 12, "wait_s": 30.0,
          "poll_at": NOW.timestamp() - 12, "poll_ok": True,
          "feed_access": True, "signal_at": NOW.timestamp() - 1860,
          "broker": {"id": "robinhood", "account_tag": ACCT,
                     "connected": True, "down_since": None,
                     "last_ok_at": NOW.timestamp() - 15, "last_error": None,
                     "buying_power": 2140.55,
                     "buying_power_at": NOW.timestamp() - 300}}
    rt.update(over)
    return rt


def real_pos(qty=1, mark=4.12, opened="2026-11-06T14:49:00+00:00", **over):
    row = {"option_id": "abc", "qty": qty, "live": True, "entry_mark": mark,
           "opened_at": datetime.fromisoformat(opened).timestamp(),
           "acct": ACCT}
    row.update(over)
    return row


def state(**positions):
    return {"positions": dict(positions), "seen": []}


SPY = "SPY|2026-11-13|700.0|C"
IWM_TODAY = f"IWM|{TODAY}|245.0|C"
QQQ = "QQQ|2026-11-13|640.0|C"
PRACTICE_QQQ = {"option_id": "dry", "qty": 1, "dry": True, "entry_mark": 4.35}


def numbered(rows):
    return [{**r, "n": i} for i, r in enumerate(rows)]


def entry(contract, ts, dry=False, **ui):
    r = {"action": "entry", "event_id": f"ENTERED|{contract}|{ts}",
         "contract": contract, "dry": dry, "reason": "test", "ts": ts}
    if ui:
        r["ui"] = ui
    return r


def exit_(contract, ts, pnl, dry=False, **over):
    r = {"action": "exit", "event_id": f"EXITED|{contract}|{ts}",
         "contract": contract, "dry": dry, "reason": "exit", "ts": ts,
         "est_pnl_usd": pnl}
    r.update(over)
    return r


def build(cfg=None, st=None, rows=(), rt="default", now=NOW, **kw):
    out = vm.build_status(CFG if cfg is None else cfg,
                          state() if st is None else st, numbered(rows),
                          runtime() if rt == "default" else rt, now=now, **kw)
    assert vm.validate(out) == [], vm.validate(out)
    json.loads(json.dumps(out, allow_nan=False))  # plain JSON, no NaN
    return out


# ── every state the dashboard shows ─────────────────────────────────────────

def test_practice_with_real_positions_still_managed():
    s = build(st=state(**{SPY: real_pos(), QQQ: PRACTICE_QQQ}),
              rows=[entry("QQQ 2026-11-13 $640.0 C", at(9, 41), dry=True,
                          mode="practice", book="other", qty=1)])
    assert s["schema"] == 2 and s["as_of"] == "2026-11-09T19:41:07Z"
    assert s["mode"] == "practice" and s["mode_explicit"] is True
    assert s["pause"] == {"kind": "none", "scope": "none",
                          "entries_paused": False, "since": None,
                          "survives_restart": False, "can_resume": False}
    assert s["agent"]["running"] and s["agent"]["answering"]
    assert s["agent"]["last_heartbeat"] == "2026-11-09T19:40:55Z"
    assert s["real_positions"] == 1 and s["real_positions_managed"] is True
    assert s["unmanaged_reason"] is None
    by_id = {p["id"]: p for p in s["positions"]}
    spy, qqq = by_id[SPY], by_id[QQQ]
    assert spy["real"] and spy["contract"] == {
        "symbol": "SPY", "expiry": "2026-11-13", "strike": 700.0, "side": "C"}
    assert spy["qty"] == 1 and spy["entry_price"] == 4.12
    assert spy["entry_cost_usd"] == 412.0
    assert spy["opened_at"] == "2026-11-06T14:49:00Z"
    assert spy["account"] == ACCT
    # the real one is still sold for real, in practice mode
    assert spy["next"]["action"] == "sell_on_exit"
    assert spy["next"]["expiry_guard_at"] == "2026-11-13T20:45:00Z"
    assert not qqq["real"] and qqq["account"] is None
    assert qqq["next"]["action"] == "paper_close"
    # opened time and book of a practice position come from its entry row
    assert qqq["opened_at"] == "2026-11-09T14:41:00Z" and qqq["book"] == "other"
    assert s["capabilities"]["opening"] == {"state": "practice_only",
                                            "reason_code": "practice_mode"}
    assert s["capabilities"]["closing"] == {"state": "yes", "reason_code": "ok"}
    assert s["capabilities"]["listening"] == {"state": "yes", "reason_code": "ok"}
    assert s["limits"]["open_positions"] == {"real": 1, "practice": 1}


def test_live():
    s = build(cfg={**CFG, "dry_run": False}, st=state(**{SPY: real_pos()}))
    assert s["mode"] == "live"
    assert s["capabilities"]["opening"] == {"state": "yes", "reason_code": "ok"}
    b = s["broker"]
    assert b["name"] == "Robinhood" and b["account_mask"] == "4821"
    assert b["connected"] is True and b["since"] is None
    assert b["last_answer"] == "2026-11-09T19:40:52Z"
    assert b["buying_power"] == 2140.55 and b["paper_account"] is False
    assert s["limits"]["cash"] == {"low": False, "low_reason": None,
                                   "trades_affordable": 4}
    assert s["signals"]["last_poll"] == "2026-11-09T19:40:55Z"
    assert s["signals"]["last_signal"] == "2026-11-09T19:10:07Z"
    assert s["signals"]["last_signal_from"] == "feed"
    assert s["signals"]["pass"]["kind"] == "subscription"


def test_mode_is_live_when_the_settings_never_said():
    """One definition, the engine's: only a true dry_run is practice."""
    cfg = {k: v for k, v in CFG.items() if k != "dry_run"}
    s = build(cfg=cfg)
    assert s["mode"] == "live" and s["mode_explicit"] is False
    assert agent._mode(cfg) == "live" and agent._mode({"dry_run": 1}) == "practice"


def test_entries_only_pause_keeps_exits_running():
    s = build(cfg={**CFG, "dry_run": False, "paused": True},
              st=state(**{SPY: real_pos()}))
    assert s["mode"] == "live"  # pausing never changes the mode
    assert s["pause"] == {"kind": "entries", "scope": "entries",
                          "entries_paused": True, "since": None,
                          "survives_restart": True, "can_resume": True}
    assert s["capabilities"]["opening"] == {"state": "paused",
                                            "reason_code": "owner_pause"}
    assert s["capabilities"]["closing"]["state"] == "yes"
    assert s["real_positions_managed"] is True
    assert s["positions"][0]["next"]["action"] == "sell_on_exit"
    assert s["signals"]["reading"] is True


def test_pause_on_a_pay_per_day_feed_with_nothing_real_open_reads_nothing():
    cfg = {**CFG, "paused": True, "agenthc_api_key": "",
           "feed_pass": {"token": "tok"}}
    s = build(cfg=cfg, st=state(**{QQQ: PRACTICE_QQQ}))
    assert s["signals"]["reading"] is False
    assert s["positions"][0]["next"] == {
        "action": "unmanaged", "at": None,
        "reason_code": "pause_feed_not_read",
        "expiry_guard_at": "2026-11-13T20:45:00Z"}
    assert s["capabilities"]["listening"] == {
        "state": "paused", "reason_code": "pause_feed_not_read"}
    assert s["capabilities"]["closing"] == {
        "state": "paused", "reason_code": "pause_feed_not_read"}
    # a practice position is never "a real position left unwatched"
    assert s["real_positions_managed"] is True


@pytest.mark.parametrize("hold,resume,survives", [
    ("state_hold", True, True), ("handoff_hold", False, False),
    ("restarting", False, False)])
def test_full_holds_leave_real_positions_unmanaged(hold, resume, survives):
    s = build(cfg={**CFG, "dry_run": False}, st=state(**{SPY: real_pos()}),
              rt=runtime(hold=hold, hold_since="2026-11-09T13:02:00+00:00"))
    assert s["pause"]["kind"] == hold and s["pause"]["scope"] == "all"
    assert s["pause"]["can_resume"] is resume
    assert s["pause"]["survives_restart"] is survives
    assert s["pause"]["since"] == ("2026-11-09T13:02:00Z"
                                   if hold == "state_hold" else None)
    assert s["mode"] == "live"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == hold
    assert s["positions"][0]["next"]["action"] == "unmanaged"
    assert s["positions"][0]["next"]["reason_code"] == hold
    for what in ("opening", "closing", "listening"):
        assert s["capabilities"][what] == {"state": "paused",
                                           "reason_code": hold}
    assert s["signals"]["reading"] is False


def test_a_hold_does_not_hide_the_owners_own_pause():
    s = build(cfg={**CFG, "paused": True}, rt=runtime(hold="state_hold",
                                                      hold_recorded=False))
    assert s["pause"]["kind"] == "state_hold"
    assert s["pause"]["entries_paused"] is True
    assert s["pause"]["survives_restart"] is False  # the record wasn't written


@pytest.mark.parametrize("rt", [None, {"running": False},
                                runtime(stopping=True)])
def test_stopped_agent(rt):
    s = build(cfg={**CFG, "dry_run": False}, st=state(**{SPY: real_pos()}),
              rt=rt)
    assert s["agent"]["running"] is False and s["agent"]["answering"] is False
    assert s["pause"]["kind"] == "stopped" and s["pause"]["scope"] == "all"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "agent_stopped"
    assert s["capabilities"]["opening"] == {"state": "cant",
                                            "reason_code": "agent_stopped"}
    assert s["mode"] == "live"


def test_daily_cap_reached_counts_each_book_apart():
    rows = [entry(f"SPY 2026-11-13 ${700 + i}.0 C", at(10, i)) for i in range(4)]
    rows += [entry("QQQ 2026-11-13 $640.0 C", at(11), dry=True)]
    live = build(cfg={**CFG, "dry_run": False}, rows=rows)
    lim = live["limits"]
    assert lim["cap"] == 4 and lim["used_today"] == {"real": 4, "practice": 1}
    assert lim["cap_reached"] is True
    assert lim["resets_at"] == "2026-11-10T00:00:00Z"  # the cap counts by UTC day
    assert live["capabilities"]["opening"] == {
        "state": "not_today", "reason_code": "daily_cap_reached"}
    # the same day in practice mode: the practice book has room
    practice = build(rows=rows)
    assert practice["limits"]["cap_reached"] is False
    assert practice["capabilities"]["opening"]["state"] == "practice_only"


def test_daily_loss_limit_reached():
    c = "QQQ 2026-11-06 $628.0 C"
    rows = [entry(c, at(10)), exit_(c, at(12, 58), -316.0),
            exit_("SPY 2026-11-13 $1.0 C", at(13), 500.0, dry=True),
            {"action": "exit", "contract": c, "dry": False, "ts": at(13, 5)}]
    s = build(cfg={**CFG, "dry_run": False}, rows=rows)
    lim = s["limits"]
    assert lim["max_daily_loss"] == 300.0
    # a practice gain never offsets a real loss
    assert lim["realised_pnl_today"] == {"real": -316.0, "practice": 500.0}
    assert lim["realised_loss_today"] == {"real": 316.0, "practice": 0.0}
    assert lim["unmeasured_exits_today"] == {"real": 1, "practice": 0}
    assert lim["loss_limit_reached"] is True
    assert s["capabilities"]["opening"] == {
        "state": "not_today", "reason_code": "daily_loss_reached"}
    assert build(cfg={**CFG, "dry_run": False, "max_daily_loss_usd": 0},
                 rows=rows)["limits"]["max_daily_loss"] is None


def test_position_cap_reached_counts_real_and_practice_together():
    s = build(cfg={**CFG, "dry_run": False, "max_open_positions": 2},
              st=state(**{SPY: real_pos(), QQQ: PRACTICE_QQQ}))
    assert s["limits"]["max_positions"] == 2
    assert s["limits"]["positions_reached"] is True
    assert s["capabilities"]["opening"] == {
        "state": "cant", "reason_code": "position_cap_reached"}


def test_broker_disconnected():
    down = NOW.timestamp() - 1200
    rt = runtime(broker={**runtime()["broker"], "connected": False,
                         "down_since": down, "last_error": "login expired"})
    s = build(cfg={**CFG, "dry_run": False},
              st=state(**{SPY: real_pos(), QQQ: PRACTICE_QQQ}), rt=rt)
    b = s["broker"]
    assert b["connected"] is False and b["since"] == "2026-11-09T19:21:07Z"
    assert b["last_error"] == "login expired"
    assert b["last_answer"] == "2026-11-09T19:40:52Z"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "broker_down"
    by_id = {p["id"]: p for p in s["positions"]}
    assert by_id[SPY]["next"]["action"] == "unmanaged"
    assert by_id[QQQ]["next"]["action"] == "paper_close"  # needs no broker
    assert s["capabilities"]["opening"] == {"state": "cant",
                                            "reason_code": "broker_down"}
    assert s["capabilities"]["closing"] == {"state": "cant",
                                            "reason_code": "broker_down"}
    # in practice mode a new (pretend) trade needs no broker
    assert build(st=state(), rt=rt)["capabilities"]["opening"]["state"] \
        == "practice_only"


def test_broker_that_does_not_report_its_connection_is_unknown_not_ok():
    rt = runtime(broker={"id": "alpaca", "account_tag": "alpaca:paper",
                         "connected": None, "buying_power": None})
    s = build(cfg={**CFG, "broker": "alpaca"}, rt=rt)
    assert s["broker"]["connected"] is None and s["broker"]["name"] == "Alpaca"
    assert s["broker"]["paper_account"] is True
    assert s["broker"]["account_mask"] is None
    assert s["broker"]["buying_power"] is None
    assert s["broker"]["buying_power_at"] is None


@pytest.mark.parametrize("bp,reason,afford", [
    (31.5, "under_minimum", 0), (420.0, "under_trade_size", 0),
    (1700.0, None, 3)])
def test_low_cash(bp, reason, afford):
    rt = runtime(broker={**runtime()["broker"], "buying_power": bp})
    s = build(cfg={**CFG, "dry_run": False}, rt=rt)
    assert s["limits"]["cash"] == {"low": reason is not None,
                                   "low_reason": reason,
                                   "trades_affordable": afford}
    # the same conditions as the agent's own cash warning
    assert bool(agent._cash_warning(CFG, bp)) == (reason is not None)


def test_agent_not_answering_is_unknown_not_ok():
    rt = runtime(heartbeat_at=NOW.timestamp() - 1500, wait_s=30.0)
    s = build(cfg={**CFG, "dry_run": False}, st=state(**{SPY: real_pos()}),
              rt=rt)
    assert s["agent"]["running"] is True and s["agent"]["answering"] is False
    assert s["agent"]["last_heartbeat"] == "2026-11-09T19:16:07Z"
    assert s["mode"] == "live"  # its last report
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "agent_not_answering"
    assert s["positions"][0]["next"]["action"] == "unknown"
    for what in ("opening", "closing", "listening"):
        assert s["capabilities"][what] == {
            "state": "unknown", "reason_code": "agent_not_answering"}


def test_answering_follows_the_loops_own_schedule():
    """Outside market hours the loop sleeps 5 minutes: a 4-minute-old
    heartbeat is on time then, and late by the loop's own gap rule only
    after its sleep plus agent.EXPIRY_WAKE_GAP_S."""
    ok = runtime(heartbeat_at=NOW.timestamp() - 240, wait_s=300.0)
    assert build(rt=ok)["agent"]["answering"] is True
    late = runtime(heartbeat_at=NOW.timestamp() - 300 - agent.EXPIRY_WAKE_GAP_S - 1,
                   wait_s=300.0)
    assert build(rt=late)["agent"]["answering"] is False
    assert build(rt=runtime(heartbeat_at=None))["agent"]["answering"] is False


def test_empty_first_run():
    s = build(cfg={}, st={}, rows=[], rt=None)
    assert s["agent"] == {"running": False, "answering": False,
                          "last_heartbeat": None, "heartbeat_every_s": None,
                          "started_at": None, "settings_readable": True}
    assert s["positions"] == [] and s["real_positions"] == 0
    assert s["real_positions_managed"] is True
    assert s["activity"] == {"rows": [], "total": 0, "next": None,
                             "oldest": None, "day_lines": []}
    for book in ("real", "practice", "paper_account"):
        assert s["results"][book] == {
            "today": 0.0, "last_7_days": 0.0, "total": 0.0, "closed": 0,
            "won": 0, "lost": 0, "unmeasured": 0, "unmeasured_today": 0, "opened_today": 0,
            "days": []}
    assert s["expiry_guard_on"] is True
    assert s["signals"]["last_poll"] is None
    assert s["signals"]["last_signal"] is None
    assert s["signals"]["pass"] == {"kind": "none", "status": "none",
                                    "days_left": None, "auto_pay": False}
    assert s["broker"]["connected"] is None
    assert s["limits"]["cap"] == 5 and s["limits"]["max_positions"] == 10
    assert s["entry_checks"] == [c for c, _k in agent.ENTRY_CHECKS]


def test_settings_unreadable_is_reported():
    assert build(rt=runtime(settings_readable=False))["agent"][
        "settings_readable"] is False


# ── positions: what the agent will do, from the loop's own rules ────────────

def test_expiry_guard_times_come_from_the_agents_own_rule():
    st = state(**{IWM_TODAY: real_pos(mark=1.86)})
    cfg = {**CFG, "dry_run": False}
    guard, _alert, close = agent._expiry_times(cfg, TODAY)
    assert (guard, close) == (15 * 60 + 45, 16 * 60)
    before = build(cfg=cfg, st=st)["positions"][0]
    assert before["expires_today"] is True
    assert before["next"] == {"action": "sell_at_expiry_guard",
                              "at": "2026-11-09T20:45:00Z",
                              "reason_code": "expiry_guard",
                              "expiry_guard_at": "2026-11-09T20:45:00Z"}
    during = build(cfg=cfg, st=st, now=NOW.replace(hour=20, minute=46),
                   rt=runtime(heartbeat_at=NOW.replace(
                       hour=20, minute=46).timestamp()))["positions"][0]
    assert during["next"]["action"] == "selling_now"
    assert during["next"]["reason_code"] == "expiry_guard"
    after = build(cfg=cfg, st=st, now=NOW.replace(hour=21, minute=1),
                  rt=runtime(heartbeat_at=NOW.replace(
                      hour=21, minute=1).timestamp()))
    assert after["positions"][0]["next"]["action"] == "unmanaged"
    assert after["positions"][0]["next"]["reason_code"] == "expired"
    assert after["unmanaged_reason"] == "expired"


def test_expiry_guard_moves_with_the_owners_setting_and_can_be_off():
    st = state(**{IWM_TODAY: real_pos()})
    moved = build(cfg={**CFG, "expiry_guard_et": "15:30"}, st=st)
    assert moved["positions"][0]["next"]["at"] == "2026-11-09T20:30:00Z"
    off = build(cfg={**CFG, "expiry_guard_et": "off"}, st=st)["positions"][0]
    assert off["next"] == {"action": "sell_on_exit", "at": None,
                           "reason_code": "exit_signal",
                           "expiry_guard_at": None}


def test_expiry_guard_on_an_early_close_day():
    """The day after Thanksgiving closes at 1:00 PM ET: the guard moves
    three hours earlier with it (brokers/market_hours.py)."""
    key = "SPY|2026-11-27|700.0|C"
    s = build(st=state(**{key: real_pos()}))
    assert s["positions"][0]["next"]["expiry_guard_at"] == "2026-11-27T17:45:00Z"


def test_practice_position_expiring_today_is_closed_on_paper_at_the_guard():
    key = f"QQQ|{TODAY}|640.0|C"
    p = build(st=state(**{key: PRACTICE_QQQ}))["positions"][0]
    assert p["next"] == {"action": "paper_close", "at": "2026-11-09T20:45:00Z",
                         "reason_code": "expiry_guard",
                         "expiry_guard_at": "2026-11-09T20:45:00Z"}


def test_position_from_another_account_is_not_sold():
    s = build(cfg={**CFG, "dry_run": False},
              st=state(**{SPY: real_pos(acct="robinhood:other")}))
    assert s["positions"][0]["next"]["reason_code"] == "other_account"
    assert s["real_positions_managed"] is False
    assert s["unmanaged_reason"] == "other_account"


def test_working_sell_and_exit_remainder_read_as_selling_now():
    s = build(st=state(**{
        SPY: real_pos(pending={"side": "sell", "requested": 1}),
        "AAPL|2026-11-20|285.0|C": real_pos(qty=2, exiting={"at": 1.0})}))
    by = {p["id"]: p for p in s["positions"]}
    assert by[SPY]["pending"] == "sell"
    assert by[SPY]["next"]["reason_code"] == "sell_working"
    assert by["AAPL|2026-11-20|285.0|C"]["next"] == {
        "action": "selling_now", "at": None, "reason_code": "exit_remainder",
        "expiry_guard_at": "2026-11-20T20:45:00Z"}


def test_old_ledger_rows_count_as_real_like_the_agent_reads_them():
    """A row from before "live" was recorded: real unless it says dry."""
    s = build(st=state(**{SPY: {"option_id": "abc", "qty": 2}}))
    p = s["positions"][0]
    assert p["real"] is True and p["qty"] == 2
    assert p["entry_price"] is None and p["entry_cost_usd"] is None
    assert p["opened_at"] is None and p["book"] is None and p["account"] is None
    assert p["next"]["action"] == "sell_on_exit"


# ── market hours: the repo's own calendar ───────────────────────────────────

@pytest.mark.parametrize("now,open_,trading,opens,closes,early", [
    # Monday 2:41 PM ET: this session
    (NOW, True, True, "2026-11-09T14:30:00Z", "2026-11-09T21:00:00Z", False),
    # Monday 8:00 AM ET: today's session, not open yet
    (datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc), False, True,
     "2026-11-09T14:30:00Z", "2026-11-09T21:00:00Z", False),
    # Monday 5:00 PM ET: tomorrow's
    (datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc), False, True,
     "2026-11-10T14:30:00Z", "2026-11-10T21:00:00Z", False),
    # Saturday: Monday's
    (datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc), False, False,
     "2026-11-09T14:30:00Z", "2026-11-09T21:00:00Z", False),
    # Thanksgiving (closed): Friday's 1:00 PM early close
    (datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc), False, False,
     "2026-11-27T14:30:00Z", "2026-11-27T18:00:00Z", True),
    # that Friday at 1:30 PM ET: already closed, next is Monday
    (datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc), False, True,
     "2026-11-30T14:30:00Z", "2026-11-30T21:00:00Z", False),
    # summer time (EDT, UTC-4)
    (datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc), True, True,
     "2026-07-08T13:30:00Z", "2026-07-08T20:00:00Z", False),
])
def test_market_hours(now, open_, trading, opens, closes, early):
    m = build(now=now, rt=runtime(heartbeat_at=now.timestamp()))["market"]
    assert m == {"open": open_, "trading_day": trading, "opens_at": opens,
                 "closes_at": closes, "early_close": early,
                 "timezone": "America/New_York"}
    assert m["open"] == agent._trading_now(CFG, now.astimezone(agent.MARKET_TZ))


def test_market_hours_use_the_owners_extra_closures():
    cfg = {**CFG, "market_holidays": [TODAY],
           "early_close_days": ["2026-11-10"]}
    m = build(cfg=cfg)["market"]
    assert m["open"] is False and m["trading_day"] is False
    assert m["closes_at"] == "2026-11-10T18:00:00Z" and m["early_close"] is True
    assert build(cfg=cfg)["capabilities"]["opening"] == {
        "state": "not_today", "reason_code": "market_closed"}


def test_before_the_open_nothing_stops_an_entry():
    early = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
    s = build(cfg={**CFG, "dry_run": False}, now=early,
              rt=runtime(heartbeat_at=early.timestamp()))
    assert s["capabilities"]["opening"] == {"state": "yes", "reason_code": "ok"}


# ── limits: the same counts the loop's checks use ───────────────────────────

def test_limits_match_the_agents_own_counters(home, frozen_now):
    day = frozen_now.date().isoformat()
    yesterday = (frozen_now - timedelta(days=1)).date().isoformat()
    rows = [
        {"action": "entry", "contract": "A", "dry": False, "ts": f"{day}T14:00:00+00:00"},
        {"action": "entry", "contract": "B", "dry": True, "ts": f"{day}T14:01:00+00:00"},
        {"action": "entry", "contract": "C", "ts": f"{day}T14:02:00+00:00"},
        {"action": "entry", "contract": "D", "dry": False,
         "ts": f"{yesterday}T14:02:00+00:00"},
        {"action": "skip_daily_cap", "contract": "E", "ts": f"{day}T14:03:00+00:00"},
        {"action": "exit", "contract": "A", "dry": False, "est_pnl_usd": -120.5,
         "ts": f"{day}T14:30:00+00:00"},
        {"action": "exit", "contract": "B", "dry": True, "est_pnl_usd": 40,
         "ts": f"{day}T14:31:00+00:00"},
        {"action": "exit", "contract": "C", "dry": False, "ts": f"{day}T14:32:00+00:00"},
        # found in the account: counts on the day its sale filled
        {"action": "exit", "contract": "D", "dry": False, "est_pnl_usd": -10,
         "manual": True, "closed_at": f"{yesterday}T20:00:00+00:00",
         "ts": f"{day}T14:33:00+00:00"},
        {"action": "exit", "contract": "F", "dry": False, "est_pnl_usd": 7,
         "closed_at": f"{day}T13:00:00+00:00", "ts": f"{day}T14:34:00+00:00"},
        # a re-sold remainder an earlier row already counted: not unmeasured
        {"action": "exit", "contract": "G", "dry": False, "qty": 2,
         "counted_before": 2, "ts": f"{day}T14:35:00+00:00"},
    ]
    (home / "trades.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    lim = vm.build_status(CFG, state(), agent._read_trade_log(), runtime(),
                          now=frozen_now)["limits"]
    assert lim["used_today"] == {"real": agent._entries_today(False),
                                 "practice": agent._entries_today(True)}
    assert lim["used_today"] == {"real": 2, "practice": 1}
    real, real_unmeasured = agent._est_pnl_today(False)
    practice, practice_unmeasured = agent._est_pnl_today(True)
    assert lim["realised_pnl_today"] == {"real": round(real, 2),
                                         "practice": round(practice, 2)}
    assert lim["unmeasured_exits_today"] == {"real": real_unmeasured,
                                             "practice": practice_unmeasured}
    assert (real, real_unmeasured) == (-113.5, 1)


def test_trade_sizes():
    s = build()["limits"]["trade_size"]
    assert s["main"] == {"mode": "budget", "usd": 500.0, "contracts": None,
                         "percent": None, "max_contracts": None,
                         "same_as_main": False}
    assert s["other"]["usd"] == 150.0 and s["other"]["same_as_main"] is False
    s = build(cfg={"sizing_mode": "percent", "percent_per_trade": 2.5,
                   "max_contracts_per_trade": 3})["limits"]["trade_size"]
    assert s["main"] == {"mode": "percent", "usd": None, "contracts": None,
                         "percent": 2.5, "max_contracts": 3,
                         "same_as_main": False}
    assert s["other"] == {**s["main"], "same_as_main": True}
    s = build(cfg={"contracts_per_trade": 2})["limits"]["trade_size"]
    assert s["main"]["mode"] == "contracts" and s["main"]["contracts"] == 2


# ── activity ────────────────────────────────────────────────────────────────

def test_activity_rows_recorded_going_forward():
    rows = [
        entry("SPY 2026-11-13 $700.0 C", at(9, 49), mode="live", book="main",
              qty=2, mark=4.12, sent=True, filled=True),
        {"action": "skip_daily_cap", "book": "shadow", "event_id": "e2",
         "contract": "GOOGL 2026-11-13 $305.0 C", "ts": at(14, 10),
         "ui": {"mode": "live", "check": "daily_cap", "params": {"cap": 4}}},
        {"action": "skip_rule", "book": "main", "event_id": "e3",
         "contract": "TSLA 2026-11-13 $452.5 C", "ts": at(12, 15),
         "reason": "TSLA is on your skip list",
         "ui": {"mode": "practice", "check": "your_rules",
                "code": "rule_skip_list", "params": {"ticker": "TSLA"}}},
        exit_("SPY 2026-11-13 $700.0 C", at(15), 64.0, qty=2, cause="expiry_guard",
              ui={"mode": "practice", "book": "main", "sent": True,
                  "filled": True, "qty": 2, "mark": 4.44}),
    ]
    got = {r["n"]: r for r in build(rows=rows)["activity"]["rows"]}
    e = got[0]
    assert e["action"] == "entry" and e["known"] and e["reason_code"] == "entered"
    assert e["contract"] == {"symbol": "SPY", "expiry": "2026-11-13",
                             "strike": 700.0, "side": "C"}
    assert (e["mode_at_time"], e["real"], e["book"]) == ("live", True, "main")
    assert (e["order_sent"], e["filled"], e["qty"]) == (True, True, 2)
    assert e["quote_price"] == 4.12 and e["fill_price"] is None
    assert e["at"] == "2026-11-09T14:49:00Z" and e["stopped_at_check"] is None
    cap = got[1]
    assert cap["stopped_at_check"] == "daily_cap"
    assert cap["reason_code"] == "daily_cap_reached"
    assert cap["reason_params"] == {"cap": 4} and cap["book"] == "other"
    assert (cap["order_sent"], cap["filled"]) == (False, False)
    assert cap["mode_at_time"] == "live" and cap["real"] is True
    rule = got[2]
    assert rule["stopped_at_check"] == "your_rules"
    assert rule["reason_code"] == "rule_skip_list"
    assert rule["reason_params"] == {"ticker": "TSLA"}
    assert rule["reason_text"] == "TSLA is on your skip list"
    assert rule["mode_at_time"] == "practice" and rule["real"] is False
    x = got[3]
    # a real position sold while the agent was in practice mode
    assert (x["real"], x["mode_at_time"]) == (True, "practice")
    assert x["reason_code"] == "expiry_guard" and x["cause"] == "expiry_guard"
    assert x["est_pnl_usd"] == 64.0 and x["qty"] == 2 and x["filled"] is True


def test_old_rows_are_unknown_never_guessed():
    rows = [
        {"action": "entry", "event_id": "e1", "contract": "SPY 2026-11-13 $700.0 C",
         "dry": False, "reason": "ok", "ts": at(9, 49)},
        {"action": "exit", "event_id": "x1", "contract": "SPY 2026-11-13 $700.0 C",
         "dry": True, "reason": "exit", "ts": at(10), "est_pnl_usd": 12},
        {"action": "skip_rule", "book": "main", "event_id": "e2",
         "contract": "TSLA 2026-11-13 $452.5 C", "reason": "x", "ts": at(11)},
        {"action": "skip_daily_cap", "book": "main", "event_id": "e3",
         "contract": "TSLA 2026-11-13 $452.5 C", "ts": at(11, 1)},
        {"action": "order_failed", "contract": "AAPL 2026-11-13 $282.5 P",
         "reason": "insufficient buying power", "hint": "add cash",
         "ts": at(11, 30)},
        {"action": "skip_budget", "book": "shadow", "dry": True,
         "contract": "COST 2026-11-13 $1010.0 C", "ts": at(12, 42),
         "reason": "1 contract ≈ $1,240 exceeds your $500 budget"},
        {"action": "exit", "manual": True, "cause": "sold", "dry": False,
         "contract": "META 2026-11-13 $745.0 C", "qty": 1, "exit_price": 5.15,
         "est_pnl_usd": 55, "closed_at": at(15, 6, "2026-11-05"),
         "ts": at(15, 9, "2026-11-05"), "reason": "sold in the app"},
    ]
    got = {r["n"]: r for r in build(rows=rows)["activity"]["rows"]}
    # an entry was always made in the mode of its own book
    assert got[0]["mode_at_time"] == "live" and got[0]["order_sent"] is True
    assert got[0]["filled"] is None and got[0]["qty"] is None
    assert got[0]["book"] is None and got[0]["quote_price"] is None
    # a practice-book exit says nothing about the mode at the time
    assert got[1]["mode_at_time"] is None and got[1]["real"] is False
    assert got[1]["order_sent"] is False
    # two checks log skip_rule: which one is not known for an old row
    assert got[2]["stopped_at_check"] is None and got[2]["reason_code"] == "rule"
    assert got[2]["mode_at_time"] is None and got[2]["real"] is None
    # only one check logs skip_daily_cap
    assert got[3]["stopped_at_check"] == "daily_cap"
    assert got[3]["reason_params"] == {}
    # a rejected order may have been a sell: no entry check claimed
    assert got[4]["stopped_at_check"] is None
    assert got[4]["reason_code"] == "order_rejected" and got[4]["hint"] == "add cash"
    assert got[4]["order_sent"] is None and got[4]["filled"] is None
    assert got[5]["reason_params"] == {"cost_usd": 1240.0}
    assert got[5]["mode_at_time"] == "practice" and got[5]["book"] == "other"
    assert got[5]["stopped_at_check"] == "trade_size"
    sold = got[6]
    assert sold["reason_code"] == "closed_outside" and sold["fill_price"] == 5.15
    assert sold["at"] == "2026-11-05T20:06:00Z"  # when the sale filled
    assert sold["order_sent"] is False and sold["filled"] is True


@pytest.mark.parametrize("stamp,want", [
    ("2026-11-05T20:06:00.123456789Z", "2026-11-05T20:06:00Z"),  # a broker's
    ("2026-11-05T15:06:00-05:00", "2026-11-05T20:06:00Z"),
    ("2026-11-05 20:06:00", "2026-11-05T20:06:00Z"),             # no zone = UTC
    ("2026-11-05T20:06:00.5+00:00", "2026-11-05T20:06:00Z"),
    ("0001-01-01T00:00:00+00:00", None), ("9999-12-31T23:59:59-05:00", None),
    ("yesterday", None), ("", None), (None, None), (12345, None), ([], None),
])
def test_row_times_are_read_like_the_agent_reads_them(stamp, want):
    row = {"n": 0, "action": "exit", "closed_at": stamp}
    assert vm.activity_row(row)["at"] == want
    if want:
        assert agent._event_time(row)[0][:19] + "Z" == want


def test_activity_paging_matches_the_trade_log_paging(home):
    rows = [{"action": "skip_paused", "contract": f"SPY 2026-11-13 ${i}.0 C",
             "ts": at(10, i % 60)} for i in range(1, 8)]
    lines = [json.dumps(r) for r in rows]
    lines.insert(3, "{torn")  # a damaged line keeps its line number
    (home / "trades.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log = agent._read_trade_log()
    page = vm.activity_page(log, limit=3)
    old = agent.trade_page(limit=3)
    assert [r["n"] for r in page["rows"]] == [t["n"] for t in old["trades"]]
    assert (page["total"], page["next"], page["oldest"]) == (
        old["total"], old["next"], old["oldest"])
    nxt = vm.activity_page(log, before=page["next"], limit=3)
    old2 = agent.trade_page(before=old["next"], limit=3)
    assert [r["n"] for r in nxt["rows"]] == [t["n"] for t in old2["trades"]]
    assert vm.validate(nxt, "ActivityPage") == []
    last = vm.activity_page(log, before=nxt["next"], limit=3)
    assert last["next"] is None and len(last["rows"]) == 1
    # a hostile limit / cursor falls back, never raises
    assert len(vm.activity_page(log, before="x", limit="lots")["rows"]) == 7
    assert len(vm.activity_page(log, limit=10 ** 9)["rows"]) == 7
    assert len(vm.activity_page(log, limit=-5)["rows"]) == 1


def test_status_carries_the_newest_rows_only():
    rows = [{"action": "skip_paused", "contract": "SPY 2026-11-13 $1.0 C",
             "ts": at(10)} for _ in range(120)]
    a = build(rows=rows)["activity"]
    assert len(a["rows"]) == vm.ACTIVITY_LIMIT and a["total"] == 120
    assert a["rows"][0]["n"] == 119 and a["next"] == 70 and a["oldest"] == 0
    assert len(build(rows=rows, activity_limit=5)["activity"]["rows"]) == 5


def test_last_signal_falls_back_to_the_log_and_ignores_the_agents_own_events():
    rows = [
        {"action": "skip_paused", "event_id": "ENTERED|SPY|1", "ts": at(10),
         "contract": "SPY 2026-11-13 $1.0 C"},
        {"action": "exit", "event_id": f"expiry-guard|{SPY}|{TODAY}",
         "cause": "expiry_guard", "ts": at(15, 45), "dry": False,
         "contract": "SPY 2026-11-13 $700.0 C"},
        {"action": "ledger_adopt", "contract": "X", "ts": at(15, 50)},
    ]
    s = build(rows=rows, rt=runtime(signal_at=None))["signals"]
    assert s["last_signal"] == "2026-11-09T15:00:00Z"
    assert s["last_signal_from"] == "activity_log"


# ── the rows the agent really writes ────────────────────────────────────────

def test_rows_handle_event_logs_become_activity(home, no_policy, monkeypatch):
    from conftest import make_event
    from test_entry_checks import run
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    practice = {"dry_run": True, "contracts_per_trade": 3}
    _, st = run(make_event(track="other"), practice)
    run(make_event(event_id="t2", strike=700.0), {**practice, "paused": True})
    run(make_event(event_id="t3", strike=701.0),
        {**practice, "max_entries_per_day": 1})
    run(make_event(event_id="t4", ticker="TSLA"),
        {"dry_run": False, "rule_skip_tickers": ["TSLA"]})
    # the practice position's exit arrives after the owner went live
    run(make_event(event="EXITED", event_id="x1", track="other"),
        {"dry_run": False}, st)
    page = vm.activity_page(agent._read_trade_log())
    assert vm.validate(page, "ActivityPage") == []
    bought, paused, capped, ruled, sold = reversed(page["rows"])
    assert (bought["action"], bought["reason_code"]) == ("entry", "entered")
    assert (bought["mode_at_time"], bought["real"], bought["book"]) == (
        "practice", False, "other")
    assert (bought["order_sent"], bought["filled"], bought["qty"]) == (
        False, False, 3)
    assert bought["contract"] == {"symbol": "SPY", "expiry": "2026-07-10",
                                  "strike": 752.0, "side": "C"}
    assert (paused["action"], paused["stopped_at_check"],
            paused["reason_code"]) == ("skip_paused", "not_paused", "paused")
    assert (paused["order_sent"], paused["filled"], paused["real"]) == (
        False, False, False)
    assert (capped["stopped_at_check"], capped["reason_code"],
            capped["reason_params"]) == ("daily_cap", "daily_cap_reached",
                                         {"cap": 1})
    assert (ruled["stopped_at_check"], ruled["reason_code"],
            ruled["reason_params"], ruled["reason_text"]) == (
        "your_rules", "rule_skip_list", {"ticker": "TSLA"},
        "TSLA is on your skip list")
    assert (ruled["mode_at_time"], ruled["real"]) == ("live", True)
    # booked on paper, in the practice book, while the mode was live
    assert (sold["action"], sold["real"], sold["mode_at_time"],
            sold["order_sent"], sold["qty"]) == ("exit", False, "live", False, 3)
    assert sold["stopped_at_check"] is None


def test_broker_rows_from_an_entry_and_from_an_exit(home, no_policy, monkeypatch):
    import broker_events
    from conftest import make_event
    from test_entry_checks import run
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())

    class Rejects:
        @staticmethod
        def execute(client, cfg, ev, state):
            broker_events.report("entry_unfilled" if ev["event"] == "ENTERED"
                                 else "order_failed", "no", placed=True)
            return False
    run(make_event(), {"dry_run": False}, broker=Rejects())
    key = "SPY|2026-07-10|700.0|C"
    with pytest.raises(agent.ExitNotDone):
        run(make_event(event="EXITED", event_id="x1", strike=700.0),
            {"dry_run": False, "position_sync": False},
            {"positions": {key: {"option_id": "x", "qty": 1, "live": True}}},
            broker=Rejects())
    sell, buy = vm.activity_page(agent._read_trade_log())["rows"]
    assert (buy["action"], buy["stopped_at_check"], buy["order_sent"]) == (
        "entry_unfilled", "broker_order", True)
    # a rejected SELL stopped at no entry check
    assert (sell["action"], sell["stopped_at_check"], sell["order_sent"]) == (
        "order_failed", None, True)


def test_logs_written_by_older_versions_still_load(home):
    from test_entry_checks import write_log
    write_log(home, with_facts=False)
    log = agent._read_trade_log()
    assert [t["n"] for t in log] == [0, 1, 3, 4, 6, 7, 8, 9, 10]
    page = vm.activity_page(log)
    assert vm.validate(page, "ActivityPage") == []
    got = {r["n"]: r for r in page["rows"]}
    # what an old row says is shown; what it never said is null, not a guess
    assert got[0]["mode_at_time"] == "live" and got[0]["filled"] is None
    assert got[0]["qty"] is None and got[0]["quote_price"] is None
    assert got[3]["stopped_at_check"] == "daily_cap"
    assert got[3]["mode_at_time"] is None and got[3]["reason_params"] == {}
    assert got[6]["mode_at_time"] is None and got[6]["real"] is True
    assert got[8]["stopped_at_check"] is None
    assert got[9]["reason_code"] == "closed_outside" and got[9]["fill_price"] == 5.15
    assert got[10]["reason_code"] == "records_corrected"
    status = vm.build_status({"dry_run": False}, {}, log,
                             {"running": True})
    assert vm.validate(status) == []
    assert status["limits"]["used_today"] == {"real": 1, "practice": 1}
    assert status["results"]["real"]["total"] == -9.0
    assert status["results"]["practice"]["total"] == 12.5
    # the same log with the new key on its rows: the same counts and results
    write_log(home, with_facts=True)
    again = vm.build_status({"dry_run": False}, {}, agent._read_trade_log(),
                            {"running": True})
    assert again["limits"] == status["limits"]
    assert again["results"] == status["results"]
    assert again["activity"]["rows"][-1]["qty"] == 1  # ...and more is known


# ── results ─────────────────────────────────────────────────────────────────

def test_results_never_mix_real_and_practice():
    c = "SPY 2026-11-13 $700.0 C"
    rows = [
        exit_(c, at(11, 31), 64.0),
        exit_(c, at(12), -20.0, dry=True),
        exit_(c, at(14, 10, "2026-11-06"), -74.0),
        exit_(c, at(15, 45, "2026-11-03"), -316.0),          # 6 days back: in
        exit_(c, at(13, 26, "2026-11-02"), 188.0),           # 7 days back: out
        exit_(c, at(10, 0, "2026-11-02"), 0.0, dry=True),    # break-even
        {"action": "exit", "contract": c, "dry": False, "ts": at(9)},  # no estimate
        exit_(c, at(9, 5), float("nan")),
    ]
    r = build(rows=rows)["results"]
    assert r["real"] == {
        "today": 64.0, "last_7_days": -326.0, "total": -138.0, "closed": 4,
        "won": 2, "lost": 2, "unmeasured": 2, "unmeasured_today": 2, "opened_today": 0,
        "days": [{"day": "2026-11-09", "pnl": 64.0, "closed": 1},
                 {"day": "2026-11-06", "pnl": -74.0, "closed": 1},
                 {"day": "2026-11-03", "pnl": -316.0, "closed": 1},
                 {"day": "2026-11-02", "pnl": 188.0, "closed": 1}]}
    assert r["practice"] == {
        "today": -20.0, "last_7_days": -20.0, "total": -20.0, "closed": 2,
        "won": 0, "lost": 1, "unmeasured": 0, "unmeasured_today": 0, "opened_today": 0,
        "days": [{"day": "2026-11-09", "pnl": -20.0, "closed": 1},
                 {"day": "2026-11-02", "pnl": 0.0, "closed": 1}]}


def test_results_days_are_market_days_not_utc_days():
    # 7:30 PM ET on Friday is already Saturday in UTC
    r = build(rows=[exit_("X", "2026-11-07T00:30:00+00:00", 10.0)])["results"]
    assert r["real"]["days"] == [{"day": "2026-11-06", "pnl": 10.0, "closed": 1}]


def test_results_match_the_classic_pnl_totals(home):
    """Same log, same estimate: each book's total, count and win / loss
    split are what /api/pnl (agent.pnl_summary) reports."""
    c = "SPY 2026-11-13 $700.0 C"
    rows = [entry(c, at(10)), exit_(c, at(11), 64.0, qty=2, partial=True),
            exit_(c, at(11, 1), -20.5, qty=2), entry(c, at(12), dry=True),
            exit_(c, at(13), 7.25, dry=True),
            {"action": "exit", "contract": c, "dry": False, "ts": at(14)},
            exit_(c, at(14, 1), None, qty=2, counted_before=2)]
    (home / "trades.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    old = agent.pnl_summary({}, {})
    new = build(rows=rows)["results"]
    for book, was in (("real", old["live"]), ("practice", old["dry"])):
        assert (new[book]["total"], new[book]["closed"], new[book]["won"],
                new[book]["lost"], new[book]["unmeasured"]) == (
            was["total"], was["exits"], was["wins"], was["losses"],
            was["unmeasured"]), book
    assert new["real"]["total"] == 43.5 and new["real"]["unmeasured"] == 1


def test_signal_cost_counts_recorded_days_and_never_invents_a_total():
    cfg = {**CFG, "agenthc_api_key": "", "feed_pass": {"token": "tok"}}
    st = {**state(), "paid_days": ["2026-11-05", "2026-11-06", TODAY],
          "autopay_spent": {"day": TODAY, "sats": 14500, "count": 1}}
    rows = [entry("A", at(10, 0, "2026-11-03")), entry("B", at(10, 0, "2026-11-06"))]
    cost = build(cfg=cfg, st=st, rows=rows)["results"]["signal_cost"]
    assert cost == {"applies": True, "days_used": 3, "days_estimated": 1,
                    "sats_paid_today": 14500, "sats_paid": None,
                    "usd_paid": None}
    assert cost["days_used"] == len(agent._paid_days(st))
    stale = {**st, "autopay_spent": {"day": "2026-11-06", "sats": 9, "count": 1}}
    assert build(cfg=cfg, st=stale)["results"]["signal_cost"][
        "sats_paid_today"] == 0
    # a subscription (or another source) has no per-day charge
    assert build(st=st)["results"]["signal_cost"] == {
        "applies": False, "days_used": 0, "days_estimated": 0,
        "sats_paid_today": None, "sats_paid": None, "usd_paid": None}


def test_signal_pass_states():
    from sources import agenthc_day_trade_ideas as hc
    base = {**CFG, "agenthc_api_key": ""}
    tid = hc._token_id("tok")

    def p(cfg, st=None, **rt):
        return build(cfg=cfg, st={**state(), **(st or {})},
                     rt=runtime(**rt))
    prepaid = {**base, "feed_pass": {"token": "tok"}}
    s = p(prepaid, {"pass_status": {tid: {"status": "active", "days_left": 13}}})
    assert s["signals"]["pass"] == {"kind": "prepaid", "status": "active",
                                    "days_left": 13, "auto_pay": False}
    used = p(prepaid, {"pass_status": {tid: {"status": "exits_only"}}},
             feed_access=False)
    assert used["signals"]["pass"]["status"] == "exits_only"
    assert used["signals"]["entry_access"] is False
    assert used["capabilities"]["opening"] == {
        "state": "cant", "reason_code": "no_signal_access"}
    assert used["capabilities"]["listening"]["state"] == "yes"  # exits still come
    waiting = p(prepaid, {"pass_status": {tid: {"status": "used_up"}}},
                feed_access=False)
    assert waiting["signals"]["pass"]["status"] == "waiting"
    assert waiting["capabilities"]["listening"] == {
        "state": "cant", "reason_code": "no_signal_access"}
    # no record of the pass (its check failed): not known, never "waiting"
    unchecked = p(prepaid, feed_access=False)
    assert unchecked["signals"]["pass"]["status"] == "unknown"
    assert unchecked["capabilities"]["listening"] == {
        "state": "cant", "reason_code": "pass_unchecked"}
    wallet = {**base, "lnbits_url": "https://wallet.example", "day_pass_recurring": True}
    assert p(wallet, feed_access=None)["signals"]["pass"] == {
        "kind": "day_pass", "status": "waiting", "days_left": None,
        "auto_pay": True}
    # not read yet, auto-pay on: nothing says it can't
    assert p(wallet, feed_access=None)["capabilities"]["opening"]["state"] \
        == "practice_only"
    live = p(wallet, {"l402": {"token": "t", "expires_at": NOW.timestamp() + 3600}})
    assert live["signals"]["pass"]["status"] == "active"
    assert p(base, feed_access=None)["signals"]["pass"]["kind"] == "none"
    assert p({**CFG, "source": "manual"})["signals"]["pass"] == {
        "kind": "not_needed", "status": "active", "days_left": None,
        "auto_pay": False}
    assert p(base, poll_ok=False, feed_access=True)["capabilities"][
        "listening"] == {"state": "cant", "reason_code": "feed_unreachable"}


def test_ai_block_stops_opening():
    assert build(rt=runtime(ai_blocked=True))["capabilities"]["opening"] == {
        "state": "cant", "reason_code": "ai_needed"}


def test_notices_pass_through_with_a_level_and_a_code():
    s = build(rt=runtime(notices=[
        {"level": "urgent", "code": "expiry", "text": "IWM expires today"},
        {"level": "shouting", "text": "x" * 5000}, {"text": ""}, "junk", None]))
    assert s["notices"] == [
        {"level": "urgent", "code": "expiry", "text": "IWM expires today"},
        {"level": "info", "code": "note", "text": "x" * 300}]


# ── hostile data ────────────────────────────────────────────────────────────

LONG = "A" * 5000
HOSTILE_CFG = {
    "dry_run": "yes", "paused": 1, "source": LONG, "broker": ["x"],
    "max_entries_per_day": "many", "max_open_positions": None,
    "max_daily_loss_usd": "NaN", "sizing_mode": {"a": 1},
    "budget_per_trade_usd": float("inf"), "contracts_per_trade": -3,
    "percent_per_trade": "x", "max_contracts_per_trade": [],
    "other_sizing_mode": "percent", "other_percent_per_trade": None,
    "robinhood_account": {"n": 1}, "expiry_guard_et": "25:99",
    "early_close_days": "tomorrow", "market_holidays": 7,
    "feed_pass": "tok", "feed_pass_extra": [1, None, {"token": 5}],
    "agenthc_api_key": 0, "lnbits_url": 1, "max_event_age_s": "soon",
}
HOSTILE_STATE = {
    "positions": {
        LONG: {"qty": 1},
        f"{LONG}|2026-11-13|700.0|C": {"qty": "three", "entry_mark": "x"},
        "SPY|not-a-date|700.0|C": {"qty": 1, "opened_at": "yesterday"},
        "SPY|2026-11-13|nan|C": {"qty": float("nan")},
        "SPY|2026-11-13|700.0|X": {"qty": 10 ** 30, "entry_mark": float("inf")},
        "A|B": {"qty": -4, "pending": "sell", "acct": LONG},
        "": {"pending": {"side": LONG}},
        "SPY|2026-13-45|700.0|C": {"qty": 1, "live": True},
        "not a dict": "row", "list": [1, 2], "none": None,
        SPY: {"qty": 1, "dry": "maybe", "opened_at": -5, "exiting": "yes",
              "entry_mark": -1},
    },
    "paid_days": [1, None, "2026-11-09", LONG], "pass_status": "active",
    "pass_charged_day": 9, "l402": "token", "autopay_spent": [1],
}
HOSTILE_ROWS = [
    {"action": "launch_missiles", "contract": LONG, "reason": LONG,
     "hint": LONG, "cause": LONG, "book": LONG, "ts": "not a time"},
    {"action": LONG, "ts": "9999-12-31T23:59:59+00:00"},
    {"action": "entry", "contract": f"{LONG} 2026-11-13 $700.0 C", "dry": "no",
     "ts": at(10), "ui": "not a dict"},
    {"action": "exit", "est_pnl_usd": "lots", "qty": "all", "ts": None,
     "closed_at": 5, "ui": {"mode": "moon", "check": "nonsense",
                            "code": LONG, "sent": "yes", "filled": 1,
                            "qty": float("inf"), "mark": "x", "book": 3,
                            "params": {LONG: LONG, "n": float("nan"),
                                       "ok": 1, "deep": {"a": 1}}}},
    {"action": "exit", "est_pnl_usd": float("inf"), "dry": False,
     "ts": "0001-01-01T00:00:00+00:00", "exit_price": float("-inf")},
    {"action": "skip_budget", "reason": "1 contract ≈ $" + "9" * 400,
     "ts": at(11), "ui": {"params": ["not", "a", "dict"]}},
    {"action": None, "contract": None, "ts": at(11), "event_id": 7},
    {"action": "skip_rule", "contract": 12345, "reason": {"why": "x"},
     "ts": at(12), "ui": {"params": {str(i): i for i in range(100)}}},
    {},
]


def test_hostile_data_still_makes_a_valid_page():
    rows = numbered(HOSTILE_ROWS) + ["junk", None, 7, {"n": "x"}, {"n": True},
                                     {"action": "entry"}]
    rt = runtime(heartbeat_at="soon", wait_s=float("nan"), started_at=-1,
                 poll_at=LONG, poll_ok="yes", signal_at=float("inf"),
                 feed_access="maybe", hold="confused", hold_since=LONG,
                 broker={"id": LONG, "connected": "yes", "down_since": LONG,
                         "last_ok_at": float("nan"), "last_error": LONG,
                         "buying_power": float("inf"), "account_tag": 7},
                 notices="lots", ai_blocked="no")
    s = vm.build_status(HOSTILE_CFG, HOSTILE_STATE, rows, rt, now=NOW)
    assert vm.validate(s) == [], vm.validate(s)
    text = json.dumps(s, allow_nan=False)
    # nothing unbounded reaches the page
    assert LONG not in text and len(text) < 60_000

    def strings(v):
        if isinstance(v, dict):
            for k, x in v.items():
                yield k
                yield from strings(x)
        elif isinstance(v, list):
            for x in v:
                yield from strings(x)
        elif isinstance(v, str):
            yield v
    assert max(len(x) for x in strings(s)) <= 300
    acts = {r["n"]: r for r in s["activity"]["rows"]}
    assert len(acts) == len(HOSTILE_ROWS)
    unknown = acts[0]
    assert unknown["action"] == "launch_missiles" and unknown["known"] is False
    assert unknown["reason_code"] == "unknown" and unknown["contract"] is None
    assert unknown["at"] is None and unknown["stopped_at_check"] is None
    assert len(unknown["contract_text"]) == 80
    assert acts[2]["contract"] is None            # a 5,000-letter ticker
    # facts that aren't what they should be are dropped, one by one
    assert acts[3]["mode_at_time"] is None and acts[3]["filled"] is None
    assert acts[3]["book"] is None and acts[3]["quote_price"] is None
    assert acts[3]["est_pnl_usd"] is None and acts[3]["qty"] is None
    assert acts[3]["reason_params"] == {"A" * 40: "A" * 80, "ok": 1}
    assert acts[4]["est_pnl_usd"] is None and acts[4]["fill_price"] is None
    assert len(acts[7]["reason_params"]) == 12
    # the positions list keeps every ledger row that is a row
    assert len(s["positions"]) == 9
    by = {p["id"]: p for p in s["positions"]}
    assert by[LONG[:80]]["contract"] is None
    assert by[SPY]["entry_price"] is None and by[SPY]["opened_at"] is None
    assert s["limits"]["cap"] == 5 and s["limits"]["max_positions"] == 10
    assert s["limits"]["max_daily_loss"] is None
    assert s["broker"]["connected"] is None and s["broker"]["buying_power"] is None
    assert s["pause"]["kind"] == "entries"  # the engine reads any truthy "paused"
    assert s["mode"] == "practice"          # ...and any truthy dry_run


@pytest.mark.parametrize("cfg,st,rows,rt", [
    (None, None, None, None), ([], "x", 5, "running"), ({}, {"positions": []}, [], {}),
    ({"dry_run": None}, {"positions": {"a": {}}}, [None], {"broker": "x"}),
])
def test_wrong_typed_inputs_never_raise(cfg, st, rows, rt):
    s = vm.build_status(cfg, st, rows, rt, now=NOW)
    assert vm.validate(s) == []


def test_now_defaults_to_the_clock_and_accepts_naive_or_text():
    assert vm.validate(vm.build_status({}, {}, [])) == []
    assert vm.build_status({}, {}, [], now="2026-11-09T19:41:07")["as_of"] \
        == "2026-11-09T19:41:07Z"


# ── purity ──────────────────────────────────────────────────────────────────

def test_builder_does_no_io_and_does_not_change_what_it_is_handed(monkeypatch):
    cfg = {**CFG, "agenthc_api_key": "", "feed_pass": {"token": "tok"}}
    st = state(**{SPY: real_pos(), QQQ: dict(PRACTICE_QQQ)})
    rows = numbered([entry("QQQ 2026-11-13 $640.0 C", at(9, 41), dry=True),
                     exit_("SPY 2026-11-13 $700.0 C", at(11), 5.0)])
    rt = runtime(notices=[{"level": "warn", "code": "x", "text": "y"}])
    vm.build_status(cfg, st, rows, rt, now=NOW)  # every lazy import done
    before = copy.deepcopy((cfg, st, rows, rt))

    def no_io(*a, **k):
        raise AssertionError("the view-model builder must not do I/O")
    import builtins
    import socket
    import time
    monkeypatch.setattr(builtins, "open", no_io)
    monkeypatch.setattr(os, "stat", no_io)
    monkeypatch.setattr(os, "listdir", no_io)
    monkeypatch.setattr(socket, "socket", no_io)
    monkeypatch.setattr(time, "time", no_io)
    monkeypatch.setattr(agent, "_load", no_io)
    monkeypatch.setattr(agent, "_log_trade", no_io)
    monkeypatch.setattr(agent, "_save", no_io)
    out = vm.build_status(cfg, st, rows, rt, now=NOW)
    monkeypatch.undo()
    assert vm.validate(out) == []
    assert (cfg, st, rows, rt) == before
    # the same inputs, the same answer
    assert out == vm.build_status(cfg, st, rows, rt, now=NOW)


# ── the schema: one source for Python and TypeScript ────────────────────────

def test_typescript_file_is_generated_from_the_schema():
    path = os.path.join(ROOT, *vm.TS_PATH.split("/"))
    with open(path, encoding="utf-8") as f:
        committed = f.read()
    assert committed.replace("\r\n", "\n") == vm.typescript(), (
        "webui_app/src/lib/viewModel.ts is stale: run `python view_model.py --ts`")


def test_typescript_interfaces_have_exactly_the_schemas_keys():
    """Read the .ts file back on its own terms: every interface, its field
    names and their order are the Python declaration's."""
    with open(os.path.join(ROOT, *vm.TS_PATH.split("/")), encoding="utf-8") as f:
        src = f.read()
    found = {}
    for name, body in re.findall(r"export interface (\w+) \{\n(.*?)\n\}\n",
                                 src, re.S):
        found[name] = re.findall(r"^  (\w+): ", body, re.M)
    assert list(found) == list(vm.types())
    for name, fields in vm.types().items():
        assert found[name] == list(fields), name
    assert f"export const SCHEMA_VERSION = {vm.SCHEMA_VERSION};" in src
    # the ordered checks and the reason codes are in the file as data
    listed = re.findall(r'\{ id: "(\w+)", kinds: \[', src)
    assert listed == [c for c, _k in agent.ENTRY_CHECKS]
    codes = re.search(r"export const REASON_CODES = \[(.*?)\] as const", src, re.S)
    assert re.findall(r'"(\w+)"', codes.group(1)) == list(vm.REASON_CODES)


def test_every_documented_field_exists():
    for key in vm._DOC:
        name, field = key.split(".")
        assert field in vm.types()[name], key


def test_validate_catches_drift():
    good = build()
    assert vm.validate(good) == []
    extra = copy.deepcopy(good)
    extra["broker"]["secret"] = "x"
    assert vm.validate(extra) == ["$.broker.secret: not in the schema"]
    missing = copy.deepcopy(good)
    del missing["limits"]["cap"]
    assert vm.validate(missing) == ["$.limits.cap: missing"]
    wrong = copy.deepcopy(good)
    wrong["mode"] = "LIVE"
    wrong["as_of"] = "today"
    wrong["limits"]["cap"] = True
    wrong["market"]["open"] = 1
    wrong["agent"]["heartbeat_every_s"] = math.inf
    assert len(vm.validate(wrong)) == 5


def test_reason_codes_cover_the_rule_codes_and_every_logged_kind():
    import broker_events
    import rules
    assert set(rules.RULE_CODES) <= set(vm.REASON_CODES)
    kinds = {k for _c, logged in agent.ENTRY_CHECKS for k in logged}
    assert kinds | set(broker_events.KINDS) <= set(vm._KIND_CODES)
    for check, logged in agent.ENTRY_CHECKS:
        assert check in vm.check_ids()
    assert set(vm._ENTRY_ONLY_KINDS) <= kinds
