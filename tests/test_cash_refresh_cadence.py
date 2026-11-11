"""Buying power is read every 10 minutes inside the feed window and once an
hour outside it (only a deposit changes it then); at once on start, when
the owner switches between practice and live, on Resume and on "check
now". Zero network: a stub broker counts its buying-power reads."""

from datetime import datetime

import agent

ET = agent.MARKET_TZ


class CountingBroker:
    def __init__(self):
        self.reads = 0

    def buying_power(self, client, cfg):
        self.reads += 1
        return 1234.5


def at(monkeypatch, start):
    """_refresh_cash stamps its read with agent._now: follow the loop."""
    box = {"t": start.timestamp()}
    monkeypatch.setattr(agent, "_now", lambda: box["t"])
    return box


def test_offwindow_hourly(monkeypatch):
    """A Saturday at the 5-minute off-hours loop: 24 reads (it was 144)."""
    start = datetime(2026, 10, 31, 0, 0, tzinfo=ET)
    box = at(monkeypatch, start)
    broker = CountingBroker()
    cash = {"at": 0.0, "bp": None, "warning": ""}
    for i in range(288):
        box["t"] = start.timestamp() + i * 300
        if agent._cash_refresh_due({}, cash, box["t"],
                                   datetime.fromtimestamp(box["t"], ET)):
            agent._refresh_cash({}, {}, lambda s: None, broker, object(), cash)
    assert broker.reads == 24
    assert cash["bp"] == 1234.5 and cash["at"] == start.timestamp() + 23 * 3600


def test_offwindow_setting(monkeypatch):
    """cash_refresh_idle_seconds: 600 is the old cadence (never faster)."""
    start = datetime(2026, 10, 31, 0, 0, tzinfo=ET)
    box = at(monkeypatch, start)
    broker = CountingBroker()
    cash = {"at": 0.0}
    for i in range(288):
        box["t"] = start.timestamp() + i * 300
        if agent._cash_refresh_due({"cash_refresh_idle_seconds": 0}, cash, box["t"],
                                   datetime.fromtimestamp(box["t"], ET)):
            agent._refresh_cash({}, {}, lambda s: None, broker, object(), cash)
    assert broker.reads == 144


def test_inwindow_unchanged(monkeypatch):
    """09:25 to 16:15 on a Wednesday at 30-second loops: every 10 minutes."""
    start = datetime(2026, 10, 28, 9, 25, tzinfo=ET)
    box = at(monkeypatch, start)
    broker = CountingBroker()
    cash = {"at": 0.0}
    loops = (16 * 60 + 15 - (9 * 60 + 25)) * 2
    for i in range(loops):
        box["t"] = start.timestamp() + i * 30
        if agent._cash_refresh_due({}, cash, box["t"],
                                   datetime.fromtimestamp(box["t"], ET)):
            agent._refresh_cash({}, {}, lambda s: None, broker, object(), cash)
    assert broker.reads == (loops - 1) * 30 // 600 + 1   # 0, 600, 1200, ... s


def test_start_and_resume_read_immediately(home, monkeypatch):
    (home / "config.json").write_text('{"source": "agenthc"}', encoding="utf-8")
    sat = datetime(2026, 10, 31, 10, 0, tzinfo=ET)
    box = at(monkeypatch, sat)
    cash = {"at": 0.0}
    broker = CountingBroker()
    # start: nothing read yet -> due
    assert agent._cash_refresh_due({}, cash, box["t"], sat)
    agent._refresh_cash({}, {}, lambda s: None, broker, object(), cash)
    later = sat.replace(minute=5)
    assert not agent._cash_refresh_due({}, cash, box["t"] + 300, later)
    # Resume: at once, once
    agent._apply_command("resume")
    assert agent._cash_refresh_due({}, cash, box["t"] + 300, later)
    agent._refresh_cash({}, {}, lambda s: None, broker, object(), cash)
    assert not agent._cash_refresh_due({}, cash, box["t"] + 300, later)
    # going live (or back to practice): at once, as before
    assert agent._cash_refresh_due({"dry_run": True}, cash, box["t"] + 300, later)
    assert broker.reads == 2
