"""Audit PR7: a contract that expires without an exit is booked as an exit
(a total loss when its entry quote is known), and the after-close daily
digest counts the ET trading day from the whole log."""

import json
from datetime import datetime, timezone

import pytest

import agent
import rules
import view_model

EXP = "2026-11-13"                       # a Friday
KEY = f"SPY|{EXP}|700.0|C"
CONTRACT = agent._contract_of(KEY)
EXP_CLOSE_UTC = "2026-11-13T21:00:00+00:00"   # 16:00 EST


def _freeze(monkeypatch, when):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when.replace(tzinfo=None)
    monkeypatch.setattr(agent, "datetime", Frozen)


@pytest.fixture
def morning_after(home, monkeypatch):
    """Monday 09:00 ET after the Friday expiry; notifications captured."""
    _freeze(monkeypatch, datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc))
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, m, **k: sent.append(m))
    return sent


def _rows():
    return agent._read_trade_log()


def _exits():
    return [t for t in _rows() if t.get("action") == "exit"]


def _entry(dry=False):
    agent._log_trade({"action": "entry", "event_id": "e1", "contract": CONTRACT,
                      "dry": dry, "qty": 5})


# ── worthless expiry booked ─────────────────────────────────────────────────

def test_expired_live_position_is_booked_as_a_total_loss(morning_after):
    _entry()
    state = {"positions": {KEY: {"option_id": "o", "qty": 5, "entry_mark": 2.0,
                                 "live": True, "opened_event": "e1",
                                 "acct": "robinhood:abc", "expiry_no_bid": EXP}}}
    assert agent._prune_expired_positions({}, state, lambda s: None) == [KEY]
    assert state["positions"] == {}
    [x] = _exits()
    assert x["cause"] == "expired" and x["dry"] is False and x["qty"] == 5
    assert x["est_pnl_usd"] == -1000.0
    assert x["closed_at"] == EXP_CLOSE_UTC            # dated at the expiry close
    assert x["event_id"] == f"expired|{KEY}|e1"
    assert x[agent.ROW_FACTS_KEY]["code"] == "expired_unsold"
    assert x[agent.ROW_FACTS_KEY]["sent"] is False    # no order was placed

    s = agent.pnl_summary({}, {}, None)["live"]
    assert (s["total"], s["exits"], s["losses"], s["unmeasured"]) == (-1000.0, 1, 1, 0)
    assert s["days"][0]["day"] == "2026-11-16"        # opened (entry) day
    res = view_model._results(_rows(), datetime(2026, 11, 16, 15, tzinfo=timezone.utc))
    assert (res["real"]["total"], res["real"]["closed"], res["real"]["lost"]) == (-1000.0, 1, 1)
    assert res["real"]["days"][0]["day"] == EXP       # Results: the day it was lost
    assert res["practice"]["closed"] == 0
    assert any("counted as a loss of $1,000.00" in m for m in morning_after)


def test_expired_row_does_not_block_the_next_days_entries(morning_after):
    """The loss belongs to the expiry day: today's daily-loss total and the
    losing-streak rule never see it."""
    _entry()
    state = {"positions": {KEY: {"option_id": "o", "qty": 5, "entry_mark": 2.0}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    assert _exits()  # booked …
    assert agent._est_pnl_today(False) == (0.0, 0)    # … but not today
    assert rules.loss_streak(agent._trades_today()) == 0
    cfg = {"rule_loss_streak": 1}
    ok, _why = rules.check(cfg, {"event": "ENTERED", "ticker": "SPY", "type": "C"},
                           agent._trades_today())
    assert ok


def test_expired_without_entry_quote_counts_as_unmeasured(morning_after):
    _entry()
    state = {"positions": {KEY: {"option_id": "o", "qty": 2}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    [x] = _exits()
    assert "est_pnl_usd" not in x and x["qty"] == 2
    s = agent.pnl_summary({}, {}, None)["live"]
    assert (s["total"], s["exits"], s["unmeasured"]) == (0.0, 0, 1)


def test_expired_practice_row_goes_to_the_practice_book(morning_after):
    _entry(dry=True)
    state = {"positions": {KEY: {"option_id": "dry", "qty": 3, "dry": True,
                                 "entry_mark": 1.5}}}
    # the agent is in live mode now: the row's own book still decides
    agent._prune_expired_positions({"dry_run": False}, state, lambda s: None)
    [x] = _exits()
    assert x["dry"] is True and x["est_pnl_usd"] == -450.0
    s = agent.pnl_summary({}, {}, None)
    assert s["dry"]["total"] == -450.0 and s["live"]["exits"] == 0


def test_contracts_an_exit_row_already_counted_are_not_booked_again(morning_after):
    state = {"positions": {
        KEY: {"option_id": "o", "qty": 5, "exit_logged": 2, "entry_mark": 1.0},
        "QQQ|2026-11-13|600.0|P": {"option_id": "o2", "qty": 3, "exit_logged": 3,
                                   "entry_mark": 1.0},
        "IWM|2026-11-13|200.0|C": {"option_id": "o3", "qty": 0}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    [x] = _exits()
    assert x["contract"] == CONTRACT and x["qty"] == 3 and x["est_pnl_usd"] == -300.0


def _sale_that_died():
    """The exit row a working sell logged on Friday: 2x at +$50 each (sold
    at 2.00 over a 1.50 entry, an estimate). Its order then died unfilled."""
    agent._log_trade({"action": "exit", "event_id": "x1", "contract": CONTRACT,
                      "dry": False, "qty": 2, "est_pnl_usd": 100.0,
                      "closed_at": "2026-11-13T19:00:00+00:00"})


def test_contracts_counted_by_a_sell_that_died_are_rebooked_once(morning_after):
    """2x counted by a sell row at +$50 each whose order died unfilled, plus
    1x never sold: all three expired. The prune books -$150 for the unsold
    one and -$200 a piece for the two (total loss less the +$50 counted);
    a second prune never books them again."""
    _entry()
    _sale_that_died()
    row = {"option_id": "o", "qty": 3, "exit_logged": 2, "exit_logged_pc": 50.0,
           "exit_logged_by": "x1", "entry_mark": 1.5, "opened_event": "e1"}
    state = {"positions": {KEY: dict(row)}}
    agent._prune_expired_positions({}, state, lambda s: None)
    x = _exits()[-1]
    assert x["qty"] == 3 and x["est_pnl_usd"] == -550.0
    assert x["corrects"] == "x1" and "never filled" in x["reason"]
    agent._prune_expired_positions({}, {"positions": {KEY: dict(row)}}, lambda s: None)
    assert len(_exits()) == 2
    # a sell still pending at the prune may have filled: only the unsold 1x
    morning = {"positions": {"QQQ|2026-11-13|600.0|P": {
        **row, "opened_event": "e2",
        "pending": {"order_id": "o-1", "side": "sell", "requested": 2}}}}
    agent._prune_expired_positions({}, morning, lambda s: None)
    last = _exits()[-1]
    assert (last["qty"], last["est_pnl_usd"]) == (1, -150.0) and "corrects" not in last
    # no exit row named (an older version's row): nothing re-booked for it
    agent._prune_expired_positions({}, {"positions": {"IWM|2026-11-13|200.0|C": {
        **{k: v for k, v in row.items() if k != "exit_logged_by"}, "qty": 2}}},
        lambda s: None)
    assert len(_exits()) == 3


def test_a_dead_sale_that_expired_is_one_loss_in_every_count(morning_after,
                                                             monkeypatch):
    """The sale was booked as a win (+$100), its order died, the 2x expired:
    one position, one LOSS of -$300 (the $1.50 entry x 2 x 100) — in the
    dashboard's all-time P&L, Results (/v2), the losing-streak rule, the AI
    policy check's counts and the daily digest. Never a win plus a loss."""
    _entry()
    _sale_that_died()
    state = {"positions": {KEY: {"option_id": "o", "qty": 2, "exit_logged": 2,
                                 "exit_logged_pc": 50.0, "exit_logged_by": "x1",
                                 "entry_mark": 1.5, "opened_event": "e1"}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    assert [x["est_pnl_usd"] for x in _exits()] == [100.0, -400.0]   # the log
    s = agent.pnl_summary({}, {}, None)["live"]
    assert (s["total"], s["exits"], s["wins"], s["losses"]) == (-300.0, 1, 0, 1)
    assert sum(d["exits"] for d in s["days"]) == 1
    res = view_model._results(_rows(), datetime(2026, 11, 16, 15, tzinfo=timezone.utc))
    real = res["real"]
    assert (real["total"], real["closed"], real["won"], real["lost"]) == (-300.0, 1, 0, 1)
    assert [(d["day"], d["pnl"], d["closed"]) for d in real["days"]] == [
        (EXP, -300.0, 1)]
    assert rules.exit_results(_rows()) == [-300.0]
    assert rules.loss_streak(_rows()) == 1
    # a window of the log without the sale's row: P&L only, never a result
    corr = [t for t in _rows() if t.get("corrects")]
    assert rules.exit_results(corr) == []
    s = agent.pnl_summary({}, {}, corr)["live"]
    assert (s["total"], s["exits"], s["wins"], s["losses"]) == (-400.0, 0, 0, 0)
    res = view_model._results(corr, datetime(2026, 11, 16, 15, tzinfo=timezone.utc))
    assert (res["real"]["total"], res["real"]["closed"], res["real"]["lost"]) == (-400.0, 0, 0)
    # Monday's digest: the correction is not an exit of its own
    sent, _ = _digest(monkeypatch, datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc))
    assert "0 exits" in sent[-1]


def test_closed_outside_records_are_not_booked(morning_after):
    state = {"positions": {}, "closed_outside": {KEY: {"at": "2026-11-12T15:00:00+00:00",
                                                      "qty": 5, "cause": "sold"}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    assert state["closed_outside"] == {}
    assert _exits() == []


def test_booked_once_even_if_the_drop_was_not_saved(morning_after):
    """A crash after the row was logged but before state.json kept the drop:
    the next prune finds the row again and must not count the loss twice."""
    row = {"option_id": "o", "qty": 5, "entry_mark": 2.0, "opened_event": "e1"}
    agent._prune_expired_positions({}, {"positions": {KEY: dict(row)}}, lambda s: None)
    agent._prune_expired_positions({}, {"positions": {KEY: dict(row)}}, lambda s: None)
    assert len(_exits()) == 1


def test_a_failed_save_still_books_the_loss_then_raises(morning_after):
    state = {"positions": {KEY: {"option_id": "o", "qty": 1, "entry_mark": 2.0}}}

    def broken(_s):
        raise OSError("disk full")
    with pytest.raises(OSError):
        agent._prune_expired_positions({}, state, broken)
    assert len(_exits()) == 1 and state["positions"] == {}


def test_activity_names_it_expired_not_an_exit_signal(morning_after):
    state = {"positions": {KEY: {"option_id": "o", "qty": 5, "entry_mark": 2.0,
                                 "acct": "alpaca:paper"}}}
    agent._prune_expired_positions({}, state, lambda s: None)
    [x] = _exits()
    a = view_model.activity_row(x)
    assert a["reason_code"] == "expired_unsold"
    assert a["order_sent"] is False and a["at"].startswith("2026-11-13T21:00")
    assert a["est_pnl_usd"] == -1000.0
    assert a["paper_account"] is True     # a paper account is not real money
    assert "expired_unsold" in view_model.REASON_CODES


# ── daily digest: the ET day, from the whole log ─────────────────────────────

def _write(rows):
    with open(agent.TRADES_PATH, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _digest(monkeypatch, when):
    _freeze(monkeypatch, when)
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, m, **k: sent.append(m))
    state = {"positions": {}}
    agent._maybe_daily_digest({}, state, lambda s: None)
    return sent, state


def test_digest_sent_after_utc_midnight_counts_the_et_day(home, monkeypatch):
    _write([
        {"ts": "2026-11-17T03:00:00+00:00", "action": "entry"},  # Mon 22:00 ET
        {"ts": "2026-11-17T15:01:00+00:00", "action": "entry"},
        {"ts": "2026-11-17T17:40:00+00:00", "action": "exit"},
        {"ts": "2026-11-17T18:10:00+00:00", "action": "entry"},
        {"ts": "2026-11-17T20:30:00+00:00", "action": "exit"},
        {"ts": "2026-11-17T21:10:00+00:00", "action": "skip_daily_cap"},
        {"ts": "2026-11-18T00:10:00+00:00", "action": "policy_veto"},  # Tue 19:10 ET
    ])
    # Tue 19:30 EST = Wed 00:30 UTC: the first loop after the PC woke up
    sent, state = _digest(monkeypatch, datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc))
    assert len(sent) == 1
    assert "daily digest 2026-11-17: 2 entries, 2 exits, 1 policy vetoes, 1 skipped" in sent[0]
    assert state["last_digest_day"] == "2026-11-17"


def test_digest_counts_a_busy_day_past_the_last_300_rows(home, monkeypatch):
    _write([{"ts": "2026-11-17T15:00:00+00:00", "action": "entry"},
            {"ts": "2026-11-17T15:05:00+00:00", "action": "entry"}]
           + [{"ts": "2026-11-17T16:00:00+00:00", "action": "skip_daily_cap"}] * 350
           + [{"ts": "2026-11-17T20:00:00+00:00", "action": "exit"}])
    sent, _ = _digest(monkeypatch, datetime(2026, 11, 17, 21, 30, tzinfo=timezone.utc))
    assert "2 entries, 1 exits, 0 policy vetoes, 350 skipped" in sent[0]


def test_digest_dates_a_close_found_in_the_account_by_its_fill(home, monkeypatch):
    _write([{"ts": "2026-11-18T14:00:00+00:00", "action": "exit", "manual": True,
             "closed_at": "2026-11-17T19:00:00+00:00"},
            {"ts": "2026-11-18T14:00:05+00:00", "action": "exit", "manual": True,
             "closed_at": "2026-11-17T19:30:00+00:00"},
            {"ts": "2026-11-17T15:00:00+00:00", "action": "exit",
             "closed_at": "2026-11-16T20:00:00+00:00"}])
    sent, _ = _digest(monkeypatch, datetime(2026, 11, 17, 21, 30, tzinfo=timezone.utc))
    assert "0 entries, 2 exits" in sent[0]


def test_digest_counts_an_expired_exit_on_the_day_it_was_booked(home, monkeypatch):
    # Friday's digest (16:20 ET) went out before the expiry could be pruned;
    # Monday's prune writes the row dated at Friday's close. Monday's digest
    # counts it (by ts), else no digest ever would.
    _write([{"ts": "2026-11-16T14:00:00+00:00", "action": "exit",
             "cause": "expired", "closed_at": EXP_CLOSE_UTC}])
    sent, _ = _digest(monkeypatch, datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc))
    assert "0 entries, 1 exits" in sent[0]
