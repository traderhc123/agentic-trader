"""moomoo: a buy whose place_order gets no clear answer is tracked, never
sent again, and adopted when it turns up; every buy carries an id derived
from its signal (the order remark), so a signal handled again after a crash
never sends a second buy (audit PR8)."""

import time

import pytest

import broker_events
from brokers import moomoo, order_guard

KEY = "SPY|2026-07-10|752.0|C"
CODE = "US.SPY260710C752000"


class _DF:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def iloc(self):
        return self._rows


class FakeOpenD:
    """moomoo-api stand-in with an order book. place_order `mode`:
    "ok"      accepted, answered;
    "timeout" the order reaches moomoo, OpenD answers (-1, "Timeout");
    "raise"   the order reaches moomoo, the call raises (dropped socket);
    "reject"  OpenD turns it down (nothing reaches moomoo)."""
    RET_OK = 0

    class TrdEnv:
        SIMULATE, REAL = "SIM", "REAL"

    class TrdSide:
        BUY, SELL = "BUY", "SELL"

    class OrderType:
        NORMAL = "NORMAL"

    class TrdMarket:
        US = "US"

    class SecurityFirm:
        FUTUINC = "FUTUINC"

    def __init__(self, mode="ok", status="SUBMITTED", held=0):
        self.mode, self.status, self.held = mode, status, held
        self.sent = []        # every place_order request
        self.book = []        # the orders that reached moomoo
        self.orders_ok = True  # can order_list_query be read?
        self.queries = []

    def OpenSecTradeContext(self, **kw):
        sdk = self

        class Ctx:
            def place_order(self, **kw):
                sdk.sent.append(kw)
                if sdk.mode == "reject":
                    return -1, "insufficient buying power"
                oid = f"mm-{len(sdk.book) + 1}"
                sdk.book.append({
                    "order_id": oid, "code": kw["code"],
                    "trd_side": "TrdSide." + kw["trd_side"],
                    "qty": float(kw["qty"]), "dealt_qty": 0.0,
                    "order_status": sdk.status, "remark": kw.get("remark", "")})
                if sdk.mode == "timeout":
                    return -1, "Timeout"
                if sdk.mode == "raise":
                    raise ConnectionError("OpenD connection dropped")
                return sdk.RET_OK, _DF([{"order_id": oid}])

            def order_list_query(self, **kw):
                sdk.queries.append(kw)
                if not sdk.orders_ok:
                    return -1, "disconnected"
                rows = [o for o in sdk.book
                        if (not kw.get("order_id") or o["order_id"] == kw["order_id"])
                        and (not kw.get("code") or o["code"] == kw["code"])]
                if not kw.get("code") and not kw.get("order_id"):
                    rows = [o for o in rows
                            if o["order_status"] not in moomoo._MM_DONE]
                return sdk.RET_OK, _DF(rows)

            def position_list_query(self, **kw):
                return sdk.RET_OK, _DF([{"code": CODE, "qty": float(sdk.held)}]
                                       if sdk.held else [])

            def close(self):
                pass
        return Ctx()

    def OpenQuoteContext(self, **kw):
        class Q:
            def get_market_snapshot(self, codes):
                return 0, _DF([{"ask_price": 1.5, "bid_price": 1.4,
                                "last_price": 1.45}])

            def close(self):
                pass
        return Q()


CFG = {"broker": "moomoo", "moomoo_paper": True, "contracts_per_trade": 2}


def _ev(event="ENTERED", event_id="ENTERED|SPY|2026-07-10|752.0|C|t1"):
    return {"event": event, "ticker": "SPY", "expiry": "2026-07-10",
            "strike": 752.0, "type": "C", "event_id": event_id}


@pytest.fixture
def opend(monkeypatch):
    sdk = FakeOpenD()
    monkeypatch.setattr(moomoo, "_sdk", lambda: sdk)
    monkeypatch.setattr(moomoo, "_LIMIT_WAITS_S", ())
    return sdk


@pytest.fixture
def reports(monkeypatch):
    got = []
    monkeypatch.setattr(broker_events, "report",
                        lambda kind, msg, **kw: got.append((kind, msg, kw)))
    return got


@pytest.mark.parametrize("mode", ["timeout", "raise"])
def test_buy_without_a_clear_answer_is_tracked_not_called_rejected(
        opend, reports, mode):
    opend.mode = mode
    state = {"positions": {}}
    # the pre-buy check reads the (empty) list; the lookup after the lost
    # answer can't: the order is remembered, not re-sent
    real = opend.OpenSecTradeContext

    def ctx_after_send(**kw):
        ctx = real(**kw)
        if opend.sent:
            opend.orders_ok = False
        return ctx
    opend.OpenSecTradeContext = ctx_after_send
    assert moomoo.execute(None, CFG, _ev(), state) is False
    assert len(opend.sent) == 1
    note = order_guard.get(state, KEY)
    assert note and note["side"] == "buy" and note["qty"] == 2
    assert note["key"] == opend.sent[0]["remark"]
    assert state["positions"] == {}
    kinds = [(k, kw.get("placed")) for k, _m, kw in reports]
    assert kinds == [("order_failed", True)]
    assert "rejected" not in reports[0][1]
    assert "NOT send this buy again" in reports[0][1]


def test_explicit_rejection_stays_a_clean_failure(opend, reports):
    opend.mode = "reject"
    state = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(), state) is False
    assert order_guard.unconfirmed(state) == {}
    assert reports and reports[-1][0] == "order_failed"
    assert "rejected" in reports[-1][1] and not reports[-1][2].get("placed")


def test_lost_answer_but_order_listed_is_adopted_at_once(opend, reports):
    opend.mode = "timeout"
    state = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(), state) is True
    row = state["positions"][KEY]
    assert row["qty"] == 2 and row["order_ids"] == ["mm-1"]
    assert row["option_id"] == CODE
    assert "mm-1" in state["agent_order_ids"]
    assert order_guard.unconfirmed(state) == {}
    assert len(opend.sent) == 1


def test_reconcile_adopts_the_buy_once_moomoo_lists_it(opend, reports):
    opend.mode = "timeout"
    state = {"positions": {}}
    opend.orders_ok = True
    hidden = []

    real = opend.OpenSecTradeContext

    def ctx(**kw):  # the order shows up only on a later loop
        c = real(**kw)
        if opend.sent and not hidden:
            hidden.extend(opend.book)
            opend.book.clear()
        return c
    opend.OpenSecTradeContext = ctx
    assert moomoo.execute(None, CFG, _ev(), state) is False
    assert order_guard.get(state, KEY)
    opend.OpenSecTradeContext = real
    opend.book.extend(hidden)
    opend.book[0]["order_status"] = "FILLED_ALL"
    opend.book[0]["dealt_qty"] = 2.0
    saved = []
    moomoo.reconcile(moomoo.client(CFG), CFG, state, saved.append)
    assert saved, "the adopted position must be saved"
    assert state["positions"][KEY]["qty"] == 2
    assert state["positions"][KEY]["order_ids"] == ["mm-1"]
    assert order_guard.unconfirmed(state) == {}
    queued = order_guard.take_adopted(state)
    assert [q["key"] for q in queued] == [KEY] and queued[0]["order_id"] == "mm-1"
    assert len(opend.sent) == 1


def test_reconcile_never_adopts_an_order_without_the_agents_remark(opend, reports,
                                                                  monkeypatch):
    state = {"positions": {}}
    order_guard.note(state, KEY, "buy", "the-agents-remark", 2, CODE,
                     event_id="e1", contract="SPY")
    # the owner's own buy of the same contract and size, placed in the app
    opend.book.append({"order_id": "owner-1", "code": CODE, "trd_side": "TrdSide.BUY",
                       "qty": 2.0, "dealt_qty": 2.0, "order_status": "FILLED_ALL",
                       "remark": ""})
    # ...and one under the remark but for another size
    opend.book.append({"order_id": "x-2", "code": CODE, "trd_side": "TrdSide.BUY",
                       "qty": 5.0, "dealt_qty": 5.0, "order_status": "FILLED_ALL",
                       "remark": "the-agents-remark"})
    moomoo.reconcile(moomoo.client(CFG), CFG, state, lambda s: None)
    assert state["positions"] == {}
    assert order_guard.get(state, KEY)  # still looked for (it may show up late)
    # long after: it never showed up — forgotten, never bought
    state[order_guard.STATE_KEY][KEY]["at"] = time.time() - order_guard.BUY_KEEP_S - 5
    moomoo.reconcile(moomoo.client(CFG), CFG, state, lambda s: None)
    assert order_guard.unconfirmed(state) == {} and state["positions"] == {}
    assert opend.sent == []


def test_replayed_signal_after_a_crash_sends_no_second_buy(opend, reports):
    s1 = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(), s1) is True
    assert opend.sent[0].get("remark"), "every buy carries the signal's id"
    s2 = {"positions": {}}  # state.json as it was before the crash
    assert moomoo.execute(None, CFG, _ev(), s2) is True
    assert len(opend.sent) == 1, "a replayed ENTERED placed a second buy"
    assert s2["positions"][KEY]["order_ids"] == ["mm-1"]


def test_another_signal_for_the_same_contract_still_buys(opend, reports):
    s1 = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(event_id="sig-1"), s1)
    s2 = {"positions": {}}  # the first position is gone; a new signal
    assert moomoo.execute(None, CFG, _ev(event_id="sig-2"), s2)
    assert len(opend.sent) == 2
    assert opend.sent[0]["remark"] != opend.sent[1]["remark"]


def test_replay_of_a_signal_already_booked_and_sold_buys_nothing(opend, reports):
    s1 = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(), s1)
    s1["positions"].clear()  # sold since; its order id stays remembered
    assert moomoo.execute(None, CFG, _ev(), s1) is False
    assert len(opend.sent) == 1 and s1["positions"] == {}


def test_buy_is_held_back_while_the_order_list_cant_be_read(opend, reports):
    opend.orders_ok = False
    state = {"positions": {}}
    assert moomoo.execute(None, CFG, _ev(), state) is False
    assert opend.sent == [] and state["positions"] == {}
    assert reports and reports[-1][0] == "order_failed"


def test_no_second_buy_while_an_earlier_one_is_unconfirmed(opend, reports):
    state = {"positions": {}}
    order_guard.note(state, KEY, "buy", "k", 2, CODE, event_id="e0")
    opend.orders_ok = False
    assert moomoo.execute(None, CFG, _ev(event_id="e9"), state) is False
    assert opend.sent == []
    assert reports[-1][0] == "order_failed" and reports[-1][2].get("placed")


def test_exit_waits_while_its_buy_is_unconfirmed(opend, reports):
    state = {"positions": {}}
    order_guard.note(state, KEY, "buy", "k", 2, CODE, event_id="e0")
    opend.orders_ok = False
    with pytest.raises(RuntimeError):
        moomoo.execute(None, CFG, _ev(event="EXITED", event_id="x1"), state)
    assert opend.sent == []


def test_unclear_sell_is_not_noted(opend, reports):
    # sells are safe already: a retry re-reads what the account holds
    opend.mode, opend.held = "timeout", 2
    state = {"positions": {KEY: {"option_id": CODE, "qty": 2}}}
    assert moomoo.execute(None, CFG, _ev(event="EXITED", event_id="x1"), state) \
        is False
    assert order_guard.unconfirmed(state) == {}
    assert "remark" not in opend.sent[0]
