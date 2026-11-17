"""Per-book exits: the main pick and "other trades" never sell each other's
position.

With "other trades" on, the AgentHC feed sends two books of signals: the
main pick (track "main") and the wider journal (track "other", the agent's
"shadow" book). Both can name the SAME contract. The ledger holds one row
per contract, and an entry for a contract it already holds buys nothing, so
that row's contracts belong to the book whose entry bought them. An exit
signal from the other book must not sell them: they stay open until their
own book's exit. Rows saved before the book was recorded (no "book") close
on an exit from either book, as before — never stranded. The agent's own
exits (expiry guard, no-signal sell, exit remainder) sell a row whatever
its book. Zero network."""

import json
from datetime import datetime, timezone

import pytest

import agent
import broker_events
from brokers import alpaca, moomoo, robinhood
from brokers.pricing import exit_qty
from conftest import make_event

KEY = "SPY|2026-07-10|752.0|C"
CONTRACT = "SPY 2026-07-10 $752.0 C"


def ev(event, track, eid, **over):
    """A feed event; track None = a source that sends no track at all."""
    e = make_event(event=event, event_id=eid, **over)
    if track is not None:
        e["track"] = track
    return e


class Book:
    """A broker adapter that behaves like the real ones for the ledger: an
    ENTERED for a contract the ledger already holds buys nothing; an EXITED
    sells exit_qty(row, event), filling the next `plan` entry (default:
    everything asked)."""

    def __init__(self, qty=2, plan=()):
        self.qty, self.plan = qty, list(plan)
        self.buys, self.sells = [], []

    def execute(self, client, cfg, event, state):
        key = f"{event['ticker']}|{event['expiry']}|{event['strike']}|{event['type']}"
        pos = state["positions"].get(key)
        if event["event"] == "ENTERED":
            if pos:
                return False
            state["positions"][key] = {"option_id": "opt-1", "qty": self.qty,
                                       "opened_event": event["event_id"]}
            self.buys.append((event["event_id"], self.qty))
            return True
        if not pos:
            return False
        q = exit_qty(pos, event)
        filled = min(q, self.plan.pop(0)) if self.plan else q
        self.sells.append((event["event_id"], q, filled))
        if filled >= pos["qty"]:
            state["positions"].pop(key)
        else:
            pos["qty"] -= filled
            broker_events.report("exit_partial", f"exit filled {filled}/{q}")
        return True


# "other trades" on: the AgentHC source asks for both books
LIVE = {"dry_run": False, "max_entries_per_day": 20, "max_open_positions": 10,
        "include_other_trades": True}
DRY = {**LIVE, "dry_run": True}


def run(b, state, e, cfg=LIVE):
    return agent.handle_event(dict(e), cfg, state, b, object(), lambda s: None)


def _log(home):
    p = home / "trades.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] \
        if p.exists() else []


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


# ── the bug ─────────────────────────────────────────────────────────────────

def test_other_trades_exit_never_sells_the_main_picks_position(home, no_policy):
    """THE BUG: both books signal the same contract; the "other trades"
    exit sold the main pick's contracts."""
    b, state = Book(qty=2), {"positions": {}}
    run(b, state, ev("ENTERED", "main", "m-in"))
    run(b, state, ev("ENTERED", "other", "o-in"))     # held already: nothing bought
    assert b.buys == [("m-in", 2)]
    out = run(b, state, ev("EXITED", "other", "o-out"))
    assert b.sells == []                               # no order for the main pick's 2
    assert state["positions"][KEY]["qty"] == 2
    assert "no order placed" in out and "main pick" in out
    assert [r["action"] for r in _log(home)] == ["entry"]   # no exit row
    assert run(b, state, ev("EXITED", "main", "m-out")) == f"EXIT: {CONTRACT}"
    assert b.sells == [("m-out", 2, 2)] and KEY not in state["positions"]
    assert [r["action"] for r in _log(home)] == ["entry", "exit"]


def test_main_exit_never_sells_the_other_trades_position(home, no_policy):
    b, state = Book(qty=3), {"positions": {}}
    run(b, state, ev("ENTERED", "other", "o-in"))
    run(b, state, ev("ENTERED", "main", "m-in"))
    assert b.buys == [("o-in", 3)]
    out = run(b, state, ev("EXITED", "main", "m-out"))
    assert b.sells == [] and state["positions"][KEY]["qty"] == 3
    assert "no order placed" in out and '"other trades"' in out
    run(b, state, ev("EXITED", "other", "o-out"))
    assert b.sells == [("o-out", 3, 3)] and state["positions"] == {}


def test_each_entry_records_its_book_on_the_row(home, no_policy):
    for track, book in (("main", "main"), ("other", "shadow")):
        b, state = Book(), {"positions": {}}
        run(b, state, ev("ENTERED", track, f"{track}-in"))
        assert state["positions"][KEY]["book"] == book


def test_a_second_books_entry_never_changes_the_rows_book(home, no_policy):
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", "main", "m-in"))
    run(b, state, ev("ENTERED", "other", "o-in"))
    assert state["positions"][KEY]["book"] == "main"


# ── legacy rows and sources without books: as before ───────────────────────

@pytest.mark.parametrize("track", ["main", "other", None])
def test_legacy_row_without_a_book_still_exits_on_either_books_signal(
        home, no_policy, track):
    """A row saved before the book was recorded is never stranded: an exit
    from either book (or a source with no tracks) sells it, as before."""
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2,
                                 "opened_event": "old-in"}}}
    assert run(b, state, ev("EXITED", track, "x-1")) == f"EXIT: {CONTRACT}"
    assert b.sells == [("x-1", 2, 2)] and state["positions"] == {}


@pytest.mark.parametrize("bad", ["", "other", None, 7])
def test_a_damaged_book_value_reads_as_legacy(home, no_policy, bad):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": bad}}}
    run(b, state, ev("EXITED", "main", "x-1"))
    assert b.sells == [("x-1", 2, 2)]


def test_a_source_without_tracks_records_no_book_and_exits_as_before(
        home, no_policy):
    """manual_file / generic_json_url send no track: rows stay book-less."""
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", None, "in-1"))
    assert "book" not in state["positions"][KEY]
    run(b, state, ev("EXITED", "other", "out-1"))
    assert b.sells == [("out-1", 2, 2)]


# ── practice (dry-run) ─────────────────────────────────────────────────────

def test_practice_exits_are_per_book_too(home, no_policy):
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", "main", "m-in"), DRY)
    run(b, state, ev("ENTERED", "other", "o-in"), DRY)
    row = state["positions"][KEY]
    assert row["dry"] is True and row["book"] == "main"
    out = run(b, state, ev("EXITED", "other", "o-out"), DRY)
    assert "no order placed" in out and KEY in state["positions"]
    assert run(b, state, ev("EXITED", "main", "m-out"), DRY) == \
        f"[DRY-RUN] EXIT: {CONTRACT}"
    assert state["positions"] == {}
    assert b.buys == [] and b.sells == []              # practice never orders
    acts = [(r["action"], r["event_id"]) for r in _log(home)]
    assert acts == [("entry", "m-in"), ("exit", "m-out")]


def test_practice_legacy_row_exits_on_either_book(home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "dry", "qty": 1, "dry": True}}}
    run(b, state, ev("EXITED", "other", "o-out"), DRY)
    assert state["positions"] == {}


def test_real_row_of_the_other_book_in_dry_run_is_not_reported_as_failed(
        home, no_policy, notes):
    """In dry-run a real row's exit is reported as "sell it yourself" —
    but an exit of the OTHER book isn't this row's exit at all."""
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "live": True,
                                 "book": "main"}}}
    out = run(b, state, ev("EXITED", "other", "o-out"), DRY)
    assert "no order placed" in out
    assert [r["action"] for r in _log(home)] == []
    assert KEY in state["positions"]


# ── partial fills and the exit remainder ───────────────────────────────────

def test_partial_fill_remainder_is_resold_but_never_by_the_other_book(
        home, no_policy):
    b, state = Book(qty=3, plan=[1]), {"positions": {}}
    run(b, state, ev("ENTERED", "main", "m-in"))
    run(b, state, ev("EXITED", "main", "m-out"))       # sold 1 of 3
    row = state["positions"][KEY]
    assert row["qty"] == 2 and row["book"] == "main"
    assert isinstance(row.get("exiting"), dict)
    run(b, state, ev("EXITED", "other", "o-out"))
    assert b.sells == [("m-out", 3, 1)]                # the other book sold nothing
    rest = agent._exit_event(KEY, "exit-rest|x", "exit_remainder")
    assert run(b, state, rest) == f"EXIT: {CONTRACT}"
    assert b.sells[-1] == ("exit-rest|x", 2, 2) and state["positions"] == {}


# ── the agent's own exits sell a row whatever its book ─────────────────────

@pytest.mark.parametrize("cause", ["expiry_guard", "signal_loss", "exit_remainder"])
@pytest.mark.parametrize("book", ["main", "shadow"])
def test_agent_made_exits_sell_a_row_of_either_book(home, no_policy, cause, book):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": book}}}
    e = agent._exit_event(KEY, f"{cause}|{KEY}", cause)
    assert run(b, state, e) == f"EXIT: {CONTRACT}"
    assert b.sells == [(f"{cause}|{KEY}", 2, 2)]


def test_expiry_guard_sells_the_other_trades_row(home, no_policy, notes):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow"}}}
    out = agent._expiry_guard_sell(LIVE, state, lambda s: None, b, object(), KEY,
                                   "2026-07-10", 15 * 60 + 45)
    assert out == f"EXIT: {CONTRACT}" and state["positions"] == {}


def test_exit_row_carries_the_book_of_the_position_it_sold(home, no_policy):
    """The guard's exit has no track; the trade-log row is labelled with
    the book whose contracts it sold."""
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow"}}}
    run(b, state, agent._exit_event(KEY, "g-1", "expiry_guard"))
    (row,) = _log(home)
    assert row["action"] == "exit" and row["ui"]["book"] == "other"


@pytest.mark.parametrize("held,label", [
    ({"book": "shadow"}, "other"), ({"book": "main"}, "main"),
    ({"track": "other"}, "other"), ({}, None)])
def test_an_expired_unsold_row_carries_its_book(home, held, label):
    """audit PR7's expiry booking: the exit row for contracts that expired
    unsold is labelled with the row's book, like a sale (none: no label)."""
    row = {"option_id": "opt-1", "qty": 2, "entry_mark": 1.5, **held}
    rec = agent._expired_exit_row(LIVE, KEY, row)
    assert rec["action"] == "exit" and rec["cause"] == "expired"
    assert rec["ui"].get("book") == label


# ── exits still owed (the feed can't be read, the pass lapsed) ─────────────

def test_owed_exits_are_matched_by_book_and_settled(home, no_policy, notes):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow"}},
             "seen": [],
             "exits_to_retry": [ev("EXITED", "main", "m-out"),
                                ev("EXITED", "other", "o-out")]}
    agent._handle_feed_events([], LIVE, state, b, object(), lambda s: None,
                              feed_read=False)
    assert b.sells == [("o-out", 2, 2)] and state["positions"] == {}
    assert agent._exits_to_retry(state) == []          # both settled
    assert {"m-out", "o-out"} <= set(state["seen"])


def test_owed_exit_of_the_other_book_does_not_sell_and_is_not_retried(
        home, no_policy, notes):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "main"}},
             "seen": [], "exits_to_retry": [ev("EXITED", "other", "o-out")]}
    for _ in range(2):
        agent._handle_feed_events([], LIVE, state, b, object(), lambda s: None,
                                  feed_read=False)
    assert b.sells == [] and state["positions"][KEY]["qty"] == 2
    assert agent._exits_to_retry(state) == []


def test_one_poll_with_both_exits_sells_once(home, no_policy, notes):
    b, state = Book(qty=2), {"positions": {}, "seen": []}
    agent._handle_feed_events([ev("ENTERED", "main", "m-in")], LIVE, state, b,
                              object(), lambda s: None)
    agent._handle_feed_events([ev("EXITED", "other", "o-out"),
                               ev("EXITED", "main", "m-out")], LIVE, state, b,
                              object(), lambda s: None)
    assert b.sells == [("m-out", 2, 2)] and state["positions"] == {}


# ── restart ─────────────────────────────────────────────────────────────────

def test_the_book_survives_a_restart(home, no_policy):
    b, state = Book(), {"positions": {}, "seen": []}
    run(b, state, ev("ENTERED", "other", "o-in"))
    agent._save(agent.STATE_PATH, state, private=True, backup=True)
    loaded, info = agent._load_state()
    assert info["how"] == "ok" and loaded["positions"][KEY]["book"] == "shadow"
    run(b, loaded, ev("EXITED", "main", "m-out"))
    assert b.sells == [] and KEY in loaded["positions"]
    run(b, loaded, ev("EXITED", "other", "o-out"))
    assert b.sells == [("o-out", 2, 2)]


# ── a buy found at the broker later, and the ledger check's re-add ─────────

def test_a_buy_found_later_gets_the_book_of_the_signal_that_sent_it(
        home, no_policy, notes):
    """The buy's answer was lost (no row); the adapter finds it on a later
    loop. The row it adopts belongs to the signal's book."""
    from brokers import order_guard

    class Lost(Book):
        def execute(self, client, cfg, event, state):
            if event["event"] == "ENTERED":
                order_guard.note(state, KEY, "buy", "k", 2, "opt-1",
                                 event_id=event["event_id"])
                return False
            return super().execute(client, cfg, event, state)

    b, state = Lost(), {"positions": {}}
    run(b, state, ev("ENTERED", "other", "o-in"))
    assert state["positions"] == {}
    # a later loop: the adapter's reconcile finds the buy at the broker
    order_guard.drop(state, KEY)
    state["positions"][KEY] = {"option_id": "opt-1", "qty": 2, "opened_event": "o-in"}
    order_guard.adopted_buy(state, KEY, {"event_id": "o-in", "at": agent._now()}, "b-1")
    agent._log_adopted_buys(LIVE, state, lambda s: None, b, object())
    assert state["positions"][KEY]["book"] == "shadow"
    run(b, state, ev("EXITED", "main", "m-out"))
    assert b.sells == []


def test_entry_books_memory_is_bounded(home):
    state = {}
    for i in range(agent._ENTRY_BOOKS_KEPT + 30):
        agent._note_entry_book(state, f"e{i}", "main")
    books = state["entry_books"]
    assert len(books) == agent._ENTRY_BOOKS_KEPT
    assert f"e{agent._ENTRY_BOOKS_KEPT + 29}" in books and "e0" not in books


def test_the_ledger_checks_record_keeps_the_book(home):
    state = {"positions": {}}
    row = {"option_id": "opt-1", "qty": 2, "book": "shadow", "opened_event": "o-in"}
    agent._remember_agent_row(state, KEY, row, "fake:", agent._now(), new_ids=["b-1"])
    assert state["agent_bought"]["opt-1"]["book"] == "shadow"


# ── manual close found by the holdings read (broker reconcile) ─────────────

class Holdings(Book):
    """Book + the read hooks: `held` is what the account holds; `fills`
    the owner's own sells."""

    def __init__(self, held, fills=(), **kw):
        super().__init__(**kw)
        self.held, self.fills = dict(held), list(fills)

    def held_option_qty(self, client, cfg, option_ids):
        return {i: self.held.get(i, 0) for i in option_ids}

    def closed_fills(self, client, cfg, option_id, since_iso=None,
                     own_order_ids=(), exclude_agentic=False):
        return [dict(f) for f in self.fills if f["order_id"] not in own_order_ids]

    def execute(self, client, cfg, event, state):
        changed = super().execute(client, cfg, event, state)
        if event["event"] == "EXITED" and changed:
            self.held["opt-1"] -= self.sells[-1][2]
        return changed


def test_owner_sold_part_in_the_app_then_each_book_exits_its_own(
        home, no_policy, notes, monkeypatch):
    key = "SPY|2099-12-18|752.0|C"
    t = {"now": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc).timestamp()}
    monkeypatch.setattr(agent, "_now", lambda: t["now"])
    monkeypatch.setattr(agent, "_et_now",
                        lambda: datetime(2026, 10, 30, 11, 0, tzinfo=agent.MARKET_TZ))
    monkeypatch.setattr(agent, "_market_open_now", lambda: True)
    monkeypatch.setattr(agent, "_in_open_rush", lambda: False)
    monkeypatch.setitem(agent._POS_SYNC, "next_at", 0.0)
    cfg = {**LIVE, "broker": "fake"}
    tag = agent._account_tag(cfg)
    state = {"positions": {key: {"option_id": "opt-1", "qty": 3, "book": "main",
                                 "acct": tag, "opened_at": t["now"] - 3600,
                                 "last_order_at": t["now"] - 3600,
                                 "order_ids": ["b-1"], "entry_mark": 1.0}},
             "position_sync": {"proven": {tag: "2026-10-29T15:00:00+00:00"}},
             "seen": []}
    b = Holdings({"opt-1": 1}, fills=[{"order_id": "u1", "qty": 2, "price": 1.5,
                                       "at": "2026-10-30T14:30:00Z", "by": "user"}])
    for _ in range(2):  # two reads, apart: the owner's sale is booked
        agent._sync_positions(cfg, state, lambda s: None, b, object())
        t["now"] += 60
    row = state["positions"][key]
    assert row["qty"] == 1 and row["book"] == "main"
    out = agent.handle_event(ev("EXITED", "other", "o-out", expiry="2099-12-18"),
                             cfg, state, b, object(), lambda s: None)
    assert "no order placed" in out and b.sells == []
    agent.handle_event(ev("EXITED", "main", "m-out", expiry="2099-12-18"), cfg,
                       state, b, object(), lambda s: None)
    assert b.sells == [("m-out", 1, 1)] and state["positions"] == {}
    sold = [r for r in _log(home) if r["action"] == "exit"]
    assert [r.get("qty") for r in sold] == [2, 1]      # the owner's 2, then the agent's 1


# ── each broker adapter sells only that book's contracts ───────────────────

def _alpaca_api(monkeypatch, held):
    from test_order_once import FakeAlpaca
    monkeypatch.setattr(alpaca, "_CONFIRM_WAIT_S", 0)
    return FakeAlpaca(monkeypatch, start=held)


@pytest.mark.parametrize("legacy", [False, True], ids=["booked", "legacy"])
def test_alpaca_sells_only_the_exiting_books_contracts(home, no_policy, notes,
                                                       monkeypatch, legacy):
    from test_order_once import ALP_CFG, OCC
    api = _alpaca_api(monkeypatch, {OCC: 3})
    row = {"option_id": OCC, "qty": 3, "order_ids": ["b-0"], "opened_event": "m-in"}
    if not legacy:
        row["book"] = "main"
    state = {"positions": {KEY: row}, "seen": []}
    cl = alpaca.client(ALP_CFG)
    agent.handle_event(ev("EXITED", "other", "o-out"), ALP_CFG, state, alpaca, cl,
                       lambda s: None)
    if legacy:  # as before: the exit sells the row
        assert [p["qty"] for p in api.posts] == ["3"] and state["positions"] == {}
        return
    assert api.posts == [] and state["positions"][KEY]["qty"] == 3
    agent.handle_event(ev("EXITED", "main", "m-out"), ALP_CFG, state, alpaca, cl,
                       lambda s: None)
    (sell,) = api.posts
    assert sell["side"] == "sell" and sell["qty"] == "3"
    assert state["positions"] == {}


def test_alpaca_both_books_same_contract_end_to_end(home, no_policy, notes,
                                                    monkeypatch):
    """Real Alpaca adapter: the main pick buys, the other book's entry for
    the same contract buys nothing, its exit sells nothing, the main pick's
    exit sells exactly what it bought."""
    from test_order_once import ALP_CFG, Feed
    api = _alpaca_api(monkeypatch, {})
    state = {"positions": {}, "seen": []}
    feed = Feed(alpaca, alpaca.client(ALP_CFG), ALP_CFG, state)
    feed.poll(ev("ENTERED", "main", "m-in"))
    feed.poll(ev("ENTERED", "other", "o-in"))
    bought = [p for p in api.posts if p["side"] == "buy"]
    assert len(bought) == 1 and state["positions"][KEY]["book"] == "main"
    n = int(bought[0]["qty"])
    feed.poll(ev("EXITED", "other", "o-out"))
    assert [p for p in api.posts if p["side"] == "sell"] == []
    feed.poll(ev("EXITED", "main", "m-out"))
    sells = [p for p in api.posts if p["side"] == "sell"]
    assert [s["qty"] for s in sells] == [str(n)] and api.held() == 0
    assert feed.errors == []


@pytest.mark.parametrize("legacy", [False, True], ids=["booked", "legacy"])
def test_robinhood_sells_only_the_exiting_books_contracts(home, no_policy, notes,
                                                          monkeypatch, legacy):
    from test_order_once import OPT, RH_CFG, FakeRobinhood
    monkeypatch.setattr(robinhood, "_SETTLE_WAIT_S", 0)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)
    rh = FakeRobinhood()
    row = {"option_id": OPT, "qty": 2, "order_ids": ["b-0"], "opened_event": "o-in"}
    if not legacy:
        row["book"] = "shadow"
    state = {"positions": {KEY: row}, "seen": []}
    cfg = {**RH_CFG, "include_other_trades": True}
    agent.handle_event(ev("EXITED", "main", "m-out"), cfg, state, robinhood, rh,
                       lambda s: None)
    if legacy:
        assert [a["quantity"] for a in rh.placed] == ["2"]
        return
    assert rh.placed == [] and state["positions"][KEY]["qty"] == 2
    agent.handle_event(ev("EXITED", "other", "o-out"), cfg, state, robinhood, rh,
                       lambda s: None)
    (sell,) = rh.placed
    assert sell["legs"][0]["side"] == "sell" and sell["quantity"] == "2"
    assert state["positions"] == {}


@pytest.mark.parametrize("legacy", [False, True], ids=["booked", "legacy"])
def test_moomoo_sells_only_the_exiting_books_contracts(home, no_policy, notes,
                                                       monkeypatch, legacy):
    from test_never_sell_to_open import CODE, MM, MM_CFG
    sdk = MM(held=3)
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    key = "SPY|2099-12-18|752.0|C"
    row = {"option_id": CODE, "qty": 3, "opened_event": "m-in"}
    if not legacy:
        row["book"] = "main"
    state = {"positions": {key: row}, "seen": []}
    cfg = {**MM_CFG, "position_sync": False}
    agent.handle_event(ev("EXITED", "other", "o-out", expiry="2099-12-18"), cfg,
                       state, moomoo, None, lambda s: None)
    if legacy:
        assert [o["qty"] for o in sdk.placed] == [3]
        return
    assert sdk.placed == [] and state["positions"][key]["qty"] == 3
    agent.handle_event(ev("EXITED", "main", "m-out", expiry="2099-12-18"), cfg,
                       state, moomoo, None, lambda s: None)
    (sell,) = sdk.placed
    assert sell["trd_side"] == "SELL" and sell["qty"] == 3
    assert state["positions"] == {}


# ── "other trades" switched off: each row still waits for its own exit ─────

def test_other_trades_switched_off_still_holds_their_row_for_its_own_exit(
        home, no_policy):
    """With "other trades" off the AgentHC source keeps reading every track
    while an other-trades row is held (audit PR2, held_as_other), so its own
    exit still arrives: the main pick's exit for the same contract must not
    sell it, and its own exit does."""
    from sources import agenthc_day_trade_ideas as hc
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow",
                                 "track": "other"}}}
    off = {**LIVE, "include_other_trades": False}
    assert hc._feed_params(off, state)["track"] == "all"
    assert "no order placed" in run(b, state, ev("EXITED", "main", "m-out"), off)
    assert b.sells == [] and state["positions"][KEY]["qty"] == 2
    assert run(b, state, ev("EXITED", "other", "o-out"), off) == f"EXIT: {CONTRACT}"
    assert b.sells == [("o-out", 2, 2)] and state["positions"] == {}


@pytest.mark.parametrize("row,other", [
    ({"book": "shadow"}, True), ({"book": "main"}, False),
    ({"track": "other"}, True), ({"track": "swing"}, True),
    ({"book": "main", "track": "other"}, False),
    ({"book": "shadow", "track": "main"}, True), ({}, False)])
def test_the_source_and_the_agent_agree_on_a_rows_book(row, other):
    """One model: the feed reads every track for exactly the rows the agent
    holds for an "other trades" exit."""
    from sources import agenthc_day_trade_ideas as hc
    assert hc.held_as_other(row) is other
    assert (agent._row_book(row) == "shadow") is other


def test_main_pick_row_is_still_held_with_other_trades_off(home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "main"}}}
    off = {**LIVE, "include_other_trades": False}
    assert "no order placed" in run(b, state, ev("EXITED", "other", "o-out"), off)
    assert b.sells == []


def test_other_sources_send_any_track_so_their_rows_stay_per_book(
        home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow"}}}
    cfg = {**LIVE, "include_other_trades": False, "source": "generic_json_url"}
    assert "no order placed" in run(b, state, ev("EXITED", "main", "m-out"), cfg)
    assert b.sells == []


# ── two non-main tracks are both "other trades", but never one book ────────

def test_entry_records_the_raw_track(home, no_policy):
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", "swing", "s-in"))
    row = state["positions"][KEY]
    assert row["book"] == "shadow" and row["track"] == "swing"


def test_an_exit_of_another_non_main_track_does_not_sell(home, no_policy):
    b, state = Book(), {"positions": {}}
    run(b, state, ev("ENTERED", "other", "o-in"))
    out = run(b, state, ev("EXITED", "swing", "s-out"))
    assert "no order placed" in out and 'track "swing"' in out
    assert b.sells == [] and state["positions"][KEY]["qty"] == 2
    assert run(b, state, ev("EXITED", "other", "o-out")) == f"EXIT: {CONTRACT}"
    assert b.sells == [("o-out", 2, 2)]


def test_a_shadow_row_without_a_track_closes_on_any_non_main_exit(home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "shadow"}}}
    assert run(b, state, ev("EXITED", "swing", "s-out")) == f"EXIT: {CONTRACT}"


def test_a_track_that_contradicts_the_book_is_ignored(home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2, "book": "main",
                                 "track": "other"}}}
    assert "no order placed" in run(b, state, ev("EXITED", "other", "o-out"))
    assert run(b, state, ev("EXITED", "main", "m-out")) == f"EXIT: {CONTRACT}"


# ── the ledger check's record: never a stale book ──────────────────────────

def test_the_record_drops_an_old_positions_book(home):
    """agent_bought outlives each position in its contract: a later position
    with no book must not inherit the earlier one's."""
    state = {"positions": {}}
    old = {"option_id": "opt-1", "qty": 2, "book": "shadow", "track": "other",
           "opened_event": "o-in"}
    agent._remember_agent_row(state, KEY, old, "fake:", agent._now(), new_ids=["b-1"])
    rec = state["agent_bought"]["opt-1"]
    assert rec["book"] == "shadow" and rec["track"] == "other"
    new = {"option_id": "opt-1", "qty": 1, "opened_event": "n-in"}
    agent._remember_agent_row(state, KEY, new, "fake:", agent._now(), new_ids=["b-2"])
    assert "book" not in rec and "track" not in rec


@pytest.mark.parametrize("book,exit_track,sold", [
    ("shadow", "main", False), ("shadow", "other", True),
    ("main", "other", False), (None, "main", True), (None, "other", True)])
def test_ledger_check_readds_the_row_with_its_book(home, monkeypatch, book,
                                                   exit_track, sold):
    """The ledger lost a row the agent's own fills prove it bought: the
    ledger check re-adds it with the record's book, and only that book's
    exit sells it. A record with no book sells on either book's exit."""
    from test_exit_remainder import LATER, U1, FakeBroker, _bought_state, _loop, et
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    monkeypatch.setattr(agent, "_WATCH_SINCE", {})  # the run loop sets it
    st = _bought_state(held_key=LATER)
    if book:
        st["agent_bought"][U1].update(book=book, track="main" if book == "main"
                                      else "other")
    fills = [{"order_id": "buy-1", "side": "buy", "qty": 4,
              "at": et(10, 0, day=(2026, 11, 4)).isoformat()}]
    b = FakeBroker(held={U1: 4}, fills=fills)
    out = {"event": "EXITED", "ticker": "RIVN", "expiry": "2026-11-13",
           "strike": 13.5, "type": "C", "event_id": "x-out", "track": exit_track}
    day = (2026, 11, 6)
    saved = _loop(home, monkeypatch, st, b,
                  [et(14, 0, day=day), et(14, 0, day=day, s=30), et(14, 1, day=day)],
                  events={2: [out]})
    if sold:
        assert [(s["event_id"], s["qty"]) for s in b.sells] == [("x-out", 4)]
        assert saved["positions"] == {}
    else:
        assert b.sells == []
        row = saved["positions"][LATER]
        assert row["qty"] == 4 and row["book"] == book


# ── a buy that got no clear answer meets an exit ───────────────────────────

class LostThenSettles(Book):
    """Like the Alpaca and Robinhood adapters: the buy's answer is lost (a
    note, no row); an EXITED with no row settles the note, finds the buy at
    the broker, adopts it and sells it in the same call."""

    def execute(self, client, cfg, event, state):
        from brokers import order_guard
        pos = state["positions"].get(KEY)
        if event["event"] == "ENTERED" and not pos:
            order_guard.note(state, KEY, "buy", "k", self.qty, "opt-1",
                             event_id=event["event_id"])
            return False
        unc = order_guard.get(state, KEY)
        if event["event"] == "EXITED" and not pos and unc:
            order_guard.drop(state, KEY)
            state["positions"][KEY] = {"option_id": "opt-1", "qty": self.qty,
                                       "opened_event": unc["event_id"]}
            order_guard.adopted_buy(state, KEY, unc, "b-1")
        return super().execute(client, cfg, event, state)


@pytest.mark.parametrize("entry_track,exit_track", [("main", "other"),
                                                    ("other", "main")])
def test_the_other_books_exit_never_settles_and_sells_a_lost_buy(
        home, no_policy, notes, entry_track, exit_track):
    from brokers import order_guard
    b, state = LostThenSettles(), {"positions": {}}
    run(b, state, ev("ENTERED", entry_track, "e-in"))
    assert state["positions"] == {} and order_guard.get(state, KEY)
    out = run(b, state, ev("EXITED", exit_track, "x-out"))
    assert "no order placed" in out and "hasn't confirmed" in out
    assert b.sells == [] and state["positions"] == {}
    assert order_guard.get(state, KEY)             # left for the loop's reconcile


@pytest.mark.parametrize("legacy", [False, True], ids=["same-book", "legacy"])
def test_a_lost_buy_is_still_sold_by_its_own_books_exit(home, no_policy, notes,
                                                        legacy):
    b, state = LostThenSettles(), {"positions": {}}
    run(b, state, ev("ENTERED", "main", "m-in"))
    exit_track = "main"
    if legacy:  # no book remembered for it: any exit sells it, as before
        state.pop("entry_books")
        exit_track = "other"
    assert run(b, state, ev("EXITED", exit_track, "x-out")) == f"EXIT: {CONTRACT}"
    assert b.sells == [("x-out", 2, 2)] and state["positions"] == {}


def test_a_found_buy_is_booked_before_the_next_exit_is_matched(
        home, no_policy, notes):
    """A buy an adapter found at the broker on the loop's reconcile, not yet
    logged: the next exit signal first gives its row its book."""
    from brokers import order_guard
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2,
                                 "opened_event": "m-in"}},
             "entry_books": {"m-in": "main"}}
    order_guard.adopted_buy(state, KEY, {"event_id": "m-in", "at": agent._now()},
                            "b-1")
    out = run(b, state, ev("EXITED", "other", "o-out"))
    assert "no order placed" in out and b.sells == []
    assert state["positions"][KEY]["book"] == "main"


def test_a_retried_entry_stamps_its_own_row(home, no_policy):
    """The adapter wrote the row but its call raised: the retry of the same
    signal finds the row (buys nothing) and records its book then."""
    class Flaky(Book):
        def execute(self, client, cfg, event, state):
            changed = super().execute(client, cfg, event, state)
            if event["event"] == "ENTERED" and changed:
                raise RuntimeError("connection reset after the order")
            return changed

    b, state = Flaky(), {"positions": {}}
    with pytest.raises(RuntimeError):
        run(b, state, ev("ENTERED", "main", "m-in"))
    assert "book" not in state["positions"][KEY]
    run(b, state, ev("ENTERED", "main", "m-in"))
    assert state["positions"][KEY]["book"] == "main" and b.buys == [("m-in", 2)]
    assert "no order placed" in run(b, state, ev("EXITED", "other", "o-out"))


def test_a_retry_never_stamps_another_signals_row(home, no_policy):
    b = Book()
    state = {"positions": {KEY: {"option_id": "opt-1", "qty": 2,
                                 "opened_event": "old-in"}}}
    run(b, state, ev("ENTERED", "main", "m-in"))
    assert "book" not in state["positions"][KEY]
