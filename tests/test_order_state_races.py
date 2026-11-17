"""Orders whose fate is still open when the agent has to act on them.

A cancel the broker accepted is not a dead order: Alpaca's pending_cancel,
or a Robinhood order still "queued"/"confirmed" a moment after its cancel,
can still fill. Booking it as "never filled", buying the remainder again on
top of it, or spending an exit signal on it leaves contracts in the account
that the ledger doesn't list and nothing ever sells. These tests drive the
real adapters against fake brokers whose cancels take effect late. Zero
network."""

import time

import pytest

import agent
import broker_events
from brokers import alpaca, order_guard, robinhood
from conftest import make_event
from test_order_once import (ALP_CFG, KEY, OPT, RH_CFG, FakeAlpaca, FakeRobinhood,
                             Feed, _log, _mcp)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(alpaca, "_CONFIRM_WAIT_S", 0)
    monkeypatch.setattr(alpaca, "_CANCEL_RECHECK_S", 0, raising=False)
    monkeypatch.setattr(robinhood, "_SETTLE_WAIT_S", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(robinhood, "_in_opening_blackout", lambda now=None: False)


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "notify", lambda cfg, msg, **kw: sent.append(msg))
    monkeypatch.setattr(agent, "_problems_logged", set())
    return sent


# ── Alpaca: a buy left in pending_cancel ────────────────────────────────────

class SlowCancelAlpaca(FakeAlpaca):
    """Buys don't fill in time; their cancel is accepted (204) but the
    order stays pending_cancel until the test settles it."""

    def post(self, url, **kw):
        resp = super().post(url, **kw)
        order = self.orders[-1] if self.orders else None
        if order and order["side"] == "buy":
            order.update(status="new", filled_qty="0")
        return resp

    def delete(self, url, **kw):
        order = self._find(id=url.rsplit("/", 1)[-1])
        if order and order["status"] not in ("filled", "canceled"):
            order["status"] = "pending_cancel"
        return super().delete(url, **kw)


def _alpaca_feed():
    state = {"positions": {}, "seen": []}
    return Feed(alpaca, alpaca.client(ALP_CFG), ALP_CFG, state), state


def test_alpaca_pending_cancel_buy_that_fills_late_is_managed(
        home, no_policy, notes, monkeypatch):
    """THE BUG: the buy's cancel was accepted but the order still read
    pending_cancel, so the agent booked "never filled" and forgot it. It
    then filled: real contracts with no ledger row, no exit, no expiry
    guard. Now the order is kept until Alpaca says filled or dead; no
    second buy goes out meanwhile, its exit waits, and the late fill is
    taken over, logged as the entry and sold by that exit."""
    api = SlowCancelAlpaca(monkeypatch)
    feed, state = _alpaca_feed()
    buy, sell = make_event(), make_event(event="EXITED", event_id="x-1")
    feed.poll(buy)
    assert buy["event_id"] in feed.seen and not feed.errors
    assert api.orders[0]["status"] == "pending_cancel"
    assert KEY not in state["positions"]
    assert order_guard.get(state, KEY)["order_id"] == "a-1"
    assert [r["action"] for r in _log(home)] == ["order_failed"]   # not "never filled"
    assert "may still fill" in notes[0]
    # a repeat signal for the contract never buys on top of it
    feed.poll(buy, make_event(event_id="test-2"))
    assert len(api.posts) == 1
    # its exit signal waits for it
    feed.poll(sell)
    assert sell["event_id"] not in feed.seen and len(api.posts) == 1
    # the cancel loses the race: it fills
    api.orders[0].update(status="filled", filled_qty="2")
    feed.poll(sell)
    assert sell["event_id"] in feed.seen
    assert [o["side"] for o in api.orders] == ["buy", "sell"]
    assert api.held() == 0 and state["positions"] == {}
    assert order_guard.get(state, KEY) is None
    # (the second row: test-2 skipped because the first buy was still open)
    assert [r["action"] for r in _log(home)] == ["order_failed", "order_failed",
                                                 "entry", "exit"]


def test_alpaca_pending_cancel_buy_that_dies_is_dropped(home, no_policy, notes,
                                                        monkeypatch):
    """The cancel goes through with nothing filled: the note is dropped, the
    owner is told it never filled, and a later signal may buy again."""
    api = SlowCancelAlpaca(monkeypatch)
    feed, state = _alpaca_feed()
    feed.poll(make_event())
    api.orders[0]["status"] = "canceled"
    feed.poll()
    assert order_guard.get(state, KEY) is None and state["positions"] == {}
    assert [r["action"] for r in _log(home)] == ["order_failed"]
    assert api.held() == 0
    feed.poll(make_event(event_id="test-2"))
    assert len(api.posts) == 2


def test_alpaca_pending_cancel_partial_fill_is_adopted(home, no_policy, notes,
                                                       monkeypatch):
    api = SlowCancelAlpaca(monkeypatch)
    feed, state = _alpaca_feed()
    feed.poll(make_event())
    api.orders[0].update(status="canceled", filled_qty="1")
    feed.poll()
    assert state["positions"][KEY]["qty"] == 1
    assert state["positions"][KEY]["order_ids"] == ["a-1"]
    assert [r["action"] for r in _log(home)] == ["order_failed", "entry"]



def _other_buy():
    return make_event(strike=753.0, event_id="other-1")


@pytest.mark.parametrize("cap_key", ["max_open_positions", "max_entries_per_day"])
def test_alpaca_pending_cancel_buy_holds_its_cap_slot(home, no_policy, notes,
                                                      monkeypatch, cap_key):
    """A buy still cancelling isn't a ledger row or an entry row yet, but it
    may fill: with a cap of 1 it holds the slot, so a buy of another
    contract can't take it and leave the account over the cap once the
    first one fills late."""
    api = SlowCancelAlpaca(monkeypatch)
    state = {"positions": {}, "seen": []}
    feed = Feed(alpaca, alpaca.client(ALP_CFG), dict(ALP_CFG, **{cap_key: 1}), state)
    feed.poll(make_event())
    assert order_guard.get(state, KEY)["order_id"] == "a-1"
    feed.poll(_other_buy())
    assert len(api.posts) == 1, "a second buy went out past the cap"
    skip = {"max_open_positions": "skip_position_cap",
            "max_entries_per_day": "skip_daily_cap"}[cap_key]
    assert _log(home)[-1]["action"] == skip
    # once it settles unfilled the slot is free again
    api.orders[0]["status"] = "canceled"
    feed.poll(make_event(strike=753.0, event_id="other-2"))
    assert len(api.posts) == 2


def test_practice_cap_ignores_a_real_buy_still_settling(home):
    state = {"positions": {}}
    order_guard.note(state, KEY, "buy", None, 2, OPT, order_id="a-1")
    assert agent._buys_settling(state) == [KEY]
    state["positions"][KEY] = {"qty": 0, "option_id": OPT}  # Robinhood-style row
    assert agent._buys_settling(state) == []


def _aged_cancelling_note(state):
    rec = order_guard.note(state, KEY, "buy", None, 2, OPT, order_id="a-1",
                           contract="SPY 2026-07-10 $752 CALL", event_id="test-1")
    rec["at"] = time.time() - order_guard.GIVE_UP_S - 60
    return rec


def test_alpaca_cancelling_buy_past_give_up_is_still_read(monkeypatch):
    """Past GIVE_UP_S a cancelling buy with an order id is read once more:
    a late fill is still taken over, not dropped unmanaged."""
    state = {"positions": {}}
    _aged_cancelling_note(state)
    monkeypatch.setattr(alpaca, "_order_state", lambda cfg, oid: ("filled", 2))
    assert alpaca._settle_unconfirmed({}, state, KEY) == "adopted"
    assert state["positions"][KEY]["qty"] == 2


def test_alpaca_cancelling_buy_given_up_is_never_silent(monkeypatch):
    state = {"positions": {}}
    _aged_cancelling_note(state)
    monkeypatch.setattr(alpaca, "_order_state", lambda cfg, oid: ("", 0))
    broker_events.begin()
    assert alpaca._settle_unconfirmed({}, state, KEY) == "clear"
    events = broker_events.drain()
    assert order_guard.get(state, KEY) is None and state["positions"] == {}
    assert [e["kind"] for e in events] == ["order_failed"]
    assert "check your alpaca dashboard" in events[0]["message"].lower()


_REAL_CONFIRM_FILL = alpaca._confirm_fill


def _confirm(monkeypatch, after_cancel, side="buy", delete_status=204):
    states = iter([("accepted", 0)] * alpaca._CONFIRM_TRIES + list(after_cancel))
    last = [None]

    def read(cfg, oid):
        last[0] = next(states, last[0])
        return last[0]
    monkeypatch.setattr(alpaca, "_order_state", read)
    monkeypatch.setattr(alpaca.requests, "delete",
                        lambda url, **kw: type("R", (), {"status_code": delete_status})())
    broker_events.begin()
    got = _REAL_CONFIRM_FILL({}, "o1", 2, side=side)
    return got, broker_events.drain()


def test_alpaca_buy_still_cancelling_is_unknown_not_unfilled(monkeypatch):
    got, events = _confirm(monkeypatch, [("pending_cancel", 0)])
    assert got is None and events == []


def test_alpaca_buy_cancel_that_lands_on_a_recheck_settles(monkeypatch):
    assert _confirm(monkeypatch, [("pending_cancel", 0), ("canceled", 1)])[0] == 1
    assert _confirm(monkeypatch, [("pending_cancel", 0), ("", 0),
                                  ("filled", 2)])[0] == 2


def test_alpaca_buy_whose_cancel_is_refused_while_working_is_unknown(monkeypatch):
    """Alpaca refused the cancel (422) and the order still works: it may
    fill or die, so reconcile settles it — the full qty is not booked (a
    ledger row with nothing behind it if it dies)."""
    got, events = _confirm(monkeypatch, [("pending_cancel", 0)], delete_status=422)
    assert got is None and events == []
    got, _ = _confirm(monkeypatch, [("accepted", 0), ("canceled", 0)],
                      delete_status=422)
    assert got == 0
    # unreadable after a refused cancel: the optimistic fallback, said aloud
    got, events = _confirm(monkeypatch, [("", 0)], delete_status=422)
    assert got == 2 and [e["kind"] for e in events] == ["order_failed"]


def test_alpaca_sell_pending_cancel_is_unchanged(monkeypatch):
    """Exits keep their behaviour (B7 owns that path): 0 sold, re-sold next
    loop against what the account still holds."""
    assert _confirm(monkeypatch, [("pending_cancel", 0)], side="sell")[0] == 0


# ── Robinhood: blackout-limit buy converted to market ───────────────────────

class SlowCancelRH:
    """Robinhood's MCP for reconcile(): order states the test sets; cancel
    requests are accepted but change nothing until the test says so."""

    def __init__(self, state="queued", processed=0):
        self.orders = {"A": {"id": "A", "state": state,
                             "processed_quantity": str(processed)}}
        self.placed, self.cancels = [], []
        self.last_error = None

    def call_tool(self, name, args):
        if name == "get_option_orders":
            oid = args.get("order_id")
            rows = [self.orders[oid]] if oid in self.orders else list(self.orders.values())
            return _mcp({"data": {"orders": rows}})
        if name == "cancel_option_order":
            self.cancels.append(args["order_id"])
            return _mcp({})
        if name == "place_option_order":
            self.placed.append(args)
            oid = f"M{len(self.placed)}"
            self.orders[oid] = {"id": oid, "state": "queued", "processed_quantity": "0"}
            return _mcp({"data": {"id": oid}})
        if name == "get_option_quotes":
            return _mcp({"data": {"results": [{"ask_price": "1.00",
                                               "bid_price": "0.95"}]}})
        return _mcp({})


def _limit_buy_state(requested=5, age_s=60):
    return {"positions": {KEY: {"option_id": OPT, "qty": 0, "opened_event": "e-1",
                                "pending": {"order_id": "A", "side": "buy",
                                            "requested": requested, "filled": 0,
                                            "was_limit": True,
                                            "placed_at": time.time() - age_s}}}}


def _reconcile(rh, state):
    robinhood.reconcile(rh, {"robinhood_account": "1"}, state, lambda s: None)


def test_robinhood_convert_waits_until_the_limit_is_dead(monkeypatch):
    """THE BUG: at 9:35 reconcile cancelled the ask+5% limit and bought the
    remainder at market while the limit still read queued: if it filled
    too, the account held twice the configured size. Nothing is re-placed
    until the limit is dead; then the remainder goes out once."""
    rh = SlowCancelRH()
    state = _limit_buy_state()
    _reconcile(rh, state)
    assert rh.cancels == ["A"] and rh.placed == []
    assert state["positions"][KEY]["pending"]["order_id"] == "A"
    _reconcile(rh, state)                                # still queued
    assert rh.placed == []
    rh.orders["A"].update(state="cancelled", processed_quantity="2")
    _reconcile(rh, state)
    (args,) = rh.placed
    assert args["type"] == "market" and args["quantity"] == "3"
    pos = state["positions"][KEY]
    assert pos["qty"] == 2 and pos["pending"]["order_id"] == "M1"
    assert pos["pending"]["requested"] == 3
    _reconcile(rh, state)
    assert len(rh.placed) == 1


@pytest.mark.parametrize("after", [("partially_filled", 2), (None, 0)],
                         ids=["partially-filled", "unreadable"])
def test_robinhood_convert_never_replaces_on_a_live_or_unknown_order(monkeypatch,
                                                                    after):
    rh = SlowCancelRH()
    st = iter([("confirmed", 0), after])
    monkeypatch.setattr(robinhood, "_order_status", lambda rh_, a, o: next(st))
    state = _limit_buy_state()
    _reconcile(rh, state)
    assert rh.placed == []
    assert state["positions"][KEY]["pending"]["order_id"] == "A"


def test_robinhood_limit_that_fills_after_its_cancel_is_finalized(monkeypatch):
    rh = SlowCancelRH()
    state = _limit_buy_state()
    _reconcile(rh, state)
    rh.orders["A"].update(state="filled", processed_quantity="5")
    _reconcile(rh, state)
    assert rh.placed == []
    assert state["positions"][KEY]["qty"] == 5
    assert "pending" not in state["positions"][KEY]


def test_robinhood_expired_order_is_not_settled_while_it_can_fill(monkeypatch):
    """The 30-minute give-up has the same gap: a working order is only
    settled once its cancel has taken effect."""
    rh = SlowCancelRH(state="confirmed")
    state = _limit_buy_state(age_s=robinhood._PENDING_GIVEUP_S + 60)
    _reconcile(rh, state)
    assert KEY in state["positions"] and state["positions"][KEY]["pending"]
    rh.orders["A"].update(state="filled", processed_quantity="5")
    _reconcile(rh, state)
    assert rh.placed == [] and state["positions"][KEY]["qty"] == 5


# ── Robinhood: exit signal while the entry buy is still working ─────────────

def _exit_on_pending(rh, monkeypatch, cancel_ok):
    monkeypatch.setattr(robinhood, "_cancel_order",
                        lambda rh_, a, o: rh_.cancels.append(o) or cancel_ok)
    state = _limit_buy_state(requested=3)
    ev = make_event(event="EXITED", event_id="x-1")
    return state, ev


@pytest.mark.parametrize("cancel_ok", [True, False], ids=["cancel-pending",
                                                         "cancel-refused"])
def test_robinhood_fast_exit_keeps_the_row_while_the_buy_can_fill(monkeypatch,
                                                                  cancel_ok):
    """THE BUG: the exit cancelled the working buy, ignored whether the
    cancel took, and deleted the row with 0 filled — the exit signal was
    spent. If the buy then filled, the long was unmanaged. Now the row
    stays and the exit stays owed (raises: handle_event retries it)."""
    rh = SlowCancelRH(state="confirmed")
    state, ev = _exit_on_pending(rh, monkeypatch, cancel_ok)
    with pytest.raises(RuntimeError, match="exit waits"):
        robinhood.execute(rh, {"robinhood_account": "1"}, ev, state)
    pos = state["positions"][KEY]
    assert pos["pending"]["order_id"] == "A" and pos["pending"]["exit_cancel"]
    assert rh.placed == []
    # reconcile never converts a buy an exit is cancelling
    _reconcile(rh, state)
    assert rh.placed == [] and state["positions"][KEY]["pending"]["order_id"] == "A"
    # the buy fills 2 of 3 and dies: the retried exit sells those 2
    rh.orders["A"].update(state="cancelled", processed_quantity="2")
    monkeypatch.setattr(robinhood, "_sell_holding", lambda rh_, cfg, oid: (2, 0))
    assert robinhood.execute(rh, {"robinhood_account": "1"}, ev, state)
    (args,) = rh.placed
    assert args["legs"][0]["side"] == "sell" and args["quantity"] == "2"


def test_robinhood_fast_exit_after_the_buy_filled_sells_it(monkeypatch):
    """The cancel is refused because the buy already filled: that is a
    known outcome, so the exit sells what filled right away."""
    rh = SlowCancelRH(state="filled", processed=3)
    state, ev = _exit_on_pending(rh, monkeypatch, cancel_ok=False)
    monkeypatch.setattr(robinhood, "_sell_holding", lambda rh_, cfg, oid: (3, 0))
    assert robinhood.execute(rh, {"robinhood_account": "1"}, ev, state)
    (args,) = rh.placed
    assert args["legs"][0]["side"] == "sell" and args["quantity"] == "3"


def test_robinhood_fast_exit_on_a_working_buy_is_not_logged_as_an_exit(
        home, no_policy, notes):
    """Through the agent: the exit signal stays unseen (retried) and no
    phantom 'exit' row is written while the buy can still fill."""
    rh = FakeRobinhood()
    rh.orders.append({"id": "rh-1", "ref_id": "r", "state": "confirmed",
                      "type": "limit", "quantity": "2.00000",
                      "processed_quantity": "0.00000", "created_at": "",
                      "legs": [{"option_id": OPT, "side": "buy",
                                "position_effect": "open"}]})
    state = {"positions": {KEY: {"option_id": OPT, "qty": 0, "opened_event": "test-1",
                                 "pending": {"order_id": "rh-1", "side": "buy",
                                             "requested": 2, "filled": 0,
                                             "was_limit": False,
                                             "placed_at": time.time()}}},
             "seen": []}
    feed = Feed(robinhood, rh, RH_CFG, state)
    sell = make_event(event="EXITED", event_id="x-1")
    feed.poll(sell)
    assert sell["event_id"] not in feed.seen
    assert KEY in state["positions"]
    assert not [r for r in _log(home) if r.get("action") == "exit"]
