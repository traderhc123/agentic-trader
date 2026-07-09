"""Shared fixtures — every test runs against a throwaway AGENT_HOME.

agent.py computes its state paths at import time, so the `home` fixture both
sets the AGENT_HOME env var (for modules that read it at call time) and
repoints agent's module-level path constants (for the import-time ones).
No test may ever touch the real ~/.agentic-trader or place a network call.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated AGENT_HOME; yields its path."""
    h = tmp_path / "agent-home"
    h.mkdir()
    monkeypatch.setenv("AGENT_HOME", str(h))
    monkeypatch.setattr(agent, "HOME", str(h))
    monkeypatch.setattr(agent, "ACCEPTANCE_PATH", str(h / "acceptance.json"))
    monkeypatch.setattr(agent, "CONFIG_PATH", str(h / "config.json"))
    monkeypatch.setattr(agent, "STATE_PATH", str(h / "state.json"))
    monkeypatch.setattr(agent, "TRADES_PATH", str(h / "trades.jsonl"))
    return h


@pytest.fixture
def no_policy(monkeypatch):
    """Force pure mechanical mode: the policy brain always approves."""
    import llm_policy
    monkeypatch.setattr(
        llm_policy, "evaluate",
        lambda cfg, ev, log: {"act": True, "reason": "test: mechanical"})


class StubBroker:
    """Records execute() calls; behaves like a real adapter for state."""

    def __init__(self, changed=True):
        self.calls = []
        self.changed = changed

    def execute(self, client, cfg, event, state):
        self.calls.append(event)
        if not self.changed:
            return False
        pos_key = (f"{event['ticker']}|{event['expiry']}|{event['strike']}|"
                   f"{event['type']}")
        if event["event"] == "ENTERED":
            state["positions"][pos_key] = {"option_id": "stub", "qty": 1}
        else:
            state["positions"].pop(pos_key, None)
        return True


@pytest.fixture
def broker():
    return StubBroker()


def make_event(**over):
    ev = {
        "event": "ENTERED",
        "ticker": "SPY",
        "expiry": "2026-07-10",
        "strike": 752.0,
        "type": "C",
        "event_id": "test-1",
    }
    ev.update(over)
    return ev
