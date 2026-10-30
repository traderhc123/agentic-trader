"""Trades the owner closes in the broker app: the agent reads what the
account holds and, only on repeated positive evidence, stops tracking what's
gone — logged as an exit (P&L, slot, later exit signal all line up). An
unreadable or unclear answer must never change anything. Zero network."""

import json
import sys
from datetime import datetime, timezone

import pytest

import agent
import broker_events
import rules
from brokers import account_tag
from conftest import StubBroker, make_event

U1 = "14a8e49b-0f10-4d64-9cf9-bd1a68e9da44"
U2 = "7f9e2c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
KEY = "SPY|2099-12-18|752.0|C"
KEY2 = "QQQ|2099-12-18|500.0|P"
CONTRACT = "SPY 2099-12-18 $752.0 C"
CFG = {"broker": "robinhood", "robinhood_account": "A1",
       "max_entries_per_day": 50}
TAG = account_tag(CFG)
T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc).timestamp()
_REAL_IN_OPEN_RUSH = agent._in_open_rush  # the autouse clock fixture patches it


# ── fakes ───────────────────────────────────────────────────────────────────

class HoldBroker:
    """Adapter with the holdings hook but no sales hook. held_option_qty
    answers from a script (one step per call, the last step repeats; an
    exception step is raised). execute records the ledger qty it sells."""

    def __init__(self, *held, mark=0.0):
        self.held = list(held)
        self.asked = []
        self.executed = []
        self.mark = mark

    def held_option_qty(self, client, cfg, option_ids):
        self.asked.append(list(option_ids))
        step = self.held.pop(0) if len(self.held) > 1 else (
            self.held[0] if self.held else None)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step()
        return dict(step) if isinstance(step, dict) else step

    def execute(self, client, cfg, event, state):
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        if event["event"] == "ENTERED":
            if key in state["positions"]:
                return False
            state["positions"][key] = {"option_id": U1, "qty": 2,
                                       "opened_event": event["event_id"]}
            return True
        pos = state["positions"].get(key)
        if not pos:
            return False
        self.executed.append(("EXITED", pos["qty"]))
        state["positions"].pop(key)
        return True

    def mark_price(self, client, cfg, event, side="buy"):
        return self.mark


class FillsBroker(HoldBroker):
    """Also reports the owner's sells (like Robinhood and Alpaca). `fills`
    is a list for every contract, a {option_id: list} dict, or None
    (can't read)."""

    def __init__(self, *held, fills=(), mark=0.0):
        super().__init__(*held, mark=mark)
        self.fills = fills
        self.fill_calls = []

    def closed_fills(self, client, cfg, option_id, since_iso=None,
                     own_order_ids=(), exclude_agentic=False):
        self.fill_calls.append({"option_id": option_id, "since_iso": since_iso,
                                "own": list(own_order_ids),
                                "exclude_agentic": exclude_agentic})
        fills = self.fills
        if isinstance(fills, dict):
            fills = fills.get(option_id, [])
        return None if fills is None else [dict(f) for f in fills]


class UnverifiedBroker(HoldBroker):
    """Books entries without checking they filled (like moomoo)."""
    VERIFIES_FILLS = False


class Down:
    last_error = "Robinhood connection lost"


def _fill(oid="u1", qty=2, price=1.8, at="2026-10-30T15:01:00Z"):
    return {"order_id": oid, "qty": qty, "price": price, "at": at, "by": "user"}


# ── fixtures and helpers ────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(agent, "_now", c)
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 11, 0, tzinfo=agent.MARKET_TZ))
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    return c


@pytest.fixture(autouse=True)
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


@pytest.fixture(autouse=True)
def _home(home):
    return home


def _log(home):
    p = home / "trades.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _row(**over):
    pos = {"option_id": U1, "qty": 2, "opened_event": "open-1", "entry_mark": 1.5,
           "acct": TAG, "opened_at": T0 - 3600, "last_order_at": T0 - 3600,
           "order_ids": ["o-agent"]}
    pos.update(over)
    return pos


def _state(trusted=True, **over):
    st = {"positions": {KEY: _row(**over)}, "seen": []}
    if trusted:
        st["position_sync"] = {"proven": {TAG: "2026-10-29T15:00:00+00:00"}}
    return st


def sync(st, b, save=None, client=None, **kw):
    return agent._sync_positions(CFG, st, save or (lambda s: None), b,
                                 client or object(), **kw)


def _exit(event_id="x1", **over):
    return make_event(event="EXITED", expiry="2099-12-18", event_id=event_id, **over)


def handle(st, b, ev=None, cfg=None, client=None):
    return agent.handle_event(ev or _exit(), cfg or CFG, st, b,
                              client or object(), lambda s: None)


def _status(r):
    return r.get(KEY, {}).get("status")


# ── periodic sync ───────────────────────────────────────────────────────────

def test_first_shortfall_read_records_miss_only(home, notes):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    assert sync(st, b) == {KEY: {"held": 0, "status": "miss"}}
    pos = st["positions"][KEY]
    assert pos["qty"] == 2 and pos["miss"]["reads"] == 1 and pos["miss"]["held"] == 0
    assert b.asked == [[U1]] and b.fill_calls == []
    assert _log(home) == [] and notes == []


def test_second_read_after_gap_closes_full(home, notes, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    saves = []

    def save(s):
        manual = [r for r in _log(home) if r.get("manual")]
        saves.append((KEY in s["positions"], len(manual)))

    sync(st, b, save)
    clock.tick(60)
    assert _status(sync(st, b, save)) == "closed"
    assert KEY not in st["positions"]
    tomb = st["closed_outside"][KEY]
    assert tomb["opened_event"] == "open-1" and tomb["option_id"] == U1
    (row,) = _log(home)
    assert row["action"] == "exit" and row["manual"] is True and row["cause"] == "sold"
    assert row["contract"] == CONTRACT and row["dry"] is False and row["qty"] == 2
    assert row["est_pnl_usd"] == 60.0 and row["exit_price"] == 1.8
    assert row["closed_at"] == "2026-10-30T15:01:00+00:00"
    assert row["detected_by"] == "sync" and row["event_id"] == f"outside|{KEY}|open-1"
    assert row["broker_order_ids"] == ["u1"]
    assert "hint" not in row and "partial" not in row
    assert "sold in your Robinhood account @ $1.80" in row["reason"]
    assert len(row["reason"]) <= 120
    assert len(notes) == 1 and "closed outside the agent" in notes[0]
    assert "$+60.00" in notes[0] and "exit signal will be ignored" in notes[0]
    assert (False, 0) in saves  # ledger saved before the P&L row was written
    assert b.fill_calls == [{"option_id": U1, "since_iso": "2026-10-30T14:00:00+00:00",
                             "own": ["o-agent"], "exclude_agentic": False}]


def test_read_inside_confirm_gap_not_counted(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(30)
    assert _status(sync(st, b)) == "miss"
    assert st["positions"][KEY]["miss"]["reads"] == 1
    clock.tick(30)
    assert _status(sync(st, b)) == "closed"


@pytest.mark.parametrize("unknown", [None, {}, {U1: None}, {U1: True}, {U1: -1},
                                     {U1: 1.0}, RuntimeError("boom")],
                         ids=["none", "empty", "null", "bool", "negative",
                              "float", "raises"])
def test_unknown_reads_neither_count_nor_reset(home, clock, notes, unknown):
    st = _state()
    b = FillsBroker({U1: 0}, unknown, {U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    assert sync(st, b) == {}
    pos = st["positions"][KEY]
    assert pos["qty"] == 2 and pos["miss"]["reads"] == 1
    assert _log(home) == [] and notes == []
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"


def test_always_unknown_never_drops(home, clock, notes):
    st = _state()
    b = FillsBroker(None, fills=[_fill()])
    for _ in range(10):
        assert sync(st, b) == {}
        clock.tick(600)
    assert st["positions"][KEY]["qty"] == 2 and "miss" not in st["positions"][KEY]
    assert _log(home) == [] and notes == [] and b.fill_calls == []


def test_client_last_error_skips_hook():
    st = _state()
    b = FillsBroker({U1: 0})
    assert sync(st, b, client=Down()) == {}
    assert b.asked == []


def test_last_error_set_during_read_is_unknown():
    class Flaky:
        last_error = None
    client = Flaky()

    def drop():
        client.last_error = "lost"
        return {U1: 0}
    st = _state()
    assert sync(st, HoldBroker(drop), client=client) == {}
    assert "miss" not in st["positions"][KEY]


def test_held_at_or_above_ledger_never_raises_qty_and_clears_miss():
    st = _state(miss={"held": 0, "reads": 1, "need": 2, "first_at": T0, "last_at": T0})
    r = sync(st, FillsBroker({U1: 5}))
    assert r == {KEY: {"held": 5, "status": "ok"}}
    pos = st["positions"][KEY]
    assert pos["qty"] == 2 and "miss" not in pos and pos["held_seen"] is True


def test_partial_close_reduces_qty(home, clock, notes):
    st = _state(qty=3)
    b = FillsBroker({U1: 1}, fills=[_fill(qty=2)])
    sync(st, b)
    clock.tick(60)
    assert sync(st, b) == {KEY: {"held": 1, "status": "reduced"}}
    pos = st["positions"][KEY]
    assert pos["qty"] == 1 and "miss" not in pos and pos["fill_ids_used"] == ["u1"]
    (row,) = _log(home)
    assert row["qty"] == 2 and row["partial"] is True and row["remaining"] == 1
    assert row["est_pnl_usd"] == 60.0  # (1.80 - 1.50) x 2 x 100
    assert row["reason"] == ("2 of 3 sold outside the agent @ $1.80 — the agent "
                             "still tracks 1")
    assert len(notes) == 1 and "still tracks 1x" in notes[0]


def test_conservative_target_then_new_miss(home, clock):
    st = _state()
    b = FillsBroker({U1: 1}, {U1: 0}, fills=[_fill("u1", 1, at="2026-10-30T15:01:00Z"),
                                             _fill("u2", 1, at="2026-10-30T15:02:00Z")])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "reduced"  # 1 then 0 -> keep 1, not 0
    pos = st["positions"][KEY]
    assert pos["qty"] == 1 and pos["miss"]["held"] == 0 and pos["miss"]["reads"] == 1
    assert pos["fill_ids_used"] == ["u2"]       # newest sale first
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    rows = _log(home)
    assert [r["broker_order_ids"] for r in rows] == [["u2"], ["u1"]]


def test_target_never_below_the_latest_read(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, {U1: 1}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    assert sync(st, b) == {KEY: {"held": 1, "status": "reduced"}}
    assert st["positions"][KEY]["qty"] == 1     # 0 then 1: still holds 1
    assert _log(home)[0]["qty"] == 1


def test_recovered_read_clears_miss(clock):
    st = _state()
    b = FillsBroker({U1: 0}, {U1: 2}, {U1: 0}, {U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "ok"
    assert "miss" not in st["positions"][KEY]
    clock.tick(60)
    assert _status(sync(st, b)) == "miss"   # a new shortfall starts over
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"


def test_ineligible_rows_never_requested(monkeypatch, clock):
    st = {"positions": {
        "A|2099-12-18|1.0|C": _row(dry=True),
        "B|2099-12-18|1.0|C": _row(option_id="dry"),
        "C|2099-12-18|1.0|C": _row(option_id=U2, pending={"side": "buy"}),
        "D|2099-12-18|1.0|C": _row(option_id="u-d", qty=0),
        "E|2099-12-18|1.0|C": _row(option_id="u-e", last_order_at=T0 - 100,
                                   miss={"held": 0, "reads": 1}),
        "F|2099-12-18|1.0|C": _row(option_id="u-f", acct="alpaca:paper"),
        "G|2020-01-17|1.0|C": _row(option_id="u-g"),
        "H|2099-12-18|1.0|C": _row(option_id="u-h"),
        "H|2099-12-18|1|C": _row(option_id="u-h"),
        "not-a-key": _row(option_id="u-x"),
    }}
    saved = []
    b = FillsBroker({})
    assert sync(st, b, lambda s: saved.append(1)) == {}
    assert b.asked == []
    assert "miss" not in st["positions"]["E|2099-12-18|1.0|C"]  # grace clears it
    assert saved
    # expiry day from 4 PM ET on: expiry/exercise move holdings
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 16, 0, tzinfo=agent.MARKET_TZ))
    st = {"positions": {"SPY|2026-10-30|752.0|C": _row()}}
    assert sync(st, b) == {} and b.asked == []
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 15, 59, tzinfo=agent.MARKET_TZ))
    sync(st, b)
    assert b.asked == [[U1]]


def test_legacy_row_stamped_with_current_acct_on_conclusive_read():
    st = {"positions": {KEY: {"option_id": U1, "qty": 2}}}
    assert _status(sync(st, FillsBroker({U1: 2}))) == "ok"
    pos = st["positions"][KEY]
    assert pos["acct"] == TAG and pos["held_seen"] is True
    assert TAG in st["position_sync"]["proven"]
    st = {"positions": {KEY: {"option_id": U1, "qty": 2}}}
    sync(st, FillsBroker(None))
    assert "acct" not in st["positions"][KEY]  # unknown read stamps nothing


def test_no_hook_or_disabled_is_noop():
    st = _state()
    assert agent._sync_positions(CFG, st, lambda s: None, StubBroker(), object()) == {}
    b = FillsBroker({U1: 0})
    assert agent._sync_positions({**CFG, "position_sync": False}, st,
                                 lambda s: None, b, object()) == {}
    assert agent._sync_positions(CFG, st, lambda s: None, b, None) == {}
    assert b.asked == []


def test_untrusted_without_fills_hook_never_drops_then_trust_enables(home, clock):
    st = _state(trusted=False)
    b = HoldBroker({U1: 0})
    for _ in range(4):
        assert _status(sync(st, b)) == "unproven"
        clock.tick(60)
    assert "miss" not in st["positions"][KEY] and st["positions"][KEY]["qty"] == 2
    assert _log(home) == []
    # another agent row is seen held in this account: the read is proven
    st["positions"][KEY2] = _row(option_id=U2)
    b = HoldBroker({U1: 0, U2: 2})
    assert _status(sync(st, b)) == "miss"
    assert TAG in st["position_sync"]["proven"]
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    (row,) = _log(home)
    assert row["cause"] == "gone"


def test_untrusted_with_fills(home, clock):
    st = _state(trusted=False)
    b = FillsBroker({U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"   # the sale itself is the proof
    assert _log(home)[0]["cause"] == "sold"

    st = _state(trusted=False)
    b = FillsBroker({U1: 0}, fills=[])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "unproven"
    assert st["positions"][KEY]["miss"]["noted"] is True
    assert len(b.fill_calls) == 1
    clock.tick(60)
    assert _status(sync(st, b)) == "unproven"
    assert len(b.fill_calls) == 1               # not asked again within 15 min
    clock.tick(900)
    sync(st, b)
    assert len(b.fill_calls) == 2
    assert st["positions"][KEY]["qty"] == 2


def test_mass_guard(home, clock):
    st = _state()
    st["positions"][KEY2] = _row(option_id=U2)
    b = HoldBroker({U1: 0, U2: 0})
    sync(st, b)
    clock.tick(60)
    r = sync(st, b)
    assert {k: v["status"] for k, v in r.items()} == {KEY: "miss", KEY2: "miss"}
    clock.tick(60)
    r = sync(st, b)
    assert {k: v["status"] for k, v in r.items()} == {KEY: "closed", KEY2: "closed"}

    # with sales data each row needs its own sale, even after the other closes
    st = _state()
    st["positions"][KEY2] = _row(option_id=U2)
    b = FillsBroker({U1: 0, U2: 0}, fills={U1: [_fill()], U2: []})
    for _ in range(3):
        r = sync(st, b)
        clock.tick(60)
    assert {k: v["status"] for k, v in r.items()} == {KEY: "closed", KEY2: "unproven"}
    for _ in range(5):
        r = sync(st, b)
        assert KEY not in r and r[KEY2]["status"] == "unproven"
        clock.tick(60)
    assert st["positions"][KEY2]["qty"] == 2


def test_absence_only_trusted_is_gone_unmeasured(home, clock, notes):
    st = _state()
    b = HoldBroker({U1: 0})
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    (row,) = _log(home)
    assert row["cause"] == "gone" and "est_pnl_usd" not in row
    assert "exit_price" not in row and "closed_at" not in row
    assert row["reason"] == ("Not in your Robinhood account anymore and no sale "
                             "found — no longer tracked")
    assert len(notes) == 1 and "no sale was found" in notes[0]
    assert agent.pnl_summary({"source": "url"}, {})["live"]["unmeasured"] == 1
    assert rules.loss_streak(agent._trades_today()) == 0


def test_gone_needs_three_reads_when_broker_reports_sales(home, clock, notes):
    """A broker that can list the owner's sales but shows none for this
    shortfall: one extra read before trusting the absence."""
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "miss"
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == [] and notes == []
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    assert _log(home)[0]["cause"] == "gone"
    assert len(b.fill_calls) == 2   # asked again on the third read


def test_sale_found_on_third_read_is_sold(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    b.fills = [_fill()]
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    assert _log(home)[0]["cause"] == "sold" and _log(home)[0]["est_pnl_usd"] == 60.0


def test_sale_before_the_agent_opened_does_not_count(home, clock):
    st = _state(trusted=False)
    b = FillsBroker({U1: 0}, fills=[_fill(at="2026-10-29T15:00:00Z")])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "unproven"
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == []


def test_moomoo_never_held_seen_zero_is_entry_unfilled_not_exit(home, clock, notes):
    st = _state()
    b = UnverifiedBroker({U1: 0})
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    assert KEY not in st["positions"] and KEY in st["closed_outside"]
    (row,) = _log(home)
    assert row["action"] == "entry_unfilled" and row["contract"] == CONTRACT
    assert "never showed up" in row["reason"]
    assert len(notes) == 1
    # once the account has shown it held, a later absence is a real close
    st = _state(held_seen=True)
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    assert _log(home)[-1]["action"] == "exit"


def test_exit_logged_prevents_double_count(home, clock, notes):
    st = _state(exit_logged=2)
    b = FillsBroker({U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "closed"
    assert _log(home) == []
    assert len(notes) == 1 and "already counted" in notes[0]

    st = _state(exit_logged=1)
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    (row,) = _log(home)
    assert row["qty"] == 1 and row["est_pnl_usd"] == 30.0

    st = _state(qty=3, exit_logged=3)  # 1 still held, 2 already counted
    b = FillsBroker({U1: 1}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    pos = st["positions"][KEY]
    assert pos["qty"] == 1 and pos["exit_logged"] == 1
    assert len(_log(home)) == 1


def test_used_fill_ids_not_reused(home, clock):
    st = _state(fill_ids_used=["u1"])
    b = FillsBroker({U1: 0}, fills=[_fill()])
    for _ in range(3):
        r = sync(st, b)
        clock.tick(60)
    assert _status(r) == "closed"
    (row,) = _log(home)
    assert row["cause"] == "gone" and "est_pnl_usd" not in row and "exit_price" not in row


def test_utc_iso_parses_broker_timestamps():
    assert agent._utc_iso("2026-10-08T17:27:05.234000Z") == "2026-10-08T17:27:05+00:00"
    assert agent._utc_iso("2026-10-08T17:27:05.123456789Z") == "2026-10-08T17:27:05+00:00"
    assert agent._utc_iso("2026-10-08T17:27:05.2Z") == "2026-10-08T17:27:05+00:00"
    assert agent._utc_iso("2026-10-08T13:27:05-04:00") == "2026-10-08T17:27:05+00:00"
    assert agent._utc_iso("2026-10-08T17:27:05") == "2026-10-08T17:27:05+00:00"
    assert agent._utc_iso("junk") is None and agent._utc_iso(None) is None
    assert agent._utc_iso("") is None


# ── periodic wiring ─────────────────────────────────────────────────────────

def test_upkeep_throttle(monkeypatch, clock):
    st = _state()
    b = FillsBroker({U1: 2})
    up = agent._position_upkeep
    up(CFG, st, lambda s: None, b, object(), now=T0)
    assert len(b.asked) == 1 and agent._POS_SYNC["next_at"] == T0 + 120
    up(CFG, st, lambda s: None, b, object(), now=T0 + 119)
    assert len(b.asked) == 1
    for setting, every in ((10, 60), (99999, 3600), ("garbage", 120)):
        agent._POS_SYNC["next_at"] = 0.0
        up({**CFG, "position_sync_seconds": setting}, st, lambda s: None, b,
           object(), now=T0)
        assert agent._POS_SYNC["next_at"] == T0 + every
    monkeypatch.setattr(agent, "_market_open_now", lambda: False)
    agent._POS_SYNC["next_at"] = 0.0
    up(CFG, st, lambda s: None, b, object(), now=T0)
    assert agent._POS_SYNC["next_at"] == T0 + 1800
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: True)
    agent._POS_SYNC["next_at"] = 0.0
    n = len(b.asked)
    up(CFG, st, lambda s: None, b, object(), now=T0)
    assert len(b.asked) == n and agent._POS_SYNC["next_at"] == 0.0
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    b.held = [{U1: 0}]
    up(CFG, st, lambda s: None, b, object(), now=T0)
    assert agent._POS_SYNC["next_at"] == T0 + 60   # confirm a shortfall sooner
    up(CFG, st, lambda s: None, b, None, now=T0 + 600)
    up({**CFG, "position_sync": False}, st, lambda s: None, b, object(), now=T0 + 600)
    up(CFG, st, lambda s: None, StubBroker(), object(), now=T0 + 600)
    assert len(b.asked) == n + 1


def test_open_rush_window(monkeypatch):
    tz = agent.MARKET_TZ
    for (h, m), want in (((9, 27), False), ((9, 28), True), ((9, 39), True),
                         ((9, 40), False)):
        monkeypatch.setattr(agent, "_et_now",
                            lambda h=h, m=m: datetime(2026, 10, 30, h, m, tzinfo=tz))
        assert _REAL_IN_OPEN_RUSH() is want
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 31, 9, 30, tzinfo=tz))  # Saturday
    assert _REAL_IN_OPEN_RUSH() is False


def test_upkeep_swallows_errors(capsys):
    st = _state()

    def boom(s):
        raise OSError("disk full")
    agent._position_upkeep(CFG, st, boom, FillsBroker({U1: 0}), object(), now=T0)
    assert "position check error (non-fatal): disk full" in capsys.readouterr().out


def test_disconnect_event_reported_once_with_blank_contract_periodic(home):
    def lost():
        broker_events.report("broker_disconnected", "Robinhood connection lost")
        return None
    st = _state()
    sync(st, HoldBroker(lost))
    (row,) = _log(home)
    assert row["action"] == "broker_disconnected" and row["contract"] == ""
    assert "event_id" not in row


def test_disconnect_event_reported_with_contract_and_event_id_at_gate(home, notes):
    def lost():
        broker_events.report("broker_disconnected", "Robinhood connection lost")
        return None
    # a fresh trusted shortfall + unknown reads: the exit waits, twice
    st = _state(miss={"held": 0, "reads": 1, "need": 2, "first_at": T0,
                      "last_at": T0})
    b = HoldBroker(lost)
    for _ in range(2):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
    rows = [r for r in _log(home) if r["action"] == "broker_disconnected"]
    assert len(rows) == 1 and len(b.asked) == 2
    assert rows[0]["contract"] == CONTRACT and rows[0]["event_id"] == "x1"
    assert b.executed == []


def test_manual_close_never_uses_problem_rows_or_trade_not_placed_alerts(home, clock, notes):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    assert all(r["action"] not in broker_events.KINDS for r in _log(home))
    assert agent._problems_today() == []
    assert notes and not any("NOT placed" in n for n in notes)


# ── the check right before an exit ──────────────────────────────────────────

def test_gate_held_equals_ledger_sells_ledger_qty(home):
    st = _state()
    b = FillsBroker({U1: 2})
    assert handle(st, b).startswith("EXIT")
    assert b.executed == [("EXITED", 2)]
    (row,) = _log(home)
    assert row["action"] == "exit" and row["qty"] == 2 and "manual" not in row


def test_gate_user_added_contracts_sells_only_agent_qty():
    st = _state(qty=3)
    b = FillsBroker({U1: 5})
    handle(st, b)
    assert b.executed == [("EXITED", 3)]


def test_gate_zero_two_polls_closes_without_order(home, clock, notes):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    assert b.executed == [] and _log(home) == [] and notes == []
    assert st["positions"][KEY]["miss"]["reads"] == 1
    clock.tick(21)
    assert handle(st, b) is None
    assert KEY not in st["positions"] and b.executed == []
    (row,) = _log(home)
    assert row["manual"] is True and row["event_id"] == "x1"
    assert row["detected_by"] == "exit_check" and row["cause"] == "sold"
    assert len(notes) == 1


def test_gate_gone_needs_three_polls_when_broker_reports_sales(home, clock, notes):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    for _ in range(2):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        clock.tick(21)
    assert b.executed == [] and _log(home) == [] and notes == []
    assert handle(st, b) is None
    assert KEY not in st["positions"] and b.executed == []
    (row,) = _log(home)
    assert row["cause"] == "gone" and row["event_id"] == "x1"


def test_gate_reduced_below_new_qty_waits(home, clock):
    st = _state()
    b = FillsBroker({U1: 1}, {U1: 0}, fills=[_fill("u1", 1, at="2026-10-30T15:01:00Z"),
                                             _fill("u2", 1, at="2026-10-30T15:02:00Z")])
    sync(st, b)
    clock.tick(20)
    with pytest.raises(agent.ExitNotDone):  # 1 of 2 sold, and now 0 held
        handle(st, b)
    assert st["positions"][KEY]["qty"] == 1 and b.executed == []
    clock.tick(21)
    assert handle(st, b) is None
    assert KEY not in st["positions"] and b.executed == []
    assert [r["qty"] for r in _log(home)] == [1, 1]


def test_gate_uses_miss_from_periodic_sync(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    sync(st, b)
    clock.tick(20)
    assert handle(st, b) is None
    assert KEY not in st["positions"] and b.executed == []
    assert _log(home)[0]["detected_by"] == "exit_check"


def test_gate_partial_then_sells_remainder(home, clock):
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps({"action": "entry", "contract": CONTRACT, "dry": False,
                            "ts": "2026-10-28T15:00:00+00:00"}) + "\n")
    st = _state(qty=3)
    b = FillsBroker({U1: 1}, fills=[_fill(qty=2)], mark=2.0)
    sync(st, b)
    clock.tick(20)
    assert handle(st, b).startswith("EXIT")
    assert b.executed == [("EXITED", 1)]
    entry, partial, final = _log(home)
    assert partial["manual"] and partial["qty"] == 2 and partial["partial"] is True
    assert final["qty"] == 1 and final["est_pnl_usd"] == 50.0  # (2.00-1.50) x 1
    s = agent.pnl_summary({"source": "url"}, {})
    assert [(d["day"], d["exits"]) for d in s["live"]["days"]] == [("2026-10-28", 2)]


def test_gate_unknown_read_goes_today_path():
    st = _state()
    b = FillsBroker(None)
    handle(st, b)
    assert b.executed == [("EXITED", 2)] and len(b.asked) == 1


def test_gate_unknown_read_with_fresh_trusted_miss_waits_and_stale_miss_goes(clock):
    miss = {"held": 0, "reads": 1, "need": 2, "first_at": T0 - 600,
            "last_at": T0 - 600}
    st = _state(miss=dict(miss))
    b = FillsBroker(None)
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    assert b.executed == []
    st = _state(miss=dict(miss, last_at=T0 - 601))
    handle(st, b)
    assert b.executed == [("EXITED", 2)]
    st = _state(trusted=False, miss=dict(miss))   # untrusted: never waits
    b = FillsBroker(None)
    handle(st, b)
    assert b.executed == [("EXITED", 2)]


def test_gate_untrusted_no_fills_goes():
    st = _state(trusted=False)
    b = HoldBroker({U1: 0})
    handle(st, b)
    assert b.executed == [("EXITED", 2)]


def test_gate_inside_grace_and_expiry_close_go_without_read(monkeypatch):
    st = _state(last_order_at=T0 - 100)
    b = FillsBroker({U1: 0})
    handle(st, b)
    assert b.executed == [("EXITED", 2)] and b.asked == []
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 16, 1, tzinfo=agent.MARKET_TZ))
    st = {"positions": {"SPY|2026-10-30|752.0|C": _row()}}
    b = FillsBroker({U1: 0})
    handle(st, b, ev=make_event(event="EXITED", expiry="2026-10-30"))
    assert b.executed == [("EXITED", 2)] and b.asked == []


def test_gate_last_error_goes_without_read():
    st = _state()
    b = FillsBroker({U1: 0})
    handle(st, b, client=Down())
    assert b.executed == [("EXITED", 2)] and b.asked == []


def test_gate_account_mismatch_blocks(home, notes):
    st = _state(acct=account_tag({"broker": "robinhood", "robinhood_account": "OTHER"}))
    b = FillsBroker({U1: 2})
    assert handle(st, b) is None
    assert b.executed == [] and b.asked == []
    (row,) = _log(home)
    assert row["action"] == "exit_failed" and row["contract"] == CONTRACT
    assert row["event_id"] == "x1" and "different broker account" in row["reason"]
    assert len(notes) == 1
    assert handle(st, b) is None
    assert len(_log(home)) == 1 and KEY in st["positions"]


def test_exit_while_agent_sell_pending_is_noop(home):
    st = _state(pending={"order_id": "o-9", "side": "sell", "requested": 2})
    b = FillsBroker({U1: 0})
    assert handle(st, b) is None
    assert b.executed == [] and b.asked == [] and _log(home) == []


def test_dry_paths_untouched(home):
    st = _state()
    b = FillsBroker({U1: 0})
    out = handle(st, b, cfg={**CFG, "dry_run": True})
    assert out.startswith("[DRY-RUN] EXIT") and b.asked == [] and b.executed == []
    st = {"positions": {KEY: {"option_id": "dry", "qty": 1, "dry": True}}}
    b = FillsBroker({U1: 0})
    assert handle(st, b).startswith("[DRY-RUN]")
    assert b.asked == [] and b.executed == []


def test_gate_exception_falls_back_to_go(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad state")
    monkeypatch.setattr(agent, "_sync_positions", boom)
    st = _state()
    b = FillsBroker({U1: 0})
    handle(st, b)
    assert b.executed == [("EXITED", 2)]


# ── exits the broker accepted but didn't sell ───────────────────────────────

class ScriptedExit:
    """execute() for EXITED does what `fn(pos)` says; no holdings hook."""

    def __init__(self, fn, mark=0.0):
        self.fn, self.mark, self.calls = fn, mark, 0

    def execute(self, client, cfg, event, state):
        self.calls += 1
        return self.fn(state["positions"].get(KEY), state)

    def mark_price(self, client, cfg, event, side="buy"):
        return self.mark


def test_accepted_but_unsold_exit_retries(home, notes):
    def dead(pos, state):  # Robinhood async reject / Alpaca 0 fill
        broker_events.report("exit_partial", f"exit filled 0/{pos['qty']}")
        return True
    st = _state()
    b = ScriptedExit(dead)
    for _ in range(3):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
    assert b.calls == 3 and st["positions"][KEY]["qty"] == 2
    assert [r["action"] for r in _log(home)] == ["exit_partial"]


def test_pending_sell_exit_logs_requested_and_marks_exit_logged(home):
    def working(pos, state):
        pos["pending"] = {"order_id": "o-9", "side": "sell", "requested": 2}
        return True
    st = _state()
    assert handle(st, ScriptedExit(working, mark=2.0)).startswith("EXIT")
    (row,) = _log(home)
    assert row["action"] == "exit" and row["qty"] == 2 and row["est_pnl_usd"] == 100.0
    assert st["positions"][KEY]["exit_logged"] == 2


def test_partial_fill_exit_row_qty_is_sold(home):
    def partial(pos, state):
        pos["qty"] = 1
        broker_events.report("exit_partial", "exit filled 2/3")
        return True
    st = _state(qty=3)
    handle(st, ScriptedExit(partial, mark=2.0))
    row = [r for r in _log(home) if r["action"] == "exit"][0]
    assert row["qty"] == 2 and row["est_pnl_usd"] == 100.0


def test_exit_with_prior_exit_logged_has_no_estimate(home):
    def sold(pos, state):
        state["positions"].pop(KEY)
        return True
    st = _state(exit_logged=2)
    handle(st, ScriptedExit(sold, mark=2.0))
    (row,) = _log(home)
    assert row["qty"] == 2 and "est_pnl_usd" not in row


# ── after a close: tombstones, re-entry ─────────────────────────────────────

def _closed_state():
    return {"positions": {}, "seen": [],
            "position_sync": {"proven": {TAG: "2026-10-29T15:00:00+00:00"}},
            "closed_outside": {KEY: {"option_id": U1, "opened_event": "open-1",
                                     "acct": TAG, "qty": 2,
                                     "at": "2026-10-30T15:00:00+00:00"}}}


def test_exit_after_close_is_silent_noop(home, notes):
    st = _closed_state()
    b = FillsBroker({U1: 0})
    assert handle(st, b) is None
    assert b.executed == [] and _log(home) == [] and notes == []


def test_reheld_warning_once(home, notes):
    st = _closed_state()
    b = FillsBroker({U1: 2})
    assert handle(st, b) is None
    assert len(notes) == 1 and "holds 2 now" in notes[0] and "did NOT sell" in notes[0]
    assert st["closed_outside"][KEY]["warned"] is True
    assert handle(st, b, ev=_exit("x2")) is None
    assert len(notes) == 1 and len(b.asked) == 1 and b.executed == []
    for answer in ({U1: 0}, None):
        st = _closed_state()
        assert handle(st, FillsBroker(answer)) is None
        assert "warned" not in st["closed_outside"][KEY]
    assert len(notes) == 1 and _log(home) == []


def test_replayed_opening_entry_ignored(home, no_policy):
    st = _closed_state()
    b = FillsBroker({U1: 2})
    ev = make_event(expiry="2099-12-18", event_id="open-1")
    assert agent.handle_event(ev, CFG, st, b, object(), lambda s: None) is None
    assert st["positions"] == {} and _log(home) == []


def test_new_entry_after_close_trades_and_frees_slot(home, no_policy):
    st = _closed_state()
    b = FillsBroker({U1: 2})
    ev = make_event(expiry="2099-12-18", event_id="open-2")
    out = agent.handle_event(ev, {**CFG, "max_open_positions": 1}, st, b,
                             object(), lambda s: None)
    assert out.startswith("ENTRY")
    pos = st["positions"][KEY]
    assert pos["opened_at"] == T0 and pos["last_order_at"] == T0 and pos["acct"] == TAG
    assert KEY not in st["closed_outside"]


def test_prune_drops_expired_tombstones_and_caps_100():
    tombs = {f"T{i}|2099-12-18|1.0|C":
             {"at": f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00"}
             for i in range(105)}
    tombs["OLD|2020-01-17|1.0|C"] = {"at": "2026-10-30T00:00:00+00:00"}
    st = {"positions": {}, "closed_outside": tombs}
    saved = []
    agent._prune_expired_positions({}, st, lambda s: saved.append(1))
    assert len(st["closed_outside"]) == 100 and saved
    assert "OLD|2020-01-17|1.0|C" not in st["closed_outside"]  # expired
    assert not any(f"T{i}|2099-12-18|1.0|C" in st["closed_outside"] for i in range(5))
    assert "T5|2099-12-18|1.0|C" in st["closed_outside"]        # oldest 5 dropped
    st = {"positions": {}, "closed_outside": {KEY: {"at": "x"}}}
    saved.clear()
    agent._prune_expired_positions({}, st, lambda s: saved.append(1))
    assert not saved and KEY in st["closed_outside"]


# ── python agent.py check-positions (read-only) ─────────────────────────────

def test_check_positions_cli_never_writes_state(home, monkeypatch, capsys):
    import types
    st = _state(qty=2, miss={"held": 1, "reads": 1, "need": 2, "first_at": T0,
                             "last_at": T0})
    st["positions"]["QQQ|2099-12-18|500.0|P"] = _row(option_id=U2, acct="alpaca:paper")
    st["positions"]["DRY|2099-12-18|1.0|C"] = {"option_id": "dry", "qty": 1, "dry": True}
    agent._save(agent.STATE_PATH, st)
    agent._save(agent.CONFIG_PATH, CFG)
    before = (home / "state.json").read_bytes()
    asked = []
    fake = types.SimpleNamespace(
        client=lambda cfg: object(),
        held_option_qty=lambda client, cfg, ids: asked.append(ids) or {U1: 1})
    monkeypatch.setitem(agent.BROKERS, "robinhood", fake)
    monkeypatch.setattr(agent, "_save", lambda *a, **k: pytest.fail("must not write"))
    monkeypatch.setattr(sys, "argv", ["agent.py", "check-positions"])
    agent.main()
    out = capsys.readouterr().out
    assert asked == [[U1, U2]]                       # live rows only
    assert "SPY 2099-12-18 752.0 C: agent tracks 2, broker shows 1, same account: yes" in out
    assert "showed fewer on 1 check(s)" in out
    assert "QQQ 2099-12-18 500.0 P: agent tracks 2, broker shows unknown" in out
    assert "NO — opened in another account" in out and "DRY" not in out
    assert "holdings check   : proven on this account\n" in out
    assert (home / "state.json").read_bytes() == before
    # an unclear answer says so, and still writes nothing
    fake.held_option_qty = lambda client, cfg, ids: None
    agent.cmd_check_positions()
    assert "broker answer    : unknown" in capsys.readouterr().out
    assert (home / "state.json").read_bytes() == before


def test_check_positions_is_in_the_help_text():
    assert "check-positions" in agent.__doc__
