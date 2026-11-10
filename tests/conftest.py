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


@pytest.fixture(autouse=True)
def _no_order_waits(monkeypatch):
    """moomoo's exit fill check polls with sleeps: none in tests."""
    from brokers import moomoo
    monkeypatch.setattr(moomoo, "_CONFIRM_WAIT_S", 0)
    monkeypatch.setattr(moomoo, "_CANCEL_SETTLE_S", 0)


@pytest.fixture(autouse=True)
def _no_state_hold_left_over(monkeypatch):
    """The damaged-state hold is kept in memory for the run (agent._STATE_HELD):
    one test's hold must not stop the next test's loop. The same for "a
    move file was imported" (webui.CONTROLS["replaced"]), after which the
    loop saves nothing."""
    import webui
    monkeypatch.setitem(agent._STATE_HELD, "on", False)
    monkeypatch.setitem(webui.CONTROLS, "replaced", False)


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


@pytest.fixture(autouse=True)
def _no_leftover_sigterm_handler():
    """`agent.cmd_run()` makes SIGTERM a stop request for its process — here
    that is the test runner. Put back whatever was there after every test."""
    import signal

    import keep_running
    try:
        before = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):
        before = None
    yield
    getattr(keep_running, "_sigterm", [False])[0] = False
    if before is not None:
        try:
            signal.signal(signal.SIGTERM, before)
        except (ValueError, OSError):
            pass


@pytest.fixture
def frozen_now(monkeypatch):
    """agent's datetime.now() pinned to 2026-10-30 15:00 UTC (returned), so
    a test that reads 'today' more than once can't straddle midnight."""
    from datetime import datetime, timezone
    fixed = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(agent, "datetime", Frozen)
    return fixed


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
