"""Dashboard, settings and chat-command fixes from the audit (W1-W7).

Each test reproduces one way an owner lost control of their agent:
a settings save that undid a chat command, a form that reset the mode, a
switch to dry-run that abandoned a real position, a pause that also
stopped exits, a question that changed a rule, a secret sent to the AI.
"""

import json
import re
from datetime import datetime

import pytest

import agent
import webui
from conftest import StubBroker, make_event


def _write_cfg(home, cfg):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _cfg(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


@pytest.fixture
def app(home, monkeypatch):
    """start(**cfg) -> the settings/dashboard server's routes, with the
    config written first. No port is opened."""
    made = {}

    class NoPort:
        def __init__(self, addr, handler):
            made["handler"] = handler

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(webui, "_AppServer", NoPort)

    def start(cfg, running=True):
        _write_cfg(home, cfg)
        if running:
            webui.start_app(lambda: {"mode": "TEST", "fields": {}}, lambda: [],
                            agent._apply_command,
                            lambda: agent._load(agent.CONFIG_PATH, {}) or {})
        else:
            webui.start_app()
        return made["handler"]
    return start


LIVE = {"source": "agenthc", "broker": "stub", "dry_run": False,
        "max_entries_per_day": 2, "max_daily_loss_usd": 300.0,
        "sizing_mode": "budget", "budget_per_trade_usd": 500.0}


# ── W1: a settings save never undoes a chat command ──────────────────────────

def test_w1_settings_save_keeps_what_chat_commands_changed(home, app):
    h = app(LIVE)
    for cmd in ("dry on", "set cap 1", "set max loss 100"):
        assert agent._apply_command(cmd)[0]
    assert _cfg(home)["dry_run"] is True

    # any later save by the settings server, e.g. the recurring day-pass switch
    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]

    saved = _cfg(home)
    assert saved["day_pass_recurring"] is True          # its own key: saved
    assert saved["dry_run"] is True                      # not back to LIVE
    assert saved["max_entries_per_day"] == 1
    assert saved["max_daily_loss_usd"] == 100.0


def test_w1_pass_status_poll_does_not_restore_old_settings(home, app, monkeypatch):
    from sources import agenthc_day_trade_ideas as hc
    h = app({**LIVE, "feed_pass_pending": {"token": "tok-1", "days": 5,
                                           "bought_at": 0}})
    agent._apply_command("dry on")
    monkeypatch.setattr(hc, "pass_status", lambda token: {
        "status": "active", "days_left": 5, "access_until": "2026-12-01"})

    assert h.routes_get["/api/pass/status"](None)["status"] == "active"

    saved = _cfg(home)
    assert saved["feed_pass"]["token"] == "tok-1"        # the pass was promoted
    assert "feed_pass_pending" not in saved
    assert saved["dry_run"] is True                      # and nothing else moved


def test_w1_settings_pages_see_what_chat_commands_changed(home, app):
    h = app(LIVE)
    agent._apply_command("set budget 750")
    assert h.routes_get["/api/state"](None)["sizing_detail"]["main"]["value"] == 750


def test_w1_a_request_that_failed_leaves_nothing_behind_for_the_next_save(
        home, app, monkeypatch):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    h = app(LIVE)
    # rejected (bad URL) after the handler had already put "url" in its copy
    assert not h.routes_post["/api/source"](
        None, {"source": "url", "source_url": "ftp://nope"})["ok"]
    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]
    assert _cfg(home)["source"] == "agenthc"


def test_w1_commands_and_settings_saves_at_the_same_time_lose_nothing(home, app):
    """The chat box and the settings pages write from different threads."""
    import threading
    h = app(LIVE)
    errors = []

    def commands():
        try:
            for n in range(1, 41):
                assert agent._apply_command(f"set cap {n}")[0]
                assert agent._apply_command(f"set positions {n}")[0]
        except Exception as exc:  # surfaced below: a thread can't fail the test
            errors.append(exc)

    def settings():
        try:
            for n in range(40):
                assert h.routes_post["/api/wallet/recurring"](None, {"on": n % 2 == 0})["ok"]
                assert h.routes_post["/api/source/scope"](
                    None, {"scope": "all" if n % 2 else "main"})["ok"]
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=commands), threading.Thread(target=settings)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    saved = _cfg(home)                                   # still valid JSON
    assert saved["max_entries_per_day"] == 40 and saved["max_open_positions"] == 40
    assert saved["day_pass_recurring"] is False and saved["include_other_trades"] is True
    assert saved["dry_run"] is False and saved["budget_per_trade_usd"] == 500.0


def test_w1_command_refuses_to_save_over_settings_it_could_not_read(home, monkeypatch):
    _write_cfg(home, LIVE)
    before = (home / "config.json").read_text(encoding="utf-8")

    def locked(*a, **k):
        raise PermissionError("file is locked by another program")
    monkeypatch.setattr(agent, "_read_config", locked)
    handled, reply = agent._apply_command("dry on")
    assert handled and "nothing was changed" in reply.lower()
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_w1_read_config_tells_unreadable_and_damaged_from_empty(home, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    assert agent._read_config() == {}                    # no file yet: the only {}
    for text in ("{not json", '{"dry_run": true,}', "", "[1, 2]", "null"):
        (home / "config.json").write_text(text, encoding="utf-8")
        with pytest.raises(agent.ConfigDamaged) as err:  # a typo is not "empty"
            agent._read_config()
        assert "Nothing was changed" in str(err.value)
        assert str(home / "config.json") in str(err.value)
    _write_cfg(home, LIVE)
    assert agent._read_config()["max_entries_per_day"] == 2
    real_open = open

    def flaky(path, *a, **k):
        if str(path) == agent.CONFIG_PATH:
            raise PermissionError("locked")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", flaky)
    with pytest.raises(OSError) as err:
        agent._read_config()
    assert not isinstance(err.value, agent.ConfigDamaged)
    assert "nothing was changed" in str(err.value)


WITH_KEYS = {**LIVE, "dry_run": True, "lnbits_url": "https://demo.lnbits.com",
             "lnbits_admin_key": "adm-k3y-51",
             "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=u-1",
             "rule_tickers": ["SPY"]}


def _hand_edit_with_a_typo(home, cfg):
    """config.json as an owner leaves it after a manual edit: a trailing
    comma. Returns the file's text."""
    text = json.dumps(cfg, indent=2).rstrip().rstrip("}") + ",\n}\n"
    (home / "config.json").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        json.loads(text)
    return text


def test_w1_a_typo_in_config_json_is_never_saved_over(home, app, monkeypatch):
    """The reviewer's scenario: dry-run owner, agent running, a trailing
    comma left in config.json. No button or command may then replace the
    file with its own few keys (which lost the wallet key, the limits and
    dry-run, so the agent went LIVE on defaults)."""
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    h = app(WITH_KEYS)                                   # the agent is running
    broken = _hand_edit_with_a_typo(home, WITH_KEYS)

    r = h.routes_post["/api/wallet/recurring"](None, {"on": True})
    assert r["ok"] is False and "typo" in r["error"]
    assert "Nothing was changed" in r["error"]
    for cmd in ("pause", "resume", "dry off", "set cap 9", "tickers QQQ"):
        handled, reply = agent._apply_command(cmd)
        assert handled and "Nothing was changed" in reply and "typo" in reply, cmd
    assert h.routes_post["/api/safety"](None, {"rule_tickers": "QQQ"})["ok"] is False
    assert (home / "config.json").read_text(encoding="utf-8") == broken
    assert not [p.name for p in home.iterdir() if ".damaged" in p.name]

    # the owner fixes the typo: everything is still there, and saves work again
    _write_cfg(home, WITH_KEYS)
    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]
    assert agent._apply_command("pause")[0]
    assert _cfg(home) == {**WITH_KEYS, "day_pass_recurring": True, "paused": True}


def test_w1_a_refused_save_leaves_nothing_behind_in_the_settings_pages(
        home, app, monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    h = app(WITH_KEYS)
    _hand_edit_with_a_typo(home, WITH_KEYS)
    assert not h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]
    _write_cfg(home, WITH_KEYS)                          # fixed
    assert h.routes_get["/api/state"](None)["recurring"] is False
    assert h.routes_post["/api/source/scope"](None, {"scope": "all"})["ok"]
    assert "day_pass_recurring" not in _cfg(home)        # the refused change is gone


def test_w1_setup_wizard_may_start_over_a_damaged_config_and_keeps_a_copy(
        home, app, monkeypatch, capsys):
    """Only the setup wizard, with no agent running, writes a new file —
    from the settings it last read — and the damaged one is kept."""
    import keep_running
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    h = app(WITH_KEYS, running=False)
    broken = _hand_edit_with_a_typo(home, WITH_KEYS)

    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]

    assert _cfg(home) == {**WITH_KEYS, "day_pass_recurring": True}
    kept = [p for p in home.iterdir() if p.name.startswith("config.json.damaged-")]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == broken
    assert kept[0].name in capsys.readouterr().out       # the owner is told where


def test_w1_setup_wizard_leaves_a_damaged_config_alone_while_an_agent_runs(
        home, app, monkeypatch):
    import keep_running
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    monkeypatch.setattr(keep_running, "is_running", lambda: True)
    h = app(WITH_KEYS, running=False)
    broken = _hand_edit_with_a_typo(home, WITH_KEYS)
    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"] is False
    assert (home / "config.json").read_text(encoding="utf-8") == broken


def test_w1_main_loop_keeps_its_settings_when_config_json_has_a_typo(home):
    """What the reply promises: the agent keeps running on the last settings
    it could read (the loop's own re-read, unchanged from before)."""
    _hand_edit_with_a_typo(home, WITH_KEYS)
    running_on = dict(WITH_KEYS)
    assert (agent._load(agent.CONFIG_PATH, running_on) or running_on) is running_on


def test_w1_stop_works_even_when_settings_cannot_be_read(home, monkeypatch):
    monkeypatch.setitem(webui.CONTROLS, "stop", False)

    def locked(*a, **k):
        raise PermissionError("file is locked by another program")
    monkeypatch.setattr(agent, "_read_config", locked)
    handled, reply = agent._apply_command("stop")
    assert handled and reply.startswith("Stopping")
    assert webui.CONTROLS["stop"] is True
    # and a command that does need the settings says how to stop
    assert "type: stop" in agent._apply_command("pause")[1]


def _in_thread(fn):
    """Run fn() in a thread; returns (thread, result box)."""
    import threading
    box = {}

    def run():
        try:
            box["result"] = fn()
        except Exception as exc:  # surfaced by the test
            box["error"] = exc
    t = threading.Thread(target=run)
    t.start()
    return t, box


def test_w1_a_failed_request_cannot_ride_along_with_a_request_still_running(
        home, app, monkeypatch):
    """The reviewer's scenario: a slow pass-status poll is in flight; a bad
    feed address is rejected after "url" was already put in the copy; the
    poll's own save must not write it."""
    import threading
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    h = app({**LIVE, "feed_pass_pending": {"token": "tok-1", "days": 5,
                                           "bought_at": 0}})
    asked, answer = threading.Event(), threading.Event()

    def slow_status(token):
        asked.set()
        assert answer.wait(30)
        return {"status": "active", "days_left": 5, "access_until": "2026-12-01"}
    monkeypatch.setattr(hc, "pass_status", slow_status)
    poll, box = _in_thread(lambda: h.routes_get["/api/pass/status"](None))
    assert asked.wait(30)

    assert not h.routes_post["/api/source"](
        None, {"source": "url", "source_url": "ftp://nope"})["ok"]
    answer.set()
    poll.join(30)

    assert box.get("result", {}).get("status") == "active", box
    saved = _cfg(home)
    assert saved["feed_pass"]["token"] == "tok-1"        # the poll's own change
    assert saved["source"] == "agenthc"                  # not the rejected one
    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]
    assert _cfg(home)["source"] == "agenthc"


def test_w1_a_save_never_writes_what_another_request_is_still_working_on(
        home, app, monkeypatch):
    """A broker connect puts the new broker and keys in the copy, then
    checks them with the broker (seconds). A save by another request in
    that time must not write them — the check may still fail."""
    import threading
    import brokers
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    checking, verdict = threading.Event(), threading.Event()

    class SlowBroker:
        @staticmethod
        def connect(cfg, values):
            cfg["broker"] = "slow"
            cfg["slow_key"] = values.get("key")
            checking.set()
            assert verdict.wait(30)
            return False, "the broker rejected that key"
    monkeypatch.setitem(brokers.BROKERS, "slow", SlowBroker)
    h = app(LIVE)
    conn, box = _in_thread(lambda: h.routes_post["/api/broker/connect"](
        None, {"broker": "slow", "values": {"key": "bad-key"}}))
    assert checking.wait(30)

    assert h.routes_post["/api/wallet/recurring"](None, {"on": True})["ok"]
    mid = _cfg(home)
    verdict.set()
    conn.join(30)

    assert box.get("result", {}).get("ok") is False, box
    for saved in (mid, _cfg(home)):
        assert saved["broker"] == "stub" and "slow_key" not in saved
    assert mid["day_pass_recurring"] is True
    assert h.routes_post["/api/source/scope"](None, {"scope": "all"})["ok"]
    assert _cfg(home)["broker"] == "stub" and "slow_key" not in _cfg(home)
    assert h.routes_get["/api/state"](None)["broker"] == "stub"


def test_w1_sizing_save_still_saves_and_clears(home, app):
    """/api/sizing rebuilds its keys from a copy: what it changes and what
    it removes are both still written."""
    h = app({**LIVE, "other_sizing_mode": "budget",
             "other_budget_per_trade_usd": 50.0})
    assert h.routes_post["/api/sizing"](None, {
        "main": {"mode": "contracts", "value": "2"}, "other": {"mode": "same"}})["ok"]
    saved = _cfg(home)
    assert saved["sizing_mode"] == "contracts" and saved["contracts_per_trade"] == 2
    assert "other_sizing_mode" not in saved and "other_budget_per_trade_usd" not in saved
    assert saved["dry_run"] is False and saved["max_entries_per_day"] == 2


# ── W2: the Safety form keeps the mode and cap it didn't change ──────────────

def test_w2_state_reports_the_current_mode_and_limits(home, app):
    st = app(LIVE).routes_get["/api/state"](None)
    assert st["dry_run"] is False
    assert st["max_entries_per_day"] == 2
    assert st["max_daily_loss_usd"] == 300.0


def test_w2_state_has_no_mode_before_safety_was_ever_saved(home, app):
    st = app({"source": "agenthc"}, running=False).routes_get["/api/state"](None)
    assert st["dry_run"] is None and st["max_entries_per_day"] is None


def test_w2_changing_a_ticker_rule_keeps_live_mode_and_cap(home, app):
    h = app(LIVE)
    # the form sends only what the owner changed
    r = h.routes_post["/api/safety"](None, {"rule_tickers": "SPY, QQQ"})
    assert r["ok"]
    saved = _cfg(home)
    assert saved["rule_tickers"] == ["SPY", "QQQ"]
    assert saved["dry_run"] is False
    assert saved["max_entries_per_day"] == 2
    assert saved["max_daily_loss_usd"] == 300.0


def test_w2_first_safety_save_still_defaults_to_dry_run_and_cap_5(home, app):
    h = app({"source": "agenthc"}, running=False)
    assert h.routes_post["/api/safety"](None, {})["ok"]
    saved = _cfg(home)
    assert saved["dry_run"] is True and saved["max_entries_per_day"] == 5


def test_w2_safety_form_can_still_change_mode_and_cap_on_purpose(home, app):
    h = app(LIVE)
    assert h.routes_post["/api/safety"](None, {"dry": True, "cap": "4",
                                               "max_loss": "0", "seeded": True})["ok"]
    saved = _cfg(home)
    assert saved["dry_run"] is True and saved["max_entries_per_day"] == 4
    assert saved["max_daily_loss_usd"] == 0.0


def test_w2_a_page_from_before_the_update_cannot_raise_the_cap(home, app):
    """A Safety page left open across an update still sends dry-run + cap 5
    whatever is set. It may lower the cap and switch to dry-run (both the
    careful direction), never raise the cap."""
    h = app(LIVE)                                        # cap 2
    assert h.routes_post["/api/safety"](None, {"dry": True, "cap": "5"})["ok"]
    saved = _cfg(home)
    assert saved["max_entries_per_day"] == 2             # not raised to 5
    assert saved["dry_run"] is True                      # asked for dry-run: always honoured
    assert h.routes_post["/api/safety"](None, {"dry": True, "cap": "1"})["ok"]
    assert _cfg(home)["max_entries_per_day"] == 1


# ── W3: a real position stays real-managed after a switch to dry-run ────────

KEY = "SPY|2026-07-10|752.0|C"


def _nosave(state):
    pass


def _log():
    try:
        with open(agent.TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


def test_w3_exit_signal_after_switch_to_dry_run_sells_the_real_position(
        home, broker, no_policy):
    state = {"positions": {}}
    agent.handle_event(make_event(), {"dry_run": False}, state, broker,
                       object(), _nosave)                       # bought for real
    marked = state["positions"][KEY].get("live")

    out = agent.handle_event(make_event(event="EXITED", event_id="x-1"),
                             {"dry_run": True}, state, broker, object(), _nosave)

    assert [e["event"] for e in broker.calls] == ["ENTERED", "EXITED"]  # a real sell
    assert KEY not in state["positions"]
    last = _log()[-1]
    assert last["action"] == "exit" and last["dry"] is False    # a real exit
    assert out == "EXIT: SPY 2026-07-10 $752.0 C"               # no [DRY-RUN]
    assert marked is True                    # the ledger row said "opened live"


def test_w3_ledger_from_an_older_version_counts_as_real(home, broker, no_policy):
    # rows written before the "live" field: a broker adapter's row has a real
    # option id and no "dry" mark
    state = {"positions": {KEY: {"option_id": "opt-123", "qty": 2}}}
    agent.handle_event(make_event(event="EXITED", event_id="x-2"),
                       {"dry_run": True}, state, broker, object(), _nosave)
    assert [e["event"] for e in broker.calls] == ["EXITED"]
    assert _log()[-1]["dry"] is False
    assert agent._opened_live({"option_id": "opt-123", "qty": 2})


def test_w3_dry_run_positions_stay_paper(home, broker, no_policy):
    state = {"positions": {}}
    agent.handle_event(make_event(), {"dry_run": True}, state, broker,
                       object(), _nosave)
    row = state["positions"][KEY]
    assert row["dry"] is True and not agent._opened_live(row)
    # even after going live, its exit is a paper exit: no order
    out = agent.handle_event(make_event(event="EXITED", event_id="x-3"),
                             {"dry_run": False}, state, broker, object(), _nosave)
    assert broker.calls == [] and KEY not in state["positions"]
    assert _log()[-1]["dry"] is True and out.startswith("[DRY-RUN] ")


def test_w3_dry_run_still_places_no_new_real_entries(home, broker, no_policy):
    state = {"positions": {KEY: {"option_id": "opt-123", "qty": 2}}}
    other = make_event(ticker="QQQ", event_id="e-9")
    agent.handle_event(other, {"dry_run": True}, state, broker, object(), _nosave)
    assert broker.calls == []
    assert state["positions"]["QQQ|2026-07-10|752.0|C"]["dry"] is True
    # and a repeat entry signal for the real position buys nothing either
    agent.handle_event(make_event(event_id="e-10"), {"dry_run": True}, state,
                       broker, object(), _nosave)
    assert broker.calls == [] and state["positions"][KEY]["qty"] == 2


def test_w3_opened_live_reads_old_and_new_rows():
    assert agent._opened_live({"option_id": "opt-1", "qty": 1})            # adapter row
    assert agent._opened_live({"option_id": "opt-1", "qty": 1, "live": True})
    assert agent._opened_live({"qty": 1})                 # unknown: never orphan it
    assert not agent._opened_live({"option_id": "dry", "qty": 1, "dry": True})
    assert not agent._opened_live({"option_id": "dry", "qty": 1})
    assert not agent._opened_live(None)


def test_w3_expiry_guard_sells_a_real_position_in_dry_run(home, broker, monkeypatch):
    now = datetime(2026, 10, 30, 15, 46, tzinfo=agent.MARKET_TZ)   # a Friday
    monkeypatch.setattr(agent, "_et_now", lambda: now)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    key = "RIVN|2026-10-30|13.5|C"
    state = {"positions": {key: {"option_id": "opt-1", "qty": 2}}}
    out = agent.expiry_guard_tick({"dry_run": True}, state, _nosave, broker,
                                  object(), now_et=now)
    assert out == ["EXIT: RIVN 2026-10-30 $13.5 C"]
    assert [e["event"] for e in broker.calls] == ["EXITED"]
    assert key not in state["positions"] and _log()[-1]["dry"] is False


def test_w3_switch_to_dry_run_says_what_happens_to_real_positions(home):
    _write_cfg(home, LIVE)
    (home / "state.json").write_text(json.dumps({"positions": {
        KEY: {"option_id": "opt-1", "qty": 1, "live": True},
        "QQQ|2026-07-10|500.0|P": {"option_id": "dry", "qty": 1, "dry": True},
    }}), encoding="utf-8")
    handled, reply = agent._apply_command("dry on")
    assert handled and _cfg(home)["dry_run"] is True
    assert "You have 1 real position open." in reply
    assert "I will still sell it when its exit signal arrives" in reply
    assert "No new real trades will be placed." in reply
    assert "PAUSED" not in agent._apply_command("status")[1]
    assert "You have 1 real position open." in agent._apply_command("status")[1]


def test_w3_switch_to_dry_run_with_nothing_open_is_short(home):
    _write_cfg(home, LIVE)
    reply = agent._apply_command("dry on")[1]
    assert "real position" not in reply and "no real trades" in reply


def test_w3_decision_flipped_keeps_the_position_and_tells_the_owner(
        home, broker, no_policy, monkeypatch):
    """The operator's switch the other way: dry-run places no orders at all.
    The real position must then stay listed — never booked as sold."""
    monkeypatch.setattr(agent, "_dry_run_keeps_selling_real_positions",
                        lambda cfg: False)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    state = {"positions": {KEY: {"option_id": "opt-123", "qty": 2}}}
    out = agent.handle_event(make_event(event="EXITED", event_id="x-4"),
                             {"dry_run": True}, state, broker, object(), _nosave)
    assert out is None and broker.calls == []
    assert state["positions"][KEY]["qty"] == 2            # still tracked
    last = _log()[-1]
    assert last["action"] == "exit_failed" and "dry-run" in last["reason"]
    note = agent._dry_run_note({"dry_run": True}, state)
    assert "I will NOT sell it" in note


def test_w3_startup_line_does_not_promise_no_orders_with_real_positions_open():
    real = {"positions": {KEY: {"option_id": "opt-1", "qty": 1, "live": True}}}
    paper = {"positions": {KEY: {"option_id": "dry", "qty": 1, "dry": True}}}
    assert agent._mode_label({"dry_run": False}, real) == "LIVE"
    assert agent._mode_label({"dry_run": True}, paper) == "DRY-RUN (no orders)"
    assert agent._mode_label({"dry_run": True}, {"positions": {}}) == "DRY-RUN (no orders)"
    label = agent._mode_label({"dry_run": True}, real)
    assert "no orders" not in label
    assert label == "DRY-RUN (no new trades; real positions still get sold)"


def test_w3_decision_flipped_startup_line_says_no_orders(monkeypatch):
    monkeypatch.setattr(agent, "_dry_run_keeps_selling_real_positions",
                        lambda cfg: False)
    real = {"positions": {KEY: {"option_id": "opt-1", "qty": 1, "live": True}}}
    assert agent._mode_label({"dry_run": True}, real) == "DRY-RUN (no orders)"


def test_w3_moved_setup_copy_says_real_positions_are_still_sold(
        home, monkeypatch, capsys):
    """A moved setup starts in dry-run, which does not stop it selling the
    real positions that came with it: every place that says "starts in
    dry-run" also says to switch the old computer off first."""
    import os
    import keep_running
    import transfer
    note = agent.MOVED_POSITIONS_NOTE
    assert "still sold on their exit signal" in note and "old computer is off" in note
    move_file = home / "move.bin"
    move_file.write_bytes(b"x")
    monkeypatch.setattr(keep_running, "is_running", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "passphrase")
    monkeypatch.setattr(transfer, "import_bundle", lambda blob, pw, replace=False: {
        "files": 3, "from_os": "Windows", "created_at": "2026-11-01", "was_live": True})
    agent.cmd_import(str(move_file))
    out = capsys.readouterr().out
    assert "It starts in DRY-RUN: no new trades." in out and note in out
    root = os.path.dirname(os.path.abspath(agent.__file__))
    for rel in (("webui_app", "src", "wizard", "MoveImport.tsx"),
                ("webui_app", "src", "dash", "MovePanel.tsx"), ("README.md",)):
        with open(os.path.join(root, *rel), encoding="utf-8") as f:
            text = " ".join(f.read().split())
        assert "still sold" in text and "exit signal" in text, rel


# ── W4: paused = no new entries; everything else keeps running ──────────────

def test_w4_paused_skips_entries_but_exits_still_sell(home, broker, no_policy):
    state = {"positions": {KEY: {"option_id": "opt-123", "qty": 1}}}
    cfg = {"dry_run": False, "paused": True}
    out = agent.handle_event(make_event(ticker="QQQ", event_id="e-1"), cfg,
                             state, broker, object(), _nosave)
    assert broker.calls == [] and "paused" in out
    assert _log()[-1]["action"] == "skip_paused"
    assert "QQQ|2026-07-10|752.0|C" not in state["positions"]

    agent.handle_event(make_event(event="EXITED", event_id="x-1"), cfg, state,
                       broker, object(), _nosave)
    assert [e["event"] for e in broker.calls] == ["EXITED"]
    assert KEY not in state["positions"]


def test_w4_pause_is_saved_and_survives_other_saves(home, app):
    h = app(LIVE)
    handled, reply = agent._apply_command("pause")
    assert handled and _cfg(home)["paused"] is True
    assert "no new entries" in reply and "restart" in reply
    assert "Exit signals" in reply
    # a settings save doesn't unpause; a fresh process reads it from the file
    h.routes_post["/api/wallet/recurring"](None, {"on": False})
    assert agent._paused(agent._load(agent.CONFIG_PATH))
    assert "PAUSED" in agent._apply_command("status")[1]
    handled, reply = agent._apply_command("resume")
    assert handled and "paused" not in _cfg(home)
    assert not agent._paused(_cfg(home))


REAL_ROW = {"option_id": "opt-1", "qty": 1, "live": True}
PAPER_ROW = {"option_id": "dry", "qty": 1, "dry": True}


def test_w4_paused_with_no_real_position_does_not_use_up_a_signal_day():
    paid = {"source": "agenthc", "paused": True}
    assert agent._pause_skips_feed(paid, {"positions": {}})
    # a REAL open position needs its exit signal: keep reading
    assert not agent._pause_skips_feed(paid, {"positions": {KEY: REAL_ROW}})
    assert not agent._pause_skips_feed(paid, {"positions": {KEY: {"qty": 1}}})
    # a dry-run (paper) position is no reason to spend a signal day
    assert agent._pause_skips_feed(paid, {"positions": {KEY: PAPER_ROW}})
    assert not agent._pause_skips_feed(
        paid, {"positions": {KEY: PAPER_ROW, "QQQ|2026-07-10|500.0|P": REAL_ROW}})
    assert not agent._pause_skips_feed({**paid, "paused": False}, {"positions": {}})
    # feeds that cost nothing per day are always read (entries logged as skipped)
    assert not agent._pause_skips_feed({**paid, "agenthc_api_key": "k"},
                                       {"positions": {}})
    assert not agent._pause_skips_feed({"source": "manual", "paused": True},
                                       {"positions": {}})


def test_w4_pause_says_when_a_paused_agent_still_uses_a_signal_day(home):
    _write_cfg(home, {**LIVE, "source": "agenthc"})
    reply = agent._apply_command("pause")[1]
    assert ("While a real position is open the agent still reads the signal "
            "feed to get its exit signal, which uses a signal day") in reply
    assert "With no real position open it stops reading the feed" in reply
    paid = {"source": "agenthc", "paused": True}
    assert "uses a signal day." in agent._pause_note(paid, {"positions": {KEY: REAL_ROW}})
    assert "uses no signal day" in agent._pause_note(paid, {"positions": {KEY: PAPER_ROW}})
    assert agent._pause_note({**paid, "paused": False}, {"positions": {}}) == ""
    # a feed that costs nothing per day: nothing to say
    assert agent._pause_note({**paid, "agenthc_api_key": "k"}, {"positions": {}}) == ""
    _write_cfg(home, {**LIVE, "source": "manual"})
    assert "signal day" not in agent._apply_command("pause")[1]


def _one_loop(home, monkeypatch, cfg, state, events=(), now=None, feed="stub"):
    """One pass of `agent.py run` with a stub broker and feed (registered
    under the source name `feed`). Returns (calls in order, broker, saved
    state)."""
    import brokers
    import keep_running
    calls = []
    calls_status = _one_loop.status = []    # the dashboard's get_status()

    class Broker(StubBroker):
        def client(self, cfg):
            return object()

        def reconcile(self, client, cfg, state, save_state):
            calls.append("reconcile")

        def execute(self, client, cfg, event, state):
            calls.append(("order", event["event"], event["ticker"]))
            return super().execute(client, cfg, event, state)

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            calls.append("poll")
            return list(events)

    def no_dashboard(get_status, *a, **k):
        calls_status.append(get_status)
        raise OSError("no dashboard in tests")

    def stop(*a, **k):
        raise KeyboardInterrupt
    b = Broker()
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", b)
    monkeypatch.setitem(agent.SOURCES, feed, Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_position_upkeep",
                        lambda *a, **k: calls.append("upkeep"))
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", stop)
    if now is not None:
        monkeypatch.setattr(agent, "_et_now", lambda: now)
    _write_cfg(home, {"source": "stub", "broker": "stub", **cfg})
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    agent.cmd_run()
    saved = json.loads((home / "state.json").read_text(encoding="utf-8"))
    return calls, b, saved


def test_w4_paused_loop_still_reconciles_checks_and_exits(
        home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    far = "2099-12-18"                                    # never pruned as expired
    key = f"SPY|{far}|752.0|C"
    state = {"seen": [], "positions": {key: {"option_id": "opt-1", "qty": 1}}}
    events = [make_event(ticker="QQQ", expiry=far, event_id="e-1"),
              make_event(event="EXITED", expiry=far, event_id="x-1")]
    calls, _b, saved = _one_loop(home, monkeypatch, {"paused": True}, state, events)
    assert "reconcile" in calls and "upkeep" in calls and "poll" in calls
    assert ("order", "EXITED", "SPY") in calls            # the exit was sold
    assert not [c for c in calls if c[:2] == ("order", "ENTERED")]   # no new entry
    assert saved["positions"] == {}
    assert set(saved["seen"]) == {"e-1", "x-1"}           # the entry isn't retried
    actions = [r["action"] for r in _log()]
    assert actions == ["skip_paused", "exit"]


def test_w4_not_paused_the_same_loop_takes_the_entry(home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    far = "2099-12-18"
    calls, _b, saved = _one_loop(home, monkeypatch, {}, {"seen": [], "positions": {}},
                                 [make_event(ticker="QQQ", expiry=far, event_id="e-1")])
    assert ("order", "ENTERED", "QQQ") in calls
    assert f"QQQ|{far}|752.0|C" in saved["positions"]


def test_w4_paused_on_the_paid_feed_with_nothing_open_does_not_read_it(
        home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    calls, _b, _saved = _one_loop(home, monkeypatch,
                                  {"paused": True, "source": "agenthc"},
                                  {"seen": [], "positions": {}}, feed="agenthc")
    assert "poll" not in calls and "reconcile" in calls


def test_w4_paused_on_the_paid_feed_with_only_a_paper_position_does_not_read_it(
        home, monkeypatch, no_policy):
    """The reviewer's scenario: trialling in dry-run, Pause before a week
    away with one paper position open. No signal day may be used for it."""
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    far = "2099-12-18"
    state = {"seen": [], "positions": {f"SPY|{far}|752.0|C": dict(PAPER_ROW)}}
    calls, _b, saved = _one_loop(home, monkeypatch,
                                 {"paused": True, "dry_run": True, "source": "agenthc"},
                                 state, feed="agenthc")
    assert "poll" not in calls and "reconcile" in calls
    assert f"SPY|{far}|752.0|C" in saved["positions"]      # still listed, on paper
    status = _one_loop.status[0]()
    assert status["paused"] is True and "uses no signal day" in status["pause_note"]


def test_w4_paused_on_the_paid_feed_with_a_real_position_still_reads_it(
        home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    far = "2099-12-18"
    key = f"SPY|{far}|752.0|C"
    state = {"seen": [], "positions": {key: dict(REAL_ROW)}}
    calls, _b, saved = _one_loop(
        home, monkeypatch, {"paused": True, "source": "agenthc"}, state,
        [make_event(event="EXITED", expiry=far, event_id="x-1")], feed="agenthc")
    assert "poll" in calls and ("order", "EXITED", "SPY") in calls
    assert saved["positions"] == {}


def test_w4_paused_loop_expiry_guard_still_sells(home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    now = datetime(2026, 10, 30, 15, 46, tzinfo=agent.MARKET_TZ)
    key = "RIVN|2026-10-30|13.5|C"
    state = {"seen": [], "positions": {key: {"option_id": "opt-1", "qty": 1}}}
    calls, _b, saved = _one_loop(home, monkeypatch, {"paused": True}, state, now=now)
    assert ("order", "EXITED", "RIVN") in calls
    assert key not in saved["positions"]


def test_w4_restart_hold_still_stops_everything_and_runs_out(home, monkeypatch):
    """An update or a move holds the loop for the seconds before the restart
    (what the old in-memory pause was also used for) — and only for a while."""
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    assert not webui.orders_held(now=1000.0)
    webui.hold_orders(now=1000.0)
    assert webui.orders_held(now=1001.0)
    # a restart that never comes can't freeze the agent for good
    assert not webui.orders_held(now=1000.0 + webui._HOLD_S + 1)
    webui.hold_orders(now=1000.0)
    webui.release_orders()
    assert not webui.orders_held(now=1001.0)


def test_w4_held_loop_places_no_orders(home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", float("inf"))
    state = {"seen": [], "positions": {KEY: {"option_id": "opt-1", "qty": 1}}}
    calls, _b, saved = _one_loop(home, monkeypatch, {}, state,
                                 [make_event(event="EXITED", event_id="x-1")])
    assert calls == [] and KEY in saved["positions"]


def test_w4_status_says_restarting_while_the_restart_hold_is_on(
        home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    _one_loop(home, monkeypatch, {}, {"seen": [], "positions": {}})
    get_status = _one_loop.status[0]
    assert get_status()["restarting"] is False
    webui.hold_orders()
    assert get_status()["restarting"] is True
    # both dashboards show it
    assert "s.restarting" in webui._DASH_HTML and "Restarting" in webui._DASH_HTML


def test_w4_status_shows_the_settings_the_agent_runs_on_when_config_has_a_typo(
        home, monkeypatch, no_policy):
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    _one_loop(home, monkeypatch, {"dry_run": True, "max_entries_per_day": 2},
              {"seen": [], "positions": {}})
    get_status = _one_loop.status[0]
    (home / "config.json").write_text('{"dry_run": true,}', encoding="utf-8")
    status = get_status()
    assert status["mode"] == "DRY-RUN"                     # not "LIVE" from {}
    assert status["fields"]["entries today"].endswith("cap 2")


def _import_route(home, app, monkeypatch, restart):
    """/api/move/import on a running agent, with the bundle itself faked.
    `restart` stands in for platform_compat.restart_self."""
    import platform_compat
    import transfer
    monkeypatch.setitem(webui.CONTROLS, "hold_until", 0.0)
    monkeypatch.setenv("AGENT_UPDATE_RESTARTED", "")     # the route sets it: undo after
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(transfer, "decrypt", lambda blob, pw: b"")
    monkeypatch.setattr(transfer, "existing_setup", lambda: False)
    monkeypatch.setattr(transfer, "import_bundle", lambda blob, pw, replace=False: {
        "files": 3, "from_os": "Windows", "created_at": "2026-11-01", "was_live": True})
    monkeypatch.setattr(platform_compat, "restart_self", restart)
    return app(LIVE).routes_post["/api/move/import"]


def test_w4_import_restarts_even_if_the_settings_file_is_busy_right_after(
        home, app, monkeypatch):
    """The reviewer's scenario: config.json is locked for a moment just
    after the import (antivirus, a sync program). The agent is on hold
    with no time limit by then — the restart must still happen."""
    import threading
    restarted = threading.Event()
    route = _import_route(home, app, monkeypatch, lambda argv, port: restarted.set())

    def locked(*a, **k):
        raise agent.ConfigUnreadable("config.json is locked by another program")
    monkeypatch.setattr(agent, "_read_config", locked)

    r = route(None, {"data": "", "passphrase": "pw"})

    assert r["ok"] is True and r["restarting"] is True
    assert webui.orders_held()                           # held until the restart…
    assert restarted.wait(30)                            # …which does come


def test_w4_import_restarts_from_this_thread_when_no_thread_can_be_started(
        home, app, monkeypatch):
    done = []
    route = _import_route(home, app, monkeypatch, lambda argv, port: done.append(port))

    class NoThreads:
        def __init__(self, *a, **k):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(webui.threading, "Thread", NoThreads)
    assert route(None, {"data": "", "passphrase": "pw"})["ok"] is True
    assert done == [webui.APP_PORT]


def test_w4_import_that_cannot_restart_leaves_instead_of_staying_on_hold(
        home, app, monkeypatch, capsys):
    """A restart that fails after the files were replaced: the process must
    not stay alive on hold for good (looking fine, selling nothing), and
    must not carry on with the old setup in memory. It exits; the
    supervisor starts it again."""
    import threading
    import platform_compat
    left = threading.Event()
    codes = []

    def cannot(argv, port):
        raise OSError("exec failed")

    def fake_exit(code):
        codes.append(code)
        left.set()
    monkeypatch.setattr("os._exit", fake_exit)
    route = _import_route(home, app, monkeypatch, cannot)
    assert route(None, {"data": "", "passphrase": "pw"})["ok"] is True
    assert left.wait(30)
    assert codes == [platform_compat.RESTART_EXIT]
    assert "python agent.py start" in capsys.readouterr().out


def test_w4_import_that_fails_releases_the_hold(home, app, monkeypatch):
    import transfer
    route = _import_route(home, app, monkeypatch, lambda argv, port: None)

    def bad(blob, pw, replace=False):
        raise transfer.TransferError("that isn't an agentic-trader move file")
    monkeypatch.setattr(transfer, "import_bundle", bad)
    r = route(None, {"data": "", "passphrase": "pw"})
    assert r["ok"] is False and not webui.orders_held()


# ── W5: a question is never a command ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    "tickers skipped today and why?",
    "set budget to 500 or 1000?",
    "should I set cap 1?",
    "pause?",
    "dry off?",
])
def test_w5_a_question_changes_nothing_and_goes_to_qa(home, text):
    _write_cfg(home, LIVE)
    before = (home / "config.json").read_text(encoding="utf-8")
    assert agent._apply_command(text) == (False, "")
    assert (home / "config.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("text", [
    "tickers skipped today and why",
    "tickers spy and qqq",
    "tickers now",                      # an ordinary word, not in capitals
    "tickers",
    "skip tickers that lost money",
    "set budget to 500 or 1000",
    "set budget higher",
    "set budget",
    "set cap 3 please",
    "set cap 2.5",
    "set cap 0",
    "set positions a few",
    "set max loss about 100",
    "set contracts 1 or 2",
    "set percent two",
    "set other budget 100 200",
    "set other contracts 1.5",
    # sentences made of short words that are not in any word list
    "tickers look wrong",
    "ticker rule help",
    "tickers got reset",
    "tickers help",
    "tickers fine",
    "tickers ok",
    "skip tickers like tsla",
    "tickers spy qqq thanks",
    "TICKERS LOOK WRONG",               # caps lock: capitals mean nothing
    "tickers Look Wrong",
    # a percent sign on a command that is not a percent
    "set budget 500%",
    "set cap 3%",
    "set max loss 5%",
    "set positions 4%",
    "set contracts 2%",
    "set other budget 100%",
    "set percent $2",
    "set cap $3",
])
def test_w5_a_sentence_that_is_not_a_whole_command_changes_nothing(home, text):
    _write_cfg(home, {**LIVE, "rule_tickers": ["NVDA"], "rule_skip_tickers": ["GME"]})
    before = (home / "config.json").read_text(encoding="utf-8")
    handled, reply = agent._apply_command(text)
    assert handled and reply.startswith("Nothing was changed.")
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_w5_an_unsure_ticker_list_gets_the_exact_command_to_confirm(home):
    _write_cfg(home, LIVE)
    reply = agent._apply_command("tickers look wrong")[1]
    assert reply.startswith("Nothing was changed. To trade only LOOK and WRONG, "
                            "type: tickers LOOK WRONG")
    assert "question mark" in reply
    reply = agent._apply_command("skip tickers like tsla")[1]
    assert "To never trade LIKE and TSLA, type: skip tickers LIKE TSLA" in reply
    # ordinary words are left out of the suggestion
    reply = agent._apply_command("tickers spy and qqq")[1]
    assert "To trade only SPY and QQQ, type: tickers SPY QQQ" in reply
    # typing the suggested command does it
    assert agent._apply_command("tickers LOOK WRONG")[1].startswith("Ticker list updated.")
    assert _cfg(home)["rule_tickers"] == ["LOOK", "WRONG"]


def test_w5_lower_case_is_a_ticker_only_when_the_agent_knows_it(home):
    """Known: the common list, the owner's rule lists, open positions and
    the trade log. Anything else must be typed in CAPITALS (or with $)."""
    _write_cfg(home, {**LIVE, "rule_skip_tickers": ["ZETA"]})
    (home / "state.json").write_text(json.dumps({"positions": {
        "HIMS|2026-07-10|50.0|C": {"option_id": "opt-1", "qty": 1}}}), encoding="utf-8")
    agent._log_trade({"action": "exit", "contract": "OKLO 2026-07-10 $90 CALL"})
    assert agent._known_tickers(_cfg(home)) >= {"SPY", "ZETA", "HIMS", "OKLO"}
    assert agent._apply_command("tickers hims oklo zeta spy")[1].startswith(
        "Ticker list updated.")
    assert _cfg(home)["rule_tickers"] == ["HIMS", "OKLO", "ZETA", "SPY"]
    # an unknown one among them: nothing changes
    assert agent._apply_command("tickers hims abcd")[1].startswith("Nothing was changed.")
    assert _cfg(home)["rule_tickers"] == ["HIMS", "OKLO", "ZETA", "SPY"]
    assert agent._apply_command("tickers hims $abcd")[1].startswith("Ticker list updated.")
    assert _cfg(home)["rule_tickers"] == ["HIMS", "ABCD"]
    # an ordinary word stays a word even when a ticker of that name is known
    agent._apply_command("tickers NOW")
    assert agent._apply_command("skip tickers now")[1].startswith("Nothing was changed.")
    # the whole command in capitals: known tickers still work
    assert agent._apply_command("TICKERS SPY QQQ")[1].startswith("Ticker list updated.")
    assert _cfg(home)["rule_tickers"] == ["SPY", "QQQ"]


@pytest.mark.parametrize("text,key,want", [
    ("tickers spy, qqq", "rule_tickers", ["SPY", "QQQ"]),
    ("tickers SPY QQQ", "rule_tickers", ["SPY", "QQQ"]),
    ("only tickers $nvda", "rule_tickers", ["NVDA"]),
    ("Tickers BRK.B", "rule_tickers", ["BRK.B"]),
    ("tickers NOW ON", "rule_tickers", ["NOW", "ON"]),   # capitals: tickers
    ("tickers any", "rule_tickers", []),
    ("skip tickers tsla", "rule_skip_tickers", ["TSLA"]),
    ("skip tickers none", "rule_skip_tickers", []),
    ("set budget 750", "budget_per_trade_usd", 750.0),
    ("set budget to $1,500", "budget_per_trade_usd", 1500.0),
    ("set cap 3", "max_entries_per_day", 3),
    ("set positions 4", "max_open_positions", 4),
    ("set max loss 0", "max_daily_loss_usd", 0.0),
    ("set percent 2%", "percent_per_trade", 2.0),
    ("set contracts 2", "contracts_per_trade", 2),
    ("set other budget 100", "other_budget_per_trade_usd", 100.0),
    ("set other contracts 1", "other_contracts_per_trade", 1),
])
def test_w5_whole_commands_still_work(home, text, key, want):
    _write_cfg(home, LIVE)
    handled, reply = agent._apply_command(text)
    assert handled and not reply.startswith("Nothing was changed")
    assert _cfg(home)[key] == want


def test_w5_commands_that_only_show_something_may_end_in_a_question_mark(home):
    _write_cfg(home, LIVE)
    handled, reply = agent._apply_command("status?")
    assert handled and reply.startswith("LIVE")
    assert agent._apply_command("rules?")[0]


# ── W6: no secret reaches the AI provider ───────────────────────────────────

SECRETS = {
    "lnbits_wallet_page": "https://demo.lnbits.com/wallet?usr=u-7Qx2&wal=w-9Zk4",
    "lnbits_admin_key": "adm-k3y-51",
    "lnbits_url": "https://demo.lnbits.com",
    "source_url": "https://bob:hunter2@feed.example.com/v1/tok-abc123/signals.json?auth=zz-88",
    "llm_api_key": "sk-ant-test-0001",
    "agenthc_api_key": "aghc_live_000111",
    "telegram_bot_token": "123456:ABC-tg",
    "discord_webhook_url": "https://discord.com/api/webhooks/1/hook-xyz",
}
MUST_NOT_LEAK = ["u-7Qx2", "w-9Zk4", "adm-k3y-51", "hunter2", "tok-abc123",
                 "zz-88", "sk-ant-test-0001", "aghc_live_000111", "ABC-tg",
                 "hook-xyz"]


@pytest.fixture
def prompts(monkeypatch):
    import llm_client
    sent = []

    def complete(cfg, system, user, **kw):
        sent.append(system + "\n" + user)
        return "ok"
    monkeypatch.setattr(llm_client, "configured", lambda cfg: True)
    monkeypatch.setattr(llm_client, "complete", complete)
    return sent


def test_w6_setup_assistant_prompt_carries_no_secret(home, app, prompts):
    h = app({**LIVE, **SECRETS, "source": "url"}, running=False)
    # the fields that used to leak are really in the state the page gets
    st = h.routes_get["/api/state"](None)
    assert "usr=u-7Qx2" in st["wallet"]["page"]
    assert "tok-abc123" in st["notes"]["source"]

    assert h.routes_post["/api/ask"](None, {"question": "how do I fund it?"}) == {
        "reply": "ok"}

    assert len(prompts) == 1 and "how do I fund it?" in prompts[0]
    for secret in MUST_NOT_LEAK:
        assert secret not in prompts[0], secret
    assert '"connected": true' in prompts[0]              # still useful to the AI
    assert "feed.example.com" in prompts[0]


def test_w6_dashboard_question_prompt_is_scrubbed(home, prompts):
    trades = [{"action": "order_failed", "contract": "SPY 2026-07-10 $752 CALL",
               "reason": "401 from https://broker.example/api?token=tk-555 "
                         "(L402 abcdef0123:9988)"}]
    fields = {"feed access": "wallet page https://w.example/wallet?usr=u-7Qx2"}
    assert webui._ask_llm({}, "why did it fail?", fields, trades) == "ok"
    for secret in ("tk-555", "u-7Qx2", "abcdef0123:9988"):
        assert secret not in prompts[0], secret
    assert "order_failed" in prompts[0] and "why did it fail?" in prompts[0]


def test_w6_scrubber_knows_the_wallet_link():
    import support
    out = support.scrub('{"page": "https://demo.lnbits.com/wallet?usr=u-7Qx2&wal=w-9Zk4"}')
    assert "u-7Qx2" not in out and "w-9Zk4" not in out
    assert out.endswith('"}')                             # the JSON around it survives
    # the "enable options" link the wizard shows carries the broker account number
    out = support.scrub("apply: https://applink.robinhood.com/upgrade_options"
                        "?account_number=123456789")
    assert "123456789" not in out and "upgrade_options" in out


# ── W7: the classic wizard never puts a server message into the page raw ────

def test_w7_classic_pages_escape_server_messages():
    """Error and note texts can carry a third party's raw reply. The
    built-in pages never join one into HTML without esc()."""
    raw = re.compile(r"\+\s*\w+\.(?:error|note|warning|rh_warning|reply|message)\b")
    bad = [line.strip()[:120]
           for page in (webui._WIZARD_HTML, webui._DASH_HTML)
           for line in page.splitlines() if raw.search(line)]
    assert bad == []
    assert "esc(r.error)" in webui._WIZARD_HTML
