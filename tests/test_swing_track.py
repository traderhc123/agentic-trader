"""Track "swing": a third book beside the main pick and "other trades".

The same event schema as the other tracks; taken only with
include_swing_trades on (off by default), handled the way "other trades"
are: its own book, so its exit sells only its own position; with the
setting off, a held position's exit still reaches it. Zero network."""
from datetime import datetime, timedelta, timezone

import pytest

import agent
import setup_plan
import view_model as vm
from conftest import StubBroker, make_event
from sources import agenthc_day_trade_ideas as hc
from test_cross_book_exit import Book
from test_feed_reads_exit_signals import FAR, Server, _loop, _setup
from test_feed_reads_exit_signals import ev as feed_ev
from test_v2_settings_api import BASE, app, read_cfg, write_cfg  # noqa: F401

KEY = "ABC|2026-07-10|10.0|C"
CONTRACT = "ABC 2026-07-10 $10.0 C"
LIVE = {"dry_run": False, "max_entries_per_day": 20, "max_open_positions": 10,
        "include_swing_trades": True}


def ev(event, track, eid):
    return make_event(event=event, ticker="ABC", strike=10.0, track=track,
                      event_id=eid)


def run(b, state, e, cfg=LIVE):
    return agent.handle_event(dict(e), cfg, state, b, object(), lambda s: None)


# ── the setting ─────────────────────────────────────────────────────────────

def test_swing_is_off_by_default_its_entries_dropped_once(home, monkeypatch,
                                                          no_policy):
    cfg, state = _setup(monkeypatch, other=True)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [
        feed_ev("ENTERED", "ABC", 10.0, "swing", t0),
        feed_ev("ENTERED", "DEF", 20.0, "other", t0),
        feed_ev("ENTERED", "GHI", 30.0, "main", t0)]})
    b = StubBroker()
    agent._handle_feed_events(events, cfg, state, b, None, lambda s: None)
    assert sorted(c["ticker"] for c in b.calls) == ["DEF", "GHI"]
    abc = next(e["event_id"] for e in events if e["ticker"] == "ABC")
    assert abc in state["seen"]                  # not looked at again


def test_swing_on_takes_its_entries_but_not_other_trades(home, monkeypatch,
                                                         no_policy):
    cfg, state = _setup(monkeypatch, other=False)
    cfg["include_swing_trades"] = True
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [
        feed_ev("ENTERED", "ABC", 10.0, "swing", t0),
        feed_ev("ENTERED", "DEF", 20.0, "other", t0)]})
    b = StubBroker()
    agent._handle_feed_events(events, cfg, state, b, None, lambda s: None)
    assert [c["ticker"] for c in b.calls] == ["ABC"]
    row = state["positions"][f"ABC|{FAR}|10.0|C"]
    assert row["book"] == "swing" and row["track"] == "swing"


def test_swing_off_still_reads_a_held_swing_exit(home, monkeypatch, no_policy):
    cfg, state = _setup(monkeypatch, other=False)
    cfg["include_swing_trades"] = True
    b, srv = StubBroker(), Server()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    srv.lines.append(feed_ev("ENTERED", "ABC", 10.0, "swing", t0))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    key = f"ABC|{FAR}|10.0|C"
    assert key in state["positions"]
    cfg["include_swing_trades"] = False          # owner: swing off
    srv.lines.append(feed_ev("ENTERED", "DEF", 20.0, "swing", t0 + timedelta(seconds=20)))
    srv.lines.append(feed_ev("EXITED", "ABC", 10.0, "swing", t0 + timedelta(seconds=30)))
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "all"
    assert key not in state["positions"]         # sold on its exit signal
    assert [c["ticker"] for c in b.calls] == ["ABC", "ABC"]   # DEF never bought
    _loop(cfg, state, b, srv, monkeypatch)
    assert srv.asked[-1]["track"] == "main"


def test_feed_track_follows_the_setting_and_the_ledger():
    p = lambda cfg, st: hc._feed_params(cfg, st)["track"]  # noqa: E731
    assert p({}, {"positions": {}}) == "main"
    assert p({"include_swing_trades": True}, {"positions": {}}) == "all"
    assert p({}, {"positions": {"a": {"track": "swing"}}}) == "all"
    assert p({}, {"positions": {"a": {"book": "swing"}}}) == "all"
    assert p({}, {"positions": {}, "exits_to_retry": [
        {"event": "EXITED", "track": "swing"}]}) == "all"


# ── its own book ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("row,book,track", [
    ({"track": "swing"}, "swing", "swing"), ({"book": "swing"}, "swing", "swing"),
    ({"book": "swing", "track": "swing"}, "swing", "swing"),
    ({"book": "swing", "track": "other"}, "swing", "swing"),
    ({"book": "shadow", "track": "swing"}, "shadow", "swing")])
def test_the_source_and_the_agent_agree_on_a_swing_row(row, book, track):
    assert agent._row_book(row) == book
    assert hc._row_track(row) == track
    assert hc.held_as_other(row) is True


def test_a_shadow_row_of_an_older_version_still_follows_its_own_exit(
        home, no_policy):
    # older versions stamped every non-main track "shadow"
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2,
                                 "book": "shadow", "track": "swing"}}}
    assert agent._row_track(state["positions"][KEY]) == "swing"
    out = run(b, state, ev("EXITED", "other", "o-out"))
    assert "no order placed" in out and b.sells == []
    assert run(b, state, ev("EXITED", "swing", "s-out")) == f"EXIT: {CONTRACT}"
    assert b.sells == [("s-out", 2, 2)]


def test_a_swing_exit_sells_only_the_swing_position(home, no_policy):
    b, state = Book(), {"positions": {}}
    cfg = {**LIVE, "include_other_trades": True}
    run(b, state, ev("ENTERED", "swing", "s-in"), cfg)
    assert state["positions"][KEY]["book"] == "swing"
    for track, eid in (("main", "m-out"), ("other", "o-out")):
        out = run(b, state, ev("EXITED", track, eid), cfg)
        assert "no order placed" in out and '"Swing Trade"' in out
    assert b.sells == [] and state["positions"][KEY]["qty"] == 2
    assert run(b, state, ev("EXITED", "swing", "s-out"), cfg) == f"EXIT: {CONTRACT}"
    assert b.sells == [("s-out", 2, 2)] and state["positions"] == {}


@pytest.mark.parametrize("book,track", [("main", "main"), ("shadow", "other")])
def test_a_swing_exit_never_sells_another_books_position(home, no_policy,
                                                         book, track):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": book,
                                 "track": track}}}
    out = run(b, state, ev("EXITED", "swing", "s-out"))
    assert "no order placed" in out and '"Swing Trade"' in out
    assert b.sells == []


def test_trade_rows_name_the_swing_book(home, no_policy):
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", "swing", "s-in"))
    run(b, state, ev("EXITED", "swing", "s-out"))
    rows = agent._recent_trades(10)
    assert [r["ui"]["book"] for r in rows if r["action"] in ("entry", "exit")] \
        == ["swing", "swing"]
    assert [vm._book_of(r, r["ui"]) for r in rows
            if r["action"] in ("entry", "exit")] == ["swing", "swing"]


def test_swing_is_sized_as_other_trades_are():
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
           "other_sizing_mode": "budget", "other_budget_per_trade_usd": 100}
    assert agent._sizing_cfg(cfg, {"track": "main"}) is cfg
    for track in ("other", "swing"):
        assert agent._sizing_cfg(cfg, {"track": track})["budget_per_trade_usd"] == 100


def test_a_swing_budget_skip_is_an_other_trades_budget_hint():
    today = "2026-07-01"
    cfg = {"sizing_mode": "budget", "budget_per_trade_usd": 500,
           "other_sizing_mode": "budget", "other_budget_per_trade_usd": 100}
    rows = [{"action": "skip_budget", "ts": f"{today}T15:00:00Z", "book": "swing",
             "reason": "1 contract ≈ $240 is over the budget"}]
    hints = agent._budget_hints(cfg, trades=rows, today=today)
    assert [(h["book"], h["budget"]) for h in hints] == [("shadow", 100.0)]


def test_dropped_signals_are_logged_per_track(home, monkeypatch, capsys,
                                              no_policy):
    cfg, state = _setup(monkeypatch, other=False)
    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)
    events = hc._parse_events({"events": [
        feed_ev("ENTERED", "ABC", 10.0, "swing", t0),
        feed_ev("ENTERED", "DEF", 20.0, "other", t0)]})
    agent._handle_feed_events(events, cfg, state, StubBroker(), None,
                              lambda s: None)
    out = capsys.readouterr().out
    assert 'ignored 1 "other trades" signal(s): other trades are off' in out
    assert 'ignored 1 "Swing Trade" signal(s): Swing Trade is off' in out


# ── the clocks the other tracks keep ────────────────────────────────────────

def test_watching_clock_starts_and_restarts_with_the_setting(monkeypatch):
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    agent.note_watching({"include_swing_trades": True}, now=t0)
    assert agent._WATCH_SINCE["swing"] == t0 and "other" not in agent._WATCH_SINCE
    agent.note_watching({"include_swing_trades": False})
    assert "swing" not in agent._WATCH_SINCE
    agent.note_watching({"include_swing_trades": True})
    assert agent._WATCH_SINCE["swing"] > t0


def test_practice_clock_follows_the_setting(monkeypatch):
    monkeypatch.setattr(agent, "_feed_mode", lambda cfg, state=None: "dry_run")
    state = {}
    agent._note_practice_since({"include_swing_trades": True}, state, lambda s: None)
    assert set(state["practice_since"]) == {"main", "swing"}
    agent._note_practice_since({}, state, lambda s: None)
    assert set(state["practice_since"]) == {"main"}


# ── where the setting is changed ────────────────────────────────────────────

def test_chat_command(home):
    agent._save(agent.CONFIG_PATH, {"source": "agenthc"})
    handled, reply = agent._apply_command("swing trades on")
    assert handled and agent._load(agent.CONFIG_PATH, {})["include_swing_trades"] is True
    handled, reply = agent._apply_command("swing trades off")
    assert handled and agent._load(agent.CONFIG_PATH, {})["include_swing_trades"] is False
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    handled, reply = agent._apply_command("swing trades on")
    assert handled and "AgentHC" in reply
    assert "include_swing_trades" not in agent._load(agent.CONFIG_PATH, {})


def test_setup_plan_sets_it_and_a_live_agent_counts_it_as_looser():
    new = {"source": "agenthc", "include_swing_trades": True}
    assert "take \"Swing Trade\" signals" in setup_plan._live_increases(
        {"source": "agenthc"}, new, False)
    assert not any("Swing" in x for x in setup_plan._live_increases(
        {"source": "agenthc", "include_swing_trades": True}, new, False))
    assert setup_plan.EXAMPLE_PLAN["source"]["include_swing_trades"] is False


def _running(c_app):
    return c_app(lambda: {"mode": "DRY-RUN", "fields": {}}, lambda: [],
                 agent._apply_command, lambda: agent._load(agent.CONFIG_PATH, {}))


@pytest.mark.parametrize("on", [True, False])
def test_v2_switch_is_the_chat_command_in_two_steps(home, app, on):
    from test_v2_settings_api import same_effect
    write_cfg(home, BASE)
    c = _running(app)
    start = {**BASE, "include_swing_trades": not on}
    out = same_effect(home, start,
                      lambda: c.post("/api/command",
                                     {"text": f"swing trades {'on' if on else 'off'}"}),
                      lambda: c.confirmed("/api/v2/settings/signals",
                                          {"include_swing_trades": on}))
    assert out["include_swing_trades"] is on
    assert c.get("/api/v2/settings")["signals"]["include_swing_trades"] is on
    write_cfg(home, start)
    ch = c.post("/api/v2/settings/signals",
                {"include_swing_trades": on})["preview"]["changes"][0]
    assert ch["key"] == "include_swing_trades" and ch["label"] == "Swing Trade"
    assert ch["direction"] == ("raises_risk" if on else "lowers_risk")
    assert read_cfg(home) == start               # nothing saved before confirm
    assert c.post("/api/v2/settings/signals",
                  {"include_swing_trades": "yes"})["ok"] is False
    write_cfg(home, {**start, "source": "manual"})
    assert c.post("/api/v2/settings/signals",
                  {"include_swing_trades": on})["ok"] is False


def test_classic_source_form_sets_it_and_other_saves_keep_it(home, app):
    write_cfg(home, {**BASE, "source": "agenthc", "feed_pass": {"token": "t"}})
    c = _running(app)
    assert c.post("/api/source", {"source": "agenthc", "scope": "main",
                                  "swing": True})["ok"]
    assert read_cfg(home)["include_swing_trades"] is True
    assert c.post("/api/source", {"source": "agenthc", "scope": "main"})["ok"]
    assert read_cfg(home)["include_swing_trades"] is True
    assert c.get("/api/state")["include_swing_trades"] is True
    assert c.post("/api/source", {"source": "agenthc", "scope": "main",
                                  "swing": False})["ok"]
    assert read_cfg(home)["include_swing_trades"] is False


def test_support_report_says_it(tmp_path, monkeypatch):
    import net_check
    import support
    monkeypatch.setattr(support, "connection_check", lambda host: [])
    monkeypatch.setattr(net_check, "proxy_for", lambda url: "")
    cfg = {"source": "agenthc", "include_swing_trades": True}
    text = support.report(str(tmp_path), lambda: ([], cfg), cfg, "localhost")
    assert "swing_trades=on" in text


@pytest.mark.parametrize("saved", [True, False])
def test_terminal_setup_enter_keeps_the_saved_setting(home, monkeypatch, capsys,
                                                      saved):
    from test_setup_keeps_saved import BASE as SETUP_BASE, _drive
    cfg, asked, _, _ = _drive(home, monkeypatch, capsys,
                              {**SETUP_BASE, "broker": "alpaca",
                               "include_swing_trades": saved})
    assert cfg["include_swing_trades"] is saved
    assert any("Swing Trade" in p and f"default {'y' if saved else 'n'}" in p
               for p in asked)
