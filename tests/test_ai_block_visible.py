"""Anything that blocks entries because an AI is needed must show on the
owner's dashboard, with the fix — before and after a signal is skipped."""
import agent
import broker_events
import llm_policy
from conftest import make_event


def _write_policy(home):
    (home / "policy.md").write_text("- Skip puts.\n", encoding="utf-8")


def test_missing_ai_skip_is_a_problem_not_a_policy_veto(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: None)
    _write_policy(home)                       # policy set, no AI connected
    out = agent.handle_event(make_event(event_id="e1"), {}, {"positions": {}},
                             None, None, lambda s: None)
    assert out.startswith("SKIPPED") and "AI needed" in out
    row = agent._recent_trades(5)[-1]
    assert row["action"] == "skip_ai_needed"
    assert "Connect your AI" in row["hint"]
    kinds = [p["kind"] for p in agent._problems_today()]
    assert "skip_ai_needed" in kinds and "policy_veto" not in kinds


def test_failing_ai_skip_is_also_a_problem(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: None)
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, l: {
        "act": False, "ai_problem": True,
        "reason": "AI key invalid/missing (Setup → Connect your AI) — skipping (fail-safe)"})
    agent.handle_event(make_event(event_id="e2"), {}, {"positions": {}},
                       None, None, lambda s: None)
    row = agent._recent_trades(5)[-1]
    assert row["action"] == "skip_ai_needed" and "Connect your AI" in row["hint"]


def test_ai_rate_limit_gets_the_ai_hint_not_the_robinhood_one():
    h = broker_events.hint("policy check rate-limited — skipping (fail-safe)")
    assert "Connect your AI" in h and "Robinhood" not in h


def test_real_rule_veto_is_still_a_policy_veto(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: None)
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, l: {
        "act": False, "reason": "rule: skip puts"})
    agent.handle_event(make_event(event_id="e3"), {}, {"positions": {}},
                       None, None, lambda s: None)
    assert agent._recent_trades(5)[-1]["action"] == "policy_veto"


def test_standing_alert_before_any_signal(home):
    _write_policy(home)
    rows = agent._problems_with_ai_block({})
    assert rows and rows[0]["kind"] == "skip_ai_needed" and rows[0]["standing"]
    assert "EVERY entry" in rows[0]["last"] and "Connect your AI" in rows[0]["hint"]


def test_no_standing_alert_when_ai_connected_or_no_policy(home):
    assert agent._problems_with_ai_block({}) == []            # no policy
    _write_policy(home)
    assert agent._problems_with_ai_block({"anthropic_api_key": "sk-ant-x"}) == []
    assert agent._problems_with_ai_block({"llm_fallback": "act"}) == []


def test_standing_alert_merges_with_todays_skips(home, monkeypatch):
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: None)
    _write_policy(home)
    agent.handle_event(make_event(event_id="e4"), {}, {"positions": {}},
                       None, None, lambda s: None)
    rows = agent._problems_with_ai_block({})
    ai = [r for r in rows if r["kind"] == "skip_ai_needed"]
    assert len(ai) == 1 and ai[0]["count"] == 1 and ai[0]["standing"]
    assert rows[0] is ai[0]
