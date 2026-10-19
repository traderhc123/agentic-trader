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
