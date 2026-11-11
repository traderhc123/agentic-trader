"""The AgentHC feed answering the same refusal again and again: after three
identical refusals the agent waits 2, 4, 8, then 15 minutes between reads
(staying at 15) until any different answer arrives. Exits owed and the
expiry guard don't need the feed and keep running on every loop. Zero
network: the feed's HTTP is a stub that counts requests."""

import json
from datetime import datetime, timezone

import pytest

import agent
from sources import agenthc_day_trade_ideas as agenthc
from test_main_loop_events import KEY, Broker, feed, run_loop  # noqa: F401
from test_main_loop_events import clock, sent  # noqa: F401  (fixture reuse)

ET = agent.MARKET_TZ
OPEN = datetime(2026, 10, 28, 9, 25, tzinfo=ET)   # a Wednesday
SESSION_LOOPS = (16 * 60 + 15 - (9 * 60 + 25)) * 2   # 30-second loops: 820
REFUSED = b'{"detail":"Forbidden"}' + b" " * 125   # 147 bytes, no error code


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Transport:
    """The feed: answers[i] for request i (the last one repeats)."""

    def __init__(self, answers):
        self.answers = answers
        self.n = 0

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        got = self.answers[min(self.n, len(self.answers) - 1)]
        self.n += 1
        return got


@pytest.fixture
def wire(monkeypatch):
    """Install a Transport; the source's clock follows `t["now"]`."""
    t = {"now": OPEN.timestamp()}
    monkeypatch.setattr(agenthc, "_now_s", lambda: t["now"])

    def install(answers):
        tr = Transport(answers)
        monkeypatch.setattr(agenthc.requests, "get", tr.get)
        return tr
    return t, install


def drive(t, loops, step=30, cfg=None, state=None):
    """The loop's read decision + poll, `loops` times, every `step` s."""
    cfg = cfg or {"agenthc_api_key": "k"}
    state = state if state is not None else {}
    start = t["now"]
    for i in range(loops):
        now = start + i * step
        t["now"] = now
        now_et = datetime.fromtimestamp(now, ET)
        due, _ = agent._feed_read_due(cfg, state, agenthc, now, now_et)
        if not due:
            continue
        agent.FEED_GATE["now"] = False
        try:
            agenthc.poll(cfg, state)
            agent.FEED_GATE["read_at"] = now
        except Exception:
            pass


def test_three_identical_403s_then_backoff(wire):
    """A whole session of the same 147-byte 403: ~35 requests, not 820."""
    t, install = wire
    tr = install([Resp(403, REFUSED)])
    drive(t, SESSION_LOOPS)
    assert 30 <= tr.n <= 40, tr.n
    info = agenthc.refusal_info()
    assert info["status"] == 403 and info["next_at"] - t["now"] <= 900


def test_backoff_steps(wire):
    t, install = wire
    tr = install([Resp(403, {"detail": {"error": "not_allowed"}})])
    gaps, last = [], None
    for i in range(400):
        now = OPEN.timestamp() + i * 30
        t["now"] = now
        due, _ = agent._feed_read_due({}, {}, agenthc, now,
                                      datetime.fromtimestamp(now, ET))
        if due:
            agent.FEED_GATE["now"] = False
            before = tr.n
            with pytest.raises(RuntimeError):
                agenthc.poll({"agenthc_api_key": "k"}, {})
            assert tr.n == before + 1
            if last is not None:
                gaps.append(now - last)
            last = now
    assert gaps[:7] == [30, 30, 120, 240, 480, 900, 900]


def test_different_answer_resets(wire):
    t, install = wire
    tr = install([Resp(403, REFUSED)] * 3
                 + [Resp(200, {"events": []})] + [Resp(403, REFUSED)] * 50)
    drive(t, 3)                        # three refusals: backing off
    assert agenthc.refusal_wait_until() is not None
    agent.FEED_GATE["now"] = True      # the owner says "check now"
    t["now"] += 30
    drive(t, 1)
    assert tr.n == 4                   # read at once, despite the back-off
    assert agenthc.refusal_wait_until() is None   # a 200: back to normal
    drive(t, 2)                        # two more refusals: not three yet
    assert tr.n == 6 and agenthc.refusal_wait_until() is None
    # a different refusal starts its own count
    tr.answers = [Resp(403, {"detail": {"error": "a"}}),
                  Resp(403, {"detail": {"error": "b"}})] * 10
    tr.n = 0
    drive(t, 6)
    assert tr.n == 6 and agenthc.refusal_wait_until() is None


def test_not_for_handled_or_server_answers(wire):
    """feed_not_live (the feed's own "later"), a 5xx outage: read as usual."""
    t, install = wire
    tr = install([Resp(403, {"detail": {"error": "feed_not_live"}})])
    drive(t, 10)
    assert tr.n == 10 and agenthc.refusal_wait_until() is None
    tr = install([Resp(503, b"busy")])
    drive(t, 10)
    assert tr.n == 10 and agenthc.refusal_wait_until() is None


def test_owed_exit_runs_during_backoff(home, monkeypatch, sent, clock):
    """The feed keeps refusing; the exit the agent owes is retried on every
    loop and goes out on the first one the broker takes."""
    clock.t = OPEN.replace(hour=11).astimezone(timezone.utc)
    monkeypatch.setattr(agent, "_now", lambda: clock.t.timestamp())
    monkeypatch.setattr(agenthc, "_now_s", lambda: clock.t.timestamp())
    tr = Transport([Resp(403, REFUSED)])
    monkeypatch.setattr(agenthc.requests, "get", tr.get)
    exited = feed("EXITED", "2026-10-28T14:00:00+00:00")

    class Late(Broker):
        """Sells are placed from the 6th try on (the 6th loop)."""
        def __init__(self):
            super().__init__()
            self.tries = 0

        def execute(self, client, cfg, event, state):
            if event["event"] == "EXITED":
                self.tries += 1
                self.sell_ok = self.tries >= 6
            return super().execute(client, cfg, event, state)
    broker = Late()
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}},
             "exits_to_retry": [exited]}
    broker, saved = run_loop(home, monkeypatch, state, [[]] * 6, clock=clock,
                             steps=(30,) * 6, source=agenthc, broker=broker,
                             config={"source": "stub", "agenthc_api_key": "k"})
    assert tr.n == 3                   # three refusals, then the back-off
    assert broker.tries == 6           # one try per loop, read or not
    assert broker.calls == [("sell", exited["event_id"])]
    assert saved["positions"] == {} and saved["exits_to_retry"] == []


def test_status_shows_refusal_and_times(wire):
    t, install = wire
    install([Resp(403, {"detail": {"error": "key_revoked"}})])
    drive(t, 3)
    note = agent._feed_schedule_note({}, agenthc,
                                     datetime.fromtimestamp(t["now"], ET))
    assert note.startswith("Signal feed refused (403 key_revoked) since 09:25 ET; "
                           "next try 09:28 ET")
    # outside the window too, the refusal is what the owner needs to see
    assert agent._feed_schedule_note(
        {}, agenthc, datetime(2026, 10, 31, 10, 0, tzinfo=ET)).startswith(
        "Signal feed refused")


# ── the no-signal sell: reads the back-off or the schedule skips ─────────────

def _backing_off(since, next_at):
    """The source's state after three identical 403s (refusal_wait_until)."""
    agenthc._REFUSAL.clear()
    agenthc._REFUSAL.update(key=agenthc._refusal_key(Resp(403, REFUSED)),
                            count=3, since=since, next_at=next_at)


def test_reads_the_backoff_skips_keep_the_no_signal_clock_running(
        home, monkeypatch, sent, clock):
    """The feed has refused the agent since before the open. 09:29:30: one
    refused read (no loss before the session), then the back-off skips the
    reads at 09:30 and after. Those skipped reads are no exit signal either:
    the loss starts at 09:30, as feed_refused, and the owner is warned five
    minutes later, before the next refused read could have started it."""
    clock.t = OPEN.replace(minute=29, second=30).astimezone(timezone.utc)
    monkeypatch.setattr(agent, "_now", lambda: clock.t.timestamp())
    monkeypatch.setattr(agenthc, "_now_s", lambda: clock.t.timestamp())
    tr = Transport([Resp(403, REFUSED)])
    monkeypatch.setattr(agenthc.requests, "get", tr.get)
    start = clock.t.timestamp()
    _backing_off(start - 600, start - 1)     # 4th refusal now: wait 240 s
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1,
                                             "live": True}}}
    _b, saved = run_loop(home, monkeypatch, state, [[]] * 12, clock=clock,
                         steps=(30,) * 12, source=agenthc,
                         config={"source": "stub", "agenthc_api_key": "k"})
    assert tr.n == 2                         # 09:29:30 and 09:33:30 only
    loss = saved["signal_loss"]
    assert loss["cause"] == "feed_refused"
    assert loss["since"] == start + 30       # 09:30:00, a skipped read
    (w,) = [m for m in sent if "no exit signals have reached" in m]
    assert "since 9:30 ET (the signal feed is refusing the agent)" in w


def test_a_refusal_backoff_outside_the_session_records_nothing(
        home, monkeypatch, sent, clock):
    """The same back-off at 19:00 ET: no signal is published then, so no
    loss starts."""
    clock.t = OPEN.replace(hour=19).astimezone(timezone.utc)
    monkeypatch.setattr(agent, "_now", lambda: clock.t.timestamp())
    monkeypatch.setattr(agenthc, "_now_s", lambda: clock.t.timestamp())
    tr = Transport([Resp(403, REFUSED)])
    monkeypatch.setattr(agenthc.requests, "get", tr.get)
    start = clock.t.timestamp()
    _backing_off(start - 600, start - 1)
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1,
                                             "live": True}}}
    _b, saved = run_loop(home, monkeypatch, state, [[]] * 6, clock=clock,
                         source=agenthc,
                         config={"source": "stub", "agenthc_api_key": "k"})
    assert tr.n == 1
    assert "signal_loss" not in saved
    assert not [m for m in sent if "no exit signals" in m]


class OffHours:
    """A source read on the hourly schedule outside the feed window. Its one
    read (the loop's first, "asked") answers without exit signals."""
    IDLE_READS = True
    reads = 0

    @classmethod
    def poll(cls, cfg, st, save_state):
        cls.reads += 1
        return []

    had_access = staticmethod(lambda: False)
    had_exit_access = staticmethod(lambda: False)
    exit_loss_reason = staticmethod(lambda: "feed_not_live")


@pytest.mark.parametrize("before", [None, {"since": 1.0, "cause": "pass_ended",
                                           "warned_at": 2.0,
                                           "warned_day": "2026-10-28"}])
def test_reads_the_off_hours_schedule_skips_never_start_or_extend_a_loss(
        home, monkeypatch, sent, clock, before):
    """20:00 ET: the first loop reads, the next ones are skipped by the
    hourly schedule. With no loss on record none starts; one on record from
    the session is kept exactly as it was (same start, same cause)."""
    clock.t = OPEN.replace(hour=20).astimezone(timezone.utc)
    monkeypatch.setattr(agent, "_now", lambda: clock.t.timestamp())
    OffHours.reads = 0
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1,
                                             "live": True}}}
    if before is not None:
        state["signal_loss"] = dict(before)
    _b, saved = run_loop(home, monkeypatch, state, [[]] * 6, clock=clock,
                         source=OffHours)
    assert OffHours.reads == 1
    assert saved.get("signal_loss") == before
    assert not [m for m in sent if "no exit signals" in m]
