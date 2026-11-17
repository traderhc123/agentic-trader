"""Feed reads keep exit signals flowing (audit PR2).

- "Other trades" switched off while one is held: its EXITED must still be
  read (track=all while the ledger holds a non-main row), and new
  other-trades entries are still not taken.
- The window: track=all, or a read after a gap, asks for FEED_LIMIT_MAX
  events; a full window that may have dropped events keeps the no-signal
  sell armed for what was held (cause "feed_gap").
- An entry's age is measured with AgentHC's clock (body["timestamp"]),
  corrected at most CLOCK_SKEW_MAX_S for a fast PC clock.

The fake server mirrors AgentHC's read_feed: newest first, filtered by
track, cut at `limit`."""
import time
from datetime import datetime, timedelta, timezone

import pytest

import agent
from conftest import StubBroker
from sources import agenthc_day_trade_ideas as hc

FAR = "2099-12-18"
MAX = getattr(hc, "FEED_LIMIT_MAX", 100)   # (literal on a version without it)


def _iso(t):
    return t.isoformat(timespec="seconds")


def ev(event, ticker, strike, track, at):
    return {"event": event, "ticker": ticker, "expiry": FAR, "strike": strike,
            "type": "C", "track": track, "occurred_at": _iso(at)}


class Server:
    def __init__(self, clock=None):
        self.lines = []          # oldest first, like the journal file
        self.asked = []
        self.clock = clock       # server "now" sent as body["timestamp"]

    def get(self, url, params=None, headers=None, **kw):
        params = params or {}
        self.asked.append(dict(params))
        track, limit = params.get("track", "main"), int(params.get("limit", 50))
        out = []
        for e in reversed(self.lines):
            if track != "all" and e.get("track", "main") != track:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        body = {"events": out}
        if self.clock is not None:
            body["timestamp"] = _iso(self.clock())

        class R:
            status_code = 200
            content = b"{}"
            text = "{}"

            def json(s):
                return body

            def raise_for_status(s):
                pass
        return R()


@pytest.fixture
def told(monkeypatch):
    import notifications
    sent = []
    monkeypatch.setattr(notifications, "notify", lambda cfg, text, *a, **k: sent.append(text))
    monkeypatch.setattr(agent, "notify", lambda cfg, text, *a, **k: sent.append(text))
    return sent


def _loop(cfg, state, broker, srv, monkeypatch):
    monkeypatch.setattr(hc.requests, "get", srv.get)
    events = hc.poll(cfg, state)
    agent._handle_feed_events(events, cfg, state, broker, None, lambda s: None)
    return events


def _setup(monkeypatch, other=True):
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    monkeypatch.setattr(hc, "_SKEW_WARNED", {"done": False}, raising=False)
    cfg = {"source": "agenthc", "agenthc_api_key": "k", "dry_run": False,
           "broker": "stub", "max_entries_per_day": 50, "max_open_positions": 50,
           "include_other_trades": other}
    return cfg, {"seen": [], "positions": {}}


# ── "other trades" off while one is held ────────────────────────────────────

def test_other_trades_off_still_reads_held_other_trade_exit(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    srv.lines.append(ev("ENTERED", "WULF", 15.0, "other", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    key = f"WULF|{FAR}|15.0|C"
    assert key in state["positions"]
    cfg["include_other_trades"] = False          # owner: other trades off
    # a new other-trades entry and the held one's exit
    srv.lines.append(ev("ENTERED", "RIOT", 9.0, "other", t0 + timedelta(seconds=20)))
    srv.lines.append(ev("EXITED", "WULF", 15.0, "other", t0 + timedelta(seconds=30)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    assert key not in state["positions"]         # sold on its exit signal
    assert [c["ticker"] for c in b.calls] == ["WULF", "WULF"]   # RIOT never bought
    assert f"RIOT|{FAR}|9.0|C" not in state["positions"]
    # nothing non-main held any more: back to the main pick only
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "main"


def test_other_trades_off_drops_other_entries_once_and_keeps_main(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch, other=False)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [
        ev("ENTERED", "RIOT", 9.0, "other", t0),
        ev("ENTERED", "SPY", 752.0, "main", t0)]})
    b = StubBroker()
    agent._handle_feed_events(events, cfg, state, b, None, lambda s: None)
    assert [c["ticker"] for c in b.calls] == ["SPY"]
    riot = next(e["event_id"] for e in events if e["ticker"] == "RIOT")
    assert riot in state["seen"]                 # not looked at again
    # other trades on: an other-trades entry reaches handle_event as before
    cfg2, state2 = _setup(monkeypatch, other=True)
    b2 = StubBroker()
    agent._handle_feed_events([e for e in events if e["ticker"] == "RIOT"],
                              cfg2, state2, b2, None, lambda s: None)
    assert [c["ticker"] for c in b2.calls] == ["RIOT"]


def test_new_rows_remember_their_track(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [
        ev("ENTERED", "RIOT", 9.0, "other", t0),
        ev("ENTERED", "SPY", 752.0, "main", t0)]})
    agent._handle_feed_events(events, cfg, state, StubBroker(), None, lambda s: None)
    assert state["positions"][f"RIOT|{FAR}|9.0|C"]["track"] == "other"
    assert state["positions"][f"SPY|{FAR}|752.0|C"]["track"] == "main"


def test_track_follows_what_the_ledger_holds(monkeypatch):
    cfg = {"include_other_trades": False}
    p = lambda st: hc._feed_params(cfg, st)["track"]
    assert p({"positions": {}}) == "main"
    # a live row from the main pick: its opening event id has no track suffix
    main_id = f"ENTERED|SPY|{FAR}|752.0|C|2026-11-17T15:00:00+00:00"
    assert p({"positions": {"a": {"opened_event": main_id, "qty": 1}}}) == "main"
    assert p({"positions": {"a": {"track": "main", "qty": 1}}}) == "main"
    # a live row from other trades (id ends in |other), a stamped row, and
    # an exit still owed for one: read every track
    assert p({"positions": {"a": {"opened_event": main_id + "|other"}}}) == "all"
    assert p({"positions": {"a": {"track": "other"}}}) == "all"
    assert p({"positions": {}, "exits_to_retry": [{"event": "EXITED", "track": "other"}]}) == "all"
    # a row that doesn't say (a practice row of an older version, a rebuilt
    # row with no opening event) counts as the main pick: main only
    assert p({"positions": {"a": {"option_id": "dry", "qty": 1, "dry": True}}}) == "main"
    assert p({"positions": {"a": {"option_id": "x", "qty": 1, "opened_event": None}}}) == "main"
    assert hc._feed_params({"include_other_trades": True}, {"positions": {}})["track"] == "all"


def test_other_trades_off_shadow_exit_never_sells_the_main_position(home, monkeypatch, no_policy, told):
    # review: with other trades off the agent reads every track while it
    # holds an other-trades row; a shadow-book EXITED for the contract the
    # MAIN pick holds (positions are keyed by contract alone) sold it
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    srv.lines.append(ev("ENTERED", "SPY", 700.0, "main", t0))
    srv.lines.append(ev("ENTERED", "WULF", 15.0, "other", t0 + timedelta(seconds=1)))
    _loop(cfg, state, b, srv, monkeypatch)
    spy, wulf = f"SPY|{FAR}|700.0|C", f"WULF|{FAR}|15.0|C"
    assert spy in state["positions"] and wulf in state["positions"]
    cfg["include_other_trades"] = False
    srv.lines.append(ev("EXITED", "SPY", 700.0, "other", t0 + timedelta(seconds=30)))
    srv.lines.append(ev("EXITED", "WULF", 15.0, "other", t0 + timedelta(seconds=40)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    assert spy in state["positions"]               # the shadow exit isn't its exit
    assert wulf not in state["positions"]          # the held other trade's is
    assert [c["ticker"] for c in b.calls].count("SPY") == 1
    shadow = next(e["event_id"] for e in hc._parse_events({"events": srv.lines})
                  if e["ticker"] == "SPY" and e["event"] == "EXITED")
    assert shadow in state["seen"]                 # not looked at again
    # the main pick's own exit still sells it
    srv.lines.append(ev("EXITED", "SPY", 700.0, "main", t0 + timedelta(seconds=50)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert spy not in state["positions"]


def test_other_trades_off_untracked_row_reads_main_and_ignores_shadow_exit(home, monkeypatch, no_policy, told):
    # a practice row of an older version says neither track nor opening
    # event: it counts as the main pick (reads stay track=main, limit 20
    # once read recently) and a shadow-book exit for its contract is not
    # its exit
    cfg, state = _setup(monkeypatch, other=False)
    cfg["dry_run"] = True
    key = f"SPY|{FAR}|700.0|C"
    state["positions"][key] = {"option_id": "dry", "qty": 1, "dry": True}
    state["feed_read"] = {"local": time.time() - 10, "server": None}
    assert hc._feed_params(cfg, state) == {"limit": hc.FEED_LIMIT, "track": "main"}
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [ev("EXITED", "SPY", 700.0, "other", t0)]})
    agent._handle_feed_events(events, cfg, state, StubBroker(), None, lambda s: None)
    assert key in state["positions"]


class OneRowBroker(StubBroker):
    """Like the real adapters: an ENTERED for a contract the ledger already
    holds buys nothing."""

    def execute(self, client, cfg, event, state):
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        if event["event"] == "ENTERED" and key in state["positions"]:
            self.calls.append(event)
            return False
        return super().execute(client, cfg, event, state)


def test_other_trades_off_held_other_row_waits_for_its_own_exit_not_main(
        home, monkeypatch, no_policy, told):
    # per-book exits (#91) + this PR: the main pick and other trades signal
    # the SAME contract; the row is the other-trades buy. With other trades
    # then switched off, the main pick's exit must not sell it (per-book),
    # and its own other-trades exit must still reach it (track=all).
    cfg, state = _setup(monkeypatch)
    b, srv = OneRowBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    key = f"SPY|{FAR}|700.0|C"
    srv.lines.append(ev("ENTERED", "SPY", 700.0, "other", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    # the main pick enters the same contract later: it buys nothing
    srv.lines.append(ev("ENTERED", "SPY", 700.0, "main", t0 + timedelta(seconds=5)))
    _loop(cfg, state, b, srv, monkeypatch)
    row = state["positions"][key]
    assert row["book"] == "shadow" and row["track"] == "other"
    cfg["include_other_trades"] = False          # owner: other trades off
    srv.lines.append(ev("EXITED", "SPY", 700.0, "main", t0 + timedelta(seconds=30)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    assert key in state["positions"]             # the main exit isn't its exit
    assert not [c for c in b.calls if c["event"] == "EXITED"]
    assert any("no order placed" in t for t in told)
    srv.lines.append(ev("EXITED", "SPY", 700.0, "other", t0 + timedelta(seconds=40)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    assert key not in state["positions"]         # sold on its own exit
    (sold,) = [c for c in b.calls if c["event"] == "EXITED"]
    assert sold["track"] == "other"
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "main"      # nothing other held any more


def test_other_trades_off_shadow_exit_does_not_skip_a_main_entry(home, monkeypatch, no_policy, told):
    # review: other-track exits were "known" exits, so a main ENTERED was
    # skipped as already over because the shadow book exited the contract
    cfg, state = _setup(monkeypatch, other=False)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=40)
    events = hc._parse_events({"events": [
        ev("EXITED", "SPY", 700.0, "other", t0 + timedelta(seconds=20)),
        ev("ENTERED", "SPY", 700.0, "main", t0)]})
    b = StubBroker()
    agent._handle_feed_events(events, cfg, state, b, None, lambda s: None)
    assert f"SPY|{FAR}|700.0|C" in state["positions"]
    assert [c["ticker"] for c in b.calls] == ["SPY"]


# ── the window ───────────────────────────────────────────────────────────────

def test_other_trades_on_reads_enough_to_keep_a_main_exit(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    srv.lines.append(ev("ENTERED", "SPY", 752.0, "main", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    key = f"SPY|{FAR}|752.0|C"
    assert key in state["positions"]
    # offline a while: the main exit, then 20 shadow-book events
    srv.lines.append(ev("EXITED", "SPY", 752.0, "main", t0 + timedelta(minutes=2)))
    for i in range(20):
        srv.lines.append(ev("ENTERED" if i % 2 == 0 else "EXITED", f"T{i // 2}",
                            10.0 + i // 2, "other", t0 + timedelta(minutes=3 + i * 0.5)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert key not in state["positions"]          # the main exit was read
    assert hc.had_exit_access() is True
    assert srv.asked[-1] == {"limit": MAX, "track": "all"}


def test_limit_main_only_is_small_unless_after_a_gap(monkeypatch):
    cfg = {"include_other_trades": False}
    st = {"positions": {}}
    assert hc._feed_params(cfg, st)["limit"] == hc.FEED_LIMIT_MAX   # no read yet
    st["feed_read"] = {"local": time.time() - 10, "server": None}
    assert hc._feed_params(cfg, st) == {"limit": hc.FEED_LIMIT, "track": "main"}
    st["feed_read"] = {"local": time.time() - hc.FEED_GAP_S - 5, "server": None}
    assert hc._feed_params(cfg, st)["limit"] == hc.FEED_LIMIT_MAX


def _flood(srv, start, n):
    for i in range(n):
        srv.lines.append(ev("ENTERED" if i % 2 == 0 else "EXITED", f"X{i // 2}",
                            20.0 + i // 2, "other", start + timedelta(seconds=i)))


def test_full_window_after_a_gap_keeps_the_no_signal_sell_armed(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    srv.lines.append(ev("ENTERED", "SPY", 752.0, "main", datetime.now(timezone.utc)))
    _loop(cfg, state, b, srv, monkeypatch)
    key = f"SPY|{FAR}|752.0|C"
    assert key in state["positions"] and hc.had_exit_access()
    # the last good read was hours ago; since then more events than one
    # read lists (the SPY exit, if any, scrolled out)
    state["feed_read"] = {"local": t0.timestamp(), "server": t0.timestamp()}
    _flood(srv, t0 + timedelta(minutes=5), MAX + 5)
    _loop(cfg, state, b, srv, monkeypatch)
    assert hc.had_exit_access() is False
    assert hc.exit_loss_reason() == "feed_gap"
    assert state["feed_gap"]["keys"] == [key]
    assert any("may have been missed" in t for t in told)
    # the next read overlaps the last one, but SPY is still held: still armed
    n = len(told)
    _loop(cfg, state, b, srv, monkeypatch)
    assert hc.had_exit_access() is False and len(told) == n   # told once
    # sold (here: its exit arrives) -> exit access is back, the record goes
    srv.lines.append(ev("EXITED", "SPY", 752.0, "main", datetime.now(timezone.utc)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert key not in state["positions"]
    _loop(cfg, state, b, srv, monkeypatch)
    assert hc.had_exit_access() is True and "feed_gap" not in state


def test_full_window_that_overlaps_the_last_read_is_no_gap(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    now = datetime.now(timezone.utc)
    _flood(srv, now - timedelta(minutes=30), MAX + 5)
    state["positions"]["SPY|x"] = {"option_id": "stub", "qty": 1, "track": "main"}
    state["feed_read"] = {"local": time.time() - 60,
                          "server": (now - timedelta(minutes=20)).timestamp()}
    _loop(cfg, state, b, srv, monkeypatch)
    assert hc.had_exit_access() is True and "feed_gap" not in state
    assert not any("may have been missed" in t for t in told)


def test_no_previous_read_full_window_with_nothing_seen_is_a_gap(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch)
    b, srv = StubBroker(), Server()
    _flood(srv, datetime.now(timezone.utc) - timedelta(minutes=30), MAX)
    state["positions"]["SPY|x"] = {"option_id": "stub", "qty": 1, "track": "main"}
    _loop(cfg, state, b, srv, monkeypatch)
    assert hc.exit_loss_reason() == "feed_gap"
    # ... but not when the window lists something the agent already handled
    cfg2, state2 = _setup(monkeypatch)
    state2["positions"]["SPY|x"] = {"option_id": "stub", "qty": 1, "track": "main"}
    oldest = hc._parse_events({"events": [srv.lines[0]]})[0]["event_id"]
    state2["seen"] = [oldest]
    _loop(cfg2, state2, StubBroker(), srv, monkeypatch)
    assert hc.had_exit_access() is True and "feed_gap" not in state2


def test_first_read_of_every_track_is_no_gap_by_itself(home, monkeypatch, no_policy, told):
    # review: the first read after the upgrade has no record of the last
    # read; the agent's seen ids are main-pick ones (it read track=main),
    # and the newest MAX events of every track may list no main event at
    # all: that is no sign anything was missed
    cfg, state = _setup(monkeypatch, other=False)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    old_main = ev("ENTERED", "QQQ", 600.0, "main", t0 - timedelta(hours=1))
    srv.lines.append(old_main)
    _flood(srv, t0, MAX + 5)
    state["seen"] = [hc._parse_events({"events": [old_main]})[0]["event_id"]]
    state["positions"][f"WULF|{FAR}|15.0|C"] = {"option_id": "stub", "qty": 1,
                                                "track": "other"}
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1] == {"limit": MAX, "track": "all"}
    assert hc.had_exit_access() is True and "feed_gap" not in state
    assert not any("may have been missed" in t for t in told)


def test_feed_gap_sells_only_what_was_held_at_the_gap(monkeypatch):
    # review: a feed_gap sold every row at the no-signal sell time,
    # positions bought after the gap (whose exits arrive normally) too
    a, b_ = f"SPY|{FAR}|700.0|C", f"QQQ|{FAR}|600.0|C"
    state = {"positions": {
        a: {"option_id": "x", "qty": 1, "opened_event": "E1", "live": True},
        b_: {"option_id": "y", "qty": 1, "opened_event": "E2", "live": True}},
        "signal_loss": {"since": time.time() - 600, "cause": "feed_gap"},
        "feed_gap": {"since": time.time() - 600, "keys": [a], "opened": {a: "E1"}}}
    assert [k for k, _r in agent._signal_loss_rows(state)] == [a]
    # another cause: every row, as before
    state["signal_loss"]["cause"] = "pass_ended"
    assert sorted(k for k, _r in agent._signal_loss_rows(state)) == sorted([a, b_])
    # the gap row sold and bought again under the same contract: not the
    # position that may have missed its exit -> the gap ends
    state["signal_loss"]["cause"] = "feed_gap"
    state["positions"][a] = {"option_id": "x", "qty": 1, "opened_event": "E3"}
    hc._LAST_POLL.update(exits=True, why="")
    hc._check_window({}, state, lambda s: None, {"events": []},
                     {"limit": hc.FEED_LIMIT}, [])
    assert "feed_gap" not in state and hc.had_exit_access() is True
    assert agent._signal_loss_rows(state) == []


def test_feed_gap_cause_has_its_own_words():
    assert "scrolled out" in agent._signal_loss_why("feed_gap")


# ── clock skew ───────────────────────────────────────────────────────────────

def _fast_clock(monkeypatch, skew):
    real = datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + skew
    monkeypatch.setattr(agent, "datetime", Shifted)
    monkeypatch.setattr(hc.time, "time", lambda: time.time_ns() / 1e9 + skew.total_seconds())


def test_fast_pc_clock_does_not_make_fresh_entries_stale(monkeypatch):
    monkeypatch.setattr(hc, "_SKEW_WARNED", {"done": False}, raising=False)
    server_now = datetime.now(timezone.utc)
    _fast_clock(monkeypatch, timedelta(minutes=6))
    body = {"timestamp": _iso(server_now), "events": [
        ev("ENTERED", "SPY", 752.0, "main", server_now - timedelta(seconds=5)),
        ev("ENTERED", "QQQ", 600.0, "main", server_now - timedelta(minutes=10))]}
    fresh, old = hc._parse_events(body)
    cfg = {"max_event_age_s": 300}
    assert agent._stale(fresh, cfg) is False
    assert agent._stale(old, cfg) is True          # still too old by AgentHC's clock
    assert 355 <= fresh["clock_skew_s"] <= 365


def test_no_server_time_falls_back_to_the_local_clock(monkeypatch):
    server_now = datetime.now(timezone.utc)
    _fast_clock(monkeypatch, timedelta(minutes=6))
    (e,) = hc._parse_events({"events": [
        ev("ENTERED", "SPY", 752.0, "main", server_now - timedelta(seconds=5))]})
    assert "clock_skew_s" not in e
    assert agent._stale(e, {"max_event_age_s": 300}) is True


def test_fast_clock_correction_is_capped(monkeypatch):
    monkeypatch.setattr(hc, "_SKEW_WARNED", {"done": False}, raising=False)
    server_now = datetime.now(timezone.utc)
    _fast_clock(monkeypatch, timedelta(hours=1))
    (e,) = hc._parse_events({"timestamp": _iso(server_now), "events": [
        ev("ENTERED", "SPY", 752.0, "main", server_now - timedelta(seconds=5))]})
    assert e["clock_skew_s"] == hc.CLOCK_SKEW_MAX_S
    assert agent._stale(e, {"max_event_age_s": 300}) is True   # fails closed
    # a skew sent by anything else is capped by the agent too
    forged = dict(e, clock_skew_s=10 ** 6)
    assert agent._stale(forged, {"max_event_age_s": 300}) is True


def test_slow_pc_clock_does_not_let_old_entries_through(monkeypatch):
    monkeypatch.setattr(hc, "_SKEW_WARNED", {"done": False}, raising=False)
    server_now = datetime.now(timezone.utc)
    _fast_clock(monkeypatch, -timedelta(minutes=30))
    (e,) = hc._parse_events({"timestamp": _iso(server_now), "events": [
        ev("ENTERED", "SPY", 752.0, "main", server_now - timedelta(minutes=10))]})
    assert agent._stale(e, {"max_event_age_s": 300}) is True
    assert e["clock_skew_s"] < -1700
