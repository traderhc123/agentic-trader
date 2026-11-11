"""The position upkeep (every 120 s) uses the holdings list the ledger check
(every 60 s) just read in the same loop, instead of asking the broker for
the same contracts again. Nothing that decides or confirms an exit ever
uses a remembered list. Real `agent.py run` loop paths with a fake broker.
Zero network."""

from datetime import timedelta

import agent
from test_exit_remainder import TAG, U1, U2, Client, FakeBroker, _loop, et
from test_exit_remainder import notes  # noqa: F401  (fixture reuse)

LATER = "RIVN|2026-11-13|13.5|C"   # expires next week: no expiry-day paths


def held_state(qty=6):
    t = et(9, 0).timestamp() - 3600
    row = {"option_id": U1, "qty": qty, "acct": TAG, "opened_event": "open-1",
           "opened_at": t, "last_order_at": t, "entry_mark": 1.0,
           "order_ids": ["buy-1"]}
    return {"seen": [], "positions": {LATER: row}, "agent_order_ids": ["buy-1"],
            "position_sync": {"proven": {TAG: "2026-11-01T15:00:00+00:00"}}}


def test_upkeep_uses_fresh_ledger_read(home, monkeypatch, notes):
    """09:30 to 16:00 with one position held, a loop every 30 s: the ledger
    check reads 390 times, the upkeep's 195 reads are all the ledger
    check's — 390 broker reads, not 585."""
    b = FakeBroker(held={U1: 6})
    start = et(9, 30)
    times = [start + timedelta(seconds=30 * i) for i in range(780)]
    saved = _loop(home, monkeypatch, held_state(), b, times, upkeep=True)
    assert len(b.asked) == 390
    assert LATER in saved["positions"]        # nothing changed: all held
    assert b.sells == []


def test_upkeep_reads_itself_without_a_ledger_read(home, monkeypatch, notes):
    """After the close the ledger check doesn't run: the upkeep reads the
    broker itself, as before."""
    b = FakeBroker(held={U1: 6})
    times = [et(16, 30), et(16, 30, s=30)]
    _loop(home, monkeypatch, held_state(), b, times, upkeep=True)
    assert len(b.asked) == 1


def test_memo_too_old_or_other_ids_reads_broker(monkeypatch):
    c = Client()
    clock = {"t": 1000.0}
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock["t"])
    agent._remember_holdings(c, [U1], {U1: 6})
    assert agent._remembered_holdings(c, [U1, U2]) is None   # not all covered
    assert agent._remembered_holdings(Client(), [U1]) is None  # another client
    clock["t"] += agent.HOLDINGS_MEMO_S
    assert agent._remembered_holdings(c, [U1]) is None        # too old
    clock["t"] = 1000.0
    assert agent._remembered_holdings(c, [U1]) == {U1: 6}
    assert agent._remembered_holdings(c, [U1]) is None        # used once only


def test_exit_paths_never_use_memo(home, monkeypatch, notes):
    """_check_before_exit with a fresh remembered list that says the
    contracts are held: it still asks the broker (which says they're gone)."""
    now = et(14, 0)
    monkeypatch.setattr(agent, "_now", lambda: now.timestamp())
    monkeypatch.setattr(agent, "_et_now", lambda: now)
    b = FakeBroker(held={U1: 0})
    c = Client()
    st = held_state()
    agent._remember_holdings(c, [U1], {U1: 6})
    ev = {"event": "EXITED", "event_id": "feed-x", "ticker": "RIVN",
          "expiry": "2026-11-13", "strike": 13.5, "type": "C"}
    agent._check_before_exit({"broker": "fake"}, st, lambda s: None, b, c, LATER, ev)
    assert b.asked == [[U1]]
    assert not agent._HOLDINGS_MEMO.get("used")
    # the position upkeep's own call (not deferred) reads the broker too
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    agent._position_upkeep({"broker": "fake"}, st, lambda s: None, b, c)
    assert len(b.asked) == 2 and not agent._HOLDINGS_MEMO.get("used")


def test_deferred_upkeep_is_not_starved(monkeypatch):
    """A loop that broke off before running the deferred upkeep: the next
    loop's upkeep runs on its own instead of waiting again."""
    calls = []
    monkeypatch.setattr(agent, "_sync_positions",
                        lambda *a, **k: calls.append(k.get("memo_ok")) or {})
    monkeypatch.setattr(agent, "_ledger_check_due", lambda *a, **k: True)
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    b, c = FakeBroker(held={U1: 6}), Client()
    up = agent._position_upkeep
    up({}, {}, lambda s: None, b, c, now=100.0, defer_to_ledger=True)
    assert calls == [] and agent._POS_SYNC.get("deferred")
    up({}, {}, lambda s: None, b, c, now=130.0, defer_to_ledger=True)
    assert calls == [False] and not agent._POS_SYNC.get("deferred")
