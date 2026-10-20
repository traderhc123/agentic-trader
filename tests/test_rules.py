"""Built-in rules: plain settings that skip entries without any AI."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import agent
import rules
import setup_plan
from conftest import StubBroker, make_event

ET = ZoneInfo("America/New_York")


def test_parse_tickers():
    assert rules.parse_tickers("spy, QQQ  nvda;$tsla") == ["SPY", "QQQ", "NVDA", "TSLA"]
    assert rules.parse_tickers(["spy", "spy", "bad ticker!"]) == ["SPY"]
    assert rules.parse_tickers(None) == []


@pytest.mark.parametrize("cfg,ev,ok", [
    ({}, {}, True),
    ({"rule_tickers": ["SPY", "QQQ"]}, {"ticker": "SPY"}, True),
    ({"rule_tickers": ["SPY", "QQQ"]}, {"ticker": "NVDA"}, False),
    ({"rule_skip_tickers": ["SPY"]}, {"ticker": "SPY"}, False),
    ({"rule_sides": "calls"}, {"type": "P"}, False),
    ({"rule_sides": "calls"}, {"type": "C"}, True),
    ({"rule_sides": "puts"}, {"type": "C"}, False),
    ({"rule_min_strike": 10}, {"strike": 9.5}, False),
    ({"rule_min_strike": 10}, {"strike": 10}, True),
])
def test_check(cfg, ev, ok):
    assert rules.check(cfg, make_event(**ev))[0] is ok


def test_exits_always_pass():
    cfg = {"rule_tickers": ["QQQ"], "rule_sides": "puts", "rule_min_strike": 1e9}
    assert rules.check(cfg, make_event(event="EXITED"))[0]


def test_skip_0dte_uses_market_date():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=ET)
    cfg = {"rule_skip_0dte": True}
    assert not rules.check(cfg, make_event(expiry="2026-07-10"), now=now)[0]
    assert rules.check(cfg, make_event(expiry="2026-07-11"), now=now)[0]


def test_loss_streak():
    ex = lambda p: {"action": "exit", "est_pnl_usd": p}
    assert rules.loss_streak([ex(-5), ex(-3)]) == 2
    assert rules.loss_streak([ex(-5), ex(2), ex(-3)]) == 1
    assert rules.loss_streak([ex(-5), {"action": "exit"}, ex(-1)]) == 2  # unmeasured ignored
    cfg = {"rule_loss_streak": 2}
    assert not rules.check(cfg, make_event(), [ex(-1), ex(-2)])[0]
    assert rules.check(cfg, make_event(), [ex(-1), ex(0)])[0]


def test_describe():
    cfg = {"rule_tickers": ["SPY"], "rule_sides": "calls", "rule_min_strike": 10,
           "rule_skip_0dte": True, "rule_loss_streak": 2}
    d = rules.describe(cfg)
    assert d == "SPY only · calls only · strike ≥ $10 · no 0DTE · stop after 2 losses in a row"
    assert rules.describe({}) == "" and not rules.active({})


def test_apply_settings_from_wizard_strings():
    cfg = rules.apply_settings({}, {"rule_tickers": "spy qqq", "rule_sides": "weird",
                                    "rule_min_strike": "", "rule_loss_streak": "3",
                                    "rule_skip_0dte": True})
    assert cfg == {"rule_tickers": ["SPY", "QQQ"], "rule_sides": "both",
                   "rule_min_strike": 0.0, "rule_loss_streak": 3, "rule_skip_0dte": True}


# ── pipeline: a rule skip happens before any order, with no AI involved ──

def test_handle_event_skips_and_logs(home, broker, monkeypatch):
    import llm_policy
    monkeypatch.setattr(llm_policy, "evaluate",
                        lambda *a: pytest.fail("rules must decide before the AI"))
    state = {"positions": {}, "seen": []}
    cfg = {"rule_tickers": ["QQQ"]}
    out = agent.handle_event(make_event(), cfg, state, broker, object(), lambda s: None)
    assert "your rules" in out and broker.calls == []
    row = json.loads((home / "trades.jsonl").read_text().splitlines()[-1])
    assert row["action"] == "skip_rule" and "ticker list" in row["reason"]


def test_handle_event_trades_when_rules_pass_without_ai(home, broker, no_policy):
    state = {"positions": {}, "seen": []}
    cfg = {"rule_tickers": ["SPY"], "rule_sides": "calls"}
    out = agent.handle_event(make_event(), cfg, state, broker, object(), lambda s: None)
    assert out and out.startswith("ENTRY") and broker.calls


def test_loss_streak_stops_entries_today(home, broker, no_policy):
    for p in (-10, -20):
        agent._log_trade({"action": "exit", "contract": "x", "est_pnl_usd": p})
    out = agent.handle_event(make_event(), {"rule_loss_streak": 2},
                             {"positions": {}, "seen": []}, broker, object(),
                             lambda s: None)
    assert "losing exit" in out and broker.calls == []


# ── dashboard chat ──

def test_chat_commands(home):
    agent._save(agent.CONFIG_PATH, {"source": "manual"})
    assert agent._apply_command("tickers spy, qqq")[0]
    assert agent._apply_command("calls only")[0]
    assert agent._apply_command("min strike 10")[0]
    assert agent._apply_command("loss stop 2")[0]
    handled, reply = agent._apply_command("no 0dte on")
    assert handled and "no 0DTE" in reply
    cfg = agent._load(agent.CONFIG_PATH)
    assert cfg["rule_tickers"] == ["SPY", "QQQ"] and cfg["rule_sides"] == "calls"
    assert cfg["rule_min_strike"] == 10 and cfg["rule_loss_streak"] == 2
    agent._apply_command("tickers any")
    assert agent._load(agent.CONFIG_PATH)["rule_tickers"] == []
    handled, reply = agent._apply_command("rules")
    assert handled and "calls only" in reply


# ── setup plans ──

def test_plan_rules_apply_without_ai(home):
    plan = {"source": {"type": "manual"},
            "rules": {"tickers": ["spy"], "sides": "puts", "loss_streak": 1}}
    cfg, report = setup_plan.apply(plan, {}, True, str(home))
    assert cfg is not None, report["errors"]
    assert cfg["rule_tickers"] == ["SPY"] and cfg["rule_sides"] == "puts"
    assert any(a.startswith("rules:") for a in report["applied"])


def test_plan_rejects_bad_rules(home):
    cfg, report = setup_plan.apply({"rules": {"colour": "red"}}, {}, True, str(home))
    assert cfg is None and "unknown rules" in " ".join(report["errors"])
    cfg, report = setup_plan.apply({"rules": {"sides": "straddles"}}, {}, True, str(home))
    assert cfg is None


def test_plan_rejects_unknown_rule():
    assert "rules" in setup_plan.ALLOWED_TOP
    assert "rules" in setup_plan.EXAMPLE_PLAN and "policy" not in setup_plan.EXAMPLE_PLAN
