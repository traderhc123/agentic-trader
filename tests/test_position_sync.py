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
    (can't read). Like the real adapters it leaves out own_order_ids and,
    with exclude_agentic, orders placed through the agentic API."""

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
        if fills is None:
            return None
        return [dict(f) for f in fills if f["order_id"] not in own_order_ids
                and not (exclude_agentic and f.get("by") == "agentic")]


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
    # sales since the open, with the same 10 min clock slack _pick_fills uses
    assert b.fill_calls == [{"option_id": U1, "since_iso": "2026-10-30T13:50:00+00:00",
                             "own": ["o-agent"], "exclude_agentic": False}]
    assert st["position_sync"]["fills_used"] == ["u1"]


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


def test_unprovable_shortfall_is_rechecked_slowly(home, clock):
    """Only a sale could prove a shortfall every row showed at once. Once
    that's noted, sales are asked for at most every 15 min (trusted or not)
    and the periodic check goes back to its normal pace."""
    st = _state()
    st["positions"][KEY2] = _row(option_id=U2)
    b = FillsBroker({U1: 0, U2: 0}, fills=[])

    def upkeep():
        agent._position_upkeep(CFG, st, lambda s: None, b, object())
        return agent._POS_SYNC["next_at"] - clock.t
    assert upkeep() == 60               # confirming: 60 s
    clock.tick(60)
    assert upkeep() == 60
    clock.tick(60)
    assert upkeep() == 120              # 3rd read: unprovable, noted
    assert all(p["miss"]["noted"] for p in st["positions"].values())
    assert len(b.fill_calls) == 2
    for _ in range(6):                  # 12 more minutes
        clock.tick(120)
        upkeep()
    assert len(b.fill_calls) == 2 and len(b.asked) == 9
    clock.tick(240)                     # 15 min after the last ask
    upkeep()
    assert len(b.fill_calls) == 4
    assert all(p["qty"] == 2 for p in st["positions"].values())


def test_agents_earlier_orders_and_used_sales_never_prove_a_shortfall(home, clock):
    """Re-entry in a contract sold minutes earlier (inside the open-time
    slack): the agent's own earlier sell, and an owner's sale an earlier
    close already used, must not explain the new position's shortfall."""
    st = _state(trusted=False)
    st["agent_order_ids"] = ["prev-agent-sell"]
    st["position_sync"] = {"fills_used": ["prev-owner-sell"]}
    early = agent._iso_at(T0 - 3600 - 300)      # 5 min before this entry
    b = FillsBroker({U1: 0}, fills=[_fill("prev-agent-sell", at=early),
                                    _fill("prev-owner-sell", at=early)])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "unproven"
    assert b.fill_calls[0]["own"] == ["o-agent", "prev-agent-sell"]
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == []


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


@pytest.mark.parametrize("cfg, oid", [
    ({"broker": "moomoo", "moomoo_paper": True}, "US.SPY991218C752000"),
    ({**CFG, "robinhood_account": "B2"}, U1),
], ids=["moomoo", "other-rh-account"])
def test_trust_is_per_account(home, clock, cfg, oid):
    """Proof that reads work in one account says nothing about another: a
    wrong id format there reads as an explicit 0 for every row."""
    st = {"positions": {KEY: _row(option_id=oid, acct=account_tag(cfg))},
          "seen": [], "position_sync": {"proven": {TAG: "2026-10-29T15:00:00+00:00"}}}
    b = UnverifiedBroker({oid: 0})
    for _ in range(4):
        r = agent._sync_positions(cfg, st, lambda s: None, b, object())
        assert r[KEY]["status"] == "unproven"
        clock.tick(60)
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == []


def test_legacy_row_excludes_agent_sells(home, clock):
    """A row from before the agent kept its order ids: nothing but
    exclude_agentic keeps the agent's own earlier sells out."""
    st = {"positions": {KEY: {"option_id": U1, "qty": 2, "opened_event": "open-1",
                              "entry_mark": 1.5}}, "seen": []}     # untrusted
    old = dict(_fill("agent-old", 2, 2.5, "2026-10-20T15:00:00Z"), by="agentic")
    b = FillsBroker({U1: 0}, fills=[old])
    sync(st, b)
    clock.tick(60)
    assert _status(sync(st, b)) == "unproven"
    assert b.fill_calls[0]["exclude_agentic"] is True
    assert b.fill_calls[0]["since_iso"] is None
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == []


def test_mixed_priced_and_unpriced_sales_have_no_price(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill("u1", 1, 1.8, "2026-10-30T15:01:00Z"),
                                    _fill("u2", 1, None, "2026-10-30T15:02:00Z")])
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    (row,) = _log(home)
    assert row["cause"] == "sold" and row["qty"] == 2
    assert "est_pnl_usd" not in row and "exit_price" not in row
    assert agent._pick_fills(b.fills, 2, [])[:2] == (2, None)


def test_mass_flag_sticks_when_a_later_read_is_all_zero(home, clock):
    st = _state()
    st["positions"][KEY2] = _row(option_id=U2)
    b = FillsBroker({U1: 0, U2: 2}, {U1: 0, U2: 0}, fills=[])
    for _ in range(3):
        r = sync(st, b)
        clock.tick(60)
    assert r[KEY]["status"] == "unproven" and st["positions"][KEY]["qty"] == 2
    assert st["positions"][KEY]["miss"]["mass"] is True


def test_sales_count_from_ten_minutes_before_the_open():
    def fill(offset):
        return {"order_id": "a", "qty": 1, "price": 1.0, "at": agent._iso_at(T0 + offset)}
    assert agent._pick_fills([fill(-599)], 1, [], T0)[0] == 1
    assert agent._pick_fills([fill(-601)], 1, [], T0)[0] == 0
    assert agent._pick_fills([fill(60)], 1, [], T0)[0] == 1


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
    assert len(notes) == 1 and notes[0].startswith(
        f"agentic-trader: trade NOT placed ({CONTRACT}) — the order was placed "
        "but never filled: ")
    # once the account has shown it held, a later absence is a real close
    st = _state(held_seen=True)
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    assert _log(home)[-1]["action"] == "exit"


def test_moomoo_partly_filled_entry_shrinks_without_an_exit(home, clock, notes):
    """moomoo books the requested qty when the limit buy is accepted. If the
    account never shows all of it, the rest never filled: not a manual
    close, no P&L row, no 'exercised or assigned?' alert."""
    st = _state(qty=3)
    b = UnverifiedBroker({U1: 1})
    sync(st, b)
    clock.tick(60)
    assert sync(st, b) == {KEY: {"held": 1, "status": "reduced"}}
    pos = st["positions"][KEY]
    assert pos["qty"] == 1 and pos["full_seen"] is True and "miss" not in pos
    (row,) = _log(home)
    assert row["action"] == "entry_unfilled"
    assert row["reason"].startswith(f"only 1 of 3 {CONTRACT} showed up")
    assert len(notes) == 1 and "exercised" not in notes[0]
    # one contract did fill: "check this trade", not "trade NOT placed"
    assert notes[0].startswith(f"agentic-trader: check this trade ({CONTRACT}) — "
                               f"only 1 of 3 {CONTRACT} showed up")
    # the contract it did buy is a real position: a later absence is a close
    b.held = [{U1: 0}]
    for _ in range(2):
        clock.tick(60)
        sync(st, b)
    assert KEY not in st["positions"]
    assert _log(home)[-1]["action"] == "exit" and _log(home)[-1]["qty"] == 1


def test_moomoo_owner_sells_part_after_full_entry_seen_is_an_exit(home, clock, notes):
    """Once a read showed the whole entry, a later shortfall on moomoo is
    the owner's (partial) close, not a partly filled buy."""
    st = _state(qty=3)
    b = UnverifiedBroker({U1: 3}, {U1: 1})
    sync(st, b)
    assert st["positions"][KEY]["full_seen"] is True
    for _ in range(2):
        clock.tick(60)
        sync(st, b)
    assert st["positions"][KEY]["qty"] == 1
    assert [(r["action"], r["qty"], r["manual"]) for r in _log(home)] == [
        ("exit", 2, True)]
    assert len(notes) == 1 and "NOT placed" not in notes[0]


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


def _run_one_loop(home, monkeypatch, paused):
    """One pass of `agent.py run` with stub broker/source; returns the order
    of the calls that matter here."""
    import brokers
    import keep_running
    import webui
    calls = []

    class Broker:
        @staticmethod
        def client(cfg):
            return object()

        @staticmethod
        def reconcile(client, cfg, state, save_state):
            calls.append("reconcile")

        @staticmethod
        def held_option_qty(client, cfg, ids):
            return {}

    class Source:
        @staticmethod
        def poll(cfg, state, save_state):
            calls.append("poll")
            return []

    def no_dashboard(*a, **k):
        raise OSError("no dashboard in tests")

    def stop(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "require_consent_or_exit", lambda: None)
    monkeypatch.setattr(brokers, "broker_ready", lambda c: True)
    monkeypatch.setitem(agent.BROKERS, "stub", Broker)
    monkeypatch.setitem(agent.SOURCES, "stub", Source)
    monkeypatch.setattr(webui, "start_dashboard", no_dashboard)
    monkeypatch.setattr(keep_running, "refresh_autostart", lambda: False)
    monkeypatch.setattr(agent, "_refresh_cash", lambda *a, **k: calls.append("cash"))
    monkeypatch.setattr(agent, "_position_upkeep",
                        lambda *a, **k: calls.append("upkeep"))
    monkeypatch.setitem(webui.CONTROLS, "paused", paused)
    monkeypatch.setitem(webui.CONTROLS, "stop", False)
    monkeypatch.setattr(agent, "_sleep_unless_stopped", stop)
    (home / "config.json").write_text(json.dumps({"source": "stub", "broker": "stub"}))
    agent.cmd_run()
    return calls


def test_run_loop_checks_holdings_after_reconcile_before_events(home, monkeypatch):
    calls = _run_one_loop(home, monkeypatch, paused=False)
    assert calls.count("upkeep") == 1
    assert calls.index("reconcile") < calls.index("upkeep") < calls.index("poll")
    assert calls.index("upkeep") < len(calls) - 1 - calls[::-1].index("cash")


def test_run_loop_paused_skips_holdings_check(home, monkeypatch):
    calls = _run_one_loop(home, monkeypatch, paused=True)
    assert "upkeep" not in calls and "reconcile" not in calls


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


class Flaky:
    """A client whose login drops during a read, like RobinhoodMCP (sets
    last_error, then reports broker_disconnected once)."""
    last_error = None

    def lose(self):
        self.last_error = "can't reach Robinhood (ReadTimeout)"
        broker_events.report("broker_disconnected", "Robinhood connection lost")


def test_disconnect_during_a_read_is_the_broker_down_alert(home, notes):
    """No trade was attempted: a dropped login during a holdings read is the
    once-a-day 'connection is DOWN' alert, not a 'trade NOT placed ()' row."""
    client = Flaky()
    st = _state()
    sync(st, HoldBroker(lambda: client.lose()), client=client)
    assert _log(home) == [] and agent._problems_today() == []
    assert len(notes) == 1 and "broker connection is DOWN" in notes[0]
    client.last_error = None
    sync(st, HoldBroker(lambda: client.lose()), client=client)
    assert len(notes) == 1   # once a day


def test_gate_read_failure_then_exit_goes_through_no_trade_problem(home, notes):
    """The pre-exit read fails, the sell itself gets through: no 'trade NOT
    placed' alert or problem row for an exit that happened."""
    class Recovers(HoldBroker):
        def execute(self, client, cfg, event, state):
            client.last_error = None  # the order call reconnected
            return super().execute(client, cfg, event, state)
    client = Flaky()
    st = _state()
    b = Recovers(lambda: client.lose())
    assert handle(st, b, client=client).startswith("EXIT")
    assert b.executed == [("EXITED", 2)] and len(b.asked) == 1
    assert [r["action"] for r in _log(home)] == ["exit"]
    assert not any("NOT placed" in n for n in notes)


def test_gate_waits_on_fresh_shortfall_while_reads_fail(home, notes):
    # a fresh trusted shortfall + unknown reads: the exit waits, twice
    st = _state(miss={"held": 0, "reads": 1, "need": 2, "first_at": T0,
                      "last_at": T0})
    b = HoldBroker(lambda: broker_events.report("broker_disconnected", "lost"))
    for _ in range(2):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
    assert len(b.asked) == 2 and b.executed == []
    assert not [r for r in _log(home) if r["action"] == "broker_disconnected"]


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


def test_gate_gone_takes_as_long_as_the_periodic_check(home, clock, notes):
    """With no sale on record, the exit check (reads 20 s apart) drops a row
    only after 3 reads spanning 2 minutes, like the periodic check — and
    keeps the signal one more poll for the re-held check."""
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    for _ in range(6):                  # 0 … 100 s
        with pytest.raises(agent.ExitNotDone, match="re-checking"):
            handle(st, b)
        clock.tick(20)
    assert KEY in st["positions"] and _log(home) == [] and notes == []
    with pytest.raises(agent.ExitNotDone, match="no sale was found"):
        handle(st, b)                   # 120 s: dropped, no order
    assert KEY not in st["positions"] and b.executed == []
    (row,) = _log(home)
    assert row["cause"] == "gone" and row["event_id"] == "x1"
    assert st["closed_outside"][KEY]["cause"] == "gone"
    clock.tick(20)
    assert handle(st, b) is None        # still not held: the signal is done
    assert b.executed == [] and len(notes) == 1 and len(b.asked) == 8


def test_gate_gone_then_contracts_back_warns_on_next_poll(home, clock, notes):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    while KEY in st["positions"]:
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        clock.tick(20)
    b.held = [{U1: 2}]                  # the listing glitch is over
    assert handle(st, b) is None
    assert b.executed == [] and "holds 2 now" in notes[-1]


@pytest.mark.parametrize("when", ["same_poll", "next_poll"])
def test_kept_exit_signal_never_sells_a_new_position(home, clock, notes, no_policy,
                                                     when):
    """'gone' keeps the exit signal one more poll. A new entry in the same
    contract handled before that retry (later in the same batch, or first
    in the next poll) belongs to a new signal: the old exit must not sell
    it, while the new position's own exit works as usual."""
    st = _state()
    b = FillsBroker({U1: 0}, fills=[])
    while KEY in st["positions"]:
        clock.tick(20)
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
    assert st["closed_outside"][KEY]["held_exits"] == ["x1"]
    b.held = [{U1: 2}]                  # the new entry fills
    if when == "next_poll":
        clock.tick(30)
    ev = make_event(expiry="2099-12-18", event_id="open-2")
    assert handle(st, b, ev=ev).startswith("ENTRY")
    if when == "same_poll":
        clock.tick(30)
    assert handle(st, b) is None        # the kept signal comes back
    assert b.executed == [] and st["positions"][KEY]["qty"] == 2
    assert st["stale_exits"] == ["x1"] and len(notes) == 1   # the "gone" note
    assert [r["action"] for r in _log(home)] == ["exit", "entry"]
    clock.tick(300)
    assert handle(st, b, ev=_exit("x2")).startswith("EXIT")
    assert b.executed == [("EXITED", 2)] and KEY not in st["positions"]


@pytest.mark.parametrize("why_kept", ["wait", "refused"])
def test_kept_exit_never_sells_a_new_position_after_the_periodic_check(
        home, clock, notes, no_policy, why_kept):
    """An exit signal kept for another poll (the check waits on a read, or
    the sell was refused), and then the periodic holdings check — not the
    exit check — finds the position closed. A new entry in that contract
    handled before the kept signal (a newest-first batch) must not be
    sold by it; the new position's own exit sells as usual."""
    st = _state()
    if why_kept == "wait":
        b = FillsBroker({U1: 0}, fills=[_fill()])   # one read of the sale
    else:
        class Refuses(FillsBroker):
            def execute(self, client, cfg, event, state):
                if event["event_id"] == "x1":
                    broker_events.report("exit_failed", "market closed")
                    return False
                return super().execute(client, cfg, event, state)
        b = Refuses({U1: 2}, fills=[_fill()])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    b.held = [{U1: 0}]                  # the owner sold in the app
    for _ in range(3):
        clock.tick(61)
        agent._position_upkeep(CFG, st, lambda s: None, b, object())
        if KEY not in st["positions"]:
            break
    assert st["closed_outside"][KEY]["held_exits"] == ["x1"]
    b.held = [{U1: 2}]                  # the new entry fills
    ev = make_event(expiry="2099-12-18", event_id="open-2")
    assert handle(st, b, ev=ev).startswith("ENTRY")
    assert handle(st, b) is None        # the kept signal comes back
    assert b.executed == [] and st["positions"][KEY]["qty"] == 2
    assert st["stale_exits"] == ["x1"]
    clock.tick(300)
    assert handle(st, b, ev=_exit("x2")).startswith("EXIT")
    assert b.executed == [("EXITED", 2)] and KEY not in st["positions"]


def test_stale_exit_ids_keep_the_last_50():
    st = {}
    for i in range(55):
        agent._note_stale_exit(st, f"x{i}")
    agent._note_stale_exit(st, "x10")
    assert st["stale_exits"] == [f"x{i}" for i in range(5, 55) if i != 10] + ["x10"]


def test_holdings_checks_follow_the_running_adapter(home, clock, notes, monkeypatch):
    """After a dashboard switch to Alpaca, the Robinhood adapter `run`
    started with still checks its own rows: legacy rows and trust get its
    tag, a close found names Robinhood, and the re-held check still runs."""
    b = FillsBroker({U1: 2}, {U1: 0}, fills=[_fill()])
    monkeypatch.setitem(agent.BROKERS, "robinhood", b)
    cfg = {**CFG, "broker": "alpaca", "alpaca_paper": True}
    st = _state(trusted=False)
    del st["positions"][KEY]["acct"]
    for _ in range(3):
        agent._sync_positions(cfg, st, lambda s: None, b, object())
        if KEY in st["positions"]:
            assert st["positions"][KEY]["acct"] == TAG
        clock.tick(60)
    assert list(st["position_sync"]["proven"]) == [TAG]
    (row,) = _log(home)
    assert "sold in your Robinhood account" in row["reason"]
    assert st["closed_outside"][KEY]["acct"] == TAG
    b.held = [{U1: 2}]
    assert handle(st, b, cfg=cfg) is None
    assert b.executed == [] and "holds 2 now" in notes[-1]


def test_unreadable_sales_dont_count_toward_gone(home, clock):
    """'Gone with no sale' needs the sales list: while it can't be read,
    the shortfall waits (up to 10 min)."""
    st = _state()
    b = FillsBroker({U1: 0}, fills=None)
    for _ in range(10):                 # 0 … 540 s
        assert _status(sync(st, b)) == "miss"
        clock.tick(60)
    assert st["positions"][KEY]["qty"] == 2 and _log(home) == []
    assert _status(sync(st, b)) == "closed"     # 600 s
    assert _log(home)[0]["cause"] == "gone"


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


def test_gate_mass_guard_counts_every_row_read(home, clock):
    """The exit check reads only its own row's shortfall, but an all-zero
    listing across every row still needs a sale before it drops anything."""
    st = _state()
    st["positions"][KEY2] = _row(option_id=U2)
    b = FillsBroker({U1: 0, U2: 0}, fills=[])
    for _ in range(2):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        clock.tick(21)
    assert handle(st, b).startswith("EXIT")     # unproven: sells as before
    assert b.executed == [("EXITED", 2)]
    assert not [r for r in _log(home) if r.get("manual")]


@pytest.mark.parametrize("answer", [None, {U1: 2}], ids=["unknown", "held"])
def test_gate_legacy_row_without_acct_sells(home, answer):
    st = _state()
    del st["positions"][KEY]["acct"]
    b = FillsBroker(answer)
    assert handle(st, b).startswith("EXIT")
    assert b.executed == [("EXITED", 2)]
    assert not [r for r in _log(home) if r["action"] == "exit_failed"]


def test_gate_reads_under_20s_apart_dont_count(home, clock):
    st = _state()
    b = FillsBroker({U1: 0}, fills=[_fill()])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    clock.tick(10)
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    assert st["positions"][KEY]["miss"]["reads"] == 1
    assert st["positions"][KEY]["qty"] == 2
    clock.tick(10)
    assert handle(st, b) is None
    assert KEY not in st["positions"] and b.executed == []


def test_gate_touches_only_its_row(home, clock):
    st = _state()
    miss = {"held": 0, "reads": 1, "need": 2, "first_at": T0 - 20,
            "last_at": T0 - 20}
    st["positions"][KEY2] = _row(option_id=U2, miss=dict(miss))
    b = FillsBroker({U1: 2, U2: 0}, fills={U2: [_fill("q1")]})
    handle(st, b)
    assert b.executed == [("EXITED", 2)]
    assert KEY2 in st["positions"] and st["positions"][KEY2]["miss"] == miss
    assert not [r for r in _log(home) if r.get("manual")]


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


@pytest.mark.parametrize("close_only", [True, False], ids=["close-only", "plain-sell"])
def test_dead_exit_from_4pm_on_expiry_day(home, notes, monkeypatch, close_only):
    """Holdings aren't read from 4 PM ET on expiry day, so a sell that
    waits for a read can't get one. After a sell that filled nothing, only
    an adapter whose sells can't open a position sells again; elsewhere
    the exit waits (no read, no order) and the owner is told once."""
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 16, 1, tzinfo=agent.MARKET_TZ))
    key = "SPY|2026-10-30|752.0|C"
    st = {"positions": {key: _row(exit_unfilled_at=T0 - 30)}, "seen": [],
          "position_sync": {"proven": {TAG: "2026-10-29T15:00:00+00:00"}}}
    b = FillsBroker({U1: 2})
    b.CLOSE_ONLY_EXITS = close_only
    ev = make_event(event="EXITED", expiry="2026-10-30", event_id="x1")
    if close_only:
        assert handle(st, b, ev=ev).startswith("EXIT")
        assert b.executed == [("EXITED", 2)] and _on_hold(notes) == []
        return
    for _ in range(3):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b, ev=ev)
    assert b.executed == [] and b.asked == [] and st["positions"][key]["qty"] == 2
    (note,) = _on_hold(notes)
    assert note.startswith("agentic-trader: the exit for SPY 2026-10-30 $752.0 C "
                           "is on hold")
    assert "after 4 PM ET on its expiry day, so it won't sell again" in note
    assert "close it yourself if you still hold it" in note


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


@pytest.mark.parametrize("hooks", [True, False], ids=["sync-off", "no-hooks"])
def test_gate_account_mismatch_blocks_without_holdings_reads(home, hooks):
    """The account check needs no read: it holds with position_sync off and
    for adapters without the hooks (a live sell of a paper row could open a
    short on Alpaca)."""
    st = _state(acct="alpaca:paper")
    cfg = {"broker": "alpaca", "alpaca_paper": False}
    if hooks:
        b = FillsBroker({U1: 2})
        cfg["position_sync"] = False
    else:
        b = ScriptedExit(lambda pos, state: pytest.fail("must not sell"))
    assert handle(st, b, cfg=cfg) is None
    assert [r["action"] for r in _log(home)] == ["exit_failed"]
    assert KEY in st["positions"]


def test_account_tag_follows_the_running_adapter(home, monkeypatch, no_policy):
    """`run` keeps the adapter it started with while config.json reloads:
    switching broker in the dashboard mustn't block the running adapter's
    exits, or tag its new entries with the other broker."""
    b = FillsBroker({U1: 2})
    monkeypatch.setitem(agent.BROKERS, "robinhood", b)
    cfg = {**CFG, "broker": "alpaca", "alpaca_paper": True}
    st = _state()
    assert handle(st, b, cfg=cfg).startswith("EXIT")
    assert b.executed == [("EXITED", 2)]
    ev = make_event(expiry="2099-12-18", event_id="open-2")
    assert handle(st, b, ev=ev, cfg=cfg).startswith("ENTRY")
    assert st["positions"][KEY]["acct"] == TAG


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


@pytest.mark.parametrize("close_only", [True, False], ids=["close-only", "plain-sell"])
def test_gate_exception_after_a_dead_exit_waits_unless_close_only(monkeypatch,
                                                                  close_only):
    """A failed check falls back to selling, except after a sell that
    filled nothing on an adapter whose sells aren't close-only: that sell
    may still fill late, and selling again unchecked could open a short."""
    def boom(*a, **k):
        raise PermissionError(13, "state.json is open in another program")
    monkeypatch.setattr(agent, "_sync_positions", boom)
    st = _state(exit_unfilled_at=T0 - 30)
    b = FillsBroker({U1: 0})
    b.CLOSE_ONLY_EXITS = close_only
    if close_only:
        handle(st, b)
        assert b.executed == [("EXITED", 2)]
    else:
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        assert b.executed == [] and st["positions"][KEY]["qty"] == 2


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


class DeadSell(FillsBroker):
    """Every exit is accepted and then dies with nothing filled, like a
    Robinhood async reject or an Alpaca 0-fill cancel. Like those adapters,
    it records the order and stamps last_order_at (holdings lag orders)."""

    def __init__(self, *held, fills=(), refuse_after=None):
        super().__init__(*held, fills=fills)
        self.sells, self.refuse_after = 0, refuse_after

    def execute(self, client, cfg, event, state):
        pos = state["positions"].get(KEY)
        if not pos:
            return False
        self.sells += 1
        if self.refuse_after is not None and self.sells > self.refuse_after:
            broker_events.report("exit_failed", "not enough contracts to close")
            return False  # refused when placed: nothing recorded
        pos["last_order_at"] = agent._now()
        pos["order_ids"] = pos.get("order_ids", []) + [f"dead-{self.sells}"]
        pos.pop("miss", None)
        broker_events.report("exit_partial", f"exit filled 0/{pos['qty']}")
        return True


def _poll(st, b, clock, times, every=30):
    """Up to `times` polls of the same exit signal, `every` s apart, until
    one is done (returns its outcome) — like the heartbeat."""
    for _ in range(times):
        try:
            return handle(st, b)
        except agent.ExitNotDone:
            clock.tick(every)
    return "not done"


def test_accepted_but_unsold_exit_retries_after_reading_holdings(home, notes, clock):
    st = _state()
    b = DeadSell({U1: 2})
    for _ in range(3):
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        clock.tick(30)
    pos = st["positions"][KEY]
    assert b.sells == 3 and len(b.asked) == 3 and pos["qty"] == 2
    # the dead order doesn't restart the grace period: every retry read first
    assert pos["last_order_at"] == T0 - 3600 and pos["exit_unfilled_at"] == T0 + 60
    assert pos["order_ids"] == ["o-agent", "dead-1", "dead-2", "dead-3"]
    assert [r["action"] for r in _log(home)] == ["exit_partial"]


def test_owner_closes_after_dead_exit_no_second_sell(home, notes, clock):
    """The exit_partial alert says 'close it yourself'; the owner does. The
    retry reads the account first and closes the row — never sells again."""
    st = _state()
    b = DeadSell({U1: 2}, {U1: 0}, fills=[_fill("owner-1", at="2026-10-30T15:00:20Z")])
    assert _poll(st, b, clock, 10) is None
    assert b.sells == 1 and KEY not in st["positions"]
    row = [r for r in _log(home) if r["action"] == "exit"][0]
    assert row["manual"] is True and row["cause"] == "sold"
    assert row["broker_order_ids"] == ["owner-1"]


class CloseOnlyDeadSell(DeadSell):
    """Like Robinhood: every exit sells to close, so it can't open a short."""
    CLOSE_ONLY_EXITS = True


def _on_hold(notes):
    return [n for n in notes if "is on hold" in n]


@pytest.mark.parametrize("close_only", [True, False], ids=["close-only", "plain-sell"])
def test_dead_exit_then_unclear_reads_wait_before_selling_again(home, clock, notes,
                                                                close_only):
    st = _state()
    b = (CloseOnlyDeadSell if close_only else DeadSell)({U1: 2}, None)
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    for _ in range(19):                 # unknown for 9.5 min: no sale
        clock.tick(30)
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
    assert b.sells == 1 and len(b.asked) == 20 and _on_hold(notes) == []
    clock.tick(30)                      # 10 min on
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    if close_only:                      # can't open a short: "unknown goes"
        assert b.sells == 2 and _on_hold(notes) == []
    else:                               # the dead sell may have filled late
        assert b.sells == 1
        (note,) = _on_hold(notes)
        assert note.startswith("agentic-trader: the exit for SPY 2099-12-18 $752.0 C")
        assert "won't sell again until a holdings check shows them" in note
        assert "close it yourself if you still hold it" in note
        assert st["positions"][KEY]["exit_stuck_warned"] is True


def test_stuck_exit_alert_names_the_running_adapter(home, clock, notes, monkeypatch):
    """The dashboard switched broker after `run` started: the on-hold alert
    names the account the running adapter trades."""
    b = DeadSell({U1: 2}, None)
    monkeypatch.setitem(agent.BROKERS, "robinhood", b)
    cfg = {**CFG, "broker": "alpaca", "alpaca_paper": True}
    st = _state()
    for _ in range(25):                 # 12 min of 30 s polls
        with pytest.raises(agent.ExitNotDone):
            handle(st, b, cfg=cfg)
        clock.tick(30)
    (note,) = _on_hold(notes)
    assert "your Robinhood account" in note and b.sells == 1


class SellsLater(DeadSell):
    """The first exit dies unfilled; each later one sells one contract."""

    def execute(self, client, cfg, event, state):
        pos = state["positions"].get(KEY)
        if not self.sells or not pos:
            return super().execute(client, cfg, event, state)
        self.sells += 1
        pos["qty"] -= 1
        pos["last_order_at"] = agent._now()
        broker_events.report("exit_partial", f"exit filled 1/{pos['qty'] + 1}")
        return True


def test_stuck_exit_reads_once_a_minute_then_sells_once_held(home, clock, notes):
    """Past 10 min of unclear reads after a dead exit (no close-only
    exits): wait without reading in between, alert once, and sell as soon
    as a read shows every contract still held."""
    st = _state()
    b = SellsLater({U1: 2})
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)                   # read 2, sell, nothing fills
    b.held = [None]
    asked = []
    for _ in range(30):                 # 30 s polls for 15 min
        clock.tick(30)
        with pytest.raises(agent.ExitNotDone):
            handle(st, b)
        asked.append(len(b.asked))
    assert b.sells == 1
    assert asked[:19] == list(range(2, 21))     # every poll for 10 min
    assert asked[19:] == [21, 21, 22, 22, 23, 23, 24, 24, 25, 25, 26]
    assert len(_on_hold(notes)) == 1
    b.held = [{U1: 2}]
    clock.tick(30)                      # under a minute since the last read
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    assert len(b.asked) == 26
    clock.tick(30)
    assert handle(st, b).startswith("EXIT")
    assert b.sells == 2
    pos = st["positions"][KEY]
    assert pos["qty"] == 1
    for k in ("exit_unfilled_at", "exit_read_at", "exit_stuck_warned"):
        assert k not in pos


def test_dead_exit_inside_entry_grace_never_resells_blind(home, clock):
    """An exit within 3 min of the entry dies; the owner then closes in the
    app. Polls still inside the entry's grace period read first."""
    st = _state(opened_at=T0 - 30, last_order_at=T0 - 30)
    b = DeadSell({U1: 0}, fills=[_fill("owner-1", at="2026-10-30T15:00:20Z")])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)                   # in grace: sells without a read
    assert b.sells == 1 and b.asked == []
    assert _poll(st, b, clock, 10) is None
    assert b.sells == 1 and KEY not in st["positions"]
    assert [r["cause"] for r in _log(home) if r.get("manual")] == ["sold"]


def test_dead_exit_sells_again_only_when_every_contract_is_held(home, clock):
    """After a dead exit the owner sold 1 of 3: the account shows 1 while
    the ledger, now 2, still waits on a confirming read. Selling 2 then
    could open a short."""
    st = _state(qty=3, exit_unfilled_at=T0 - 30,
                miss={"held": 2, "reads": 1, "need": 2, "first_at": T0 - 60,
                      "last_at": T0 - 60})
    b = FillsBroker({U1: 1}, fills=[_fill("owner-1", qty=1)])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    pos = st["positions"][KEY]
    assert b.executed == [] and pos["qty"] == 2 and pos["miss"]["held"] == 1


def test_dead_exit_owner_sold_part_rest_held_sells_the_rest_at_once(home, clock):
    """After a dead exit a read proves the owner sold 1 of 3 and shows the
    other 2 still held: the row shrinks to 2 and the exit sells those 2 on
    this poll, not a poll later."""
    st = _state(qty=3, exit_unfilled_at=T0 - 30,
                miss={"held": 2, "reads": 1, "need": 2, "first_at": T0 - 60,
                      "last_at": T0 - 60})
    b = FillsBroker({U1: 2}, fills=[_fill("owner-1", qty=1)])
    assert handle(st, b).startswith("EXIT")
    assert b.executed == [("EXITED", 2)]
    assert [(r["action"], r["qty"]) for r in _log(home)] == [("exit", 1), ("exit", 2)]


def test_dead_exit_then_refused_sells_close_after_reads(home, clock):
    """Later sells refused when placed (no order, no stamp): the gate keeps
    reading and closes the row once the owner's sale shows up."""
    st = _state()
    b = DeadSell({U1: 2}, {U1: 2}, {U1: 2}, {U1: 0}, fills=[_fill("owner-1")],
                 refuse_after=1)
    assert _poll(st, b, clock, 10) is None
    assert KEY not in st["positions"] and b.sells == 3 and len(b.asked) == 5


class _AResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeAlpaca:
    """Alpaca's REST API for one contract: every order stays 'accepted',
    then the cancel leaves it `after_cancel`; positions; closed orders."""
    OCC = "SPY991218C00752000"

    def __init__(self, after_cancel=("canceled", "0")):
        self.held, self.posts, self.position_reads = 2, [], 0
        self.after_cancel, self.cancelled, self.closed_orders = after_cancel, set(), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(dict(json))
        return _AResp(200, {"id": f"o{len(self.posts)}"})

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/positions"):
            self.position_reads += 1
            return _AResp(200, [{"symbol": self.OCC, "qty": str(self.held),
                                 "qty_available": str(self.held), "side": "long",
                                 "asset_class": "us_option"}] if self.held else [])
        if "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[1]
            status, filled = (self.after_cancel if oid in self.cancelled
                              else ("accepted", "0"))
            return _AResp(200, {"id": oid, "status": status, "filled_qty": filled})
        return _AResp(200, list(self.closed_orders))

    def delete(self, url, headers=None, timeout=None):
        self.cancelled.add(url.rsplit("/", 1)[1])
        return _AResp(204, None)


ALPACA = {"broker": "alpaca", "alpaca_paper": False, "alpaca_key_id": "k",
          "alpaca_secret": "s"}


class FakeAlpaca0DTE(FakeAlpaca):
    OCC = "SPY261030C00752000"     # expires on the test day, 2026-10-30
    KEY = "SPY|2026-10-30|752.0|C"


def _alpaca(monkeypatch, clock, after_cancel=("pending_cancel", "0"),
            fake=FakeAlpaca):
    """The real Alpaca adapter on a FakeAlpaca, one trusted row."""
    import types
    from brokers import alpaca
    http = fake(after_cancel=after_cancel)
    monkeypatch.setattr(alpaca, "requests", http)
    monkeypatch.setattr(alpaca, "time", types.SimpleNamespace(time=clock,
                                                              sleep=lambda s: None))
    tag = account_tag(ALPACA)
    key = getattr(fake, "KEY", KEY)
    st = {"positions": {key: _row(option_id=fake.OCC, acct=tag)}, "seen": [],
          "position_sync": {"proven": {tag: "2026-10-29T15:00:00+00:00"}}}
    return alpaca, http, st, alpaca.client(ALPACA)


@pytest.mark.parametrize("then", ["owner_sells", "first_sell_fills_late"])
def test_alpaca_exit_that_filled_nothing_never_resells_blind(home, clock, monkeypatch,
                                                             then):
    """Real Alpaca adapter: an exit sell cancelled with nothing filled. The
    owner then sells in the app, or the cancelled order fills after all —
    either way the account is flat and the retry must not sell again."""
    alpaca, http, st, client = _alpaca(monkeypatch, clock)
    cfg = ALPACA
    with pytest.raises(agent.ExitNotDone):
        handle(st, alpaca, cfg=cfg, client=client)
    assert len(http.posts) == 1 and KEY in st["positions"]
    http.held = 0
    if then == "owner_sells":
        http.closed_orders = [{"id": "owner-1", "symbol": FakeAlpaca.OCC,
                               "side": "sell", "filled_qty": "2",
                               "filled_avg_price": "1.90", "status": "filled",
                               "filled_at": "2026-10-30T15:00:20Z"}]
    for _ in range(12):
        clock.tick(30)
        try:
            handle(st, alpaca, cfg=cfg, client=client)
        except agent.ExitNotDone:
            continue
        break
    assert len(http.posts) == 1 and KEY not in st["positions"]
    assert http.position_reads >= 3
    manual = [r for r in _log(home) if r.get("manual")]
    assert manual[0]["cause"] == ("sold" if then == "owner_sells" else "gone")


def test_alpaca_dead_exit_that_fills_late_never_sells_again(home, clock, monkeypatch,
                                                            notes):
    """The cancelled exit fills after all, so the account is flat, but with
    a second row it reads all-zero: nothing proves the shortfall. A plain
    sell on Alpaca could open a short, so even long after the dead sell the
    agent never sends a second one; it alerts once instead."""
    alpaca, http, st, client = _alpaca(monkeypatch, clock)
    st["positions"][KEY2] = _row(option_id="QQQ991218P00500000",
                                 acct=account_tag(ALPACA))
    with pytest.raises(agent.ExitNotDone):
        handle(st, alpaca, cfg=ALPACA, client=client)
    http.held = 0                       # both rows read 0 from now on
    for _ in range(30):                 # 15 min
        clock.tick(30)
        with pytest.raises(agent.ExitNotDone):
            handle(st, alpaca, cfg=ALPACA, client=client)
    assert len(http.posts) == 1 and st["positions"][KEY]["qty"] == 2
    assert len(_on_hold(notes)) == 1 and "your Alpaca account" in _on_hold(notes)[0]
    assert http.position_reads == 1 + 19 + 6   # once a minute after 10 min


@pytest.mark.parametrize("then", ["owner_sells", "first_sell_fills_late"])
def test_alpaca_0dte_dead_exit_never_resells_blind_after_4pm(home, clock, monkeypatch,
                                                              notes, then):
    """Real Alpaca adapter, a contract expiring today: the exit sell dies at
    3:59 PM ET and the account goes flat. Holdings aren't read from 4 PM ET
    on expiry day, so no read can clear a second plain sell: through 4:15 PM
    the agent sends just the one sell and alerts once."""
    key = FakeAlpaca0DTE.KEY
    alpaca, http, st, client = _alpaca(monkeypatch, clock, fake=FakeAlpaca0DTE)
    clock.t = datetime(2026, 10, 30, 15, 59, 25, tzinfo=agent.MARKET_TZ).timestamp()
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime.fromtimestamp(clock(), agent.MARKET_TZ))
    ev = make_event(event="EXITED", expiry="2026-10-30", event_id="x1")
    with pytest.raises(agent.ExitNotDone):
        handle(st, alpaca, ev=ev, cfg=ALPACA, client=client)
    assert len(http.posts) == 1
    http.held = 0
    if then == "owner_sells":
        http.closed_orders = [{"id": "owner-1", "symbol": FakeAlpaca0DTE.OCC,
                               "side": "sell", "filled_qty": "2",
                               "filled_avg_price": "0.40", "status": "filled",
                               "filled_at": "2026-10-30T19:59:50Z"}]
    end = datetime(2026, 10, 30, 16, 15, tzinfo=agent.MARKET_TZ)
    while agent._et_now() < end:        # 30 s polls past 4 PM
        clock.tick(30)
        with pytest.raises(agent.ExitNotDone):
            handle(st, alpaca, ev=ev, cfg=ALPACA, client=client)
    assert len(http.posts) == 1 and st["positions"][key]["qty"] == 2
    (note,) = _on_hold(notes)
    assert "your Alpaca account for it after 4 PM ET on its expiry day" in note


def test_alpaca_state_save_failure_after_dead_exit_never_resells_blind(
        home, clock, monkeypatch):
    """Real Alpaca adapter: the exit sell dies, then fills late (account
    flat). On the next poll state.json can't be written once (on Windows
    another program can hold it open), so the exit check fails part-way.
    The agent must not fall back to a second plain sell."""
    alpaca, http, st, client = _alpaca(monkeypatch, clock)
    with pytest.raises(agent.ExitNotDone):
        handle(st, alpaca, cfg=ALPACA, client=client)
    http.held = 0
    saves = []

    def locked_once(s):
        saves.append(1)
        if len(saves) == 1:
            raise PermissionError(13, "The process cannot access the file")
    clock.tick(30)
    with pytest.raises(agent.ExitNotDone):
        agent.handle_event(_exit(), ALPACA, st, alpaca, client, locked_once)
    assert len(saves) == 2 and len(http.posts) == 1
    assert st["positions"][KEY]["qty"] == 2


def test_alpaca_paper_row_blocked_after_switch_to_live_with_startup_client(
        home, clock, monkeypatch, notes, no_policy):
    """`run` keeps the Alpaca client built from the paper config; the owner
    switches to live in the dashboard and Alpaca orders follow the live
    config. A row opened in paper is blocked, never sold in the live
    account, and a new entry is tagged live."""
    import types
    from brokers import alpaca
    http = FakeAlpaca()
    monkeypatch.setattr(alpaca, "requests", http)
    monkeypatch.setattr(alpaca, "time", types.SimpleNamespace(time=clock,
                                                              sleep=lambda s: None))
    paper = {**ALPACA, "alpaca_paper": True}
    client = alpaca.client(paper)       # the startup copy: paper
    ptag = account_tag(paper)
    st = {"positions": {KEY: _row(option_id=FakeAlpaca.OCC, acct=ptag)}, "seen": [],
          "position_sync": {"proven": {ptag: "2026-10-29T15:00:00+00:00"}}}
    assert handle(st, alpaca, cfg=ALPACA, client=client) is None
    assert http.posts == [] and KEY in st["positions"]
    assert [r["action"] for r in _log(home)] == ["exit_failed"]
    st2 = {"positions": {}, "seen": []}
    monkeypatch.setattr(alpaca, "_size", lambda cfg, occ: (1, "test"))
    http.after_cancel = ("filled", "1")
    ev = make_event(expiry="2099-12-18", event_id="open-9")
    assert handle(st2, alpaca, ev=ev, cfg=ALPACA, client=client).startswith("ENTRY")
    assert st2["positions"][KEY]["acct"] == "alpaca:live"


def test_alpaca_exit_with_unconfirmed_cancel_says_check_this_trade(home, clock,
                                                                   monkeypatch, notes):
    """The exit's cancel can't be confirmed, so the agent assumes it sold:
    the report is about the sell (exit_failed) and the alert doesn't claim
    the trade wasn't placed."""
    alpaca, http, st, client = _alpaca(monkeypatch, clock, after_cancel=("", "0"))
    assert handle(st, alpaca, cfg=ALPACA, client=client).startswith("EXIT")
    assert KEY not in st["positions"] and len(http.posts) == 1
    problem, exit_row = _log(home)
    assert problem["action"] == "exit_failed" and exit_row["action"] == "exit"
    assert "the sell order for 2x" in problem["reason"]
    (note,) = notes
    assert note.startswith(f"agentic-trader: check this trade ({CONTRACT}) — "
                           "Alpaca couldn't confirm whether the sell order")
    assert "NOT placed" not in note


def test_unfilled_exit_without_holdings_reads_is_logged_as_before(home):
    """Adapters without the read hooks (or position_sync off) keep the old
    behaviour: no retry loop that no read could ever stop."""
    def dead(pos, state):
        broker_events.report("exit_partial", f"exit filled 0/{pos['qty']}")
        return True
    st = _state()
    assert handle(st, ScriptedExit(dead)).startswith("EXIT")
    assert [r["action"] for r in _log(home)] == ["exit_partial", "exit"]
    b = DeadSell({U1: 2})
    st = _state()
    assert handle(st, b, cfg={**CFG, "position_sync": False}).startswith("EXIT")
    assert b.sells == 1 and b.asked == []


def test_unfilled_exit_with_nothing_reported_says_so(home):
    class Silent(DeadSell):
        def execute(self, client, cfg, event, state):
            self.sells += 1
            return True
    with pytest.raises(agent.ExitNotDone):
        handle(_state(), Silent({U1: 2}))
    (row,) = _log(home)
    assert row["action"] == "exit_failed"
    assert row["reason"] == "the broker accepted the exit order but nothing sold — retrying"


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
    assert row["partial"] is True and row["remaining"] == 1


def test_owner_closes_the_rest_after_partial_agent_exit_same_pnl_day(home, clock):
    with open(agent.TRADES_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps({"action": "entry", "contract": CONTRACT, "dry": False,
                            "ts": "2026-10-28T15:00:00+00:00"}) + "\n")

    def partial(pos, state):
        pos["qty"] = 1
        broker_events.report("exit_partial", "exit filled 1/2")
        return True
    st = _state()
    handle(st, ScriptedExit(partial, mark=2.0))
    b = FillsBroker({U1: 0}, fills=[_fill(qty=1)])
    sync(st, b)
    clock.tick(60)
    sync(st, b)
    assert KEY not in st["positions"]
    s = agent.pnl_summary({"source": "url"}, {})
    assert [(d["day"], d["pnl"], d["exits"]) for d in s["live"]["days"]] == [
        ("2026-10-28", 80.0, 2)]   # (2.00-1.50) x 1 + (1.80-1.50) x 1


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


@pytest.mark.parametrize("variant", ["other_acct", "sync_off"])
def test_reheld_check_only_for_this_account_and_when_enabled(home, notes, variant):
    st = _closed_state()
    cfg = CFG
    if variant == "other_acct":
        st["closed_outside"][KEY]["acct"] = account_tag(
            {"broker": "robinhood", "robinhood_account": "OTHER"})
    else:
        cfg = {**CFG, "position_sync": False}
    b = FillsBroker({U1: 2})
    assert handle(st, b, cfg=cfg) is None
    assert b.asked == [] and b.executed == []
    assert not [n for n in notes if "holds" in n]


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


def test_check_positions_with_the_broker_disconnected_says_so(home, monkeypatch,
                                                             capsys):
    import types
    agent._save(agent.STATE_PATH, _state())
    agent._save(agent.CONFIG_PATH, CFG)
    fake = types.SimpleNamespace(
        client=lambda cfg: None,        # e.g. an expired Robinhood login
        held_option_qty=lambda client, cfg, ids: pytest.fail("no read without a client"))
    monkeypatch.setitem(agent.BROKERS, "robinhood", fake)
    agent.cmd_check_positions()
    out = capsys.readouterr().out
    assert ("broker answer    : unknown — broker not connected — run: "
            "python agent.py setup") in out
    assert "agent tracks 2, broker shows unknown" in out


def test_check_positions_is_in_the_help_text():
    assert "check-positions" in agent.__doc__


# ── kept exits and dead sells: saves, alerts, first-seen "gone" ─────────────

def test_exit_first_seen_on_the_gone_read_never_sells_a_reentry(home, clock, notes,
                                                                no_policy):
    """The periodic check already counted 2 of 3 "gone" reads; the exit
    signal arrives for the first time and its own read confirms "gone". The
    row never held that signal, so only the gate's note on the tombstone
    keeps it from selling a re-entry of the same contract."""
    st = _state(miss={"held": 0, "reads": 2, "need": 2, "first_at": T0 - 150,
                      "last_at": T0 - 60})
    b = FillsBroker({U1: 0}, fills=[])
    with pytest.raises(agent.ExitNotDone):
        handle(st, b)
    assert KEY not in st["positions"]
    assert st["closed_outside"][KEY]["held_exits"] == ["x1"]
    b.held = [{U1: 2}]
    handle(st, b, ev=make_event(expiry="2099-12-18", event_id="open-2"))
    assert KEY in st["positions"]
    assert handle(st, b) is None
    assert b.executed == [] and st["stale_exits"] == ["x1"]
    assert st["positions"][KEY]["opened_event"] == "open-2"


def test_dead_exit_mark_and_kept_exit_are_saved_before_retrying(home, clock, notes,
                                                                frozen_now):
    """With today's alert slots already used (no save from an alert), the
    state saved before the retry still carries the dead-sell mark and the
    kept exit, so a restart can't lose them."""
    import copy
    saved = []
    st = _state()
    today = frozen_now.date().isoformat()
    st["problem_notified"] = {k: today for k in (
        "exit_failed", "exit_partial", "exit_failed:placed", "exit_partial:placed")}
    b = DeadSell({U1: 2}, None)
    with pytest.raises(agent.ExitNotDone):
        agent.handle_event(_exit(), CFG, st, b, object(),
                           lambda s: saved.append(copy.deepcopy(s)))
    last = saved[-1]["positions"][KEY]
    assert last.get("exit_unfilled_at") and last.get("held_exits") == ["x1"]


def test_on_hold_alert_survives_a_failed_save(home, clock, notes):
    st = _state()
    b = DeadSell({U1: 2}, None)
    failed = []

    def save(s):
        row = s["positions"].get(KEY) or {}
        if row.get("exit_stuck_warned") and not failed:
            failed.append(1)
            raise PermissionError(13, "state.json is open in another program")
    for _ in range(30):
        try:
            agent.handle_event(_exit(), CFG, st, b, object(), save)
        except agent.ExitNotDone:
            pass
        clock.tick(30)
    assert failed and len(_on_hold(notes)) == 1 and b.sells == 1


def test_failing_check_after_a_dead_exit_alerts_once_and_never_sells(home, clock,
                                                                    notes, monkeypatch):
    """The check before the exit keeps raising after a sell that filled
    nothing (plain-sell adapter): the exit waits, and after 10 minutes the
    owner hears once that it's on hold."""
    def boom(*a, **k):
        raise PermissionError(13, "state.json is open in another program")
    monkeypatch.setattr(agent, "_sync_positions", boom)
    st = _state(exit_unfilled_at=T0 - 30)
    b = FillsBroker({U1: 2})
    waits = 0
    for i in range(40):                 # 20 min of 30 s polls
        try:
            handle(st, b)
        except agent.ExitNotDone:
            waits += 1
        if i == 15:                     # 8 min in: not yet
            assert _on_hold(notes) == []
        clock.tick(30)
    assert waits == 40 and b.executed == []
    assert len(_on_hold(notes)) == 1
