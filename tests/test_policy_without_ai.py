"""A trading policy with no AI connected skips EVERY entry (fail-safe).

A paying user hit this: skipped "Connect your AI", wrote a policy in Safety,
launched, and the agent never traded. The wizard, launch, agent startup and
doctor must all catch that state instead of letting it pass silently.
"""

import json

import pytest

import agent
import llm_policy
import webui


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _write_policy(home, text="- Skip puts.\n"):
    (home / "policy.md").write_text(text)


# ── llm_policy.entries_blocked ──────────────────────────────────────────────

def test_no_policy_never_blocked(home):
    assert llm_policy.entries_blocked({}) is False


def test_policy_without_ai_is_blocked(home):
    _write_policy(home)
    assert llm_policy.entries_blocked({}) is True


def test_policy_with_ai_not_blocked(home):
    _write_policy(home)
    assert llm_policy.entries_blocked({"anthropic_api_key": "sk-ant-x"}) is False


def test_fallback_act_not_blocked(home):
    _write_policy(home)
    assert llm_policy.entries_blocked({"llm_fallback": "act"}) is False


def test_blocked_state_really_skips_entries(home):
    """Guard the premise: this is the state evaluate() skips on."""
    _write_policy(home)
    v = llm_policy.evaluate({}, {"event_id": "e1"}, [])
    assert v["act"] is False and "no AI model connected" in v["reason"]


def test_disable_policy_unblocks_and_is_reversible(home):
    _write_policy(home)
    assert llm_policy.disable_policy() is True
    assert llm_policy.entries_blocked({}) is False
    assert (home / "policy.md.disabled").read_text() == "- Skip puts.\n"
    assert llm_policy.disable_policy() is False  # nothing left to move


# ── wizard Safety step ──────────────────────────────────────────────────────

def test_safety_refuses_policy_without_ai():
    e = webui._policy_save_error({}, "- Skip puts.", "")
    assert e and "needs a connected AI" in e


def test_safety_allows_policy_with_claude_key():
    assert webui._policy_save_error({}, "- Skip puts.", "sk-ant-abc") is None


def test_safety_refuses_non_claude_key_for_policy():
    e = webui._policy_save_error({}, "- Skip puts.", "sk-proj-abc")
    assert e and "isn't a Claude key" in e


def test_safety_allows_policy_when_ai_connected():
    cfg = {"llm_provider": "anthropic", "llm_api_key": "sk-ant-x"}
    assert webui._policy_save_error(cfg, "- Skip puts.", "") is None


def test_safety_allows_blank_policy_without_ai():
    assert webui._policy_save_error({}, "   ", "") is None


# ── agent doctor + dashboard status ─────────────────────────────────────────

def test_doctor_flags_policy_without_ai(home, monkeypatch):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    (home / "config.json").write_text(json.dumps({"source": "manual"}))
    _write_policy(home)
    checks, _ = agent.doctor_checks()
    c = next(c for c in checks if c["id"] == "policy_ai")
    assert c["ok"] is False and "every entry will be skipped" in c["detail"]
    assert c["who"] == "owner" and c["fix"]


def test_doctor_policy_check_green_without_policy(home, monkeypatch):
    monkeypatch.setattr(agent, "consent_ok", lambda: True)
    (home / "config.json").write_text(json.dumps({"source": "manual"}))
    checks, _ = agent.doctor_checks()
    c = next(c for c in checks if c["id"] == "policy_ai")
    assert c["ok"] is True


# ── remaining paths: run, go live, CLI setup, broken-AI alert ──────────────

class _Broker:
    @staticmethod
    def client(cfg):
        return object()


@pytest.fixture
def runnable(home, monkeypatch):
    """A configured install, up to the point cmd_run would start the loop."""
    import brokers
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", _Broker)
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    (home / "config.json").write_text(json.dumps({"source": "manual", "broker": "stub"}))
    return sent


def test_run_refuses_to_start_and_notifies_once_a_day(home, runnable):
    _write_policy(home)
    for _ in range(2):  # systemd restarts it every 30s: alert only once
        with pytest.raises(SystemExit) as e:
            agent.cmd_run()
        assert e.value.code == 1
    assert len(runnable) == 1 and "did NOT start" in runnable[0]


def test_run_starts_anyway_with_open_positions(home, runnable, monkeypatch):
    """Exits must still run, so open positions never get stranded."""
    _write_policy(home)
    (home / "state.json").write_text(json.dumps(
        {"seen": [], "positions": {"SPY|2026-10-20|600|C": {"qty": 1}}}))
    import webui

    class _Started(Exception):
        pass

    def _boom(*a, **k):
        raise _Started
    monkeypatch.setattr(webui, "start_dashboard", _boom)
    with pytest.raises(_Started):
        agent.cmd_run()


def test_go_live_refused_while_blocked(home):
    (home / "config.json").write_text(json.dumps({"dry_run": True}))
    _write_policy(home)
    handled, reply = agent._apply_command("go live")
    assert handled and reply.startswith("Not going live")
    assert json.loads((home / "config.json").read_text())["dry_run"] is True


def test_policy_off_command_then_go_live(home):
    (home / "config.json").write_text(json.dumps({"dry_run": True}))
    _write_policy(home)
    assert "Policy turned off" in agent._apply_command("policy off")[1]
    handled, reply = agent._apply_command("dry off")
    assert "REAL orders" in reply
    assert json.loads((home / "config.json").read_text())["dry_run"] is False


def test_cli_setup_turns_policy_off_if_no_ai(home, monkeypatch):
    answers = iter(["y", "", "n"])  # enable policy, blank AI key, don't retry
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(llm_policy, "connect_cli", lambda cfg: cfg)
    llm_policy.setup({})
    assert llm_policy.entries_blocked({}) is False
    assert (home / "policy.md.disabled").exists()


def test_fallback_verdicts_flag_ai_problem(home):
    _write_policy(home)
    assert llm_policy.evaluate({}, {"event_id": "e"}, []).get("ai_problem") is True


def test_ai_problem_veto_alerts_once_per_day(home, monkeypatch):
    from conftest import make_event
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, l: {
        "act": False, "ai_problem": True, "reason": "AI key invalid/missing"})
    state = {"positions": {}}
    for i in range(3):
        out = agent.handle_event(make_event(event_id=f"e{i}"), {}, state,
                                 None, None, lambda s: None)
        assert out.startswith("SKIPPED") and "AI needed" in out
    assert len(sent) == 1 and "couldn't be checked" in sent[0]


def test_rule_veto_does_not_alert(home, monkeypatch):
    from conftest import make_event
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(llm_policy, "evaluate", lambda c, e, l: {
        "act": False, "reason": "rule: skip puts"})
    agent.handle_event(make_event(), {}, {"positions": {}}, None, None, lambda s: None)
    assert sent == []
