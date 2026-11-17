"""An owner who wants 20 positions open at once gets 20, not 10.

User report: "if you say you want 20 open trades at once it still limits
you to 10". The entry gate's limit is max_open_positions (10 when never
set). The setup wizard's Safety step only had "Max new entries per day"
(a different limit), and the chat only took the exact words
'set positions N': 'set max open positions 20' and the like were answered
as questions and changed nothing. So the cap stayed at 10."""

import json
import re
from pathlib import Path

import pytest

import agent
import webui
from conftest import StubBroker, make_event


def _write_cfg(home, cfg):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _cfg(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})


@pytest.fixture
def app(home, monkeypatch):
    """start(cfg) -> the settings/dashboard server's routes. No port."""
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
        "max_entries_per_day": 50, "sizing_mode": "contracts",
        "contracts_per_trade": 1}


def _ten_open():
    return {"positions": {f"T{i}|2026-07-10|{i}.0|C": {"qty": 1}
                          for i in range(10)}}


def _eleventh_entry(cfg):
    """Send an 11th entry signal with 10 positions open: the broker calls."""
    broker = StubBroker()
    cfg = {k: v for k, v in cfg.items() if k not in ("source", "broker")}
    out = agent.handle_event(make_event(), cfg, _ten_open(), broker, object(),
                             lambda s: None)
    return out, broker.calls


# ── the setup wizard's Safety step ──────────────────────────────────────────

def test_safety_form_saves_positions_open_at_once_and_the_gate_honours_it(
        home, app, no_policy):
    h = app(LIVE)
    r = h.routes_post["/api/safety"](None, {"positions": "20", "seeded": True})
    assert r["ok"], r
    cfg = _cfg(home)
    assert cfg["max_open_positions"] == 20
    assert cfg["max_entries_per_day"] == 50          # the daily cap untouched
    out, calls = _eleventh_entry(cfg)
    assert len(calls) == 1, out                      # entered, not skipped


def test_state_reports_positions_open_at_once_for_the_form(home, app):
    h = app({**LIVE, "max_open_positions": 25})
    assert h.routes_get["/api/state"](None)["max_open_positions"] == 25
    h = app({"source": "agenthc"}, running=False)
    assert h.routes_get["/api/state"](None)["max_open_positions"] is None


def test_safety_form_left_blank_or_not_sent_keeps_positions(home, app):
    h = app({**LIVE, "max_open_positions": 25})
    assert h.routes_post["/api/safety"](None, {"rule_tickers": "SPY"})["ok"]
    assert h.routes_post["/api/safety"](None, {"positions": "",
                                               "seeded": True})["ok"]
    assert _cfg(home)["max_open_positions"] == 25


def test_a_page_from_before_the_update_cannot_raise_positions(home, app):
    """As for the daily cap: only a form filled in from the current
    settings may raise it; lowering is always allowed."""
    h = app(LIVE)                                    # never set: 10
    assert h.routes_post["/api/safety"](None, {"positions": "20"})["ok"]
    assert "max_open_positions" not in _cfg(home)
    assert h.routes_post["/api/safety"](None, {"positions": "4"})["ok"]
    assert _cfg(home)["max_open_positions"] == 4


@pytest.mark.parametrize("bad", ["0", "1001", "abc", "2.5", "-3"])
def test_safety_form_refuses_positions_out_of_range(home, app, bad):
    h = app({**LIVE, "max_open_positions": 12})
    before = (home / "config.json").read_text(encoding="utf-8")
    r = h.routes_post["/api/safety"](None, {"positions": bad, "seeded": True,
                                            "dry": True})
    assert r["ok"] is False and "Nothing was changed" in r["error"]
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_built_wizard_has_the_positions_field():
    """The React build users get (webui_dist) was rebuilt with the field."""
    dist = Path(webui.__file__).parent / "webui_dist" / "assets"
    js = "".join(p.read_text(encoding="utf-8")
                 for p in dist.glob("index-*.js"))
    assert "sf-positions" in js and "Open positions at once" in js


def test_builtin_classic_wizard_has_the_positions_field():
    html = webui._WIZARD_HTML if hasattr(webui, "_WIZARD_HTML") else ""
    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert 'id="sf-pos"' in (html or src)
    assert "body.positions=pos" in src


# ── chat ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "set positions 20",
    "set positions to 20",
    "set max positions 20",
    "set open positions 20",
    "set max open positions 20",
    "set max open positions to 20",
    "set position cap 20",
    "set open trades 20",
    "set max open trades 20",
    "Set Max Open Positions 20",
])
def test_chat_sets_positions_open_at_once_in_the_words_owners_use(
        home, text, no_policy):
    _write_cfg(home, LIVE)
    handled, reply = agent._apply_command(text)
    assert handled and "set to 20" in reply, reply
    cfg = _cfg(home)
    assert cfg["max_open_positions"] == 20
    out, calls = _eleventh_entry(cfg)
    assert len(calls) == 1, out


@pytest.mark.parametrize("text", [
    "set positions 0", "set positions 1001", "set max open positions 2.5",
    "set open positions twenty", "set max positions 20 please",
])
def test_chat_positions_out_of_range_or_not_a_command_changes_nothing(
        home, text):
    _write_cfg(home, {**LIVE, "max_open_positions": 12})
    before = (home / "config.json").read_text(encoding="utf-8")
    handled, reply = agent._apply_command(text)
    assert handled and reply.startswith("Nothing was changed.")
    assert "set positions" in reply
    assert (home / "config.json").read_text(encoding="utf-8") == before


def test_set_cap_says_it_is_the_daily_cap_and_names_set_positions(home):
    _write_cfg(home, LIVE)
    handled, reply = agent._apply_command("set cap 20")
    assert handled and "set positions" in reply
    cfg = _cfg(home)
    assert cfg["max_entries_per_day"] == 20 and "max_open_positions" not in cfg


def test_a_positions_request_the_chat_cannot_do_says_nothing_changed(
        home, app, monkeypatch):
    monkeypatch.setattr(webui, "_ask_llm", lambda *a, **k: "Some answer.")
    h = app(LIVE)
    reply = h.routes_post["/api/command"](
        None, {"text": "I want 20 open trades at once"})["reply"]
    assert reply.startswith("Nothing was changed.")
    assert "set positions" in reply and reply.endswith("Some answer.")
    assert "max_open_positions" not in _cfg(home)
    # an ordinary question gets just the answer
    for q in ("how did SPY do today?", "why are my 3 open trades down?"):
        reply = h.routes_post["/api/command"](None, {"text": q})["reply"]
        assert reply == "Some answer."
    for q in ("max open positions 20", "can I have 20 positions at once?"):
        reply = h.routes_post["/api/command"](None, {"text": q})["reply"]
        assert reply.startswith("Nothing was changed.")


# ── the skip says how to raise the limit ────────────────────────────────────

def test_position_cap_skip_names_the_command_to_raise_it(home, no_policy):
    out, calls = _eleventh_entry({**LIVE})
    assert calls == []
    assert "max open positions reached (10)" in out
    assert "set positions" in out and "10 real positions open now" in out
    rows = [json.loads(x) for x in
            (home / "trades.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "skip_position_cap"
    assert "set positions" in rows[-1]["reason"]


# ── the terminal setup asks for it ──────────────────────────────────────────

@pytest.mark.parametrize("start,answer,want", [
    ({}, "", 10),
    ({}, "20", 20),
    ({"max_open_positions": 25}, "", 25),
    ({"max_open_positions": 25}, "0", 25),
    ({"max_open_positions": 25}, "5000", 25),
    ({"max_open_positions": 25}, "abc", 25),
    ({"max_open_positions": 25.0}, "", 25),
    ({"max_open_positions": "25"}, "", 25),
])
def test_terminal_setup_asks_for_positions_open_at_once(
        monkeypatch, start, answer, want):
    asked = []
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": asked.append(prompt) or answer)
    cfg = dict(start)
    agent._ask_position_cap(cfg)
    assert cfg["max_open_positions"] == want
    assert re.search(r"positions open at once", asked[0], re.I)


def test_terminal_setup_runs_the_question():
    import inspect
    src = inspect.getsource(agent.cmd_setup)
    assert "_ask_position_cap(cfg)" in src
    assert 'setdefault("max_open_positions"' not in src


def test_every_place_uses_the_same_range():
    assert webui._V2_LIMIT_RANGES["max_open_positions"] == agent.POSITION_CAP_RANGE


def test_skip_counts_what_is_open_after_the_cap_was_lowered(home, no_policy):
    # 10 open, cap lowered to 4: still blocked, and the skip says 10 are open
    out, calls = _eleventh_entry({**LIVE, "max_open_positions": 4})
    assert calls == []
    assert "max open positions reached (4)" in out and "10 real positions open now" in out


@pytest.mark.parametrize("cap", [20, 11])
def test_the_gate_still_blocks_at_a_raised_cap(home, no_policy, cap):
    cfg = {**LIVE, "max_open_positions": cap}
    state = {"positions": {f"T{i}|2026-07-10|{i}.0|C": {"qty": 1}
                           for i in range(cap)}}
    broker = StubBroker()
    cfg = {k: v for k, v in cfg.items() if k not in ("source", "broker")}
    out = agent.handle_event(make_event(), cfg, state, broker, object(),
                             lambda s: None)
    assert broker.calls == [] and f"max open positions reached ({cap})" in out


def test_a_seeded_form_compares_with_a_cap_saved_as_text(home, app):
    # "20" saved by hand: an old page (not seeded) may still lower it to 15
    h = app({**LIVE, "max_open_positions": "20"})
    r = h.routes_post["/api/safety"](None, {"positions": "15", "seeded": False})
    assert r["ok"] and _cfg(home)["max_open_positions"] == 15
