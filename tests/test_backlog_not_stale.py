"""Signals from before the agent was watching (agent started after the open,
or "other trades" just switched on) are skipped quietly — never bought, but
no "too old / computer asleep?" problem or alert. A signal that arrives late
while the agent IS running still gets flagged."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import agent
from conftest import make_event


@pytest.fixture
def fresh(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})
    return sent


class Broker:
    def execute(self, client, cfg, ev, state):
        raise AssertionError("a stale signal must never reach the broker")


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _rows(home):
    p = home / "trades.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


CFG = {"max_entries_per_day": 5, "include_other_trades": True}


def test_backlog_on_first_poll_is_quiet(home, no_policy, fresh):
    agent.note_watching(CFG)  # first poll happens now
    ev = make_event(occurred_at=_ago(90), track="other")
    assert agent.handle_event(ev, CFG, {"positions": {}}, Broker(), object(),
                              lambda s: None) is None
    assert _rows(home) == [] and fresh == []


def test_other_trades_switched_on_later(home, no_policy, fresh):
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    agent.note_watching({"include_other_trades": False}, now=t0)  # main only
    agent.note_watching(CFG)                                      # switched on now
    ev = make_event(occurred_at=_ago(60), track="other")
    agent.handle_event(ev, CFG, {"positions": {}}, Broker(), object(), lambda s: None)
    assert _rows(home) == [] and fresh == []
    # ...but a MAIN signal from an hour ago, while main was being watched, is a real delay
    agent.handle_event(make_event(occurred_at=_ago(60), event_id="m1"), CFG,
                       {"positions": {}}, Broker(), object(), lambda s: None)
    assert [r["action"] for r in _rows(home)] == ["skip_stale"] and len(fresh) == 1


def test_late_while_running_still_flagged(home, no_policy, fresh):
    agent.note_watching(CFG, now=datetime.now(timezone.utc) - timedelta(hours=1))
    ev = make_event(occurred_at=_ago(10), track="other")   # asleep for 10 min
    agent.handle_event(ev, CFG, {"positions": {}}, Broker(), object(), lambda s: None)
    (row,) = _rows(home)
    assert row["action"] == "skip_stale" and len(fresh) == 1


def test_switching_off_then_on_restarts_the_clock(fresh):
    t0 = datetime.now(timezone.utc) - timedelta(hours=3)
    agent.note_watching(CFG, now=t0)
    agent.note_watching({"include_other_trades": False})
    agent.note_watching(CFG)
    assert agent._WATCH_SINCE["other"] > t0
