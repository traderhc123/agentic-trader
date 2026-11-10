"""The heartbeat's side of handing a setup to a server: while the copy is
being taken (and for good once it has moved) the loop places no orders —
whatever the pause switch says — and it tells the mover when it is between
cycles, so the copy can never miss a trade that is half-placed.

Hermetic: fake broker and feed, no dashboard, no sleeps, tmp AGENT_HOME.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import agent
import keep_running
import transfer
import webui

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=ET)


class _Broker:
    @staticmethod
    def client(cfg):
        return object()


@pytest.fixture
def run_loops(home, monkeypatch):
    """`agent.py run` for a number of loops; returns what happened."""
    import brokers
    seen = {"polls": 0, "idle_during_poll": [], "notes": [], "status": None}

    class Source:
        @staticmethod
        def poll(cfg, st, save_state):
            seen["polls"] += 1
            seen["idle_during_poll"].append(transfer.CYCLE_IDLE.is_set())
            return []

    def no_dashboard(get_status, *a, **k):
        seen["status"] = get_status
        raise OSError("no dashboard in tests")

    def run(loops=2, before_loop=None):
        n = {"i": 0}

        def sleep(wait, controls):
            n["i"] += 1
            if n["i"] >= loops:
                raise KeyboardInterrupt
            if before_loop:
                before_loop(n["i"])
        monkeypatch.setattr(agent, "_sleep_unless_stopped", sleep)
        agent.cmd_run()
        return seen

    monkeypatch.setattr(agent, "_now", lambda: NOON.timestamp())
    monkeypatch.setattr(agent, "_et_now", lambda: NOON)
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "fake", _Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_maybe_daily_digest", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_position_upkeep", lambda *a, **k: None)
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, priority=None:
                        seen["notes"].append(msg))
    monkeypatch.setitem(webui.CONTROLS, "paused", False)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setitem(transfer.HANDOFF, "hold", False)
    transfer.CYCLE_IDLE.set()
    (home / "config.json").write_text(json.dumps(
        {"source": "stub", "broker": "fake", "dry_run": True}), encoding="utf-8")
    (home / "state.json").write_text(json.dumps(
        {"seen": [], "positions": {}}), encoding="utf-8")
    yield run
    transfer.CYCLE_IDLE.set()


def test_heartbeat_says_when_it_is_mid_cycle(run_loops):
    seen = run_loops(loops=2)
    assert seen["polls"] == 2
    assert seen["idle_during_poll"] == [False, False]   # busy while it works


def test_heartbeat_is_idle_again_between_cycles(run_loops):
    idle_between = []
    run_loops(loops=3, before_loop=lambda i: idle_between.append(
        transfer.CYCLE_IDLE.is_set()))
    assert idle_between == [True, True]


def test_hold_stops_trading_even_after_resume(run_loops):
    """Loop 1 runs normally. Then the setup is handed over (hold) and the
    owner types resume: loops 2 and 3 still must not read the feed."""
    def handed_over(i):
        transfer.HANDOFF["hold"] = True
        webui.CONTROLS["paused"] = False   # "resume" got through somehow
    seen = run_loops(loops=3, before_loop=handed_over)
    assert seen["polls"] == 1


# ── owners who deployed a server before the hand-over fix ──

def test_start_warns_once_when_an_earlier_deploy_left_two_copies(
        run_loops, home):
    (home / "do_ssh_key").write_text("PRIVATE", encoding="utf-8")
    seen = run_loops(loops=1)
    warned = [m for m in seen["notes"] if "DigitalOcean" in m]
    assert len(warned) == 1 and "twice" in warned[0]
    # ...and it stays on the dashboard for this run
    banners = seen["status"]()["expiry_warnings"]
    assert any("DigitalOcean" in b["text"] and b["level"] == "warn"
               for b in banners)
    seen["notes"].clear()
    seen = run_loops(loops=1)   # next start: not repeated
    assert not [m for m in seen["notes"] if "DigitalOcean" in m]
    assert not any("DigitalOcean" in b["text"]
                   for b in seen["status"]()["expiry_warnings"])


def test_start_does_not_warn_without_an_earlier_deploy(run_loops):
    seen = run_loops(loops=1)
    assert not [m for m in seen["notes"] if "DigitalOcean" in m]
