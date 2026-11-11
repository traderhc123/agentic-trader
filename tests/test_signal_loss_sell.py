"""No-signal sell: when no exit signal can reach the agent (a pass that ran
out, a refused day-pass, a feed that can't be read) and it still holds a
position, it warns the owner and, from signal_loss_sell_et (default 15:30
ET), sells what it holds through its normal sell path, so nothing is carried
past the close with nobody watching. Zero network."""

import json
from datetime import datetime

import pytest

import agent
from conftest import StubBroker

TZ = agent.MARKET_TZ
DAY = "2026-11-06"                       # a Friday
KEY = "RIVN|2026-11-13|13.5|C"           # expires next week
TODAY_KEY = f"SPY|{DAY}|600.0|P"         # expires today


def et(h, m, day=(2026, 11, 6), s=0):
    return datetime(*day, h, m, s, tzinfo=TZ)


class Broker(StubBroker):
    def __init__(self, bid=1.0, fail=0):
        super().__init__()
        self.bid, self.fail = bid, fail

    def exit_bid(self, client, cfg, event, option_id):
        return self.bid

    def execute(self, client, cfg, event, state):
        if self.fail:
            self.fail -= 1
            self.calls.append(event)
            return False
        return super().execute(client, cfg, event, state)


@pytest.fixture(autouse=True)
def sent(monkeypatch, home):
    out = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, msg, priority=None: out.append((msg, priority)))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 30))
    return out


def _state(*keys, since=None, cause="pass_ended", **row):
    base = {"option_id": "opt-1", "qty": 2, "live": True}
    base.update(row)
    st = {"positions": {k: dict(base) for k in keys}}
    if since is not None:
        st["signal_loss"] = {"since": since.timestamp(), "cause": cause}
    return st


def _tick(state, broker, now, cfg=None, paused=False):
    return agent.signal_loss_tick(cfg or {}, state, lambda s: None, broker,
                                  object(), paused=paused, now_et=now)


def _note(state, status, now, cause="", cfg=None):
    agent._note_exit_source(cfg or {}, state, lambda s: None, status, cause,
                            now_et=now)


def _log():
    try:
        with open(agent.TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def _warnings(sent):
    return [m for m, _p in sent if "no exit signals have reached" in m]


# ── when it sells ───────────────────────────────────────────────────────────

def test_sells_a_real_position_at_the_sell_time_after_a_warning(sent):
    state = _state(KEY, since=et(13, 0))
    b = Broker()
    assert _tick(state, b, et(13, 10)) == []
    assert b.calls == []
    (w,) = _warnings(sent)
    assert "since 13:00 ET (your signal pass has run out)" in w
    assert "RIVN 2026-11-13 $13.5 C" in w and "by 15:30 ET today" in w
    assert "buy signal days" in w
    assert [p for m, p in sent if m == w] == ["high"]
    assert _tick(state, b, et(15, 29)) == [] and b.calls == []
    out = _tick(state, b, et(15, 30))
    assert out and len(b.calls) == 1
    ev = b.calls[0]
    assert ev["event"] == "EXITED" and ev["exit_cause"] == "signal_loss"
    assert ev["exit_limit"] == "marketable"
    assert ev["event_id"] == f"no-signal|{KEY}|{DAY}"
    assert KEY not in state["positions"]
    (row,) = [r for r in _log() if r.get("action") == "exit"]
    assert row["cause"] == "signal_loss"
    (s,) = [(m, p) for m, p in sent if m.startswith("agentic-trader: sold ")]
    assert s[1] == "high" and "since 13:00 ET" in s[0]
    assert "opens nothing new today" in s[0]
    assert state["signal_loss_sold_day"] == DAY


def test_no_sale_without_fifteen_minutes_notice(sent):
    """The loss began in the morning but no warning went out (the agent
    was just updated): the first tick at 15:31 warns; the sale waits until
    15 minutes after it."""
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    assert _tick(state, b, et(15, 31)) == [] and b.calls == []
    (w,) = _warnings(sent)
    assert "by 15:46 ET today" in w
    assert _tick(state, b, et(15, 45, s=59)) == [] and b.calls == []
    assert _tick(state, b, et(15, 47)) and len(b.calls) == 1


def test_short_outage_sells_nothing(sent):
    state = _state(KEY)
    b = Broker()
    _note(state, "lost", et(10, 0), "feed_error")
    assert _tick(state, b, et(10, 3)) == [] and _warnings(sent) == []
    _note(state, "ok", et(10, 4))
    assert "signal_loss" not in state
    assert _tick(state, b, et(15, 30)) == [] and b.calls == [] and sent == []
    # a longer one warns; signals coming back clear it, and say so once
    _note(state, "lost", et(11, 0), "feed_error")
    _tick(state, b, et(11, 6))
    (w,) = _warnings(sent)
    assert "The agent keeps trying." in w and "buy signal days" not in w
    _note(state, "ok", et(11, 10))
    _note(state, "lost", et(11, 11), "feed_error")
    _note(state, "ok", et(11, 12))
    back = [m for m, _p in sent if "reaching your agent again" in m]
    assert back == ["agentic-trader: exit signals are reaching your agent "
                    "again. Nothing was sold."]
    assert _tick(state, b, et(15, 30)) == [] and b.calls == []


@pytest.mark.parametrize("cause, cfg, says, never", [
    # Recurring day-pass OFF: sats change nothing, the switch (or a pass) does
    ("day_pass_off", {"lnbits_url": "https://w"},
     "Recurring day-pass is off, so the agent has no day-pass for today",
     "switch Recurring day-pass on, or buy signal days"),
    ("day_pass_off", {}, "Recurring day-pass is off",
     "buy signal days (dashboard → Settings → Signal source)"),
    ("day_pass_short", {}, "the agent's wallet does not hold enough sats",
     "add sats to the agent's wallet"),
    ("day_pass_failed", {}, "the agent's wallet could not pay for today's",
     "check the agent's wallet"),
    ("day_pass_undecided", {}, "has not said whether today's day-pass payment",
     "The agent keeps trying."),
    ("no_day_pass", {}, "the agent has no day-pass for today",
     "buy signal days (dashboard → Settings → Signal source)"),
])
def test_the_warning_names_the_fix_for_the_cause(sent, cause, cfg, says, never):
    state = _state(KEY, since=et(10, 0), cause=cause)
    _tick(state, Broker(), et(10, 10), cfg=cfg)
    (w,) = _warnings(sent)
    assert says in w and never in w
    if cause != "day_pass_short":
        assert "add sats" not in w
    if cause == "day_pass_off" and not cfg:
        assert "Recurring day-pass on" not in w      # no wallet to switch on


def test_off_never_sells_and_says_so(sent):
    cfg = {"signal_loss_sell_et": "off"}
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 10), cfg=cfg)
    (w,) = _warnings(sent)
    assert "will not sell it on its own" in w and "switched off" in w
    assert "sell it yourself at" in w
    for now in (et(15, 30), et(15, 59)):
        assert _tick(state, b, now, cfg=cfg) == []
    assert b.calls == [] and KEY in state["positions"]
    assert not agent.signal_loss_busy(cfg, state, et(15, 45))


def test_custom_time():
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    cfg = {"signal_loss_sell_et": "14:00"}
    _tick(state, b, et(10, 10), cfg=cfg)
    assert _tick(state, b, et(13, 59), cfg=cfg) == []
    assert _tick(state, b, et(14, 0), cfg=cfg) and len(b.calls) == 1


def test_not_while_held(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 10))
    assert _tick(state, b, et(15, 40), paused=True) == []
    assert b.calls == []


def test_owner_pause_still_sells(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    cfg = {"paused": True}
    _tick(state, b, et(10, 10), cfg=cfg)
    assert _tick(state, b, et(15, 30), cfg=cfg) and len(b.calls) == 1


def test_dry_run_that_holds_real_sells_does_not_sell(sent, monkeypatch):
    monkeypatch.setattr(agent, "_dry_run_keeps_selling_real_positions",
                        lambda cfg: False)
    cfg = {"dry_run": True}
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 10), cfg=cfg)
    for _ in range(2):
        assert _tick(state, b, et(15, 35), cfg=cfg) == []
    assert b.calls == [] and KEY in state["positions"]


def test_real_position_in_dry_run_is_still_sold(sent):
    cfg = {"dry_run": True}
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 10), cfg=cfg)
    assert _tick(state, b, et(15, 35), cfg=cfg) and len(b.calls) == 1


def test_no_bid_is_not_sold(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker(bid=0.0)
    _tick(state, b, et(10, 10))
    for _ in range(3):
        assert _tick(state, b, et(15, 35)) == []
    assert b.calls == [] and KEY in state["positions"]
    nobid = [m for m, _p in sent if "has no bid" in m]
    assert nobid == ["agentic-trader: RIVN 2026-11-13 $13.5 C has no bid, so the "
                     "agent did not sell it. No exit signals reach the agent "
                     "(your signal pass has run out); check it at Robinhood."]
    b.bid = 0.5   # a bid appears: sold on the next loop
    assert _tick(state, b, et(15, 36)) and len(b.calls) == 1


def test_pending_sell_is_not_doubled(sent):
    state = _state(KEY, since=et(10, 0),
                   pending={"side": "sell", "requested": 2, "order_id": "o"})
    b = Broker()
    _tick(state, b, et(10, 10))
    assert _tick(state, b, et(15, 35)) == [] and b.calls == []


def test_failed_sell_is_retried_next_loop(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker(fail=1)
    _tick(state, b, et(10, 10))
    _tick(state, b, et(15, 30))
    assert KEY in state["positions"]
    assert "signal_loss_sold_day" not in state
    assert _tick(state, b, et(15, 31)) and KEY not in state["positions"]


def test_rows_of_every_expiry_are_sold(sent):
    state = _state(KEY, TODAY_KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 10))
    (w,) = _warnings(sent)
    assert "sells them itself" in w and "they are not carried" in w
    _tick(state, b, et(15, 30))
    assert {e["event_id"] for e in b.calls} == {
        f"no-signal|{KEY}|{DAY}", f"no-signal|{TODAY_KEY}|{DAY}"}
    assert state["positions"] == {}


def test_practice_rows_are_closed_on_paper_without_a_warning(sent):
    state = _state(KEY, since=et(10, 0), dry=True, live=False, option_id="dry")
    b = Broker()
    _tick(state, b, et(10, 10))
    assert _warnings(sent) == []
    assert _tick(state, b, et(15, 30)) and KEY not in state["positions"]


def test_early_close_day_sells_three_hours_earlier(sent):
    """The day after Thanksgiving closes at 13:00: the sale is at 12:30."""
    friday = (2026, 11, 27)
    state = _state(KEY, since=et(10, 0, day=friday))
    b = Broker()
    _tick(state, b, et(10, 10, day=friday))
    (w,) = _warnings(sent)
    assert "by 12:30 ET today" in w
    assert _tick(state, b, et(12, 29, day=friday)) == []
    assert agent.signal_loss_busy({}, state, et(12, 29, day=friday))
    assert _tick(state, b, et(12, 30, day=friday)) and len(b.calls) == 1


def test_busy_only_near_the_sell_time_with_a_loss_and_a_position():
    state = _state(KEY, since=et(10, 0))
    assert not agent.signal_loss_busy({}, state, et(15, 28))
    assert agent.signal_loss_busy({}, state, et(15, 29))
    assert agent.signal_loss_busy({}, state, et(15, 59))
    assert not agent.signal_loss_busy({}, state, et(16, 0))
    assert not agent.signal_loss_busy({}, _state(since=et(10, 0)), et(15, 45))
    assert not agent.signal_loss_busy({}, _state(KEY), et(15, 45))


def test_loss_outside_the_session_is_not_stamped():
    state = _state(KEY)
    for now in (et(9, 29), et(16, 0), et(20, 0), et(11, 0, day=(2026, 11, 7))):
        _note(state, "lost", now, "pass_ended")
        assert "signal_loss" not in state, now
    _note(state, "lost", et(9, 30), "pass_ended")
    assert state["signal_loss"]["since"] == et(9, 30).timestamp()
    # still lost later: the start stays, the cause follows the latest read
    _note(state, "lost", et(9, 40), "feed_error")
    assert state["signal_loss"]["since"] == et(9, 30).timestamp()
    assert state["signal_loss"]["cause"] == "feed_error"
    _note(state, "skip", et(9, 50))
    assert "signal_loss" in state


def test_nothing_happens_outside_the_session_or_on_a_holiday(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    assert _tick(state, b, et(16, 5)) == []
    assert _tick(state, b, et(15, 40, day=(2026, 11, 26))) == []  # Thanksgiving
    assert b.calls == [] and sent == []


def test_no_new_entry_after_a_no_signal_sale(monkeypatch, no_policy):
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 40))
    state = {"positions": {}, "signal_loss_sold_day": DAY}
    ev = {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-11-13",
          "strike": 600.0, "type": "C", "event_id": "e1"}
    b = StubBroker()
    out = agent.handle_event(ev, {}, state, b, object(), lambda s: None)
    assert out and "SKIPPED" in out and b.calls == []
    (row,) = _log()
    assert row["action"] == "skip_rule"
    assert row["ui"]["code"] == "after_signal_loss_sell"
    # the next day trades again
    state["signal_loss_sold_day"] = "2026-11-05"
    agent.handle_event({**ev, "event_id": "e2"}, {}, state, b, object(),
                       lambda s: None)
    assert len(b.calls) == 1


def test_late_loss_is_not_sold_today(sent):
    state = _state(KEY, since=et(15, 42))
    b = Broker()
    _tick(state, b, et(15, 47))
    (w,) = _warnings(sent)
    assert "closes before the agent would sell" in w
    assert "it sells at 15:30 ET" in w
    for now in (et(15, 55), et(15, 59, s=59)):
        assert _tick(state, b, now) == []
    assert b.calls == []
    # still lost the next session: warned again, sold at 15:30
    monday = (2026, 11, 9)
    _tick(state, b, et(9, 31, day=monday))
    assert len(_warnings(sent)) == 2
    assert _tick(state, b, et(15, 30, day=monday)) and len(b.calls) == 1


def test_chat_command_switches_it(home):
    cfg = {}
    handled, reply = agent._expiry_command("no-signal sell off", cfg)
    assert handled and cfg["signal_loss_sell_et"] == "off"
    assert "off" in reply
    handled, reply = agent._expiry_command("no-signal sell 14:15", cfg)
    assert handled and cfg["signal_loss_sell_et"] == "14:15"
    handled, _reply = agent._expiry_command("no-signal sell on", cfg)
    assert cfg["signal_loss_sell_et"] == "15:30"


def test_never_raises(sent):
    class Boom(Broker):
        def exit_bid(self, *a):
            raise RuntimeError("down")

        def execute(self, *a):
            raise RuntimeError("down")
    state = _state(KEY, since=et(10, 0))
    _tick(state, Boom(), et(10, 10))
    assert _tick(state, Boom(), et(15, 35)) == []
    assert agent.signal_loss_tick({}, {"positions": "bad", "signal_loss": 5},
                                  lambda s: None, Broker(), None,
                                  now_et=et(15, 35)) == []


def test_dashboard_banner_while_warned(sent):
    state = _state(KEY, since=et(13, 0))
    assert agent.signal_loss_banner({}, state, et(13, 10)) == []
    _tick(state, Broker(), et(13, 10))
    (b,) = agent.signal_loss_banner({}, state, et(13, 20))
    assert b["level"] == "urgent"
    assert b["text"] == ("No exit signals reach the agent (your signal pass has "
                         "run out). It sells your 1 real position itself at "
                         "15:30 ET unless they are back.")
    off = agent.signal_loss_banner({"signal_loss_sell_et": "off"}, state, et(13, 20))
    assert "sell your 1 real position yourself" in off[0]["text"]
    assert agent.signal_loss_banner({}, state, et(16, 5)) == []
    agent._note_exit_source({}, state, lambda s: None, "ok", now_et=et(13, 30))
    assert agent.signal_loss_banner({}, state, et(13, 31)) == []


# ── a feed that comes and goes (review F4) ──────────────────────────────────

def _backs(sent):
    return [m for m, _p in sent if "reaching your agent again" in m]


def test_a_flapping_feed_warns_once_and_once_more_before_the_sale(sent):
    """Losses all morning: one warning and one "back" note, not one of each
    per loss. A loss still there 15 minutes before the sell time gets the
    day's one more warning, and the sale waits 15 minutes after it."""
    state = _state(KEY)
    b = Broker()
    for h, m in ((10, 0), (10, 10), (10, 20), (11, 0), (13, 0)):
        _note(state, "lost", et(h, m), "feed_error")
        _tick(state, b, et(h, m + 6))
        _tick(state, b, et(h, m + 8))
        _note(state, "ok", et(h, m + 9))
    assert len(_warnings(sent)) == 1 and len(_backs(sent)) == 1
    # the afternoon loss is still there at 15:15: warned again, for 15:30
    _note(state, "lost", et(14, 0), "feed_error")
    _tick(state, b, et(14, 30))
    assert len(_warnings(sent)) == 1
    assert agent.signal_loss_sell_at({}, state, et(14, 30)) == et(15, 30).timestamp()
    _tick(state, b, et(15, 15))
    assert len(_warnings(sent)) == 2 and "by 15:30 ET today" in _warnings(sent)[1]
    assert _tick(state, b, et(15, 29)) == [] and b.calls == []
    assert _tick(state, b, et(15, 30)) and len(b.calls) == 1


def test_a_late_loss_after_the_day_s_warnings_is_not_sold_unwarned(sent):
    """Two warnings already went out today. A third loss gets none, so it is
    never sold (no sale without its own warning 15 minutes before)."""
    state = _state(KEY)
    b = Broker()
    for h, m in ((10, 0), (15, 15)):
        _note(state, "lost", et(h, m), "feed_error")
        _tick(state, b, et(h, m + 6))
        _note(state, "ok", et(h, m + 8))
    assert len(_warnings(sent)) == 2 and len(_backs(sent)) == 2
    _note(state, "lost", et(15, 30), "feed_error")
    assert agent.signal_loss_sell_at({}, state, et(15, 36)) is None
    for now in (et(15, 36), et(15, 50), et(15, 59)):
        assert _tick(state, b, now) == []
    # no warning of a sale: only the one note that it does not sell today
    assert b.calls == [] and len(_warnings(sent)) == 3
    assert not [w for w in _warnings(sent)[2:] if "If exit signals are not back" in w]


def test_a_loss_past_the_day_s_warnings_still_tells_the_owner_once(sent):
    """Losses 1 and 2 were warned and cleared; loss 3 lasts past the close.
    It is not sold (no warning of a sale is left today), but the owner gets
    one high-priority note that says so, and the classic banner says it
    too. A fourth loss the same day sends nothing more."""
    state = _state(KEY)
    b = Broker()
    for h, m in ((10, 0), (15, 15)):
        _note(state, "lost", et(h, m), "feed_error")
        _tick(state, b, et(h, m + 5))
        _note(state, "ok", et(h, m + 5, s=30))
    assert len(_warnings(sent)) == 2
    _note(state, "lost", et(15, 22), "feed_error")
    _tick(state, b, et(15, 27))
    (w,) = _warnings(sent)[2:]
    assert [p for m, p in sent if m == w] == ["high"]
    assert "does not sell it on its own today" in w
    assert "sell it yourself at" in w and "If exit signals are not back" not in w
    assert "If exit signals are still missing tomorrow, it sells at 15:30 ET." in w
    (ban,) = agent.signal_loss_banner({}, state, et(15, 28))
    assert "does not sell your 1 real position itself today" in ban["text"]
    assert "itself at" not in ban["text"]
    for now in (et(15, 30), et(15, 45), et(15, 59)):
        assert _tick(state, b, now) == []
    assert b.calls == []
    _note(state, "ok", et(15, 50))
    _note(state, "lost", et(15, 51), "feed_error")
    _tick(state, b, et(15, 57))
    assert len(_warnings(sent)) == 3


def test_the_second_warning_after_the_reminder_time_gives_its_own_notice(sent):
    state = _state(KEY)
    b = Broker()
    _note(state, "lost", et(10, 0), "feed_error")
    _tick(state, b, et(10, 6))
    _note(state, "ok", et(10, 8))
    _note(state, "lost", et(15, 20), "feed_error")
    _tick(state, b, et(15, 25))                  # warned now: sells 15:40
    (_first, w) = _warnings(sent)
    assert "by 15:40 ET today" in w
    assert _tick(state, b, et(15, 39)) == [] and b.calls == []
    assert _tick(state, b, et(15, 40)) and len(b.calls) == 1


def test_the_count_starts_again_each_day(sent):
    state = _state(KEY)
    b = Broker()
    for day in ((2026, 11, 6), (2026, 11, 9)):
        _note(state, "lost", et(10, 0, day=day), "feed_error")
        _tick(state, b, et(10, 6, day=day))
        _note(state, "ok", et(10, 8, day=day))
    assert len(_warnings(sent)) == 2 and len(_backs(sent)) == 2


# ── a full hold blocks the sale: the warning says so (review F6) ────────────

def test_a_full_hold_says_it_will_not_sell(sent):
    state = _state(KEY, since=et(10, 0))
    b = Broker()
    _tick(state, b, et(10, 6), paused=True)
    _tick(state, b, et(11, 0), paused=True)
    (w,) = [m for m, _p in sent if "no exit signals have reached" in m]
    assert "on hold, so it places no orders: it does not sell it" in w
    assert "sell it yourself at" in w and "sells it itself" not in w
    (ban,) = agent.signal_loss_banner({}, state, et(11, 0), paused=True)
    assert "on hold" in ban["text"] and "sell it yourself" in ban["text"]
    assert agent.signal_loss_banner({}, state, et(11, 0)) == []
    # the hold warning is no notice of a sale: when the hold ends at 15:20
    # the real warning goes out, and the sale waits 15 minutes after it
    assert _tick(state, b, et(15, 20)) == []
    real = [m for m, _p in sent if "If exit signals are not back" in m]
    assert len(real) == 1 and "by 15:35 ET today" in real[0]
    assert _tick(state, b, et(15, 34)) == [] and b.calls == []
    assert _tick(state, b, et(15, 35)) and len(b.calls) == 1


def test_the_warning_says_where_to_switch_it_off(sent):
    state = _state(KEY, since=et(10, 0))
    _tick(state, Broker(), et(10, 6))
    (w,) = _warnings(sent)
    assert ('set "No-signal sell" to Off in the dashboard (Rules & limits), '
            'or say "no-signal sell off" in the classic dashboard') in w


def test_the_activity_rows_have_their_own_codes_and_checks(monkeypatch, no_policy):
    """The no-signal sale and the entries it (and a short day-pass) skips
    reach the dashboard with their own reason codes, each skip stopped at
    its own entry check."""
    import view_model as vm
    state = _state(KEY, since=et(13, 0))
    b = Broker()
    _tick(state, b, et(13, 10))
    _tick(state, b, et(15, 30))
    monkeypatch.setattr(agent, "_et_now", lambda: et(15, 40))
    ev = {"event": "ENTERED", "ticker": "SPY", "expiry": "2026-11-13",
          "strike": 600.0, "type": "C", "event_id": "e1"}
    agent.handle_event(ev, {}, state, StubBroker(), object(), lambda s: None)
    rows = [{**r, "n": i} for i, r in enumerate(_log())]
    page = vm.activity_page(rows, limit=10)["rows"]
    got = {(r["reason_code"], r["stopped_at_check"]) for r in page}
    assert ("signal_loss", None) in got
    assert ("after_signal_loss_sell", "exits_reachable") in got
    assert "pass_wont_cover" in vm.REASON_CODES
    ids = vm.check_ids()
    assert ids.index("before_expiry_guard") < ids.index("exits_reachable") \
        < ids.index("pass_covers_close") < ids.index("daily_cap")


# ── a row from another broker account or mode (review) ──────────────────────

def test_a_row_from_another_account_is_never_promised_a_sale(sent):
    """The agent never sells a row opened in another broker account or
    mode (_check_before_exit: blocked): no warning, banner or sell time
    promises that it does, and no sale is tried for it."""
    state = _state(KEY, since=et(13, 0), acct="robinhood:someother")
    b = Broker()
    assert _tick(state, b, et(13, 10)) == []
    (w,) = _warnings(sent)
    assert "RIVN 2026-11-13 $13.5 C" in w
    assert "If exit signals are not back" not in w and "sells it itself" not in w
    assert "different broker account or mode" in w
    assert "sell it yourself at" in w
    assert agent.signal_loss_sell_at({}, state, et(13, 20)) is None
    (ban,) = agent.signal_loss_banner({}, state, et(13, 20))
    assert "itself at" not in ban["text"]
    assert "different broker account or mode" in ban["text"]
    for t in ((15, 30), (15, 40), (15, 50)):
        assert _tick(state, b, et(*t)) == []
    assert b.calls == [] and KEY in state["positions"]
    assert not [r for r in _log() if r.get("action") in ("exit", "exit_failed")]


def test_with_rows_in_both_accounts_only_this_account_s_is_sold(sent):
    here = agent._account_tag({}, None, object())
    state = _state(KEY, since=et(13, 0), acct=here)
    state["positions"][TODAY_KEY] = {"option_id": "opt-2", "qty": 1,
                                     "live": True, "acct": "robinhood:other"}
    b = Broker()
    _tick(state, b, et(13, 10))
    (w,) = _warnings(sent)
    assert "the agent sells RIVN 2026-11-13 $13.5 C itself" in w
    assert "It does not sell SPY 2026-11-06 $600.0 P" in w
    (ban,) = agent.signal_loss_banner({}, state, et(13, 20))
    assert "It sells your 1 real position itself at 15:30 ET" in ban["text"]
    assert "does not sell the other 1" in ban["text"]
    assert _tick(state, b, et(15, 30))
    assert [e["event_id"] for e in b.calls] == [f"no-signal|{KEY}|{DAY}"]
    assert TODAY_KEY in state["positions"]
