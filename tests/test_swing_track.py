"""Swing trades: a third category of AgentHC signals (track "swing").

The feed has the main pick, "other trades" (the shadow journal) and swing
trades: rare multi-day calls after a VIX spike, held up to
SWING_HOLD_SESSIONS trading sessions and closed on their own EXITED event.
On this side:
  - include_swing_trades, OFF by default; only the owner turns it on (the
    dashboard, the chat, the terminal setup) — an assistant's setup plan can
    leave it off or turn it off, never on;
  - a swing row is its own book ("swing"): only a swing exit sells it, and a
    swing exit never sells a main-pick or "other trades" row;
  - turned off while one is held, its exit still reaches it;
  - multi-day holds: the no-signal sell leaves a swing row alone until the
    last session of its hold; after a prepaid pass has ended, its swing
    exits are still read (mode=off: never charged, nothing bought);
  - sized like the main pick; the dashboard shows it as "Swing Trade",
    with its positions and P&L apart; the practice preview and practice
    mode include it only when it is on.
Zero network."""
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import agent
import dry_run_preview
import setup_plan
import view_model as vm
from conftest import StubBroker
from sources import agenthc_day_trade_ideas as hc
from test_v2_settings_api import app  # noqa: F401  (fixture)

FAR = "2099-12-18"
ET = agent.MARKET_TZ


def _iso(t):
    return t.isoformat(timespec="seconds")


def ev(event, ticker, strike, track, at):
    return {"event": event, "ticker": ticker, "expiry": FAR, "strike": strike,
            "type": "C", "track": track, "occurred_at": _iso(at)}


def key(ticker, strike):
    return f"{ticker}|{FAR}|{float(strike)}|C"


class Server:
    """AgentHC's read_feed: newest first, filtered by track, cut at limit;
    /pass answers `pass_status`. Every read's params and headers are kept."""

    def __init__(self, pass_status="active", feed_status=200):
        self.lines, self.asked = [], []
        self.pass_status, self.feed_status = pass_status, feed_status

    def get(self, url, params=None, headers=None, **kw):
        params = params or {}
        body = None
        if url.endswith("/pass"):
            body = {"status": self.pass_status,
                    "access_until": "2020-01-01T00:00:00+00:00",
                    "trading_days_left": 0}
            status = 200
        else:
            self.asked.append((dict(params), dict(headers or {})))
            track, limit = params.get("track", "main"), int(params.get("limit", 50))
            out = []
            for e in reversed(self.lines):
                if track != "all" and e.get("track", "main") != track:
                    continue
                # swing events are in track=all only for a read that asks
                # (an agent from before swing trades never does)
                if (track == "all" and e.get("track") == "swing"
                        and params.get(hc.SWING_PARAM) != "1"):
                    continue
                out.append(e)
                if len(out) >= limit:
                    break
            body = {"events": out}
            status = self.feed_status

        class R:
            status_code = status
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


def _setup(monkeypatch, swing=True, other=False):
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    monkeypatch.setattr(hc, "_SKEW_WARNED", {"done": False}, raising=False)
    cfg = {"source": "agenthc", "agenthc_api_key": "k", "dry_run": False,
           "broker": "stub", "max_entries_per_day": 50, "max_open_positions": 50,
           "include_other_trades": other, "include_swing_trades": swing}
    return cfg, {"seen": [], "positions": {}}


def _loop(cfg, state, broker, srv, monkeypatch):
    monkeypatch.setattr(hc.requests, "get", srv.get)
    events = hc.poll(cfg, state)
    agent._handle_feed_events(events, cfg, state, broker, None, lambda s: None)
    return events


# ── the switch: off by default, reads, drops ────────────────────────────────

def test_swing_trades_are_off_by_default_and_never_bought_then(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch, swing=None)
    cfg.pop("include_swing_trades")
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    srv.lines += [ev("ENTERED", "SPY", 680, "swing", t0),
                  ev("ENTERED", "QQQ", 600, "main", t0)]
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1][0]["track"] == "main"        # main pick only: as before
    assert [c["ticker"] for c in b.calls] == ["QQQ"]
    # a feed that lists every track anyway: the swing entry is dropped, once
    srv.lines.append(ev("ENTERED", "IWM", 230, "swing", t0))
    events = hc._parse_events({"events": [ev("ENTERED", "IWM", 230, "swing", t0)]})
    agent._handle_feed_events(events, cfg, state, b, None, lambda s: None)
    assert [c["ticker"] for c in b.calls] == ["QQQ"]
    assert events[0]["event_id"] in state["seen"]


def test_swing_on_reads_every_track_and_takes_swing_but_not_other(home, monkeypatch, no_policy, told, capsys):
    cfg, state = _setup(monkeypatch, swing=True, other=False)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    srv.lines += [ev("ENTERED", "SPY", 680, "swing", t0),
                  ev("ENTERED", "RIOT", 9, "other", t0),
                  ev("ENTERED", "QQQ", 640, "main", t0)]
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1][0]["track"] == "all"
    assert srv.asked[-1][0]["limit"] == hc.FEED_LIMIT_MAX
    assert sorted(c["ticker"] for c in b.calls) == ["QQQ", "SPY"]
    row = state["positions"][key("SPY", 680)]
    assert row["book"] == "swing" and row["track"] == "swing"
    assert hc.held_as_swing(row) and hc.held_as_other(row)
    assert 'ignored 1 "other trades" signal(s)' in capsys.readouterr().out


def test_the_swing_event_id_names_its_track():
    t0 = datetime(2026, 11, 23, 14, 35, tzinfo=timezone.utc)
    (e,) = hc._parse_events({"events": [ev("ENTERED", "SPY", 680, "swing", t0)]})
    assert e["track"] == "swing" and e["event_id"].endswith("|swing")
    assert hc._id_track(e["event_id"]) == "swing"


# ── per-book exits: swing is a book of its own ──────────────────────────────

def _hold(state, k, book, track=None, opened=None, live=True):
    state["positions"][k] = {"option_id": "o", "qty": 1, "live": live,
                             "book": book, "opened_at": opened or time.time(),
                             **({"track": track} if track else {})}


def test_only_a_swing_exit_sells_a_swing_row(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch, swing=True, other=True)
    b = StubBroker()
    t0 = datetime.now(timezone.utc)
    _hold(state, key("SPY", 680), "swing", "swing")
    for track in ("main", "other"):
        e = hc._parse_events({"events": [ev("EXITED", "SPY", 680, track, t0)]})
        agent._handle_feed_events(e, cfg, state, b, None, lambda s: None)
        assert key("SPY", 680) in state["positions"], track
    assert b.calls == []
    e = hc._parse_events({"events": [ev("EXITED", "SPY", 680, "swing", t0)]})
    agent._handle_feed_events(e, cfg, state, b, None, lambda s: None)
    assert key("SPY", 680) not in state["positions"]
    assert len(b.calls) == 1


@pytest.mark.parametrize("book,track", [("main", None), ("shadow", "other"),
                                        ("shadow", None)])
def test_a_swing_exit_never_sells_another_books_row(home, monkeypatch, no_policy, told, book, track):
    cfg, state = _setup(monkeypatch, swing=True, other=True)
    b = StubBroker()
    _hold(state, key("SPY", 680), book, track)
    e = hc._parse_events({"events": [ev("EXITED", "SPY", 680, "swing",
                                        datetime.now(timezone.utc))]})
    agent._handle_feed_events(e, cfg, state, b, None, lambda s: None)
    assert key("SPY", 680) in state["positions"] and b.calls == []


def test_swing_turned_off_still_sells_the_held_swing_row_on_its_exit(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch, swing=True)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=40)
    srv.lines.append(ev("ENTERED", "SPY", 680, "swing", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    assert key("SPY", 680) in state["positions"]
    cfg["include_swing_trades"] = False            # owner: swing trades off
    srv.lines += [ev("ENTERED", "QQQ", 600, "swing", t0 + timedelta(seconds=10)),
                  ev("EXITED", "SPY", 680, "swing", t0 + timedelta(seconds=20))]
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1][0]["track"] == "all"      # still held: every track
    assert key("SPY", 680) not in state["positions"]
    assert [c["ticker"] for c in b.calls] == ["SPY", "SPY"]   # QQQ never bought
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1][0]["track"] == "main"     # nothing left: main only


def test_with_swing_off_an_other_trades_row_still_gets_its_exit(home, monkeypatch, no_policy, told):
    """Both categories off, an "other trades" row held: its exit sells it,
    a swing exit for the same contract does not."""
    cfg, state = _setup(monkeypatch, swing=False, other=False)
    b = StubBroker()
    _hold(state, key("SPY", 680), "shadow", "other")
    t0 = datetime.now(timezone.utc)
    e = hc._parse_events({"events": [ev("EXITED", "SPY", 680, "swing", t0)]})
    agent._handle_feed_events(e, cfg, state, b, None, lambda s: None)
    assert key("SPY", 680) in state["positions"]
    e = hc._parse_events({"events": [ev("EXITED", "SPY", 680, "other", t0)]})
    agent._handle_feed_events(e, cfg, state, b, None, lambda s: None)
    assert key("SPY", 680) not in state["positions"]


def test_swing_trades_are_sized_like_the_main_pick():
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
           "other_sizing_mode": "budget", "other_budget_per_trade_usd": 100}
    assert agent._sizing_cfg(cfg, {"track": "swing"}) is cfg
    assert agent._sizing_cfg(cfg, {"track": "other"})["budget_per_trade_usd"] == 100


def test_swing_rows_log_their_book_for_the_dashboard(home, monkeypatch, no_policy, told):
    cfg, state = _setup(monkeypatch, swing=True)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    srv.lines.append(ev("ENTERED", "SPY", 680, "swing", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    srv.lines.append(ev("EXITED", "SPY", 680, "swing", t0 + timedelta(seconds=5)))
    _loop(cfg, state, b, srv, monkeypatch)
    rows = agent._read_trade_log()
    books = [(r["action"], r["ui"]["book"]) for r in rows if r.get("action") in ("entry", "exit")]
    assert books == [("entry", "swing"), ("exit", "swing")]
    act = vm.activity_page(rows)
    assert {r["book"] for r in act["rows"]} == {"swing"}
    assert vm.validate(act, "ActivityPage") == []


# ── multi-day holds: the no-signal sell ─────────────────────────────────────

def _et(d, hh=15, mm=31):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


def test_sessions_held_counts_trading_days_only():
    # bought Mon 11-23-2026; Thanksgiving (Thu 11-26) is no session
    opened = _et(date(2026, 11, 23), 9, 35).timestamp()
    row = {"opened_at": opened, "book": "swing"}
    held = {d: agent._sessions_held({}, row, date(2026, 11, d)) for d in (23, 25, 27, 28, 30)}
    assert held == {23: 1, 25: 3, 27: 4, 28: 4, 30: 5}
    assert not agent._swing_hold_over({}, row, date(2026, 11, 27))
    assert agent._swing_hold_over({}, row, date(2026, 11, 30))
    # a row that doesn't say when it was opened: the usual safety net applies
    assert agent._swing_hold_over({}, {"book": "swing"}, date(2026, 11, 23))


def test_the_no_signal_sell_leaves_a_swing_row_alone_until_its_last_session():
    opened = _et(date(2026, 11, 23), 9, 35).timestamp()
    state = {"positions": {}, "signal_loss": {"since": opened + 600}}
    _hold(state, key("SPY", 680), "swing", "swing", opened)
    _hold(state, key("QQQ", 640), "main", None, opened)
    day1 = _et(date(2026, 11, 23))
    assert [k for k, _r in agent._signal_loss_rows(state, {}, day1)] == [key("QQQ", 640)]
    # only the swing row: nothing to sell today, no sell time
    state["positions"].pop(key("QQQ", 640))
    assert agent._signal_loss_rows(state, {}, day1) == []
    assert agent.signal_loss_sell_at({}, state, now_et=_et(date(2026, 11, 23), 14, 0)) is None
    assert agent.signal_loss_banner({}, {**state, "signal_loss": {
        **state["signal_loss"], "warned_day": "2026-11-23"}}, now_et=day1) == []
    # the last session of the hold (AgentHC's own time exit is that day)
    day5 = _et(date(2026, 11, 30))
    assert [k for k, _r in agent._signal_loss_rows(state, {}, day5)] == [key("SPY", 680)]


def test_a_swing_row_is_never_sold_by_the_no_signal_sell_on_day_one(home, told):
    opened = _et(date(2026, 11, 23), 9, 35).timestamp()
    state = {"positions": {}, "signal_loss": {"since": opened + 600,
                                              "warned_at": opened + 900,
                                              "warned_day": "2026-11-23"}}
    _hold(state, key("SPY", 680), "swing", "swing", opened)
    sold = agent.signal_loss_tick({}, state, lambda s: None, StubBroker(), None,
                                  now_et=_et(date(2026, 11, 23), 15, 45))
    assert sold == [] and key("SPY", 680) in state["positions"]


def test_a_practice_swing_row_knows_when_it_opened(home, monkeypatch, no_policy, told):
    # review: practice rows had no opened_at, so a practice swing row counted
    # as "hold over" and the no-signal sell could sell it on day 1
    cfg = {"source": "agenthc", "dry_run": True, "include_swing_trades": True,
           "max_entries_per_day": 50, "max_open_positions": 50}
    state = {"seen": [], "positions": {}}
    t0 = (datetime.now(timezone.utc) - timedelta(minutes=3)).replace(microsecond=0)
    events = hc._parse_events({"events": [ev("ENTERED", "SPY", 680, "swing", t0)]})
    agent._handle_feed_events(events, cfg, state, StubBroker(), None, lambda s: None)
    row = state["positions"][key("SPY", 680)]
    assert row["dry"] is True and row["book"] == "swing"
    assert row["opened_at"] == t0.timestamp()
    # a signal time in the future (a skewed clock): now instead
    assert agent._dry_opened_at({"occurred_at": _iso(t0 + timedelta(days=1))}) <= time.time()


def test_a_practice_swing_row_is_not_sold_by_the_no_signal_sell_on_day_one(home, told):
    opened = _et(date(2026, 11, 23), 9, 35).timestamp()
    state = {"positions": {key("SPY", 680): {
        "option_id": "dry", "qty": 1, "dry": True, "book": "swing",
        "track": "swing", "opened_at": opened}},
        "signal_loss": {"since": opened + 600}}
    sold = agent.signal_loss_tick({}, state, lambda s: None, StubBroker(), None,
                                  now_et=_et(date(2026, 11, 23), 15, 45))
    assert sold == [] and key("SPY", 680) in state["positions"]
    # the last session of its hold: sold like any practice row
    sold = agent.signal_loss_tick({}, state, lambda s: None, StubBroker(), None,
                                  now_et=_et(date(2026, 11, 30), 15, 45))
    assert key("SPY", 680) not in state["positions"]


def test_a_row_without_opened_at_counts_from_its_entry_signal():
    row = {"book": "swing",
           "opened_event": "ENTERED|SPY|2099-12-18|680.0|C|2026-11-23T14:35:00+00:00|swing"}
    assert agent._sessions_held({}, row, date(2026, 11, 23)) == 1
    assert not agent._swing_hold_over({}, row, date(2026, 11, 27))
    assert hc._swing_opened(row) == datetime(2026, 11, 23, 14, 35,
                                             tzinfo=timezone.utc).timestamp()


def test_the_swing_switch_is_part_of_the_confirmation_fingerprint():
    # review: switching swing (the chat command) after a /v2 preview left
    # its confirmation valid for settings the owner never reviewed
    import webui
    assert "include_swing_trades" in webui._FP_KEYS
    off = webui._settings_fingerprint({"include_swing_trades": False}, b"k" * 32)
    on = webui._settings_fingerprint({"include_swing_trades": True}, b"k" * 32)
    assert off != on


# ── a swing exit that never arrives ─────────────────────────────────────────

def _overdue_state():
    opened = _et(date(2026, 11, 23), 9, 35).timestamp()
    state = {"positions": {}, "seen": []}
    _hold(state, key("SPY", 680), "swing", "swing", opened)
    _hold(state, key("QQQ", 640), "main", None, opened)        # not swing
    _hold(state, key("IWM", 230), "swing", "swing",
          _et(date(2026, 11, 30), 9, 35).timestamp())           # day 2 of 5
    return state


def test_a_swing_row_held_past_its_last_session_is_overdue():
    rows = _overdue_state()["positions"]
    # 11-23 .. 11-30 = 5 sessions (Thanksgiving skipped): AgentHC's time exit
    assert hc._swing_overdue({}, rows, now_et=_et(date(2026, 11, 30))) == []
    assert hc._swing_overdue({}, rows, now_et=_et(date(2026, 12, 1), 9, 40)) == [
        key("SPY", 680)]
    # a row that doesn't say when it was opened: left to the usual rules
    assert hc._swing_overdue({}, {"x": {"book": "swing"}},
                             now_et=_et(date(2026, 12, 1))) == []


def test_an_overdue_swing_row_arms_the_no_signal_sell_for_it_alone(monkeypatch, told):
    # review: with swing on, the agent reads track=all at 100 events; a swing
    # exit that scrolled out (or never came) left the row held until the
    # expiry guard, ~17 days later, with no warning
    monkeypatch.setattr(hc, "_now_et", lambda: _et(date(2026, 12, 1), 9, 40))
    monkeypatch.setattr(hc, "_LAST_POLL", {"access": True, "exits": True, "why": ""})
    state = _overdue_state()
    hc._check_window({}, state, lambda s: None, {"events": []}, {"limit": 100}, [])
    assert state["feed_gap"]["keys"] == [key("SPY", 680)]
    assert state["feed_gap"]["swing_overdue"] == [key("SPY", 680)]
    assert hc.had_exit_access() is False
    assert hc.exit_loss_reason() == "swing_overdue"
    assert len(told) == 1 and "swing trade" in told[0] and key("SPY", 680) in told[0]
    hc._check_window({}, state, lambda s: None, {"events": []}, {"limit": 100}, [])
    assert len(told) == 1                                   # told once
    # the agent sells only that row (the main pick and the young swing stay)
    state["signal_loss"] = {"since": _et(date(2026, 12, 1), 9, 40).timestamp(),
                            "cause": "swing_overdue"}
    assert [k for k, _r in agent._signal_loss_rows(
        state, {}, _et(date(2026, 12, 1), 15, 0))] == [key("SPY", 680)]
    assert "swing trade" in agent._signal_loss_why("swing_overdue")
    # its exit arrives after all: the row goes, and so does the gap
    state["positions"].pop(key("SPY", 680))
    hc._LAST_POLL.update(exits=True, why="")
    hc._check_window({}, state, lambda s: None, {"events": []}, {"limit": 100}, [])
    assert "feed_gap" not in state and hc.had_exit_access() is True


def test_an_overdue_swing_row_beside_a_real_gap_reads_as_a_gap(monkeypatch, told):
    monkeypatch.setattr(hc, "_now_et", lambda: _et(date(2026, 12, 1), 9, 40))
    monkeypatch.setattr(hc, "_LAST_POLL", {"access": True, "exits": True, "why": ""})
    state = _overdue_state()
    state["feed_gap"] = {"since": time.time(), "keys": [key("QQQ", 640)],
                         "opened": {key("QQQ", 640): state["positions"][key("QQQ", 640)]["opened_at"]}}
    hc._check_window({}, state, lambda s: None, {"events": []}, {"limit": 100}, [])
    assert state["feed_gap"]["keys"] == sorted([key("QQQ", 640), key("SPY", 680)])
    assert hc.exit_loss_reason() == "feed_gap"


def test_an_agent_that_never_asks_for_swing_gets_no_swing_event(home, monkeypatch, no_policy, told):
    # review: an agent from before swing trades reads track=all with other
    # trades on and maps every non-main track to "other trades"; AgentHC
    # lists swing events in track=all only for a read that asks (SWING_PARAM)
    cfg, state = _setup(monkeypatch, swing=False, other=True)
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    srv.lines += [ev("ENTERED", "SPY", 680, "swing", t0),
                  ev("ENTERED", "QQQ", 600, "other", t0)]
    _loop(cfg, state, b, srv, monkeypatch)
    params = srv.asked[-1][0]
    assert params["track"] == "all" and hc.SWING_PARAM not in params
    assert [c["ticker"] for c in b.calls] == ["QQQ"]


# ── after the pass ends: the swing exit is still owed ───────────────────────

PASS = "L402 MACPASS:ended"


def _ended_pass(state, status="expired"):
    state.setdefault("pass_status", {})[hc._token_id(PASS)] = {
        "status": status, "until_ts": 1.0, "checked": time.time(),
        "first_seen": 0.0}


def test_an_ended_pass_still_reads_a_held_swing_positions_exit(home, monkeypatch, told):
    cfg = {"source": "agenthc", "feed_pass": {"token": PASS}, "dry_run": False,
           "include_swing_trades": False}
    state = {"positions": {}, "seen": []}
    _hold(state, key("SPY", 680), "swing", "swing")
    _ended_pass(state)
    srv = Server(pass_status="expired")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    srv.lines += [ev("ENTERED", "QQQ", 600, "main", t0),
                  ev("EXITED", "SPY", 680, "swing", t0),
                  ev("EXITED", "IWM", 230, "main", t0)]
    monkeypatch.setattr(hc.requests, "get", srv.get)
    got = hc.poll(cfg, state, lambda s: None)
    # exits only, and only the ones the ledger holds
    assert [(e["event"], e["ticker"]) for e in got] == [("EXITED", "SPY")]
    params, headers = srv.asked[-1]
    assert params["mode"] == "off" and headers["X-Agent-Mode"] == "off"
    assert headers["Authorization"] == PASS and params["track"] == "all"
    assert hc.had_exit_access() and not hc.had_access()


def test_no_owed_read_without_a_real_swing_position(home, monkeypatch, told):
    cfg = {"source": "agenthc", "feed_pass": {"token": PASS}, "dry_run": False}
    state = {"positions": {}, "seen": []}
    _hold(state, key("SPY", 680), "main")
    _hold(state, key("IWM", 230), "swing", "swing", live=False)
    state["positions"][key("IWM", 230)].update(dry=True, option_id="dry")
    _ended_pass(state)
    srv = Server(pass_status="expired")
    monkeypatch.setattr(hc.requests, "get", srv.get)
    assert hc.poll(cfg, state, lambda s: None) == []
    assert srv.asked == [] and hc.exit_loss_reason() == "pass_ended"


def test_a_refused_owed_read_is_asked_again_only_after_an_hour(home, monkeypatch, told):
    cfg = {"source": "agenthc", "feed_pass": {"token": PASS}, "dry_run": False}
    state = {"positions": {}, "seen": []}
    _hold(state, key("SPY", 680), "swing", "swing")
    _ended_pass(state, "used_up")
    srv = Server(pass_status="used_up", feed_status=402)
    monkeypatch.setattr(hc.requests, "get", srv.get)
    assert hc.poll(cfg, state, lambda s: None) == []
    assert len(srv.asked) == 1
    rec = state["pass_status"][hc._token_id(PASS)]
    assert rec["owed_refused_at"] > 0
    hc.poll(cfg, state, lambda s: None)
    assert len(srv.asked) == 1                       # not again so soon
    rec["owed_refused_at"] -= hc._OWED_EXITS_RETRY_S + 1
    srv.feed_status = 200
    hc.poll(cfg, state, lambda s: None)
    assert len(srv.asked) == 2 and "owed_refused_at" not in rec


def test_the_owed_read_never_buys_or_charges(home, monkeypatch, told):
    """The wallet path with no day-pass: a held swing position's exit is
    read with the ended prepaid pass saying off, and nothing is paid."""
    paid = []
    monkeypatch.setattr(hc, "_buy_day_pass", lambda *a, **k: paid.append(a) or False)
    cfg = {"source": "agenthc", "feed_pass": {"token": PASS}, "dry_run": False,
           "lnbits_url": "https://w", "day_pass_recurring": False}
    state = {"positions": {}, "seen": []}
    _hold(state, key("SPY", 680), "swing", "swing")
    _ended_pass(state)
    srv = Server(pass_status="expired")
    srv.lines.append(ev("EXITED", "SPY", 680, "swing", datetime.now(timezone.utc)))
    monkeypatch.setattr(hc.requests, "get", srv.get)
    got = hc.poll(cfg, state, lambda s: None)
    assert [e["ticker"] for e in got] == ["SPY"] and paid == []
    assert all(p["mode"] == "off" for p, _h in srv.asked)


# ── the owner's switch: setup plans, chat, settings API ─────────────────────

def _plan(cfg, src):
    return setup_plan.apply({"source": {"type": "agenthc", **src}}, cfg,
                            True, "/nonexistent")


def test_a_setup_plan_can_never_turn_swing_trades_on():
    new, report = _plan({"dry_run": True}, {"include_swing_trades": True})
    assert new is None
    assert any("only be turned on by the owner" in e for e in report["errors"])
    # off, or left out: fine
    new, report = _plan({"dry_run": True, "include_swing_trades": True},
                        {"include_swing_trades": False})
    assert report["errors"] == [] and new["include_swing_trades"] is False
    new, report = _plan({"dry_run": True, "include_swing_trades": True}, {})
    assert report["errors"] == [] and new["include_swing_trades"] is True
    # already on (the owner's choice): a plan that says so changes nothing
    new, report = _plan({"dry_run": True, "include_swing_trades": True},
                        {"include_swing_trades": True})
    assert report["errors"] == [] and new["include_swing_trades"] is True
    new, report = _plan({"dry_run": True}, {"include_swing_trades": "yes"})
    assert new is None


def test_turning_swing_on_counts_as_more_live_trading():
    assert "take swing trades (multi-day holds)" in setup_plan._live_increases(
        {}, {"include_swing_trades": True}, False)


def test_the_chat_command_is_the_owners_switch(home):
    cfg = {"source": "agenthc"}
    ok, reply = agent._command("swing trades on", cfg, "swing trades on")
    assert ok and cfg["include_swing_trades"] is True and "VIX spike" in reply
    ok, reply = agent._command("swing trades off", cfg, "swing trades off")
    assert ok and cfg["include_swing_trades"] is False
    assert "still get their exit signals" in reply
    other = {"source": "manual"}
    ok, reply = agent._command("swing trades on", other, "swing trades on")
    assert "include_swing_trades" not in other


def test_doctor_and_support_say_which_categories(home):
    cfg = {"source": "agenthc", "include_swing_trades": True}
    assert agent.signal_scope_words(cfg) == "main pick + swing trades"
    assert agent.signal_scope_words({**cfg, "include_other_trades": True}) == \
        "main pick + other trades + swing trades"
    assert agent.signal_scope_words({"source": "agenthc"}) == "main pick"


def test_the_v2_swing_switch_is_two_steps_and_the_chat_command(home, app):
    from test_v2_parity import _running
    from test_v2_settings_api import BASE, read_cfg, same_effect, write_cfg
    write_cfg(home, BASE)
    c = _running(app)
    out = same_effect(home, BASE,
                      lambda: c.post("/api/command", {"text": "swing trades on"}),
                      lambda: c.confirmed("/api/v2/settings/signals", {"swing": True}))
    assert out["include_swing_trades"] is True
    # the first POST is only the preview: nothing saved, the risk named
    write_cfg(home, BASE)
    pv = c.post("/api/v2/settings/signals", {"swing": True})
    assert pv["needs_confirm"] is True
    (ch,) = pv["preview"]["changes"]
    assert (ch["key"], ch["saved_now"], ch["new"], ch["direction"]) == (
        "swing", False, True, "raises_risk")
    assert "include_swing_trades" not in read_cfg(home)
    assert c.get("/api/v2/settings")["signals"]["include_swing_trades"] is False
    # off again lowers the risk; the classic scope route takes it too
    write_cfg(home, {**BASE, "include_swing_trades": True})
    ch = c.post("/api/v2/settings/signals", {"swing": False})["preview"]["changes"][0]
    assert ch["direction"] == "lowers_risk"
    assert c.post("/api/source/scope", {"swing": False})["ok"] is True
    assert read_cfg(home)["include_swing_trades"] is False
    # the "which signals" save keeps the swing switch as it is
    write_cfg(home, {**BASE, "include_swing_trades": True})
    c.confirmed("/api/v2/settings/signals", {"scope": "all"})
    assert read_cfg(home)["include_swing_trades"] is True
    for bad in ({"swing": "on"}, {"swing": 1}):
        assert c.post("/api/v2/settings/signals", bad)["ok"] is False, bad
    write_cfg(home, {**BASE, "source": "manual"})
    assert c.post("/api/v2/settings/signals", {"swing": True})["ok"] is False


# ── practice, preview and the dashboard ─────────────────────────────────────

def test_practice_starts_the_swing_clock_only_when_it_is_on(home):
    state = {}
    agent._note_practice_since({"source": "agenthc", "dry_run": True}, state,
                               lambda s: None)
    assert set(state["practice_since"]) == {"main"}
    agent._note_practice_since({"source": "agenthc", "dry_run": True,
                                "include_swing_trades": True}, state,
                               lambda s: None)
    assert set(state["practice_since"]) == {"main", "swing"}
    agent._note_practice_since({"source": "agenthc", "dry_run": True}, state,
                               lambda s: None)
    assert set(state["practice_since"]) == {"main"}


def test_a_released_swing_practice_trade_is_dropped_while_swing_is_off(home, no_policy, told):
    cfg = {"source": "agenthc", "dry_run": True, "include_swing_trades": False}
    state = {"seen": [], "positions": {}}
    t0 = datetime.now(timezone.utc) - timedelta(days=2)
    raw = [ev("ENTERED", "SPY", 680, "swing", t0),
           {**ev("EXITED", "SPY", 680, "swing", t0 + timedelta(days=2)),
            "paper_pnl_pct": 40.0}]
    events = hc._parse_events({"events": [{**e, "practice_release": "on_close"} for e in raw]})
    agent._handle_feed_events(events, cfg, state, StubBroker(), None, lambda s: None)
    assert [r for r in agent._read_trade_log() if r.get("action") in ("entry", "exit")] == []
    assert all(e["event_id"] in state["seen"] for e in events)


def _closed(track=None, ticker="SPY"):
    t = {"date": "2026-11-20", "ticker": ticker, "direction": "call",
         "strike": 680, "expiry": "2026-12-04", "paper_pnl_pct": 30.0,
         "entry_time": "2026-11-16T09:35:00-05:00"}
    if track:
        t["track"] = track
    return t


def test_the_practice_preview_shows_swing_trades_only_when_on():
    trades = [_closed(), _closed("swing", "QQQ")]
    off = dry_run_preview.evaluate({"source": "agenthc"}, trades)
    assert [r["contract"].split()[0] for r in off] == ["SPY"]
    assert off[0]["book"] == "main"
    on = dry_run_preview.evaluate({"source": "agenthc",
                                   "include_swing_trades": True}, trades)
    assert sorted((r["contract"].split()[0], r["book"]) for r in on) == [
        ("QQQ", "swing"), ("SPY", "main")]


def test_the_dashboard_keeps_swing_apart(home):
    now = datetime(2026, 11, 25, 19, 0, tzinfo=timezone.utc)
    opened = datetime(2026, 11, 23, 14, 35, tzinfo=timezone.utc).timestamp()
    cfg = {"source": "agenthc", "dry_run": False, "include_swing_trades": True}
    state = {"positions": {
        key("SPY", 680): {"option_id": "o", "qty": 1, "live": True,
                          "book": "swing", "track": "swing",
                          "opened_at": opened, "entry_mark": 9.8},
        key("QQQ", 640): {"option_id": "o", "qty": 1, "live": True,
                          "book": "main", "opened_at": opened}}}

    def row(action, book, pnl=None, ts="2026-11-25T15:00:00+00:00"):
        r = {"action": action, "ts": ts, "contract": "IWM x",
             "ui": {"mode": "live", "book": book}}
        if pnl is not None:
            r["est_pnl_usd"] = pnl
        return r
    rows = [row("exit", "swing", 120.0), row("exit", "main", -30.0),
            row("exit", "other", 5.0), {**row("exit", "swing", 7.0), "dry": True}]
    rows = [{**r, "n": i} for i, r in enumerate(rows)]
    s = vm.build_status(cfg, state, rows, {"running": True}, now=now)
    assert vm.validate(s) == []
    books = {p["id"]: p["book"] for p in s["positions"]}
    assert books == {key("SPY", 680): "swing", key("QQQ", 640): "main"}
    by = s["results"]["by_signal"]
    assert by["swing"]["label"] == "Swing Trade" and by["swing"]["multi_day"]
    assert by["swing"]["on"] is True and by["other"]["on"] is False
    assert by["swing"]["open"]["real"] == 1 and by["main"]["open"]["real"] == 1
    assert by["swing"]["total"] == {"real": 120.0, "practice": 7.0, "paper_account": 0.0}
    assert by["main"]["total"]["real"] == -30.0 and by["other"]["total"]["real"] == 5.0
    assert by["swing"]["closed"]["real"] == 1


def test_the_classic_dashboard_names_swing_positions(home):
    st = {"positions": {key("SPY", 680): {"book": "swing", "track": "swing"},
                        key("QQQ", 640): {"book": "main"}}}
    f = agent._swing_fields({"source": "agenthc", "include_swing_trades": True}, st)
    assert f == {"swing trades": "on", "swing positions": key("SPY", 680)}
    assert agent._swing_fields({"source": "agenthc"}, {"positions": {}}) == {
        "swing trades": "off"}
    assert agent._swing_fields({"source": "manual"}, {"positions": {}}) == {}


def test_the_terminal_setup_asks_about_swing_trades(monkeypatch):
    asked = []

    def answer(prompt=""):
        asked.append(prompt)
        return "y" if prompt.startswith("Take swing trades") else "1"
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(hc, "wallet_setup", lambda c: c)
    monkeypatch.setattr(hc, "wallet_from_cfg", lambda c: object())
    cfg = hc.setup({})
    assert cfg["include_swing_trades"] is True
    assert any(p.startswith("Take swing trades too? [y/n, default n]") for p in asked)


def test_the_feed_params_follow_the_switch_and_the_ledger(monkeypatch):
    monkeypatch.setattr(hc.time, "time", lambda: 1000.0)
    state = {"feed_read": {"local": 990.0}, "positions": {}}
    assert hc._feed_params({}, state)["track"] == "main"
    assert hc._feed_params({"include_swing_trades": True}, state) == {
        "limit": hc.FEED_LIMIT_MAX, "track": "all", hc.SWING_PARAM: "1"}
    # "other trades" alone: every track, but swing events are not asked for
    assert hc._feed_params({"include_other_trades": True}, state) == {
        "limit": hc.FEED_LIMIT_MAX, "track": "all"}
    state["positions"]["x"] = {"book": "swing"}
    assert hc._feed_params({}, state)["track"] == "all"
    assert hc._feed_params({}, state)[hc.SWING_PARAM] == "1"   # its exit
    state["positions"] = {}
    state["exits_to_retry"] = [{"event": "EXITED", "track": "swing"}]
    assert hc._feed_params({}, state)[hc.SWING_PARAM] == "1"
    assert hc._row_track({"book": "swing"}) == "swing"
    assert hc._row_track({"book": "swing", "track": "other"}) == "swing"


def test_watching_starts_per_category(monkeypatch):
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    agent.note_watching({"source": "agenthc", "include_swing_trades": True})
    assert set(agent._WATCH_SINCE) == {"main", "swing"}
    agent.note_watching({"source": "agenthc"})
    assert set(agent._WATCH_SINCE) == {"main"}


def test_the_daily_digest_names_swing_positions(home, monkeypatch, told):
    class Late(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 23, 16, 30, tzinfo=ET)
    monkeypatch.setattr(agent, "datetime", Late)
    state = {"positions": {key("SPY", 680): {"book": "swing"}}}
    agent._maybe_daily_digest({}, state, lambda s: None)
    assert any(f"{key('SPY', 680)} (swing trade)" in t for t in told)


def test_an_ended_day_pass_is_offered_again_hourly_while_a_swing_is_held(home, monkeypatch, told):
    """A refused ended day-pass is normally never offered again; a swing
    position's exit is owed days later, so it is asked for again hourly."""
    cfg = {"source": "agenthc", "dry_run": False, "lnbits_url": "https://w",
           "day_pass_recurring": False}
    state = {"positions": {}, "seen": [],
             "l402": {"token": "L402 DAY:old", "expires_at": time.time() - 60,
                      "paid_day": "2026-11-20"}}
    _hold(state, key("SPY", 680), "swing", "swing")
    srv = Server(feed_status=402)
    monkeypatch.setattr(hc.requests, "get", srv.get)
    hc.poll(cfg, state, lambda s: None)
    assert len(srv.asked) == 1 and state["l402"]["exits_done"] is True
    hc.poll(cfg, state, lambda s: None)
    assert len(srv.asked) == 1
    state["l402"]["exits_done_at"] -= hc._OWED_EXITS_RETRY_S + 1
    srv.feed_status = 200
    srv.lines.append(ev("EXITED", "SPY", 680, "swing", datetime.now(timezone.utc)))
    got = hc.poll(cfg, state, lambda s: None)
    assert [e["ticker"] for e in got] == ["SPY"] and len(srv.asked) == 2
    # without a swing position a refused day-pass stays refused, as before
    state["positions"][key("SPY", 680)]["book"] = "main"
    state["positions"][key("SPY", 680)].pop("track", None)
    state["l402"].update(exits_done=True, exits_done_at=0)
    hc.poll(cfg, state, lambda s: None)
    assert len(srv.asked) == 2
