"""The signal feed is read on every loop inside the feed window (09:25 ET to
15 minutes after the close, on trading days) and about once an hour outside
it, where no signal has ever been published. Holidays and early closes
count. A loop that doesn't read the feed still retries the exits it owes.
Zero network; `agent.py run` is driven with a stub feed and broker."""

from datetime import datetime, timezone

import agent
from sources import agenthc_day_trade_ideas as agenthc
from test_main_loop_events import KEY, Broker, Clock, feed, run_loop  # noqa: F401
from test_main_loop_events import clock, sent  # noqa: F401  (fixture reuse)

ET = agent.MARKET_TZ


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


class IdleSource:
    """A feed like AgentHC's: read hourly outside the window. Counts reads."""
    IDLE_READS = True

    def __init__(self, events=None):
        self.reads = 0
        self.events = events or []

    def poll(self, cfg, state, save_state):
        self.reads += 1
        return [dict(ev) for ev in self.events]


def follow_clock(monkeypatch, clock):
    """agent._now (epoch seconds) moves with the harness clock too."""
    monkeypatch.setattr(agent, "_now", lambda: clock.t.timestamp())


def run_at(home, monkeypatch, clock, start, loops, step, source, state=None,
           broker=None, config=None):
    clock.t = start.astimezone(timezone.utc)
    follow_clock(monkeypatch, clock)
    return run_loop(home, monkeypatch,
                    state or {"seen": [], "positions": {}},
                    [[]] * loops, clock=clock, steps=(step,) * loops,
                    source=source, broker=broker, config=config)


# ── the window ───────────────────────────────────────────────────────────────

def test_window_holiday_and_half_day():
    # an ordinary Wednesday: 09:25 to 16:15
    assert not agent._feed_window_now({}, et(2026, 10, 28, 9, 24))
    assert agent._feed_window_now({}, et(2026, 10, 28, 9, 25))
    assert agent._feed_window_now({}, et(2026, 10, 28, 16, 15))
    assert not agent._feed_window_now({}, et(2026, 10, 28, 16, 16))
    # Thanksgiving (a weekday the market is closed all day)
    assert not agent._feed_window_now({}, et(2026, 11, 26, 11, 0))
    # the day after: closes 13:00, so the window ends 13:15
    assert agent._feed_window_now({}, et(2026, 11, 27, 13, 15))
    assert not agent._feed_window_now({}, et(2026, 11, 27, 13, 16))
    # the owner's own closures and early closes
    assert not agent._feed_window_now({"market_holidays": ["2026-10-28"]},
                                      et(2026, 10, 28, 11, 0))
    assert not agent._feed_window_now({"early_close_days": ["2026-10-28"]},
                                      et(2026, 10, 28, 14, 0))
    # a weekend
    assert not agent._feed_window_now({}, et(2026, 10, 31, 11, 0))


def test_market_open_now_follows_holidays(monkeypatch):
    """_market_open_now (the loop's fast/slow cadence) was weekday-only: on
    a holiday it polled every 30 s all day for nothing."""
    monkeypatch.setattr(agent, "_et_now", lambda: et(2026, 11, 26, 11, 0))
    assert agent._market_open_now() is False
    monkeypatch.setattr(agent, "_et_now", lambda: et(2026, 11, 25, 11, 0))
    assert agent._market_open_now() is True


# ── how often the loop reads ────────────────────────────────────────────────

def test_offwindow_polls_hourly(home, monkeypatch, sent, clock):
    """A Saturday at the 5-minute off-hours cadence: 288 loops, 24 reads
    (it was 288)."""
    src = IdleSource()
    run_at(home, monkeypatch, clock, et(2026, 10, 31, 0, 0), 288, 300, src)
    assert src.reads == 24


def test_offwindow_setting_zero_reads_every_loop(home, monkeypatch, sent, clock):
    src = IdleSource()
    run_at(home, monkeypatch, clock, et(2026, 10, 31, 0, 0), 24, 300, src,
           config={"feed_idle_read_seconds": 0})
    assert src.reads == 24


def test_inwindow_cadence_unchanged(home, monkeypatch, sent, clock):
    """09:25 to 16:15 on a Wednesday: every loop reads the feed."""
    src = IdleSource()
    loops = (16 * 60 + 15 - (9 * 60 + 25)) // 1 + 1   # one loop a minute
    run_at(home, monkeypatch, clock, et(2026, 10, 28, 9, 25), loops, 60, src)
    assert src.reads == loops


def test_a_source_without_idle_reads_is_read_every_loop(home, monkeypatch, sent, clock):
    """The owner's own feed or the commands file: not changed."""
    class Plain(IdleSource):
        IDLE_READS = False
    src = Plain()
    run_at(home, monkeypatch, clock, et(2026, 10, 31, 0, 0), 12, 300, src)
    assert src.reads == 12


def test_agenthc_source_reads_idle():
    assert agenthc.IDLE_READS is True


# ── what a loop without a read still does ───────────────────────────────────

def test_skipped_loop_keeps_owed_exit_retry(home, monkeypatch, sent, clock):
    """An exit the agent owes is retried on every loop, also the ones that
    don't read the feed (a Tuesday evening)."""
    exited = feed("EXITED", "2026-10-27T19:00:00+00:00")

    class Flaky(Broker):
        """The first three sells aren't placed."""
        def __init__(self):
            super().__init__()
            self.tries = 0

        def execute(self, client, cfg, event, state):
            if event["event"] == "EXITED":
                self.tries += 1
                self.sell_ok = self.tries > 3
            return super().execute(client, cfg, event, state)
    broker = Flaky()
    src = IdleSource()
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}},
             "exits_to_retry": [exited]}
    broker, saved = run_at(home, monkeypatch, clock, et(2026, 10, 27, 20, 0),
                           5, 300, src, state=state, broker=broker)
    assert src.reads == 1                 # the first loop only
    assert broker.tries == 4              # loops 1-3 retried it without a read
    assert broker.calls == [("sell", exited["event_id"])]
    assert saved["positions"] == {} and saved["exits_to_retry"] == []
    # nothing was forgotten from the handled list on the loops without a read
    assert exited["event_id"] in saved["seen"]


# ── when it reads at once ───────────────────────────────────────────────────

SAT = et(2026, 10, 31, 10, 0)


def test_first_loop_and_resume_read_immediately(home, monkeypatch):
    (home / "config.json").write_text('{"source": "agenthc"}', encoding="utf-8")
    now = SAT.timestamp()
    src = IdleSource()
    # a fresh start: read at once
    assert agent._feed_read_due({}, {}, src, now, SAT) == (True, "asked")
    agent.FEED_GATE.update(read_at=now, now=False)
    later = SAT.replace(minute=5)
    assert agent._feed_read_due({}, {}, src, now + 300, later)[0] is False
    # Resume: at once
    assert agent._apply_command("resume")[0] is True
    assert agent._feed_read_due({}, {}, src, now + 300, later) == (True, "asked")
    agent.FEED_GATE.update(now=False)
    # "check now": at once (and buying power with it)
    handled, reply = agent._apply_command("check now")
    assert handled and "Checking now" in reply
    assert agent._feed_read_due({}, {}, src, now + 300, later) == (True, "asked")
    assert agent._cash_refresh_due({}, {"at": now, "dry": False}, now + 300, later)
    # an hour after the last read: the regular idle read
    agent.FEED_GATE.update(now=False)
    assert agent._feed_read_due({}, {}, src, now + 3600, SAT.replace(hour=11))[0]


def test_pending_payment_reads():
    """A pass invoice or a wallet day-pass payment waiting to be decided:
    poll() is what checks it, so the agent reads on every loop."""
    now = SAT.timestamp()
    agent.FEED_GATE.update(read_at=now, now=False)
    later = SAT.replace(minute=5)
    assert agent._feed_read_due({}, {}, agenthc, now + 300, later)[0] is False
    # a wallet day-pass paid but not decided yet
    st = {"daypass_pending": {"payment_hash": "h", "macaroon": "m"}}
    assert agent._feed_read_due({}, st, agenthc, now + 300, later) == (True, "payment")
    # a prepaid pass invoice shown, not paid yet
    cfg = {"feed_pass_pending": {"token": "tok-1"}}
    tid = agenthc._token_id("tok-1")
    st = {"pass_status": {tid: {"status": "pending"}}}
    assert agent._feed_read_due(cfg, st, agenthc, now + 300, later) == (True, "payment")
    # ... paid: back to the hourly schedule
    st = {"pass_status": {tid: {"status": "active"}}}
    assert agent._feed_read_due(cfg, st, agenthc, now + 300, later)[0] is False
    # a Premium key pays nothing per day
    st = {"daypass_pending": {"payment_hash": "h"}}
    assert agent._feed_read_due({"agenthc_api_key": "k"}, st, agenthc,
                                now + 300, later)[0] is False


# ── what the dashboard says ─────────────────────────────────────────────────

def test_status_shows_last_read():
    agent.FEED_GATE.update(read_at=et(2026, 10, 31, 9, 0).timestamp(), now=False)
    note = agent._feed_schedule_note({}, agenthc, SAT)
    assert note == ("Signal feed: last read 09:00 ET (market closed, next "
                    "regular read Mon 09:25 ET)")
    # before the open on a trading day: today's 09:25
    note = agent._feed_schedule_note({}, agenthc, et(2026, 11, 2, 7, 0))
    assert note.endswith("next regular read 09:25 ET)")
    # inside the window it is read every loop: nothing to say
    assert agent._feed_schedule_note({}, agenthc, et(2026, 11, 2, 11, 0)) == ""
    # the owner turned idle reads off, or a source that is always read
    assert agent._feed_schedule_note({"feed_idle_read_seconds": 0}, agenthc, SAT) == ""

    class Plain:
        IDLE_READS = False
    assert agent._feed_schedule_note({}, Plain, SAT) == ""
