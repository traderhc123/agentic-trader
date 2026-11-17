"""Audit PR1: when practice or live was never chosen (no `dry_run` in
config.json — the Safety step was never saved), doctor, the dashboard and
handle_event must agree on the mode, and an unchosen mode must never place
a real order.

Operator decision: no `dry_run` and no live trading history -> practice
(`dry_run: true` is saved when the agent starts). No `dry_run` but the setup
HAS traded live (a real entry/exit row, or a real position in the ledger)
-> it keeps live, `dry_run: false` is saved explicitly and the owner is
told once. Never practice -> live by itself.

Before: handle_event read a missing key as LIVE and sent real orders while
`agent.py doctor` / `doctor --json` said DRY-RUN."""

import json

import pytest

import agent
import webui
from conftest import StubBroker, make_event
from test_v2_parity import starting  # noqa: F401
from test_v2_settings_api import BASE, app, read_cfg, write_cfg  # noqa: F401

# a config as the web wizard leaves it after the Source + Broker steps
UNSET = {"source": "manual", "broker": "alpaca", "alpaca_key_id": "PKTEST",
         "alpaca_secret": "x", "alpaca_paper": False}

LIVE_ROW = {"action": "entry", "event_id": "old-1",
            "contract": "SPY 2026-10-01 $500.0 C", "dry": False,
            "ts": "2026-10-01T14:00:00+00:00"}
PRACTICE_ROW = {**LIVE_ROW, "dry": True}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify",
                        lambda cfg, text, **k: sent.append(text))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    return sent


def _write(home, cfg, rows=()):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if rows:
        (home / "trades.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _disk(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def _doctor_json(monkeypatch, capsys, cfg):
    monkeypatch.setattr(agent, "doctor_checks", lambda: ([], cfg))
    with pytest.raises(SystemExit):
        agent.cmd_doctor(as_json=True)
    return json.loads(capsys.readouterr().out)


def _entry(cfg, state=None):
    b = StubBroker()
    state = {"positions": {}} if state is None else state
    agent.handle_event(make_event(), cfg, state, b, object(), lambda s: None)
    return b.calls


def test_never_chosen_is_practice_everywhere(home, no_policy, monkeypatch, capsys):
    """REGRESSION: doctor said DRY-RUN while handle_event sent a real order."""
    _write(home, UNSET)
    cfg = agent._load(agent.CONFIG_PATH)
    assert _doctor_json(monkeypatch, capsys, cfg)["mode"] == "DRY-RUN"
    assert agent._mode(cfg) == "practice"
    assert agent._mode_label(cfg, {}).startswith("DRY-RUN")
    assert _entry(cfg) == []  # no real order
    rows = [json.loads(x) for x in
            (home / "trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "entry" and rows[-1]["dry"] is True
    assert rows[-1]["ui"]["mode"] == "practice"


def test_practice_history_only_is_still_practice(home, no_policy, monkeypatch,
                                                 capsys):
    _write(home, UNSET, [PRACTICE_ROW])
    cfg = agent._load(agent.CONFIG_PATH)
    assert _doctor_json(monkeypatch, capsys, cfg)["mode"] == "DRY-RUN"
    assert _entry(cfg) == []


@pytest.mark.parametrize("history", ["trade_row", "real_position"])
def test_never_chosen_with_live_history_stays_live(home, no_policy, monkeypatch,
                                                    capsys, history):
    """A setup that has been trading live with no key keeps trading live —
    and doctor now says so (it said DRY-RUN)."""
    state = {"positions": {}}
    if history == "trade_row":
        _write(home, UNSET, [PRACTICE_ROW, LIVE_ROW])
    else:
        _write(home, UNSET)
        state["positions"]["QQQ|2026-12-18|500.0|P"] = {
            "option_id": "abc", "qty": 1, "live": True}
        (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    cfg = agent._load(agent.CONFIG_PATH)
    doc = _doctor_json(monkeypatch, capsys, cfg)
    assert doc["mode"] == "LIVE" and doc["mode_chosen"] is False
    assert agent._mode(cfg) == "live"
    assert len(_entry(cfg, state)) == 1  # unchanged: it was live before


def test_doctor_text_names_the_mode_it_will_trade_in(home, monkeypatch, capsys):
    _write(home, UNSET, [LIVE_ROW])
    cfg = agent._load(agent.CONFIG_PATH)
    monkeypatch.setattr(agent, "doctor_checks", lambda: ([], cfg))
    agent.cmd_doctor()
    mode = [x for x in capsys.readouterr().out.splitlines()
            if x.startswith("mode:")][0]
    assert "LIVE ORDERS" in mode and "never chosen" in mode


def test_settle_saves_practice_once(home, quiet):
    _write(home, {**UNSET, "max_entries_per_day": 2})
    cfg = agent._settle_dry_run(agent._load(agent.CONFIG_PATH), {"positions": {}})
    assert cfg["dry_run"] is True
    assert _disk(home) == {**UNSET, "max_entries_per_day": 2, "dry_run": True}
    assert len(quiet) == 1 and "PRACTICE" in quiet[0]
    agent._settle_dry_run(_disk(home), {"positions": {}})  # chosen now
    assert len(quiet) == 1


def test_settle_keeps_live_history_live_and_tells_the_owner(home, quiet):
    _write(home, UNSET, [LIVE_ROW])
    cfg = agent._settle_dry_run(agent._load(agent.CONFIG_PATH), {"positions": {}})
    assert cfg["dry_run"] is False and _disk(home)["dry_run"] is False
    assert len(quiet) == 1 and "stays LIVE" in quiet[0]


@pytest.mark.parametrize("saved", [True, False])
def test_a_chosen_mode_is_never_changed(home, quiet, saved):
    # live history with practice chosen stays practice; no history with
    # live chosen stays live
    _write(home, {**UNSET, "dry_run": saved}, [LIVE_ROW] if saved else [])
    cfg = agent._load(agent.CONFIG_PATH)
    assert agent._settle_dry_run(cfg, {"positions": {}}) is cfg
    assert agent._is_dry(cfg) is saved
    assert _disk(home)["dry_run"] is saved and quiet == []


def test_cmd_run_saves_the_mode_before_trading(home, monkeypatch):
    """`python agent.py`, start and start-at-login all run cmd_run: the mode
    is in config.json before the first signal is read."""
    _write(home, UNSET)
    import keep_running
    import llm_policy
    import transfer

    class Lock:
        def acquire(self, wait=0):
            return True

    class Stop(Exception):
        pass

    def stop_here(*a, **k):
        raise Stop
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(keep_running, "InstanceLock", Lock)
    monkeypatch.setattr(keep_running, "clear_stop", lambda: None)
    monkeypatch.setattr(keep_running, "clear_alive", lambda: None)
    monkeypatch.setattr(keep_running, "stop_on_sigterm", lambda: None)
    monkeypatch.setattr(transfer, "moved_away", lambda: None)
    monkeypatch.setattr(agent.BROKERS["alpaca"], "client", lambda c: object())
    # checked right after the mode is settled: stop there
    monkeypatch.setattr(llm_policy, "entries_blocked", stop_here)
    with pytest.raises(Stop):
        agent.cmd_run()
    assert _disk(home)["dry_run"] is True


def test_v2_start_never_chosen_starts_in_practice(home, app, starting):  # noqa: F811
    """/v2 Start with no mode chosen: practice, saved before the agent
    starts. Before, it asked to confirm a LIVE start the owner never chose
    (and the agent then traded live)."""
    write_cfg(home, {k: v for k, v in BASE.items() if k != "dry_run"})
    c = app()
    r = c.post("/api/v2/agent/start", {})
    assert r.get("ok") is True and "needs_confirm" not in r
    assert starting == [("start", webui.APP_PORT)]
    assert read_cfg(home)["dry_run"] is True


def test_v2_start_never_chosen_with_live_history_needs_the_confirmation(
        home, app, starting):  # noqa: F811
    write_cfg(home, {k: v for k, v in BASE.items() if k != "dry_run"})
    (home / "trades.jsonl").write_text(json.dumps(LIVE_ROW) + "\n",
                                       encoding="utf-8")
    c = app()
    r = c.post("/api/v2/agent/start", {})
    assert r["needs_confirm"] is True and starting == []
    assert r["preview"]["mode"] == "live"
    r = c.post("/api/v2/agent/start", {"confirm": r["preview"]["token"]})
    assert r["ok"] is True and starting == [("start", webui.APP_PORT)]
    assert read_cfg(home)["dry_run"] is False


# ── review fixups ───────────────────────────────────────────────────────────

def test_unwritable_config_keeps_the_first_decision_for_the_run(
        home, no_policy, quiet, monkeypatch, capsys):
    """REGRESSION: config.json readable but not writable, so the key is never
    saved and every loop reloads a config without it. The trade log can't be
    read at start (no history: practice); later it can and holds a live row.
    The next loop must still be practice — never practice -> live by
    itself — and "could not save the mode" is printed once, not every loop."""
    _write(home, UNSET)
    (home / "trades.jsonl").mkdir()  # can't be read as a file at start

    def read_only(change, if_damaged=None):
        raise PermissionError("config.json is read-only")
    monkeypatch.setattr(agent, "update_config", read_only)
    state = {"positions": {}}
    cfg = agent._load(agent.CONFIG_PATH)
    cfg = agent._settle_dry_run(cfg, state)                # cmd_run start
    assert cfg["dry_run"] is True
    (home / "trades.jsonl").rmdir()
    _write(home, UNSET, [LIVE_ROW])                       # readable now: live
    for _ in range(2):                                    # the run loop
        cfg = agent._load(agent.CONFIG_PATH, cfg) or cfg
        assert agent._dry_run_unset(cfg)                  # still not saved
        cfg = agent._settle_dry_run(cfg, state)
        assert cfg["dry_run"] is True
    keyless = agent._load(agent.CONFIG_PATH)
    assert agent._is_dry(keyless) is True and agent._mode(keyless) == "practice"
    assert _entry(keyless) == []                          # no real order
    assert capsys.readouterr().out.count("could not save the mode") == 1
    assert "dry_run" not in _disk(home)


def test_tests_never_read_the_real_agent_home():
    """REGRESSION: tests without the `home` fixture logged rows into the real
    ~/.agentic-trader/trades.jsonl, and a never-chosen mode read that file's
    live rows, so results depended on the machine and the test order."""
    import os
    real = os.path.expanduser("~/.agentic-trader")
    for p in (agent.HOME, agent.CONFIG_PATH, agent.STATE_PATH,
              agent.TRADES_PATH, os.environ.get("AGENT_HOME", real)):
        assert not os.path.abspath(p).startswith(os.path.abspath(real))
    assert agent._is_dry({}) is True and agent._mode(None) == "practice"


def test_safety_note_and_move_report_the_mode_the_agent_trades_in(
        home, tmp_path, monkeypatch):
    """The wizard's "Safety saved" note and a moved setup's was_live read
    a never-chosen mode as the agent does (_is_dry), not with a default of
    their own."""
    import transfer
    cfg = {**UNSET, "max_entries_per_day": 3}
    assert "DRY-RUN" in webui._restore_notes(cfg)["safety"]
    _write(home, cfg, [LIVE_ROW])
    assert "LIVE" in webui._restore_notes(cfg)["safety"]

    def move(rows):
        old, new = tmp_path / f"old{len(rows)}", tmp_path / f"new{len(rows)}"
        old.mkdir()
        (old / "config.json").write_text(json.dumps(UNSET), encoding="utf-8")
        (old / "trades.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        monkeypatch.setenv("AGENT_HOME", str(old))
        blob = transfer.export_bundle("correct horse battery")
        monkeypatch.setenv("AGENT_HOME", str(new))
        return transfer.import_bundle(blob, "correct horse battery")
    assert move([PRACTICE_ROW])["was_live"] is False   # never traded live
    assert move([PRACTICE_ROW, LIVE_ROW])["was_live"] is True
