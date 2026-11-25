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
def _no_phone_remote(monkeypatch):
    """start_app would also open the phone API on the fixed port 8722; tests
    of the phone API switch it back on (tests/test_remote_access.py)."""
    monkeypatch.setenv("AGENT_REMOTE", "0")


@pytest.fixture(autouse=True)
def _no_pass_price_ask(monkeypatch):
    """Pass prices come from AgentHC (pass_prices asks it): tests start with
    a cache that never expires and holds no server prices, so the built-in
    fallback prices apply and nothing goes to the network. Tests of the
    server-price path reset hc._price_cache themselves."""
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(hc, "_price_cache",
                        {"at": float("inf"), "usd": None, "sats": None})
    monkeypatch.setattr(hc, "_live_btc_usd", lambda: 0.0)   # the auto cap's BTC price


@pytest.fixture(autouse=True)
def _no_wallet_health_left_over(monkeypatch):
    """The wallet health the dashboard shows is kept in memory for the run
    and re-asked in a background thread: each test starts with none, and
    no thread asks a wallet's server (tests of it switch it on with the
    network stubbed)."""
    import lightning_wallet
    monkeypatch.setattr(lightning_wallet, "_HEALTH", {})
    monkeypatch.setattr(lightning_wallet, "_HEALTH_ASKING", set())
    monkeypatch.setattr(lightning_wallet, "HEALTH_BACKGROUND", False)


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
    import transfer
    import webui
    monkeypatch.setitem(agent._STATE_HELD, "on", False)
    monkeypatch.setitem(webui.CONTROLS, "replaced", False)
    # a "Create move file" holds the heartbeat until "I saved the file"
    # (or transfer.MOVE_HOLD_S): never into the next test
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    monkeypatch.setitem(transfer.HANDOFF, "by", None)
    monkeypatch.setattr(transfer, "MOVE_HOLD_S", 24 * 3600)
    transfer.CYCLE_IDLE.set()
    # the in-memory "trade log can't be read" skip rows (_problems_today)
    monkeypatch.setattr(agent, "_UNREADABLE_SKIPS", [])


@pytest.fixture(autouse=True)
def _no_read_schedule_left_over(monkeypatch):
    """The feed's read schedule, a refusal back-off, a pending "read buying
    power now", the ledger check's remembered holdings and the update
    check's last answer are kept in memory for the run: one test's must not
    change what the next test reads."""
    from sources import agenthc_day_trade_ideas as hc
    monkeypatch.setattr(hc, "_REFUSAL", {})
    monkeypatch.setattr(agent, "FEED_GATE", {"read_at": None, "now": True})
    monkeypatch.setattr(agent, "_CASH_NOW", {"on": False})
    monkeypatch.setattr(agent, "_HOLDINGS_MEMO", {})
    monkeypatch.setitem(agent._RUN_CFG, "cfg", None)
    import webui
    monkeypatch.setattr(webui, "_UPDATE_MEMO", {})  # the update check's answer


@pytest.fixture(autouse=True)
def _no_real_agent_home(tmp_path_factory, monkeypatch):
    """A test without `home` still gets a throwaway agent home: a test that
    logs a row must not write it into the real ~/.agentic-trader, and what
    the agent reads from there (the live trade history behind a mode that
    was never chosen, agent._is_dry) must not depend on the machine or on
    the tests that ran before. `home` repoints the same paths again. Also
    forgets the mode a previous test's run settled (agent._SETTLED_DRY)."""
    h = tmp_path_factory.mktemp("default-agent-home")
    monkeypatch.setenv("AGENT_HOME", str(h))
    monkeypatch.setattr(agent, "HOME", str(h))
    monkeypatch.setattr(agent, "ACCEPTANCE_PATH", str(h / "acceptance.json"))
    monkeypatch.setattr(agent, "CONFIG_PATH", str(h / "config.json"))
    monkeypatch.setattr(agent, "STATE_PATH", str(h / "state.json"))
    monkeypatch.setattr(agent, "TRADES_PATH", str(h / "trades.jsonl"))
    monkeypatch.setattr(agent, "_SETTLED_DRY", {"dry": None, "warned": False})


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
        lambda cfg, ev, log, **kw: {"act": True, "reason": "test: mechanical"})


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
