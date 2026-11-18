"""Practice gets completed trades only, released at their exit.

OPERATOR DECISION (2026-11-18): only LIVE agents are charged a signal day,
and practice gets nothing tradable in real time. A read that says
mode=dry_run gets each EXITED with its paired ENTERED (same contract and
track), both with their ORIGINAL times and tagged "practice_release":
"on_close". The agent books each pair as ONE completed practice trade the
moment it arrives: dated at the signal and the exit, checked as of the
signal time (pairs in entry-time order), sized by today's per-trade budget
in dollars, P&L = amount x paper_pnl_pct / 100 (no paper_pnl_pct: -100%,
"exited near worthless; no price published"). It never opens a practice
position, and books a pair once, also across a restart.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import agent
from sources import agenthc_day_trade_ideas as hc
from test_main_loop_events import Broker

EXPIRY = "2099-12-18"
# Wed 2026-11-18, 16:00 ET: after the close, when the agent reads the pairs
NOW = datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)


def at(hh, mm, day=18):
    """An ET wall time on 2026-11-<day> (EST: UTC-5) as an ISO string."""
    return datetime(2026, 11, day, hh + 5, mm, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def clock(monkeypatch):
    c = {"t": NOW}

    class Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return c["t"].astimezone(tz) if tz else c["t"].replace(tzinfo=None)
    monkeypatch.setattr(agent, "datetime", Fake)
    return c


@pytest.fixture
def sent(monkeypatch, home, no_policy):
    out = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, priority=None: out.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    return out


def wire(event, occurred_at, ticker="SPY", strike=600.0, track="main",
         pnl=None, release=True, expiry=EXPIRY):
    ev = {"event": event, "ticker": ticker, "expiry": expiry, "strike": strike,
          "type": "C", "occurred_at": occurred_at, "track": track}
    if pnl is not None:
        ev["paper_pnl_pct"] = pnl
    if release:
        ev["practice_release"] = "on_close"
    return ev


def parse(*evs):
    """The events exactly as the AgentHC source hands them to the agent."""
    return hc._parse_events({"events": list(evs)})


def pair(t_in, t_out, pnl=12.5, **kw):
    return parse(wire("ENTERED", t_in, **kw), wire("EXITED", t_out, pnl=pnl, **kw))


CFG = {"source": "agenthc", "dry_run": True, "sizing_mode": "budget",
       "budget_per_trade_usd": 500, "include_other_trades": True}
# practice started before every trade here (_note_practice_since)
EARLY = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def handle(events, cfg=None, state=None, broker=None):
    state = state if state is not None else {"positions": {}}
    state.setdefault("practice_since", {"main": EARLY, "other": EARLY})
    agent._handle_feed_events(events, {**CFG, **(cfg or {})}, state, broker,
                              broker.client({}) if broker else None,
                              lambda s: None)
    return state


def rows(home):
    p = home / "trades.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def trades(home):
    return [r for r in rows(home) if r.get("action") in ("entry", "exit")]


# ── the source keeps the tag ────────────────────────────────────────────────

def test_the_source_keeps_the_release_tag_and_the_original_times():
    (ev,) = parse(wire("EXITED", at(14, 31), pnl=8.0))
    assert ev["practice_release"] == "on_close"
    assert ev["occurred_at"] == at(14, 31) and ev["paper_pnl_pct"] == 8.0
    (live,) = parse(wire("ENTERED", at(10, 2), release=False))
    assert "practice_release" not in live


@pytest.mark.parametrize("last_read, gap", [(1.0, True), (4102444800.0, False)])
def test_a_practice_window_is_judged_on_its_released_exits(last_read, gap):
    """AgentHC's `limit` counts the exits of a practice read (each comes with
    its entry): a window full of exits all newer than the last good read may
    have dropped one. The entries keep their own, older times and don't hide
    that; exits older than the last read are no gap."""
    state = {"positions": {"X|2099-12-18|1.0|C": {"option_id": "x", "qty": 1}},
             "feed_read": {"local": last_read, "server": last_read}}
    raw = []
    for i in range(3):
        raw += [wire("EXITED", "2099-01-01T15:00:00+00:00", ticker=f"T{i}", pnl=1.0),
                wire("ENTERED", "2000-01-01T15:00:00+00:00", ticker=f"T{i}")]
    hc._check_window({}, state, lambda s: None, {"events": raw}, {"limit": 3},
                     hc._parse_events({"events": raw}))
    assert ("feed_gap" in state) is gap
    # fewer exits than the limit (6 events, but 3 exits of 4): never a gap
    state = {"positions": state["positions"],
             "feed_read": {"local": 1.0, "server": 1.0}}
    hc._check_window({}, state, lambda s: None, {"events": raw}, {"limit": 4},
                     hc._parse_events({"events": raw}))
    assert "feed_gap" not in state


# ── one completed practice trade, booked at its exit ────────────────────────

def test_a_pair_is_booked_once_with_its_own_times_amount_and_pnl(home, sent, clock):
    events = pair(at(10, 2), at(14, 31), pnl=12.5)
    state = handle(events)
    got = trades(home)
    assert [r["action"] for r in got] == ["entry", "exit"]
    entry, exit_ = got
    # dated at the signal and at the exit, not when the agent logged them
    assert entry["ts"] == at(10, 2)
    assert exit_["ts"] == at(14, 31)
    for r in got:
        assert r["dry"] is True and r["practice_release"] is True
        assert r["amount_usd"] == 500
        assert "signal 10:02 → exit 14:31, logged at exit" in r["reason"]
        assert r["logged_at"] == NOW.isoformat()
        assert r["ui"]["mode"] == "practice" and r["ui"]["sent"] is False
    assert exit_["est_pnl_usd"] == 62.5 and exit_["pnl_pct"] == 12.5
    assert "no_price" not in exit_
    # never an open practice row
    assert state["positions"] == {}
    assert agent._open_in_mode(state, True) == 0
    assert "practice_held" not in state
    assert any("[PRACTICE] SPY" in m and "+$62.50 on $500.00" in m for m in sent)
    # the same read again: nothing new
    handle(events, state=state)
    assert len(trades(home)) == 2


def test_a_pair_is_booked_once_across_a_restart_that_lost_its_note(home, sent, clock):
    events = pair(at(10, 2), at(14, 31))
    handle(events)
    handle(events, state={"positions": {}})   # state.json from before the booking
    assert len(trades(home)) == 2


def test_an_exit_without_a_price_is_a_full_loss_labelled_so(home, sent, clock):
    handle(pair(at(10, 2), at(15, 50), pnl=None))
    exit_ = trades(home)[1]
    assert exit_["est_pnl_usd"] == -500.0 and exit_["pnl_pct"] == -100.0
    assert exit_["no_price"] is True
    assert agent.PRACTICE_NO_PRICE in exit_["reason"]
    assert agent.PRACTICE_NO_PRICE == "exited near worthless; no price published"


def test_the_amount_is_the_budget_now_per_track(home, sent, clock):
    handle(pair(at(10, 2), at(14, 31), pnl=-20.0, track="other"),
           cfg={"other_sizing_mode": "budget", "other_budget_per_trade_usd": 100})
    exit_ = trades(home)[1]
    assert exit_["amount_usd"] == 100 and exit_["est_pnl_usd"] == -20.0
    assert exit_["ui"]["book"] == "other"


def test_a_fixed_contract_count_books_the_result_without_dollars(home, sent, clock):
    handle(pair(at(10, 2), at(14, 31), pnl=30.0),
           cfg={"sizing_mode": "contracts", "contracts_per_trade": 2})
    entry, exit_ = trades(home)
    assert "amount_usd" not in exit_ and "est_pnl_usd" not in exit_
    assert exit_["pnl_pct"] == 30.0 and "+30.0%" in exit_["reason"]
    assert "2 contracts per trade" in entry["reason"]


def test_an_overnight_carry_is_paired_across_reads(home, sent, clock):
    exit_only = parse(wire("EXITED", at(10, 5), pnl=40.0))
    entry_only = parse(wire("ENTERED", at(15, 40, day=17)))
    state = handle(exit_only)
    assert trades(home) == [] and len(state["practice_held"]) == 1
    handle(entry_only, state=state)
    entry, exit_ = trades(home)
    assert entry["ts"] == at(15, 40, day=17) and exit_["ts"] == at(10, 5)
    assert "signal Nov 17 15:40 → exit Nov 18 10:05, logged at exit" in exit_["reason"]
    assert exit_["est_pnl_usd"] == 200.0
    assert "practice_held" not in state


def test_an_other_trades_pair_is_booked_and_ignored_when_other_trades_are_off(
        home, sent, clock):
    handle(pair(at(10, 2), at(14, 31), track="other"),
           cfg={"include_other_trades": False})
    assert trades(home) == []
    handle(pair(at(11, 0), at(12, 0), track="other"))
    assert [r["ui"]["book"] for r in trades(home)] == ["other", "other"]


def test_two_round_trips_in_one_contract_pair_in_order(home, sent, clock):
    events = parse(wire("ENTERED", at(10, 0)), wire("EXITED", at(11, 0), pnl=10.0),
                   wire("ENTERED", at(12, 0)), wire("EXITED", at(13, 0), pnl=-10.0))
    handle(events)
    got = trades(home)
    assert [(r["action"], r["ts"][11:16]) for r in got] == [
        ("entry", "15:00"), ("exit", "16:00"), ("entry", "17:00"), ("exit", "18:00")]
    assert [r.get("est_pnl_usd") for r in got if r["action"] == "exit"] == [50.0, -50.0]


# ── checked as of the signal time ───────────────────────────────────────────

def test_the_daily_cap_counts_as_of_the_signal_in_entry_time_order(home, sent, clock):
    """The later signal is listed first; the earlier one is still taken."""
    late = pair(at(13, 0), at(14, 0), ticker="QQQ")
    early = pair(at(10, 0), at(15, 0), ticker="SPY")
    handle(late + early, cfg={"max_entries_per_day": 1})
    got = rows(home)
    assert [r["contract"].split()[0] for r in got if r["action"] == "entry"] == ["SPY"]
    (skip,) = [r for r in got if r["action"] == "skip_daily_cap"]
    assert skip["contract"].startswith("QQQ") and skip["ts"] == at(13, 0)
    assert skip["dry"] is True and "signal 13:00 → exit 14:00" in skip["reason"]


def test_open_positions_count_practice_trades_open_at_the_signal(home, sent, clock):
    events = (pair(at(10, 0), at(14, 0), ticker="AAA")
              + pair(at(11, 0), at(11, 30), ticker="BBB")
              + pair(at(14, 30), at(15, 0), ticker="CCC"))
    handle(events, cfg={"max_open_positions": 1})
    took = [r["contract"].split()[0] for r in rows(home) if r["action"] == "entry"]
    assert took == ["AAA", "CCC"]   # AAA was open at 11:00, closed by 14:30
    assert [r["contract"].split()[0] for r in rows(home)
            if r["action"] == "skip_position_cap"] == ["BBB"]


def test_max_daily_loss_counts_exits_before_the_signal(home, sent, clock):
    events = (pair(at(10, 0), at(11, 0), pnl=-50.0, ticker="AAA")   # -$250
              + pair(at(12, 0), at(13, 0), pnl=20.0, ticker="BBB"))
    handle(events, cfg={"max_daily_loss_usd": 200})
    assert [r["action"] for r in rows(home)] == ["entry", "exit", "skip_daily_loss"]


def test_the_0dte_rule_is_judged_on_the_signal_day(home, sent, clock):
    """Read the next morning: the contract expired the day of its signal."""
    clock["t"] = NOW + timedelta(hours=14)
    handle(pair(at(10, 0), at(11, 0), expiry="2026-11-18"),
           cfg={"rule_skip_0dte": True})
    (skip,) = rows(home)
    assert skip["action"] == "skip_rule" and "0DTE" in skip["reason"]
    assert skip["ui"]["code"] == "rule_0dte"


def test_the_ai_policy_sees_the_entry_only_as_of_its_signal(home, sent, clock,
                                                           monkeypatch):
    import llm_policy
    seen = []

    def evaluate(cfg, ev, log, context=None):
        seen.append((ev, log, context))
        return {"act": False, "reason": "not my kind of trade"}
    monkeypatch.setattr(llm_policy, "evaluate", evaluate)
    handle(pair(at(10, 2), at(14, 31), pnl=99.0))
    (ev, log, ctx), = seen
    assert ev["event"] == "ENTERED" and "paper_pnl_pct" not in ev
    assert "practice_release" not in ev
    assert ctx["now_et"].startswith("2026-11-18 10:02") and ctx["mode"] == "practice"
    assert [r["action"] for r in rows(home)] == ["policy_veto"]


# ── what goes the usual way ─────────────────────────────────────────────────

def test_a_real_position_held_in_practice_is_still_sold_on_its_released_exit(
        home, sent, clock):
    key = f"SPY|{EXPIRY}|600.0|C"
    broker = Broker()
    state = {"positions": {key: {"option_id": "stub", "qty": 1, "live": True,
                                 "book": "main", "track": "main"}}}
    handle(pair(at(10, 2), at(14, 31)), state=state, broker=broker)
    assert [c[0] for c in broker.calls] == ["sell"]
    assert state["positions"] == {}
    assert [r["action"] for r in rows(home)] == ["exit"]
    assert rows(home)[0]["dry"] is False
    assert "practice_held" not in state


def test_a_practice_position_from_before_is_closed_by_its_released_exit(
        home, sent, clock):
    key = f"SPY|{EXPIRY}|600.0|C"
    state = {"positions": {key: {"option_id": "dry", "qty": 1, "dry": True}}}
    handle(pair(at(10, 2), at(14, 31)), state=state)
    assert state["positions"] == {}
    assert [(r["action"], r.get("practice_release")) for r in rows(home)] == [
        ("exit", None)]


def test_a_live_read_is_unchanged(home, sent, clock):
    """Untagged events (live / legacy) still go through handle_event."""
    clock["t"] = datetime(2026, 11, 18, 15, 2, 30, tzinfo=timezone.utc)
    broker = Broker()
    state = handle(parse(wire("ENTERED", at(10, 2), release=False)),
                   cfg={"dry_run": False}, broker=broker)
    assert [c[0] for c in broker.calls] == ["buy"]
    assert list(state["positions"]) == [f"SPY|{EXPIRY}|600.0|C"]


def test_off_reads_exits_only_and_sells_a_real_position(home, sent, clock):
    key = f"SPY|{EXPIRY}|600.0|C"
    cfg = {"dry_run": False, "paused": True}
    assert agent._feed_mode({**CFG, **cfg}) == "off"
    broker = Broker()
    state = {"positions": {key: {"option_id": "stub", "qty": 1, "live": True}}}
    clock["t"] = datetime(2026, 11, 18, 19, 32, tzinfo=timezone.utc)
    handle(parse(wire("EXITED", at(14, 31), release=False),
                 wire("ENTERED", at(14, 31), ticker="QQQ", release=False)),
           cfg=cfg, state=state, broker=broker)
    assert [c[0] for c in broker.calls] == ["sell"]
    assert state["positions"] == {}
    assert [r["action"] for r in rows(home)] == ["exit", "skip_paused"]


# ── what the owner is told, everywhere ──────────────────────────────────────

def test_every_surface_says_what_practice_and_live_mean():
    import os
    import webui
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(*p):
        with open(os.path.join(root, *p), encoding="utf-8") as f:
            return " ".join(f.read().split())
    ts = read("webui_app", "src", "lib", "modeText.ts")
    for text in (agent.PRACTICE_MODE_TEXT, agent.LIVE_MODE_TEXT,
                 agent.ALPACA_PAPER_LIVE_TEXT):
        assert text in ts                                  # React wizard + banners
        assert text in " ".join(webui._WIZARD_HTML.split())  # classic wizard
    for doc in ("README.md", "GETTING_STARTED.md"):
        assert "Practice mode logs each trade only after it closes" in read(doc)
    assert "PRACTICE_BANNER" in webui._DASH_HTML
    assert "practice_note: string | null;" in read("webui_app", "src", "lib", "viewModel.ts")


def test_activity_shows_when_a_practice_trade_really_happened(home, sent, clock):
    import view_model
    handle(pair(at(10, 2), at(14, 31)))
    got = [view_model.activity_row({**r, "n": i}) for i, r in enumerate(rows(home))]
    assert [g["practice_note"] for g in got] == [
        "signal 10:02 → exit 14:31, logged at exit"] * 2
    assert got[0]["at"].startswith("2026-11-18T15:02")
    assert got[1]["est_pnl_usd"] == 62.5 and got[1]["real"] is False
    assert view_model.activity_row({"n": 1, "action": "entry"})["practice_note"] is None


# ── review follow-ups ───────────────────────────────────────────────────────

def test_trades_that_closed_before_practice_started_are_not_booked(
        home, sent, clock, capsys):
    """The first practice read lists earlier completed trades: a live agent
    never buys that backlog, and practice doesn't book it either."""
    state = {"positions": {}, "practice_since": {"main": ts(at(12, 0))}}
    handle(pair(at(10, 0), at(11, 0)) + pair(at(11, 30), at(12, 30), ticker="QQQ"),
           state=state)
    assert [r["contract"].split()[0] for r in trades(home)] == ["QQQ", "QQQ"]
    assert "1 earlier completed trade not booked" in capsys.readouterr().out
    assert not any("SPY" in m for m in sent)
    assert "practice_held" not in state
    # an other-track trade with no start of its own starts it now
    handle(pair(at(10, 0), at(11, 0), track="other"), state=state)
    assert len(trades(home)) == 2
    assert state["practice_since"]["other"] == NOW.timestamp()


def test_practice_starts_at_the_first_practice_read_and_ends_with_live(clock):
    saved = []
    state = {"positions": {}}
    agent._note_practice_since(CFG, state, saved.append)
    assert state["practice_since"] == {"main": NOW.timestamp(),
                                       "other": NOW.timestamp()}
    clock["t"] = NOW + timedelta(hours=1)
    agent._note_practice_since(CFG, state, saved.append)
    assert state["practice_since"]["main"] == NOW.timestamp() and len(saved) == 1
    # other trades off: "other" starts again when they are turned on
    agent._note_practice_since({**CFG, "include_other_trades": False}, state,
                               saved.append)
    assert state["practice_since"] == {"main": NOW.timestamp()}
    # a paused (off) read changes nothing; a LIVE read ends practice
    agent._note_practice_since({**CFG, "paused": True}, state, saved.append)
    assert state["practice_since"] == {"main": NOW.timestamp()}
    agent._note_practice_since({**CFG, "dry_run": False}, state, saved.append)
    assert "practice_since" not in state


def test_the_ai_policy_never_sees_a_later_exit(home, sent, clock, monkeypatch):
    """BBB's signal at 11:00 is judged while AAA (10:00 -> 14:00) is still
    open: nothing about AAA's exit may reach the policy."""
    import llm_policy
    calls = []

    def evaluate(cfg, ev, log, context=None):
        calls.append((ev, log, context))
        return {"act": True, "reason": "ok"}
    monkeypatch.setattr(llm_policy, "evaluate", evaluate)
    handle(pair(at(10, 0), at(14, 0), ticker="AAA")
           + pair(at(11, 0), at(12, 0), ticker="BBB"))
    (_ev1, log1, _c1), (ev2, log2, _c2) = calls
    assert ev2["ticker"] == "BBB" and [r["action"] for r in log2] == ["entry"]
    shown = json.dumps(log2)
    assert "→ exit" not in shown and "exit_at" not in shown
    assert "logged_at" not in shown and "practice_exit" not in shown
    assert at(14, 0)[:16] not in shown
    assert log2[0]["reason"] == "practice entry · $500.00 budget"


def test_held_halves_are_retried_on_a_read_without_new_releases(home, sent, clock):
    state = {"positions": {}, "practice_held": pair(at(10, 2), at(14, 31))}
    handle([], state=state)
    assert [r["action"] for r in trades(home)] == ["entry", "exit"]
    assert "practice_held" not in state


@pytest.mark.parametrize("opened_by", [0, 2])
def test_only_its_own_round_trip_closes_a_held_position(home, sent, clock, opened_by):
    """Two round trips in the held contract in one read (the agent was
    offline): the first exit no older than the row's opening signal sells
    it; the other round trip is a practice trade."""
    events = parse(wire("ENTERED", at(10, 0)), wire("EXITED", at(11, 0), pnl=10.0),
                   wire("ENTERED", at(12, 0)), wire("EXITED", at(13, 0), pnl=-10.0))
    key = f"SPY|{EXPIRY}|600.0|C"
    broker = Broker()
    state = {"positions": {key: {"option_id": "stub", "qty": 1, "live": True,
                                 "book": "main", "track": "main",
                                 "opened_event": events[opened_by]["event_id"]}}}
    handle(events, state=state, broker=broker)
    assert [c[0] for c in broker.calls] == ["sell"]
    assert state["positions"] == {}
    real = [r for r in rows(home) if not r.get("practice_release")]
    practice = [r for r in rows(home) if r.get("practice_release")]
    assert [r["action"] for r in real] == ["exit"]
    booked = ("10:00", "11:00") if opened_by == 2 else ("12:00", "13:00")
    assert [r["action"] for r in practice] == ["entry", "exit"]
    assert f"signal {booked[0]} → exit {booked[1]}" in practice[0]["reason"]
    assert "practice_held" not in state


def test_the_losing_streak_counts_practice_results_without_dollars(
        home, sent, clock):
    """A fixed contract count books no dollars: the losing-streak rule
    counts the recorded result's sign."""
    handle(pair(at(10, 0), at(11, 0), pnl=-20.0, ticker="AAA")
           + pair(at(12, 0), at(13, 0), ticker="BBB"),
           cfg={"sizing_mode": "contracts", "contracts_per_trade": 1,
                "rule_loss_streak": 1})
    got = rows(home)
    assert [r["action"] for r in got] == ["entry", "exit", "skip_rule"]
    assert got[2]["ui"]["code"] == "rule_loss_streak"
