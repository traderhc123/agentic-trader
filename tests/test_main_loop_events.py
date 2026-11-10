"""How the main loop handles a poll's events: which ones it remembers, what
it does when an entry and its exit arrive together, exits it still owes,
when it starts "watching" the feed, and feeds that give no event time.
Zero network; `agent.py run` is driven with a stub feed and broker."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import agent
from sources import agenthc_day_trade_ideas as agenthc
from sources import generic_json_url

EXPIRY = "2099-12-18"
KEY = f"SPY|{EXPIRY}|752.0|C"
START = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)   # Wed 11:00 ET


class Clock:
    def __init__(self):
        self.t = START

    def ago(self, seconds):
        return (self.t - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def clock(monkeypatch):
    """agent's wall clock (datetime.now), moved by the loop harness."""
    c = Clock()

    class Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.t.astimezone(tz) if tz else c.t.replace(tzinfo=None)
    monkeypatch.setattr(agent, "datetime", Fake)
    return c


@pytest.fixture
def sent(monkeypatch, home, no_policy):
    out = []
    monkeypatch.setattr(
        agent, "notify", lambda cfg, msg, priority=None:
        None if "heartbeat started" in msg else out.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    return out


class Broker:
    """Like a real adapter: buys only what it doesn't hold, sells only what
    it holds. `sell_ok=False`: exit orders aren't placed."""

    def __init__(self):
        self.calls = []
        self.sell_ok = True

    def client(self, cfg):
        return object()

    def execute(self, client, cfg, event, state):
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        held = key in state["positions"]
        if event["event"] == "ENTERED":
            if held:
                return False
            self.calls.append(("buy", event["event_id"]))
            state["positions"][key] = {"option_id": "stub", "qty": 1}
            return True
        if not held or not self.sell_ok:
            return False
        self.calls.append(("sell", event["event_id"]))
        del state["positions"][key]
        return True


def feed(event, occurred_at, ticker="SPY", track="main"):
    """One event exactly as the AgentHC source hands it to the agent."""
    (ev,) = agenthc._parse_events({"events": [{
        "event": event, "ticker": ticker, "expiry": EXPIRY, "strike": 752.0,
        "type": "C", "occurred_at": occurred_at, "track": track}]})
    return ev


def run_loop(home, monkeypatch, state, polls, clock=None, steps=(), broker=None,
             access=None, before_poll=None):
    """`agent.py run` for len(polls) loops. polls[i] is the list of events
    poll i returns, or an exception it raises. After loop i the clock moves
    steps[i] seconds (default 30). access[i]: whether poll i could read
    signals (omit = the source has no say). Returns (broker, saved state)."""
    import brokers
    import keep_running
    import webui
    broker = broker or Broker()
    i = {"n": 0}

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            if before_poll:
                before_poll(i["n"], broker)
            got = polls[i["n"]]
            if isinstance(got, Exception):
                raise got
            return [dict(ev) for ev in got]
    if access is not None:
        Source.had_access = staticmethod(lambda: access[i["n"]])

    def sleep(wait, controls):
        if i["n"] + 1 >= len(polls):
            raise KeyboardInterrupt
        if clock is not None:
            clock.t += timedelta(seconds=steps[i["n"]] if i["n"] < len(steps) else 30)
        i["n"] += 1

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")

    class NoLock:  # a test may run the loop twice (a "restart")
        def acquire(self, wait=0.0):
            return True
    monkeypatch.setattr(keep_running, "InstanceLock", NoLock)
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
    (home / "config.json").write_text(
        json.dumps({"source": "stub", "broker": "stub"}), encoding="utf-8")
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    agent.cmd_run()
    return broker, json.loads((home / "state.json").read_text(encoding="utf-8"))


def rows(home):
    p = home / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def old_exit_ids(n):
    """n ids of long-finished trades, as an older version saved them."""
    return sorted(f"EXITED|T{i:03d}|2026-01-16|1.0|C|2026-01-0{1 + i % 9}T15:00:00+00:00"
                  for i in range(n))


# ── E1: which events the agent remembers ────────────────────────────────────

def test_a_new_entry_is_remembered_when_the_list_is_full(home, monkeypatch, sent, clock):
    """500 remembered ids, all of them exits: the new entry's id sorts
    before every one of them. It was forgotten at once, so the entry was
    bought again as soon as its exit had sold it."""
    entered = feed("ENTERED", (START + timedelta(seconds=50)).isoformat())
    exited = feed("EXITED", (START + timedelta(seconds=75)).isoformat())
    broker, saved = run_loop(
        home, monkeypatch, {"seen": old_exit_ids(500), "positions": {}},
        [[], [entered], [exited, entered], [exited, entered]],
        clock=clock, steps=(60, 20, 600))
    assert broker.calls == [("buy", entered["event_id"]), ("sell", exited["event_id"])]
    assert saved["positions"] == {}
    assert not [m for m in sent if "SKIPPED" in m]      # no "too old" for a trade it took
    assert saved["seen"][-2:] == [entered["event_id"], exited["event_id"]]
    assert len(saved["seen"]) == 500


def test_remembered_ids_are_dropped_oldest_first_never_one_still_listed():
    state = {"seen": [f"id-{i:04d}" for i in range(500)], "seen_order": "oldest_first"}
    agent._mark_seen(state, "ENTERED|AAA", listed=["id-0000", "ENTERED|AAA"])
    agent._mark_seen(state, "A-second", listed=["id-0000", "ENTERED|AAA", "A-second"])
    ids = state["seen"]
    assert len(ids) == 500
    assert ids[0] == "id-0000"               # still in the feed: never dropped
    assert "id-0001" not in ids and "id-0002" not in ids and "id-0003" in ids
    assert ids[-2:] == ["ENTERED|AAA", "A-second"]
    agent._mark_seen(state, "ENTERED|AAA", listed=[])   # seen again: no duplicate
    assert state["seen"].count("ENTERED|AAA") == 1 and len(state["seen"]) == 500
    # a feed that lists more than 500 events (a long commands.jsonl): all kept
    agent._mark_seen(state, "one-more", listed=state["seen"] + ["one-more"])
    assert len(state["seen"]) == 501


def test_upgrade_keeps_the_old_list_and_trades_nothing_again(home, monkeypatch, sent, clock):
    """A state.json from the current release (a plain sorted list): every
    event it lists stays handled, fresh entries included."""
    entered = feed("ENTERED", clock.ago(20))
    exited = feed("EXITED", clock.ago(5), ticker="QQQ")
    legacy = sorted(old_exit_ids(40) + [entered["event_id"], exited["event_id"]])
    broker, saved = run_loop(home, monkeypatch, {"seen": legacy, "positions": {}},
                             [[exited, entered], [exited, entered]], clock=clock)
    assert broker.calls == [] and sent == [] and rows(home) == []
    assert sorted(saved["seen"]) == legacy and isinstance(saved["seen"], list)
    assert saved["seen_order"] == "oldest_first"


def test_upgrade_does_not_buy_again_what_the_trade_log_says_it_bought(
        home, monkeypatch, sent, clock):
    """An owner already hit by the bug: the list is full and the entry it
    bought was forgotten. The trade log still has it."""
    entered = {"event": "ENTERED", "ticker": "SPY", "expiry": EXPIRY, "strike": 752.0,
               "type": "C", "event_id": "my-feed-entry-17"}      # a feed with no times
    (home / "trades.jsonl").write_text(json.dumps(
        {"action": "entry", "event_id": "my-feed-entry-17", "dry": False,
         "contract": f"SPY {EXPIRY} $752.0 C", "ts": "2026-10-28T14:00:00+00:00"}) + "\n",
        encoding="utf-8")
    broker, saved = run_loop(home, monkeypatch,
                             {"seen": old_exit_ids(500), "positions": {}},
                             [[entered], [entered]], clock=clock)
    assert broker.calls == []
    assert "my-feed-entry-17" in saved["seen"]


# ── E3: an entry and its exit in one batch; exits the agent still owes ──────

@pytest.mark.parametrize("newest_first", [True, False])
def test_entry_whose_exit_already_arrived_is_not_bought(
        home, monkeypatch, sent, clock, newest_first):
    """Both signals arrive in one poll (the computer slept a few minutes).
    Newest first, the agent bought with no exit ever coming; oldest first,
    it bought and sold at once."""
    entered = feed("ENTERED", (START + timedelta(seconds=10)).isoformat())
    exited = feed("EXITED", (START + timedelta(seconds=100)).isoformat())
    batch = [exited, entered] if newest_first else [entered, exited]
    broker, saved = run_loop(home, monkeypatch, {"seen": [], "positions": {}},
                             [[], batch, batch], clock=clock, steps=(120, 30))
    assert broker.calls == [] and saved["positions"] == {}
    (row,) = rows(home)
    assert row["action"] == "skip_stale" and row["event_id"] == entered["event_id"]
    assert "exit" in row["reason"] and "not bought" in row["reason"]
    assert set(saved["seen"]) == {entered["event_id"], exited["event_id"]}


def test_exit_seen_earlier_also_stops_a_late_entry(home, monkeypatch, sent, clock):
    """The buy kept failing while the exit arrived and was handled (nothing
    held, nothing to sell). When the buy finally works, the trade is over."""
    entered = feed("ENTERED", (START + timedelta(seconds=10)).isoformat())
    exited = feed("EXITED", (START + timedelta(seconds=40)).isoformat())

    class DownAtFirst(Broker):
        def execute(self, client, cfg, event, state):
            if event["event"] == "ENTERED" and not self.sell_ok:
                raise ConnectionError("broker unreachable")
            return super().execute(client, cfg, event, state)
    broker = DownAtFirst()
    broker.sell_ok = False                    # "down" for the first two polls

    def before_poll(n, b):
        b.sell_ok = n >= 3
    broker, saved = run_loop(home, monkeypatch, {"seen": [], "positions": {}},
                             [[], [entered], [exited, entered], [exited, entered]],
                             clock=clock, steps=(30, 30, 30), broker=broker,
                             before_poll=before_poll)
    assert broker.calls == [] and saved["positions"] == {}


def test_reentry_after_an_older_exit_is_still_bought(home, monkeypatch, sent, clock):
    """Exit of the first trade and a new entry in the same contract, in one
    poll: the exit is done first, then the new trade is bought."""
    first = feed("ENTERED", clock.ago(3600))
    exited = feed("EXITED", (START + timedelta(seconds=10)).isoformat())
    again = feed("ENTERED", (START + timedelta(seconds=40)).isoformat())
    state = {"seen": [first["event_id"]],
             "positions": {KEY: {"option_id": "stub", "qty": 1}}}
    broker, saved = run_loop(home, monkeypatch, state,
                             [[], [again, exited, first]], clock=clock, steps=(60,))
    assert broker.calls == [("sell", exited["event_id"]), ("buy", again["event_id"])]
    assert list(saved["positions"]) == [KEY]


def test_exit_not_done_is_retried_after_the_feed_stops_listing_it(
        home, monkeypatch, sent, clock):
    exited = feed("EXITED", clock.ago(5))
    broker = Broker()

    def before_poll(n, b):
        b.sell_ok = n >= 2
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}}}
    broker, saved = run_loop(home, monkeypatch, state, [[exited], [], [], []],
                             clock=clock, broker=broker, before_poll=before_poll)
    assert broker.calls == [("sell", exited["event_id"])]
    assert saved["positions"] == {} and saved.get("exits_to_retry") == []
    assert exited["event_id"] in saved["seen"]


def test_owed_exit_is_retried_when_the_feed_cannot_be_read_and_after_a_restart(
        home, monkeypatch, sent, clock):
    exited = feed("EXITED", clock.ago(5))
    broker = Broker()
    broker.sell_ok = False
    state = {"seen": [], "positions": {KEY: {"option_id": "stub", "qty": 1}}}
    broker, saved = run_loop(home, monkeypatch, state, [[exited]], clock=clock,
                             broker=broker)
    assert [e["event_id"] for e in saved["exits_to_retry"]] == [exited["event_id"]]
    assert exited["event_id"] not in saved["seen"] and KEY in saved["positions"]
    # restarted; the feed is unreachable — the exit it owes is still done
    broker.sell_ok = True
    broker, saved = run_loop(home, monkeypatch, saved,
                             [ConnectionError("feed unreachable")], clock=clock,
                             broker=broker)
    assert broker.calls == [("sell", exited["event_id"])]
    assert saved["positions"] == {} and saved["exits_to_retry"] == []


def test_owed_exit_for_a_position_that_is_gone_is_dropped_quietly(
        home, monkeypatch, sent, clock):
    exited = feed("EXITED", clock.ago(5))
    state = {"seen": [], "positions": {}, "exits_to_retry": [exited]}
    broker, saved = run_loop(home, monkeypatch, state, [[], []], clock=clock)
    assert broker.calls == [] and saved["exits_to_retry"] == []
    assert exited["event_id"] in saved["seen"]


def test_owed_exit_never_sells_a_position_opened_after_it(home, monkeypatch, sent, clock):
    """The old position is gone, its exit is still owed (the broker errored)
    and a new trade in the same contract is bought: the old exit is not
    for it."""
    old_exit = feed("EXITED", clock.ago(900))
    again = feed("ENTERED", clock.ago(10))

    class ErrorsOnce(Broker):
        errors = 1

        def execute(self, client, cfg, event, state):
            if event["event"] == "EXITED" and self.errors:
                self.errors -= 1
                raise ConnectionError("broker unreachable")
            return super().execute(client, cfg, event, state)
    state = {"seen": [], "positions": {}, "exits_to_retry": [old_exit]}
    broker, saved = run_loop(home, monkeypatch, state, [[again], [again], [again]],
                             clock=clock, broker=ErrorsOnce())
    assert broker.calls == [("buy", again["event_id"])]
    assert list(saved["positions"]) == [KEY] and saved["exits_to_retry"] == []


# ── E4: "watching" starts when the feed can actually be read ────────────────

def test_signals_from_before_feed_access_are_not_flagged_too_old(
        home, monkeypatch, sent, clock):
    """No pass until midday: the morning's entry shows up with the first
    paid poll. It was never catchable — no "too old / computer asleep?"."""
    entered = feed("ENTERED", (START + timedelta(minutes=30)).isoformat())
    broker, _saved = run_loop(home, monkeypatch, {"seen": [], "positions": {}},
                              [[], [], [entered]], clock=clock, steps=(1800, 1800),
                              access=[False, False, True])
    assert broker.calls == [] and rows(home) == [] and sent == []


def test_losing_access_restarts_the_watching_clock(home, monkeypatch, sent, clock):
    """Always-on agent whose pass ran out yesterday, renewed at midday."""
    entered = feed("ENTERED", (START + timedelta(minutes=45)).isoformat())
    late = feed("ENTERED", (START + timedelta(minutes=75)).isoformat(), ticker="QQQ")
    broker, _saved = run_loop(
        home, monkeypatch, {"seen": [], "positions": {}},
        [[], [], [entered], [entered, late]], clock=clock, steps=(1800, 1800, 1800),
        access=[True, False, True, True])
    assert broker.calls == []
    # ...while a signal that is late with access the whole time still is flagged
    assert [r["action"] for r in rows(home)] == ["skip_stale"]
    assert "QQQ" in rows(home)[0]["contract"] and len(sent) == 2


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_agenthc_source_says_whether_the_poll_could_read_entries(monkeypatch):
    monkeypatch.setattr(agenthc, "_now_et",
                        lambda: datetime(2026, 7, 15, 10, 30, tzinfo=agenthc.MARKET_TZ))
    calls = []

    class Http:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            return Resp(200, {"events": [], "pass_exits_only": "exits" in str(headers)})
    monkeypatch.setattr(agenthc, "_http", Http)
    # no key, no day-pass, recurring off: nothing to read with
    assert agenthc.poll({}, {}, lambda s: None) == [] and calls == []
    assert agenthc.had_access() is False
    agenthc.poll({"agenthc_api_key": "k"}, {}, lambda s: None)
    assert agenthc.had_access() is True
    # a used-up prepaid pass still reads, but only exits: no entry access
    tid = agenthc._token_id("exits-token")
    state = {"pass_status": {tid: {"status": "exits_only", "until_ts": 9e12}}}
    agenthc.poll({"feed_pass": {"token": "exits-token"}}, state, lambda s: None)
    assert agenthc.had_access() is False


# ── E2: the `url` source and event times ────────────────────────────────────

def url_poll(monkeypatch, events, now):
    monkeypatch.setattr(generic_json_url.requests, "get",
                        lambda url, headers, timeout: Resp(200, {"events": events}))
    monkeypatch.setattr(generic_json_url, "_utcnow", lambda: now, raising=False)
    return generic_json_url.poll({"source_url": "https://x.example/feed"}, {})


def url_event(event="ENTERED", **over):
    return {"event": event, "ticker": "SPY", "expiry": EXPIRY, "strike": 752,
            "type": "C", "event_id": f"u-{event}", **over}


@pytest.fixture
def url_memory(monkeypatch):
    monkeypatch.setattr(generic_json_url, "_SIGHT", {}, raising=False)


def test_url_feed_time_reaches_the_staleness_check(monkeypatch, url_memory):
    """A laptop waking from sleep must not buy the url feed's hours-old
    entry: the feed's occurred_at was dropped, so nothing was ever too old."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=3)).isoformat()
    naive = (now - timedelta(seconds=20)).replace(tzinfo=None).isoformat()
    stale, fresh = url_poll(monkeypatch, [
        url_event(occurred_at=old), url_event(occurred_at=naive, event_id="u-2")], now)
    assert agent._stale(stale, {}) and not agent._stale(fresh, {})
    assert fresh["occurred_at"].endswith("+00:00")       # no offset given = UTC


def test_url_feed_without_times_is_dated_by_when_the_agent_last_read_it(
        monkeypatch, url_memory):
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(agent, "_WATCH_SINCE", {"main": t0 - timedelta(hours=1)})
    # already listed on the first read: no telling how old — never bought
    (first,) = url_poll(monkeypatch, [url_event()], t0)
    assert first["occurred_at"] is None and agent._stale(first, {})
    assert agent._backlog(first)                         # skipped quietly
    # appeared between two reads 30 s apart: at most 30 s old — bought
    first, new = url_poll(monkeypatch, [url_event(), url_event(event_id="u-new")],
                          t0 + timedelta(seconds=30))
    assert new["occurred_at"] == t0.isoformat() and "age_unknown" not in new
    assert agent._stale(first, {})
    # the computer slept for two hours: what's new now may be two hours old
    woke = datetime.now(timezone.utc)
    _f, new, later = url_poll(monkeypatch, [url_event(), url_event(event_id="u-new"),
                                           url_event(event_id="u-later")], woke)
    assert new["occurred_at"] == t0.isoformat()          # keeps its first date
    assert later["occurred_at"] == (t0 + timedelta(seconds=30)).isoformat()
    assert agent._stale(later, {}) and not agent._backlog(later)
    # exits are never blocked for their age, known or not
    (out,) = url_poll(monkeypatch, [url_event("EXITED")], woke)
    assert not agent._stale(out, {})


def test_manual_commands_are_still_run_whenever_the_agent_next_reads_them(home):
    from sources import manual_file
    (home / "commands.jsonl").write_text(
        '{"action": "enter", "ticker": "SPY", "expiry": "2099-12-18", '
        '"strike": 752, "type": "C"}\n', encoding="utf-8")
    (ev,) = manual_file.poll({}, {})
    assert not agent._stale(ev, {})


# ── E5: the close on an early-close day ─────────────────────────────────────

def test_past_expiry_close_follows_early_closes(monkeypatch):
    """The day after Thanksgiving closes at 1:00 PM ET, not 4:00 PM."""
    key = "SPY|2026-11-27|600.0|C"
    at = {}
    monkeypatch.setattr(agent, "_et_now", lambda: datetime(
        2026, 11, 27, at["h"], at["m"], tzinfo=agent.MARKET_TZ))
    at.update(h=12, m=59)
    assert not agent._past_expiry_close(key)
    at.update(h=13, m=0)
    assert agent._past_expiry_close(key)
    # an ordinary day: 4:00 PM, or the owner's own early_close_days
    monkeypatch.setattr(agent, "_et_now", lambda: datetime(
        2026, 11, 20, at["h"], at["m"], tzinfo=agent.MARKET_TZ))
    key = "SPY|2026-11-20|600.0|C"
    at.update(h=15, m=59)
    assert not agent._past_expiry_close(key)
    assert not agent._past_expiry_close(key, {"early_close_days": ["2026-11-19"]})
    assert agent._past_expiry_close(key, {"early_close_days": ["2026-11-20"]})
    at.update(h=16, m=0)
    assert agent._past_expiry_close(key)
    assert agent._past_expiry_close("SPY|2026-11-19|600.0|C")      # yesterday's
